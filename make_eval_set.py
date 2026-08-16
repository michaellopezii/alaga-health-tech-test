"""Build a small stratified subset of the corpus for slow LLM runs.

Selection is deterministic and does not look at engine output. Picking the
subset by where the rules engine currently succeeds or fails would produce a
set that flatters whatever the engine does today.

Allocation: one case per variant so no named failure mode is dropped, then the
remainder spread by stratum weight, capped at two per variant. Stratum 1 is the
exception and may take more, because it has a single variant, it is the case a
real screening population actually presents, and detecting a model that invents
findings needs a supply of panels with nothing in them.

Within a variant, instances are chosen to spread across performing laboratories
first, so unit handling and lab-specific cutoffs stay represented in the subset.

    python3 make_eval_set.py --target 60
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from models import GeneratedCase, Stratum

# Share of the leftover budget, after every variant has its guaranteed one.
# Weighted towards the strata where a wrong answer is expensive or subtle,
# rather than towards the strata that are merely numerous.
REMAINDER_WEIGHTS: dict[Stratum, float] = {
    Stratum.S1_FULLY_NORMAL: 0.22,
    Stratum.S2_NORMAL_WITH_INCIDENTAL_FLAG: 0.12,
    Stratum.S3_TRUE_CRITICAL: 0.08,
    Stratum.S4_PREANALYTIC_PSEUDOCRITICAL: 0.12,
    Stratum.S5_DERIVED_VALUE_TRAP: 0.10,
    Stratum.S6_CONFLICTING_MARKERS: 0.10,
    Stratum.S7_NONFASTING_UNINTERPRETABLE: 0.04,
    Stratum.S8_POPULATION_CONTEXT: 0.10,
    Stratum.S9_PARTIAL_PANEL: 0.04,
    Stratum.S10_ADVERSARIAL_INJECTION: 0.08,
}


def pick(cases: list[GeneratedCase], n: int, variant: str) -> list[GeneratedCase]:
    """Take n cases from one variant, spreading across labs, deterministically.

    The lab rotation starts at an offset derived from the variant name. Without
    it, every variant granted a single case would hand it to whichever lab sorts
    first, and a 41-variant subset would come almost entirely from one lab --
    losing the SI-unit and lab-cutoff coverage the subset exists to carry.
    """
    by_lab: dict[str, list[GeneratedCase]] = defaultdict(list)
    for c in sorted(cases, key=lambda c: c.case_id):
        by_lab[c.panel.lab.lab_code].append(c)
    labs = sorted(by_lab)
    offset = sum(variant.encode()) % len(labs) if labs else 0
    labs = labs[offset:] + labs[:offset]

    chosen: list[GeneratedCase] = []
    while len(chosen) < n and any(by_lab.values()):
        progressed = False
        for lab in labs:
            if by_lab[lab] and len(chosen) < n:
                chosen.append(by_lab[lab].pop(0))
                progressed = True
        if not progressed:
            break
    return chosen


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", type=Path, default=Path("fixtures/corpus.jsonl"))
    ap.add_argument("--out", type=Path, default=Path("fixtures/eval_set.jsonl"))
    ap.add_argument("--target", type=int, default=60)
    ap.add_argument("--cap-per-variant", type=int, default=2)
    args = ap.parse_args()

    cases = [GeneratedCase.model_validate_json(l) for l in args.corpus.read_text().splitlines() if l.strip()]
    by_variant: dict[str, list[GeneratedCase]] = defaultdict(list)
    stratum_of: dict[str, Stratum] = {}
    for c in cases:
        by_variant[c.variant].append(c)
        stratum_of[c.variant] = c.stratum

    quota = {v: 1 for v in by_variant}  # every named failure mode survives
    remainder = max(0, args.target - len(quota))

    variants_by_stratum: dict[Stratum, list[str]] = defaultdict(list)
    for v, s in stratum_of.items():
        variants_by_stratum[s].append(v)

    for stratum, variants in sorted(variants_by_stratum.items(), key=lambda kv: kv[0].value):
        share = round(remainder * REMAINDER_WEIGHTS.get(stratum, 0.0))
        cap = len(by_variant[variants[0]]) if stratum is Stratum.S1_FULLY_NORMAL else args.cap_per_variant
        for i in range(share):
            v = sorted(variants)[i % len(variants)]
            if quota[v] < cap and quota[v] < len(by_variant[v]):
                quota[v] += 1

    selected: list[GeneratedCase] = []
    for v in sorted(by_variant):
        selected.extend(pick(by_variant[v], quota[v], v))
    selected.sort(key=lambda c: c.case_id)

    args.out.write_text(
        "\n".join(json.dumps(c.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) for c in selected) + "\n",
        encoding="utf-8",
    )

    by_stratum = Counter(c.stratum.value for c in selected)
    by_esc = Counter(c.ground_truth.expected_escalation.value for c in selected)
    by_lab = Counter(c.panel.lab.lab_code for c in selected)
    print(f"wrote {len(selected)} cases to {args.out}")
    print(f"  variants covered      {len({c.variant for c in selected})}/{len(by_variant)}")
    print(f"  injection cases       {sum(1 for c in selected if c.ground_truth.contains_prompt_injection)}")
    print(f"  invalid-derived cases {sum(1 for c in selected if c.ground_truth.invalid_derived_values)}")
    print(f"  labs                  {dict(sorted(by_lab.items()))}")
    print("  by stratum:")
    for k, n in sorted(by_stratum.items()):
        print(f"    {k:<38} {n:>3}")
    print("  by expected escalation:")
    for k, n in sorted(by_esc.items()):
        print(f"    {k:<20} {n:>3}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
