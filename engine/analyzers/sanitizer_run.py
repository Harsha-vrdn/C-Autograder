"""memory_safety: ASan + UBSan runs, plus leak detection (LeakSanitizer on Linux, `leaks` on macOS, valgrind if present)."""
import re
import shutil

from .. import sandbox, sanitizers
from ..context import env_for_sanitizers
from .base import Analyzer, Result, register

_LEAKS_SUM = re.compile(r"(\d+) leaks? for (\d+) total leaked bytes")
_LEAKS_LOC = re.compile(r"main\.c:(\d+)")
_VG_LOST = re.compile(r"definitely lost: ([\d,]+) bytes in ([\d,]+) blocks")


@register
class SanitizerRun(Analyzer):
    name = "sanitizer_run"
    description = "Runs the program under AddressSanitizer/UBSan and a leak checker."
    needs_binary = True

    def run(self, ctx, params):
        flags = params.get("flags", "-fsanitize=address,undefined")
        penalty = float(params.get("penalty_per_issue", 0.25))
        which = params.get("inputs", "tests")
        works, why = sanitizers.self_test(flags)
        if not works:
            leak = self._leak_check(ctx, [(c["name"], c["input"]) for c in ctx.test_cases][:2] or [("sample", ctx.sample[0])]) \
                if sandbox.IS_MAC or shutil.which("valgrind") else {"tool": "none available"}
            return Result(0, why, {"self_test": why, "leak_check": leak, "flags": flags}, unavailable=True,
                          verdict="unavailable on this machine")
        comp = sandbox.compile_c(str(ctx.work_source), str(ctx.workdir / "asan_bin"),
                                 f"-std=c11 -w -O1 -g -fno-omit-frame-pointer {flags}")
        if not comp.ok:
            return Result(0, "Could not build with sanitizers.", {"stderr": comp.stderr[:500]}, verdict="build failed")
        inputs = [(c["name"], c["input"]) for c in ctx.test_cases] or [("sample", ctx.sample[0])]
        if "edge" in which:
            inputs += [(f"edge:{cat}", s) for cat, s in ctx.edge_inputs(params.get("edge_cases", ["zero", "negatives", "int_overflow"]))]
        leak_check = bool(params.get("leak_check", True))
        # Linux: LeakSanitizer runs inside the ASan build (unless valgrind is installed, which is used instead)
        use_lsan = leak_check and not sandbox.IS_MAC and not shutil.which("valgrind")
        issues: dict = {}
        lsan = {"tool": "LeakSanitizer", "leaked_bytes": 0}
        timeouts = 0
        for name, stdin in inputs:
            if timeouts >= 2:
                break  # a hanging program would make every sanitizer run hit the limit
            r = ctx.run(comp.binary, stdin, time_limit=ctx.time_limit * 3, memory_limit=None,
                        env=env_for_sanitizers(leaks=use_lsan))
            timeouts += r.timed_out
            if use_lsan and sanitizers.leak_checker_failed(r.stderr):
                lsan = {"tool": "LeakSanitizer unavailable (needs ptrace; run the container with --cap-add=SYS_PTRACE)"}
            for f in sanitizers.parse(r.stderr):
                if f["kind"] == "memory-leak":
                    if not lsan.get("leaked_bytes"):
                        lsan = {"tool": "LeakSanitizer", "leaked_bytes": f["leaked_bytes"], "leaks": f["leaks"],
                                "line": f["line"], "input": name}
                    continue
                key = (f["kind"], f["line"], f["detail"].split(":")[0])
                issues.setdefault(key, {**f, "input": name})

        if not leak_check:
            leak = {"tool": "disabled"}
        elif use_lsan:
            leak = lsan
        else:
            leak = self._leak_check(ctx, inputs[:2])
        if leak.get("leaked_bytes"):
            issues.setdefault(("memory-leak", leak.get("line"), ""), {
                "kind": "memory-leak", "line": leak.get("line"),
                "detail": f"{leak['leaked_bytes']} bytes leaked in {leak['leaks']} allocation(s) ({leak['tool']})",
                "input": leak.get("input")})

        found = sorted(issues.values(), key=lambda i: (i["kind"], i["line"] or 0))
        score = max(0.0, 1 - penalty * len(found))
        if not found:
            fb, verdict = f"No ASan/UBSan diagnostics on {len(inputs)} input(s); no leaks ({leak['tool']}).", "ASan/UBSan clean"
        else:
            fb = "; ".join(f"{i['kind']} at line {i['line']}: {i['detail']}" for i in found[:4])
            verdict = ", ".join(sorted({i["kind"] for i in found}))
        sugg = []
        kinds = {i["kind"] for i in found}
        if "memory-leak" in kinds:
            sugg.append("Every malloc/calloc needs a matching free() on all paths (including early returns).")
        if kinds & {"heap-buffer-overflow", "stack-buffer-overflow", "global-buffer-overflow"}:
            sugg.append("An array is indexed out of bounds; check loop limits and allocation sizes.")
        if "undefined-behaviour" in kinds:
            sugg.append("Undefined behaviour detected (e.g. signed overflow or invalid shift); see the reported lines.")
        if kinds & {"heap-use-after-free", "double-free"}:
            sugg.append("Memory is used or freed after it was already freed; set pointers to NULL after free().")
        return Result(score, fb, {"inputs_checked": len(inputs), "issues": found, "leak_check": leak, "flags": flags},
                      suggestions=sugg, verdict=verdict)

    @staticmethod
    def _leak_check(ctx, inputs):
        """valgrind (Linux, when installed) or macOS `leaks` on the -O0 -g build."""
        dbg = ctx.binary("debug")
        if not sandbox.IS_MAC:
            vg = shutil.which("valgrind")
            if not vg or not dbg.ok:
                return {"tool": "none available"}
            for name, stdin in inputs:
                r = sandbox.run([vg, "--leak-check=full", "--error-exitcode=0", dbg.binary], stdin,
                                max(30, ctx.time_limit * 20), None, cwd=str(ctx.workdir))
                m = _VG_LOST.search(r.stderr)
                if m and int(m[1].replace(",", "")) > 0:
                    loc = _LEAKS_LOC.search(r.stderr[m.start():]) or _LEAKS_LOC.search(r.stderr)
                    return {"tool": "valgrind", "leaked_bytes": int(m[1].replace(",", "")),
                            "leaks": int(m[2].replace(",", "")), "line": int(loc[1]) if loc else None, "input": name}
            return {"tool": "valgrind", "leaked_bytes": 0}
        exe = shutil.which("leaks")
        if not exe or not dbg.ok:
            return {"tool": "none available"}
        for name, stdin in inputs:
            r = sandbox.run([exe, "--atExit", "--", dbg.binary], stdin, max(15, ctx.time_limit * 10), None,
                            cwd=str(ctx.workdir), env={"MallocStackLogging": "1"})
            m = _LEAKS_SUM.search(r.stdout)
            if m and int(m[1]) > 0:
                loc = _LEAKS_LOC.search(r.stdout)
                return {"tool": "macOS leaks", "leaked_bytes": int(m[2]), "leaks": int(m[1]),
                        "line": int(loc[1]) if loc else None, "input": name}
        return {"tool": "macOS leaks", "leaked_bytes": 0}
