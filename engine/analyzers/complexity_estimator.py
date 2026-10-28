"""complexity: empirical timing curve (log-log slope) cross-checked with static loop nesting."""
import math
import re

from .. import clexer
from .base import Analyzer, Result, register

CLASSES = ["O(1)", "O(log n)", "O(n)", "O(n log n)", "O(n^2)", "O(n^3)", "O(2^n)"]
NOISE_FLOOR_S = 0.004   # CPU-time differences below this are indistinguishable from process start-up noise


def class_rank(text: str) -> int:
    t = re.sub(r"\s+", "", text.lower()).replace("²", "^2").replace("³", "^3").replace("*", "")
    table = {"o(1)": 0, "o(logn)": 1, "o(n)": 2, "o(nlogn)": 3, "o(n^2)": 4, "o(n2)": 4,
             "o(n^3)": 5, "o(n3)": 5, "o(2^n)": 6}
    if t not in table:
        raise ValueError(f"unknown complexity class '{text}'")
    return table[t]


@register
class ComplexityEstimator(Analyzer):
    name = "complexity_estimator"
    description = "Fits CPU time vs input size on a log-log scale; falls back to loop-nesting analysis when timings are flat."
    needs_binary = True
    timing_sensitive = True

    def run(self, ctx, params):
        required = class_rank(params.get("required", "O(n)"))
        sizes = sorted(int(s) for s in params.get("sizes", [1000, 10000, 100000]))
        repeats = int(params.get("repeats", 3))
        per_run_limit = float(params.get("time_limit_s", max(ctx.time_limit, 2)))
        static_rank, static_why = self._static(ctx)

        binary = ctx.binary("main").binary
        timings, tle_at, crashed_at = [], None, None
        for n in sizes:
            stdin = ctx.fmt.sized_input(n, ctx.rng(f"complexity:{n}")) if ctx.fmt.has_size else ctx.sample[0]
            best, rss = None, 0.0
            for _ in range(repeats):
                r = ctx.run(binary, stdin, time_limit=per_run_limit, memory_limit=max(ctx.memory_limit, 256))
                if r.timed_out:
                    tle_at = n
                    break
                if r.signal or r.mem_exceeded:
                    crashed_at = n  # timings of a crashing run say nothing about complexity
                    break
                best = r.cpu_time if best is None else min(best, r.cpu_time)
                rss = max(rss, r.max_rss_mb)
            if tle_at or crashed_at:
                break
            timings.append({"n": n, "cpu_s": round(best, 4), "max_rss_mb": rss})

        slope, conclusive = self._slope(timings)
        if crashed_at is not None:
            ctx.notes.append(f"complexity: program crashed at n={crashed_at}; used the sizes before it")
        if tle_at is not None:
            measured = max(required + 1, static_rank)
            how = f"timed out at n={tle_at} ({per_run_limit:g}s)"
        elif conclusive:
            measured = self._rank_from_slope(slope, static_rank)
            how = f"log-log slope {slope:.2f}"
        else:
            if ctx.fmt.has_size:
                measured, how = static_rank, "timings flat at these sizes; used static loop analysis"
            else:
                measured, how = 0, "input has no size parameter and run time is constant"
        diff = measured - required
        score = 1.0 if diff <= 0 else max(0.0, 1 - 0.5 * diff)
        verdict = f"{CLASSES[measured]} {'confirmed' if diff <= 0 else 'exceeds'} (required {CLASSES[required]})"
        fb = f"Estimated {CLASSES[measured]} ({how}); static analysis: {static_why}. Required {CLASSES[required]}."
        sugg = [] if diff <= 0 else [f"Your algorithm looks like {CLASSES[measured]} but {CLASSES[required]} is required; "
                                     "remove nested loops over the input or use a better data structure / precomputation."]
        return Result(score, fb, {"required": CLASSES[required], "estimated": CLASSES[measured],
                                  "timings": timings, "slope": None if slope is None else round(slope, 3),
                                  "conclusive": conclusive, "timed_out_at": tle_at, "crashed_at": crashed_at,
                                  "static_estimate": CLASSES[static_rank], "static_reason": static_why},
                      suggestions=sugg, verdict=verdict)

    @staticmethod
    def _slope(timings):
        if len(timings) < 2:
            return None, False
        base = timings[0]["cpu_s"]
        pts = [(math.log(t["n"]), math.log(t["cpu_s"] - base + 1e-4)) for t in timings[1:]
               if t["cpu_s"] - base > NOISE_FLOOR_S]
        if len(pts) < 2 and timings[-1]["cpu_s"] - base <= NOISE_FLOOR_S:
            return 0.0, False
        if len(pts) < 2:
            # only the largest size rose above noise: compare with the smallest one directly
            t0, t1 = timings[0], timings[-1]
            return math.log(max(t1["cpu_s"], 1e-4) / max(t0["cpu_s"], 1e-4)) / math.log(t1["n"] / t0["n"]), True
        mx = sum(p[0] for p in pts) / len(pts)
        my = sum(p[1] for p in pts) / len(pts)
        den = sum((p[0] - mx) ** 2 for p in pts)
        return (sum((p[0] - mx) * (p[1] - my) for p in pts) / den if den else 0.0), True

    @staticmethod
    def _rank_from_slope(s, static_rank):
        if s < 0.35:
            return 0 if static_rank == 0 else 1
        if s < 1.35:
            return 3 if static_rank == 3 else 2
        if s < 2.4:
            return 4
        return 5

    @staticmethod
    def _static(ctx):
        depth = clexer.max_loop_depth(ctx.tokens)
        recursive = []
        for f in ctx.functions:
            body = [t for t in ctx.tokens if f.start_line <= t.line <= f.end_line]
            start = next((i for i, t in enumerate(body) if t.value == "{"), len(body))
            calls = clexer.called_names(body[start:])
            if f.name in calls:
                recursive.append((f.name, calls.count(f.name)))
        has_sort = any(t.value == "qsort" for t in ctx.tokens)
        rank = {0: 0, 1: 2, 2: 4}.get(depth, 5)
        why = f"max loop nesting {depth}"
        if has_sort:
            rank = max(rank, 3)
            why += ", uses qsort"
        for name, k in recursive:
            why += f", {name}() is recursive ({k} self-call{'s' if k > 1 else ''})"
            rank = max(rank, 6 if k > 1 else 2)
        return rank, why
