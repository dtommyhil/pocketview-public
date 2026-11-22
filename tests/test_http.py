"""Exercise the real localhost HTTP server with two synthetic financial personas."""

import re
from threading import Thread

import httpx
import pytest
from werkzeug.serving import make_server

from pocketview.plaid import PlaidClient
from tests.test_sync import page, transaction

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "activity,expected",
    [
        (transaction("coffee", 12.34), "−$12.34"),
        (
            transaction(
                "payroll",
                -1250.50,
                merchant_name=None,
                original_description="Example payroll deposit",
                personal_finance_category=None,
            ),
            "+$1,250.50",
        ),
    ],
)
def test_real_http_link_exchange_sync_and_dashboard(connected_app, activity, expected):
    original = connected_app.extensions["plaid"].transport
    sync_calls = []

    def respond(request):
        if request.url.path != "/transactions/sync":
            return original.handle_request(request)
        sync_calls.append(request)
        return httpx.Response(200, json=page(added=[activity]))

    connected_app.extensions["plaid"] = PlaidClient(
        "fake-id", "fake-secret", transport=httpx.MockTransport(respond)
    )
    server = make_server("127.0.0.1", 0, connected_app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", timeout=5) as client:
            dashboard = client.get("/")
            assert dashboard.status_code == 200
            csrf = re.search(r'name="csrf-token" content="([^"]+)"', dashboard.text).group(1)
            headers = {"X-CSRF-Token": csrf}
            assert client.get("/static/app.js").status_code == 200
            assert client.get("/static/style.css").status_code == 200
            assert client.post("/api/link-token", headers=headers).status_code == 200
            exchanged = client.post(
                "/api/exchange",
                headers=headers,
                json={
                    "public_token": "public-sandbox-http-test",
                    "institution_name": "HTTP Test Bank",
                    "institution_id": "ins_test",
                    "link_session_id": "session-test",
                    "selected_account_ids": ["account-1"],
                },
            )
            assert exchanged.status_code == 200
            item = connected_app.extensions["store"].list_items()[0]
            endpoint = f"/api/items/{item['id']}/sync"
            assert client.post(endpoint).status_code == 400
            assert client.post(endpoint, headers=headers).status_code == 200
            html = client.get("/").text
            assert expected in html
            assert "Last successful sync:" in html
            if activity["amount"] > 0:
                assert 'id="spending-data"' in html and '"12.34"' in html
            else:
                assert "No posted spending to chart." in html
            assert client.get("/?q=not-a-real-merchant").status_code == 200
            filtered = client.get("/?chart=month&start=2026-11-01&end=2026-11-30")
            assert filtered.status_code == 200 and expected in filtered.text
            assert client.get("/static/vendor/chart.umd.js").status_code == 200
            for action in ("health", "reconnect-token", "reconnect-complete"):
                result = client.post(f"/api/items/{item['id']}/{action}", headers=headers, json={})
                assert result.status_code == 200
            assert len(sync_calls) == 3
            assert "access-sandbox" not in html
            saved = connected_app.extensions["store"].get_item(item["id"])
            assert saved["link_session_id"] == "session-test"
            assert saved["institution_id"] == "ins_test"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
