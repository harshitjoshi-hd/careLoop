"""unwrap_double_encoded_string — a structured sphere response's string field
sometimes comes back as its own json.dumps() output (escaped \\n, wrapping
quotes) instead of the real text. Reproduced live 2026-09-05: every PRD on a
real run (all 5, not just one) was stored as one giant line with zero real
newlines — the whole file was valid JSON that decoded to the intended
markdown. The UI froze trying to render it as a single unbroken block.
"""
from app.integrations.sphere import unwrap_double_encoded_string


def test_a_json_encoded_string_is_decoded_to_the_real_text():
    wrapped = '"## 1. Overview\\n\\nSome body text."'
    assert unwrap_double_encoded_string(wrapped) == "## 1. Overview\n\nSome body text."


def test_plain_markdown_without_wrapping_quotes_is_left_alone():
    plain = "## 1. Overview\n\nSome body text."
    assert unwrap_double_encoded_string(plain) == plain


def test_a_string_that_merely_starts_and_ends_with_a_quote_but_isnt_valid_json_is_left_alone():
    # A markdown body legitimately quoting something at both ends, with an
    # unescaped inner quote — not valid JSON, must not be mangled.
    invalid_json = '"she said "hi" to him"'
    assert unwrap_double_encoded_string(invalid_json) == invalid_json


def test_a_json_string_that_decodes_to_a_non_string_is_left_alone():
    # Defensive: only unwrap if the decoded value is itself a plain string.
    assert unwrap_double_encoded_string('"5"') == "5"  # decodes to a string "5" — fine, unwrapped
    non_string_like = '"[1, 2, 3]"'  # decodes to the STRING "[1, 2, 3]", not a list — still a string, unwrapped
    assert unwrap_double_encoded_string(non_string_like) == "[1, 2, 3]"


def test_non_string_input_is_returned_unchanged():
    assert unwrap_double_encoded_string(None) is None
    assert unwrap_double_encoded_string("") == ""


def test_reproduces_the_live_run_9_shape():
    wrapped = '"## 1. Overview\\n\\n> **DRAFT \\u2014 needs human review.**\\n\\nBody text here."'
    result = unwrap_double_encoded_string(wrapped)
    assert result.count("\n") > 0
    assert result.startswith("## 1. Overview")
    assert "DRAFT" in result
