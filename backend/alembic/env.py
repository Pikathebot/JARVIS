import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool
from alembic import context
from sqlmodel import SQLModel

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.config import settings
import app.database.models  # ensure models are registered in SQLModel.metadata

config = context.config

# Only take over logging when Alembic runs from its own CLI. FastAPI runs these migrations
# in-process at startup, and fileConfig() would reset the root logger to WARN and disable every
# logger created so far (all of jarvis.*) -- backend.log then held the startup burst and nothing
# else for the whole session. The app signals the in-process case with this attribute.
if config.config_file_name is not None and not config.attributes.get("configure_logging_skipped"):
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def get_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    return url if url else settings.database_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"check_same_thread": False},
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
