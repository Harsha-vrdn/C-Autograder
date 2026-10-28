"""modularity: is the work decomposed into functions?"""
from .base import Analyzer, Result, register


@register
class FunctionSplit(Analyzer):
    name = "function_split"
    description = "Counts helper functions and how much of the code lives in main()."

    def run(self, ctx, params):
        min_funcs = max(1, int(params.get("min_functions", 2)))
        expect_helpers = bool(params.get("expect_helpers", True))
        max_main_share = float(params.get("max_main_share", 0.75))
        funcs = ctx.functions
        names = [f.name for f in funcs]
        helpers = [f for f in funcs if f.name != "main"]
        main = next((f for f in funcs if f.name == "main"), None)
        total_nloc = sum(f.nloc for f in funcs) or 1
        main_share = (main.nloc / total_nloc) if main else 0
        unused = [h.name for h in helpers if not self._is_called(ctx, h.name)]

        if len(funcs) >= min_funcs:
            score = 1.0
        else:
            needed_helpers = max(1, min_funcs - 1)
            score = 0.3 + 0.7 * min(1, len(helpers) / needed_helpers)
        if expect_helpers and not helpers:
            score = min(score, 0.3)
        problems, sugg = [], []
        if expect_helpers and not helpers:
            problems.append("only main() is defined")
            sugg.append("Move the core computation into its own function (e.g. a helper that takes the inputs and "
                        "returns the result) and call it from main().")
        if helpers and main_share > max_main_share and len(funcs) > 1:
            score -= 0.2
            problems.append(f"main() still holds {main_share:.0%} of the code")
            sugg.append("main() should mostly read input, call helpers and print results.")
        if unused:
            score -= 0.1
            problems.append(f"unused function(s): {', '.join(unused)}")
        verdict = "; ".join(problems) or f"{len(funcs)} functions, main() {main_share:.0%} of code"
        fb = (f"{len(funcs)} function(s) defined ({', '.join(names) or 'none'}); expected at least {min_funcs}. "
              + ("; ".join(problems).capitalize() + "." if problems else "Good decomposition."))
        return Result(score, fb, {"functions_defined": len(funcs), "expected_min": min_funcs, "functions": names,
                                  "main_share": round(main_share, 3), "unused": unused},
                      suggestions=sugg, verdict=verdict)

    @staticmethod
    def _is_called(ctx, name):
        toks = ctx.tokens
        return any(t.value == name and i + 1 < len(toks) and toks[i + 1].value == "(" and
                   not (i > 0 and toks[i - 1].kind == "id" and toks[i - 1].value not in ("return",))
                   for i, t in enumerate(toks))
