---
name: gpn-restyle
description: Restyle a supplied PPT/PPTX or create an editable corporate deck from a document or brief using the project's mandatory ontology and approved slide examples. Execute the harness model tasks, measurement, native build, render, visual repair, candidate selection and final acceptance loop.
---

# GPN restyle

Use this skill for this project's restyle/create/resume tasks.

## Load the project contract

1. Resolve project_root and code_root from the existing project configuration.
2. Read the runtime contract in [runtime-contract.md](references/runtime-contract.md).
3. Read the current ontology manifest, compiled applicable rules, owned examples allowlist,
   font inventory and verified frame for the current run. Use complete applicable conditions;
   a model digest alone is insufficient if it omits them.
4. Use only project_root/slide_examples as design examples. Do not restore or open
   agent-slides examples, other skill examples or cached external slide designs.
5. Preserve input, ontology, examples and font assets. Use a new run/revision for outputs.

## Execute the runner

1. For PPT/PPTX: `slides gpn --project-root <root> restyle <in.pptx>
   --llm-provider harness --output <out.pptx> [--run-dir runs/<stage>/<id>]`.
   For documents/briefs: `slides gpn --project-root <root> create <in.pdf>
   --output <out.pptx>` (or `--prompt` / `--prompt-file`).
2. Default provider is harness. Use the current model to service returned model tasks.
   Do not create a local LLM server or require API credentials.
3. When the runner returns awaiting_harness, open each pending request packet and
   every image required by that task, then produce the typed response with its request ID,
   snapshot fingerprint and actual model identity. Resume the same run.
4. A task file is not a completed generation/review. Consume and validate the response.
5. Continue through scene generation, measurement, native emission, saved checks,
   visual review, repair and selection. Pending scene/repair/review tasks are work to do.

## Compose and inspect

1. Separate content preservation from source geometry. Restyle uses recompose by default.
2. Assign semantic child roles. Numeric values, labels, units and caveats can have
   different ontology roles inside one cluster. Do not copy a zone role to every child.
3. Measure the exact text/style/margins/wrap that native emission will use.
4. Never solve overflow by shrinking normative text, deleting required content,
   returning arbitrary source boxes or exporting off-canvas objects.
5. Read [quality-review.md](references/quality-review.md) for image review and repair.
6. Open rendered candidate PNGs. Review current hashes, actual readability and hierarchy,
   logos, chart/table/diagram fidelity, collisions and required content visibility.
7. Repair findings are actions: amend the scene or fix a proven engine defect, rebuild,
   re-check, re-render and review the new revision. Do not stop after writing repair JSON.
8. Select from real evaluated candidates using their renders and hard gate eligibility.
   Candidate order or a technical pass alone is not a visual selection.

## Finish

1. Assemble the complete selected deck and validate/render/review its actual final bytes.
2. Publish the corporate PPTX only after the central product acceptance passes.
3. Preserve actual alternatives, their renders, findings and reasons for selection.
4. Follow [gate-bundle.md](references/gate-bundle.md) to generate the next review bundle.
5. Report the final deck path, acceptance state and any actual terminal blocker briefly.
   Do not ask for permission to continue work already authorized for this task.
