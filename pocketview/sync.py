"""Fetch a complete Plaid update, then commit its rows and cursor together."""

import json
import logging
import random
import time
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

import psycopg
from cryptography.fernet import InvalidToken

from pocketview.db import connect
from pocketview.plaid import PlaidError, safe_request_id

logger = logging.getLogger("pocketview.sync")
MUTATION = "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
TRANSIENT = {
    "NETWORK_ERROR",
    "INTERNAL_SERVER_ERROR",
    "PLANNED_MAINTENANCE",
    "INSTITUTION_DOWN",
    "INSTITUTION_NOT_RESPONDING",
    "TRANSACTIONS_SYNC_LIMIT",
    "RATE_LIMIT",
}


class SyncError(Exception):
    def __init__(self, message, code="SYNC_FAILED", status=502, request_id=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.request_id = request_id


def collect_updates(client, token, original_cursor, correlation_id, item_ref):
    """Every retry discards all buffered pages and starts at the stored cursor."""
    for restart in range(3):
        added, modified, removed = [], [], []
        cursor = original_cursor
        seen = {cursor}
        try:
            for _ in range(100):
                page = client.sync(token, cursor, correlation_id, item_ref)
                if (
                    type(page.get("has_more")) is not bool
                    or not isinstance(page.get("next_cursor"), str)
                    or any(
                        not isinstance(page.get(key), list)
                        for key in ("added", "modified", "removed")
                    )
                ):
                    raise SyncError(
                        "Plaid returned an incomplete update. Existing data is unchanged.",
                        "INVALID_RESPONSE",
                    )
                added.extend(page["added"])
                modified.extend(page["modified"])
                removed.extend(page["removed"])
                cursor = page["next_cursor"]
                if not page["has_more"]:
                    return {
                        "added": added,
                        "modified": modified,
                        "removed": removed,
                        "cursor": cursor,
                        "update_status": page.get("transactions_update_status"),
                        "request_id": safe_request_id(page.get("request_id")),
                    }
                if cursor in seen:
                    raise SyncError(
                        "Plaid repeated a page cursor. Existing data is unchanged.",
                        "INVALID_RESPONSE",
                    )
                seen.add(cursor)
            raise SyncError(
                "This update exceeded the demo's 100-page safety limit. "
                "No changes were saved; inspect the sync attempt before retrying.",
                "UPDATE_TOO_LARGE",
            )
        except PlaidError as error:
            if restart < 2 and (
                error.code == MUTATION
                or error.code in TRANSIENT
                or getattr(error, "retryable", False)
            ):
                time.sleep(0.5 * (2**restart) + random.uniform(0, 0.25))
                continue
            raise


def transaction_values(item_id, row):
    amount = row["amount"]
    if isinstance(amount, bool) or not isinstance(amount, (int, Decimal)):
        raise ValueError("Money must be parsed as Decimal or integer, never float.")
    amount = Decimal(amount)
    if not amount.is_finite() or type(row["pending"]) is not bool:
        raise ValueError("Invalid money or pending state.")
    for key in ("transaction_id", "account_id"):
        if not isinstance(row[key], str) or not row[key]:
            raise ValueError("Missing transaction/account identifier.")
    category = row.get("personal_finance_category") or {}
    return (
        item_id,
        row["transaction_id"],
        row["account_id"],
        row.get("pending_transaction_id"),
        date.fromisoformat(row["date"]),
        date.fromisoformat(row["authorized_date"]) if row.get("authorized_date") else None,
        row.get("merchant_name"),
        row.get("original_description"),
        amount,
        row.get("iso_currency_code") or row.get("unofficial_currency_code"),
        row["pending"],
        row.get("payment_channel"),
        category.get("primary"),
        category.get("detailed"),
        category.get("confidence_level"),
    )


def apply_update(conn, item_id, update):
    for row in update["added"] + update["modified"]:
        conn.execute(
            """
            INSERT INTO transactions
                (item_id, plaid_transaction_id, plaid_account_id, pending_transaction_id,
                 transaction_date, authorized_date, merchant_name, original_description,
                 amount, currency_code, pending, payment_channel,
                 category_primary, category_detailed, category_confidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (item_id, plaid_transaction_id) DO UPDATE SET
                plaid_account_id = EXCLUDED.plaid_account_id,
                pending_transaction_id = EXCLUDED.pending_transaction_id,
                transaction_date = EXCLUDED.transaction_date,
                authorized_date = EXCLUDED.authorized_date,
                merchant_name = EXCLUDED.merchant_name,
                original_description = EXCLUDED.original_description,
                amount = EXCLUDED.amount, currency_code = EXCLUDED.currency_code,
                pending = EXCLUDED.pending, payment_channel = EXCLUDED.payment_channel,
                category_primary = EXCLUDED.category_primary,
                category_detailed = EXCLUDED.category_detailed,
                category_confidence = EXCLUDED.category_confidence, updated_at = now()
        """,
            transaction_values(item_id, row),
        )
    for row in update["removed"]:
        conn.execute(
            "DELETE FROM transactions WHERE item_id = %s AND plaid_transaction_id = %s",
            (item_id, row["transaction_id"]),
        )
    # Pending and posted versions have different IDs, potentially across pages.
    # Run this after all upserts, so page order cannot resurrect the pending row.
    conn.execute(
        """
        DELETE FROM transactions pending USING transactions posted
        WHERE pending.item_id = %s AND posted.item_id = pending.item_id
            AND pending.pending AND NOT posted.pending
            AND posted.pending_transaction_id = pending.plaid_transaction_id
    """,
        (item_id,),
    )


def record_failure(conn, owner_id, item_id, attempt_id, error):
    conn.execute(
        """
        UPDATE sync_attempts s SET outcome = 'failed', finished_at = now(),
            error_code = %s, request_id = %s
        FROM plaid_items i WHERE s.id = %s AND s.item_id = i.id AND i.owner_id = %s
    """,
        (error.code, error.request_id, attempt_id, owner_id),
    )
    conn.execute(
        """
        UPDATE plaid_items SET sync_state = %s, sync_error_code = %s, sync_request_id = %s
            , connection_state = CASE
                WHEN %s IN ('ITEM_LOGIN_REQUIRED', 'INVALID_CREDENTIALS') THEN 'login_required'
                WHEN %s = 'ADDITIONAL_CONSENT_REQUIRED' THEN 'consent_required'
                WHEN %s IN ('ITEM_NOT_FOUND', 'INVALID_ACCESS_TOKEN') THEN 'revoked'
                ELSE connection_state END
            , access_token_encrypted = CASE
                WHEN %s IN ('ITEM_NOT_FOUND', 'INVALID_ACCESS_TOKEN') THEN NULL
                ELSE access_token_encrypted END
        WHERE id = %s AND owner_id = %s
    """,
        (
            "loading" if error.code == "PRODUCT_NOT_READY" else "failed",
            error.code,
            error.request_id,
            error.code,
            error.code,
            error.code,
            error.code,
            item_id,
            owner_id,
        ),
    )


def sync_item(store, client, cipher, item_id, correlation_id):
    started = time.monotonic()
    outcome = "failed"
    request_id = None
    try:
        with connect(store.database_url) as conn:
            # The session lock survives a rollback, allowing the failure ledger to
            # be saved before another sync starts. Closing this connection releases it.
            conn.autocommit = True
            item = conn.execute(
                "SELECT id FROM plaid_items WHERE id = %s AND owner_id = %s",
                (item_id, store.owner_id),
            ).fetchone()
            if not item:
                raise SyncError("Connection not found.", "NOT_FOUND", 404)
            locked = conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked", (str(item_id),)
            ).fetchone()["locked"]
            if not locked:
                raise SyncError(
                    "A sync is already running for this connection. Wait, then reload.",
                    "SYNC_BUSY",
                    409,
                )
            conn.execute(
                """
                UPDATE sync_attempts SET outcome = 'failed', finished_at = now(),
                    error_code = 'INTERRUPTED' WHERE item_id = %s AND outcome = 'started'
            """,
                (item_id,),
            )
            attempt_id = uuid.uuid4()
            conn.execute(
                "INSERT INTO sync_attempts (id, item_id) VALUES (%s, %s)", (attempt_id, item_id)
            )
            try:
                with conn.transaction():
                    item = conn.execute(
                        """
                        SELECT * FROM plaid_items WHERE id = %s AND owner_id = %s
                        FOR NO KEY UPDATE
                    """,
                        (item_id, store.owner_id),
                    ).fetchone()
                    if not item:
                        raise SyncError(
                            "Connection was removed. Reload the page.", "NOT_FOUND", 404
                        )
                    if not item["access_token_encrypted"]:
                        raise SyncError(
                            "This connection is no longer available. Saved history is kept; "
                            "remove this connection before adding the bank again.",
                            "ITEM_NOT_FOUND",
                            409,
                        )
                    token = cipher.decrypt(item["access_token_encrypted"].encode()).decode()
                    update = collect_updates(
                        client, token, item["sync_cursor"], correlation_id, str(item_id)
                    )
                    request_id = update["request_id"]
                    known_accounts = {
                        r["plaid_account_id"]
                        for r in conn.execute(
                            "SELECT plaid_account_id FROM accounts WHERE item_id = %s", (item_id,)
                        )
                    }
                    needed_accounts = {
                        r["account_id"] for r in update["added"] + update["modified"]
                    }
                    if not needed_accounts.issubset(known_accounts):
                        # /accounts/get, not the sync response, is the authoritative list.
                        accounts = client.accounts(token, correlation_id, str(item_id))["accounts"]
                        store.upsert_accounts(conn, item_id, accounts)
                    apply_update(conn, item_id, update)
                    ready = update["update_status"] == "HISTORICAL_UPDATE_COMPLETE"
                    conn.execute(
                        """
                        UPDATE plaid_items SET sync_cursor = %s, sync_state = %s,
                            transactions_update_status = %s, last_successful_sync_at = now(),
                            sync_error_code = NULL, sync_request_id = %s
                            , connection_state = 'connected', last_error_code = NULL
                        WHERE id = %s AND owner_id = %s
                    """,
                        (
                            update["cursor"],
                            "ready" if ready else "loading",
                            update["update_status"],
                            request_id,
                            item_id,
                            store.owner_id,
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE sync_attempts SET outcome = 'succeeded', finished_at = now(),
                            request_id = %s, added_count = %s,
                            modified_count = %s, removed_count = %s
                        WHERE id = %s
                    """,
                        (
                            request_id,
                            len(update["added"]),
                            len(update["modified"]),
                            len(update["removed"]),
                            attempt_id,
                        ),
                    )
            except (
                PlaidError,
                SyncError,
                InvalidToken,
                psycopg.Error,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                InvalidOperation,
            ) as error:
                if isinstance(error, PlaidError):
                    failure = SyncError(str(error), error.code, request_id=error.request_id)
                elif isinstance(error, SyncError):
                    failure = error
                elif isinstance(error, InvalidToken):
                    failure = SyncError(
                        "Restore the original TOKEN_ENCRYPTION_KEY in .env and restart.",
                        "ENCRYPTION_KEY_MISMATCH",
                        503,
                    )
                elif isinstance(error, psycopg.Error):
                    failure = SyncError(
                        "The local database could not save this update. "
                        "Check Docker and migrations, then use Sync. Existing data is unchanged.",
                        "DATABASE_ERROR",
                        503,
                        request_id=request_id,
                    )
                else:
                    failure = SyncError(
                        "Plaid returned data the demo could not safely import. "
                        "Existing data is unchanged; inspect the sync attempt before retrying.",
                        "INVALID_RESPONSE",
                        request_id=request_id,
                    )
                request_id = failure.request_id
                record_failure(conn, store.owner_id, item_id, attempt_id, failure)
                raise failure from None
            outcome = "success"
            return {
                "message": (
                    "Transactions synced."
                    if ready
                    else "Updates saved. History is not confirmed complete; wait, then use Sync."
                ),
                "state": "ready" if ready else "loading",
            }
    finally:
        logger.info(
            json.dumps(
                {
                    "event": "transaction_sync",
                    "correlation_id": correlation_id,
                    "endpoint": "/transactions/sync",
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "outcome": outcome,
                    "plaid_request_id": request_id,
                    "item_ref": str(item_id),
                }
            )
        )
