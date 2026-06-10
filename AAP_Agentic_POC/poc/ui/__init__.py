"""UI package: the Streamlit Demand-Planner dashboard (Phase 5).

Three modules, presentation-only over the existing pipeline:

* :mod:`ui.app` — the Streamlit layout (``streamlit run ui/app.py``).
* :mod:`ui.service` — the bridge to :mod:`orchestration.runner` that runs a scan,
  approves/rejects drafts, and flattens pipeline state into worklist cards.
* :mod:`ui.data_access` — read-only ``SELECT`` views for the Blue Yonder,
  notifications, and audit panels.
* :mod:`ui.scenarios` — the S1–S4 registry the scenario filter keys on.

The only paths that mutate the database are the agent pipeline and the
approve/reject verbs; every panel reads through :mod:`ui.data_access`.
"""
