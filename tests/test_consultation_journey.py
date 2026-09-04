"""Journey #2 — online consultation. Golden asserts on the frozen fixture."""
import json
from pathlib import Path

from app.agents.analyst import phase1
from app.agents.analyst.aggregate_tool import AggregateTool
from app.journeys import load_journey
from app.schemas.contracts import Snapshot

FIX = Path(__file__).parent.parent / "fixtures" / "consultation"
CFG = load_journey("consultation")
SNAP = Snapshot(**json.loads((FIX / "snapshot.json").read_text()))
CUTS = json.loads((FIX / "cohort_cuts.json").read_text())


def _rate(dim, seg):
    tool = AggregateTool(CUTS, CFG["drilldown_dimensions"])
    rows = {r["segment"]: r for r in tool.aggregate("confirmed", dim)["rows"]}
    return rows[seg]["rate"]


def test_the_payment_step_is_the_whole_story():
    table = phase1.funnel_table(SNAP, CFG["stages"])
    gap = phase1.largest_drop(table)
    assert (gap["from_stage"], gap["to_stage"]) == ("created", "confirmed")
    assert gap["lost"] == 87511 and abs(gap["share_of_prev"] - 0.3862) < 0.001
    # after confirmation the pipeline is tight — this must NOT become a finding
    by = {r["stage"]: r for r in table}
    assert by["started"]["conversion_from_previous"] > 0.96


def test_abandoned_by_system_dominates_the_recorded_reasons():
    """54,015 of 87,511 lost consults are the timer-driven abandon script —
    the mechanism hand-verified at ConsultationDao.java:146 before any code."""
    clusters = phase1.cluster_reasons(SNAP.reasons, CFG["artifact_reasons"])
    user = clusters["user"] if "user" in clusters else clusters
    flat = json.dumps(clusters)
    assert "abandoned by system" in flat and "54015" in flat


def test_every_consultation_cut_is_rate_bearing():
    tool = AggregateTool(CUTS, CFG["drilldown_dimensions"])
    # payment_funnel is the one deliberately distribution-only cut: its segments
    # are defined by the payment outcome, so a "rate" would be tautological.
    assert set(tool.rate_bearing_dimensions) == set(CFG["drilldown_dimensions"]) - {"payment_funnel"}


def test_golden_rates():
    assert abs(_rate("payer", "cash") - 0.4653) < 0.001
    assert abs(_rate("payer", "insurance") - 0.7904) < 0.001          # -32.5pp vs cash
    assert abs(_rate("interface_type", "web") - 0.3559) < 0.001        # half of ios/android
    assert abs(_rate("scheduling", "scheduled") - 0.4229) < 0.001
    assert abs(_rate("hour_of_day", "22") - 0.4505) < 0.001
    assert abs(_rate("consultation_trigger", "pd_erx_consultation") - 1.0) < 0.001  # auto-confirmed


def test_consultation_code_hints_include_the_verified_mechanism():
    hints = CFG["code_hints"]["by_stage"]["consultation"]
    assert "GET_ABANDON_CONSULTATION" in hints and "abandon" in hints


def test_consultation_findings_route_to_the_consultation_service():
    """First live consultation run routed every finding to pharmacy_checkout:
    the fallback hardcoded that category, and this journey has it for the eRx
    hand-off. The journey now names its own default."""
    from app.agents.analyst.analyst import _default_routing_for_gap
    assert _default_routing_for_gap({}, CFG) == "consultation"
    assert _default_routing_for_gap({}, load_journey("pd_checkout")) == "pharmacy_checkout"
