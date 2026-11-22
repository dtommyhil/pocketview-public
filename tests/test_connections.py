import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg
import pytest

from pocketview.db import connect, migrate
from pocketview.plaid import PlaidClient
from pocketview.store import Store
from tests.conftest import csrf_headers

pytestmark = pytest.mark.postgres


def test_migrations_are_repeatable(database_url):
    migrate(database_url, "test-owner")
    with connect(database_url) as conn:
        assert conn.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"] == 3
        assert conn.execute("SELECT count(*) AS n FROM owners").fetchone()["n"] == 1


def test_connection_persists_encrypted_and_renders_accounts(
    connected_app, exchange_connection, caplog
):
    client, headers, item = exchange_connection
    record = connected_app.extensions["store"].get_item(item["id"])
    ciphertext = record["access_token_encrypted"]
    assert "access-sandbox" not in ciphertext
    cipher = connected_app.extensions["cipher"]
    assert cipher.decrypt(ciphertext.encode()).decode() == "access-sandbox-do-not-log"
    page = client.get("/")
    assert "Plaid Checking" in page.text
    assert "Plaid Savings" in page.text
    assert "Test Bank" in page.text
    assert "Connected" in page.text
    for secret in (
        "access-sandbox-do-not-log",
        ciphertext,
        "fake-secret-do-not-log",
        "public-sandbox-test-token",
    ):
        assert secret not in page.text + caplog.text
    with connect(connected_app.config["DATABASE_URL"]) as conn:
        attempt = conn.execute("SELECT * FROM exchange_attempts").fetchone()
        assert attempt["outcome"] == "saved"
        assert (
            attempt["public_token_hash"] == hashlib.sha256(b"public-sandbox-test-token").hexdigest()
        )


def test_duplicate_submission_does_not_exchange_or_duplicate_accounts(
    connected_app, exchange_connection
):
    client, headers, item = exchange_connection
    response = client.post(
        "/api/exchange", headers=headers, json={"public_token": "public-sandbox-test-token"}
    )
    assert response.status_code == 200
    items = connected_app.extensions["store"].list_items()
    assert len(items) == 1
    assert len(items[0]["accounts"]) == 2
    exchanges = [
        r
        for r in connected_app.extensions["test_requests"]
        if r.url.path == "/item/public_token/exchange"
    ]
    assert len(exchanges) == 1


def test_failed_accounts_keep_token_and_allow_retry(connected_app):
    original_client = connected_app.extensions["plaid"]
    original_accounts = original_client.accounts

    def failure(*args):
        from pocketview.plaid import PlaidError

        raise PlaidError("Wait and retry accounts.", code="ACCOUNTS_LIMIT", request_id="support123")

    original_client.accounts = failure
    client = connected_app.test_client()
    headers = csrf_headers(client)
    response = client.post(
        "/api/exchange", headers=headers, json={"public_token": "public-sandbox-test-token"}
    )
    assert response.status_code == 502
    assert "Connection saved" in response.json["error"]
    item = connected_app.extensions["store"].list_items()[0]
    assert item["connection_state"] == "accounts_pending"
    assert "Retry accounts" in client.get("/").text
    original_client.accounts = original_accounts
    retry = client.post(f"/api/items/{item['id']}/accounts", headers=headers)
    assert retry.status_code == 200
    assert len(connected_app.extensions["store"].list_items()[0]["accounts"]) == 2


def test_ambiguous_exchange_is_not_repeated(connected_app):
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret-payload", request=request)

    connected_app.extensions["plaid"] = PlaidClient(
        "id", "secret", transport=httpx.MockTransport(timeout)
    )
    client = connected_app.test_client()
    headers = csrf_headers(client)
    body = {"public_token": "public-sandbox-test-token"}
    first = client.post("/api/exchange", headers=headers, json=body)
    second = client.post("/api/exchange", headers=headers, json=body)
    assert first.status_code == 502
    assert "uncertain" in first.json["error"]
    assert second.status_code == 409
    assert len(calls) == 1
    with connect(connected_app.config["DATABASE_URL"]) as conn:
        assert (
            conn.execute("SELECT outcome FROM exchange_attempts").fetchone()["outcome"]
            == "uncertain"
        )


def test_concurrent_token_reservation_has_one_winner(database_url):
    store = Store(database_url, "test-owner")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(store.reserve_exchange, ["same-token-digest", "same-token-digest"]))
    assert sum(fresh for fresh, attempt in results) == 1


def test_account_write_failure_rolls_back_old_accounts(connected_app, exchange_connection):
    client, headers, item = exchange_connection
    store = connected_app.extensions["store"]
    first = {"account_id": "new-account", "name": "New account", "type": "depository"}
    invalid = {"account_id": "invalid-account", "name": "Invalid", "type": None}
    with pytest.raises(psycopg.errors.NotNullViolation):
        store.save_accounts(item["id"], [first, invalid])
    assert [a["name"] for a in store.list_items()[0]["accounts"]] == [
        "Plaid Checking",
        "Plaid Savings",
    ]


def test_owner_isolation_including_mutations(connected_app, exchange_connection):
    client, headers, item = exchange_connection
    with connect(connected_app.config["DATABASE_URL"]) as conn:
        conn.execute("INSERT INTO owners (id) VALUES (%s)", ("another-owner",))
    other = Store(connected_app.config["DATABASE_URL"], "another-owner")
    assert other.list_items() == []
    assert other.get_item(item["id"]) is None
    assert other.save_accounts(item["id"], []) is False
    other.delete_item(item["id"])
    assert connected_app.extensions["store"].get_item(item["id"]) is not None
    connected_app.extensions["store"] = other
    for action in ("accounts", "disconnect"):
        assert client.post(f"/api/items/{item['id']}/{action}", headers=headers).status_code == 404


def test_disconnect_removes_remote_item_and_local_rows(connected_app, exchange_connection):
    client, headers, item = exchange_connection
    response = client.post(f"/api/items/{item['id']}/disconnect", headers=headers)
    assert response.status_code == 200
    assert connected_app.extensions["test_requests"][-1].url.path == "/item/remove"
    assert connected_app.extensions["store"].list_items() == []
    with connect(connected_app.config["DATABASE_URL"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM accounts").fetchone()["n"] == 0


def test_disconnect_provider_failure_keeps_local_data(connected_app, exchange_connection):
    client, headers, item = exchange_connection

    def failure(request):
        return httpx.Response(503, json={"error_code": "PLANNED_MAINTENANCE"})

    connected_app.extensions["plaid"] = PlaidClient(
        "id", "secret", transport=httpx.MockTransport(failure)
    )
    response = client.post(f"/api/items/{item['id']}/disconnect", headers=headers)
    assert response.status_code == 502
    assert len(connected_app.extensions["store"].list_items()[0]["accounts"]) == 2


def test_foreign_keys_and_unique_identifiers(database_url):
    with pytest.raises(psycopg.errors.ForeignKeyViolation), connect(database_url) as conn:
        conn.execute(
            """INSERT INTO accounts (item_id, plaid_account_id, name, type)
                        VALUES (%s, %s, %s, %s)""",
            (uuid.uuid4(), "account", "Test", "depository"),
        )


def test_institution_name_is_escaped(connected_app):
    client = connected_app.test_client()
    headers = csrf_headers(client)
    client.post(
        "/api/exchange",
        headers=headers,
        json={
            "public_token": "public-sandbox-test-token",
            "institution_name": "<script>alert(1)</script>",
        },
    )
    assert "<script>alert(1)</script>" not in client.get("/").text
    assert "&lt;script&gt;" in client.get("/").text


def test_wrong_encryption_key_preserves_connection(connected_app, exchange_connection):
    from cryptography.fernet import Fernet

    client, headers, item = exchange_connection
    connected_app.extensions["cipher"] = Fernet(Fernet.generate_key())
    response = client.post(f"/api/items/{item['id']}/accounts", headers=headers)
    assert response.status_code == 503
    assert "Restore the original TOKEN_ENCRYPTION_KEY" in response.json["error"]
    assert len(connected_app.extensions["store"].list_items()[0]["accounts"]) == 2


def test_edited_migration_is_rejected(database_url, monkeypatch, tmp_path):
    import click

    from pocketview.config import ROOT

    copied = tmp_path / "migrations"
    copied.mkdir()
    source = (ROOT / "migrations" / "001_foundation.sql").read_text()
    (copied / "001_foundation.sql").write_text(source + "\n-- edited after application\n")
    monkeypatch.setattr("pocketview.db.ROOT", tmp_path)
    with pytest.raises(click.ClickException, match="changed"):
        migrate(database_url, "test-owner")
