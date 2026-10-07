# GPN Support Matrix (Stage 3 — real capabilities)

Scope: diagnostic native-edit operations (`set_shape_geometry`,
`set_shape_style`, `delete_shape`) plus the read-only import/doctor gates.
This is the verified native-edit scope, not corporate production output
(`strict_output_ready=false` until the Stage 4 exporter and Stage 7 render QA).

## Addressable shape operations

| Operation | Supported targets | Units | Guards |
|---|---|---|---|
| `set_shape_geometry` | shapes, textboxes, pictures, graphic frames, groups, connectors | inches, parent-relative; EMU = round(inches × 914400), converted once | finite floats; text/table/image/content rects need width,height > 0; supported lines allow one zero extent; `expected_xml_sha256` pre-check; group `chOff/chExt` preserved; rotation/flip preserved |
| `set_shape_style` | textbox/preset shapes (fill/line/text) | HEX `RRGGBB`, pt | whole-patch validation before mutation (atomic refusal); `fill_mode`/`line_mode` `keep`/`solid`/`none`; `text_scope` `defaults` (defRPr only) or `uniform_runs` (in-place run patch, identity preserved); charts/tables/groups/images-text refused as unsupported; no shadows/gradients/rounding/new tokens |
| `delete_shape` | single addressed shape or group | n/a | mandatory `expected_xml_sha256` + non-empty `deletion_reason`; owner-scoped rel cleanup (shared rels retained); connector/animation dependents refuse; transactional rollback; GPN layer additionally requires verified decoration/replacement proof for content |

## Autofit policy

| Policy | XML | Used by |
|---|---|---|
| `legacy_shrink` (default) | exactly one `a:normAutofit` | old JSON plans without the new field (backward compatible) |
| `none` | exactly one `a:noAutofit` | all new GPN text writers/wrappers; font shrink is never a fallback overflow fix |

The setter touches only the target's own `a:bodyPr` (never a nested
group/table body via a blind `.//` match), preserves wrap/margins/anchor,
and respects schema child ordering (after `prstTxWarp`).

## Text search (`find_text`)

Unicode tokenization: `re.findall(r"[^\W_]+", query.casefold())`.
Empty/punctuation-only queries return `[]`. Scoring/sort/limit and hit keys
(`slide_index, slide_id, slide_uid, shape_index, shape_id, shape_uid, score,
snippet`) are unchanged. Snippets come from the original Unicode text (never
casefolded offsets). No morphology, embeddings, or network. Ё/Е stay distinct.

## Hyperlinks

Resolved per occurrence (`owner_part + xml_path + event_kind`), never by bare
rId: two objects may share one relationship. Run/field links come from the IR;
shape-level (incl. nested groups) click/hover links come from
`iter_hyperlink_occurrences` over raw slide XML in document order.
Dangling content links are blocking uncertainties; external URIs are recorded
without any network request. `clrMapOvr` remaps scheme colors before theme
resolution (regression-tested). EA/CS theme aliases (`+mj-ea`, `+mn-cs`, …)
resolve via the font scheme; anything else is an honest unresolved scope.

## Rule checkers (Stage 2 scope, hardened in Stage 3)

8 deterministic (`C01 C02 C10 C16 C19 C21 C22 R04`), 7 partial with honest
deferred scopes (`C03 C04 C05 C11 C13 C14 C20`), rest deferred-unknown
(blocking for hard rules, never fake-passed). Deferred parts: text-overflow /
render-overlap (C04), mapping coverage (C05), business semantics (C11),
corpus index (C13), exported editability (C14), wrap alignment (C20),
semantic/render rules (C06–C09 C12 C15 C17 C18 R01–R03).

## Explicit non-goals of this stage

Full importer rewrite, planner/LLM calls, HTML layout, document authoring,
native exporter/Stage 4 transplant, render QA/Stage 7, corporate font
downloads. Missing GPN font binaries keep `production_assets_ready=false`
(`needs_assets`); they never explain away code behavior (Unicode search,
classifier, rollback are font-independent).
