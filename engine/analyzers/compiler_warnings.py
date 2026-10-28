"""syntax: compile with strict flags and count diagnostics."""
import shlex

from .. import sandbox
from .base import Analyzer, Result, register


@register
class CompilerWarnings(Analyzer):
    name = "compiler_warnings"
    description = "Compiles with the rule's flags and deducts per distinct warning."

    def run(self, ctx, params):
        flags = params.get("flags", "-Wall -Wextra -std=c11")
        penalty = float(params.get("penalty_per_warning", 0.1))
        werror = "-Werror" in shlex.split(flags)
        # Count warnings without -Werror, so they are reported as warnings, not as one error.
        count_flags = " ".join(f for f in shlex.split(flags) if f != "-Werror")
        res = sandbox.compile_c(str(ctx.work_source), str(ctx.workdir / "warn_check"), count_flags + " -fsyntax-only")
        if res.errors or res.returncode != 0:
            errs = res.errors or [{"line": 0, "message": res.stderr.strip()[:300], "flag": None}]
            return Result(0, f"Compilation failed with {len(errs)} error(s): line {errs[0]['line']}: {errs[0]['message']}",
                          {"errors": errs, "warnings": res.warnings, "flags": flags},
                          suggestions=["Fix the compiler errors first; nothing else can be graded until it builds."],
                          verdict=f"{len(errs)} compile error(s)")
        uniq = sorted({(w["line"], w["flag"] or "", w["message"]) for w in res.warnings})
        n = len(uniq)
        flags_seen = sorted({w[1] for w in uniq if w[1]})
        fb = "Compiles cleanly." if n == 0 else (
            f"{n} warning(s): " + "; ".join(f"line {l}: {m}" for l, _, m in uniq[:4]))
        if n and werror:
            fb += " Under -Werror this would fail to build."
        sugg = [f"Line {l}: {m}" + (f" [{f}]" if f else "") for l, f, m in uniq[:5]]
        return Result(max(0.0, 1 - penalty * n), fb,
                      {"warnings": [{"line": l, "flag": f, "message": m} for l, f, m in uniq],
                       "flags": flags, "would_fail_werror": bool(n and werror)},
                      suggestions=sugg, verdict="clean" if n == 0 else f"{n} warning(s) " + " ".join(flags_seen))
