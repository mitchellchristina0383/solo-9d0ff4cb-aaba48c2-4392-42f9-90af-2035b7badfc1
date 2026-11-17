"""Pydantic request/response models."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Status = Literal["fault", "recover"]


class ServiceIn(BaseModel):
    name: str = Field(..., min_length=1, description="Unique service name")
    dependencies: list[str] = Field(
        default_factory=list,
        description="Names of upstream services this service depends on",
    )


class GraphPayload(BaseModel):
    version: Optional[str] = Field(
        default=None, description="Optional caller-supplied graph version label"
    )
    services: list[ServiceIn] = Field(
        ..., min_length=1, description="Full dependency graph (whole-version replace)"
    )


class ReportPayload(BaseModel):
    service: str = Field(..., min_length=1)
    seq: int = Field(..., gt=0, description="Strictly positive, increasing per service")
    status: Status


class RootCause(BaseModel):
    service: str
    paths: list[list[str]]


class ActionableFault(BaseModel):
    service: str
    last_seq: int


class SuppressedFault(BaseModel):
    service: str
    last_seq: int
    root_causes: list[str]
    paths: list[list[str]]


class AlertsResponse(BaseModel):
    graph_revision: int
    graph_version: Optional[str]
    services: list[str]
    actionable_faults: list[ActionableFault]
    suppressed_faults: list[SuppressedFault]
    down_services: list[str]
    unreported_services: list[str]
    healthy_services: list[str]


class GraphResponse(BaseModel):
    revision: int
    version: Optional[str]
    committed_at: Optional[str]
    services: list[ServiceIn]


class ReportResponse(BaseModel):
    applied: bool
    duplicate: bool
    service: str
    seq: int
    status: Status
    current_status: Literal["up", "down"]
    last_seq: int


class ReplaceResponse(BaseModel):
    accepted: bool
    revision: int
    version: Optional[str]
    service_count: int
    edge_count: int
    preserved_state: list[str]
    invalidated_batches: int = 0


# ---------------------------------------------------------------------------
# Upgrade batches
# ---------------------------------------------------------------------------

StepStatus = Literal["pending", "claimed", "succeeded", "failed"]
BatchStatus = Literal["active", "invalidated"]


class BatchCreatePayload(BaseModel):
    services: list[str] = Field(
        ..., min_length=1,
        description="Services to upgrade, selected from the current graph",
    )


class StepInfo(BaseModel):
    service: str
    status: StepStatus
    claim_epoch: int
    upstreams_all: list[str]
    upstreams_in_batch: list[str]
    claimed_at: Optional[str] = None
    finished_at: Optional[str] = None


class BatchResponse(BaseModel):
    batch_id: int
    graph_revision: int
    status: BatchStatus
    created_at: str
    steps: list[StepInfo]


class BatchSummary(BaseModel):
    batch_id: int
    graph_revision: int
    status: BatchStatus
    created_at: str
    step_count: int
    succeeded: int


class ClaimResponse(BaseModel):
    batch_id: int
    service: str
    claim_token: str
    claim_epoch: int
    health_snapshot: dict[str, int]


class CompletePayload(BaseModel):
    claim_token: str = Field(..., min_length=1)
    result: Literal["success", "failure"]


class CompleteResponse(BaseModel):
    batch_id: int
    service: str
    status: StepStatus
    claim_epoch: int
