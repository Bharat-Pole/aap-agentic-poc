"""Pytest fixtures and import setup for the Phase-4 guardrail test suites.

This conftest lives at the ``poc/`` source root so its directory is placed on
``sys.path`` for collection — letting the tests under ``tests/`` import the
``agents``, ``data``, and ``orchestration`` packages exactly as the app does.

Two cross-cutting fixtures make the suites deterministic and offline:

* ``no_llm`` (autouse) forces ``config.USE_LLM = False`` so the justification and
  notification text come from the deterministic templates — no network, no Gemini
  / Ollama calls, byte-stable bodies the assertions can rely on.
* ``fresh_db`` reseeds ``poc.db`` from the canonical scenario seed and resets the
  cached orchestration graph (a clean in-memory checkpointer) so each test starts
  from an identical world with no thread-id collisions from a previous test.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

# --- Make the poc/ source root importable for the test packages -------------
_POC_ROOT = Path(__file__).resolve().parent
if str(_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_POC_ROOT))

import config  # noqa: E402
from orchestration import runner  # noqa: E402
from scripts.seed_data import seed  # noqa: E402

# Agents log at INFO; keep the test output clean.
logging.disable(logging.CRITICAL)


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    """Force the deterministic template path for every test (offline, stable).

    Args:
        monkeypatch: Pytest's attribute patcher.

    Rationale: the LLM edges are optional and non-deterministic; disabling them
        keeps the guardrail/audit assertions about *facts*, not phrasing, and
        avoids any network call (and its timeout) during the suite.
    """
    monkeypatch.setattr(config, "USE_LLM", False)


@pytest.fixture
def fresh_db():
    """Reseed poc.db and reset the orchestration graph for one isolated test.

    Yields:
        None — the test runs against a freshly seeded database whose
        ``blue_yonder_po``, ``audit_log``, and ``notifications`` tables are empty.

    Rationale: PO numbering and the guardrail invariants ("a SUPPRESS run writes
        no PO row") are global to the DB, so each behavioural test needs a clean
        slate; resetting ``runner._GRAPH`` gives a fresh checkpointer so reused
        thread ids from a prior test cannot collide.
    """
    seed()
    runner._GRAPH = None  # fresh MemorySaver on next get_graph().
    runner._PAUSED_THREADS.clear()
    yield
