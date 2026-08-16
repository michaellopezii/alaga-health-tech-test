"""Run the rules engine over the corpus and report where it disagrees with ground truth.

This script does not tune anything. It reports. Disagreements are the output,
not a problem with the output: the corpus labels were authored by hand from the
clinical scenario, the engine derives its answer from thresholds.yaml, and the
two were built independently on purpose. Every divergence is a place where one
of them is wrong and a human has to decide which.

    python3 evaluate.py
    python3 evaluate.py --stratum s6_conflicting_markers --verbose
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from models import Escalation, GeneratedCase
from rules import DEFAULT_THRESHOLDS_PATH, ENGINE_VERSION, assess_panel, load_thresholds

ORDER = [
    Escalation.NO_ACTION,
    Escalation.ROUTINE,
    Escalation.SEE_DOCTOR_2WK,
    Escalation.URGENT_24H,
    Escalation.EMERGENCY_NOW,
]
SHORT = {
    Escalation.NO_ACTION: "NO_ACT",
    Escalation.ROUTINE: "ROUTIN",
    Escalation.SEE_DOCTOR_2WK: "2WK",
    Escalation.URGENT_24H: "URG24",
    Escalation.EMERGENCY_NOW: "EMERG",
}
RANK = {e: i for i, e in enumerate(ORDER)}


def load_cases(path: Path) -> list[GeneratedCase]:
    return [GeneratedCase.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", type=Path, default=Path("fixtures/corpus.jsonl"))
    ap.add_argument("--thresholds", type=Path, default=DEFAULT_THRESHOLDS_PATH)
    ap.add_argument("--stratum", help="Only report disagreements in this stratum.")
    ap.add_argument("--verbose", action="store_true", help="Print findings for each disagreement.")
    ap.add_argument("--json-out", type=Path, help="Write per-case results as JSONL.")
    args = ap.parse_args()

    cfg = load_thresholds(args.thresholds)
    cases = load_cases(args.corpus)

    print(f"engine {ENGINE_VERSION}  thresholds {cfg.thresholds_version} ({cfg.status})")
    print(f"{len(cases)} cases from {args.corpus}\n")

    matrix: Counter = Counter()
    disagreements: list[tuple[GeneratedCase, object]] = []
    by_variant: dict[str, list[int]] = defaultdict(list)
    rows = []

    for case in cases:
        got = assess_panel(case.panel, cfg)
        want = case.ground_truth.expected_escalation
        matrix[(want, got.escalation)] += 1
        agreed = want is got.escalation
        by_variant[case.variant].append(1 if agreed else 0)
        if not agreed:
            disagreements.append((case, got))
        rows.append(
            {
                "case_id": case.case_id,
                "stratum": case.stratum.value,
                "variant": case.variant,
                "expected": want.value,
                "predicted": got.escalation.value,
                "agree": agreed,
                "max_severity": got.max_severity.value,
                "n_findings": len(got.findings),
                "unnarratable": [a.value for a in got.unnarratable_analytes],
            }
        )

    # ---- confusion matrix ----
    print("CONFUSION MATRIX   rows = expected (ground truth), cols = predicted (engine)")
    header = " " * 22 + "".join(f"{SHORT[e]:>8}" for e in ORDER) + f"{'total':>9}"
    print(header)
    print(" " * 22 + "-" * (8 * len(ORDER) + 9))
    for want in ORDER:
        total = sum(matrix[(want, g)] for g in ORDER)
        cells = ""
        for got in ORDER:
            n = matrix[(want, got)]
            cells += f"{(str(n) if n else '.'):>8}" if want is not got else f"{('[' + str(n) + ']' if n else '.'):>8}"
        print(f"  {want.value:<20}{cells}{total:>9}")
    print(" " * 22 + "-" * (8 * len(ORDER) + 9))
    print(" " * 22 + "".join(f"{sum(matrix[(w, g)] for w in ORDER):>8}" for g in ORDER) + f"{len(cases):>9}")

    agree = sum(matrix[(e, e)] for e in ORDER)
    over = sum(n for (w, g), n in matrix.items() if RANK[g] > RANK[w])
    under = sum(n for (w, g), n in matrix.items() if RANK[g] < RANK[w])
    print(f"\n  exact agreement   {agree}/{len(cases)}  ({agree / len(cases):.1%})")
    print(f"  engine higher     {over:>3}  (over-escalates vs ground truth)")
    print(f"  engine lower      {under:>3}  (under-escalates vs ground truth)")

    dangerous = [
        (c, g) for c, g in disagreements
        if RANK[c.ground_truth.expected_escalation] >= RANK[Escalation.URGENT_24H]
        and RANK[g.escalation] < RANK[Escalation.URGENT_24H]
    ]
    print(f"  SAFETY-RELEVANT   {len(dangerous):>3}  (ground truth urgent+, engine below urgent)")

    # ---- per-variant agreement ----
    print("\nAGREEMENT BY VARIANT  (variants with any disagreement)")
    for variant, results in sorted(by_variant.items(), key=lambda kv: (sum(kv[1]) / len(kv[1]), kv[0])):
        rate = sum(results) / len(results)
        if rate < 1.0:
            bar = "#" * int(rate * 20)
            print(f"  {rate:>5.0%} {bar:<20} {variant}  ({sum(results)}/{len(results)})")
    clean = [v for v, r in by_variant.items() if sum(r) == len(r)]
    print(f"\n  {len(clean)}/{len(by_variant)} variants agree on every instance")

    # ---- disagreement detail ----
    shown = [
        (c, g) for c, g in disagreements if not args.stratum or c.stratum.value == args.stratum
    ]
    print(f"\nDISAGREEMENTS ({len(shown)} shown of {len(disagreements)})")
    grouped: dict[tuple[str, str, str], list[tuple[GeneratedCase, object]]] = defaultdict(list)
    for c, g in shown:
        grouped[(c.variant, c.ground_truth.expected_escalation.value, g.escalation.value)].append((c, g))

    for (variant, want, got), items in sorted(
        grouped.items(), key=lambda kv: (-abs(RANK[Escalation(kv[0][1])] - RANK[Escalation(kv[0][2])]), kv[0])
    ):
        case, assessment = items[0]
        gap = RANK[Escalation(got)] - RANK[Escalation(want)]
        direction = "engine HIGHER" if gap > 0 else "engine LOWER"
        print(f"\n  {case.stratum.value} / {variant}   x{len(items)}")
        print(f"    expected {want}  ->  predicted {got}   ({direction} by {abs(gap)} tier(s))")
        print(f"    cases: {', '.join(c.case_id for c, _ in items[:4])}" + (" ..." if len(items) > 4 else ""))
        print(f"    ground truth says: {case.ground_truth.rationale.strip()[:200]}")
        if args.verbose:
            for f in sorted(assessment.findings, key=lambda f: -RANK[f.escalation])[:6]:
                mark = " [unnarratable]" if f.unnarratable else ""
                gated = f" (was {f.escalation_before_gates.value})" if f.escalation_before_gates else ""
                print(f"      - {f.escalation.value:<14} {f.severity.value:<10} {f.rule_id}{gated}{mark}")
                print(f"        {f.machine_summary[:150]}")

    if args.json_out:
        args.json_out.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        print(f"\nper-case results written to {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
