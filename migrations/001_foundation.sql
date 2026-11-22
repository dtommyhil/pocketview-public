CREATE TABLE owners (
    id text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE plaid_items (
    id uuid PRIMARY KEY,
    owner_id text NOT NULL REFERENCES owners(id),
    plaid_item_id text NOT NULL UNIQUE,
    access_token_encrypted text NOT NULL,
    institution_name text NOT NULL DEFAULT 'Sandbox institution',
    connection_state text NOT NULL DEFAULT 'accounts_pending'
        CHECK (connection_state IN ('accounts_pending', 'connected')),
    accounts_loaded_at timestamptz,
    last_error_code text,
    last_request_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, owner_id)
);
CREATE INDEX plaid_items_owner_idx ON plaid_items(owner_id);

CREATE TABLE accounts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    item_id uuid NOT NULL REFERENCES plaid_items(id) ON DELETE CASCADE,
    plaid_account_id text NOT NULL UNIQUE,
    name text NOT NULL,
    official_name text,
    mask text,
    type text NOT NULL,
    subtype text,
    currency_code text,
    UNIQUE (item_id, plaid_account_id)
);
CREATE INDEX accounts_item_idx ON accounts(item_id);

-- A public token must never be exchanged again automatically after an ambiguous
-- outcome. Only its SHA-256 digest is retained, never the public token itself.
CREATE TABLE exchange_attempts (
    id uuid PRIMARY KEY,
    owner_id text NOT NULL REFERENCES owners(id),
    public_token_hash text NOT NULL UNIQUE,
    outcome text NOT NULL DEFAULT 'started'
        CHECK (outcome IN ('started', 'saved', 'failed', 'uncertain')),
    item_id uuid,
    error_code text,
    request_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (item_id, owner_id) REFERENCES plaid_items(id, owner_id) ON DELETE CASCADE
);
CREATE INDEX exchange_attempts_owner_idx ON exchange_attempts(owner_id);
