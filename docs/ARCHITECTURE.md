# How Pocketview works

Pocketview connects a simulated bank, imports transaction changes and makes the
saved activity searchable. Flask handles web requests; PostgreSQL keeps the data;
Plaid Sandbox supplies the bank connection and financial records.

```text
Browser: HTML + small JavaScript controls + Chart.js
   |
   | GET page / POST button action
   v
Flask: validate inputs, check local owner/CSRF, coordinate operations
   |                                    |
   | parameterized SQL                  | authenticated HTTPS
   v                                    v
PostgreSQL                         Plaid Sandbox
saved records + cursors             Link + accounts + updates
```

The browser also opens Plaid's hosted Link window for simulated sign-in. It does
not receive the client secret or access token. There is no real bank connection,
money movement, automatic polling or public webhook receiver.

## Connect once, then keep the saved permission

1. Flask creates a temporary Link token for Transactions and US institutions.
2. The visitor completes the simulated sign-in in Plaid Link.
3. JavaScript sends the resulting temporary public token to Flask.
4. Flask reserves its SHA-256 digest in `exchange_attempts`, then exchanges it once.
5. The resulting access token is Fernet-encrypted before PostgreSQL stores it.
6. Flask loads account metadata and attempts the first transaction sync.

An **Item** represents one institution login, which may include multiple accounts.
The core relationship is `owners → plaid_items → accounts → transactions`.
`exchange_attempts` and `sync_attempts` retain sanitized operation outcomes.

Exchange is an external side effect: Plaid and PostgreSQL cannot commit together.
The durable reservation prevents blindly repeating an exchange after an ambiguous
timeout. A saved Item can retry account retrieval without starting Link again.
A crash before saving an exchanged token can still leave an orphaned Sandbox Item;
inspect Plaid Dashboard Logs rather than automatically relinking.

## Sync changes and cursor together

A cursor is Plaid's bookmark for an Item's transaction-update stream.

```text
Acquire Item lock → read saved cursor → collect every update page
    → apply added/modified/removed + pending replacements
    → save terminal cursor and success timestamp → COMMIT
```

- A PostgreSQL session advisory lock serializes syncs for the same Item. Another
  request gets a clear busy response. Different Items can sync independently.
- Plaid pages stay in memory until `has_more` is false. There are at most 100 pages
  per attempt, requesting up to 500 updates per page.
- On pagination mutation or a retryable failure, buffers are discarded and the
  next bounded attempt starts from the cursor held at the beginning. There are
  at most three complete attempts.
- Added/modified records upsert using `(item_id, plaid_transaction_id)`. Removed
  records are deleted. `pending_transaction_id` reconciles a pending purchase
  with its posted successor, including when they arrive on different pages.
- Rows, cursor, success timestamp and successful-attempt record commit together.
  A failure rolls back that transaction; a separate failure record survives.
- Interrupted attempts are marked failed when a later sync acquires the lock.
  Closing a crashed worker's database connection releases its session lock.

The network fetch runs while the Item row is locked. That keeps this small demo
consistent, but a long backfill can make other operations on that Item wait. A
larger system would move work to a queue and design its concurrency explicitly.

Tests in `tests/test_sync.py` exercise complete pagination, rollback, mutation
restarts, concurrent requests, pending replacement and duplicate prevention.

## Explore the saved copy

Search/date/category filters are validated in `explore.py`. `Store.explore()` uses
parameterized SQL and one read-only repeatable-read transaction, so the count,
visible rows and aggregate totals describe one consistent snapshot.

The table shows 25 matching USD records per page. Shared dates affect both the
chart and table. Merchant search/category filters narrow only the table; chart
aggregates cover the entire selected period. Each query group still uses one
read-only snapshot. Filtering does not call Plaid or advance a sync cursor.

Amounts are parsed as Python `Decimal` and stored/summed as PostgreSQL `numeric`.
The spending estimate includes positive Plaid amounts representing posted USD
outflows. It excludes pending records, inflows/refunds, `TRANSFER_IN`/`TRANSFER_OUT`
primary categories, and `LOAN_PAYMENTS_CREDIT_CARD_PAYMENT` detailed categories.
Refunds are excluded rather than netted. Other loan payments remain included;
misclassification can affect the estimate. Uncertain categorization is disclosed.

One Chart.js canvas switches between category and month. JavaScript converts
decimal strings to numbers only for drawing. Exact server-rendered amounts remain
available in an accessible table. There is no currency conversion or balance total.

## Repair an existing connection

```text
Check status / failed Sync → sign-in needed → Reconnect
   → existing-token Link update mode → verify /item/get health
   → sync the same Item from its stored cursor
```

Update mode never repeats the public-token exchange or replaces the Item. Missing
Transactions consent is requested when needed. A browser completion message alone
is insufficient: Flask checks actual health before claiming repair. Canceling or
failing leaves history available. A failed sync after repair preserves the old
cursor and records so Sync can be retried.

For a revoked/missing Item, its unusable encrypted token is purged while imported
history remains. Explicit Disconnect removes local accounts, transactions and
attempts, and removes the remote Item when its token remains usable.

The local `sandbox-login-required` command reproduces the documented Sandbox
failure. It is not exposed through a web endpoint.

## Boundaries and tradeoffs

- One configured local demo owner, loopback requests only, trusted localhost hosts.
  Link is not application authentication. Public access needs separate work.
- CSRF for mutations; HttpOnly/SameSite cookies; debugger disabled; safe errors.
- Access tokens encrypted at rest. The encryption key stays outside PostgreSQL;
  replacing it makes old tokens unreadable. This does not encrypt every field.
- Logs contain operation/correlation/request references, not credentials or payloads.
- Sync is explicitly manual after the initial import. A timestamp means Pocketview
  saved Plaid's available updates; it does not guarantee the bank was just checked.
- Non-OAuth Sandbox institutions only. There is no redirect/resume implementation.
- Cached institution labels, possible duplicates from separate intentional Link
  sessions, and unpersisted Link-exit analytics remain documented demo limits.

## Public read-only mode

`APP_MODE=demo` fixes the owner to a reserved fixture reference, disables credential
and cipher initialization, and rejects every API/mutation request. Its Jinja page
loads only chart JavaScript, not Plaid Link or bank-action handlers. `demo.py` seeds
two illustrative accounts and 56 transactions; no live connection is represented.

`scripts/start_demo.sh` runs migrations/seeding before Gunicorn starts. An exact
public hostname is required for external access. The normal `APP_MODE=local` path
keeps the original loopback/CSRF/token controls and refuses the reserved fixture
owner. This adds a public sample viewer, not public bank connection or app login.
See `DEPLOYMENT.md` for the reviewed hosting configuration.

For a shorter introduction, start with the project README.
