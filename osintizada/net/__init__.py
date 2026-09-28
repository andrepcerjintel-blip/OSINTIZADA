"""Rede: cliente HTTP seguro e proteção SSRF."""

from osintizada.net.http_client import (
    HTTPResult,
    ResponseTooLargeError,
    SafeHTTPClient,
    parse_retry_after,
)
from osintizada.net.ssrf import UnsafeURLError, is_public_ip, validate_url

__all__ = [
    "HTTPResult", "ResponseTooLargeError", "SafeHTTPClient", "UnsafeURLError", "is_public_ip",
    "parse_retry_after", "validate_url",
]
