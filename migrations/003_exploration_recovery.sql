ALTER TABLE plaid_items
    DROP CONSTRAINT plaid_items_connection_state_check,
    ADD CONSTRAINT plaid_items_connection_state_check CHECK (connection_state IN
        ('accounts_pending', 'connected', 'login_required', 'consent_required', 'revoked', 'error')),
    ALTER COLUMN access_token_encrypted DROP NOT NULL,
    ADD COLUMN health_checked_at timestamptz;

CREATE INDEX transactions_category_date_idx ON transactions(category_primary, transaction_date DESC)
    WHERE currency_code = 'USD';
