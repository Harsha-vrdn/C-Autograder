"""Analyzer plug-ins. Importing this package registers every built-in analyzer.

To add a metric: subclass ``Analyzer``, decorate it with ``@register`` and import
the module here; rules files can then reference it by ``name``.
"""
from .base import REGISTRY, Analyzer, Result, register  # noqa: F401
from . import (  # noqa: F401
    io_tests, compiler_warnings, static_analysis, function_split, edge_case_runner,
    complexity_estimator, sanitizer_run, llm_review, similarity_flag,
)
