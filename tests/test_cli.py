import csv
import logging

import pytest
import typer
from typer.testing import CliRunner

from corpusforge import cli
from corpusforge.db.session import get_engine, get_session
from corpusforge.logs import LOGGER_ROOTS, get_log_engine
from corpusforge.models import Chunk, Document, GenTask
from corpusforge.settings import Settings, set_settings

runner = CliRunner()
ENV = {"COLUMNS": "250"}  # keep Rich tables on one line


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A throwaway project directory wired in through the settings seam."""
    monkeypatch.setattr("corpusforge.settings.PROJECT_ROOT", tmp_path)
    settings = Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'data' / 'corpus.db'}",
        log_database_url=f"sqlite:///{tmp_path / 'data' / 'logs.db'}", data_dir=tmp_path / "data",
        configs_dir=tmp_path / "configs",
    )
    set_settings(settings)
    get_engine.cache_clear()
    get_log_engine.cache_clear()
    _detach_log_handlers()
    yield tmp_path
    _detach_log_handlers()
    set_settings(None)
    get_engine.cache_clear()
    get_log_engine.cache_clear()


def _detach_log_handlers():
    """The CLI callback attaches DB log handlers to process-global logger trees; keep tests independent."""
    for name in LOGGER_ROOTS:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)


def invoke(*args, **kwargs):
    return runner.invoke(cli.app, list(args), env=ENV, **kwargs)


def test_init_scaffolds_configs_env_directories_and_database(project):
    result = invoke("init")
    assert result.exit_code == 0, result.output

    for name in ("sources.yaml", "generation.yaml", "llm_routes.yaml"):
        assert (project / "configs" / name).exists()
    assert (project / ".env").read_text().startswith("# Copy to .env")
    for sub in ("raw", "images", "export", "review"):
        assert (project / "data" / sub).is_dir()
    assert (project / "data" / "corpus.db").exists()


def test_init_is_idempotent_and_never_overwrites_customisations(project):
    invoke("init")
    (project / "configs" / "sources.yaml").write_text("customised: true\n")
    (project / ".env").write_text("CF_CONTACT_EMAIL=me@example.org\n")

    result = invoke("init")

    assert "already exists, left alone" in result.output
    assert (project / "configs" / "sources.yaml").read_text() == "customised: true\n"
    assert (project / ".env").read_text() == "CF_CONTACT_EMAIL=me@example.org\n"


def test_init_force_restores_the_bundled_defaults(project):
    invoke("init")
    (project / "configs" / "sources.yaml").write_text("customised: true\n")
    invoke("init", "--force")
    assert "scope:" in (project / "configs" / "sources.yaml").read_text()


def test_bundled_templates_are_valid_and_consistent(project):
    import yaml

    invoke("init")
    sources = yaml.safe_load((project / "configs" / "sources.yaml").read_text())
    assert sources["scope"]["must"] and sources["license_allow"] and "openalex" in sources["sources"]
    assert "chunking" in yaml.safe_load((project / "configs" / "generation.yaml").read_text())
    routes = yaml.safe_load((project / "configs" / "llm_routes.yaml").read_text())
    assert {"default", "fast", "judge"} <= set(routes["routes"])


def test_stats_reports_row_counts(project):
    invoke("init")
    with get_session() as s:
        s.add(Document(doc_id="a:1", source="a", external_id="1", title="t", norm_title="t", license="CC-BY"))
    result = invoke("stats")
    assert result.exit_code == 0
    assert "documents" in result.output and "CC-BY" in result.output


def test_blacklist_commands_roundtrip(project):
    invoke("init")
    with get_session() as s:
        s.add(Document(doc_id="bad:1", source="bad", external_id="1", title="Bad paper", norm_title="bad paper",
                       status="chunked"))
        s.flush()
        s.add(Chunk(chunk_id="bad:1#c0", doc_id="bad:1", order=0, tokens=5, text="x"))
        for i in range(3):
            s.add(GenTask(task_type="extract", key=f"extract:bad:1#c{i}", status="failed", last_error="boom"))
        # resolvable failures need a real chunk id
        s.add(GenTask(task_type="extract_facts", key="extract_facts:bad:1#c0", status="failed", last_error="boom"))

    listed = invoke("blacklist-list")
    assert "no documents are blacklisted" in listed.output

    assert "blacklisted 1 document" in invoke("blacklist", "--threshold", "1").output
    assert "bad:1" in invoke("blacklist-list").output
    assert "no documents reached" in invoke("blacklist", "--threshold", "1").output  # idempotent

    assert "removed from blacklist" in invoke("blacklist-remove", "bad:1").output
    assert "was not blacklisted" in invoke("blacklist-remove", "bad:1").output
    missing = invoke("blacklist-remove", "nope:0")
    assert missing.exit_code == 1


def test_fetch_failures_and_pipeline_failures_write_csv(project):
    invoke("init")
    with get_session() as s:
        s.add(Document(doc_id="x:1", source="x", external_id="1", title="Needs manual", norm_title="n",
                       status="accepted", status_reason="fetch:paywalled", doi="10.1/x", relevance=0.9))
    out = project / "review" / "ff.csv"
    result = invoke("fetch-failures", "--output", str(out))
    assert result.exit_code == 0 and "1 documents need manual retrieval" in result.output
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["doc_id"] == "x:1" and rows[0]["reason"] == "fetch:paywalled"

    out2 = project / "review" / "pf.csv"
    result = invoke("pipeline-failures", "--output", str(out2))
    assert result.exit_code == 0 and "0 documents have at least one failed task" in result.output


def test_discover_skips_disabled_sources(project):
    invoke("init")
    import yaml

    path = project / "configs" / "sources.yaml"
    cfg = yaml.safe_load(path.read_text())
    for src in cfg["sources"].values():
        src["enabled"] = False
    path.write_text(yaml.safe_dump(cfg))
    result = invoke("discover", "--source", "openalex")
    assert result.exit_code == 0 and "openalex: disabled in sources.yaml, skipped" in result.output


def test_discover_rejects_unknown_source(project):
    invoke("init")
    assert invoke("discover", "--source", "nope").exit_code != 0


def test_logs_commands_read_the_log_database(project):
    invoke("init")  # the callback attaches the DB log handler
    logging.getLogger("corpusforge.testcomponent").warning("hello from the test")
    out = invoke("logs", "query", "--contains", "hello from the test")
    assert out.exit_code == 0 and "hello from the test" in out.output
    assert "by level" in invoke("logs", "stats").output
    assert "deleted" in invoke("logs", "clear").output


def test_pipeline_commands_can_be_registered_on_a_hosts_own_app():
    host = typer.Typer()
    cli.register_pipeline_commands(host)
    names = {c.name or c.callback.__name__.replace("_", "-") for c in host.registered_commands}
    assert {"discover", "screen", "fetch", "images", "add-local", "fetch-failures", "pipeline-failures",
            "blacklist", "blacklist-list", "blacklist-remove", "parse"} <= {n.replace("_cmd", "") for n in names}
    result = runner.invoke(host, ["screen", "--help"])
    assert result.exit_code == 0 and "license gate" in result.output
