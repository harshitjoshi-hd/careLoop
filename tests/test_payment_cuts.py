"""The payments cuts (added 2026-09-04 from dwh.fact_scrooge_payments) exist on
both journeys, reconcile with the snapshot, and behave as designed: the
method cut is rate-bearing over ATTEMPTS only; the funnel cut is distribution
only and names the pre-payment loss explicitly."""
import json

import pytest

from app.agents.analyst.aggregate_tool import AggregateTool
from app.journeys import load_journey


@pytest.mark.parametrize("journey,expected_never_paid", [("pd_checkout", 412477), ("consultation", 83449)])
def test_payment_cuts_present_and_shaped(journey, expected_never_paid):
    cfg = load_journey(journey)
    cuts = json.load(open(f"fixtures/{journey}/cohort_cuts.json"))
    tool = AggregateTool(cuts, cfg["drilldown_dimensions"])
    assert "payment_method" in tool.rate_bearing_dimensions
    assert "payment_funnel" in tool.dimensions_with_data and "payment_funnel" not in tool.rate_bearing_dimensions

    method = tool.aggregate("confirmed", "payment_method")
    assert all("rate" in r and r["rate"] <= 1 for r in method["rows"])
    assert all(r["entered"] >= 25 for r in method["rows"])              # k floor honoured at source

    funnel = tool.aggregate("confirmed", "payment_funnel")
    never = next(r for r in funnel["rows"] if r["segment"] == "never reached payment")
    assert never["entered"] == expected_never_paid and "rate" not in never


def test_pd_payment_funnel_reconciles_with_the_snapshot():
    cuts = json.load(open("fixtures/pd_checkout/cohort_cuts.json"))
    snap = json.load(open("fixtures/pd_checkout/snapshot.json"))
    created = next(s["entered"] for s in snap["stages"] if s["stage"] == "created")
    total = sum(r["entered"] for r in cuts["payment_funnel"]["rows"])
    assert abs(total - created) < 0.001 * created          # 647,198 vs 647,191: k-floor drops only


def test_payment_aliases_cover_how_people_ask():
    for journey in ("pd_checkout", "consultation"):
        aliases = load_journey(journey)["dimension_aliases"]
        assert {"wallet", "bank transfer", "qris"} <= set(aliases["payment_method"])
        assert {"insurance", "voided", "refund"} <= set(aliases["payment_funnel"])
