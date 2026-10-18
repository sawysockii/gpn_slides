"""Stage 7 §4: typed, expected-first, evidence-aware product acceptance.

``aggregate_product_acceptance`` is the ONLY public status derivation.
Every gate is validated against the expectation and the artifact store:

* empty expected slide set / empty candidate hash -> invalid, never pass;
* slide count / order / hidden flags / canvas compared independently of
  ``candidate_after`` JSON — expectation comes from the source projection;
* required gates without evidence are ``GATE_MISSING``; duplicate gate
  identities merge deterministically keeping the worst status;
* ``not_applicable`` requires an ``ApplicabilityProof``; a missing
  checker is never a pass;
* visual reviews must carry the current candidate hash and a complete
  ordered page set matching the render manifest;
* selections are exactly one per expected source slide and the assembled
  package must BE the selected candidate (``SELECTED_CANDIDATE_NOT_ASSEMBLED``);
* blocker list and per-scope status never disagree.

Run with::

    .venv/bin/python -m slides_cli.gpn.acceptance RUN_DIR
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import (
    ApplicabilityProof,
    ArtifactEvidenceRef,
    ArtifactStore,
    CandidateKey,
    CapabilityInventory,
    EvidenceValidation,
    FrameExpectation,
    GateEvidence,
    GateExpectation,
    GateStatus,
    InputSpec,
    ProductAcceptance,
    ProductExpectation,
    ProductScope,
    ReviewedPage,
    RunContext,
    SavedPackageManifest,
    SavedRenderPage,
    SavedSlide,
    SelectionEntry,
    SelectionPolicy,
    SelectionReport,
    SemanticDeck,
    SemanticRelation,
    SemanticSlide,
    SemanticUnit,
    SlideExpectation,
    SnapshotSet,
    TypedFinding,
    VisualReview,
)

ACCEPTANCE_SCHEMA = "product-acceptance/v2"

# §3 contract name; the shared model module calls it ArtifactEvidenceRef.
EvidenceRef = ArtifactEvidenceRef

# Worst-status ordering for deterministic scope merge (§4.2.3, §4.2.11).
_STATUS_RANK = {
    GateStatus.FAIL: 4,
    GateStatus.UNKNOWN: 3,
    GateStatus.NOT_APPLICABLE: 2,
    GateStatus.PASS: 1,
}

# Mandatory gate families (§4.3). Units are derived from the semantic
# projection and the compiled ontology — never from "reports we found".
# build_product_expectation emits these (per-slide suffixed where scoped).
EXPECTATION_GATE_FAMILIES: tuple[str, ...] = (
    "semantic_content_preservation",
    "semantic_relations",
    "package_graph",
    "saved_auth_rules",
    "verified_frame",
    "saved_text_exact",
    "text_fit_visibility",
    "geometry_overlap",
    "font_loading",
    "render_coverage",
    "deck_visual_review",
    "visual_design",
    "candidate_selection",
    "assembly_linkage",
    "native_editability",
)


def _family_of(gate_id: str) -> str:
    base = gate_id.split(":")[0]
    return base if base in EXPECTATION_GATE_FAMILIES else gate_id


class AcceptanceInputError(ValueError):
    """Invalid input to the acceptance pipeline (typed failure)."""


# ---------------------------------------------------------------------------
# expectation
# ---------------------------------------------------------------------------

def _gate_units_for_slide(slide: SemanticSlide, prefix: str) -> list[str]:
    return [f"{prefix}:unit:{u.id}" for u in slide.units
            if u.preservation != "optional"]


def build_product_expectation(
    input_spec: InputSpec,
    semantic: SemanticDeck,
    ontology,
    capability_inventory: CapabilityInventory,
    selection_policy: SelectionPolicy,
) -> ProductExpectation:
    """Expected-first contract (§4.1). Empty slide set is invalid."""
    if not semantic.slides:
        raise AcceptanceInputError(
            "EXPECTATION_EMPTY: semantic projection has no slides")
    if not input_spec.project_root.is_dir():
        raise AcceptanceInputError(
            f"project root missing: {input_spec.project_root}")

    canvas = getattr(ontology, "canvas", None)
    from .models import RectEMU
    if canvas is None:
        canvas = RectEMU()

    slides: list[SlideExpectation] = []
    for s in semantic.slides:
        rel_ids = [r.id for r in s.relations
                   if r.preservation == "required"]
        unit_ids = [u.id for u in s.units
                    if u.preservation != "optional"]
        slides.append(SlideExpectation(
            source_slide_id=s.source_slide_id,
            hidden=s.hidden,
            required_units=unit_ids,
            required_relations=rel_ids))

    rule_ids = sorted(getattr(getattr(ontology, "rule_registry", None),
                              "rules", {}) or {})
    gates: list[GateExpectation] = []

    def gate(gid: str, units: list[str], origin: str, applies: list[str],
             required: bool = True) -> None:
        gates.append(GateExpectation(
            gate_id=gid, required=required, applies_to=applies,
            expected_units=sorted(units), policy_origin=origin))

    for s in semantic.slides:
        sid = s.source_slide_id
        gate(f"semantic_content_preservation:{sid}",
             _gate_units_for_slide(s, sid),
             "source semantic projection (required units)",
             [sid], required=True)
        rel_units = [f"{sid}:relation:{r.id}" for r in s.relations
                     if r.preservation == "required"]
        if rel_units:
            gate(f"semantic_relations:{sid}", rel_units,
                 "source semantic projection (required relations)", [sid])
    if any(s.relations for s in semantic.slides):
        gate("semantic_relations",
             [f"deck:relation:{rid}" for rid in semantic.relations
              if semantic.relations[rid].preservation == "required"],
             "source semantic projection (required relations)", ["deck"])

    gate("package_graph", ["package:parts", "package:media",
                           "package:relationships"],
         "OPC closure", ["deck"])
    rule_units = [f"rule:{rid}" for rid in rule_ids]
    if capability_inventory.has_native_chart:
        rule_units.append("rule:chart_applicability")
    gate("saved_auth_rules", rule_units or ["rule:registry"],
         "compiled ontology rule registry", ["deck"])
    frame = FrameExpectation(
        required=True, verified=False,
        source_refs=["ontology:canvas", "examples:allowlist"])
    gate("verified_frame", ["frame:regions", "frame:protected"],
         "JSON constraints + approved examples", ["deck"])
    text_units = [u.id for s in semantic.slides for u in s.units
                  if u.kind in ("text", "paragraph", "title", "label",
                                "metric_value", "footnote")]
    if not capability_inventory.fonts_verified:
        text_units = text_units or ["text:inventory"]
    gate("saved_text_exact", [f"text:{u}" for u in text_units] or
         ["text:inventory"], "resolved-vs-saved typography", ["deck"])
    gate("text_fit_visibility", ["fit:no_overflow", "visibility:required"],
         "measurement + native visibility", ["deck"])
    gate("geometry_overlap", ["geometry:in_bounds",
                              "geometry:allowed_overlap"],
         "geometry rules + frame", ["deck"])
    gate("font_loading",
         ["font:verified"] if capability_inventory.fonts_verified
         else ["font:missing_blocking"],
         "font policy", ["deck"])
    gate("render_coverage", [f"render:page:{i + 1}" for i in
                             range(len(semantic.slides))],
         "render manifest over final bytes", ["deck"])
    gate("deck_visual_review", ["review:pages", "review:verdict"],
         "current-bytes visual review", ["deck"])
    gate("visual_design",
         ["design:dimensions", "design:diversity", "design:repair"],
         "design briefs + concepts + current design review", ["deck"])
    gate("candidate_selection",
         [f"selection:{s.source_slide_id}" for s in semantic.slides],
         "real candidate selection policy", ["deck"])
    gate("assembly_linkage", ["assembly:scene->slide"],
         "full assembly provenance", ["deck"])
    gate("native_editability", ["native:editable"],
         "native OOXML editability", ["deck"])

    return ProductExpectation(
        input_spec=input_spec,
        source_sha256=semantic.source_manifest_sha256,
        slides=slides,
        canvas=canvas,
        required_gates=gates,
        frame=frame,
        selection_policy=selection_policy,
        visual_design_required=True,
        ui_proof_required=False,
    )


# ---------------------------------------------------------------------------
# saved manifest (from candidate bytes, never from trusted JSON)
# ---------------------------------------------------------------------------

def _slide_kind_counts(package_path: Path) -> tuple[dict[str, int], int]:
    """Walk OOXML parts counting drawable objects (incl. group children)."""
    from pptx.oxml.ns import qn  # noqa: PLC0415
    counts: dict[str, int] = {}
    addresses = 0

    def walk(elem, depth=0) -> None:
        nonlocal addresses
        tag = elem.tag
        if not isinstance(tag, str):
            return
        local = tag.rsplit("}", 1)[-1]
        if local in ("sp", "graphicFrame", "pic", "grpSp", "cxnSp"):
            kind = {"sp": "shape", "graphicFrame": "graphicFrame",
                    "pic": "image", "grpSp": "group",
                    "cxnSp": "connector"}[local]
            counts[kind] = counts.get(kind, 0) + 1
            nv = elem.find(".//" + qn("p:cNvPr"))
            if nv is not None:
                addresses += 1
        for child in elem:
            if local in ("spTree", "grpSp", "cSld") or depth == 0:
                walk(child, depth + 1)

    with zipfile.ZipFile(package_path) as zf:
        slide_parts = sorted(n for n in zf.namelist()
                             if n.startswith("ppt/slides/slide")
                             and n.endswith(".xml"))
        for part in slide_parts:
            from lxml import etree  # noqa: PLC0415
            root = etree.fromstring(zf.read(part))
            walk(root)
    return counts, addresses


def collect_saved_manifest(candidate: Path, ctx: RunContext) -> SavedPackageManifest:
    """Read package facts from candidate bytes (§4.1)."""
    candidate = Path(candidate)
    if not candidate.is_file():
        raise AcceptanceInputError(
            f"CANDIDATE_MISSING: {candidate}")
    sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
    if not sha:
        raise AcceptanceInputError("CANDIDATE_MISSING: empty hash")

    from pptx import Presentation  # noqa: PLC0415
    from pptx.oxml.ns import qn  # noqa: PLC0415
    prs = Presentation(str(candidate))
    sld_sz = prs.part._element.find(qn("p:sldSz"))  # type: ignore[attr-defined]
    canvas = None
    if sld_sz is not None:
        from .models import RectEMU
        canvas = RectEMU(x=int(sld_sz.get("cx", 0)),
                         y=0,
                         w=int(sld_sz.get("cx", 0)),
                         h=int(sld_sz.get("cy", 0)))
    if canvas is None:
        raise AcceptanceInputError("CANVAS_MISMATCH: no sldSz")

    slides: list[SavedSlide] = []
    sld_id_lst = prs.part._element.find(qn("p:sldIdLst"))  # type: ignore
    if sld_id_lst is None or len(sld_id_lst) == 0:
        raise AcceptanceInputError(
            "SLIDE_SET_MISMATCH: candidate has no sldIdLst")
    for sld_id in sld_id_lst:
        rid = sld_id.get(
            "{http://schemas.openxmlformats.org/officeDocument/"
            "2006/relationships}id") or ""
        part = prs.part.rels[rid].target_part if rid else None
        hidden = bool(sld_id.get("show", "1") == "0")
        slides.append(SavedSlide(
            sld_id=str(sld_id.get("id", "")), r_id=rid,
            part=getattr(part, "partname", str(part)) if part else "",
            hidden=hidden))

    inventory, address_count = _slide_kind_counts(candidate)
    with zipfile.ZipFile(candidate) as zf:
        names = zf.namelist()
    media = sorted(n for n in names if n.startswith("ppt/media/"))
    notes = sorted(n for n in names if n.startswith("ppt/notesSlides/")
                   and n.endswith(".xml"))

    render_pages: list[SavedRenderPage] = []
    manifest_sha = ""
    manifest_p = ctx.run_dir / "render" / "render_manifest.json"
    if manifest_p.is_file():
        try:
            manifest = json.loads(manifest_p.read_text(encoding="utf-8"))
            manifest_sha = hashlib.sha256(
                manifest_p.read_bytes()).hexdigest()
            for p in manifest.get("pages", []):
                render_pages.append(SavedRenderPage(
                    page=int(p.get("page", 0)), image=str(p.get("image", "")),
                    sha256=str(p.get("sha256", ""))))
        except (OSError, json.JSONDecodeError, ValueError):
            render_pages = []

    return SavedPackageManifest(
        candidate_path=str(candidate), candidate_sha256=sha,
        canvas=canvas, slides=slides, drawing_inventory=inventory,
        address_count=address_count, package_parts=sorted(names),
        media_parts=media, notes_parts=notes,
        render_manifest_sha256=manifest_sha, render_pages=render_pages)


# ---------------------------------------------------------------------------
# evidence validation
# ---------------------------------------------------------------------------

def validate_evidence(
    expectation: GateExpectation,
    evidence: GateEvidence,
    store: ArtifactStore,
    current: SnapshotSet,
) -> EvidenceValidation:
    """§4.2.4: units coverage, stale fingerprints, artifact hashes."""
    errors: list[str] = []
    if evidence.gate_id != expectation.gate_id:
        errors.append("EVIDENCE_STALE: gate_id mismatch")

    missing = [u for u in expectation.expected_units
               if u not in evidence.checked_units]
    extra = [u for u in evidence.checked_units
             if u not in expectation.expected_units]
    has_proof = (evidence.status is GateStatus.NOT_APPLICABLE
                 and evidence.not_applicable_evidence is not None
                 and bool(evidence.not_applicable_evidence.reason)
                 and bool(evidence.not_applicable_evidence.inventory_evidence))
    if expectation.expected_units and missing and not has_proof:
        errors.append(f"SCOPE_COVERAGE_INCOMPLETE: missing {missing[:4]}")
    if extra and not has_proof:
        errors.append(f"SCOPE_COVERAGE_INCOMPLETE: undeclared {extra[:4]}")
    if expectation.expected_units and not evidence.checked_units \
            and not has_proof:
        errors.append("SCOPE_COVERAGE_INCOMPLETE: zero checked units")

    if evidence.status is GateStatus.NOT_APPLICABLE:
        proof: ApplicabilityProof | None = evidence.not_applicable_evidence
        if proof is None or not proof.reason or not proof.inventory_evidence:
            errors.append("SCOPE_COVERAGE_INCOMPLETE: not_applicable "
                          "without proof")

    snap = evidence.snapshots
    for field in ("input_sha256", "compiled_rules_sha256"):
        expected_v = getattr(current, field, "")
        actual_v = getattr(snap, field, "")
        if expected_v and actual_v and expected_v != actual_v:
            errors.append(f"EVIDENCE_STALE: {field} changed")
            break

    for ref in evidence.artifacts:
        ok, _ = store.verify(ref)
        if not ok and ref.sha256:
            errors.append(f"EVIDENCE_HASH_MISMATCH: {ref.artifact_id} "
                          f"({ref.relative_path})")
    return EvidenceValidation(gate_id=evidence.gate_id,
                              valid=not errors, errors=errors)


# ---------------------------------------------------------------------------
# aggregation (§4.2)
# ---------------------------------------------------------------------------

def _worst(a: GateStatus, b: GateStatus) -> GateStatus:
    return a if _STATUS_RANK[a] >= _STATUS_RANK[b] else b


def aggregate_product_acceptance(
    expected: ProductExpectation,
    saved: SavedPackageManifest,
    gates: list[GateEvidence],
    reviews: list[VisualReview],
    selections: list[SelectionEntry | SelectionReport],
    *,
    artifact_store: ArtifactStore,
) -> ProductAcceptance:
    """Single public derivation of product status. Never hand-edited."""
    findings: list[TypedFinding] = []
    scopes: dict[str, str] = {}

    def finding(code: str, message: str, severity: str = "error",
               scope: str | None = None) -> None:
        findings.append(TypedFinding(code=code, severity=severity,  # type: ignore[arg-type]
                                     message=message, scope=scope))

    # -- 1. schemas/finite/mandatory hashes --------------------------------
    if not saved.candidate_sha256:
        finding("CANDIDATE_MISSING", "empty candidate hash")
    if not expected.slides:
        finding("EXPECTATION_EMPTY", "expected slide set is empty")
        return _result(False, findings, scopes, expected, saved,
                       {"CANDIDATE_MISSING": "no candidate",
                        "EXPECTATION_EMPTY": "empty expectation"})

    # -- 2. independent comparison of package vs expectation ----------------
    exp_slides = expected.slides
    if len(saved.slides) != len(exp_slides):
        finding("SLIDE_SET_MISMATCH",
                f"expected {len(exp_slides)} slides, package has "
                f"{len(saved.slides)}")
    for got, want in zip(saved.slides, exp_slides, strict=False):
        if got.part and want.slide_part and got.part != want.slide_part:
            finding("ORDER_MISMATCH",
                    f"slide order: expected {want.slide_part}, "
                    f"package has {got.part}", scope=want.source_slide_id)
        if got.hidden != want.hidden:
            finding("HIDDEN_FLAGS_MISMATCH",
                    f"hidden flag: expected {want.hidden}, "
                    f"package has {got.hidden}", scope=want.source_slide_id)
    if (saved.canvas.w and expected.canvas.w
            and (saved.canvas.w != expected.canvas.w
                 or saved.canvas.h != expected.canvas.h)):
        finding("CANVAS_MISMATCH",
                f"expected {expected.canvas.w}x{expected.canvas.h}, "
                f"package has {saved.canvas.w}x{saved.canvas.h}")

    # -- 3./4. gate identity index + per-gate validation -------------------
    merged: dict[str, GateEvidence] = {}
    duplicates: set[str] = set()
    for ev in gates:
        if ev.gate_id in merged:
            duplicates.add(ev.gate_id)
            keep = merged[ev.gate_id]
            merged[ev.gate_id] = ev if _STATUS_RANK[ev.status] > \
                _STATUS_RANK[keep.status] else keep
        else:
            merged[ev.gate_id] = ev

    current = expected.snapshots
    for gid in sorted(duplicates):
        finding("GATE_DUPLICATE",
                f"duplicate gate identity evidence: {gid}")

    expected_ids = {g.gate_id for g in expected.required_gates}
    unknown = sorted(set(merged) - expected_ids)
    if unknown:
        families = sorted({_family_of(g) for g in unknown})
        finding("GATE_DUPLICATE",
                f"unknown gate identities (contradictory): {unknown[:4]} "
                f"families={families[:4]}")

    checked_units: set[str] = set()
    for exp_gate in expected.required_gates:
        ev = merged.get(exp_gate.gate_id)
        if ev is None:
            if exp_gate.required:
                finding("GATE_MISSING",
                        f"required gate without evidence: "
                        f"{exp_gate.gate_id}")
            continue
        validation = validate_evidence(exp_gate, ev, artifact_store, current)
        for err in validation.errors:
            code = err.split(":", 1)[0]
            finding(code, f"{exp_gate.gate_id}: {err}",
                    scope=exp_gate.applies_to[0] if exp_gate.applies_to
                    else None)
        worst = merged_gate_statuses(merged, exp_gate.gate_id)
        scopes[exp_gate.gate_id] = worst.value
        if exp_gate.expected_units:
            checked_units.update(
                u for u in ev.checked_units if u in exp_gate.expected_units)
        if worst in (GateStatus.FAIL, GateStatus.UNKNOWN):
            finding(exp_gate.gate_id,
                    f"required gate {worst.value}: {exp_gate.gate_id}")
        frame_required = (
            exp_gate.gate_id == "verified_frame"
            and expected.frame.required and not expected.frame.verified)
        if frame_required and worst is not GateStatus.PASS:
            finding("FRAME_UNVERIFIED",
                    "normative frame not verified from JSON + examples")

    # -- 7. visual reviews ---------------------------------------------------
    for rev in reviews:
        scope = "deck_visual_review"
        if not rev.candidate_sha256:
            finding("REVIEW_STALE", "review without candidate hash")
            scopes[scope] = "stale"
            continue
        if rev.candidate_sha256 != saved.candidate_sha256:
            finding("REVIEW_STALE",
                    f"review of other bytes: {rev.candidate_sha256[:12]}… "
                    f"vs saved {saved.candidate_sha256[:12]}…")
            scopes[scope] = "stale"
            continue
        if not rev.pages:
            finding("REVIEW_PAGE_SET_MISMATCH", "review has no pages")
            scopes[scope] = "repair"
            continue
        pages = [p.page for p in rev.pages]
        if len(pages) != len(set(pages)):
            finding("REVIEW_PAGE_SET_MISMATCH", "duplicate review pages")
        manifest_pages = [p.page for p in saved.render_pages]
        if manifest_pages and sorted(pages) != sorted(manifest_pages):
            finding("REVIEW_PAGE_SET_MISMATCH",
                    f"review pages {sorted(pages)} != render manifest "
                    f"{sorted(manifest_pages)}")
        elif not manifest_pages and len(pages) != len(exp_slides):
            finding("REVIEW_PAGE_SET_MISMATCH",
                    f"review covers {len(pages)}/{len(exp_slides)} pages")
        for p in rev.pages:
            if not p.sha256 or p.byte_count <= 0:
                finding("EVIDENCE_HASH_MISMATCH",
                        f"page {p.page} without verified image bytes")
        if rev.verdict != "accept":
            finding("deck_visual_review",
                    f"visual verdict {rev.verdict}")
            scopes[scope] = rev.verdict
        elif scope not in scopes:
            scopes[scope] = "accept"

    # -- 9./10. selections + assembly linkage -------------------------------
    entries: list[SelectionEntry] = []
    for s in selections:
        if isinstance(s, SelectionReport):
            entries.extend(s.selections)
        else:
            entries.append(s)
    by_slide: dict[str, list[SelectionEntry]] = {}
    for e in entries:
        by_slide.setdefault(e.source_slide_id, []).append(e)
    for exp_slide in exp_slides:
        got = by_slide.get(exp_slide.source_slide_id, [])
        if len(got) > 1:
            finding("SELECTION_INVALID",
                    f"duplicate selection for {exp_slide.source_slide_id}")
        elif not got:
            finding("SELECTION_INVALID",
                    f"missing selection for {exp_slide.source_slide_id}")
            continue
        e = got[0]
        if not e.candidate_sha256:
            finding("SELECTION_INVALID",
                    f"selection without candidate hash: "
                    f"{exp_slide.source_slide_id}")
            continue
        ck = e.candidate_key
        if ck is None:
            finding("SELECTION_INVALID",
                    f"selection without full CandidateKey: "
                    f"{exp_slide.source_slide_id}")
        if e.candidate_sha256 != saved.candidate_sha256:
            finding("SELECTED_CANDIDATE_NOT_ASSEMBLED",
                    f"selected {ck} bytes {e.candidate_sha256[:12]}… != "
                    f"assembled {saved.candidate_sha256[:12]}…",
                    scope=exp_slide.source_slide_id)
    for sid in by_slide:
        if sid not in {s.source_slide_id for s in exp_slides}:
            finding("SELECTION_INVALID", f"unexpected selected slide {sid}")

    # -- 11./12. derive ------------------------------------------------------
    accepted = not findings
    required_count = len({g.gate_id for g in expected.required_gates
                          if g.required})
    checked_count = len({g.gate_id for g in expected.required_gates
                         if g.required and g.gate_id in scopes
                         and scopes[g.gate_id] in ("pass", "not_applicable",
                                                   "accept")
                         and g.gate_id in merged})
    error_codes = sorted({f.code for f in findings})
    evidence_index: dict[str, object] = {
        "gates": {gid: ev.status.value for gid, ev in merged.items()},
        "reviews": [r.task_id for r in reviews],
        "selections": [str(e.candidate_key) for e in entries],
    }
    return ProductAcceptance(
        accepted=accepted,
        subject_candidate_sha256=saved.candidate_sha256,
        snapshots=current,
        required_scope_count=required_count,
        checked_scope_count=checked_count,
        findings=findings,
        evidence_index=evidence_index,
        error_codes=error_codes,
        publication_eligible=accepted,
    )


def merged_gate_statuses(merged: dict[str, GateEvidence],
                         gate_id: str) -> GateStatus:
    ev = merged.get(gate_id)
    return ev.status if ev else GateStatus.UNKNOWN


def _result(accepted: bool, findings: list[TypedFinding],
            scopes: dict[str, str], expected: ProductExpectation,
            saved: SavedPackageManifest,
            codes: dict[str, str]) -> ProductAcceptance:
    return ProductAcceptance(
        accepted=accepted,
        subject_candidate_sha256=saved.candidate_sha256,
        findings=findings, evidence_index=dict(codes),
        error_codes=sorted(codes),
        publication_eligible=False,
    )


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_acceptance_atomic(path: Path, acceptance: ProductAcceptance) -> None:
    """§4.2.12: atomic write; accepted bool never hand-edited after this."""
    path = Path(path)
    payload = json.dumps(acceptance.model_dump(mode="json"),
                         ensure_ascii=False, indent=1)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# CLI: same derivation, assembling typed evidence from the run directory
# ---------------------------------------------------------------------------

def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _snapshots_from_run(run: Path) -> SnapshotSet:
    data = _load_json(run / "input_hashes.json") or {}
    inv = _load_json(run / "font_inventory.json") or {}
    compiled = _load_json(run / "compiled_rules.json") or {}
    ont = _load_json(run / "ontology_snapshot.json") or {}

    def h(v) -> str:
        return str(v or "")

    return SnapshotSet(
        input_sha256=h(data.get("input_sha256")),
        semantic_sha256=h(data.get("semantic_sha256")),
        ontology_sha256=h(ont.get("sha256") or data.get("ontology_sha256")),
        compiled_rules_sha256=h(compiled.get("source_hash")
                                or data.get("compiled_rules_sha256")),
        examples_allowlist_sha256=h(data.get("examples_allowlist_sha256")),
        fonts_manifest_sha256=h(inv.get("sha256")
                                or data.get("fonts_manifest_sha256")),
        frame_sha256=h(data.get("frame_sha256")),
        code_sha256=h(data.get("code_sha256")),
    )


def _review_from_json(data: dict, run: Path) -> VisualReview | None:
    if not data:
        return None
    pages = [ReviewedPage(
        page=int(p.get("page", 0)), image=str(p.get("image", "")),
        sha256=str(p.get("sha256", "")),
        byte_count=int(p.get("bytes", p.get("byte_count", 0))))
        for p in data.get("pages", [])]
    ck_data = data.get("candidate_key") or {}
    ck = None
    if ck_data.get("run_id"):
        ck = CandidateKey(
            run_id=str(ck_data.get("run_id", "")),
            source_slide_id=str(ck_data.get("source_slide_id", "")),
            candidate_id=str(ck_data.get("candidate_id", "final")),
            revision=int(ck_data.get("revision", 0)))
    return VisualReview(
        task_id=str(data.get("task_id", data.get("request_id", ""))),
        candidate_key=ck,
        candidate_sha256=str(data.get("candidate_sha256", "")),
        render_manifest_sha256=str(data.get("render_manifest_sha256", "")),
        pages=pages,
        verdict=data.get("verdict", data.get("status", "repair")),
        findings=[TypedFinding(code=f.get("code", "visual_finding"),
                               severity=f.get("severity", "warning"),
                               message=f.get("message", ""),
                               scope=f.get("scope"))
                  for f in data.get("findings", [])],
        quality={k: int(v) for k, v in
                 (data.get("quality") or {}).items()},
        actual_model=str(data.get("actual_model", "")),
    )


def _gate_evidence_from_json(data: dict, run: Path,
                             snapshots: SnapshotSet) -> GateEvidence | None:
    if not data:
        return None
    status_raw = str(data.get("status", "unknown"))
    try:
        status = GateStatus(status_raw)
    except ValueError:
        status = GateStatus.UNKNOWN
    artifacts = []
    for a in data.get("artifacts", []) or []:
        if isinstance(a, str):
            p = run / a
            if p.is_file():
                artifacts.append(EvidenceRef(
                    artifact_id=p.name, relative_path=a,
                    sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
                    byte_count=p.stat().st_size))
    proof = None
    nap = data.get("not_applicable_evidence") or {}
    if nap:
        proof = ApplicabilityProof(
            rule_id=nap.get("rule_id"),
            inventory_evidence=list(nap.get("inventory_evidence", [])),
            reason=str(nap.get("reason", "")))
    return GateEvidence(
        gate_id=str(data.get("gate", data.get("gate_id", "unnamed"))),
        status=status,
        expected_units=list(data.get("expected_units", []) or []),
        checked_units=list(data.get("checked_units", []) or []),
        findings=[TypedFinding(code=f.get("code", "gate_finding"),
                               severity=f.get("severity", "warning"),
                               message=f.get("message", ""),
                               scope=f.get("scope"))
                  for f in data.get("findings", [])],
        artifacts=artifacts,
        implementation_id=str(data.get("implementation_id", "")),
        snapshots=snapshots,
        subject_candidate_sha256=str(
            data.get("subject_candidate_sha256", "")),
        not_applicable_evidence=proof,
    )


def _expectation_from_run(run: Path, ctx: RunContext,
                          snapshots: SnapshotSet) -> ProductExpectation:
    """Build expectation from run inputs (semantic deck + compiled rules).

    Expected-first: slide set/order/hidden come from the semantic deck
    projection, never from emitted slides or found reports.
    """
    semantic = _load_json(run / "inputs" / "semantic_deck.json") or \
        _load_json(run / "semantic_deck.json") or {}
    slides: list[SemanticSlide] = []
    for s in semantic.get("slides", []):
        units = [SemanticUnit(
            id=str(u.get("id", "")), kind=str(u.get("kind", "text")),
            content=_content(u), preservation=u.get(
                "preservation", "required_semantic"),
            semantic_role=str(u.get("semantic_role", "")))
            for u in s.get("units", [])]
        relations = [SemanticRelation(
            id=str(r.get("id", "")), kind=str(r.get("kind", "relation")),
            from_unit_ids=[str(x) for x in r.get("from_unit_ids", [])],
            to_unit_ids=[str(x) for x in r.get("to_unit_ids", [])],
            directed=r.get("directed"),
            conditions=[str(c) for c in r.get("conditions", [])],
            preservation=r.get("preservation", "required"))
            for r in s.get("relations", [])]
        slides.append(SemanticSlide(
            source_slide_id=str(s.get("source_slide_id", s.get("id", ""))),
            title=str(s.get("title", "")), units=units, relations=relations,
            hidden=bool(s.get("hidden", False))))
    deck_units = {u.id: u for s in slides for u in s.units}
    deck_rels = {rel.id: rel for s in slides for rel in s.relations}
    deck = SemanticDeck(
        source_manifest_sha256=semantic.get("source_manifest_sha256", ""),
        slides=slides, units=deck_units, relations=deck_rels,
        sha256=semantic.get("sha256", ""))

    from .models import RectEMU
    compiled = _load_json(run / "inputs" / "compiled_rules.json") or \
        _load_json(run / "compiled_rules.json") or {}
    registry = compiled.get("rule_registry", {}) or {}
    rules = registry.get("rules", registry) if isinstance(registry,
                                                          dict) else {}
    if isinstance(rules, list):
        sorted({str(r.get("id", "")) for r in rules})
    else:
        sorted(str(k) for k in rules)
    from types import SimpleNamespace
    ontology = SimpleNamespace(
        canvas=RectEMU(x=0, y=0,
                       w=int(compiled.get("canvas", {}).get("cx", 0)
                             or 12192000),
                       h=int(compiled.get("canvas", {}).get("cy", 0)
                             or 6858000)),
        rule_registry=SimpleNamespace(rules=rules))

    from .models import InputKind  # noqa: PLC0415
    input_kind = str((semantic.get("input") or {}).get("kind", "pptx"))
    try:
        kind = InputKind(input_kind)
    except ValueError:
        kind = InputKind.PPTX
    scope_raw = str((semantic.get("input") or {}).get("scope", "restyle"))
    scope = ProductScope.CREATE if scope_raw == "create" \
        else ProductScope.RESTYLE
    input_spec = InputSpec(
        kind=kind,
        path=Path((semantic.get("input") or {}).get("path", ".")
               ) if (semantic.get("input") or {}).get("path") else None,
        project_root=ctx.project_root,
        output_path=ctx.run_dir / "candidate.pptx",
        requested_slide_count=(semantic.get("input") or {}).get(
            "requested_slide_count"),
        requested_scope=scope)

    inv_data = _load_json(run / "font_inventory.json") or {}
    capability = CapabilityInventory(
        fonts_verified=bool(inv_data.get("verified", False)),
        images_restored=bool((run / "authorized_replacements_report.json")
                             .is_file()),
        links_preserved=bool((run / "candidate_after_links.json").is_file()),
        notes_preserved=bool((run / "candidate_after_support.json")
                             .is_file()),
    )
    sel_policy = SelectionPolicy()
    cfg = _load_json(run / "run_config.json") or {}
    if cfg.get("search_policy"):
        alt = cfg["search_policy"].get("alternatives_required")
        if alt:
            sel_policy = SelectionPolicy(alternatives_required=int(alt))

    expectation = build_product_expectation(
        input_spec, deck, ontology, capability, sel_policy)
    expectation.snapshots = snapshots
    return expectation


def _content(u: dict):
    from .models import TypedContentPayload
    c = u.get("content") or {}
    if isinstance(c, str):
        return TypedContentPayload(text=c)
    return TypedContentPayload(
        text=str(c.get("text", "")),
        literals=[str(x) for x in c.get("literals", [])],
        field_tokens=[str(x) for x in c.get("field_tokens", [])],
        media_ref=c.get("media_ref"), data_ref=c.get("data_ref"),
        links=[str(x) for x in c.get("links", [])])


def _design_gate_evidence(data: dict, run: Path,
                          snapshots: SnapshotSet) -> GateEvidence | None:
    """DesignReview JSON -> visual_design gate evidence (§10.7)."""
    if not data.get("candidate_sha256"):
        return None
    dimensions = data.get("dimensions", []) or []
    sufficient = [d for d in dimensions
                  if d.get("result") == "sufficient"]
    na = [d for d in dimensions if d.get("result") == "not_applicable"]
    findings = [TypedFinding(code=f.get("code", "design_finding"),
                             severity=f.get("severity", "warning"),
                             message=f.get("message", ""),
                             scope=f.get("scope"))
                for f in data.get("findings", [])]
    for d in dimensions:
        if d.get("result") == "insufficient":
            findings.append(TypedFinding(
                code=f"design:{d.get('dimension', '?')}", severity="error",
                message=str(d.get("reason", "")), scope=None))
    verdict = str(data.get("verdict", "repair"))
    hard = [f for f in findings if f.severity in ("error", "hard")]
    # §4.2.8: unresolved hard defects forbid accept; prose cannot waive them.
    status = GateStatus.FAIL if hard \
        else GateStatus.PASS if verdict == "accept" \
        else GateStatus.FAIL if verdict in ("repair", "reject") \
        else GateStatus.UNKNOWN
    artifacts = []
    for img in data.get("image_refs", []) or []:
        p = run / img if not Path(img).is_absolute() else Path(img)
        if p.is_file():
            artifacts.append(EvidenceRef(
                artifact_id=p.name, relative_path=str(
                    p.relative_to(run)) if p.is_relative_to(run) else img,
                sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
                byte_count=p.stat().st_size,
                subject_candidate_sha256=str(
                    data.get("candidate_sha256", ""))))
    return GateEvidence(
        gate_id="visual_design", status=status,
        expected_units=["design:dimensions", "design:diversity",
                        "design:repair"],
        checked_units=["design:dimensions", "design:diversity",
                        "design:repair"] if (
                            sufficient or na) else [],
        findings=findings, artifacts=artifacts,
        implementation_id="design-review/v1", snapshots=snapshots,
        subject_candidate_sha256=str(data.get("candidate_sha256", "")))


def _candidate_ref(run: Path) -> Path:
    """Explicit manifest ref, never 'first candidate*.pptx' (§4.2)."""
    manifest = _load_json(run / "saved_package_manifest.json") or {}
    ref = manifest.get("candidate_path") or ""
    if ref:
        p = Path(ref)
        if not p.is_absolute():
            p = run / ref
        if p.is_file():
            return p
    sel = _load_json(run / "selection_report.json") or {}
    for e in sel.get("selections", []) or []:
        p = Path(str(e.get("candidate_path", "")))
        if p.is_file():
            return p
    fixed = run / "candidate.pptx"
    if fixed.is_file():
        return fixed
    raise AcceptanceInputError(
        "CANDIDATE_MISSING: no explicit candidate path in manifest")


class ProductSummary(BaseModel):
    """Convenience flags, fully DERIVED from the canonical acceptance.

    This is not a second source of truth. Every field is computed from the
    ``ProductAcceptance`` produced by ``aggregate_product_acceptance`` plus
    explicit user decisions; ``user_approved`` is never set by the model.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "product-summary/v1"
    subject_candidate_sha256: str = ""
    acceptance_schema_version: str = ""
    accepted: bool = False
    publication_eligible: bool = False
    required_scope_count: int = 0
    checked_scope_count: int = 0
    automated_gates_pass: bool = False
    failing_gate_ids: list[str] = Field(default_factory=list)
    unknown_gate_ids: list[str] = Field(default_factory=list)
    model_design_review_pass: bool = False
    model_design_review_basis: str = ""
    user_approved: bool = False
    user_approved_note: str = (
        "never set by the model; only an explicit user decision may set it")
    release_ready: bool = False
    release_ready_blockers: list[str] = Field(default_factory=list)
    derived_from: list[str] = Field(default_factory=list)


def build_product_summary(
    acceptance: ProductAcceptance,
    evidence: Sequence[GateEvidence],
    user_decision: dict[str, Any] | None = None,
) -> ProductSummary:
    """Derive the convenience summary from the canonical acceptance.

    A convenience flag can never contradict the canonical result: it is a
    projection of the same gate evidence, and any fail/unknown/missing
    scope keeps ``release_ready`` false with the reason listed.
    """
    user_decision = dict(user_decision or {})
    failing = sorted({ev.gate_id for ev in evidence
                      if ev.status is GateStatus.FAIL})
    unknown = sorted({ev.gate_id for ev in evidence
                      if ev.status is GateStatus.UNKNOWN})
    review_ids = sorted({ev.gate_id for ev in evidence
                         if ev.gate_id.startswith("visual_review")
                         or ev.gate_id.startswith("design_review")})
    review_pass = bool(
        review_ids
        and all(ev.status is GateStatus.PASS for ev in evidence
                if ev.gate_id in review_ids))
    blockers = list(failing) + [f"unknown: {g}" for g in unknown]
    if not review_ids:
        blockers.append("no model design/visual review evidence")
    if not acceptance.subject_candidate_sha256:
        blockers.append("acceptance is not bound to a subject candidate")
    user_approved = bool(user_decision.get("user_approved", False))
    return ProductSummary(
        subject_candidate_sha256=acceptance.subject_candidate_sha256,
        acceptance_schema_version=acceptance.acceptance_schema_version,
        accepted=acceptance.accepted,
        publication_eligible=acceptance.publication_eligible,
        required_scope_count=acceptance.required_scope_count,
        checked_scope_count=acceptance.checked_scope_count,
        automated_gates_pass=bool(acceptance.accepted
                                  and not failing and not unknown),
        failing_gate_ids=failing,
        unknown_gate_ids=unknown,
        model_design_review_pass=review_pass,
        model_design_review_basis=(
            "every visual/design review gate for the current candidate SHA "
            "has PASS status"
            if review_pass else
            "no current PASS verdict for every review gate: "
            + (", ".join(sorted(set(unknown) | set(failing))) or "absent")),
        user_approved=user_approved,
        release_ready=bool(acceptance.publication_eligible and user_approved
                           and not blockers),
        release_ready_blockers=blockers,
        derived_from=["product_acceptance.json",
                      "gate_index.json"],
    )


def main(run_dir: str) -> int:
    run = Path(run_dir).resolve()
    # project_root: the directory containing ontology/ (project layout),
    # falling back to the repo root two levels above this file.
    project_root = run.parents[1] if (run.parents[1] / "ontology").is_dir() \
        else Path(__file__).resolve().parents[3]
    ctx = RunContext(project_root=project_root, run_dir=run)
    snapshots = _snapshots_from_run(run)
    store = ArtifactStore(ctx.project_root)

    try:
        candidate = _candidate_ref(run)
        saved = collect_saved_manifest(candidate, ctx)
        expectation = _expectation_from_run(run, ctx, snapshots)
    except (AcceptanceInputError, ValidationError) as exc:
        rejection = ProductAcceptance(
            accepted=False,
            findings=[TypedFinding(code="INPUT_INVALID", severity="error",
                                    message=str(exc))],
            error_codes=["INPUT_INVALID"], publication_eligible=False)
        write_acceptance_atomic(run / "product_acceptance.json", rejection)
        print(json.dumps({"accepted": False, "error": str(exc)},
                         ensure_ascii=False))
        return 3

    gate_reports: list[GateEvidence] = []
    gates_dir = run / "gate_reports"
    gate_files = sorted(gates_dir.glob("*.json")) if gates_dir.is_dir() \
        else []
    # Fall back to flat gate_*.json records in run root / final.
    if not gate_files:
        gate_files = sorted(run.glob("gate_*.json"))
    for gf in gate_files:
        ev = _gate_evidence_from_json(_load_json(gf) or {}, run, snapshots)
        if ev:
            gate_reports.append(ev)
    # Also accept records embedded in gate_index.json (list or map form).
    index = _load_json(run / "gate_index.json") or {}
    existing = {e.gate_id for e in gate_reports}
    raw_gates = index.get("gates", []) or []
    if isinstance(raw_gates, dict):
        raw_gates = list(raw_gates.values())
    for g in raw_gates:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("gate", g.get("gate_id", "")))
        if gid in existing:
            continue
        ev = _gate_evidence_from_json(g, run, snapshots)
        if ev:
            gate_reports.append(ev)

    reviews: list[VisualReview] = []
    for name in ("visual_review.json", "final/visual_review.json"):
        rv = _review_from_json(_load_json(run / name) or {}, run)
        if rv:
            reviews.append(rv)

    # Design reviews feed the mandatory visual_design gate (§10.7), not
    # the page-coverage visual review list.
    for dp in [run / "final" / "design_review.json",
               run / "design_review.json"] + sorted(
                   run.glob("scenes/*/*/*/design_review.json")):
        data = _load_json(dp) or {}
        if not data:
            continue
        ev = _design_gate_evidence(data, run, snapshots)
        if ev and ev.gate_id not in {e.gate_id for e in gate_reports}:
            gate_reports.append(ev)

    selections: list[SelectionEntry] = []
    sel = _load_json(run / "selection_report.json") or {}
    for e in sel.get("selections", []) or []:
        ck_data = e.get("candidate_key") or {}
        ck = None
        if ck_data.get("run_id"):
            ck = CandidateKey(
                run_id=str(ck_data.get("run_id", "")),
                source_slide_id=str(ck_data.get("source_slide_id", "")),
                candidate_id=str(ck_data.get("candidate_id", "")),
                revision=int(ck_data.get("revision", 0)))
        elif e.get("candidate_id"):
            ck = CandidateKey(run_id=run.name,
                              source_slide_id=str(
                                  e.get("source_slide_id", "")),
                              candidate_id=str(e.get("candidate_id", "")),
                              revision=int(e.get("revision", 0)))
        selections.append(SelectionEntry(
            source_slide_id=str(e.get("source_slide_id", "")),
            candidate_key=ck or CandidateKey(
                run_id=run.name,
                source_slide_id=str(e.get("source_slide_id", "")),
                candidate_id="unknown"),
            candidate_sha256=str(e.get("candidate_sha256", "")),
            candidate_path=str(e.get("candidate_path", "")),
            considered=[str(c) for c in e.get("considered", [])],
            rejected=[str(r) for r in e.get("rejected", [])],
            reason=str(e.get("reason", ""))))

    acceptance = aggregate_product_acceptance(
        expectation, saved, gate_reports, reviews, selections,
        artifact_store=store)
    write_acceptance_atomic(run / "product_acceptance.json", acceptance)

    # Persist the derived contracts for the bundle (§14.1).
    (run / "product_expectation.json").write_text(
        expectation.model_dump_json(indent=1), encoding="utf-8")
    (run / "saved_package_manifest.json").write_text(
        saved.model_dump_json(indent=1), encoding="utf-8")
    index = {"candidate_sha256": saved.candidate_sha256,
             "gates": {e.gate_id: e.model_dump(mode="json")
                       for e in gate_reports},
             "reviews": [r.model_dump(mode="json") for r in reviews],
             "selections": [s.model_dump(mode="json") for s in selections],
             "files": {}}
    for rel in ("candidate_after_ir.json", "content_diff.json",
                "semantic_relation_report.json",
                "ontology_rule_coverage.json",
                "saved_authoring_rule_report.json",
                "saved_frame_report.json",
                "saved_geometry_report.json", "saved_geometry_diff.json",
                "saved_measurement_report.json",
                "native_editability_report.json",
                "native_edit_roundtrip_report.json",
                "layout_geometry_report.json",
                "required_visibility_report.json",
                "render/native_render_probe.json",
                "render/render_font_report.json",
                "render/render_manifest.json", "emission_report.json",
                "output_object_map.json", "selection_report.json",
                "visual_review.json", "final/design_review.json",
                "product_expectation.json",
                "saved_package_manifest.json"):
        p = run / rel
        if p.is_file():
            index["files"][rel] = {"sha256": _sha_file(p),
                                   "bytes": p.stat().st_size}
    (run / "gate_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    summary = build_product_summary(acceptance, gate_reports)
    (run / "product_summary.json").write_text(
        summary.model_dump_json(indent=1), encoding="utf-8")

    print(json.dumps({
        "accepted": acceptance.accepted,
        "findings": [f.message for f in acceptance.findings][:10],
        "error_codes": acceptance.error_codes,
        "required": acceptance.required_scope_count,
        "checked": acceptance.checked_scope_count,
    }, ensure_ascii=False, indent=1))
    return 0 if acceptance.publication_eligible else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
