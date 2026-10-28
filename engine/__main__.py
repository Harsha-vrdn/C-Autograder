"""CLI: python -m engine <submission.c ...> --rules rules/x.json [--out reports/] [--offline]"""
import argparse
import json
import sys
from pathlib import Path

from .agent import Agent, RulesError


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m engine", description="Grade C submissions against a rules file.")
    ap.add_argument("submissions", nargs="+", type=Path)
    ap.add_argument("--rules", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=Path("reports"))
    ap.add_argument("--base-dir", type=Path, default=Path.cwd())
    ap.add_argument("--offline", action="store_true", help="no Groq/Ollama calls (cached answers are not used either)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    agent = Agent(a.base_dir, offline=a.offline)
    a.out.mkdir(parents=True, exist_ok=True)
    for sub in a.submissions:
        try:
            report = agent.grade(sub, a.rules)
        except RulesError as exc:
            sys.exit(f"rules error: {exc}")
        stem = sub.stem
        text = report.pop("text")
        (a.out / f"{stem}.json").write_text(json.dumps(report, indent=2))
        (a.out / f"{stem}.txt").write_text(text)
        if not a.quiet:
            print(text)
        print(f"{sub}: {report['grade']:.2f}/100 -> {a.out / stem}.json, .txt", file=sys.stderr)


if __name__ == "__main__":
    main()
