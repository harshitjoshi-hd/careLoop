"""prd_generator._render_prd_llm — a live run was observed with every one of
its 5 PRDs stored as prd_markdown's own json.dumps() output (escaped \\n,
wrapping quotes) instead of the real markdown text. Must unwrap that before
the length/grounding checks run, not reject a perfectly good draft for being
"one giant line" or ship the escaped text as-is.
"""
import json

from app.pipeline.nodes.prd_generator import _render_prd_llm

REAL_MARKDOWN = (
    "## 1. Overview\n\n"
    "> **DRAFT — needs human review.**\n\n"
    "This finding describes a real problem grounded in the data below, "
    "long enough to clear the minimum length the generator requires here.\n\n"
    "## 2. Goals\n\n- Recover some of the loss\n"
)


def test_a_json_double_encoded_response_is_unwrapped_and_accepted():
    llm = lambda ctx: {"prd_markdown": json.dumps(REAL_MARKDOWN)}
    body, source = _render_prd_llm(llm, inputs={})
    assert source == "llm"
    assert body.count("\n") > 0
    assert "## 1. Overview" in body


def test_a_plain_response_still_works_unchanged():
    llm = lambda ctx: {"prd_markdown": REAL_MARKDOWN}
    body, source = _render_prd_llm(llm, inputs={})
    assert source == "llm"
    assert "## 1. Overview" in body
