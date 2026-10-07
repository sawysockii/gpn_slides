"""Shared Pydantic v2 foundation models for the GPN pipeline (spec §3.4, §7).

Only corpus/geometry/diagnostic primitives live here at Stage 0/1; IR models are
added by later stages. All serializable models use ``extra="forbid"``.
"""

from __future__ import annotations

import math
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

StrictModel = ConfigDict(extra="forbid")


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Issue(BaseModel):
    """Machine-addressable diagnostic (spec §7.1)."""

    model_config = StrictModel

    rule_id: str
    code: str
    severity: Severity
    slide_id: str | None = None
    object_ids: list[str] = Field(default_factory=list)
    details: str = ""
    evidence: list[str] = Field(default_factory=list)
    repairable: bool = False


class CorpusFileRecord(BaseModel):
    """One file inside a mandatory project corpus (spec §3.4)."""

    model_config = StrictModel

    relative_path: str  # POSIX path relative to project_root
    kind: Literal["json", "markdown", "attachment"]
    sha256: str
    byte_count: int = Field(ge=0)
    normative: bool


class IssueListMixin(BaseModel):
    issues: list[Issue] = Field(default_factory=list)


class OntologySources(BaseModel):
    """Resolved ``project_root/ontology`` corpus (spec §3.4)."""

    model_config = StrictModel

    root: Path
    primary_json: Path | None
    markdown_files: list[Path] = Field(default_factory=list)
    normative_files: list[CorpusFileRecord] = Field(default_factory=list)
    ontology_hash: str | None = None
    ontology_corpus_hash: str
    issues: list[Issue] = Field(default_factory=list)
    ready: bool = False
    selected_version: str | None = None
    candidates: list[str] = Field(default_factory=list)


class ExampleDiscoveryReport(BaseModel):
    """Resolved ``project_root/slide_examples`` corpus (spec §3.4)."""

    model_config = StrictModel

    root: Path
    files: list[CorpusFileRecord] = Field(default_factory=list)
    examples_corpus_hash: str
    missing: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    ready: bool = False


def _finite(name: str, value: float) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return value


class PointPt(BaseModel):
    model_config = StrictModel

    x: float
    y: float

    @field_validator("x", "y")
    @classmethod
    def _check_finite(cls, v: float) -> float:
        return _finite("coordinate", v)


class RectEMU(BaseModel):
    """Rectangle in EMU. Source x/y may be negative; extents are non-negative."""

    model_config = StrictModel

    x: int = 0
    y: int = 0
    w: int = Field(default=0, ge=0)
    h: int = Field(default=0, ge=0)


class RectPt(BaseModel):
    model_config = StrictModel

    x: float
    y: float
    w: float = Field(ge=0)
    h: float = Field(ge=0)

    @model_validator(mode="after")
    def _finite_extents(self) -> RectPt:
        _finite("w", self.w)
        _finite("h", self.h)
        return self


class RectPct(BaseModel):
    """Percent rectangle inside a content box; units are 0–100, not 0–1."""

    model_config = StrictModel

    x: float
    y: float
    w: float = Field(gt=0, le=100)
    h: float = Field(gt=0, le=100)


class Affine2D(BaseModel):
    """Affine matrix ``x'=a*x+c*y+tx``, ``y'=b*x+d*y+ty`` (Pt units)."""

    model_config = StrictModel

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    tx: float = 0.0
    ty: float = 0.0

    def determinant(self) -> float:
        return self.a * self.d - self.b * self.c


class AssetRef(BaseModel):
    model_config = StrictModel

    sha256: str
    relative_path: str
    media_type: str
    byte_count: int = Field(ge=0)


class ArtifactRef(BaseModel):
    model_config = StrictModel

    absolute_path: Path
    sha256: str
    byte_count: int = Field(ge=0)


Scalar = Annotated[
    str | int | float | bool | None,
    Field(description="Scalar config override value."),
]


# ---------------------------------------------------------------------------
# Evidence / value primitives (spec §7.1)
# ---------------------------------------------------------------------------

class SourceRef(BaseModel):
    """Reference into an actual PPTX package object."""

    model_config = StrictModel

    kind: Literal["pptx"] = "pptx"
    deck_sha256: str
    slide_part: str
    shape_id: int
    group_path: list[int] = Field(default_factory=list)
    subpath: str = ""
    xml_sha256: str | None = None


class DocumentSourceRef(BaseModel):
    """Reference into a PDF page/bbox or Word part/paragraph/cell."""

    model_config = StrictModel

    kind: Literal["document"] = "document"
    source_sha256: str
    document_id: str
    block_id: str
    page_index: int | None = None
    bbox_pt: RectPt | None = None
    part_path: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    normalized_sha256: str | None = None


class PromptSourceRef(BaseModel):
    """Reference into the immutable snapshot of the user prompt."""

    model_config = StrictModel

    kind: Literal["prompt"] = "prompt"
    source_sha256: str
    request_id: str
    char_start: int
    char_end: int


EvidenceRef = Annotated[
    SourceRef | DocumentSourceRef | PromptSourceRef,
    Field(discriminator="kind"),
]


class Capability(BaseModel):
    model_config = StrictModel

    level: Literal["native_full", "native_partial", "image_source", "unsupported"]
    reason: str = ""
    missing_features: list[str] = Field(default_factory=list)


class Uncertainty(BaseModel):
    model_config = StrictModel

    id: str
    subject_id: str
    code: str
    evidence_refs: list[str] = Field(default_factory=list)
    question: str = ""
    blocking: bool = False


NumberState = Literal["present", "missing", "not_applicable", "unknown"]


class TypedValue(BaseModel):
    """A numeric/text value that never silently converts missing to zero."""

    model_config = StrictModel

    state: NumberState
    decimal: str | None = None
    display_text: str = ""
    number_format: str | None = None
    unit_ref: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> TypedValue:
        if self.state == "present" and self.decimal is None:
            raise ValueError("present TypedValue requires a decimal string (0 is '0')")
        if self.state != "present" and self.decimal is not None:
            raise ValueError(f"state {self.state!r} must not carry a decimal")
        return self


# ---------------------------------------------------------------------------
# Text (spec §7.2)
# ---------------------------------------------------------------------------

class ColorModifier(BaseModel):
    model_config = StrictModel

    name: str
    value: int


class ColorExpression(BaseModel):
    model_config = StrictModel

    kind: Literal["srgb", "scheme", "system", "unknown"]
    value: str
    modifiers: list[ColorModifier] = Field(default_factory=list)
    resolved_rgb: str | None = None


class LineStyleIR(BaseModel):
    model_config = StrictModel

    color: ColorExpression | None = None
    width_emu: int | None = None
    dash: str | None = None
    begin_arrow: str | None = None
    end_arrow: str | None = None


class ResolvedTextStyle(BaseModel):
    model_config = StrictModel

    typeface: str | None = None
    size_pt: float | None = None
    bold: bool | None = None
    italic: bool | None = None
    underline: str | None = None
    color: ColorExpression | None = None
    language: str | None = None
    inherited_from: list[str] = Field(default_factory=list)
    effective_font_scale: float = 1.0
    provenance: dict[str, StyleSource] = Field(default_factory=dict)
    unresolved: list[str] = Field(default_factory=list)


class StyleSource(BaseModel):
    """Where one resolved style property actually came from (spec §3 task table)."""

    model_config = StrictModel

    part_name: str
    xml_path: str
    layer: str
    property_name: str
    raw_value: str


class TypefaceResolution(BaseModel):
    model_config = StrictModel

    raw_typeface: str
    resolved_typeface: str | None = None
    theme_part: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class ThemeFontScheme(BaseModel):
    model_config = StrictModel

    major_latin: str | None = None
    major_east_asian: str | None = None
    major_complex_script: str | None = None
    minor_latin: str | None = None
    minor_east_asian: str | None = None
    minor_complex_script: str | None = None
    script_fonts: dict[str, str] = Field(default_factory=dict)


class ThemeProfile(BaseModel):
    """One ``ppt/theme/*.xml`` part actually reachable from a slide (spec §3)."""

    model_config = StrictModel

    theme_part: str
    source_hash: str
    major_fonts: ThemeFontScheme = Field(default_factory=ThemeFontScheme)
    minor_fonts: ThemeFontScheme = Field(default_factory=ThemeFontScheme)
    script_fonts: dict[str, str] = Field(default_factory=dict)
    colors: dict[str, str] = Field(default_factory=dict)
    color_transforms: dict[str, list[ColorModifier]] = Field(default_factory=dict)
    issues: list[Issue] = Field(default_factory=list)


class FieldMetadata(BaseModel):
    """``a:fld`` identity + cached visible text (spec §4.1)."""

    model_config = StrictModel

    field_id: str
    field_type: str
    cached_text: str
    source_ref: SourceRef
    raw_xml_asset: AssetRef | None = None


class LinkResolutionRecord(BaseModel):
    model_config = StrictModel

    subject_id: str
    owner_part: str
    source_rel_id: str
    source_target: str
    target_slide_ref: str | None = None
    action: str | None = None
    status: Literal["resolved", "unresolved", "external", "navigation"]
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class LinkResolutionReport(BaseModel):
    model_config = StrictModel

    resolved: list[LinkResolutionRecord] = Field(default_factory=list)
    unresolved: list[LinkResolutionRecord] = Field(default_factory=list)
    navigation_actions: list[LinkResolutionRecord] = Field(default_factory=list)
    external_links: list[LinkResolutionRecord] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_duplicates(self) -> LinkResolutionReport:
        groups = (
            self.resolved,
            self.unresolved,
            self.navigation_actions,
            self.external_links,
        )
        seen: set[tuple[str, str, str]] = set()
        for group in groups:
            for rec in group:
                key = (rec.subject_id, rec.owner_part, rec.source_rel_id)
                if key in seen:
                    raise ValueError(f"link record duplicated across categories: {key}")
                seen.add(key)
        return self


BindingStatus = Literal["equal", "conflict", "unverifiable", "not_applicable"]


class ChartBindingComparison(BaseModel):
    model_config = StrictModel

    chart_id: str
    series_id: str
    binding_id: str
    formula: str | None = None
    workbook_ref: str | None = None
    cached_value: TypedValue | None = None
    workbook_value: TypedValue | None = None
    status: BindingStatus
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class ObjectCapabilityReport(BaseModel):
    model_config = StrictModel

    object_id: str
    kind: str
    capability: Capability
    source_ref: SourceRef | None = None
    reason: str = ""
    uncertainty_ids: list[str] = Field(default_factory=list)


class ImportSupportReport(BaseModel):
    model_config = StrictModel

    schema_version: str = "1.0"
    source_hash: str
    object_count: int = Field(ge=0)
    capabilities_by_kind: dict[str, dict[str, int]] = Field(default_factory=dict)
    per_object: list[ObjectCapabilityReport] = Field(default_factory=list)
    uncertainties: list[Uncertainty] = Field(default_factory=list)
    unresolved_links: list[LinkResolutionRecord] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    import_status: Literal["source_import_complete", "source_import_partial"] = (
        "source_import_complete"
    )
    source_data_verified: bool = False
    blocking_uncertainty_ids: list[str] = Field(default_factory=list)



class SemanticMark(BaseModel):
    model_config = StrictModel

    kind: Literal["emphasis", "subscript", "superscript", "footnote_anchor", "status"]
    start: int
    end: int
    evidence_ref: str = ""


class HyperlinkIR(BaseModel):
    model_config = StrictModel

    target_kind: Literal["external", "internal_slide"]
    uri: str | None = None
    target_slide_ref: str | None = None
    tooltip: str | None = None
    source_rel_id: str = ""


class TextRunIR(BaseModel):
    model_config = StrictModel

    id: str
    source_ref: EvidenceRef
    text: str
    source_style: ResolvedTextStyle
    semantic_marks: list[SemanticMark] = Field(default_factory=list)
    hyperlink: HyperlinkIR | None = None
    field: FieldMetadata | None = None
    kind: Literal["text", "field"] = "text"


class TextBreakIR(BaseModel):
    model_config = StrictModel

    after_run_id: str
    offset: int
    kind: Literal["soft", "hard"]


class NumberingIR(BaseModel):
    model_config = StrictModel

    scheme: str
    start_at: int = 1
    source_marker: str | None = None


class ParagraphIR(BaseModel):
    model_config = StrictModel

    id: str
    source_ref: EvidenceRef
    runs: list[TextRunIR] = Field(default_factory=list)
    breaks: list[TextBreakIR] = Field(default_factory=list)
    list_kind: Literal["none", "bullet", "numbered"] = "none"
    level: int = 0
    numbering: NumberingIR | None = None
    source_indent_emu: int | None = None
    source_hanging_emu: int | None = None
    space_before_pt: float | None = None
    space_after_pt: float | None = None


TextRole = Literal[
    "title", "body", "section", "footnote", "caption", "metric", "unknown"
]


class TextPayload(BaseModel):
    model_config = StrictModel

    paragraphs: list[ParagraphIR] = Field(default_factory=list)
    role: TextRole = "unknown"
    source_box: RectEMU | None = None
    text_direction: str = "horz"
    vertical_anchor: str = "top"
    internal_margins_emu: tuple[int, int, int, int] = (91440, 45720, 91440, 45720)


# ---------------------------------------------------------------------------
# Tables (spec §7.3)
# ---------------------------------------------------------------------------

class CellStyleIR(BaseModel):
    model_config = StrictModel

    fill: ColorExpression | None = None
    borders: dict[str, LineStyleIR] = Field(default_factory=dict)
    margins_emu: tuple[int, int, int, int] = (45720, 45720, 45720, 45720)
    vertical_anchor: str = "middle"


class TableCellIR(BaseModel):
    model_config = StrictModel

    id: str
    source_ref: EvidenceRef
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    col_span: int = Field(default=1, ge=1)
    is_merge_origin: bool = False
    is_spanned: bool = False
    paragraphs: list[ParagraphIR] = Field(default_factory=list)
    typed_value: TypedValue | None = None
    source_style: CellStyleIR = Field(default_factory=CellStyleIR)
    visible: bool = True


class MergeRange(BaseModel):
    model_config = StrictModel

    r0: int = Field(ge=0)
    c0: int = Field(ge=0)
    r1: int = Field(ge=0)
    c1: int = Field(ge=0)

    @model_validator(mode="after")
    def _rect(self) -> MergeRange:
        if self.r1 < self.r0 or self.c1 < self.c0:
            raise ValueError("merge range end must not precede its origin")
        return self


class TablePayload(BaseModel):
    model_config = StrictModel

    rows: int = Field(ge=1)
    cols: int = Field(ge=1)
    cells: list[TableCellIR] = Field(default_factory=list)
    merges: list[MergeRange] = Field(default_factory=list)
    column_widths_emu: list[int] = Field(default_factory=list)
    row_heights_emu: list[int] = Field(default_factory=list)
    header_row_ids: list[int] = Field(default_factory=list)
    header_col_ids: list[int] = Field(default_factory=list)
    semantic_axis_labels: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Charts (spec §7.4)
# ---------------------------------------------------------------------------

class ChartPointIR(BaseModel):
    model_config = StrictModel

    id: str
    index: int
    category_path: list[str] | None = None
    x: TypedValue | None = None
    y: TypedValue | None = None
    value: TypedValue | None = None
    bubble_size: TypedValue | None = None
    custom_label: TextPayload | None = None


class ChartSeriesIR(BaseModel):
    model_config = StrictModel

    id: str
    name: str = ""
    source_index: int = 0
    points: list[ChartPointIR] = Field(default_factory=list)
    plot_id: str = ""
    axis_group: str = "primary"
    formula_refs: list[str] = Field(default_factory=list)
    semantic_color_key: str | None = None


class AxisIR(BaseModel):
    model_config = StrictModel

    id: str
    type: Literal["category", "value", "date", "series"]
    unit_ref: str | None = None
    minimum: str | None = None
    maximum: str | None = None
    log_base: str | None = None
    reversed: bool = False
    crosses: str | None = None
    number_format: str | None = None
    title: TextPayload | None = None


class PlotIR(BaseModel):
    model_config = StrictModel

    id: str
    type_token: str
    series_ids: list[str] = Field(default_factory=list)
    axis_ids: list[str] = Field(default_factory=list)
    grouping: str | None = None
    stacking: str | None = None


class CategoryIR(BaseModel):
    model_config = StrictModel

    id: str
    index: int
    path: list[str] = Field(default_factory=list)
    raw_value: str | None = None
    is_date: bool = False


class ChartPayload(BaseModel):
    """Native chart part with its preserved data graph."""

    model_config = StrictModel

    representation: Literal["native_part"] = "native_part"
    chart_part: str
    chart_type: str
    plots: list[PlotIR] = Field(default_factory=list)
    series: list[ChartSeriesIR] = Field(default_factory=list)
    axes: list[AxisIR] = Field(default_factory=list)
    categories: list[CategoryIR] = Field(default_factory=list)
    chart_xml: AssetRef | None = None
    workbook: AssetRef | None = None
    external_workbook_uri: str | None = None
    data_source_status: Literal["embedded", "cache_only", "external", "missing"] = "missing"
    display_blanks_as: str = "gap"
    semantic_annotations: list[str] = Field(default_factory=list)
    part_graph_root: str = ""


class VerifiedChartData(BaseModel):
    model_config = StrictModel

    chart_type: str
    series: list[ChartSeriesIR] = Field(default_factory=list)
    categories: list[CategoryIR] = Field(default_factory=list)
    axes: list[AxisIR] = Field(default_factory=list)
    unit_refs: list[str] = Field(default_factory=list)
    source_evidence_refs: list[str] = Field(default_factory=list)
    verification_status: Literal["verified"] = "verified"


class NewChartPayload(BaseModel):
    """Chart to be created from verified data (not an existing chart part)."""

    model_config = StrictModel

    representation: Literal["verified_data"] = "verified_data"
    data: VerifiedChartData


ChartUnion = Annotated[ChartPayload | NewChartPayload, Field(discriminator="representation")]


# ---------------------------------------------------------------------------
# Shapes, connectors, images, groups, unknown (spec §7.5)
# ---------------------------------------------------------------------------

class PathCommand(BaseModel):
    model_config = StrictModel

    op: Literal["move", "line", "quad", "cubic", "close"]
    points: list[PointPt] = Field(default_factory=list)

    @model_validator(mode="after")
    def _point_count(self) -> PathCommand:
        required = {"move": 1, "line": 1, "quad": 2, "cubic": 3, "close": 0}[self.op]
        if len(self.points) != required:
            raise ValueError(f"op {self.op!r} requires {required} points, got {len(self.points)}")
        return self


class ShapePayload(BaseModel):
    model_config = StrictModel

    geometry_kind: Literal["preset", "freeform"]
    geometry_token: str | None = None
    path_commands: list[PathCommand] = Field(default_factory=list)
    text: TextPayload | None = None
    source_line: LineStyleIR = Field(default_factory=LineStyleIR)
    source_fill: ColorExpression | None = None


class ConnectorPayload(BaseModel):
    model_config = StrictModel

    from_object_id: str | None = None
    to_object_id: str | None = None
    from_site: int | None = None
    to_site: int | None = None
    path_points: list[PointPt] = Field(default_factory=list)
    arrow_start: str = "none"
    arrow_end: str = "none"
    relation_kind: Literal[
        "sequence", "causal", "containment", "association", "unknown"
    ] = "unknown"
    label_refs: list[str] = Field(default_factory=list)
    connection_evidence: list[str] = Field(default_factory=list)
    text: TextPayload | None = None


class ImagePayload(BaseModel):
    model_config = StrictModel

    asset: AssetRef
    crop: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    rotation_deg: float = 0.0
    source_role: Literal[
        "photo", "logo", "illustration", "data_chart", "diagram", "unknown"
    ] = "unknown"
    alt_text: str | None = None
    linked_uri: str | None = None


class GroupPayload(BaseModel):
    model_config = StrictModel

    children: list[ObjectIR] = Field(default_factory=list)
    local_transform: Affine2D = Field(default_factory=Affine2D)
    source_child_box: RectEMU | None = None
    text: TextPayload | None = None


class UnknownPayload(BaseModel):
    model_config = StrictModel

    raw_shape_xml: AssetRef | None = None
    part_graph_root: str = ""
    object_type: str = ""
    extracted_text: list[TextPayload] = Field(default_factory=list)
    capability: Capability


SemanticSignificance = Literal["content", "decoration", "unknown"]


class ObjectIR(BaseModel):
    model_config = StrictModel

    id: str
    source_ref: SourceRef
    kind: Literal["text", "table", "chart", "shape", "connector", "image", "group", "unknown"]
    payload: Annotated[
        ChartUnion
        | TablePayload
        | ShapePayload
        | ConnectorPayload
        | ImagePayload
        | GroupPayload
        | UnknownPayload
        | TextPayload,
        Field(discriminator=None),
    ]
    local_box: RectEMU | None = None
    slide_transform: Affine2D | None = None
    z_order: int = 0
    visible: bool = True
    hidden_reason: str | None = None
    capability: Capability = Field(default_factory=lambda: Capability(level="native_full"))
    semantic_significance: SemanticSignificance = "unknown"
    classification_evidence: list[str] = Field(default_factory=list)
    name: str = ""


class RelationshipIR(BaseModel):
    model_config = StrictModel

    owner_part: str
    rel_id: str
    rel_type: str
    target_mode: Literal["internal", "external"] = "internal"
    target: str
    resolved_part: str | None = None


class PartIR(BaseModel):
    model_config = StrictModel

    part_name: str
    content_type: str
    asset: AssetRef | None = None
    relationships: list[RelationshipIR] = Field(default_factory=list)


class PackageManifest(BaseModel):
    model_config = StrictModel

    parts: list[PartIR] = Field(default_factory=list)
    root_relationships: list[RelationshipIR] = Field(default_factory=list)
    slide_order: list[str] = Field(default_factory=list)
    content_types_asset: AssetRef | None = None


class SlideIR(BaseModel):
    model_config = StrictModel

    id: str
    source_index: int
    slide_part: str | None = None
    title_ref: str | None = None
    objects: list[ObjectIR] = Field(default_factory=list)
    notes: list[ParagraphIR] = Field(default_factory=list)
    hidden: bool = False
    source_master_part: str | None = None
    source_layout_part: str | None = None
    source_canvas: RectEMU
    relationships: list[RelationshipIR] = Field(default_factory=list)
    uncertainties: list[Uncertainty] = Field(default_factory=list)


class SourceDeckIR(BaseModel):
    model_config = StrictModel

    schema_version: str = "1.0"
    input_kind: Literal["pptx", "document", "prompt"]
    source_sha256: str
    source_filename: str | None = None
    slides: list[SlideIR] = Field(default_factory=list)
    assets: list[AssetRef] = Field(default_factory=list)
    package_manifest: PackageManifest | None = None
    source_properties: dict[str, str] = Field(default_factory=dict)
    import_issues: list[Issue] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Preservation ledger (spec §7.7, §16.1)
# ---------------------------------------------------------------------------

LedgerAtomKind = Literal[
    "text", "number", "unit", "condition", "footnote", "table_cell", "chart_point",
    "diagram_node", "diagram_edge", "asset", "notes", "hyperlink",
]


class LedgerAtom(BaseModel):
    model_config = StrictModel

    id: str
    source_ref: EvidenceRef
    kind: LedgerAtomKind
    canonical_value: str
    visibility: Literal["visible", "hidden", "notes"] = "visible"
    required: bool = True
    permitted_transforms: list[str] = Field(default_factory=list)


class DecorationDecision(BaseModel):
    model_config = StrictModel

    object_id: str
    reason: str
    evidence: list[str] = Field(default_factory=list)
    policy_rule_id: str = ""
    requires_review: bool = True


class SourceLedger(BaseModel):
    model_config = StrictModel

    atoms: list[LedgerAtom] = Field(default_factory=list)
    excluded_decoration: list[DecorationDecision] = Field(default_factory=list)
    source_order: list[str] = Field(default_factory=list)


class OutputBinding(BaseModel):
    model_config = StrictModel

    source_atom_id: str
    output_slide_id: str
    output_part: str
    output_shape_id: int
    output_subpath: str = ""
    transform: Literal[
        "identity", "wrap", "title_uppercase", "style_only", "relocate", "authorized_rewrite"
    ] = "identity"
    evidence: list[str] = Field(default_factory=list)


class OutputObjectMap(BaseModel):
    model_config = StrictModel

    bindings: list[OutputBinding] = Field(default_factory=list)
    output_ids_by_source: dict[str, list[str]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Stage 1: import artifacts
# ---------------------------------------------------------------------------

class ImportArtifactManifest(BaseModel):
    model_config = StrictModel

    source_ir: ArtifactRef | None = None
    source_ledger: ArtifactRef | None = None
    source_package_manifest: ArtifactRef | None = None
    support_report: ArtifactRef | None = None
    link_resolution: ArtifactRef | None = None


# ---------------------------------------------------------------------------
# Stage 1: workbook snapshot for chart comparison
# ---------------------------------------------------------------------------

class WorkbookCellSnapshot(BaseModel):
    model_config = StrictModel

    cell_ref: str
    value: TypedValue
    formula: str | None = None


class WorkbookSheetSnapshot(BaseModel):
    model_config = StrictModel

    sheet_name: str
    cells: list[WorkbookCellSnapshot] = Field(default_factory=list)
    source_ref: str = ""


class WorkbookSnapshot(BaseModel):
    model_config = StrictModel

    workbook_asset: AssetRef
    sheets: list[WorkbookSheetSnapshot] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 2: ontology documents and normative statements
# ---------------------------------------------------------------------------

class OntologyDocument(BaseModel):
    model_config = StrictModel

    source_kind: Literal["json", "embedded_markdown", "external_markdown"]
    path: Path | None = None
    source_hash: str
    content: str


class NormativeRef(BaseModel):
    model_config = StrictModel

    source_kind: Literal["json", "embedded_markdown", "external_markdown"]
    relative_path: str
    json_pointer: str | None = None
    heading: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    source_hash: str


class NormativeStatement(BaseModel):
    model_config = StrictModel

    id: str
    subject: str
    property: str
    operator: str
    typed_value: str
    applicability: str = ""
    modality: Literal["must", "must_not", "conditional", "evidence"]
    rule_id: str = ""
    catalog_id: str = ""
    refs: list[NormativeRef] = Field(default_factory=list)
    extraction_status: Literal["parsed", "unparsed", "ambiguous"] = "parsed"


class NormativeExtraction(BaseModel):
    model_config = StrictModel

    statements: list[NormativeStatement] = Field(default_factory=list)
    sections_examined: list[str] = Field(default_factory=list)
    evidence_sections: list[str] = Field(default_factory=list)
    unparsed_normative_sections: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class NormalizedPredicate(BaseModel):
    """Typed predicate for normative comparison (Stage 3 §2.1).

    operator: eq/ge/le/in/interval/forbid/unknown.
    values: typed Decimal strings for exact comparison.
    scope: normalized applicability scope (e.g. all/body/footnote/conditional).
    """

    model_config = StrictModel

    subject: str = ""
    property: str = ""
    value_domain: str = "unknown"
    operator: Literal["eq", "ge", "le", "in", "interval", "forbid", "unknown"] = "unknown"
    values: list[str] = Field(default_factory=list)
    unit: str = ""
    scope: str = "all"
    source_refs: list[str] = Field(default_factory=list)
    authority: str = ""


class ScopeComparison(BaseModel):
    """Scope overlap decision for two predicates (Stage 3 §2.1)."""

    model_config = StrictModel

    left_scope: str = "all"
    right_scope: str = "all"
    overlap: bool | None = None
    reason: str = ""


class PredicateComparison(BaseModel):
    """Result of comparing two normalized predicates (Stage 3 §2.1)."""

    model_config = StrictModel

    relation: Literal[
        "equivalent", "compatible_refinement", "disjoint_scope",
        "contradiction", "unresolved",
    ] = "unresolved"
    intersection: str = ""
    reason: str = ""
    refs: list[str] = Field(default_factory=list)


class OntologyConflict(BaseModel):
    model_config = StrictModel

    id: str
    subject: str
    property: str
    left: NormativeStatement | None = None
    right: NormativeStatement | None = None
    kind: Literal[
        "contradiction", "compatible_refinement", "disjoint_scope", "unresolved"
    ]
    blocking: bool
    explanation: str


class OntologyConflictReport(BaseModel):
    model_config = StrictModel

    source_hashes: dict[str, str] = Field(default_factory=dict)
    documents_differ: bool = False
    equivalent: list[str] = Field(default_factory=list)
    compatible_additions: list[str] = Field(default_factory=list)
    compatible_refinements: list[str] = Field(default_factory=list)
    conflicts: list[OntologyConflict] = Field(default_factory=list)
    unparsed_normative_sections: list[str] = Field(default_factory=list)
    resolved_rule_ids: list[str] = Field(default_factory=list)
    ready_for_compilation: bool = False


# ---------------------------------------------------------------------------
# Stage 2: compiled ontology
# ---------------------------------------------------------------------------

class RoleStyle(BaseModel):
    model_config = StrictModel

    role: str
    allowed_faces: list[str] = Field(default_factory=list)
    default_size_pt: float | None = None
    allowed_sizes_pt: list[float] = Field(default_factory=list)
    color_tokens: list[str] = Field(default_factory=list)
    # None = the loaded JSON does not declare the property for this role.
    # An absent property is unresolved, not False (§4.2).
    uppercase: bool | None = None
    font_face_policy_id: str = ""
    applicability: str = ""
    status: Literal[
        "core_observed",
        "core_observed_through_active_layout",
        "observed_variant",
        "description_only",
    ] = "description_only"
    evidence_refs: list[str] = Field(default_factory=list)


class ColorCondition(BaseModel):
    model_config = StrictModel

    token: str
    rgb: str
    allowed_context_ids: list[str] = Field(default_factory=list)
    required_evidence_refs: list[str] = Field(default_factory=list)


class ParagraphSpacing(BaseModel):
    model_config = StrictModel

    before_pt: float | None = None
    after_pt: float | None = None
    line_spacing_mode: Literal["multiple", "points"] | None = None
    line_spacing_value: float | None = None


class ListLevelProfile(BaseModel):
    model_config = StrictModel

    level: int
    size_pt: float | None = None
    typeface: str | None = None
    text_token: str | None = None
    marker_color_mode: Literal["fixed", "follow_paragraph_text"] = "follow_paragraph_text"
    marker_color_token: str | None = None
    mar_left_emu: int | None = None
    hanging_indent_emu: int | None = None
    spacing: ParagraphSpacing = Field(default_factory=ParagraphSpacing)
    source_ref: str = ""
    derived: bool = False
    derivation_rule: str = ""


class ListStyleProfile(BaseModel):
    model_config = StrictModel

    id: str
    representation: str = ""
    marker_settings: dict[str, str] = Field(default_factory=dict)
    color_rules: list[dict[str, Any]] = Field(default_factory=list)
    level_profiles: list[ListLevelProfile] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    nested_size_policy: NestedSizePolicy | None = None
    nested_text_color: str | None = None
    nested_text_color_token: str | None = None
    list_text_typeface: str | None = None
    list_text_primary_color: str | None = None


class CatalogRecord(BaseModel):
    model_config = StrictModel

    id: str
    kind: str
    name: str = ""
    semantic_features: list[str] = Field(default_factory=list)
    input_data: str = ""
    encoding: str = ""
    recipe: str = ""
    pitfalls: list[str] = Field(default_factory=list)
    provenance_status: Literal["observed", "adapted", "extrapolated", "unknown"] = "unknown"
    source_refs: list[str] = Field(default_factory=list)
    renderer_id: str | None = None
    implementation_status: Literal["ready", "partial", "description_only"] = "description_only"


class RuleSpec(BaseModel):
    model_config = StrictModel

    id: str
    severity: Literal["hard", "contextual"]
    condition: str
    remedy: str
    applicability: str = ""
    source_refs: list[str] = Field(default_factory=list)


class RuleBinding(BaseModel):
    model_config = StrictModel

    rule_id: str
    checker_key: str
    check_kind: Literal["deterministic", "hybrid"]
    supported_scopes: list[str] = Field(default_factory=list)
    implementation_status: Literal[
        "implemented", "partial", "deferred", "needs_binding"
    ] = "deferred"
    required_evidence: list[str] = Field(default_factory=list)
    deferred_stage: int | None = None
    condition_fingerprint: str = ""
    unresolved_reason: str = ""


class RuleRegistry(BaseModel):
    model_config = StrictModel

    specs: dict[str, RuleSpec] = Field(default_factory=dict)
    bindings: dict[str, RuleBinding] = Field(default_factory=dict)
    registry_version: str = "1.0"


class RuleBindingReportEntry(BaseModel):
    """One reviewed/unreviewed semantic binding (Stage 3.5 §5.1)."""

    model_config = StrictModel

    rule_id: str
    source_refs: list[str] = Field(default_factory=list)
    normalized_condition: str = ""
    condition_fingerprint: str = ""
    severity: Literal["hard", "contextual"] = "hard"
    scope: str = ""
    checker_type: str = "hybrid"
    checker_key: str = ""
    parameter_refs: list[str] = Field(default_factory=list)
    implementation_status: str = "deferred"
    checked_scopes: list[str] = Field(default_factory=list)
    unresolved_reason: str = ""


class RuleBindingReport(BaseModel):
    """Binding report artifact: reviewed links, fingerprints, unresolved norms."""

    model_config = StrictModel

    library_source: str = ""
    library_reviewed_ontology_sha256: str = ""
    registry_version: str = "1.0"
    entries: list[RuleBindingReportEntry] = Field(default_factory=list)

    @property
    def unresolved_rule_ids(self) -> list[str]:
        return [
            e.rule_id for e in self.entries
            if e.implementation_status in ("needs_binding", "deferred")
            and e.severity == "hard"
        ]


class RuleCoverage(BaseModel):
    model_config = StrictModel

    rule_id: str
    context: str = ""
    subject_id: str = ""
    check_kind: Literal["deterministic", "hybrid"]
    result: Literal["pass", "fail", "unknown", "not_applicable"]
    evidence_refs: list[str] = Field(default_factory=list)
    reason: str = ""
    blocking: bool = False
    checked_scopes: list[str] = Field(default_factory=list)


class RuleReadinessReport(BaseModel):
    model_config = StrictModel

    rules_evaluated: int = 0
    fails: list[str] = Field(default_factory=list)
    blocking_unknowns: list[str] = Field(default_factory=list)
    not_applicable_with_evidence: list[str] = Field(default_factory=list)
    ready_for_this_scope: bool = False
    strict_output_ready: bool = False


class ExtensionContract(BaseModel):
    model_config = StrictModel

    required_fields: list[str] = Field(default_factory=list)
    style_new_tokens_allowed: bool = False
    semantic_new_geometry_allowed: bool = False
    requires_render_review: bool = False
    requires_data_invariants: bool = False


class FontAsset(BaseModel):
    model_config = StrictModel

    asset: AssetRef | None = None
    typeface: str = ""
    postscript_name: str = ""
    family: str = ""
    subfamily: str = ""
    collection_index: int | None = None
    cmap_coverage: dict[str, bool] = Field(default_factory=dict)
    symbol_mapping: dict[str, str] = Field(default_factory=dict)
    fs_type: int | None = None
    embedding_rights: str = "unknown"
    browser_verified: bool = False
    renderer_verified: bool = False


class FontInventory(BaseModel):
    model_config = StrictModel

    faces: dict[str, FontAsset] = Field(default_factory=dict)
    marker_faces: dict[str, FontAsset] = Field(default_factory=dict)
    missing_faces: list[str] = Field(default_factory=list)
    unverified_faces: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class ProtectedObject(BaseModel):
    model_config = StrictModel

    source_ref: str
    scope: Literal["master", "layout", "slide"]
    semantic_role: str
    xml_hash: str = ""
    asset_hashes: list[str] = Field(default_factory=list)
    mutable_fields: list[str] = Field(default_factory=list)


class LayoutProfile(BaseModel):
    model_config = StrictModel

    id: str
    source_layout_part: str = ""
    placeholder_roles: dict[int, str] = Field(default_factory=dict)
    title_box: RectPt | None = None
    content_box: RectPt | None = None
    footer_boxes: list[RectPt] = Field(default_factory=list)
    protected_regions: list[RectPt] = Field(default_factory=list)
    context: str = ""
    evidence: list[str] = Field(default_factory=list)
    is_verified: bool = False


class TemplateProfile(BaseModel):
    model_config = StrictModel

    template_hash: str = ""
    source_library_hash: str = ""
    source_library_path: str = ""
    ontology_corpus_hash: str = ""
    canvas: RectEMU = Field(default_factory=RectEMU)
    layout_profiles: list[LayoutProfile] = Field(default_factory=list)
    protected_objects: list[ProtectedObject] = Field(default_factory=list)
    fonts: FontInventory = Field(default_factory=FontInventory)
    list_profiles: list[ListStyleProfile] = Field(default_factory=list)
    master_parts: list[str] = Field(default_factory=list)
    theme_parts: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    structure_verified: bool = False


class TemplateDerivationReport(BaseModel):
    model_config = StrictModel

    source_path: str = ""
    source_hash: str = ""
    output_path: str = ""
    output_hash: str = ""
    selected_layout_ids: list[str] = Field(default_factory=list)
    copied_parts: list[str] = Field(default_factory=list)
    preserved_objects: list[str] = Field(default_factory=list)
    removed_demo_objects: list[str] = Field(default_factory=list)
    relationship_checks: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class ContentBoxResolution(BaseModel):
    model_config = StrictModel

    content_box: RectEMU | None = None
    protected_regions: list[RectEMU] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    verified: bool = False
    issues: list[Issue] = Field(default_factory=list)


class ReferenceStyleEvidence(BaseModel):
    model_config = StrictModel

    source_slide_ref: str = ""
    source_hash: str = ""
    checks: list[str] = Field(default_factory=list)
    allowed_style_hints: list[str] = Field(default_factory=list)
    excluded_style_hints: list[str] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    composition_reusable: bool = False


class CorporateProfileResult(BaseModel):
    model_config = StrictModel

    rules_compiled: bool = False
    structure_verified: bool = False
    rule_coverage_complete_for_stage2: bool = False
    assets_ready: bool = False
    strict_output_ready: bool = False
    status: Literal["completed", "needs_assets", "needs_review", "invalid_input"] = "needs_review"
    blockers: list[Issue] = Field(default_factory=list)
    artifact_manifest: dict[str, str] = Field(default_factory=dict)


class MinimumFontSize(BaseModel):
    model_config = StrictModel

    value_pt: float
    severity: str = "hard"
    applies_to: str = ""
    scope_note: str = ""
    source: str = ""
    remedy: str = ""


class FontPolicyRule(BaseModel):
    model_config = StrictModel

    role: str
    typeface: str
    bold: bool = False
    exception: bool = False


class FontForbiddenCombination(BaseModel):
    model_config = StrictModel

    typeface: str
    bold: bool
    replace_with_typeface: str
    replace_with_bold: bool = False


class NestedSizePolicy(BaseModel):
    model_config = StrictModel

    if_parent_gte: float
    then_subtract: float
    otherwise_subtract: float
    minimum: float
    parent_at_minimum_action: str = ""


class CompiledOntology(BaseModel):
    model_config = StrictModel

    schema_version: str = "1.0"
    source_hash: str = ""
    ontology_corpus_hash: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)
    canvas: RectEMU = Field(default_factory=RectEMU)
    roles: dict[str, RoleStyle] = Field(default_factory=dict)
    colors: dict[str, str] = Field(default_factory=dict)
    conditional_colors: dict[str, ColorCondition] = Field(default_factory=dict)
    font_policy: dict[str, object] = Field(default_factory=dict)
    font_policy_rules: list[FontPolicyRule] = Field(default_factory=list)
    font_forbidden_combinations: list[FontForbiddenCombination] = Field(default_factory=list)
    minimum_text_font_size: MinimumFontSize | None = None
    composition_operators: list[str] = Field(default_factory=list)
    lists: dict[str, ListStyleProfile] = Field(default_factory=dict)
    catalog_records: list[CatalogRecord] = Field(default_factory=list)
    rule_registry: RuleRegistry = Field(default_factory=RuleRegistry)
    extension_contract: ExtensionContract = Field(default_factory=ExtensionContract)
    compilation_issues: list[Issue] = Field(default_factory=list)
