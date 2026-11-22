# Troubleshooting Pocketview

Start by identifying the layer: browser, Flask, local PostgreSQL, or Plaid Sandbox.
Use an error's request/correlation reference to investigate without copying keys,
tokens, `.env`, full API responses or database connection strings into chat/issues.

## Local startup

From the project root, activate the virtual environment and run:

```sh
source .venv/bin/activate
docker compose up -d --wait db
flask --app pocketview db-migrate
FLASK_DEBUG=0 flask --app pocketview run --host 127.0.0.1 --port 5087 --no-debugger --no-reload
```

If a server is already running on 5087, use its terminal's Ctrl+C before restarting.
Restart Flask after editing configuration; it deliberately does not auto-reload.

| Symptom | First check and next action |
| --- | --- |
| Docker command cannot reach the daemon | Start Docker Desktop, then retry `docker compose up -d --wait db`. |
| Database unavailable | Run `docker compose ps db`; expect healthy. Run migrations. The local port is 55439. |
| Database authentication fails after generating another `.env` | The Docker volume retains its original password. Restore the matching local configuration. Do not delete the volume to troubleshoot data you want to keep. |
| Migration changed | Restore the applied SQL file. Add a new numbered migration for a real schema change. |
| Host/access rejected | Open `http://127.0.0.1:5087`. The current app intentionally accepts loopback access only. |
| Session/CSRF error | Reload, then repeat the button action with the fresh session. |
| Cannot decrypt connection | Restore the original `TOKEN_ENCRYPTION_KEY` locally and restart; generating another key cannot recover an old token. |

## Link and credentials

Set `PLAID_CLIENT_ID` and the matching **Sandbox** `PLAID_SECRET` only in local
`.env`. Keep `PLAID_ENV=sandbox` and restart Flask. A Production secret does not
authenticate Sandbox calls. Plaid MCP login does not configure this Flask app.

| Symptom | Next action |
| --- | --- |
| Connect disabled with setup message | Configure both local Plaid fields, then restart. |
| Secret-format or API-key rejection | Recopy the complete matching Sandbox secret locally; verify the Dashboard section says Sandbox. Do not share its value. |
| Link did not load or timed out | Check internet access/content blockers. Use the exposed retry; reload if necessary. |
| Cannot find Platypus | Try First Gingham Credit Union and its password-based Sandbox flow. |
| Phone step is confusing | Skip it. If required: Sandbox phone `415-555-0011`, SMS code `123456`; bank MFA is separately `1234`. |
| Saved demo-bank selection fails | Choose Add new account; the seeded saved-bank share flow is not reliably simulated. |
| Institution asks for OAuth redirect | Exit and choose a password-based Sandbox institution. OAuth is outside this demo's scope. |
| Accounts did not load | Use Retry accounts on the saved connection; avoid creating a second Item. |
| Exchange outcome is uncertain | Inspect Plaid Dashboard Logs. An exchange is single-use; the app intentionally refuses a blind repeat. Clean up any orphaned Sandbox Item before intentionally connecting again. |

Use `user_good` / `pass_good`, and bank MFA `1234` when requested. These are public
Sandbox test credentials, never a request for real banking credentials.

## Transactions and chart

| Symptom | Explanation and next action |
| --- | --- |
| History is preparing | The initial response can be empty. Wait, then click Sync; reloading only reads saved data. |
| Completed update has no USD activity | Different from a failure. Non-USD records are stored separately and excluded from this view. |
| Sync already running | Wait and reload. One request owns this Item's database lock. |
| Last sync failed | Old rows/cursor remain. Follow the displayed next step, then retry Sync. |
| Invalid-response/database error | Inspect the failed attempt with the SQL below. Fix the reported layer; do not edit the cursor to skip records. |
| Merchant search finds nothing | Clear filters; search matches merchant or original description, not account names. Dates are inclusive. |
| Chart is smaller than table outflows | Pending activity, inflows/refunds and recognized transfers/card payments are excluded. It is a spending estimate, not a balance. |
| Chart total seems larger than the visible page | Aggregates cover every matching page, while the table shows 25 rows at a time. |
| Chart does not draw | Expand View exact amounts as a table. The values still render without Chart.js. Check browser JavaScript errors. |

Plaid's positive amount means money out; Pocketview displays that with a minus
sign. Negative Plaid amounts are money in, displayed with a plus sign. Refunds
are not subtracted from the chart. Categorization can be uncertain or incorrect.

## Reconnect without losing history

1. Click Check status, or follow a failed Sync's sign-in/permission message.
2. Click Reconnect on the existing connection and finish the Sandbox sign-in.
3. Confirm the Reconnected message and a successful Sync timestamp.

To deliberately reproduce a sign-in failure, use another terminal:

```sh
source .venv/bin/activate
flask --app pocketview sandbox-login-required
```

If there is more than one connection, run `flask --app pocketview connections`,
then pass a local UUID to `sandbox-login-required`. Reload the page afterward.
The CLI refuses non-Sandbox configuration and never prints access tokens.

- Canceling Link leaves history saved and Reconnect available.
- A successful sign-in followed by a failed Sync still preserves the old records;
  retry Sync after addressing the displayed cause.
- Access ended/revoked is different: the unusable token is purged. History remains
  until explicit Disconnect, then a new connection can be created intentionally.

## Investigate using SQL and safe logs

Run the read-only queries against the local database. Use your configured
`DEMO_OWNER_ID`; the example below is the default opaque local owner, not a key.

```sh
docker compose exec -T db psql -U pocketview -d pocketview \
  -v owner_id=pocketview-local-demo < docs/investigation.sql
```

The script reports schema versions, connection states, active-account/transaction
counts, recent sync attempts, per-currency activity, duplicate/pending checks and
the chart's exact calculation. It does not select tokens or cursor values. Some
output is still financial activity; inspect it locally rather than posting dumps.

Application logs use three useful references:

- **Correlation ID:** joins one browser action to its Flask/Plaid calls.
- **Plaid request ID:** locate the upstream request in Dashboard Logs.
- **Local Item reference:** connects the operation to a saved local connection.

An attempt outcome of failed with `INTERRUPTED` means a later sync found an earlier
unfinished attempt. The next sync resumes from the last committed cursor. Never
set the cursor to `now` or delete diagnostic rows to make an error disappear.
