"""Demo surface over the existing pipeline. Streamlit, single file.

    streamlit run app.py

This computes nothing clinical. Every value, verdict, ordering and piece of
prose on screen is produced by models.py / rules.py / narrator.py /
eval_narrative.py / judge.py and rendered here unchanged. The only things this
file decides are layout, grouping for readability, and which case is selected.

The physician view shows RAW PANEL VALUES FIRST, before the assessment and well
before the AI narrative. That ordering is the point of the view, not a layout
preference: a reviewer who reads the model's framing first tends to check the
data against the framing rather than the other way round. Putting the numbers
first means the physician forms a view before seeing the machine's.
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from eval_narrative import run_gates
from judge import JudgeError, judge, make_judge_provider, payload_from_report
from models import ANALYTES, Escalation, Flag, GeneratedCase, ResultStatus
from narrator import NarrationError, Prompt, make_provider, narrate
from rules import ENGINE_VERSION, assess_panel, load_thresholds, queue_sort_key

EVAL_SET = Path(__file__).parent / "fixtures" / "eval_set.jsonl"

# Presentation only. `AnalyteMeta.category` is identity metadata from models.py;
# this just gives the groups a reading order and a heading.
CATEGORY_ORDER = [
    ("hematology", "CBC with differential"),
    ("glycemic", "Glycemic"),
    ("lipid", "Lipids"),
    ("renal", "Renal"),
    ("electrolyte", "Electrolytes"),
    ("liver", "Liver"),
    ("thyroid", "Thyroid"),
    ("add_on", "Add-on"),
    ("index", "Screening indices"),
]
FLAG_MARK = {Flag.HIGH: "HIGH", Flag.LOW: "LOW", Flag.NORMAL: "", Flag.NOT_EVALUABLE: ""}
TIER_COLOUR = {
    Escalation.EMERGENCY_NOW: "red",
    Escalation.URGENT_24H: "orange",
    Escalation.SEE_DOCTOR_2WK: "violet",
    Escalation.ROUTINE: "blue",
    Escalation.NO_ACTION: "green",
}


# ---------------------------------------------------------------------------
# Loading. Cached so a rerun does not re-read or re-assess everything.
# ---------------------------------------------------------------------------


@st.cache_resource
def load_everything():
    cfg = load_thresholds()
    cases = [
        GeneratedCase.model_validate_json(line)
        for line in EVAL_SET.read_text().splitlines()
        if line.strip()
    ]
    assessments = {c.case_id: assess_panel(c.panel, cfg) for c in cases}
    return cfg, cases, assessments


@st.cache_resource
def get_providers(name: str):
    """Build narrator and judge providers. Never raises to the caller."""
    try:
        return make_provider(name), make_judge_provider(name), None
    except NarrationError as exc:
        return make_provider("fake"), make_judge_provider("fake"), str(exc)


def run_pipeline(case, assessment, narrator_provider, judge_provider):
    """rules -> narrator -> gates -> judge, exactly as the harness runs it."""
    try:
        report = narrate(case.panel, assessment, provider=narrator_provider)
    except NarrationError as exc:
        return None, [], None, f"narration failed: {exc}"

    failures = run_gates(case.case_id, report, case.panel, assessment)

    # The judge is the second net and runs only behind the gates. judge()
    # enforces that itself; this mirrors it so the UI shows why it was skipped.
    if failures:
        return report, failures, None, None
    try:
        return report, failures, judge(
            payload_from_report(report, assessment, case.panel),
            gate_failures=failures,
            provider=judge_provider,
        ), None
    except JudgeError as exc:
        return report, failures, None, f"judge failed: {exc}"


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def tier_badge(escalation: Escalation) -> str:
    return f":{TIER_COLOUR[escalation]}[**{escalation.value}**]"


def render_raw_panel(panel) -> None:
    """The lab report as printed. No interpretation, no ordering by severity."""
    by_category: dict[str, list] = {}
    for analyte, result in panel.results.items():
        by_category.setdefault(ANALYTES[analyte].category, []).append((analyte, result))

    for key, heading in CATEGORY_ORDER:
        rows = by_category.get(key)
        if not rows:
            continue
        st.markdown(f"**{heading}**")
        st.dataframe(
            [
                {
                    "Analyte": ANALYTES[a].display_name,
                    "Result": r.display_value(),
                    "Flag": FLAG_MARK.get(r.flag, ""),
                    "Reference range": r.reference_range.display() if r.reference_range else "",
                    "Population": (r.reference_range.population or "") if r.reference_range else "",
                    "Derived": r.derivation.method.value if r.derivation else "",
                    "Valid": "" if not r.derivation else ("yes" if r.derivation.valid else "NO"),
                }
                for a, r in rows
            ],
            hide_index=True,
            width="stretch",
        )


def render_specimen(panel) -> None:
    s = panel.specimen
    c1, c2, c3 = st.columns(3)
    c1.metric("Fasting", s.fasting_status.value)
    c2.metric("Hemolysis", s.hemolysis.value)
    c3.metric("Lipemia", s.lipemia.value)
    if s.observations:
        st.markdown("**Coded pre-analytic observations** (what the rules engine may read)")
        st.write(", ".join(o.value for o in s.observations))
    if s.comments:
        st.markdown("**Free-text comments** — untrusted; the rules engine and the narrator never see these")
        for c in s.comments:
            st.code(c.raw, language=None)


def render_findings(assessment) -> None:
    if not assessment.findings:
        st.info("The rules engine produced no findings for this panel.")
        return
    st.dataframe(
        [
            {
                "Escalation": f.escalation.value,
                "Severity": f.severity.value,
                "Rule": f.rule_id,
                "Analytes": ", ".join(ANALYTES[a].display_name for a in f.triggering_analytes),
                "Observed": f.observed or "",
                "Before gates": f.escalation_before_gates.value if f.escalation_before_gates else "",
                "Suppressed by": ", ".join(f.suppressed_by),
                "Unnarratable": "YES" if f.unnarratable else "",
            }
            for f in assessment.findings
        ],
        hide_index=True,
        width="stretch",
    )
    with st.expander("Escalation trace — how the engine reached this tier"):
        for line in assessment.trace:
            st.text(line)
        for f in assessment.findings:
            for note in f.gate_notes:
                st.caption(f"{f.rule_id}: {note}")


def render_narrative(report) -> None:
    st.markdown(f"**Summary** — {report.summary}")
    for block in report.blocks:
        st.markdown(f"- {block.text}")
    st.markdown(f"**Next step** — {report.next_step}")
    for notice in report.system_notices:
        st.warning(notice, icon=":material/info:")
    st.caption(
        f"model {report.model_id} · prompt {report.prompt_version} · "
        f"escalation copied from the assessment, not authored by the model"
    )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Alaga — demo", layout="wide")
cfg, cases, assessments = load_everything()

with st.sidebar:
    st.title("Alaga")
    st.caption(f"engine {ENGINE_VERSION} · thresholds {cfg.thresholds_version}")
    st.error(f"**{cfg.status}**", icon=":material/warning:")

    st.subheader("Provider")
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    choice = st.radio(
        "LLM provider",
        ["fake", "anthropic"],
        index=0,
        help="fake is deterministic, needs no key and no network. It is the default.",
    )
    if choice == "anthropic" and not has_key:
        st.warning("ANTHROPIC_API_KEY is not set — falling back to the fake provider.")
        choice = "fake"
    narrator_provider, judge_provider, provider_error = get_providers(choice)
    if provider_error:
        st.warning(f"Falling back to the fake provider: {provider_error}")
    st.caption(f"narrator: `{narrator_provider.model_id}`")
    st.caption(f"judge: `{judge_provider.model_id}`")

    st.subheader("Case")
    selected_id = st.selectbox(
        "Case",
        [c.case_id for c in cases],
        format_func=lambda cid: f"{cid}  ·  {next(c for c in cases if c.case_id == cid).stratum.value}",
        label_visibility="collapsed",
    )

case = next(c for c in cases if c.case_id == selected_id)
assessment = assessments[case.case_id]

tab_case, tab_customer, tab_queue = st.tabs(
    ["1 · Case", "2 · Customer profile", "3 · Physician review queue"]
)

# --- 1. the case ------------------------------------------------------------
with tab_case:
    st.header(case.case_id)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Stratum", case.stratum.value.replace("_", " "))
    c2.metric("Lab", case.panel.lab.lab_code)
    c3.metric("Patient", f"{case.panel.patient.age_years}y {case.panel.patient.biological_sex.value}")
    c4.metric("Engine escalation", assessment.escalation.value)
    st.caption(f"variant: `{case.variant}`")

    st.subheader("Corpus ground truth")
    st.caption(
        "Authored when the case was designed, before any value was sampled. The engine "
        "never sees this; it is what the engine is measured against."
    )
    gt = case.ground_truth
    agree = gt.expected_escalation is assessment.escalation
    st.markdown(
        f"expected **{gt.expected_escalation.value}** · engine said **{assessment.escalation.value}** "
        + ("— agree" if agree else "— **disagree**")
    )
    st.markdown(f"*{gt.rationale}*")
    st.markdown(f"**Expected action** — {gt.expected_action}")
    if gt.traps:
        st.markdown("**Traps this case encodes**")
        for t in gt.traps:
            st.markdown(f"- {t}")
    if gt.must_not_claim:
        st.markdown("**Must not claim** — " + "; ".join(gt.must_not_claim))

    st.subheader("Specimen")
    render_specimen(case.panel)

# --- 2. what the customer would see ----------------------------------------
with tab_customer:
    st.header("Plain-language health profile")
    report, failures, verdict, error = run_pipeline(
        case, assessment, narrator_provider, judge_provider
    )
    if error:
        st.error(error)
    elif report is None:
        st.error("No narrative was produced.")
    else:
        st.markdown(f"### {tier_badge(assessment.escalation)}")
        render_narrative(report)
        st.divider()
        if failures:
            st.error(
                f"{len(failures)} gate failure(s). In the real pipeline this narrative "
                "does not reach a customer at all.",
                icon=":material/block:",
            )
        elif verdict and not verdict.no_objections:
            st.warning(
                f"{len(verdict.failures)} judge objection(s). Routed to a physician with "
                "the objections attached.",
                icon=":material/gavel:",
            )
        st.info(
            "Nothing here reaches a customer without physician review. "
            "`HealthProfileReport` cannot enter RELEASED without a `PhysicianReview`, "
            "and a judge pass authorises nothing.",
            icon=":material/lock:",
        )

# --- 3. the queue -----------------------------------------------------------
with tab_queue:
    st.header("Physician review queue")
    st.caption(
        f"Ordered by `rules.queue_sort_key`: escalation, then severity, then finding count. "
        f"{len(cases)} cases."
    )

    st.dataframe(
        [
            {
                "Case": c.case_id,
                "Escalation": assessments[c.case_id].escalation.value,
                "Max severity": assessments[c.case_id].max_severity.value,
                "Findings": len(assessments[c.case_id].findings),
                "Stratum": c.stratum.value,
                "Selected": "<--" if c.case_id == selected_id else "",
            }
            for c in sorted(cases, key=lambda c: queue_sort_key(assessments[c.case_id]))
        ],
        hide_index=True,
        width="stretch",
        height=280,
    )

    st.divider()
    st.subheader(f"Review — {case.case_id}")
    st.caption(
        "Raw values come first by design. Reading the AI's framing first invites checking "
        "the data against the story rather than the story against the data."
    )

    st.markdown("#### 1 · Raw panel values, as the laboratory printed them")
    render_raw_panel(case.panel)

    # From BloodPanel.untrustworthy_values() — a data-layer accessor, not a
    # computation here. Surfaced explicitly because the rules engine emits no
    # finding for a value that is inside its printed range, so an invalid
    # derived value that happens to look normal is flagged nowhere downstream.
    untrustworthy = case.panel.untrustworthy_values()
    if untrustworthy:
        st.error(
            "**Printed on the report but not trustworthy** — "
            + "; ".join(f"{ANALYTES[a].display_name}: {why}" for a, why in untrustworthy.items()),
            icon=":material/report:",
        )
    with st.expander("Specimen and pre-analytic detail"):
        render_specimen(case.panel)
    warnings = case.panel.consistency_warnings()
    if warnings:
        st.warning("Internal consistency warnings: " + "; ".join(warnings))

    st.markdown("#### 2 · Rules engine findings")
    st.markdown(f"Escalation: {tier_badge(assessment.escalation)}")
    render_findings(assessment)

    st.markdown("#### 3 · AI narrative")
    st.caption("Generated prose. Read it after the data above, not before.")
    if report is None:
        st.error(error or "No narrative was produced.")
    else:
        render_narrative(report)

    st.markdown("#### 4 · Gate failures and judge objections")
    if failures:
        for f in failures:
            st.error(f"**{f.gate}** — {f.detail}\n\n> {f.offending}")
    else:
        st.success("All deterministic gates passed.", icon=":material/check:")

    if verdict is None:
        st.info("Judge not run: it runs only behind passing gates." if failures else (error or ""))
    elif verdict.no_objections:
        st.success(
            f"Judge raised no objections ({verdict.model_id}). This authorises nothing — "
            "physician review is still required.",
            icon=":material/check:",
        )
    else:
        for v in verdict.failures:
            st.warning(f"**{v.category.value}** — {v.reason}\n\n> {v.offending_span}")
    if judge_provider.model_id.startswith("fake"):
        st.caption(
            "Judge verdicts above come from the keyword stub, not a model. "
            "See calibrate.py for what its numbers do and do not mean."
        )
