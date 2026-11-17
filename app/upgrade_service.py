"""Upgrade batches: claim steps in dependency order and report the outcome.

A *batch* selects a set of services from the currently committed graph.  Each
service becomes one *step*.  A step may be claimed only when

* the batch is still active (its graph revision is the committed one),
* the service itself and **all** of its transitive upstreams are reported
  healthy (status ``up``), and
* every in-batch upstream step has already succeeded.

Claiming records a health snapshot ``{service: last_seq}`` of the service and
all of its upstreams.  If any of those services later accepts a report with a
higher sequence number, the snapshot is stale: a success receipt from that
claim round must not unblock downstream steps, even if the service recovered
again in the meantime.  The round is voided (step back to ``failed``) so it
can be re-claimed under the new health situation.

Every claim gets a fresh random token and a monotonically increasing epoch.
Completion requires the token of the *current* claim round, so a late receipt
from an older round can never overwrite a newer one.

All state changes run inside one serialized ``BEGIN IMMEDIATE`` transaction,
so a failed write never leaves half a batch or a phantom completion, and
concurrent claims of the same step yield exactly one winner.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

from .database import read_tx, write_tx
from .errors import (
    BatchInvalid,
    BatchNotFound,
    NoGraph,
    StepNotFound,
    UnknownService,
    UpgradeConflict,
)
from .schemas import BatchCreatePayload, CompletePayload

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _transitive_upstreams(
    start: str, upstream_map: dict[str, list[str]]
) -> list[str]:
    """All services ``start`` depends on, directly or transitively (sorted)."""
    seen: set[str] = set()
    stack = list(upstream_map.get(start, []))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(upstream_map.get(node, []))
    return sorted(seen)


def _load_current_graph(conn: sqlite3.Connection) -> tuple[int, dict[str, list[str]]]:
    """(revision, downstream -> direct upstreams) of the committed graph."""
    meta = conn.execute("SELECT revision FROM graph_meta WHERE id = 1").fetchone()
    if meta is None:
        raise NoGraph("no dependency graph has been committed yet")
    upstream_map: dict[str, list[str]] = {
        r["service"]: [] for r in conn.execute("SELECT service FROM nodes")
    }
    for r in conn.execute("SELECT upstream, downstream FROM edges"):
        upstream_map[r["downstream"]].append(r["upstream"])
    return meta["revision"], upstream_map


def _get_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM upgrade_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if row is None:
        raise BatchNotFound(f"upgrade batch {batch_id} does not exist")
    return row


def _get_step(conn: sqlite3.Connection, batch_id: int, service: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM upgrade_steps WHERE batch_id = ? AND service = ?",
        (batch_id, service),
    ).fetchone()
    if row is None:
        raise StepNotFound(
            f"service {service!r} is not a step of batch {batch_id}"
        )
    return row


def _require_active(batch: sqlite3.Row) -> None:
    if batch["status"] != "active":
        raise UpgradeConflict(
            "batch_invalidated",
            f"batch {batch['id']} was invalidated by a newer graph revision; "
            "its steps are read-only",
        )


def _step_info(row: sqlite3.Row) -> dict:
    return {
        "service": row["service"],
        "status": row["status"],
        "claim_epoch": row["claim_epoch"],
        "upstreams_all": json.loads(row["upstreams_all"]),
        "upstreams_in_batch": json.loads(row["upstreams_batch"]),
        "claimed_at": row["claimed_at"],
        "finished_at": row["finished_at"],
    }


def _batch_response(conn: sqlite3.Connection, batch: sqlite3.Row) -> dict:
    steps = conn.execute(
        "SELECT * FROM upgrade_steps WHERE batch_id = ? ORDER BY service",
        (batch["id"],),
    ).fetchall()
    return {
        "batch_id": batch["id"],
        "graph_revision": batch["graph_revision"],
        "status": batch["status"],
        "created_at": batch["created_at"],
        "steps": [_step_info(s) for s in steps],
    }


# ---------------------------------------------------------------------------
# Create / read
# ---------------------------------------------------------------------------


def create_batch(payload: BatchCreatePayload) -> dict:
    """Create a batch with one step per selected service.

    All validation happens before any INSERT and everything commits in one
    transaction, so a rejected batch leaves no partial rows behind.
    """
    services = [s.strip() for s in payload.services]
    if any(not s for s in services):
        raise BatchInvalid("service names must be non-empty")
    duplicates = sorted({s for s in services if services.count(s) > 1})
    if duplicates:
        raise BatchInvalid(f"duplicate services in batch: {duplicates}")

    with write_tx() as conn:
        revision, upstream_map = _load_current_graph(conn)

        unknown = [s for s in services if s not in upstream_map]
        if unknown:
            raise UnknownService(
                f"services not part of the current graph: {unknown}"
            )

        selected = set(services)
        cur = conn.execute(
            "INSERT INTO upgrade_batches (graph_revision, status) VALUES (?, 'active')",
            (revision,),
        )
        batch_id = cur.lastrowid
        for svc in services:
            all_ups = _transitive_upstreams(svc, upstream_map)
            in_batch = [u for u in all_ups if u in selected]
            conn.execute(
                "INSERT INTO upgrade_steps "
                "(batch_id, service, upstreams_all, upstreams_batch) "
                "VALUES (?, ?, ?, ?)",
                (batch_id, svc, json.dumps(all_ups), json.dumps(in_batch)),
            )
        batch = _get_batch(conn, batch_id)
        return _batch_response(conn, batch)


def list_batches() -> list[dict]:
    with read_tx() as conn:
        rows = conn.execute(
            "SELECT b.*, "
            "(SELECT COUNT(*) FROM upgrade_steps s WHERE s.batch_id = b.id) AS step_count, "
            "(SELECT COUNT(*) FROM upgrade_steps s WHERE s.batch_id = b.id "
            "  AND s.status = 'succeeded') AS succeeded "
            "FROM upgrade_batches b ORDER BY b.id"
        ).fetchall()
    return [
        {
            "batch_id": r["id"],
            "graph_revision": r["graph_revision"],
            "status": r["status"],
            "created_at": r["created_at"],
            "step_count": r["step_count"],
            "succeeded": r["succeeded"],
        }
        for r in rows
    ]


def get_batch(batch_id: int) -> dict:
    """Batch detail; completed steps stay visible even after invalidation."""
    with read_tx() as conn:
        batch = _get_batch(conn, batch_id)
        return _batch_response(conn, batch)


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


def claim_step(batch_id: int, service: str) -> dict:
    """Claim one step.  Exactly one concurrent claim wins; the losers get 409."""
    with write_tx() as conn:
        batch = _get_batch(conn, batch_id)
        _require_active(batch)
        step = _get_step(conn, batch_id, service)

        if step["status"] == "claimed":
            raise UpgradeConflict(
                "already_claimed",
                f"step {service!r} of batch {batch_id} is already claimed",
            )
        if step["status"] == "succeeded":
            raise UpgradeConflict(
                "already_succeeded",
                f"step {service!r} of batch {batch_id} already succeeded",
            )

        # Every in-batch upstream step must have succeeded first.
        ups_batch: list[str] = json.loads(step["upstreams_batch"])
        if ups_batch:
            marks = ",".join("?" * len(ups_batch))
            rows = conn.execute(
                f"SELECT service, status FROM upgrade_steps "
                f"WHERE batch_id = ? AND service IN ({marks})",
                (batch_id, *ups_batch),
            ).fetchall()
            not_done = sorted(r["service"] for r in rows if r["status"] != "succeeded")
            if not_done:
                raise UpgradeConflict(
                    "upstream_not_upgraded",
                    f"in-batch upstream steps not succeeded yet: {not_done}",
                )

        # The service itself and all of its upstreams must be reported healthy.
        ups_all: list[str] = json.loads(step["upstreams_all"])
        related = [service, *ups_all]
        marks = ",".join("?" * len(related))
        rows = conn.execute(
            f"SELECT service, status, last_seq FROM service_state "
            f"WHERE service IN ({marks})",
            related,
        ).fetchall()
        state = {r["service"]: r for r in rows}
        unhealthy = sorted(
            s for s in related
            if s not in state or state[s]["status"] != "up"
        )
        if unhealthy:
            raise UpgradeConflict(
                "upstream_unhealthy",
                f"service or upstream not reported healthy: {unhealthy}",
            )

        token = uuid.uuid4().hex
        epoch = step["claim_epoch"] + 1
        snapshot = {s: state[s]["last_seq"] for s in related}
        conn.execute(
            "UPDATE upgrade_steps SET status = 'claimed', claim_token = ?, "
            "claim_epoch = ?, claim_snapshot = ?, "
            f"claimed_at = {_NOW}, finished_at = NULL "
            "WHERE id = ?",
            (token, epoch, json.dumps(snapshot), step["id"]),
        )

    return {
        "batch_id": batch_id,
        "service": service,
        "claim_token": token,
        "claim_epoch": epoch,
        "health_snapshot": snapshot,
    }


# ---------------------------------------------------------------------------
# Complete
# ---------------------------------------------------------------------------


def complete_step(batch_id: int, service: str, payload: CompletePayload) -> dict:
    """Report the outcome of the current claim round.

    A receipt is accepted only with the token of the current round, so a late
    receipt from an older round can never overwrite a newer one.  A success
    receipt is honoured only if no related service accepted a newer report
    since the claim; otherwise the round is voided (step back to ``failed``)
    and the receipt is rejected with 409 so it cannot unblock downstream steps.
    """
    snapshot_broken: list[str] = []
    with write_tx() as conn:
        batch = _get_batch(conn, batch_id)
        _require_active(batch)
        step = _get_step(conn, batch_id, service)

        if step["status"] != "claimed" or step["claim_token"] != payload.claim_token:
            raise UpgradeConflict(
                "stale_claim_token",
                f"receipt does not match the current claim round of step "
                f"{service!r} in batch {batch_id}; late receipts from older "
                "rounds are rejected",
            )

        if payload.result == "failure":
            conn.execute(
                "UPDATE upgrade_steps SET status = 'failed', claim_token = NULL, "
                f"claim_snapshot = NULL, finished_at = {_NOW} WHERE id = ?",
                (step["id"],),
            )
            final_status = "failed"
        else:
            snapshot: dict[str, int] = json.loads(step["claim_snapshot"])
            related = list(snapshot)
            marks = ",".join("?" * len(related))
            rows = conn.execute(
                f"SELECT service, last_seq FROM service_state "
                f"WHERE service IN ({marks})",
                related,
            ).fetchall()
            current = {r["service"]: r["last_seq"] for r in rows}
            snapshot_broken = sorted(
                s for s in related if current.get(s) != snapshot[s]
            )
            if snapshot_broken:
                # A related service accepted a newer report since the claim.
                # Even if it recovered again, this success receipt must not
                # unblock downstream steps: void the round, allow re-claim.
                conn.execute(
                    "UPDATE upgrade_steps SET status = 'failed', "
                    "claim_token = NULL, claim_snapshot = NULL, "
                    f"finished_at = {_NOW} WHERE id = ?",
                    (step["id"],),
                )
                final_status = "failed"
            else:
                conn.execute(
                    "UPDATE upgrade_steps SET status = 'succeeded', "
                    "claim_token = NULL, claim_snapshot = NULL, "
                    f"finished_at = {_NOW} WHERE id = ?",
                    (step["id"],),
                )
                final_status = "succeeded"

    if snapshot_broken:
        raise UpgradeConflict(
            "health_snapshot_invalid",
            f"related services accepted newer reports since the claim: "
            f"{snapshot_broken}; the success receipt cannot unblock downstream "
            "steps and the claim round was voided - re-claim the step",
        )
    return {
        "batch_id": batch_id,
        "service": service,
        "status": final_status,
        "claim_epoch": step["claim_epoch"],
    }


# ---------------------------------------------------------------------------
# Graph replacement hook
# ---------------------------------------------------------------------------


def invalidate_active_batches(conn: sqlite3.Connection) -> int:
    """Mark every active batch as invalidated (called inside the graph
    replacement transaction, so the swap and the invalidation commit
    atomically).  Completed steps stay readable via ``get_batch``."""
    cur = conn.execute(
        "UPDATE upgrade_batches SET status = 'invalidated' WHERE status = 'active'"
    )
    return cur.rowcount
