"""Helpers that keep agent-supplied values from changing the shape of a SOQL query."""

from __future__ import annotations

import datetime as dt
import re

_ID = re.compile(r"^[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?$")


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def literal(value: str) -> str:
    return f"'{_escape(value)}'"


def contains(value: str) -> str:
    """LIKE pattern matching ``value`` anywhere; caller-supplied % and _ are matched literally."""
    escaped = _escape(value).replace("%", "\\%").replace("_", "\\_")
    return f"'%{escaped}%'"


def record_id(value: str) -> str:
    if not _ID.match(value):
        raise ValueError(f"not a Salesforce record id: {value!r}")
    return f"'{value}'"


def date(value: dt.date | str) -> str:
    if isinstance(value, str):
        value = dt.date.fromisoformat(value)
    return value.isoformat()
