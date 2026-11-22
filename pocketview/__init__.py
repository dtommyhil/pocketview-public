"""Flask application factory: configuration, dependencies and safe request logging."""

import json
import logging
import secrets
import time
import uuid

import psycopg
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, g, jsonify, request, session
from werkzeug.exceptions import HTTPException

from pocketview.cli import register_cli as register_connection_cli
from pocketview.config import load_config
from pocketview.db import register_cli
from pocketview.demo import register_cli as register_demo_cli
from pocketview.plaid import PlaidClient
from pocketview.store import Store


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(load_config(config))
    app.logger.setLevel(logging.INFO)
    # Keep HTTP client diagnostics from exposing URLs, headers or financial payloads.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app.extensions["store"] = Store(app.config["DATABASE_URL"], app.config["DEMO_OWNER_ID"])
    if app.config["APP_MODE"] == "local":
        app.extensions["cipher"] = Fernet(app.config["TOKEN_ENCRYPTION_KEY"].encode())
        app.extensions["plaid"] = PlaidClient(
            app.config["PLAID_CLIENT_ID"], app.config["PLAID_SECRET"]
        )
    register_cli(app)
    register_connection_cli(app)
    register_demo_cli(app)

    @app.before_request
    def protect_request():
        g.correlation_id = str(uuid.uuid4())
        g.started = time.monotonic()
        demo = app.config["APP_MODE"] == "demo"
        if demo and (
            request.path.startswith("/api/") or request.method not in {"GET", "HEAD", "OPTIONS"}
        ):
            return jsonify(
                error="This portfolio demo is read-only. "
                "Explore the sample dashboard and transactions."
            ), 403
        if not demo and request.remote_addr not in {"127.0.0.1", "::1"}:
            return jsonify(error="Pocketview is available on localhost only."), 403
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            expected = session.get("csrf_token")
            received = request.headers.get("X-CSRF-Token", "")
            if not expected or not secrets.compare_digest(expected.encode(), received.encode()):
                return jsonify(error="Your session expired. Reload the page and try again."), 400

    @app.after_request
    def finish_request(response):
        correlation_id = getattr(g, "correlation_id", None)
        response.headers["X-Request-ID"] = correlation_id or "unavailable"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        app.logger.info(
            json.dumps(
                {
                    "event": "web_request",
                    "correlation_id": correlation_id,
                    # A route pattern avoids logging user-controlled URLs or query strings.
                    "endpoint": request.url_rule.rule if request.url_rule else "unmatched",
                    "duration_ms": round(
                        (time.monotonic() - getattr(g, "started", time.monotonic())) * 1000
                    ),
                    "outcome": response.status_code,
                    "item_ref": getattr(g, "item_ref", None),
                }
            )
        )
        return response

    @app.errorhandler(psycopg.Error)
    def database_error(error):
        if app.config["APP_MODE"] == "demo":
            return jsonify(
                error="The demo is temporarily unavailable. Please try again shortly."
            ), 503
        return jsonify(
            error="Local database operation failed. Check Docker and run "
            "flask --app pocketview db-migrate. "
            "If this happened during connection, check Plaid Dashboard Logs before linking again.",
            correlation_id=getattr(g, "correlation_id", None),
        ), 503

    @app.errorhandler(InvalidToken)
    def encryption_error(error):
        return jsonify(
            error="Cannot decrypt this connection. Restore the original TOKEN_ENCRYPTION_KEY "
            "in .env and restart. Do not generate a replacement key."
        ), 503

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(
            error="Request could not be accepted. Reload the page and check the request."
        ), error.code

    @app.errorhandler(Exception)
    def unexpected_error(error):
        # Exception text/tracebacks can contain tokens or SQL parameters; withhold them.
        app.logger.error(
            json.dumps(
                {
                    "event": "unexpected_error",
                    "outcome": "failed",
                    "correlation_id": getattr(g, "correlation_id", None),
                }
            )
        )
        return jsonify(
            error="An unexpected local error occurred. Reload to check whether the connection "
            "was saved before starting Link again.",
            correlation_id=getattr(g, "correlation_id", None),
        ), 500

    from pocketview.routes import bp

    app.register_blueprint(bp)
    return app
