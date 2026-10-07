"""Stage 0 tests: config loading, project root, policy consistency (spec §6, §9.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from slides_cli.gpn.config import load_config, resolve_project_root
from slides_cli.gpn.errors import InputError, SchemaError

VALID = """\
schema_version = "1.0"
default_workflow = "presentation"

[paths]
ontology_dir = "ontology"
references_dir = "slide_examples"

[model]
base_url = "http://127.0.0.1:1234/v1"

[policy]
mode = "preserve_structure"
allow_split = false
"""


def _project(tmp: Path, config_text: str = VALID) -> Path:
    (tmp / "config").mkdir(parents=True)
    (tmp / "config" / "gpn.toml").write_text(config_text, encoding="utf-8")
    (tmp / "ontology").mkdir()
    (tmp / "slide_examples").mkdir()
    return tmp


def test_standard_layout_infers_project_root(tmp_path: Path) -> None:
    root = _project(tmp_path / "proj")
    cfg = load_config(root / "config" / "gpn.toml")
    assert cfg.project_root == root.resolve()
    assert cfg.ontology_dir == root.resolve() / "ontology"
    assert cfg.references_dir == root.resolve() / "slide_examples"
    assert cfg.selected_ontology_file is None


def test_cwd_does_not_affect_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path / "proj")
    monkeypatch.chdir(tmp_path)
    cfg = load_config(root / "config" / "gpn.toml")
    assert cfg.ontology_dir == root.resolve() / "ontology"


def test_nonstandard_config_requires_project_root(tmp_path: Path) -> None:
    conf = tmp_path / "elsewhere" / "gpn.toml"
    conf.parent.mkdir(parents=True)
    conf.write_text(VALID, encoding="utf-8")
    with pytest.raises(InputError) as exc:
        load_config(conf)
    assert exc.value.code == "PROJECT_ROOT_AMBIGUOUS"
    root = _project(tmp_path / "proj2")
    cfg = load_config(conf, project_root=root)
    assert cfg.project_root == root.resolve()


def test_unknown_key_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path / "proj", VALID + "\n[layout]\nbogus_key = 1\n")
    with pytest.raises(SchemaError) as exc:
        load_config(root / "config" / "gpn.toml")
    assert exc.value.code == "UNKNOWN_CONFIG_KEY"


def test_unknown_override_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path / "proj")
    with pytest.raises(SchemaError) as exc:
        load_config(root / "config" / "gpn.toml", {"layout.nope": 1})
    assert exc.value.code == "UNKNOWN_CONFIG_OVERRIDE"


def test_remote_endpoint_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path / "proj", VALID.replace("127.0.0.1:1234", "api.openai.com:443"))
    with pytest.raises(SchemaError) as exc:
        load_config(root / "config" / "gpn.toml")
    assert "loopback" in str(exc.value)


def test_preserve_with_split_rejected(tmp_path: Path) -> None:
    root = _project(
        tmp_path / "proj",
        VALID + "\n[policy]\nmode = 'preserve_structure'\nallow_split = true\n",
    )
    # duplicate [policy] section is invalid TOML -> SchemaError either way
    with pytest.raises(SchemaError):
        load_config(root / "config" / "gpn.toml")
    root2 = _project(
        tmp_path / "proj2",
        VALID.replace("allow_split = false", "allow_split = true"),
    )
    with pytest.raises(SchemaError) as exc:
        load_config(root2 / "config" / "gpn.toml")
    assert "allow_split" in str(exc.value)


def test_concurrency_must_be_one(tmp_path: Path) -> None:
    root = _project(tmp_path / "proj", VALID + "\n[model]\nconcurrency = 2\n")
    with pytest.raises(SchemaError):
        load_config(root / "config" / "gpn.toml")


def test_ontology_file_resolved_inside_ontology_dir(tmp_path: Path) -> None:
    root = _project(
        tmp_path / "proj",
        VALID.replace(
            'ontology_dir = "ontology"',
            'ontology_dir = "ontology"\n'
            'ontology_file = "GPN_Slide_Design_Ontology(4).json"',
        ),
    )
    cfg = load_config(root / "config" / "gpn.toml")
    expected = root.resolve() / "ontology" / "GPN_Slide_Design_Ontology(4).json"
    assert cfg.selected_ontology_file == expected


def test_non_canonical_corpus_dir_rejected(tmp_path: Path) -> None:
    root = _project(
        tmp_path / "proj", VALID.replace('ontology_dir = "ontology"', 'ontology_dir = "rules"')
    )
    with pytest.raises(SchemaError) as exc:
        load_config(root / "config" / "gpn.toml")
    assert exc.value.code == "NON_CANONICAL_ONTOLOGY_DIR"


def test_target_slide_count_zero_means_auto(tmp_path: Path) -> None:
    root = _project(tmp_path / "proj")
    cfg = load_config(root / "config" / "gpn.toml")
    assert cfg.target_slide_count is None


def test_resolve_project_root_explicit_wins(tmp_path: Path) -> None:
    root = _project(tmp_path / "proj")
    conf = root / "config" / "gpn.toml"
    explicit = tmp_path / "other"
    explicit.mkdir()
    assert resolve_project_root(conf, explicit) == explicit.resolve()
