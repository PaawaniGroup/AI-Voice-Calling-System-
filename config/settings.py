"""Typed, validated settings for MME_TELEPHONY.

Authoritative sources, highest precedence first:
  1. process environment variables (``MME_TEL_*``)
  2. ``.env`` file (development only; never committed)
  3. defaults declared here
Non-secret policy lives in ``config/telephony.yaml`` (see ``load_policy_file``).
Secrets are only ever read from the environment / secret manager, never YAML.
"""
from __future__ import annotations

import ipaddress
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("mme_telephony.config")

Env = Literal["development", "test", "staging", "production"]


def F(default: Any, desc: str, *, restart: bool = True, dev_only: bool = False, **kw: Any) -> Any:
    """Field helper that records documentation metadata used by the docs generator."""
    extra = {"restart": restart, "dev_only": dev_only}
    return Field(default, description=desc, json_schema_extra=extra, **kw)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MME_TEL_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- Application / runtime -------------------------------------------------
    app_name: str = F("mme-telephony", "Service name used in logs/metrics.")
    app_version: str = F("0.2.0", "Service version.")
    env: Env = F("development", "Deployment environment.")
    bind_host: str = F("127.0.0.1", "API bind address. Terminate TLS in front of it.")
    bind_port: int = F(8800, "API port.", ge=1, le=65535)
    workers: int = F(1, "Uvicorn workers. MUST be 1: the controller owns the ESL event stream.", ge=1, le=1)
    request_timeout_s: float = F(30.0, "Per-request timeout for outbound HTTP made by the service.", gt=0, le=300)
    shutdown_timeout_s: float = F(15.0, "Graceful shutdown budget.", gt=0, le=120)
    max_body_bytes: int = F(65536, "Maximum accepted request body.", ge=1024, le=10_485_760)
    trusted_proxies: str = F("", "Comma-separated CIDRs allowed to set X-Forwarded-For. Empty = none.")
    api_prefix: str = F("/v1", "API version prefix.")
    docs_enabled: bool = F(True, "Expose /docs and /openapi.json. Disable in production.", dev_only=True)
    cors_origins: str = F("", "Comma-separated allowed browser origins. Empty = CORS disabled.")

    # ---- Database -----------------------------------------------------------------
    mongo_uri: SecretStr = F(SecretStr(""), "MongoDB connection URI (secret). Required.")
    mongo_db: str = F("mme_telephony", "Dedicated telephony database name.", min_length=1)
    mongo_connect_timeout_ms: int = F(5000, "Connect timeout.", ge=100, le=60000)
    mongo_server_selection_timeout_ms: int = F(5000, "Server selection timeout.", ge=100, le=60000)
    mongo_max_pool: int = F(50, "Max pool size.", ge=1, le=500)
    mongo_min_pool: int = F(0, "Min pool size.", ge=0, le=100)
    mongo_init_indexes: bool = F(True, "Create indexes/schema version on startup (idempotent).")
    retention_events_days: int = F(180, "Call event retention.", ge=1)
    retention_calls_days: int = F(730, "Call record retention.", ge=1)
    retention_idempotency_hours: int = F(48, "Idempotency record retention.", ge=1)
    retention_audit_days: int = F(365, "Audit log retention.", ge=1)
    retention_dedup_hours: int = F(72, "Provider-callback replay/nonce window.", ge=1)
    retention_interval_s: int = F(3600, "Retention sweep interval.", ge=10)

    # ---- FreeSWITCH / ESL --------------------------------------------------------------
    esl_host: str = F("127.0.0.1", "ESL host. Must be loopback/private unless esl_allow_non_private=true.")
    esl_port: int = F(8021, "ESL port.", ge=1, le=65535)
    esl_password: SecretStr = F(SecretStr(""), "ESL password (secret). Required unless fs_enabled=false.")
    esl_allow_non_private: bool = F(False, "Allow a non-private ESL address. Requires security review.")
    fs_enabled: bool = F(True, "Enable the FreeSWITCH controller.")
    esl_connect_timeout_s: float = F(5.0, "ESL connect+auth timeout.", gt=0, le=60)
    esl_command_timeout_s: float = F(10.0, "Per ESL command timeout.", gt=0, le=120)
    esl_reconnect_initial_s: float = F(0.5, "Initial reconnect delay.", gt=0, le=60)
    esl_reconnect_max_s: float = F(30.0, "Max reconnect delay.", gt=0, le=600)
    esl_reconnect_jitter: float = F(0.25, "Jitter fraction (0..1).", ge=0, le=1)
    esl_event_queue_size: int = F(10000, "Event queue bound.", ge=100)
    esl_event_concurrency: int = F(8, "Concurrent event handlers.", ge=1, le=256)
    esl_idle_timeout_s: float = F(60.0, "No event/heartbeat for this long => connection considered dead.", gt=5)
    reconcile_interval_s: float = F(30.0, "Reconciliation sweep interval.", ge=1)
    reconcile_grace_s: float = F(20.0, "Do not finalize a call younger than this on reconcile.", ge=0)
    reconcile_batch: int = F(200, "Max calls per reconcile pass.", ge=1, le=5000)
    fs_gateway: str = F("mme_carrier", "FreeSWITCH gateway name used for PSTN legs.")
    fs_dial_template: str = F(
        "sofia/gateway/{gateway}/{destination}",
        "Dial-string template. {gateway}, {destination} only. Test mode may use loopback.",
    )
    fs_transfer_template: str = F("sofia/gateway/{gateway}/{destination}", "Dial-string template used by the mme_transfer dialplan bridge.")
    fs_inbound_context: str = F("mme_inbound", "Dialplan context for inbound calls handled by the controller.")
    fs_recordings_dir: str = F("/var/lib/mme_telephony/recordings", "Local spool dir for recordings.")

    # ---- SIP/RTP/WebRTC (documented; FreeSWITCH XML is authoritative) --------------------
    rtp_port_start: int = F(16384, "RTP range start (must match FreeSWITCH switch.conf).", ge=1024, le=65535)
    rtp_port_end: int = F(32768, "RTP range end.", ge=1024, le=65535)
    ring_timeout_s: int = F(45, "Outbound ring timeout.", ge=5, le=180)
    answer_timeout_s: int = F(15, "Inbound: max time to take a routing decision.", ge=1, le=120)
    transfer_timeout_s: int = F(30, "Transfer completion timeout.", ge=5, le=120)
    max_call_duration_s: int = F(1800, "Hard cap on call duration.", ge=10, le=14400)
    initiating_timeout_s: int = F(90, "Max time in 'initiating' before declared failed.", ge=10)
    webrtc_enabled: bool = F(False, "Browser calling. NOT implemented in Phase 2; must remain false.")

    # ---- Provider -------------------------------------------------------------------
    provider: Literal["freeswitch_gateway", "plivo_api", "mock"] = F(
        "freeswitch_gateway", "Call path. 'mock' is rejected outside development/test."
    )
    plivo_auth_id: SecretStr = F(SecretStr(""), "Plivo Auth ID (secret).")
    plivo_auth_token: SecretStr = F(SecretStr(""), "Plivo Auth Token (secret); also verifies webhook signatures.")
    webhook_public_base_url: str = F("", "Public HTTPS base URL Plivo calls back (no trailing slash).")
    plivo_inbound_enabled: bool = F(False, "Accept Plivo Voice-Application callbacks (answer/status/hangup/stream) for INBOUND calls even when "
                                          "provider is not plivo_api. Does not change how outbound calls are placed. Needs PLIVO_AUTH_TOKEN, "
                                          "WEBHOOK_PUBLIC_BASE_URL (https) and JWT_SECRET (signs the stream ticket).")
    plivo_stream_ticket_ttl_s: int = F(120, "Lifetime of the signed ticket Plivo echoes back when it opens the media stream.", ge=30, le=900)
    plivo_stream_connect_s: int = F(20, "After the Answer XML is returned, the media stream must connect within this time or the call is hung up.", ge=5, le=120)
    plivo_stream_max_concurrent: int = F(10, "Maximum simultaneous Plivo media streams (AI conversations) in this process.", ge=1, le=500)
    plivo_orphan_grace_s: int = F(60, "A Plivo call still open this long after max duration, with the provider hangup unconfirmed, is closed locally.", ge=10, le=900)
    drain_timeout_s: int = F(15, "On shutdown: stop accepting new calls, wait this long for AI conversations to end, then close them. Keep below the supervisor's stop timeout.", ge=0, le=300)
    provider_timeout_s: float = F(10.0, "Provider API timeout.", gt=0, le=120)
    provider_max_concurrent: int = F(10, "Provider concurrency ceiling. VERIFY against the Plivo account.", ge=1)
    provider_max_cps: float = F(1.0, "Provider calls-per-second ceiling. VERIFY against the account.", gt=0)
    provider_callback_max_age_s: int = F(300, "Reject provider callbacks older than this (nonce replay window).", ge=30)
    real_calls_enabled: bool = F(False, "Explicit opt-in to dial real PSTN numbers. Default off.")
    estimate_rate_per_min: float = F(0.0, "Estimated carrier rate/minute used ONLY for estimates; 0 disables.", ge=0)

    # ---- Call control / queue ----------------------------------------------------------
    max_active_global: int = F(30, "Global max active calls.", ge=1)
    max_active_per_tenant: int = F(10, "Default per-tenant max active calls.", ge=1)
    max_queued: int = F(200, "Max queued calls (global).", ge=0)
    queue_policy: Literal["queue", "reject"] = F("queue", "On capacity exhaustion: queue or reject (429).")
    queue_max_wait_s: int = F(300, "Queued calls older than this fail with queue_timeout.", ge=1)
    max_initiation_rate: float = F(1.0, "Max originations/second (also bounded by provider_max_cps).", gt=0)
    dispatch_poll_s: float = F(0.25, "Dispatcher poll interval.", gt=0, le=10)
    default_allowed_countries: str = F("IN", "Comma-separated ISO regions allowed by default.")
    default_deny_prefixes: str = F("+911860,+911900,+91140", "Premium/telemarketing prefixes always denied by default.")
    default_region: str = F("IN", "Region for parsing national-format numbers.", min_length=2, max_length=2)

    # ---- Auth / security ---------------------------------------------------------------------
    jwt_secret: SecretStr = F(SecretStr(""), "HS256 verification secret shared with MME identity (secret). Required.")
    jwt_algorithm: Literal["HS256"] = F("HS256", "JWT algorithm.")
    jwt_issuer: str = F("", "Expected 'iss' claim; empty disables the check.")
    jwt_audience: str = F("", "Expected 'aud' claim; empty disables the check.")
    jwt_leeway_s: int = F(0, "Clock-skew tolerance in seconds applied to exp/nbf/iat when verifying tokens. Default 0 (strict). Raise only for a "
                             "host whose clock is known to step (a WSL2 dev machine flips by about 52 s, so 60) or an issuer whose clock differs; "
                             "it extends the validity of an expired token by that many seconds, so never more than a few minutes.", ge=0, le=300)
    jwt_tenant_claim: str = F("tenant_id", "Claim carrying the tenant id.")
    jwt_roles_claim: str = F("roles", "Claim carrying the role list.")
    role_admin: str = F("telephony:admin", "Role granting tenant-admin operations.")
    role_user: str = F("telephony:user", "Role granting call operations.")
    role_platform: str = F("platform:admin", "Role granting cross-tenant/system admin operations.")
    role_viewer: str = F("telephony:viewer", "Read-only dashboard role.")
    login_enabled: bool = F(True, "Enable POST /v1/auth/login (local users in MongoDB, scrypt hashes).")
    jwt_ttl_s: int = F(28800, "Lifetime of tokens issued by /v1/auth/login.", ge=300, le=604800)
    login_rate_limit_per_min: int = F(10, "Failed+successful login attempts per client IP per minute.", ge=1)
    dashboard_enabled: bool = F(True, "Serve the management dashboard at /dashboard.")
    error_log_buffer: int = F(500, "Recent warning/error log records kept in memory for the dashboard.", ge=10, le=10000)
    observe_contexts: str = F("", "Comma-separated FreeSWITCH contexts (e.g. 'public') whose inbound calls are RECORDED ONLY "
                                  "(call+session created for numbers with an inbound route; nothing is answered/rejected/bridged).")
    rate_limit_per_min: int = F(120, "Per-principal API requests/minute.", ge=1)
    rate_limit_call_create_per_min: int = F(30, "Per-tenant POST /calls per minute.", ge=1)
    metrics_token: SecretStr = F(SecretStr(""), "Bearer token required for /metrics. Empty = loopback clients only.")
    status_requires_auth: bool = F(True, "Require auth for /v1/telephony/status.")
    webhook_signing_master: SecretStr = F(SecretStr(""), "Master key deriving per-endpoint outbound webhook secrets.")

    # ---- Recording / storage -------------------------------------------------------------------
    recording_enabled: bool = F(False, "Feature flag. Needs consent_mode + storage configured.")
    recording_consent_mode: Literal["", "announce", "contractual"] = F("", "Consent/disclosure mechanism. Required to record.")
    recording_max_seconds: int = F(3600, "Max recording length.", ge=10)
    recording_retention_days: int = F(90, "Recording retention.", ge=1)
    storage_backend: Literal["none", "local", "s3"] = F("none", "Recording storage backend.")
    s3_bucket: str = F("", "S3 bucket (operator supplied).")
    s3_region: str = F("", "S3 region.")
    s3_prefix: str = F("telephony/recordings/", "S3 key prefix.")
    s3_kms_key_id: str = F("", "SSE-KMS key id; empty = SSE-S3 (AES256).")
    s3_upload_timeout_s: float = F(60.0, "Upload timeout.", gt=0)
    s3_upload_retries: int = F(3, "Upload retry attempts.", ge=0, le=10)
    recording_url_ttl_s: int = F(300, "Presigned download URL lifetime.", ge=30, le=3600)

    # ---- Outbound webhooks ----------------------------------------------------------------------
    webhooks_enabled: bool = F(True, "Deliver outbound lifecycle webhooks.")
    webhook_allow_http: bool = F(False, "Permit http:// destinations.", dev_only=True)
    webhook_allow_private: bool = F(False, "Permit private/loopback destinations (SSRF protection off).", dev_only=True)
    webhook_connect_timeout_s: float = F(3.0, "Connect timeout.", gt=0)
    webhook_read_timeout_s: float = F(10.0, "Read timeout.", gt=0)
    webhook_max_attempts: int = F(8, "Max delivery attempts before dead-letter.", ge=1, le=50)
    webhook_backoff_base_s: float = F(5.0, "Backoff base (exponential, jittered).", gt=0)
    webhook_backoff_max_s: float = F(3600.0, "Backoff cap.", gt=0)
    webhook_queue_limit: int = F(50000, "Max pending deliveries before new ones are dropped+alerted.", ge=1)
    webhook_concurrency: int = F(8, "Concurrent deliveries.", ge=1, le=128)

    # ---- Observability -----------------------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = F("INFO", "Log level.", restart=False)
    log_format: Literal["json", "text"] = F("json", "Log format.")
    tracing_enabled: bool = F(False, "Reserved; tracing not implemented.")
    ready_max_event_age_s: float = F(120.0, "Readiness: FreeSWITCH heartbeat must be fresher than this.", gt=1)

    # ---- AI (Phase 3) -----------------------------------------------------------------------------------
    ai_enabled: bool = F(False, "Global AI switch. Default off. Even when true, an AI session only starts when the media bridge is ready.")
    ai_media_bridge: Literal["none", "ws_fork"] = F("none", "Media bridge implementation. 'none' = not available (default). 'ws_fork' needs a FreeSWITCH media-fork module that is NOT installed on the audited host.")
    ai_media_ws_url: str = F("ws://127.0.0.1:8800/v1/ai/media/fs", "WebSocket URL FreeSWITCH (mod_audio_stream) connects to. Must be reachable FROM FreeSWITCH; use wss:// across hosts.")
    ai_media_token_ttl_s: int = F(120, "Lifetime of the one-off media token handed to FreeSWITCH.", ge=30, le=900)
    ai_media_sample_rate: Literal[8000, 16000] = F(16000, "Sample rate requested from the media module.")
    ai_fs_autostart: bool = F(False, "Start the AI conversation automatically when a FreeSWITCH-delivered INBOUND call whose route action is 'ai' is answered. "
                                     "Requires AI_ENABLED and AI_MEDIA_BRIDGE=ws_fork. Replaces the prototype's MME_FS_MEDIA_ENABLED media service: never run both against the same FreeSWITCH.")
    ai_allowed_hosts: str = F("127.0.0.1,localhost", "Comma-separated host allowlist for model/RAG URLs set through the dashboard.")
    ai_api_key: SecretStr = F(SecretStr(""), "Bearer key sent to model servers (secret; never shown in the UI).")
    ai_request_timeout_s: float = F(15.0, "Default timeout for model service requests.", gt=0, le=120)
    ai_max_concurrency: int = F(4, "Max concurrent model requests per process.", ge=1, le=64)
    ai_stt_model: str = F("Oriserve/Whisper-Hindi2Hinglish-Prime", "STT model identifier (informational; the model server owns the weights).")
    ai_brain_model: str = F("Qwen3-30B-A3B-Instruct-2507", "Brain/LLM model identifier.")
    ai_tts_model: str = F("Qwen/Qwen3-TTS-12Hz-1.7B-Base", "TTS model identifier.")
    ai_tts_reference_wav: str = F("", "Path to the reference voice WAV sent to RunPod TTS workers (voice cloning; the worker rejects requests without one).")
    ai_tts_normalize: bool = F(True, "Write numbers, rupees, dates, phone numbers and acronyms as spoken Devanagari words before TTS (Chatterbox test level 1) and pick the hi/en language tag per sentence.")
    ai_trace_evidence_path: str = F("", "File to which each finished turn appends ONE numbers-only JSON line (job timings, counts; never words). Empty = off. Used for supervised latency calls.")
    ai_ack_dir: str = F("", "Directory OUTSIDE the repository with the cached acknowledgement clips (manifest.json + raw 8 kHz mu-law files, see app/ai/streaming/ack.py). "
                            "Used only by tenants with ai_config.ack_enabled; empty or missing files = the feature is silently unavailable.")
    ai_glossary_path: str = F("", "File OUTSIDE the repository with the operator's said/heard pairs (UTF-8, tab separated: said, heard, optional guard cue|always, optional tenant id; see app/ai/glossary_correction.py). "
                                  "Read at the start of each call of a tenant with glossary_correction; empty or missing = the built-in sound-alikes only.")
    ai_greeting: str = F("Welcome to MME AI Voice Calling Agent.", "Greeting spoken when an inbound call answers before connecting to AI services.")
    ai_script_enforcement: bool = F(False, "Enforce script policy: Hindi/Hinglish in Devanagari, English in Latin. Default False.")
    ai_counsellor_gender: Literal["masculine", "feminine"] = F("masculine", "Gender for Hindi verb forms in counsellor replies ('masculine' or 'feminine').")
    ai_counsellor_name: str = F("MME AI counsellor", "Counsellor name used in system prompt.")
    ai_temperature: float = F(0.3, "Sampling temperature for Brain replies (default 0.3).", ge=0.0, le=2.0)
    ai_script_allowlist_path: str = F("config/script_allowlist_terms.txt", "Path to editable allow-listed English terms file.")
    ai_hint_placement: Literal["user_suffix", "system_message"] = F("user_suffix", "Placement for per-turn mode hint.")

    @property
    def is_prod_like(self) -> bool:
        return self.env in ("staging", "production")

    def csv(self, value: str) -> list[str]:
        return [v.strip() for v in value.split(",") if v.strip()]

    # ------------------------------------------------------------------------------------------
    @field_validator("ai_media_sample_rate", mode="before")
    @classmethod
    def _sample_rate_from_text(cls, v):
        """Environment variables and `.env` values are strings; pydantic does not coerce "16000" to Literal[16000], so the
        documented template line `MME_TEL_AI_MEDIA_SAMPLE_RATE=16000` used to stop the service at startup. Only digit strings
        are converted; anything else still fails the Literal check with its normal message."""
        return int(v) if isinstance(v, str) and v.strip().isdigit() else v

    @model_validator(mode="after")
    def _cross_validate(self) -> "Settings":
        errs, warns = self.problems()
        if errs:
            raise ValueError("; ".join(errs))
        for w in warns:
            log.warning("config: %s", w)
        return self

    def problems(self) -> tuple[list[str], list[str]]:
        """Return (errors, warnings). Messages never contain secret values."""
        e: list[str] = []
        w: list[str] = []
        if self.ai_media_bridge == "ws_fork" and self.is_prod_like and self.ai_media_ws_url.startswith("ws://") and "127.0.0.1" not in self.ai_media_ws_url:
            e.append("AI_MEDIA_WS_URL must use wss:// to a non-loopback host in staging/production")
        if self.ai_enabled and self.ai_media_bridge == "none" and not self.plivo_inbound_enabled:
            w.append("AI_ENABLED=true but AI_MEDIA_BRIDGE=none: AI sessions cannot start until a media bridge is verified")
        if self.ai_fs_autostart and not (self.ai_enabled and self.ai_media_bridge == "ws_fork"):
            e.append("AI_FS_AUTOSTART requires AI_ENABLED=true and AI_MEDIA_BRIDGE=ws_fork")
        if self.plivo_inbound_enabled:
            if not self.plivo_auth_token.get_secret_value():
                e.append("plivo_inbound_enabled requires PLIVO_AUTH_TOKEN")
            if not self.webhook_public_base_url.startswith("https://"):
                e.append("plivo_inbound_enabled requires WEBHOOK_PUBLIC_BASE_URL (https)")
            if not self.jwt_secret.get_secret_value():
                e.append("plivo_inbound_enabled requires JWT_SECRET (the stream ticket key is derived from it)")
            if not self.plivo_auth_id.get_secret_value():
                w.append("PLIVO_AUTH_ID unset: the service cannot hang up Plivo calls through the REST API (hangup/timeouts stay local)")
            if not self.ai_enabled:
                w.append("plivo_inbound_enabled but AI_ENABLED=false: routes with action 'ai' will be rejected")
        if self.webrtc_enabled:
            e.append("MME_TEL_WEBRTC_ENABLED is not implemented in Phase 2 and must be false")
        if self.rtp_port_end <= self.rtp_port_start:
            e.append("RTP port range invalid (end must exceed start)")
        if self.esl_reconnect_max_s < self.esl_reconnect_initial_s:
            e.append("ESL_RECONNECT_MAX_S must be >= ESL_RECONNECT_INITIAL_S")
        if self.max_active_per_tenant > self.max_active_global:
            e.append("MAX_ACTIVE_PER_TENANT must be <= MAX_ACTIVE_GLOBAL")
        if self.provider_max_concurrent < self.max_active_global and self.provider != "mock":
            w.append("provider_max_concurrent < max_active_global: provider ceiling will bind first")
        if self.ring_timeout_s >= self.max_call_duration_s:
            e.append("RING_TIMEOUT_S must be < MAX_CALL_DURATION_S")
        if self.recording_enabled and (
            not self.recording_consent_mode or self.storage_backend == "none"
        ):
            e.append("recording_enabled requires recording_consent_mode and a storage_backend")
        if self.storage_backend == "s3" and not (self.s3_bucket and self.s3_region):
            e.append("storage_backend=s3 requires S3_BUCKET and S3_REGION")
        if not self._esl_private() and not self.esl_allow_non_private:
            e.append("ESL host is not loopback/private; set ESL_ALLOW_NON_PRIVATE only after security review")
        if self.esl_allow_non_private:
            w.append("ESL_ALLOW_NON_PRIVATE is set: ESL must be firewalled and reviewed")
        if self.provider == "plivo_api" and not (
            self.plivo_auth_id.get_secret_value() and self.plivo_auth_token.get_secret_value()
        ):
            e.append("provider=plivo_api requires PLIVO_AUTH_ID and PLIVO_AUTH_TOKEN")
        if self.provider == "plivo_api" and not self.webhook_public_base_url.startswith("https://"):
            e.append("provider=plivo_api requires WEBHOOK_PUBLIC_BASE_URL (https)")

        if self.is_prod_like:
            if self.provider == "mock":
                e.append("mock provider is forbidden in staging/production")
            if not self.mongo_uri.get_secret_value():
                e.append("MONGO_URI is required")
            if len(self.jwt_secret.get_secret_value()) < 32:
                e.append("JWT_SECRET must be set and >= 32 chars")
            if len(self.webhook_signing_master.get_secret_value()) < 32:
                e.append("WEBHOOK_SIGNING_MASTER must be set and >= 32 chars")
            if self.fs_enabled and not self.esl_password.get_secret_value():
                e.append("ESL_PASSWORD is required")
            if self.docs_enabled:
                w.append("DOCS_ENABLED=true in a production-like environment")
            if self.webhook_allow_http or self.webhook_allow_private:
                e.append("webhook_allow_http/private are development-only")
            if self.bind_host in ("0.0.0.0", "::"):
                w.append("API binds to all interfaces; ensure a TLS proxy and firewall front it")
            if self.real_calls_enabled and not self.default_allowed_countries:
                e.append("DEFAULT_ALLOWED_COUNTRIES must be set when real calls are enabled")
        else:
            if self.webhook_allow_private or self.webhook_allow_http:
                w.append("development-only webhook relaxations are enabled")
            if not self.jwt_secret.get_secret_value():
                w.append("JWT_SECRET empty: all authenticated endpoints will reject requests")
        if self.env in ("development", "test") and self.provider != "mock" and not self.real_calls_enabled:
            w.append("real_calls_enabled=false: PSTN-looking destinations will be refused")
        return e, w

    def _esl_private(self) -> bool:
        try:
            ip = ipaddress.ip_address(self.esl_host)
        except ValueError:
            return self.esl_host in ("localhost",)
        return ip.is_loopback or ip.is_private

    def redacted(self) -> dict[str, Any]:
        """Settings for diagnostics with every secret masked."""
        out: dict[str, Any] = {}
        for k, v in self.model_dump().items():
            if isinstance(v, SecretStr):
                out[k] = "***set***" if v.get_secret_value() else "***unset***"
            else:
                out[k] = v
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_policy_file(path: Path | None = None) -> dict[str, Any]:
    """Non-secret telephony policy (config/telephony.yaml). Missing file => {}."""
    p = path or ROOT / "config" / "telephony.yaml"
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("telephony.yaml must be a mapping")
    return data
