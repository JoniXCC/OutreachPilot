"""Input validation and sanitisation (pure Python)."""

from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
from urllib.parse import urlparse

from email_validator import EmailNotValidError, validate_email

from app.utils.helpers import normalize_domain

# RFC 2606 / RFC 6761 reserved names - safe for demos, must never be "really" sent to.
RESERVED_DOMAINS = frozenset({"example.com", "example.org", "example.net"})
RESERVED_TLDS = frozenset({"example", "test", "invalid", "localhost"})

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ValidationError(ValueError):
    """Raised when user-supplied data is invalid."""


def is_valid_email(email: str | None) -> bool:
    """Syntax check only (no DNS lookups -> fast, offline, free)."""
    if not email or len(email) > 254:
        return False
    try:
        validate_email(email.strip(), check_deliverability=False, test_environment=True)
    except EmailNotValidError:
        return False
    return True


def is_reserved_domain(email_or_domain: str) -> bool:
    """True for example.com-style domains that must never receive real email."""
    domain = normalize_domain(email_or_domain)
    if not domain:
        return False
    if domain in RESERVED_DOMAINS or any(domain.endswith("." + d) for d in RESERVED_DOMAINS):
        return True
    return domain.rsplit(".", 1)[-1] in RESERVED_TLDS


def sanitize_text(value: str | None, max_length: int = 2000) -> str:
    """Normalise unicode, drop control characters and cap length."""
    if value is None:
        return ""
    value = unicodedata.normalize("NFKC", str(value))
    value = _CONTROL_CHARS.sub("", value)
    return value.strip()[:max_length]


def sanitize_single_line(value: str | None, max_length: int = 255) -> str:
    """Like :func:`sanitize_text` but also collapses newlines (header-injection safe)."""
    return " ".join(sanitize_text(value, max_length * 2).split())[:max_length]


def validate_public_url(url: str, resolve_dns: bool = True) -> str:
    """Ensure a URL is http(s) and does not point at a private/internal address.

    Protects the research module against SSRF (e.g. a CSV containing
    ``http://127.0.0.1:8000/admin`` or cloud metadata endpoints).
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValidationError(f"unsupported URL scheme: {parsed.scheme or 'none'}")
    host = parsed.hostname
    if not host:
        raise ValidationError("URL has no host")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValidationError("local hosts are not allowed")
    if parsed.port not in (None, 80, 443):
        raise ValidationError("non-standard ports are not allowed")

    addresses: list[str] = []
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        if resolve_dns:
            try:
                infos = socket.getaddrinfo(host, None)
            except socket.gaierror as exc:
                raise ValidationError(f"cannot resolve host {host}") from exc
            addresses = sorted({info[4][0] for info in infos})
    for addr in addresses:
        ip = ipaddress.ip_address(addr.split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise ValidationError(f"host {host} resolves to a non-public address")
    return url
