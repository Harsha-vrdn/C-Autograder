"""structure: cyclomatic complexity, function length and source hygiene (lizard + own checks, cppcheck if installed)."""
import re
import shutil
import subprocess

from .. import clexer
from .base import Analyzer, Result, register


@register
class StaticAnalysis(Analyzer):
    name = "static_analysis"
    description = "lizard cyclomatic complexity / function length plus structural hygiene checks."

    def run(self, ctx, params):
        max_cc = int(params.get("max_cyclomatic", 10))
        max_len = int(params.get("max_function_length", 50))
        max_line = int(params.get("max_line_length", 100))
        deductions: list[tuple[float, str]] = []
        funcs = [{"name": f.name, "cyclomatic": f.cyclomatic_complexity, "nloc": f.nloc,
                  "length": f.length, "start_line": f.start_line, "params": f.parameter_count}
                 for f in ctx.functions]
        for f in funcs:
            if f["cyclomatic"] > max_cc:
                deductions.append((0.2, f"{f['name']}() has cyclomatic complexity {f['cyclomatic']} (> {max_cc}); split the branching logic."))
            if f["length"] > max_len:
                deductions.append((0.2, f"{f['name']}() is {f['length']} lines long (> {max_len}); extract helpers."))

        cmts = clexer.comments(ctx.source)
        first_code = next((t.line for t in ctx.tokens), 1)
        if not cmts:
            deductions.append((0.2, "No comments at all: document what the program and each function does."))
        elif not any(c.line < first_code or c.line <= 3 for c in cmts):
            deductions.append((0.1, "No header comment describing the file/program at the top."))
        if funcs and len(funcs) == 1 and len(ctx.lines) > 1:
            deductions.append((0.1, "All logic is in a single block (main only)."))

        globals_ = self._mutable_globals(ctx.tokens)
        if globals_:
            deductions.append((min(0.2, 0.1 * len(globals_)), f"Mutable global variable(s): {', '.join(globals_[:4])}; pass values as parameters."))
        if any(t.value == "goto" for t in ctx.tokens):
            deductions.append((0.1, "Uses goto; prefer structured control flow."))
        long_lines = [i + 1 for i, ln in enumerate(ctx.lines) if len(ln) > max_line]
        if long_lines:
            deductions.append((0.05, f"{len(long_lines)} line(s) longer than {max_line} characters (e.g. line {long_lines[0]})."))
        magic = self._magic_numbers(ctx.tokens)
        if len(magic) >= 3:
            deductions.append((0.05, f"Magic numbers {', '.join(sorted(set(magic))[:5])}: name them with #define or const."))

        cpp = self._cppcheck(ctx)
        for issue in cpp[:4]:
            deductions.append((0.05, f"cppcheck: line {issue['line']}: {issue['message']}"))

        score = 1 - sum(d for d, _ in deductions)
        issues = [m for _, m in deductions]
        verdict = "well structured" if not issues else "; ".join(self._short(m) for m in issues[:2])
        return Result(score, "No structural issues." if not issues else " ".join(issues),
                      {"functions": funcs, "max_cyclomatic": max_cc, "max_function_length": max_len,
                       "comment_count": len(cmts), "globals": globals_, "cppcheck": cpp,
                       "deductions": [{"points": d, "reason": m} for d, m in deductions]},
                      suggestions=issues[:4], verdict=verdict)

    @staticmethod
    def _short(msg: str) -> str:
        return msg.split(":")[0].split(";")[0][:50]

    @staticmethod
    def _mutable_globals(tokens):
        """Identifiers declared at file scope (brace depth 0) that are not functions, typedefs or consts."""
        out, depth, stmt = [], 0, []
        for t in tokens:
            if t.value == "{":
                if depth == 0:
                    stmt = []
                depth += 1
            elif t.value == "}":
                depth -= 1
                if depth == 0:
                    stmt = []  # end of function or struct body; the trailing ';' is handled below
            elif depth == 0:
                if t.value == ";":
                    vals = [x.value for x in stmt]
                    if vals and "(" not in vals and not {"typedef", "const", "extern", "struct", "enum", "union"} & set(vals[:2]):
                        names = [x.value for i, x in enumerate(stmt) if x.kind == "id" and x.value not in clexer.KEYWORDS
                                 and (i + 1 == len(stmt) or stmt[i + 1].value in ("=", ",", "["))]
                        out.extend(names)
                    stmt = []
                else:
                    stmt.append(t)
        return out

    @staticmethod
    def _magic_numbers(tokens):
        allowed = {"0", "1", "2", "-1", "10", "100"}
        out = []
        for i, t in enumerate(tokens):
            if t.kind == "num" and t.value not in allowed:
                prev = tokens[i - 1].value if i else ""
                if prev not in ("[",):  # array sizes are usually fine
                    out.append(t.value)
        return out

    @staticmethod
    def _cppcheck(ctx):
        exe = shutil.which("cppcheck")
        if not exe:
            return []
        try:
            p = subprocess.run([exe, "--enable=warning,style,performance,portability", "--quiet",
                                "--template={line}|{severity}|{message}", str(ctx.work_source)],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return []
        issues = []
        for ln in p.stderr.splitlines():
            m = re.match(r"(\d+)\|(\w+)\|(.*)", ln)
            if m:
                issues.append({"line": int(m[1]), "severity": m[2], "message": m[3]})
        return sorted(issues, key=lambda i: (i["line"], i["message"]))
