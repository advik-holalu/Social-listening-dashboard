"""Translation for the Social Listening app, by Google Cloud Translation.

Non-English comments are translated on demand and the result is stored beside
the comment, so a comment is only ever translated once.

Authentication reuses the service account already configured for Sheets, so
there is no second key to manage. That account needs the Cloud Translation API
enabled on its project and the Cloud Translation API User role.

Nothing in here touches Streamlit's UI, only st.secrets for the credentials.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Callable, Sequence

import pandas as pd
import streamlit as st
from google.api_core.exceptions import GoogleAPIError
from google.cloud import translate_v2
from google.oauth2.service_account import Credentials

_LOG = logging.getLogger(__name__)

# The same block Sheets authenticates with.
SECRET_SERVICE_ACCOUNT = "gcp_service_account"

# Translation is its own product, with its own scope.
SCOPES = ["https://www.googleapis.com/auth/cloud-translation"]


# Translation columns. comment_language is what marks a comment as processed:
# an English comment gets language "en" and an empty translation, which is a
# real result, so a blank translation must never re-queue the comment.
TRANSLATION_COLUMN = "comment_translation"


TRANSLATION_COLUMNS: list[str] = ["comment_language", TRANSLATION_COLUMN]
# Everything the translator writes back onto a comment row.
TAG_COLUMNS: list[str] = list(TRANSLATION_COLUMNS)


# Comments per call. The API takes a list and bills by character, so the batch
# size costs nothing either way; it just keeps one failure cheap to retry.
BATCH_SIZE = 25


# Per-comment character cap. Billing is per character and YouTube allows very
# long comments, so a rambling one is trimmed rather than paid for in full.
_MAX_COMMENT_CHARS = 2000


# Progress callback: (batch_number, batch_total, tagged_so_far) -> None
ProgressCallback = Callable[[int, int, int], None]


class InsightsError(Exception):
    """Raised for configuration or API problems the user needs to fix."""


def is_configured() -> bool:
    """True when the service account block is present in secrets."""
    try:
        return SECRET_SERVICE_ACCOUNT in st.secrets
    except Exception:
        # st.secrets raises when there is no secrets.toml at all.
        return False


@st.cache_resource(show_spinner=False)
def _client() -> translate_v2.Client:
    """The Translation client, built once per session from the Sheets account.

    Cached like the Sheets client: building it parses a private key and opens
    a session, and neither needs doing per rerun.
    """
    try:
        info = dict(st.secrets[SECRET_SERVICE_ACCOUNT])
    except (KeyError, FileNotFoundError) as exc:
        raise InsightsError(
            f"Missing [{SECRET_SERVICE_ACCOUNT}] in .streamlit/secrets.toml, "
            "so comments cannot be translated."
        ) from exc

    # TOML escapes newlines in the private key as literal backslash-n.
    if "private_key" in info:
        info["private_key"] = str(info["private_key"]).replace("\\n", "\n")

    try:
        credentials = Credentials.from_service_account_info(info, scopes=SCOPES)
        return translate_v2.Client(credentials=credentials)
    except Exception as exc:
        raise InsightsError(
            f"Could not authenticate for translation: {exc}"
        ) from exc


def ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Return a frame that definitely has every translation column as text."""
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


def _has_non_latin(text: str) -> bool:
    """Devanagari, Tamil, Telugu, Kannada, Bengali and friends."""
    return any(char.isalpha() and ord(char) > 0x02AF for char in text)


def _is_hinglish(text: object) -> bool:
    """An Indian language written in the Latin alphabet.

    Worth singling out because language detection reads these as English and
    returns them untranslated, so they are sent with the source declared.
    """
    body = str(text or "").strip()
    if not body or _has_non_latin(body):
        return False

    words = set(re.findall(r"[a-z]+", body.lower()))
    if words & _STRONG_MARKERS:
        return True
    return len(words & _WEAK_MARKERS) >= 2


def looks_non_english(text: object) -> bool:
    """Best-effort guess at whether a comment needs translating.

    Deliberately approximate: it decides whether to offer a Translate link,
    and the translator settles it once the link is clicked.
    """
    body = str(text or "").strip()
    if not body:
        return False
    return _has_non_latin(body) or _is_hinglish(body)


def needs_translation(record) -> bool:
    """True when this comment should offer a Translate link.

    A comment the translator has already seen carries a language, so it never
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


def _call(client, values: Sequence[str], source: str | None) -> list[dict]:
    """One Translation API call, with the errors turned into our own."""
    try:
        result = client.translate(
            list(values),
            target_language="en",
            format_="text",
            source_language=source,
        )
    except GoogleAPIError as exc:
        raise InsightsError(
            f"Google Translation refused the request: {exc}"
        ) from exc
    except Exception as exc:
        raise InsightsError(f"Could not reach Google Translation: {exc}") from exc

    # A single string comes back as a dict rather than a list of one.
    return [result] if isinstance(result, dict) else list(result)


def translate_batch(client, comments: Sequence[str]) -> list[dict]:
    """Detect language and translate one batch. One dict per input, in order.

    Comments go out with the language auto-detected, except the ones our own
    heuristic reads as an Indian language typed in Latin letters. Detection
    calls those English and hands them straight back, so they are sent as
    Hindi instead and actually get translated.
    """
    if not comments:
        return []

    texts = [_clip(text) for text in comments]
    results: list[dict] = [{"language": "en", "translation": ""} for _ in texts]

    detect_at = [i for i, text in enumerate(texts) if text and not _is_hinglish(text)]
    hinglish_at = [i for i, text in enumerate(texts) if text and _is_hinglish(text)]

    for positions, source in ((detect_at, None), (hinglish_at, "hi")):
        if not positions:
            continue
        for position, payload in zip(
            positions, _call(client, [texts[i] for i in positions], source)
        ):
            results[position] = _as_result(texts[position], payload, source)

    return results


def _as_result(original: str, payload: dict, source: str | None) -> dict:
    """One API response turned into the pair stored on the row."""
    language = str(
        payload.get("detectedSourceLanguage") or source or "en"
    ).strip().lower()
    # The API returns HTML entities even asking for plain text.
    translation = html.unescape(str(payload.get("translatedText", "") or "")).strip()

    # English in, nothing to store. Same when the translation only echoes the
    # original, which is what comes back for a comment that was already English
    # or was only emoji.
    if language.startswith("en") or translation.lower() == original.strip().lower():
        return {"language": "en" if language.startswith("en") else language,
                "translation": ""}
    return {"language": language, "translation": translation}


def translate_rows(df: pd.DataFrame, ids: Sequence[str]) -> tuple[pd.DataFrame, int]:
    """Translate exactly these comment_ids in a single call.

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

