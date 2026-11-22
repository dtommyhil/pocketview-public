# Run Pocketview locally

[Back to the project](../README.md) · [Live demo](https://pocketview-demo-o317.onrender.com)

## Start locally

Requires Python 3.13+ and Docker Desktop with Docker Compose. Start Docker Desktop.
Node.js is needed only for the JavaScript tests. Flask runs on your computer;
PostgreSQL runs in Docker.

From a checkout of this repository:

```sh
cd pocketview
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python scripts/init_env.py
docker compose up -d --wait db
flask --app pocketview db-migrate
FLASK_DEBUG=0 flask --app pocketview run --host 127.0.0.1 --port 5087 --no-debugger --no-reload
```

Open **http://127.0.0.1:5087**. The database listens on localhost port **55439**.
The dashboard works without Plaid credentials; connecting a simulated bank requires
one local configuration step below.

For the existing workspace, keep `.venv` and `.env` and start from
`source .venv/bin/activate`. The setup script refuses to overwrite an existing
`.env`. Migrations can be repeated; do not edit an already-applied SQL migration.

Stop Flask with Ctrl+C. `docker compose stop db` stops PostgreSQL while preserving
its data. Restart using the same configuration and Docker volume.

## Configure Plaid Sandbox locally

1. Open [Plaid Dashboard → Developers → Keys](https://dashboard.plaid.com/developers/keys).
   Find your client ID and matching **Sandbox** secret.
2. Edit the ignored `.env` in the repository root locally.
3. Set `PLAID_CLIENT_ID` and `PLAID_SECRET` in that file. Keep `PLAID_ENV=sandbox`.
4. Save and restart Flask.

Do not put keys in chat, source code, GitHub secrets for CI, or `.env.example`.
The environment initializer creates local database/session/encryption keys with
owner-only file permissions. Preserve `TOKEN_ENCRYPTION_KEY`: a replacement cannot
read existing encrypted tokens. Actual process environment settings override `.env`.
Plaid MCP authentication is separate from this Flask application's credentials.

## Try the app

1. Click **Connect Sandbox bank**. Choose a password-based Sandbox institution,
   such as **First Gingham Credit Union**.
2. Use `user_good` / `pass_good`; bank MFA code `1234` if asked. Skip the optional
   phone step. Choose **Add new account** if offered saved demo-bank entries.
3. Complete Link. Accounts are saved, then the initial sync starts. If history is
   still preparing, wait and click **Sync**.
   Under Bank connections, **Show accounts** expands the compact account list;
   **Hide accounts** closes it again. Connection actions and sync status stay visible.
4. Repeat Sync: transaction rows should not duplicate. Search, filter, paginate,
   and switch the spending chart between Category and Month. The dropdown updates
   the chart and exact-amounts table immediately, without reloading the page.
   Dates control the overview and table together; merchant search/category narrow
   only the transaction table. Clearing transaction search preserves the selected dates.
5. Reload/restart Flask: saved data remains in PostgreSQL.

A failed account load has **Retry accounts**. A failed sync retains the previous
rows/cursor and offers a next step. **Disconnect** explicitly removes that
connection and its local history; use it only when you intend to remove them.

These local tests use **non-OAuth** institutions. OAuth redirects/resume and public
webhooks are outside the current scope. Never enter real banking credentials.
Phone/SMS guidance and common Sandbox errors are in [troubleshooting](TROUBLESHOOTING.md).

### Reproduce and repair a connection failure

With Flask running, open a second terminal in the project directory:

```sh
source .venv/bin/activate
flask --app pocketview sandbox-login-required
```

Reload the dashboard, click **Reconnect** on the saved connection, and complete
Link with the same Sandbox credentials. Confirm the Reconnected message and a
successful Sync. The backend checks actual Item health before claiming success;
update mode retains the same Item/access token and resumes its cursor.

With several saved connections, run `flask --app pocketview connections`, then
supply one **local** UUID to `sandbox-login-required`. The simulation is local CLI
only and refuses non-Sandbox configuration. Canceling Reconnect preserves history.
