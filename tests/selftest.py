"""End-to-end self-test for the Dependency Alert API.

Runs entirely on the Python standard library so it works inside the slim
Docker container as well as on a host.  Point it at a running server:

    python tests/selftest.py            # default http://127.0.0.1:8000
    BASE_URL=http://host:8000 python tests/selftest.py

Exits non-zero on any failed check.  Covers graph validation/replacement,
report sequence semantics, suppression rules, recovery reclassification and
key concurrent interleavings.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}")
        print(f"  FAIL  {name} {detail}")


def request(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def reset() -> None:
    status, _ = request("POST", "/admin/reset")
    assert status == 200, status


# ---------------------------------------------------------------------------
# Test graphs
# ---------------------------------------------------------------------------

# A -> B -> D, A -> C -> D, plus X -> Y  (edges stored as "downstream deps on upstream")
GRAPH_V1 = {
    "version": "v1",
    "services": [
        {"name": "A", "dependencies": ["B", "C"]},
        {"name": "B", "dependencies": ["D"]},
        {"name": "C", "dependencies": ["D"]},
        {"name": "D", "dependencies": []},
        {"name": "X", "dependencies": ["Y"]},
        {"name": "Y", "dependencies": []},
    ],
}

# v2: same A/B/C/D topology (states preserved), Y removed, Z added (X->Z)
GRAPH_V2 = {
    "version": "v2",
    "services": [
        {"name": "A", "dependencies": ["B", "C"]},
        {"name": "B", "dependencies": ["D"]},
        {"name": "C", "dependencies": ["D"]},
        {"name": "D", "dependencies": []},
        {"name": "X", "dependencies": ["Z"]},
        {"name": "Z", "dependencies": []},
    ],
}

CYCLIC_GRAPH = {
    "services": [
        {"name": "A", "dependencies": ["B"]},
        {"name": "B", "dependencies": ["C"]},
        {"name": "C", "dependencies": ["A"]},
    ]
}

UNKNOWN_REF_GRAPH_BAD = {
    "services": [{"name": "A", "dependencies": ["GHOST"]}]
}


def test_graph_validation() -> None:
    print("[T1] graph validation & atomic replacement")
    reset()

    status, body = request("GET", "/alerts")
    check("alerts before any graph -> 409", status == 409, str(body))

    status, body = request("POST", "/graph", CYCLIC_GRAPH)
    check("cyclic graph rejected 422", status == 422 and body["error"] == "invalid_graph", str(body))

    status, body = request("POST", "/graph", UNKNOWN_REF_GRAPH_BAD)
    check("unknown reference rejected 422", status == 422, str(body))

    status, body = request("POST", "/graph", {
        "services": [
            {"name": "A", "dependencies": ["B"]},
            {"name": "A", "dependencies": []},
            {"name": "B", "dependencies": []},
        ]
    })
    check("duplicate service rejected 422", status == 422, str(body))

    status, body = request("POST", "/graph", {
        "services": [
            {"name": "A", "dependencies": ["B", "B"]},
            {"name": "B", "dependencies": []},
        ]
    })
    check("duplicate edge rejected 422", status == 422, str(body))

    status, body = request("POST", "/graph", {
        "services": [{"name": "A", "dependencies": ["A"]}]
    })
    check("self dependency rejected 422", status == 422, str(body))

    status, body = request("POST", "/graph", GRAPH_V1)
    check("valid graph accepted", status == 200 and body["revision"] == 1, str(body))
    check("edge count correct", body["edge_count"] == 5, str(body))

    # Rejected replacement must keep the old version.
    status, _ = request("POST", "/graph", CYCLIC_GRAPH)
    status, body = request("GET", "/graph")
    check(
        "old graph retained after rejected replacement",
        status == 200 and body["revision"] == 1 and body["version"] == "v1",
        str(body),
    )

    status, body = request("POST", "/graph", GRAPH_V1)
    check("re-commit increments revision", status == 200 and body["revision"] == 2, str(body))


def test_report_semantics() -> None:
    print("[T2] report sequence semantics")
    reset()
    request("POST", "/graph", GRAPH_V1)

    status, body = request("POST", "/reports", {"service": "NOPE", "seq": 1, "status": "fault"})
    check("unknown service report -> 404", status == 404, str(body))

    status, body = request("POST", "/reports", {"service": "D", "seq": 0, "status": "fault"})
    check("non-positive seq -> 422", status == 422, str(body))

    status, body = request("POST", "/reports", {"service": "D", "seq": 1, "status": "fault"})
    check("first fault applied", status == 200 and body["applied"] is True, str(body))

    status, body = request("POST", "/reports", {"service": "D", "seq": 1, "status": "fault"})
    check(
        "same seq same content idempotent retry",
        status == 200 and body["applied"] is False and body["duplicate"] is True,
        str(body),
    )

    status, body = request("POST", "/reports", {"service": "D", "seq": 1, "status": "recover"})
    check("same seq different content -> 409", status == 409, str(body))

    # State must not have been changed by the conflicting retry.
    status, alerts = request("GET", "/alerts")
    d_down = "D" in alerts["down_services"]
    check("conflict did not change state", d_down, str(alerts))

    status, body = request("POST", "/reports", {"service": "D", "seq": 3, "status": "recover"})
    check("higher seq applies (gaps allowed)", status == 200 and body["applied"] is True, str(body))

    status, body = request("POST", "/reports", {"service": "D", "seq": 2, "status": "fault"})
    check("late lower seq -> 409", status == 409, str(body))

    status, alerts = request("GET", "/alerts")
    check("late report never rolls state back", "D" not in alerts["down_services"], str(alerts))

    status, body = request("POST", "/reports", {"service": "D", "seq": 3, "status": "fault"})
    check("retry of old seq after newer state -> 409", status == 409, str(body))


def test_suppression() -> None:
    print("[T3] suppression: only all-faulty paths suppress; healthy/unreported cut paths")
    reset()
    request("POST", "/graph", GRAPH_V1)

    def fault(svc: str, seq: int):
        return request("POST", "/reports", {"service": svc, "seq": seq, "status": "fault"})

    fault("D", 1)
    fault("B", 1)
    fault("A", 1)

    status, alerts = request("GET", "/alerts")
    actionable = [f["service"] for f in alerts["actionable_faults"]]
    suppressed = {f["service"]: f for f in alerts["suppressed_faults"]}
    check("root D is actionable", actionable == ["D"], str(alerts))
    check("B suppressed by D", "B" in suppressed and suppressed["B"]["root_causes"] == ["D"], str(alerts))
    check(
        "A suppressed only via all-down B-D path (C healthy cuts the other branch)",
        "A" in suppressed and suppressed["A"]["root_causes"] == ["D"],
        str(alerts),
    )
    check(
        "A path list only contains all-down paths",
        suppressed["A"]["paths"] == [["A", "B", "D"]],
        str(suppressed["A"]),
    )

    # C becomes faulty too: second all-down path opens.
    fault("C", 1)
    _, alerts = request("GET", "/alerts")
    suppressed = {f["service"]: f for f in alerts["suppressed_faults"]}
    check(
        "A gains second all-down path A-C-D",
        suppressed["A"]["paths"] == [["A", "B", "D"], ["A", "C", "D"]],
        str(suppressed["A"]),
    )

    # B recovers: A must stay suppressed via C, B becomes actionable if still down?
    # B is up now -> A's B-branch is cut, C-branch still all down.
    request("POST", "/reports", {"service": "B", "seq": 2, "status": "recover"})
    _, alerts = request("GET", "/alerts")
    actionable = [f["service"] for f in alerts["actionable_faults"]]
    suppressed = {f["service"]: f for f in alerts["suppressed_faults"]}
    check("after B recovery: D remains actionable", actionable == ["D"], str(alerts))
    check(
        "A remains suppressed via C only; B removed from suppressed (healthy)",
        "A" in suppressed and "B" not in suppressed
        and suppressed["A"]["paths"] == [["A", "C", "D"]],
        str(alerts),
    )

    # D recovers: C is still faulty with no faulty upstream, so C becomes the
    # root (actionable) and A stays suppressed via the all-down A->C path --
    # reclassification happens immediately, no resend needed.
    request("POST", "/reports", {"service": "D", "seq": 2, "status": "recover"})
    _, alerts = request("GET", "/alerts")
    actionable = sorted(f["service"] for f in alerts["actionable_faults"])
    suppressed = {f["service"]: f for f in alerts["suppressed_faults"]}
    check(
        "D recovery immediately makes C the new actionable root",
        actionable == ["C"],
        str(alerts),
    )
    check(
        "A reclassified to suppressed-by-C without resend",
        "A" in suppressed and suppressed["A"]["root_causes"] == ["C"]
        and suppressed["A"]["paths"] == [["A", "C"]],
        str(alerts),
    )

    # C recovers too: A is still faulty but no all-down path remains at all,
    # so it must immediately become actionable.
    request("POST", "/reports", {"service": "C", "seq": 2, "status": "recover"})
    _, alerts = request("GET", "/alerts")
    actionable = sorted(f["service"] for f in alerts["actionable_faults"])
    check(
        "once all all-down paths are cut, A becomes actionable without resend",
        actionable == ["A"],
        str(alerts),
    )
    check("no suppressed faults remain", alerts["suppressed_faults"] == [], str(alerts))
    check("healthy list includes recovered B, C and D",
          {"B", "C", "D"}.issubset(set(alerts["healthy_services"])), str(alerts))

    # Never-reported nodes cut paths.  Fresh graph: X down, Y never reported.
    reset()
    request("POST", "/graph", GRAPH_V1)
    fault("X", 1)
    _, alerts = request("GET", "/alerts")
    actionable = [f["service"] for f in alerts["actionable_faults"]]
    check("unreported upstream Y means X stays actionable", actionable == ["X"], str(alerts))
    check("Y listed as unreported", "Y" in alerts["unreported_services"], str(alerts))

    # A single isolated fault with no upstream is always actionable.
    fault("Y", 1)
    _, alerts = request("GET", "/alerts")
    actionable = sorted(f["service"] for f in alerts["actionable_faults"])
    suppressed = {f["service"] for f in alerts["suppressed_faults"]}
    check("Y root actionable", "Y" in actionable, str(alerts))
    check("X now suppressed by Y", "X" in suppressed, str(alerts))


def test_graph_replace_preserves_state() -> None:
    print("[T4] graph replacement preserves same-named states; dropped node state hidden")
    reset()
    request("POST", "/graph", GRAPH_V1)
    request("POST", "/reports", {"service": "D", "seq": 1, "status": "fault"})
    request("POST", "/reports", {"service": "B", "seq": 7, "status": "fault"})
    request("POST", "/reports", {"service": "Y", "seq": 1, "status": "fault"})

    status, body = request("POST", "/graph", GRAPH_V2)
    check("v2 accepted", status == 200 and body["revision"] == 2, str(body))
    check(
        "same-named service states in the new graph are preserved",
        set(body["preserved_state"]) == {"B", "D"},
        str(body),
    )

    _, alerts = request("GET", "/alerts")
    check("D still down after swap", "D" in alerts["down_services"], str(alerts))
    check("B still down after swap", "B" in alerts["down_services"], str(alerts))
    check("dropped-node state not exposed", "Y" not in alerts["down_services"], str(alerts))
    check("new node Z reported as unreported", "Z" in alerts["unreported_services"], str(alerts))

    # Sequence numbers preserved as well: seq 7 already used for B.
    status, body = request("POST", "/reports", {"service": "B", "seq": 7, "status": "fault"})
    check("seq history preserved across swap (idempotent)", body["duplicate"] is True, str(body))
    status, _ = request("POST", "/reports", {"service": "B", "seq": 6, "status": "recover"})
    check("stale seq after swap still rejected", status == 409)

    # And if Y comes back in a later version, its old state resurfaces.
    status, _ = request("POST", "/graph", GRAPH_V1)
    _, alerts = request("GET", "/alerts")
    check("re-added Y shows its preserved down state", "Y" in alerts["down_services"], str(alerts))


def test_concurrency() -> None:
    print("[T5] concurrent interleavings")
    reset()
    request("POST", "/graph", GRAPH_V1)

    # 5a: parallel reports with distinct seqs for one service.  Under real
    # at processing time must be rejected (409) and state may never roll back.
    seqs = [2, 5, 3, 8, 1]
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = [
            pool.submit(request, "POST", "/reports",
                        {"service": "A", "seq": s, "status": "fault" if s % 2 else "recover"})
            for s in seqs
        ]
        results = [f.result() for f in futs]
    ok = [b for st, b in results if st == 200]
    stale_pairs = [(st, b) for st, b in results if st == 409 and "stale" in b["detail"]]
    stale_seqs = [s for s in seqs if any(f"seq {s} <" in b["detail"] for _, b in stale_pairs)]
    applied_seqs = sorted(b["seq"] for b in ok if b["applied"])
    check(
        "applied seqs form a strictly increasing chain",
        all(a < b for a, b in zip(applied_seqs, applied_seqs[1:])),
        str(results),
    )
    check(
        "a report rejected as stale is one that arrived out of order",
        all(s < max(applied_seqs) for s in stale_seqs),
        str(results),
    )
    check("no unexpected error codes", all(st in (200, 409) for st, _ in results), str(results))
    _, alerts = request("GET", "/alerts")
    # Final state matches the content of the highest-seq report (seq 8 -> recover),
    # proving a late low seq never rolled the state backwards.
    check(
        "state never rolled back: A reflects highest seq (seq 8 recover -> up)",
        "A" in alerts["healthy_services"],
        str(alerts),
    )

    # 5b: burst of identical (service, seq, content): one apply, rest idempotent.
    reset()
    request("POST", "/graph", GRAPH_V1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(request, "POST", "/reports",
                            {"service": "A", "seq": 1, "status": "fault"})
                for _ in range(8)]
        burst = [f.result() for f in futs]
    applied = sum(1 for st, b in burst if st == 200 and b["applied"])
    dupes = sum(1 for st, b in burst if st == 200 and b["duplicate"])
    check("identical concurrent retries: exactly 1 apply", applied == 1, str(burst))
    check("identical concurrent retries: 7 idempotent", dupes == 7, str(burst))

    # 5c: same seq, different content in parallel: one wins, others 409, no crash.
    reset()
    request("POST", "/graph", GRAPH_V1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [
            pool.submit(request, "POST", "/reports",
                        {"service": "A", "seq": 1, "status": "fault"}),
            pool.submit(request, "POST", "/reports",
                        {"service": "A", "seq": 1, "status": "recover"}),
        ]
        race = [f.result() for f in futs]
    ok = [b for st, b in race if st == 200]
    conf = [st for st, _ in race if st == 409]
    check("same-seq conflicting race: one accepted, one 409",
          len(ok) == 1 and len(conf) == 1, str(race))

    # 5d: parallel graph replacements: every observed revision is a fully
    # committed graph; no mixed node/edge sets.
    alt_graphs = [
        {"version": f"g{i}", "services": [
            {"name": f"S{j}", "dependencies": [f"S{j-1}"] if j else []}
            for j in range(i + 1)
        ]}
        for i in range(1, 4)
    ]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(request, "POST", "/graph", g) for g in alt_graphs for _ in range(2)]
        replacements = [f.result() for f in futs]
    check("all parallel replacements accepted", all(st == 200 for st, _ in replacements),
          str(replacements))
    status, graph = request("GET", "/graph")
    names = sorted(s["name"] for s in graph["services"])
    expected = sorted(
        s["name"]
        for s in next(g for g in alt_graphs if g["version"] == graph["version"])["services"]
    )
    check("final graph internally consistent", names == expected, str(graph))

    # 5e: replace + query interleaving: every alerts snapshot must be
    # internally consistent with exactly one committed revision.
    with ThreadPoolExecutor(max_workers=6) as pool:
        writer = [pool.submit(request, "POST", "/graph", g) for g in alt_graphs for _ in range(3)]
        reader = [pool.submit(request, "GET", "/alerts") for _ in range(30)]
        for f in writer:
            f.result()
        reads = [f.result() for f in reader]

    consistent = True
    for st, body in reads:
        if st != 200:
            consistent = False
            break
        nodes = set(body["services"])
        classified = (
            set(body["down_services"])
            | set(body["unreported_services"])
            | set(body["healthy_services"])
        )
        action = {f["service"] for f in body["actionable_faults"]}
        supp = {f["service"] for f in body["suppressed_faults"]}
        if classified != nodes:
            consistent = False  # every node belongs to exactly one class
        if action | supp != set(body["down_services"]) or action & supp:
            consistent = False  # every down node is either actionable or suppressed
    check(
        "every concurrent read is a consistent single-revision snapshot",
        consistent,
        str(reads[:2]),
    )


# ---------------------------------------------------------------------------
# Upgrade batches
# ---------------------------------------------------------------------------


def healthy(svc: str, seq: int = 1):
    return request("POST", "/reports", {"service": svc, "seq": seq, "status": "recover"})


def fault(svc: str, seq: int):
    return request("POST", "/reports", {"service": svc, "seq": seq, "status": "fault"})


def create_batch(services: list[str]):
    return request("POST", "/upgrade-batches", {"services": services})


def claim(batch_id: int, svc: str):
    return request("POST", f"/upgrade-batches/{batch_id}/steps/{svc}/claim")


def complete(batch_id: int, svc: str, token: str, result: str):
    return request(
        "POST",
        f"/upgrade-batches/{batch_id}/steps/{svc}/complete",
        {"claim_token": token, "result": result},
    )


def test_batch_creation() -> None:
    print("[T6] batch creation & validation (atomic, no partial batches)")
    reset()

    status, body = create_batch(["A"])
    check("batch without graph -> 409 no_graph", status == 409 and body["error"] == "no_graph", str(body))

    request("POST", "/graph", GRAPH_V1)

    status, body = create_batch(["NOPE"])
    check("unknown service -> 404", status == 404 and body["error"] == "unknown_service", str(body))

    status, body = create_batch(["D", "GHOST"])
    check("one unknown among valid -> 404", status == 404, str(body))

    status, body = create_batch(["A", "A"])
    check("duplicate services -> 422 invalid_batch",
          status == 422 and body["error"] == "invalid_batch", str(body))

    status, body = create_batch([])
    check("empty service list -> 422", status == 422, str(body))

    status, body = request("GET", "/upgrade-batches")
    check("rejected creations leave no partial batch",
          status == 200 and body == [], str(body))

    status, body = create_batch(["D", "B", "A"])
    check("valid batch -> 201", status == 201, str(body))
    batch_id = body["batch_id"]
    steps = {s["service"]: s for s in body["steps"]}
    check("all steps pending",
          all(s["status"] == "pending" for s in steps.values()), str(body))
    check("graph revision recorded", body["graph_revision"] == 1, str(body))
    check("transitive upstreams computed",
          steps["A"]["upstreams_all"] == ["B", "C", "D"]
          and steps["B"]["upstreams_all"] == ["D"]
          and steps["D"]["upstreams_all"] == [], str(steps))
    check("in-batch upstreams computed",
          steps["A"]["upstreams_in_batch"] == ["B", "D"]
          and steps["B"]["upstreams_in_batch"] == ["D"]
          and steps["D"]["upstreams_in_batch"] == [], str(steps))

    status, body = request("GET", f"/upgrade-batches/{batch_id}")
    check("batch detail readable", status == 200 and len(body["steps"]) == 3, str(body))

    status, body = request("GET", "/upgrade-batches/9999")
    check("missing batch -> 404", status == 404 and body["error"] == "batch_not_found", str(body))

    status, body = claim(batch_id, "NOPE")
    check("claim of non-step service -> 404 step_not_found",
          status == 404 and body["error"] == "step_not_found", str(body))


def test_claim_preconditions() -> None:
    print("[T7] claim preconditions: health + in-batch order")
    reset()
    request("POST", "/graph", GRAPH_V1)

    _, batch = create_batch(["D", "B"])
    bid = batch["batch_id"]

    status, body = claim(bid, "D")
    check("unreported service -> 409 upstream_unhealthy",
          status == 409 and body["error"] == "upstream_unhealthy", str(body))

    fault("D", 1)
    status, body = claim(bid, "D")
    check("faulty service -> 409 upstream_unhealthy", status == 409, str(body))

    healthy("D", 2)
    status, d1 = claim(bid, "D")
    check("healthy root claimable", status == 200 and d1["claim_epoch"] == 1, str(d1))
    check("snapshot recorded", d1["health_snapshot"] == {"D": 2}, str(d1))

    status, body = claim(bid, "D")
    check("double claim -> 409 already_claimed",
          status == 409 and body["error"] == "already_claimed", str(body))

    status, body = claim(bid, "B")
    check("in-batch upstream not succeeded -> 409 upstream_not_upgraded",
          status == 409 and body["error"] == "upstream_not_upgraded", str(body))

    status, body = complete(bid, "D", d1["claim_token"], "success")
    check("claimed root completes", status == 200 and body["status"] == "succeeded", str(body))

    status, body = claim(bid, "B")
    check("downstream still needs its own health report",
          status == 409 and body["error"] == "upstream_unhealthy", str(body))
    healthy("B", 1)
    status, body = claim(bid, "B")
    check("downstream claimable once healthy and upstream succeeded",
          status == 200, str(body))

    # Out-of-batch upstream must be healthy too: batch [A] needs B, C, D up.
    reset()
    request("POST", "/graph", GRAPH_V1)
    _, batch = create_batch(["A"])
    bid = batch["batch_id"]
    healthy("A", 1)
    healthy("B", 1)
    healthy("C", 1)
    fault("D", 1)
    status, body = claim(bid, "A")
    check("out-of-batch upstream down -> 409 upstream_unhealthy",
          status == 409 and body["error"] == "upstream_unhealthy", str(body))
    healthy("D", 2)
    status, body = claim(bid, "A")
    check("all upstreams healthy -> claim ok", status == 200, str(body))
    check("snapshot covers service + all upstreams",
          body["health_snapshot"] == {"A": 1, "B": 1, "C": 1, "D": 2}, str(body))


def test_claim_order_and_success() -> None:
    print("[T8] dependency order + concurrent claim: exactly one winner")
    reset()
    request("POST", "/graph", GRAPH_V1)
    for svc in ("A", "B", "C", "D", "X", "Y"):
        healthy(svc, 1)

    _, batch = create_batch(["D", "B", "A"])
    bid = batch["batch_id"]

    status, body = claim(bid, "A")
    check("downstream blocked before upstreams succeed",
          status == 409 and body["error"] == "upstream_not_upgraded", str(body))

    status, d1 = claim(bid, "D")
    check("root D claimed", status == 200, str(d1))
    status, body = complete(bid, "D", d1["claim_token"], "success")
    check("D succeeded", status == 200 and body["status"] == "succeeded", str(body))

    status, body = claim(bid, "D")
    check("re-claim of succeeded step -> 409 already_succeeded",
          status == 409 and body["error"] == "already_succeeded", str(body))

    status, body = claim(bid, "A")
    check("A still blocked (B not done)",
          status == 409 and body["error"] == "upstream_not_upgraded", str(body))

    status, b1 = claim(bid, "B")
    check("B claimable after D", status == 200, str(b1))
    complete(bid, "B", b1["claim_token"], "success")
    status, a1 = claim(bid, "A")
    check("A claimable after B and D", status == 200, str(a1))
    complete(bid, "A", a1["claim_token"], "success")

    status, batch = request("GET", f"/upgrade-batches/{bid}")
    check("whole batch succeeded",
          all(s["status"] == "succeeded" for s in batch["steps"]), str(batch))

    # Concurrent claims of the same step: exactly one winner.
    _, batch = create_batch(["X"])
    bid = batch["batch_id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [f.result() for f in
                   [pool.submit(claim, bid, "X") for _ in range(8)]]
    wins = [b for st, b in results if st == 200]
    losers = [b for st, b in results if st == 409]
    check("concurrent claims: exactly one winner",
          len(wins) == 1 and len(losers) == 7, str(results))
    check("losers all already_claimed",
          all(b["error"] == "already_claimed" for b in losers), str(results))
    tokens = {w["claim_token"] for w in wins}
    status, body = complete(bid, "X", next(iter(tokens)), "success")
    check("winner can complete", status == 200 and body["status"] == "succeeded", str(body))


def test_health_snapshot_invalidation() -> None:
    print("[T9] fault+recovery after claim voids the success receipt")
    reset()
    request("POST", "/graph", GRAPH_V1)
    for svc in ("A", "B", "C", "D"):
        healthy(svc, 1)

    _, batch = create_batch(["D", "B"])
    bid = batch["batch_id"]

    status, d1 = claim(bid, "D")
    check("D claimed", status == 200, str(d1))

    # D faults and recovers while its upgrade is in flight.
    fault("D", 2)
    healthy("D", 3)

    status, body = complete(bid, "D", d1["claim_token"], "success")
    check("success receipt after fault+recovery -> 409 health_snapshot_invalid",
          status == 409 and body["error"] == "health_snapshot_invalid", str(body))

    status, batch = request("GET", f"/upgrade-batches/{bid}")
    step_d = next(s for s in batch["steps"] if s["service"] == "D")
    check("voided round falls back to failed (re-claimable)",
          step_d["status"] == "failed", str(batch))

    status, body = claim(bid, "B")
    check("downstream NOT unblocked by the voided receipt",
          status == 409 and body["error"] == "upstream_not_upgraded", str(body))

    status, d2 = claim(bid, "D")
    check("re-claim after voided round works", status == 200 and d2["claim_epoch"] == 2, str(d2))
    check("new snapshot uses the newer seq", d2["health_snapshot"] == {"D": 3}, str(d2))
    status, body = complete(bid, "D", d2["claim_token"], "success")
    check("fresh round succeeds", status == 200 and body["status"] == "succeeded", str(body))

    # Upstream (not the claimed service itself) moving also voids the round.
    status, b1 = claim(bid, "B")
    check("B claimed", status == 200, str(b1))
    fault("D", 4)
    healthy("D", 5)
    status, body = complete(bid, "B", b1["claim_token"], "success")
    check("upstream report during claim voids downstream receipt too",
          status == 409 and body["error"] == "health_snapshot_invalid", str(body))

    status, b2 = claim(bid, "B")
    check("B re-claimable", status == 200, str(b2))
    status, body = complete(bid, "B", b2["claim_token"], "success")
    check("B succeeds on a quiet round", status == 200, str(body))

    # A fault that is still active must also block re-claiming.
    _, batch = create_batch(["A"])
    bid2 = batch["batch_id"]
    status, a1 = claim(bid2, "A")
    check("A claimed", status == 200, str(a1))
    fault("C", 2)  # upstream of A goes down and stays down
    status, body = complete(bid2, "A", a1["claim_token"], "success")
    check("still-down upstream also voids the receipt",
          status == 409 and body["error"] == "health_snapshot_invalid", str(body))
    status, body = claim(bid2, "A")
    check("re-claim blocked while upstream still down",
          status == 409 and body["error"] == "upstream_unhealthy", str(body))
    healthy("C", 3)
    status, a2 = claim(bid2, "A")
    check("re-claim ok after upstream recovers", status == 200, str(a2))
    status, body = complete(bid2, "A", a2["claim_token"], "success")
    check("A finally succeeds", status == 200, str(body))


def test_late_receipts() -> None:
    print("[T10] failure -> re-claim; late receipts from old rounds rejected")
    reset()
    request("POST", "/graph", GRAPH_V1)
    healthy("X", 1)
    healthy("Y", 1)

    _, batch = create_batch(["X"])
    bid = batch["batch_id"]

    status, c1 = claim(bid, "X")
    check("round 1 claimed", status == 200 and c1["claim_epoch"] == 1, str(c1))
    status, body = complete(bid, "X", c1["claim_token"], "failure")
    check("failure reported", status == 200 and body["status"] == "failed", str(body))

    status, c2 = claim(bid, "X")
    check("re-claim after failure", status == 200 and c2["claim_epoch"] == 2, str(c2))
    check("new round gets a new token", c2["claim_token"] != c1["claim_token"], str(c2))

    status, body = complete(bid, "X", c1["claim_token"], "success")
    check("late success from round 1 -> 409 stale_claim_token",
          status == 409 and body["error"] == "stale_claim_token", str(body))

    status, batch = request("GET", f"/upgrade-batches/{bid}")
    step = batch["steps"][0]
    check("late receipt did not overwrite round 2 (still claimed)",
          step["status"] == "claimed" and step["claim_epoch"] == 2, str(batch))

    status, body = complete(bid, "X", c2["claim_token"], "success")
    check("round 2 receipt accepted", status == 200 and body["status"] == "succeeded", str(body))

    status, body = complete(bid, "X", c2["claim_token"], "success")
    check("replayed receipt after completion -> 409 stale_claim_token",
          status == 409 and body["error"] == "stale_claim_token", str(body))

    status, batch = request("GET", f"/upgrade-batches/{bid}")
    check("final state still succeeded", batch["steps"][0]["status"] == "succeeded", str(batch))

    # Concurrent re-claims after a failure: still exactly one winner.
    _, batch = create_batch(["Y"])
    bid2 = batch["batch_id"]
    _, y1 = claim(bid2, "Y")
    complete(bid2, "Y", y1["claim_token"], "failure")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [f.result() for f in
                   [pool.submit(claim, bid2, "Y") for _ in range(8)]]
    wins = [b for st, b in results if st == 200]
    check("concurrent re-claims after failure: exactly one winner",
          len(wins) == 1, str(results))
    epochs = {w["claim_epoch"] for w in wins}
    check("the winning re-claim is the next epoch", epochs == {2}, str(results))


def test_graph_swap_invalidates() -> None:
    print("[T11] graph replacement invalidates batches; history stays readable")
    reset()
    request("POST", "/graph", GRAPH_V1)
    for svc in ("A", "B", "C", "D", "X", "Y"):
        healthy(svc, 1)

    _, batch = create_batch(["D", "B"])
    bid = batch["batch_id"]
    _, d1 = claim(bid, "D")
    complete(bid, "D", d1["claim_token"], "success")
    _, b1 = claim(bid, "B")  # claimed but never completed

    status, body = request("POST", "/graph", GRAPH_V2)
    check("graph swap accepted", status == 200 and body["revision"] == 2, str(body))
    check("swap reports invalidated batches", body.get("invalidated_batches") == 1, str(body))

    status, body = claim(bid, "B")
    check("claim on invalidated batch -> 409 batch_invalidated",
          status == 409 and body["error"] == "batch_invalidated", str(body))

    status, body = complete(bid, "B", b1["claim_token"], "success")
    check("complete on invalidated batch -> 409 batch_invalidated",
          status == 409 and body["error"] == "batch_invalidated", str(body))

    status, batch = request("GET", f"/upgrade-batches/{bid}")
    check("invalidated batch still readable", status == 200, str(batch))
    steps = {s["service"]: s["status"] for s in batch["steps"]}
    check("completed step stays visible as succeeded",
          steps.get("D") == "succeeded" and steps.get("B") == "claimed", str(batch))
    check("batch marked invalidated", batch["status"] == "invalidated", str(batch))

    # A new batch on the new graph works normally.
    for svc in ("Z",):
        healthy(svc, 1)
    status, batch2 = create_batch(["X", "Z"])
    check("new batch on new graph", status == 201 and batch2["graph_revision"] == 2, str(batch2))
    bid2 = batch2["batch_id"]
    status, body = claim(bid2, "X")
    check("order enforced on new batch (Z first)",
          status == 409 and body["error"] == "upstream_not_upgraded", str(body))
    _, z1 = claim(bid2, "Z")
    complete(bid2, "Z", z1["claim_token"], "success")
    status, x1 = claim(bid2, "X")
    check("X claimable after Z on new graph", status == 200, str(x1))

    # A rejected graph replacement must NOT invalidate anything.
    _, batch3 = create_batch(["D"])
    bid3 = batch3["batch_id"]
    request("POST", "/graph", CYCLIC_GRAPH)
    status, body = claim(bid3, "D")
    check("rejected graph swap keeps batches active", status == 200, str(body))

    status, listing = request("GET", "/upgrade-batches")
    check("batch list shows all batches with progress",
          status == 200 and len(listing) == 3
          and {b["batch_id"] for b in listing} == {bid, bid2, bid3}
          and all("succeeded" in b and "step_count" in b for b in listing),
          str(listing))


def main() -> int:
    print(f"Self-test against {BASE_URL}\n")
    try:
        urllib.request.urlopen(BASE_URL + "/health", timeout=5)
    except Exception as exc:  # noqa: BLE001
        print(f"server not reachable: {exc}")
        return 2

    test_graph_validation()
    test_report_semantics()
    test_suppression()
    test_graph_replace_preserves_state()
    test_concurrency()
    test_batch_creation()
    test_claim_preconditions()
    test_claim_order_and_success()
    test_health_snapshot_invalidation()
    test_late_receipts()
    test_graph_swap_invalidates()

    print(f"\n{'-' * 60}\nPASS={PASS} FAIL={FAIL}")
    if FAIL:
        print("Failures:")
        for f in FAILURES:
            print(" -", f)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
