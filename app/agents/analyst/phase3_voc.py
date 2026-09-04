"""Analyst phase 3 — VoC corroboration + threshold escalation.

Single primary theme per review: the journey's lexicon is PRIORITY-ORDERED and
the first matching theme wins, so counts cannot double-trigger. Any theme with
MORE than `escalation_threshold` negative reviews in the window becomes a
VoC-originated Finding routed to Code Scout with the theme's pre-derived
search terms. Its evidence IS the review count — a countable fact; funnel
magnitudes stay warehouse-only (bias rule). Quotes carry rating+date, never
identity (PII scrubbed at ingest by the Fetcher).

Phase 3.5 (correlate_with_llm, 2026-09-04) generalizes corroborate() beyond
its pre-configured theme->routing_stage lookup - see that function's
docstring for why the lookup alone misses real correlations.
"""
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Callable, Optional

from app.agents.scope_resolver import _MIN_TOKEN, _shares_stem, _tokens

from app.schemas.contracts import Finding, Voc, VocQuote

NEGATIVE_MAX_SCORE = 2
POSITIVE_MIN_SCORE = 4  # mirrors semantic_voc.POSITIVE_MIN_SCORE


def classify_review(text: str, themes: list[dict]) -> str:
    t = (text or "").lower()
    for theme in themes:  # priority order — first match wins
        if any(kw.lower() in t for kw in theme["keywords"]):
            return theme["name"]
    return "unmapped"


def is_foreign_journey_review(text: str, own_keywords: set[str], other_keywords: set[str]) -> bool:
    """True if `text` names another journey's own vocabulary (journey_keywords)
    and not this journey's — i.e. it almost certainly isn't about this journey,
    even when it also happens to match a theme this journey shares with others
    verbatim (app/technical, payment/refund, price: byte-identical keyword
    lists were found copy-pasted across homecare/consultation/pd_checkout/
    digital_clinic.yaml, 2026-09-05).

    Every journey shares one Play Store app, so this is a real, unavoidable
    corpus, not a bug in the Fetcher — the shared reviews are genuinely about
    the whole app. Reproduced live: with homecare's own theme keywords (nurse,
    perawat, kunjungan) matching ZERO of the 600 fixture reviews, its only
    themes with real volume were the shared ones, and the same top-3-by-thumbs
    reviews landed in "Users say" for homecare and consultation both, because
    the underlying corpus is ~44% consultation content (dokter/konsul) that
    incidentally also mentions "aplikasi"/"error". A review clearly about
    another flow should not inflate this journey's shared-theme count or be
    quoted as if it were evidence about this journey — even though the theme
    name and keyword list are identical.

    A review naming BOTH journeys' vocabulary (own_keywords wins) is kept: it
    may genuinely be a crosscutting complaint, and this filter's job is only
    to reject a review that names some OTHER flow and nothing about this one.

    Reuses scope_resolver.pick_journey()'s own word-matching, not a plain `kw
    in text` substring check: pd_checkout's journey_keywords include the bare
    abbreviation "pd", which a substring check matched inside "update" — every
    review mentioning an app update looked "about" pd_checkout and skipped
    this filter entirely. A 4-char prefix-stem match still catches Indonesian
    inflection ("dokter" in "dokternya"); short keywords like "pd" fall back
    to exact-word equality instead of substring containment.
    """
    words = _tokens(text) | set(re.findall(r"[a-z]{2,3}\b", (text or "").lower()))

    def matches(keywords: set[str]) -> bool:
        return any(_shares_stem(kw, w) if len(kw) >= _MIN_TOKEN else kw == w
                   for kw in keywords for w in words)

    if matches(own_keywords):
        return False
    return matches(other_keywords)


def filter_by_days(reviews: list[dict], days: Optional[int]) -> tuple[list[dict], dict]:
    """Keep reviews from the last `days`, measured from the newest review present.

    Anchored to the corpus, not to wall-clock now(): the fixture is a frozen
    capture, and anchoring to today would silently return nothing the moment it
    ages past the window. Returns the real span alongside, so a report can state
    what it actually looked at rather than what was asked for.
    """
    if not days:
        return reviews, {}
    dated = [(str(r.get("at") or "")[:10], r) for r in reviews]
    dated = [(d, r) for d, r in dated if len(d) == 10]
    if not dated:
        return reviews, {"review_window_days": days, "note": "no dated reviews; window ignored"}
    newest = max(d for d, _ in dated)
    y, m, dd = (int(x) for x in newest.split("-"))
    cutoff = date(y, m, dd) - timedelta(days=days - 1)
    kept = [r for d, r in dated if date(*(int(x) for x in d.split("-"))) >= cutoff]
    return kept, {"review_window_days": days, "review_window_from": cutoff.isoformat(),
                  "review_window_to": newest, "reviews_in_window": len(kept),
                  "reviews_available": len(reviews)}


def run_voc(reviews: list[dict], journey_voc_cfg: dict, next_rank: int,
            themes_per_review: Optional[list[str]] = None,
            extra_meta: Optional[dict] = None,
            own_journey_keywords: Optional[list[str]] = None,
            other_journey_keywords: Optional[list[str]] = None) -> tuple[list[Finding], Voc]:
    themes_cfg = journey_voc_cfg["themes"]
    threshold = int(journey_voc_cfg.get("escalation_threshold", 20))
    by_theme_cfg = {t["name"]: t for t in themes_cfg}
    own_kw = {k.lower() for k in (own_journey_keywords or [])}
    other_kw = {k.lower() for k in (other_journey_keywords or [])}

    # themes_per_review lets the caller supply semantic classifications (one per
    # review, same order); without it we fall back to the keyword lexicon.
    assigned = list(themes_per_review) if themes_per_review else None
    negatives, neg_themes = [], []
    excluded_foreign = 0
    for i, r in enumerate(reviews):
        if r.get("score", 5) > NEGATIVE_MAX_SCORE:
            continue
        if other_kw and is_foreign_journey_review(r.get("text", ""), own_kw, other_kw):
            excluded_foreign += 1
            continue
        negatives.append(r)
        neg_themes.append(assigned[i] if assigned and i < len(assigned)
                          else classify_review(r.get("text", ""), themes_cfg))

    buckets: dict[str, list[dict]] = defaultdict(list)
    for r, theme in zip(negatives, neg_themes):
        buckets[theme if theme in by_theme_cfg or theme == "unmapped" else "unmapped"].append(r)

    voc = Voc(
        reviews_meta={"total": len(reviews), "negatives": len(negatives),
                      "threshold": threshold, "excluded_foreign_journey": excluded_foreign,
                      **(extra_meta or {})},
        themes=[{"theme": name, "count": len(items),
                 "escalated": name != "unmapped" and len(items) > threshold}
                for name, items in sorted(buckets.items(), key=lambda kv: -len(kv[1]))],
    )

    findings: list[Finding] = []
    rank = next_rank
    for name, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        if name == "unmapped" or len(items) <= threshold:
            continue
        cfg = by_theme_cfg[name]
        top = sorted(items, key=lambda r: -r.get("thumbs", 0))[:3]
        quotes = [f"[{q.get('score')}★ {str(q.get('at',''))[:10]}] {q.get('text','')[:180]}"
                  for q in top]
        findings.append(Finding(
            rank=rank, origin="voc", stage=cfg["routing_stage"],
            hypothesis=(f"{len(items)} of {len(negatives)} negative Play Store reviews in the "
                        f"window share the theme '{name}' — users repeatedly report this problem"),
            confidence="high" if len(items) >= 2 * threshold else "medium",
            confirm_via=("Correlate the reviews' dates/app versions with the matching funnel "
                         "segment; then A/B the proposed fix and watch the theme count fall"),
            theme=name,
            theme_search_terms=list(cfg.get("search_terms", [])),
            review_count=len(items),
            top_quotes=quotes,
        ))
        voc.per_finding_quotes[str(rank)] = [
            VocQuote(rating=q.get("score", 1), date=str(q.get("at", ""))[:10],
                     text=q.get("text", "")[:300], theme=name) for q in top]
        rank += 1
    return findings, voc


def run_positive_voc(reviews: list[dict], positive_themes_cfg: list[dict],
                     themes_per_review: Optional[list[str]] = None) -> list[dict]:
    """Buckets PRAISE reviews (score >= POSITIVE_MIN_SCORE) into praise
    themes — the mirror of run_voc(), for GROWTH rather than diagnosis
    (2026-09-04). Unlike run_voc(), this never escalates into a Finding: a
    compliment is not a drop-off to route to Code Scout. It exists purely as
    grounding for the Analyst's growth_ideas step (phase2.py) — what users
    already love, so a new feature can be proposed as an extension of a
    proven strength instead of a guess. Same "single primary theme, priority
    order, closed taxonomy" discipline as run_voc(), just with no escalation
    threshold or routed Finding at the end.
    """
    by_theme_cfg = {t["name"]: t for t in positive_themes_cfg}
    assigned = list(themes_per_review) if themes_per_review else None
    positives, pos_themes = [], []
    for i, r in enumerate(reviews):
        if r.get("score", 0) < POSITIVE_MIN_SCORE:
            continue
        positives.append(r)
        pos_themes.append(assigned[i] if assigned and i < len(assigned)
                          else classify_review(r.get("text", ""), positive_themes_cfg))

    buckets: dict[str, list[dict]] = defaultdict(list)
    for r, theme in zip(positives, pos_themes):
        buckets[theme if theme in by_theme_cfg or theme == "unmapped" else "unmapped"].append(r)

    return [
        {"theme": name, "count": len(items),
         "sample_quotes": [f"[{q.get('score')}★ {str(q.get('at', ''))[:10]}] {q.get('text', '')[:180]}"
                           for q in items[:3]]}
        for name, items in sorted(buckets.items(), key=lambda kv: -len(kv[1]))
        if name != "unmapped"
    ]


CORROBORATION_FLOOR = 5  # fewer matching reviews than this is noise, not corroboration


def corroborate(warehouse_findings: list[Finding], voc: Voc,
                journey_voc_cfg: dict) -> None:
    """Attach VoC corroboration to warehouse findings whose routing stage has a
    matching theme cluster. Several themes can share one routing stage — pick
    the LARGEST cluster, and only attach when it clears the floor (a 3-review
    cluster corroborates nothing). Mutates findings in place."""
    counts = {t["theme"]: t["count"] for t in voc.themes}
    best_by_stage: dict[str, tuple[str, int]] = {}
    for theme in journey_voc_cfg["themes"]:
        n = counts.get(theme["name"], 0)
        stage = theme["routing_stage"]
        if n >= CORROBORATION_FLOOR and n > best_by_stage.get(stage, ("", 0))[1]:
            best_by_stage[stage] = (theme["name"], n)
    for f in warehouse_findings:
        hit = best_by_stage.get(f.stage)
        if hit and not f.theme:
            f.theme, f.review_count = hit


CORRELATION_FLOOR = CORROBORATION_FLOOR  # PR #12 review point 4 (Nakul): originally
                        # set lower than CORROBORATION_FLOOR on the theory that
                        # reasoning deserves more benefit of the doubt than a blind
                        # count match. That's the more debatable assumption, not the
                        # safer one - an LLM has no particular immunity to a false
                        # positive on thin data, and this codebase is consistently
                        # conservative about evidence thresholds elsewhere (k>=25
                        # suppression, the 20-review escalation floor). Keeping both
                        # passes at the same floor is the more defensible default.
CORRELATION_BUDGET = 3  # LLM calls spent on this pass, mirrors phase2's
                        # drill-down budget discipline - only the top-N
                        # still-uncorrelated findings get a reasoning pass,
                        # not every finding in the run.

VocCorrelationLLM = Callable[[dict[str, Any]], dict[str, Any]]  # context -> parsed model output


def correlate_with_llm(
    warehouse_findings: list[Finding],
    voc: Voc,
    llm: VocCorrelationLLM,
    budget: int = CORRELATION_BUDGET,
) -> None:
    """Phase 3.5 - LLM-driven correlation, run AFTER corroborate()'s cheap
    deterministic pass, on whatever it left untouched.

    corroborate() only attaches a VoC theme to a warehouse finding when both
    share the EXACT same pre-configured routing_stage. That misses real
    correlations whenever nobody wired that mapping ahead of time - e.g. a
    "chat won't open" theme filed under `re_engagement` never corroborates a
    `consultation`-stage cart drop, even if they plausibly describe the same
    underlying failure, because the YAML never said routing_stage: consultation
    for that theme. This pass reasons about the actual CONTENT - the finding's
    hypothesis text vs. each theme's name/count/sample quotes - instead of
    stage-name equality, so it can catch what the lookup table can't.

    Deliberately conservative:
      - Only touches findings corroborate() left untouched (`not f.theme`) -
        never overrides a deterministic match with a probabilistic one.
      - Only spends the LLM budget on the top `budget` such findings by rank
        (mirrors phase2's drill-down budget discipline) - not unbounded calls.
      - Only sees ALREADY-aggregated theme data (name/count/top_quotes) - no
        raw review text, no PII, matching the Fetcher's scrubbing guarantee.
      - A theme the LLM names that doesn't actually exist in `voc.themes` is
        discarded, not trusted - never fabricate a correlation the data can't
        back up.
      - Sets `correlation_rationale` (see contracts.py) so the reasoning is
        auditable, not a black-box mutation like corroborate()'s lookup hit.

    Sphere use case: "voc-funnel-correlation" (config.llm_use_case_voc_correlation)
    - NOT YET PROVISIONED in AI Studio project 7121 as of 2026-09-04; needs the
    same setup code-gap-assessment got. The prompt (server-side, in the
    template) should: given one warehouse finding's hypothesis/stage and a
    list of VoC themes with counts and sample quotes, decide whether any theme
    plausibly describes the same underlying user problem, and say why - never
    claim a match without being able to point at what in the theme's quotes
    justifies it.
    """
    candidates = [f for f in warehouse_findings if not f.theme][:budget]
    if not candidates:
        return

    theme_summaries = [
        t for t in voc.themes if t["theme"] != "unmapped" and t["count"] >= CORRELATION_FLOOR
    ]
    if not theme_summaries:
        return

    quotes_by_theme = _quotes_by_theme(voc)
    themes_by_name = {t["theme"]: t for t in voc.themes}

    for f in candidates:
        ctx = {
            "warehouse_finding": {"stage": f.stage, "hypothesis": f.hypothesis},
            "voc_themes": [
                {**t, "sample_quotes": quotes_by_theme.get(t["theme"], [])}
                for t in theme_summaries
            ],
        }
        out = llm(ctx) or {}
        if not out.get("correlated"):
            continue
        theme_name = out.get("theme")
        matched = themes_by_name.get(theme_name)
        if not matched:
            continue  # LLM named a theme the data doesn't have - don't trust it
        f.theme = theme_name
        f.review_count = matched["count"]
        f.correlation_rationale = out.get("rationale")


def _quotes_by_theme(voc: Voc, limit: int = 3) -> dict[str, list[str]]:
    """voc.per_finding_quotes is keyed by finding rank, not theme - regroup
    by theme so the LLM sees quotes per theme regardless of which specific
    finding they were originally attached to."""
    by_theme: dict[str, list[str]] = defaultdict(list)
    for quotes in voc.per_finding_quotes.values():
        for q in quotes:
            if len(by_theme[q.theme]) < limit:
                by_theme[q.theme].append(q.text)
    return dict(by_theme)
