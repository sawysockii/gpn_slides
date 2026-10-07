"""Stage 3.5 artifact run: dirty RU fixture + CLI flows + readiness.

Phases (argv[1]):

- ``setup``    build the isolated dirty fixture, snapshot corpus hashes,
               then run ``doctor``/``profile``/``import``/``plan-packet``.
- ``collect``  read back what the run produced (candidates, answers,
               diagnostic build) and write the honest readiness file.

Everything lands in ``project_root/runs/stage3_5/<run_id>``; previous runs and
the immutable corpora are never touched. Every command and exit code is
recorded in ``commands_and_results.json``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = CODE_ROOT.parent
STAGE = "stage3_5"
RUN_ID = sys.argv[2] if len(sys.argv) > 2 else "2026-10-05-02"
RUN_DIR = PROJECT_ROOT / "runs" / STAGE / RUN_ID
COMMANDS = RUN_DIR / "commands_and_results.json"

sys.path.insert(0, str(CODE_ROOT / "src"))
from slides_cli.gpn.planner import safe_token  # noqa: E402


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
# Dirty fixture: three native RU slides with deliberately non-corporate styling
# ---------------------------------------------------------------------------

def build_dirty_fixture(path: Path) -> dict:
    """Build the isolated dirty fixture (never taken from slide_examples).

    Content is qualitative on purpose: no invented corporate numbers are
    presented as facts. Titles are uppercase from the start because the
    diagnostic bridge has no supported display-transform provenance.
    """
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Emu, Inches, Pt

    prs = Presentation()
    prs.slide_width = Emu(12192000)
    prs.slide_height = Emu(6858000)
    blank = prs.slide_layouts[6]

    def box(slide, lines, *, left, top, width, height, size, color="000000",
            font="Arial", bold=False):
        shape = slide.shapes.add_textbox(
            Inches(left), Inches(top), Inches(width), Inches(height)
        )
        frame = shape.text_frame
        frame.word_wrap = True
        for idx, line in enumerate(lines):
            para = frame.paragraphs[0] if idx == 0 else frame.add_paragraph()
            run = para.add_run()
            run.text = line
            run.font.size = Pt(size)
            run.font.name = font
            run.font.color.rgb = RGBColor.from_string(color)
            run.font.bold = bold
        return shape

    # Slide 1 — comparison of three variants by available properties.
    s1 = prs.slides.add_slide(blank)
    box(s1, ["СРАВНЕНИЕ ВАРИАНТОВ"], left=0.4, top=0.3, width=6.0, height=0.6,
        size=20, color="C00000")
    box(s1, ["Варианты различаются по доступным возможностям"],
        left=0.4, top=0.95, width=8.0, height=0.4, size=13, color="595959")
    box(s1, ["Вариант А", "Разбор вручную", "Отчёт в табличном виде"],
        left=0.5, top=1.7, width=3.6, height=2.2, size=11, color="333333")
    box(s1, ["Вариант Б", "Автоматический разбор", "Отчёт в табличном виде",
             "Выгрузка в хранилище"],
        left=4.4, top=1.7, width=3.6, height=2.2, size=11, color="333333")
    box(s1, ["Вариант В", "Автоматический разбор", "Отчёт в графическом виде",
             "Выгрузка в хранилище", "Ревью по ролям"],
        left=8.3, top=1.7, width=3.6, height=2.2, size=11, color="333333")
    box(s1, ["Оговорка: сопоставление выполнено по возможностям, "
             "перечисленным на слайде"],
        left=0.5, top=4.6, width=7.0, height=0.6, size=9, color="7F7F7F")

    # Slide 2 — ordered sequence of stages, order stated in the labels.
    s2 = prs.slides.add_slide(blank)
    box(s2, ["ЭТАПЫ РАБОТЫ"], left=0.3, top=0.2, width=5.0, height=0.7,
        size=22, color="0000FF", bold=True)
    box(s2, ["Этап 1. Подготовка данных"],
        left=0.6, top=1.2, width=11.5, height=0.7, size=15, color="006100")
    box(s2, ["Этап 2. Проверка гипотезы"],
        left=0.6, top=2.1, width=11.5, height=0.7, size=15, color="006100")
    box(s2, ["Этап 3. Согласование"],
        left=0.6, top=3.0, width=11.5, height=0.7, size=15, color="006100")
    box(s2, ["Этап 4. Запуск"],
        left=0.6, top=3.9, width=11.5, height=0.7, size=15, color="006100")
    box(s2, ["Порядок этапов задан самими подписями слайда"],
        left=0.6, top=5.0, width=7.0, height=0.5, size=9, color="7F7F7F")

    # Slide 3 — group of theses, one emphasis and a caveat.
    s3 = prs.slides.add_slide(blank)
    box(s3, ["ТЕЗИСЫ И АКЦЕНТ"], left=0.2, top=0.25, width=6.0, height=0.6,
        size=18, color="800080")
    box(s3, ["Первый тезис: разбор остаётся ручной операцией"],
        left=0.7, top=1.3, width=7.0, height=0.7, size=12, color="404040")
    box(s3, ["Второй тезис: отчёт собирается в табличном виде"],
        left=0.7, top=2.2, width=7.0, height=0.7, size=12, color="404040")
    box(s3, ["Третий тезис: выгрузка доступна только в вариантах Б и В"],
        left=0.7, top=3.1, width=7.0, height=0.7, size=12, color="404040")
    box(s3, ["Главный акцент: автоматический разбор требует ревью"],
        left=0.7, top=4.2, width=9.0, height=1.0, size=16, color="C00000",
        bold=True)
    box(s3, ["Оговорка: перечень тезисов исходный, без сокращений"],
        left=0.7, top=5.5, width=7.0, height=0.5, size=8, color="7F7F7F")

    prs.save(str(path))
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "slides": len(prs.slides.__iter__.__self__._sldIdLst)
        if hasattr(prs.slides, "_sldIdLst") else 3,
        "note": (
            "synthetic dirty fixture: Arial 8-22pt, arbitrary colors, "
            "ragged geometry; titles uppercase by construction; no numeric "
            "corporate facts invented"
        ),
    }


def setup() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    fixture = RUN_DIR / "fixture_dirty.pptx"
    if fixture.is_file():
        fixture.unlink()
    info = build_dirty_fixture(fixture)
    print(f"fixture: {info}")

    (RUN_DIR / "fixture_dirty_info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    before = {
        "run_id": RUN_ID,
        "stage": STAGE,
        "code_head": subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            cwd=CODE_ROOT).stdout.strip(),
        "code_branch": subprocess.run(
            ["git", "branch", "--show-current"], capture_output=True, text=True,
            cwd=CODE_ROOT).stdout.strip(),
        "corpus_hashes": corpus_hashes(),
        "fixture_sha256": sha256_file(fixture),
        "amendment": (
            "1.0: all LLM answers are produced by the harness' own online "
            "model via the model_requests/model_responses JSON contract; the "
            "local HTTP endpoint is not connected at this stage"
        ),
    }
    (RUN_DIR / "audit_before.json").write_text(
        json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")

    run_cmd(gpn("doctor", "--check-model", "--run-dir", str(RUN_DIR / "doctor")),
            expect={0, 3, 4}, label="doctor --check-model")
    run_cmd(gpn("profile", "--run-dir", str(RUN_DIR)), expect={0, 3, 4},
            label="profile")
    run_cmd(gpn("import", "--input", str(fixture), "--run-dir", str(RUN_DIR)),
            expect={0, 4}, label="import dirty fixture")

    deck = json.loads((RUN_DIR / "source_ir.json").read_text(encoding="utf-8"))
    slide_ids = [s["id"] for s in deck["slides"]]
    print(f"slide ids: {slide_ids}")
    for slide_id in slide_ids:
        run_cmd(gpn("plan-packet", "--run-dir", str(RUN_DIR),
                    "--slide-id", slide_id),
                expect={0}, label=f"plan-packet {slide_id}")

    after = dict(before)
    after["corpus_hashes_after_setup"] = corpus_hashes()
    after["commands"] = len(_load_commands())
    (RUN_DIR / "audit_after.json").write_text(
        json.dumps(after, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"setup done: {RUN_DIR}")


def collect() -> None:
    """Aggregate the per-slide plan/diagnostic results into run artifacts."""
    slide_ids: list[str] = []
    deck_path = RUN_DIR / "source_ir.json"
    if deck_path.is_file():
        slide_ids = [s["id"] for s in
                     json.loads(deck_path.read_text(encoding="utf-8"))["slides"]]

    slides_report: list[dict] = []
    for slide_id in slide_ids:
        cand_dir = RUN_DIR / "candidates"
        candidates = []
        for path in sorted(cand_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            candidates.append({
                "candidate_id": data.get("candidate_id"),
                "valid": data.get("valid"),
                "origin": data.get("origin"),
                "request_id": data.get("request_id"),
                "diversity_signature": data.get("diversity_signature"),
                "errors": data.get("errors"),
                "communication_job": (data.get("intent") or {}).get(
                    "communication_job"),
                "operators": (data.get("intent") or {}).get("operators"),
                "zones": [
                    {"zone_id": z.get("zone_id"), "role": z.get("role"),
                     "refs": z.get("content_refs"),
                     "box": [z.get("x_pct"), z.get("y_pct"),
                             z.get("w_pct"), z.get("h_pct")]}
                    for z in (data.get("intent") or {}).get("zones", [])
                ],
            })
        selected = None
        sel_path = RUN_DIR / "selected_plans" / f"{safe_token(slide_id)}.json"
        if sel_path.is_file():
            data = json.loads(sel_path.read_text(encoding="utf-8"))
            selected = {
                "candidate_id": data.get("candidate_id"),
                "status": data.get("status"),
                "scores": data.get("scores"),
                "reason": data.get("reason"),
            }
        slides_report.append({
            "slide_id": slide_id,
            "candidates": candidates,
            "selected": selected,
        })

    usage_path = RUN_DIR / "model_usage.json"
    usage = json.loads(usage_path.read_text(encoding="utf-8")) \
        if usage_path.is_file() else {}

    diag_path = RUN_DIR / "diagnostic_result.json"
    diagnostic = json.loads(diag_path.read_text(encoding="utf-8")) \
        if diag_path.is_file() else None

    candidate_pptx = RUN_DIR / "diagnostic_candidate.pptx"
    readiness = {
        "run_id": RUN_ID,
        "ontology_source_driven": bool(
            (RUN_DIR / "field_origins.json").is_file()),
        "ontology_conflicts_resolved": bool(
            (RUN_DIR / "ontology_conflicts.json").is_file()),
        "rule_binding_complete": False,
        "runtime_planner_ready": bool(
            slides_report and any(s["candidates"] for s in slides_report)),
        "model_endpoint_reachable": False,
        "real_generation_verified": int(
            usage.get("generation_requests", 0) or 0) > 0,
        "diagnostic_bridge_ready": bool(
            diagnostic and diagnostic.get("status") == "applied"
        ),
        "meaningful_diversity_demonstrated": any(
            len({c["diversity_signature"] for c in s["candidates"]
                 if c["valid"] and c["diversity_signature"]}) >= 2
            for s in slides_report
        ),
        "source_preservation_verified": bool(
            diagnostic and diagnostic.get("status") == "applied"
            and (diagnostic.get("import_diff") or {}).get("missing_atoms") == []
        ),
        "production_assets_ready": False,
        "render_verified": False,
        "strict_output_ready": False,
        "diagnostic_candidate": str(candidate_pptx) if candidate_pptx.is_file()
        else None,
        "usage": usage,
        "slides": slides_report,
        "notes": [],
        "next_stage": (
            "Stage 4 native exporter/transplant (tables/charts/notes/links); "
            "Stage 5 exact text measurement; Stage 6 extended retrieval"
        ),
    }
    (RUN_DIR / "stage3_5_readiness.json").write_text(
        json.dumps(readiness, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in readiness.items()
                      if k not in ("slides", "usage")}, ensure_ascii=False,
                     indent=2))


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "setup"
    if phase == "setup":
        setup()
    elif phase == "collect":
        collect()
    else:
        raise SystemExit(f"unknown phase {phase!r}")