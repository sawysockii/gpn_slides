# Quality review procedure (gpn-restyle)

No corporate norms live here. Norms live in `ontology/` JSON; this file
is procedure only.

## What to open

Every review reads, for CURRENT bytes only (verify SHA first):

1. `render/page-<1..8>.png` — all eight pages, never a subset.
2. The source texts/numbers the page must preserve (source_ir + plan).
3. The applicable ontology conditions for the roles on the page.

A review of other bytes is stale and must be re-done, never carried over.

## What to check per page

- Required numbers/labels/units/conditions present, exact, readable.
- One idea per visual group; numeric value, label, unit and caveat use
  their own roles (a shared zone role must not inflate labels).
- Line breaks: no mid-number wraps (`01` on one line), no orphaned units,
  no text overflowing its box or colliding with neighbours.
- Columns/rows align; whitespace is even; reading order top-to-bottom,
  left-to-right matches the accepted plan.
- Footer + source lines present; logo visible (contrast) and correct
  colorway; images uncropped and sharp.
- Chart/table/diagram fidelity: labels match data, no clipped axes.
- No off-canvas content; no placeholder boxes; no lorem ipsum.

## Verdicts

- `accept` — page ships as-is.
- `repair` — actionable findings (node ids + measured deficit or visible
  defect + recommended change). A repair verdict MUST be followed by a
  scene/engine fix, rebuild, re-check, re-render and re-review of the
  new revision. Writing the finding alone closes nothing.
- `reject` — candidate abandoned; reason recorded; a new candidate or
  strategy is required (never re-resolve the identical scene hash
  against the identical finding set).

## Recording

`visual_review.json`: request id, candidate SHA256, per-page image hash,
verdict, findings (with node ids + regions), quality scores, actual
model identity. Missing review stays `pending`, never `reviewed`.

## Design review (mandatory, §10.7)

Technical pass is a precondition, not the verdict. Review the CURRENT
render against the design brief + visual concept + source facts:

1. Semantic readability and relations: does the viewer get the takeaway
   without reading every sentence? Are input->transform->output,
   comparison, stages or evidence relations visible as designed?
2. Hierarchy/dominance: one clear dominant group; primary vs secondary
   vs caveat weights differ.
3. Grouping/alignment/rhythm; purposeful whitespace (no random empty
   bottom, no decorative frames without function).
4. Encoding/data fidelity: no invented causality, time series without
   datapoints, or common scale for incompatible units.
5. Corporate visual language per loaded JSON rules (roles/surfaces).

`flat_text_dump` — content blocks placed on canvas with hierarchy and
relations left for the viewer to reconstruct — is a `repair`/`reject`
finding even with zero overlaps. Purely typographic slides are allowed
only with a job-specific rationale (title, key message, appendix).
`design_review.json` records brief/concept SHAs, per-dimension results,
image regions and the actual model; the reviewer never receives a
pre-selected winner or a target score.
