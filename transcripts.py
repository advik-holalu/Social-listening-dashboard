"""YouTube transcript fetching for the Social Listening app.

Transcripts come from youtube-transcript-api, which scrapes the caption tracks
the YouTube player itself uses. It is not the official Data API, so it costs no
quota -- but it is also unofficial, which is why every failure here is treated
as "no transcript" rather than an error worth stopping a run for.

Nothing in here touches Streamlit or Claude, so it can be exercised from a
plain script.
"""

from __future__ import annotations

from dataclasses import dataclass

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api import (
    AgeRestricted,
    InvalidVideoId,
    NoTranscriptFound,
    NotTranslatable,
    RequestBlocked,
    TranscriptsDisabled,
    TranslationLanguageNotAvailable,
    VideoUnavailable,
    VideoUnplayable,
)

# What happened when we asked for a transcript. The distinction matters: a
# video with captions disabled is a permanent fact, while a block is YouTube
# throttling this IP and will pass. Collapsing them into one "no transcript"
# is what hid a total block behind a message about missing captions.
FOUND = "found"
UNAVAILABLE = "unavailable"
BLOCKED = "blocked"
ERROR = "error"

# Nothing will ever produce a transcript for these: no captions published,
# captions switched off, the video gone, private, or age-gated.
_ABSENT = (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    VideoUnplayable,
    AgeRestricted,
    InvalidVideoId,
    NotTranslatable,
    TranslationLanguageNotAvailable,
)

# Preference order when the video has more than one caption track. Anything
# outside this list is still accepted -- see fetch_transcript().
_ENGLISH = ("en", "en-US", "en-GB", "en-IN")

# Transcripts of long videos run to tens of thousands of characters. The
# summary only needs the substance, and this bounds what gets sent to Claude.
MAX_TRANSCRIPT_CHARS = 12000


@dataclass
class Transcript:
    """One video's caption track, flattened to plain text."""

    video_id: str
    text: str
    language: str
    language_code: str
    is_english: bool

    @property
    def truncated(self) -> bool:
        return self.text.endswith("...[truncated]")


@dataclass
class TranscriptResult:
    """The transcript if we got one, and why not if we did not."""

    outcome: str
    transcript: Transcript | None = None
    detail: str = ""

    @property
    def found(self) -> bool:
        return self.outcome == FOUND

    @property
    def blocked(self) -> bool:
        return self.outcome == BLOCKED


def _flatten(fetched) -> str:
    """Join caption snippets into one blob, dropping the timing metadata."""
    parts = [
        str(snippet.text).strip()
        for snippet in fetched.snippets
        if str(snippet.text).strip()
    ]
    text = " ".join(parts)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        return text[:MAX_TRANSCRIPT_CHARS] + " ...[truncated]"
    return text


def fetch_transcript(video_id: str) -> TranscriptResult:
    """Fetch one video's transcript and say plainly what happened.

    Never raises, but never lies either: the caller gets FOUND, UNAVAILABLE
    (this video will never have one), BLOCKED (YouTube is throttling this IP,
    try later) or ERROR (network trouble, or the unofficial endpoint changed
    shape under us).
    """
    if not video_id:
        return TranscriptResult(UNAVAILABLE, detail="no video id")

    try:
        available = YouTubeTranscriptApi().list(video_id)
    except RequestBlocked as exc:
        return TranscriptResult(BLOCKED, detail=_reason(exc))
    except _ABSENT as exc:
        return TranscriptResult(UNAVAILABLE, detail=_reason(exc))
    except Exception as exc:
        return TranscriptResult(ERROR, detail=_reason(exc))

    try:
        transcript = available.find_transcript(_ENGLISH)
    except NoTranscriptFound:
        # No English track. Take whatever the video does have, untranslated.
        transcript = next(iter(available), None)
    except RequestBlocked as exc:
        return TranscriptResult(BLOCKED, detail=_reason(exc))
    except Exception as exc:
        return TranscriptResult(ERROR, detail=_reason(exc))

    if transcript is None:
        return TranscriptResult(UNAVAILABLE, detail="no caption tracks")

    # The block usually lands here rather than on list(): the caption tracks
    # are visible while the fetch of their contents is refused.
    try:
        fetched = transcript.fetch()
    except RequestBlocked as exc:
        return TranscriptResult(BLOCKED, detail=_reason(exc))
    except _ABSENT as exc:
        return TranscriptResult(UNAVAILABLE, detail=_reason(exc))
    except Exception as exc:
        return TranscriptResult(ERROR, detail=_reason(exc))

    text = _flatten(fetched)
    if not text:
        return TranscriptResult(UNAVAILABLE, detail="caption track was empty")

    code = str(getattr(fetched, "language_code", "") or "")
    return TranscriptResult(
        FOUND,
        transcript=Transcript(
            video_id=video_id,
            text=text,
            language=str(getattr(fetched, "language", "") or code or "unknown"),
            language_code=code,
            is_english=code.lower().startswith("en"),
        ),
    )


def _reason(exc: Exception) -> str:
    """A one-line reason for the logs, without the library's essay."""
    first = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {first[0][:120]}" if first else type(exc).__name__
