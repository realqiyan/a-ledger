from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

from .errors import LedgerError
from .models import (
    AccountCategory,
    LotAllocation,
    LotEventKind,
    LotOperation,
    OpenLot,
    PostResult,
    PostingDraft,
    Reservation,
    ReservationKind,
    ReservationStatus,
    TransactionDraft,
)
from .schema import DDL_STATEMENTS, IMMUTABILITY_TRIGGERS, SCHEMA_VERSION


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Mapping[str, Any] | None = None) -> str:
    try:
        return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"dimensions must be JSON serializable: {exc}") from exc


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LedgerError(f"{label} must be an integer")
    return value


class Ledger:
    """Double-entry ledger operating inside a caller-owned SQLite transaction."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        if not connection.in_transaction:
            connection.execute("PRAGMA foreign_keys=ON")
        enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        if enabled != 1:
            raise LedgerError("SQLite foreign keys must be enabled before starting a transaction")

    def _require_transaction(self) -> None:
        if not self.connection.in_transaction:
            raise LedgerError("write API requires a caller-owned transaction")

    @contextmanager
    def _savepoint(self):
        self._require_transaction()
        name = f"ledger_{uuid4().hex}"
        self.connection.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self.connection.execute(f"ROLLBACK TO {name}")
            self.connection.execute(f"RELEASE {name}")
            raise
        else:
            self.connection.execute(f"RELEASE {name}")

    def install_schema(self) -> None:
        self._require_transaction()
        for statement in (*DDL_STATEMENTS, *IMMUTABILITY_TRIGGERS):
            self.connection.execute(statement)
        self.connection.execute(
            "INSERT OR IGNORE INTO ledger_schema_versions(version, installed_at) VALUES (?, ?)",
            (SCHEMA_VERSION, _now()),
        )

    def create_portfolio(
        self,
        portfolio_id: str,
        *,
        code: str,
        currency: str,
        minor_unit: int = 2,
        status: str = "ACTIVE",
    ) -> str:
        self._require_transaction()
        _integer(minor_unit, "minor_unit")
        if not portfolio_id or not code or not currency:
            raise LedgerError("portfolio id, code, and currency are required")
        self.connection.execute(
            """INSERT INTO ledger_portfolios(
                   id, code, currency, minor_unit, status, created_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (portfolio_id, code, currency, minor_unit, status, _now()),
        )
        return portfolio_id

    def create_account(
        self,
        account_id: str,
        *,
        portfolio_id: str,
        code: str,
        category: AccountCategory,
        currency: str,
        name: str | None = None,
    ) -> str:
        self._require_transaction()
        try:
            normalized_category = AccountCategory(category).value
        except ValueError as exc:
            raise LedgerError("unknown account category") from exc
        portfolio = self.connection.execute(
            "SELECT currency FROM ledger_portfolios WHERE id=?", (portfolio_id,)
        ).fetchone()
        if portfolio is None:
            raise LedgerError("portfolio does not exist")
        if portfolio[0] != currency:
            raise LedgerError("account currency differs from portfolio currency")
        self.connection.execute(
            """INSERT INTO ledger_accounts(
                   id, portfolio_id, code, category, currency, name, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (account_id, portfolio_id, code, normalized_category, currency, name, _now()),
        )
        return account_id

    def post(
        self,
        draft: TransactionDraft,
        *,
        lot_allocations: Mapping[int, Sequence[LotAllocation]] | None = None,
        lot_operations: Mapping[int, LotOperation | str] | None = None,
    ) -> PostResult:
        with self._savepoint():
            return self._post(
                draft,
                lot_allocations=lot_allocations or {},
                lot_operations=lot_operations or {},
            )

    def _post(
        self,
        draft: TransactionDraft,
        *,
        lot_allocations: Mapping[int, Sequence[LotAllocation]],
        lot_operations: Mapping[int, LotOperation | str] | None = None,
        reverses_transaction_id: str | None = None,
        replacement_for_transaction_id: str | None = None,
        lot_event_overrides: Mapping[int, Sequence[tuple[str, int, int, str]]] | None = None,
    ) -> PostResult:
        self._validate_draft(draft)
        normalized_operations = {
            index: LotOperation(operation)
            for index, operation in (lot_operations or {}).items()
        }
        allocations_payload = {
            str(index): [asdict(item) for item in allocations]
            for index, allocations in sorted(lot_allocations.items())
        }
        payload = {
            "draft": asdict(draft),
            "lot_allocations": allocations_payload,
            "lot_operations": {
                str(index): operation.value
                for index, operation in sorted(normalized_operations.items())
            },
            "reverses_transaction_id": reverses_transaction_id,
            "replacement_for_transaction_id": replacement_for_transaction_id,
        }
        payload_hash = hashlib.sha256(_json(payload).encode()).hexdigest()
        existing = self.connection.execute(
            """SELECT id, payload_hash FROM ledger_transactions
               WHERE portfolio_id=? AND source_namespace=? AND idempotency_key=?""",
            (draft.portfolio_id, draft.source_namespace, draft.idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing[1] != payload_hash:
                raise LedgerError("idempotency key was already used for a different transaction")
            return PostResult(existing[0], created=False)

        transaction_id = str(uuid4())
        now = _now()
        self.connection.execute(
            """INSERT INTO ledger_transactions(
                   id, portfolio_id, source_namespace, idempotency_key,
                   event_code, source_type, business_date, currency, payload_hash,
                   reverses_transaction_id, replacement_for_transaction_id,
                   dimensions_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                transaction_id,
                draft.portfolio_id,
                draft.source_namespace,
                draft.idempotency_key,
                draft.event_code,
                draft.source_type,
                draft.business_date,
                draft.currency,
                payload_hash,
                reverses_transaction_id,
                replacement_for_transaction_id,
                _json(draft.dimensions),
                now,
            ),
        )
        for index, posting in enumerate(draft.postings):
            posting_id = str(uuid4())
            self.connection.execute(
                """INSERT INTO ledger_postings(
                       id, transaction_id, portfolio_id, account_id, amount_minor,
                       instrument_code, quantity_delta, price_scaled, price_scale,
                       dimensions_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    posting_id,
                    transaction_id,
                    draft.portfolio_id,
                    posting.account_id,
                    posting.amount_minor,
                    posting.instrument_code,
                    posting.quantity_delta,
                    posting.price_scaled,
                    posting.price_scale,
                    _json(posting.dimensions),
                    now,
                ),
            )
            overrides = (lot_event_overrides or {}).get(index)
            if overrides is not None:
                self._write_override_lot_events(
                    draft.portfolio_id, transaction_id, posting_id, overrides
                )
            elif normalized_operations.get(index) == LotOperation.OPEN:
                if index in lot_allocations:
                    raise LedgerError("opening quantity postings cannot consume lot allocations")
                if posting.quantity_delta == 0:
                    raise LedgerError("lot OPEN requires a nonzero posting quantity")
                self._acquire_lot(draft, posting, transaction_id, posting_id)
            elif normalized_operations.get(index) == LotOperation.CLOSE:
                if posting.quantity_delta == 0:
                    raise LedgerError("lot CLOSE requires a nonzero posting quantity")
                self._close_lots(
                    draft,
                    posting,
                    transaction_id,
                    posting_id,
                    lot_allocations.get(index),
                    required_lot_sign=-1 if posting.quantity_delta > 0 else 1,
                )
            elif posting.quantity_delta > 0:
                if index in lot_allocations:
                    raise LedgerError("positive quantity postings cannot consume lot allocations")
                self._acquire_lot(draft, posting, transaction_id, posting_id)
            elif posting.quantity_delta < 0:
                self._consume_lots(
                    draft,
                    posting,
                    transaction_id,
                    posting_id,
                    lot_allocations.get(index),
                )
        self._audit(
            draft.portfolio_id,
            "TRANSACTION_POSTED",
            "TRANSACTION",
            transaction_id,
            {"event_code": draft.event_code},
        )
        return PostResult(transaction_id, created=True)

    def _validate_draft(self, draft: TransactionDraft) -> None:
        if len(draft.postings) < 2:
            raise LedgerError("a transaction requires at least two postings")
        if not all(
            (
                draft.portfolio_id,
                draft.source_namespace,
                draft.idempotency_key,
                draft.event_code,
                draft.business_date,
                draft.currency,
            )
        ):
            raise LedgerError("transaction identity and classification fields are required")
        portfolio = self.connection.execute(
            "SELECT currency FROM ledger_portfolios WHERE id=?", (draft.portfolio_id,)
        ).fetchone()
        if portfolio is None:
            raise LedgerError("portfolio does not exist")
        if portfolio[0] != draft.currency:
            raise LedgerError("transaction currency differs from portfolio currency")
        balance = 0
        for posting in draft.postings:
            balance += _integer(posting.amount_minor, "posting amount")
            quantity = _integer(posting.quantity_delta, "posting quantity")
            account = self.connection.execute(
                "SELECT portfolio_id, currency FROM ledger_accounts WHERE id=?",
                (posting.account_id,),
            ).fetchone()
            if account is None:
                raise LedgerError("posting account does not exist")
            if account[0] != draft.portfolio_id:
                raise LedgerError("posting account belongs to another portfolio")
            if account[1] != draft.currency:
                raise LedgerError("posting account currency differs from transaction currency")
            if quantity != 0 and not posting.instrument_code:
                raise LedgerError("a quantity posting requires an instrument code")
            if (posting.price_scaled is None) != (posting.price_scale is None):
                raise LedgerError("scaled price and price scale must be provided together")
            if posting.price_scaled is not None:
                _integer(posting.price_scaled, "scaled price")
                if _integer(posting.price_scale, "price scale") <= 0:  # type: ignore[arg-type]
                    raise LedgerError("price scale must be positive")
            _json(posting.dimensions)
        _json(draft.dimensions)
        if balance != 0:
            raise LedgerError(f"transaction is not balanced: {balance}")

    def _acquire_lot(
        self,
        draft: TransactionDraft,
        posting: PostingDraft,
        transaction_id: str,
        posting_id: str,
    ) -> None:
        lot_id = str(uuid4())
        now = _now()
        self.connection.execute(
            """INSERT INTO ledger_lots(
                   id, portfolio_id, account_id, instrument_code, acquired_date,
                   source_transaction_id, source_posting_id, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                lot_id,
                draft.portfolio_id,
                posting.account_id,
                posting.instrument_code,
                draft.business_date,
                transaction_id,
                posting_id,
                now,
            ),
        )
        self._insert_lot_event(
            draft.portfolio_id,
            transaction_id,
            posting_id,
            lot_id,
            LotEventKind.ACQUIRE,
            posting.quantity_delta,
            abs(posting.amount_minor),
        )

    def _consume_lots(
        self,
        draft: TransactionDraft,
        posting: PostingDraft,
        transaction_id: str,
        posting_id: str,
        explicit: Sequence[LotAllocation] | None,
    ) -> None:
        self._close_lots(
            draft,
            posting,
            transaction_id,
            posting_id,
            explicit,
            required_lot_sign=1,
        )

    def _close_lots(
        self,
        draft: TransactionDraft,
        posting: PostingDraft,
        transaction_id: str,
        posting_id: str,
        explicit: Sequence[LotAllocation] | None,
        *,
        required_lot_sign: int,
    ) -> None:
        requested = abs(posting.quantity_delta)
        open_lots = [
            lot
            for lot in self.open_signed_lots(
                posting.account_id, posting.instrument_code or ""
            )
            if (1 if lot.quantity > 0 else -1) == required_lot_sign
        ]
        by_id = {lot.id: lot for lot in open_lots}
        if explicit is None:
            allocations: list[LotAllocation] = []
            remaining = requested
            for lot in open_lots:
                used = min(abs(lot.quantity), remaining)
                if used:
                    allocations.append(LotAllocation(lot.id, used))
                    remaining -= used
                if remaining == 0:
                    break
            if remaining:
                raise LedgerError("quantity exceeds available open lots")
        else:
            allocations = list(explicit)
            if sum(_integer(item.quantity, "lot allocation quantity") for item in allocations) != requested:
                raise LedgerError("explicit lot allocations do not match posting quantity")
        seen: set[str] = set()
        for allocation in allocations:
            if allocation.lot_id in seen:
                raise LedgerError("a lot may appear only once in one posting allocation")
            seen.add(allocation.lot_id)
            lot = by_id.get(allocation.lot_id)
            if lot is None:
                raise LedgerError(
                    "explicit lot is unavailable, not opposite-side, or belongs "
                    "to another account/instrument"
                )
            quantity = _integer(allocation.quantity, "lot allocation quantity")
            if quantity <= 0 or quantity > abs(lot.quantity):
                raise LedgerError("lot allocation exceeds available quantity")
            available = abs(lot.quantity)
            cost = (
                lot.cost_minor
                if quantity == available
                else lot.cost_minor * quantity // available
            )
            self._insert_lot_event(
                draft.portfolio_id,
                transaction_id,
                posting_id,
                lot.id,
                LotEventKind.CONSUME,
                -required_lot_sign * quantity,
                -cost,
            )

    def _write_override_lot_events(
        self,
        portfolio_id: str,
        transaction_id: str,
        posting_id: str,
        overrides: Sequence[tuple[str, int, int, str]],
    ) -> None:
        for lot_id, quantity_delta, cost_delta, reversed_event_id in overrides:
            current = self._lot_totals(lot_id)
            if current is None or current[0] + quantity_delta < 0 or current[1] + cost_delta < 0:
                raise LedgerError("reversal would invalidate a dependent lot consumption")
            self._insert_lot_event(
                portfolio_id,
                transaction_id,
                posting_id,
                lot_id,
                LotEventKind.REVERSAL,
                quantity_delta,
                cost_delta,
                reverses_lot_event_id=reversed_event_id,
            )

    def _insert_lot_event(
        self,
        portfolio_id: str,
        transaction_id: str,
        posting_id: str,
        lot_id: str,
        kind: LotEventKind,
        quantity_delta: int,
        cost_delta: int,
        *,
        reverses_lot_event_id: str | None = None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO ledger_lot_events(
                   id, transaction_id, portfolio_id, posting_id, lot_id, event_kind,
                   quantity_delta, cost_delta_minor, reverses_lot_event_id, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid4()),
                transaction_id,
                portfolio_id,
                posting_id,
                lot_id,
                kind.value,
                quantity_delta,
                cost_delta,
                reverses_lot_event_id,
                _now(),
            ),
        )

    def reverse(
        self,
        transaction_id: str,
        *,
        source_namespace: str,
        idempotency_key: str,
        business_date: str | None = None,
    ) -> PostResult:
        with self._savepoint():
            original = self.connection.execute(
                "SELECT * FROM ledger_transactions WHERE id=?", (transaction_id,)
            ).fetchone()
            if original is None:
                raise LedgerError("transaction to reverse does not exist")
            already = self.connection.execute(
                "SELECT id FROM ledger_transactions WHERE reverses_transaction_id=?",
                (transaction_id,),
            ).fetchone()
            if already is not None:
                idempotent = self.connection.execute(
                    """SELECT id FROM ledger_transactions
                       WHERE portfolio_id=? AND source_namespace=? AND idempotency_key=?""",
                    (original["portfolio_id"], source_namespace, idempotency_key),
                ).fetchone()
                if idempotent is not None and idempotent[0] == already[0]:
                    return PostResult(already[0], created=False)
                raise LedgerError("transaction has already been reversed")
            postings = self.connection.execute(
                "SELECT * FROM ledger_postings WHERE transaction_id=? ORDER BY rowid",
                (transaction_id,),
            ).fetchall()
            draft = TransactionDraft(
                portfolio_id=original["portfolio_id"],
                source_namespace=source_namespace,
                idempotency_key=idempotency_key,
                event_code="REVERSAL",
                source_type="LEDGER_CORRECTION",
                business_date=business_date or original["business_date"],
                currency=original["currency"],
                dimensions={"reverses_transaction_id": transaction_id},
                postings=tuple(
                    PostingDraft(
                        account_id=row["account_id"],
                        amount_minor=-row["amount_minor"],
                        instrument_code=row["instrument_code"],
                        quantity_delta=-row["quantity_delta"],
                        price_scaled=row["price_scaled"],
                        price_scale=row["price_scale"],
                        dimensions={"reverses_posting_id": row["id"]},
                    )
                    for row in postings
                ),
            )
            overrides: dict[int, list[tuple[str, int, int, str]]] = {}
            for index, posting in enumerate(postings):
                events = self.connection.execute(
                    "SELECT * FROM ledger_lot_events WHERE posting_id=? ORDER BY rowid",
                    (posting["id"],),
                ).fetchall()
                if events:
                    overrides[index] = [
                        (
                            event["lot_id"],
                            -event["quantity_delta"],
                            -event["cost_delta_minor"],
                            event["id"],
                        )
                        for event in events
                    ]
            return self._post(
                draft,
                lot_allocations={},
                lot_operations={},
                reverses_transaction_id=transaction_id,
                lot_event_overrides=overrides,
            )

    def replace(
        self,
        transaction_id: str,
        replacement: TransactionDraft,
        *,
        reversal_source_namespace: str,
        reversal_idempotency_key: str,
    ) -> tuple[PostResult, PostResult]:
        with self._savepoint():
            reversal = self.reverse(
                transaction_id,
                source_namespace=reversal_source_namespace,
                idempotency_key=reversal_idempotency_key,
                business_date=replacement.business_date,
            )
            replaced = self._post(
                replacement,
                lot_allocations={},
                lot_operations={},
                replacement_for_transaction_id=transaction_id,
            )
            return reversal, replaced

    def account_balance(self, account_id: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(sum(amount_minor), 0) FROM ledger_postings WHERE account_id=?",
            (account_id,),
        ).fetchone()
        return int(row[0])

    def open_lots(self, account_id: str, instrument_code: str) -> list[OpenLot]:
        return [
            lot
            for lot in self.open_signed_lots(account_id, instrument_code)
            if lot.quantity > 0
        ]

    def open_signed_lots(
        self, account_id: str, instrument_code: str
    ) -> list[OpenLot]:
        rows = self.connection.execute(
            """SELECT l.id, l.portfolio_id, l.account_id, l.instrument_code,
                      l.acquired_date, l.source_transaction_id,
                      sum(e.quantity_delta) AS quantity,
                      sum(e.cost_delta_minor) AS cost_minor
               FROM ledger_lots l
               JOIN ledger_lot_events e ON e.lot_id = l.id
               WHERE l.account_id=? AND l.instrument_code=?
               GROUP BY l.id
               HAVING sum(e.quantity_delta) != 0
               ORDER BY l.acquired_date, l.created_at, l.id""",
            (account_id, instrument_code),
        ).fetchall()
        return [OpenLot(**dict(row)) for row in rows]

    def _lot_totals(self, lot_id: str) -> tuple[int, int] | None:
        row = self.connection.execute(
            """SELECT sum(quantity_delta), sum(cost_delta_minor)
               FROM ledger_lot_events WHERE lot_id=?""",
            (lot_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0]), int(row[1])

    def available_amount(self, account_id: str) -> int:
        reserved = self.connection.execute(
            """SELECT COALESCE(sum(amount_minor), 0) FROM ledger_reservations
               WHERE account_id=? AND kind='AMOUNT' AND status='ACTIVE'""",
            (account_id,),
        ).fetchone()[0]
        return self.account_balance(account_id) - int(reserved)

    def available_quantity(self, account_id: str, instrument_code: str) -> int:
        open_quantity = sum(
            lot.quantity for lot in self.open_lots(account_id, instrument_code)
        )
        reserved = self.connection.execute(
            """SELECT COALESCE(sum(quantity), 0) FROM ledger_reservations
               WHERE account_id=? AND instrument_code=?
                 AND kind='QUANTITY' AND status='ACTIVE'""",
            (account_id, instrument_code),
        ).fetchone()[0]
        return open_quantity - int(reserved)

    def reserve_amount(
        self,
        *,
        portfolio_id: str,
        account_id: str,
        amount_minor: int,
        source_namespace: str,
        idempotency_key: str,
        supplemental_availability_minor: int = 0,
        dimensions: Mapping[str, Any] | None = None,
    ) -> Reservation:
        amount_minor = _integer(amount_minor, "reservation amount")
        supplemental = _integer(
            supplemental_availability_minor, "supplemental availability"
        )
        if amount_minor <= 0 or supplemental < 0:
            raise LedgerError("reservation amount must be positive and supplemental availability nonnegative")
        with self._savepoint():
            existing = self._reservation_by_key(
                portfolio_id, source_namespace, idempotency_key
            )
            if existing is not None:
                if (
                    existing.kind != ReservationKind.AMOUNT
                    or existing.account_id != account_id
                    or existing.amount_minor != amount_minor
                    or existing.supplemental_availability_minor != supplemental
                ):
                    raise LedgerError("reservation idempotency key was reused with different data")
                return existing
            self._validate_account_scope(account_id, portfolio_id)
            if self.available_amount(account_id) + supplemental < amount_minor:
                raise LedgerError("amount exceeds available cash")
            return self._insert_reservation(
                portfolio_id=portfolio_id,
                account_id=account_id,
                kind=ReservationKind.AMOUNT,
                source_namespace=source_namespace,
                idempotency_key=idempotency_key,
                amount_minor=amount_minor,
                supplemental_availability_minor=supplemental,
                dimensions=dimensions,
            )

    def reserve_quantity(
        self,
        *,
        portfolio_id: str,
        account_id: str,
        instrument_code: str,
        quantity: int,
        source_namespace: str,
        idempotency_key: str,
        eligible_lot_ids: Iterable[str] | None = None,
        dimensions: Mapping[str, Any] | None = None,
    ) -> Reservation:
        quantity = _integer(quantity, "reservation quantity")
        if quantity <= 0 or not instrument_code:
            raise LedgerError("reservation quantity and instrument are required")
        with self._savepoint():
            existing = self._reservation_by_key(
                portfolio_id, source_namespace, idempotency_key
            )
            if existing is not None:
                if (
                    existing.kind != ReservationKind.QUANTITY
                    or existing.account_id != account_id
                    or existing.instrument_code != instrument_code
                    or existing.quantity != quantity
                ):
                    raise LedgerError("reservation idempotency key was reused with different data")
                return existing
            self._validate_account_scope(account_id, portfolio_id)
            available = self.available_quantity(account_id, instrument_code)
            if eligible_lot_ids is not None:
                eligible = set(eligible_lot_ids)
                open_eligible = sum(
                    lot.quantity
                    for lot in self.open_lots(account_id, instrument_code)
                    if lot.id in eligible
                )
                active_reserved = self.connection.execute(
                    """SELECT COALESCE(sum(quantity), 0) FROM ledger_reservations
                       WHERE account_id=? AND instrument_code=?
                         AND kind='QUANTITY' AND status='ACTIVE'""",
                    (account_id, instrument_code),
                ).fetchone()[0]
                available = open_eligible - int(active_reserved)
            if available < quantity:
                raise LedgerError("quantity exceeds available lots")
            return self._insert_reservation(
                portfolio_id=portfolio_id,
                account_id=account_id,
                kind=ReservationKind.QUANTITY,
                source_namespace=source_namespace,
                idempotency_key=idempotency_key,
                instrument_code=instrument_code,
                quantity=quantity,
                dimensions=dimensions,
            )

    def _insert_reservation(
        self,
        *,
        portfolio_id: str,
        account_id: str,
        kind: ReservationKind,
        source_namespace: str,
        idempotency_key: str,
        amount_minor: int | None = None,
        supplemental_availability_minor: int = 0,
        instrument_code: str | None = None,
        quantity: int | None = None,
        dimensions: Mapping[str, Any] | None = None,
    ) -> Reservation:
        reservation_id = str(uuid4())
        now = _now()
        self.connection.execute(
            """INSERT INTO ledger_reservations(
                   id, portfolio_id, account_id, kind, status, source_namespace,
                   idempotency_key, amount_minor, supplemental_availability_minor,
                   instrument_code, quantity, dimensions_json, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                reservation_id,
                portfolio_id,
                account_id,
                kind.value,
                ReservationStatus.ACTIVE.value,
                source_namespace,
                idempotency_key,
                amount_minor,
                supplemental_availability_minor,
                instrument_code,
                quantity,
                _json(dimensions),
                now,
                now,
            ),
        )
        self._audit(
            portfolio_id,
            "RESERVATION_CREATED",
            "RESERVATION",
            reservation_id,
            {"kind": kind.value},
        )
        return Reservation(
            id=reservation_id,
            portfolio_id=portfolio_id,
            account_id=account_id,
            kind=kind,
            status=ReservationStatus.ACTIVE,
            source_namespace=source_namespace,
            idempotency_key=idempotency_key,
            amount_minor=amount_minor,
            supplemental_availability_minor=supplemental_availability_minor,
            instrument_code=instrument_code,
            quantity=quantity,
        )

    def consume_reservation(
        self,
        reservation_id: str,
        *,
        consumed_amount_minor: int | None = None,
        consumed_quantity: int | None = None,
    ) -> None:
        with self._savepoint():
            row = self.connection.execute(
                "SELECT * FROM ledger_reservations WHERE id=?", (reservation_id,)
            ).fetchone()
            if row is None:
                raise LedgerError("reservation does not exist")
            if row["status"] != ReservationStatus.ACTIVE.value:
                raise LedgerError("only an active reservation can be consumed")
            if row["kind"] == ReservationKind.AMOUNT.value:
                if consumed_amount_minor is None or consumed_quantity is not None:
                    raise LedgerError("amount reservation requires consumed_amount_minor")
                if _integer(consumed_amount_minor, "consumed amount") < 0:
                    raise LedgerError("consumed amount cannot be negative")
            else:
                if consumed_quantity is None or consumed_amount_minor is not None:
                    raise LedgerError("quantity reservation requires consumed_quantity")
                if _integer(consumed_quantity, "consumed quantity") < 0:
                    raise LedgerError("consumed quantity cannot be negative")
            self.connection.execute(
                """UPDATE ledger_reservations
                   SET status='CONSUMED', consumed_amount_minor=?, consumed_quantity=?, updated_at=?
                   WHERE id=?""",
                (consumed_amount_minor, consumed_quantity, _now(), reservation_id),
            )
            self._audit(
                row["portfolio_id"],
                "RESERVATION_CONSUMED",
                "RESERVATION",
                reservation_id,
                {
                    "consumed_amount_minor": consumed_amount_minor,
                    "consumed_quantity": consumed_quantity,
                },
            )

    def release_reservation(self, reservation_id: str) -> None:
        with self._savepoint():
            row = self.connection.execute(
                "SELECT portfolio_id, status FROM ledger_reservations WHERE id=?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise LedgerError("reservation does not exist")
            if row["status"] != ReservationStatus.ACTIVE.value:
                raise LedgerError("only an active reservation can be released")
            self.connection.execute(
                "UPDATE ledger_reservations SET status='RELEASED', updated_at=? WHERE id=?",
                (_now(), reservation_id),
            )
            self._audit(
                row["portfolio_id"],
                "RESERVATION_RELEASED",
                "RESERVATION",
                reservation_id,
                {},
            )

    def _reservation_by_key(
        self, portfolio_id: str, source_namespace: str, idempotency_key: str
    ) -> Reservation | None:
        row = self.connection.execute(
            """SELECT * FROM ledger_reservations
               WHERE portfolio_id=? AND source_namespace=? AND idempotency_key=?""",
            (portfolio_id, source_namespace, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return Reservation(
            id=row["id"],
            portfolio_id=row["portfolio_id"],
            account_id=row["account_id"],
            kind=ReservationKind(row["kind"]),
            status=ReservationStatus(row["status"]),
            source_namespace=row["source_namespace"],
            idempotency_key=row["idempotency_key"],
            amount_minor=row["amount_minor"],
            supplemental_availability_minor=row["supplemental_availability_minor"],
            instrument_code=row["instrument_code"],
            quantity=row["quantity"],
        )

    def _validate_account_scope(self, account_id: str, portfolio_id: str) -> None:
        row = self.connection.execute(
            "SELECT portfolio_id FROM ledger_accounts WHERE id=?", (account_id,)
        ).fetchone()
        if row is None:
            raise LedgerError("reservation account does not exist")
        if row[0] != portfolio_id:
            raise LedgerError("reservation account belongs to another portfolio")

    def _audit(
        self,
        portfolio_id: str,
        event_code: str,
        entity_type: str,
        entity_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.connection.execute(
            """INSERT INTO ledger_audit_events(
                   id, portfolio_id, event_code, entity_type, entity_id,
                   payload_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid4()),
                portfolio_id,
                event_code,
                entity_type,
                entity_id,
                _json(payload),
                _now(),
            ),
        )
