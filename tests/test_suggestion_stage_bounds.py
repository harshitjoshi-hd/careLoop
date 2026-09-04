"""The suggestion stage runs findings concurrently and never holds the run past
its deadline — run 28 sat in that stage for over 30 minutes behind a slow
GitLab while five findings, five gaps and a report waited."""
import time

from app.agents.code_scout import suggestion_node as sn
from app.schemas.contracts import Finding, RunState


def _state(n):
    return RunState(run_id=1, window_start="a", window_end="b", demo_mode=False,
                    findings=[Finding(rank=i, origin="warehouse", stage="pharmacy_checkout", hypothesis="h",
                                      confidence="high", confirm_via="x") for i in range(1, n + 1)])


def test_findings_are_processed_concurrently_and_in_order(monkeypatch):
    calls = []
    def fake(finding, search_client, assessor, journey):
        calls.append(finding.rank); time.sleep(0.2); return []
    monkeypatch.setattr(sn, "_process_finding", fake)
    t = time.monotonic()
    sn.suggestion_code_scout_node(_state(3), search_client=object(), assessor=object())
    assert time.monotonic() - t < 0.5          # 3 x 0.2 s sequential would be 0.6 s
    assert sorted(calls) == [1, 2, 3]


def test_a_slow_finding_does_not_hold_the_stage_past_the_deadline(monkeypatch):
    def fake(finding, search_client, assessor, journey):
        if finding.rank == 2:
            time.sleep(5)                       # "GitLab read timed out", repeatedly
        return []
    monkeypatch.setattr(sn, "_process_finding", fake)
    t = time.monotonic()
    out = sn.suggestion_code_scout_node(_state(3), search_client=object(), assessor=object(), deadline_s=0.5)
    assert time.monotonic() - t < 1.5
    assert out["suggestions"] == []


def test_one_failing_finding_does_not_sink_the_others(monkeypatch):
    from types import SimpleNamespace
    def fake(finding, search_client, assessor, journey):
        if finding.rank == 1:
            raise RuntimeError("boom")
        return [SimpleNamespace(finding_rank=finding.rank)]
    monkeypatch.setattr(sn, "_process_finding", fake)
    out = sn.suggestion_code_scout_node(_state(3), search_client=object(), assessor=object())
    assert [s.finding_rank for s in out["suggestions"]] == [2, 3]
