"""Local Sandbox simulation controls. No simulation route is exposed to browsers."""

import uuid

import click
import psycopg
from cryptography.fernet import InvalidToken

from pocketview.connections import RecoveryError, check_health, decrypt_token
from pocketview.db import connect
from pocketview.plaid import PlaidError


def register_cli(app):
    @app.cli.command("connections")
    def connections():
        """List local connection references; never prints Plaid IDs or tokens."""
        for item in app.extensions["store"].list_items():
            click.echo(f"{item['id']}  {item['institution_name']}  {item['connection_state']}")

    @app.cli.command("sandbox-login-required")
    @click.argument("item_id", required=False, type=click.UUID)
    def sandbox_login_required(item_id):
        """Force one local Sandbox connection into the documented login-required state."""
        if app.config["APP_MODE"] == "demo":
            raise click.ClickException("The public demo cannot access Plaid or run simulations.")
        if app.config["PLAID_ENV"] != "sandbox":
            raise click.ClickException("This simulation is available in Sandbox only.")
        if not app.config["PLAID_CLIENT_ID"]:
            raise click.ClickException("Configure Sandbox credentials in the local .env first.")
        store = app.extensions["store"]
        client = app.extensions["plaid"]
        cipher = app.extensions["cipher"]
        try:
            if item_id is None:
                items = store.list_items()
                if len(items) != 1:
                    raise click.ClickException(
                        "Run flask --app pocketview connections, then supply one local ID."
                    )
                item_id = items[0]["id"]
            item = store.get_item(item_id)
            if not item:
                raise click.ClickException("Connection not found for this demo owner.")
            token = decrypt_token(cipher, item)
            with connect(store.database_url) as conn:
                conn.autocommit = True
                locked = conn.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked",
                    (str(item_id),),
                ).fetchone()["locked"]
                if not locked:
                    raise click.ClickException(
                        "A sync is running. Wait, then rerun the simulation."
                    )
                correlation_id = str(uuid.uuid4())
                client.reset_login(token, correlation_id, str(item_id))
                checked = check_health(store, client, cipher, item_id, correlation_id)
            click.echo(f"Sandbox simulation requested. Current state: {checked['state']}.")
            click.echo(
                "Reload the dashboard, choose Reconnect, complete Link, then confirm Sync resumes."
            )
        except (PlaidError, RecoveryError) as error:
            raise click.ClickException(str(error)) from None
        except InvalidToken:
            raise click.ClickException(
                "Restore the original TOKEN_ENCRYPTION_KEY and restart."
            ) from None
        except psycopg.Error:
            raise click.ClickException(
                "Database operation failed. Check Docker and migrations."
            ) from None
