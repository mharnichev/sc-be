"""Read-only schema gate for API replicas; migration ownership stays external."""
from pathlib import Path
import sys

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool



def require_current_schema(connection, script: ScriptDirectory) -> None:
    expected = set(script.get_heads())
    actual = set(MigrationContext.configure(connection).get_current_heads())
    if len(expected) != 1 or actual != expected:
        raise RuntimeError("Database schema differs from the release head; run the migration owner first")


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    script = ScriptDirectory.from_config(config)
    engine = None
    try:
        from app.core.config import settings
        engine = create_engine(settings.sqlalchemy_sync_database_uri, poolclass=NullPool,
                               connect_args={"connect_timeout": 5,
                                             "options": "-c statement_timeout=5000 -c lock_timeout=5000"})
        with engine.connect() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            require_current_schema(connection, script)
    except Exception:
        # DB driver exceptions may contain a URL/username; logs must stay safe.
        print("Schema check failed: database unavailable or schema is not the release head", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    print("Schema is at the release head")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
