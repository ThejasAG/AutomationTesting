from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from automation.database.models import Base
target_metadata = Base.metadata

# Run against whatever DATABASE_URL points at, exactly as the application does.
# alembic.ini's sqlalchemy.url is only the fallback for a bare `alembic` invocation;
# without this, autogenerate and upgrade silently target a different database than
# the app — which is how the 4-table baseline came to be stamped on a 25-table DB.
_url = os.getenv("DATABASE_URL")
if not _url:
    raise RuntimeError(
        "DATABASE_URL is not set. Alembic refuses to guess which database to "
        "migrate.\n\n"
        "alembic.ini used to name sqlite:///test_automation_new.db as a fallback, "
        "so an unset DATABASE_URL silently pointed migrations at a stale "
        "repo-root file instead of the real database. A schema migration aimed at "
        "the wrong database is the worst failure this tool can have, so it now "
        "stops instead.\n\n"
        "Run it explicitly, e.g.:\n"
        "  DATABASE_URL=sqlite:////Users/<you>/.vya-platform/platform.db \\\n"
        "    .venv/bin/python -m alembic upgrade head"
    )
config.set_main_option("sqlalchemy.url", _url)

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER/DROP most constraints in place. Batch mode makes
            # a migration authored once work on both engines.
            render_as_batch=connection.dialect.name == "sqlite",
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
