# Security scope

Pocketview is a portfolio project using Plaid Sandbox only. The local integration
accepts loopback requests and uses one demo owner; it is not a multi-user banking
service. The hosted demo serves separate fictional records and rejects API calls
and mutating requests before their handlers run.

## Credentials and data

- Keep `.env`, database connection strings, Plaid keys, access/public/Link tokens,
  and token encryption keys out of Git, screenshots, issues, and logs.
- `scripts/init_env.py` creates owner-only local configuration and refuses to
  overwrite it. `.env.example` contains placeholders, not working credentials.
- Access tokens are encrypted before persistence. Keep the encryption key outside
  PostgreSQL and preserve it to read existing tokens.
- The public demo needs only its dedicated database connection and server session
  key. Do not upload local Plaid credentials or local bank data to it.
- Use simulated Sandbox credentials in Link; never enter real bank credentials.

## Reporting a concern

Do not disclose credentials or exploitation details in a public issue. If GitHub
private vulnerability reporting is enabled, use **Security → Report a vulnerability**.
Otherwise contact the repository owner through a private channel before sharing
sensitive details. There is no guaranteed response time or formal support policy.

If a credential is exposed, revoke or rotate it with its provider. Deleting the
latest file does not remove it from Git history. Changing the token encryption key
also requires a plan for existing encrypted data.

## Verification limits

Tests cover request guards, owner isolation, encryption, rollback, and secret-safe
error handling. These are bounded checks, not a security audit or certification.
See [verification](docs/VALIDATION.md) and [architecture](docs/ARCHITECTURE.md).
