import logging
import re

SENSITIVE = re.compile(r"password|secret|token|authorization|cookie|api.?key|private|credential|proof", re.I)
PATTERNS = [
    (re.compile(r"(https?://[^\s?]+)\?[^\s]+"), r"\1?[REDACTED]"),
    (re.compile(r"(\w+://)[^/@\s]+:[^/@\s]+@"), r"\1[REDACTED]@"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "[JWT]"),
    (re.compile(r"\b(?:sk|pk)-(?:lf-)?[A-Za-z0-9_-]{8,}\b"), "[KEY]"),
    (re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[PHONE]"),
    (re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\w)"), "[ID]"),
    (re.compile(r"(?i)(Bearer|DPoP)\s+[^\s,;]+"), "[AUTH]"),
    (
        re.compile(r"(?i)(password|secret|token|api_key)(['\"]?\s*[:=]\s*['\"]?)[^\s,}\"']+"),
        r"\1\2[REDACTED]",
    ),
]


def redact(value, secret_values=()):
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]" if SENSITIVE.search(str(k)) else redact(v, secret_values)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, secret_values) for v in value]
    if isinstance(value, str):
        for secret in secret_values:
            if secret and len(secret) >= 6:
                value = value.replace(secret, "[REDACTED]")
        for pattern, replacement in PATTERNS:
            value = pattern.sub(replacement, value)
        return value
    return value


class RedactionFilter(logging.Filter):
    def __init__(self, secret_values=()):
        super().__init__()
        self.secret_values = secret_values

    def filter(self, record):
        record.msg = redact(record.getMessage(), self.secret_values)
        record.args = ()
        if record.exc_info:
            record.msg += " exception=" + record.exc_info[0].__name__
            record.exc_info = None
            record.exc_text = None
        return True
