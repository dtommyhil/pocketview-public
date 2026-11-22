import json

import httpx
import pytest

from pocketview.db import connect
from pocketview.explore import Filters
from pocketview.plaid import PlaidClient
from pocketview.store import Store
from tests.test_exploration import seed
from tests.test_sync import page

pytestmark = pytest.mark.postgres


@pytest.fixture
def recovery(connected_app, exchange_connection):
    client, headers, item = exchange_connection
    seed(connected_app, item, [{"amount": "12.34"}])
    state = {"error": "ITEM_LOGIN_REQUIRED", "calls": []}

    def respond(request):
        state["calls"].append((request.url.path, json.loads(request.content)))
        if request.url.path == "/item/get":
            error = {"error_code": state["error"]} if state["error"] else None
            return httpx.Response(200, json={"item": {"error": error}, "request_id": "health-ref"})
        if request.url.path == "/link/token/create":
            return httpx.Response(200, json={"link_token": "link-sandbox-update"})
        if request.url.path == "/transactions/sync":
            return httpx.Response(200, json=page(cursor="cursor-after-recovery"))
        if request.url.path == "/sandbox/item/reset_login":
            state["error"] = "ITEM_LOGIN_REQUIRED"
            return httpx.Response(200, json={"reset_login": True})
        raise AssertionError("Recovery must never exchange tokens or create another Item")

    connected_app.extensions["plaid"] = PlaidClient(
        "fake-id", "fake-secret", transport=httpx.MockTransport(respond)
    )
    return client, headers, item, state


def test_update_mode_checks_actual_health_and_resumes_existing_cursor(
    connected_app, recovery, caplog
):
    client, headers, item, state = recovery
    store = connected_app.extensions["store"]
    before = store.get_item(item["id"])
    base = f"/api/items/{item['id']}"
    health = client.post(base + "/health", headers=headers)
    assert health.status_code == 200 and health.json["state"] == "login_required"
    assert "Reconnect" in client.get("/").text
    token = client.post(base + "/reconnect-token", headers=headers)
    assert token.status_code == 200
    payload = state["calls"][-1][1]
    assert payload["access_token"] == "access-sandbox-do-not-log"
    assert payload["products"] == [] and "additional_consented_products" not in payload
    not_fixed = client.post(
        base + "/reconnect-complete", headers=headers, json={"link_session_id": "update-session"}
    )
    assert not_fixed.status_code == 409
    assert store.get_item(item["id"])["sync_cursor"] == before["sync_cursor"]
    assert store.explore(Filters())["total"] == 1
    state["error"] = None  # Fake the user completing the hosted Link update flow.
    done = client.post(
        base + "/reconnect-complete", headers=headers, json={"link_session_id": "update-session"}
    )
    assert done.status_code == 200 and "Reconnected" in done.json["message"]
    after = store.get_item(item["id"])
    assert after["access_token_encrypted"] == before["access_token_encrypted"]
    assert after["id"] == before["id"] and len(store.list_items()) == 1
    assert after["connection_state"] == "connected"
    assert after["sync_cursor"] == "cursor-after-recovery"
    assert after["link_session_id"] == "update-session"
    sync_call = [body for path, body in state["calls"] if path == "/transactions/sync"][0]
    assert sync_call["cursor"] == "cursor-initial"
    assert "access-sandbox-do-not-log" not in done.text + health.text + caplog.text


def test_consent_recovery_requests_transactions_consent(connected_app, recovery):
    client, headers, item, state = recovery
    state["error"] = "ADDITIONAL_CONSENT_REQUIRED"
    result = client.post(f"/api/items/{item['id']}/reconnect-token", headers=headers)
    assert result.status_code == 200
    assert state["calls"][-1][1]["additional_consented_products"] == ["transactions"]


def test_repaired_link_with_failed_sync_keeps_cursor_and_data(connected_app, recovery, monkeypatch):
    from pocketview.plaid import PlaidError

    client, headers, item, state = recovery
    state["error"] = None
    store = connected_app.extensions["store"]
    before = store.get_item(item["id"])

    def failure(*args):
        raise PlaidError("Please retry Sync.", code="INVALID_FIELD", request_id="sync-failed-ref")

    monkeypatch.setattr(connected_app.extensions["plaid"], "sync", failure)
    response = client.post(f"/api/items/{item['id']}/reconnect-complete", headers=headers, json={})
    assert response.status_code == 502 and "Reconnected" in response.json["error"]
    after = store.get_item(item["id"])
    assert after["sync_cursor"] == before["sync_cursor"]
    assert after["access_token_encrypted"] == before["access_token_encrypted"]
    assert after["sync_state"] == "failed"
    assert store.explore(Filters())["total"] == 1


def test_revoked_item_purges_token_but_keeps_history_until_explicit_removal(
    connected_app, recovery
):
    client, headers, item, state = recovery
    state["error"] = "ITEM_NOT_FOUND"
    base = f"/api/items/{item['id']}"
    assert client.post(base + "/health", headers=headers).json["state"] == "revoked"
    assert connected_app.extensions["store"].get_item(item["id"])["access_token_encrypted"] is None
    with connect(connected_app.config["DATABASE_URL"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM transactions").fetchone()["n"] == 1
    for action in ("sync", "accounts", "reconnect-token"):
        assert client.post(base + "/" + action, headers=headers).status_code == 409
    assert client.post(base + "/disconnect", headers=headers).status_code == 200
    assert connected_app.extensions["store"].list_items() == []


def test_new_recovery_routes_are_owner_scoped(connected_app, recovery):
    client, headers, item, state = recovery
    with connect(connected_app.config["DATABASE_URL"]) as conn:
        conn.execute("INSERT INTO owners (id) VALUES ('another-owner')")
    connected_app.extensions["store"] = Store(connected_app.config["DATABASE_URL"], "another-owner")
    for action in ("health", "reconnect-token", "reconnect-complete"):
        response = client.post(f"/api/items/{item['id']}/{action}", headers=headers, json={})
        assert response.status_code == 404
    assert state["calls"] == []


def test_malformed_completion_does_not_call_plaid(connected_app, recovery):
    client, headers, item, state = recovery
    for body in ([], "text", {"link_session_id": []}):
        response = client.post(
            f"/api/items/{item['id']}/reconnect-complete", headers=headers, json=body
        )
        assert response.status_code == 400
    assert not state["calls"]


def test_cli_simulation_is_sandbox_only_and_preserves_history(connected_app, recovery):
    client, headers, item, state = recovery
    state["error"] = None
    runner = connected_app.test_cli_runner()
    result = runner.invoke(args=["sandbox-login-required"])
    assert result.exit_code == 0 and "login_required" in result.output
    assert "access-sandbox" not in result.output
    assert connected_app.extensions["store"].get_item(item["id"])["sync_cursor"] == "cursor-initial"
    assert client.get("/").status_code == 200
    state["calls"].clear()
    connected_app.config["PLAID_ENV"] = "production"
    refused = runner.invoke(args=["sandbox-login-required"])
    assert refused.exit_code == 1 and "Sandbox only" in refused.output
    assert state["calls"] == []


def test_transient_health_error_preserves_last_known_state(connected_app, recovery, monkeypatch):
    from pocketview.plaid import PlaidError

    client, headers, item, state = recovery
    before = connected_app.extensions["store"].get_item(item["id"])

    def unavailable(*args):
        raise PlaidError("Temporary service failure.", code="INTERNAL_SERVER_ERROR")

    monkeypatch.setattr(connected_app.extensions["plaid"], "item", unavailable)
    assert client.post(f"/api/items/{item['id']}/health", headers=headers).status_code == 502
    after = connected_app.extensions["store"].get_item(item["id"])
    assert after["connection_state"] == before["connection_state"]
    assert after["sync_cursor"] == before["sync_cursor"]
