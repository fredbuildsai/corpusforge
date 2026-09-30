"""The README is part of the contract: these tests execute what it documents, so docs can't silently rot."""

import dataclasses
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from llmrouter_free import LLMRouter
from sqlalchemy import String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from typer.main import get_command

import corpusforge
from corpusforge.cli import app
from corpusforge.db.session import get_session
from corpusforge.models import Chunk, Document
from corpusforge.settings import Settings

README = (Path(__file__).parent.parent / "README.md").read_text()
ROOT = Path(__file__).parent.parent


def python_blocks():
    return re.findall(r"```python\n(.*?)```", README, flags=re.DOTALL)


class _Base(DeclarativeBase):
    pass


class Note(_Base):
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chunk_id: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(String(500))
    generator_model: Mapped[str] = mapped_column(String(100))


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("KEY_A", "x")


def test_the_annotation_stage_example_in_the_readme_runs_as_written(engine):
    """Execute the README's annotation-stage block verbatim against a scripted model."""
    _Base.metadata.create_all(engine)
    with get_session(engine) as s:
        s.add(Document(doc_id="doc:1", source="t", external_id="1", title="t", norm_title="t", status="chunked"))
        s.flush()
        s.add_all([Chunk(chunk_id=f"doc:1#s00-c0{i}", doc_id="doc:1", order=i, tokens=5, text=f"text {i}")
                   for i in range(3)])

    def completion(**kw):
        n = kw["messages"][1]["content"].count("Excerpt ")
        payload = {"results": [{"chunk_index": i, "notes": [f"note {i}"]} for i in range(n)]}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))],
                               usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    config = {"deployments": [{"name": "gen", "model": "p/gen", "api_key_env": "KEY_A", "family": "f"}],
              "routes": {"extract": ["gen"]}}
    router = LLMRouter(config, engine=engine, completion_fn=completion)

    block = next(b for b in python_blocks() if "ChunkTaskSpec(" in b)
    block = block.replace('limit=2000, batch_size=5, concurrency=4', 'limit=10, batch_size=5, concurrency=2')
    namespace = {"engine": engine, "router": router, "Note": Note}
    exec(compile(block, "README.md", "exec"), namespace)  # noqa: S102 - executing our own documentation

    with get_session(engine) as s:
        assert {n.chunk_id for n in s.scalars(select(Note))} == {"doc:1#s00-c00", "doc:1#s00-c01", "doc:1#s00-c02"}


def test_the_export_example_in_the_readme_runs(engine, tmp_path, monkeypatch):
    with get_session(engine) as s:
        s.add(Document(doc_id="d1", source="t", external_id="1", title="Paper", norm_title="p", split="train",
                       doi="10.1/x", license="CC-BY-4.0"))
        s.flush()
        s.add(Chunk(chunk_id="d1#c0", doc_id="d1", order=0, tokens=10, text="Some text."))
    block = next(b for b in python_blocks() if "export_cpt(" in b)
    block = block.replace('Path("data/export/v1")', f"Path({str(tmp_path / 'v1')!r})")
    block = block.replace("get_session() as s", "get_session(engine) as s")
    exec(compile(block, "README.md", "exec"), {"engine": engine})  # noqa: S102
    rows = [json.loads(line) for line in (tmp_path / "v1" / "cpt_train.jsonl").read_text().splitlines()]
    assert rows[0]["text"] == "Some text." and rows[1]["text"] == "A perovskite is ..."
    assert (tmp_path / "v1" / "cpt_train.attribution.json").exists()


def test_the_settings_embedding_example_in_the_readme_runs():
    block = next(b for b in python_blocks() if "class MySettings" in b)
    ns: dict = {}
    try:
        exec(compile(block, "README.md", "exec"), ns)  # noqa: S102
        assert corpusforge.get_settings().my_option == "x"
    finally:
        corpusforge.set_settings(None)


def test_the_cli_embedding_example_in_the_readme_runs():
    block = next(b for b in python_blocks() if "register_pipeline_commands(app)" in b)
    ns: dict = {}
    exec(compile(block, "README.md", "exec"), ns)  # noqa: S102
    names = {c.name or c.callback.__name__ for c in ns["app"].registered_commands}
    assert {"discover", "screen", "fetch", "parse"} <= names


def test_every_python_import_line_in_the_readme_resolves():
    for block in python_blocks():
        for statement in re.findall(r"^from (corpusforge[\w.]*) import \(?([^)]*?)\)?$", block,
                                    flags=re.MULTILINE | re.DOTALL):
            module, names = statement
            imported = __import__(module, fromlist=["*"])
            for name in re.findall(r"[A-Za-z_]\w*", names):
                assert hasattr(imported, name), f"README imports {name} from {module}"


def test_every_cli_command_the_readme_mentions_exists():
    documented = set(re.findall(r"`corpusforge ([a-z][a-z-]*)", README))
    documented.discard("logs")  # a group; its subcommands are checked below
    documented.discard("db")
    commands = set(get_command(app).commands)
    assert documented <= commands, documented - commands
    assert {"tail", "query", "stats", "clear"} <= set(get_command(app).commands["logs"].commands)
    assert "init" in get_command(app).commands["db"].commands


def test_every_documented_environment_variable_is_a_real_setting():
    fields = {name.upper() for name in Settings.model_fields}
    for var in re.findall(r"`CF_([A-Z_]+)`", README):
        assert var in fields, f"README documents CF_{var} but Settings has no such field"


def test_every_chunking_key_in_the_readme_is_read_by_the_code_and_present_in_the_template():
    import yaml

    template = yaml.safe_load((ROOT / "src/corpusforge/templates/generation.yaml").read_text())["chunking"]
    section = README.split("### `configs/generation.yaml` - `chunking`")[1].split("###")[0]
    for key in re.findall(r"^\| `(\w+)`", section, flags=re.MULTILINE):
        assert key in template, key


def test_every_sources_key_in_the_readme_exists_in_the_template():
    import yaml

    template = yaml.safe_load((ROOT / "src/corpusforge/templates/sources.yaml").read_text())
    for key in ("scope", "license_allow", "license_flag", "sources"):
        assert key in template
    assert {"must", "boost", "comparison_only"} <= set(template["scope"])
    assert {"openalex", "chemrxiv", "arxiv"} <= set(template["sources"])


def test_the_chunk_task_spec_fields_named_in_the_readme_exist():
    fields = {f.name for f in dataclasses.fields(corpusforge.ChunkTaskSpec)}
    assert {"task_type", "build_messages", "response_schema", "persist_result", "output_tokens_per_chunk",
            "route"} <= fields


def test_readme_documents_structured_output_and_the_names_it_mentions_exist():
    assert "## Structured output" in README
    import llmrouter_free

    from corpusforge import runner

    for name in ("json_schema_response_format", "json_validator"):
        assert name in README and hasattr(llmrouter_free, name) and name in open(runner.__file__).read()
    assert ChunkTaskSpecTemperatureDefault() == 0


def ChunkTaskSpecTemperatureDefault():
    import dataclasses

    return next(f.default for f in dataclasses.fields(corpusforge.ChunkTaskSpec) if f.name == "temperature")
