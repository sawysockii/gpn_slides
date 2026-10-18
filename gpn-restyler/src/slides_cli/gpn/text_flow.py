"""One source-to-rich-paragraph projection for measurement AND emission."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def measurement_paragraphs(paragraphs: list[dict]) -> list[dict]:
    """Project the rich source spec into the strict measurement wire model.

    Field/link metadata and span bindings remain in the canonical spec for
    the native writer. The browser receives their visible cached text and
    the exact same run/paragraph boundaries, roles and resolved styles.
    """
    from .models import MeasureParagraph, MeasureRun

    result = []
    for paragraph in paragraphs:
        projected = {
            key: deepcopy(value)
            for key, value in paragraph.items()
            if key in MeasureParagraph.model_fields and key != "runs"
        }
        projected["runs"] = [
            {key: deepcopy(value) for key, value in run.items() if key in MeasureRun.model_fields}
            for run in paragraph.get("runs", [])
        ]
        result.append(MeasureParagraph.model_validate(projected).model_dump(mode="json"))
    return result


def build_rich_paragraphs(
    objects: dict[str, Any],
    refs: list[str],
    ref_roles: dict[str, str] | None = None,
    paragraph_groups: list[list[str]] | None = None,
    inline_separator: str = " ",
) -> list[dict]:
    from .layout import _object_paragraphs

    if inline_separator not in ("", " "):
        raise ValueError("TEXT_FLOW_SEPARATOR_INVALID")
    roles = ref_roles or {}
    groups = paragraph_groups or [[ref] for ref in refs]
    flattened = [ref for group in groups for ref in group]
    if set(flattened) != set(refs) or len(flattened) != len(set(flattened)):
        raise ValueError("TEXT_FLOW_COVERAGE_MISMATCH")
    source = {}
    for ref in refs:
        if ref not in objects:
            raise ValueError(f"TEXT_FLOW_OBJECT_MISSING:{ref}")
        paragraphs = deepcopy(_object_paragraphs(objects[ref]))
        if not paragraphs:
            raise ValueError(f"TEXT_FLOW_NOT_TEXT:{ref}")
        for index, paragraph in enumerate(paragraphs):
            paragraph["source_ref"] = ref
            paragraph["source_paragraph_index"] = index
            paragraph["role"] = roles.get(ref, "")
            if not paragraph.get("id"):
                raise ValueError(f"SOURCE_PARAGRAPH_ID_MISSING:{ref}:{index}")
            for run in paragraph["runs"]:
                if not run.get("id"):
                    raise ValueError(f"SOURCE_RUN_ID_MISSING:{ref}:{index}")
                run["source_para_id"] = paragraph["id"]
                run["source_ref"] = ref
                run["role"] = roles.get(ref, "")
        source[ref] = paragraphs
    output = []
    for group in groups:
        if len(group) == 1:
            output.extend(source[group[0]])
            continue
        if any(len(source[ref]) != 1 or source[ref][0].get("list_kind") != "none" for ref in group):
            raise ValueError(f"TEXT_FLOW_AMBIGUOUS_PARAGRAPHS:{group}")
        merged = deepcopy(source[group[0]][0])
        merged["runs"] = []
        for index, ref in enumerate(group):
            if index and inline_separator:
                merged["runs"].append(
                    {
                        "id": f"flow-separator:{group[0]}:{index}",
                        "text": inline_separator,
                        "style": {},
                        "kind": "text",
                        "source_para_id": "",
                        "source_ref": "",
                        "role": "",
                        "authoring_whitespace": True,
                    }
                )
            merged["runs"].extend(source[ref][0]["runs"])
        output.append(merged)
    return output
