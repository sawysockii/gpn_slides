"""Stage 1 tests: chart cache↔workbook comparison (spec §4.3)."""

from __future__ import annotations

from slides_cli.gpn.importer import (
    collect_import_uncertainties,
    compare_chart_bindings,
)
from slides_cli.gpn.models import (
    ChartPayload,
    ChartPointIR,
    ChartSeriesIR,
    RectEMU,
    SlideIR,
    SourceDeckIR,
    TypedValue,
    WorkbookCellSnapshot,
    WorkbookSheetSnapshot,
    WorkbookSnapshot,
)


def _chart() -> ChartPayload:
    return ChartPayload(
        chart_part="ppt/charts/chart1.xml",
        chart_type="barChart",
        series=[
            ChartSeriesIR(
                id="ser0",
                name="Plan",
                source_index=0,
                points=[
                    ChartPointIR(
                        id="p0", index=0,
                        value=TypedValue(state="present", decimal="10.5",
                                        display_text="10.5"),
                    ),
                ],
                formula_refs=["Sheet1!$B$2"],
            )
        ],
        part_graph_root="ppt/charts/chart1.xml",
    )


def _workbook(value: str, formula: str | None = "10.5") -> WorkbookSnapshot:
    from slides_cli.gpn.models import AssetRef

    return WorkbookSnapshot(
        workbook_asset=AssetRef(sha256="x" * 64, relative_path="wb.xlsx",
                                media_type="application/xlsx", byte_count=1),
        sheets=[
            WorkbookSheetSnapshot(
                sheet_name="Sheet1",
                cells=[
                    WorkbookCellSnapshot(
                        cell_ref="B2",
                        value=TypedValue(state="present", decimal=value,
                                        display_text=value),
                        formula=formula,
                    )
                ],
            )
        ],
    )


def _slide() -> SlideIR:
    return SlideIR(id="slide0", source_index=0,
                   source_canvas=RectEMU(x=0, y=0, w=12192000, h=6858000))


def test_equal_values_no_uncertainty() -> None:
    comps = compare_chart_bindings(_chart(), workbook=_workbook("10.5"))
    assert comps
    assert all(c.status == "equal" for c in comps)
    assert collect_import_uncertainties(_slide(), comps) == []


def test_numeric_1_vs_10_equivalent() -> None:
    chart = _chart()
    chart.series[0].points[0].value = TypedValue(
        state="present", decimal="1", display_text="1")
    comps = compare_chart_bindings(chart, workbook=_workbook("1.0"))
    assert all(c.status == "equal" for c in comps)


def test_conflict_is_blocking_uncertainty() -> None:
    comps = compare_chart_bindings(_chart(), workbook=_workbook("11.0"))
    assert any(c.status == "conflict" for c in comps)
    slide = _slide()
    uncertainties = collect_import_uncertainties(slide, comps)
    assert uncertainties
    assert all(u.blocking for u in uncertainties)
    assert any(u.code == "CHART_CACHE_WORKBOOK_CONFLICT" for u in uncertainties)


def test_missing_formula_cache_is_unverifiable_not_mismatch() -> None:
    chart = _chart()
    chart.series[0].formula_refs = ["Sheet1!$Z$99"]
    comps = compare_chart_bindings(chart, workbook=_workbook("10.5"))
    assert any(c.status == "unverifiable" for c in comps)
    assert not any(c.status == "conflict" for c in comps)
    uncertainties = collect_import_uncertainties(_slide(), comps)
    assert any(u.blocking for u in uncertainties)


def test_blank_vs_zero_stay_different() -> None:
    chart = _chart()
    chart.series[0].points[0].value = TypedValue(state="missing", display_text="")
    comps = compare_chart_bindings(chart, workbook=_workbook("0"))
    # missing cached vs present workbook value must not compare equal
    assert not any(c.status == "equal" for c in comps)


def test_quoted_sheet_name_supported() -> None:
    chart = _chart()
    chart.series[0].formula_refs = ["'My Sheet'!$B$2"]
    wb = _workbook("10.5")
    wb.sheets[0].sheet_name = "My Sheet"
    comps = compare_chart_bindings(chart, workbook=wb)
    assert any(c.status == "equal" for c in comps)


def test_literal_data_without_workbook_is_not_conflict() -> None:
    chart = _chart()
    chart.series[0].formula_refs = []
    comps = compare_chart_bindings(chart, workbook=None)
    assert not any(c.status == "conflict" for c in comps)


def test_deck_model_accepts_uncertainties() -> None:
    slide = _slide()
    comps = compare_chart_bindings(_chart(), workbook=_workbook("11.0"))
    slide.uncertainties = collect_import_uncertainties(slide, comps)
    deck = SourceDeckIR(input_kind="pptx", source_sha256="s", slides=[slide])
    assert deck.slides[0].uncertainties
    assert deck.slides[0].uncertainties[0].blocking
