"""Claude-powered tagging for the Social Listening app.

Every comment gets three tags -- sentiment, category, and flagged_ask -- from a
single Claude call that classifies a whole batch at once. The tags are stored
alongside the comment in the Sheet, so a comment is only ever analysed once.

Nothing in here touches Streamlit's UI, only st.secrets for the API key.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Sequence

import anthropic
import pandas as pd
import streamlit as st

MODEL = "claude-sonnet-4-6"

SECRET_API_KEY = "ANTHROPIC_API_KEY"

# The three columns this module owns. They are appended to the sheet schema
# after the fetch columns, and a row counts as "not yet analysed" while any of
# the first two is blank.
ANALYSIS_COLUMNS: list[str] = ["sentiment", "category", "flagged_ask"]

# Translation columns. comment_language is what marks a comment as processed:
# an English comment gets language "en" and an empty translation, which is a
# real result, so a blank translation must never re-queue the comment.
TRANSLATION_COLUMN = "comment_translation"
TRANSLATION_COLUMNS: list[str] = ["comment_language", TRANSLATION_COLUMN]

# The By Kind view's bucket, written by classify_rows().
KIND_COLUMN = "kind"
SENTIMENT_COLUMN = "sentiment"

# One Claude call fills both, so By Kind and Analysis never pay twice.
CLASSIFY_COLUMNS: list[str] = [KIND_COLUMN, SENTIMENT_COLUMN]
KIND_COLUMNS: list[str] = [KIND_COLUMN]

KINDS = ["question", "compliment", "complaint", "suggestion", "other"]

# Bucket -> the heading it appears under.
KIND_LABELS = {
    "question": "Questions",
    "compliment": "Compliments",
    "complaint": "Complaints",
    "suggestion": "Suggestions",
    "other": "Other",
}

# Rough Claude cost per comment classified, used only for the warning shown
# before anyone spends anything. Sonnet 4.6 at $3/$15 per MTok, with a batch of
# 25 sharing one system prompt, lands near this.
COST_PER_COMMENT = 0.0005

# Everything Claude writes back onto a comment row.
TAG_COLUMNS: list[str] = ANALYSIS_COLUMNS + TRANSLATION_COLUMNS + KIND_COLUMNS

# Where a video's transcript summary lives, both in the videos worksheet and on
# a comment frame once the summaries have been joined on for display.
VIDEO_SUMMARY_COLUMN = "video_summary"

SENTIMENTS = ["positive", "neutral", "negative"]

# Sentiment is a diverging scale: two poles with a neutral gray midpoint. Blue
# and orange rather than green and red because green/red is the pair colour
# blindness hits hardest; these were validated for CVD separation and contrast
# against both a light and a dark chart surface. The labels always travel with
# the colour, so identity never rests on hue alone.
SENTIMENT_COLORS = {
    "positive": "#2F6FD0",
    "neutral": "#8E949B",
    "negative": "#D9622B",
}

CATEGORIES = [
    "praise",
    "complaint",
    "recipe_request",
    "price_mention",
    "availability_question",
    "health_question",
    "nostalgia",
    "other",
]

# Comments per Claude call. Big enough that the prompt overhead is amortised,
# small enough that one bad batch costs little to retry.
BATCH_SIZE = 25

# Classification sends more per call than translation: the answer per comment
# is two short words, so the round trip dominates and fewer, fatter calls win.
CLASSIFY_BATCH_SIZE = 50

# Batches in flight at once. The work is entirely network wait, so threads are
# the right tool; the ceiling keeps well inside Anthropic's rate limits.
CLASSIFY_WORKERS = 6

# Per-comment character cap. YouTube allows very long comments; sentiment and
# intent live in the opening lines, and this keeps a batch prompt bounded.
_MAX_COMMENT_CHARS = 2000

# Video summaries are 2-3 sentences by construction, but a batch repeats one per
# comment, so a stray long summary is clipped rather than paid for 25 times.
_MAX_CONTEXT_CHARS = 700

_SYSTEM = """You tag YouTube comments for a snack brand's social listening dashboard.

For each comment return exactly three fields:

- sentiment: "positive", "neutral", or "negative" -- how the commenter feels
  about the product or brand being discussed. A comment that is merely factual
  or off-topic is "neutral".
- category: the single best fit from the allowed list. Use "other" when nothing
  else fits rather than forcing a match.
- flagged_ask: a short phrase (under 10 words) naming a specific request,
  complaint, or suggestion the comment makes, written so a brand manager can act
  on it -- for example "wants sugar-free version" or "packaging arrived damaged".
  Return an empty string when the comment carries no specific ask. Generic
  praise or generic criticism is not an ask.

Tag what the comment actually says. Do not infer asks that are not there.

Some comments carry a <video_context> block summarising the video they were left
on. Use it to read the comment correctly -- "not sweet enough" is a complaint on
a regular sweet, but may be neutral or even praise on a video about a low-sugar
product. Tag the comment, never the video: the context explains what the
commenter is reacting to, it is not itself something to classify."""

_SUMMARY_SYSTEM = """You summarise YouTube video transcripts for a snack brand's
social listening dashboard. The summary gives a brand manager the context they
need to read the video's comments correctly.

Write 2 to 3 sentences covering:
- what the video is about and what is shown or done in it
- any specific claims made about a product, quoted closely -- for example "no
  sugar added", "homemade recipe", "100% natural", a price, or a health claim

Write plainly, no preamble, no bullet points. If the transcript is in a language
other than English, summarise it in English. If the transcript is too fragmentary
to tell what the video is about, say so in one sentence instead of guessing."""

# Transcript summaries are short by construction; this is headroom, not a target.
_SUMMARY_MAX_TOKENS = 400

_SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "sentiment": {"type": "string", "enum": SENTIMENTS},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "flagged_ask": {"type": "string"},
                },
                "required": ["index", "sentiment", "category", "flagged_ask"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tags"],
    "additionalProperties": False,
}

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

_KIND_SYSTEM = """You sort YouTube comments for a snack brand's social listening
dashboard into one of five buckets.

- question: the commenter is asking something and expects an answer -- where to
  buy, what is in it, how to make it, is it available near them.
- compliment: praise for the product, the video, or the brand.
- complaint: something went wrong or the commenter is unhappy -- taste, price,
  packaging, delivery, service.
- suggestion: an idea for what the brand should do next, including requests for
  a new flavour, size, or version.
- other: anything else, including off-topic chatter, tagging a friend, emoji
  only, and comments about someone other than the brand.

Pick the single best fit. When a comment does two things, use the one the
commenter cares most about: "tastes great, please make it sugar free" is a
suggestion. Do not force a bucket -- "other" is a real answer.

Also give each comment a sentiment: how the commenter feels about the product
or brand.

- positive: they like it, are enjoying it, or are recommending it.
- negative: they are unhappy, disappointed, or warning others off.
- neutral: factual, asking without complaint, off-topic, or too short to read
  either way. A plain question is neutral unless it carries feeling.

Sentiment is about the commenter's feeling, not the bucket: a complaint is
usually negative, but a suggestion or question is often neutral."""

_KIND_SCHEMA = {
    "type": "object",
    "properties": {
        "kinds": {
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
    "required": ["kinds"],
    "additionalProperties": False,
}

_DIGEST_SYSTEM = """You brief a snack brand's marketing team on what their
YouTube audience is saying. You are given counts and a sample of real comments.

Write 2 to 3 sentences of plain English. Lead with the sentiment split and what
is driving it, then name the clearest complaint or request and roughly how often
it appears. Quote the audience's own words where a phrase is telling.

Be concrete and stick to the evidence: say "packaging arrived damaged, in 12
comments" rather than "there are some concerns". If the sample does not support
a theme, do not invent one -- say the comments are mixed or unremarkable. No
preamble, no bullet points, no advice."""

# The sample handed to the digest. Enough to spot a theme, small enough that
# the call stays one cheap request no matter how many comments are in view.
DIGEST_SAMPLE = 60

# Progress callback: (batch_number, batch_total, tagged_so_far) -> None
ProgressCallback = Callable[[int, int, int], None]

# Classification progress: (comments_done, comments_total) -> None
CountCallback = Callable[[int, int], None]

# Called on the main thread as each batch lands, with the frame so far and the
# ids it just filled, so the caller can persist partial work.
BatchCallback = Callable[[pd.DataFrame, list], None]

# Summariser: (video_title, transcript_text, language) -> summary
Summarizer = Callable[[str, str, str], str]


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


def pending_mask(df: pd.DataFrame) -> pd.Series:
    """Boolean mask of rows still needing analysis.

    A row is pending when sentiment or category is blank. flagged_ask is
    deliberately excluded -- an empty flagged_ask is a real result, not a gap.
    """
    if df.empty:
        return pd.Series(dtype=bool)
    tagged = ensure_columns(df)
    return (tagged["sentiment"] == "") | (tagged["category"] == "")


def _clip(text: object) -> str:
    body = str(text or "").strip()
    if len(body) > _MAX_COMMENT_CHARS:
        return body[:_MAX_COMMENT_CHARS] + " ...[truncated]"
    return body


def _clip_context(text: object) -> str:
    body = " ".join(str(text or "").split())
    if len(body) > _MAX_CONTEXT_CHARS:
        return body[:_MAX_CONTEXT_CHARS] + " ..."
    return body


def _blank_tag() -> dict:
    """Fallback tag for a comment the model did not return a row for."""
    return {"sentiment": "neutral", "category": "other", "flagged_ask": ""}


def _comment_block(index: int, text: object, context: str) -> str:
    """One comment, with the summary of the video it was left on when we have it."""
    body = f"<comment index=\"{index}\">"
    if context:
        body += f"\n<video_context>{_clip_context(context)}</video_context>"
    return body + f"\n{_clip(text)}\n</comment>"


def analyze_batch(
    client: anthropic.Anthropic,
    comments: Sequence[str],
    contexts: Sequence[str] | None = None,
) -> list[dict]:
    """Tag one batch of comments. Returns one dict per input, in input order.

    `contexts` is an optional parallel sequence of video summaries -- entry i
    describes the video comment i was left on. Missing or blank entries just
    mean that comment is tagged without context.
    """
    if not comments:
        return []

    if contexts is None:
        contexts = [""] * len(comments)

    numbered = "\n\n".join(
        _comment_block(i, text, str(contexts[i] if i < len(contexts) else "") or "")
        for i, text in enumerate(comments)
    )
    prompt = (
        f"Tag each of the {len(comments)} comments below. Return one entry per "
        "comment, using the same index it was given.\n\n" + numbered
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
    except anthropic.AuthenticationError as exc:
        raise InsightsError(
            f"Anthropic rejected the API key. Check {SECRET_API_KEY} in secrets."
        ) from exc
    except anthropic.RateLimitError as exc:
        raise InsightsError(
            "Anthropic rate limit hit. Wait a moment and analyse the rest."
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

    # Map back by the index the model echoed, so a missing or reordered entry
    # lands on the right comment instead of silently shifting every tag.
    by_index: dict[int, dict] = {}
    for entry in payload.get("tags", []):
        try:
            position = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if 0 <= position < len(comments):
            by_index[position] = {
                "sentiment": str(entry.get("sentiment", "")).strip() or "neutral",
                "category": str(entry.get("category", "")).strip() or "other",
                "flagged_ask": str(entry.get("flagged_ask", "")).strip(),
            }

    return [by_index.get(i, _blank_tag()) for i in range(len(comments))]


def analyze_dataframe(
    df: pd.DataFrame,
    progress_cb: ProgressCallback | None = None,
    batch_size: int = BATCH_SIZE,
    video_summaries: dict[str, str] | None = None,
) -> tuple[pd.DataFrame, int]:
    """Tag every un-analysed row of `df`.

    Returns (updated_frame, rows_tagged). The frame comes back with the three
    analysis columns filled in for the rows that were pending; rows already
    carrying tags are left untouched, so repeat runs cost nothing.

    `video_summaries` maps video_id to a transcript summary. When a comment's
    video is in there, the summary rides along with the comment so Claude can
    read it in context.

    A failure part-way through raises, but the rows tagged before it are lost --
    callers that care should analyse in smaller passes.
    """
    tagged = ensure_columns(df)
    if tagged.empty:
        return tagged, 0

    pending = tagged.index[pending_mask(tagged)].tolist()
    if not pending:
        return tagged, 0

    client = _client()
    batches = [pending[i : i + batch_size] for i in range(0, len(pending), batch_size)]
    done = 0

    for number, batch in enumerate(batches, start=1):
        if progress_cb is not None:
            progress_cb(number, len(batches), done)

        results = analyze_batch(
            client,
            [tagged.at[index, "comment_text"] for index in batch],
            [_context_for(tagged, index, video_summaries) for index in batch],
        )
        for index, result in zip(batch, results):
            tagged.at[index, "sentiment"] = result["sentiment"]
            tagged.at[index, "category"] = result["category"]
            tagged.at[index, "flagged_ask"] = result["flagged_ask"]
        done += len(batch)

    if progress_cb is not None:
        progress_cb(len(batches), len(batches), done)

    return tagged, done


def _context_for(
    df: pd.DataFrame, index: object, video_summaries: dict[str, str] | None
) -> str:
    """The video summary for one row, from the lookup or the frame's own column."""
    if video_summaries:
        video_id = str(df.at[index, "video_id"]) if "video_id" in df.columns else ""
        summary = video_summaries.get(video_id, "")
        if summary:
            return str(summary)
    if VIDEO_SUMMARY_COLUMN in df.columns:
        return str(df.at[index, VIDEO_SUMMARY_COLUMN] or "")
    return ""


# --------------------------------------------------------------------------
# Transcript summaries
# --------------------------------------------------------------------------
def summarize_transcript(
    client: anthropic.Anthropic,
    video_title: str,
    transcript: str,
    language: str = "",
) -> str:
    """Summarise one video transcript in 2-3 sentences.

    Returns an empty string rather than raising when the transcript is empty or
    the API call fails -- a missing summary degrades comment tagging slightly,
    but it must never take down the search run that triggered it.
    """
    body = str(transcript or "").strip()
    if not body:
        return ""

    header = f"Video title: {video_title}".strip()
    if language:
        header += f"\nTranscript language: {language}"

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=_SUMMARY_MAX_TOKENS,
            system=_SUMMARY_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"{header}\n\n<transcript>\n{body}\n</transcript>",
                }
            ],
        )
    except (anthropic.APIStatusError, anthropic.APIConnectionError):
        return ""

    text = next((b.text for b in response.content if b.type == "text"), "")
    return " ".join(text.split())


def make_summarizer() -> Summarizer | None:
    """Build the callable run_search uses to summarise each new video.

    Returns None when no API key is configured, which run_search reads as
    "collect transcripts but leave the summaries blank".
    """
    if not is_configured():
        return None
    client = _client()

    def summarize(video_title: str, transcript: str, language: str = "") -> str:
        return summarize_transcript(client, video_title, transcript, language)

    return summarize


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


def pending_translation_ids(df: pd.DataFrame, limit: int | None = None) -> list[str]:
    """comment_ids never sent to the translator, oldest position first."""
    if df.empty:
        return []
    tagged = ensure_columns(df)
    ids = tagged.loc[translation_pending_mask(tagged), "comment_id"].astype(str).tolist()
    return ids[:limit] if limit else ids


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


# --------------------------------------------------------------------------
# Kind classification
# --------------------------------------------------------------------------
def kind_pending_mask(df: pd.DataFrame) -> pd.Series:
    """Rows missing a bucket or a sentiment.

    One pass writes both, so a row is only sent when something is actually
    missing -- running By Kind leaves nothing for Analysis to pay for.
    """
    if df.empty:
        return pd.Series(dtype=bool)
    tagged = ensure_columns(df)
    return (tagged[KIND_COLUMN] == "") | (tagged[SENTIMENT_COLUMN] == "")


def classify_batch(
    client: anthropic.Anthropic, comments: Sequence[str]
) -> list[dict]:
    """Bucket and score one batch. One dict per input, in input order."""
    if not comments:
        return []

    numbered = "\n\n".join(
        f"<comment index=\"{i}\">\n{_clip(text)}\n</comment>"
        for i, text in enumerate(comments)
    )
    prompt = (
        f"Sort each of the {len(comments)} comments below into one bucket and "
        "give it a sentiment. Return one entry per comment, using the same "
        "index it was given.\n\n" + numbered
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=_KIND_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": _KIND_SCHEMA}},
        )
    except anthropic.AuthenticationError as exc:
        raise InsightsError(
            f"Anthropic rejected the API key. Check {SECRET_API_KEY} in secrets."
        ) from exc
    except anthropic.RateLimitError as exc:
        raise InsightsError(
            "Anthropic rate limit hit. Wait a moment and classify the rest."
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
    for entry in payload.get("kinds", []):
        try:
            position = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= position < len(comments):
            continue
        kind = str(entry.get("kind", "")).strip().lower()
        sentiment = str(entry.get("sentiment", "")).strip().lower()
        by_index[position] = {
            "kind": kind if kind in KINDS else "other",
            "sentiment": sentiment if sentiment in SENTIMENTS else "neutral",
        }

    # A comment the model skipped is filled in rather than left pending, so one
    # odd batch cannot be retried forever.
    return [
        by_index.get(i, {"kind": "other", "sentiment": "neutral"})
        for i in range(len(comments))
    ]


def classify_rows(
    df: pd.DataFrame,
    ids: Sequence[str] | None = None,
    progress_cb: CountCallback | None = None,
    batch_size: int = CLASSIFY_BATCH_SIZE,
    max_workers: int = CLASSIFY_WORKERS,
    on_batch: BatchCallback | None = None,
) -> tuple[pd.DataFrame, int]:
    """Bucket and score every unclassified row, or just `ids` when given.

    Batches run concurrently: each one is a network round trip that spends its
    life waiting, so firing several at once cuts a long run to a fraction of
    the sequential time. Results are applied as they land, in the calling
    thread, which keeps pandas single-threaded and lets the caller draw
    progress and persist partial work through `on_batch`.

    Returns (updated_frame, rows_classified). Rows already carrying both a kind
    and a sentiment are dropped before any call is made, so an interrupted run
    resumes exactly where it stopped rather than starting over.
    """
    tagged = ensure_columns(df)
    if tagged.empty:
        return tagged, 0

    pending = tagged.index[kind_pending_mask(tagged)]
    if ids is not None:
        wanted = {str(i) for i in ids}
        pending = [
            index for index in pending
            if str(tagged.at[index, "comment_id"]) in wanted
        ]
    else:
        pending = list(pending)
    if not pending:
        return tagged, 0

    client = _client()
    batches = [pending[i : i + batch_size] for i in range(0, len(pending), batch_size)]
    total = len(pending)
    done = 0
    first_error: Exception | None = None

    if progress_cb is not None:
        progress_cb(0, total)

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(batches)))) as pool:
        futures = {
            pool.submit(
                classify_batch,
                client,
                [tagged.at[index, "comment_text"] for index in batch],
            ): batch
            for batch in batches
        }

        for future in as_completed(futures):
            batch = futures[future]
            try:
                results = future.result()
            except Exception as exc:
                # Keep whatever else lands; one failed batch stays pending and
                # is picked up by the next click.
                first_error = first_error or exc
                continue

            for index, result in zip(batch, results):
                tagged.at[index, KIND_COLUMN] = result["kind"]
                tagged.at[index, SENTIMENT_COLUMN] = result["sentiment"]
            done += len(batch)

            if progress_cb is not None:
                progress_cb(done, total)
            if on_batch is not None:
                on_batch(
                    tagged, [str(tagged.at[index, "comment_id"]) for index in batch]
                )

    if done == 0 and first_error is not None:
        # Keep the original type in the message: "Classification failed" is
        # added by the caller, and the type is what identifies the fault.
        raise (
            first_error
            if isinstance(first_error, InsightsError)
            else InsightsError(f"{type(first_error).__name__}: {first_error}")
        )

    return tagged, done


def estimated_cost(count: int) -> float:
    """Rough dollars to classify `count` comments."""
    return count * COST_PER_COMMENT


def sentiment_percentages(df: pd.DataFrame) -> pd.DataFrame:
    """Keyword x sentiment as percentages of that keyword's scored comments.

    Percentages rather than counts so two keywords of very different sizes can
    be read side by side.
    """
    counts = sentiment_counts(df)
    if counts.empty:
        return counts
    totals = counts.sum(axis=1).replace(0, pd.NA)
    return (counts.div(totals, axis=0) * 100).round(1).fillna(0.0)


def examples_for(df: pd.DataFrame, sentiment: str, limit: int = 3) -> pd.DataFrame:
    """A few comments that stand for a sentiment bucket.

    Sorted by likes: the ones other viewers agreed with say more about the
    bucket than whichever happened to be collected first.
    """
    if df.empty or SENTIMENT_COLUMN not in df.columns:
        return df.iloc[0:0]
    rows = df[df[SENTIMENT_COLUMN].astype(str).str.strip() == sentiment]
    if rows.empty:
        return rows
    if "comment_likes" in rows.columns:
        rows = rows.sort_values("comment_likes", ascending=False, na_position="last")
    return rows.head(limit)


# --------------------------------------------------------------------------
# Aggregations for the Analysis view. All read stored columns; none call out.
# --------------------------------------------------------------------------
def _scored(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or SENTIMENT_COLUMN not in df.columns:
        return df.iloc[0:0]
    return df[df[SENTIMENT_COLUMN].astype(str).str.strip() != ""]


def sentiment_share(df: pd.DataFrame, by: str) -> pd.DataFrame:
    """Long-form percentage split of sentiment within each value of `by`.

    Returns columns [by, sentiment, pct, count] -- the shape Vega wants, and
    percentages so groups of different sizes stay comparable.
    """
    scored = _scored(df)
    if scored.empty or by not in scored.columns:
        return pd.DataFrame(columns=[by, "sentiment", "pct", "count"])

    counts = (
        scored.groupby([by, SENTIMENT_COLUMN]).size().rename("count").reset_index()
    )
    totals = counts.groupby(by)["count"].transform("sum")
    counts["pct"] = (counts["count"] / totals * 100).round(1)
    counts = counts.rename(columns={SENTIMENT_COLUMN: "sentiment"})
    return counts


def kind_sentiment_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """Long-form counts of sentiment within each kind, for a stacked bar."""
    scored = _scored(df)
    if scored.empty or KIND_COLUMN not in scored.columns:
        return pd.DataFrame(columns=["kind", "sentiment", "count"])
    rows = scored[scored[KIND_COLUMN].astype(str).str.strip() != ""]
    if rows.empty:
        return pd.DataFrame(columns=["kind", "sentiment", "count"])
    out = (
        rows.groupby([KIND_COLUMN, SENTIMENT_COLUMN]).size().rename("count").reset_index()
    )
    return out.rename(columns={KIND_COLUMN: "kind", SENTIMENT_COLUMN: "sentiment"})


def problem_videos(
    df: pd.DataFrame, min_comments: int = 10, limit: int = 10
) -> pd.DataFrame:
    """Videos ranked by how negative their comments are.

    Videos below `min_comments` are dropped: three comments and one grumpy
    viewer is 33% negative, which says nothing worth ranking.
    """
    scored = _scored(df)
    if scored.empty or "video_id" not in scored.columns:
        return pd.DataFrame(columns=["video_id", "video_title", "pct_negative", "comments"])

    grouped = scored.groupby("video_id")
    out = pd.DataFrame({
        "video_title": grouped["video_title"].first(),
        "comments": grouped.size(),
        "negative": grouped[SENTIMENT_COLUMN].apply(lambda c: int((c == "negative").sum())),
    })
    out = out[out["comments"] >= min_comments]
    if out.empty:
        return out.reset_index()
    out["pct_negative"] = (out["negative"] / out["comments"] * 100).round(1)
    out = out.sort_values(["pct_negative", "comments"], ascending=[False, False])
    return out.head(limit).reset_index()


def top_comment(df: pd.DataFrame, kind: str) -> pd.Series | None:
    """The most-liked comment of one kind, or None when there is not one."""
    scored = df[df.get(KIND_COLUMN, pd.Series(dtype=str)) == kind] if not df.empty else df
    if scored is None or scored.empty:
        return None
    if "comment_likes" in scored.columns:
        scored = scored.sort_values("comment_likes", ascending=False, na_position="last")
    return scored.iloc[0]


def digest_payload(df: pd.DataFrame, keywords: Sequence[str]) -> str:
    """The counts and comment sample the digest call is built from.

    Kept separate from the call so it can be fingerprinted for caching and
    read in a test without spending anything.
    """
    scored = _scored(df)
    total = len(scored)
    if not total:
        return ""

    lines = [f"Keywords: {', '.join(keywords) if keywords else 'all'}",
             f"Comments analysed: {total}"]

    counts = scored[SENTIMENT_COLUMN].value_counts()
    lines.append("Sentiment: " + ", ".join(
        f"{name} {int(counts.get(name, 0))} ({counts.get(name, 0) / total * 100:.0f}%)"
        for name in SENTIMENTS
    ))

    if KIND_COLUMN in scored.columns:
        kinds = scored[KIND_COLUMN].value_counts()
        lines.append("Types: " + ", ".join(
            f"{name} {int(kinds.get(name, 0))}" for name in KINDS if kinds.get(name, 0)
        ))

    # Sample the negatives more heavily: a theme worth acting on usually hides
    # in the complaints, and they are the minority of most comment sets.
    negatives = scored[scored[SENTIMENT_COLUMN] == "negative"]
    others = scored[scored[SENTIMENT_COLUMN] != "negative"]
    if "comment_likes" in scored.columns:
        negatives = negatives.sort_values("comment_likes", ascending=False)
        others = others.sort_values("comment_likes", ascending=False)
    sample = pd.concat([negatives.head(DIGEST_SAMPLE // 2),
                        others.head(DIGEST_SAMPLE // 2)])

    lines.append("\nComments:")
    for _, record in sample.iterrows():
        text = " ".join(str(record.get("comment_text", "")).split())[:220]
        lines.append(
            f"- [{record.get(SENTIMENT_COLUMN, '')}/"
            f"{record.get(KIND_COLUMN, '')}] {text}"
        )
    return "\n".join(lines)


def write_digest(payload: str) -> str:
    """One Claude call that turns the payload into a short written summary."""
    if not payload.strip():
        return ""
    try:
        response = _client().messages.create(
            model=MODEL,
            max_tokens=500,
            system=_DIGEST_SYSTEM,
            messages=[{"role": "user", "content": payload}],
        )
    except anthropic.AuthenticationError as exc:
        raise InsightsError(
            f"Anthropic rejected the API key. Check {SECRET_API_KEY} in secrets."
        ) from exc
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        raise InsightsError(f"Could not write the summary: {exc}") from exc

    text = next((b.text for b in response.content if b.type == "text"), "")
    return " ".join(text.split())


def sentiment_counts(df: pd.DataFrame) -> pd.DataFrame:
    """Keyword x sentiment count matrix, ready for st.bar_chart.

    Columns are always positive/neutral/negative in that order, so the chart
    colours stay stable no matter which sentiments a keyword happens to have.
    """
    if df.empty or "sentiment" not in df.columns:
        return pd.DataFrame(columns=SENTIMENTS)

    scored = df[df["sentiment"].astype(str).str.strip() != ""]
    if scored.empty:
        return pd.DataFrame(columns=SENTIMENTS)

    table = pd.crosstab(scored["keyword"], scored["sentiment"])
    for sentiment in SENTIMENTS:
        if sentiment not in table.columns:
            table[sentiment] = 0
    return table[SENTIMENTS]


def flagged_asks(df: pd.DataFrame) -> pd.DataFrame:
    """Rows carrying a non-empty flagged_ask, newest comment first."""
    if df.empty or "flagged_ask" not in df.columns:
        return df.iloc[0:0]

    asks = df[df["flagged_ask"].fillna("").astype(str).str.strip() != ""]
    if asks.empty or "comment_published_at" not in asks.columns:
        return asks
    return asks.sort_values("comment_published_at", ascending=False, na_position="last")
