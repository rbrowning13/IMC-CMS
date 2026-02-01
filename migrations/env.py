from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

import os

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# Alembic resolves `config.config_file_name` relative to the CWD; in this repo
# alembic.ini lives at the project root (../alembic.ini), not inside migrations/.
# Resolve robustly so `flask db ...` works from any working directory.
if config.config_file_name is not None:
    cfg_path = config.config_file_name

    # If it's a relative path that doesn't exist, try the project-root alembic.ini
    if not os.path.isabs(cfg_path) and not os.path.exists(cfg_path):
        candidate = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
        if os.path.exists(candidate):
            cfg_path = candidate

    fileConfig(cfg_path)

# --- IMC-CMS: SQLAlchemy metadata for autogenerate ---
import sys

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.extensions import db  # ensure db is loaded
import app.models  # noqa: F401  (register all models)

target_metadata = db.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


# Helper to resolve DB URL for Alembic migrations
def _get_migration_db_url(section: dict | None = None) -> str:
    """Resolve the database URL Alembic should use.

    Priority:
      1) DATABASE_URL (standard)
      2) PSQL_URL (local dev convenience)
      3) SQLALCHEMY_DATABASE_URI (Flask app config convention)
      4) alembic.ini / config values

    Also normalizes SQLAlchemy-style driver URLs like
    `postgresql+psycopg2://...` to `postgresql://...` for consistency.
    """
    section = section or {}

    url = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("PSQL_URL")
        or os.environ.get("SQLALCHEMY_DATABASE_URI")
        or section.get("sqlalchemy.url")
        or section.get("url")
        or config.get_main_option("sqlalchemy.url")
    )

    if not url:
        raise RuntimeError(
            "DATABASE_URL (or PSQL_URL / SQLALCHEMY_DATABASE_URI / sqlalchemy.url) is not set for Alembic migrations"
        )

    # Normalize common SQLAlchemy driver URLs for Postgres.
    if url.startswith("postgresql+psycopg2://"):
        url = "postgresql://" + url[len("postgresql+psycopg2://") :]

    return url

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = _get_migration_db_url()
    if not url:
        raise RuntimeError("DATABASE_URL (or sqlalchemy.url) is not set for Alembic migrations")

    # Ensure Alembic's config has a URL (some setups rely on env vars instead of alembic.ini)
    try:
        config.set_main_option("sqlalchemy.url", url)
    except Exception:
        pass

    context.configure(
        url=url,
        target_metadata=target_metadata,
        compare_type=True,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    section = config.get_section(config.config_ini_section, {}) or {}

    # Alembic/Flask-Migrate expect a URL in the ini section. In this project we often rely on
    # DATABASE_URL in the environment, so patch it in if it's missing.
    url = _get_migration_db_url(section)
    if not url:
        raise RuntimeError("DATABASE_URL (or sqlalchemy.url) is not set for Alembic migrations")

    # engine_from_config reads keys after removing the `sqlalchemy.` prefix, so it wants either:
    #   section['sqlalchemy.url'] (preferred) or section['url'] (works in many cases)
    section.setdefault("sqlalchemy.url", url)
    section.setdefault("url", url)

    try:
        config.set_main_option("sqlalchemy.url", url)
    except Exception:
        pass

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
