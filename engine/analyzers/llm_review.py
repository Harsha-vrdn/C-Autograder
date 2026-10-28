"""readability: LLM rubric review through Groq (temperature 0, fixed seed, cached)."""
import re

from .. import clexer
from ..llm import GroqClient, LLMError, parse_json
from .base import Analyzer, Result, register

_SYSTEM = ("You are a strict, consistent teaching assistant grading the READABILITY of C code only: naming, "
           "comments, formatting/indentation, whitespace, consistency and clarity. Ignore correctness, "
           "efficiency and modularity; other graders cover those. Always answer with a single JSON object.")

_RUBRIC = """Score readability from 0 to 10 using this rubric:
10 = descriptive names, helpful comments, consistent indentation and spacing, immediately clear.
7-8 = mostly clear; a few terse names or missing comments.
5-6 = works but cramped: single-letter names, no comments, inconsistent spacing.
3-4 = hard to follow: confusing names, dense lines, misleading or no structure.
0-2 = obfuscated or unreadable.

Code (line numbers added for reference, they are not part of the file):
```c
{code}
```

Reply with JSON: {{"score": <integer 0-10>, "reasons": ["<short reason>", ...], "suggestions": ["<concrete fix, cite line numbers>", ...]}}"""


@register
class LLMReview(Analyzer):
    name = "llm_review"
    description = "Groq-hosted LLM grades readability with a fixed rubric (T=0, seeded, cached)."

    def run(self, ctx, params):
        model = params.get("model", "openai/gpt-oss-120b")
        temperature = float(params.get("temperature", 0))
        seed = int(params.get("seed", 42))
        groq = ctx.groq if ctx.groq.model == model else GroqClient(ctx.groq.cache, model)
        numbered = "\n".join(f"{i + 1:3d} | {ln}" for i, ln in enumerate(ctx.source.splitlines()[:400]))
        messages = [{"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": _RUBRIC.format(code=numbered)}]
        err = None
        if not ctx.offline:
            try:
                text, cached = groq.chat(messages, temperature=temperature, seed=seed, json_mode=True, max_tokens=2000)
                data = parse_json(text)
                raw = float(data.get("score", 0))
                reasons = [str(r) for r in data.get("reasons", [])][:5]
                sugg = [str(s) for s in data.get("suggestions", [])][:4]
                fb = f"Readability {raw:g}/10" + (": " + "; ".join(r.rstrip(".") for r in reasons) if reasons else "") + "."
                return Result(raw / 10, fb,
                              {"provider": "groq", "model": model, "temperature": temperature, "seed": seed,
                               "cached": cached, "raw_response": text[:2000]},
                              suggestions=sugg, verdict=", ".join(reasons[:2])[:60] or f"{raw:g}/10")
            except (LLMError, ValueError, KeyError, TypeError) as exc:
                err = str(exc)[:300]
        score, why, sugg = self._heuristic(ctx)
        return Result(score, "Groq unavailable; heuristic readability estimate: " + "; ".join(why),
                      {"provider": "heuristic-fallback", "error": err or "offline mode", "model": model},
                      suggestions=sugg, verdict="(fallback) " + ", ".join(why[:2])[:50])

    @staticmethod
    def _heuristic(ctx):
        """Deterministic fallback: names, comments, spacing, line density."""
        names = {t.value for t in ctx.tokens if t.kind == "id" and t.value not in clexer.KEYWORDS
                 and t.value not in clexer.LIBCALLS and t.value != "main"}
        short = [n for n in names if len(n) <= 1 and n not in ("i", "j", "k", "n")]
        code_lines = [ln for ln in ctx.lines if ln.strip()]
        n_comments = len(clexer.comments(ctx.source))
        cramped = sum(1 for ln in code_lines if re.search(r"[,;](?=\S)|\w[=+\-*/<>]=?\w", ln.split("//")[0]))
        score, why, sugg = 10.0, [], []
        if short:
            names_ = ", ".join(sorted(short)[:5])
            score -= min(3, len(short)); why.append(f"single-letter names: {names_}")
            sugg.append(f"Rename {names_} to names that say what they hold (e.g. first, second, total).")
        if n_comments == 0:
            score -= 2.5; why.append("no comments")
        elif n_comments / max(1, len(code_lines)) < 0.05:
            score -= 1; why.append("very few comments")
        if code_lines and cramped / len(code_lines) > 0.25:
            score -= 1.5; why.append("cramped spacing around operators/commas")
            sugg.append("Put spaces after commas and around operators: scanf(\"%d %d\", &a, &b);")
        indents = {len(ln) - len(ln.lstrip()) for ln in code_lines
                   if ln.startswith((" ", "\t")) and not ln.lstrip().startswith(("*", "//", "/*"))}
        if len({i % 4 for i in indents}) > 1 and len({i % 2 for i in indents}) > 1:
            score -= 1; why.append("inconsistent indentation")
        return max(0, score) / 10, why or ["clear naming and layout"], sugg
