from pathlib import Path

from corpusforge.settings import PACKAGE_DIR, PROJECT_ROOT, TEMPLATES_DIR, Settings, get_settings, set_settings


def test_package_assets_are_located_relative_to_the_installed_package_not_the_cwd():
    """Templates and migrations ship inside the package (so they exist in a `pip install`), while PROJECT_ROOT
    (configs/data/.env) is CWD-based, so `corpusforge init` can scaffold a fresh project directory anywhere."""
    assert TEMPLATES_DIR.parent == PACKAGE_DIR
    assert (PACKAGE_DIR / "migrations" / "env.py").exists()
    assert isinstance(PROJECT_ROOT, Path)


def test_relative_sqlite_url_and_dirs_resolve_against_project_root():
    settings = Settings(_env_file=None, database_url="sqlite:///data/x.db", data_dir="data", configs_dir="configs")
    assert settings.database_url == f"sqlite:///{PROJECT_ROOT / 'data' / 'x.db'}"
    assert settings.data_dir == PROJECT_ROOT / "data"
    assert settings.configs_dir == PROJECT_ROOT / "configs"


def test_absolute_and_non_sqlite_urls_are_untouched():
    assert Settings(_env_file=None, database_url="sqlite:////tmp/a.db").database_url == "sqlite:////tmp/a.db"
    url = "postgresql+psycopg://u:p@host/db"
    assert Settings(_env_file=None, database_url=url).database_url == url
    assert Settings(_env_file=None, data_dir=Path("/tmp/cf")).data_dir == Path("/tmp/cf")


def test_set_settings_lets_a_host_application_inject_its_own_settings():
    class HostSettings(Settings):
        model_config = {**Settings.model_config, "env_prefix": "HOST_"}

    host = HostSettings(_env_file=None, app_name="myproject", contact_email="me@example.org")
    try:
        set_settings(host)
        assert get_settings() is host
    finally:
        set_settings(None)
    assert get_settings() is not host
    assert get_settings().app_name == "corpusforge"


def test_cf_prefixed_environment_variables_configure_the_default_settings(monkeypatch):
    monkeypatch.setenv("CF_CONTACT_EMAIL", "who@example.org")
    assert Settings(_env_file=None).contact_email == "who@example.org"
