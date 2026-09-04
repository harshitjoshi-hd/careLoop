"""Code Scout's alternate output flow (Rev 3, PR #3): explore -> suggest ->
verify, producing Suggestion objects rather than CodeGap. Kept additive
alongside node.py's find_gap()-based code_scout_node - see contracts.py's
module docstring (decision #11) and this repo's PR #3 review (S2) for why
this isn't wired into app/pipeline/graph.py: which of the two Code Scout
output shapes (or both) ships is an explicit three-way decision (Nakul /
Mohit / Harshit), not something to force via whichever branch merges last.

  1. explore() the routing-matched repo(s) to inventory what already exists
     in that feature area (bounded by EXPLORATION_SEARCH_BUDGET).
  2. Generate suggestions via the assessor - improvements to what exists, or
     new features. NOT limited to code: business/process suggestions are
     equally valid and carry no code evidence.
  3. For suggestion_type="tech" only, verify against check_within_file()
     whether it's already built.

Shaped as a LangGraph node: a pure function taking RunState and returning a
dict of state updates. search_client/assessor are injected so tests (and
Day 1 vs Day 2) can swap Fixture/Stub for the live implementations without
touching this file.

External-call resilience (PR #3 review): explore()/propose_search_terms()/
propose_suggestions()/check_within_file() failing used to propagate and
crash the whole run. All four are now caught at finding/repo/verification
granularity - a bad call is logged and the run continues with whatever DID
resolve, and a verification failure reports "unverified" rather than being
conflated with "not_applicable" (nothing to check) or guessed at.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from app.agents.code_scout.errors import CodeScoutExternalError
from app.agents.code_scout.explore_search_client import ExploreSearchClient, GapLocation
from app.agents.code_scout.routing import repos_for_stage
from app.agents.code_scout.suggestion_assessor import FeatureSuggestionAssessor, SuggestionProposal
from app.schemas.contracts import Finding, RunState, Suggestion

logger = logging.getLogger(__name__)

EXPLORATION_SEARCH_BUDGET = 8  # searches to build the feature inventory, per finding
MAX_SUGGESTIONS_PER_FINDING = 5
VERIFICATION_PROXIMITY_LINES = 15  # "exists" if the signature is within this many lines of the cited mechanism
SUGGESTION_WORKERS = 3             # findings are independent; run 24 spent ~190 s doing them one by one
SUGGESTION_STAGE_DEADLINE_S = 240  # a slow GitLab must not hold the run: findings still pending get no suggestions


def suggestion_code_scout_node(
    state: RunState, *, search_client: ExploreSearchClient, assessor: FeatureSuggestionAssessor,
    deadline_s: float = SUGGESTION_STAGE_DEADLINE_S,
) -> dict:
    findings = list(state.findings)
    new_suggestions: list[Suggestion] = []
    if len(findings) <= 1:
        for f in findings:
            new_suggestions.extend(_process_finding(f, search_client, assessor, state.journey))
        return {"suggestions": [*state.suggestions, *new_suggestions]}

    started = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=min(SUGGESTION_WORKERS, len(findings)))
    futures = {pool.submit(_process_finding, f, search_client, assessor, state.journey): f for f in findings}
    results: dict[int, list[Suggestion]] = {}
    pending = set(futures)
    while pending:
        remaining = deadline_s - (time.monotonic() - started)
        if remaining <= 0:
            break
        done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
        for fut in done:
            f = futures[fut]
            try:
                results[f.rank] = fut.result()
            except Exception as exc:                       # one finding never sinks the others
                logger.warning("suggestions for finding #%s failed: %s", f.rank, exc)
                results[f.rank] = []
    if pending:
        logger.warning("suggestion stage hit its %.0f s deadline; %d finding(s) get no suggestions: %s",
                       deadline_s, len(pending), sorted(futures[p].rank for p in pending))
    pool.shutdown(wait=False, cancel_futures=True)
    for f in findings:                                     # order preserved
        new_suggestions.extend(results.get(f.rank, []))
    return {"suggestions": [*state.suggestions, *new_suggestions]}


def _process_finding(
    finding: Finding, search_client: ExploreSearchClient, assessor: FeatureSuggestionAssessor, journey: str
) -> list[Suggestion]:
    try:
        search_terms = assessor.propose_search_terms(finding)
    except CodeScoutExternalError as exc:
        logger.warning("propose_search_terms failed for finding #%s: %s", finding.rank, exc)
        return []

    suggestions: list[Suggestion] = []

    for repo_info in repos_for_stage(finding.stage, journey):
        try:
            inventory, searches_run = search_client.explore(
                finding.rank, repo_info["repo"], search_terms, EXPLORATION_SEARCH_BUDGET
            )
        except CodeScoutExternalError as exc:
            logger.warning(
                "explore() failed for finding #%s in %r: %s", finding.rank, repo_info["repo"], exc
            )
            continue
        if not inventory:
            logger.info(
                "explore() found nothing for finding #%s in %r after %d search(es)",
                finding.rank, repo_info["repo"], searches_run,
            )
            continue

        try:
            proposals = assessor.propose_suggestions(finding, inventory)[:MAX_SUGGESTIONS_PER_FINDING]
        except CodeScoutExternalError as exc:
            logger.warning(
                "propose_suggestions failed for finding #%s in %r: %s", finding.rank, repo_info["repo"], exc
            )
            continue
        budget_remaining = EXPLORATION_SEARCH_BUDGET - searches_run

        for proposal in proposals:
            suggestions.append(
                _verify_and_build(
                    finding=finding,
                    proposal=proposal,
                    repo_info=repo_info,
                    inventory=inventory,
                    search_client=search_client,
                    search_terms=search_terms,
                    searches_run=searches_run,
                    budget_remaining=budget_remaining,
                )
            )
            if proposal.suggestion_type == "tech" and budget_remaining > 0:
                budget_remaining -= 1

    return suggestions


def _verify_and_build(
    *,
    finding: Finding,
    proposal: SuggestionProposal,
    repo_info: dict,
    inventory: list[GapLocation],
    search_client: ExploreSearchClient,
    search_terms: list[str],
    searches_run: int,
    budget_remaining: int,
) -> Suggestion:
    if proposal.suggestion_type != "tech" or not proposal.signature:
        # Business/process suggestions have nothing to verify against code,
        # and neither does a tech suggestion with no signature to check.
        return _suggestion(finding, proposal, repo_info, "not_applicable", search_terms, searches_run)

    if budget_remaining <= 0:
        # There WAS something to check, we just didn't get to it - distinct
        # from not_applicable: "we didn't check" must not read as "there
        # was nothing to check."
        return _suggestion(finding, proposal, repo_info, "unverified", search_terms, searches_run)

    try:
        evidence_line = search_client.check_within_file(
            finding.rank, repo_info["repo"], proposal.evidence_file, proposal.signature
        )
    except CodeScoutExternalError as exc:
        logger.warning(
            "check_within_file failed for finding #%s in %r: %s", finding.rank, repo_info["repo"], exc
        )
        return _suggestion(finding, proposal, repo_info, "unverified", search_terms, searches_run)

    mechanism_line = _line_for_file(inventory, proposal.evidence_file)

    if evidence_line is None:
        status = "absent"
    elif mechanism_line is not None and abs(evidence_line - mechanism_line) <= VERIFICATION_PROXIMITY_LINES:
        status = "exists"
    else:
        # Found in the same file, but far from the cited mechanism - the
        # capability exists in the codebase but isn't proven wired into this
        # specific path.
        status = "partial"

    return Suggestion(
        finding_rank=finding.rank,
        origin=finding.origin,
        stage=finding.stage,
        service=repo_info["service"],
        repo=repo_info["repo"],
        suggestion_type=proposal.suggestion_type,
        title=proposal.title,
        description=proposal.description,
        rationale=proposal.rationale,
        verification_status=status,
        evidence_file=proposal.evidence_file,
        evidence_line=evidence_line,
        search_terms_used=[*search_terms, proposal.signature],
        searches_run=searches_run + 1,
    )


def _suggestion(
    finding: Finding,
    proposal: SuggestionProposal,
    repo_info: dict,
    verification_status: str,
    search_terms: list[str],
    searches_run: int,
) -> Suggestion:
    return Suggestion(
        finding_rank=finding.rank,
        origin=finding.origin,
        stage=finding.stage,
        service=repo_info["service"],
        repo=repo_info["repo"],
        suggestion_type=proposal.suggestion_type,
        title=proposal.title,
        description=proposal.description,
        rationale=proposal.rationale,
        verification_status=verification_status,
        search_terms_used=search_terms,
        searches_run=searches_run,
    )


def _line_for_file(inventory: list[GapLocation], file: str) -> int | None:
    for loc in inventory:
        if loc.file == file:
            return loc.line
    return None
