"""A small C lexer plus the abstract representations built on top of it.

Three views of a program are derived here:

* the raw token stream (used by static checks),
* an *abstract token stream* where identifiers, literals and equivalent
  constructs are collapsed (``for``/``while``/``do`` -> ``LOOP``, ``int``/``long``
  -> ``INT`` ...).  Renaming variables or swapping loop kinds does not change it,
  which is what makes it useful for copy detection;
* a *control skeleton*: only the control-flow shape (loops, branches, returns,
  block structure and library calls).

Everything here is pure and deterministic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

KEYWORDS = {
    "auto", "break", "case", "char", "const", "continue", "default", "do", "double",
    "else", "enum", "extern", "float", "for", "goto", "if", "inline", "int", "long",
    "register", "restrict", "return", "short", "signed", "sizeof", "static", "struct",
    "switch", "typedef", "union", "unsigned", "void", "volatile", "while", "_Bool",
    "bool", "size_t",
}

# Library calls carry meaning, so they survive abstraction.
LIBCALLS = {
    "printf", "scanf", "fprintf", "fscanf", "sscanf", "sprintf", "snprintf", "puts",
    "putchar", "getchar", "gets", "fgets", "fputs", "getline", "read", "write",
    "malloc", "calloc", "realloc", "free", "memcpy", "memset", "memmove", "strlen",
    "strcpy", "strncpy", "strcmp", "strncmp", "strcat", "strchr", "strstr", "qsort",
    "bsearch", "abs", "labs", "llabs", "sqrt", "pow", "fabs", "floor", "ceil", "exit",
    "atoi", "atol", "strtol", "strtoll", "isdigit", "isalpha", "isspace", "toupper",
    "tolower",
}

INPUT_CALLS = {"scanf", "fscanf", "getchar", "gets", "fgets", "getline", "read", "sscanf", "getc", "fgetc", "fread"}
OUTPUT_CALLS = {"printf", "puts", "putchar", "fprintf", "fputs", "write", "fwrite", "putc", "fputc"}
DANGEROUS_CALLS = {"system", "fork", "vfork", "execl", "execlp", "execle", "execv", "execvp",
                   "execve", "popen", "socket", "connect", "kill", "unlink", "remove",
                   "rmdir", "rename", "ptrace", "dlopen"}

_INT_TYPES = {"int", "long", "short", "unsigned", "signed", "size_t", "_Bool", "bool"}
_FLT_TYPES = {"float", "double"}
_LOOPS = {"for", "while", "do"}
_COMPOUND = {"+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}

_PATTERNS = [
    ("ws", r"\s+"),
    ("comment", r"//[^\n]*|/\*.*?\*/"),
    ("pp", r"\#[^\n]*(?:\\\n[^\n]*)*"),
    ("str", r'"(?:\\.|[^"\\\n])*"'),
    ("chr", r"'(?:\\.|[^'\\\n])*'"),
    ("num", r"(?:0[xX][0-9a-fA-F]+|\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)[uUlLfF]*"),
    ("id", r"[A-Za-z_]\w*"),
    ("op", r"->|\+\+|--|<<=|>>=|<=|>=|==|!=|&&|\|\||\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<|>>|\.\.\.|[{}()\[\];,.<>+\-*/%&|^!~?:=]"),
    ("other", r"."),
]
_TOKEN_RE = re.compile("|".join(f"(?P<{k}>{p})" for k, p in _PATTERNS), re.S)


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    line: int


def tokenize(src: str, keep_trivia: bool = False) -> list[Token]:
    tokens, line = [], 1
    for m in _TOKEN_RE.finditer(src):
        kind, value = m.lastgroup, m.group()
        if keep_trivia or kind not in ("ws", "comment", "pp"):
            tokens.append(Token(kind, value, line))
        line += value.count("\n")
    return tokens


def comments(src: str) -> list[Token]:
    return [t for t in tokenize(src, keep_trivia=True) if t.kind == "comment"]


def abstract_tokens(tokens: list[Token]) -> list[str]:
    """Collapse a token stream into a renaming-invariant abstract stream."""
    out = []
    for t in tokens:
        if t.kind == "str":
            out.append("S")
        elif t.kind == "chr":
            out.append("C")
        elif t.kind == "num":
            out.append("N")
        elif t.kind == "id":
            v = t.value
            if v in _INT_TYPES:
                # "long long", "unsigned int" -> a single INT
                if not out or out[-1] != "INT":
                    out.append("INT")
            elif v in _FLT_TYPES:
                out.append("FLT")
            elif v in _LOOPS:
                out.append("LOOP")
            elif v in KEYWORDS or v in LIBCALLS:
                out.append(v)
            else:
                out.append("V")
        elif t.kind == "op":
            v = t.value
            if v in ("++", "--"):
                out.append("INC")
            elif v in _COMPOUND:
                out.append("OPEQ")
            else:
                out.append(v)
    return out


_SKELETON_KEEP = {"LOOP", "if", "else", "switch", "case", "return", "break", "continue",
                  "goto"} | LIBCALLS


def _match(tokens: list[Token], i: int, open_: str, close: str) -> int:
    """Index of the bracket closing tokens[i] (or len(tokens)-1)."""
    depth = 0
    for j in range(i, len(tokens)):
        if tokens[j].value == open_:
            depth += 1
        elif tokens[j].value == close:
            depth -= 1
            if depth == 0:
                return j
    return len(tokens) - 1


def _statement_end(tokens: list[Token], i: int) -> int:
    """Index of the last token of the statement starting at i."""
    if i < len(tokens) and tokens[i].value == "{":
        return _match(tokens, i, "{", "}")
    depth = 0
    for j in range(i, len(tokens)):
        v = tokens[j].value
        if v in "([":
            depth += 1
        elif v in ")]":
            depth -= 1
        elif v == "{" and depth == 0:
            return _match(tokens, j, "{", "}")
        elif v == ";" and depth == 0:
            return j
    return len(tokens) - 1


def canonicalize(tokens: list[Token]) -> list[Token]:
    """Rewrite ``for (A; B; C) S`` as ``A; while (B) { S C; }`` so loop style does not matter."""
    out, i, n = [], 0, len(tokens)
    while i < n:
        t = tokens[i]
        if t.value == "for" and i + 1 < n and tokens[i + 1].value == "(":
            close = _match(tokens, i + 1, "(", ")")
            header = tokens[i + 2:close]
            parts, cur, depth = [], [], 0
            for h in header:
                if h.value in "([":
                    depth += 1
                elif h.value in ")]":
                    depth -= 1
                if h.value == ";" and depth == 0 and len(parts) < 2:
                    parts.append(cur)
                    cur = []
                else:
                    cur.append(h)
            parts.append(cur)
            if len(parts) == 3:
                init, cond, step = parts
                end = _statement_end(tokens, close + 1)
                body = canonicalize(tokens[close + 1:end + 1])
                mk = lambda v: Token("op" if not v.isalpha() else "id", v, t.line)  # noqa: E731
                out += init + [mk(";"), mk("while"), mk("(")] + cond + [mk(")"), mk("{")] + body
                out += step + ([mk(";")] if step else []) + [mk("}")]
                i = end + 1
                continue
        out.append(t)
        i += 1
    return out


def similarity_stream(tokens: list[Token]) -> list[str]:
    """Abstract stream used for copy detection: loop-style and brace-style insensitive."""
    return [a for a in abstract_tokens(canonicalize(tokens)) if a not in ("{", "}")]


def control_skeleton(tokens: list[Token]) -> list[str]:
    return [a for a in abstract_tokens(canonicalize(tokens)) if a in _SKELETON_KEEP]


def kgrams(seq: list[str], k: int = 5) -> set[tuple[str, ...]]:
    if len(seq) < k:
        return {tuple(seq)} if seq else set()
    return {tuple(seq[i:i + k]) for i in range(len(seq) - k + 1)}


def called_names(tokens: list[Token]) -> list[str]:
    return [t.value for i, t in enumerate(tokens[:-1])
            if t.kind == "id" and tokens[i + 1].value == "(" and t.value not in KEYWORDS]


def max_loop_depth(tokens: list[Token]) -> int:
    """Approximate maximum loop nesting depth (handles braced and single-statement bodies)."""
    stack: list[tuple[bool, int]] = []  # (braced, brace_level_at_push)
    brace = 0
    best = 0
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        v = t.value
        if t.kind == "id" and v in _LOOPS:
            j = i + 1
            if v != "do" and j < n and tokens[j].value == "(":
                depth = 0
                while j < n:
                    if tokens[j].value == "(":
                        depth += 1
                    elif tokens[j].value == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                j += 1
            # `while (...);` closing a do-while is not a new loop
            if v == "while" and j < n and tokens[j].value == ";" and i > 0 and tokens[i - 1].value == "}":
                i = j + 1
                continue
            braced = j < n and tokens[j].value == "{"
            stack.append((braced, brace + (1 if braced else 0)))
            best = max(best, len(stack))
            if braced:
                brace += 1
                i = j + 1
            else:
                i = j
            continue
        if v == "{":
            brace += 1
        elif v == "}":
            brace -= 1
            while stack and stack[-1][0] and stack[-1][1] > brace:
                stack.pop()
            while stack and not stack[-1][0] and stack[-1][1] == brace and (i + 1 >= n or tokens[i + 1].value != "else"):
                stack.pop()
        elif v == ";":
            while stack and not stack[-1][0] and stack[-1][1] == brace:
                stack.pop()
        i += 1
    return best
