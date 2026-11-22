"""Check current Item health before offering or completing Link update mode."""

from pocketview.plaid import PlaidError, safe_error_code, safe_request_id

MESSAGES = {
    "connected": "Plaid reports this connection is healthy.",
    "login_required": "Your bank needs you to sign in again. "
    "Use Reconnect; saved transactions are kept.",
    "consent_required": "Transactions consent is needed. Use Reconnect to review permissions.",
    "revoked": "This connection is no longer available. "
    "Saved history is kept; remove it before adding the bank again.",
    "error": "Plaid reports a connection problem. Wait, then check status again or retry Sync.",
}


class RecoveryError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def owned_item(store, item_id):
    item = store.get_item(item_id)
    if not item:
        raise RecoveryError("Connection not found.", 404)
    return item


def decrypt_token(cipher, item):
    if not item["access_token_encrypted"]:
        raise RecoveryError(MESSAGES["revoked"])
    return cipher.decrypt(item["access_token_encrypted"].encode()).decode()


def check_health(store, client, cipher, item_id, correlation_id):
    item = owned_item(store, item_id)
    if not item["access_token_encrypted"]:
        return {"state": "revoked", "message": MESSAGES["revoked"]}
    token = decrypt_token(cipher, item)
    try:
        result = client.item(token, correlation_id, str(item_id))
    except PlaidError as error:
        if error.code not in {"ITEM_NOT_FOUND", "INVALID_ACCESS_TOKEN", "ITEM_LOGIN_REQUIRED"}:
            raise
        state = store.record_health(item_id, error.code, error.request_id)
        return {"state": state, "message": MESSAGES[state]}
    metadata = result.get("item")
    if not isinstance(metadata, dict) or "error" not in metadata:
        raise RecoveryError(
            "Plaid returned an incomplete health check. Wait, then retry Check status.", 502
        )
    error = metadata["error"]
    if error is not None and not isinstance(error, dict):
        raise RecoveryError(
            "Plaid returned an incomplete health check. Wait, then retry Check status.", 502
        )
    code = safe_error_code(error.get("error_code")) if isinstance(error, dict) else None
    state = store.record_health(item_id, code, safe_request_id(result.get("request_id")))
    return {"state": state, "message": MESSAGES[state]}
