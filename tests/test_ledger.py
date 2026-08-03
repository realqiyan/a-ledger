from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace

import pytest

from a_ledger import (
    AccountCategory,
    Ledger,
    LedgerError,
    LotAllocation,
    LotOperation,
    Money,
    PostingDraft,
    Price,
    ReservationStatus,
    TransactionDraft,
)


def begin(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")


@pytest.fixture
def connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    ledger = Ledger(connection)
    begin(connection)
    ledger.install_schema()
    ledger.create_portfolio("portfolio-1", code="main", currency="CNY")
    ledger.create_account(
        "cash-1",
        portfolio_id="portfolio-1",
        code="ASSET:CASH",
        category=AccountCategory.ASSET,
        currency="CNY",
    )
    ledger.create_account(
        "security-1",
        portfolio_id="portfolio-1",
        code="ASSET:SECURITY",
        category=AccountCategory.ASSET,
        currency="CNY",
    )
    ledger.create_account(
        "capital-1",
        portfolio_id="portfolio-1",
        code="EQUITY:CAPITAL",
        category=AccountCategory.EQUITY,
        currency="CNY",
    )
    connection.commit()
    yield connection
    connection.close()


def capital_draft(amount_minor: int, *, key: str = "capital-1") -> TransactionDraft:
    return TransactionDraft(
        portfolio_id="portfolio-1",
        source_namespace="test",
        idempotency_key=key,
        event_code="CAPITAL_FLOW",
        business_date="2026-08-03",
        currency="CNY",
        postings=(
            PostingDraft(account_id="cash-1", amount_minor=amount_minor),
            PostingDraft(account_id="capital-1", amount_minor=-amount_minor),
        ),
    )


def buy_draft(
    *, key: str, quantity: int, cost_minor: int, instrument: str = "510300.SH"
) -> TransactionDraft:
    return TransactionDraft(
        portfolio_id="portfolio-1",
        source_namespace="test",
        idempotency_key=key,
        event_code="TRADE",
        business_date="2026-08-03",
        currency="CNY",
        postings=(
            PostingDraft(
                account_id="security-1",
                amount_minor=cost_minor,
                instrument_code=instrument,
                quantity_delta=quantity,
            ),
            PostingDraft(account_id="cash-1", amount_minor=-cost_minor),
        ),
    )


def sell_draft(
    *, key: str, quantity: int, proceeds_minor: int, instrument: str = "510300.SH"
) -> TransactionDraft:
    return TransactionDraft(
        portfolio_id="portfolio-1",
        source_namespace="test",
        idempotency_key=key,
        event_code="TRADE",
        business_date="2026-08-04",
        currency="CNY",
        postings=(
            PostingDraft(
                account_id="security-1",
                amount_minor=-proceeds_minor,
                instrument_code=instrument,
                quantity_delta=-quantity,
            ),
            PostingDraft(account_id="cash-1", amount_minor=proceeds_minor),
        ),
    )


def post_committed(connection: sqlite3.Connection, draft: TransactionDraft):
    begin(connection)
    result = Ledger(connection).post(draft)
    connection.commit()
    return result


@pytest.mark.parametrize(
    "postings",
    [
        (
            PostingDraft(account_id="cash-1", amount_minor=100),
            PostingDraft(account_id="capital-1", amount_minor=-99),
        ),
        (
            PostingDraft(
                account_id="security-1",
                amount_minor=100,
                instrument_code="510300.SH",
                quantity_delta=0.5,  # type: ignore[arg-type]
            ),
            PostingDraft(account_id="cash-1", amount_minor=-100),
        ),
    ],
)
def test_unbalanced_and_non_integer_quantity_are_rejected(connection, postings):
    draft = TransactionDraft(
        portfolio_id="portfolio-1",
        source_namespace="test",
        idempotency_key="invalid",
        event_code="TRADE",
        business_date="2026-08-03",
        currency="CNY",
        postings=postings,
    )

    begin(connection)
    with pytest.raises(LedgerError):
        Ledger(connection).post(draft)
    connection.rollback()


def test_cross_portfolio_account_and_currency_mismatch_are_rejected(connection):
    ledger = Ledger(connection)
    begin(connection)
    ledger.create_portfolio("portfolio-2", code="other", currency="USD")
    ledger.create_account(
        "cash-2",
        portfolio_id="portfolio-2",
        code="ASSET:CASH",
        category=AccountCategory.ASSET,
        currency="USD",
    )
    connection.commit()

    cross_portfolio = replace(
        capital_draft(100),
        postings=(
            PostingDraft(account_id="cash-2", amount_minor=100),
            PostingDraft(account_id="capital-1", amount_minor=-100),
        ),
    )
    wrong_currency = replace(
        capital_draft(100), idempotency_key="usd", currency="USD"
    )

    begin(connection)
    with pytest.raises(LedgerError, match="portfolio"):
        ledger.post(cross_portfolio)
    with pytest.raises(LedgerError, match="currency"):
        ledger.post(wrong_currency)
    connection.rollback()


def test_idempotent_post_returns_the_original_transaction(connection):
    ledger = Ledger(connection)
    begin(connection)
    first = ledger.post(capital_draft(10_000))
    second = ledger.post(capital_draft(10_000))
    connection.commit()

    assert first.created is True
    assert second.created is False
    assert second.transaction_id == first.transaction_id
    assert connection.execute("SELECT count(*) FROM ledger_transactions").fetchone()[0] == 1


def test_idempotency_key_cannot_hide_different_payload(connection):
    ledger = Ledger(connection)
    begin(connection)
    ledger.post(capital_draft(10_000))
    with pytest.raises(LedgerError, match="idempotency"):
        ledger.post(capital_draft(20_000))
    connection.rollback()


def test_posted_facts_are_immutable_and_reversal_replacement_preserve_links(connection):
    original = post_committed(connection, capital_draft(10_000))

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE ledger_postings SET amount_minor=0 WHERE transaction_id=?",
            (original.transaction_id,),
        )
    connection.rollback()

    replacement = capital_draft(20_000, key="replacement")
    begin(connection)
    reversal, replaced = Ledger(connection).replace(
        original.transaction_id,
        replacement,
        reversal_source_namespace="test-correction",
        reversal_idempotency_key="reverse-capital-1",
    )
    connection.commit()

    reversal_row = connection.execute(
        "SELECT reverses_transaction_id FROM ledger_transactions WHERE id=?",
        (reversal.transaction_id,),
    ).fetchone()
    replacement_row = connection.execute(
        "SELECT replacement_for_transaction_id FROM ledger_transactions WHERE id=?",
        (replaced.transaction_id,),
    ).fetchone()
    assert reversal_row[0] == original.transaction_id
    assert replacement_row[0] == original.transaction_id
    assert Ledger(connection).account_balance("cash-1") == 20_000


def test_fifo_and_explicit_lot_consumption(connection):
    first_buy = post_committed(connection, buy_draft(key="buy-1", quantity=100, cost_minor=1_000))
    second_buy = post_committed(connection, buy_draft(key="buy-2", quantity=100, cost_minor=2_000))
    lots = Ledger(connection).open_lots("security-1", "510300.SH")
    assert [lot.source_transaction_id for lot in lots] == [
        first_buy.transaction_id,
        second_buy.transaction_id,
    ]

    post_committed(connection, sell_draft(key="sell-fifo", quantity=120, proceeds_minor=1_800))
    lots = Ledger(connection).open_lots("security-1", "510300.SH")
    assert [(lot.source_transaction_id, lot.quantity) for lot in lots] == [
        (second_buy.transaction_id, 80)
    ]

    third_buy = post_committed(connection, buy_draft(key="buy-3", quantity=100, cost_minor=900))
    lots = Ledger(connection).open_lots("security-1", "510300.SH")
    third_lot = next(lot for lot in lots if lot.source_transaction_id == third_buy.transaction_id)
    begin(connection)
    Ledger(connection).post(
        sell_draft(key="sell-explicit", quantity=50, proceeds_minor=600),
        lot_allocations={0: (LotAllocation(third_lot.id, 50),)},
    )
    connection.commit()
    lots = Ledger(connection).open_lots("security-1", "510300.SH")
    assert [(lot.source_transaction_id, lot.quantity) for lot in lots] == [
        (second_buy.transaction_id, 80),
        (third_buy.transaction_id, 50),
    ]
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "positions" not in tables
    assert "balances" not in tables


def test_explicit_lot_operations_support_short_open_and_fifo_close(connection):
    sell_open = sell_draft(
        key="option-sell-open",
        quantity=2,
        proceeds_minor=1_000,
        instrument="10000001.SH",
    )
    begin(connection)
    opened = Ledger(connection).post(
        sell_open,
        lot_operations={0: LotOperation.OPEN},
    )
    connection.commit()

    signed = Ledger(connection).open_signed_lots("security-1", "10000001.SH")
    assert [(lot.quantity, lot.cost_minor) for lot in signed] == [(-2, 1_000)]
    assert Ledger(connection).open_lots("security-1", "10000001.SH") == []

    buy_close = buy_draft(
        key="option-buy-close",
        quantity=1,
        cost_minor=400,
        instrument="10000001.SH",
    )
    begin(connection)
    Ledger(connection).post(
        buy_close,
        lot_operations={0: LotOperation.CLOSE},
    )
    connection.commit()

    signed = Ledger(connection).open_signed_lots("security-1", "10000001.SH")
    assert [(lot.source_transaction_id, lot.quantity, lot.cost_minor) for lot in signed] == [
        (opened.transaction_id, -1, 500)
    ]


def test_explicit_close_cannot_consume_a_lot_on_the_same_side(connection):
    post_committed(
        connection,
        buy_draft(key="long-open", quantity=10, cost_minor=1_000),
    )
    another_buy = buy_draft(key="invalid-close", quantity=5, cost_minor=500)
    begin(connection)
    with pytest.raises(LedgerError, match="available open lots"):
        Ledger(connection).post(
            another_buy,
            lot_operations={0: LotOperation.CLOSE},
        )
    connection.rollback()


def test_amount_and_quantity_reservations_affect_availability(connection):
    post_committed(connection, capital_draft(10_000))
    post_committed(connection, buy_draft(key="buy", quantity=100, cost_minor=1_000))
    ledger = Ledger(connection)

    begin(connection)
    cash_reservation = ledger.reserve_amount(
        portfolio_id="portfolio-1",
        account_id="cash-1",
        amount_minor=8_000,
        source_namespace="order",
        idempotency_key="cash-order",
    )
    quantity_reservation = ledger.reserve_quantity(
        portfolio_id="portfolio-1",
        account_id="security-1",
        instrument_code="510300.SH",
        quantity=60,
        source_namespace="order",
        idempotency_key="sell-order",
    )
    connection.commit()

    assert ledger.available_amount("cash-1") == 1_000
    assert ledger.available_quantity("security-1", "510300.SH") == 40

    begin(connection)
    with pytest.raises(LedgerError, match="available"):
        ledger.reserve_amount(
            portfolio_id="portfolio-1",
            account_id="cash-1",
            amount_minor=1_001,
            source_namespace="order",
            idempotency_key="too-large",
        )
    supplemented = ledger.reserve_amount(
        portfolio_id="portfolio-1",
        account_id="cash-1",
        amount_minor=2_000,
        supplemental_availability_minor=1_000,
        source_namespace="order",
        idempotency_key="rotation-buy",
    )
    ledger.consume_reservation(cash_reservation.id, consumed_amount_minor=8_500)
    ledger.release_reservation(quantity_reservation.id)
    ledger.release_reservation(supplemented.id)
    connection.commit()

    statuses = dict(
        connection.execute("SELECT id, status FROM ledger_reservations").fetchall()
    )
    assert statuses[cash_reservation.id] == ReservationStatus.CONSUMED.value
    assert statuses[quantity_reservation.id] == ReservationStatus.RELEASED.value
    assert ledger.available_amount("cash-1") == 9_000
    assert ledger.available_quantity("security-1", "510300.SH") == 100


def test_application_and_ledger_writes_share_the_callers_transaction(connection):
    connection.execute("CREATE TABLE app_orders(id TEXT PRIMARY KEY)")
    begin(connection)
    connection.execute("INSERT INTO app_orders(id) VALUES ('order-1')")
    Ledger(connection).post(capital_draft(100))
    connection.rollback()

    assert connection.execute("SELECT count(*) FROM app_orders").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM ledger_transactions").fetchone()[0] == 0


def test_write_apis_require_a_caller_owned_transaction(connection):
    with pytest.raises(LedgerError, match="transaction"):
        Ledger(connection).post(capital_draft(100))


def test_sqlite_integrity_checks_pass(connection):
    post_committed(connection, capital_draft(10_000))
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_money_and_price_reject_implicit_float_values():
    assert Money(123, "CNY").minor == 123
    assert Price(12345, 10_000).as_decimal().as_tuple().exponent == -4
    with pytest.raises(LedgerError, match="integer"):
        Money(1.5, "CNY")  # type: ignore[arg-type]
    with pytest.raises(LedgerError, match="integer"):
        Price(1.5, 10_000)  # type: ignore[arg-type]


def test_audit_events_are_append_only(connection):
    result = post_committed(connection, capital_draft(10_000))
    audit_id = connection.execute(
        "SELECT id FROM ledger_audit_events WHERE entity_id=?", (result.transaction_id,)
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("DELETE FROM ledger_audit_events WHERE id=?", (audit_id,))
    connection.rollback()


def test_competing_cash_reservations_are_serialized(tmp_path):
    path = tmp_path / "competition.sqlite3"
    setup = sqlite3.connect(path)
    setup.row_factory = sqlite3.Row
    ledger = Ledger(setup)
    begin(setup)
    ledger.install_schema()
    ledger.create_portfolio("portfolio-1", code="main", currency="CNY")
    ledger.create_account(
        "cash-1",
        portfolio_id="portfolio-1",
        code="ASSET:CASH",
        category=AccountCategory.ASSET,
        currency="CNY",
    )
    ledger.create_account(
        "capital-1",
        portfolio_id="portfolio-1",
        code="EQUITY:CAPITAL",
        category=AccountCategory.EQUITY,
        currency="CNY",
    )
    ledger.post(capital_draft(10_000))
    setup.commit()
    setup.close()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def compete(key: str) -> None:
        connection = sqlite3.connect(path, timeout=5)
        connection.row_factory = sqlite3.Row
        contender = Ledger(connection)
        barrier.wait()
        begin(connection)
        try:
            contender.reserve_amount(
                portfolio_id="portfolio-1",
                account_id="cash-1",
                amount_minor=7_000,
                source_namespace="race",
                idempotency_key=key,
            )
        except LedgerError:
            connection.rollback()
            outcomes.append("rejected")
        else:
            connection.commit()
            outcomes.append("reserved")
        finally:
            connection.close()

    threads = [threading.Thread(target=compete, args=(f"order-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["rejected", "reserved"]
