from sqlalchemy import create_engine, inspect, text

from corpusforge.db.session import MIGRATIONS_DIR, VERSION_TABLE, init_db, migrate, stamp
from corpusforge.models import Base


def test_migrate_creates_every_model_table_and_is_idempotent(tmp_path):
    url = f"sqlite:///{tmp_path / 'nested' / 'migrated.db'}"

    migrate(url)
    migrate(url)  # second run must be a no-op, not a "table already exists" failure

    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert set(Base.metadata.tables) <= tables
    with engine.connect() as conn:
        assert conn.execute(text(f"SELECT count(*) FROM {VERSION_TABLE}")).scalar() == 1


def test_migrated_schema_matches_the_models_column_for_column(tmp_path):
    """The baseline migration must not drift from `models.py` (a model change needs a new revision)."""
    migrated = create_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    created = create_engine(f"sqlite:///{tmp_path / 'created.db'}")
    migrate(str(migrated.url))
    init_db(created)
    for table in Base.metadata.tables:
        want = {c["name"]: str(c["type"]) for c in inspect(created).get_columns(table)}
        got = {c["name"]: str(c["type"]) for c in inspect(migrated).get_columns(table)}
        assert got == want, table


def test_migrations_ship_inside_the_package():
    assert MIGRATIONS_DIR.is_dir() and (MIGRATIONS_DIR / "env.py").exists()
    assert list((MIGRATIONS_DIR / "versions").glob("*.py"))


def test_uses_its_own_version_table_so_a_host_can_run_its_own_alembic_history(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    migrate(str(engine.url))
    assert VERSION_TABLE != "alembic_version"
    assert "alembic_version" not in inspect(engine).get_table_names()


def test_stamp_adopts_an_existing_schema_without_running_ddl(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'existing.db'}")
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO releases (version, filters, counts, content_hash, created_at) "
                          "VALUES ('v1', '{}', '{}', 'h', '2026-01-01')"))

    stamp(str(engine.url))
    migrate(str(engine.url))  # already at head: must not recreate/alter anything

    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM releases")).scalar() == 1
        assert conn.execute(text(f"SELECT count(*) FROM {VERSION_TABLE}")).scalar() == 1
