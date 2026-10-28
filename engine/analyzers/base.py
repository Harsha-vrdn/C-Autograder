from __future__ import annotations

from dataclasses import dataclass, field

REGISTRY: dict[str, type["Analyzer"]] = {}


@dataclass
class Result:
    score: float                      # 0..1
    feedback: str
    evidence: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)      # {input, expected, got, verdict, note, ...}
    suggestions: list = field(default_factory=list)
    flags: list = field(default_factory=list)          # {code, message, ...}
    verdict: str = ""                                  # short one-line summary for tables
    # The grading machine cannot measure this metric (e.g. AddressSanitizer does not work there).
    # The metric is then left out of the grade instead of scoring 0 or a false 1.
    unavailable: bool = False

    def __post_init__(self):
        self.score = round(min(1.0, max(0.0, float(self.score))), 4)


class Analyzer:
    name = ""
    description = ""
    needs_binary = False      # skipped when the program cannot be compiled / run safely
    timing_sensitive = False  # run alone, after the parallel batch, to reduce timing noise

    def run(self, ctx, params: dict) -> Result:  # pragma: no cover
        raise NotImplementedError


def register(cls):
    REGISTRY[cls.name] = cls
    return cls
