"""Callers may persist facts while owning their lot projection."""

import sqlite3

import pytest

from a_ledger import (
    AccountCategory,
    Ledger,
    PostingDraft,
    TransactionDraft,
)


@pytest.fixture
def connection():
    value = sqlite3.connect(":memory:")
    value.row_factory = sqlite3.Row
    value.execute("PRAGMA foreign_keys=ON")
    value.execute("BEGIN IMMEDIATE")
    ledger = Ledger(value)
    ledger.install_schema()
    ledger.create_portfolio("portfolio-1", code="main", currency="CNY")
    ledger.create_account(
        "security-1",
        portfolio_id="portfolio-1",
        code="ASSET:SECURITY",
        category=AccountCategory.ASSET,
        currency="CNY",
    )
    ledger.create_account(
        "cash-1",
        portfolio_id="portfolio-1",
        code="ASSET:CASH",
        category=AccountCategory.ASSET,
        currency="CNY",
    )
    value.commit()
    yield value
    value.close()


def _buy(*, key: str, amount: int = 1_000) -> TransactionDraft:
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
                amount_minor=amount,
                instrument_code="510300.SH",
                quantity_delta=100,
            ),
            PostingDraft(account_id="cash-1", amount_minor=-amount),
        ),
    )


def _lot_event_count(connection) -> int:
    return int(
        connection.execute(
            "SELECT count(*) FROM ledger_lot_events"
        ).fetchone()[0]
    )


def test_post_can_skip_lot_projection_while_persisting_facts(connection):
    connection.execute("BEGIN IMMEDIATE")
    result = Ledger(connection).post(_buy(key="buy"), project_lots=False)
    connection.commit()

    assert result.created is True
    assert connection.execute(
        "SELECT count(*) FROM ledger_transactions"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT count(*) FROM ledger_postings"
    ).fetchone()[0] == 2
    assert connection.execute(
        "SELECT count(*) FROM ledger_lots"
    ).fetchone()[0] == 0
    assert _lot_event_count(connection) == 0


def test_reverse_can_skip_dependent_lot_projection(connection):
    connection.execute("BEGIN IMMEDIATE")
    ledger = Ledger(connection)
    original = ledger.post(_buy(key="original"))
    before_events = _lot_event_count(connection)
    reversal = ledger.reverse(
        original.transaction_id,
        source_namespace="correction",
        idempotency_key="reverse-original",
        project_lots=False,
    )
    connection.commit()

    row = connection.execute(
        "SELECT reverses_transaction_id FROM ledger_transactions WHERE id=?",
        (reversal.transaction_id,),
    ).fetchone()
    assert row[0] == original.transaction_id
    assert _lot_event_count(connection) == before_events


def test_replace_can_skip_both_lot_projections(connection):
    connection.execute("BEGIN IMMEDIATE")
    ledger = Ledger(connection)
    original = ledger.post(_buy(key="original"))
    before_events = _lot_event_count(connection)
    reversal, replacement = ledger.replace(
        original.transaction_id,
        _buy(key="replacement", amount=2_000),
        reversal_source_namespace="correction",
        reversal_idempotency_key="reverse-original",
        project_lots=False,
    )
    connection.commit()

    reversal_link = connection.execute(
        "SELECT reverses_transaction_id FROM ledger_transactions WHERE id=?",
        (reversal.transaction_id,),
    ).fetchone()[0]
    replacement_link = connection.execute(
        """SELECT replacement_for_transaction_id FROM ledger_transactions
           WHERE id=?""",
        (replacement.transaction_id,),
    ).fetchone()[0]
    assert reversal_link == original.transaction_id
    assert replacement_link == original.transaction_id
    assert _lot_event_count(connection) == before_events
