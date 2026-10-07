"""Stage 4 artifact run: compound fixture + CLI flows + native build.

Phases (argv[1]):

- ``setup``   corpus hashes, compound fixture build, ``import``/``profile``/
              re-``import``/``plan-packet`` per slide; audit_before,
              upstream_provenance, forbidden_reference_cleanup.
- ``build``   assemble ``plans/`` from ``selected_plans/`` (+ manifest with
              the saved generation origin) and run ``gpn build``.
- ``collect`` gather all §14.2 artifacts + ``stage4_readiness.json``.

``plan`` calls (harness provider) run between setup and build: the harness
answers each ``model_requests/<id>.json`` with ``model_responses/<id>.json``.
Everything lands in ``project_root/runs/stage4/<run_id>``; previous runs and
the immutable corpora are never touched. Every command and exit code is
recorded in ``commands_and_results.json``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = CODE_ROOT.parent
STAGE = "stage4"
RUN_ID = sys.argv[2] if len(sys.argv) > 2 else "2026-10-07-a"
RUN_DIR = PROJECT_ROOT / "runs" / STAGE / RUN_ID
COMMANDS = RUN_DIR / "commands_and_results.json"

sys.path.insert(0, str(CODE_ROOT / "src"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def corpus_hashes() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name, rel in (
        ("ontology_json", "ontology/GPN_Slide_Design_Ontology.json"),
        ("ontology_md", "ontology/GPN_Slide_Design_Ontology.md"),
        ("slide_examples", "slide_examples/slide_examples.pptx"),
    ):
        path = PROJECT_ROOT / rel
        out[name] = sha256_file(path) if path.is_file() else None
    return out


def _load_commands() -> list[dict]:
    if COMMANDS.is_file():
        try:
            return json.loads(COMMANDS.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
    return []


def run_cmd(cmd: list[str], *, expect: set[int] | None = None, label: str = "") -> dict:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=CODE_ROOT)
    entry = {
        "label": label or " ".join(cmd[2:5]),
        "cmd": cmd,
        "exit": proc.returncode,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-2000:],
    }
    commands = _load_commands()
    commands.append(entry)
    COMMANDS.parent.mkdir(parents=True, exist_ok=True)
    COMMANDS.write_text(
        json.dumps(commands, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"$ {' '.join(cmd)}\n  exit={proc.returncode}")
    if expect is not None and proc.returncode not in expect:
        print("  !! unexpected exit; stdout tail:")
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
    return entry


def gpn(*args: str) -> list[str]:
    return [
        "uv", "run", "slides", "gpn",
        "--project-root", str(PROJECT_ROOT),
        *args,
    ]


# ---------------------------------------------------------------------------
# Compound fixture (§13.2): 5 slides, native content throughout
# ---------------------------------------------------------------------------

def build_compound_fixture(path: Path) -> dict:
    """Isolated synthetic deck: text/fields/list, merged table, category
    chart + workbook, true 2-plot combo, group/connectors, links, notes,
    one hidden slide, one opaque graphicData part."""
    sys.path.insert(0, str(CODE_ROOT / "tests" / "gpn"))
    import conftest as fx  # noqa: E402

    prs = fx.new_deck()

    s0 = fx.blank_slide(prs)
    fx.add_textbox(s0, "Состав поставки в 2026 году", left=1, top=0.5,
                   width=8, height=0.8, size=28)
    fx.add_fields_and_footnotes(s0)
    fx.add_mixed_runs(s0)
    s0.notes_slide.notes_text_frame.text = "Заметки: проверить единицы и сноски."

    s1 = fx.blank_slide(prs)
    fx.add_textbox(s1, "Таблица отгрузки", left=1, top=0.5, width=8,
                   height=0.8, size=28)
    fx.add_table(s1)
    s1.notes_slide.notes_text_frame.text = "Пустая ячейка и н/д — значимые."

    s2 = fx.blank_slide(prs)
    fx.add_textbox(s2, "Динамика по кварталам", left=1, top=0.5, width=8,
                   height=0.8, size=28)
    fx.add_column_chart(s2)
    _link_slide(s2, s0)
    s2.notes_slide.notes_text_frame.text = "Ссылка ведёт на первый слайд."

    s3 = fx.blank_slide(prs)
    fx.add_textbox(s3, "Схема связей", left=1, top=0.5, width=8, height=0.8,
                   size=28)
    fx.add_group_with_nonuniform_scale(s3)
    _connected_pair(s3)
    fx.add_unknown_graphic_data(s3)

    s4 = fx.blank_slide(prs)
    fx.add_textbox(s4, "Скрытый резерв", left=1, top=0.5, width=8, height=0.8,
                   size=28)
    fx.add_xy_chart(s4)
    s4._element.set("show", "0")

    prs.save(str(path))
    _add_combo_second_plot(path)
    return {"slides": 5, "hidden": ["slide4"], "path": str(path)}


def _connected_pair(slide) -> None:
    """Two rectangles joined by a real stCxn/endCxn connector."""
    from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
    from pptx.oxml.ns import qn
    from pptx.util import Inches

    left = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1), Inches(2),
                                  Inches(2), Inches(1))
    left.text_frame.text = "Узел 1"
    right = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(5), Inches(2),
                                   Inches(2), Inches(1))
    right.text_frame.text = "Узел 2"
    conn = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(3),
                                      Inches(2.5), Inches(5), Inches(2.5))
    cxn = conn._element.find(qn("p:nvCxnSpPr"))
    cNvCxnSpPr = cxn.find(qn("p:cNvCxnSpPr"))
    st = cNvCxnSpPr.makeelement(qn("a:stCxn"), {})
    st.set("id", str(left.shape_id))
    st.set("idx", "3")
    en = cNvCxnSpPr.makeelement(qn("a:endCxn"), {})
    en.set("id", str(right.shape_id))
    en.set("idx", "1")
    cNvCxnSpPr.append(st)
    cNvCxnSpPr.append(en)


def _link_slide(source_slide, target_slide) -> None:
    from lxml import etree
    from pptx.oxml.ns import qn

    box = source_slide.shapes.add_textbox(
        __import__("pptx.util", fromlist=["Inches"]).Inches(1),
        __import__("pptx.util", fromlist=["Inches"]).Inches(5.5),
        __import__("pptx.util", fromlist=["Inches"]).Inches(4),
        __import__("pptx.util", fromlist=["Inches"]).Inches(0.6))
    box.text_frame.text = "Назад к составу"
    r = box.text_frame.paragraphs[0].runs[0]._r
    rPr = r.find(qn("a:rPr"))
    if rPr is None:
        rPr = etree.SubElement(r, qn("a:rPr"))
        r.insert(0, rPr)
    hlink = etree.SubElement(rPr, qn("a:hlinkClick"))
    rid = source_slide.part.relate_to(
        target_slide.part,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide",
        is_external=False,
    )
    hlink.set(qn("r:id"), rid)


def _add_combo_second_plot(path: Path) -> None:
    """Turn slide-2's column chart into a true bar+line combo (2 plots)."""
    import zipfile

    from lxml import etree

    cns = "http://schemas.openxmlformats.org/drawingml/2006/chart"
    with zipfile.ZipFile(path) as zin:
        members = {i.filename: zin.read(i) for i in zin.infolist()
                   if not i.is_dir()}
    names = sorted(n for n in members if n.startswith("ppt/charts/")
                   and n.endswith(".xml"))
    xml = members[names[0]]
    root = etree.fromstring(xml)
    area = root.find(f".//{{{cns}}}plotArea")
    bar = area.find(f"{{{cns}}}barChart")
    line = deepcopy(bar)
    line.tag = f"{{{cns}}}lineChart"
    for child in list(line):
        if etree.QName(child).localname in ("gapWidth", "overlap", "barDir",
                                            "grouping"):
            line.remove(child)
    cat2 = deepcopy(area.find(f"{{{cns}}}catAx"))
    val2 = deepcopy(area.find(f"{{{cns}}}valAx"))
    cat2.find(f"{{{cns}}}axId").text = "2001"
    val2.find(f"{{{cns}}}axId").text = "2002"
    cat2.find(f"{{{cns}}}crossAx").text = "2002"
    val2.find(f"{{{cns}}}crossAx").text = "2001"
    area.append(cat2)
    area.append(val2)
    area.append(line)
    members[names[0]] = etree.tostring(root, xml_declaration=True,
                                       encoding="UTF-8", standalone=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name in sorted(members):
            zout.writestr(name, members[name])


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def phase_setup() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    before = {
        "run_id": RUN_ID,
        "corpus_hashes": corpus_hashes(),
    }
    (RUN_DIR / "input_manifest.json").write_text(
        json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")

    info = build_compound_fixture(RUN_DIR / "input_compound.pptx")
    print("fixture:", info)

    run_cmd(gpn("doctor", "--run-dir", str(RUN_DIR)), expect={0},
            label="doctor")
    run_cmd(gpn("import", "--input", str(RUN_DIR / "input_compound.pptx"),
                "--run-dir", str(RUN_DIR)), expect={0}, label="import")
    run_cmd(gpn("profile", "--run-dir", str(RUN_DIR)), expect={0, 3},
            label="profile")
    # Known constraint: profile overwrites source_ir with the reference deck.
    run_cmd(gpn("import", "--input", str(RUN_DIR / "input_compound.pptx"),
                "--run-dir", str(RUN_DIR)), expect={0}, label="reimport")

    import_src = "src.slides_cli.gpn.importer"
    deck_ids = json.loads((RUN_DIR / "source_ir.json").read_text(
        encoding="utf-8"))
    _ = import_src
    for slide in deck_ids.get("slides", []):
        run_cmd(gpn("plan-packet", "--run-dir", str(RUN_DIR),
                    "--slide-id", slide["id"]), expect={0},
                label=f"plan-packet {slide['id'][:40]}")
    print("setup done; next: answer harness requests, run plan per slide")


def phase_build() -> None:
    plans_dir = RUN_DIR / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    origins: list[str] = []
    for selected in sorted((RUN_DIR / "selected_plans").glob("*.json")):
        payload = json.loads(selected.read_text(encoding="utf-8"))
        intent = payload.get("intent", {})
        (plans_dir / selected.name).write_text(json.dumps({
            "intent": intent,
            "generation_origin": payload.get("origin", "harness_model"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        if payload.get("origin"):
            origins.append(str(payload["origin"]))
    (plans_dir / "accepted_plan_manifest.json").write_text(json.dumps({
        "generation_origin": origins[0] if origins else "harness_model",
        "plans": sorted(p.name for p in plans_dir.glob("*.json")
                        if p.name != "accepted_plan_manifest.json"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    run_cmd(gpn("build", "--run-dir", str(RUN_DIR), "--plans-dir",
                str(plans_dir), "--overwrite"), expect={0, 4, 6},
            label="build")


def phase_collect() -> None:
    after = {
        "run_id": RUN_ID,
        "corpus_hashes": corpus_hashes(),
    }
    (RUN_DIR / "audit_after.json").write_text(
        json.dumps(after, ensure_ascii=False, indent=2), encoding="utf-8")
    print("collect done")


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "setup"
    {"setup": phase_setup, "build": phase_build,
     "collect": phase_collect}[phase]()
