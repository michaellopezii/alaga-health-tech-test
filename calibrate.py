"""Run the judge against hand-labelled narratives and report where it misses.

    python3 calibrate.py
    python3 calibrate.py --provider anthropic --model claude-opus-4-8

The headline number is the SAFETY FALSE-NEGATIVE rate: narratives that contain a
diagnosis, a causal claim, or a treatment recommendation which the judge passed.
Every one of those reaches a physician's queue with our "the model does not
diagnose or treat" claim attached to it. Overall agreement is the less
interesting number and is reported second.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import yaml

from judge import (
    SAFETY_CATEGORIES,
    JudgeCategory,
    JudgeError,
    JudgePayload,
    judge,
    make_judge_provider,
)
from narrator import Prompt

DEFAULT_SET = Path(__file__).parent / "calibration" / "labeled_narratives.yaml"
SAFE = "safe"


def load_cases(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]


def to_payload(case: dict) -> JudgePayload:
    return JudgePayload(
        panel_id=case["id"],
        panel_escalation=case["panel_escalation"],
        findings=tuple(case["findings"]),
        not_resulted=tuple(case.get("not_resulted") or ()),
        narrative=case["narrative"],
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", dest="path", type=Path, default=DEFAULT_SET)
    ap.add_argument("--provider", default="fake", help="fake | anthropic")
    ap.add_argument("--model", default=None)
    ap.add_argument("--prompt", default="judge_v1")
    args = ap.parse_args()

    prompt = Prompt.load(args.prompt)
    provider = make_judge_provider(
        args.provider, **({"model_id": args.model} if args.model else {})
    )
    cases = load_cases(args.path)

    print("=" * 74)
    print("JUDGE CALIBRATION")
    print("=" * 74)
    print(f"  set        {args.path}  ({len(cases)} cases)")
    print(f"  provider   {args.provider} ({provider.model_id})")
    print(f"  prompt     {prompt.version}")
    print()
    print("  LABELS ARE SELF-ASSIGNED, NOT CLINICIAN-VALIDATED. They were written")
    print("  by the same team that wrote the judge prompt, so agreement partly")
    print("  measures two artifacts of one author agreeing with each other.")
    if args.provider == "fake":
        print()
        print("  !! THE FAKE PROVIDER IS A KEYWORD STUB, NOT A JUDGE. Every number")
        print("  !! below describes a handful of substring checks. It says nothing")
        print("  !! about any model's semantic ability. Re-run with a real provider")
        print("  !! before treating any of this as a measurement.")
    print("=" * 74)
    print()

    rows: list[tuple[dict, set[JudgeCategory] | None, str]] = []
    for case in cases:
        try:
            report = judge(
                to_payload(case),
                gate_failures=(),  # this set is written to pass the gates by construction
                provider=provider,
                prompt=prompt,
            )
            rows.append((case, report.failed_categories(), ""))
        except JudgeError as exc:
            rows.append((case, None, str(exc)))

    # ---- safety false negatives: the number that matters -----------------
    safety_cases = [
        (c, f) for c, f, _ in rows if c["expected"] in {s.value for s in SAFETY_CATEGORIES}
    ]
    safety_misses = [
        (c, f) for c, f in safety_cases if f is None or JudgeCategory(c["expected"]) not in f
    ]

    print("SAFETY FALSE NEGATIVES")
    if safety_cases:
        rate = len(safety_misses) / len(safety_cases)
        print(f"  {len(safety_misses)}/{len(safety_cases)}  ({rate:.1%})")
    else:
        print("  no safety-category cases in this set")
    print("  Narratives containing a diagnosis, causal claim, or treatment")
    print("  recommendation that the judge passed. Each one reaches a patient-facing")
    print("  queue under our claim that the model does not diagnose or treat.")
    for case, found in safety_misses:
        print(f"\n    MISSED  {case['id']}  expected {case['expected']}  [{case['label_source']}]")
        print(f"      {case['note'].strip()}")
        said = "judge errored" if found is None else (sorted(c.value for c in found) or "nothing")
        print(f"      judge said: {said}")
    print()

    # ---- false positives on safe narratives ------------------------------
    safe_cases = [(c, f) for c, f, _ in rows if c["expected"] == SAFE]
    false_positives = [(c, f) for c, f in safe_cases if f]
    print("FALSE POSITIVES ON SAFE NARRATIVES")
    if safe_cases:
        print(f"  {len(false_positives)}/{len(safe_cases)}  ({len(false_positives) / len(safe_cases):.1%})")
    print("  A judge that flags clean prose sends valid reports back for rework and")
    print("  trains reviewers to dismiss it.")
    for case, found in false_positives:
        print(f"\n    FLAGGED {case['id']}  [{case['label_source']}]")
        print(f"      {case['note'].strip()}")
        print(f"      judge said: {sorted(c.value for c in found)}")
    print()

    # ---- overall agreement ------------------------------------------------
    agree = 0
    for case, found, _ in rows:
        if found is None:
            continue
        expected = case["expected"]
        if expected == SAFE:
            agree += not found
        else:
            agree += JudgeCategory(expected) in found
    print("OVERALL AGREEMENT")
    print(f"  {agree}/{len(rows)}  ({agree / len(rows):.1%})")
    print("  Counts a case as agreement when the judge flagged the labelled category")
    print("  (extra categories tolerated), or flagged nothing on a `safe` case.")
    print()

    # ---- per-case table ---------------------------------------------------
    print("PER CASE")
    print(f"  {'id':<8} {'expected':<26} {'judge':<38} ok")
    print("  " + "-" * 76)
    for case, found, err in rows:
        expected = case["expected"]
        if found is None:
            verdict, ok = f"ERROR: {err[:30]}", False
        else:
            verdict = ", ".join(sorted(c.value for c in found)) or "-"
            ok = (not found) if expected == SAFE else JudgeCategory(expected) in found
        flag = "" if ok else "  <-- disagrees"
        print(f"  {case['id']:<8} {expected:<26} {verdict[:38]:<38} {'y' if ok else 'n'}{flag}")

    needs_review = [c for c, _, _ in rows if c["label_source"] == "needs_your_review"]
    if needs_review:
        print()
        print(f"AWAITING YOUR LABEL: {', '.join(c['id'] for c in needs_review)}")
        print("  These are the arguable ones. The numbers above assume the placeholder")
        print("  labels are right; if you disagree, edit `expected` and re-run.")

    errors = [r for r in rows if r[1] is None]
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
