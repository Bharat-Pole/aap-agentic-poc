"""Shared, environment-aware configuration for the replenishment POC.

Centralises the few values that must stay consistent across the data layer,
the agents, and the UI: the deterministic random seed, the reference "today"
(so ETAs and promo windows reproduce exactly), the canonical sub-category list,
and the LLM provider settings. Importing config here — rather than scattering
`datetime.now()` / `random.seed()` calls — is what makes the four scenarios
replay identically on every machine.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# Load .env if present; real environment variables always win over the file.
load_dotenv()

# --- Paths -------------------------------------------------------------------
#: Source root (this file's directory). Sub-packages live beneath it.
PROJECT_ROOT: Path = Path(__file__).resolve().parent
#: Directory that holds the SQLite artifact.
DATA_DIR: Path = PROJECT_ROOT / "data"
#: The single local database that mocks Palantir + Snowflake/AS400.
DB_PATH: Path = DATA_DIR / "poc.db"
DB_URL: str = f"sqlite:///{DB_PATH}"


def _parse_reference_date(raw: str) -> date:
    """Parse the configured reference date (YYYY-MM-DD).

    Args:
        raw: Date string from the environment.

    Returns:
        The parsed ``date``.

    Rationale: a single fixed "today" keeps eta_days/promo-active checks
    deterministic across the whole pipeline.
    """
    return date.fromisoformat(raw)


# --- Determinism -------------------------------------------------------------
#: Seed for numpy + Faker so synthetic data is byte-stable.
RANDOM_SEED: int = int(os.getenv("POC_RANDOM_SEED", "42"))
#: The "current date" the whole POC reasons against.
REFERENCE_DATE: date = _parse_reference_date(
    os.getenv("POC_REFERENCE_DATE", "2026-06-09")
)

# --- Domain ------------------------------------------------------------------
#: Canonical product sub-categories. Every SKU belongs to exactly one, and
#: user_directory seeds one Demand Planner per entry here.
SUBCATEGORIES: tuple[str, ...] = (
    "Household",
    "Personal Care",
    "Confectionery",
    "Grocery",
    "Beverages",
    "Snacks",
    "Frozen",
    "Bakery",
)

# --- Replenishment policy (deterministic routing knobs) ----------------------
# These tune the deterministic detection/forecast logic in the agents. They are
# centralised here so a reviewer can see — and a demo can tweak — every magic
# number behind a routing decision in one place. None of them involve an LLM.

#: A breach is "critical" when on_hand falls below this fraction of the
#: reorder threshold (Stock Monitor: CRITICAL-LOW vs LOW-STOCK).
CRITICAL_LOW_RATIO: float = float(os.getenv("POC_CRITICAL_LOW_RATIO", "0.25"))

#: Planning-horizon multiplier the Demand Forecast Agent applies to weekly
#: demand for a normal SKU. The seed's ``inventory.lead_multiplier`` overrides
#: this when it carries an explicit non-neutral (> 1.0) value.
DEFAULT_LEAD_MULTIPLIER: float = float(os.getenv("POC_DEFAULT_LEAD_MULTIPLIER", "1.5"))

#: Planning-horizon multiplier for a peak-season SKU (e.g. S3 Diwali surge).
PEAK_LEAD_MULTIPLIER: float = float(os.getenv("POC_PEAK_LEAD_MULTIPLIER", "2.0"))

#: Scales each SKU's ``safety_stock`` into the order-quantity safety buffer.
#: 1.0 = use safety_stock as-is; raise it to pad orders during volatile periods.
SAFETY_BUFFER_FACTOR: float = float(os.getenv("POC_SAFETY_BUFFER_FACTOR", "1.0"))

#: Prefix and sequence base for generated Blue Yonder PO numbers
#: (``PO-<year>-<seq>``). Kept distinct from the seeded open-PO numbering.
PO_NUMBER_SEQ_BASE: int = int(os.getenv("POC_PO_SEQ_BASE", "90000"))

#: SLA window (hours) granted to a human to action a CRITICAL-LOW draft PO
#: before it is considered overdue. The Approval Agent stamps an
#: ``approval_deadline`` on critical-low drafts from this; no real timer fires in
#: the POC — the deadline is stored and displayed so escalation is demonstrable.
CRITICAL_DRAFT_SLA_HOURS: int = int(os.getenv("POC_CRITICAL_SLA_HOURS", "24"))

# --- LLM (used ONLY at the two human-facing edges) ---------------------------
# The LLM never touches detection, forecasting, or routing. It is used in exactly
# two places — the draft-PO justification narrative (Approval Agent) and the
# notification body phrasing (Notification Agent) — and always behind a
# deterministic template fallback, so the POC completes every scenario offline.


def _parse_bool(raw: str | None, default: bool) -> bool:
    """Parse a truthy environment string into a bool.

    Args:
        raw: The raw environment value (or None if unset).
        default: Value to use when ``raw`` is None/empty.

    Returns:
        ``True`` for {1,true,yes,on} (case-insensitive), ``False`` otherwise.

    Rationale: one parser keeps env-driven feature flags consistent and lets a
    demo flip the whole LLM layer off with ``USE_LLM=false``.
    """
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


#: Master switch for the LLM edges. When False, justification + notification text
#: come straight from the deterministic templates — a fully offline, byte-stable
#: demo. When True (default), the provider chain is tried first and degrades to
#: the same templates if no provider is reachable.
USE_LLM: bool = _parse_bool(os.getenv("USE_LLM"), default=True)

#: Preferred provider tried first; the chain falls through to the other provider
#: and then the template regardless. One of {"gemini", "ollama"}.
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY") or None
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5")

#: (connect, read) timeouts in seconds for the Ollama HTTP call. A short connect
#: timeout means an offline demo (nothing listening on 11434) fails over to the
#: next provider almost instantly instead of stalling.
OLLAMA_TIMEOUT: tuple[float, float] = (
    float(os.getenv("POC_OLLAMA_CONNECT_TIMEOUT", "1.0")),
    float(os.getenv("POC_OLLAMA_READ_TIMEOUT", "60.0")),
)
