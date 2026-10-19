"""Bounded dashboard commands. Validation errors are sanitized by the API."""

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sealed.discovery.models import Request as DiscoveryRequest


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Spec(Command):
    quantity: Literal[1] = 1
    details: str = Field(min_length=1, max_length=2000)


class Source(Command):
    kind: Literal["manual", "discovery"] = "manual"
    urls: list[str] = Field(default_factory=list, max_length=10)
    discovery_id: str | None = Field(default=None, max_length=80)
    candidate_id: str | None = Field(default=None, max_length=80)


class Vendor(Command):
    name: str = Field(min_length=1, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=30)
    mode: Literal["sim", "live"] = "sim"
    source: Source = Field(default_factory=Source)

    @model_validator(mode="after")
    def contact(self):
        if self.email is not None and not re.fullmatch(r"[^\s@<>;,]+@[^\s@<>;,]+\.[^\s@<>;,]+", self.email):
            raise ValueError("valid_email_required")
        if not self.email and not self.phone:
            raise ValueError("contact_required")
        if self.phone and not re.fullmatch(r"\+?[\d ()-]{7,30}", self.phone):
            raise ValueError("valid_phone_required")
        return self


class CreateJob(Command):
    item: str = Field(min_length=1, max_length=200)
    spec: Spec
    deadline: date
    currency: Literal["USD", "CAD", "EUR", "GBP"] = "USD"
    target: float = Field(gt=0, le=1e9)
    ceiling: float = Field(gt=0, le=1e9, exclude=True, repr=False)
    max_rounds: Literal[2] = 2
    zip: str = Field(default="", pattern=r"^(\d{5})?$")
    vendors: list[Vendor] = Field(min_length=1, max_length=5)
    buyer: str = Field(default="Buyer", min_length=1, max_length=80)
    timeline: str = Field(default="before the requested deadline", min_length=1, max_length=120)

    @model_validator(mode="after")
    def valid_terms(self):
        if self.ceiling <= self.target or self.deadline < date.today():
            raise ValueError("invalid_terms")
        contacts = [(v.email or v.phone or "").casefold() for v in self.vendors]
        if len(set(contacts)) != len(contacts):
            raise ValueError("duplicate_vendor")
        return self

    @field_validator("item", "buyer", "timeline")
    @classmethod
    def single_line(cls, value):
        if any(c in value for c in "\r\n\x00") or not value.strip():
            raise ValueError("single_line_required")
        return value.strip()


class Start(Command):
    seed: int = 42
    allow_model_requests: bool = False
    allow_dispatch: bool = False
    allow_calls: bool = False


class Approve(Command):
    proposal_id: str = Field(min_length=1, max_length=80)
    vendor_id: str = Field(min_length=1, max_length=80)
    price_total: float = Field(gt=0)
    currency: Literal["USD", "CAD", "EUR", "GBP"]
    delivery_date: date | None


class Reject(Command):
    proposal_id: str = Field(min_length=1, max_length=80)
    reason: str = Field(default="", max_length=500)


class Cancel(Command):
    reason: str = Field(default="", max_length=500)


class Advance(Command):
    job_id: str = Field(min_length=1, max_length=80)
    steps: int = Field(default=1, ge=-1, le=100)

    @field_validator("steps")
    @classmethod
    def nonzero(cls, value):
        if value == 0:
            raise ValueError("steps_required")
        return value


class Restore(Command):
    ceiling: float = Field(gt=0, le=1e9, exclude=True, repr=False)


class AllowCalls(Command):
    allow_calls: Literal[True]


class Discover(DiscoveryRequest):
    allow_fo_research: bool = False
    allow_claude_research: bool = False     # opt-in backup if Fo's research is unusable or thin
