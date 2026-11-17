"""FastAPI application: dependency-alert API."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .database import close_connection, get_connection, init_db, write_tx
from .errors import (
    BatchInvalid,
    BatchNotFound,
    GraphError,
    NoGraph,
    ReportConflict,
    StepNotFound,
    UnknownService,
    UpgradeConflict,
)
from .graph_service import classify_alerts, get_graph, replace_graph
from .report_service import submit_report
from .schemas import (
    AlertsResponse,
    BatchCreatePayload,
    BatchResponse,
    BatchSummary,
    ClaimResponse,
    CompletePayload,
    CompleteResponse,
    GraphPayload,
    GraphResponse,
    ReplaceResponse,
    ReportPayload,
    ReportResponse,
)
from .upgrade_service import (
    claim_step,
    complete_step,
    create_batch,
    get_batch,
    list_batches,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Dependency Alert API",
    version="1.0.0",
    description=(
        "Service dependency graph + fault/recovery reports with monotonic "
        "sequence numbers and upstream-root-cause alert suppression."
    ),
    lifespan=lifespan,
)


@app.middleware("http")
async def _db_session(request: Request, call_next):
    # One SQLite connection per request (thread-local); always released.
    get_connection()
    try:
        return await call_next(request)
    finally:
        close_connection()


@app.exception_handler(GraphError)
async def _graph_error(_: Request, exc: GraphError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(ReportConflict)
async def _conflict(_: Request, exc: ReportConflict):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(UnknownService)
async def _unknown(_: Request, exc: UnknownService):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(NoGraph)
async def _no_graph(_: Request, exc: NoGraph):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(BatchNotFound)
async def _batch_not_found(_: Request, exc: BatchNotFound):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(StepNotFound)
async def _step_not_found(_: Request, exc: StepNotFound):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(BatchInvalid)
async def _batch_invalid(_: Request, exc: BatchInvalid):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(UpgradeConflict)
async def _upgrade_conflict(_: Request, exc: UpgradeConflict):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.post("/graph", response_model=ReplaceResponse, status_code=200)
def put_graph(payload: GraphPayload) -> ReplaceResponse:
    """Replace the **whole** dependency graph.

    Invalid payloads (cycles, unknown references, duplicate names/edges) are
    rejected and the previously committed graph is retained.  States of
    same-named services survive the replacement.
    """
    return ReplaceResponse(**replace_graph(payload))


@app.get("/graph", response_model=GraphResponse)
def read_graph() -> GraphResponse:
    """Return the currently committed graph."""
    return get_graph()


@app.post("/reports", response_model=ReportResponse, status_code=200)
def post_report(payload: ReportPayload) -> ReportResponse:
    """Report a service fault or recovery with a per-service increasing seq."""
    return ReportResponse(**submit_report(payload))


@app.get("/alerts", response_model=AlertsResponse)
def read_alerts() -> AlertsResponse:
    """Return actionable faults, suppressed faults and their upstream root causes."""
    return AlertsResponse(**classify_alerts())


# ---------------------------------------------------------------------------
# Upgrade batches
# ---------------------------------------------------------------------------


@app.post("/upgrade-batches", response_model=BatchResponse, status_code=201)
def post_upgrade_batch(payload: BatchCreatePayload) -> BatchResponse:
    """Create an upgrade batch from services of the **current** graph.

    One step per service is created atomically; if the payload is invalid
    (unknown/duplicate services, no committed graph) no partial batch is
    left behind.
    """
    return BatchResponse(**create_batch(payload))


@app.get("/upgrade-batches", response_model=list[BatchSummary])
def read_upgrade_batches() -> list[BatchSummary]:
    """List all upgrade batches (including invalidated ones)."""
    return [BatchSummary(**b) for b in list_batches()]


@app.get("/upgrade-batches/{batch_id}", response_model=BatchResponse)
def read_upgrade_batch(batch_id: int) -> BatchResponse:
    """Batch detail.  Completed steps stay visible after invalidation."""
    return BatchResponse(**get_batch(batch_id))


@app.post(
    "/upgrade-batches/{batch_id}/steps/{service}/claim",
    response_model=ClaimResponse,
)
def claim_upgrade_step(batch_id: int, service: str) -> ClaimResponse:
    """Claim a step.  Requires the batch to be active, the service and all of
    its upstreams reported healthy, and every in-batch upstream step succeeded.
    Concurrent claims of the same step yield exactly one winner."""
    return ClaimResponse(**claim_step(batch_id, service))


@app.post(
    "/upgrade-batches/{batch_id}/steps/{service}/complete",
    response_model=CompleteResponse,
)
def complete_upgrade_step(
    batch_id: int, service: str, payload: CompletePayload
) -> CompleteResponse:
    """Report success/failure for the current claim round (token required).

    A success receipt is rejected (and the round voided) if any related
    service accepted a newer report since the claim, even if it recovered
    again - such a receipt must not unblock downstream steps.  Late receipts
    from older rounds never overwrite a newer round."""
    return CompleteResponse(**complete_step(batch_id, service, payload))


@app.post("/admin/reset", status_code=200)
def admin_reset() -> dict:
    """Clear all data (test/operations helper)."""
    with write_tx() as conn:
        conn.execute("DELETE FROM upgrade_steps")
        conn.execute("DELETE FROM upgrade_batches")
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM service_state")
        conn.execute("DELETE FROM edges")
        conn.execute("DELETE FROM nodes")
        conn.execute("DELETE FROM graph_meta")
    return {"reset": True}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
