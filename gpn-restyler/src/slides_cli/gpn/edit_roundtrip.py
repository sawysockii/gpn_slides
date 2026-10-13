"""Stage 7 §12.3: reusable native edit roundtrip verifier.

On a COPY of an accepted candidate: edit one native text run and the
geometry of one native object, save, reopen, and verify the ACTUAL
changed text + geometry values after reopen. Unchanged media/embeddings
and chart workbook payload bytes must keep identical SHA256; only the
edited spans/boxes plus necessary package metadata may change.
PowerPoint UI editing stays a separate unverified scope.
"""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path

from pydantic import BaseModel, Field

from .models import StrictModel


class RoundtripReport(BaseModel):
    model_config = StrictModel

    schema_version: str = "native-edit-roundtrip/v2"
    candidate_sha256: str = ""
    roundtrip_copy: str = ""
    edited_slide: int = 1
    edited_text_expected: str = ""
    edited_text_after_reopen: str = ""
    geometry_delta_emu: int = 0
    geometry_after_reopen_emu: tuple[int, int, int, int] = (0, 0, 0, 0)
    geometry_match: bool = False
    unchanged_media_sha256: dict[str, str] = Field(default_factory=dict)
    media_bytes_stable: bool = False
    workbook_bytes_stable: bool = False
    slides_after_reopen: int = 0
    package_graph_ok: bool = False
    powerpoint_ui_edit_verified: bool = False


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _payload_hashes(package: Path, prefixes: tuple[str, ...]
                    ) -> dict[str, str]:
    out: dict[str, str] = {}
    with zipfile.ZipFile(package) as zf:
        for name in zf.namelist():
            if name.startswith(prefixes):
                out[name] = _sha(zf.read(name))
    return out


def verify_edit_roundtrip(candidate: Path, work_dir: Path,
                          text_suffix: str = " [rt]",
                          geometry_nudge_emu: int = 12700
                          ) -> RoundtripReport:
    """Edit a copy, save/reopen, verify changed values + stable bytes."""
    from pptx import Presentation  # noqa: PLC0415

    candidate = Path(candidate)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    digest = _sha(candidate.read_bytes())
    copy_path = work_dir / "edit_roundtrip_copy.pptx"
    shutil.copyfile(candidate, copy_path)

    before_media = _payload_hashes(
        candidate, ("ppt/media/", "ppt/embeddings/"))
    before_workbooks = _payload_hashes(candidate, ("ppt/charts/",
                                                   "ppt/embeddings/"))

    prs = Presentation(str(copy_path))
    slide = prs.slides[0]
    edited_shape = None
    new_text = ""
    for shape in slide.shapes:
        if shape.has_text_frame and shape.text.strip():
            run = shape.text_frame.paragraphs[0].runs[0]
            run.text = run.text + text_suffix
            new_text = run.text
            edited_shape = shape
            break
    if edited_shape is None:
        raise ValueError("no editable text run on slide 1")
    left = edited_shape.left.emu if hasattr(edited_shape.left, "emu") \
        else int(edited_shape.left)
    top = edited_shape.top.emu if hasattr(edited_shape.top, "emu") \
        else int(edited_shape.top)
    width = edited_shape.width.emu if hasattr(edited_shape.width, "emu") \
        else int(edited_shape.width)
    height = edited_shape.height.emu if hasattr(edited_shape.height, "emu") \
        else int(edited_shape.height)
    edited_shape.left = left + geometry_nudge_emu
    edited_shape.top = top + geometry_nudge_emu
    expected_box = (left + geometry_nudge_emu, top + geometry_nudge_emu,
                    width, height)
    prs.save(str(copy_path))

    reopened = Presentation(str(copy_path))
    after_text = ""
    after_box = (0, 0, 0, 0)
    for shape in reopened.slides[0].shapes:
        if shape.has_text_frame and text_suffix in shape.text:
            after_text = shape.text_frame.paragraphs[0].runs[0].text
            after_box = (int(shape.left), int(shape.top),
                         int(shape.width), int(shape.height))
            break

    after_media = _payload_hashes(
        copy_path, ("ppt/media/", "ppt/embeddings/"))
    after_workbooks = _payload_hashes(copy_path, ("ppt/charts/",
                                                  "ppt/embeddings/"))
    media_stable = before_media == after_media and bool(before_media)
    workbooks_stable = before_workbooks == after_workbooks

    with zipfile.ZipFile(copy_path) as zf:
        names = zf.namelist()
        graph_ok = "ppt/presentation.xml" in names and any(
            n.startswith("ppt/slides/slide") for n in names)

    return RoundtripReport(
        candidate_sha256=digest, roundtrip_copy=str(copy_path),
        edited_text_expected=new_text,
        edited_text_after_reopen=after_text,
        geometry_delta_emu=geometry_nudge_emu,
        geometry_after_reopen_emu=after_box,
        geometry_match=tuple(after_box) == tuple(expected_box),
        unchanged_media_sha256=after_media,
        media_bytes_stable=media_stable,
        workbook_bytes_stable=workbooks_stable,
        slides_after_reopen=len(reopened.slides),
        package_graph_ok=graph_ok,
        powerpoint_ui_edit_verified=False)
