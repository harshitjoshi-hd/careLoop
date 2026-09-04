"""Prompt -> constraints. Deterministic on purpose.

phase1.largest_drop picks the target gap by arithmetic, and that is why the
headline number cannot be argued into existence. A prompt therefore resolves
into constraints that are shown back for confirmation — it never reaches the
Analyst as prose to interpret.
"""
import json
from pathlib import Path

import pytest
import yaml

from app.agents.scope_resolver import describe, resolve_scope, pick_journey

ROOT = Path(__file__).parent.parent
CFG = yaml.safe_load((ROOT / "config/journeys/pd_checkout.yaml").read_text())
EVENTS = [e["event_name"] for e in
          json.loads((ROOT / "fixtures/pd_checkout/snapshot.json").read_text())["ct_events"]]
DIMS = CFG["drilldown_dimensions"]


def _resolve(prompt):
    return resolve_scope(prompt, CFG, EVENTS, DIMS)


def test_the_ask_from_the_brief_resolves_to_a_transition():
    """The exact sentence the feature was requested with."""
    s = _resolve("I want the data and analysis for why the users are dropping off "
                 "after adding items to cart")
    assert (s.from_stage, s.to_stage) == ("created", "confirmed")
    assert any("add_to_cart_button" in m for m in s.matched_on)
    assert not s.unresolved


def test_people_do_not_type_dimension_names():
    """Nobody types "stock_status" — the journey config carries the aliases."""
    s = _resolve("why do orders with unfulfilled items keep failing")
    assert "stock_status" in s.dimensions
    assert any("via 'unfulfilled'" in m for m in s.matched_on)


def test_a_day_range_takes_the_wider_bound():
    """"past 10-15 days" fetches 15 and reports the real span: answering a
    narrower question than the one asked is worse than answering a wider one."""
    assert _resolve("analyse the last 10-15 days of reviews").review_days == 15
    assert _resolve("reviews from the past 7 days").review_days == 7
    assert _resolve("the last 2 weeks of reviews").review_days == 14


def test_an_unrelated_ask_is_refused_not_guessed():
    # "insurance" used to be unrelated here; since the payments cut landed it is a
    # real alias (no payment needed: insurance), so the unrelated ask is another.
    s = _resolve("what is happening with warehouse staffing budgets")
    assert not s.is_scoped()
    assert s.unresolved
    assert "full funnel" in describe(s)


def test_nothing_outside_the_journey_can_be_invented():
    s = _resolve("cut this by user_phone_number and salary_band")
    assert s.dimensions == []


def test_an_empty_prompt_is_simply_unscoped():
    s = _resolve("")
    assert not s.is_scoped() and s.matched_on == []


def test_the_summary_is_something_a_human_can_reject():
    s = _resolve("why are users dropping off after adding items to cart, last 10 days")
    text = describe(s)
    assert "created to confirmed" in text and "last 10 days" in text


def test_routing_categories_and_drilldown_cuts_are_disjoint_vocabularies():
    """Two fields, two meanings — and they must stay apart.

    `CreateRunRequest.dimensions` names routing categories (payments,
    consultation, ...) and filters which findings SURFACE after the run — PR #9.
    `RunScope.dimensions` names drill-down cuts (stock_status, item_count, ...)
    and narrows what the Analyst EXPLORES — this branch. The vocabularies share
    no members, so copying one into the other either 422s at the category
    validator or empties the AggregateTool whitelist. This pins both facts.
    """
    routing = set(CFG["routing"])
    cuts = set(DIMS)
    assert routing.isdisjoint(cuts), routing & cuts

    # A category never leaks into the scope's drill-down list ...
    s = _resolve("check why users are dropping off during the payments")
    assert not (set(s.dimensions) & routing)
    # ... and everything the scope does put there is a real cut.
    assert set(s.dimensions) <= cuts


def test_the_journey_is_picked_from_the_prompt():
    from app.agents.scope_resolver import pick_journey
    from app.journeys import all_journeys
    js = all_journeys()
    assert pick_journey("why do consultations get abandoned before the doctor joins", js)[0] == "consultation"
    assert pick_journey("why are users dropping off after adding items to cart", js)[0] == "pd_checkout"
    # "payment" alone decides nothing — both journeys have a payment step
    assert pick_journey("why are payments failing", js)[0] == "pd_checkout"      # default
    assert pick_journey("", js) == ("pd_checkout", [])


def test_the_journey_word_does_not_name_a_dimension():
    """"consultations" must pick the consultation journey without also pinning
    the drill-down to `consultation_trigger` — that cut is the weakest one and
    the prompt never mentioned triggers."""
    from app.journeys import load_journey
    cfg = load_journey("consultation")
    events = list((cfg.get("event_stage") or {}).keys())
    s = resolve_scope("why do consultations get abandoned before the doctor joins",
                      cfg, events, cfg["drilldown_dimensions"])
    assert "consultation_trigger" not in s.dimensions
    assert (s.from_stage, s.to_stage) == ("created", "confirmed")
    # the alias still works when a user actually means it
    s2 = resolve_scope("compare instant versus erx-driven consults", cfg, events, cfg["drilldown_dimensions"])
    assert "consultation_trigger" in s2.dimensions


def test_a_request_verb_does_not_pick_a_journey_by_prefix():
    """"check why the users are not using insurance" picked pd_checkout via
    "check" ~ "checkout" (4-char stem). Journey keywords match whole words."""
    from app.journeys import all_journeys
    journey, hits = pick_journey("check why the users are not using insurance", all_journeys(), default="pd_checkout")
    assert hits == []                       # fell back to the default, and says so
    assert journey == "pd_checkout"
    journey, hits = pick_journey("why are users dropping off after adding items to cart", all_journeys(), default="pd_checkout")
    assert journey == "pd_checkout" and "cart" in hits


def test_payment_words_resolve_to_the_payment_cuts():
    from app.journeys import load_journey
    cfg = load_journey("pd_checkout"); events = list((cfg.get("event_stage") or {}).keys())
    s = resolve_scope("check why the users are not using insurance", cfg, events, cfg["drilldown_dimensions"])
    assert "payment_funnel" in s.dimensions
    s = resolve_scope("why do bank transfer payments fail at checkout", cfg, events, cfg["drilldown_dimensions"])
    assert "payment_method" in s.dimensions
    cfg = load_journey("consultation"); events = list((cfg.get("event_stage") or {}).keys())
    s = resolve_scope("do wallet payments convert better than cards for consultations", cfg, events, cfg["drilldown_dimensions"])
    assert "payment_method" in s.dimensions and "consultation_trigger" not in s.dimensions

