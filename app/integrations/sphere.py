"""Thin sphere-platform client with a replay mode.

Modes (env LLM_MODE): "sphere" (live, stage) | "replay" (fixtures/llm_replay).
The Analyst receives this via injection, so tests use StubLLM instead.
Project/template ids: fixtures/pd_checkout/funnel_analysis_ids.json.
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

_DEFAULT_SPHERE_BASE = "http://sphere-platform.stage-k8s.halodoc.com"
SPHERE_BASE = os.environ.get("SPHERE_BASE_URL", _DEFAULT_SPHERE_BASE)   # kept for importers; prefer _base_url()


def _base_url() -> str:
    """Same rule as _app_token(): first NON-EMPTY of the shell names, the settings
    field, and the .env file, else the stage default. Until this existed the host
    came from a module constant that read only SPHERE_BASE_URL from the shell, so
    the SPHERE_PLATFORM_BASE_URL line every .env carries was dead: an empty value
    was harmless, and a deliberate override was silently ignored."""
    from app.config import get_settings
    from dotenv import dotenv_values
    candidates = [
        os.environ.get("SPHERE_BASE_URL"),
        os.environ.get("SPHERE_PLATFORM_BASE_URL"),
        get_settings().sphere_platform_base_url,
    ]
    try:
        env_file = dotenv_values(".env")
        candidates += [env_file.get("SPHERE_PLATFORM_BASE_URL"), env_file.get("SPHERE_BASE_URL")]
    except Exception:
        pass
    return next((c.strip().rstrip("/") for c in candidates if c and c.strip()), _DEFAULT_SPHERE_BASE)
# The single parameter each template's user_message renders. Sphere substitutes
# only the placeholder the template names; every other key is silently ignored
# and the prompt renders EMPTY. A live run went exactly that way — the model
# replied "analysis_context is empty" and the run produced zero findings with
# no error anywhere. call() now refuses a mismatched key instead.
TEMPLATE_PARAM: dict[str, str] = {
    "funnel-hypothesis-generation": "analysis_context",
    "voc-theme-classification":     "reviews_batch",
    "code-gap-assessment":          "code_context",
    "trend-narrative":              "delta_table",
    "prd-generation":               "prd_inputs",
    "prd-chat-edit":                "edit_inputs",  # provisioned 2026-09-04, use_case_id 12870
}


class TemplateParamError(ValueError):
    """The caller sent keys the template cannot render."""


class SphereRequestFailed(RuntimeError):
    """Sphere returned a terminal failure status, or the HTTP call itself failed."""


class SphereRequestTimedOut(RuntimeError):
    """Sphere never reached a terminal status within MAX_POLL_SECONDS."""


# Sphere's own recognised terminal-failure statuses. Not confirmed against
# live sphere-platform docs (no working SPHERE_APP_TOKEN in this dev
# environment) — anything NOT in this set and NOT "SUCCESS" is treated as
# "still in progress, keep polling" rather than guessed at, so an unlisted
# real failure status just means we poll it to the MAX_POLL_SECONDS deadline
# instead of failing fast. Worth confirming against a real run.
_TERMINAL_FAILURE_STATUSES = {"FAILED", "ERROR", "CANCELLED", "CANCELED"}

# LIVE-VERIFIED 2026-09-04 (Nakul): POST /v1/chat-ai/requests is SYNCHRONOUS on
# our sphere deployment — it returns status=SUCCESS with the data inline after the
# whole model call (8.5 s for a 3-review VoC batch; 30-50 s for an Analyst turn),
# and GET /v1/chat-ai/requests/{id} on that id then reports status=INIT with no
# data, so the poll path below can never complete a request the create call did
# not. The create timeout therefore has to cover the full model call. 15 s cut
# off every real call in run 11 (Analyst + all five VoC batches: "timed out") —
# and independently cut off a real PRD-sized prd-chat-edit request the same way.
# The ingress in front of sphere closes held connections at ~60 s, so anything
# past that is the ingress's 504, not ours — keep individual calls small instead.
CREATE_TIMEOUT_S = 75
POLL_TIMEOUT_S = 15      # one status check — comfortably inside any ~60s ingress cutoff
POLL_INTERVAL_S = 2.0
MAX_POLL_SECONDS = 180.0  # generous: real calls have been observed taking 45-75s+


def _check_params(use_case: str, params: dict[str, Any]) -> None:
    expected = TEMPLATE_PARAM.get(use_case)
    if expected is None:
        return
    if set(params) != {expected}:
        raise TemplateParamError(
            f"{use_case}: template renders only {{{expected}}} but caller sent "
            f"{sorted(params)} — the prompt would be empty")


def _app_token() -> str:
    """Shell env wins; otherwise the .env-backed settings.

    Three names had grown for one secret (SPHERE_APP_TOKEN in the shell,
    sphere_platform_app_token in settings, SPHERE_PLATFORM_API_KEY in
    .env.example) and a module-level read of only the first meant a token
    placed in .env never reached this client. Resolved lazily so the API
    server picks it up from .env without an exported shell variable — but
    that alone wasn't enough: Settings.sphere_platform_app_token had no
    alias onto SPHERE_PLATFORM_API_KEY, the name .env.example actually
    documents, so a real key placed under that name was still silently
    never read (get_settings().sphere_platform_app_token stayed "" and
    every live call went out unauthenticated). Fixed in app/config.py via
    validation_alias, confirmed live 2026-09-04.
    """
    # First NON-EMPTY value wins. An alias list alone is not enough: pydantic
    # picks the first name that is *present*, so a placeholder line
    # `SPHERE_PLATFORM_API_KEY=` in .env shadowed a real token stored under
    # SPHERE_PLATFORM_APP_TOKEN and every live call went out with an empty
    # token (run 25 on main: HTTP 401 on the first Analyst call).
    from app.config import get_settings
    from dotenv import dotenv_values
    candidates = [
        os.environ.get("SPHERE_APP_TOKEN"),
        get_settings().sphere_platform_app_token,
        os.environ.get("SPHERE_PLATFORM_APP_TOKEN"),
        os.environ.get("SPHERE_PLATFORM_API_KEY"),
    ]
    try:
        env_file = dotenv_values(".env")
        candidates += [env_file.get("SPHERE_PLATFORM_APP_TOKEN"), env_file.get("SPHERE_PLATFORM_API_KEY"),
                       env_file.get("SPHERE_APP_TOKEN")]
    except Exception:
        pass
    return next((c.strip() for c in candidates if c and c.strip()), "")


def _live_llm_wanted(demo_mode: bool) -> bool:
    from app.config import get_settings
    return (not demo_mode) or bool(get_settings().live_llm)
REPLAY_DIR = Path(os.environ.get("LLM_REPLAY_DIR", "fixtures/llm_replay"))


class SphereClient:
    def __init__(self, mode: Optional[str] = None, service_type: str = "funnel-analysis",
                 replay_root: Optional[Path] = None):
        # Replays are recorded per JOURNEY: fixtures/llm_replay/<journey>/<use_case>/.
        # A pharmacy recording replayed on the consultation journey asks for cuts
        # that do not exist and cites numbers from the wrong funnel.
        self.replay_root = replay_root or REPLAY_DIR
        self.mode = mode or os.environ.get("LLM_MODE", "sphere")
        self.service_type = service_type
        self._replay_counters: dict[str, int] = {}

    def call(self, use_case: str, template_id: int, params: dict[str, str]) -> dict[str, Any]:
        if self.mode == "replay":
            return self._replay(use_case)
        return self._live(use_case, template_id, params)

    def _live(self, use_case: str, template_id: int, params: dict[str, str]) -> dict[str, Any]:
        """
        Create the request, then poll for it instead of holding one long
        connection open. Real calls have been observed taking 45-75s+
        (semantic_voc.py: a 40-review batch ran 50-75s), and there is an
        ingress gateway in front of sphere that cuts a held-open connection
        at ~60s (HTTP 504 — even though sphere itself went on to return
        SUCCESS at 74s in that observed case; org-wide pattern, not unique
        to this call: agent-platform/Mercia hit the identical ~60s cutoff
        against sphere's own /v2/chat-ai/requests). Polling on a short
        interval means no single HTTP call is ever held open long enough to
        hit that cutoff, even though the overall wait can still legitimately
        stretch past it.

        Endpoint split (POST create / GET poll) confirmed via a live probe
        against sphere-platform's own routes — see every other real caller
        (concierge-agent, agent-platform) hitting POST /v1|v2/chat-ai/requests
        + GET /v1/chat-ai/requests/{request_id}, never the single blocking
        call the old code here made. ASSUMPTION NOT YET LIVE-VERIFIED for
        THIS project's own sphere deployment specifically (no working
        SPHERE_APP_TOKEN in this environment to test against) and flagged
        for Nakul, who verified the previous endpoint live: sphere's exact
        pending/in-progress status string is unknown, so _poll() below
        treats anything that isn't a recognised terminal status as "keep
        polling" and leans on the overall MAX_POLL_SECONDS deadline as the
        real backstop, rather than guessing at sphere's full status enum.
        """
        _check_params(use_case, params)
        body = {
            "service_type": self.service_type,
            "use_case": use_case,
            "template_id": template_id,  # required — output_schema is not applied without it
            "params": params,
        }
        created = self._request("POST", "/v1/chat-ai/requests", body=body, timeout=CREATE_TIMEOUT_S)
        data = self._data_or_poll(use_case, created)
        return data

    def _data_or_poll(self, use_case: str, resp: dict[str, Any]) -> dict[str, Any]:
        status = resp.get("status")
        if status == "SUCCESS":  # fast enough to finish inline on the create call itself
            return resp.get("data") or {}
        if status in _TERMINAL_FAILURE_STATUSES:
            raise SphereRequestFailed(f"sphere call failed for {use_case}: {str(resp)[:300]}")

        request_id = resp.get("request_id") or resp.get("id")
        if not request_id:
            raise SphereRequestFailed(
                f"sphere create-request for {use_case} returned neither a terminal status nor "
                f"a request_id to poll against: {str(resp)[:300]}"
            )
        return self._poll(use_case, request_id)

    def _poll(self, use_case: str, request_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + MAX_POLL_SECONDS
        while True:
            resp = self._request("GET", f"/v1/chat-ai/requests/{request_id}", timeout=POLL_TIMEOUT_S)
            status = resp.get("status")
            if status == "SUCCESS":
                return resp.get("data") or {}
            if status in _TERMINAL_FAILURE_STATUSES:
                raise SphereRequestFailed(f"sphere request {request_id} ({use_case}) failed: {str(resp)[:300]}")
            if time.monotonic() >= deadline:
                raise SphereRequestTimedOut(
                    f"sphere request {request_id} ({use_case}) still {status!r} after "
                    f"{MAX_POLL_SECONDS:.0f}s — giving up"
                )
            time.sleep(POLL_INTERVAL_S)

    def _request(self, method: str, path: str, *, timeout: float, body: Optional[dict] = None) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{_base_url()}{path}",
            method=method,
            headers={"X-APP-TOKEN": _app_token(), "Content-Type": "application/json"},
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.URLError as exc:
            raise SphereRequestFailed(f"sphere {method} {path} failed: {exc}") from exc

    def _replay(self, use_case: str) -> dict[str, Any]:
        """Sequential replay: <replay_root>/<use_case>/<n>.json per call."""
        n = self._replay_counters.get(use_case, 0)
        self._replay_counters[use_case] = n + 1
        path = self.replay_root / use_case / f"{n}.json"
        if not path.exists():  # exhausted -> last recorded response, or hard fail
            last = sorted((self.replay_root / use_case).glob("*.json"))
            if not last:
                raise FileNotFoundError(f"no replay fixtures for {use_case} under {REPLAY_DIR}")
            path = last[-1]
        return json.loads(path.read_text())


SPHERE_IDS_PATH = Path("fixtures/pd_checkout/sphere_ids.json")


def replay_root_for(journey: Optional[str]) -> Path:
    """fixtures/llm_replay/<journey> when it exists, else the legacy flat layout."""
    if journey and (REPLAY_DIR / journey).is_dir():
        return REPLAY_DIR / journey
    return REPLAY_DIR


def make_use_case_llm(use_case: str, demo_mode: bool, journey: Optional[str] = None):
    """An `llm(ctx) -> dict` for one sphere use case, or None if unavailable.

    Returning None rather than raising is deliberate: the Reporter and the PRD
    generator both fall back to their deterministic renderers, and a missing
    replay fixture or an unset token should degrade the prose, never fail the
    run. Demo mode replays a recorded session; live mode calls sphere.
    """
    try:
        ids = json.loads(SPHERE_IDS_PATH.read_text())
        template_id = next(u["template_id"] for u in ids["use_cases"]
                           if u["name"] == use_case)
    except Exception:
        return None

    if _live_llm_wanted(demo_mode):
        if not _app_token():
            return None
        client = SphereClient(mode="sphere")
    else:
        root = replay_root_for(journey)
        if not (root / use_case).exists():
            return None                      # nothing recorded yet for this journey
        client = SphereClient(mode="replay", replay_root=root)

    def llm(ctx: dict[str, Any]) -> dict[str, Any]:
        key = TEMPLATE_PARAM.get(use_case)
        if key and set(ctx) == {key}:
            value = ctx[key]
            params = {key: json.dumps(value) if isinstance(value, (dict, list)) else str(value)}
        elif key:
            params = {key: json.dumps(ctx)}       # whole context under the one placeholder
        else:
            params = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
                      for k, v in ctx.items()}
        return client.call(use_case, template_id, params)

    return llm
