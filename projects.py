"""Saved searches, kept as references rather than copies.

A project is a name attached to a search: its keywords, its platforms, and the
settings it ran under. It owns no comments. The rows that belong to it are
whatever the comments table currently holds for those keywords on those
platforms, which is what makes it a reference and keeps one source of truth.

That is why there is no project_id column on the comments and no mapping tab.
Both would restate a relationship the rows already carry in `keyword` and
`platform`, and both would go stale the moment a refresh adds rows: a mapping
would have to be rewritten, while a definition does not. It also means two
projects may legitimately share a keyword and both see it.

The one thing a project does store is a snapshot: the counts as they stood
when it was last opened or refreshed, so "what changed" has something to
compare against.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging

import pandas as pd

import classify

_LOG = logging.getLogger(__name__)

# What a project row holds. keywords and platforms are comma separated;
# snapshot is the JSON counts from the last time it was looked at.
PROJECT_COLUMNS: list[str] = [
    "project_id",
    "name",
    "keywords",
    "platforms",
    "exclude",
    "match",
    "created_at",
    "updated_at",
    "snapshot",
]


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def new_id(name: str) -> str:
    """A stable id from the name and the moment, readable in the Sheet."""
    slug = "-".join(
        part for part in str(name or "").lower().split() if part
    )[:32] or "project"
    stamp = hashlib.sha256(f"{name}{_now()}".encode("utf-8")).hexdigest()[:6]
    return f"{slug}-{stamp}"


def _split(text: object) -> list[str]:
    return [part.strip() for part in str(text or "").split(",") if part.strip()]


def snapshot(df: pd.DataFrame) -> dict:
    """The counts a later refresh compares itself against."""
    if df is None or df.empty:
        return {"comments": 0, "classified": 0, "sentiment": {}, "kind": {}}
    labelled = classify.ensure_columns(df)
    known = labelled[labelled[classify.KIND_COLUMN] != ""]
    return {
        "comments": int(len(labelled)),
        "classified": int(len(known)),
        "sentiment": {
            str(k): int(v)
            for k, v in known[classify.SENTIMENT_COLUMN].value_counts().items()
        },
        "kind": {
            str(k): int(v)
            for k, v in known[classify.KIND_COLUMN].value_counts().items()
        },
        "checked_at": _now(),
    }


def to_row(project: dict) -> list:
    """One project as the cells of its row."""
    return [
        project.get("project_id", ""),
        project.get("name", ""),
        ", ".join(project.get("keywords", [])),
        ", ".join(project.get("platforms", [])),
        ", ".join(project.get("exclude", [])),
        project.get("match", ""),
        project.get("created_at", "") or _now(),
        _now(),
        json.dumps(project.get("snapshot", {}), separators=(",", ":")),
    ]


def from_row(header: list[str], row: list) -> dict | None:
    """One row back as a project, or None when it is not one."""
    at = {name: index for index, name in enumerate(header)}

    def cell(name: str) -> str:
        index = at.get(name)
        if index is None or index >= len(row):
            return ""
        return str(row[index]).strip()

    if not cell("project_id") or not cell("name"):
        return None
    try:
        saved = json.loads(cell("snapshot") or "{}")
    except (ValueError, TypeError):
        saved = {}
    return {
        "project_id": cell("project_id"),
        "name": cell("name"),
        "keywords": _split(cell("keywords")),
        "platforms": _split(cell("platforms")),
        "exclude": _split(cell("exclude")),
        "match": cell("match"),
        "created_at": cell("created_at"),
        "updated_at": cell("updated_at"),
        "snapshot": saved if isinstance(saved, dict) else {},
    }


def rows_for(df: pd.DataFrame, project: dict) -> pd.DataFrame:
    """The comments that belong to a project, right now.

    Membership is a query, not a stored list: whatever the table holds for
    these keywords on these platforms is the project.
    """
    if df is None or df.empty:
        return df

    keywords = {k.lower() for k in project.get("keywords", [])}
    platforms = {p.lower() for p in project.get("platforms", [])}
    out = df
    if keywords and "keyword" in out.columns:
        out = out[out["keyword"].astype(str).str.lower().isin(keywords)]
    if platforms and "platform" in out.columns:
        known = out["platform"].astype(str).str.strip().str.lower()
        out = out[known.isin(platforms)]
    return out


def _share(counts: dict, name: str) -> float:
    total = sum(int(v) for v in counts.values()) or 0
    return (int(counts.get(name, 0)) / total) if total else 0.0


def describe_change(before: dict, after: dict) -> list[str]:
    """Plain sentences about what moved between two snapshots."""
    before = before or {}
    after = after or {}
    lines: list[str] = []

    added = int(after.get("comments", 0)) - int(before.get("comments", 0))
    since = str(before.get("checked_at", "") or "")[:10]
    if added > 0:
        lines.append(
            f"{added:,} new comment(s)"
            + (f" since {since}." if since else ".")
        )
    elif added < 0:
        lines.append(
            f"{abs(added):,} comment(s) fewer than last time, which happens "
            "when older keywords are cleared out."
        )
    else:
        lines.append(
            "No new comments" + (f" since {since}." if since else ".")
        )

    for name in ("positive", "negative"):
        old = _share(before.get("sentiment", {}), name)
        new = _share(after.get("sentiment", {}), name)
        if not (before.get("sentiment") and after.get("sentiment")):
            continue
        if abs(new - old) >= 0.01:
            lines.append(
                f"Sentiment shifted from {old:.0%} {name} to {new:.0%} {name}."
            )

    newly = int(after.get("classified", 0)) - int(before.get("classified", 0))
    if newly > 0:
        lines.append(f"{newly:,} more comment(s) have been classified.")
    return lines
