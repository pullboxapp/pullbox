"""Bounded server-owned approvals for a single series' managed comic files."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

type PropagationField = Literal[
    "title", "publisher", "language", "format", "sort_title", "year_start", "volume"
]


class PropagationReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    fields: frozenset[PropagationField] = Field(min_length=1, max_length=7)


class PropagationApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    issue_ids: list[Annotated[int, Field(gt=0, lt=2**63, strict=True)]] | None = Field(
        default=None, min_length=1, max_length=10000
    )


class PropagationRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class PropagationTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    issue_id: int = Field(gt=0, strict=True)
    label: str = Field(max_length=500)
    review_key: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class PropagationPlan(PropagationReview):
    series_id: int = Field(gt=0, strict=True)
    series_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    mode: Literal["preview", "write"]
    targets: tuple[PropagationTarget, ...] = Field(min_length=1, max_length=10000)
