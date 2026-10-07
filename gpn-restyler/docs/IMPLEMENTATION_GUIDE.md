# GPN Implementation Guide — Stage 3: native operations

## Final signatures

```python
# slides_cli/api.py
CANONICAL_SHAPE_XML_ALGO = "c14n-exclusive-v1"

def compute_shape_xml_sha256(element) -> str: ...
def resolve_shape_address(presentation, *, slide_index: int,
                           shape_id: int,
                           group_path: tuple[int, ...] = ()) -> ShapeAddressResolution: ...

class Presentation:
    def find_text(self, *, query: str, limit: int = 10) -> list[dict]: ...
    def add_text(self, *, slide_index, text, left, top, width, height,
                 font_size=20, bold=False, font_name=None, font_color=None,
                 autofit_policy: Literal["legacy_shrink", "none"] = "legacy_shrink") -> None: ...
    @staticmethod
    def _set_autofit(shape, policy: Literal["legacy_shrink", "none"]) -> None: ...
    @staticmethod
    def _ensure_autofit(shape) -> None: ...  # legacy wrapper
    def set_shape_geometry(self, *, slide_index, shape_id, group_path=(),
                           left, top, width, height,
                           expected_xml_sha256=None) -> None: ...
    def set_shape_style(self, *, slide_index, shape_id, group_path=(),
                        style: NativeShapeStyle,
                        expected_xml_sha256=None) -> None: ...
    def delete_shape(self, *, slide_index, shape_id, group_path=(),
                     expected_xml_sha256: str, deletion_reason: str) -> None: ...
```

```python
# slides_cli/model.py — all models extra="forbid", op discriminator
class NativeShapeStyle(BaseModel): ...  # explicit patch primitives
class SetShapeGeometryOp(BaseModel): ...  # op="set_shape_geometry"
class SetShapeStyleOp(BaseModel): ...     # op="set_shape_style"
class DeleteShapeOp(BaseModel): ...       # op="delete_shape"
# Operation union + _OP_DISPATCH extended; schema from model_json_schema().
```

```python
# slides_cli/gpn/patching.py — GPN adapter (no user trusted flag)
def resolve_native_style(*, style_role, rules, context) -> NativeShapeStyle: ...
def authorize_gpn_edit(edit, context) -> EditAuthorization: ...
def plan_shape_deletion(address, graph) -> DeletionPlan: ...
def preflight_gpn_edits(*, source, edits, context) -> GpnEditPreflight: ...
def apply_gpn_edits(*, source, edits, context, run_dir,
                     candidate_output, overwrite=False) -> GpnPatchResult: ...
def compare_ledger_preservation(before_ledger, after_ledger) -> dict: ...
```

## Units

Geometry is inches, parent-relative. EMU conversion happens exactly once:
`int(round(inches * 914400))`. Child coordinates are never re-projected into
slide space; resizing a group intentionally rescales its rendered children
without rewriting the parent `chOff/chExt`.

## Hash algorithm (versioned)

Shape hashes use exclusive C14N without comments
(`CANONICAL_SHAPE_XML_ALGO = "c14n-exclusive-v1"`) over the shape element.
Reader, preflight, and facade all use this one serialization — raw-bytes
hashes (e.g. legacy `SourceRef.xml_sha256`) are stored separately and never
compared against canonical hashes. Sequential ops on one object must chain
fresh hashes (preflight on a copy previews the resulting hashes); stale
hashes fail before mutation. Delete always requires a hash.

## `text_scope` semantics

- `defaults`: only paragraph/default character properties (`a:defRPr`) change;
  existing explicit runs are never overridden.
- `uniform_runs`: requested character properties are applied to the actual
  runs/fields in place (XML identity, order, hyperlinks, baseline/superscript
  preserved). Mixed rich content without a safe normalization is refused as
  unsupported/needs-review instead of being flattened.
- `None`/omitted text property means "do not change", never "clear override".
  There is no inherit/clear guessing; a future explicit mode will cover it.

## Generic facade vs GPN guard

The generic facade knows no corporate master: it enforces addressing, hashes,
types, units, and atomicity. The GPN adapter (`gpn/patching.py`) enforces
compiled policy (palette faces/tokens, no synthetic bold, conditional-color
evidence), ledger/protected evidence (content deletion needs a verified
`DecorationDecision`/replacement mapping; "это декор" model text authorizes
nothing), and canvas/protected-region checks. The future planner passes
`style_role`; primitives are built by code — no model calls in this stage.

## Dry-run guarantees

Legacy `apply_operations(dry_run=True)` only marks operations `planned`. GPN
`preflight_gpn_edits` actually applies the batch on a copy, checks addresses,
hashes, support, and shared refs, then re-verifies — the source and the
authoritative in-memory deck stay immutable. `apply_gpn_edits` additionally
saves a preview, reimports the saved bytes independently, diffs ledger atoms
(by identity/multiplicity, never literal IR IDs), package parts/relationships
(canonical XML equivalence for XML, exact bytes for binaries), and only then
atomically writes the verified diagnostic candidate (`committed_count=0` +
source/output untouched on any failure). `transactional=False` never bypasses
the preservation gate.

## Stage 3 limits

Diagnostic candidate only (`verified_for_native_edit_scope`,
`strict_output_ready=false`, `test_only=true`). Full exporter/transplant,
table/chart/notes/link writers, and render QA are later stages.
