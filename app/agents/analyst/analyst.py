"""Agent 2 — the Analyst node. Ties phases 1-3 into one RunState transition.

Reads journey config; consumes state.snapshot (Fetcher's output) plus the
journey's cohort-cut fixture (via AggregateTool) and PII-scrubbed reviews;
writes state.findings / state.drilldown_trail / state.voc. Fails loudly:
any exception marks the run failed with failed_stage="analyzing" upstream.
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

from app.agents.analyst import phase1
from app.agents.analyst.aggregate_tool import AggregateTool
from app.agents.analyst.journey_events import journey_events_for
from app.agents.analyst.phase2 import run_drilldown
from app.agents.analyst import phase3_voc
from app.agents.analyst.phase3_voc import corroborate, correlate_with_llm, run_voc
from app.agents.analyst.semantic_voc import POSITIVE_MIN_SCORE, classify_reviews
from app.agents.analyst.validator import collect_numbers, filter_findings
from app.journeys import all_journeys, load_journey
from app.schemas.contracts import RunState, validate_routing_stage

logger = logging.getLogger(__name__)

FIXTURES = Path("fixtures")

# PR #22 review (Nakul): on the fixture corpus, 499 of 600 reviews score >= 4
# - 25 batches at BATCH_SIZE=20, 5 sequential rounds at PARALLEL_BATCHES=5,
# ~200s, dwarfing the ~40s (1 round) the complaint pass has always cost.
# Running the two passes concurrently (below) stopped them ADDING, but the
# positive pass's own 5-round cost was still the Analyst's new critical path.
# Capping the sample to the most-recent reviews - plenty to ground a growth
# idea - brings it to the same 1-round scale as the complaint pass.
MAX_POSITIVE_SAMPLE = 60


def _score(review: dict) -> int:
    try:
        return int(review.get("score", 5))
    except (TypeError, ValueError):
        return 5


def _most_recent_positive(reviews: list[dict], limit: int = MAX_POSITIVE_SAMPLE) -> list[dict]:
    positive = [r for r in reviews if _score(r) >= POSITIVE_MIN_SCORE]
    positive.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    return positive[:limit]


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
    # The mirror of `gap`: the best-converting transition, decided the same
    # deterministic way — growth_ideas' positive funnel grounding (2026-09-04).
    top_strength = phase1.strongest_stage(table)
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
    positive_voc_signals: list = []
    if reviews is None and state.demo_mode:
        rv_path = FIXTURES / state.journey / "reviews_scrubbed.json"
        reviews = json.loads(rv_path.read_text()) if rv_path.exists() else []
    if reviews:
        reviews, window_meta = phase3_voc.filter_by_days(reviews, state.scope.review_days)
        positive_themes_cfg = cfg["voc"].get("positive_themes") or []
        # Sampled, not the full positive set - see MAX_POSITIVE_SAMPLE above
        # (PR #22 review: 499 of 600 reviews on the fixture score >= 4, which
        # is 5x the complaint pass's own batch count for a signal that only
        # grounds an optional growth idea, never a Finding).
        positive_sample = _most_recent_positive(reviews) if positive_themes_cfg else []

        # Negative and positive classification are fully independent (different
        # taxonomies, different score bands, and now different-sized inputs) -
        # run them CONCURRENTLY rather than sequentially, so the optional pass
        # never adds to the mandatory one's wall-clock time.
        def _classify_negative():
            return classify_reviews(voc_llm, reviews, cfg["voc"]["themes"], phase3_voc.classify_review,
                                    scope_hint=state.scope.prompt)

        def _classify_positive():
            if not positive_sample:
                return [], {}
            return classify_reviews(voc_llm, positive_sample, positive_themes_cfg, phase3_voc.classify_review,
                                    scope_hint=state.scope.prompt, polarity="positive")

        with ThreadPoolExecutor(max_workers=2) as pool:
            negative_future = pool.submit(_classify_negative)
            positive_future = pool.submit(_classify_positive)
            themes_per_review, voc_meta = negative_future.result()
            # PR #22 review (Nakul): praise classification is grounding for
            # growth_ideas, never load-bearing - it must never take the
            # mandatory complaint pass and the whole funnel analysis down
            # with it. classify_reviews() already guards each SPHERE BATCH
            # internally; this guards everything else that can go wrong
            # around it (a malformed positive_themes_cfg entry, for example).
            try:
                positive_per_review, _positive_meta = positive_future.result()
            except Exception as exc:
                logger.warning("positive VoC pass failed (%s) — continuing without praise signals", exc)
                positive_per_review, _positive_meta = [], {}

        own_journey_keywords = cfg.get("journey_keywords") or []
        other_journey_keywords = [
            kw for name, other_cfg in all_journeys().items() if name != state.journey
            for kw in (other_cfg.get("journey_keywords") or [])
        ]
        voc_findings, voc = run_voc(reviews, cfg["voc"], 1,
                                    themes_per_review=themes_per_review,
                                    extra_meta={**window_meta,
                                                "classifier": voc_meta["classifier"]},
                                    own_journey_keywords=own_journey_keywords,
                                    other_journey_keywords=other_journey_keywords)
        # Praise reviews (2026-09-04): the mirror of the complaint pass above.
        # Never escalates into a Finding — see phase3_voc.run_positive_voc —
        # this exists purely as growth_ideas' second real grounding source
        # (what users already love), alongside top_strength.
        if positive_sample:
            positive_voc_signals = phase3_voc.run_positive_voc(
                positive_sample, positive_themes_cfg, themes_per_review=positive_per_review)
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
    findings, trail, growth_ideas = run_drilldown(
        llm, tool, gap or {}, summary, routing_keys,
        _default_routing_for_gap(gap or {}, cfg), voc_signals=voc_signals,
        top_strength=top_strength, positive_voc_signals=positive_voc_signals,
        user_question=state.scope.prompt, user_intent=state.scope.intent,
        labels={"stage_labels": cfg.get("stage_labels") or {}, "dimension_labels": cfg.get("dimension_labels") or {}})

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
    state.growth_ideas = growth_ideas
    state.top_gap_to_stage = (gap or {}).get("to_stage")
    state.status = "scanning_code"
    return state
