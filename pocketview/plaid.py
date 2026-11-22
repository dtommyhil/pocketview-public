"""Sandbox-only REST calls, timeouts and safe error messages. Never log payloads."""

import json
import logging
import random
import time
from decimal import Decimal

import httpx

logger = logging.getLogger("pocketview.plaid")
SAFE_CODES = {
    "INVALID_API_KEYS",
    "UNAUTHORIZED_ENVIRONMENT",
    "INVALID_PUBLIC_TOKEN",
    "INVALID_LINK_TOKEN",
    "INVALID_ACCESS_TOKEN",
    "INVALID_REQUEST",
    "MISSING_FIELDS",
    "INVALID_FIELD",
    "INVALID_PRODUCT",
    "ITEM_LOGIN_REQUIRED",
    "RATE_LIMIT",
    "ACCOUNTS_LIMIT",
    "INTERNAL_SERVER_ERROR",
    "PLANNED_MAINTENANCE",
    "INSTITUTION_DOWN",
    "INSTITUTION_NOT_RESPONDING",
    "ITEM_NOT_FOUND",
    "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION",
    "TRANSACTIONS_SYNC_LIMIT",
    "PRODUCT_NOT_READY",
    "ADDITIONAL_CONSENT_REQUIRED",
    "INVALID_CREDENTIALS",
    "ITEM_LOCKED",
    "ACCESS_NOT_GRANTED",
    "USER_PERMISSION_REVOKED",
}


def safe_request_id(value):
    # Identifiers are opaque and may change format. JSON logging escapes controls.
    return value if isinstance(value, str) else None


def safe_error_code(value):
    return value if isinstance(value, str) and value in SAFE_CODES else "PLAID_ERROR"


class PlaidError(Exception):
    def __init__(
        self, message, *, code="PLAID_ERROR", request_id=None, ambiguous=False, retryable=False
    ):
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.ambiguous = ambiguous
        self.retryable = retryable


class PlaidClient:
    def __init__(self, client_id, secret, *, transport=None):
        self.client_id = client_id
        self.secret = secret
        self.transport = transport

    def post(
        self, endpoint, payload, *, correlation_id, item_ref=None, read_only=False, timeout=20.0
    ):
        started = time.monotonic()
        request_id = None
        outcome = "failed"
        try:
            with httpx.Client(
                timeout=httpx.Timeout(timeout, connect=5.0),
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                for attempt in range(2 if read_only else 1):
                    try:
                        response = client.post(
                            f"https://sandbox.plaid.com{endpoint}",
                            headers={
                                "PLAID-CLIENT-ID": self.client_id,
                                "PLAID-SECRET": self.secret,
                                "Plaid-Version": "2020-09-14",
                            },
                            json=payload,
                        )
                    except httpx.RequestError:
                        raise PlaidError(
                            "Plaid could not be reached. Check your internet connection.",
                            code="NETWORK_ERROR",
                            ambiguous=not read_only,
                        ) from None
                    try:
                        # Parse money directly as Decimal, before it can become a binary float.
                        data = response.json(parse_float=Decimal)
                        if not isinstance(data, dict):
                            raise ValueError
                    except ValueError:
                        raise PlaidError(
                            "Plaid returned an unreadable response. Try later.",
                            code="INVALID_RESPONSE",
                            ambiguous=not read_only,
                        ) from None
                    request_id = safe_request_id(data.get("request_id"))
                    if response.is_success:
                        outcome = "success"
                        return data
                    raw_code = data.get("error_code")
                    code = safe_error_code(raw_code)
                    error_type = data.get("error_type")
                    if (
                        read_only
                        and attempt == 0
                        and (
                            error_type == "RATE_LIMIT_EXCEEDED"
                            or code
                            in {
                                "INSTITUTION_DOWN",
                                "INSTITUTION_NOT_RESPONDING",
                                "INTERNAL_SERVER_ERROR",
                                "PLANNED_MAINTENANCE",
                            }
                        )
                    ):
                        time.sleep(0.5 + random.uniform(0, 0.25))
                        continue
                    if code in {"INVALID_API_KEYS", "UNAUTHORIZED_ENVIRONMENT"}:
                        message = (
                            "Check PLAID_CLIENT_ID and the Sandbox PLAID_SECRET in local .env."
                        )
                    elif code == "ITEM_LOGIN_REQUIRED":
                        message = (
                            "This connection needs reauthentication. Use Reconnect to repair it."
                        )
                    elif code == "PRODUCT_NOT_READY":
                        message = (
                            "Plaid is still preparing transaction history. Wait, then use Sync."
                        )
                    elif code == "ADDITIONAL_CONSENT_REQUIRED":
                        message = (
                            "This connection has not consented to Transactions. "
                            "Use Reconnect to grant the required consent."
                        )
                    elif error_type == "RATE_LIMIT_EXCEEDED":
                        message = "Plaid's request limit was reached. Wait before trying again."
                    elif error_type == "API_ERROR" or code in {
                        "INTERNAL_SERVER_ERROR",
                        "PLANNED_MAINTENANCE",
                        "INSTITUTION_DOWN",
                        "INSTITUTION_NOT_RESPONDING",
                    }:
                        message = (
                            "Plaid is temporarily unavailable. "
                            "Check status.plaid.com and try later."
                        )
                    elif code in {"INVALID_PUBLIC_TOKEN", "INVALID_LINK_TOKEN"}:
                        message = (
                            "The connection token expired or is invalid. "
                            "Start a fresh Link session."
                        )
                    else:
                        message = (
                            "Plaid rejected this request. "
                            "Check the reference in Plaid Dashboard Logs."
                        )
                    raise PlaidError(
                        message,
                        code=code,
                        request_id=request_id,
                        ambiguous=not read_only and response.status_code >= 500,
                        retryable=error_type == "RATE_LIMIT_EXCEEDED",
                    )
        finally:
            logger.info(
                json.dumps(
                    {
                        "event": "plaid_request",
                        "correlation_id": correlation_id,
                        "endpoint": endpoint,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                        "outcome": outcome,
                        "plaid_request_id": request_id,
                        "item_ref": item_ref,
                    }
                )
            )

    def create_link_token(self, owner_id, correlation_id):
        return self.post(
            "/link/token/create",
            {
                "client_name": "Pocketview",
                "language": "en",
                "country_codes": ["US"],
                "user": {"client_user_id": owner_id},
                "products": ["transactions"],
                "enable_multi_item_link": False,
            },
            correlation_id=correlation_id,
        )

    def exchange(self, public_token, correlation_id):
        return self.post(
            "/item/public_token/exchange",
            {"public_token": public_token},
            correlation_id=correlation_id,
        )

    def accounts(self, access_token, correlation_id, item_ref):
        return self.post(
            "/accounts/get",
            {"access_token": access_token},
            correlation_id=correlation_id,
            item_ref=item_ref,
            read_only=True,
        )

    def remove(self, access_token, correlation_id, item_ref):
        return self.post(
            "/item/remove",
            {"access_token": access_token},
            correlation_id=correlation_id,
            item_ref=item_ref,
        )

    def sync(self, access_token, cursor, correlation_id, item_ref):
        payload = {
            "access_token": access_token,
            "count": 500,
            "options": {
                "include_original_description": True,
                "personal_finance_category_version": "v2",
            },
        }
        if cursor is not None:
            payload["cursor"] = cursor
        # Pagination errors must restart the complete update in the sync service.
        # That service, rather than this HTTP layer, owns the retry boundary.
        return self.post(
            "/transactions/sync",
            payload,
            correlation_id=correlation_id,
            item_ref=item_ref,
            timeout=90.0,
        )

    def item(self, access_token, correlation_id, item_ref):
        return self.post(
            "/item/get",
            {"access_token": access_token},
            correlation_id=correlation_id,
            item_ref=item_ref,
            read_only=True,
        )

    def update_link_token(self, owner_id, access_token, correlation_id, item_ref, *, consent=False):
        payload = {
            "client_name": "Pocketview",
            "language": "en",
            "country_codes": ["US"],
            "user": {"client_user_id": owner_id},
            "access_token": access_token,
            "products": [],
        }
        if consent:
            payload["additional_consented_products"] = ["transactions"]
        return self.post(
            "/link/token/create", payload, correlation_id=correlation_id, item_ref=item_ref
        )

    def reset_login(self, access_token, correlation_id, item_ref):
        return self.post(
            "/sandbox/item/reset_login",
            {"access_token": access_token},
            correlation_id=correlation_id,
            item_ref=item_ref,
        )
