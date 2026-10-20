"""Expected-first presence/canvas checks for full or selected source slides.

Source atoms define the expectation. Saved output bindings locate them;
merged target-node IDs never replace the source identity. Text correctness,
fit and occlusion remain the responsibility of their separate saved gates.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _address(value: dict) -> tuple[str, int, tuple[int, ...]] | None:
    try:
        part, shape = str(value["slide_part"]), int(value["shape_id"])
        path = tuple(int(i) for i in value.get("group_path", []))
    except (KeyError, TypeError, ValueError):
        return None
    return (part, shape, path) if part and shape > 0 else None


def _objects(slides: list[dict]):
    def visit(items):
        for obj in items:
            yield obj
            yield from visit(obj.get("payload", {}).get("children", []))

    for slide in slides:
        yield from visit(slide.get("objects", []))


def check_run_visibility(
    run_dir: Path, after_deck: Any, *, selected_slide_ids: list[str] | None = None
) -> dict[str, Any]:
    from .geometry import check_required_content_visibility

    root = Path(run_dir)
    report: dict[str, Any] = {
        "per_object": {},
        "missing": [],
        "unknown_required": [],
        "outside_proofs": [],
        "result": "unknown",
        "scope": "saved_presence_and_canvas_intersection_only",
        "candidate_sha256": str(getattr(after_deck, "source_sha256", "")),
        "source_scope": "full_source" if selected_slide_ids is None else "selected_source_slides",
        "omitted_authorized_parts": [],
        "notes_atoms_checked_elsewhere": 0,
    }
    hashes = {}
    try:
        docs = {}
        for name in (
            "source_ir.json",
            "source_ledger.json",
            "emission_report.json",
            "saved_geometry_report.json",
        ):
            raw = (root / name).read_bytes()
            hashes[name] = hashlib.sha256(raw).hexdigest()
            docs[name] = json.loads(raw)
        source, ledger = docs["source_ir.json"], docs["source_ledger.json"]
        slide_parts = {s["id"]: s.get("slide_part") for s in source["slides"]}
        if len(slide_parts) != len(source["slides"]):
            raise ValueError("SOURCE_SLIDE_ID_DUPLICATE")
        if selected_slide_ids is None:
            selected = list(slide_parts)
        else:
            if not isinstance(selected_slide_ids, list) or not selected_slide_ids:
                raise ValueError("VISIBILITY_SCOPE_EMPTY_OR_INVALID")
            if len(set(selected_slide_ids)) != len(selected_slide_ids):
                raise ValueError("VISIBILITY_SCOPE_DUPLICATE")
            if set(selected_slide_ids) - set(slide_parts):
                raise ValueError("VISIBILITY_SCOPE_UNKNOWN_SLIDE")
            selected = selected_slide_ids
        wanted_parts = {slide_parts[s] for s in selected}
        all_parts = set(slide_parts.values())
        active, outside_scope, notes = [], [], 0
        for atom in ledger.get("atoms", []):
            if not atom.get("required"):
                continue
            ref = atom.get("source_ref", {})
            part = ref.get("slide_part")
            if selected_slide_ids is not None:
                if part not in all_parts or not part:
                    report["unknown_required"].append(atom["id"])
                    continue
                if part not in wanted_parts:
                    outside_scope.append(atom["id"])
                    continue
            if atom.get("kind") == "notes":
                notes += 1
            else:
                active.append(atom)
        report.update(
            {
                "selected_source_slide_ids": selected,
                "expected_source_atom_ids": [a["id"] for a in active],
                "expected_source_atom_count": len(active),
                "out_of_scope_required_atoms": outside_scope,
                "notes_atoms_checked_elsewhere": notes,
                "required_source_parts": len(
                    {
                        (
                            a.get("source_ref", {}).get("slide_part"),
                            a.get("source_ref", {}).get("shape_id"),
                        )
                        for a in active
                    }
                ),
            }
        )
        source_objects = list(_objects(source["slides"]))
        source_at_address = {_address(o.get("source_ref", {})): o["id"] for o in source_objects}
        legacy = {}
        for record in docs["emission_report.json"].get("records", []):
            legacy.setdefault(record.get("source_object_id"), []).append(record.get("address", {}))
        map_path = root / "output_object_map.json"
        atom_targets: dict[str, list[dict]] = {}
        if map_path.is_file():
            raw = map_path.read_bytes()
            hashes[map_path.name] = hashlib.sha256(raw).hexdigest()
            mapping = json.loads(raw)
            if not isinstance(mapping.get("bindings"), list):
                raise ValueError("OUTPUT_BINDING_DOCUMENT_INVALID")
            for binding in mapping["bindings"]:
                atom_targets.setdefault(binding.get("source_atom_id"), []).append(
                    binding.get("target_address", {})
                )
            report["binding_source"] = "output_object_map.source_atom_id"
        else:
            report["binding_source"] = "legacy_emission_exact_source_object"
        authorized = set()
        classification = root / "classification_and_transformation_ledger.json"
        if classification.is_file():
            raw = classification.read_bytes()
            hashes[classification.name] = hashlib.sha256(raw).hexdigest()
            for decision in json.loads(raw).get("decisions", []):
                if decision.get("authorization_ref") and (
                    decision.get("action") == "omit"
                    or decision.get("action") in ("recompose", "replace")
                    and decision.get("target_relation_ids")
                ):
                    authorized.add(decision.get("source_object_id"))
        after_at_address = {}
        for slide in getattr(after_deck, "slides", []) or []:
            for obj in getattr(slide, "objects", []) or []:
                ref = getattr(obj, "source_ref", None)
                key = _address(
                    {
                        "slide_part": getattr(ref, "slide_part", None) or slide.slide_part,
                        "shape_id": getattr(ref, "shape_id", -1),
                        "group_path": getattr(ref, "group_path", []),
                    }
                )
                if key in after_at_address:
                    raise ValueError("SAVED_SHAPE_ADDRESS_AMBIGUOUS")
                after_at_address[key] = obj.id
        required_after = set()
        mapping_evidence = []
        for atom in active:
            key = _address(atom.get("source_ref", {}))
            oid = source_at_address.get(key) if key is not None else None
            targets = (
                atom_targets.get(atom["id"], []) if map_path.is_file() else legacy.get(oid, [])
            )
            if not targets:
                if oid is not None and oid in authorized:
                    report["omitted_authorized_parts"].append(atom["id"])
                else:
                    report["missing"].append(f"{atom['id']}:no_saved_binding")
                continue
            for address in targets:
                saved_id = after_at_address.get(_address(address))
                if saved_id is None:
                    report["missing"].append(f"{atom['id']}:saved_target_absent")
                    continue
                required_after.add(saved_id)
                mapping_evidence.append(
                    {
                        "source_atom_id": atom["id"],
                        "saved_object_id": saved_id,
                        "target_address": address,
                    }
                )
        canvas = check_required_content_visibility(
            after_deck, docs["saved_geometry_report.json"], sorted(required_after)
        )
        report["per_object"] = canvas["per_object"]
        report["missing"].extend(canvas["missing"])
        report["unknown_required"].extend(canvas["unknown_required"])
        report["outside_proofs"] = canvas["outside_proofs"]
        report["required_after_ids"] = len(required_after)
        report["mapping_evidence"] = mapping_evidence
        if report["missing"]:
            report["result"] = "fail"
        elif report["unknown_required"] or not active or not required_after:
            report["result"] = "unknown"
        else:
            report["result"] = canvas["result"]
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        report["result"] = "unknown"
        report["reason"] = f"visibility inputs invalid: {exc}"
    report["input_hashes"] = hashes
    return report
