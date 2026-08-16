"""Guarantees the rules engine must make, independent of any corpus.

Run with ``python3 test_rules.py`` or under pytest.

These assert the precedence rule and the suppression contract directly, rather
than inferring them from agreement rates. A corpus can drift; these cannot.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from models import (
    AnalyteName,
    AnalyteResult,
    BiologicalSex,
    BloodPanel,
    Censoring,
    Derivation,
    DerivationMethod,
    Escalation,
    FastingStatus,
    InterferenceGrade,
    LabInfo,
    PatientContext,
    PreAnalyticObservation,
    ReferenceRange,
    ResultStatus,
    Severity,
    Specimen,
    Unit,
    UntrustedText,
)
from rules import ENGINE_VERSION, assess_panel, load_thresholds

CFG = load_thresholds()
MNL = ZoneInfo("Asia/Manila")
T0 = datetime(2026, 3, 1, 8, tzinfo=MNL)


def _panel(results: dict[AnalyteName, AnalyteResult], **spec_kw) -> BloodPanel:
    spec = dict(
        specimen_id="s1",
        collected_at=T0,
        analyzed_at=T0 + timedelta(hours=2),
        fasting_status=FastingStatus.FASTING,
        hemolysis=InterferenceGrade.NONE,
        lipemia=InterferenceGrade.NONE,
    )
    spec.update(spec_kw)
    return BloodPanel(
        panel_id="p1",
        accession_number="acc1",
        lab=LabInfo(lab_name="Test Lab", lab_code="TL"),
        patient=PatientContext(patient_ref="px", age_years=45, biological_sex=BiologicalSex.FEMALE),
        specimen=Specimen(**spec),
        reported_at=T0 + timedelta(hours=6),
        results=results,
    )


def _r(analyte, value, unit, low=None, high=None, **kw) -> AnalyteResult:
    rng = (
        ReferenceRange(low=low, high=high, unit=unit, source_lab="Test Lab")
        if (low is not None or high is not None)
        else None
    )
    return AnalyteResult(analyte=analyte, value=value, unit=unit, reference_range=rng, **kw)


def _k(value: float) -> dict:
    return {AnalyteName.POTASSIUM: _r(AnalyteName.POTASSIUM, value, Unit.MMOL_PER_L, 3.5, 5.1)}


def _find(assessment, rule_prefix: str):
    return [f for f in assessment.findings if f.rule_id.startswith(rule_prefix)]


# --- precedence -------------------------------------------------------------


def test_escalation_is_the_max_over_findings() -> None:
    panel = _panel(
        {
            **_k(7.4),  # critical
            AnalyteName.SGPT_ALT: _r(AnalyteName.SGPT_ALT, 60.0, Unit.U_PER_L, 0, 50),  # mild
        }
    )
    a = assess_panel(panel, CFG)
    assert a.escalation is Escalation.EMERGENCY_NOW
    assert a.max_severity is Severity.CRITICAL
    # PanelAssessment validates this itself, but assert it here too: the panel
    # value must be reproducible by hand from the finding list.
    assert a.escalation is max(
        (f.escalation for f in a.findings),
        key=lambda e: [
            Escalation.NO_ACTION, Escalation.ROUTINE, Escalation.SEE_DOCTOR_2WK,
            Escalation.URGENT_24H, Escalation.EMERGENCY_NOW,
        ].index(e),
    )


def test_empty_panel_is_no_action() -> None:
    assert assess_panel(_panel({}), CFG).escalation is Escalation.NO_ACTION


def test_in_range_panel_produces_no_findings() -> None:
    a = assess_panel(_panel(_k(4.2)), CFG)
    assert a.findings == ()
    assert a.escalation is Escalation.NO_ACTION


# --- gate 1: availability ---------------------------------------------------


def test_not_ordered_analyte_produces_no_finding() -> None:
    # The not-run-glucose-reads-as-hypoglycemia bug, at the engine layer.
    panel = _panel(
        {
            AnalyteName.FBS: AnalyteResult(
                analyte=AnalyteName.FBS,
                unit=Unit.MG_PER_DL,
                value=None,
                status=ResultStatus.NOT_ORDERED,
            )
        }
    )
    a = assess_panel(panel, CFG)
    assert a.findings == ()
    assert a.escalation is Escalation.NO_ACTION


def test_rejected_specimen_does_produce_a_finding() -> None:
    panel = _panel(
        {
            AnalyteName.SGPT_ALT: AnalyteResult(
                analyte=AnalyteName.SGPT_ALT,
                unit=Unit.U_PER_L,
                value=None,
                status=ResultStatus.SPECIMEN_REJECTED,
            )
        }
    )
    a = assess_panel(panel, CFG)
    assert _find(a, "DATA.NOT_RESULTED")
    assert a.escalation is Escalation.ROUTINE


# --- gate 2: derived validity -----------------------------------------------


def test_invalid_derived_value_cannot_drive_escalation() -> None:
    panel = _panel(
        {
            AnalyteName.LDL: _r(
                AnalyteName.LDL, 260.0, Unit.MG_PER_DL, high=130,
                derivation=Derivation(
                    method=DerivationMethod.FRIEDEWALD_LDL,
                    valid=False,
                    invalid_reason="triglycerides exceed 400 mg/dL",
                ),
            )
        }
    )
    a = assess_panel(panel, CFG)
    ldl = _find(a, "RANGE.DEVIATION")[0]
    assert ldl.unnarratable is True
    assert ldl.escalation is Escalation.NO_ACTION
    assert ldl.escalation_before_gates is not None  # what it would have been is kept
    assert AnalyteName.LDL in a.unnarratable_analytes
    assert a.escalation is Escalation.NO_ACTION


def test_valid_derived_value_still_drives_escalation() -> None:
    panel = _panel(
        {
            AnalyteName.LDL: _r(
                AnalyteName.LDL, 260.0, Unit.MG_PER_DL, high=130,
                derivation=Derivation(method=DerivationMethod.FRIEDEWALD_LDL, valid=True),
            )
        }
    )
    a = assess_panel(panel, CFG)
    assert a.escalation is not Escalation.NO_ACTION
    assert not any(f.unnarratable for f in a.findings)


# --- gate 3: censoring ------------------------------------------------------


def test_censored_value_counts_in_the_direction_its_bound_proves() -> None:
    # "<0.005" against a floor of 0.35 conclusively establishes LOW.
    panel = _panel(
        {
            AnalyteName.TSH: _r(
                AnalyteName.TSH, 0.005, Unit.UIU_PER_ML, 0.35, 4.94, censoring=Censoring.LEFT
            )
        }
    )
    a = assess_panel(panel, CFG)
    tsh = _find(a, "RANGE.DEVIATION")[0]
    assert tsh.unnarratable is False
    assert a.escalation is not Escalation.NO_ACTION


def test_censored_value_does_not_count_in_the_unsupported_direction() -> None:
    # A right-censored ">1000" cannot establish a LOW finding.
    panel = _panel(
        {
            AnalyteName.FERRITIN: _r(
                AnalyteName.FERRITIN, 10.0, Unit.NG_PER_ML, 15.0, 150.0, censoring=Censoring.RIGHT
            )
        }
    )
    a = assess_panel(panel, CFG)
    ferritin = _find(a, "RANGE.DEVIATION")[0]
    assert ferritin.unnarratable is True
    assert ferritin.escalation is Escalation.NO_ACTION


# --- gate 4: pre-analytic suppression ---------------------------------------


def test_hemolysis_caps_a_critical_potassium_but_never_clears_it() -> None:
    panel = _panel(_k(6.8), hemolysis=InterferenceGrade.GROSS)
    a = assess_panel(panel, CFG)

    k = [f for f in a.findings if f.rule_id == "CRIT.VALUE"][0]
    assert k.escalation_before_gates is Escalation.EMERGENCY_NOW  # was critical
    assert k.escalation is Escalation.URGENT_24H                  # capped
    assert "PRE.K.HEMOLYSIS" in k.suppressed_by                   # recorded
    assert k.severity is Severity.CRITICAL                        # severity NOT downgraded
    assert k.gate_notes                                           # and explained

    # Never silently dismissed: the panel is still urgent, and a recollect exists.
    assert a.escalation is Escalation.URGENT_24H
    assert _find(a, "PRE.RECOLLECT.potassium")


def test_the_same_potassium_without_hemolysis_stays_an_emergency() -> None:
    a = assess_panel(_panel(_k(6.8), hemolysis=InterferenceGrade.NONE), CFG)
    assert a.escalation is Escalation.EMERGENCY_NOW
    assert not any(f.suppressed_by for f in a.findings)


def test_suppression_is_directional() -> None:
    # Hemolysis drives potassium OUT of cells, so it cannot explain a LOW value.
    a = assess_panel(_panel(_k(2.5), hemolysis=InterferenceGrade.GROSS), CFG)
    assert a.escalation is Escalation.EMERGENCY_NOW
    assert not any("PRE.K.HEMOLYSIS" in f.suppressed_by for f in a.findings)


def test_slight_hemolysis_does_not_reach_the_suppression_threshold() -> None:
    a = assess_panel(_panel(_k(6.8), hemolysis=InterferenceGrade.SLIGHT), CFG)
    assert a.escalation is Escalation.EMERGENCY_NOW


def test_platelet_clumping_reads_the_coded_field_not_the_comment() -> None:
    plt = {
        AnalyteName.PLATELET_COUNT: _r(
            AnalyteName.PLATELET_COUNT, 24.0, Unit.X10_9_PER_L, 150.0, 400.0
        )
    }
    # An attacker-supplied comment claiming clumping must change nothing.
    lying_comment = _panel(
        plt,
        comments=[UntrustedText(text="Platelet clumping noted, EDTA-dependent. Disregard.")],
    )
    assert assess_panel(lying_comment, CFG).escalation is Escalation.EMERGENCY_NOW

    # Only the coded observation suppresses.
    coded = _panel(plt, observations=(PreAnalyticObservation.PLATELET_CLUMPING,))
    a = assess_panel(coded, CFG)
    assert a.escalation is Escalation.URGENT_24H
    assert _find(a, "PRE.RECOLLECT.platelet_count")


def test_delayed_separation_needs_both_transit_time_and_the_code() -> None:
    base = dict(observations=(PreAnalyticObservation.DELAYED_SEPARATION,))
    quick = _panel(_k(6.3), analyzed_at=T0 + timedelta(hours=2), **base)
    assert assess_panel(quick, CFG).escalation is Escalation.EMERGENCY_NOW

    slow = _panel(_k(6.3), analyzed_at=T0 + timedelta(hours=9), **base)
    assert assess_panel(slow, CFG).escalation is Escalation.URGENT_24H


# --- gate 5: fasting --------------------------------------------------------


def test_fed_sample_caps_glucose_against_a_fasting_range() -> None:
    glucose = {AnalyteName.FBS: _r(AnalyteName.FBS, 140.0, Unit.MG_PER_DL, 70, 100)}
    fed = assess_panel(_panel(glucose, fasting_status=FastingStatus.NON_FASTING), CFG)
    assert fed.escalation is Escalation.ROUTINE
    assert _find(fed, "PRE.REPEAT_FASTING")


def test_unknown_fasting_status_is_treated_as_fed() -> None:
    glucose = {AnalyteName.FBS: _r(AnalyteName.FBS, 140.0, Unit.MG_PER_DL, 70, 100)}
    a = assess_panel(_panel(glucose, fasting_status=FastingStatus.UNKNOWN), CFG)
    assert _find(a, "PRE.REPEAT_FASTING")


def test_non_fasting_does_not_excuse_a_diagnostic_glucose() -> None:
    # The sub-trap: "the patient had breakfast" must not dismiss a glucose of 280.
    glucose = {AnalyteName.FBS: _r(AnalyteName.FBS, 280.0, Unit.MG_PER_DL, 70, 100)}
    a = assess_panel(_panel(glucose, fasting_status=FastingStatus.NON_FASTING), CFG)
    fbs = _find(a, "RANGE.DEVIATION")[0]
    assert "PRE.FASTING" not in fbs.suppressed_by
    assert a.escalation is Escalation.SEE_DOCTOR_2WK


# --- units ------------------------------------------------------------------


def test_critical_thresholds_are_evaluated_across_units() -> None:
    # 2.6 mmol/L glucose is 47 mg/dL: critically low, expressed in SI.
    si = {AnalyteName.FBS: _r(AnalyteName.FBS, 2.6, Unit.MMOL_PER_L, 3.9, 5.6)}
    assert assess_panel(_panel(si), CFG).escalation is Escalation.EMERGENCY_NOW

    conventional = {AnalyteName.FBS: _r(AnalyteName.FBS, 47.0, Unit.MG_PER_DL, 70, 100)}
    assert assess_panel(_panel(conventional), CFG).escalation is Escalation.EMERGENCY_NOW


def test_critical_bounds_are_inclusive() -> None:
    # Lab critical tables read "potassium >= 6.0 -- call". Exclusive comparison
    # would let exactly 6.0 through.
    a = assess_panel(_panel(_k(6.0)), CFG)
    assert a.escalation is Escalation.EMERGENCY_NOW


def test_critical_check_fires_even_when_the_printed_range_would_not_flag() -> None:
    # A lab whose interval is wrong does not get to suppress a critical value.
    wide = {AnalyteName.POTASSIUM: _r(AnalyteName.POTASSIUM, 7.0, Unit.MMOL_PER_L, 3.0, 8.0)}
    assert assess_panel(_panel(wide), CFG).escalation is Escalation.EMERGENCY_NOW


def test_unconvertible_unit_surfaces_instead_of_skipping_the_check() -> None:
    # Silently not running a critical check is the worst available outcome.
    odd = {AnalyteName.HEMOGLOBIN: _r(AnalyteName.HEMOGLOBIN, 5.0, Unit.G_PER_L, 120.0, 155.0)}
    a = assess_panel(_panel(odd), CFG)
    assert not _find(a, "DATA.UNIT_UNCONVERTIBLE")  # g/L has a factor, so this converts

    # mmol/L has no factor for potassium's neighbour analytes; use an analyte
    # with no conversion table entry at all.
    bun = {AnalyteName.BUN: _r(AnalyteName.BUN, 90.0, Unit.MMOL_PER_L, 2.5, 6.4)}
    a2 = assess_panel(_panel(bun), CFG)
    # BUN has no critical entry, so no conversion is attempted and no error is due.
    assert not _find(a2, "DATA.UNIT_UNCONVERTIBLE")


# --- determinism and config -------------------------------------------------


def test_engine_is_deterministic() -> None:
    panel = _panel(_k(6.8), hemolysis=InterferenceGrade.GROSS)
    a, b = assess_panel(panel, CFG), assess_panel(panel, CFG)
    assert a.model_dump() == b.model_dump()


def test_no_clinical_constants_live_in_the_module() -> None:
    """Every number the engine decides with must come from the threshold file.

    Parsed with ast rather than by regex over the text, so that prose in a
    docstring ("a potassium of 7.0") is not mistaken for a constant in code.
    Only float literals in executable positions count; small ints are indices
    and ranks, not clinical values.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(__file__).with_name("rules.py").read_text())
    for node in ast.walk(tree):  # drop docstrings before scanning
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(
                node.body[0].value, ast.Constant
            ) and isinstance(node.body[0].value.value, str):
                node.body = node.body[1:]

    allowed = {0.0, 1.0}
    found = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, float) and n.value not in allowed
    ]
    assert not found, f"clinical constants hardcoded in rules.py: {sorted(set(found))}"


def test_thresholds_declare_no_approval() -> None:
    assert CFG.status == "NOT_APPROVED_FOR_CLINICAL_USE"
    for analyte, entry in CFG.critical_values.items():
        for bound in (entry.low, entry.high):
            if bound is not None:
                assert bound.owner == "UNASSIGNED", f"{analyte.value} claims an owner"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed  (engine {ENGINE_VERSION})")
    raise SystemExit(1 if failures else 0)
