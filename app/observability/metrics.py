"""Prometheus metrics. Labels are bounded enums only (never numbers, call ids or tenants)."""
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

REGISTRY = CollectorRegistry()

ACTIVE_CALLS = Gauge("mme_tel_active_calls", "Calls consuming a channel", registry=REGISTRY)
QUEUED_CALLS = Gauge("mme_tel_queued_calls", "Calls waiting in queue", registry=REGISTRY)
CALLS_CREATED = Counter("mme_tel_calls_created_total", "Calls accepted", ["direction"], registry=REGISTRY)
CALLS_TERMINAL = Counter("mme_tel_calls_terminal_total", "Calls reaching a terminal state", ["state"], registry=REGISTRY)
HANGUP_CAUSES = Counter("mme_tel_hangup_cause_total", "Normalized failure/hangup reasons", ["reason"], registry=REGISTRY)
ORIGINATIONS = Counter("mme_tel_originations_total", "Origination attempts", ["outcome"], registry=REGISTRY)
SETUP_LATENCY = Histogram("mme_tel_call_setup_seconds", "Initiating -> answered", registry=REGISTRY,
                          buckets=(0.5, 1, 2, 4, 8, 15, 30, 60))
RING_DURATION = Histogram("mme_tel_ring_seconds", "Ring duration", registry=REGISTRY, buckets=(1, 3, 6, 10, 20, 30, 45, 90))
CONNECTED_DURATION = Histogram("mme_tel_connected_seconds", "Connected duration", registry=REGISTRY,
                               buckets=(5, 15, 30, 60, 120, 180, 300, 600, 1800))
PROVIDER_ERRORS = Counter("mme_tel_provider_errors_total", "Provider errors", ["kind"], registry=REGISTRY)
ESL_CONNECTED = Gauge("mme_tel_esl_connected", "1 if the ESL event stream is connected", registry=REGISTRY)
ESL_RECONNECTS = Gauge("mme_tel_esl_reconnects", "ESL reconnect count", registry=REGISTRY)
ESL_EVENT_LAG = Gauge("mme_tel_esl_event_lag_seconds", "Queue latency of the latest event", registry=REGISTRY)
ESL_QUEUE_DEPTH = Gauge("mme_tel_esl_queue_depth", "Pending ESL events", registry=REGISTRY)
ESL_EVENTS_DROPPED = Gauge("mme_tel_esl_events_dropped", "Events dropped (queue full)", registry=REGISTRY)
ESL_CMD_FAILURES = Gauge("mme_tel_esl_command_failures", "ESL command failures", registry=REGISTRY)
EVENTS = Counter("mme_tel_events_processed_total", "Call events processed", ["outcome"], registry=REGISTRY)
WEBHOOK_DELIVERIES = Counter("mme_tel_webhook_deliveries_total", "Webhook delivery results", ["result"], registry=REGISTRY)
WEBHOOK_LATENCY = Histogram("mme_tel_webhook_seconds", "Webhook delivery latency", registry=REGISTRY)
WEBHOOK_DROPPED = Counter("mme_tel_webhook_dropped_total", "Events dropped: queue saturated", registry=REGISTRY)
DB_PING_MS = Gauge("mme_tel_db_ping_ms", "Latest DB ping", registry=REGISTRY)
DB_FAILURES = Gauge("mme_tel_db_failures", "DB ping failures", registry=REGISTRY)
API_LATENCY = Histogram("mme_tel_api_seconds", "API latency", ["route_group", "status_class"], registry=REGISTRY,
                        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5))
RECONCILE_FIXES = Counter("mme_tel_reconcile_total", "Reconciliation actions", ["action"], registry=REGISTRY)
RECORDING_FAILURES = Counter("mme_tel_recording_failures_total", "Recording failures", registry=REGISTRY)
SESSIONS = Counter("mme_tel_sessions_total", "Session state changes", ["state"], registry=REGISTRY)
AI_REQUESTS = Counter("mme_tel_ai_requests_total", "Model service requests", ["service", "outcome"], registry=REGISTRY)
AI_LATENCY = Histogram("mme_tel_ai_seconds", "Model service latency", ["service"], registry=REGISTRY,
                       buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30))
AI_SESSIONS = Counter("mme_tel_ai_sessions_total", "AI session lifecycle", ["state"], registry=REGISTRY)
LOGINS = Counter("mme_tel_logins_total", "Dashboard logins", ["outcome"], registry=REGISTRY)
PLIVO_CALLBACKS = Counter("mme_tel_plivo_callbacks_total", "Plivo callbacks by route and outcome", ["route", "outcome"], registry=REGISTRY)
PLIVO_STREAMS = Counter("mme_tel_plivo_streams_total", "Plivo media streams by outcome", ["outcome"], registry=REGISTRY)
PLIVO_STREAMS_ACTIVE = Gauge("mme_tel_plivo_streams_active", "Open Plivo media streams", registry=REGISTRY)
AI_TURN_SECONDS = Histogram("mme_tel_ai_turn_seconds", "End of caller speech -> reply audio ready (STT+RAG+Brain+TTS)", registry=REGISTRY,
                            buckets=(0.25, 0.5, 1, 1.5, 2, 3, 5, 8, 13, 20, 30))
AI_TURN_ERRORS = Counter("mme_tel_ai_turn_errors_total", "AI turns that produced no audio", ["code"], registry=REGISTRY)
AI_RUNTIMES = Gauge("mme_tel_ai_runtimes", "AI conversations holding resources in this process", registry=REGISTRY)
DRAINING = Gauge("mme_tel_draining", "1 while the service is draining for shutdown", registry=REGISTRY)
RECOVERY = Counter("mme_tel_recovery_total", "Startup recovery actions", ["action"], registry=REGISTRY)
AI_AUTOSTART = Counter("mme_tel_ai_autostart_total", "AI auto-start on answered FreeSWITCH inbound calls", ["result"], registry=REGISTRY)
RATE_LIMITED = Counter("mme_tel_rate_limited_total", "Requests rejected by rate limiting", registry=REGISTRY)
TTS_RETRIES = Counter("mme_tel_tts_retries_total", "TTS retry decisions", ["decision", "reason"], registry=REGISTRY)
TTS_RECOVERY_OUTCOMES = Counter("mme_tel_tts_recovery_outcomes_total", "TTS recovery final outcomes", ["outcome"], registry=REGISTRY)


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST

# --- inbound capture / turn lifecycle (per-process; never labelled with call ids, numbers or text)
AI_FRAMES = Counter("mme_tel_ai_frames_total", "Inbound audio frames by capture outcome", ["outcome"], registry=REGISTRY)
AI_CAPTURE_REJECTED = Counter("mme_tel_ai_capture_rejected_total", "Captured audio rejected before STT, by reason", ["reason"], registry=REGISTRY)
AI_UTTERANCES = Counter("mme_tel_ai_utterances_total", "Captured utterances by terminal state", ["state"], registry=REGISTRY)
AI_QUEUE_EVENTS = Counter("mme_tel_ai_queue_events_total", "Utterance queue events", ["event"], registry=REGISTRY)
AI_QUEUE_DEPTH = Gauge("mme_tel_ai_queue_depth", "Utterances waiting for the turn worker (all calls)", registry=REGISTRY)
AI_QUEUE_WAIT = Histogram("mme_tel_ai_queue_wait_seconds", "Utterance wait between endpoint and the start of its turn", registry=REGISTRY,
                          buckets=(0.01, 0.05, 0.25, 1, 3, 8, 20, 60))
AI_UTTERANCE_SECONDS = Histogram("mme_tel_ai_utterance_seconds", "Audio duration handed to STT per utterance", registry=REGISTRY,
                                 buckets=(0.25, 0.5, 1, 2, 4, 8, 16, 30))
AI_STT_SUBMISSIONS = Counter("mme_tel_ai_stt_submissions_total", "Utterances submitted to STT", ["kind"], registry=REGISTRY)
AI_BARGE_IN = Counter("mme_tel_ai_barge_in_total", "Barge-in detections", registry=REGISTRY)
AI_TURN_CANCELLED = Counter("mme_tel_ai_turn_cancelled_total", "In-flight turns invalidated by newer caller speech", ["reason"], registry=REGISTRY)
AI_STALE_REJECTED = Counter("mme_tel_ai_stale_rejected_total", "Assistant audio/replies rejected because their turn was invalidated", ["kind"], registry=REGISTRY)
AI_CALLER_COMMIT = Counter("mme_tel_ai_caller_commit_total", "Caller transcripts committed to conversation history", ["status"], registry=REGISTRY)
