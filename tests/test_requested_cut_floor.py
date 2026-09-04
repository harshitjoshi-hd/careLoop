"""A dimension the user asked for is cut before the run may conclude, even a
distribution-only one. Run 28 was scoped to payment_funnel, the model said
done on turn 1 and the trail was empty."""
import json

from app.agents.analyst.aggregate_tool import AggregateTool
from app.agents.analyst.phase2 import run_drilldown
from app.journeys import load_journey


def _tool(whitelist):
    cuts = json.load(open("fixtures/pd_checkout/cohort_cuts.json"))
    return AggregateTool(cuts, whitelist)


GAP = {"from_stage": "created", "to_stage": "confirmed", "entered": 647191, "converted": 229622, "lost": 417569}


def test_a_requested_distribution_only_cut_is_made_before_concluding():
    eager = lambda ctx: {"done": True, "findings": []}
    _, trail = run_drilldown(eager, _tool(["payment_funnel"]), GAP, {}, ["pharmacy_checkout"], "pharmacy_checkout",
                             must_try=["payment_funnel"])
    assert [t.dimension for t in trail] == ["payment_funnel"]
    assert trail[0].question.startswith("requested cut")
    assert trail[0].note == "distribution_only"


def test_requested_cuts_come_before_the_rate_bearing_floor():
    eager = lambda ctx: {"done": True, "findings": []}
    _, trail = run_drilldown(eager, _tool(["payment_funnel", "item_count", "payment_method"]), GAP, {},
                             ["pharmacy_checkout"], "pharmacy_checkout", must_try=["payment_funnel"])
    dims = [t.dimension for t in trail]
    assert dims[0] == "payment_funnel"
    assert set(dims) >= {"payment_funnel", "item_count", "payment_method"}


def test_the_model_sees_what_was_requested():
    seen = {}
    def llm(ctx):
        seen.setdefault("first", ctx.get("requested_not_yet_tried"))
        return {"done": True, "findings": []}
    run_drilldown(llm, _tool(["payment_funnel", "item_count"]), GAP, {}, ["pharmacy_checkout"], "pharmacy_checkout",
                  must_try=["payment_funnel"])
    assert seen["first"] == ["payment_funnel"]
