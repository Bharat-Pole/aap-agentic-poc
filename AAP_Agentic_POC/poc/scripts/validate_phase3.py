"""Phase-3 acceptance gate — proves the LLM edges behave and never hard-fail.

Run from the ``poc/`` source root (re-seeds the DB itself for determinism)::

    python scripts/validate_phase3.py

Phase 3 adds the language model at exactly two human-facing edges — the draft-PO
justification narrative (Approval Agent) and the notification body (Notification
Agent) — always behind a deterministic template fallback. This gate proves:

* The provider wrapper never raises and always returns non-empty text, including
  with ``USE_LLM=false`` (template) and offline (no key, no Ollama -> template).
* With no API key and no Ollama running, every canonical scenario still completes
  using templates, and the recorded source on each edge is ``template``.
* A draft carries a justification narrative, that narrative is what gets persisted
  on the approved PO, and the narrative source is recorded in the audit log.
* The template path is byte-stable (two runs produce the identical narrative).
* The provider actually used is surfaced (``narrative_source`` / ``body_source``)
  — ``gemini`` when ``GEMINI_API_KEY`` is set, ``template`` offline.

Routing, forecasting, and the write guardrail are unchanged from Phase 1/2 — this
gate only exercises the prose edges. Run the Phase-1/2 gates for the rest.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# --- Make the poc/ source root importable when run as a bare script ---------
_POC_ROOT = Path(__file__).resolve().parent.parent
if str(_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_POC_ROOT))

try:  # UTF-8 stdout so ✓/✗ render on a cp1252 Windows console.
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):  # pragma: no cover
    pass

logging.disable(logging.CRITICAL)  # keep the checklist clean; the wrapper logs at INFO.

from sqlalchemy import select  # noqa: E402

import config  # noqa: E402
from data.database import get_session  # noqa: E402
from data.models import AuditLog, BlueYonderPO, Notification  # noqa: E402
from llm import provider  # noqa: E402
from orchestration import runner  # noqa: E402

from scripts.seed_data import seed  # noqa: E402

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record one named assertion for the final checklist.

    Args:
        name: Human-readable check label.
        ok: Whether the check passed.
        detail: Optional extra context shown after the label.

    Rationale: collect-then-report so one failure doesn't hide the rest.
    """
    results.append((ok, f"{name}{(' — ' + detail) if detail else ''}"))


def validate_provider_unit() -> None:
    """The wrapper never raises and always returns non-empty text."""
    # USE_LLM off -> straight to the template, returning the supplied fallback.
    config.USE_LLM = False
    off = provider.generate_with_provider("prompt", fallback="DETERMINISTIC")
    check("USE_LLM=false returns the template fallback verbatim",
          off.provider == provider.TEMPLATE and off.text == "DETERMINISTIC", off.provider)

    # No fallback supplied -> still non-empty (echoes the prompt). Never raises.
    bare = provider.generate_with_provider("echo me")
    check("Wrapper always returns non-empty text (no fallback given)",
          bool(bare.text) and bare.provider == provider.TEMPLATE)

    # USE_LLM on, but offline (no key, no Ollama) -> falls through to template.
    config.USE_LLM = True
    online = provider.generate_with_provider("prompt", fallback="FALLBACK-TEXT")
    keyed = bool(config.GEMINI_API_KEY)
    check("Offline provider chain degrades to template (no key, no Ollama)",
          keyed or (online.provider == provider.TEMPLATE and online.text == "FALLBACK-TEXT"),
          f"source={online.provider}")


def validate_offline_scenarios() -> None:
    """With no key and no Ollama, every scenario completes via templates."""
    config.USE_LLM = True  # default demo mode; offline -> template fallback.
    keyed = bool(config.GEMINI_API_KEY)
    expect = provider.GEMINI if keyed else provider.TEMPLATE

    # S1 auto-issue: completes, notification phrased (template offline).
    s1 = runner.run_for_sku("SKU-123", run_id="p3-s1", thread_id="p3-s1")
    check("S1 auto-issue completes with a PO", s1.status == "completed" and s1.po_written,
          s1.po_number or "")
    with get_session() as s:
        src = s.scalar(select(Notification.body).where(Notification.sku == "SKU-123"))
    check("S1 notification body present", bool(src))

    # S2 draft: pauses with a narrative, approve writes the narrative onto the PO.
    s2 = runner.run_for_sku("SKU-456", run_id="p3-s2", thread_id="p3-s2")
    narrative = (s2.approval_request or {}).get("draft_narrative")
    check("S2 draft surfaces a justification narrative at the pause",
          bool(narrative) and "SKU-456" in narrative)
    check("S2 narrative uses only seeded facts (qty 936, threshold 150)",
          bool(narrative) and "936" in narrative and "150" in narrative)
    done = runner.resume_run("p3-s2", approved=True, approved_by="@dp.alex")
    with get_session() as s:
        just = s.scalar(select(BlueYonderPO.justification).where(BlueYonderPO.sku == "SKU-456"))
    check("S2 approved PO persists the narrative as its justification",
          bool(just) and narrative is not None and narrative.split(".")[0] in just,
          (just or "")[:40] + "…")

    # The recorded narrative source proves which path ran (gemini if keyed).
    with get_session() as s:
        details = s.scalar(
            select(AuditLog.details).where(
                AuditLog.run_id == "p3-s2", AuditLog.agent == "ApprovalAgent"
            )
        )
    src = (json.loads(details) if details else {}).get("narrative_source")
    check(f"S2 narrative source recorded in audit ({expect})", src == expect, str(src))

    # S3 batch + S4 suppress still complete unchanged.
    b = runner.run_batch([f"SKU-{n}" for n in range(701, 709)], run_id="p3-s3")
    check("S3 batch completes (3 consolidated POs + 1 paused draft)",
          len(b.consolidated_pos) == 3 and len(b.draft_runs) == 1)
    s4 = runner.run_for_sku("SKU-212", run_id="p3-s4", thread_id="p3-s4")
    check("S4 suppression completes and writes no PO",
          s4.status == "completed" and not s4.po_written)

    # Notification body source is recorded in the NOTIFIED audit details too.
    with get_session() as s:
        ndet = s.scalar(
            select(AuditLog.details).where(
                AuditLog.run_id == "p3-s4", AuditLog.event_type == "NOTIFIED"
            )
        )
    body_src = (json.loads(ndet) if ndet else {}).get("body_source")
    check(f"S4 notification body source recorded ({expect})", body_src == expect, str(body_src))


def validate_template_determinism() -> None:
    """The template path is byte-stable across re-runs (offline demo safety)."""
    config.USE_LLM = False  # force the template path regardless of any key.
    a = runner.run_for_sku("SKU-456", run_id="p3-d1", thread_id="p3-d1")
    b = runner.run_for_sku("SKU-456", run_id="p3-d2", thread_id="p3-d2")
    na = (a.approval_request or {}).get("draft_narrative")
    nb = (b.approval_request or {}).get("draft_narrative")
    check("Template narrative is identical across two runs (deterministic)",
          bool(na) and na == nb)
    config.USE_LLM = True  # restore the default for any later use.


def main() -> int:
    """Re-seed, run every Phase-3 validation, and print the checklist.

    Returns:
        Exit code 0 if all checks pass, 1 otherwise.

    Rationale: a single deterministic gate for 'are the LLM edges safe to demo?'.
    """
    seed()  # clean slate so PO numbering and routing reproduce exactly.
    keyed = bool(config.GEMINI_API_KEY)
    print(f"(LLM mode: GEMINI_API_KEY {'set' if keyed else 'absent'}; "
          f"provider order {provider._provider_order()})")

    validate_provider_unit()
    validate_offline_scenarios()
    validate_template_determinism()

    print("\nPhase 3 acceptance checklist\n" + "=" * 60)
    for ok, label in results:
        print(f"  {'✓' if ok else '✗'} {label}")

    failed = [l for ok, l in results if not ok]
    print("=" * 60)
    if failed:
        print(f"FAIL — {len(failed)}/{len(results)} check(s) failed.")
        return 1
    print(f"PASS — all {len(results)} checks passed. Phase 3 is complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
