"""The HTTP client loggers stay at WARNING whatever LOG_LEVEL is (at DEBUG they log request headers, i.e. the Authorization key)."""
import io
import logging

import pytest

from app.core.logging import FORCED_WARNING_LOGGERS, setup_logging


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    saved = (list(root.handlers), root.level, {n: logging.getLogger(n).level for n in FORCED_WARNING_LOGGERS})
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved[0]:
        root.addHandler(h)
    root.setLevel(saved[1])
    for n, lv in saved[2].items():
        logging.getLogger(n).setLevel(lv)


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR"])
def test_http_client_loggers_are_warning_for_every_log_level(level, restore_logging):
    setup_logging(level, "text")
    assert set(FORCED_WARNING_LOGGERS) >= {"httpx", "httpcore", "uvicorn.access"}
    for name in FORCED_WARNING_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING


def test_a_debug_record_from_httpcore_is_not_emitted_at_debug_level(restore_logging):
    setup_logging("DEBUG", "text")
    buf = io.StringIO()
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(logging.StreamHandler(buf))
    logging.getLogger("httpcore.http11").debug("send_request_headers authorization=Bearer FAKE")
    logging.getLogger("httpx").info("HTTP Request: POST https://example.invalid")
    logging.getLogger("mme_telephony.ai").debug("our own debug line is still emitted")
    out = buf.getvalue()
    assert "send_request_headers" not in out and "HTTP Request" not in out
    assert "our own debug line" in out
