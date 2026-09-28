"""Rede: cliente HTTP seguro e proteção SSRF."""

from osintizada.net.http_client import (
    HTTPResult,
    ResponseTooLargeError,
    SafeHTTPClient,
    parse_retry_after,
)
from osintizada.net.ssrf import (
    UnsafeURLError,
    ValidatedTarget,
    ip_block_reason,
    is_public_ip,
    resolve_and_validate,
    validate_url,
)

__all__ = [
    "HTTPResult", "ResponseTooLargeError", "SafeHTTPClient", "UnsafeURLError", "is_public_ip",
    "parse_retry_after", "validate_url", "ValidatedTarget", "ip_block_reason", "resolve_and_validate",
]
