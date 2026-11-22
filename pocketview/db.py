"""Small SQL migration runner. No ORM; every data value is a SQL parameter."""

import hashlib

import click
import psycopg
from psycopg.rows import dict_row

from pocketview.config import ROOT


def connect(database_url):
    return psycopg.connect(database_url, row_factory=dict_row, connect_timeout=5)


def migrate(database_url, owner_id):
    with connect(database_url) as conn:
        # Serialize migration runners. DDL and migration history commit together.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (810035001,))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version text PRIMARY KEY,
                checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        for file in sorted((ROOT / "migrations").glob("*.sql")):
            source = file.read_text()
            checksum = hashlib.sha256(source.encode()).hexdigest()
            prior = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version = %s", (file.name,)
            ).fetchone()
            if prior:
                if prior["checksum"] != checksum:
                    raise click.ClickException(
                        f"Applied migration {file.name} changed. Restore it."
                    )
                continue
            conn.execute(source)
            conn.execute(
                "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                (file.name, checksum),
            )
        conn.execute("INSERT INTO owners (id) VALUES (%s) ON CONFLICT DO NOTHING", (owner_id,))


def register_cli(app):
    @app.cli.command("db-migrate")
    def db_migrate():
        """Apply new migrations and create the configured demo owner."""
        try:
            migrate(app.config["DATABASE_URL"], app.config["DEMO_OWNER_ID"])
        except psycopg.Error:
            raise click.ClickException(
                "Database migration failed. Check Docker, DATABASE_URL and migration files. "
                "Database exception details are withheld to protect credentials."
            ) from None
        click.echo("Migrations applied; demo owner is ready.")
