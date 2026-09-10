"""Kind and sentiment for comments, from either platform.

Every comment gets two labels: what it is (a question, a compliment, a
complaint, a suggestion, or none of those) and how it reads (positive, neutral
or negative). Both come back from one Claude call per batch, because the two
judgements are made from the same reading of the same text.

Three properties matter more than speed:

- **Never twice.** A comment carrying a kind is finished. Re-running costs
  nothing and changes nothing.
- **Resumable.** Batches run concurrently and are applied as they land, so an
  interrupted run keeps whatever finished and the next one picks up only what
  is still missing.
- **Platform-blind.** It reads comment_text, which every row has whatever it
  was collected from, so a Reddit comment and a YouTube one are the same job.

Nothing here touches Streamlit's UI, only st.secrets by way of relevance.py.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Sequence

import pandas as pd

import relevance

_LOG = logging.getLogger(__name__)

KIND_COLUMN = "kind"
SENTIMENT_COLUMN = "sentiment"

# Written back onto the row, and so onto the Sheet.
CLASSIFY_COLUMNS: list[str] = [KIND_COLUMN, SENTIMENT_COLUMN]

KINDS: list[str] = ["question", "compliment", "complaint", "suggestion", "other"]
SENTIMENTS: list[str] = ["positive", "neutral", "negative"]

# Comments per call. The prompt overhead is paid once per batch, and a batch
# that fails is small enough to lose cheaply.
BATCH_SIZE = 25

# Batches in flight at once. The work is all waiting on the API, so a handful
# of threads turns a long queue into a short one.
MAX_WORKERS = 4

# Per-comment character cap. What a comment is saying is clear from its
# opening; the rest is paid-for tokens.
_MAX_COMMENT_CHARS = 1000


# Progress callback: (batches_done, batches_total, comments_done) -> None
ProgressCallback = Callable[[int, int, int], None]


class ClassifyError(Exception):
    """Raised when the whole run fails. A partial run reports instead."""


_SYSTEM = f"""You label YouTube and Reddit comments for a food company's social
listening. The company sells Indian snacks and sweets.

For every comment give two labels.

kind, one of:
- question: the writer is asking something, about the product, where to buy it,
  how it is made, anything.
- compliment: praise for the product, the brand, the video or the post.
- complaint: something is wrong. Taste, price, packaging, delivery, service,
  the content itself.
- suggestion: an idea or request. A flavour they want, a change they would
  make, something they wish existed.
- other: none of those. Chatter, tags, emoji, spam, off topic remarks.

sentiment, one of:
- positive: warm, pleased, enthusiastic.
- neutral: factual, asking, indifferent.
- negative: unhappy, annoyed, disappointed.

The two are independent: a complaint is usually negative but a politely worded
one can read neutral, and a question can be enthusiastic.

Comments may be in English, Hindi, Tamil, Telugu, Kannada, Bengali, Marathi, or
those languages typed in Latin letters. Judge the meaning, not the script.
Answer for every comment you are given, using the index it was given. Use only
the listed values: kind must be one of {KINDS}, sentiment one of {SENTIMENTS}.
"""


_SCHEMA = {
    "type": "object",
    "properties": {
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "kind": {"type": "string", "enum": KINDS},
                    "sentiment": {"type": "string", "enum": SENTIMENTS},
                },
                "required": ["index", "kind", "sentiment"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["comments"],
    "additionalProperties": False,
}


def is_configured() -> bool:
    """True when Claude can be reached. Same key as the relevance filter."""
    return relevance.is_configured()


def ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    """A frame that definitely carries both label columns, as text."""
    out = df.copy()
    for column in CLASSIFY_COLUMNS:
        if column not in out.columns:
            out[column] = ""
        out[column] = out[column].fillna("").astype(str).str.strip()
    return out


def pending_mask(df: pd.DataFrame) -> pd.Series:
    """Rows with no kind yet.

    Keyed on kind alone: it is always filled when a comment has been read, so
    it is the marker for "done", exactly as comment_language is for
    translation.
    """
    if df.empty:
        return pd.Series(dtype=bool)
    labelled = ensure_columns(df)
    return labelled[KIND_COLUMN] == ""


def pending_ids(df: pd.DataFrame, limit: int | None = None) -> list[str]:
    """comment_ids not yet classified, in the order they appear."""
    if df.empty or "comment_id" not in df.columns:
        return []
    labelled = ensure_columns(df)
    ids = labelled.loc[pending_mask(labelled), "comment_id"].astype(str).tolist()
    return ids[:limit] if limit else ids


def _clip(text: object) -> str:
    body = " ".join(str(text or "").split())
    return body[:_MAX_COMMENT_CHARS]


def classify_batch(client, comments: Sequence[str]) -> list[dict]:
    """Label one batch. One dict per input, in the order given."""
    if not comments:
        return []

    listing = "\n\n".join(
        f"<comment index=\"{i}\">\n{_clip(text)}\n</comment>"
        for i, text in enumerate(comments)
    )
    prompt = (
        f"Label each of these {len(comments)} comments with a kind and a "
        "sentiment.\n\n" + listing
    )
    payload = relevance.ask_json(client, _SYSTEM, prompt, _SCHEMA,
                                 action="classification")

    by_index: dict[int, dict] = {}
    for entry in payload.get("comments", []):
        try:
            position = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= position < len(comments):
            continue
        kind = str(entry.get("kind", "") or "").strip().lower()
        sentiment = str(entry.get("sentiment", "") or "").strip().lower()
        by_index[position] = {
            KIND_COLUMN: kind if kind in KINDS else "other",
            SENTIMENT_COLUMN: sentiment if sentiment in SENTIMENTS else "neutral",
        }

    # A comment the model skipped is labelled rather than left pending, so one
    # odd batch cannot keep coming back forever.
    return [
        by_index.get(i, {KIND_COLUMN: "other", SENTIMENT_COLUMN: "neutral"})
        for i in range(len(comments))
    ]


def classify_rows(
    df: pd.DataFrame,
    ids: Sequence[str],
    progress_cb: ProgressCallback | None = None,
    workers: int = MAX_WORKERS,
    batch_size: int = BATCH_SIZE,
    client=None,
) -> tuple[pd.DataFrame, int, list[str]]:
    """Label exactly these comment_ids. Returns (frame, labelled, problems).

    Batches go out concurrently and are written onto the frame as they come
    back, on this thread. A batch that fails leaves its rows unlabelled and
    the rest stand, so running again picks up only what is missing.
    """
    labelled = ensure_columns(df)
    if labelled.empty or not ids:
        return labelled, 0, []

    wanted = [str(i) for i in dict.fromkeys(ids)]
    positions = labelled.index[
        labelled["comment_id"].astype(str).isin(wanted)
        & (labelled[KIND_COLUMN] == "")
    ].tolist()
    if not positions:
        return labelled, 0, []

    batches = [
        positions[start : start + batch_size]
        for start in range(0, len(positions), batch_size)
    ]
    client = client or relevance._client()
    done = 0
    problems: list[str] = []

    if progress_cb is not None:
        progress_cb(0, len(batches), 0)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(batches)))) as pool:
        futures = {
            pool.submit(
                classify_batch,
                client,
                [labelled.at[index, "comment_text"] for index in batch],
            ): batch
            for batch in batches
        }
        for finished, future in enumerate(as_completed(futures), start=1):
            batch = futures[future]
            try:
                results = future.result()
            except Exception as exc:
                # This batch stays pending and will be picked up next time.
                _LOG.exception("Classification batch failed")
                problems.append(f"{type(exc).__name__}: {exc}")
                if progress_cb is not None:
                    progress_cb(finished, len(batches), done)
                continue

            for index, result in zip(batch, results):
                labelled.at[index, KIND_COLUMN] = result[KIND_COLUMN]
                labelled.at[index, SENTIMENT_COLUMN] = result[SENTIMENT_COLUMN]
            done += len(batch)
            if progress_cb is not None:
                progress_cb(finished, len(batches), done)

    if problems and done == 0:
        raise ClassifyError(problems[0])
    return labelled, done, problems
