"""Parse AddressSanitizer / LeakSanitizer / UBSan reports (clang on macOS, gcc or clang on Linux).

The source line is taken from the first stack frame inside the submission (``main.c:LINE``), not
from the SUMMARY line: with gcc the SUMMARY often points into the sanitizer runtime
(e.g. the scanf interceptor) rather than into the student's code.
"""
from __future__ import annotations

import functools
import os
import re
import tempfile

_UB = re.compile(r"main\.c:(\d+):\d+: runtime error: (.*)")
_ERROR = re.compile(r"ERROR: (AddressSanitizer|LeakSanitizer): ([\w-]+)")
_FRAME = re.compile(r"#\d+ 0x[0-9a-f]+ in \S+ \S*?main\.c:(\d+)")
_SUMMARY_LOC = re.compile(r"SUMMARY: AddressSanitizer: [\w-]+ \S*?main\.c:(\d+)")
_LEAK_BLOCK = re.compile(r"(Direct|Indirect) leak of (\d+) byte\(s\) in (\d+) object\(s\)")
_LSAN_FATAL = "LeakSanitizer has encountered a fatal error"


def parse(stderr: str) -> list[dict]:
    """Return findings ``{"kind", "line", "detail"}``; kinds match the macOS ``leaks`` path (``memory-leak``)."""
    found = []
    for m in _UB.finditer(stderr):
        found.append({"kind": "undefined-behaviour", "line": int(m[1]), "detail": m[2]})
    m = _ERROR.search(stderr)
    if m and m[1] == "AddressSanitizer":
        frame = _FRAME.search(stderr, m.end()) or _SUMMARY_LOC.search(stderr)
        found.append({"kind": m[2], "line": int(frame[1]) if frame else None,
                      "detail": f"AddressSanitizer: {m[2]}"})
    if m and m[1] == "LeakSanitizer":
        leaks = list(_LEAK_BLOCK.finditer(stderr))
        total = sum(int(b[2]) for b in leaks if b[1] == "Direct") or sum(int(b[2]) for b in leaks)
        objects = sum(int(b[3]) for b in leaks if b[1] == "Direct") or sum(int(b[3]) for b in leaks)
        frame = _FRAME.search(stderr, leaks[0].end()) if leaks else None
        found.append({"kind": "memory-leak", "line": int(frame[1]) if frame else None,
                      "detail": f"{total} bytes leaked in {objects} allocation(s) (LeakSanitizer)",
                      "leaked_bytes": total, "leaks": objects})
    return found


def leak_checker_failed(stderr: str) -> bool:
    """LSan needs ptrace; it aborts in containers started without CAP_SYS_PTRACE."""
    return _LSAN_FATAL in stderr


def describe(finding: dict) -> str:
    where = f" at line {finding['line']}" if finding.get("line") else ""
    if finding["kind"] == "undefined-behaviour":
        return f"runtime error{where}: {finding['detail']}"
    return f"{finding['detail']}{where}"


_PROBES = {
    "address": ("#include <stdlib.h>\nint main(void) { char *p = malloc(8); p[8] = 1; free(p); return 0; }\n",
                "heap-buffer-overflow"),
    "undefined": ("int main(int argc, char **argv) { (void)argv; volatile int x = 2147483647; x = x + argc;"
                  " return x == 0; }\n", "runtime error"),
}


@functools.lru_cache(maxsize=8)
def self_test(flags: str) -> tuple[bool, str]:
    """Check that the sanitizers in ``flags`` actually work on this machine.

    ASan can be installed yet unusable: under CPU emulation its shadow-memory reservation gets the
    process killed, and on some kernels (high mmap randomisation) older ASan runtimes crash at start.
    A silent failure would otherwise look like "no diagnostics" and give a false clean score.
    """
    from . import sandbox  # local import: sandbox imports nothing from here
    from .context import env_for_sanitizers
    wanted = [k for k in _PROBES if k in flags]
    with tempfile.TemporaryDirectory(prefix="ag_probe_") as d:
        for kind in wanted:
            src_code, expect = _PROBES[kind]
            src, exe = os.path.join(d, f"{kind}.c"), os.path.join(d, kind)
            with open(src, "w") as fh:
                fh.write(src_code)
            comp = sandbox.compile_c(src, exe, f"-std=c11 -w -O0 -g -fno-omit-frame-pointer {flags}")
            if not comp.ok:
                return False, f"cannot build with {flags}: {comp.stderr.strip()[:200]}"
            r = sandbox.run([exe], "", 20, None, cwd=d, env=env_for_sanitizers())
            if expect not in r.stderr:
                return False, (f"the {kind} sanitizer does not work on this machine "
                               f"(self-test {r.describe()}, no '{expect}' report)")
    return True, "ok"
