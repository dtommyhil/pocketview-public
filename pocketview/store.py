"""Owner-scoped SQL for connections and accounts. Tokens arrive already encrypted."""

import uuid

from pocketview.db import connect


class Store:
    def __init__(self, database_url, owner_id):
        self.database_url = database_url
        self.owner_id = owner_id

    def list_items(self):
        with connect(self.database_url) as conn:
            items = conn.execute(
                """
                SELECT id, institution_name, connection_state, accounts_loaded_at,
                    sync_state, last_successful_sync_at, transactions_update_status,
                    sync_error_code,
                    health_checked_at, last_error_code
                FROM plaid_items WHERE owner_id = %s ORDER BY created_at, id
            """,
                (self.owner_id,),
            ).fetchall()
            accounts = conn.execute(
                """
                SELECT a.item_id, a.name, a.mask, a.type, a.subtype, a.currency_code
                FROM accounts a JOIN plaid_items i ON i.id = a.item_id
                WHERE i.owner_id = %s AND a.active ORDER BY a.name, a.id
            """,
                (self.owner_id,),
            ).fetchall()
        for item in items:
            item["accounts"] = [a for a in accounts if a["item_id"] == item["id"]]
        return items

    def get_item(self, item_id):
        with connect(self.database_url) as conn:
            return conn.execute(
                "SELECT * FROM plaid_items WHERE id = %s AND owner_id = %s",
                (item_id, self.owner_id),
            ).fetchone()

    def reserve_exchange(self, token_hash):
        """Commit before the remote call, so repeat submissions cannot exchange again."""
        attempt_id = uuid.uuid4()
        with connect(self.database_url) as conn:
            reserved = conn.execute(
                """
                INSERT INTO exchange_attempts (id, owner_id, public_token_hash)
                VALUES (%s, %s, %s) ON CONFLICT (public_token_hash) DO NOTHING RETURNING id
            """,
                (attempt_id, self.owner_id, token_hash),
            ).fetchone()
            if reserved:
                return True, {"id": attempt_id}
            prior = conn.execute(
                """
                SELECT id, outcome, item_id FROM exchange_attempts
                WHERE public_token_hash = %s AND owner_id = %s
            """,
                (token_hash, self.owner_id),
            ).fetchone()
            return False, prior

    def fail_exchange(self, attempt_id, error):
        with connect(self.database_url) as conn:
            conn.execute(
                """
                UPDATE exchange_attempts SET outcome = %s, error_code = %s, request_id = %s
                WHERE id = %s AND owner_id = %s
            """,
                (
                    "uncertain" if error.ambiguous else "failed",
                    error.code,
                    error.request_id,
                    attempt_id,
                    self.owner_id,
                ),
            )

    def save_item(
        self,
        attempt_id,
        plaid_item_id,
        encrypted_token,
        institution_name,
        request_id,
        institution_id=None,
        link_session_id=None,
        selected_account_ids=None,
    ):
        item_id = uuid.uuid4()
        with connect(self.database_url) as conn:
            conn.execute(
                """
                INSERT INTO plaid_items
                    (id, owner_id, plaid_item_id, access_token_encrypted, institution_name,
                     institution_id, link_session_id, link_selected_account_ids)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    item_id,
                    self.owner_id,
                    plaid_item_id,
                    encrypted_token,
                    institution_name,
                    institution_id,
                    link_session_id,
                    selected_account_ids or [],
                ),
            )
            conn.execute(
                """
                UPDATE exchange_attempts SET outcome = 'saved', item_id = %s, request_id = %s
                WHERE id = %s AND owner_id = %s
            """,
                (item_id, request_id, attempt_id, self.owner_id),
            )
        return item_id

    def save_accounts(self, item_id, accounts):
        with connect(self.database_url) as conn:
            # Serializes account refreshes and disconnects for this Item.
            owned = conn.execute(
                """
                SELECT id FROM plaid_items WHERE id = %s AND owner_id = %s FOR UPDATE
            """,
                (item_id, self.owner_id),
            ).fetchone()
            if not owned:
                return False
            self.upsert_accounts(conn, item_id, accounts)
            conn.execute(
                """
                UPDATE plaid_items SET connection_state = 'connected', accounts_loaded_at = now(),
                    last_error_code = NULL, last_request_id = NULL
                WHERE id = %s AND owner_id = %s
            """,
                (item_id, self.owner_id),
            )
        return True

    @staticmethod
    def upsert_accounts(conn, item_id, accounts):
        # This helper runs inside a caller's already owner-scoped transaction.
        conn.execute("UPDATE accounts SET active = false WHERE item_id = %s", (item_id,))
        for account in accounts:
            conn.execute(
                """
                INSERT INTO accounts
                    (item_id, plaid_account_id, name, official_name, mask, type, subtype,
                     currency_code)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (item_id, plaid_account_id) DO UPDATE SET
                    name = EXCLUDED.name, official_name = EXCLUDED.official_name,
                    mask = EXCLUDED.mask, type = EXCLUDED.type, subtype = EXCLUDED.subtype,
                    currency_code = EXCLUDED.currency_code, active = true
                """,
                (
                    item_id,
                    account["account_id"],
                    account["name"],
                    account.get("official_name"),
                    account.get("mask"),
                    account["type"],
                    account.get("subtype"),
                    (account.get("balances") or {}).get("iso_currency_code"),
                ),
            )

    def list_transactions(self):
        with connect(self.database_url) as conn:
            return conn.execute(
                """
                SELECT t.transaction_date, t.merchant_name, t.original_description,
                    t.amount, t.pending, t.category_primary, t.category_confidence,
                    a.name AS account_name, i.institution_name
                FROM transactions t
                JOIN plaid_items i ON i.id = t.item_id
                JOIN accounts a ON a.item_id = t.item_id AND
                    a.plaid_account_id = t.plaid_account_id
                WHERE i.owner_id = %s AND t.currency_code = 'USD'
                ORDER BY t.transaction_date DESC, t.plaid_transaction_id, t.item_id
                LIMIT 50
            """,
                (self.owner_id,),
            ).fetchall()

    def explore(self, filters):
        where, values = filters.where(self.owner_id)
        period_where, period_values = filters.period().where(self.owner_id)
        source = """
            FROM transactions t JOIN plaid_items i ON i.id = t.item_id
            JOIN accounts a ON a.item_id = t.item_id AND a.plaid_account_id = t.plaid_account_id
        """
        # Gross posted outflows. Refunds are excluded rather than netted; the UI
        # explains that this estimate also excludes recognized internal transfers.
        spending = """
            AND NOT t.pending AND t.amount > 0
            AND COALESCE(t.category_primary, '') NOT IN ('TRANSFER_IN', 'TRANSFER_OUT')
            AND COALESCE(t.category_detailed, '') <> 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'
        """
        with connect(self.database_url) as conn:
            # Counts, rows and aggregates describe the same database snapshot.
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            total = conn.execute(f"SELECT count(*) AS n {source} WHERE {where}", values).fetchone()[
                "n"
            ]
            pages = max(1, (total + filters.page_size - 1) // filters.page_size)
            page = min(filters.page, pages)
            rows = conn.execute(
                f"""
                SELECT t.transaction_date, t.merchant_name, t.original_description,
                    t.amount, t.pending, t.category_primary, t.category_confidence,
                    a.name AS account_name, i.institution_name
                {source} WHERE {where}
                ORDER BY t.transaction_date DESC, t.plaid_transaction_id, t.item_id
                LIMIT %s OFFSET %s
            """,
                [*values, filters.page_size, (page - 1) * filters.page_size],
            ).fetchall()
            categories = conn.execute(
                """
                SELECT DISTINCT COALESCE(NULLIF(t.category_primary, ''), 'UNCATEGORIZED') AS name
                FROM transactions t JOIN plaid_items i ON i.id = t.item_id
                WHERE i.owner_id = %s AND t.currency_code = 'USD' ORDER BY name
            """,
                (self.owner_id,),
            ).fetchall()
            by_category = conn.execute(
                f"""
                SELECT COALESCE(NULLIF(t.category_primary, ''), 'UNCATEGORIZED') AS label,
                    SUM(t.amount) AS amount
                {source} WHERE {period_where} {spending}
                GROUP BY label ORDER BY amount DESC, label
            """,
                period_values,
            ).fetchall()
            by_month = conn.execute(
                f"""
                SELECT to_char(t.transaction_date, 'YYYY-MM') AS label, SUM(t.amount) AS amount
                {source} WHERE {period_where} {spending} GROUP BY label ORDER BY label
            """,
                period_values,
            ).fetchall()
            summary = conn.execute(
                f"""
                SELECT COALESCE(SUM(t.amount), 0) AS amount, count(*) AS count,
                    count(*) FILTER (WHERE COALESCE(t.category_confidence, '')
                        NOT IN ('HIGH', 'VERY_HIGH')) AS uncertain
                {source} WHERE {period_where} {spending}
            """,
                period_values,
            ).fetchone()
        return {
            "rows": rows,
            "total": total,
            "page": page,
            "pages": pages,
            "categories": [r["name"] for r in categories],
            "by_category": by_category,
            "by_month": by_month,
            "summary": summary,
        }

    def record_health(self, item_id, code=None, request_id=None):
        state = {
            "ITEM_LOGIN_REQUIRED": "login_required",
            "INVALID_CREDENTIALS": "login_required",
            "ADDITIONAL_CONSENT_REQUIRED": "consent_required",
            "ITEM_NOT_FOUND": "revoked",
            "INVALID_ACCESS_TOKEN": "revoked",
        }.get(code, "error" if code else "connected")
        with connect(self.database_url) as conn:
            conn.execute(
                """
                UPDATE plaid_items SET connection_state = CASE
                    WHEN %s = 'connected' AND accounts_loaded_at IS NULL THEN 'accounts_pending'
                    ELSE %s END,
                    health_checked_at = now(), last_error_code = %s, last_request_id = %s,
                    access_token_encrypted = CASE WHEN %s = 'revoked' THEN NULL
                        ELSE access_token_encrypted END
                WHERE id = %s AND owner_id = %s
            """,
                (state, state, code, request_id, state, item_id, self.owner_id),
            )
        return state

    def record_link_session(self, item_id, session_id):
        with connect(self.database_url) as conn:
            conn.execute(
                "UPDATE plaid_items SET link_session_id = %s WHERE id = %s AND owner_id = %s",
                (session_id, item_id, self.owner_id),
            )

    def account_error(self, item_id, error):
        with connect(self.database_url) as conn:
            conn.execute(
                """
                UPDATE plaid_items SET last_error_code = %s, last_request_id = %s
                WHERE id = %s AND owner_id = %s
            """,
                (error.code, error.request_id, item_id, self.owner_id),
            )

    def delete_item(self, item_id):
        with connect(self.database_url) as conn:
            conn.execute(
                "DELETE FROM plaid_items WHERE id = %s AND owner_id = %s", (item_id, self.owner_id)
            )
