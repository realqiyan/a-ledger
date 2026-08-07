# a-ledger

`a-ledger` is an application-neutral, SQLite-backed double-entry ledger SDK.
It owns accounting primitives and persistence; calling applications own business
commands, broker integration, market calendars, projections, APIs, and pages.

## Language

**Ledger**:
The application-neutral engine that records accounting entries inside a
caller-owned database transaction.
_Avoid_: Book, books

**Portfolio**:
A named, single-currency scope that owns accounts, transactions, and
reservations.

**Account**:
A named ledger account that belongs to exactly one portfolio and one currency
and is classified by an AccountCategory.

**AccountCategory**:
One of `ASSET`, `LIABILITY`, `EQUITY`, `INCOME`, or `EXPENSE`.

**Money**:
An amount expressed as integer minor units of a currency; never a float.

**Posting**:
One account leg of a transaction, with a signed integer amount where a positive
amount is a debit and a negative amount is a credit.
_Avoid_: Entry, line

**Transaction**:
A balanced set of postings whose amounts sum to zero, carrying identity,
classification, and dimensions.
_Avoid_: Entry, voucher

**Idempotency Key**:
The `(portfolio, source namespace, key)` triple that makes posting the same
transaction twice a no-op.

**Source Namespace**:
The calling application's namespace that owns an idempotency key.

**Event Code**:
The calling application's semantic label for a transaction (e.g.
`CAPITAL_FLOW`); opaque to the ledger.

**Dimensions**:
Arbitrary JSON key/value data attached to a transaction or posting; opaque to
the ledger.

**Lot**:
An identifiable quantity of an instrument held in an account, acquired by one
transaction and consumed in whole or in part by later ones.

**Lot Allocation**:
An explicit instruction selecting which lot or lots a quantity posting consumes.

**Lot Operation**:
An explicit `OPEN` or `CLOSE` directive on a quantity posting.

**Lot Event**:
An immutable event on a lot (`ACQUIRE`, `CONSUME`, or `REVERSAL`) from which
quantities and costs are replayed.

**Reservation**:
Mutable authorization state that sets aside an amount or a quantity; not an
accounting entry.
_Avoid_: Hold, 冻结

**Reversal**:
A linked correction transaction that exactly negates an original posted
transaction.

**Replacement**:
A reversal of an original transaction immediately followed by a new transaction
that supersedes it.

**Audit Event**:
An immutable record of a ledger action, for inspection rather than accounting.
