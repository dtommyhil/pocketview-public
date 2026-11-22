import json

import httpx
import psycopg
import pytest

from pocketview import create_app
from pocketview.config import load_config
from pocketview.plaid import PlaidClient, PlaidError
from tests.conftest import csrf_headers, empty_exploration


@pytest.mark.parametrize(
    "setting,value",
    [
        ("PLAID_ENV", "production"),
        ("PLAID_ENV", "development"),
        ("TOKEN_ENCRYPTION_KEY", "not-a-key"),
        ("FLASK_SECRET_KEY", "short"),
        ("DATABASE_URL", ""),
        ("DEMO_OWNER_ID", "someone@example.com"),
        ("PLAID_SECRET", ""),
    ],
)
def test_invalid_configuration_fails_clearly(config, setting, value):
    config[setting] = value
    with pytest.raises(ValueError):
        load_config(config)


def test_missing_plaid_credentials_leave_dashboard_usable(config):
    config.update(PLAID_CLIENT_ID="", PLAID_SECRET="")
    app = create_app(config)
    app.extensions["store"].list_items = lambda: []
    app.extensions["store"].list_transactions = lambda: []
    app.extensions["store"].explore = empty_exploration
    client = app.test_client()
    headers = csrf_headers(client)
    page = client.get("/").text
    assert "One local setup step left" in page
    response = client.post("/api/link-token", headers=headers)
    assert response.status_code == 503
    assert response.json["code"] == "NOT_CONFIGURED"


def test_dashboard_and_local_access_protection(app):
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert "Sandbox demo — simulated financial data." in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert "HttpOnly" in response.headers["Set-Cookie"]
    assert "SameSite=Strict" in response.headers["Set-Cookie"]
    assert client.get("/", environ_overrides={"REMOTE_ADDR": "192.0.2.1"}).status_code == 403
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/link-token",
        "/api/exchange",
        "/api/items/00000000-0000-0000-0000-000000000001/accounts",
        "/api/items/00000000-0000-0000-0000-000000000001/disconnect",
        "/api/items/00000000-0000-0000-0000-000000000001/sync",
        "/api/items/00000000-0000-0000-0000-000000000001/health",
        "/api/items/00000000-0000-0000-0000-000000000001/reconnect-token",
        "/api/items/00000000-0000-0000-0000-000000000001/reconnect-complete",
    ],
)
def test_mutations_require_csrf(app, endpoint):
    client = app.test_client()
    assert client.post(endpoint, json={}).status_code == 400
    csrf_headers(client)
    assert client.post(endpoint, headers={"X-CSRF-Token": "wrong"}, json={}).status_code == 400


@pytest.mark.parametrize(
    "body", [None, [], {}, {"public_token": 123}, {"public_token": "public-production-nope"}]
)
def test_exchange_validates_before_calling_plaid(app, body):
    client = app.test_client()
    response = client.post("/api/exchange", headers=csrf_headers(client), json=body)
    assert response.status_code == 400


def test_link_request_uses_only_sandbox_and_server_owner(app):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"link_token": "link-sandbox-example"})

    app.extensions["plaid"] = PlaidClient(
        "client", "secret", transport=httpx.MockTransport(respond)
    )
    client = app.test_client()
    response = client.post(
        "/api/link-token", headers=csrf_headers(client), json={"owner_id": "other"}
    )
    assert response.json == {"link_token": "link-sandbox-example"}
    assert calls[0].url.host == "sandbox.plaid.com"
    body = json.loads(calls[0].content)
    assert body["user"]["client_user_id"] == "test-owner"
    assert body["products"] == ["transactions"]
    assert body["enable_multi_item_link"] is False
    assert calls[0].extensions["timeout"] == {
        "connect": 5.0,
        "read": 20.0,
        "write": 20.0,
        "pool": 20.0,
    }


@pytest.mark.parametrize(
    "code,error_type,message",
    [
        ("INVALID_API_KEYS", "INVALID_INPUT", "local .env"),
        ("ITEM_LOGIN_REQUIRED", "ITEM_ERROR", "reauthentication"),
        ("ACCOUNTS_LIMIT", "RATE_LIMIT_EXCEEDED", "Wait"),
        ("INVALID_PUBLIC_TOKEN", "INVALID_INPUT", "fresh Link"),
    ],
)
def test_actionable_errors_without_raw_payloads(code, error_type, message, caplog):
    def respond(request):
        return httpx.Response(
            400,
            json={
                "error_code": code,
                "error_type": error_type,
                "error_message": "private-bank-credentials",
                "request_id": "support123",
            },
        )

    client = PlaidClient("id", "secret", transport=httpx.MockTransport(respond))
    with caplog.at_level("INFO", logger="pocketview"):
        with pytest.raises(PlaidError) as caught:
            client.exchange("public-sandbox-private", "correlation123")
    assert message in str(caught.value)
    assert caught.value.request_id == "support123"
    assert "private-bank-credentials" not in str(caught.value) + caplog.text
    assert "public-sandbox-private" not in caplog.text
    event = json.loads(caplog.records[-1].message)
    assert event["endpoint"] == "/item/public_token/exchange"
    assert event["plaid_request_id"] == "support123"
    assert event["correlation_id"] == "correlation123"
    assert event["outcome"] == "failed"


def test_read_only_transient_retry_is_bounded(monkeypatch):
    calls = []
    monkeypatch.setattr("pocketview.plaid.time.sleep", lambda _: None)

    def respond(request):
        calls.append(request)
        return httpx.Response(500, json={"error_code": "INTERNAL_SERVER_ERROR"})

    client = PlaidClient("id", "secret", transport=httpx.MockTransport(respond))
    with pytest.raises(PlaidError):
        client.accounts("private-access-token", "correlation", "local-item")
    assert len(calls) == 2
    calls.clear()
    with pytest.raises(PlaidError) as caught:
        client.exchange("public-sandbox-private", "correlation")
    assert len(calls) == 1
    assert caught.value.ambiguous


def test_rate_limit_retry_is_bounded(monkeypatch):
    monkeypatch.setattr("pocketview.plaid.time.sleep", lambda _: None)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            429, json={"error_type": "RATE_LIMIT_EXCEEDED", "error_code": "ACCOUNTS_LIMIT"}
        )

    client = PlaidClient("id", "secret", transport=httpx.MockTransport(respond))
    with pytest.raises(PlaidError):
        client.accounts("token", "correlation", "local-item")
    assert len(calls) == 2


def test_network_timeout_is_ambiguous_without_retry():
    calls = []

    def respond(request):
        calls.append(request)
        raise httpx.ReadTimeout("Do not expose this secret", request=request)

    client = PlaidClient("id", "secret", transport=httpx.MockTransport(respond))
    with pytest.raises(PlaidError) as caught:
        client.exchange("public-sandbox-private", "correlation")
    assert caught.value.ambiguous
    assert "secret" not in str(caught.value)
    assert len(calls) == 1


def test_database_errors_withhold_connection_secrets(app, caplog):
    def failure():
        raise psycopg.OperationalError("password=private-db-password")

    app.extensions["store"].list_items = failure
    response = app.test_client().get("/")
    assert response.status_code == 503
    assert "Local database operation failed" in response.json["error"]
    assert "private-db-password" not in response.text + caplog.text
