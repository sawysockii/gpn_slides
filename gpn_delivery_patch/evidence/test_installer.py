"""Safety tests for the real installer, not a fake git reset workflow."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("delivery_installer",Path(__file__).parents[1]/"APPLY_PATCH.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def package(tmp_path,files):
    package,repo = tmp_path/"package",tmp_path/"repo"
    entries = []
    for name,(before,after) in files.items():
        p = package/"payload"/name
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_bytes(after)
        if before is not None:
            b = package/"baseline"/name
            b.parent.mkdir(parents=True,exist_ok=True)
            b.write_bytes(before)
            target = repo/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(before)
        entries.append({"path":name,"base_sha256":installer.sha(before) if before else None,
                        "new_sha256":installer.sha(after)})
    (package/"patch_manifest.json").write_text(json.dumps({"files":entries}))
    repo.mkdir(exist_ok=True)
    return package,repo


def test_preflight_never_changes_code(tmp_path):
    p,r = package(tmp_path,{"src/a.py":(b"old\n",b"new\n")})
    assert installer.apply_patch(p,r)["status"] == "preflight_ok"
    assert (r/"src/a.py").read_bytes() == b"old\n"


def test_apply_keeps_dirty_unrelated_file_and_backs_up(tmp_path):
    p,r = package(tmp_path,{"src/a.py":(b"old\n",b"new\n")})
    (r/"user_work.txt").write_bytes(b"uncommitted")
    result = installer.apply_patch(p,r,apply=True)
    assert (r/"src/a.py").read_bytes() == b"new\n"
    assert (r/"user_work.txt").read_bytes() == b"uncommitted"
    assert (Path(result["backup"])/"src/a.py").read_bytes() == b"old\n"
    assert installer.apply_patch(p,r,apply=True)["changed_files"] == 0


def test_diverged_code_is_merged_only_by_unique_exact_context(tmp_path):
    base = b"intro\nunique function\nreturn old\nend\n"
    desired = base.replace(b"return old",b"return new")
    current = base+b"user_function()\n"
    assert installer.merge_exact_context(base,desired,current) == desired+b"user_function()\n"
    assert installer.contains_delta(base,desired,desired+b"user_function()\n")
    p,r = package(tmp_path,{"src/a.py":(base,desired)})
    (r/"src/a.py").write_bytes(current)
    assert installer.apply_patch(p,r,apply=True)["changed_files"] == 1
    assert installer.apply_patch(p,r,apply=True)["changed_files"] == 0


def test_one_conflict_stops_every_project_write(tmp_path):
    p,r = package(tmp_path,{"src/a.py":(b"old\n",b"new\n"),
                          "src/b.py":(b"old second\n",b"new second\n")})
    (r/"src/b.py").write_bytes(b"different local implementation\n")
    result = installer.apply_patch(p,r,apply=True)
    assert result["status"] == "conflicts"
    assert result["applied"] is False
    assert (r/"src/a.py").read_bytes() == b"old\n"
    assert (r/"src/b.py").read_bytes() == b"different local implementation\n"


def test_write_failure_rolls_back_prior_files(tmp_path,monkeypatch):
    p,r = package(tmp_path,{"src/a.py":(b"a\n",b"A\n"),"src/b.py":(b"b\n",b"B\n")})
    real = installer.atomic_write
    def failing(path,data):
        if path == r/"src/b.py" and data == b"B\n":
            raise OSError("injected write failure")
        return real(path,data)
    monkeypatch.setattr(installer,"atomic_write",failing)
    with pytest.raises(OSError,match="injected"):
        installer.apply_patch(p,r,apply=True)
    assert (r/"src/a.py").read_bytes() == b"a\n"
    assert (r/"src/b.py").read_bytes() == b"b\n"


def test_source_corpus_path_is_forbidden(tmp_path):
    p,r = package(tmp_path,{"ontology/rules.json":(b"{}",b"[]")})
    with pytest.raises(ValueError,match="PAYLOAD_PATH_FORBIDDEN"):
        installer.apply_patch(p,r,apply=True)
    assert (r/"ontology/rules.json").read_bytes() == b"{}"


def test_symlink_escape_is_refused(tmp_path):
    root = tmp_path/"root"
    root.mkdir()
    outside = tmp_path/"outside"
    outside.mkdir()
    (root/"src").symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError,match="PATH_OUTSIDE_ROOT"):
        installer.confined(root,"src/a.py")
