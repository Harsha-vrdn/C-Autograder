"""Glue between Django and the framework-independent engine: storage, rules files and the grading worker."""
from __future__ import annotations

import hashlib
import json
import queue
import re
import threading
import traceback
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from engine import Agent, RulesError, validate_rules

from .models import Submission

_queue: "queue.Queue[int]" = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_agent: Agent | None = None


# ------------------------------------------------------------------ rules files
def rules_dir() -> Path:
    return Path(settings.AUTOGRADER_RULES_DIR)


def safe_rules_name(name: str) -> str:
    name = Path(name).name
    if not re.fullmatch(r"[\w.-]+\.json", name):
        raise RulesError("rules file name must look like my_problem.json (letters, digits, _ - .)")
    return name


def list_rules() -> list[dict]:
    out = []
    for p in sorted(rules_dir().glob("*.json")):
        try:
            data = json.loads(p.read_text())
            validate_rules(data)
            out.append({"file": p.name, "problem": data.get("problem", p.stem), "metrics": data["metrics"],
                        "time_limit_s": data.get("time_limit_s"), "memory_limit_mb": data.get("memory_limit_mb"),
                        "weights": sum(float(m.get("weight", 0)) for m in data["metrics"]), "valid": True, "error": ""})
        except (ValueError, RulesError) as exc:
            out.append({"file": p.name, "problem": p.stem, "metrics": [], "valid": False, "error": str(exc)})
    return out


def read_rules(name: str) -> str:
    return (rules_dir() / safe_rules_name(name)).read_text()


def write_rules(name: str, text: str) -> dict:
    """Validate and save. Raises RulesError with a readable message."""
    name = safe_rules_name(name)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RulesError(f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    validate_rules(data)
    (rules_dir() / name).write_text(json.dumps(data, indent=2) + "\n")
    return data


# ------------------------------------------------------------------ submissions
def create_submission(rules_file: str, filename: str, source: bytes | str, student: str = "") -> Submission:
    rules_file = safe_rules_name(rules_file)
    rules = json.loads(read_rules(rules_file))
    validate_rules(rules)
    if isinstance(source, bytes):
        if len(source) > settings.AUTOGRADER_MAX_UPLOAD_BYTES:
            raise ValueError(f"{filename}: file too large (max {settings.AUTOGRADER_MAX_UPLOAD_BYTES // 1024} KB)")
        source = source.decode("utf-8", errors="replace")
    if not source.strip():
        raise ValueError(f"{filename or 'pasted code'}: empty source")
    problem = re.sub(r"[^\w-]", "_", rules.get("problem", Path(rules_file).stem))
    stem = re.sub(r"[^\w.-]", "_", Path(filename or "main.c").stem)[:60] or "main"
    sub = Submission.objects.create(student=student[:120], problem=problem, rules_file=rules_file,
                                    filename=f"{stem}.c", file_path="", source=source,
                                    source_hash=hashlib.sha256(source.encode()).hexdigest())
    # Stored next to the problem's other submissions so the originality analyzer sees peers.
    folder = Path(settings.AUTOGRADER_SUBMISSIONS_DIR) / problem / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sub.pk:05d}_{stem}.c"
    path.write_text(source)
    sub.file_path = str(path)
    sub.save(update_fields=["file_path"])
    return sub


def enqueue(sub_id: int):
    global _worker
    Submission.objects.filter(pk=sub_id).update(status=Submission.QUEUED, error="")
    _queue.put(sub_id)
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name="autograder-worker", daemon=True)
            _worker.start()


def _get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = Agent(settings.AUTOGRADER_ROOT)
    return _agent


def _work():
    # One submission at a time: keeps CPU timings (complexity metric) stable and reproducible.
    while True:
        sub_id = _queue.get()
        close_old_connections()
        try:
            grade_now(sub_id)
        except Submission.DoesNotExist:
            pass  # deleted (e.g. "Clear all") while it was waiting in the queue
        finally:
            close_old_connections()
            _queue.task_done()


def grade_now(sub_id: int) -> Submission:
    sub = Submission.objects.get(pk=sub_id)
    sub.status = Submission.RUNNING
    sub.save(update_fields=["status"])
    try:
        report = _get_agent().grade(Path(sub.file_path), rules_dir() / sub.rules_file, label=sub.filename)
        sub.report_text = report.pop("text")
        sub.report = report
        sub.grade = report["grade"]
        sub.status = Submission.DONE
        sub.error = ""
        reports = Path(settings.AUTOGRADER_REPORTS_DIR)
        reports.mkdir(exist_ok=True)
        stem = Path(sub.file_path).stem
        (reports / f"{stem}.json").write_text(json.dumps(report, indent=2))
        (reports / f"{stem}.txt").write_text(sub.report_text)
    except Exception as exc:  # report the failure on the page instead of losing the job
        sub.status = Submission.ERROR
        sub.error = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()[-2000:]}"
    sub.finished_at = timezone.now()
    sub.save()
    return sub


def clear_all() -> dict:
    """Delete every submission: database rows, uploaded files and their reports.

    Only files the web UI created are removed (``submissions/<problem>/uploads/`` and the matching
    ``reports/<stem>.*``); sample submissions, rules, tests and the LLM cache are kept.
    """
    subs_root = Path(settings.AUTOGRADER_SUBMISSIONS_DIR).resolve()
    reports = Path(settings.AUTOGRADER_REPORTS_DIR)
    files = 0
    for sub in Submission.objects.all():
        path = Path(sub.file_path).resolve() if sub.file_path else None
        if path and path.parent.name == "uploads" and subs_root in path.parents:
            for p in (path, reports / f"{path.stem}.json", reports / f"{path.stem}.txt"):
                if p.is_file():
                    p.unlink()
                    files += 1
    for d in subs_root.glob("*/uploads"):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    count, _ = Submission.objects.all().delete()
    return {"submissions": count, "files": files}


def queue_size() -> int:
    return _queue.qsize()
