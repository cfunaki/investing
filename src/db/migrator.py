"""
Lightweight database migrator that runs SQL migrations on app startup.

Scans src/db/migrations/*.sql for numbered migration files (e.g. 003_name.sql),
tracks applied migrations in a schema_migrations table, and applies any new ones
in order.
"""

import logging
from pathlib import Path

from sqlalchemy import text

from src.db.session import get_db_context

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def run_migrations() -> None:
    """Apply any pending SQL migrations on startup."""
    if not MIGRATIONS_DIR.exists():
        logger.info("No migrations directory found, skipping")
        return

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        logger.info("No migration files found")
        return

    async with get_db_context() as db:
        # Ensure tracking table exists
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """))

        # Get already-applied migrations
        result = await db.execute(text("SELECT filename FROM schema_migrations"))
        applied = {row[0] for row in result.fetchall()}

        for migration_file in migration_files:
            if migration_file.name in applied:
                continue

            logger.info(f"Applying migration: {migration_file.name}")
            sql = migration_file.read_text()

            # Execute each statement separately (sqlalchemy text() doesn't support multi-statement)
            for statement in sql.split(";"):
                statement = statement.strip()
                if statement and not statement.startswith("--"):
                    await db.execute(text(statement))

            await db.execute(
                text("INSERT INTO schema_migrations (filename) VALUES (:f)"),
                {"f": migration_file.name},
            )
            logger.info(f"Migration applied: {migration_file.name}")

    logger.info(f"Migrations complete ({len(migration_files)} total, {len(migration_files) - len(applied)} new)")
