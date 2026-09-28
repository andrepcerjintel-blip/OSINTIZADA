import io
import json
import logging

from osintizada.observability import case_context, configure_logging


def test_structured_json_logs_with_case_and_redaction():
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    log = logging.getLogger("osintizada.providers")
    with case_context("case-123"):
        log.info("provider search finished token=abc123", extra={"provider": "infra.dns", "operation": "search",
                                                                  "status": "SUCCESS", "duration_ms": 12.5})
    log.info("fora do case")
    first, second = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert first["case_id"] == "case-123" and first["provider"] == "infra.dns"
    assert first["status"] == "SUCCESS" and first["duration_ms"] == 12.5 and "timestamp" in first
    assert "abc123" not in first["message"]
    assert "case_id" not in second
    configure_logging("WARNING")
