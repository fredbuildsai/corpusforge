import corpusforge


def test_every_name_in___all___is_importable():
    for name in corpusforge.__all__:
        assert hasattr(corpusforge, name), name


def test_version_matches_the_packaged_metadata():
    from importlib.metadata import version

    assert corpusforge.__version__ == version("corpusforge")


def test_the_documented_host_api_is_exported():
    documented = {"ChunkTaskSpec", "run_batch", "run_backlog", "export_cpt", "ExtraCptRow", "set_settings",
                  "migrate", "stamp", "Document", "Chunk", "GenTask", "configure_logging"}
    assert documented <= set(corpusforge.__all__)
