"""Dashboard and the three steps of a Plaid connection."""

import hashlib
import secrets
from dataclasses import replace

from flask import Blueprint, current_app, g, jsonify, render_template, request, session, url_for

from pocketview.connections import RecoveryError, check_health, decrypt_token, owned_item
from pocketview.db import connect
from pocketview.explore import FilterError, Filters
from pocketview.plaid import PlaidError, safe_request_id
from pocketview.sync import SyncError, sync_item

bp = Blueprint("dashboard", __name__)


def store():
    return current_app.extensions["store"]


def plaid():
    if not current_app.config["PLAID_CLIENT_ID"]:
        raise PlaidError(
            "Add PLAID_CLIENT_ID and Sandbox PLAID_SECRET to your local .env, then restart.",
            code="NOT_CONFIGURED",
        )
    return current_app.extensions["plaid"]


def api_error(error, status=502):
    return jsonify(
        error=str(error),
        code=error.code,
        request_id=error.request_id,
        correlation_id=g.correlation_id,
    ), status


def load_accounts(item_id):
    g.item_ref = str(item_id)
    item = store().get_item(item_id)
    if not item:
        return jsonify(error="Connection not found."), 404
    try:
        token = decrypt_token(current_app.extensions["cipher"], item)
    except RecoveryError as error:
        return jsonify(error=str(error)), error.status
    try:
        result = plaid().accounts(token, g.correlation_id, str(item_id))
    except PlaidError as error:
        store().account_error(item_id, error)
        return jsonify(
            error="Connection saved, but accounts could not be loaded. Use Retry accounts. "
            + str(error),
            request_id=error.request_id,
            correlation_id=g.correlation_id,
        ), 502
    if not store().save_accounts(item_id, result["accounts"]):
        return jsonify(error="Connection was removed. Reload the page."), 404
    if item["sync_state"] == "never":
        return run_sync(item_id, initial=True)
    return jsonify(message="Sandbox connection saved and accounts loaded.")


def run_sync(item_id, initial=False, prefix=""):
    g.item_ref = str(item_id)
    prefix = "Connection and accounts saved. " if initial else prefix
    try:
        result = sync_item(
            store(), plaid(), current_app.extensions["cipher"], item_id, g.correlation_id
        )
    except PlaidError as error:
        return api_error(error, 503 if error.code == "NOT_CONFIGURED" else 502)
    except SyncError as error:
        message = prefix + str(error)
        if error.code == "PRODUCT_NOT_READY":
            return jsonify(message=message, state="loading"), 202
        return jsonify(
            error=message,
            code=error.code,
            request_id=error.request_id,
            correlation_id=g.correlation_id,
        ), error.status
    result["message"] = prefix + result["message"]
    return jsonify(result)


@bp.get("/")
def index():
    if current_app.config["APP_MODE"] == "local":
        session.setdefault("csrf_token", secrets.token_urlsafe(32))
    filter_error = None
    try:
        filters = Filters.parse(request.args)
    except FilterError as error:
        filter_error = str(error)
        filters = Filters()
    data = store().explore(filters)
    if filter_error:
        data.update(
            rows=[],
            total=0,
            page=1,
            pages=1,
            by_category=[],
            by_month=[],
            summary={"amount": 0, "count": 0, "uncertain": 0},
        )
    chart_views = {}
    for view, rows in (("category", data["by_category"]), ("month", data["by_month"])):
        chart_views[view] = {
            "labels": [
                row["label"].replace("_", " ").title() if view == "category" else row["label"]
                for row in rows
            ],
            "amounts": [str(row["amount"]) for row in rows],
        }
    return render_template(
        "dashboard.html",
        items=store().list_items(),
        transactions=data["rows"],
        exploration=data,
        filters=filters,
        form=request.args,
        filter_error=filter_error,
        clear_transactions_url=url_for("dashboard.index", **filters.period().url_args()),
        clear_dates_url=url_for(
            "dashboard.index", **replace(filters, start=None, end=None).url_args(page=1)
        ),
        previous_url=url_for("dashboard.index", **filters.url_args(page=data["page"] - 1))
        if data["page"] > 1
        else None,
        next_url=url_for("dashboard.index", **filters.url_args(page=data["page"] + 1))
        if data["page"] < data["pages"]
        else None,
        chart_data=chart_views[filters.chart],
        chart_views=chart_views,
        plaid_configured=bool(current_app.config["PLAID_CLIENT_ID"]),
        demo_mode=current_app.config["APP_MODE"] == "demo",
    ), 400 if filter_error else 200


@bp.get("/healthz")
def healthcheck():
    with connect(current_app.config["DATABASE_URL"]) as conn:
        conn.execute("SELECT 1")
    return jsonify(status="ok")


@bp.post("/api/link-token")
def link_token():
    try:
        result = plaid().create_link_token(current_app.config["DEMO_OWNER_ID"], g.correlation_id)
    except PlaidError as error:
        return api_error(error, 503 if error.code == "NOT_CONFIGURED" else 502)
    return jsonify(link_token=result["link_token"])


@bp.post("/api/exchange")
def exchange():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Send a JSON object containing public_token."), 400
    public_token = body.get("public_token")
    if (
        not isinstance(public_token, str)
        or not public_token.startswith("public-sandbox-")
        or len(public_token) > 512
    ):
        return jsonify(error="A valid Sandbox public token is required."), 400
    institution = body.get("institution_name") or "Sandbox institution"
    if not isinstance(institution, str) or len(institution) > 150:
        return jsonify(error="Institution name must be text up to 150 characters."), 400
    institution_id = body.get("institution_id")
    link_session_id = body.get("link_session_id")
    account_ids = body.get("selected_account_ids", [])
    if (
        any(
            value is not None and not isinstance(value, str)
            for value in (institution_id, link_session_id)
        )
        or not isinstance(account_ids, list)
        or any(not isinstance(value, str) for value in account_ids)
    ):
        return jsonify(error="Invalid Link session metadata. Reload and try again."), 400
    try:
        client = plaid()  # Do not reserve an attempt before configuration is ready.
    except PlaidError as error:
        return api_error(error, 503)
    fresh, attempt = store().reserve_exchange(hashlib.sha256(public_token.encode()).hexdigest())
    if not fresh:
        if attempt and attempt["outcome"] == "saved":
            return load_accounts(attempt["item_id"])
        return jsonify(
            error="This token was already submitted. Reload to check saved connections. "
            "If none appears, inspect Plaid Dashboard Logs before linking again. "
            "Pocketview will not repeat an exchange with an uncertain result."
        ), 409
    try:
        result = client.exchange(public_token, g.correlation_id)
    except PlaidError as error:
        store().fail_exchange(attempt["id"], error)
        if error.ambiguous:
            return jsonify(
                error="The exchange outcome is uncertain. Do not connect again yet. "
                "Check Plaid Dashboard Logs for /item/public_token/exchange; "
                "remove any orphaned Sandbox Item before starting a new connection.",
                request_id=error.request_id,
                correlation_id=g.correlation_id,
            ), 502
        return api_error(error)
    encrypted = current_app.extensions["cipher"].encrypt(result["access_token"].encode()).decode()
    item_id = store().save_item(
        attempt["id"],
        result["item_id"],
        encrypted,
        institution,
        safe_request_id(result.get("request_id")),
        institution_id=institution_id,
        link_session_id=link_session_id,
        selected_account_ids=account_ids,
    )
    return load_accounts(item_id)


@bp.post("/api/items/<uuid:item_id>/accounts")
def retry_accounts(item_id):
    return load_accounts(item_id)


@bp.post("/api/items/<uuid:item_id>/sync")
def manual_sync(item_id):
    return run_sync(item_id)


@bp.post("/api/items/<uuid:item_id>/health")
def health(item_id):
    g.item_ref = str(item_id)
    try:
        return jsonify(
            check_health(
                store(), plaid(), current_app.extensions["cipher"], item_id, g.correlation_id
            )
        )
    except RecoveryError as error:
        return jsonify(error=str(error)), error.status
    except PlaidError as error:
        return api_error(error)


@bp.post("/api/items/<uuid:item_id>/reconnect-token")
def reconnect_token(item_id):
    g.item_ref = str(item_id)
    try:
        client = plaid()
        checked = check_health(
            store(), client, current_app.extensions["cipher"], item_id, g.correlation_id
        )
        if checked["state"] == "revoked":
            raise RecoveryError(checked["message"])
        item = owned_item(store(), item_id)
        token = decrypt_token(current_app.extensions["cipher"], item)
        result = client.update_link_token(
            current_app.config["DEMO_OWNER_ID"],
            token,
            g.correlation_id,
            str(item_id),
            consent=checked["state"] == "consent_required"
            or item["sync_error_code"] == "ADDITIONAL_CONSENT_REQUIRED",
        )
        return jsonify(link_token=result["link_token"])
    except RecoveryError as error:
        return jsonify(error=str(error)), error.status
    except PlaidError as error:
        return api_error(error)


@bp.post("/api/items/<uuid:item_id>/reconnect-complete")
def reconnect_complete(item_id):
    g.item_ref = str(item_id)
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("link_session_id", ""), str):
        return jsonify(error="Invalid Link completion metadata."), 400
    try:
        checked = check_health(
            store(), plaid(), current_app.extensions["cipher"], item_id, g.correlation_id
        )
        if checked["state"] != "connected":
            raise RecoveryError(checked["message"])
        if body.get("link_session_id"):
            store().record_link_session(item_id, body["link_session_id"])
        # Update mode keeps the same access token and Item. Never exchange again.
        return run_sync(item_id, prefix="Reconnected. ")
    except RecoveryError as error:
        return jsonify(error=str(error)), error.status
    except PlaidError as error:
        return api_error(error)


@bp.post("/api/items/<uuid:item_id>/disconnect")
def disconnect(item_id):
    g.item_ref = str(item_id)
    item = store().get_item(item_id)
    if not item:
        return jsonify(error="Connection not found."), 404
    if item["access_token_encrypted"]:
        token = (
            current_app.extensions["cipher"]
            .decrypt(item["access_token_encrypted"].encode())
            .decode()
        )
        try:
            plaid().remove(token, g.correlation_id, str(item_id))
        except PlaidError as error:
            # A prior remote removal may have succeeded before a local failure.
            if error.code not in {"INVALID_ACCESS_TOKEN", "ITEM_NOT_FOUND"}:
                return api_error(error)
    store().delete_item(item_id)
    return jsonify(message="Sandbox connection and local accounts removed.")
