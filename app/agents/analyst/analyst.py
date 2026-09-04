"""Agent 2 — the Analyst node. Ties phases 1-3 into one RunState transition.

Reads journey config; consumes state.snapshot (Fetcher's output) plus the
journey's cohort-cut fixture (via AggregateTool) and PII-scrubbed reviews;
writes state.findings / state.drilldown_trail / state.voc. Fails loudly:
any exception marks the run failed with failed_stage="analyzing" upstream.
"""
import json
from pathlib import Path
from typing import Any, Callable, Optional

from app.agents.analyst import phase1
from app.agents.analyst.aggregate_tool import AggregateTool
from app.agents.analyst.journey_events import journey_events_for
from app.agents.analyst.phase2 import run_drilldown
from app.agents.analyst import phase3_voc
from app.agents.analyst.phase3_voc import corroborate, correlate_with_llm, run_voc
from app.agents.analyst.semantic_voc import classify_reviews
from app.agents.analyst.validator import collect_numbers, filter_findings
from app.journeys import load_journey
from app.schemas.contracts import RunState, validate_routing_stage

FIXTURES = Path("fixtures")


def _default_routing_for_gap(gap: dict, journey_cfg: dict) -> str:
    """The routing category a finding gets when the model names a funnel stage
    instead of a category (it usually does).

    Comes from the journey config's `default_routing`, falling back to the first
    `routing:` key. It used to hardcode pharmacy_checkout whenever that key
    existed — and the consultation journey has that key for the eRx hand-off,
    so every consultation finding was routed to the pharmacy order service on
    the first live consultation run.
    """
    keys = list(journey_cfg["routing"].keys())
    default = journey_cfg.get("default_routing")
    if default in keys:
        return default
    return keys[0]


def run_analyst(state: RunState,
                llm: Callable[[dict[str, Any]], dict[str, Any]],
                cohort_cuts: Optional[dict] = None,
                reviews: Optional[list[dict]] = None,
                voc_llm: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
                correlation_llm: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
                ) -> RunState:
    cfg = load_journey(state.journey)
    routing_keys = list(cfg["routing"].keys())

    # ---- phase 1: deterministic ----
    table = phase1.funnel_table(state.snapshot, cfg["stages"])
    gap = None
    if state.scope.from_stage and state.scope.to_stage:
        # The user named a transition. If it does not exist in this journey we
        # fall back to the largest drop rather than analysing nothing, and say
        # so on the scope so the report can admit it answered a wider question.
        gap = phase1.gap_for_transition(table, state.scope.from_stage, state.scope.to_stage)
        if gap is None:
            state.scope.unresolved.append(
                f"no {state.scope.from_stage} -> {state.scope.to_stage} transition in this "
                f"journey's funnel; analysed the largest drop instead")
    if gap is None:
        gap = phase1.largest_drop(table)
    clusters = phase1.cluster_reasons(state.snapshot.reasons, cfg["artifact_reasons"])
    summary = {"funnel": table, "reason_clusters": clusters,
               "caveats": phase1.censoring_caveats(cfg["stages"], cfg.get("maturing_stages") or [])}

    # ---- VoC classification (before the drill-down) ----
    # Themes are classified first so the drill-down model can see them as
    # voc_signals and use a large, unescalated theme to pick its next cut.
    # Ranks are assigned afterwards: VoC findings always sit after warehouse
    # findings, so they are re-ranked once the drill-down is done.
    voc_findings: list = []
    voc = None
    if reviews is None and state.demo_mode:
        rv_path = FIXTURES / state.journey / "reviews_scrubbed.json"
        reviews = json.loads(rv_path.read_text()) if rv_path.exists() else []
    if reviews:
        reviews, window_meta = phase3_voc.filter_by_days(reviews, state.scope.review_days)
        themes_per_review, voc_meta = classify_reviews(
            voc_llm, reviews, cfg["voc"]["themes"], phase3_voc.classify_review,
            scope_hint=state.scope.prompt)
        voc_findings, voc = run_voc(reviews, cfg["voc"], 1,
                                    themes_per_review=themes_per_review,
                                    extra_meta={**window_meta,
                                                "classifier": voc_meta["classifier"]})
    voc_signals = [{"theme": t["theme"], "count": t["count"], "escalated": t["escalated"]}
                   for t in (voc.themes if voc else []) if t["theme"] != "unmapped"]

    # ---- phase 2: agentic drill-down ----
    if cohort_cuts is None and state.demo_mode:
        cuts_path = FIXTURES / state.journey / "cohort_cuts.json"
        cohort_cuts = json.loads(cuts_path.read_text()) if cuts_path.exists() else {}
    # A scoped run narrows the whitelist to what was asked for; anything the
    # user named that has no cohort data is reported rather than silently dropped.
    whitelist = cfg["drilldown_dimensions"]
    if state.scope.dimensions:
        wanted = [d for d in state.scope.dimensions if d in whitelist]
        missing = [d for d in state.scope.dimensions if d not in whitelist]
        if missing:
            state.scope.unresolved.append(f"not drillable in this journey: {missing}")
        if wanted:
            whitelist = wanted
    tool = AggregateTool(cohort_cuts or {}, whitelist)
    findings, trail = run_drilldown(
        llm, tool, gap or {}, summary, routing_keys,
        _default_routing_for_gap(gap or {}, cfg), voc_signals=voc_signals,
        must_try=list(state.scope.dimensions or []))

    # ---- evidence gate (accepts every number the model was shown) ----
    shown = collect_numbers(summary) | collect_numbers(gap or {})
    kept, rejected = filter_findings(findings, state.snapshot, trail, shown)
    for f in kept:
        validate_routing_stage(f.stage, routing_keys)
        # Decision #11 - real analytics event names, better GitLab search
        # seed material for Code Scout than hypothesis-prose splitting.
        f.journey_events = journey_events_for(f, state.snapshot.ct_events)

    # ---- VoC findings: ranked after the warehouse findings ----
    if voc is not None:
        next_rank = (max((f.rank for f in kept), default=0)) + 1
        for i, f in enumerate(voc_findings):
            f.rank = next_rank + i
            validate_routing_stage(f.stage, routing_keys)
        corroborate(kept, voc, cfg["voc"])
        # Phase 3.5 - optional (backward-compatible: existing callers that
        # don't pass correlation_llm just skip this and keep today's
        # behavior). Generalizes corroborate()'s stage-equality lookup with
        # LLM reasoning over content - see phase3_voc.correlate_with_llm.
        if correlation_llm is not None:
            correlate_with_llm(kept, voc, correlation_llm)
        state.voc = voc

    state.findings = kept + voc_findings
    state.findings_rejected = rejected
    state.drilldown_trail = trail
    state.status = "scanning_code"
    return state
