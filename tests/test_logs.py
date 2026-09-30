import logging

import pytest

from corpusforge.logs import (
    LOGGER_ROOTS,
    DBLogHandler,
    clear_logs,
    configure_logging,
    get_log_engine,
    get_log_session,
    init_log_db,
    log_stats,
    query_logs,
)


@pytest.fixture
def log_engine(tmp_path):
    engine = get_log_engine(f"sqlite:///{tmp_path / 'logs.db'}")
    init_log_db(engine)
    yield engine
    get_log_engine.cache_clear()


@pytest.fixture
def logger(log_engine):
    log = logging.getLogger("test.logs")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    handler = DBLogHandler(log_engine)
    log.addHandler(handler)
    yield log
    log.removeHandler(handler)


def test_emitted_record_is_persisted_with_level_component_message_and_context(logger, log_engine):
    logger.warning("chunk failed: %s", "c1", extra={"context": {"chunk_id": "c1", "attempts": 2}})

    with get_log_session(log_engine) as s:
        entries = query_logs(s, limit=10)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.level == "WARNING" and entry.component == "test.logs"
    assert entry.message == "chunk failed: c1"
    assert entry.context == {"chunk_id": "c1", "attempts": 2}


def test_record_without_context_stores_an_empty_dict(logger, log_engine):
    logger.info("plain message")
    with get_log_session(log_engine) as s:
        entries = query_logs(s, limit=10)
    assert entries[0].context == {}


def test_query_logs_filters_by_level_component_and_contains(logger, log_engine):
    logger.info("alpha message")
    logger.error("beta failure")
    logger.warning("gamma alpha thing")

    with get_log_session(log_engine) as s:
        assert len(query_logs(s, level="ERROR")) == 1
        assert len(query_logs(s, contains="alpha")) == 2
        assert len(query_logs(s, level="WARNING", contains="alpha")) == 1
        assert len(query_logs(s, component="test.logs")) == 3
        assert len(query_logs(s, component="nonexistent")) == 0


def test_query_logs_after_id_returns_ascending_for_tail_follow(logger, log_engine):
    logger.info("one")
    logger.info("two")
    with get_log_session(log_engine) as s:
        first = query_logs(s, limit=10)[-1]  # oldest of the desc-ordered results = "one"
        logger.info("three")
        newer = query_logs(s, after_id=first.id)
    assert [e.message for e in newer] == ["two", "three"]


def test_log_stats_groups_by_level_and_component(logger, log_engine):
    logger.info("a")
    logger.info("b")
    logger.error("c")
    with get_log_session(log_engine) as s:
        stats = log_stats(s)
    assert stats["by_level"] == {"INFO": 2, "ERROR": 1}
    assert stats["by_component"] == {"test.logs": 3}


def test_clear_logs_removes_everything_by_default(logger, log_engine):
    logger.info("a")
    logger.info("b")
    with get_log_session(log_engine) as s:
        n = clear_logs(s)
    assert n == 2
    with get_log_session(log_engine) as s:
        assert query_logs(s, limit=10) == []


def test_clear_logs_respects_older_than(logger, log_engine):
    from datetime import datetime, timedelta, timezone

    logger.info("old one")
    future_cutoff = datetime.now(timezone.utc) + timedelta(days=1)
    with get_log_session(log_engine) as s:
        n = clear_logs(s, older_than=future_cutoff)
    assert n == 1


@pytest.fixture
def clean_trees():
    """configure_logging mutates process-global logger trees: start and finish every test with none attached."""
    def reset():
        for name in LOGGER_ROOTS:
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                lg.removeHandler(h)
    reset()
    yield
    reset()


def test_configure_logging_is_idempotent(log_engine, clean_trees):
    h1 = configure_logging(engine=log_engine)
    h2 = configure_logging(engine=log_engine)
    assert h1 is h2
    for name in LOGGER_ROOTS:
        handlers = logging.getLogger(name).handlers
        assert sum(isinstance(h, DBLogHandler) for h in handlers) == 1
        # console handler added once too, not duplicated (exact type: pytest's capture handlers subclass it)
        assert sum(type(h) is logging.StreamHandler for h in handlers) == 1


def test_configure_logging_covers_every_tree_including_a_hosts_own(log_engine, clean_trees):
    try:
        configure_logging(engine=log_engine, roots=(*LOGGER_ROOTS, "myproject"))
        logging.getLogger("myproject.stage").info("from the host")
        logging.getLogger("llmrouter_free.router").info("from the router")
        with get_log_session(log_engine) as s:
            components = {e.component for e in query_logs(s, limit=10)}
        assert {"myproject.stage", "llmrouter_free.router"} <= components
    finally:
        logging.getLogger("myproject").handlers.clear()


def test_configure_logging_disables_propagation_to_the_stdlib_root_logger(log_engine, clean_trees):
    configure_logging(engine=log_engine)
    assert all(logging.getLogger(name).propagate is False for name in LOGGER_ROOTS)


def test_configure_logging_console_handler_only_shows_warning_and_above(log_engine, clean_trees, capsys):
    configure_logging(engine=log_engine)
    logging.getLogger("corpusforge.somemodule").info("routine progress, DB only")
    logging.getLogger("corpusforge.somemodule").warning("worth seeing live")
    err = capsys.readouterr().err
    assert "routine progress" not in err
    assert "worth seeing live" in err


def test_handler_failure_does_not_raise(logger, log_engine, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("db is gone")

    monkeypatch.setattr("corpusforge.logs.get_log_session", boom)
    monkeypatch.setattr(logging.Handler, "handleError", lambda self, record: None)
    logger.info("this must not raise even though persistence fails")  # no exception propagates
