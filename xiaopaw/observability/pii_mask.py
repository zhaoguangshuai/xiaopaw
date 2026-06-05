"""PII masking for log output."""

from __future__ import annotations

import re

_PII_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"1[3-9]\d{9}"), lambda m: m.group()[:3] + "****" + m.group()[-4:]),
    (re.compile(r"[\w.+-]+@[\w-]+\.\w+"), lambda _: "***@***"),
    (re.compile(r"\d{17}[\dXx]"), lambda m: m.group()[:6] + "********" + m.group()[-4:]),
]


def mask_pii(text: str) -> str:
    for pattern, replacer in _PII_RULES:
        text = pattern.sub(replacer, text)
    return text
