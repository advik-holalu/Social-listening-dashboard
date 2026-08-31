"""Claude-powered translation for the Social Listening app.

Non-English comments are translated on demand and the result is stored beside
the comment, so a comment is only ever translated once.

Nothing in here touches Streamlit's UI, only st.secrets for the API key.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Sequence

import anthropic
import pandas as pd
import streamlit as st

MODEL = "claude-sonnet-4-6"


SECRET_API_KEY = "ANTHROPIC_API_KEY"


# Translation columns. comment_language is what marks a comment as processed:
# an English comment gets language "en" and an empty translation, which is a
# real result, so a blank translation must never re-queue the comment.
TRANSLATION_COLUMN = "comment_translation"


TRANSLATION_COLUMNS: list[str] = ["comment_language", TRANSLATION_COLUMN]
# Everything Claude writes back onto a comment row.
TAG_COLUMNS: list[str] = list(TRANSLATION_COLUMNS)


# Comments per Claude call. Big enough that the prompt overhead is amortised,
# small enough that one bad batch costs little to retry.
BATCH_SIZE = 25


# Per-comment character cap. YouTube allows very long comments; sentiment and
# intent live in the opening lines, and this keeps a batch prompt bounded.
_MAX_COMMENT_CHARS = 2000


_TRANSLATE_SYSTEM = """You translate YouTube comments for a snack brand's social
listening dashboard. The brand is Indian, so expect English, Hindi, Tamil,
Telugu, Kannada, Malayalam, Bengali, Marathi -- and plenty of Hinglish, meaning
Indian languages written in the Latin alphabet.

For each comment return two fields:

- language: the BCP-47 code of the language the comment is actually written in,
  regardless of alphabet. Use "en" only for genuine English. A comment written
  in Latin letters but in Hindi words ("bahut accha hai", "kitne ka hai") is
  "hi", not "en". Mixed comments take the language of the majority of the words.
- translation: a natural English translation. Return an EMPTY STRING when
  language is "en" -- there is nothing to translate.

Translate meaning, not word by word, and keep it about as long as the original.
Leave product names, brand names, and @handles as they are. If a comment is only
emoji, punctuation, or is too garbled to read, return language "en" and an empty
translation rather than guessing."""


_TRANSLATE_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "language": {"type": "string"},
                    "translation": {"type": "string"},
                },
                "required": ["index", "language", "translation"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}


# Progress callback: (batch_number, batch_total, tagged_so_far) -> None
ProgressCallback = Callable[[int, int, int], None]


class InsightsError(Exception):
    """Raised for configuration or API problems the user needs to fix."""


def is_configured() -> bool:
    """True when an Anthropic API key is present in secrets."""
    try:
        return bool(str(st.secrets.get(SECRET_API_KEY, "")).strip())
    except Exception:
        # st.secrets raises when there is no secrets.toml at all.
        return False


def _client() -> anthropic.Anthropic:
    try:
        api_key = str(st.secrets.get(SECRET_API_KEY, "")).strip()
    except Exception:
        api_key = ""
    if not api_key:
        raise InsightsError(
            f"No {SECRET_API_KEY} found in secrets. Add it to "
            ".streamlit/secrets.toml to use the Insights tab."
        )
    return anthropic.Anthropic(api_key=api_key)


def ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Return a frame that definitely has every Claude-written column as text."""
    out = df.copy()
    for column in TAG_COLUMNS:
        if column not in out.columns:
            out[column] = ""
        out[column] = out[column].fillna("").astype(str).str.strip()
    return out


def _clip(text: object) -> str:
    body = str(text or "").strip()
    if len(body) > _MAX_COMMENT_CHARS:
        return body[:_MAX_COMMENT_CHARS] + " ...[truncated]"
    return body


# --------------------------------------------------------------------------
# Translation
# --------------------------------------------------------------------------
# Words that place a Latin-script comment in an Indian language. Detection has
# to happen without an API call now -- the Translate link only appears on
# comments worth translating -- and a character-set test alone would wave
# Hinglish through as English. "Strong" markers are unambiguous on their own;
# the rest have to turn up in pairs, so an English comment does not sprout a
# Translate link over one coincidence.
_STRONG_MARKERS = frozenset("""
nahi nahin bahut bohot accha acha achha kitna kitne kitni dhanyavad dhanyawad
shukriya swadisht swad mujhe chahiye kyun kyu banao banaya khaya khana paisa
bagundi bagunnadi chennagide chala romba nalla vanakkam namaskar
""".split())


_WEAK_MARKERS = frozenset("""
hai hain kya mera meri mere aap aapka bhai yaar mast bilkul zyada thoda karo
kaise kahan hum tum ye yeh woh wo bhi nahi to se ka ki ke me main
""".split())


_NON_LATIN_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def looks_non_english(text: object) -> bool:
    """Best-effort guess at whether a comment needs translating.

    Deliberately approximate: it decides whether to offer a Translate link, and
    Claude makes the real call once the link is clicked. Anything it misses is
    still caught by the bulk pass in the sidebar.
    """
    body = str(text or "").strip()
    if not body:
        return False

    # Devanagari, Tamil, Telugu, Kannada, Bengali and friends settle it outright.
    for char in body:
        if char.isalpha() and ord(char) > 0x02AF:
            return True

    words = set(re.findall(r"[a-z]+", body.lower()))
    if words & _STRONG_MARKERS:
        return True
    return len(words & _WEAK_MARKERS) >= 2


def needs_translation(record) -> bool:
    """True when this comment should offer a Translate link.

    A comment Claude has already looked at carries a language, so it never
    offers the link again -- it either has a translation to show or is English.
    """
    if str(record.get("comment_language", "") or "").strip():
        return False
    return looks_non_english(record.get("comment_text"))


def translation_pending_mask(df: pd.DataFrame) -> pd.Series:
    """Rows never sent to the translator.

    Keyed on comment_language, not comment_translation: an English comment
    comes back with a language and no translation, and that is a finished
    result, not a gap to retry.
    """
    if df.empty:
        return pd.Series(dtype=bool)
    tagged = ensure_columns(df)
    return tagged["comment_language"] == ""


def pending_translation_ids(df: pd.DataFrame, limit: int | None = None) -> list[str]:
    """comment_ids never sent to the translator, oldest position first."""
    if df.empty:
        return []
    tagged = ensure_columns(df)
    ids = tagged.loc[translation_pending_mask(tagged), "comment_id"].astype(str).tolist()
    return ids[:limit] if limit else ids


def translate_batch(
    client: anthropic.Anthropic, comments: Sequence[str]
) -> list[dict]:
    """Detect language and translate one batch. One dict per input, in order."""
    if not comments:
        return []

    numbered = "\n\n".join(
        f"<comment index=\"{i}\">\n{_clip(text)}\n</comment>"
        for i, text in enumerate(comments)
    )
    prompt = (
        f"Identify the language of each of the {len(comments)} comments below "
        "and translate the ones that are not English. Return one entry per "
        "comment, using the same index it was given.\n\n" + numbered
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=_TRANSLATE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "format": {"type": "json_schema", "schema": _TRANSLATE_SCHEMA}
            },
        )
    except anthropic.AuthenticationError as exc:
        raise InsightsError(
            f"Anthropic rejected the API key. Check {SECRET_API_KEY} in secrets."
        ) from exc
    except anthropic.RateLimitError as exc:
        raise InsightsError(
            "Anthropic rate limit hit. Wait a moment and translate the rest."
        ) from exc
    except anthropic.APIStatusError as exc:
        raise InsightsError(f"Anthropic API error ({exc.status_code}): {exc}") from exc
    except anthropic.APIConnectionError as exc:
        raise InsightsError(f"Could not reach the Anthropic API: {exc}") from exc

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InsightsError(f"Claude returned unreadable JSON: {exc}") from exc

    by_index: dict[int, dict] = {}
    for entry in payload.get("translations", []):
        try:
            position = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= position < len(comments):
            continue
        language = str(entry.get("language", "")).strip().lower() or "en"
        translation = str(entry.get("translation", "")).strip()
        # An English comment has nothing to translate; drop any echo of itself.
        if language.startswith("en"):
            translation = ""
        by_index[position] = {"language": language, "translation": translation}

    # A comment the model skipped is marked English rather than left pending,
    # so one odd batch cannot loop forever.
    return [
        by_index.get(i, {"language": "en", "translation": ""})
        for i in range(len(comments))
    ]


def translate_rows(df: pd.DataFrame, ids: Sequence[str]) -> tuple[pd.DataFrame, int]:
    """Translate exactly these comment_ids in a single Claude call.

    Returns (updated_frame, rows_touched). Ids already carrying a language are
    dropped before the call, so a double click costs nothing.
    """
    tagged = ensure_columns(df)
    if tagged.empty or not ids:
        return tagged, 0

    wanted = [str(i) for i in dict.fromkeys(ids)]
    positions = tagged.index[
        tagged["comment_id"].astype(str).isin(wanted)
        & (tagged["comment_language"] == "")
    ].tolist()
    if not positions:
        return tagged, 0

    results = translate_batch(
        _client(), [tagged.at[index, "comment_text"] for index in positions]
    )
    for index, result in zip(positions, results):
        tagged.at[index, "comment_language"] = result["language"]
        tagged.at[index, "comment_translation"] = result["translation"]

    return tagged, len(positions)


def translate_dataframe(
    df: pd.DataFrame,
    progress_cb: ProgressCallback | None = None,
    batch_size: int = BATCH_SIZE,
) -> tuple[pd.DataFrame, int]:
    """Translate every comment not yet seen by the translator.

    Returns (updated_frame, rows_translated). Rows that already carry a
    comment_language are skipped, so repeat runs cost nothing -- the same
    contract analyze_dataframe() follows for the sentiment tags.
    """
    tagged = ensure_columns(df)
    if tagged.empty:
        return tagged, 0

    pending = tagged.index[translation_pending_mask(tagged)].tolist()
    if not pending:
        return tagged, 0

    client = _client()
    batches = [pending[i : i + batch_size] for i in range(0, len(pending), batch_size)]
    done = 0

    for number, batch in enumerate(batches, start=1):
        if progress_cb is not None:
            progress_cb(number, len(batches), done)

        results = translate_batch(
            client, [tagged.at[index, "comment_text"] for index in batch]
        )
        for index, result in zip(batch, results):
            tagged.at[index, "comment_language"] = result["language"]
            tagged.at[index, "comment_translation"] = result["translation"]
        done += len(batch)

    if progress_cb is not None:
        progress_cb(len(batches), len(batches), done)

    return tagged, done


def translation_of(record) -> str:
    """The stored English translation for one row, empty when it needs none."""
    return str(record.get("comment_translation", "") or "").strip()

