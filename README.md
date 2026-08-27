# a-ledger

`a-ledger` is an application-neutral, SQLite-backed double-entry ledger SDK.
It owns accounting primitives and persistence; calling applications own business
commands, broker integration, market calendars, projections, APIs, and pages.

## Accounting convention

- Money is stored as integer minor units; posted amounts never use `float`.
- A positive posting is a debit and a negative posting is a credit.
- Every posted transaction balances exactly to zero.
- Accounts use one of `ASSET`, `LIABILITY`, `EQUITY`, `INCOME`, or `EXPENSE`.
- Security quantities and cost lots are replayed from immutable lot events. There
  are no persisted balance or position snapshots.
- Posted transactions, postings, lots, lot events, and audit events cannot be
  updated or deleted. Corrections use linked reversal and replacement entries.
- Reservations are mutable authorization state, not accounting entries.

## Transaction ownership

The caller supplies a `sqlite3.Connection`, enables a short transaction, and
decides whether to commit or roll back. Write APIs reject calls without an active
caller-owned transaction. The SDK never calls `commit()` or `rollback()` and
never runs network or application callbacks.

```python
import sqlite3

from a_ledger import AccountCategory, Ledger, PostingDraft, TransactionDraft

connection = sqlite3.connect("application.sqlite3")
ledger = Ledger(connection)  # enables and verifies SQLite foreign keys

connection.execute("BEGIN IMMEDIATE")
ledger.install_schema()
ledger.create_portfolio("portfolio-uuid", code="main", currency="CNY")
ledger.create_account(
    "cash-account-uuid",
    portfolio_id="portfolio-uuid",
    code="ASSET:CASH",
    category=AccountCategory.ASSET,
    currency="CNY",
)
ledger.create_account(
    "capital-account-uuid",
    portfolio_id="portfolio-uuid",
    code="EQUITY:CONTRIBUTED_CAPITAL",
    category=AccountCategory.EQUITY,
    currency="CNY",
)
ledger.post(
    TransactionDraft(
        portfolio_id="portfolio-uuid",
        source_namespace="app.capital",
        idempotency_key="capital-flow-uuid",
        event_code="CAPITAL_FLOW",
        business_date="2026-08-03",
        currency="CNY",
        postings=(
            PostingDraft("cash-account-uuid", 100_00),
            PostingDraft("capital-account-uuid", -100_00),
        ),
    )
)
connection.commit()
```

## Lot operations

Quantity postings may select a `LotOperation` per posting index:

- `OPEN` always acquires a new signed lot.
- `CLOSE` consumes opposite-side lots and rejects insufficient quantity.
- `NET` consumes opposite-side lots FIFO, then opens any remaining quantity as a
  new same-side lot. When a posting crosses zero, its absolute amount is split by
  quantity and the final new-lot segment receives the integer remainder. `NET`
  does not accept explicit lot allocations.

Advanced callers that own a compatible lot projection may pass
`project_lots=False` to `post`, `reverse`, or `replace`. The SDK still persists
the immutable transaction, postings, and reversal/replacement links, but writes
no lot or lot-event rows for that operation. The caller must rebuild its lot
projection in the same SQLite transaction before committing. The default is
`True` and preserves the SDK-managed FIFO behavior.

## Non-goals

The package does not define trading strategies, orders, options, QMT behavior,
HTTP APIs, UI, market prices, settlement calendars, or bank/broker transfers.
Generic `event_code`, `source_type`, and JSON dimensions let each application
attach its own domain semantics without coupling those semantics to the SDK.
