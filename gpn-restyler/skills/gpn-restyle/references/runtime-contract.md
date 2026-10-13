# Runtime contract (gpn-restyle)

Verified against the stage7 runner (2026-10-12). Paths are relative
to `project_root` unless stated otherwise. The `slides` entry point is
`gpn-restyler/.venv/bin/python -m slides_cli.cli`.

## Entry points (public routes, Stage 7 §11)

```text
# restyle a presentation through the durable runner:
slides gpn --project-root <root> restyle NTC_business_AI_GPN.pptx \
  --llm-provider harness --output NTC_business_AI_GPN_corporate.pptx
# exit 4 = awaiting_harness (pending task packets in model_requests/);
#   service the tasks, then:
slides gpn --project-root <root> resume runs/<stage>/<run_id> \
  --response model_responses/<task>.json
# exit 0 = accepted user result only; exit 3 = blocked (see findings)

# create from document/brief (same engine after narrative planning):
slides gpn --project-root <root> create INPUT.pdf \
  --llm-provider harness --output report_corporate.pptx
slides gpn --project-root <root> create --prompt-file BRIEF.txt \
  --llm-provider harness --output brief_corporate.pptx

# inspect / package:
slides gpn --project-root <root> status runs/<stage>/<run_id>
slides gpn --project-root <root> bundle runs/<stage>/<run_id> \
  --output review_increment_system.zip
```

Service commands (`doctor/import/profile/plan/build/layout`) remain for
diagnostics; user results go through `restyle/create/resume` only.
`local_server` provider returns an explicit deferred result (exit 3) —
no silent fallback, no network model calls from Python, no API keys.

## Invariant inputs (never modified)

## Invariant inputs (never modified)

- input PPTX (hash recorded in `run_manifest.json`)
- `ontology/` JSON + md, `slide_examples/` (only allowed design corpus)
- installed font binaries, `base_template.pptx` lineage

Outputs always go to a NEW `runs/<stage>/<run_id>/` with parent refs.

## Run directory contract

```text
runs/<stage>/<run_id>/
  run_manifest.json            # source hash/path, parent run, policies
  source_ir.json / source_ledger.json / compiled_rules.json
  template_profile.json / font_inventory.json / list_profile.json
  base_template.pptx  assets/  (copies by hash from parent or fresh import)
  plans/slide<N>-c<M>.json     # accepted intents + plan_lineage.json
  asset_overrides.json         # approved brand-variant swaps (optional)
  classification_and_transformation_ledger.json   # authorized omits
  composition/ layout/resolved/ layout/attempts/
  measurement/ (debug html/png per request)
  candidate.pptx  emission_report.json  output_object_map.json
  candidate_after_ir.json  content_diff.json  part_preservation_diff.json
  saved_geometry_diff.json  saved_measurement_report.json
  saved_authoring_rule_report.json  layout_geometry_report.json
  native_editability_report.json
  render/candidate.pdf  render/page-<1..8>.png  render/*probe*.json
  candidate_evaluations.json  repair_history.json
  runner_transition_log.jsonl  model_task_manifest.json
  traces/<slide>/<cand>/layout_contract_trace.json  (deep dives)
  selection_report.json  visual_review.json
  product_acceptance.json  gate_index.json
```

## Measurement facts (one configuration, not universal rules)

- The stage6 content box was nearly full-canvas with an UNVERIFIED frame;
  do not treat those coordinates as the corporate standard. `%` zones map
  to the verified frame of the current run.
- Chromium-measured needs are per exact (face binary, size, margins,
  wrap, spacing) case — e.g. one past configuration measured body 12pt
  1-line ~= 25pt and large_metric 30pt 1-line ~= 47pt. These numbers are
  historical observations of that request, never solver constants: every
  new case is measured with its own request/response pair.
- Same-zone siblings stack vertically by measured heights in READING
  order (never equal shares, never id order).
- `ENGINE_VERSION` (`layout.py`) MUST bump on every behavior change;
  the layout cache key carries it. Stale hits resurrect pre-fix geometry.
- Measurement cache is content-addressed (request hash); layout resume
  reuses only identical tree hashes. New run dirs start clean.

## Runner states (Stage 7 §8)

Run: `ingest -> profile -> semantic -> design_briefs -> visual_concepts
-> candidate_search -> selections -> full_assembly -> final_checks
-> accepted`. `awaiting_harness` is a machine state (exit 4), not a
failure; `blocked`/`budget_exhausted` keep resumable state.

Per candidate: `concept_pending -> concept_fidelity_validated ->
scene_pending -> scene_validated -> styled -> measured -> resolved ->
emitted -> saved_checked -> rendered -> visually/design_reviewed ->
eligible -> selected`. Any repair creates a new revision and invalidates
downstream measurement/emission/review/selection refs
(`cache_invalidation_report.json`). Hard-ineligible candidates are never
selected; the assembled package must BE the selected candidate bytes.

## Design cycle (mandatory, §10.4-10.8)

Before bbox assignment the harness designs the visual explanation:
design brief (communication job + takeaway) -> 3 semantically different
visual concepts (topology/reading/dominance) -> fidelity validation ->
concept scene -> native build/render -> design review. A visually weak
concept returns to the concept stage, never to cosmetic bbox shifts.
`visual_design` is a required acceptance gate alongside the technical
gates; `flat_text_dump` (text placement without designed composition)
is a repair/reject finding even when all technical checks pass.
