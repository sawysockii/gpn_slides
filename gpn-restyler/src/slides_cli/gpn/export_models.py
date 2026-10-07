"""Stage 4 native-exporter contracts (spec §§2–3, 12).

Pydantic models ( codebase convention) for the exporter: part preservation
contracts, transplant/relationship maps, emission records, preflight reports
and the ``NativeBuildResult``. No normative values live here — every style
payload is loaded from the ``CompiledOntology`` snapshot at runtime.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

StrictModel = ConfigDict(extra="forbid")

PreservationMode = Literal[
    "binary_exact", "xml_allowed_rewrite", "replacement", "derived", "excluded"
]

EmissionStrategy = Literal[
    "native_rebuild", "native_transplant", "opaque_transplant", "unsupported"
]

ExportWorkflow = Literal["presentation", "document"]


class SourcePartKey(BaseModel):
    """Source-qualified part identity (package hash + part name)."""

    model_config = StrictModel

    package_sha256: str
    part_name: str


class OutputShapeAddress(BaseModel):
    """Address of one emitted output shape."""

    model_config = StrictModel

    slide_part: str
    shape_id: int
    group_path: list[int] = Field(default_factory=list)
    kind: str


class OutputSubBinding(BaseModel):
    """One source-atom → output-subaddress binding."""

    model_config = StrictModel

    source_atom_id: str
    target_address: OutputShapeAddress
    target_subaddress: str = ""
    origin: str = "exporter"
    transform: str = "identity"
    verification_state: Literal["unverified", "verified", "failed"] = "unverified"
    evidence: list[str] = Field(default_factory=list)


class PartPreservationContract(BaseModel):
    """How one part must be treated during export."""

    model_config = StrictModel

    source: SourcePartKey | None = None
    target_part: str | None = None
    mode: PreservationMode
    allowed_rewrite_paths: list[str] = Field(default_factory=list)
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class RelationshipKey(BaseModel):
    model_config = StrictModel

    source_package_hash: str
    owner_part: str
    rid: str


class RelationshipMapping(BaseModel):
    model_config = StrictModel

    source_key: RelationshipKey
    target_owner: str
    target_rid: str
    target_part_or_uri: str = ""
    target_mode: Literal["internal", "external"] = "internal"
    relationship_type: str = ""
    handling: str = ""
    evidence: list[str] = Field(default_factory=list)


class TransplantIssue(BaseModel):
    model_config = StrictModel

    code: str
    details: str = ""
    subject_ids: list[str] = Field(default_factory=list)


class PartClosurePlan(BaseModel):
    model_config = StrictModel

    root: str
    source_package_hash: str
    part_keys: list[SourcePartKey] = Field(default_factory=list)
    edge_handling: dict[str, str] = Field(default_factory=dict)
    contracts: list[PartPreservationContract] = Field(default_factory=list)
    unknown_edges: list[str] = Field(default_factory=list)
    issues: list[TransplantIssue] = Field(default_factory=list)


class TransplantResult(BaseModel):
    model_config = StrictModel

    root_part: str
    actual_root_part: str = ""
    part_map: dict[str, str] = Field(default_factory=dict)
    relationship_map: list[RelationshipMapping] = Field(default_factory=list)
    contracts: list[PartPreservationContract] = Field(default_factory=list)
    issues: list[TransplantIssue] = Field(default_factory=list)


class EmissionRecord(BaseModel):
    model_config = StrictModel

    source_object_id: str
    strategy: EmissionStrategy
    address: OutputShapeAddress | None = None
    bindings: list[OutputSubBinding] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class ExportOptions(BaseModel):
    model_config = StrictModel

    workflow: ExportWorkflow = "presentation"
    preserve_structure: bool = True
    diagnostic: bool = True
    allow_opaque_carry: bool = True
    allow_split: bool = False
    allow_cross_slide_move: bool = False
    overwrite: bool = False
    output_policy: Literal["candidate", "production"] = "candidate"


class ExportPreflightReport(BaseModel):
    model_config = StrictModel

    ok: bool = False
    source_sha256: str = ""
    rules_snapshot_id: str = ""
    template_hash: str = ""
    required_atoms: int = 0
    kind_matrix: dict[str, int] = Field(default_factory=dict)
    emission_plan: dict[str, EmissionStrategy] = Field(default_factory=dict)
    data_preservation_support: str = "unknown"
    style_normalization_support: str = "unknown"
    native_structure_support: str = "unknown"
    render_support: str = "unsupported"
    contracts: list[PartPreservationContract] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    capabilities: dict[str, str] = Field(default_factory=dict)


class ContentDiff(BaseModel):
    model_config = StrictModel

    missing: list[str] = Field(default_factory=list)
    duplicated: list[str] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)
    dangling: list[str] = Field(default_factory=list)
    unexpected_rewrites: list[str] = Field(default_factory=list)
    ok: bool = False


class PartDiff(BaseModel):
    model_config = StrictModel

    unmapped_required_parts: list[str] = Field(default_factory=list)
    missing_binary_payloads: list[str] = Field(default_factory=list)
    unexpected_xml_mutations: list[str] = Field(default_factory=list)
    changed_relationship_semantics: list[str] = Field(default_factory=list)
    template_substitution_violations: list[str] = Field(default_factory=list)
    dangling_targets: list[str] = Field(default_factory=list)
    unverified_exclusions: list[str] = Field(default_factory=list)
    ok: bool = False


class PackageGraphReport(BaseModel):
    model_config = StrictModel

    readable: bool = False
    duplicate_members: list[str] = Field(default_factory=list)
    dangling_targets: list[str] = Field(default_factory=list)
    content_type_problems: list[str] = Field(default_factory=list)
    duplicate_rids: list[str] = Field(default_factory=list)
    ok: bool = False


class NativeEditabilityReport(BaseModel):
    model_config = StrictModel

    native_structure_verified: bool = False
    data_preservation_verified: bool = False
    graph_integrity_verified: bool = False
    style_checked_scopes: list[str] = Field(default_factory=list)
    render_verified: bool = False
    powerpoint_ui_edit_verified: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class CandidateArtifact(BaseModel):
    model_config = StrictModel

    path: str = ""
    sha256: str = ""
    success: bool = False
    issues: list[str] = Field(default_factory=list)


class NativeBuildResult(BaseModel):
    """Service-operation result for ``slides gpn build`` (spec §12)."""

    model_config = StrictModel

    operation_status: Literal["completed", "needs_assets", "needs_review",
                              "invalid_input", "failed"] = "failed"
    export_scope: str = "presentation"
    llm_provider: str = "harness"
    plan_generation_origin: str = ""
    candidate_path: str | None = None
    candidate_sha256: str | None = None
    source_preservation_verified: bool = False
    package_graph_verified: bool = False
    native_structure_verified: bool = False
    style_coverage: dict[str, Any] = Field(default_factory=dict)
    unresolved_scopes: list[str] = Field(default_factory=list)
    production_assets_ready: bool = False
    render_verified: bool = False
    powerpoint_ui_edit_verified: bool = False
    strict_output_ready: bool = False
    issues: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _strict_never_implied(self) -> NativeBuildResult:
        if self.strict_output_ready and not (
            self.source_preservation_verified
            and self.package_graph_verified
            and self.native_structure_verified
            and self.production_assets_ready
        ):
            raise ValueError("strict_output_ready requires all gates + assets")
        return self


class ResolvedSlide(BaseModel):
    """Resolved layout for one slide (native exporter input)."""

    model_config = StrictModel

    slide_id: str
    source_slide_id: str = ""
    intent_id: str = ""
    generation_origin: str = ""
    zones: list[dict[str, Any]] = Field(default_factory=list)
    role_assignments: dict[str, str] = Field(default_factory=dict)
    geometry_emu: dict[str, dict[str, int]] = Field(default_factory=dict)
    diagnostic_geometry: bool = True
    issues: list[str] = Field(default_factory=list)
