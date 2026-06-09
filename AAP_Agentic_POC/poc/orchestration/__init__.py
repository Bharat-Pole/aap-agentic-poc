"""Orchestration package: the LangGraph state machine wiring the six agents
together, including the human-in-the-loop interrupt on the draft path.

See :mod:`orchestration.graph` for the graph, :mod:`orchestration.state` for the
shared state schema, and :mod:`orchestration.runner` for ``run_for_sku`` /
``resume_run`` / ``run_batch``. Documented in ``docs/orchestration.md``.
"""
