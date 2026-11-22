-- Usage from the project root (owner_id is not a secret):
-- docker compose exec -T db psql -U pocketview -d pocketview \
--   -v owner_id=pocketview-local-demo < docs/investigation.sql
\set ON_ERROR_STOP on
\if :{?owner_id}
\else
  \echo 'Supply -v owner_id=YOUR_DEMO_OWNER_ID. No queries were run.'
  \quit 1
\endif
\pset pager off
BEGIN READ ONLY;

-- 1. Verify applied versions without modifying migration history.
SELECT version, applied_at FROM schema_migrations ORDER BY version;

-- 2. Inspect local connection state and counts. Tokens/Plaid Item IDs/cursors are omitted.
SELECT i.id AS local_item_id, i.institution_name, i.connection_state,
       i.sync_state, i.transactions_update_status,
       i.sync_cursor IS NOT NULL AS cursor_saved,
       i.last_successful_sync_at, i.health_checked_at,
       i.sync_error_code, i.last_error_code,
       (SELECT count(*) FROM accounts a WHERE a.item_id = i.id AND a.active) AS active_accounts,
       (SELECT count(*) FROM transactions t WHERE t.item_id = i.id) AS transactions
FROM plaid_items i WHERE i.owner_id = :'owner_id'
ORDER BY i.created_at, i.id;

-- 3. Follow recent attempts, including failed/interrupted runs.
SELECT s.item_id AS local_item_id, s.outcome, s.started_at, s.finished_at,
       s.error_code, s.request_id, s.added_count, s.modified_count, s.removed_count
FROM sync_attempts s JOIN plaid_items i ON i.id = s.item_id
WHERE i.owner_id = :'owner_id'
ORDER BY s.started_at DESC, s.id LIMIT 20;

-- 4. Do not add different currencies together.
SELECT t.currency_code, count(*) AS records,
       count(*) FILTER (WHERE t.pending) AS pending_records,
       min(t.transaction_date) AS oldest, max(t.transaction_date) AS newest
FROM transactions t JOIN plaid_items i ON i.id = t.item_id
WHERE i.owner_id = :'owner_id'
GROUP BY t.currency_code ORDER BY t.currency_code;

-- 5. Expect no duplicate (Item, transaction) pairs; the primary key enforces this.
SELECT count(*) AS duplicate_pairs FROM (
    SELECT t.item_id, t.plaid_transaction_id
    FROM transactions t JOIN plaid_items i ON i.id = t.item_id
    WHERE i.owner_id = :'owner_id'
    GROUP BY t.item_id, t.plaid_transaction_id HAVING count(*) > 1
) duplicates;

-- 6. Expect no pending predecessor left beside its imported posted replacement.
SELECT count(*) AS pending_predecessors_still_saved
FROM transactions posted JOIN transactions pending
  ON pending.item_id = posted.item_id
  AND pending.plaid_transaction_id = posted.pending_transaction_id
JOIN plaid_items i ON i.id = posted.item_id
WHERE i.owner_id = :'owner_id' AND pending.pending AND NOT posted.pending;

-- 7. Inspect recent saved activity without selecting account/transaction identifiers.
SELECT t.transaction_date, t.merchant_name, t.original_description,
       a.name AS account_name, t.amount, t.currency_code, t.pending,
       t.category_primary, t.category_detailed, t.category_confidence
FROM transactions t JOIN plaid_items i ON i.id = t.item_id
JOIN accounts a ON a.item_id = t.item_id AND a.plaid_account_id = t.plaid_account_id
WHERE i.owner_id = :'owner_id'
ORDER BY t.transaction_date DESC, t.plaid_transaction_id, t.item_id LIMIT 25;

-- 8. Reproduce the unfiltered chart's exact monthly spending calculation.
-- This estimate uses gross posted outflows; refunds are excluded, not netted.
SELECT to_char(t.transaction_date, 'YYYY-MM') AS month, sum(t.amount) AS posted_usd_outflows
FROM transactions t JOIN plaid_items i ON i.id = t.item_id
WHERE i.owner_id = :'owner_id' AND t.currency_code = 'USD'
  AND NOT t.pending AND t.amount > 0
  AND COALESCE(t.category_primary, '') NOT IN ('TRANSFER_IN', 'TRANSFER_OUT')
  AND COALESCE(t.category_detailed, '') <> 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'
GROUP BY month ORDER BY month;

ROLLBACK;
