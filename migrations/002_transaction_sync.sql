ALTER TABLE plaid_items
    ADD COLUMN institution_id text,
    ADD COLUMN link_session_id text,
    ADD COLUMN link_selected_account_ids text[] NOT NULL DEFAULT '{}',
    ADD COLUMN sync_cursor text,
    ADD COLUMN sync_state text NOT NULL DEFAULT 'never'
        CHECK (sync_state IN ('never', 'loading', 'ready', 'failed')),
    ADD COLUMN last_successful_sync_at timestamptz,
    ADD COLUMN transactions_update_status text,
    ADD COLUMN sync_error_code text,
    ADD COLUMN sync_request_id text;

-- Keep historical accounts when Plaid no longer returns them, so refreshing
-- account metadata cannot delete imported transactions.
ALTER TABLE accounts ADD COLUMN active boolean NOT NULL DEFAULT true;

CREATE TABLE transactions (
    item_id uuid NOT NULL REFERENCES plaid_items(id) ON DELETE CASCADE,
    plaid_transaction_id text NOT NULL,
    plaid_account_id text NOT NULL,
    pending_transaction_id text,
    transaction_date date NOT NULL,
    authorized_date date,
    merchant_name text,
    original_description text,
    amount numeric NOT NULL,
    currency_code text,
    pending boolean NOT NULL,
    payment_channel text,
    category_primary text,
    category_detailed text,
    category_confidence text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (item_id, plaid_transaction_id),
    FOREIGN KEY (item_id, plaid_account_id)
        REFERENCES accounts(item_id, plaid_account_id)
);
CREATE INDEX transactions_date_idx ON transactions(transaction_date DESC, plaid_transaction_id);
CREATE INDEX transactions_account_idx ON transactions(item_id, plaid_account_id);
CREATE INDEX transactions_pending_idx ON transactions(item_id, pending_transaction_id)
    WHERE pending_transaction_id IS NOT NULL;

CREATE TABLE sync_attempts (
    id uuid PRIMARY KEY,
    item_id uuid NOT NULL REFERENCES plaid_items(id) ON DELETE CASCADE,
    outcome text NOT NULL DEFAULT 'started' CHECK (outcome IN ('started', 'succeeded', 'failed')),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    error_code text,
    request_id text,
    added_count integer,
    modified_count integer,
    removed_count integer
);
CREATE INDEX sync_attempts_item_time_idx ON sync_attempts(item_id, started_at DESC);
