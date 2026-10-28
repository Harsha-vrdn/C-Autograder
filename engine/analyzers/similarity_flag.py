"""originality: abstract-structure similarity against peers, known sources and LLM-generated solutions.

Each program is reduced to an abstract representation (see clexer): identifiers ->
V, literals -> N/S, for/while/do -> LOOP, int/long -> INT, ``i++``/``i+=1`` -> INC/OPEQ.
Two views are compared:

* token k-gram fingerprints (Jaccard), with every k-gram that also appears in the
  instructor's base code / reference solution removed first (MOSS-style base-code
  exclusion), so shared boilerplate like ``scanf("%d %d", ...)`` does not count;
* the control-flow skeleton (loops/branches/returns/blocks/library calls), compared
  with a sequence-alignment ratio.

Renaming variables, reformatting, rewriting comments, or turning a ``for`` into a
``while`` therefore does not hide a copy.
"""
import difflib
import hashlib
import re
import threading
from pathlib import Path

from .. import clexer
from ..llm import LLMError, parse_json
from .base import Analyzer, Result, register

_GEN_PROMPT = """Write a complete, correct C11 program that solves the following problem. Read from stdin and
write to stdout exactly as specified.

Problem:
{statement}

Reply with JSON only: {{"lines": ["<line 1 of the C source>", "<line 2>", ...]}}"""
_GEN_LOCK = threading.Lock()


def fingerprint(src: str, k: int, with_len: bool = False):
    toks = clexer.tokenize(src)
    stream = clexer.similarity_stream(toks)
    fp = (clexer.kgrams(stream, k), clexer.control_skeleton(toks))
    return (*fp, len(stream)) if with_len else fp


@register
class SimilarityFlag(Analyzer):
    name = "similarity_flag"
    description = "Abstract-structure fingerprints vs. corpus, peers and qwen3-generated solutions."

    def run(self, ctx, params):
        k = int(params.get("k", 5))
        threshold = float(params.get("flag_threshold", 0.85))
        min_unique = int(params.get("min_unique_kgrams", 8))
        min_tokens = int(params.get("min_tokens", 50))
        w_tok = float(params.get("token_weight", 0.75))

        base = set()
        base_files = [ctx.resolve(p) for p in params.get("base_code", [])]
        if ctx.rules.get("reference_solution"):
            base_files.append(ctx.resolve(ctx.rules["reference_solution"]))
        for p in base_files:
            if p and p.exists():
                base |= fingerprint(p.read_text(errors="replace"), k)[0]

        mine_grams, mine_skel, mine_len = fingerprint(ctx.source, k, with_len=True)
        mine_unique = mine_grams - base
        corpus, llm_info = self._corpus(ctx, params)
        corpus_hash = hashlib.sha256("".join(sorted(f"{c['path']}:{c['sha']}" for c in corpus)).encode()).hexdigest()[:16]

        own = {ctx.source_path.resolve()}
        exact_dupes, matches = [], []
        for c in corpus:
            if Path(c["abs"]).resolve() in own:
                continue
            if c["sha"] == ctx.source_hash:
                exact_dupes.append(c["path"])  # byte-identical: same submission re-uploaded, or an exact copy
                continue
            grams, skel = fingerprint(c["text"], k)
            other = grams - base
            union = mine_unique | other
            jac = len(mine_unique & other) / len(union) if union else 0.0
            skel_ratio = difflib.SequenceMatcher(None, mine_skel, skel, autojunk=False).ratio() if mine_skel and skel else 0.0
            sim = w_tok * jac + (1 - w_tok) * skel_ratio
            matches.append({"path": c["path"], "origin": c["origin"], "similarity": round(sim, 3),
                            "token_jaccard": round(jac, 3), "skeleton_ratio": round(skel_ratio, 3)})
        matches.sort(key=lambda m: (-m["similarity"], m["path"]))

        flags = [{"code": "exact_duplicate", "message": f"Byte-identical to {p}", "match": p} for p in exact_dupes]
        # Very short programs (e.g. a 6-line a+b) are written identically by most people and by LLMs,
        # so a match carries no evidence of copying.
        judgeable = len(mine_unique) >= min_unique and mine_len >= min_tokens
        top = matches[0] if matches else None
        max_sim = top["similarity"] if (top and judgeable) else 0.0
        if not judgeable:
            score, fb = 1.0, (f"Program is too short to judge originality ({mine_len} abstract tokens, {len(mine_unique)} "
                              f"k-grams outside the base code; need {min_tokens}/{min_unique}). Top match "
                              f"{top['similarity'] if top else 0:.2f} is reported but not flagged.")
            verdict = "too little unique code to judge"
        elif max_sim >= threshold:
            score = max(0.0, 1 - (max_sim - threshold) / max(1e-9, 1 - threshold))
            kind = "suspected_llm_copy" if top["origin"] == "llm" else "suspected_copy"
            flags.append({"code": kind, "message": f"Similarity {max_sim:.2f} with {top['path']} (threshold {threshold})",
                          "match": top["path"], "similarity": max_sim})
            fb = (f"Structure is {max_sim:.0%} similar to {top['path']}"
                  + (" (an LLM-generated solution)" if top["origin"] == "llm" else "") + ". Flagged for manual review.")
            verdict = f"FLAGGED: {max_sim:.2f} vs {Path(top['path']).name}"
        else:
            score = 1.0
            fb = f"Max structural similarity {max_sim:.2f} ({top['path'] if top else 'empty corpus'}); below {threshold}."
            verdict = f"max similarity {max_sim:.2f}, not flagged"
        return Result(score, fb, {"max_similarity": max_sim, "threshold": threshold, "judgeable": judgeable,
                                  "unique_kgrams": len(mine_unique), "abstract_tokens": mine_len, "base_kgrams_excluded": len(mine_grams & base),
                                  "top_matches": matches[:5], "corpus_size": len(corpus), "corpus_hash": corpus_hash,
                                  "llm_reference": llm_info},
                      flags=flags, verdict=verdict)

    # ----------------------------------------------------------------- corpus
    def _corpus(self, ctx, params):
        files: dict[Path, str] = {}
        for d in [params.get("corpus_dir")] + list(params.get("extra_corpus", [])):
            p = ctx.resolve(d)
            if p and p.is_dir():
                for f in sorted(p.rglob("*.c")):
                    files[f.resolve()] = "peer"
        with _GEN_LOCK:
            llm_dir, llm_info = self._llm_solutions(ctx, int(params.get("llm_reference_solutions", 3)))
        if llm_dir:
            for f in sorted(llm_dir.glob("*.c")):
                files[f.resolve()] = "llm"
        out = []
        for f, origin in sorted(files.items()):
            text = f.read_text(errors="replace")
            try:
                rel = str(f.relative_to(ctx.base_dir.resolve()))
            except ValueError:
                rel = str(f)
            out.append({"path": rel, "abs": str(f), "origin": origin, "text": text,
                        "sha": hashlib.sha256(text.encode()).hexdigest()})
        return out, llm_info

    def _llm_solutions(self, ctx, n):
        if n <= 0:
            return None, {"used": False}
        problem = re.sub(r"[^\w-]", "_", ctx.rules.get("problem", "problem"))
        d = ctx.base_dir / "corpus" / "llm_generated" / problem
        existing = sorted(d.glob("*.c")) if d.exists() else []
        if len(existing) >= n or ctx.offline or not ctx.statement or not ctx.ollama.available:
            return (d if existing else None), {"used": bool(existing), "files": len(existing),
                                               "generated_now": 0, "model": ctx.ollama.model}
        d.mkdir(parents=True, exist_ok=True)
        made = 0
        for seed in range(1, n + 1):
            target = d / f"{ctx.ollama.model.replace(':', '_')}_seed{seed}.c"
            if target.exists():
                continue
            try:
                text, _ = ctx.ollama.chat([{"role": "user", "content": _GEN_PROMPT.format(statement=ctx.statement)}],
                                          temperature=0.7, seed=seed, json_mode=True, max_tokens=1200)
                lines = parse_json(text).get("lines", [])
            except (LLMError, ValueError, AttributeError) as exc:
                ctx.notes.append(f"LLM reference generation failed: {exc}")
                continue
            code = "\n".join(str(ln) for ln in lines).strip()
            if "main" in code:
                target.write_text(code + "\n")
                made += 1
        return d, {"used": True, "files": len(list(d.glob('*.c'))), "generated_now": made, "model": ctx.ollama.model}
