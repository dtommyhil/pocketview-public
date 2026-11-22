import os
import uuid
from types import SimpleNamespace

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from dotenv import dotenv_values
from psycopg import sql
from psycopg.conninfo import make_conninfo

from pocketview import create_app
from pocketview.config import ROOT
from pocketview.db import migrate
from pocketview.plaid import PlaidClient


def empty_exploration(filters):
    return {
        "rows": [],
        "total": 0,
        "page": 1,
        "pages": 1,
        "categories": [],
        "by_category": [],
        "by_month": [],
        "summary": {"amount": 0, "count": 0, "uncertain": 0},
    }


@pytest.fixture
def config():
    return {
        "TESTING": True,
        "APP_MODE": "local",
        "PUBLIC_HOSTNAME": "",
        "DATABASE_URL": "postgresql://unused:unused@127.0.0.1:1/unused",
        "FLASK_SECRET_KEY": "test-session-key-which-is-at-least-32-characters",
        "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "DEMO_OWNER_ID": "test-owner",
        "PLAID_ENV": "sandbox",
        "PLAID_CLIENT_ID": "fake-client-id",
        "PLAID_SECRET": "fake-secret-do-not-log",
    }


@pytest.fixture
def app(config):
    application = create_app(config)
    application.extensions["store"] = SimpleNamespace(
        list_items=lambda: [], list_transactions=lambda: [], explore=empty_exploration
    )
    return application


@pytest.fixture
def database_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url and os.environ.get("POCKETVIEW_TEST_POSTGRES") == "1":
        url = dotenv_values(ROOT / ".env").get("DATABASE_URL")
    if not url:
        pytest.skip("Set POCKETVIEW_TEST_POSTGRES=1 or TEST_DATABASE_URL for PostgreSQL tests.")
    schema = "pocketview_test_" + uuid.uuid4().hex
    # Only this randomly generated schema is created/deleted; public data is untouched.
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as admin:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    except psycopg.Error:
        pytest.fail(
            "Test database is unavailable. Check Docker and local database settings.", pytrace=False
        )
    isolated_url = make_conninfo(url, options=f"-csearch_path={schema}")
    try:
        migrate(isolated_url, "test-owner")
        yield isolated_url
    finally:
        with psycopg.connect(url, autocommit=True, connect_timeout=5) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def connected_app(config, database_url):
    config["DATABASE_URL"] = database_url
    application = create_app(config)
    requests = []

    def respond(request):
        requests.append(request)
        responses = {
            "/link/token/create": {"link_token": "link-sandbox-test"},
            "/item/public_token/exchange": {
                "access_token": "access-sandbox-do-not-log",
                "item_id": "plaid-item-1",
            },
            "/accounts/get": {
                "accounts": [
                    {
                        "account_id": "account-1",
                        "name": "Plaid Checking",
                        "type": "depository",
                        "subtype": "checking",
                        "mask": "0000",
                        "balances": {"iso_currency_code": "USD"},
                    },
                    {
                        "account_id": "account-2",
                        "name": "Plaid Savings",
                        "type": "depository",
                        "subtype": "savings",
                        "mask": "1111",
                        "balances": {"iso_currency_code": "USD"},
                    },
                ]
            },
            "/item/remove": {},
            "/transactions/sync": {
                "added": [],
                "modified": [],
                "removed": [],
                "has_more": False,
                "next_cursor": "cursor-initial",
                "transactions_update_status": "NOT_READY",
            },
            "/item/get": {"item": {"item_id": "plaid-item-1", "error": None}},
            "/sandbox/item/reset_login": {"reset_login": True},
        }
        return httpx.Response(200, json={**responses[request.url.path], "request_id": "request123"})

    application.extensions["plaid"] = PlaidClient(
        config["PLAID_CLIENT_ID"],
        config["PLAID_SECRET"],
        transport=httpx.MockTransport(respond),
    )
    application.extensions["test_requests"] = requests
    return application


def csrf_headers(client):
    assert client.get("/").status_code == 200
    with client.session_transaction() as session:
        return {"X-CSRF-Token": session["csrf_token"]}


@pytest.fixture
def exchange_connection(connected_app):
    client = connected_app.test_client()
    headers = csrf_headers(client)
    response = client.post(
        "/api/exchange",
        headers=headers,
        json={
            "public_token": "public-sandbox-test-token",
            "institution_name": "Test Bank",
        },
    )
    assert response.status_code == 200
    item = connected_app.extensions["store"].list_items()[0]
    return client, headers, item
