import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event

import httpx
import pytest

from pocketview.db import connect
from pocketview.plaid import PlaidClient
from pocketview.store import Store
from tests.conftest import csrf_headers

pytestmark = pytest.mark.postgres


def transaction(identifier, amount=12.34, **fields):
    return {
        "transaction_id": identifier,
        "account_id": "account-1",
        "date": "2026-11-20",
        "amount": amount,
        "iso_currency_code": "USD",
        "pending": False,
        "merchant_name": "Corner Cafe",
        "original_description": "CARD CORNER CAFE",
        "payment_channel": "in store",
        "personal_finance_category": {
            "primary": "FOOD_AND_DRINK",
            "detailed": "FOOD_AND_DRINK_COFFEE",
            "confidence_level": "VERY_HIGH",
        },
        **fields,
    }


def page(
    *,
    added=None,
    modified=None,
    removed=None,
    cursor="cursor-final",
    more=False,
    status="HISTORICAL_UPDATE_COMPLETE",
):
    return {
        "added": added or [],
        "modified": modified or [],
        "removed": removed or [],
        "next_cursor": cursor,
        "has_more": more,
        "transactions_update_status": status,
        "request_id": "syncRequest123",
    }


@pytest.fixture
def script(connected_app, monkeypatch):
    monkeypatch.setattr("pocketview.sync.time.sleep", lambda _: None)
    original_transport = connected_app.extensions["plaid"].transport

    def install(responses, account_response=None):
        calls = []
        iterator = iter(responses) if not callable(responses) else None

        def respond(request):
            if request.url.path == "/accounts/get" and account_response is not None:
                return httpx.Response(200, json={"accounts": account_response})
            if request.url.path != "/transactions/sync":
                return original_transport.handle_request(request)
            body = json.loads(request.content)
            calls.append(body)
            result = responses(body) if callable(responses) else next(iterator)
            if isinstance(result, httpx.Response):
                return result
            if isinstance(result, Exception):
                raise result
            return httpx.Response(200, json=result)

        connected_app.extensions["plaid"] = PlaidClient(
            "fake-client-id",
            "fake-secret-do-not-log",
            transport=httpx.MockTransport(respond),
        )
        return calls

    return install


def sync(client, headers, item):
    return client.post(f"/api/items/{item['id']}/sync", headers=headers)


def stored(connected_app, item):
    with connect(connected_app.config["DATABASE_URL"]) as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE item_id = %s ORDER BY plaid_transaction_id",
            (item["id"],),
        ).fetchall()
        record = conn.execute("SELECT * FROM plaid_items WHERE id = %s", (item["id"],)).fetchone()
        attempts = conn.execute(
            "SELECT * FROM sync_attempts WHERE item_id = %s ORDER BY started_at", (item["id"],)
        ).fetchall()
    return rows, record, attempts


def test_link_initializes_sync_and_loading_has_reachable_manual_action(
    connected_app, exchange_connection
):
    client, headers, item = exchange_connection
    requests = connected_app.extensions["test_requests"]
    initial = [r for r in requests if r.url.path == "/transactions/sync"]
    assert len(initial) == 1
    body = json.loads(initial[0].content)
    assert "cursor" not in body
    assert body["count"] == 500
    assert body["options"]["personal_finance_category_version"] == "v2"
    assert initial[0].extensions["timeout"]["read"] == 90.0
    assert "Preparing your history" in client.get("/").text
    assert f'data-item="{item["id"]}"' in client.get("/").text
    assert stored(connected_app, item)[1]["sync_state"] == "loading"


def test_full_pagination_upserts_modifies_removes_and_repeats_without_duplicates(
    connected_app, exchange_connection, script
):
    client, headers, item = exchange_connection
    responses = [
        page(added=[transaction("keep"), transaction("remove")], cursor="page-two", more=True),
        page(modified=[transaction("keep", 20.10)], removed=[{"transaction_id": "remove"}]),
    ]
    calls = script(responses * 2)
    assert sync(client, headers, item).status_code == 200
    assert sync(client, headers, item).status_code == 200
    rows, record, attempts = stored(connected_app, item)
    assert [(r["plaid_transaction_id"], r["amount"]) for r in rows] == [("keep", Decimal("20.1"))]
    assert [c["cursor"] for c in calls] == [
        "cursor-initial",
        "page-two",
        "cursor-final",
        "page-two",
    ]
    assert record["sync_cursor"] == "cursor-final"
    assert record["sync_state"] == "ready"
    assert attempts[-1]["added_count"] == 2 and attempts[-1]["removed_count"] == 1


def test_failed_pagination_preserves_data_cursor_and_freshness(
    connected_app, exchange_connection, script
):
    client, headers, item = exchange_connection
    script([page(added=[transaction("saved")], cursor="saved-cursor")])
    assert sync(client, headers, item).status_code == 200
    _, before, _ = stored(connected_app, item)
    script(
        [
            page(added=[transaction("must-not-save")], cursor="temporary", more=True),
            httpx.Response(
                400,
                json={
                    "error_code": "ITEM_LOGIN_REQUIRED",
                    "error_type": "ITEM_ERROR",
                    "request_id": "failure123",
                    "error_message": "private-payload",
                },
            ),
        ]
    )
    response = sync(client, headers, item)
    rows, record, attempts = stored(connected_app, item)
    assert response.status_code == 502
    assert [r["plaid_transaction_id"] for r in rows] == ["saved"]
    assert record["sync_cursor"] == "saved-cursor"
    assert record["last_successful_sync_at"] == before["last_successful_sync_at"]
    assert record["sync_state"] == "failed"
    assert attempts[-1]["error_code"] == "ITEM_LOGIN_REQUIRED"
    assert attempts[-1]["request_id"] == "failure123"
    assert "Saved transactions are preserved" in client.get("/").text
    assert "private-payload" not in response.text


def test_mutation_discards_partial_pages_and_restarts_original_cursor(
    connected_app, exchange_connection, script
):
    client, headers, item = exchange_connection
    calls = script(
        [
            page(added=[transaction("discarded")], cursor="mutated-page", more=True),
            httpx.Response(
                400, json={"error_code": "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"}
            ),
            page(added=[transaction("fresh")], cursor="fresh-page", more=True),
            page(modified=[transaction("fresh", 44)]),
        ]
    )
    assert sync(client, headers, item).status_code == 200
    assert [c["cursor"] for c in calls] == [
        "cursor-initial",
        "mutated-page",
        "cursor-initial",
        "fresh-page",
    ]
    assert [r["plaid_transaction_id"] for r in stored(connected_app, item)[0]] == ["fresh"]


@pytest.mark.parametrize("error", ["mutation", "rate_limit", "network"])
def test_repeated_transient_errors_are_bounded_and_release_lock(
    connected_app, exchange_connection, script, error
):
    client, headers, item = exchange_connection
    if error == "network":
        response = httpx.ReadTimeout("secret-access-token")
    elif error == "rate_limit":
        response = httpx.Response(
            429, json={"error_code": "NEW_LIMIT_CODE", "error_type": "RATE_LIMIT_EXCEEDED"}
        )
    else:
        response = httpx.Response(
            400, json={"error_code": "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"}
        )
    calls = script([response] * 3)
    assert sync(client, headers, item).status_code == 502
    assert len(calls) == 3
    assert stored(connected_app, item)[1]["sync_cursor"] == "cursor-initial"
    script([page()])
    assert sync(client, headers, item).status_code == 200


@pytest.mark.parametrize("with_removed", [True, False])
def test_pending_to_posted_reconciles_different_ids_across_pages(
    connected_app, exchange_connection, script, with_removed
):
    client, headers, item = exchange_connection
    pending = transaction("pending", 15, pending=True, date="2026-11-19")
    script([page(added=[pending])])
    assert sync(client, headers, item).status_code == 200
    posted = transaction("posted", 17, pending_transaction_id="pending")
    script(
        [
            page(
                added=[posted],
                more=True,
                cursor="second-page",
                removed=[{"transaction_id": "pending"}] if with_removed else [],
            ),
            page(added=[] if with_removed else [pending]),
        ]
    )
    assert sync(client, headers, item).status_code == 200
    rows = stored(connected_app, item)[0]
    assert len(rows) == 1
    assert rows[0]["plaid_transaction_id"] == "posted"
    assert rows[0]["pending_transaction_id"] == "pending"
    assert rows[0]["amount"] == Decimal("17") and not rows[0]["pending"]


def test_failure_writing_cursor_rolls_back_transaction_changes(
    connected_app, exchange_connection, script
):
    client, headers, item = exchange_connection
    with connect(connected_app.config["DATABASE_URL"]) as conn:
        conn.execute("""
            CREATE FUNCTION reject_test_cursor() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.sync_cursor = 'reject-cursor' THEN RAISE EXCEPTION 'Test rollback'; END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER reject_test_cursor BEFORE UPDATE OF sync_cursor ON plaid_items
                FOR EACH ROW EXECUTE FUNCTION reject_test_cursor();
        """)
    before = stored(connected_app, item)[1]
    script([page(added=[transaction("must-roll-back")], cursor="reject-cursor")])
    response = sync(client, headers, item)
    rows, record, attempts = stored(connected_app, item)
    assert response.status_code == 503
    assert rows == []
    assert record["sync_cursor"] == before["sync_cursor"]
    assert record["last_successful_sync_at"] == before["last_successful_sync_at"]
    assert attempts[-1]["outcome"] == "failed" and attempts[-1]["error_code"] == "DATABASE_ERROR"


def test_concurrent_sync_rejected_without_second_plaid_call(
    connected_app, exchange_connection, script
):
    client, headers, item = exchange_connection
    entered, release = Event(), Event()

    def blocked(body):
        entered.set()
        assert release.wait(5)
        return page(added=[transaction("once")])

    calls = script(blocked)
    other = connected_app.test_client()
    other_headers = csrf_headers(other)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(sync, client, headers, item)
        try:
            assert entered.wait(5)
            second = sync(other, other_headers, item)
            assert second.status_code == 409
            assert second.json["code"] == "SYNC_BUSY"
            assert len(calls) == 1
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
    assert len(stored(connected_app, item)[0]) == 1


def test_owner_cannot_read_or_sync_another_owners_transactions(
    connected_app, exchange_connection, script
):
    client, headers, item = exchange_connection
    script([page(added=[transaction("private")])])
    assert sync(client, headers, item).status_code == 200
    with connect(connected_app.config["DATABASE_URL"]) as conn:
        conn.execute("INSERT INTO owners (id) VALUES ('other-owner')")
    connected_app.extensions["store"] = Store(connected_app.config["DATABASE_URL"], "other-owner")
    calls = script([])
    assert connected_app.extensions["store"].list_transactions() == []
    assert sync(client, headers, item).status_code == 404
    assert calls == []


def test_product_not_ready_is_loading_and_empty_complete_is_not_loading(
    connected_app, exchange_connection, script
):
    client, headers, item = exchange_connection
    calls = script([httpx.Response(400, json={"error_code": "PRODUCT_NOT_READY"})])
    response = sync(client, headers, item)
    assert response.status_code == 202 and response.json["state"] == "loading"
    assert len(calls) == 1
    assert "Preparing your history" in client.get("/").text
    script([page()])
    assert sync(client, headers, item).status_code == 200
    assert "No USD transactions." in client.get("/").text
    assert "Preparing your history" not in client.get("/").text


def test_decimal_money_null_fields_and_currencies_are_handled(
    connected_app, exchange_connection, script
):
    client, headers, item = exchange_connection
    body = page(
        added=[
            transaction("usd", -0.10, merchant_name=None, personal_finance_category=None),
            transaction("cad", 99, iso_currency_code="CAD"),
            transaction("unofficial", 1, iso_currency_code=None, unofficial_currency_code="GBX"),
        ]
    )
    script([body])
    assert sync(client, headers, item).status_code == 200
    rows = stored(connected_app, item)[0]
    assert len(rows) == 3
    assert next(r for r in rows if r["plaid_transaction_id"] == "usd")["amount"] == Decimal("-0.1")
    visible = connected_app.extensions["store"].list_transactions()
    assert len(visible) == 1
    html = client.get("/").text
    assert "+$0.10" in html
    assert "CARD CORNER CAFE" in html and "Uncategorized" in html
    assert "$99.00" not in html


def test_account_refresh_keeps_historical_transactions(connected_app, exchange_connection, script):
    client, headers, item = exchange_connection
    script([page(added=[transaction("historical")])])
    assert sync(client, headers, item).status_code == 200
    connected_app.extensions["store"].save_accounts(item["id"], [])
    assert connected_app.extensions["store"].list_items()[0]["accounts"] == []
    assert len(connected_app.extensions["store"].list_transactions()) == 1


def test_new_account_uses_authoritative_accounts_endpoint(
    connected_app, exchange_connection, script
):
    client, headers, item = exchange_connection
    script(
        [page(added=[transaction("new", account_id="new-account")])],
        account_response=[
            {
                "account_id": "new-account",
                "name": "Authoritative account",
                "type": "depository",
                "balances": {"iso_currency_code": "USD"},
            }
        ],
    )
    assert sync(client, headers, item).status_code == 200
    assert (
        connected_app.extensions["store"].list_transactions()[0]["account_name"]
        == "Authoritative account"
    )


@pytest.mark.parametrize(
    "bad_page",
    [
        {"has_more": "false"},
        {"added": None},
        {"next_cursor": None},
    ],
)
def test_incomplete_updates_do_not_advance_cursor(
    connected_app, exchange_connection, script, bad_page
):
    client, headers, item = exchange_connection
    script([{**page(), **bad_page}])
    response = sync(client, headers, item)
    assert response.status_code == 502
    assert response.json["code"] == "INVALID_RESPONSE"
    assert stored(connected_app, item)[1]["sync_cursor"] == "cursor-initial"


def test_sync_and_disconnect_keep_tokens_and_payloads_out_of_logs(
    connected_app, exchange_connection, script, caplog
):
    client, headers, item = exchange_connection
    script([page(added=[transaction("txn-private", merchant_name="private-financial-payload")])])
    assert sync(client, headers, item).status_code == 200
    for private in (
        "access-sandbox-do-not-log",
        "fake-secret-do-not-log",
        "private-financial-payload",
    ):
        assert private not in caplog.text
    response = client.post(f"/api/items/{item['id']}/disconnect", headers=headers)
    assert response.status_code == 200
    with connect(connected_app.config["DATABASE_URL"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM transactions").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM sync_attempts").fetchone()["n"] == 0
