"""End-to-end Analyst node on fixtures with a stubbed LLM — the golden run."""
import json
from pathlib import Path

from app.agents.analyst.analyst import run_analyst
from app.schemas.contracts import RunState, Snapshot

FIX = Path(__file__).parent.parent / "fixtures" / "pd_checkout"


def test_golden_run(cohort_cuts, reviews):
    state = RunState(
        run_id=1, journey="pd_checkout",
        window_start="2026-08-27", window_end="2026-09-02",
        prev_window_start="2026-08-20", prev_window_end="2026-08-26",
        status="analyzing",
        snapshot=Snapshot(**json.loads((FIX / "snapshot.json").read_text())),
    )
    llm_script = iter([
        {"done": False, "next_question": {"dimension": "consultation_required",
                                          "rationale": "rx gating vs the 64% abandonment"}},
        {"done": True, "findings": [{
            "hypothesis": "rx-gated orders confirm at 30.0% vs 39.0% non-rx (-9pp)",
            "stage": "pharmacy_checkout", "confidence": "high",
            "evidence": ["255293", "76641", "391898", "152981"],
            "confirm_via": "A/B a prescription-cart resume flow; watch rx confirm rate"}]},
    ])
    # The exploration floor keeps asking after this script says done, so hold
    # the last response rather than raising StopIteration — the run must still
    # visit every rate-bearing cut before it is allowed to conclude.
    held = {}

    def llm(ctx):
        nonlocal held
        try:
            held = next(llm_script)
        except StopIteration:
            pass
        return held

    out = run_analyst(state, llm=llm, cohort_cuts=cohort_cuts, reviews=reviews)

    # warehouse finding survived the evidence gate
    wh = [f for f in out.findings if f.origin == "warehouse"]
    assert wh and wh[0].stage == "pharmacy_checkout"
    # journey_events populated from real ct_events (decision #11). The fixture
    # now carries the REAL dotted CT names, so "confirm" in the hypothesis
    # stems to the pharmacy confirm events rather than to a simplified alias.
    assert wh[0].journey_events
    assert all(e.startswith("pharmacy.") for e in wh[0].journey_events)
    assert any("confirm" in e for e in wh[0].journey_events)
    # VoC escalations appended after warehouse ranks. Neither theme escalates
    # any more (2026-09-05): the shared 600-review fixture is ~44%
    # consultation content that used to inflate pd_checkout's own
    # payment/refund and consultation/doctor buckets past the threshold;
    # is_foreign_journey_review() now excludes reviews that are clearly about
    # a different flow, and pd_checkout genuinely has too little of its own
    # signal in this fixture to escalate — an honest empty result, not a
    # broken one. See test_phase3_voc.py for the exclusion logic itself.
    voc = [f for f in out.findings if f.origin == "voc"]
    assert voc == []
    assert all(f.rank > wh[-1].rank for f in voc)
    # trail persisted, status advanced
    assert out.drilldown_trail and out.drilldown_trail[0].dimension == "consultation_required"
    assert out.status == "scanning_code"
    # Was 92 before is_foreign_journey_review() (2026-09-05) started excluding
    # negative reviews that are clearly about a different flow (38 of them,
    # mostly consultation content) from pd_checkout's own count — 54 is the
    # honest figure: negatives actually about this journey, not the raw
    # corpus size before journey attribution.
    assert out.voc.reviews_meta["negatives"] == 54
    assert out.voc.reviews_meta["excluded_foreign_journey"] == 38


def test_rejected_findings_are_reported_not_swallowed(cohort_cuts, reviews):
    """A live run once ended with zero warehouse findings and no explanation:
    the model's only finding cited prose, the gate dropped it, nothing recorded
    that. The rejection and its reason now travel with the run."""
    import json
    from app.schemas.contracts import RunState, Snapshot
    state = RunState(run_id=1, journey="pd_checkout", window_start="a", window_end="b",
                     status="analyzing",
                     snapshot=Snapshot(**json.loads((FIX / "snapshot.json").read_text())))
    llm = lambda ctx: {"done": True, "findings": [{
        "hypothesis": "insufficient data", "stage": "insufficient-data", "confidence": "low",
        "evidence": ["No funnel aggregates were provided."],
        "confirm_via": "provide the aggregates and re-run the analysis"}]}
    out = run_analyst(state, llm=llm, cohort_cuts=cohort_cuts, reviews=reviews)
    assert not [f for f in out.findings if f.origin == "warehouse"]
    assert out.findings_rejected and "no evidence" in out.findings_rejected[0]["reason"]


def test_drilldown_sees_voc_signals_and_voc_findings_rank_last(cohort_cuts):
    """The v7 prompt tells the model about voc_signals, so the drill-down must
    actually receive them: review themes are classified BEFORE phase 2 now.
    Ranks still put every VoC finding after every warehouse finding, and a
    review count is never accepted as warehouse evidence.

    Uses a small synthetic review set of its own rather than the shared
    600-review fixture: that fixture is ~44% consultation content that
    incidentally also says "aplikasi"/"error", and is_foreign_journey_review()
    (2026-09-05) now correctly excludes it from pd_checkout's own themes —
    the shared fixture no longer clears pd_checkout's escalation_threshold on
    its own. This test's actual subject is rank ordering, not that count, so
    it grounds the escalation in reviews unambiguously about payment instead.
    """
    import json
    from app.schemas.contracts import RunState, Snapshot
    state = RunState(run_id=1, journey="pd_checkout", window_start="a", window_end="b",
                     status="analyzing",
                     snapshot=Snapshot(**json.loads((FIX / "snapshot.json").read_text())))
    reviews = [
        {"text": f"gagal bayar terus, uang saya tidak kembali sama sekali (kasus {i})",
         "score": 1, "thumbs": 0, "at": "2026-08-29"}
        for i in range(25)
    ]
    seen = []

    def llm(ctx):
        seen.append(ctx)
        if len(seen) == 1:
            return {"done": False, "next_question": {"dimension": "consultation_required",
                                                     "rationale": "rx"}}
        return {"done": True, "findings": [
            {"hypothesis": "rx-gated orders confirm at 30.0% vs 39.0%", "stage": "pharmacy_checkout",
             "confidence": "high", "evidence": ["255293", "76641"], "confirm_via": "A/B a resume nudge"},
            {"hypothesis": "41 reviews say payment", "stage": "payments", "confidence": "high",
             "evidence": ["41"], "confirm_via": "correlate reviews with funnel"}]}

    out = run_analyst(state, llm=llm, cohort_cuts=cohort_cuts, reviews=reviews)

    signals = seen[0]["voc_signals"]
    assert signals and {"theme", "count", "escalated"} <= set(signals[0])
    assert all(s["theme"] != "unmapped" for s in signals)
    # the review-count "finding" was rejected: voc_signals is context, not evidence
    assert any("41 reviews" in r["finding"] for r in out.findings_rejected)
    wh = [f for f in out.findings if f.origin == "warehouse"]
    voc = [f for f in out.findings if f.origin == "voc"]
    assert wh and voc and min(f.rank for f in voc) > max(f.rank for f in wh)
