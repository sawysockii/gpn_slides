# Gate bundle (gpn-restyle)

How to produce the next review increment: one archive with bytes,
inputs, callgraph and evidence — never reports alone.

## Required gate -> evidence map

```text
semantic preservation  -> source projection + transformation ledger
                          + content_diff.json + output_object_map.json
scene actually applied -> scene plans + resolved scenes
                          + layout_contract_trace.json + emission map
                          + saved boxes (saved_geometry_diff.json)
typography identity/fit -> resolved styles + exact measure
                          requests/responses + saved/native findings
                          (saved_measurement_report.json)
ontology compliance    -> used ontology snapshot + compiled rules
                          + scope coverage + saved authoring checks
visible branding/frame -> approved frame/surface + current PNG review
                          + exact source refs (+ asset_overrides.json
                          + authorized_replacements_report.json)
geometry/visibility    -> expected-first raw/saved reports + transforms
                          + addresses + text/occlusion scopes
                          (check_saved_geometry + visibility report)
visual quality         -> current eight-page review + actionable findings
                          + executed repair history
real diversity/select  -> actual candidate bytes/renders/reviews
                          + signatures + selection_report.json
native editability     -> object-kind inventory + edit/save/reopen
                          roundtrip report
product acceptance     -> product_acceptance.json + gate_index.json
                          + current full bytes
```

## Bundle layout

```text
review_increment_next.zip
  final/candidate_or_corporate.pptx  render/candidate.pdf
  render/slide-01.png ... slide-08.png
  product_acceptance.json  gate_index.json  selection_report.json
  output_object_map.json  emission_report.json
  candidate_after_ir.json  candidate_after_ledger.json
  content_diff.json  part_preservation_diff.json
  native_structure_report.json  native_edit_roundtrip_report.json
  saved_geometry_report.json  saved_geometry_diff.json
  required_visibility_report.json  saved_authoring_rule_report.json
  saved_text_spec_report.json  saved_measurement_report.json
  layout_overlap_report.json  ontology_rule_coverage.json
  verified_frame.json  font_inventory.json  browser_font_report.json
  calibration_profile.json  measurement_environment.json
  slides/slide-03/<cand>/<rev>/ + slides/slide-06/<cand>/<rev>/
    (scene_plan, composition_tree, resolved_scene, text specs,
     layout_contract_trace, emission_map, measure req/resp,
     candidate.pptx, render, visual_review, gates, findings, tasks)
  model_requests/ model_responses/ model_task_manifest.json
  runner_transition_log.jsonl  candidate_evaluations.json
  repair_history.json  run_config.json  runtime_callgraph.md
  inputs/* (manifests, IR, ledger, semantic deck, classification,
    ontology snapshot hash, compiled rules, conflicts, examples,
    fonts, template profile/derivation)
  code/git_diff.patch  code/repo_head_and_status.txt
  code/code_manifest.json  code/commands_and_results.json
  code/test_evidence.json  code/full/<changed files>
```

`bundle manifest` lists every file with path/size/SHA256, input/corpus/
code/renderer snapshot hashes, parent run, created time and accepted
outputs (self-hash excluded with a stated reason). The builder refuses
`bundle_complete=true` while a required evidence ref is missing; gaps
are recorded as missing/pending/blocked so the gate sees why it is not
accepted. Assemble with one command, never by hand:

```text
slides gpn --project-root <root> bundle runs/<stage>/<run_id> \
  --output review_increment_system.zip
```

The generic builder (`gpn/review_bundle.py`) derives required roles from
the fixed review schema, resolves ArtifactStore refs to bytes with
canonical portable paths, verifies hash/size/subject/snapshot per ref,
records an explicit live-path -> bundle-path alias map, and self-inspects
the ZIP for ref closure. `bundle_complete = no missing required AND all
required refs validated` — never "chosen files present".
