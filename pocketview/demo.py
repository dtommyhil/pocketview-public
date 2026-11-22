"""Illustrative public-demo fixtures. No Plaid credentials, tokens or API calls."""

import calendar
import uuid
from datetime import date
from decimal import Decimal

import click
import psycopg

from pocketview.config import PUBLIC_DEMO_OWNER
from pocketview.db import connect
from pocketview.store import Store

DEMO_ITEM_ID = uuid.uuid5(uuid.NAMESPACE_URL, "pocketview/portfolio-demo/item")


def sample_transactions(anchor=None):
    """Four months of fictional activity, with exact decimal amounts."""
    anchor = anchor or date.today()
    examples = [
        (1, "Example payroll", "-3250.00", "INCOME", None, False),
        (2, "Example apartment", "1350.00", "RENT_AND_UTILITIES", None, False),
        (4, "Neighborhood Grocer", "82.45", "FOOD_AND_DRINK", None, False),
        (6, "Corner Coffee", "6.50", "FOOD_AND_DRINK", None, False),
        (8, "City Transit", "24.00", "TRANSPORTATION", None, False),
        (10, "Neighborhood Grocer", "65.20", "FOOD_AND_DRINK", None, False),
        (12, "Music subscription", "10.99", "ENTERTAINMENT", None, False),
        (14, "Market Kitchen", "42.80", "FOOD_AND_DRINK", None, False),
        (16, "Corner Coffee", "8.75", "FOOD_AND_DRINK", None, False),
        (17, "Sample savings transfer", "300.00", "TRANSFER_OUT", None, False),
        (
            18,
            "Sample credit card payment",
            "220.00",
            "LOAN_PAYMENTS",
            "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
            False,
        ),
        (19, "Sample store refund", "-24.00", "GENERAL_MERCHANDISE", None, False),
        (20, "Local market", "31.25", None, None, False),
        (21, "Weekend booking", "79.40", "TRAVEL", None, True),
    ]
    rows = []
    for offset in range(4):
        month_index = anchor.year * 12 + anchor.month - 1 - offset
        year, zero_month = divmod(month_index, 12)
        month = zero_month + 1
        last_day = anchor.day if offset == 0 else calendar.monthrange(year, month)[1]
        for number, (day, merchant, amount, primary, detailed, pending) in enumerate(examples):
            value = Decimal(amount)
            if merchant == "Neighborhood Grocer":
                value += Decimal(offset * 7)
            rows.append(
                (
                    DEMO_ITEM_ID,
                    f"portfolio-demo-{offset}-{number}",
                    "portfolio-demo-checking"
                    if number in {0, 1, 4, 9, 10}
                    else "portfolio-demo-credit",
                    date(year, month, min(day, last_day)),
                    merchant,
                    merchant,
                    value,
                    "USD",
                    pending and offset == 0,
                    primary,
                    detailed,
                    "LOW" if merchant == "Market Kitchen" else "HIGH" if primary else None,
                )
            )
    return rows


def seed_demo(store, anchor=None):
    """Only touch the fixed portfolio fixture Item; preserve all other owners."""
    if store.owner_id != PUBLIC_DEMO_OWNER:
        raise click.ClickException("Sample data can only be loaded for the public demo owner.")
    with connect(store.database_url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (810035002,))
        unexpected = conn.execute(
            "SELECT 1 FROM plaid_items WHERE owner_id=%s AND id<>%s LIMIT 1",
            (PUBLIC_DEMO_OWNER, DEMO_ITEM_ID),
        ).fetchone()
        if unexpected:
            raise click.ClickException(
                "The public sample owner contains unexpected Items; stopped."
            )
        existing = conn.execute(
            "SELECT owner_id, plaid_item_id, access_token_encrypted FROM plaid_items WHERE id=%s",
            (DEMO_ITEM_ID,),
        ).fetchone()
        if existing and (
            existing["owner_id"] != PUBLIC_DEMO_OWNER
            or existing["plaid_item_id"] != "portfolio-demo-item"
            or existing["access_token_encrypted"] is not None
        ):
            raise click.ClickException(
                "The sample Item reference is already in use; no changes made."
            )
        conn.execute(
            "INSERT INTO owners(id) VALUES (%s) ON CONFLICT DO NOTHING", (PUBLIC_DEMO_OWNER,)
        )
        conn.execute(
            """INSERT INTO plaid_items
                (id, owner_id, plaid_item_id, access_token_encrypted, institution_name,
                 connection_state, sync_state)
                VALUES (%s, %s, 'portfolio-demo-item', NULL, 'Example Bank', 'connected', 'ready')
                ON CONFLICT (id) DO NOTHING""",
            (DEMO_ITEM_ID, PUBLIC_DEMO_OWNER),
        )
        Store.upsert_accounts(
            conn,
            DEMO_ITEM_ID,
            [
                {
                    "account_id": "portfolio-demo-checking",
                    "name": "Everyday Checking",
                    "type": "depository",
                    "subtype": "checking",
                    "mask": "4242",
                    "balances": {"iso_currency_code": "USD"},
                },
                {
                    "account_id": "portfolio-demo-credit",
                    "name": "Everyday Credit",
                    "type": "credit",
                    "subtype": "credit card",
                    "mask": "1010",
                    "balances": {"iso_currency_code": "USD"},
                },
            ],
        )
        # Replacing this reproducible fixture is atomic and never touches live Items.
        conn.execute("DELETE FROM transactions WHERE item_id=%s", (DEMO_ITEM_ID,))
        with conn.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO transactions
                (item_id, plaid_transaction_id, plaid_account_id, transaction_date,
                 merchant_name, original_description, amount, currency_code, pending,
                 category_primary, category_detailed, category_confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                sample_transactions(anchor),
            )


def register_cli(app):
    @app.cli.command("seed-demo")
    def load_demo():
        """Load fictional read-only portfolio data; refuses local integration mode."""
        if app.config["APP_MODE"] != "demo":
            raise click.ClickException("Set APP_MODE=demo to load the portfolio sample dataset.")
        try:
            seed_demo(app.extensions["store"])
        except psycopg.Error:
            raise click.ClickException(
                "Could not load sample data. Check database setup and migrations."
            ) from None
        click.echo("Loaded 2 sample accounts and 56 fictional transactions; no Plaid calls made.")
