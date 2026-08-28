"""Shared Chinese terminology for user-facing GUI copy."""

from __future__ import annotations

import re


RENAME_PLAN_TERM = "整理计划"

_GUI_TERM_REPLACEMENTS = (
    (re.compile(r"\bplan\s+fingerprint\b", re.IGNORECASE), "整理计划版本标识"),
    (re.compile(r"\bplan\s+metadata\b", re.IGNORECASE), "整理计划元数据"),
    (re.compile(r"\bplan\s+entr(?:y|ies)\b", re.IGNORECASE), "整理计划条目"),
    (re.compile(r"\bplan\s+schema\b", re.IGNORECASE), "整理计划架构"),
    (re.compile(r"execution[ _-]+ledger", re.IGNORECASE), "本次整理记录"),
    (re.compile(r"series[_ -]+key", re.IGNORECASE), "作品标识"),
    (re.compile(r"ledger", re.IGNORECASE), "本次整理记录"),
    (re.compile(r"snapshot", re.IGNORECASE), "记录"),
    (re.compile(r"binding", re.IGNORECASE), "关联"),
    (re.compile(r"fingerprint", re.IGNORECASE), "版本标识"),
    (re.compile(r"revision", re.IGNORECASE), "版本"),
    (re.compile(r"\bplan\b", re.IGNORECASE), RENAME_PLAN_TERM),
    (re.compile(r"\bverifier\b", re.IGNORECASE), "结构校验"),
)


def localize_plan_term(value: object) -> str:
    """Translate internal Plan terminology before showing it in the GUI."""

    localized = str(value)
    for pattern, replacement in _GUI_TERM_REPLACEMENTS:
        localized = pattern.sub(replacement, localized)
    return localized
