from datetime import date
from decimal import Decimal

import pytest

from pocketview import create_app
from pocketview.config import PUBLIC_DEMO_OWNER, load_config
from pocketview.db import connect
from pocketview.demo import DEMO_ITEM_ID, sample_transactions, seed_demo
from pocketview.explore import Filters


@pytest.fixture
def demo_app(config, database_url):
    app = create_app(
        {
            **config,
            "DATABASE_URL": database_url,
            "APP_MODE": "demo",
            "PUBLIC_HOSTNAME": "demo.example",
        }
    )
    seed_demo(app.extensions["store"], date(2026, 11, 21))
    return app


def test_demo_configuration_disables_credentials_and_uses_fixed_owner(config, monkeypatch):
    def refuse_local_env(*args):
        raise AssertionError("Explicit public mode must not load the local credential file.")

    monkeypatch.setattr("pocketview.config.load_dotenv", refuse_local_env)
    settings = load_config(
        {
            **config,
            "APP_MODE": "demo",
            "PUBLIC_HOSTNAME": "demo.example",
            "TOKEN_ENCRYPTION_KEY": "",
            "DEMO_OWNER_ID": "a-private-owner",
        }
    )
    assert settings["DEMO_OWNER_ID"] == PUBLIC_DEMO_OWNER
    assert settings["PLAID_CLIENT_ID"] == settings["PLAID_SECRET"] == ""
    assert settings["TOKEN_ENCRYPTION_KEY"] == ""
    assert settings["SESSION_COOKIE_SECURE"] is True
    assert "demo.example" in settings["TRUSTED_HOSTS"]


@pytest.mark.parametrize(
    "host",
    [
        "*",
        ".example.com",
        "https://example.com",
        "example.com/path",
        "example.com:123",
        "bad host",
        "-bad.example",
    ],
)
def test_public_hostname_must_be_explicit(config, host):
    with pytest.raises(ValueError, match="hostname"):
        load_config({**config, "APP_MODE": "demo", "PUBLIC_HOSTNAME": host})


def test_demo_never_enables_production_or_an_unknown_mode(config):
    with pytest.raises(ValueError, match="sandbox"):
        load_config({**config, "APP_MODE": "demo", "PLAID_ENV": "production"})
    with pytest.raises(ValueError, match="APP_MODE"):
        load_config({**config, "APP_MODE": "other"})
    with pytest.raises(ValueError, match="reserved"):
        load_config({**config, "DEMO_OWNER_ID": PUBLIC_DEMO_OWNER})


def test_render_hostname_is_trusted_only_in_demo_mode(config, monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "demo.onrender.com")
    settings = load_config({**config, "APP_MODE": "demo", "PUBLIC_HOSTNAME": ""})
    assert "demo.onrender.com" in settings["TRUSTED_HOSTS"]
    assert "demo.onrender.com" not in load_config(config)["TRUSTED_HOSTS"]


def test_sample_data_dates_and_money_are_valid_across_new_year():
    rows = sample_transactions(date(2026, 1, 1))
    assert len(rows) == 56
    assert all(isinstance(row[6], Decimal) for row in rows)
    assert all(row[3] <= date(2026, 1, 1) for row in rows)
    assert len({(row[3].year, row[3].month) for row in rows}) == 4


@pytest.mark.postgres
def test_demo_is_public_read_only_and_does_not_initialize_plaid(demo_app):
    assert "plaid" not in demo_app.extensions and "cipher" not in demo_app.extensions
    client = demo_app.test_client()
    remote = {"REMOTE_ADDR": "198.51.100.10"}
    response = client.get("/", base_url="https://demo.example", environ_overrides=remote)
    assert response.status_code == 200
    assert "fictional financial data" in response.text
    assert "no live bank connection" in response.text
    for forbidden in (
        "link-initialize.js",
        "static/app.js",
        'id="connect"',
        'data-action="sync"',
        'data-action="disconnect"',
        'data-action="reconnect"',
        "One local setup step",
    ):
        assert forbidden not in response.text
    assert "Set-Cookie" not in response.headers
    assert (
        client.get(
            "/healthz", base_url="https://demo.example", environ_overrides=remote
        ).status_code
        == 200
    )
    assert (
        client.get("/", base_url="https://attacker.example", environ_overrides=remote).status_code
        == 400
    )
    paths = [
        "/api/link-token",
        "/api/exchange",
        *[
            f"/api/items/{DEMO_ITEM_ID}/{action}"
            for action in (
                "accounts",
                "sync",
                "health",
                "reconnect-token",
                "reconnect-complete",
                "disconnect",
            )
        ],
    ]
    for path in paths:
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            result = client.open(
                path, method=method, base_url="https://demo.example", environ_overrides=remote
            )
            assert result.status_code == 403
    assert demo_app.extensions["store"].explore(Filters())["total"] == 56


@pytest.mark.postgres
def test_demo_seed_is_repeatable_and_preserves_other_owners(
    demo_app, connected_app, exchange_connection
):
    _, _, local_item = exchange_connection
    local_store = connected_app.extensions["store"]
    before = local_store.get_item(local_item["id"])
    demo_store = demo_app.extensions["store"]
    seed_demo(demo_store, date(2026, 11, 21))
    data = demo_store.explore(Filters())
    assert (data["total"], data["pages"]) == (56, 3)
    assert data["summary"]["amount"] == Decimal("6809.96")
    assert data["summary"]["count"] == 39
    assert len(demo_store.list_items()) == 1
    assert len(demo_store.list_items()[0]["accounts"]) == 2
    assert local_store.get_item(local_item["id"]) == before
    assert len(local_store.list_items()) == 1
    html = demo_app.test_client().get("/", base_url="https://demo.example").text
    assert "Test Bank" not in html and "Plaid Checking" not in html
    with connect(demo_store.database_url) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM transactions WHERE item_id=%s", (DEMO_ITEM_ID,)
            ).fetchone()["n"]
            == 56
        )


@pytest.mark.postgres
def test_seed_and_simulation_cli_respect_mode(connected_app, demo_app):
    result = connected_app.test_cli_runner().invoke(args=["seed-demo"])
    assert result.exit_code == 1 and "APP_MODE=demo" in result.output
    refused = demo_app.test_cli_runner().invoke(args=["sandbox-login-required"])
    assert refused.exit_code == 1 and "cannot access Plaid" in refused.output
    seeded = demo_app.test_cli_runner().invoke(args=["seed-demo"])
    assert seeded.exit_code == 0
    assert demo_app.extensions["store"].explore(Filters())["total"] == 56


@pytest.mark.postgres
def test_demo_database_error_is_safe_for_visitors(demo_app, monkeypatch):
    import psycopg

    def fail(*args):
        raise psycopg.OperationalError("private database details")

    monkeypatch.setattr(demo_app.extensions["store"], "explore", fail)
    response = demo_app.test_client().get("/", base_url="https://demo.example")
    assert response.status_code == 503
    assert "temporarily unavailable" in response.text
    assert "private database details" not in response.text and "Docker" not in response.text


@pytest.mark.postgres
def test_hosted_startup_serves_read_only_demo_with_gunicorn(config, database_url, tmp_path):
    import os
    import signal
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path

    import httpx

    from pocketview.config import ROOT

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
        "APP_MODE": "demo",
        "DATABASE_URL": database_url,
        "FLASK_SECRET_KEY": config["FLASK_SECRET_KEY"],
        "PUBLIC_HOSTNAME": "demo.example",
        "PLAID_ENV": "sandbox",
        "PLAID_CLIENT_ID": "",
        "PLAID_SECRET": "",
        "PORT": str(port),
    }
    with (tmp_path / "server.log").open("w") as log:
        process = subprocess.Popen(
            ["sh", "scripts/start_demo.sh"],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=1) as client:
                for _ in range(80):
                    assert process.poll() is None, "Hosted startup exited before serving."
                    try:
                        if client.get("/healthz").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    pytest.fail("Hosted startup did not become healthy.")
                page = client.get("/")
                assert page.status_code == 200 and "56 matching" in page.text
                assert "fictional financial data" in page.text
                assert client.post("/api/exchange", json={}).status_code == 403
                assert client.get("/static/charts.js").status_code == 200
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
