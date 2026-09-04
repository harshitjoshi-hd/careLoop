"""
PRD Generator (FR-05). OWNER: Mohit.

Fills the prd-generator template's 8 sections from findings + code_gaps +
trend_report + voc via the `prd-generation` sphere-platform use case.
<=2 user quotes in the Problem section, labelled anecdotal. Unverified
assumptions land in Section 8. Stamped DRAFT — never auto-filed.

`_render_prd_llm_stub` below does template-filling with plain string
formatting so this runs without live LLM credentials (Day 1 gate). Swap
it for a real sphere-platform call (use case:
settings.llm_use_case_prd_generation) when ready — keep the "<=2 quotes,
labelled anecdotal" and "unconfirmed assumptions -> Section 8" rules.
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional, Union

from app.agents.evidence_gate import unsupported_numbers
from app.config import get_settings
from app.integrations.sphere import make_use_case_llm, unwrap_double_encoded_string
from app.pipeline.state import GraphState
from app.schemas.contracts import CodeGap, Finding, RunState, ShippedFix, Suggestion, TrendReport, VocQuote

logger = logging.getLogger("careloop.prd")
LLMCall = Callable[[dict[str, Any]], dict[str, Any]]

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "templates" / "prd_template.md"

GAP_CLASS_SOLUTION_HINTS = {
    "logic_flaw": "Fix the incorrect logic at the cited line so the system stops behaving contrary to intent.",
    "missing_retention_hook": (
        "Add the missing re-engagement hook at the cited line so the user is proactively reached "
        "(push/WA/email) before the flow terminates, instead of silently killing it."
    ),
    "ux_gap": "Close the experience gap at the cited surface — the mechanism works, but the user isn't guided through it.",
    "unclassified": (
        "The mechanism was located at the cited line but could not be auto-classified — review it "
        "and classify (logic flaw / missing retention hook / UX gap) before proposing a fix."
    ),
}


def _evidence_phrase(finding: Finding) -> str:
    """Origin-aware: a funnel number for warehouse, 'N users report X' for voc."""
    if finding.origin == "voc":
        return f"{finding.review_count} users report this in reviews (theme: {finding.theme})"
    return "; ".join(f"{e.metric}={e.value}" for e in finding.evidence)


def _quote_line(quote: Union[str, VocQuote, dict]) -> str:
    if isinstance(quote, str):
        return f'> "{quote}"'
    q = quote.model_dump() if hasattr(quote, "model_dump") else quote
    return f"> {q['rating']}★ · {q['date']} — \"{q['text']}\""


def _collect_quotes(finding: Finding, per_finding_quotes: dict) -> list:
    """
    voc-origin findings carry their own top_quotes; a warehouse-origin finding whose
    routing stage converged with a VoC theme (the demo's 'human moment') gets its
    quotes from Voc.per_finding_quotes instead.
    """
    if finding.top_quotes:
        return list(finding.top_quotes)
    return list(per_finding_quotes.get(str(finding.rank), []))


SUGGESTION_TYPE_LABELS = {"tech": "Tech", "business": "Business", "process": "Process"}


def _shipped_fix_for(evidence_file: Optional[str], evidence_line: Optional[int],
                     shipped: list[ShippedFix]) -> Optional[ShippedFix]:
    return next(
        (sf for sf in shipped if sf.evidence_file == evidence_file and sf.evidence_line == evidence_line),
        None,
    )


def _requirements_block(gap: Optional[CodeGap], suggestions: list[Suggestion],
                        shipped: list[ShippedFix] = ()) -> str:
    """
    Functional requirements, numbered FR-N across BOTH sources in one list —
    a business/process suggestion is just as valid a requirement as a code
    fix (Harshit's ask, 2026-09-04: "not only PRD [code] — changes in the
    business, delivery, and other org verticals"), so it isn't relegated to
    a second-class appendix with its own numbering.

    Remedy Loop verdicts come first (Appendix A #9): ABSENT remedies become
    proposed FRs; EXISTING ones are surfaced so the PRD never re-proposes
    what's already built; PARTIAL needs a closer look; UNVERIFIED
    (status=None — the loop never actually searched, e.g. budget ran out
    first) gets its own honest label rather than folding into "partial"
    (PR #1 B4: "confirmed missing" is the strongest claim available and it
    was being attached to the verdict with the weakest support).

    Closed-loop impact (2026-09-04): an EXISTING remedy that also matches a
    ShippedFix (its evidence line's commit landed after this run's baseline
    — see app.agents.code_scout.impact) is labelled SHIPPED instead, with
    the commit and — when Reporter found a comparable delta — the measured
    impact, so a PRD for an already-resolved finding says so plainly rather
    than reading identically to a finding nobody has touched yet.

    Suggestions (tech/business/process, contracts.py decision #11) follow,
    continuing the same FR-N sequence, each labelled with its type and
    verification status so a reader can tell "proven missing" apart from
    "proposed, nothing to verify" apart from "we didn't get to check."
    """
    lines: list[str] = []
    n = 1

    if gap and gap.remedies:
        for r in gap.remedies:
            searched = len(r.searched_terms)
            tag = f"FR-{n}:"
            n += 1
            if r.status == "absent":
                lines.append(f"- {tag} **[FR candidate — not found in {searched} search{'es' if searched != 1 else ''}]** {r.proposal}")
            elif r.status == "exists":
                sf = _shipped_fix_for(r.evidence_file, r.evidence_line, shipped)
                if sf is None:
                    lines.append(
                        f"- {tag} **[Already built — do not re-propose]** {r.proposal} "
                        f"(`{r.evidence_file}:{r.evidence_line}`)"
                    )
                else:
                    impact = (
                        f" Measured impact: {sf.metric_name} {sf.previous_value}{sf.metric_unit} → "
                        f"{sf.current_value}{sf.metric_unit} "
                        f"({'+' if sf.pct_change >= 0 else ''}{sf.pct_change}%)."
                        if sf.metric_name is not None else " Impact not yet measurable — no comparable baseline."
                    )
                    lines.append(
                        f"- {tag} **[Shipped — do not re-propose]** {r.proposal} "
                        f"(`{r.evidence_file}:{r.evidence_line}`, commit `{sf.commit.short_sha}` "
                        f"by {sf.commit.author} on {sf.commit.date}).{impact}"
                    )
            elif r.status == "partial":
                lines.append(f"- {tag} **[Needs a closer look — partial match]** {r.proposal} — {r.evidence_file or 'related code found'}")
            else:
                lines.append(f"- {tag} **[Unverified — no search ran, e.g. budget exhausted first]** {r.proposal}")

    for s in suggestions:
        tag = f"FR-{n}:"
        n += 1
        type_label = SUGGESTION_TYPE_LABELS[s.suggestion_type]
        if s.verification_status == "exists":
            verdict = f"**[{type_label} — already built, do not re-propose]** ({s.evidence_file}:{s.evidence_line})"
        elif s.verification_status == "absent":
            verdict = f"**[{type_label} — not found in code, FR candidate]**"
        elif s.verification_status == "partial":
            verdict = f"**[{type_label} — needs a closer look, partial match]** ({s.evidence_file or 'related code found'})"
        elif s.verification_status == "unverified":
            verdict = f"**[{type_label} — unverified, budget exhausted before the check ran]**"
        else:  # not_applicable — business/process suggestions carry no code evidence by design
            verdict = f"**[{type_label} suggestion]**"
        lines.append(f"- {tag} {verdict} {s.title}: {s.description} — _{s.rationale}_")

    if not lines:
        return ""
    return "\n\n**Functional requirements (code fixes + suggested improvements, verified where applicable):**\n" + "\n".join(lines)


def _render_prd_llm_stub(
    finding: Finding,
    gaps: list[CodeGap],
    suggestions: list[Suggestion],
    trend: TrendReport,
    quotes: list,
    run_id: int,
    window_start: str,
    window_end: str,
    shipped: list[ShippedFix] = (),
) -> tuple[str, str]:
    template = TEMPLATE_PATH.read_text()
    gap = gaps[0] if gaps else None
    quotes = quotes[:2]  # Section 3 rule: at most 2 quotes, labelled anecdotal

    quote_block = ""
    if quotes:
        quote_block = "\n\n**User voice (anecdotal):**\n" + "\n".join(_quote_line(q) for q in quotes)

    title = f"Fix: {finding.hypothesis.splitlines()[0][:80]}"
    problem = (
        f"{finding.hypothesis} Evidence: {_evidence_phrase(finding)}. "
        f"Confirm via: {finding.confirm_via}."
        + quote_block
    )
    segment_desc = ", ".join(f"{s.dimension}={s.value}" for s in finding.segments) or "all"
    background = f"Routing category `{finding.stage}` (segments: {segment_desc}). Trend context: {trend.narrative}"

    finding_shipped = [sf for sf in shipped if sf.finding_rank == finding.rank]
    requirements = _requirements_block(gap, suggestions, shipped)

    if gap and gap.mechanism_found:
        goals = f"Close the `{gap.gap_class}` gap at `{gap.repo}/{gap.file}:{gap.line}` without regressing existing behaviour."
        if finding_shipped:
            goals = (
                "A fix for this finding has already SHIPPED (see the Remedy Loop verdicts below) — "
                "goals below apply only to whatever remains unaddressed. " + goals
            )
        solution = (
            f"{GAP_CLASS_SOLUTION_HINTS[gap.gap_class]}\n\n"
            f"**Gap statement:** {gap.gap_statement}\n\n"
            f"**Location:** `{gap.repo}/{gap.file}:{gap.line}`"
            + (f"\n\n**Proposed change location:** {gap.proposed_change_location}" if gap.proposed_change_location else "")
            + requirements
        )
        scope = f"In scope: routing category `{finding.stage}` in `{gap.service}`. Out of scope: unrelated stages."
    elif gap and not gap.mechanism_found:
        goals = f"TODO(Code Scout): no mechanism found yet ({gap.no_match_reason}) — re-run the search or widen the term budget."
        solution = (
            f"Code Scout searched `{gap.repo}` ({gap.searches_run} of a 5-search budget) but found no matching "
            f"mechanism (reason: `{gap.no_match_reason}`). No code-level solution can be proposed until this resolves."
            + requirements
        )
        scope = f"In scope: routing category `{finding.stage}` in `{gap.repo}`. Out of scope: unrelated stages."
    elif suggestions:
        # No diagnosed code bug for this finding, but Code Scout's alternate
        # flow still surfaced improvement ideas (business/process/tech) —
        # that's a legitimate PRD on its own, not a TODO placeholder.
        repos = sorted({s.repo for s in suggestions})
        goals = f"Address the drop-off via the improvement(s) below rather than a diagnosed code bug — no code gap was located for this finding."
        solution = (
            f"Code Scout found no single diagnosed mechanism for this finding, but proposes the following "
            f"improvement(s) after exploring {', '.join(f'`{r}`' for r in repos)}."
            + requirements
        )
        scope = f"In scope: routing category `{finding.stage}` in {', '.join(f'`{r}`' for r in repos)}. Out of scope: unrelated stages."
    else:
        goals = "TODO(Code Scout): no code gap was routed for this finding yet."
        solution = "TODO(Code Scout): pipeline ran without a resolved code_gap for the top finding."
        scope = f"In scope: routing category `{finding.stage}`. Out of scope: unrelated stages."

    success_metrics = (
        f"Stage conversion for routing category `{finding.stage}` moves within ±2pp of the Power BI baseline "
        f"post-fix; no regression in adjacent stages."
    )
    for sf in finding_shipped:
        if sf.metric_name is not None:
            success_metrics += (
                f" Already measured: {sf.metric_name} moved {sf.previous_value}{sf.metric_unit} → "
                f"{sf.current_value}{sf.metric_unit} ({'+' if sf.pct_change >= 0 else ''}{sf.pct_change}%) "
                f"since commit `{sf.commit.short_sha}` ({sf.commit.date})."
            )
    open_questions = "- " + finding.confirm_via
    if finding.confidence == "low":
        open_questions += f"\n- Hypothesis confidence is only '{finding.confidence}' — treat as unconfirmed."
    if gap and not gap.mechanism_found:
        open_questions += f"\n- Code Scout found no mechanism ({gap.no_match_reason}); solution above is a placeholder."

    body = (
        template.replace("{{title}}", title)
        .replace("{{run_id}}", str(run_id))
        .replace("{{window_start}}", window_start)
        .replace("{{window_end}}", window_end)
        .replace("{{confidence}}", finding.confidence)
        .replace("{{overview}}", f"CareLoop-generated fix proposal for the #{finding.rank} ranked drop-off finding.")
        .replace("{{background}}", background)
        .replace("{{problem}}", problem)
        .replace("{{goals}}", goals)
        .replace("{{gap_class}}", (gap.gap_class if gap and gap.gap_class else "unclassified"))
        .replace("{{solution}}", solution)
        .replace("{{scope}}", scope)
        .replace("{{success_metrics}}", success_metrics)
        .replace("{{open_questions}}", open_questions)
    )
    return title, body


def _prd_inputs(finding, gaps, suggestions, trend, quotes, run_id, window_start, window_end, shipped=()) -> dict:
    """Exactly what the drafting model is allowed to know.

    Remedy verdicts and suggestions are passed as structured `status`/
    `verification_status` values, not prose. The model may word a
    requirement however it likes; whether the fix is already built is
    decided by the Remedy Loop / Suggestion verifier and cannot be
    upgraded by writing more confidently — which is the failure this
    pipeline has corrected twice already ("confirmed missing" on an
    unsearched remedy).

    `shipped_fixes` is new (2026-09-04, closed-loop impact) and additive: a
    caller/template that doesn't know this key can ignore it exactly as it
    already ignores any other input it wasn't told to use — the evidence
    gate (`unsupported_numbers`) still rejects any number the model writes
    that isn't traceable to one of these inputs, shipped-fix numbers
    included, so an old prompt degrades safely rather than being able to
    invent an impact figure.
    """
    gap = gaps[0] if gaps else None
    finding_shipped = [sf for sf in shipped if sf.finding_rank == finding.rank]
    return {
        "run_id": run_id,
        "window": {"start": window_start, "end": window_end},
        "finding": {
            "rank": finding.rank, "origin": finding.origin, "stage": finding.stage,
            "hypothesis": finding.hypothesis, "confidence": finding.confidence,
            "confirm_via": finding.confirm_via,
            "segments": [{"dimension": s.dimension, "value": s.value} for s in finding.segments],
            "evidence": [{"metric": e.metric, "value": e.value} for e in finding.evidence],
            "review_count": finding.review_count, "theme": finding.theme,
        },
        "code_gap": None if gap is None else {
            "repo": gap.repo, "service": gap.service, "file": gap.file, "line": gap.line,
            "mechanism_found": gap.mechanism_found, "gap_class": gap.gap_class,
            "gap_statement": gap.gap_statement, "no_match_reason": gap.no_match_reason,
            "proposed_change_location": gap.proposed_change_location,
            "remedies": [{"proposal": r.proposal, "status": r.status,
                          "evidence_file": r.evidence_file,
                          "searches_run": len(r.searched_terms)} for r in gap.remedies],
        },
        "suggestions": [{"suggestion_type": s.suggestion_type, "title": s.title,
                         "description": s.description, "rationale": s.rationale,
                         "verification_status": s.verification_status,
                         "evidence_file": s.evidence_file} for s in suggestions],
        "trend_narrative": trend.narrative,
        "anecdotal_quotes": [{"text": getattr(q, "text", str(q)),
                              "rating": getattr(q, "rating", None)} for q in quotes[:2]],
        "shipped_fixes": [
            {
                "remedy_proposal": sf.remedy_proposal, "evidence_file": sf.evidence_file,
                "commit_sha": sf.commit.short_sha, "commit_date": sf.commit.date,
                "metric_name": sf.metric_name, "metric_unit": sf.metric_unit,
                "previous_value": sf.previous_value, "current_value": sf.current_value,
                "pct_change": sf.pct_change,
            }
            for sf in finding_shipped
        ],
        "rules": [
            "Every number must come from these inputs. Do not compute new totals.",
            "Express targets relatively ('recover 5% of X'), never as an invented absolute count.",
            "A remedy's status is given; never restate an absent remedy as confirmed or built.",
            "A suggestion's verification_status is given the same way — never restate 'absent' as built.",
            "No angle brackets anywhere in the output.",
            "Format each of the eight sections as a markdown heading: '## 1. Overview', "
            "'## 2. Goals & Success Metrics', and so on. Functional requirements are list "
            "items beginning '- FR-1:', '- FR-2:'.",
            "If shipped_fixes is non-empty, this finding's fix has ALREADY SHIPPED: do not write it "
            "as a new functional requirement. Instead, Section 4 (Goals) must say it already shipped "
            "(citing commit_sha/commit_date) and Section 7 (Success Metrics) must cite its measured "
            "impact verbatim from metric_name/previous_value/current_value/pct_change when those are "
            "not null — never invent or rephrase the numbers, and never claim measured impact when "
            "metric_name is null (say impact is not yet measurable instead).",
        ],
    }


def _render_prd_llm(llm: LLMCall, inputs: dict) -> tuple[str, str]:
    """Returns (markdown, source). Falls back rather than shipping bad prose."""
    try:
        out = llm({"prd_inputs": inputs})
    except Exception as exc:                       # first-ever caller of 21691
        logger.warning("prd-generation call failed (%s) — deterministic PRD", exc)
        return "", f"llm_error:{type(exc).__name__}"

    body = unwrap_double_encoded_string(out.get("prd_markdown") or "").strip()
    if len(body) < 200:
        logger.warning("prd-generation returned %d chars — deterministic PRD", len(body))
        return "", "too_short"

    invented = unsupported_numbers(body, inputs)
    if invented:
        logger.warning("prd-generation cited ungrounded numbers %s — deterministic PRD", invented)
        return "", f"ungrounded_numbers:{invented}"
    return normalise_headings(body), "llm"


_BARE_SECTION = re.compile(r"^\s*(\d{1,2})[.)]?\s+([A-Z][^\n]{2,80})$")


def normalise_headings(body: str) -> str:
    """Make the eight sections render as headings.

    Run 8's model-written draft titled its sections "1 Overview (What /
    Problem / Users / Out of Scope)" with no markdown marker, so the PRD
    drawer rendered them as plain paragraphs. If the draft has no ## headings
    at all, a line that is just a section number and a title becomes one.
    Drafts that already use markdown headings are left alone.
    """
    if re.search(r"^##\s", body, re.M):
        return body
    out = []
    for line in body.splitlines():
        m = _BARE_SECTION.match(line)
        out.append(f"## {m.group(1)}. {m.group(2).strip()}" if m else line)
    return "\n".join(out)


def _with_draft_banner(body: str, run_id: int, window_start: str, window_end: str,
                       confidence: str) -> str:
    """The banner is ours, not the model's.

    It is the one line that stops a generated document being mistaken for an
    approved one, so it is prepended deterministically and any model-authored
    version is dropped — a drafting model must not be able to omit or soften it.
    """
    banner = (f"> **DRAFT — needs human review.** Generated by CareLoop run `{run_id}` on "
              f"`{window_start}`–`{window_end}`. Hypothesis confidence: `{confidence}`. "
              f"Never auto-filed as a ticket or MR.")
    kept = [ln for ln in body.splitlines() if "DRAFT" not in ln or not ln.lstrip().startswith(">")]
    return "\n".join([kept[0], "", banner, ""] + kept[1:]) if kept and kept[0].startswith("#") \
        else "\n".join([banner, ""] + kept)


MAX_PRDS_PER_RUN = 5
# Drafts are independent of each other, so they are rendered concurrently —
# five sequential ~40 s sphere calls would add over three minutes to a run.
PRD_RENDER_WORKERS = 3


def _draft_for(finding: Finding, run_state: RunState, llm: Optional[LLMCall]) -> dict:
    """One PRD for one finding: model-written when the draft survives the
    evidence gate, deterministic template otherwise. `source` says which."""
    gaps = run_state.gaps_for(finding.rank)
    suggestions = run_state.suggestions_for(finding.rank)
    quotes = _collect_quotes(finding, run_state.voc.per_finding_quotes)
    title, deterministic = _render_prd_llm_stub(
        finding, gaps, suggestions, run_state.trend_report, quotes,
        run_id=run_state.run_id,
        window_start=run_state.window_start,
        window_end=run_state.window_end,
        shipped=run_state.shipped_fixes,
    )
    body, source = "", "deterministic"
    if llm is not None:
        inputs = _prd_inputs(finding, gaps, suggestions, run_state.trend_report, quotes,
                             run_state.run_id, run_state.window_start, run_state.window_end,
                             shipped=run_state.shipped_fixes)
        body, source = _render_prd_llm(llm, inputs)
        if body:
            body = _with_draft_banner(body, run_state.run_id, run_state.window_start,
                                      run_state.window_end, finding.confidence)
    if not body:
        body, source = deterministic, (source if source != "llm" else "deterministic")
    return {"finding_rank": finding.rank, "title": title, "markdown": body, "source": source}


def prd_generator_node(state: GraphState, *, llm: Optional[LLMCall] = None) -> GraphState:
    """
    Generates one PRD per finding, not just the #1 ranked one — capped at
    MAX_PRDS_PER_RUN. `prd_draft` is kept as the #1 finding's markdown alone
    (existing field, other consumers read it) for backward compatibility;
    `prd_drafts` (new, additive) carries the full list.

    Each draft goes through the `prd-generation` sphere use case (live) or its
    recorded replay (demo) and is accepted only if every number it cites is in
    its own inputs; otherwise that finding gets the deterministic template and
    `source` records why. The DRAFT banner is always ours.
    """
    if llm is None:
        llm = make_use_case_llm(get_settings().llm_use_case_prd_generation,
                                bool(state.get("demo_mode", True)), journey=state.get("journey"))
    # "reviews" is pipeline-level input (like cohort_cuts), not a RunState field.
    run_state = RunState(**{k: v for k, v in state.items() if k not in ("error", "reviews")})
    findings = sorted(run_state.findings, key=lambda f: f.rank)[:MAX_PRDS_PER_RUN]

    if not findings:
        return {**state, "prd_draft": None, "prd_drafts": [], "status": "completed",
                "error": "no_finding_to_draft_prd_for"}

    if llm is None or len(findings) == 1:
        drafts = [_draft_for(f, run_state, llm) for f in findings]
    else:
        with ThreadPoolExecutor(max_workers=PRD_RENDER_WORKERS) as pool:
            drafts = list(pool.map(lambda f: _draft_for(f, run_state, llm), findings))

    return {**state, "prd_draft": drafts[0]["markdown"], "prd_drafts": drafts,
            "prd_source": drafts[0]["source"], "status": "completed"}
