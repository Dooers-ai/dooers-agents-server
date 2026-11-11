"""Stable markers for values that change on every run.

Only these fields are ignored, everywhere in the result:

- ``ID_FIELDS``: the value becomes ``<id:N>``. The same value gets the same marker, and
  any other string equal to a known id (e.g. ``form_event_id``) gets that marker too,
  so references between events are still compared.
- ``DATE_FIELDS``, and any ``datetime`` value: ``<ts>``. ``None`` stays ``None``.
- ``URL_FIELD`` when the URL has a host: ``<url:N>``, one marker per artifact. The
  artifact is the URL without query or fragment, so two signed links to the same file
  share a marker. URLs without a host are kept.

Markers are numbered in a fixed walk order (dict keys sorted, lists in order), so
dict key order never changes the numbering. Every other field is compared as is.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from tests.parity.model import ParityResult

ID_FIELDS = frozenset({"id", "event_id", "thread_id", "run_id"})
DATE_FIELDS = frozenset({"created_at", "updated_at", "last_event_at"})
URL_FIELD = "url"

TS_MARKER = "<ts>"


def _sorted_items(mapping: dict) -> list[tuple[Any, Any]]:
    return sorted(mapping.items(), key=lambda item: str(item[0]))


def _collect_ids(value: Any, key: Any, ids: dict[str, str]) -> None:
    if isinstance(value, dict):
        for child_key, child in _sorted_items(value):
            _collect_ids(child, child_key, ids)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _collect_ids(child, key, ids)
    elif key in ID_FIELDS and isinstance(value, str) and value not in ids:
        ids[value] = f"<id:{len(ids) + 1}>"


def _artifact_key(url: str) -> str | None:
    parts = urlsplit(url)
    if not parts.netloc:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _rewrite(value: Any, key: Any, ids: dict[str, str], urls: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {child_key: _rewrite(child, child_key, ids, urls) for child_key, child in _sorted_items(value)}
    if isinstance(value, (list, tuple)):
        return [_rewrite(child, key, ids, urls) for child in value]
    if value is None:
        return None
    if key in DATE_FIELDS or isinstance(value, datetime):
        return TS_MARKER
    if isinstance(value, str):
        if key == URL_FIELD:
            artifact = _artifact_key(value)
            if artifact is not None:
                if artifact not in urls:
                    urls[artifact] = f"<url:{len(urls) + 1}>"
                return urls[artifact]
        if value in ids:
            return ids[value]
    return value


def normalize(result: ParityResult) -> ParityResult:
    tree = {
        "events": result.events,
        "incoming": result.incoming,
        "histories": result.histories,
        "outputs": result.outputs,
    }
    ids: dict[str, str] = {}
    _collect_ids(tree, None, ids)
    out = _rewrite(tree, None, ids, {})
    return ParityResult(
        events=out["events"],
        incoming=out["incoming"],
        histories=out["histories"],
        outputs=out["outputs"],
    )
