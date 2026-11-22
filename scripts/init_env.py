"""Create local configuration without printing any secrets or overwriting .env."""

import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

root = Path(__file__).resolve().parents[1]
content = (root / ".env.example").read_text()
for placeholder, value in {
    "replace-with-local-database-password": secrets.token_hex(24),
    "replace-with-random-session-key": secrets.token_hex(32),
    "replace-with-fernet-key": Fernet.generate_key().decode(),
}.items():
    content = content.replace(placeholder, value)

try:
    descriptor = os.open(root / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit(".env already exists; left it unchanged. Edit it locally if needed.") from None
with os.fdopen(descriptor, "w") as output:
    output.write(content)
print("Created .env with fresh local secrets. Add Sandbox credentials there when ready.")
