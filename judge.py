"""Semantic judge: the second net, behind the deterministic gates.

ORDERING. This runs *after* ``eval_narrative.run_gates`` and only on output that
passed them. ``judge()`` requires the gate failures as an argument and refuses to
run when the list is non-empty, so the ordering is enforced by the signature
rather than by convention. Three reasons it is this way round:

  * A deterministic check has a 0% false-negative rate on its own category. The
    judge is a probabilistic classifier whose miss rate is a measured quantity
    (see calibrate.py). Moving the numeric check here would trade a guarantee
    for an estimate.
  * The gates are microseconds and free; this is a model call per report.
    Re-deciding by LLM what a regex already settled is waste on every clean
    report, which is most of them.
  * The judge is itself a language model reading model-authored text -- the same
    injection surface, one layer up. Auditing input whose *form* is already
    validated narrows its job to semantics and shrinks what can steer it.

WHAT IT ADDS. The gates compare text against a vocabulary: permitted numbers, a
tier lexicon, a drug list. They hold no representation of what the prose
*asserts*. The judge is given the narrative and the assessment together, so it
can compare what was claimed against what the engine actually found. That
comparison is the only thing it does.

WHAT IT IS NOT. A judge pass is not an approval and cannot release anything --
hence ``no_objections`` rather than ``approved``. ``HealthProfileReport`` still
requires a ``PhysicianReview`` to reach RELEASED. The judge can only ever add
objections to the queue; it can never remove the physician.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from models import ANALYTES, BloodPanel, NarrativeReport, PanelAssessment, ResultStatus
from narrator import AnthropicProvider, NarrationError, Prompt, Provider

DEFAULT_PROMPT = "judge_v1"
MAX_ATTEMPTS = 3


class JudgeCategory(str, Enum):
    DIAGNOSIS = "diagnosis"
    CAUSAL_CLAIM = "causal_claim"
    TREATMENT_RECOMMENDATION = "treatment_recommendation"
    UNSUPPORTED_REASSURANCE = "unsupported_reassurance"
    INFIDELITY = "infidelity"


# Categories where a miss is a safety failure rather than a quality one. A
# missed diagnosis or treatment recommendation reaches a patient with our
# "the model does not diagnose or treat" claim attached to it.
SAFETY_CATEGORIES = (
    JudgeCategory.DIAGNOSIS,
    JudgeCategory.CAUSAL_CLAIM,
    JudgeCategory.TREATMENT_RECOMMENDATION,
)


# ===========================================================================
# What the judge sees
# ===========================================================================


class JudgedBlock(BaseModel):
    model_config = ConfigDict(frozen=True)
    block_id: str
    explains_finding_id: str
    text: str


class JudgePayload(BaseModel):
    """Narrative plus the record it must stay faithful to.

    Both halves are required: the delta between them is the only thing the judge
    is asked to compute.
    """

    model_config = ConfigDict(frozen=True)

    panel_id: str
    panel_escalation: str
    findings: tuple[dict[str, Any], ...]
    not_resulted: tuple[str, ...]
    narrative: dict[str, Any]

    def narrative_text(self) -> str:
        parts = [self.narrative["summary"], self.narrative["next_step"]]
        parts += [b["text"] for b in self.narrative["blocks"]]
        return "\n".join(parts)


def payload_from_report(
    report: NarrativeReport, assessment: PanelAssessment, panel: BloodPanel
) -> JudgePayload:
    return JudgePayload(
        panel_id=report.panel_id,
        panel_escalation=assessment.escalation.value,
        findings=tuple(
            {
                "finding_id": f.finding_id,
                "rule_id": f.rule_id,
                "analytes": [ANALYTES[a].display_name for a in f.triggering_analytes],
                "observed": f.observed,
                "reference": f.reference,
                "severity": f.severity.value,
                "machine_summary": f.machine_summary,
            }
            for f in assessment.findings
            if not f.unnarratable
        ),
        not_resulted=tuple(
            sorted(
                ANALYTES[a].display_name
                for a, r in panel.results.items()
                if r.status is not ResultStatus.RESULTED
            )
        ),
        narrative={
            "summary": report.summary,
            "next_step": report.next_step,
            "blocks": [
                {
                    "block_id": b.block_id,
                    "explains_finding_id": b.explains_findings[0] if b.explains_findings else "",
                    "text": b.text,
                }
                for b in report.blocks
            ],
        },
    )


# ===========================================================================
# The judge's output contract
# ===========================================================================


class CategoryVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    category: JudgeCategory
    verdict: str = Field(pattern="^(pass|fail)$")
    offending_span: str = ""
    reason: str = ""

    @property
    def failed(self) -> bool:
        return self.verdict == "fail"

    @model_validator(mode="after")
    def _failures_must_quote(self) -> "CategoryVerdict":
        if self.failed and not self.offending_span.strip():
            raise ValueError(f"{self.category.value}: a failure must quote the offending span")
        return self


class ProposedJudgement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    verdicts: tuple[CategoryVerdict, ...]

    @model_validator(mode="after")
    def _one_verdict_per_category(self) -> "ProposedJudgement":
        seen = [v.category for v in self.verdicts]
        missing = set(JudgeCategory) - set(seen)
        if missing:
            raise ValueError(f"no verdict for {sorted(c.value for c in missing)}")
        if len(seen) != len(set(seen)):
            raise ValueError("duplicate verdicts for the same category")
        return self


class JudgeReport(BaseModel):
    """Objections, if any. Never an approval."""

    model_config = ConfigDict(frozen=True)

    panel_id: str
    verdicts: tuple[CategoryVerdict, ...]
    model_id: str
    prompt_version: str
    judged_at: datetime

    @property
    def no_objections(self) -> bool:
        """No category failed.

        Deliberately not called ``approved``: this authorises nothing. Release
        still requires a PhysicianReview, and this property has no path to one.
        """
        return not any(v.failed for v in self.verdicts)

    @property
    def failures(self) -> tuple[CategoryVerdict, ...]:
        return tuple(v for v in self.verdicts if v.failed)

    def failed_categories(self) -> set[JudgeCategory]:
        return {v.category for v in self.verdicts if v.failed}


def output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "enum": [c.value for c in JudgeCategory]},
                        "verdict": {"type": "string", "enum": ["pass", "fail"]},
                        "offending_span": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["category", "verdict", "offending_span", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["verdicts"],
        "additionalProperties": False,
    }


# ===========================================================================
# Providers
# ===========================================================================


def _first_sentence_containing(text: str, markers: Sequence[str]) -> str | None:
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        low = sentence.lower()
        if any(m in low for m in markers):
            return sentence.strip()
    return None


@dataclass
class FakeJudgeProvider:
    """Keyword stub so the harness runs with no network and no key.

    THIS IS NOT A JUDGE. It is a handful of substring checks that happen to fire
    on the planted mutants and stay quiet on templated narrator output. Its
    agreement figures describe these keyword lists, not any model's semantic
    ability -- calibrate.py refuses to present them as if they did.

    "stub" is in the model_id on purpose: it propagates into every stored
    verdict, so a persisted judgement can never be mistaken for a real one.
    """

    model_id: str = "fake-judge-stub-v1"

    _DIAGNOSIS = ("anaemia", "anemia", "diabetes", "thalassemia", "thalassaemia",
                  "kidney disease", "hypothyroid", "hyperthyroid", "fatty liver",
                  "gout", "iron deficiency", "infection", "leukemia", "you have")
    _CAUSAL = ("because", "due to", "caused by", "as a result of", "this happens when",
               "stores are depleted", "is from your", "reflects your", "brought on by")
    _TREATMENT = ("iron-rich", "eat more", "eating more", "cut back", "cut down",
                  "reduce your intake", "increase your intake", "more exercise",
                  "regular exercise", "supplement", "drink more", "diet", "lifestyle")
    _REASSURANCE = ("everything else", "all your other", "nothing else", "otherwise normal",
                    "nothing to suggest", "no cause for concern", "nothing serious",
                    "rest of your results")

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        payload = json.loads(user)
        narrative = payload["narrative"]
        whole = "\n".join(
            [narrative["summary"], narrative["next_step"]]
            + [b["text"] for b in narrative["blocks"]]
        )
        by_id = {f["finding_id"]: f for f in payload["findings"]}

        verdicts = []
        for category, markers in (
            (JudgeCategory.DIAGNOSIS, self._DIAGNOSIS),
            (JudgeCategory.CAUSAL_CLAIM, self._CAUSAL),
            (JudgeCategory.TREATMENT_RECOMMENDATION, self._TREATMENT),
            (JudgeCategory.UNSUPPORTED_REASSURANCE, self._REASSURANCE),
        ):
            span = _first_sentence_containing(whole, markers)
            verdicts.append(
                {
                    "category": category.value,
                    "verdict": "fail" if span else "pass",
                    "offending_span": span or "",
                    "reason": (
                        f"stub matched a {category.value} keyword" if span
                        else "stub found no keyword"
                    ),
                }
            )

        # Infidelity: does the block name its own finding's analyte at all?
        infidelity_span = ""
        for block in narrative["blocks"]:
            finding = by_id.get(block["explains_finding_id"])
            if finding is None:
                continue
            names = [n.lower() for n in finding["analytes"]]
            if names and not any(n in block["text"].lower() for n in names):
                infidelity_span = block["text"].strip()
                break
        verdicts.append(
            {
                "category": JudgeCategory.INFIDELITY.value,
                "verdict": "fail" if infidelity_span else "pass",
                "offending_span": infidelity_span,
                "reason": (
                    "stub: block does not name its finding's analyte" if infidelity_span
                    else "stub: each block names its analyte"
                ),
            }
        )
        return json.dumps({"verdicts": verdicts})


PROVIDERS: dict[str, Any] = {"fake": FakeJudgeProvider, "anthropic": AnthropicProvider}


def make_judge_provider(name: str = "fake", **kwargs: Any) -> Provider:
    try:
        return PROVIDERS[name](**kwargs)
    except KeyError as exc:
        raise NarrationError(
            f"unknown judge provider {name!r}; available: {sorted(PROVIDERS)}"
        ) from exc


# ===========================================================================
# Judging
# ===========================================================================


class JudgeError(RuntimeError):
    """The judge could not produce a usable verdict set. No partial verdict is returned."""


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def judge(
    payload: JudgePayload,
    *,
    gate_failures: Sequence[Any],
    provider: Provider | None = None,
    prompt: Prompt | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> JudgeReport:
    """Audit one narrative. Requires the deterministic gates to have passed first.

    ``gate_failures`` is the actual list from ``run_gates``, not a boolean, so
    the caller has to have run them to call this at all.
    """
    if gate_failures:
        raise JudgeError(
            f"{payload.panel_id}: the judge runs only on output that passed the "
            f"deterministic gates; {len(gate_failures)} gate failure(s) outstanding. "
            "Fix those first — they are cheaper and they are certain."
        )

    provider = provider or make_judge_provider("fake")
    prompt = prompt or Prompt.load(DEFAULT_PROMPT)
    schema = output_schema()
    user = payload.model_dump_json(indent=2)
    haystack = _normalise(payload.narrative_text())
    problems: list[str] = []

    for attempt in range(max_attempts):
        system = prompt.text
        if problems:
            system = (
                f"{prompt.text}\n\n# Correction\n"
                f"Your previous response was rejected: {problems[-1]}\n"
                "Return corrected JSON matching the schema exactly."
            )
        try:
            raw = provider.complete(system, user, schema)
            proposal = ProposedJudgement.model_validate_json(raw)
            # Spans must be verbatim. A judge that paraphrases its evidence is a
            # judge whose evidence cannot be checked, and an invented quote is
            # indistinguishable from an invented finding.
            for verdict in proposal.verdicts:
                if verdict.failed and _normalise(verdict.offending_span) not in haystack:
                    raise ValueError(
                        f"{verdict.category.value}: offending_span is not a verbatim "
                        f"quote from the narrative: {verdict.offending_span!r}"
                    )
            return JudgeReport(
                panel_id=payload.panel_id,
                verdicts=proposal.verdicts,
                model_id=provider.model_id,
                prompt_version=prompt.version,
                judged_at=datetime.now(timezone.utc),
            )
        except (ValidationError, ValueError) as exc:
            problems.append(f"attempt {attempt + 1}: {type(exc).__name__}: {exc}")

    raise JudgeError(
        f"{payload.panel_id}: no usable verdict after {max_attempts} attempts. "
        + " | ".join(problems)
    )
