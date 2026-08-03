from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from .errors import LedgerError


class AccountCategory(str, Enum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"


class ReservationKind(str, Enum):
    AMOUNT = "AMOUNT"
    QUANTITY = "QUANTITY"


class ReservationStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"


class LotEventKind(str, Enum):
    ACQUIRE = "ACQUIRE"
    CONSUME = "CONSUME"
    REVERSAL = "REVERSAL"


@dataclass(frozen=True, slots=True)
class Money:
    minor: int
    currency: str = "CNY"

    def __post_init__(self) -> None:
        if isinstance(self.minor, bool) or not isinstance(self.minor, int):
            raise LedgerError("money minor units must be an integer")
        if len(self.currency) != 3 or not self.currency.isupper():
            raise LedgerError("currency must be an uppercase ISO-style code")


@dataclass(frozen=True, slots=True)
class Price:
    scaled: int
    scale: int

    def __post_init__(self) -> None:
        if isinstance(self.scaled, bool) or not isinstance(self.scaled, int):
            raise LedgerError("scaled price must be an integer")
        if isinstance(self.scale, bool) or not isinstance(self.scale, int) or self.scale <= 0:
            raise LedgerError("price scale must be a positive integer")

    def as_decimal(self) -> Decimal:
        return Decimal(self.scaled) / Decimal(self.scale)


@dataclass(frozen=True, slots=True)
class PostingDraft:
    account_id: str
    amount_minor: int
    instrument_code: str | None = None
    quantity_delta: int = 0
    price_scaled: int | None = None
    price_scale: int | None = None
    dimensions: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TransactionDraft:
    portfolio_id: str
    source_namespace: str
    idempotency_key: str
    event_code: str
    business_date: str
    currency: str
    postings: tuple[PostingDraft, ...]
    source_type: str | None = None
    dimensions: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LotAllocation:
    lot_id: str
    quantity: int


@dataclass(frozen=True, slots=True)
class PostResult:
    transaction_id: str
    created: bool


@dataclass(frozen=True, slots=True)
class OpenLot:
    id: str
    portfolio_id: str
    account_id: str
    instrument_code: str
    acquired_date: str
    quantity: int
    cost_minor: int
    source_transaction_id: str


@dataclass(frozen=True, slots=True)
class Reservation:
    id: str
    portfolio_id: str
    account_id: str
    kind: ReservationKind
    status: ReservationStatus
    source_namespace: str
    idempotency_key: str
    amount_minor: int | None = None
    supplemental_availability_minor: int = 0
    instrument_code: str | None = None
    quantity: int | None = None
