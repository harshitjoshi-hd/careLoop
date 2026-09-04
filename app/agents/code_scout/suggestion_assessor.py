"""The LLM layer for Code Scout's alternate Suggestion flow (Rev 3, PR #3),
kept additive alongside assessor.py's CodeGapAssessor - see contracts.py's
module docstring, decision #11.

StubFeatureSuggestionAssessor is the Day-1 stand-in: rule-based against the
two real areas explored live (bintan/consultation, timor/oms), so this flow
can run end-to-end before sphere-platform is wired up.
SpherePlatformFeatureSuggestionAssessor is the real LLM-backed
implementation - same interface, no caller changes needed to swap one for
the other.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import requests

from app.agents.code_scout.errors import CodeScoutExternalError
from app.agents.code_scout.explore_search_client import GapLocation
from app.schemas.contracts import Finding, SuggestionType

SPHERE_PROJECT_ID = "7121"  # AI Studio project hosting code-gap-assessment - informational only
SPHERE_USE_CASE = "code-gap-assessment"
SPHERE_TEMPLATE_ID = 21689  # output_schema is not applied server-side without this
SPHERE_SERVICE_TYPE = "code_scout"  # unconfirmed for Code Scout specifically - see propose_suggestions() note


@dataclass
class SuggestionProposal:
    """One suggestion before verification. `signature` + `evidence_file` are
    only meaningful for suggestion_type="tech" - they're what
    check_within_file() checks to see whether this is already built."""

    suggestion_type: SuggestionType
    title: str
    description: str
    rationale: str
    signature: Optional[str] = None
    evidence_file: Optional[str] = None


class FeatureSuggestionAssessor(Protocol):
    def propose_search_terms(self, finding: Finding) -> list[str]: ...

    def propose_suggestions(
        self, finding: Finding, inventory: list[GapLocation]
    ) -> list[SuggestionProposal]: ...


class StubFeatureSuggestionAssessor:
    """Day-1 rule-based stand-in - NOT the real LLM call.

    Hand-written against the two real areas explored live via GitLab: the
    consultation abandon-kill (ConsultationDao.java:146) and the pharmacy
    abandon-kill (BaseCancellationTypeAdapterService's abandonOrderV2, the
    method the timer-driven AbandonOrderService actually calls - traced its
    full body: zero notification calls). Each area produces a MIX of tech
    and business/process suggestions, deliberately - the whole point of this
    flow is that a fix doesn't have to be code.

    Unlike an earlier version of this stub, an area with no hand-verified
    suggestion returns [] rather than a generic "Investigate further"
    placeholder - that placeholder shipped as suggestion_type="tech" with a
    normal title, indistinguishable on a projector from a real hand-verified
    recommendation (PR #3 review D1.3). Report zero honestly instead.
    """

    def propose_search_terms(self, finding: Finding) -> list[str]:
        if finding.journey_events:
            return finding.journey_events
        if finding.origin == "voc" and finding.theme_search_terms:
            return finding.theme_search_terms
        # Crude Day-1 fallback - the real LLM call replaces this entirely.
        words = [w.strip(",.") for w in finding.hypothesis.split() if len(w) > 4]
        return words[:5] or [finding.hypothesis]

    def propose_suggestions(
        self, finding: Finding, inventory: list[GapLocation]
    ) -> list[SuggestionProposal]:
        for loc in inventory:
            if "ConsultationDao" in loc.file:
                return self._consultation_suggestions(finding, loc)
            if "CancellationTypeAdapterService" in loc.file:
                return self._pharmacy_abandon_suggestions(finding, loc)
        return []

    def _consultation_suggestions(self, finding: Finding, loc: GapLocation) -> list[SuggestionProposal]:
        magnitude = _evidence_phrase(finding)
        return [
            SuggestionProposal(
                suggestion_type="tech",
                title="Re-engagement call before consultation abandon",
                description=(
                    "Call Garuda's re-engagement gateway before the timeout script "
                    "kills a stuck consultation."
                ),
                rationale=(
                    "GET_ABANDON_CONSULTATION (ConsultationDao.java:146) silently kills "
                    "consultations in requested/payment_processing/payment_failed past "
                    "timeout, with no notification anywhere in that path."
                ),
                signature="garuda",
                evidence_file=loc.file,
            ),
            SuggestionProposal(
                suggestion_type="business",
                title="Payment-retry grace period",
                description=(
                    "Offer a short grace-period SMS/WhatsApp reminder with a one-tap "
                    "'resume payment' link before the timeout fires, instead of a "
                    "silent cancellation."
                ),
                rationale=(
                    "The abandon-kill is purely timer-driven with no user-facing "
                    f"warning - a process change (not a code fix) could recover some "
                    f"of the {magnitude} lost here."
                ),
            ),
        ]

    def _pharmacy_abandon_suggestions(self, finding: Finding, loc: GapLocation) -> list[SuggestionProposal]:
        magnitude = _evidence_phrase(finding)
        return [
            SuggestionProposal(
                suggestion_type="tech",
                title="Re-engagement call before order abandon",
                description="Call Garuda before abandonOrderV2 completes.",
                rationale=(
                    "abandonOrderV2 - the method the timer-driven AbandonOrderService "
                    "actually calls - reverses benefits/rewards/payment links and marks "
                    "the order failed, but never calls a notification method."
                ),
                signature="garuda",
                evidence_file=loc.file,
            ),
            SuggestionProposal(
                suggestion_type="tech",
                title="Reuse the existing communication hook",
                description=(
                    "Wire the sendCommunication call (already used by "
                    "cancelOrderAndNotifyUser in this same class) into abandonOrderV2 too."
                ),
                rationale=(
                    "The capability already exists in this file, just isn't invoked "
                    "from the timer-driven abandon path - cheaper than building "
                    "something new."
                ),
                signature="sendCommunication",
                evidence_file=loc.file,
            ),
            SuggestionProposal(
                suggestion_type="business",
                title="Cart-recovery incentive",
                description=(
                    "Offer a small discount or reminder nudge when an order sits in "
                    "payment_processing/payment_failed beyond a threshold, instead of "
                    "a silent timeout-driven abandon."
                ),
                rationale=(
                    f"Based on {magnitude}, orders are abandoned on a timer with no "
                    "user-facing recovery moment - a policy change could recover some "
                    "of this independent of any code fix."
                ),
            ),
        ]


def _evidence_phrase(finding: Finding) -> str:
    if finding.evidence:
        item = finding.evidence[0]
        value = f"{item.value:,.0f}" if item.value == int(item.value) else f"{item.value:,}"
        return f"{value}/wk ({item.metric})"
    if finding.origin == "voc" and finding.review_count:
        return f"{finding.review_count} user reports"
    return f"the drop-off described in \"{finding.hypothesis}\""


class SpherePlatformFeatureSuggestionAssessor:
    """Real LLM-backed assessor. Implements the exact same interface as
    StubFeatureSuggestionAssessor - callers need zero changes to swap one
    for the other.

    Endpoint/template_id/status-check confirmed live by Nakul's own
    app/integrations/sphere.py (PR #2) - the original version of this file
    (PR #3, first draft) got all three wrong before that cross-check:
      1. Endpoint is POST {SPHERE_BASE_URL}/v1/chat-ai/requests/validation,
         not .../requests.
      2. `template_id` is REQUIRED in the body - without it the use case's
         output_schema is not applied server-side and you get fenced prose
         back instead of JSON. code-gap-assessment = use_case_id 12814,
         template_id 21689 (project 7121 and the use_case string were
         already correct).
      3. A failed call can arrive as HTTP 200 with `status: "FAILED"` in the
         body - resp.raise_for_status() alone does not catch that. Checked
         explicitly below.

    ASSUMPTIONS THAT STILL NEED CONFIRMING (flagged, not silently guessed):
      1. "code-gap-assessment" is passed as the `use_case` field's literal
         string value, not resolved to a separate numeric use_case_id first.
      2. `service_type` has no confirmed value for Code Scout specifically -
         "code_scout" is a placeholder. Nakul's own client defaults to
         "funnel-analysis", but that's the Analyst's own service_type, not
         necessarily Code Scout's - copying it verbatim would just swap one
         unverified guess for a different, more likely wrong one.
      3. The response's `data.suggestions` shape (matching SuggestionProposal)
         assumes the use case's prompt template has its output_schema set to
         match - a real dependency on the template's own setup, not just
         this client.
    """

    def __init__(self, base_url: Optional[str] = None, app_token: Optional[str] = None):
        # Same host/token resolution as every other sphere caller in this
        # repo. The first live run to reach this node (run 23) died in this
        # constructor with KeyError: 'SPHERE_BASE_URL' — nothing else reads
        # that variable.
        from app.config import get_settings
        from app.integrations.sphere import _app_token, _base_url
        self.base_url = (base_url or _base_url()).rstrip("/")
        self.app_token = app_token or os.environ.get("SPHERE_APP_TOKEN") or _app_token()
        self.service_type = get_settings().sphere_platform_service_type

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "x-app-token": self.app_token}

    def _call(self, params: dict) -> dict[str, Any]:
        try:
            resp = requests.post(
                f"{self.base_url}/v1/chat-ai/requests/validation",
                headers=self._headers(),
                json={
                    "use_case": SPHERE_USE_CASE,
                    "template_id": SPHERE_TEMPLATE_ID,
                    "service_type": self.service_type,
                    # Template 21689 renders exactly one placeholder, {code_context};
                    # sphere ignores every other param key silently, so the raw task
                    # dict here produced an EMPTY prompt. Same convention as
                    # assessor.py / remedy loop: the whole ctx as one JSON string.
                    "params": {"code_context": json.dumps(params)},
                },
                timeout=75,   # the call is synchronous for the whole model run; ingress cuts at ~60 s
            )
            resp.raise_for_status()
            body = resp.json()
        except requests.RequestException as exc:
            raise CodeScoutExternalError(f"Sphere Platform call failed (task={params.get('task')!r}): {exc}") from exc
        except ValueError as exc:
            raise CodeScoutExternalError(
                f"Sphere Platform returned invalid JSON (task={params.get('task')!r}): {exc}"
            ) from exc

        if body.get("status") != "SUCCESS":
            raise CodeScoutExternalError(
                f"Sphere Platform call did not succeed (task={params.get('task')!r}): {str(body)[:300]}"
            )
        return body.get("data") or {}

    def propose_search_terms(self, finding: Finding) -> list[str]:
        if finding.journey_events:
            return finding.journey_events
        data = self._call(
            {"task": "propose_search_terms", "hypothesis": finding.hypothesis, "stage": finding.stage}
        )
        try:
            return data["search_terms"]
        except (KeyError, TypeError) as exc:
            raise CodeScoutExternalError(f"Sphere response missing data.search_terms: {exc}") from exc

    def propose_suggestions(
        self, finding: Finding, inventory: list[GapLocation]
    ) -> list[SuggestionProposal]:
        data = self._call(
            {
                "task": "propose_suggestions",
                "hypothesis": finding.hypothesis,
                "stage": finding.stage,
                "journey_events": finding.journey_events,
                "confidence": finding.confidence,
                "evidence": [
                    {"type": e.type, "metric": e.metric, "value": e.value} for e in finding.evidence
                ],
                "segments": [
                    {"dimension": s.dimension, "value": s.value} for s in finding.segments
                ],
                "review_count": finding.review_count,
                "inventory": [
                    {"file": loc.file, "line": loc.line, "snippet": loc.snippet}
                    for loc in inventory
                ],
            }
        )
        try:
            items = data["suggestions"]
            return [
                SuggestionProposal(
                    suggestion_type=item["suggestion_type"],
                    title=item["title"],
                    description=item["description"],
                    rationale=item["rationale"],
                    signature=item.get("signature"),
                    evidence_file=item.get("evidence_file"),
                )
                for item in items
            ]
        except (KeyError, TypeError) as exc:
            raise CodeScoutExternalError(f"Sphere response missing expected suggestion fields: {exc}") from exc
