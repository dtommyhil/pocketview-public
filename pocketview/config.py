"""Load local settings and reject unsafe or incomplete configuration early."""

import os
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DEMO_OWNER = "pocketview-portfolio-demo"


def load_config(overrides=None):
    requested_mode = (overrides or {}).get("APP_MODE", os.environ.get("APP_MODE", "local"))
    if requested_mode != "demo":
        load_dotenv(ROOT / ".env")  # Existing process environment takes precedence.
    names = (
        "DATABASE_URL",
        "FLASK_SECRET_KEY",
        "TOKEN_ENCRYPTION_KEY",
        "DEMO_OWNER_ID",
        "PLAID_ENV",
        "PLAID_CLIENT_ID",
        "PLAID_SECRET",
        "APP_MODE",
        "PUBLIC_HOSTNAME",
    )
    settings = {name: os.environ.get(name, "").strip() for name in names}
    settings["PLAID_ENV"] = settings["PLAID_ENV"] or "sandbox"
    settings["APP_MODE"] = settings["APP_MODE"] or "local"
    settings.update(overrides or {})
    if settings["APP_MODE"] not in {"local", "demo"}:
        raise ValueError("APP_MODE must be local or demo.")
    demo = settings["APP_MODE"] == "demo"
    if demo:
        # The public app cannot select the local owner's data or initialize Plaid.
        settings.update(
            DEMO_OWNER_ID=PUBLIC_DEMO_OWNER,
            PLAID_CLIENT_ID="",
            PLAID_SECRET="",
            TOKEN_ENCRYPTION_KEY="",
        )
    required = (
        ("DATABASE_URL", "FLASK_SECRET_KEY")
        if demo
        else ("DATABASE_URL", "FLASK_SECRET_KEY", "TOKEN_ENCRYPTION_KEY", "DEMO_OWNER_ID")
    )
    for name in required:
        if not settings.get(name) or str(settings[name]).startswith("replace-with-"):
            raise ValueError(f"Configure {name} in the local .env. Run python scripts/init_env.py.")
    if settings["PLAID_ENV"] != "sandbox":
        raise ValueError("PLAID_ENV must be sandbox. Pocketview rejects other environments.")
    if len(settings["FLASK_SECRET_KEY"]) < 32:
        raise ValueError("FLASK_SECRET_KEY must contain at least 32 random characters.")
    if not demo:
        try:
            Fernet(settings["TOKEN_ENCRYPTION_KEY"].encode())
        except (ValueError, TypeError):
            raise ValueError(
                "TOKEN_ENCRYPTION_KEY must be a valid Fernet key in local .env."
            ) from None
    if bool(settings["PLAID_CLIENT_ID"]) != bool(settings["PLAID_SECRET"]):
        raise ValueError("Set both PLAID_CLIENT_ID and PLAID_SECRET in .env, or leave both blank.")
    if any(
        settings[name].startswith("replace-with-") for name in ("PLAID_CLIENT_ID", "PLAID_SECRET")
    ):
        raise ValueError("Replace the Plaid credential placeholders in the local .env.")
    if "@" in settings["DEMO_OWNER_ID"] or len(settings["DEMO_OWNER_ID"]) > 128:
        raise ValueError(
            "DEMO_OWNER_ID must be a stable opaque ID (up to 128 characters), not email."
        )
    if not demo and settings["DEMO_OWNER_ID"] == PUBLIC_DEMO_OWNER:
        raise ValueError("This owner reference is reserved for fictional public demo data.")
    hosts = ["localhost", "127.0.0.1", "[::1]"]
    if demo:
        hostname = settings["PUBLIC_HOSTNAME"] or os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
        if hostname:
            if len(hostname) > 253 or not all(
                part
                and len(part) <= 63
                and all(c.isascii() and (c.isalnum() or c == "-") for c in part)
                and not part.startswith("-")
                and not part.endswith("-")
                for part in hostname.split(".")
            ):
                raise ValueError("PUBLIC_HOSTNAME must be a hostname without scheme, path or port.")
            hosts.append(hostname)
    settings.update(
        SECRET_KEY=settings["FLASK_SECRET_KEY"],
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=demo,
        MAX_CONTENT_LENGTH=8192,
        TRUSTED_HOSTS=hosts,
    )
    return settings
