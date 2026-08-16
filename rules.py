"""Deterministic rules engine. Panel in, findings and one escalation out.

No LLM. No network. No I/O beyond reading the threshold file once. Every
function here is pure with respect to its inputs, so the same panel and the
same threshold file always produce the same assessment.

No clinical constant appears in this module. If you need to change what the
engine decides, change ``thresholds.yaml``.

PRECEDENCE RULE
---------------
The panel escalation is the maximum over the finding set, computed once, after
all gates have run, and never adjusted afterwards.

    EMERGENCY_NOW > URGENT_24H > SEE_DOCTOR_2WK > ROUTINE > NO_ACTION

An empty finding set gives NO_ACTION. Gates apply to individual findings, in a
fixed order, and are monotone non-increasing -- a gate may lower a finding's
escalation, never raise it (enforced by a validator on ``RuleFinding``):

    1. Availability      not RESULTED            -> no finding emitted
    2. Derived validity  derivation.valid False  -> unnarratable, forced NO_ACTION
    3. Censoring         bound proves one way    -> unsupported direction forced NO_ACTION
    4. Pre-analytic      artifact plausible      -> capped at ceiling + recollect finding
    5. Fasting           fed or unknown          -> capped at ceiling + repeat finding

Because gates only lower, and because each one records
``escalation_before_gates``, ``suppressed_by`` and ``gate_notes`` on the finding
it touched, a reviewer can read the finding list, apply the ordering by hand,
and arrive at the same escalation the engine did. There is no path by which a
value is dropped without leaving a record.

SUPPRESSION
-----------
A suppression rule caps, never clears: ``new = min(original, ceiling)``. A
critical potassium in a grossly hemolyzed tube becomes URGENT_24H, not
NO_ACTION. Hemolysis is evidence the number is unreliable, never evidence the
patient's potassium is normal -- the two occur in the same tube. Suppression is
also directional: hemolysis drives potassium out of cells so it can only explain
a HIGH potassium, and clumping can only falsely lower a platelet count.

The engine reads ``Specimen.observations`` (coded) and never
``Specimen.comments`` (free text). A rule that pattern-matched comment prose
would be a rule an attacker could fire or silence by writing the right sentence
into a field we transcribe from a PDF.
"""

from __future__ import annotations

from datetime import date as _Date
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml
from pydantic import BaseModel, ConfigDict, Field

from models import (
    ANALYTES,
    AnalyteName,
    AnalyteResult,
    BloodPanel,
    Censoring,
    Escalation,
    FastingStatus,
    Flag,
    InterferenceGrade,
    PanelAssessment,
    PreAnalyticObservation,
    ResultStatus,
    RuleFinding,
    Severity,
    max_escalation,
)

ENGINE_VERSION = "0.1.0"
DEFAULT_THRESHOLDS_PATH = Path(__file__).parent / "thresholds.yaml"

_ESCALATION_RANK = {
    Escalation.NO_ACTION: 0,
    Escalation.ROUTINE: 1,
    Escalation.SEE_DOCTOR_2WK: 2,
    Escalation.URGENT_24H: 3,
    Escalation.EMERGENCY_NOW: 4,
}
_INTERFERENCE_RANK = {
    InterferenceGrade.NOT_ASSESSED: -1,
    InterferenceGrade.NONE: 0,
    InterferenceGrade.SLIGHT: 1,
    InterferenceGrade.MODERATE: 2,
    InterferenceGrade.GROSS: 3,
}


def _min_escalation(a: Escalation, b: Escalation) -> Escalation:
    return a if _ESCALATION_RANK[a] <= _ESCALATION_RANK[b] else b


# ===========================================================================
# Threshold configuration
# ===========================================================================


class ThresholdError(RuntimeError):
    """The threshold file is malformed. Fail loudly; never fall back to a default."""


class Bound(BaseModel):
    model_config = ConfigDict(frozen=True)
    value: float
    source: str
    date: _Date
    owner: str


class CriticalEntry(BaseModel):
    model_config = ConfigDict(frozen=True)
    unit: str
    low: Bound | None = None
    high: Bound | None = None


class SeverityAnchor(BaseModel):
    model_config = ConfigDict(frozen=True)
    direction: str
    unit: str
    bands: dict[str, float]
    source: str
    date: _Date
    owner: str
    comment: str | None = None


class SuppressionRule(BaseModel):
    model_config = ConfigDict(frozen=True)
    rule_id: str
    analyte: AnalyteName
    direction: str
    condition: dict[str, Any]
    ceiling: Escalation
    recollect_action: str
    rationale: str
    source: str
    date: _Date
    owner: str


class UnitConversion(BaseModel):
    model_config = ConfigDict(frozen=True)
    canonical: str
    factors: dict[str, float]
    source: str
    owner: str


class Thresholds(BaseModel):
    """Validated threshold file. A malformed entry raises at load, not at use."""

    model_config = ConfigDict(frozen=True)

    schema_version: str
    thresholds_version: str
    status: str
    severity_to_escalation: dict[str, Escalation]
    default_bands: dict[str, float]
    unit_conversions: dict[AnalyteName, UnitConversion]
    critical_values: dict[AnalyteName, CriticalEntry]
    severity_anchors: dict[AnalyteName, SeverityAnchor]
    suppression: tuple[SuppressionRule, ...]
    fasting: dict[str, Any]
    not_resulted: dict[str, Any]

    def escalation_for(self, severity: Severity) -> Escalation:
        try:
            return self.severity_to_escalation[severity.value]
        except KeyError as exc:  # pragma: no cover - config guard
            raise ThresholdError(f"no escalation mapped for severity {severity.value}") from exc


def _as_analyte(name: str, where: str) -> AnalyteName:
    try:
        return AnalyteName(name)
    except ValueError as exc:
        raise ThresholdError(f"{where}: {name!r} is not a known analyte") from exc


def load_thresholds(path: Path | str = DEFAULT_THRESHOLDS_PATH) -> Thresholds:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ThresholdError(f"{path}: expected a mapping at the top level")

    try:
        crit = {
            _as_analyte(k, "critical_values"): CriticalEntry.model_validate(v)
            for k, v in (raw.get("critical_values") or {}).items()
        }
        anchors = {
            _as_analyte(k, "severity_anchors"): SeverityAnchor.model_validate(v)
            for k, v in (raw.get("severity_anchors") or {}).items()
        }
        conversions = {
            _as_analyte(k, "unit_conversions"): UnitConversion.model_validate(v)
            for k, v in (raw.get("unit_conversions") or {}).items()
        }
        suppression = tuple(
            SuppressionRule.model_validate(r) for r in (raw.get("pre_analytic_suppression") or [])
        )
        bands = dict(raw["default_severity_bands"]["bands"])
        thresholds = Thresholds(
            schema_version=raw["schema_version"],
            thresholds_version=raw["thresholds_version"],
            status=raw["status"],
            severity_to_escalation=raw["severity_to_escalation"],
            default_bands=bands,
            unit_conversions=conversions,
            critical_values=crit,
            severity_anchors=anchors,
            suppression=suppression,
            fasting=raw["fasting_gating"],
            not_resulted=raw["not_resulted_handling"],
        )
    except KeyError as exc:
        raise ThresholdError(f"{path}: missing required section {exc}") from exc

    # Every threshold must carry an owner, even if that owner is UNASSIGNED.
    # An entry with no owner field at all is an entry nobody has looked at.
    for analyte, entry in thresholds.critical_values.items():
        for bound in (entry.low, entry.high):
            if bound is not None and not bound.owner:
                raise ThresholdError(f"critical_values.{analyte.value}: missing owner")
    return thresholds


@lru_cache(maxsize=4)
def _cached_thresholds(path: str) -> Thresholds:
    return load_thresholds(Path(path))


# ===========================================================================
# Unit handling
# ===========================================================================


class UnitUnconvertible(Exception):
    """A threshold cannot be compared because no factor exists for this unit.

    Never swallowed. The engine turns this into a visible finding rather than
    skipping the check, because a critical threshold that silently did not run
    is worse than one that fails loudly.
    """

    def __init__(self, analyte: AnalyteName, unit: str, target: str) -> None:
        super().__init__(f"{analyte.value}: no factor from {unit} to {target}")
        self.analyte, self.unit, self.target = analyte, unit, target


def to_canonical(analyte: AnalyteName, value: float, unit: str, cfg: Thresholds) -> float:
    """Convert a reported value into the unit the thresholds are stated in."""
    conv = cfg.unit_conversions.get(analyte)
    if conv is None:
        raise UnitUnconvertible(analyte, unit, "?")
    factor = conv.factors.get(unit)
    if factor is None:
        raise UnitUnconvertible(analyte, unit, conv.canonical)
    return value * factor


# ===========================================================================
# Severity
# ===========================================================================


def _band_from_fold(fold: float, bands: dict[str, float]) -> Severity:
    """Highest band whose cutoff the fold-deviation reaches."""
    chosen = Severity.NONE
    for name in ("borderline", "mild", "moderate", "marked"):
        cutoff = bands.get(name)
        if cutoff is not None and fold >= cutoff:
            chosen = Severity(name)
    return chosen


def deviation_severity(result: AnalyteResult, direction: str, cfg: Thresholds) -> Severity:
    """How far out the value is, ignoring criticals.

    Default metric is fold-deviation beyond the printed boundary, which needs no
    unit conversion because it is a ratio of two same-unit quantities. Analytes
    with an anchor entry use absolute grading instead.
    """
    value, rng = result.value, result.reference_range
    if value is None:
        return Severity.NONE

    anchor = cfg.severity_anchors.get(result.analyte)
    if anchor is not None and anchor.direction == direction:
        try:
            canonical = to_canonical(result.analyte, value, result.unit.value, cfg)
        except UnitUnconvertible:
            canonical = None
        if canonical is not None:
            chosen = Severity.BORDERLINE
            for name in ("mild", "moderate", "marked"):
                cutoff = anchor.bands.get(name)
                if cutoff is None:
                    continue
                if (direction == "low" and canonical < cutoff) or (
                    direction == "high" and canonical > cutoff
                ):
                    chosen = Severity(name)
            return chosen

    if rng is None or not rng.is_numeric:
        return Severity.NONE
    if direction == "high" and rng.high is not None and rng.high > 0:
        return _band_from_fold(value / rng.high, cfg.default_bands)
    if direction == "low" and rng.low is not None and value > 0:
        return _band_from_fold(rng.low / value, cfg.default_bands)
    return Severity.NONE


# ===========================================================================
# Finding construction
# ===========================================================================


def _fid(panel: BloodPanel, rule_id: str, suffix: str = "") -> str:
    return f"{panel.panel_id}:{rule_id}:{suffix}" if suffix else f"{panel.panel_id}:{rule_id}"


def _display(result: AnalyteResult) -> tuple[str, str | None]:
    return result.display_value(), (
        result.reference_range.display() if result.reference_range else None
    )


def _critical_breach(result: AnalyteResult, cfg: Thresholds) -> tuple[str, Bound] | None:
    """Return the breached direction and bound, or None.

    Runs independently of the lab's printed range. A lab whose interval would
    leave a potassium of 7.0 unflagged does not get to suppress a critical.
    """
    entry = cfg.critical_values.get(result.analyte)
    if entry is None or result.value is None:
        return None
    canonical = to_canonical(result.analyte, result.value, result.unit.value, cfg)
    # Bounds are INCLUSIVE, matching how laboratory critical-value tables are
    # written ("potassium >= 6.0 -- call the physician"). With an exclusive
    # comparison a potassium of exactly 6.0 slips past a 6.0 threshold, which is
    # the wrong direction to be wrong in.
    if entry.low is not None and canonical <= entry.low.value:
        return ("low", entry.low)
    if entry.high is not None and canonical >= entry.high.value:
        return ("high", entry.high)
    return None


def rule_analyte_findings(panel: BloodPanel, cfg: Thresholds) -> list[RuleFinding]:
    """One finding per out-of-range or critical resulted analyte.

    Gate 1 lives here: an analyte that is not RESULTED produces nothing, so a
    not-run glucose can never become a hypoglycemia finding.
    """
    findings: list[RuleFinding] = []

    for analyte in sorted(panel.results, key=lambda a: a.value):
        result = panel.results[analyte]
        if result.status is not ResultStatus.RESULTED or result.value is None:
            continue  # gate 1: availability

        try:
            breach = _critical_breach(result, cfg)
        except UnitUnconvertible as exc:
            observed, reference = _display(result)
            findings.append(
                RuleFinding(
                    finding_id=_fid(panel, "DATA.UNIT_UNCONVERTIBLE", analyte.value),
                    rule_id="DATA.UNIT_UNCONVERTIBLE",
                    rule_version=cfg.thresholds_version,
                    escalation=Escalation.SEE_DOCTOR_2WK,
                    severity=Severity.MODERATE,
                    triggering_analytes=(analyte,),
                    machine_summary=(
                        f"{ANALYTES[analyte].display_name} reported in {exc.unit}, which has "
                        f"no conversion to {exc.target}. The critical-value check for this "
                        "analyte did NOT run. Manual review required."
                    ),
                    observed=observed,
                    reference=reference,
                )
            )
            breach = None

        flag = result.flag
        if breach is None and flag not in (Flag.HIGH, Flag.LOW):
            continue

        direction = breach[0] if breach else ("high" if flag is Flag.HIGH else "low")
        if breach is not None:
            severity = Severity.CRITICAL
            rule_id = "CRIT.VALUE"
            summary = (
                f"{ANALYTES[analyte].display_name} {result.display_value()} breaches the "
                f"critical {direction} threshold of {breach[1].value} "
                f"{cfg.critical_values[analyte].unit}."
            )
        else:
            severity = deviation_severity(result, direction, cfg)
            rule_id = "RANGE.DEVIATION"
            summary = (
                f"{ANALYTES[analyte].display_name} {result.display_value()} is {direction} "
                f"against the range printed by {result.reference_range.source_lab}"
                f" ({result.reference_range.display()})."
                if result.reference_range
                else f"{ANALYTES[analyte].display_name} {result.display_value()} is {direction}."
            )

        observed, reference = _display(result)
        findings.append(
            RuleFinding(
                finding_id=_fid(panel, rule_id, analyte.value),
                rule_id=rule_id,
                rule_version=cfg.thresholds_version,
                escalation=cfg.escalation_for(severity),
                severity=severity,
                triggering_analytes=(analyte,),
                machine_summary=summary,
                observed=observed,
                reference=reference,
            )
        )
    return findings


def rule_not_resulted(panel: BloodPanel, cfg: Thresholds) -> list[RuleFinding]:
    """Analytes that were attempted and failed.

    NOT_ORDERED is excluded by configuration: a test nobody ordered is not an
    event. A rejected or insufficient specimen is, and warrants completing the
    panel.
    """
    emit_for = {ResultStatus(s) for s in cfg.not_resulted["emit_finding_for"]}
    never = {ResultStatus(s) for s in cfg.not_resulted["never_emit_for"]}
    affected = [
        a
        for a, r in panel.results.items()
        if r.status in emit_for and r.status not in never
    ]
    if not affected:
        return []
    affected.sort(key=lambda a: a.value)
    by_status: dict[str, list[str]] = {}
    for a in affected:
        by_status.setdefault(panel.results[a].status.value, []).append(ANALYTES[a].display_name)
    detail = "; ".join(f"{status}: {', '.join(names)}" for status, names in sorted(by_status.items()))
    return [
        RuleFinding(
            finding_id=_fid(panel, cfg.not_resulted["rule_id"]),
            rule_id=cfg.not_resulted["rule_id"],
            rule_version=cfg.thresholds_version,
            escalation=Escalation(cfg.not_resulted["escalation"]),
            severity=Severity(cfg.not_resulted["severity"]),
            triggering_analytes=tuple(affected),
            machine_summary=(
                f"{len(affected)} analyte(s) were attempted but not resulted. {detail}. "
                "The panel is incomplete; these were not assessed."
            ),
        )
    ]


# ===========================================================================
# Gates
# ===========================================================================


def _apply_gate(
    finding: RuleFinding,
    *,
    rule_id: str,
    new_escalation: Escalation,
    note: str,
    unnarratable: bool = False,
) -> RuleFinding:
    """Rewrite a finding, preserving what it was before.

    Escalation is floored through ``_min_escalation`` so a gate can never raise
    one, which is the property the precedence rule rests on.
    """
    before = finding.escalation_before_gates or finding.escalation
    return finding.model_copy(
        update={
            "escalation": _min_escalation(finding.escalation, new_escalation),
            "escalation_before_gates": before,
            "suppressed_by": finding.suppressed_by + (rule_id,),
            "gate_notes": finding.gate_notes + (note,),
            "unnarratable": finding.unnarratable or unnarratable,
        }
    )


def gate_derived_validity(
    findings: list[RuleFinding], panel: BloodPanel, cfg: Thresholds
) -> list[RuleFinding]:
    """Gate 2. An invalid calculation cannot drive escalation.

    The value stays on the finding, because it is printed on the patient's
    report and somebody will ask about it. It is marked unnarratable so no
    downstream prose states it as fact, and forced to NO_ACTION so it cannot
    move the maximum. A Friedewald LDL computed above the triglyceride limit
    often reads as reassuringly normal, which is precisely why it must not count.
    """
    out: list[RuleFinding] = []
    for f in findings:
        invalid = [
            a
            for a in f.triggering_analytes
            if (r := panel.get(a)) is not None
            and r.derivation is not None
            and not r.derivation.valid
        ]
        if not invalid:
            out.append(f)
            continue
        reasons = "; ".join(
            panel.get(a).derivation.invalid_reason or "invalid derivation"  # type: ignore[union-attr]
            for a in invalid
        )
        out.append(
            _apply_gate(
                f,
                rule_id="GATE.DERIVED_INVALID",
                new_escalation=Escalation.NO_ACTION,
                note=(
                    f"Derived value is not valid ({reasons}). Cannot drive escalation and "
                    "must not be stated as fact."
                ),
                unnarratable=True,
            )
        )
    return out


def gate_censoring(
    findings: list[RuleFinding], panel: BloodPanel, cfg: Thresholds
) -> list[RuleFinding]:
    """Gate 3. A censored value supports only the direction its bound proves.

    ``<0.005`` means the true value is at most 0.005, which conclusively
    establishes LOW and says nothing that could establish HIGH. Kept separate
    from the derived-validity gate because a censored value in its supported
    direction is perfectly good evidence and must keep its escalation.
    """
    out: list[RuleFinding] = []
    for f in findings:
        unsupported = []
        for a in f.triggering_analytes:
            r = panel.get(a)
            if r is None or r.censoring is Censoring.NONE or r.value is None or r.reference_range is None:
                continue
            rng = r.reference_range
            proves_low = r.censoring is Censoring.LEFT and rng.low is not None and r.value < rng.low
            proves_high = r.censoring is Censoring.RIGHT and rng.high is not None and r.value > rng.high
            if not (proves_low or proves_high):
                unsupported.append((a, r.censoring))
        if not unsupported:
            out.append(f)
            continue
        detail = ", ".join(f"{ANALYTES[a].display_name} ({c.value}-censored)" for a, c in unsupported)
        out.append(
            _apply_gate(
                f,
                rule_id="GATE.CENSORED_INCONCLUSIVE",
                new_escalation=Escalation.NO_ACTION,
                note=(
                    f"{detail}: the reported number is an assay bound and does not establish "
                    "this finding's direction."
                ),
                unnarratable=True,
            )
        )
    return out


def _condition_met(cond: dict[str, Any], panel: BloodPanel) -> bool:
    """Evaluate a suppression condition against structured specimen fields only."""
    spec = panel.specimen

    if (need := cond.get("hemolysis_at_least")) is not None:
        if _INTERFERENCE_RANK[spec.hemolysis] < _INTERFERENCE_RANK[InterferenceGrade(need)]:
            return False
    if (need := cond.get("lipemia_at_least")) is not None:
        if _INTERFERENCE_RANK[spec.lipemia] < _INTERFERENCE_RANK[InterferenceGrade(need)]:
            return False
    if (need := cond.get("transit_hours_at_least")) is not None:
        transit = spec.transit_hours
        if transit is None or transit < float(need):
            return False
    if (needed := cond.get("any_observation")) is not None:
        wanted = {PreAnalyticObservation(o) for o in needed}
        if not wanted & set(spec.observations):
            return False
    return True


def gate_pre_analytic(
    findings: list[RuleFinding], panel: BloodPanel, cfg: Thresholds
) -> list[RuleFinding]:
    """Gate 4. Cap findings a pre-analytic artifact plausibly explains.

    Caps, never clears, and emits a companion recollect finding at the same
    capped level. Both carry the escalation, so deleting one of them later does
    not silently collapse the result.
    """
    out: list[RuleFinding] = []
    companions: list[RuleFinding] = []

    for f in findings:
        applied: list[SuppressionRule] = []
        gated = f
        for rule in cfg.suppression:
            if rule.analyte not in f.triggering_analytes:
                continue
            # Directionality: hemolysis only inflates potassium, clumping only
            # deflates platelets. The mirrored direction is not explained.
            r = panel.get(rule.analyte)
            if r is None:
                continue
            observed_direction = "high" if r.flag is Flag.HIGH else "low" if r.flag is Flag.LOW else None
            crit_direction = None
            try:
                breach = _critical_breach(r, cfg)
                crit_direction = breach[0] if breach else None
            except UnitUnconvertible:
                pass
            if rule.direction not in {observed_direction, crit_direction}:
                continue
            if not _condition_met(rule.condition, panel):
                continue
            gated = _apply_gate(
                gated,
                rule_id=rule.rule_id,
                new_escalation=rule.ceiling,
                note=(
                    f"Unconfirmed: {rule.rationale.strip()} Capped at "
                    f"{rule.ceiling.value}, not cleared."
                ),
            )
            applied.append(rule)

        out.append(gated)

        for rule in applied:
            level = _min_escalation(f.escalation, rule.ceiling)
            companions.append(
                RuleFinding(
                    finding_id=_fid(panel, f"PRE.RECOLLECT.{rule.analyte.value}", rule.rule_id),
                    rule_id=f"PRE.RECOLLECT.{rule.analyte.value}",
                    rule_version=cfg.thresholds_version,
                    escalation=level,
                    severity=f.severity,
                    triggering_analytes=(rule.analyte,),
                    machine_summary=(
                        f"{rule.recollect_action.strip()} Triggered by {rule.rule_id}. The "
                        f"current {ANALYTES[rule.analyte].display_name} is unusable; that is "
                        "not the same as normal."
                    ),
                    observed=f.observed,
                    reference=f.reference,
                )
            )
    return out + companions


def gate_fasting(
    findings: list[RuleFinding], panel: BloodPanel, cfg: Thresholds
) -> list[RuleFinding]:
    """Gate 5. Glucose and triglycerides read against fasting ranges on a fed sample.

    Unknown fasting status is treated as not-fasting: absent metadata is not
    evidence of compliance. The exception carries as much weight as the rule --
    above the fasting-independent threshold the gate does not apply, because a
    random glucose of 280 is abnormal however recently the patient ate.
    """
    gate_when = {FastingStatus(s) for s in cfg.fasting["gate_when_fasting_status_in"]}
    if panel.specimen.fasting_status not in gate_when:
        return findings

    applies = {AnalyteName(a) for a in cfg.fasting["applies_to"]}
    ceiling = Escalation(cfg.fasting["ceiling"])
    independent = cfg.fasting.get("fasting_independent") or {}

    out: list[RuleFinding] = []
    gated_any = False

    for f in findings:
        targets = [a for a in f.triggering_analytes if a in applies]
        if not targets:
            out.append(f)
            continue

        exempt = False
        for a in targets:
            spec = independent.get(a.value)
            r = panel.get(a)
            if spec is None or r is None or r.value is None:
                continue
            try:
                canonical = to_canonical(a, r.value, r.unit.value, cfg)
            except UnitUnconvertible:
                continue  # a visible finding was already raised elsewhere
            if canonical >= float(spec["value"]):
                exempt = True
        if exempt:
            out.append(
                f.model_copy(
                    update={
                        "gate_notes": f.gate_notes
                        + (
                            "Fasting gate not applied: the value is abnormal irrespective of "
                            "fasting state.",
                        )
                    }
                )
            )
            continue

        gated_any = True
        out.append(
            _apply_gate(
                f,
                rule_id=cfg.fasting["rule_id"],
                new_escalation=ceiling,
                note=(
                    f"Specimen fasting status is "
                    f"{panel.specimen.fasting_status.value}; this analyte is being compared "
                    f"against a fasting reference range and is not interpretable. Capped at "
                    f"{ceiling.value}."
                ),
            )
        )

    if gated_any:
        out.append(
            RuleFinding(
                finding_id=_fid(panel, "PRE.REPEAT_FASTING"),
                rule_id="PRE.REPEAT_FASTING",
                rule_version=cfg.thresholds_version,
                escalation=ceiling,
                severity=Severity.MILD,
                triggering_analytes=tuple(sorted(applies, key=lambda a: a.value)),
                machine_summary=(
                    f"{cfg.fasting['repeat_action'].strip()} Fasting status was "
                    f"{panel.specimen.fasting_status.value}."
                ),
            )
        )
    return out


# ===========================================================================
# Entry point
# ===========================================================================

GATES = (gate_derived_validity, gate_censoring, gate_pre_analytic, gate_fasting)


def assess_panel(panel: BloodPanel, cfg: Thresholds | None = None) -> PanelAssessment:
    """Run the engine over one panel. Pure: no I/O, no clock, no randomness."""
    cfg = cfg or _cached_thresholds(str(DEFAULT_THRESHOLDS_PATH))

    findings = rule_analyte_findings(panel, cfg) + rule_not_resulted(panel, cfg)
    trace = [f"{len(findings)} finding(s) before gating"]

    for gate in GATES:
        before = [(f.finding_id, f.escalation) for f in findings]
        findings = gate(findings, panel, cfg)
        changed = sum(
            1
            for f in findings
            if (f.finding_id, f.escalation) not in before
        )
        if changed:
            trace.append(f"{gate.__name__}: {changed} finding(s) added or lowered")

    escalation = max_escalation(f.escalation for f in findings)
    driver = next(
        (f for f in findings if f.escalation is escalation and escalation is not Escalation.NO_ACTION),
        None,
    )
    trace.append(
        f"escalation = max over {len(findings)} finding(s) = {escalation.value}"
        + (f", set by {driver.rule_id} on {driver.triggering_analytes[0].value}" if driver else "")
    )

    return PanelAssessment(
        panel_id=panel.panel_id,
        escalation=escalation,
        findings=tuple(findings),
        trace=tuple(trace),
        engine_version=ENGINE_VERSION,
        thresholds_version=cfg.thresholds_version,
    )


def queue_sort_key(assessment: PanelAssessment) -> tuple:
    """Ordering for the physician review queue: urgency, then how bad, then how much."""
    from models import SEVERITY_RANK

    return (
        -_ESCALATION_RANK[assessment.escalation],
        -SEVERITY_RANK[assessment.max_severity],
        -len(assessment.findings),
        assessment.panel_id,
    )


def sort_queue(assessments: Iterable[PanelAssessment]) -> list[PanelAssessment]:
    return sorted(assessments, key=queue_sort_key)
