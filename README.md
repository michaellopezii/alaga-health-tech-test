# Alaga Health Eval Panel
There is a blood panel result as input, then a plain-language health profile comes out, with a physician review queue in between. **Nothing auto-releases.**

Severity and escalation are decided by a deterministic rules engine. The LLM
only writes prose explaining what the rules already decided. But it cannot
introduce a finding, a number, or an escalation tier.

> **Not approved for clinical use.** Every threshold in `thresholds.yaml` has
> `owner: UNASSIGNED`. No medical personnel has reviewed any of them. Reference
> intervals are not sourced. The judge calibration labels are self-assigned, 
> and are not clinically been validated. I am not a health professional.

---

## Setup

Python 3.12. Everything below runs with **no API key and no network**. The default LLM provider is a deterministic fake.

```bash
pip install -r requirements.txt
```

### 1. Run the tests: 72 checks

```bash
python3 test_models.py && python3 test_rules.py && python3 test_narrative_gates.py
```

`20/20`, `25/25`, `27/27`. These assert the schema's guarantees (a not-run
glucose can never become `0.0`; a value cannot be compared against a range in
another unit; nothing reaches `RELEASED` without a `PhysicianReview`), the
engine's precedence rule, and that the narrative gates catch their mutants.

### 2. Regenerate the corpus (optional and this is committed)

```bash
python3 generator.py --seed 20260816 --n 600 --out fixtures
```

600 cases across 10 strata, byte-reproducible from the seed. Ground truth is
authored in each case's blueprint *before* any value is sampled; `generator.py`
imports no rules engine.

### 3. Evaluate the rules engine against the corpus

```bash
python3 evaluate.py
```

**476/600 (79.3%) exact agreement. All 38 `EMERGENCY_NOW` cases caught, with
zero false emergencies. There's 3 safety-relevant disagreements** (ground truth urgent or above, engine below urgent). Prints a confusion matrix and every
disagreement with its case ID. The disagreements are the output, not a defect.

### 4. Run the narrative gates

```bash
python3 eval_narrative.py --corpus fixtures/corpus.jsonl
```

**All four gates pass over all 600 cases**: numeric provenance, narration
scope, escalation fidelity, prohibited form. Drop `--corpus` to run the 61-case
`fixtures/eval_set.jsonl` subset instead.

### 5. Run the judge calibration

```bash
python3 calibrate.py
```

Reports the judge's **safety false-negative rate**. These are narratives containing a diagnosis, causal claim, or treatment recommendation that it passed. With the default provider this measures a keyword stub, not a model, and the output says so on every run. Labels are self-assigned by the same author as the judge prompts. Four cases are marked `needs_your_review`.

### 6. Launch the demo UI

```bash
streamlit run app.py
```

Three views: pick a case, see the customer-facing profile, and the physician
review queue. In the review view **raw panel values come first**, before the
assessment and well before the AI narrative — a reviewer who reads the model's
framing first tends to check the data against the story rather than the story
against the data.

### Running against a real model

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

The `anthropic` package is already in `requirements.txt`. Then `--provider anthropic` on `eval_narrative.py` or `calibrate.py`, or the
sidebar toggle in the UI. Default model is `claude-haiku-4-5`. Without a key the
UI falls back to the fake provider rather than failing.

---

## What is here

| File | |
|---|---|
| `models.py` | Schema. Ranges travel with each result; no global range table. |
| `generator.py` | Seeded corpus, correlated physiology, authored ground truth. |
| `thresholds.yaml` | Every clinical number, with source, date and owner. |
| `rules.py` | Deterministic engine. No clinical constant appears in the code. |
| `narrator.py` | LLM prose layer + provider interface. |
| `eval_narrative.py` | Four deterministic gates. Build-blocking. |
| `judge.py` | Semantic second net, behind the gates. |
| `mutations.py` | Eight deliberately broken narratives. |
| `app.py` | Streamlit demo. Computes nothing clinical. |
| `data_notes.md` | Corpus stratification and what each stratum catches. |

---

## The four invariants

**Reference ranges travel with the result.** `models.py` contains no table of
normal values. The same SGPT of 44 U/L is `NORMAL` at a lab whose ceiling is 50
and `HIGH` at a lab whose ceiling is 41. Both of those reports are correct.

**Structured decisions are separate from generated prose.** `RuleFinding`
carries escalation and rule version. `NarrativeBlock` carries text and a
`prompt_version`. A validator rejects prose referencing a finding that does not
exist. `Flag` has no `CRITICAL` member — that is a clinical judgement and
belongs to the engine.

**The model cannot invent a finding.** Its output schema constrains
`finding_id` to a JSON Schema `enum` of the narratable IDs, so under constrained
decoding an invented ID is unsampleable. It supplies `(id, text)` pairs; our
code looks the ID up and builds the block. There is no `escalation` field in its
schema at all.

**Nothing auto-releases.** `HealthProfileReport` cannot enter `RELEASED` without
an approving `PhysicianReview`. A judge pass is `no_objections`, not `approved`,
and authorises nothing.

---

## Known gaps

- **The engine has no pregnancy-aware ranges.** All 8
  `pregnancy_physiologic_shifts` cases fail: it reads a third-trimester patient
  against the non-pregnant column the lab printed and calls physiologic
  hemodilution a finding. Highest-priority fix. (`python3 evaluate.py`)
- **No composite pattern rules.** Thalassemia trait scores `ROUTINE` because
  every individual analyte is only mildly deviant; the finding lives in the
  combination. A single-analyte severity model cannot see it. 16 cases.
- **An invalid derived value that lands inside its printed range produces no
  finding**, so nothing marks it unnarratable. 16 of 61 eval-set cases carry
  one that is flagged nowhere by the engine. The UI surfaces these from `BloodPanel.untrustworthy_values()`.
- **`NOT_ORDERED` produces no finding** which disagrees with the corpus label of `ROUTINE` for an incomplete panel. 6 cases. One of the two has to give.
- **The escalation enum has no `RECOLLECT`.** All of stratum 4 wants it;
  those cases are labelled `URGENT_24H` with the real intent in
  `expected_action`.
- **The judge's semantic gaps are documented, not closed**: numbers written as
  words evade the provenance scan. Aggregate softening across sentences evades
  the tier lexicon.
