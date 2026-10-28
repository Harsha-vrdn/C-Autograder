import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from engine import Agent, RulesError
from grader import services


class Command(BaseCommand):
    help = "Grade C submissions unattended: writes reports/<name>.json and .txt (and optionally stores them in the web UI)."

    def add_arguments(self, parser):
        parser.add_argument("submissions", nargs="+", type=Path)
        parser.add_argument("--rules", required=True, help="rules file, e.g. rules/add_two_integers.json")
        parser.add_argument("--out", type=Path, default=Path(settings.AUTOGRADER_REPORTS_DIR))
        parser.add_argument("--save", action="store_true", help="also record the submissions in the web UI database")
        parser.add_argument("--offline", action="store_true", help="skip Groq/Ollama (heuristic fallbacks)")
        parser.add_argument("--print", action="store_true", dest="show", help="print the text report")

    def handle(self, *args, **o):
        rules = Path(o["rules"])
        if not rules.exists():
            raise CommandError(f"rules file not found: {rules}")
        o["out"].mkdir(parents=True, exist_ok=True)
        agent = Agent(settings.AUTOGRADER_ROOT, offline=o["offline"])
        for path in o["submissions"]:
            if not path.exists():
                self.stderr.write(f"skip {path}: not found")
                continue
            if o["save"]:
                sub = services.create_submission(rules.name, path.name, path.read_bytes())
                sub = services.grade_now(sub.pk)
                if sub.status != sub.DONE:
                    self.stderr.write(f"{path}: {sub.error.splitlines()[0]}")
                    continue
                report, text = sub.report, sub.report_text
            else:
                try:
                    report = agent.grade(path, rules)
                except RulesError as exc:
                    raise CommandError(str(exc))
                text = report.pop("text")
            (o["out"] / f"{path.stem}.json").write_text(json.dumps(report, indent=2))
            (o["out"] / f"{path.stem}.txt").write_text(text)
            if o["show"]:
                self.stdout.write(text)
            flags = ", ".join(f["code"] for f in report["flags"]) or "no flags"
            self.stdout.write(self.style.SUCCESS(f"{path}: {report['grade']:.2f}/100 ({report['status']}, {flags})"))
