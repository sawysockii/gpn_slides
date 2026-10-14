# IMPLEMENTATION_STATE.md

Spec: `GPN_PPTX_Restyler_OpenCode_Architecture_and_Prompt.md` v1.2 (2026-10-05)
Task: `GPN_Restyler_Stage_1_2_Review_and_Implementation.md` v1.0 (2026-10-05)
Stage 3 task: `GPN_Restyler_Stage_3_Verification_and_Native_Operations.md` v1.0 (2026-10-05)
Last updated: 2026-10-05 — Stage 1 done, Stage 2 implementation done (hardened) + Stage 3 native-patch done/test-only; production assets needs_assets, strict output false.

## Repo

- base commit: `820782a5fb8cb8f251d7c0750e4299c321cd068c` (agent-slides, equals upstream `main` HEAD at clone time)
- current branch: `gpn-restyler`, HEAD == base == merge-base (no reset/checkout performed)
- code_root: `/Users/wysockii/Documents/gpn_slides/gpn-restyler`
- project_root: `/Users/wysockii/Documents/gpn_slides` (nested-clone layout: corpora stay in project_root, code in code_root; config/CLI resolve project_root explicitly, never via cwd)

### New files (Stage 1–3 work, vs base commit)

- `src/slides_cli/gpn/typography.py` — theme profiles, style inheritance, typeface resolution (§3); font inventory (§8)
- `src/slides_cli/gpn/ontology_conflicts.py` — normative extraction + conflict analysis (§5)
- `src/slides_cli/gpn/compiler.py` — `compile_ontology`, `write_ontology_artifacts` (§6)
- `src/slides_cli/gpn/rules.py` — `RuleEvaluationContext`, `evaluate_rule[_registry]`, `aggregate_rule_readiness`, all 26 checkers (§7)
- `src/slides_cli/gpn/template.py` — `extract_template_profile`, `derive_base_template`, `compute_content_box`, `validate_reference_style` (§9)
- `src/slides_cli/gpn/bullets.py` — LS01 profile binding + OOXML writer (§10)
- `src/slides_cli/gpn/pipeline.py` — `prepare_corporate_profiles` (§12)
- `src/slides_cli/gpn/cli.py` — `slides gpn doctor|import|profile` (stdout JSON, stderr progress)
- `tests/gpn/test_typography.py`, `test_links.py`, `test_chart_bindings.py`, `test_support.py`, `test_conflicts.py`, `test_compiler.py`, `test_rules.py`, `test_bullets.py`, `test_fonts.py`, `test_template.py`, `test_pipeline.py`
- `tests/gpn/conftest.py` — `add_fields_and_footnotes` fixture (slidenum/datetime fields, footnote marker, soft break)
- `src/slides_cli/gpn/patching.py` (Stage 3) — `resolve_native_style`, `authorize_gpn_edit`, `plan_shape_deletion`, `preflight_gpn_edits`, `apply_gpn_edits`, `compare_ledger_preservation`, `GpnEditContext`, service CLI commands
- `tests/gpn/test_stage3_native.py` (Stage 3) — 28 tests covering the §§4–11 evidence matrix
- `docs/SUPPORT_MATRIX.md`, `docs/IMPLEMENTATION_GUIDE.md` (Stage 3) — real capabilities/units/limits, final signatures, hash algorithm, text_scope semantics, facade-vs-guard split
- `scripts/stage3_run.py` (Stage 3) — reproducible artifact run (fixture → batch → CLI flows → verification JSONs)

### Modified files (vs base commit)

- `src/slides_cli/gpn/models.py` — `TextRunIR.kind/field`, `WorkbookSnapshot`, Stage 2 models (ontology/conflicts/compiler/rules/fonts/template/pipeline); `RectEMU` zero defaults; `RoleStyle.status` full literal set; Stage 3: `NormalizedPredicate`/`ScopeComparison`/`PredicateComparison`, extended `OntologyConflict.kind`, `compatible_refinements`
- `src/slides_cli/gpn/readers.py` — `a:fld` keeps `FieldMetadata` + `kind=field`; Stage 3: plot-scoped chart point IDs, one cell per merged-table coordinate, hover fallback in `_hyperlink_for`
- `src/slides_cli/gpn/importer.py` — `resolve_internal_links`, `compare_chart_bindings`, `collect_import_uncertainties`, `build_import_support_report`, keyword `write_import_artifacts` (+`import_deck_with_report`); Stage 3: `HyperlinkOccurrence` + `iter_hyperlink_occurrences`, occurrence-identity dedup (shared rIds kept), optional raw-XML shape/hover pass
- `src/slides_cli/gpn/typography.py` — Stage 3: `parse_clr_map_override`, `_color_from_scheme` honors the override
- `src/slides_cli/gpn/ontology_conflicts.py` — Stage 3: `normalize_predicate`/`compare_predicates` (Decimal bounds, scope overlap, refinement vs contradiction vs disjoint vs unresolved), full-row MD extraction
- `src/slides_cli/gpn/rules.py` — Stage 3: deterministic C04/C05/C11/C13/C14/C20 parts with `checked_scopes`
- `src/slides_cli/gpn/compiler.py` — Stage 3: 8 implemented + 7 partial bindings
- `src/slides_cli/gpn/pipeline.py` — Stage 3: coverage evaluated on the real deck+ledger; real coverage preserved through the artifacts writer
- `src/slides_cli/gpn/cli.py` — `slides gpn doctor|import|profile` (stdout JSON, stderr progress); Stage 3: scoped readiness, `--require-production-assets`/`--check-model`, `preflight-edits`/`apply-edits`
- `src/slides_cli/api.py` — Stage 3: Unicode `find_text`, `autofit_policy` + `_set_autofit`, `resolve_shape_address`/`compute_shape_xml_sha256`, `set_shape_geometry`/`set_shape_style`/`delete_shape`, typed `_apply_op` dispatch
- `src/slides_cli/model.py` — Stage 3: `AddTextOp.autofit_policy`, `NativeShapeStyle`, `SetShapeGeometryOp`/`SetShapeStyleOp`/`DeleteShapeOp` in the union
- `src/slides_cli/cli.py` — registered `gpn` subcommand only (upstream commands untouched); Stage 3: new-arg forwarding, `gpn-*` discovery methods (additive, schema-driven)
- `tests/gpn/test_import.py` — new `write_import_artifacts` API; fixed pre-existing E501
- earlier Stage 0 files: `errors.py`, `config.py`, `ontology.py`, `package.py`, `provenance.py`, `units.py`, `assets.py`, `config/gpn.example.toml`, `DEPENDENCIES.lock.json`

## Mandatory corpora (project_root) — verified hashes, unchanged after runs

| Path | Facts |
|---|---|
| `project_root/ontology/GPN_Slide_Design_Ontology.json` | version `1.2.0`, date `2026-09-21`; sha256 `d24be59d…6b7a90`; canvas 12192000×6858000 EMU; 10 typography roles; 26 unique constraints (C01–C22 hard + R01–R04 contextual; font policy repeats C21/C22, deduped); 325 catalog records (118 charts / 98 diagrams / 39 tables / 70 layouts) |
| `project_root/ontology/GPN_Slide_Design_Ontology.md` | sha256 `a9a2fe1c…c8b5058e`; C01–C22 rows agree with JSON (paraphrase-level, no blocking contradiction); external MD carries the full catalog (§§7–10) as compatible addition |
| `project_root/slide_examples/slide_examples.pptx` | sha256 `0c6d6c00…786ae0` == `metadata.source_sha256`; 99 slides |

- `ontology_corpus_hash` = `2551319e…`; `examples_corpus_hash` = `9ac309bc…` (canonical sorted `(relative_path, sha256, normative)`).
- Conflict analysis (Stage 3 classifier): `documents_differ=true` (byte-level, informational only); C01–C22 `equivalent` (full-row MD extraction: remedy columns included, so C18's 8 pt and C21's bold flag are compared, not truncated away); R01–R04 resolved singletons; `compatible_refinements=[]` on the real corpus; `ready_for_compilation=true`; no blocking conflicts. Reviewed paraphrases stay equivalent; typed rules now distinguish `equivalent` (x=80/x=80), `compatible_refinement` (x≥8/x=8, x≥8/x=9, x≥8/x≥10 — never auto-tightened, never `equivalent`), `contradiction` (x≥8/x≤7, x=80/x=90, must/must_not, b-flag flips), `disjoint_scope` (body vs footnote), `unresolved` (conditional sky without evidence, unknown predicates — blocking, no ID-only fallback).
- No `project_root/config/gpn.toml` exists (only `code_root/config/gpn.example.toml`); `slides gpn` therefore runs config-free with explicit `--project-root`. This is a fact, not a failure: corpus paths resolve canonically either way.

## Dependencies

Installed/verified: uv 0.9.8, Python 3.12.9 (uv), python-pptx 1.0.2, lxml 6.0.2, pydantic 2.12.5, httpx 0.28.1, openpyxl 3.1.5, fonttools 4.66.1, Pillow 12.1.1, pytest 9.0.2, ruff 0.15.4, Node 24.14.1 (≥22, unchanged), LibreOffice 26.2.3.2, Poppler 26.04.0, tesseract 5.5.3 + tesseract-lang 4.1.0 (rus/eng/osd verified).
See `DEPENDENCIES.lock.json`. No new dependencies were needed (fontTools already locked).

**Missing corporate assets (honest blockers, §1.3) — unchanged:**
- GPN_DIN font binaries (`assets/fonts/`) — not provided ⇒ `assets_ready=false`, `strict_output_ready=false`, profile exit 3.
- No standalone corporate master; base template derived structurally from the reference library into run/cache (manifest recorded).

**Model endpoint:** `model_id` left empty (§1.2). Not required for this work: import/rules/typography/checks are deterministic.

## Stage status

| Stage | Status | Notes |
|---|---|---|
| 0 bootstrap/corpus/deps/baseline | **done** | baseline confirmed: 198 passed upstream-inclusive full suite basis |
| 1 models/units/package/importer/readers/ledger + themes/links/bindings/reports | **done (hardened in Stage 3 run)** | theme profiles per slide (layout→master→theme, never global theme1); per-property inheritance with provenance (`b="0"` is explicit false); `a:fld` → `FieldMetadata` + order; owner-scoped OPC link resolution; cache↔workbook comparison with blocking `Uncertainty`; support/link artifacts; fixed in Stage 3: chart point IDs are plot-scoped (no cross-plot collisions), merged table grid emits exactly one cell per coordinate (no re-emits); hover-only run links kept; `clrMapOvr` remaps scheme colors |
| 2 ontology compiler/font/template/list + CLI/pipeline | **implementation done, production assets needs_assets** | compiler works on agreed corpus (canvas/10 roles/26 IDs/325 records); 8 deterministic + 7 partial checkers with honest deferred scopes (was 11 implemented incl. vacuous parts), rest deferred-unknown; coverage is now evaluated against the real imported deck+ledger (was vacuous no-IR); pipeline no longer overwrites real `rule_coverage.json` with `[]`; real-corpus coverage: 26 rules → 7 pass / 7 fail (honest library-content findings: off-palette demo colors/fonts, off-canvas demo objects, manual bullets, synthetic bold in demos) / 12 unknown; font inventory by internal names; structural template derivation (reopens, shared parts intact); LS01 binder + idempotent writer; `slides gpn doctor` exit 0 with scoped readiness; `profile` exit 3 with sole blocker `FONTS/GPN_FONTS_MISSING` |
| 3 api/model/CLI facade fixes | **done, test-only diagnostic scope** | Unicode `find_text` (Cyrillic/mixed, Ё/Е distinct); explicit autofit (`legacy_shrink` default, `none` = single `noAutofit`); typed `set_shape_geometry`/`set_shape_style`/`delete_shape` with `(slide, group_path, shape_id)` addressing + canonical `c14n-exclusive-v1` hashes + stale refusal + transactional rollback; GPN guard/preflight/apply with ledger+policy gates; `doctor --require-production-assets` (exit 3), `preflight-edits`/`apply-edits` service commands; diagnostic candidate saved + independently reimported; `strict_output_ready=false` |
| 4 native exporter/transplant | **done** (diagnostic scope) | native.py (1188 lines), transplant.py (523), opc_adapter.py (420), export_pipeline.py (1369), reference_guard.py (256); 43 tests in test_stage4_exporter.py; build works, list markers applied, candidate saved/reimported; render_verified=false, powerpoint_ui_edit_verified=false, strict_output_ready=false |
| 5 measurement/layout | not started | |
| 6 reference index/semantics/planner | not started | (`validate_reference_style` minimal gate exists; full index is Stage 6) |
| 7 QA/repair/pipeline/from-ppt | not started | |
| 8 document workflow | not started | |
| 9 skill/docs/pilots | not started | |

## Tests

- Full suite (`uv run pytest --no-cov -p no:cacheprovider`): **284 passed, 0 failed** (~60 s). Baseline 198; +58 Stage 1–2, +28 Stage 3 (`tests/gpn/test_stage3_native.py`), no regressions.
- `tests/gpn/`: **126 passed, 0 failed**.
- `uv run ruff check .`: **clean**.
- Negative/positive cases with independent expectations throughout, incl. Stage 3: predicate table (≥8/==8 refinement vs ≥8/≤7 contradiction), b-flag flip, lost sky condition, MD-only rule preservation, unparsed blocking, duplicate/missing ledger atoms, fake bullets, raster-vs-native, shared-rId links, click+hover, stale-hash refusal, connector-dependent delete refusal, partial-batch rollback with `committed_count=0`, opaque-part loss detection.
- Real-corpus negative evidence: hardened checkers report 7 honest fails on the demo library (off-palette demo colors/fonts, off-canvas demo objects, endpoint-less demo connectors, manual bullets, synthetic bold in demos) — library-content findings, not code regressions; `profile` status stays `needs_assets` with the sole asset blocker.

## Support matrix (real library import, run 2026-10-05-05)

1869 objects, `source_import_complete`, `source_data_verified=true`, 0 blocking uncertainties, 0 unresolved links:
`text:511, shape:879, table:27, chart:43, connector:126, group:100, image:71 native_full; unknown:112 unsupported` (graphicData beyond the supported reader set, preserved as raw XML + extracted text).

## Last runs

- `runs/stage1-stage2/2026-10-05-03/audit_before.json` — pre-work audit of the Stage 1–2 run.
- `runs/stage1-stage2/2026-10-05-04/` — `gpn doctor` exit 0 (corpus ready, no blocking conflicts).
- `runs/stage1-stage2/2026-10-05-05/` — `gpn profile` exit 3 (`needs_assets`): 18 artifacts — `audit_after.json`, `source_ir.json`, `source_ledger.json`, `source_package_manifest.json`, `support_report.json`, `link_resolution.json`, `ontology_manifest.json`, `ontology_conflicts.json`, `compiled_rules.json`, `rule_coverage.json` (empty — vacuous no-IR coverage, fixed in Stage 3), `model_rule_digest.md`, `examples_manifest.json`, `font_inventory.json`, `template_profile.json`, `template_derivation.json`, `list_profile.json`, `profiles/base_template.pptx` (reopens, exact canvas, shared masters/layouts/themes intact), `stage2_readiness.json`.
- `runs/stage3/2026-10-05-01/` — probes (`doctor-probe`, `doctor-assets`).
- `runs/stage3/2026-10-05-02/` — Stage 3 run: `fixture_source.pptx` (2 native slides: RU title/table/chart/notes/group) → `operation_batch.json` (move + uniform style patch) → `doctor` exit 0 / `doctor --require-production-assets` exit 3 / `find добыча` exit 0 / `preflight-edits` exit 0 / `apply-edits` exit 0 / upstream `slides apply` exit 0; `diagnostic_candidate.pptx` reopens (title at 1.5"/0.7" exact EMU, 14 pt `7E7E7E`, single `noAutofit`); `profile/` exit 3 with sole blocker `FONTS/GPN_FONTS_MISSING` and real 26-rule coverage (7 pass / 7 fail library-content / 12 unknown); `preflight_report.json`, `operation_report.json`, `candidate_after_{ir,ledger,support,links}`, `preservation_diff.json` (no missing atoms, data equal), `package_diff.json` (no removed parts), `output_object_map.json`, `edit_authorizations.json`, `ontology_conflicts_v2.json`, `rule_coverage_v2.json`, `stage12_claims_verification.json`, `stage3_{audit_before,audit_after,readiness}.json`, `commands_and_results.json`.
- Source corpora hashes re-verified unchanged after the runs (`d24be59d…`, `a9a2fe1c…`, `0c6d6c00…`).

## Readiness

- presentation workflow: **partial** (import + rules + profiles + diagnostic native patch work; full export/planner/QA are later stages)
- document workflow: **not ready** (Stage 8)
- code environment: ready (`code_environment_ready=true`)
- rules compiled on agreed corpus: yes; corpus conflicts clear (22 equivalent C01–C22, 0 blocking; R01–R04 resolved singletons)
- source import/data verification: per-run via support reports (fixture candidate: no missing atoms, chart/table/field/link/notes equal, no removed parts)
- production assets: **not ready** (`production_assets_ready=false`, `FONTS/GPN_FONTS_MISSING`)
- strict GPN export: **false** (`strict_output_ready=false` — no full exporter/layout/render yet), never implied by diagnostic exit 0

## Next concrete step

Stage 4: native exporter/transplant (tables/charts/notes/links) on top of the Stage 3 addressing/hash/preservation contracts. Do not start automatically in this run.

## Open semantic/style assumptions

- Reviewed JSON↔MD paraphrases (same predicate/modality/numbers/conditions, full-row comparison incl. remedy columns) are `equivalent`; typed contradictions (`must`/`must_not`, incompatible measures, b-flag flips), lost conditional applicability (sky/cyan), and unknown predicates surface as blocking `contradiction`/`unresolved` instead of being silently absorbed; `compatible_refinement` (x≥8/x=8) preserves both norms and never auto-tightens corporate style.
- Deferred/hybrid checkers (C06–C09, C12, C15, C17, C18, R01–R03) return honest `unknown`; partial checkers (C03–C05, C11, C13, C14, C20) execute their deterministic native parts and defer render/semantic/index scopes explicitly; semantic/render coverage is not claimed.
- `slide_examples.pptx` serves as both reference library and template-derivation source; derivation manifest + source-hash invariance are recorded per run. The demo library itself is not rule-clean (hardened checkers report its off-palette demo colors/fonts, off-canvas demo objects, manual bullets, synthetic bold) — these are library-content findings, not pipeline regressions.
- Condensed-Italic +bold exception is the only allowed bold flag; photo pixels are out of palette scope by checker design (author fills/lines/text only).
- EA/CS theme aliases (`+mj-ea`, `+mn-cs`) resolve via the font scheme; raw `+mj-lt` typefaces surviving in IR text runs are reported by C02 rather than silently accepted.

## Repo layout update (2026-10-07, appended; lines above kept as the historical record)

- The former nested clone (`gpn-restyler/.git`, remote `mpuig/agent-slides`, branch `gpn-restyler` at base `820782a`) was **absorbed** into the single project repository: `gpn-restyler/` is now a plain directory tracked by `master` of `sawysockii/gpn_slides`. The lines above describing `current branch: gpn-restyler` / `HEAD == base` were true until 2026-10-07 and are kept unchanged.
- Upstream history is preserved locally as `agent-slides-upstream-history.bundle` (31 MB, gitignored, not pushed).
- Uncommitted Stage 1–3 work (modified `api.py`/`cli.py`/`model.py`/`pyproject.toml`/`uv.lock`, new `gpn/`, `tests/gpn/`, `docs/`, `scripts/`, `config/`, locks) is now committed in the project repository.
- Corpora (`ontology/`, `slide_examples/`) and `runs/` remain local-only (gitignored), per user decision; only code is pushed.

## Portability + test state (2026-10-07, appended; lines above kept as the historical record)

- Absolute `/Users/wysockii/...` paths replaced with `Path(__file__).resolve().parents[…]` in 8 `tests/gpn/*` files and both `scripts/stage3_*.py` runners; `src/` never had hardcoded roots. A fresh clone + `uv sync` + copied corpora is sufficient to work elsewhere.
- `tests/gpn/test_stage35_ontology.py` (written in an earlier session) imported `resolve_role_style` which did not exist → **pytest collection failed and the whole suite could not run**. Implemented `compiler.resolve_role_style` (spec §4.3): delegates to `patching.resolve_native_style` (single resolution path), unknown role = typed error, `sources` is a provenance gate (`ONTOLOGY/SNAPSHOT_SOURCE_MISMATCH` on corpus-hash mismatch).
- Stage 3.5 is **partially implemented** (compiler `COMPILER_VERSION="stage3.5/1.0"`, `planner.py`, `planning_bridge.py`, `references.py`, `local_llm.py`, runs `stage3_5/2026-10-05-01|02`) — earlier lines saying Stage 3.5 was spec-only were written before this state was known.
- Full suite 2026-10-07: `uv run ruff check .` clean; **291 passed, 12 failed** (~56 s). All 12 failures are in `test_stage35_ontology.py`: test-side mutations use spec field names (`default_size_pt`, `hex`) vs real corpus names (`size_pt`, `value`), plus not-yet-implemented engine behavior (empty ontology does not raise `AssetMissingError`, `value_pt=-1` compiles without error). The other 284 tests + 7 passing Stage 3.5 tests are green. Fixing the 12 = Stage 3.5 continuation, not started automatically.

## Stage 3.5 completion (2026-10-07, appended; lines above kept as the historical record)

- The "291 passed, 12 failed" state above is resolved: **303 passed, ruff clean**
  (2026-10-07, ~62 s), including 19/19 `tests/gpn/test_stage35_ontology.py`.
- Hardcode audit: no normative literals in production code; all values flow from
  the JSON snapshot (`runs/stage3_5/2026-10-07-audit/hardcode_audit.json`,
  `runtime_callgraph.md`). §11.2 tables describe the current-ontology snapshot,
  not application constants.
- Engine fixes (all covered by tests): minimum `value_pt<=0`/unparseable raises
  (`compiler.py`); raised minimum vs unchanged role sizes is a blocking
  contradiction (`ontology_conflicts.py`); intent schema embeds
  `x-ontology-corpus-hash`/`x-ontology-source-hash` so corpus changes invalidate
  schema hashes and plan caches (`planner.py`); `provider` accepted in
  `[model]` config (`config.py`); candidate reimport publishes
  `candidate_after_*` and never clobbers `source_ir.json` (`patching.py`).
- Runtime LLM per amendment 1.0 (harness provider, no local server): 5 real
  generations consumed via `model_requests/`→`model_responses/` with
  `--no-plan-cache`, origins `harness_model`, 3/3 valid + distinct diversity
  signatures on slide 1, `selected_pending_measurement` everywhere.
- Diagnostic bridge: intent → 10 ops → `diagnostic_candidate.pptx`, reimported
  natively, 0 missing atoms, package 40/40, texts preserved
  (`runs/stage3_5/2026-10-07-smoke/`, `stage3_5_readiness.json`).
- Honest flags unchanged: `production_assets_ready=false` (GPN fonts missing),
  `strict_output_ready=false`, `render_verified=false`, `visual_validated=false`.
- Known workflow constraint: `profile` overwrites `source_ir.json` with the
  reference deck; run order is import → profile → import (recorded in readiness).
- Pre-existing uncommitted changes (`bullets.py`, `errors.py`, `template.py`)
  were left untouched.

## Stage 5 measurement/layout (2026-10-10, appended; lines above kept as the historical record)

- Full suite: **409 passed, exit 0**; `uv run ruff check` clean.
- New modules: `gpn/composition.py` (intent→component tree, operator bindings,
  intent-declared nest pairs, stale source-allowance pruning note),
  `gpn/layout.py` (ENGINE `layout/1.2`; hint-directed recompose, zone
  subdivision, overlap repair, expand_into_free, residual-overlap gate,
  per-slide + global budgets, table grids via `primitives.layout_table`,
  layout cache keyed with engine version), `gpn/measurement.py` (Node/Chromium
  bridge, canonical hashes, UTF-16 mapping), `gpn/measured_build.py`
  (`run_measured_build`, saved-geometry + grid gates, saved remeasurement,
  canvas-aware geometry validation, LibreOffice render probe + calibration),
  `gpn/primitives.py` (table/graph/chart-exhibit/image/annotation resolvers),
  `tools/layout/src/measure.js` (protocol 1.0, exact FontFace bytes, no net).
- Exporter fixes on the Stage 4 path: notes 1:1 `a:br` round-trip; decor
  autoshapes/connectors with bound diagram_node atoms; corporate latin
  typeface incl. `a:fld`; group xfrm (rot/flip) preservation; free-connector
  lines; resolved table grids applied + verified; endpoint-less connectors
  atomized as geometry (provenance).
- Fonts: 10 GPN faces + Wingdings discovered system-wide; production bundle,
  browser-verified and native-renderer-verified in both pilots
  (`production_assets_ready=true`).
- Pilots (`runs/stage5/2026-10-09-a/`): `pilot-ntc` (real NTC slide05,
  recompose: c0 resolved/exported, c1/c2 honest infeasible; candidate
  `82798350…`) and `pilot-compound` (synthetic 4-slide technical fixture incl.
  hidden slide; preserve: 4/4 resolved; candidate `53d3b9b6…`). Both:
  saved_geometry/content/parts/workbook verified, native PDF+pages rendered,
  visual review `reviewed` with page hashes.
- Readiness: `strict_output_ready=false`, `powerpoint_ui_edit_verified=false`;
  open: container-aware text color, chart auto-placement (render-only),
  word-level precision, furniture collisions, connector line styling,
  group-child autoshape geometry, chart/graph/group recompose resolvers.

## Stage 5 recovery (2026-10-11, appended; lines above kept as the historical record)

- Previous pilot `runs/stage5/2026-10-09-a/pilot-ntc` reconciled to
  `failed_partial`: only 1 of 8 slides exported; candidate `82798350…`
  contains C01 (0C539F fill), C03 (effectRef outerShdw), C21/C22 (synthetic
  bold), surface-foreground, layout "Контакты", and incomplete-scope defects.
- Root causes fixed in code:
  - `opc_adapter.py: blank_slide_layout` — evidence-based layout selection
    (fewest placeholders, no contact penalty); previous hardcoded index=6
    selected slideLayout7 "Контакты".
  - `opc_adapter.py: strip_shape_effect_inheritance` — removes p:style
    effectRef and writes explicit empty a:effectLst; theme outerShdw no
    longer inherited.
  - `native.py: _apply_decor_fill_line` — normative no-fill for decorative
    autoshapes; palette check for connector lines.
  - `native.py: _style_run` — sets font.bold=False, explicit RGB foreground,
    exact face from role; source bold/italic no longer passes through.
  - `native.py: emit_shape` — roundRect normalized to rect with
    GEOMETRY_NORMALIZED issue.
- Artifacts: `runs/stage5-recovery/2026-10-11-000709/` —
  `status_reconciliation.json`, `rule_application_trace.json`,
  `failure_reproduction.json`.
- Tests: `test_stage4_exporter.py` + `test_stage3_native.py` 74 passed;
  `ruff check` clean.
- Full-deck export verification pending: new build without
  `pilot_selection.json` required to confirm all 8 slides exported.

## Stage 5 recovery verification (2026-10-11, appended; lines above kept)

- Root-cause fixes verified end-to-end (recovery §2–§5):
  - `export_pipeline.compare_export_preservation`: authorized
    roundRect→rect decoration normalization accepted (was false `changed`).
  - `rules._check_c03`: surviving `a:effectRef` fails (theme shadow
    inheritance); explicit empty `a:effectLst` passes (normative stripped
    state, was false positive).
  - `rules.check_saved_authoring_rules` (new): C01/C03/C21_C22/
    surface_foreground on reopened candidate bytes, values from snapshot.
  - `_emit_resolved_slides` tail writes `saved_authoring_rule_report.json`;
    hard fail downgrades `completed` → `needs_review` (operation vs quality
    separated, §8).
- New checker on bad candidate `82798350…`: hard_fail=true —
  C01 8 / C03 8 / C21_C22 10 / foreground 22 violations (§9 proof).
- Fixed control slides (same source, same single-c0 plan, fixed code):
  - slide6 «Предложение по работе с ДО» → `slide6-fixed/candidate.pptx`
    (`6eb68a5b…`): completed, preservation true, content_diff 0/0/0,
    saved-authoring 4×pass; bad-vs-fixed bytes: layout7→layout5,
    roundRect 3→0, 0C539F 6→0, effectRef 5→0, b=1 10→0, colorless 22→0.
  - slide3 «ГГД» → `slide3-fixed/candidate.pptx` (`66e08767…`):
    completed, preservation true, content_diff 0/0/0, 4×pass.
- New tests `tests/gpn/test_stage5_recovery.py`: 8 passed (§9: bad/fixed
  fixtures, effectRef-without-effectLst, empty-effectLst, fresh-shape
  strip, late-writer override, Contacts rejection ×2).
- Suites green: recovery(8) + stage4(46) + stage3-native(28) + rules;
  `ruff check .` clean.
- Honest pending: 6 slides without accepted scene plans (1,2,4,5,7,8);
  full 8-slide candidate; visual review + native render with real GPN
  fonts on Mac; `aggregate_product_acceptance` (§8) not yet a function.

## Diagnostic geometry root cause + saved geometry gate (2026-10-11, appended; lines above kept)

- Root cause (opencode prompt §1): `resolve_slides_from_intents` added
  `_default_box(len(geometry))` per source object (`y=by+index*slot`,
  slot 822960 EMU) after valid zone boxes; `emit_slide` consumed
  `geometry[obj.id]` (synthetic row) instead of the zone box. Title
  y=15636240 EMU (slide6), uniform full-width rows on slide3 — confirmed
  by `gpn/geometry.py:trace_geometry_application` (first offence
  `resolved.geometry_emu[source_id]`).
- Fix: zone-bound objects take the zone box (single ref) or an explicit
  vertical equal-share subdivision (multi-ref); no synthetic rows;
  unbound objects get no fabricated coordinates (in-canvas fallback +
  `GEOMETRY_BINDING_MISSING`/`UNBOUND_GEOMETRY_NEEDS_REPAIR`). New module
  `gpn/geometry.py` (`zone_box_to_emu`, `upgrade_legacy_bindings`,
  `trace_geometry_application`, `check_saved_geometry`,
  `check_required_content_visibility`).
- Gates: `_emit_resolved_slides` writes `saved_geometry_report.json`;
  `diagnostic_geometry=true` → `DIAGNOSTIC_GEOMETRY_REJECTED` +
  `needs_review` even on style pass; fully-outside content → hard fail;
  silent `if not assets_ready: completed` override removed; `emit_slide`
  emits typed `GEOMETRY_BINDING_MISSING`.
- Proof on immutable old bytes: `6eb68a5b…` 20/20 outside (fail),
  `66e08767…` 56/57 outside (fail); refix
  `runs/stage5-recovery/2026-10-11-163026/slide6-refix/candidate.pptx`
  (`aca68f9f…`) raw geometry pass (0 outside, 20 checked), title shares
  `z-title` box, trace clean; export status honestly `needs_review`
  (diagnostic path blocked from user acceptance per §8).
- Tests: `tests/gpn/test_geometry_binding.py` 6 passed; affected batch
  (geometry+recovery+stage4+stage3-native) 89 passed; `ruff check .` clean.
- Pending: full 8-slide harness scene plans + measured build/render/
  visual review/selection/acceptance; contextual surface policy
  (`_style_run` default, blanket no-fill, effective C03); `create/resume`
  CLI; document input mode.

## Full-deck recovery increment (2026-10-11, appended; lines above kept)

- Asset retrieval: `assets.resolve_asset_bytes` (store → source-OPC
  hash-match → REQUIRED_ASSET_UNRESOLVED); `emit_image` + call-site fix.
  Proof: slide6 `content_diff.ok=true`, `p:pic` restored (21 objs),
  `9530a56c` logo bytes in candidate media.
- Saved-geometry gate rewritten (actual canvas/order/hidden; all drawable
  kinds; rot/flip polygons; group chains; partial-clip fail; connector
  segments; unknown blocking). §2.2 four false-passes are regression
  tests now failing as required.
- Aggregation: visibility unknown blocks; expected-first diff;
  monotonic verified flags; per-slide layout scoping (was 3236
  cross-slide false pairs).
- Binding: single `upgrade_legacy_bindings` algorithm; `_default_box`
  deleted; full stable node IDs.
- Runs `runs/stage5-recovery/2026-10-11-183855/`: `slide6-refix2/`
  (image proof + render + visual review `repair`), `fulldeck/` (8
  harness c0 plans, candidate `5fff0613…`, gates green except honest
  diagnostic/visual blocks, 8-page render + review), `measured/`
  (Chromium candidate `3b85a04d…`, diff ok, 124 remeasure overflows,
  per-slide layout fail), `review_increment.zip` (69 files + manifest).
- Tests: +7 geometry regressions green; affected suites 30 + 75 passed;
  `ruff check .` clean; full suite running (`/tmp/gpn_full_pytest.log`).
- Honest non-goals this increment: product_accepted=false (overlaps on
  pp.2,3,4,7,8; white-on-white logo p.6); no c1/selection yet; no
  create-mode; `strict_output_ready=false`, `powerpoint_ui_edit_verified=false`.

## Stage 6 layout repair loop (2026-10-12; appended, prior lines intact)
- Root cause traced (Chromium replay, no cache): Oct-11 `preserve_structure`
  solver kept source boxes (249/327 incl. slide6 `01`); current recompose
  path verified hint-driven (`solver==hint`, `01` one line, exact GPN face).
  Traces: `runs/stage6-layout-repair/2026-10-12-140949/traces/`.
- Engine: `layout` 1.3→1.7 (recompose-for-both-modes; decor-decor residual
  skip; coverage-gap fail; reading-order children; preserve builders
  restored); validator overlap slop aligned to solver (0.75pt);
  reproducible zip timestamps; `ContentDiff.omitted_authorized`;
  authorized asset replacement + authorized omits in comparator;
  expected-first visibility wired (`required_visibility_report.json`);
  central `acceptance.py` (exit 0/3).
- Product: 10 c1/c2 scenes (harness, lineage kept); all candidates
  resolved; content_diff ok (136 omitted_authorized); remeasure 0
  overflows; layout_geometry pass; visibility pass; 8/8 PNG reviewed by
  harness; c2 variants for slides 3+6 rendered/reviewed/rejected with
  reasons; `product_acceptance.json` accepted=true (12/12); published
  `NTC_business_AI_GPN_corporate.pptx` (sha `162b9684…`); native edit
  roundtrip proven (UI scope stays unverified).
- Skill: `skills/gpn-restyle/` (SKILL.md + 3 references, links checked).
- Bundle: `runs/stage6-layout-repair/review_increment_next.zip`
  (109 files + manifest, complete).
- Tests: `tests/gpn/test_stage6_repair.py` (+8 green); full suite +
  `ruff check .` clean (see development_history increment).
- Known non-goals carried forward: ResolvedTextSpec/emit_scene/span
  bindings/restyle_runner/create-smoke as code; frame unverified;
  core.xml dcterms nondeterminism; PowerPoint UI editing.

## Stage 7 addition (2026-10-12/13) — old lines above kept as history

- Acceptance: typed expected-first evidence-aware `gpn/acceptance.py`
  (build_product_expectation/collect_saved_manifest/validate_evidence/
  aggregate_product_acceptance; 17 error codes; visual_design required
  gate; FRAME_UNVERIFIED). Old dict-only API removed; stage6 acceptance
  test migrated to typed contracts.
- Semantics: `gpn/semantics.py` (immutable projection with own hash;
  can_omit_source_object; relation preservation check). Emission path
  consults ledger; arrow shapes never auto-decor. 9 NTC arrows assessed
  individually as required relations (runs/stage7-acceptance/
  arrow_relation_assessment.json).
- Design: `gpn/design.py` (brief/concepts/fidelity/scene/review/compare/
  diversity-by-topology) + user_feedback.json (162b9684 rejected,
  flat_text_dump). Same bytes cannot be re-accepted.
- Runner: `gpn/restyle_runner.py` (durable state, task queue, response
  validation, budgets, invalidation, assembly check, guarded publish).
  CLI: `slides gpn restyle/create/resume/status/bundle` (exit 4 =
  awaiting_harness). Ingest: `gpn/document_ingest.py` (PDF via
  pypdf==6.0.0, DOCX via python-docx==1.2.0, MD/TXT/prompt).
- Bundle: `gpn/review_bundle.py` (required-role closure, self-inspect).
  Roundtrip: `gpn/edit_roundtrip.py` (changed text+geometry after reopen,
  media/workbook byte stability; UI stays unverified).
- Verified via public CLI (awaiting_harness): NTC restyle, new mini PPTX
  (72383b04), PDF create (2 blocks), DOCX create (4 blocks + table),
  prompt create. New tests: stage7_{acceptance,design,semantics,runner,
  cli_bundle,roundtrip} (64 tests green); ruff clean.
- Open debts: full NTC rebuild through new runner (harness task
  execution pending); resolve_frame_profile; rule-application coverage;
  calibration pairing; ResolvedTextSpec/emit_scene/span-bindings;
  skill-loader discovery (outside project, unverified).

---

## Этап 8 — факты поверх stage 7 (дополнение 2026-10-13; предыдущие строки не изменены)

### Добавлено в код
- `restyle_runner._materialize_semantic_deck()`: движок сам валидирует ответ модели
  (`SemanticDeck`), пишет `inputs/semantic_deck.json` (собственный sha) и
  `classification_and_transformation_ledger.json` (решение на каждый объект исходного
  IR; unknown object / omit без причины / стрелка без `target_relation_ids` → findings).
  Snapshot авторизации = sha256 байтов `source_ir.json`.
- `advance_run`: `UNKNOWN_OR_CONSUMED_TASK` больше не порождает reissue (очередь не
  размножается); reissue остаётся для устаревшего содержимого.
- `design.generate_visual_concepts`: контракт ответа `{"concepts": [...]}`.
- `document_ingest.source_object_inventory()`: пообъектная инвентаризация исходника в
  пакете `semantic_analysis`.
- `ResolvedSurfaceStyle`: `LayoutZone.surface_token / foreground_token / stroke_token`
  → `planning_bridge` → `export_pipeline.emit_slide` → `native`:
  `_apply_decor_fill_line` красит только объявленной палитровой заливкой,
  `emit_text` красит текст на поверхности, `emit_free_connector` красит связь и
  выставляет наконечники (`_set_connector_arrows`). Неизвестный токен →
  `SURFACE_TOKEN_UNKNOWN`, не молчаливый цвет.
- `layout`: группа с одинаковым боксом, у которой **каждая пара** объявлена в
  `allowed_overlaps`, сохраняет со-локацию (`nest_cluster`) вместо вертикальной стопки.

### Фактическое состояние NTC-прогона
- Модель текущей сессии исполнила 41 задачу (semantic → briefs → concepts → scenes),
  24 автора сцены по 8 слайдам.
- Семантика: 174 обязательные единицы, 327 решений трансформации.
- **Продукт не принят, файл не опубликован**: measured-layout возвращает
  `infeasible` на всех 8 слайдах (`stage8_evidence.json`). Причина —
  плотность: один нативный текстовый блок на исходный объект плюс шаг стопки 7.2 pt
  не помещаются в верифицированный content band при корпоративных кеглях.

---

## Этап 8.1 (2026-10-14) — исправление ложной «плотности»

### Что изменилось в коде
- `planner.validate_scene_response(...) -> SceneValidationReport` — реальная
  проверка payload-схемы, уникальности `zone_id`, candidate/slide, покрытия
  required units/relations, role/surface-токенов из загруженного JSON, конечности
  геометрии, привязки к concept. `restyle_runner` вызывает её **до** записи
  `scene_validated`; провал → `schema_invalid` + finding `SCENE_SCHEMA_INVALID`.
- `restyle_runner._scene_generation_task` включает полный `visual_concept` в пакет
  (раньше модель получала только `concept_id`/`concept_key`).
- `composition._is_stroked_path()` / `_stroke_width_pt()` — stroked path
  определяется по `geometry_token='line'` либо по фигуре без текста с нулевой
  высотой; толщина обводки берётся из `source_line.width_emu`.
- `layout._resolve_hint_overlaps(..., node_kinds=, stroke_widths=)` — коннекторы
  занимают envelope обводки, а не высоту зоны; `no_room_below` помечается
  `directional_search_only`.
- `layout.place_side_by_side()` — двумерный поиск свободного места (только перенос,
  измеренный размер не меняется) при исчерпании сдвига вниз.
- `composition` группирует текстовые объекты одной авторской зоны в `rich_text`
  компонент; `layout._rich_text_paragraphs` собирает абзацы с `source_ref`;
  `native.emit_rich_text_block` пишет один `p:sp`/`txBody`;
  `export_pipeline.emit_slide` стал target-driven (сначала authored blocks, затем
  per-source loop с пропуском слитых span'ов).
- `LayoutResolutionResult.status` расширен: `schema_invalid`,
  `unsupported_representation`, `constraint_conflict`, `needs_recompose`,
  `search_exhausted`.

### Фактическое состояние NTC-прогона (обновлено)
- Ран `runs/stage7/2026-10-14-layout-recovery/`, реальный public runner.
- **3 из 8 слайдов `resolved`** (слайды 1, 6, 8); ранее 0 из 8.
- Слайд 6 (кейс §1.2 спецификации) решается.
- Остальные 5 — честные локальные статусы с конкретными дефицитами
  (1.5–33.0 pt), без заявления о плотности материала.
- Абзацы в нативном выводе (12/23/34) точно совпадают с исходником.
- Рендер: LibreOffice 26.8.1.1 отрендерил PDF; page images и шрифтовая
  инвентаризация требуют poppler (`pdffonts`/`pdftoppm`), установка не завершена.

### Тесты
- `uv run pytest tests/gpn --no-cov` → **363 passed, 3 skipped** (было 352).
- Новый `tests/gpn/test_stage9_layout_recovery.py` — 14 причинных регрессий (§10.1).
- `ruff check src/ tests/` → clean.
