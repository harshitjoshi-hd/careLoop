from functools import lru_cache
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Service
    app_name: str = "careloop-service"
    app_token: str = "dev-local-token"
    demo_mode: bool = True
    # demo_mode governs the DATA source: the Fetcher has no live warehouse path
    # yet, so the snapshot must come from the frozen fixture. live_llm forces
    # every LLM and GitLab call to be real anyway — the realistic way to run
    # today: real analysis over frozen, k-anonymised production aggregates.
    live_llm: bool = False

    # Storage
    database_url: str = "sqlite:///./careloop.db"
    artifacts_dir: str = "./data/artifacts"

    # sphere-platform LLM gateway (Analyst / Code Scout / Reporter / PRD use cases). Real
    # endpoint confirmed 2026-09-03 via a live working curl (Krithik): POST
    # {base_url}/v1/chat-ai/requests, header X-App-Token (not Authorization Bearer), body
    # {use_case, service_type, params}. `params` values that are objects/arrays are passed as
    # JSON-encoded STRINGS, not nested JSON — matches the insurance-co-pilot example exactly.
    # Use case names verified against the real AI Studio project — Control Center project 7121
    # ("funnel-analysis"), 5 ACTIVE use cases, SPHERE_INPUT_SANITIZER + SPHERE_OUTPUT_SANITIZER
    # guardrails wired on all 5. https://controlcenter.stage.halodoc.com/ai-studio/prompt-management/projects/7121
    # Stage base URL below is a real, confirmed value. `sphere_platform_app_token` and
    # `sphere_platform_service_type` are still open — the token in the example curl belongs to
    # a different use case/team (item_diagnosis_mapping / sphere-insurance) and won't
    # necessarily authorize our project 7121 use cases; get our own from Nakul, who set up the
    # AI Studio project. service_type is likely project-scoped too (confirm the exact string —
    # possibly "funnel-analysis" to match the project name, but unverified).
    sphere_platform_base_url: str = "http://sphere-platform.stage-k8s.halodoc.com"
    # .env.example documents this secret as SPHERE_PLATFORM_API_KEY, but this field's
    # name would make pydantic-settings look for SPHERE_PLATFORM_APP_TOKEN instead — a
    # real .env carrying the documented name was silently never read, so every live
    # sphere call went out with an empty token and failed as an auth error, not a
    # config error. Confirmed live 2026-09-04: a real key in .env under the documented
    # name produced token_set=False from get_settings() until this alias was added.
    # AliasChoices keeps the field's own name too, in case anything exports it directly.
    sphere_platform_app_token: str = Field(
        default="",
        validation_alias=AliasChoices("SPHERE_PLATFORM_API_KEY", "SPHERE_PLATFORM_APP_TOKEN", "sphere_platform_app_token"),
    )
    sphere_platform_service_type: str = "funnel-analysis"  # confirmed via GET /api/v1/ai-studio/projects/7121/use-cases/search
    llm_use_case_funnel_dropoff: str = "funnel-hypothesis-generation"  # was "funnel-dropoff-analysis" — wrong name, fixed
    llm_use_case_code_gap: str = "code-gap-assessment"
    llm_use_case_trend_narrative: str = "trend-narrative"
    llm_use_case_prd_generation: str = "prd-generation"
    llm_use_case_prd_chat_edit: str = "prd-chat-edit"
    llm_use_case_voc_theme_classification: str = "voc-theme-classification"  # NEW — not previously wired anywhere
    # NOT YET PROVISIONED (2026-09-04) — project 7121 has 5 ACTIVE use cases; this
    # would be a 6th. Needs the same AI Studio setup code-gap-assessment got before
    # _sphere_llm() in pipeline/nodes/analyst.py can look up its template_id.
    llm_use_case_voc_correlation: str = "voc-funnel-correlation"
    # Provisioned 2026-09-04 as project 7121's 6th use case (use_case_id 12870,
    # template_id 21791, single placeholder {edit_inputs} -> {prd_markdown, reply}
    # output_schema) — see fixtures/pd_checkout/sphere_ids.json.
    llm_use_case_prd_chat_edit: str = "prd-chat-edit"

    # Metabase (read-only, Fetcher / Alief)
    metabase_base_url: str = ""
    metabase_api_key: str = ""
    metabase_redshift_db_id: int = 39

    # GitLab (read-only PAT, Code Scout / Harshit). Halodoc is self-hosted — NOT gitlab.com
    # (verified via org memory 2026-09-03).
    gitlab_base_url: str = "https://gitlab.devops.mhealth.tech"
    gitlab_read_token: str = ""

    # Garuda (GChat delivery) — real API verified via org memory 2026-09-03: POST
    # /v1/communication_requests (NOT /v3), auth header X-APP-TOKEN (not Bearer), no
    # self-service template registration endpoint exists. channel_id/provider_id/template_id
    # are free-form attributes that must already be provisioned in Garuda's own config —
    # get real values from whoever owns the Garuda integration, same blocker Harshit's been
    # tracking ("yet to get the webhook", SRE not fully aware). OPEN QUESTION: none of the
    # verified examples show a GChat channel type (only WhatsApp/SMS/Email/Voice) — confirm
    # GChat delivery is even a supported Garuda channel before relying on this for the demo.
    garuda_base_url: str = ""
    garuda_app_token: str = ""
    garuda_channel_id: str = ""
    garuda_provider_id: str = ""
    garuda_template_id: str = ""
    garuda_destination: str = ""  # the channel-specific target (e.g. webhook URL / space id / MSISDN)
    garuda_service_source: str = "careloop-service"

    # Privacy / batch discipline
    k_suppression_floor: int = 25
    query_timeout_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
