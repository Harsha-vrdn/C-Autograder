"""correctness: run the hidden test set and compare outputs."""
from ..context import compare_output, short
from ..explain import heuristic, sanitizer_hint
from .base import Analyzer, Result, register


@register
class IOTests(Analyzer):
    name = "io_tests"
    description = "Runs hidden input/output tests in the sandbox and compares outputs."
    needs_binary = True

    def run(self, ctx, params):
        cases = ctx.test_cases
        if not cases:
            return Result(0, "No tests found in testset_dir.", {"error": "empty testset"}, verdict="no tests")
        mode = params.get("compare", ctx.compare_mode)
        binary = ctx.binary("main").binary
        passed, rows, failures = 0, [], []
        for c in cases:
            r = ctx.run(binary, c["input"])
            ok = r.ok and compare_output(c["expected"], r.stdout, mode)
            passed += ok
            rows.append({"test": c["name"], "verdict": "AC" if ok else ("WA" if r.verdict == "OK" else r.verdict),
                         "cpu_s": r.cpu_time, "wall_s": r.wall_time, "max_rss_mb": r.max_rss_mb})
            if not ok:
                note = heuristic(c["input"], c["expected"], r.stdout, r, time_limit=ctx.time_limit)
                ub = sanitizer_hint(ctx, c["input"], skip=r.timed_out)
                failures.append({"test": c["name"], "input": short(c["input"], 300),
                                 "expected": short(c["expected"], 300), "got": short(r.stdout, 300),
                                 "verdict": rows[-1]["verdict"], "status": r.describe(),
                                 "note": note, "sanitizer": ub})
        total = len(cases)
        fb = f"{passed}/{total} tests pass."
        if failures:
            f = failures[0]
            fb += f" First failure: test {f['test']} ({f['verdict']})."
        return Result(passed / total, fb, {"tests_passed": passed, "tests_total": total, "per_test": rows,
                                           "compare_mode": mode},
                      failures=failures, verdict=f"{passed} of {total} tests pass")
