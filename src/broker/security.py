"""Credential blocking and tax-identifier stripping.

Two independent safety nets used everywhere else in the app:

- ``validate_no_credentials`` rejects any inbound POST payload that looks
  like it contains a password, MFA/OTP code, API key, token or private key.
- ``strip_tax_identifiers`` removes TFNs and Medicare numbers from any text
  before it is sent to the AI provider.
"""
from __future__ import annotations

import re
from typing import Any

TAX_ID_PLACEHOLDER = "[REMOVED_TAX_IDENTIFIER]"


class CredentialDetectedError(ValueError):
    """Raised when a payload appears to contain a secret credential."""


# --- credential blocking -----------------------------------------------

_CREDENTIAL_PATTERNS = [
    re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?key)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(mfa|otp|2fa|one[- ]?time)\s*(code)?\s*[:=]?\s*\d{4,8}\b"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._-]{10,}"),
    re.compile(r"(?i)\btoken\s*[:=]\s*\S{8,}"),
    re.compile(r"sk-[a-zA-Z0-9]{16,}"),
    re.compile(r"-----BEGIN\s+(RSA|EC|OPENSSH|DSA|PGP)?\s*PRIVATE KEY-----"),
    re.compile(r"(?i)\bsecret\s*[:=]\s*\S{6,}"),
]


def _iter_strings(payload: Any):
    if isinstance(payload, str):
        yield payload
    elif isinstance(payload, dict):
        for value in payload.values():
            yield from _iter_strings(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            yield from _iter_strings(value)


def validate_no_credentials(payload: Any) -> None:
    """Raise CredentialDetectedError if payload looks like it carries a secret.

    ``payload`` may be a raw string or any nested combination of dict/list/str
    (e.g. a parsed JSON POST body). Non-string leaves are ignored.
    """
    for text in _iter_strings(payload):
        for pattern in _CREDENTIAL_PATTERNS:
            if pattern.search(text):
                raise CredentialDetectedError(
                    "Input rejected: it looks like it may contain a password, "
                    "MFA code, API key, token or private key. Remove any "
                    "credentials and try again."
                )


# --- tax identifier stripping -------------------------------------------

_LABELLED_TAX_ID = re.compile(
    r"(?i)\b(tax\s*file\s*number|tfn|medicare\s*(number|no)?)\s*[:#]?\s*"
    r"\d[\d\s-]{6,12}\d"
)
_BARE_TFN = re.compile(r"\b\d{3}[\s-]\d{3}[\s-]\d{3}\b")
_BARE_MEDICARE = re.compile(r"\b\d{4}[\s-]\d{5}[\s-]\d{1,2}\b")


def strip_tax_identifiers(text: str) -> str:
    """Replace TFNs and Medicare numbers with a placeholder.

    Handles labelled values ("TFN: 123 456 789", "Medicare Number: 2234
    56789 1") as well as bare 3-3-3 digit groups (TFN shape) and bare
    4-5-1/4-5-2 digit groups (Medicare shape). Dollar amounts, dates, names
    and balances are left untouched.
    """
    if not text:
        return text
    text = _LABELLED_TAX_ID.sub(TAX_ID_PLACEHOLDER, text)
    text = _BARE_TFN.sub(TAX_ID_PLACEHOLDER, text)
    text = _BARE_MEDICARE.sub(TAX_ID_PLACEHOLDER, text)
    return text
