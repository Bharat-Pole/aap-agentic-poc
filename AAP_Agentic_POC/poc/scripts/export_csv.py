"""Dump every table in poc.db to a CSV file under data/exports/.

Run from the ``poc/`` source root::

    python scripts/export_csv.py

Produces one ``<table>.csv`` per table so the synthetic data can be eyeballed
in Excel / Sheets without a SQLite browser. The export directory is wiped and
rebuilt on each run so it always mirrors the current database exactly.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from sqlalchemy import inspect, select

# --- Make the poc/ source root importable when run as a bare script ---------
_POC_ROOT = Path(__file__).resolve().parent.parent
if str(_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_POC_ROOT))

from config import DATA_DIR  # noqa: E402
from data.database import ENGINE, get_session  # noqa: E402
from data.models import (  # noqa: E402
    AuditLog,
    BlueYonderPO,
    Inventory,
    Notification,
    OpenPO,
    PromoCalendar,
    SalesOfftake,
    SeasonIndex,
    UserDirectory,
    VendorApproval,
    VendorMaster,
)

EXPORT_DIR = DATA_DIR / "exports"

# Tables in a stable, readable export order alongside their model class.
_MODELS = [
    Inventory,
    SalesOfftake,
    VendorMaster,
    VendorApproval,
    PromoCalendar,
    SeasonIndex,
    OpenPO,
    BlueYonderPO,
    Notification,
    AuditLog,
    UserDirectory,
]


def _column_names(model) -> list[str]:
    """Return the ordered column names for a model's table.

    Args:
        model: A SQLAlchemy declarative model class.

    Returns:
        Column names in table-definition order.

    Rationale: drives a stable CSV header that matches the schema.
    """
    return [col.key for col in inspect(model).columns]


def _export_table(session, model) -> int:
    """Write one model's rows to ``data/exports/<table>.csv``.

    Args:
        session: Open ORM session.
        model: Declarative model to export.

    Returns:
        Number of rows written.

    Rationale: header-first, UTF-8 with BOM so Excel opens it cleanly; empty
    tables still get a header-only file so the export set is complete.
    """
    table_name = model.__tablename__
    columns = _column_names(model)
    rows = session.scalars(select(model)).all()

    out_path = EXPORT_DIR / f"{table_name}.csv"
    with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([getattr(row, col) for col in columns])
    return len(rows)


def main() -> None:
    """Dump all tables to CSV, rebuilding the export directory from scratch.

    Rationale: a single command that mirrors the live DB to flat files for
    non-technical review (Demand Planner / leadership).
    """
    if not Path(ENGINE.url.database).exists():
        print("poc.db not found — run `python scripts/seed_data.py` first.")
        return

    # Wipe stale exports so the folder always reflects the current DB.
    if EXPORT_DIR.exists():
        for stale in EXPORT_DIR.glob("*.csv"):
            stale.unlink()
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Exporting tables to {EXPORT_DIR}")
    total = 0
    with get_session() as session:
        for model in _MODELS:
            count = _export_table(session, model)
            total += count
            print(f"  {model.__tablename__:<18} {count:>6} rows")
    print(f"Done: {len(_MODELS)} files, {total} rows total.")


if __name__ == "__main__":
    main()
