# AutoGrader: a rule-driven evaluator for C programs

An agent that takes a student's C submission and an instructor-written rules file, runs the
program safely, checks it against every rule and returns **one reproducible grade** with feedback:
failing inputs beside the expected output, sanitizer and leak diagnostics, a timing profile and
suggestions.

* **Backend:** Django (SQLite) plus a framework-independent engine package (`engine/`)
* **Frontend:** plain HTML templates and one CSS file (no framework, light/dark)
* **Readability:** Groq (`openai/gpt-oss-120b`, temperature 0, seed 42). The assignment's example
  model `llama-3.3-70b-versatile` is no longer offered on Groq; gpt-oss-120b gives `add.c` the same 5/10.
* **Local LLM:** Ollama `qwen3:4b` for adversarial counter-tests, AI-tutor explanations and
  AI-generated reference solutions used by copy detection
* **Similarity:** abstract structural fingerprints ("abstract schematics"), see below

```
             rules/*.json  (outside the code: metrics, weights, thresholds, limits)
                  │
submission.c ──► Agent (engine/agent.py) ── coordinator
                  │
                  ├─ 1. First pass   compile (-O2 -Wall -Wextra) · smoke run on sample · static sanity
                  │                  · refuses system()/fork()/exec*/socket…
                  │                  timeout → suspected_infinite_loop, run only source-only analyzers
                  │
                  ├─ 2. Dispatch     one analyzer per metric, in parallel (timing ones run alone after)
                  │    io_tests · compiler_warnings · static_analysis · function_split · edge_case_runner
                  │    complexity_estimator · sanitizer_run · llm_review · similarity_flag
                  │
                  └─ 3. Collate      weighted grade · failures (input / expected / got / why / AI tutor)
                                     · flags · suggestions ranked by points lost · profile
                                     → reports/<name>.json (machines) + reports/<name>.txt (student)
```

## Setup

```bash
cd autograder
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # put your GROQ_API_KEY in .env
ollama pull qwen3:4b          # already installed on this machine
.venv/bin/python manage.py migrate
```

Requirements: a C compiler (`cc`: Apple clang or gcc) with `-fsanitize=address,undefined`.
Optional: `cppcheck` (extra structure findings), `valgrind` (Linux leak checks). On macOS, leaks are
checked with the built-in `leaks` tool.

### Linux

Tested with `scripts/setup_linux.sh` on clean installs of:

| distribution | Python | gcc | CPU | result |
|---|---|---|---|---|
| Ubuntu 22.04 LTS | 3.10 | 11.4 | arm64 | reproducibility check passes; grades = Ubuntu 24.04 |
| Debian 12 | 3.11 | 12.2 | arm64 | reproducibility check passes; grades = Ubuntu 24.04 |
| Ubuntu 24.04 LTS | 3.12 | 13.3 | arm64 | reproducibility check passes; web UI and API |
| Fedora 41 | 3.13 | 14.3 | arm64 | grading works |
| Ubuntu 24.04 LTS | 3.12 | 13.3 | x86_64 (emulated) | grading works; AddressSanitizer cannot run under emulation, see below |

The same samples get the same grades as on macOS (see *Platform differences* below for the exception).
Python 3.10 or newer is required.

**Sanitizer self-test.** Before trusting ASan/UBSan, the agent compiles and runs two tiny programs with a
known heap overflow and a known signed overflow (`engine/sanitizers.py`, `self_test`). ASan can be
installed yet unusable: under CPU emulation its shadow memory gets the process killed, and some kernels
with high address randomisation crash older ASan runtimes. Before this check, both cases looked like "no
diagnostics" and gave a **false 10/10** for memory safety. If the self-test fails, `memory_safety` is
marked `metric_unavailable`, left out of the grade (the other weights are renormalised), and the report
says why.

```bash
./scripts/setup_linux.sh                    # apt/dnf/pacman: gcc + libasan/libubsan, Python venv, database
WITH_VALGRIND=1 ./scripts/setup_linux.sh    # optional: use valgrind instead of LeakSanitizer
.venv/bin/python manage.py runserver 0.0.0.0:8000
```

Or in Docker (Ubuntu 24.04 image; the project folder is mounted, so the database, reports and LLM
cache stay on the host):

```bash
docker compose up --build          # http://localhost:8000
```

`docker-compose.yml` adds `SYS_PTRACE`, which LeakSanitizer needs inside containers (without it, the
report says the leak check was unavailable). It also points `OLLAMA_URL` at Ollama on the host.

How Linux differs internally:

| | macOS | Linux |
|---|---|---|
| compiler | Apple clang (`cc`) | gcc (`cc`); falls back to `gcc`/`clang`, or set `AUTOGRADER_CC` |
| memory limit | polls the physical footprint via `libproc` | polls `/proc/<pid>/status` (VmRSS); `RLIMIT_AS` at 2× limit + 64 MB as a safety net |
| process start | Python `preexec_fn` | tiny C launcher (`engine/launcher.c`), built once on first use |
| leak check | `leaks --atExit` | LeakSanitizer inside the ASan run, or valgrind if installed |

The launcher is needed because, on Linux, a child forked from the grader inherits the grader's
memory peak into `ru_maxrss` across `exec`. A 1 MB program was reported at 30–40 MB, and once the
long-running grader grew past 64 MB *every* test was marked "memory limit exceeded". Forking from a
tiny C process gives the program's true peak. It also applies the rlimits in C instead of
`preexec_fn`, which is not thread-safe while analyzers run in parallel.

LLM answers are served from `.cache/llm` before checking whether Groq or Ollama is reachable. A Linux
grading server without Ollama therefore reproduces the grades made on a machine with it, as long as
the cache is copied along.

**Platform differences.** Time limits and memory layout are machine properties. `sum_quadratic.c`
(O(n²)) passes hidden test 08 on an M-series Mac but is close to the limit on a 2-CPU Linux VM, so
its grade differs slightly (79.50 vs 78.90). `sum_fixed_buffer.c` crashes at n = 20 000 on Linux
(smaller default stack) but not on macOS. Each machine is reproducible on its own; for a course,
grade everything on one machine.

## Using it

**Web UI**

```bash
.venv/bin/python manage.py runserver 8000 --noreload
```

Open http://localhost:8000. **Grade** uploads one or many `.c` files (or pasted code), **Submissions**
shows history and flags, and **Rules** lists and edits the rules files with validation. Grading runs on a
background worker, one submission at a time so CPU timings stay stable.

**Unattended / batch (CLI)**

```bash
.venv/bin/python -m engine submissions/add_two_integers/add.c --rules rules/add_two_integers.json --out reports/
.venv/bin/python manage.py grade submissions/sum_array/*.c --rules rules/sum_array.json --save   # also show in the UI
```

**HTTP API**

```bash
curl -F file=@add.c -F rules=add_two_integers.json http://localhost:8000/api/grade/
```

**Reproducibility check** (grades every sample several times and compares grade, metric scores and flags):

```bash
.venv/bin/python scripts/check_reproducibility.py --runs 3
```

## The rules file

`rules/add_two_integers.json` is the assignment's example, extended with what the agent needs to
generate its own tests:

| key | meaning |
|---|---|
| `problem`, `statement_file` | name and problem text (the text is given to the LLM analyzers) |
| `reference_solution` | instructor's C solution: the **oracle** for generated corner cases |
| `time_limit_s`, `memory_limit_mb` | sandbox limits per run |
| `input_format` | typed stdin layout used to generate valid inputs and validate LLM ones |
| `compare` | `tokens` (default), `lines`, `exact`, `float:1e-6` |
| `metrics[]` | `name`, `analyzer`, `weight` (normalised if they don't sum to 1), `params` |

```json
"input_format": {"lines": [[{"name": "n", "type": "int", "min": 0, "max": 1000000}],
                           [{"name": "xs", "type": "int", "repeat": "n"}]]}
```

Adding a rule or changing a weight or threshold needs no code change. A new kind of metric is a small
plug-in: subclass `Analyzer` in `engine/analyzers/`, decorate it with `@register`, and reference its
`name` in a rules file.

## How each analyzer works (and why)

| analyzer | method |
|---|---|
| **io_tests** | Runs every `NN.in` in `testset_dir` and compares with `NN.out` (or the reference's output). AC/WA/TLE/MLE/RE per test, CPU time and peak memory per test. Each failure is re-run under ASan+UBSan to find the root cause (e.g. *runtime error at line 5: signed integer overflow*). |
| **compiler_warnings** | Compiles with the rule's flags (minus `-Werror`, so warnings are counted, not merged into one error); −0.1 per distinct warning, 0 on errors; reports if it would fail `-Werror`. |
| **static_analysis** | lizard per-function cyclomatic complexity and length against `max_cyclomatic` / `max_function_length`, plus deterministic checks: comments and header comment, single-block code, mutable globals, `goto`, long lines, magic numbers, cppcheck if installed. |
| **function_split** | Counts helper functions vs `min_functions`, the share of code in `main()`, and unused helpers. |
| **edge_case_runner** | **Generates counter-tests.** Category generators (`zero, negatives, int_overflow, boundaries, random, whitespace, large, single, empty`) build inputs from `input_format` with a RNG seeded from the rules hash. With `llm`, qwen3:4b reads the statement **and the student's code** and proposes inputs likely to break *this* program; every proposal is validated against `input_format` before use. Expected outputs come from the reference solution. Without a reference, it degrades to robustness checks (no crash, hang or UB). |
| **complexity_estimator** | Generates inputs at the rule's `sizes`, takes the **min of N CPU times** (rusage), subtracts the smallest size as baseline and fits a log-log slope, mapping slope bands to complexity classes. When timings sit below the noise floor it falls back to static analysis (loop-nesting depth, recursion, `qsort`). TLE at a size means it exceeds the bound. |
| **sanitizer_run** | Builds with `-fsanitize=address,undefined -g`, runs the tests, and collects distinct (kind, line) diagnostics: heap/stack overflow, use-after-free, UB. Leak check: macOS `leaks --atExit` with stack logging (reports the leaking line), LeakSanitizer or valgrind on Linux. −0.25 per distinct issue. |
| **llm_review** | Groq, fixed rubric, line-numbered code, JSON output, T=0, seed=42, **cached**. Without a key it uses a clearly labelled deterministic heuristic (names, comments, spacing, indentation). |
| **similarity_flag** | See below. |

### Originality: abstract structural fingerprints

Each program is lexed and reduced to an abstract representation that survives the usual disguises:

1. **Canonicalisation:** `for (A; B; C) S` is rewritten to `A; while (B) { S C; }`; braces are dropped
   (so `if (x) return 0;` ≡ `if (x) { return 0; }`); comments and whitespace are removed.
2. **Abstraction:** identifiers become `V`, literals `N`/`S`/`C`, `int/long/short/unsigned` become `INT`,
   `for/while/do` become `LOOP`, `++/--` become `INC`, compound assignment becomes `OPEQ`. Library calls
   (`scanf`, `malloc`, …) are kept because they carry meaning.
3. **Two views:** token 5-gram fingerprints (Jaccard) and the control-flow skeleton
   (loops/branches/returns/library calls, sequence-alignment ratio), combined as 0.75/0.25.
4. **Base-code exclusion (as in MOSS):** k-grams that also occur in the instructor's reference solution
   are removed first, so shared boilerplate is not evidence.
5. **Corpus:** every `.c` under `corpus_dir` (peers, including web uploads) plus **AI-generated
   solutions**: qwen3:4b writes `llm_reference_solutions` solutions once per problem
   (`corpus/llm_generated/<problem>/`), so "copied from an LLM" can be flagged like peer copying.
6. **Size gate:** programs shorter than `min_tokens` (default 50 abstract tokens) are reported but never
   flagged, because a 6-line a+b is written the same way by nearly everyone.

`sum_copy.c` (all names changed, `for` → `while`, braces and comments changed) reduces to exactly the
same abstract stream as `sum_good.c`, so both are flagged at similarity 1.00.

## Reproducibility: same submission + same rules → same grade

* Every generated input comes from `random.Random(sha256(rules_hash + category))`.
* Analyzer results are collected in **rules order**, not completion order; all iteration is sorted.
* **LLM answers are content-addressed and cached.** This is necessary: with temperature 0 and seed 42, Groq
  still returned 7, 8, 7 for `sum_leak.c` on three fresh calls. Answers are stored as
  `.cache/llm/<sha256(provider, model, messages, temperature, seed)>.json`, and the first answer is replayed
  on every later run, so neither sampling noise nor a model update can change a grade. Keep `.cache/llm`
  with the course records.
* **Time limits are CPU time, not wall-clock time**, as on competitive-programming judges. A trivial
  program took 0.42 s wall-clock for 0.01 s of CPU on its first launch, because macOS scans new
  binaries. Under load this once pushed a correct `add.c` past the 1 s limit, so it was flagged as an
  infinite loop. Spinning programs are still killed by `RLIMIT_CPU`, and a wall-clock backstop
  (3 × limit + 1 s) kills programs that hang without using CPU.
* Complexity uses min-of-N CPU time mapped onto **discrete classes**, and grading is serialised on one
  worker, so timing noise does not cross class boundaries in practice.
* Every report records `submission_sha256`, `rules_hash`, `inputs_hash` (rules + tests + reference),
  the corpus hash and tool versions.

## Sample results

The output of `scripts/check_reproducibility.py --runs 3` is below; reports are in `reports/<name>.txt` and `.json`.

| submission | what's wrong with it | grade | readability (Groq) | what the agent reported |
|---|---|---|---|---|
| `add.c` | the assignment's example: `int` sum, everything in main, no comments | **75.72** | 5/10 | modularity: only main() is defined; correctness: 8 of 10 tests pass; structure: No comments at all; All logic is in a single block (main only). |
| `add_clean.c` | 64-bit sum, helper function, comments | **100.00** | 10/10 | everything passes |
| `add_infinite.c` | waits for a `.` that never arrives | **40.00** | 8/10 | correctness: skipped; logical: skipped; complexity: skipped; **flag suspected_infinite_loop** |
| `add_syntax_error.c` | missing `;` | **26.00** | 6/10 | correctness: skipped; syntax: 1 compile error(s); logical: skipped; **flag compile_failed** |
| `sum_good.c` | correct O(n), helper, 64-bit sum | **89.50** | 9/10 | originality: FLAGGED: 1.00 vs sum_copy.c; **flag suspected_copy** |
| `sum_copy.c` | sum_good.c with names, loop kind, braces and comments changed | **88.00** | 6/10 | originality: FLAGGED: 1.00 vs sum_good.c; **flag suspected_copy** |
| `sum_ai.c` | qwen3's own solution, renamed and re-commented | **85.50** | 9/10 | originality: FLAGGED: 1.00 vs qwen3_4b_seed1.c; **flag suspected_llm_copy** |
| `sum_quadratic.c` | correct but O(n²) | **79.50** | 7/10 | complexity: O(n^2) exceeds (required O(n)); modularity: only main() is defined; structure: All logic is in a single block (main only). |
| `sum_leak.c` | `int` accumulator, `malloc` never freed | **77.55** | 7/10 | correctness: 5 of 8 tests pass; memory_safety: memory-leak, undefined-behaviour; logical: int_overflow not handled, boundaries not handled |
| `sum_fixed_buffer.c` | `int buf[100]` overflows when n > 100 | **82.64** | 7/10 | correctness: 6 of 8 tests pass; modularity: only main() is defined; memory_safety: stack-buffer-overflow |

## Usefulness and limitations

Every report includes this section; the source is `engine/usefulness_limitations.json`.

**Useful for**

1. **Large programming courses (CS1 labs).** A 300-student lab is batch-graded with one command. Every
   student gets the same rubric and the exact failing input (`2147483647 1` → expected `2147483648`)
   instead of a bare mark.
2. **Coding-interview screens.** For "sum an array in O(n)", an O(n²) nested loop is separated by the
   timing curve (`sum_quadratic.c` times out at n = 320 000), and an `int` accumulator by the
   `int_overflow` corner cases. Each rejection can be justified metric by metric.
3. **Plagiarism and LLM-copy triage.** Renamed, reformatted, for→while copies are flagged (1.00), and so
   are matches to qwen3-generated solutions; instructors review only the flagged pairs.
4. **Self-paced practice.** A student gets *memory-leak at line 12 (macOS leaks)*, a sanitizer trace and
   an AI-tutor explanation at midnight, without staff time.

**Limitations (each is a concrete case where the verdict is wrong or unreliable)**

1. **Tests cannot prove correctness.** A program that hard-codes the hidden tests, or only fails at
   n = 99 991, gets full correctness marks.
2. **Timing-based complexity is unreliable for small sizes.** With sizes `[1, 10, 100]` an O(n²) program
   runs in < 1 ms everywhere. The static fallback then counts `for (i = 0; i < 3; i++)` as an O(n) loop
   and treats recursive merge sort (two self-calls) as possibly exponential.
3. **Similarity cannot judge tiny programs and misses semantic rewrites.** A copied 6-line a+b gets
   10/10 originality (size gate), and a copy rewritten from a loop into recursion is not flagged.
4. **Verdicts depend on the compiler and platform.** `while (b != 0) { s += 1; }` never terminates, but
   clang `-O2` may delete it (C11 §6.8.5p6). We observed this: the smoke run finished in 6 ms, and the
   agent reported a wrong answer instead of an infinite loop. On macOS `RLIMIT_AS` is ignored, so memory
   is capped by polling the process footprint, and a 200 MB burst that ends within ~5 ms can slip past a
   64 MB limit.

A further caveat: **LLM readability depends on the model.** The cache makes re-grades identical, but
clearing it or switching models can move the same code from 5/10 to 7/10 (we had to switch: Groq retired llama-3.3-70b-versatile). Without `GROQ_API_KEY`, the
fallback heuristic rewards long names even when they are misleading.

## Project layout

```
engine/                 framework-independent agent
  agent.py              coordinator: first pass, dispatch, collation
  sandbox.py            compile + run under time/memory/output limits
  context.py            shared state: binaries, reference oracle, tests, seeded RNG
  inputs.py             input_format → deterministic edge-case generation and validation
  clexer.py             C lexer, abstract token stream, control skeleton, loop depth
  llm.py                Groq + Ollama clients behind a content-addressed cache
  explain.py            deterministic WHY explanations + sanitizer root-cause hints
  sanitizers.py         ASan / LSan / UBSan report parser (clang and gcc)
  launcher.c            Linux run launcher (exact rusage, rlimits in C)
  report.py             text report
  analyzers/            one module per analyzer (plug-ins)
grader/                 Django app: models, views, templates, CSS, `manage.py grade`
rules/                  instructor rules (edit freely)
problems/               statements + reference solutions
tests/                  hidden tests (NN.in / NN.out)
submissions/            sample submissions; web uploads go to submissions/<problem>/uploads/
corpus/llm_generated/   qwen3-generated solutions used for AI-copy detection
reports/                generated JSON + text reports
scripts/                check_reproducibility.py, setup_linux.sh
Dockerfile, docker-compose.yml   Linux image (Ubuntu 24.04, gcc 13)
```
