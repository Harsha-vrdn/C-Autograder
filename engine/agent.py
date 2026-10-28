"""The coordinator: first-pass sanity sweep -> dispatch analyzers -> collate grade + feedback.

Contract: same submission + same rules (+ same tests/corpus/LLM cache) -> same grade.
Determinism comes from: seeded input generation, sorted iteration everywhere,
results collected in rules order (not completion order), LLM answers replayed from a
content-addressed cache, and timing measured as min-of-N CPU time mapped onto
discrete complexity classes.
"""
from __future__ import annotations

import hashlib
import json
import platform
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import lizard

from . import clexer, sandbox
from .analyzers import REGISTRY, Result
from .context import Context, compare_output, short
from .llm import GroqClient, LLMCache, LLMError, OllamaClient, load_dotenv, parse_json
from .report import render_text

ENGINE_VERSION = "1.0.0"
ENGINE_DIR = Path(__file__).resolve().parent


class RulesError(ValueError):
    pass


def load_rules(path: Path) -> dict:
    try:
        rules = json.loads(Path(path).read_text())
    except json.JSONDecodeError as exc:
        raise RulesError(f"{path}: invalid JSON: {exc}") from exc
    return validate_rules(rules)


def validate_rules(rules: dict) -> dict:
    if not isinstance(rules, dict):
        raise RulesError("rules must be a JSON object")
    metrics = rules.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise RulesError("rules need a non-empty 'metrics' list")
    names = set()
    for i, m in enumerate(metrics):
        if not isinstance(m, dict) or "name" not in m or "analyzer" not in m:
            raise RulesError(f"metric #{i + 1} needs 'name' and 'analyzer'")
        if m["analyzer"] not in REGISTRY:
            raise RulesError(f"metric '{m['name']}': unknown analyzer '{m['analyzer']}' "
                             f"(available: {', '.join(sorted(REGISTRY))})")
        if m["name"] in names:
            raise RulesError(f"duplicate metric name '{m['name']}'")
        names.add(m["name"])
        try:
            if float(m.get("weight", 0)) < 0:
                raise RulesError(f"metric '{m['name']}': weight must be >= 0")
        except (TypeError, ValueError) as exc:
            raise RulesError(f"metric '{m['name']}': weight must be a number") from exc
        if not isinstance(m.get("params", {}), dict):
            raise RulesError(f"metric '{m['name']}': params must be an object")
    if sum(float(m.get("weight", 0)) for m in metrics) <= 0:
        raise RulesError("metric weights must not all be zero")
    for key in ("time_limit_s", "memory_limit_mb"):
        if key in rules and not isinstance(rules[key], (int, float)):
            raise RulesError(f"'{key}' must be a number")
    return rules


def canonical_hash(obj) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Agent:
    def __init__(self, base_dir: Path, offline: bool = False, explain_with_llm: bool = True):
        self.base_dir = Path(base_dir).resolve()
        load_dotenv(self.base_dir)
        self.cache = LLMCache(self.base_dir / ".cache" / "llm")
        self.offline = offline
        self.explain_with_llm = explain_with_llm
        self.groq = GroqClient(self.cache)
        self.ollama = OllamaClient(self.cache)

    # ---------------------------------------------------------------- public
    def grade(self, source_path: Path, rules_path: Path, label: str | None = None) -> dict:
        started = time.monotonic()
        rules_path = Path(rules_path)
        rules = load_rules(rules_path)
        rules_hash = canonical_hash(rules)
        workdir = Path(tempfile.mkdtemp(prefix="autograder_"))
        try:
            ctx = Context(self.base_dir, rules, rules_hash, Path(source_path), workdir, self.groq, self.ollama, self.offline)
            report = self._grade(ctx, rules_path, label or Path(source_path).name)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        report["duration_s"] = round(time.monotonic() - started, 2)
        report["text"] = render_text(report)
        return report

    # ------------------------------------------------------------- pipeline
    def _grade(self, ctx: Context, rules_path: Path, label: str) -> dict:
        fp = self._first_pass(ctx)
        ctx.first_pass = fp
        exec_ok = fp["compiled"] and fp["smoke_ok"] is not None and not fp["suspected_infinite_loop"] and not fp["unsafe_calls"]
        metrics = ctx.rules["metrics"]
        total_w = sum(float(m.get("weight", 0)) for m in metrics)
        if abs(total_w - 1) > 1e-6:
            ctx.notes.append(f"metric weights sum to {total_w:g}; normalised to 1.0")

        results: dict[str, Result | None] = {}
        skipped: dict[str, str] = {}
        parallel, serial = [], []
        for m in metrics:
            cls = REGISTRY[m["analyzer"]]
            if cls.needs_binary and not exec_ok:
                skipped[m["name"]] = self._skip_reason(fp)
                continue
            (serial if cls.timing_sensitive else parallel).append(m)

        def run_one(m):
            try:
                return REGISTRY[m["analyzer"]]().run(ctx, dict(m.get("params", {})))
            except Exception as exc:  # an analyzer bug must not kill the whole report
                return Result(0, f"Analyzer error: {type(exc).__name__}: {exc}", {"error": repr(exc)}, verdict="analyzer error")

        with ThreadPoolExecutor(max_workers=4) as pool:
            for m, r in zip(parallel, pool.map(run_one, parallel)):
                results[m["name"]] = r
        for m in serial:  # timing-sensitive analyzers run alone
            results[m["name"]] = run_one(m)

        unavailable = [m["name"] for m in metrics if results.get(m["name"]) and results[m["name"]].unavailable]
        graded_w = sum(float(m.get("weight", 0)) for m in metrics if m["name"] not in unavailable)
        if unavailable:
            ctx.notes.append(f"{', '.join(unavailable)} could not be measured on this machine and was left out of "
                             "the grade; the other weights were renormalised")
            if graded_w <= 0:
                graded_w = total_w
        rows, grade, failures, flags, sugg_ranked = [], 0.0, [], [], []
        for m in metrics:
            r = results.get(m["name"])
            if r is None:
                r = Result(0, f"Skipped: {skipped[m['name']]}", {"skipped": True}, verdict="skipped")
            w = 0.0 if r.unavailable else float(m.get("weight", 0)) / graded_w
            if r.unavailable:
                flags.append({"metric": m["name"], "code": "metric_unavailable",
                              "message": f"{m['name']} not graded on this machine: {r.feedback}"})
            contrib = round(r.score * w * 100, 2)
            grade += r.score * w * 100
            rows.append({"name": m["name"], "analyzer": m["analyzer"], "weight": round(w, 4), "score": r.score,
                         "contribution": contrib, "verdict": r.verdict, "feedback": r.feedback,
                         "evidence": r.evidence, "skipped": m["name"] in skipped, "unavailable": r.unavailable})
            for f in r.failures:
                failures.append({"metric": m["name"], **f})
            flags.extend({"metric": m["name"], **f} for f in r.flags)
            lost = (1 - r.score) * w
            sugg_ranked.extend((lost, i, s) for i, s in enumerate(r.suggestions))

        failures = self._dedupe_failures(failures)
        if self.explain_with_llm:
            self._llm_explain(ctx, failures[:3])
        for code in ("suspected_infinite_loop", "unsafe_calls", "crashes_on_sample", "compile_failed"):
            if fp.get(code):
                flags.insert(0, {"metric": "first_pass", "code": code, "message": self._flag_message(code, fp)})

        suggestions, seen = [], set()
        for _, _, s in sorted(sugg_ranked, key=lambda x: (-x[0], x[1])):
            if s not in seen:
                seen.add(s)
                suggestions.append(s)

        status = ("compile_error" if not fp["compiled"] else "unsafe" if fp["unsafe_calls"]
                  else "partial" if skipped else "graded")
        return {
            "submission": label,
            "submission_sha256": ctx.source_hash,
            "problem": ctx.rules.get("problem", ""),
            "rules_file": str(rules_path),
            "rules_hash": ctx.rules_hash,
            "inputs_hash": self._inputs_hash(ctx),
            "grade": round(grade, 2),
            "max_grade": 100,
            "status": status,
            "first_pass": fp,
            "metrics": rows,
            "failures": failures[:10],
            "flags": flags,
            "suggestions": suggestions[:8],
            "profile": self._profile(rows),
            "usefulness_limitations": json.loads((ENGINE_DIR / "usefulness_limitations.json").read_text()),
            "notes": ctx.notes,
            "tool_versions": self._versions(),
            "reproducibility": {
                "engine_version": ENGINE_VERSION,
                "seeded_generation": True,
                "llm_cache_dir": str(self.cache.dir.relative_to(self.base_dir)),
                "offline": self.offline,
            },
        }

    # -------------------------------------------------------------- first pass
    def _first_pass(self, ctx: Context) -> dict:
        fp = {"compiled": False, "compile_warnings": 0, "compile_errors": [], "smoke_ok": None,
              "suspected_infinite_loop": False, "crashes_on_sample": False, "unsafe_calls": [],
              "static": {}}
        calls = set(clexer.called_names(ctx.tokens))
        fp["unsafe_calls"] = sorted(calls & clexer.DANGEROUS_CALLS)
        fp["static"] = {"has_main": any(f.name == "main" for f in ctx.functions) or "main" in calls or
                        any(t.value == "main" for t in ctx.tokens),
                        "reads_input": bool(calls & clexer.INPUT_CALLS),
                        "writes_output": bool(calls & clexer.OUTPUT_CALLS),
                        "returns_in_main": self._returns_in_main(ctx)}
        comp = ctx.binary("main")
        fp["compiled"] = comp.ok
        fp["compile_command"] = comp.command.replace(str(ctx.workdir) + "/", "")
        fp["compile_warnings"] = len(comp.warnings)
        fp["compile_errors"] = [f"line {e['line']}: {e['message']}" for e in comp.errors][:5] or (
            [comp.stderr.strip()[:300]] if not comp.ok else [])
        fp["compile_failed"] = not comp.ok
        if not comp.ok or fp["unsafe_calls"]:
            return fp
        stdin, expected = ctx.sample
        r = ctx.run(comp.binary, stdin)
        fp["smoke"] = {"verdict": r.verdict, "status": r.describe(), "cpu_s": r.cpu_time, "max_rss_mb": r.max_rss_mb,
                       "matches_sample": compare_output(expected, r.stdout, ctx.compare_mode) if expected is not None else None}
        fp["smoke_ok"] = not r.timed_out
        fp["suspected_infinite_loop"] = r.timed_out
        fp["crashes_on_sample"] = (bool(r.signal) and not r.timed_out) or r.mem_exceeded
        return fp

    @staticmethod
    def _returns_in_main(ctx):
        main = next((f for f in ctx.functions if f.name == "main"), None)
        if not main:
            return False
        return any(t.value == "return" and main.start_line <= t.line <= main.end_line for t in ctx.tokens)

    @staticmethod
    def _skip_reason(fp):
        if not fp["compiled"]:
            return "submission does not compile"
        if fp["unsafe_calls"]:
            return f"refused to execute: uses {', '.join(fp['unsafe_calls'])}"
        if fp["suspected_infinite_loop"]:
            return "smoke run timed out (suspected infinite loop)"
        return "not executable"

    @staticmethod
    def _flag_message(code, fp):
        return {
            "suspected_infinite_loop": "Smoke run on the sample input timed out; execution-based analyzers skipped.",
            "unsafe_calls": f"Program calls {', '.join(fp['unsafe_calls'])}; it was not executed.",
            "crashes_on_sample": f"Program crashed on the sample input ({fp.get('smoke', {}).get('status')}).",
            "compile_failed": "Program does not compile: " + "; ".join(fp["compile_errors"][:2]),
        }[code]

    # ---------------------------------------------------------------- feedback
    @staticmethod
    def _dedupe_failures(failures):
        out, seen = [], set()
        for f in failures:
            key = f.get("input")
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
        return out

    def _llm_explain(self, ctx: Context, failures: list):
        """One qwen3 call explains the top failures (cached -> reproducible)."""
        if self.offline or not failures:
            return
        cases = "\n\n".join(
            f"Case {i}:\nInput: {f.get('input')!r}\nExpected: {f.get('expected')!r}\nActual: {f.get('got')!r}\n"
            f"Status: {f.get('status')}\nSanitizer: {f.get('sanitizer') or 'none'}"
            for i, f in enumerate(failures, 1))
        prompt = ("You are a C teaching assistant. For each failing case below, explain the root cause IN THIS CODE in one "
                  "sentence and give the concrete fix in one sentence.\n\n"
                  f"Problem: {(ctx.statement or ctx.rules.get('problem', ''))[:1500]}\n\nCode:\n{ctx.source[:4000]}\n\n{cases}\n\n"
                  'Reply with JSON only: {"explanations": [{"case": 1, "root_cause": "...", "fix": "..."}]}')
        try:
            text, _ = self.ollama.chat([{"role": "user", "content": prompt}], temperature=0, seed=42,
                                       json_mode=True, max_tokens=500)
            items = parse_json(text).get("explanations", [])
        except (LLMError, ValueError, AttributeError):
            return
        for it in items if isinstance(items, list) else []:
            try:
                f = failures[int(it.get("case", 0)) - 1]
            except (ValueError, TypeError, IndexError):
                continue
            msg = " ".join(f"{it.get('root_cause', '')} Fix: {it.get('fix', '')}".split())
            f["llm_explanation"] = short(msg, 400)

    @staticmethod
    def _profile(rows):
        prof = {}
        for r in rows:
            ev = r["evidence"]
            if r["analyzer"] == "io_tests" and ev.get("per_test"):
                pt = ev["per_test"]
                prof["tests"] = {"max_cpu_s": max(t["cpu_s"] for t in pt), "max_wall_s": max(t["wall_s"] for t in pt),
                                 "max_rss_mb": max(t["max_rss_mb"] for t in pt)}
            if r["analyzer"] == "complexity_estimator" and ev.get("timings"):
                prof["scaling"] = ev["timings"]
        return prof

    def _inputs_hash(self, ctx: Context) -> str:
        """Hash of everything besides the source that determines the grade (rules, tests, reference)."""
        h = hashlib.sha256(ctx.rules_hash.encode())
        for c in ctx.test_cases:
            h.update(c["name"].encode() + c["input"].encode() + c["expected"].encode())
        ref = ctx.resolve(ctx.rules.get("reference_solution"))
        if ref and ref.exists():
            h.update(ref.read_bytes())
        return "sha256:" + h.hexdigest()

    def _versions(self):
        return {"cc": sandbox.compiler_version(), "lizard": getattr(lizard, "version", "?"),
                "python": platform.python_version(), "platform": f"{sys.platform}-{platform.machine()}",
                "ollama_model": self.ollama.model, "groq_model": self.groq.model, "engine": ENGINE_VERSION}
