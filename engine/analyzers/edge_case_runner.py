"""logical: generated corner cases + LLM-proposed counter-tests, judged against the reference solution."""
import json

from .. import sanitizers
from ..context import compare_output, env_for_sanitizers, short
from ..explain import heuristic, sanitizer_hint
from ..llm import LLMError, parse_json
from .base import Analyzer, Result, register

_PROMPT = """You are a test engineer trying to BREAK a student's C program.

Problem statement:
{statement}

Exact stdin format (fields in order, one line per group): {fmt}

Student code:
```c
{code}
```

Propose up to {n} distinct stdin inputs that are VALID for the format but most likely to make THIS code
produce a wrong answer, crash or hang (integer overflow, boundaries, zero/negative values, empty or single
element, unusual whitespace, worst-case sizes...). Each input must follow the format exactly.
Reply with JSON only: {{"inputs": [{{"input": "<exact stdin>", "why": "<short reason>"}}]}}"""


@register
class EdgeCaseRunner(Analyzer):
    name = "edge_case_runner"
    description = "Generates corner-case inputs per category (and via qwen3) and compares with the reference solution."
    needs_binary = True

    def run(self, ctx, params):
        cases = list(params.get("cases", ["zero", "negatives", "int_overflow", "boundaries", "whitespace"]))
        mode = params.get("compare", ctx.compare_mode)
        inputs = ctx.edge_inputs(cases)
        llm_info = {}
        if "llm" in cases or params.get("llm_cases"):
            llm_inputs, llm_info = self._llm_inputs(ctx, int(params.get("llm_cases", 5) or 5))
            inputs += [("llm", s, why) for s, why in llm_inputs]
        inputs = [(x[0], x[1], x[2] if len(x) > 2 else "") for x in inputs]
        if not inputs:
            return Result(1.0, "No edge cases applicable to this input format.", {"cases": cases}, verdict="n/a")

        oracle = ctx.reference_binary is not None
        binary = ctx.binary("main").binary
        san = ctx.binary("sanitized") if not oracle and sanitizers.self_test("-fsanitize=address,undefined")[0] else None
        per_cat: dict = {}
        failures, passed = [], 0
        for cat, stdin, why in inputs:
            exp = ctx.expected(stdin) if oracle else None
            if oracle and exp is None:
                continue  # reference rejected this input (e.g. invalid LLM input): don't count it
            if oracle:
                r = ctx.run(binary, stdin)
                ok = r.ok and compare_output(exp, r.stdout, mode)
            else:
                # no oracle: robustness only (no crash / hang / UB under sanitizers)
                r = ctx.run(san.binary if san and san.ok else binary, stdin, time_limit=ctx.time_limit * 5,
                            memory_limit=None, env=env_for_sanitizers())
                ok = r.ok and "runtime error" not in r.stderr and "ERROR: AddressSanitizer" not in r.stderr
            c = per_cat.setdefault(cat, {"passed": 0, "total": 0})
            c["total"] += 1
            if ok:
                passed += 1
                c["passed"] += 1
            else:
                failures.append({"category": cat, "input": short(stdin, 300), "expected": short(exp, 300) if exp else None,
                                 "got": short(r.stdout, 300), "verdict": "WA" if r.verdict == "OK" else r.verdict,
                                 "status": r.describe(), "llm_reason": why or None,
                                 "note": heuristic(stdin, exp, r.stdout, r, cat, ctx.time_limit),
                                 "sanitizer": sanitizer_hint(ctx, stdin, skip=r.timed_out)})
        total = sum(c["total"] for c in per_cat.values()) or 1
        weak = [k for k, v in per_cat.items() if v["passed"] < v["total"]]
        fb = f"{passed}/{total} corner cases pass" + (f"; fails on: {', '.join(weak)}." if weak else ".")
        if not oracle:
            fb += " (No reference solution: checked robustness only.)"
        sugg = []
        if "int_overflow" in weak:
            sugg.append("Handle overflow explicitly: compute in long long, or check operands against INT_MAX/INT_MIN first.")
        if "whitespace" in weak:
            sugg.append("Do not assume exact spacing in the input; scanf(\"%d\") already skips whitespace.")
        if "zero" in weak or "empty" in weak:
            sugg.append("Test with zero / empty input; avoid division by n and reading past the data.")
        return Result(passed / total, fb, {"oracle": "reference_solution" if oracle else "robustness",
                                           "per_category": per_cat, "cases_run": total, "llm": llm_info},
                      failures=failures, suggestions=sugg,
                      verdict=", ".join(f"{k} not handled" for k in weak[:2]) or "all corner cases pass")

    def _llm_inputs(self, ctx, n):
        if ctx.offline:
            return [], {"used": False, "reason": "offline"}
        fmt = json.dumps(ctx.rules.get("input_format", {}).get("lines", []))
        prompt = _PROMPT.format(statement=ctx.statement or ctx.rules.get("problem", ""), fmt=fmt,
                                code=ctx.source[:6000], n=n)
        try:
            text, cached = ctx.ollama.chat([{"role": "user", "content": prompt}], temperature=0, seed=42,
                                           json_mode=True, max_tokens=900)
            items = parse_json(text).get("inputs", [])
        except (LLMError, ValueError, AttributeError) as exc:
            return [], {"used": False, "reason": str(exc)[:200]}
        good, rejected, seen = [], 0, set()
        for it in items[:n * 2]:
            s = it.get("input") if isinstance(it, dict) else it
            if not isinstance(s, str):
                rejected += 1
                continue
            s = s.replace("\\n", "\n")
            if not s.endswith("\n"):
                s += "\n"
            if ctx.fmt.validate(s) and s not in seen:
                seen.add(s)
                good.append((s, it.get("why", "") if isinstance(it, dict) else ""))
            else:
                rejected += 1
        return good[:n], {"used": True, "model": ctx.ollama.model, "cached": cached,
                          "proposed": len(items), "accepted": len(good[:n]), "rejected_invalid": rejected}
