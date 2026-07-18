import os
import logging
from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SQLITE_PATH = PROJECT_ROOT / "test_automation_new.db"

# Default to SQLite for local development since Docker wasn't available
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{DEFAULT_SQLITE_PATH.as_posix()}"
)

if DATABASE_URL.startswith("sqlite:///") and not DATABASE_URL.startswith("sqlite:////"):
    relative_path = DATABASE_URL.removeprefix("sqlite:///")
    DATABASE_URL = f"sqlite:///{(PROJECT_ROOT / relative_path).as_posix()}"

# For SQLite fallback during early migration tests if Postgres fails
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def _sqla_type_to_ddl(column) -> str:
    """Best-effort SQL type for an ADD COLUMN statement."""
    try:
        return column.type.compile(dialect=engine.dialect)
    except Exception:
        return "TEXT"


def _add_missing_columns():
    """Add columns that exist on the models but not yet in the physical table.

    ``create_all`` only creates missing *tables* — it never alters an existing
    one. Projects registered before the repository-management fields were added
    would otherwise break with "no such column". This performs the additive
    part of a migration (nullable / defaulted columns only), which is all the
    schema evolution here needs.
    """
    from automation.database import models  # noqa: F401

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all already handled it

            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_cols or column.primary_key:
                    continue

                ddl_type = _sqla_type_to_ddl(column)
                stmt = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}'

                # Carry over a simple scalar default so existing rows are sane.
                default = getattr(column.default, "arg", None) if column.default else None
                if isinstance(default, (str, int, float, bool)):
                    literal = f"'{default}'" if isinstance(default, str) else str(default)
                    stmt += f" DEFAULT {literal}"

                try:
                    conn.execute(text(stmt))
                    logger.info(f"Migration: added column {table.name}.{column.name}")
                except Exception as e:
                    logger.warning(f"Migration: could not add {table.name}.{column.name}: {e}")


def initialize_database():
    from automation.database import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
