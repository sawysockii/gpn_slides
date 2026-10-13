"""Stage 7 §12.3: native edit roundtrip proves changed values + bytes."""

from __future__ import annotations

from pathlib import Path

from slides_cli.gpn.edit_roundtrip import verify_edit_roundtrip

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CANDIDATE = (PROJECT_ROOT / "runs" / "stage6-layout-repair"
             / "2026-10-12-140949" / "candidate.pptx")


def test_roundtrip_proves_text_and_geometry_after_reopen(tmp_path):
    if not CANDIDATE.is_file():
        import pytest
        pytest.skip("previous run candidate not present")
    report = verify_edit_roundtrip(CANDIDATE, tmp_path)
    assert report.candidate_sha256 == \
        "162b9684274225291d5aefe99a72e3a398dcb2a5eb941c3bedf935efc56c839e"
    assert report.edited_text_after_reopen == \
        report.edited_text_expected
    assert report.geometry_match
    assert report.geometry_after_reopen_emu[0] > 0
    assert report.slides_after_reopen == 8


def test_roundtrip_proves_media_bytes_stable(tmp_path):
    if not CANDIDATE.is_file():
        import pytest
        pytest.skip("previous run candidate not present")
    report = verify_edit_roundtrip(CANDIDATE, tmp_path)
    assert report.media_bytes_stable
    assert len(report.unchanged_media_sha256) >= 1
    assert all(len(v) == 64 for v in
               report.unchanged_media_sha256.values())
    assert report.package_graph_ok
    assert report.powerpoint_ui_edit_verified is False
