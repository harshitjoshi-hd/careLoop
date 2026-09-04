"""
Chat-style PRD editing. OWNER: Mohit.

A human reviewing a DRAFT PRD can ask for a change in plain language
instead of hand-editing markdown. Two tiers:

  fast paths (no network, always available)
    - "title: <new title>" / "rename title to <new title>" -> renames the H1
    - "remove FR-<n>" / "delete FR-<n>" -> drops that functional requirement row

  everything else -> a real rewrite through the dedicated `prd-chat-edit`
    sphere use case (project 7121, use case 12870, template 21791: it receives
    original_markdown + instruction and returns prd_markdown + a one-line
    reply for the chat). The rewrite is accepted only if it is a complete
    document, contains no corrupted control bytes (the model mangles non-ASCII
    punctuation it has to copy — see _ASCII_PUNCT — so it is sent an ASCII copy
    and untouched lines are restored from the original afterwards),
    and every number it cites was already in the document or the instruction
    — the same evidence gate the generator uses — and the DRAFT banner is
    ours, re-inserted if the model dropped it. When no model is available
    (demo mode without LIVE_LLM) or the draft is rejected, the request is
    appended to Open Questions as a flagged item and the reply says exactly
    why, rather than pretending an edit was made.
"""
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.agents.evidence_gate import unsupported_numbers
from app.integrations.sphere import unwrap_double_encoded_string

logger = logging.getLogger("careloop.prd_editor")
LLMCall = Callable[[dict[str, Any]], dict[str, Any]]

MIN_REVISION_CHARS = 200          # anything shorter is a truncated or empty draft
# A live call returned prose where every em dash / middle dot / star / plus-minus sign had
# been replaced by a single control byte (e.g. em-dash "—" -> "\x14") — reproduced against the
# real prd-chat-edit endpoint 2026-09-04, not a local encoding bug (this repo's own read/write
# round-trips UTF-8 correctly; confirmed by direct test). Cause not confirmed (sphere-side
# guardrail or transport), but the fix has to live here regardless: never ship visibly-corrupted
# prose just because the length/number checks below don't catch it.
_BAD_CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Cause, confirmed from a Langfuse trace on 2026-09-05 (run 11, "how was this problem
# identified"): every corrupted byte is the LOW BYTE of the code point the model was
# reproducing — em dash U+2014 -> \x14, en dash U+2013 -> \x13, and the multiplication
# sign U+00D7 came back as a mangled "\u00d" escape. gpt-5-mini under strict JSON-schema
# output writes broken \uXXXX escapes for non-ASCII characters it has to copy verbatim.
# Our PRD generator puts "—", "–" and "×" into every document (banner, date range,
# cross-tabs), so every free-text edit had to reproduce them and was rejected above.
# Fix, in three parts: (1) hand the model an ASCII-only copy of the document so it has
# nothing non-ASCII to reproduce; (2) after the call, put the original line back for every
# line the model returned unchanged (same letters and digits), which restores our own
# punctuation in untouched sections; (3) repair the two known low-byte corruptions in text
# the model wrote itself, and still reject anything else that is unrepairable.
_ASCII_PUNCT = {
    "\u2014": "-", "\u2013": "-", "\u2012": "-", "\u2212": "-",     # dashes, minus
    "\u00d7": "x", "\u00b7": "-", "\u2022": "-", "\u00b1": "+/-",   # times, middle dot, bullet, plus-minus
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',     # curly quotes
    "\u2026": "...", "\u2192": "->", "\u2248": "~", "\u00a0": " ",  # ellipsis, arrow, approx, nbsp
}
_ASCII_TABLE = str.maketrans(_ASCII_PUNCT)
_LOW_BYTE_REPAIR = str.maketrans({"\x14": "\u2014", "\x13": "\u2013"})
_KEY_RE = re.compile(r"[^A-Za-z0-9]+")
# A mangled escape leaks one hex digit next to the control byte ("\\u00d7" -> "\\x00" + "d"),
# so the digit riding on a control byte is dropped before a line is keyed.
_CTRL_WITH_HEX_RE = re.compile(r"[\x00-\x1f\x7f][0-9a-fA-F]?")


def to_ascii(text: str) -> str:
    """The copy of the document the model sees: same words and numbers, ASCII punctuation."""
    return text.translate(_ASCII_TABLE)


def _key(line: str) -> str:
    return _KEY_RE.sub("", _CTRL_WITH_HEX_RE.sub("", line))


def restore_untouched_lines(original: str, revised: str) -> str:
    """Every revised line whose letters and digits match exactly one original line is
    that line, returned through the model — take ours back, punctuation included. Lines
    the model actually wrote (or ambiguous ones such as blank lines) are left alone."""
    by_key: dict[str, Optional[str]] = {}
    for ln in original.split("\n"):
        k = _key(ln)
        if k:
            by_key[k] = None if k in by_key else ln          # None marks a duplicate key
    out = []
    # split("\n"), not splitlines(): a corrupted "\r" (the x-sign's mangled escape) sits
    # INSIDE a line and must not be treated as a line break.
    for ln in revised.split("\n"):
        orig = by_key.get(_key(ln)) if _key(ln) else None
        out.append(orig if orig is not None else ln)
    return "\n".join(out)


@dataclass
class EditResult:
    markdown: str
    reply: str
    applied: bool


_TITLE_RE = re.compile(r"^(?:title:\s*|rename title to\s+)(.+)$", re.IGNORECASE)
_REMOVE_FR_RE = re.compile(r"\b(?:remove|delete)\s+fr[-\s]?(\d+)\b", re.IGNORECASE)
_BANNER_RE = re.compile(r"^>\s*\*\*DRAFT", re.MULTILINE)


def apply_edit_instruction(markdown: str, message: str, llm: Optional[LLMCall] = None) -> EditResult:
    message = message.strip()

    title_match = _TITLE_RE.match(message)
    if title_match:
        new_title = title_match.group(1).strip()
        new_markdown = re.sub(r"^# .+$", f"# {new_title}", markdown, count=1, flags=re.MULTILINE)
        return EditResult(new_markdown, f'Title changed to "{new_title}".', applied=True)

    fr_match = _REMOVE_FR_RE.search(message)
    if fr_match:
        fr_id = f"FR-{fr_match.group(1)}"
        fr_line_re = re.compile(rf"^-\s*{re.escape(fr_id)}:", re.IGNORECASE)
        lines = markdown.splitlines()
        kept = [ln for ln in lines if not fr_line_re.match(ln)]
        if len(kept) == len(lines):
            return EditResult(markdown, f"Couldn't find {fr_id} in this PRD — nothing removed.", applied=False)
        return EditResult("\n".join(kept), f"Removed {fr_id}.", applied=True)

    if llm is not None:
        revised = revise_with_llm(llm, markdown, message)
        if revised.applied:
            return revised
        why = revised.reply
    else:
        why = "no model is available for rewrites in this mode (demo mode without LIVE_LLM)"

    note = f"- **Reviewer request (unresolved):** {message}"
    new_markdown = markdown.rstrip() + "\n" + note + "\n"
    reply = (
        f"I couldn't apply that rewrite — {why}. "
        "Added your request to Section 8 (Open Questions) as a flagged item instead of guessing. "
        "Renaming the title or removing a specific FR-N always works directly."
    )
    return EditResult(new_markdown, reply, applied=False)


def revise_with_llm(llm: LLMCall, markdown: str, instruction: str) -> EditResult:
    """One revision call. Returns applied=False with the reason on any failure;
    the caller decides what to do with the document then."""
    inputs = {"original_markdown": to_ascii(markdown), "instruction": instruction}
    try:
        out = llm({"edit_inputs": inputs})
    except Exception as exc:                                   # network, sphere FAILED, bad JSON
        logger.warning("prd-chat-edit call failed (%s)", exc)
        return EditResult(markdown, f"the model call failed ({type(exc).__name__})", applied=False)

    body = unwrap_double_encoded_string(out.get("prd_markdown") or "").strip()
    if len(body) < MIN_REVISION_CHARS:
        return EditResult(markdown, "the model returned an empty or truncated document", applied=False)

    body = restore_untouched_lines(markdown, body).translate(_LOW_BYTE_REPAIR)
    bad_chars = set(_BAD_CONTROL_CHARS.findall(body)) - set(_BAD_CONTROL_CHARS.findall(markdown))
    if bad_chars:
        logger.warning("prd-chat-edit returned corrupted control bytes %s — rejected",
                        [hex(ord(c)) for c in bad_chars])
        return EditResult(markdown, "the rewrite came back with corrupted characters", applied=False)

    invented = unsupported_numbers(body, inputs)
    if invented:
        logger.warning("prd revision cited ungrounded numbers %s — rejected", invented)
        return EditResult(markdown, f"the rewrite cited numbers that are not in the document: {invented}",
                          applied=False)

    body = _keep_banner(markdown, body)
    added, removed = _line_delta(markdown, body)
    model_reply = " ".join((out.get("reply") or "").split())
    reply = (model_reply or f"Applied: {instruction}.") + \
        f" ({added} line(s) added, {removed} removed; every number was already in the document; still a DRAFT.)"
    return EditResult(body, reply, applied=True)


def _keep_banner(original: str, revised: str) -> str:
    """The banner is ours, not the model's: if the rewrite dropped it, put the
    original banner back under the title (or at the top)."""
    if _BANNER_RE.search(revised):
        return revised
    banner = next((ln for ln in original.splitlines() if _BANNER_RE.match(ln)), None)
    if banner is None:
        return revised
    lines = revised.splitlines()
    if lines and lines[0].startswith("#"):
        return "\n".join([lines[0], "", banner, ""] + lines[1:])
    return "\n".join([banner, ""] + lines)


def _line_delta(before: str, after: str) -> tuple[int, int]:
    a = set(before.splitlines()); b = set(after.splitlines())
    return len(b - a), len(a - b)
