"""LLM narrative layer. Panel + PanelAssessment in, NarrativeReport out.

The model writes prose and nothing else. Three structural layers enforce that,
in order of strength:

1. **Enum-constrained IDs.** The output schema's ``finding_id`` field is a JSON
   Schema ``enum`` holding exactly the narratable finding IDs. Under constrained
   decoding an ID outside that set is not rejected after the fact -- it is
   unsampleable. (A dict keyed by ``finding_id``, which the brief suggested,
   cannot express this: strict JSON Schema requires ``additionalProperties:
   false``, so arbitrary keys would have to be accepted and checked later.)

2. **The model supplies values; we supply meaning.** It returns
   ``(enum_id, text)`` pairs. It never constructs a ``NarrativeBlock`` -- our
   code looks each ID up in the assessment and builds the block with
   ``explains_findings`` taken from *our* finding object. There is no field in
   which the model can assert a linkage we then trust.

3. **Decision-shaped fields are absent from its schema**, with
   ``additionalProperties: false``. No escalation, no value, no flag, no
   severity. A model trying to downgrade an emergency has nowhere to write it.

Validation and retry sit behind all three, because the FAKE provider and any
backend without constrained decoding still need them.

The narrator never receives ``Specimen.comments``. The payload is built from an
explicit field whitelist, and ``test_narrative_gates.py`` asserts a canary
planted in a comment never reaches the rendered prompt.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from models import (
    ANALYTES,
    AnalyteName,
    BloodPanel,
    Escalation,
    NarrativeBlock,
    NarrativeReport,
    PanelAssessment,
    ResultStatus,
    RuleFinding,
)

PROMPT_DIR = Path(__file__).parent / "prompts"
DEFAULT_PROMPT = "narrative_v1"
MAX_ATTEMPTS = 3


# ===========================================================================
# Prompt loading
# ===========================================================================


@dataclass(frozen=True)
class Prompt:
    """A versioned system prompt.

    ``version`` carries a hash of the file's bytes, so editing a prompt without
    renaming the file still produces a different version string on every block.
    An unversioned edit cannot silently masquerade as the reviewed prompt.
    """

    name: str
    text: str
    version: str

    @classmethod
    def load(cls, name: str = DEFAULT_PROMPT, directory: Path = PROMPT_DIR) -> "Prompt":
        raw = (directory / f"{name}.md").read_text(encoding="utf-8")
        marker = "---SYSTEM---"
        if marker not in raw:
            raise ValueError(f"{name}.md has no {marker} marker")
        body = raw.split(marker, 1)[1].strip()
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        return cls(name=name, text=body, version=f"{name}+{digest}")


# ===========================================================================
# What the model is allowed to see
# ===========================================================================


class FindingView(BaseModel):
    """One finding, projected for the model. No comments, no identifiers."""

    model_config = ConfigDict(frozen=True)

    finding_id: str
    rule_id: str
    analytes: tuple[str, ...]
    observed: str | None
    reference: str | None
    severity: str
    escalation: str
    machine_summary: str


class NarratorPayload(BaseModel):
    """The complete model-visible view of a case.

    Built from an explicit whitelist. Absent by construction: specimen comments,
    specimen and accession identifiers, patient_ref, and every finding marked
    unnarratable.
    """

    model_config = ConfigDict(frozen=True)

    patient: dict[str, Any]
    panel_escalation: str
    findings: tuple[FindingView, ...]
    not_resulted: tuple[str, ...]
    do_not_discuss: tuple[str, ...]


def build_payload(panel: BloodPanel, assessment: PanelAssessment) -> NarratorPayload:
    narratable = [f for f in assessment.findings if not f.unnarratable]
    blocked = {
        ANALYTES[a].display_name for f in assessment.findings if f.unnarratable
        for a in f.triggering_analytes
    }
    not_resulted = [
        ANALYTES[a].display_name
        for a, r in panel.results.items()
        if r.status is not ResultStatus.RESULTED
    ]
    p = panel.patient
    return NarratorPayload(
        patient={
            "age_years": p.age_years,
            "biological_sex": p.biological_sex.value,
            "pregnancy_status": p.pregnancy_status.value,
        },
        panel_escalation=assessment.escalation.value,
        findings=tuple(
            FindingView(
                finding_id=f.finding_id,
                rule_id=f.rule_id,
                analytes=tuple(ANALYTES[a].display_name for a in f.triggering_analytes),
                observed=f.observed,
                reference=f.reference,
                severity=f.severity.value,
                escalation=f.escalation.value,
                machine_summary=f.machine_summary,
            )
            for f in narratable
        ),
        not_resulted=tuple(sorted(not_resulted)),
        do_not_discuss=tuple(sorted(blocked)),
    )


def system_notices(panel: BloodPanel, assessment: PanelAssessment) -> tuple[str, ...]:
    """Template text for what the model was not allowed to narrate.

    Code writes this, not the model. An invalid Friedewald LDL is the one number
    on the report that must not be stated as fact, so the sentence about it is
    the one sentence a language model does not get to compose.
    """
    notices: list[str] = []
    for f in assessment.findings:
        if not f.unnarratable:
            continue
        for analyte in f.triggering_analytes:
            result = panel.get(analyte)
            reason = ""
            if result is not None and result.derivation is not None:
                reason = result.derivation.invalid_reason or ""
            notices.append(
                f"{ANALYTES[analyte].display_name} is shown on the laboratory report but "
                f"could not be calculated reliably for this sample"
                + (f" ({reason})." if reason else ".")
                + " It has been excluded from this summary and needs a doctor's review."
            )
    return tuple(dict.fromkeys(notices))


# ===========================================================================
# The model's output contract
# ===========================================================================


class ProposedBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    finding_id: str
    text: str = Field(min_length=1)


class ProposedNarrative(BaseModel):
    """Raw model output, before binding. Untrusted.

    ``extra="forbid"`` means a model that adds an ``escalation`` key, or any
    other field, is rejected rather than partially accepted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    blocks: tuple[ProposedBlock, ...] = ()
    summary: str = Field(min_length=1)
    next_step: str = Field(min_length=1)


def output_schema(narratable_ids: list[str]) -> dict[str, Any]:
    """Strict JSON Schema for the model's response.

    The ``enum`` on ``finding_id`` is the load-bearing part: with constrained
    decoding the model cannot emit an ID that is not in the assessment, because
    no such token sequence is permitted. Everything else here is a closed shape
    with no field that could carry a decision.
    """
    blocks: dict[str, Any] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "string", "enum": narratable_ids},
                "text": {"type": "string"},
            },
            "required": ["finding_id", "text"],
            "additionalProperties": False,
        },
    }
    if not narratable_ids:
        # A panel with nothing to narrate permits no blocks at all. An empty
        # enum is not valid JSON Schema, and leaving finding_id an unconstrained
        # string here would reopen the exact hole the enum exists to close.
        blocks = {"type": "array", "maxItems": 0, "items": {"type": "object"}}
    return {
        "type": "object",
        "properties": {
            "blocks": blocks,
            "summary": {"type": "string"},
            "next_step": {"type": "string"},
        },
        "required": ["blocks", "summary", "next_step"],
        "additionalProperties": False,
    }


# ===========================================================================
# Providers
# ===========================================================================


class NarrationError(RuntimeError):
    """The model could not produce a valid narrative. No partial output is returned.

    For a health product, no narrative is a better outcome than an invalid one:
    a missing section is visible to the physician reviewing the queue, whereas a
    silently malformed one is not.
    """


class Provider(Protocol):
    model_id: str

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str: ...


@dataclass
class FakeProvider:
    """Deterministic canned prose. No network, no key, no SDK.

    Exists so the whole harness runs and demos offline, and so the gates have a
    known-good input to prove they pass things as well as fail them. Text is
    templated from the finding's own fields, which means every number it emits
    came from the panel -- it satisfies numeric provenance by construction.
    """

    model_id: str = "fake-deterministic-v1"

    _TIER_STEP = {
        "EMERGENCY_NOW": "Go to an emergency room now.",
        "URGENT_24H": "Arrange to be seen within 24 hours.",
        "SEE_DOCTOR_2WK": "Book an appointment with a doctor within two weeks.",
        "ROUTINE": "Raise this at your next routine visit.",
        "NO_ACTION": "No follow-up is needed for this panel.",
    }
    _SEVERITY_PHRASE = {
        "critical": "far outside",
        "marked": "well outside",
        "moderate": "outside",
        "mild": "a little outside",
        "borderline": "just outside",
        "none": "outside",
    }

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        payload = json.loads(user)
        blocks = []
        for f in payload["findings"]:
            analyte = f["analytes"][0] if f["analytes"] else "This result"
            phrase = self._SEVERITY_PHRASE.get(f["severity"], "outside")
            if f["observed"] and f["reference"]:
                text = (
                    f"Your {analyte} was measured at {f['observed']}. "
                    f"That is {phrase} the range this laboratory prints for it, "
                    f"which is {f['reference']}."
                )
            elif f["observed"]:
                text = f"Your {analyte} was measured at {f['observed']}."
            else:
                text = (
                    f"{analyte} could not be reported for this sample, so it has not "
                    "been assessed."
                )
            blocks.append({"finding_id": f["finding_id"], "text": text})

        tier = payload["panel_escalation"]
        if payload["findings"]:
            summary = (
                f"This panel has {len(payload['findings'])} result "
                f"{'that needs' if len(payload['findings']) == 1 else 'that need'} "
                "attention, explained above. A doctor reviews this report before you "
                "receive it."
            )
        else:
            summary = (
                "No results on this panel fell outside the ranges printed by the "
                "performing laboratory. A doctor reviews this report before you "
                "receive it."
            )
        if payload["not_resulted"]:
            summary += (
                f" Some tests were not completed, so they have not been assessed: "
                f"{', '.join(payload['not_resulted'])}."
            )
        return json.dumps(
            {"blocks": blocks, "summary": summary, "next_step": self._TIER_STEP[tier]}
        )


@dataclass
class AnthropicProvider:
    """Live provider. Swapping models is a config change, not a code change.

    Uses structured outputs (``output_config.format``) so the response is
    schema-valid JSON rather than prose we have to salvage. The SDK is imported
    lazily: the FAKE path must run with the package absent.
    """

    model_id: str = "claude-haiku-4-5"
    max_tokens: int = 8000
    effort: str | None = None
    _client: Any = None

    def __post_init__(self) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - environment guard
            raise NarrationError(
                "AnthropicProvider needs the `anthropic` package: pip install anthropic. "
                "Use FakeProvider to run the harness without it."
            ) from exc
        self._client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        # `effort` is off by default because the default model does not accept
        # it: on Haiku 4.5 an effort value is rejected outright. Set it only
        # when pointing this at a model that supports the parameter.
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort is not None:
            output_config["effort"] = self.effort
        response = self._client.messages.create(
            model=self.model_id,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
        )
        if response.stop_reason == "refusal":
            raise NarrationError(f"model refused: {getattr(response, 'stop_details', None)}")
        return next(b.text for b in response.content if b.type == "text")


PROVIDERS: dict[str, Any] = {"fake": FakeProvider, "anthropic": AnthropicProvider}


def make_provider(name: str = "fake", **kwargs: Any) -> Provider:
    try:
        return PROVIDERS[name](**kwargs)
    except KeyError as exc:
        raise NarrationError(
            f"unknown provider {name!r}; available: {sorted(PROVIDERS)}"
        ) from exc


# ===========================================================================
# Narration
# ===========================================================================


def _bind(
    proposal: ProposedNarrative,
    assessment: PanelAssessment,
    panel: BloodPanel,
    prompt: Prompt,
    model_id: str,
) -> NarrativeReport:
    """Turn model output into a report. The model's IDs are looked up, not trusted."""
    by_id: dict[str, RuleFinding] = {f.finding_id: f for f in assessment.findings}
    generated_at = datetime.now(timezone.utc)

    blocks: list[NarrativeBlock] = []
    for i, proposed in enumerate(proposal.blocks):
        finding = by_id.get(proposed.finding_id)
        if finding is None:
            raise ValidationError.from_exception_data(
                "ProposedNarrative",
                [
                    {
                        "type": "value_error",
                        "loc": ("blocks", i, "finding_id"),
                        "input": proposed.finding_id,
                        "ctx": {
                            "error": ValueError(
                                f"{proposed.finding_id!r} is not a finding in this assessment"
                            )
                        },
                    }
                ],
            )
        blocks.append(
            NarrativeBlock(
                block_id=f"{assessment.panel_id}:nb{i:02d}",
                # Taken from our finding, never from the model's claim.
                explains_findings=(finding.finding_id,),
                text=proposed.text,
                model_id=model_id,
                prompt_version=prompt.version,
                generated_at=generated_at,
            )
        )

    return NarrativeReport(
        panel_id=assessment.panel_id,
        escalation=assessment.escalation,  # copied, never model-authored
        blocks=tuple(blocks),
        summary=proposal.summary,
        next_step=proposal.next_step,
        system_notices=system_notices(panel, assessment),
        assessment_finding_ids=tuple(by_id),
        unnarratable_finding_ids=tuple(f.finding_id for f in assessment.findings if f.unnarratable),
        model_id=model_id,
        prompt_version=prompt.version,
        generated_at=generated_at,
    )


def narrate(
    panel: BloodPanel,
    assessment: PanelAssessment,
    provider: Provider | None = None,
    prompt: Prompt | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> NarrativeReport:
    """Generate, validate, retry. Raises rather than returning invalid prose."""
    provider = provider or make_provider("fake")
    prompt = prompt or Prompt.load()

    payload = build_payload(panel, assessment)
    schema = output_schema([f.finding_id for f in payload.findings])
    user = payload.model_dump_json(indent=2)
    system = prompt.text
    problems: list[str] = []

    for attempt in range(max_attempts):
        system_now = system
        if problems:
            # Feed the validation error back rather than silently retrying: a
            # model that failed once needs to know how, not another dice roll.
            system_now = (
                f"{system}\n\n# Correction\n"
                f"Your previous response was rejected: {problems[-1]}\n"
                "Return corrected JSON matching the schema exactly."
            )
        try:
            raw = provider.complete(system_now, user, schema)
            proposal = ProposedNarrative.model_validate_json(raw)
            return _bind(proposal, assessment, panel, prompt, provider.model_id)
        except (ValidationError, ValueError) as exc:
            problems.append(f"attempt {attempt + 1}: {type(exc).__name__}: {exc}")

    raise NarrationError(
        f"{assessment.panel_id}: no valid narrative after {max_attempts} attempts. "
        + " | ".join(problems)
    )
