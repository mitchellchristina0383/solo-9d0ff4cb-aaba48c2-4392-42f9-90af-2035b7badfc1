"""Domain errors mapped to HTTP responses in main.py."""
from __future__ import annotations


class GraphError(Exception):
    """Graph payload is structurally invalid (unknown reference, cycle, ...)."""

    status_code = 422
    code = "invalid_graph"


class ReportConflict(Exception):
    """Same seq with different content, or stale (lower) seq."""

    status_code = 409
    code = "conflict"


class UnknownService(Exception):
    """Report references a service that is not in the current graph."""

    status_code = 404
    code = "unknown_service"


class NoGraph(Exception):
    """No dependency graph has been committed yet."""

    status_code = 409
    code = "no_graph"


class BatchNotFound(Exception):
    """Upgrade batch does not exist."""

    status_code = 404
    code = "batch_not_found"


class StepNotFound(Exception):
    """The service is not a step of the given upgrade batch."""

    status_code = 404
    code = "step_not_found"


class BatchInvalid(Exception):
    """Batch payload is invalid (empty/duplicate services, ...)."""

    status_code = 422
    code = "invalid_batch"


class UpgradeConflict(Exception):
    """A claim/complete precondition failed.  ``code`` refines the reason:

    * ``batch_invalidated``       - a newer graph revision superseded the batch
    * ``already_claimed``         - the step is currently claimed by someone else
    * ``already_succeeded``       - the step already finished successfully
    * ``upstream_not_upgraded``   - an in-batch upstream step is not succeeded yet
    * ``upstream_unhealthy``      - the service or an upstream is not reported healthy
    * ``stale_claim_token``       - late receipt from a superseded claim round
    * ``health_snapshot_invalid`` - a related service accepted a newer report
                                    since the claim, so the success receipt
                                    must not unblock downstream steps
    """

    status_code = 409

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
