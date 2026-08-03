from __future__ import annotations


SCHEMA_VERSION = 1


DDL_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS ledger_schema_versions (
        version INTEGER PRIMARY KEY,
        installed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_portfolios (
        id TEXT PRIMARY KEY,
        code TEXT NOT NULL UNIQUE,
        currency TEXT NOT NULL,
        minor_unit INTEGER NOT NULL CHECK(minor_unit >= 0),
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at TEXT NOT NULL,
        UNIQUE(id, currency)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_accounts (
        id TEXT PRIMARY KEY,
        portfolio_id TEXT NOT NULL,
        code TEXT NOT NULL,
        category TEXT NOT NULL CHECK(category IN ('ASSET','LIABILITY','EQUITY','INCOME','EXPENSE')),
        currency TEXT NOT NULL,
        name TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(portfolio_id, code),
        UNIQUE(id, portfolio_id),
        FOREIGN KEY(portfolio_id, currency)
            REFERENCES ledger_portfolios(id, currency)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_transactions (
        id TEXT PRIMARY KEY,
        portfolio_id TEXT NOT NULL,
        source_namespace TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        event_code TEXT NOT NULL,
        source_type TEXT,
        business_date TEXT NOT NULL,
        currency TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        reverses_transaction_id TEXT,
        replacement_for_transaction_id TEXT,
        dimensions_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(portfolio_id, source_namespace, idempotency_key),
        UNIQUE(id, portfolio_id),
        FOREIGN KEY(portfolio_id, currency)
            REFERENCES ledger_portfolios(id, currency),
        FOREIGN KEY(reverses_transaction_id, portfolio_id)
            REFERENCES ledger_transactions(id, portfolio_id),
        FOREIGN KEY(replacement_for_transaction_id, portfolio_id)
            REFERENCES ledger_transactions(id, portfolio_id)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ledger_one_reversal_per_transaction
    ON ledger_transactions(portfolio_id, reverses_transaction_id)
    WHERE reverses_transaction_id IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_postings (
        id TEXT PRIMARY KEY,
        transaction_id TEXT NOT NULL,
        portfolio_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        amount_minor INTEGER NOT NULL CHECK(typeof(amount_minor) = 'integer'),
        instrument_code TEXT,
        quantity_delta INTEGER NOT NULL DEFAULT 0 CHECK(typeof(quantity_delta) = 'integer'),
        price_scaled INTEGER,
        price_scale INTEGER,
        dimensions_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(id, transaction_id),
        FOREIGN KEY(transaction_id, portfolio_id)
            REFERENCES ledger_transactions(id, portfolio_id),
        FOREIGN KEY(account_id, portfolio_id)
            REFERENCES ledger_accounts(id, portfolio_id),
        CHECK((quantity_delta = 0) OR (instrument_code IS NOT NULL)),
        CHECK((price_scaled IS NULL AND price_scale IS NULL)
            OR (price_scaled IS NOT NULL AND price_scale IS NOT NULL AND price_scale > 0))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_lots (
        id TEXT PRIMARY KEY,
        portfolio_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        instrument_code TEXT NOT NULL,
        acquired_date TEXT NOT NULL,
        source_transaction_id TEXT NOT NULL,
        source_posting_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(id, portfolio_id),
        FOREIGN KEY(account_id, portfolio_id)
            REFERENCES ledger_accounts(id, portfolio_id),
        FOREIGN KEY(source_transaction_id, portfolio_id)
            REFERENCES ledger_transactions(id, portfolio_id),
        FOREIGN KEY(source_posting_id, source_transaction_id)
            REFERENCES ledger_postings(id, transaction_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_lot_events (
        id TEXT PRIMARY KEY,
        transaction_id TEXT NOT NULL,
        portfolio_id TEXT NOT NULL,
        posting_id TEXT NOT NULL,
        lot_id TEXT NOT NULL,
        event_kind TEXT NOT NULL CHECK(event_kind IN ('ACQUIRE','CONSUME','REVERSAL')),
        quantity_delta INTEGER NOT NULL CHECK(typeof(quantity_delta) = 'integer' AND quantity_delta != 0),
        cost_delta_minor INTEGER NOT NULL CHECK(typeof(cost_delta_minor) = 'integer'),
        reverses_lot_event_id TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(transaction_id, portfolio_id)
            REFERENCES ledger_transactions(id, portfolio_id),
        FOREIGN KEY(posting_id, transaction_id)
            REFERENCES ledger_postings(id, transaction_id),
        FOREIGN KEY(lot_id, portfolio_id)
            REFERENCES ledger_lots(id, portfolio_id),
        FOREIGN KEY(reverses_lot_event_id)
            REFERENCES ledger_lot_events(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_reservations (
        id TEXT PRIMARY KEY,
        portfolio_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('AMOUNT','QUANTITY')),
        status TEXT NOT NULL CHECK(status IN ('ACTIVE','CONSUMED','RELEASED')),
        source_namespace TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        amount_minor INTEGER,
        supplemental_availability_minor INTEGER NOT NULL DEFAULT 0,
        instrument_code TEXT,
        quantity INTEGER,
        consumed_amount_minor INTEGER,
        consumed_quantity INTEGER,
        dimensions_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(portfolio_id, source_namespace, idempotency_key),
        UNIQUE(id, portfolio_id),
        FOREIGN KEY(account_id, portfolio_id)
            REFERENCES ledger_accounts(id, portfolio_id),
        CHECK(
            (kind = 'AMOUNT' AND amount_minor > 0 AND quantity IS NULL AND instrument_code IS NULL)
            OR
            (kind = 'QUANTITY' AND quantity > 0 AND amount_minor IS NULL AND instrument_code IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_audit_events (
        id TEXT PRIMARY KEY,
        portfolio_id TEXT NOT NULL,
        event_code TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(portfolio_id) REFERENCES ledger_portfolios(id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ledger_postings_account_idx
    ON ledger_postings(portfolio_id, account_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ledger_lot_events_lot_idx
    ON ledger_lot_events(portfolio_id, lot_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ledger_active_reservations_idx
    ON ledger_reservations(portfolio_id, account_id, status)
    """,
)


IMMUTABILITY_TRIGGERS = tuple(
    f"""
    CREATE TRIGGER IF NOT EXISTS {table}_{operation.lower()}_immutable
    BEFORE {operation} ON {table}
    BEGIN
        SELECT RAISE(ABORT, '{table} is immutable');
    END
    """
    for table in (
        "ledger_transactions",
        "ledger_postings",
        "ledger_lots",
        "ledger_lot_events",
        "ledger_audit_events",
    )
    for operation in ("UPDATE", "DELETE")
)
