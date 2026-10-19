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
RATE_LIMITED = Counter("mme_tel_rate_limited_total", "Requests rejected by rate limiting", registry=REGISTRY)


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
