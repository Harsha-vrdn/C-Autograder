"""Grade every sample N times and verify: same submission + same rules -> same grade.

    .venv/bin/python scripts/check_reproducibility.py [--runs 3] [--offline]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import Agent  # noqa: E402

SAMPLES = [("rules/add_two_integers.json", "submissions/add_two_integers"),
           ("rules/sum_array.json", "submissions/sum_array")]


def fingerprint(report):
    return report["grade"], tuple((m["name"], m["score"]) for m in report["metrics"]), \
        tuple(sorted(f["code"] for f in report["flags"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()
    agent = Agent(ROOT, offline=a.offline)
    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    ok = True
    print(f"{'submission':<26}{'grades over ' + str(a.runs) + ' runs':<34}result")
    for rules, folder in SAMPLES:
        for src in sorted((ROOT / folder).glob("*.c")):
            prints = []
            for i in range(a.runs):
                rep = agent.grade(src, ROOT / rules)
                prints.append(fingerprint(rep))
                if i == 0:
                    text = rep.pop("text")
                    (out_dir / f"{src.stem}.json").write_text(json.dumps(rep, indent=2))
                    (out_dir / f"{src.stem}.txt").write_text(text)
            same = all(p == prints[0] for p in prints)
            ok &= same
            grades = ", ".join(f"{p[0]:.2f}" for p in prints)
            print(f"{src.name:<26}{grades:<34}{'identical' if same else 'DIFFERENT'}")
            if not same:
                for name_score in zip(*[p[1] for p in prints]):
                    if len(set(name_score)) > 1:
                        print("    differs:", name_score)
    print("\nContract holds." if ok else "\nContract VIOLATED.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
