# Tests and verification

Verified November 21, 2026. This snapshot contains the same application code as
the tested private development repository. It starts with its own Git history.

## Automated checks

The full suite passed **104 Python tests** and **6 JavaScript tests**. Ruff lint
and formatting, JavaScript syntax, and Python dependency checks also passed.

Python tests cover environment validation, local request restrictions, CSRF,
encrypted persistence, owner isolation, duplicate token submissions, transaction
sync pagination, rollback, concurrent syncs, filters, spending calculations,
connection recovery, and read-only demo guards.

Database integration tests use real PostgreSQL in isolated temporary schemas.
Plaid API responses are mocked. The JavaScript tests use a fake Link SDK.
Gunicorn tests run the actual startup script and check HTTP behavior.

Run the commands in the [README](../README.md#run-checks). Database tests skip
unless `POCKETVIEW_TEST_POSTGRES=1` or `TEST_DATABASE_URL` is set. The included
[GitHub Actions workflow](../.github/workflows/ci.yml) uses disposable PostgreSQL
and needs no Plaid credentials. Its badge reports this public repository's runs.

## Actual Sandbox checks

- A user completed Link, then real Sandbox exchange/accounts/sync calls saved
  14 accounts and 48 transactions.
- Repeat Sync kept the same rows with no duplicates; restarting Flask retained data.
- A simulated login failure led to user-completed Link update mode. Recovery kept
  the same Item and encrypted token, 14 accounts, and all 48 transactions.
- Completing recovery before sign-in returned 409 and retained saved history.
- A separate temporary Sandbox persona imported test coffee/payroll records.
  Search found them and spending included only the outflow. This backend journey
  used Sandbox helpers, rather than browser Link, to create its temporary Item.
- Disconnect succeeded for a temporary Item. Test Items were cleaned up; the
  original connection was retained.

These financial records were simulated. Live checks don't establish Production
readiness or formal Plaid certification. OAuth, public webhooks, and real bank
connections are outside the app's scope. Human Link cancellation was not separately
recorded; fake-SDK cancellation checks passed.

## Hosted demo

URL: https://pocketview-demo-o317.onrender.com

Render Free runs Flask/Gunicorn in Oregon, using a dedicated Neon Free PostgreSQL
17 database. The hosted dataset is separate from imported Sandbox records.

Chrome checks against the public HTTPS site passed:

- Dashboard and `/healthz` returned 200.
- Two fictional accounts and 56 transactions; pages contained 25, 25, and 6 rows.
- Corner Coffee search found 8 rows without changing overview spending.
- November dates narrowed coffee to 2 rows. Clearing search retained dates and
  showed all 14 November records.
- Category/Month selection updated the chart, exact table, and URL immediately.
- Posted outflows totaled $6,809.96 across the sample dataset.
- The page fit a 390px mobile viewport without horizontal overflow.
- API requests and write attempts returned 403. No bank-action controls, Plaid
  browser requests, cookies, or JavaScript errors were observed.

The latest local server recheck was not performed during hosted verification
because that server was stopped. The separate local configuration and database
were not changed by deployment.
