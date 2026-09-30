import logging

from corpusforge.db.session import migrate


def test_migrate_does_not_leave_the_corpusforge_logger_tree_disabled(tmp_path):
    """Regression test: a host's Alembic env applying `logging.config.fileConfig` defaults to
    disable_existing_loggers=True, which silently disables every already-created logger (they are created at
    module-import time, before migrate() runs), turning every logger.info()/.warning() into a no-op with no
    exception anywhere. migrate() must leave the corpusforge and llmrouter_free trees enabled."""
    logger = logging.getLogger("corpusforge.annotate.tasks")
    logger.disabled = True  # simulate what fileConfig would have done

    migrate(f"sqlite:///{tmp_path / 'regression.db'}")

    assert logger.disabled is False
    assert logging.getLogger("llmrouter_free").disabled is False
