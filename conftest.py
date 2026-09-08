# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""Repo-root conftest: put the repository root on ``sys.path``.

`pytest.ini` uses ``--import-mode=importlib``, which deliberately does NOT add a test file's
directory to ``sys.path``. Everything the suite imports is an installed package except one:
``evals`` is a plain package in the working tree (``evals/run_evals.py``), and
``host/tests/test_evals.py`` imports it. That import only worked because ``python -m pytest``
prepends the CURRENT DIRECTORY -- so ``pytest -m evals`` passed from the repo root and failed
with ``ModuleNotFoundError: evals`` from anywhere else, including a bare ``pytest`` invocation.

pytest imports this file before collecting anything, and it sits next to ``pytest.ini``, so the
path it inserts is the repository root whatever the caller's working directory is.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
