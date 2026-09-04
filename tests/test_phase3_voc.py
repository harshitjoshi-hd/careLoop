"""VoC classification + escalation against the REAL scrubbed 600-review fixture."""
from app.agents.analyst.phase3_voc import classify_review, is_foreign_journey_review, run_voc


def test_single_primary_theme_priority_order(journey_cfg):
    themes = journey_cfg["voc"]["themes"]
    # mentions both payment AND doctor -> payment/refund wins (higher priority)
    t = classify_review("sudah bayar tapi dokter tidak menjawab", themes)
    assert t == "payment/refund"


def test_real_fixture_escalations(reviews, journey_cfg):
    findings, voc = run_voc(reviews, journey_cfg["voc"], next_rank=1)
    escalated = {f.theme: f.review_count for f in findings}
    # golden: payment/refund (41) and consultation/doctor (21) cross threshold 20
    assert escalated.get("payment/refund") == 41
    assert escalated.get("consultation/doctor") == 21
    assert len(findings) == 2  # delivery(9), app/technical(8) etc. stay below
    for f in findings:
        assert f.origin == "voc" and f.review_count > 20
        assert f.theme_search_terms  # pre-derived for Code Scout
        assert f.top_quotes and all("@" not in q for q in f.top_quotes)  # scrubbed


def test_escalation_routes_to_journey_categories(reviews, journey_cfg):
    findings, _ = run_voc(reviews, journey_cfg["voc"], next_rank=1)
    stages = {f.theme: f.stage for f in findings}
    assert stages["payment/refund"] == "payments"
    assert stages["consultation/doctor"] == "consultation"


# ---- is_foreign_journey_review (2026-09-05) — a review clearly about ANOTHER
# journey must not inflate or supply quotes for a theme this journey shares
# with others (app/technical, payment/refund, price were found byte-identical
# across every journey.yaml). Reproduced live: homecare's own themes (nurse,
# perawat) matched zero of the 600 fixture reviews, so its "Users say" section
# fell back to the exact same payment/refund quotes consultation was already
# showing — one of which was literally "paid but couldn't get a consultation".

def test_a_review_naming_another_journeys_vocabulary_is_foreign():
    assert is_foreign_journey_review(
        "sudah bayar tapi tidak bisa konsultasi dengan dokter",
        own_keywords={"nurse", "perawat"}, other_keywords={"dokter", "konsultasi"},
    )


def test_a_review_naming_this_journeys_own_vocabulary_is_not_foreign_even_if_it_also_names_another():
    # Own vocabulary wins — a genuinely crosscutting complaint is kept.
    assert not is_foreign_journey_review(
        "perawat datang tapi dokter tidak menjawab chat",
        own_keywords={"perawat"}, other_keywords={"dokter"},
    )


def test_a_review_naming_neither_journeys_vocabulary_is_not_foreign():
    assert not is_foreign_journey_review(
        "aplikasi error terus, tolong diperbaiki",
        own_keywords={"perawat"}, other_keywords={"dokter"},
    )


def test_short_keyword_does_not_false_positive_inside_an_unrelated_word():
    # pd_checkout's own journey_keywords include the bare abbreviation "pd" —
    # a substring check matched it inside "update", making every review that
    # mentions an app update look like it was "about" pd_checkout.
    assert not is_foreign_journey_review(
        "ada update terbaru tapi masih error", own_keywords={"pd"}, other_keywords=set(),
    )
    assert is_foreign_journey_review(
        "ada update terbaru tapi masih error", own_keywords=set(), other_keywords={"error"},
    )


def test_inflected_indonesian_forms_still_match_via_the_shared_stem_rule():
    # "dokternya" ("the/his/her doctor") must still match "dokter" — a strict
    # whole-word check would miss Indonesian's routine suffixing and silently
    # stop excluding the vast majority of real foreign-journey reviews.
    assert is_foreign_journey_review(
        "sudah bayar tapi dokternya tidak menjawab",
        own_keywords=set(), other_keywords={"dokter"},
    )


def test_run_voc_excludes_a_foreign_journey_review_from_a_shared_theme():
    themes = [{"name": "app/technical", "routing_stage": "homecare",
               "search_terms": ["error"], "keywords": ["error", "aplikasi"]}]
    cfg = {"themes": themes, "escalation_threshold": 0}
    reviews = [
        {"text": "aplikasi error saat booking perawat", "score": 1, "thumbs": 0, "at": "2026-01-01"},
        # Clearly about consultation, not homecare — incidentally also says "aplikasi".
        {"text": "aplikasi error, dokter tidak bisa dihubungi", "score": 1, "thumbs": 5, "at": "2026-01-02"},
    ]
    findings_without_filter, _ = run_voc(reviews, cfg, next_rank=1)
    assert findings_without_filter[0].review_count == 2  # both counted, no filter applied

    findings_with_filter, voc = run_voc(
        reviews, cfg, next_rank=1,
        own_journey_keywords=["perawat"], other_journey_keywords=["dokter"],
    )
    assert findings_with_filter[0].review_count == 1  # the consultation review is excluded
    assert "dokter" not in findings_with_filter[0].top_quotes[0]
    assert voc.reviews_meta["excluded_foreign_journey"] == 1


def test_run_voc_with_no_keyword_args_behaves_exactly_as_before():
    """Backward compatibility: every existing caller that doesn't pass the new
    keyword args must see identical behavior — no accidental filtering."""
    themes = [{"name": "app/technical", "routing_stage": "homecare",
               "search_terms": ["error"], "keywords": ["error"]}]
    cfg = {"themes": themes, "escalation_threshold": 1}
    reviews = [
        {"text": "error terus, dokter juga tidak bisa dihubungi", "score": 1, "thumbs": 0, "at": "2026-01-01"},
        {"text": "error lagi", "score": 1, "thumbs": 0, "at": "2026-01-02"},
    ]
    findings, voc = run_voc(reviews, cfg, next_rank=1)
    assert findings[0].review_count == 2
    assert voc.reviews_meta["excluded_foreign_journey"] == 0


def test_corroboration_picks_largest_theme_and_respects_floor(reviews, journey_cfg):
    from app.agents.analyst.phase3_voc import corroborate, run_voc
    from app.schemas.contracts import EvidenceItem, Finding
    _, voc = run_voc(reviews, journey_cfg["voc"], next_rank=10)
    wh = [Finding(rank=1, origin="warehouse", stage="pharmacy_checkout",
                  hypothesis="h", confidence="high",
                  confirm_via="run the confirming experiment please",
                  evidence=[EvidenceItem(type="snapshot", metric="m", value=1.0)])]
    corroborate(wh, voc, journey_cfg["voc"])
    # pharmacy_checkout themes: app/technical (8) and price (3).
    # 8 >= floor -> app/technical wins; price(3) must never attach.
    assert wh[0].theme == "app/technical" and wh[0].review_count == 8


# ---- Phase 3.5: LLM-driven correlation (2026-09-04) ----
# corroborate() only matches same-stage; these prove the LLM pass catches
# real cross-stage correlations the deterministic lookup structurally can't,
# without ever overriding a deterministic hit or trusting a fabricated theme.

def test_correlate_with_llm_catches_a_cross_stage_correlation(reviews, journey_cfg):
    from app.agents.analyst.phase3_voc import corroborate, correlate_with_llm, run_voc
    from app.schemas.contracts import Finding
    _, voc = run_voc(reviews, journey_cfg["voc"], next_rank=10)

    # payment/refund (41 reviews) is pre-mapped to routing_stage "payments" -
    # a "re_engagement"-stage finding can NEVER corroborate against it via
    # stage equality, no matter how large the cluster is.
    wh = [Finding(rank=1, origin="warehouse", stage="re_engagement",
                  hypothesis="Users who abandon mid-checkout after a failed payment never "
                             "return, even after a re-engagement reminder push.",
                  confidence="medium", confirm_via="check reminder click-through vs completion")]
    corroborate(wh, voc, journey_cfg["voc"])
    assert wh[0].theme is None  # confirms the deterministic pass genuinely misses this

    def stub_llm(ctx: dict) -> dict:
        assert ctx["warehouse_finding"]["stage"] == "re_engagement"
        assert any(t["theme"] == "payment/refund" for t in ctx["voc_themes"])
        return {
            "correlated": True,
            "theme": "payment/refund",
            "rationale": "Users abandoning after a failed payment never return even to a "
                         "reminder - same root cause as the payment/refund complaints.",
        }

    correlate_with_llm(wh, voc, stub_llm)
    assert wh[0].theme == "payment/refund"
    assert wh[0].review_count == 41
    assert wh[0].correlation_rationale


def test_correlate_with_llm_never_overrides_a_deterministic_match(reviews, journey_cfg):
    from app.agents.analyst.phase3_voc import corroborate, correlate_with_llm, run_voc
    from app.schemas.contracts import Finding
    _, voc = run_voc(reviews, journey_cfg["voc"], next_rank=10)
    wh = [Finding(rank=1, origin="warehouse", stage="pharmacy_checkout",
                  hypothesis="h", confidence="high", confirm_via="c")]
    corroborate(wh, voc, journey_cfg["voc"])
    assert wh[0].theme == "app/technical"

    def fail_llm(ctx: dict) -> dict:
        raise AssertionError("must never be called - this finding already has a theme")

    correlate_with_llm(wh, voc, fail_llm)
    assert wh[0].theme == "app/technical"  # untouched


def test_correlate_with_llm_discards_a_theme_name_the_data_does_not_have(reviews, journey_cfg):
    from app.agents.analyst.phase3_voc import correlate_with_llm, run_voc
    from app.schemas.contracts import Finding
    _, voc = run_voc(reviews, journey_cfg["voc"], next_rank=10)
    wh = [Finding(rank=1, origin="warehouse", stage="re_engagement",
                  hypothesis="h", confidence="low", confirm_via="c")]

    def hallucinating_llm(ctx: dict) -> dict:
        return {"correlated": True, "theme": "does-not-exist", "rationale": "made up"}

    correlate_with_llm(wh, voc, hallucinating_llm)
    assert wh[0].theme is None  # never trust a theme name the data doesn't back up


def test_correlate_with_llm_respects_its_call_budget(reviews, journey_cfg):
    from app.agents.analyst.phase3_voc import correlate_with_llm, run_voc
    from app.schemas.contracts import Finding
    _, voc = run_voc(reviews, journey_cfg["voc"], next_rank=10)
    wh = [Finding(rank=i, origin="warehouse", stage="re_engagement",
                  hypothesis=f"h{i}", confidence="low", confirm_via="c") for i in range(1, 6)]

    calls = []

    def counting_llm(ctx: dict) -> dict:
        calls.append(ctx)
        return {"correlated": False}

    correlate_with_llm(wh, voc, counting_llm, budget=2)
    assert len(calls) == 2
