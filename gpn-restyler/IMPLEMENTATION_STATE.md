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
| 4 native exporter/transplant | not started | |
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
