"""The written summary behind the Analysis view.

One Claude call reads the labelled comments already in view and says what they
add up to. It is keyed by a fingerprint of that exact set, so an unchanged set
is never sent twice, and any change to which comments are in view or to their
labels produces a new fingerprint and a new summary.

The counts it is given come from the classification pass, not from Claude
counting: the model writes prose about arithmetic that has already been done.
"""

from __future__ import annotations

import hashlib
import logging

import pandas as pd

import classify
import relevance

_LOG = logging.getLogger(__name__)

# Comment quotes handed to the model, longest-engagement first. Enough to write
# from without paying for the whole set.
QUOTES_PER_BUCKET = 12

_MAX_QUOTE_CHARS = 240


class AnalysisError(Exception):
    """Raised when the summary cannot be written."""


_SYSTEM = """You write a short brief for a food company's brand and product
teams, from comments already collected and labelled.

You are given counts, not opinions: how many comments are positive, neutral or
negative, how many are questions, compliments, complaints or suggestions, and
the same split per platform when there is more than one. You are also given a
few of the most visible comments in each bucket.

Write four or five sentences of plain English covering:

- which way sentiment leans overall, using the counts given
- what kind of comment dominates, and what that says about the audience
- when two platforms are present, how they differ from each other, naming them
- the one thing worth acting on, drawn from the complaints or suggestions

Write for someone deciding what to do next week. No headings, no bullet
points, no restating the numbers back as a list, no marketing language. If the
comments are thin or say little, say that plainly instead of inventing a
finding."""


_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


def is_configured() -> bool:
    """True when Claude can be reached. Same key as everything else."""
    return relevance.is_configured()


def fingerprint(df: pd.DataFrame) -> str:
    """A stable id for exactly this set of comments and their labels.

    Built from the comment ids and the two labels, so re-classifying or
    filtering changes it and re-reading the same rows does not.
    """
    if df.empty:
        return ""
    labelled = classify.ensure_columns(df)
    parts = (
        labelled["comment_id"].astype(str)
        + "|" + labelled[classify.KIND_COLUMN]
        + "|" + labelled[classify.SENTIMENT_COLUMN]
    )
    joined = "\n".join(sorted(parts))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def counts(df: pd.DataFrame) -> dict:
    """The arithmetic the summary is written from, and the charts drawn from."""
    labelled = classify.ensure_columns(df)
    known = labelled[labelled[classify.KIND_COLUMN] != ""]
    if known.empty:
        return {"total": 0, "sentiment": {}, "kind": {}, "platforms": {}}

    def tally(frame: pd.DataFrame) -> dict:
        return {
            "total": len(frame),
            "sentiment": frame[classify.SENTIMENT_COLUMN].value_counts().to_dict(),
            "kind": frame[classify.KIND_COLUMN].value_counts().to_dict(),
        }

    out = tally(known)
    out["platforms"] = {
        str(name): tally(rows)
        for name, rows in known.groupby("platform")
        if str(name).strip()
    }
    return out


def top_quotes(df: pd.DataFrame, kind: str, limit: int = QUOTES_PER_BUCKET):
    """The most visible comments of one kind, most engaged source first."""
    labelled = classify.ensure_columns(df)
    rows = labelled[labelled[classify.KIND_COLUMN] == kind]
    if rows.empty:
        return rows
    ranked = rows.copy()
    ranked["_reach"] = pd.to_numeric(
        ranked.get("engagement_score", 0), errors="coerce"
    ).fillna(0)
    ranked["_likes"] = pd.to_numeric(
        ranked.get("comment_likes", 0), errors="coerce"
    ).fillna(0)
    ranked = ranked.sort_values(["_reach", "_likes"], ascending=False)
    return ranked.head(limit).drop(columns=["_reach", "_likes"])


def _quote_lines(df: pd.DataFrame, kind: str) -> str:
    rows = top_quotes(df, kind, limit=6)
    if rows.empty:
        return f"  (no {kind}s)"
    out = []
    for _, row in rows.iterrows():
        text = " ".join(str(row.get("comment_text", "")).split())[:_MAX_QUOTE_CHARS]
        out.append(
            f"  - [{row.get('platform', '?')}] {text}"
        )
    return "\n".join(out)


def write_summary(df: pd.DataFrame, client=None) -> str:
    """One paragraph about this set of comments. Raises on failure."""
    tally = counts(df)
    if not tally["total"]:
        raise AnalysisError("None of these comments have been classified yet.")

    lines = [
        f"Comments classified: {tally['total']}",
        f"Sentiment: {tally['sentiment']}",
        f"Kinds: {tally['kind']}",
    ]
    if len(tally["platforms"]) > 1:
        lines.append("Per platform:")
        for name, split in tally["platforms"].items():
            lines.append(
                f"  {name}: {split['total']} comment(s), "
                f"sentiment {split['sentiment']}, kinds {split['kind']}"
            )
    lines.append("\nMost visible complaints:")
    lines.append(_quote_lines(df, "complaint"))
    lines.append("Most visible compliments:")
    lines.append(_quote_lines(df, "compliment"))
    lines.append("Most visible suggestions:")
    lines.append(_quote_lines(df, "suggestion"))

    try:
        payload = relevance.ask_json(
            client or relevance._client(), _SYSTEM, "\n".join(lines), _SCHEMA,
            action="summary digest",
        )
    except Exception as exc:
        raise AnalysisError(f"The summary could not be written: {exc}") from exc

    summary = " ".join(str(payload.get("summary", "") or "").split())
    if not summary:
        raise AnalysisError("Claude returned an empty summary.")
    return summary
