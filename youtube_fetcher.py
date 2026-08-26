"""YouTube Data API v3 fetching logic for the GO DESi Social Listening app.

Three public building blocks -- search_videos(), get_video_stats(), get_comments() --
plus run_search(), which stitches them together across multiple keywords and reports
progress back to the UI.

Nothing in here touches Streamlit, so it can be run from a plain script or a notebook.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Sequence

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import transcripts

# The raw API error goes here as well as into the message the user sees.
_LOG = logging.getLogger(__name__)

# Single source of truth for the row schema. sheets_store imports this so the
# worksheet header and the DataFrame can never drift apart.
COLUMNS: list[str] = [
    "comment_id",
    "keyword",
    "video_id",
    "video_title",
    "channel_title",
    "video_url",
    "video_published_at",
    "video_views",
    "video_likes",
    "video_comment_count",
    "video_type",
    "comment_author",
    "comment_text",
    "comment_likes",
    "comment_published_at",
    "is_reply",
    "parent_comment_id",
    "reply_count",
    "fetched_at",
]

# The column that makes a row unique. Used for dedupe on write and on load.
ID_COLUMN = "comment_id"

# API hard limits. MAX_COMMENT_PAGE and MAX_VIDEO_IDS_PER_CALL are public
# because app.py sizes its quota estimate from them.
_MAX_SEARCH_PAGE = 50
MAX_COMMENT_PAGE = 100
MAX_VIDEO_IDS_PER_CALL = 50


class YouTubeError(Exception):
    """Base class for anything this module raises deliberately."""


class QuotaExceededError(YouTubeError):
    """The API key has burned through its daily quota. Nothing to do but wait."""


class InvalidAPIKeyError(YouTubeError):
    """The key is missing, malformed, or not authorised for the YouTube Data API."""


@dataclass
class FetchReport:
    """What a run produced beyond the comment rows themselves.

    Mostly non-fatal warnings surfaced in the UI afterwards, plus the video
    summaries collected along the way -- those are keyed by video_id rather than
    per comment row, because one video's summary serves all of its comments.
    """

    warnings: list[str] = field(default_factory=list)
    videos_searched: int = 0
    videos_with_comments: int = 0
    comments_fetched: int = 0
    quota_exhausted: bool = False
    transcripts_found: int = 0
    transcripts_missing: int = 0
    transcripts_blocked: int = 0
    transcripts_error: int = 0
    transcripts_attempted: int = 0
    # Set once YouTube blocks this IP. Further fetches are pointless, so the
    # rest of the run stops asking instead of hammering a closed door.
    transcripts_halted: bool = False
    video_summaries: dict[str, dict] = field(default_factory=dict)
    # video_id -> why its transcript did not arrive, for the per-video message.
    transcript_outcomes: dict[str, str] = field(default_factory=dict)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


# Progress callback: (current, total, label) -> None
ProgressCallback = Callable[[int, int, str], None]

# Seconds to wait between transcript fetches. The endpoint is unofficial and
# rate limited by IP; twenty back-to-back requests per keyword is what got this
# IP blocked in the first place, so the calls are spaced out.
TRANSCRIPT_DELAY_SECONDS = 1.5

# Summariser: (video_title, transcript_text, language) -> 2-3 sentence summary.
# Injected by the caller so this module stays free of Streamlit and Claude.
Summarizer = Callable[[str, str, str], str]


def collect_video_summary(
    video: dict,
    summarizer: Summarizer | None,
    report: FetchReport | None = None,
) -> dict | None:
    """Fetch one video's transcript and summarise it.

    Returns a row for the videos sheet, or None when the video has no usable
    transcript. Transcript fetching is unofficial and summarising is a network
    call to Claude; neither is allowed to raise into the search loop, so every
    failure here degrades to "no summary for this video".
    """
    video_id = video.get("video_id", "")
    if not video_id:
        return None

    # Once blocked, stop asking. The remaining videos are recorded as blocked
    # rather than retried one by one against an IP that is already refused.
    if report is not None and report.transcripts_halted:
        report.transcripts_blocked += 1
        report.transcript_outcomes[video_id] = transcripts.BLOCKED
        return None

    # Space the calls out. The first fetch of a run goes straight through; the
    # delay only sits between requests.
    if report is not None and report.transcripts_attempted:
        time.sleep(TRANSCRIPT_DELAY_SECONDS)
    if report is not None:
        report.transcripts_attempted += 1

    try:
        result = transcripts.fetch_transcript(video_id)
    except Exception as exc:  # the library should not raise, but it is theirs
        result = transcripts.TranscriptResult(transcripts.ERROR, detail=str(exc))

    if report is not None:
        report.transcript_outcomes[video_id] = result.outcome

    if result.outcome == transcripts.BLOCKED:
        if report is not None:
            report.transcripts_blocked += 1
            report.transcripts_halted = True
            report.warn(
                "YouTube is rate limiting transcript downloads from this IP, so "
                "the rest of this run skipped them. They are not missing "
                "captions; try again later."
            )
        return None

    if result.outcome == transcripts.ERROR:
        if report is not None:
            report.transcripts_error += 1
            report.warn(f"Transcript fetch failed for {video_id}: {result.detail}")
        return None

    transcript = result.transcript
    if transcript is None:
        if report is not None:
            report.transcripts_missing += 1
        return None

    if report is not None:
        report.transcripts_found += 1

    summary = ""
    if summarizer is not None:
        try:
            summary = summarizer(
                video.get("video_title", ""), transcript.text, transcript.language
            )
        except Exception as exc:
            if report is not None:
                report.warn(f"Could not summarise video {video_id}: {exc}")
            summary = ""

    return {
        "video_id": video_id,
        "video_title": video.get("video_title", ""),
        "video_url": video.get("video_url", ""),
        "video_summary": summary,
        "transcript_language": transcript.language,
        "transcript_is_english": transcript.is_english,
        "summarized_at": _now_iso(),
    }


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _error_body(error: HttpError) -> dict:
    """The parsed JSON error object, or an empty dict when it is not JSON."""
    body = getattr(error, "content", b"") or b""
    text = body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else str(body)
    try:
        return json.loads(text).get("error", {}) or {}
    except (ValueError, AttributeError):
        return {}


def _error_reason(error: HttpError) -> str:
    """The machine-readable reason, e.g. 'quotaExceeded' or 'invalidRegionCode'.

    Read from the response rather than matched against a fixed list, so a
    reason nobody anticipated still arrives intact instead of coming back
    empty and being guessed at further up.
    """
    try:
        details = error.error_details  # googleapiclient >= 2.x
        if details:
            reason = str(details[0].get("reason", "")).strip()
            if reason:
                return reason
    except (AttributeError, IndexError, TypeError):
        pass

    errors = _error_body(error).get("errors") or []
    if errors:
        return str(errors[0].get("reason", "")).strip()
    return ""


def _error_detail(error: HttpError) -> str:
    """What YouTube actually said, in one line, for the message and the log.

    The friendly wrapper used to be all anyone saw, which meant a 400 about a
    bad parameter was reported as a rejected key. The API's own words travel
    with every error now.
    """
    parsed = _error_body(error)
    status = getattr(getattr(error, "resp", None), "status", "?")
    reason = _error_reason(error) or "no reason given"
    message = str(parsed.get("message", "")).strip()
    if not message:
        body = getattr(error, "content", b"") or b""
        message = (
            body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else str(body)
        )[:200]
    return f"HTTP {status} {reason}: {message}"


def _raise_if_fatal(error: HttpError) -> None:
    """Convert the errors that should stop the whole run into our own exceptions.

    Everything else is left for the caller to treat as a per-video warning.
    """
    reason = _error_reason(error)
    status = getattr(getattr(error, "resp", None), "status", 0)
    detail = _error_detail(error)
    _LOG.error("YouTube API error: %s", detail)

    if reason in ("quotaExceeded", "rateLimitExceeded"):
        raise QuotaExceededError(
            "YouTube API daily quota exhausted. Results below are partial - "
            f"quota resets at midnight Pacific Time. ({detail})"
        ) from error

    # Only blame the key when YouTube blamed the key.
    if reason in ("keyInvalid", "accessNotConfigured", "forbidden") or (
        status == 403 and reason not in ("commentsDisabled",)
    ):
        raise InvalidAPIKeyError(
            "YouTube refused the request. Check YOUTUBE_API_KEY in your secrets, "
            "that the YouTube Data API v3 is enabled for that project, and any "
            f"referrer or IP restriction on the key. ({detail})"
        ) from error

    if status == 400:
        # A bad key also comes back as a generic 400 "badRequest", so the
        # message is what separates the two. Blame the key only when YouTube
        # says the key is the problem; otherwise it is a bad parameter.
        message = str(_error_body(error).get("message", "")).lower()
        if "api key" in message or "api_key" in message:
            raise InvalidAPIKeyError(
                "YouTube rejected the API key. Check YOUTUBE_API_KEY in your "
                "secrets and that the YouTube Data API v3 is enabled for that "
                f"project. ({detail})"
            ) from error
        raise YouTubeError(f"YouTube rejected the request. ({detail})") from error


# A Short is a minute or less. YouTube reports duration as an ISO 8601 period
# on contentDetails, which videos.list already returns in the same call as the
# statistics -- so this classification costs no extra quota.
SHORT_MAX_SECONDS = 60

VIDEO_TYPE_SHORT = "Short"
VIDEO_TYPE_VIDEO = "Video"

_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?$"
)


def parse_duration(value: object) -> int:
    """ISO 8601 duration to whole seconds. Returns 0 when it cannot be read."""
    match = _DURATION.match(str(value or "").strip())
    if not match:
        return 0
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def classify_video(duration: object) -> str:
    """Short or Video, by the same minute rule YouTube uses.

    A duration we cannot read comes back as a Video: guessing "Short" would
    quietly hide long videos behind a Shorts-only filter.
    """
    seconds = parse_duration(duration)
    if 0 < seconds <= SHORT_MAX_SECONDS:
        return VIDEO_TYPE_SHORT
    return VIDEO_TYPE_VIDEO


def _as_int(value: object) -> int:
    """YouTube returns counts as strings, and omits them entirely when hidden."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def build_client(api_key: str):
    """Create an authenticated YouTube API client.

    cache_discovery is off because the default file cache is noisy (and unusable)
    in containerised deployments like Streamlit Cloud.
    """
    if not api_key or not api_key.strip():
        raise InvalidAPIKeyError(
            "No YouTube API key provided. Add YOUTUBE_API_KEY to .streamlit/secrets.toml."
        )
    return build("youtube", "v3", developerKey=api_key.strip(), cache_discovery=False)


def search_videos(
    youtube,
    keyword: str,
    max_results: int = 20,
    order: str = "relevance",
    published_after: _dt.datetime | None = None,
    region_code: str | None = None,
) -> list[dict]:
    """Search YouTube for `keyword` and return up to `max_results` video stubs.

    Costs 100 quota units per page of up to 50 results, so this is by far the
    most expensive call in the app.
    """
    if max_results <= 0:
        return []

    results: list[dict] = []
    page_token: str | None = None

    while len(results) < max_results:
        params = {
            "q": keyword,
            "part": "id,snippet",
            "type": "video",
            "order": order,
            "maxResults": min(_MAX_SEARCH_PAGE, max_results - len(results)),
        }
        if page_token:
            params["pageToken"] = page_token
        if published_after is not None:
            params["publishedAfter"] = published_after.strftime("%Y-%m-%dT%H:%M:%SZ")
        if region_code:
            params["regionCode"] = region_code

        try:
            response = youtube.search().list(**params).execute()
        except HttpError as exc:
            _raise_if_fatal(exc)
            raise YouTubeError(f"Search failed for '{keyword}': {exc}") from exc

        for item in response.get("items", []):
            video_id = item.get("id", {}).get("videoId")
            if not video_id:
                continue
            snippet = item.get("snippet", {})
            results.append(
                {
                    "video_id": video_id,
                    "video_title": snippet.get("title", ""),
                    "channel_title": snippet.get("channelTitle", ""),
                    "video_published_at": snippet.get("publishedAt", ""),
                    "video_url": f"https://www.youtube.com/watch?v={video_id}",
                }
            )

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return results[:max_results]


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def get_video_stats(youtube, video_ids: Iterable[str]) -> dict[str, dict]:
    """Fetch view/like/comment counts for videos, keyed by video_id.

    Batched 50 ids per request -- each request costs 1 unit regardless of batch
    size, so batching matters a lot for quota.
    """
    ids = [vid for vid in dict.fromkeys(video_ids) if vid]  # de-dupe, keep order
    if not ids:
        return {}

    stats: dict[str, dict] = {}
    for batch in _chunks(ids, MAX_VIDEO_IDS_PER_CALL):
        try:
            response = (
                youtube.videos()
                .list(part="statistics,snippet,contentDetails", id=",".join(batch))
                .execute()
            )
        except HttpError as exc:
            _raise_if_fatal(exc)
            # A non-fatal stats failure shouldn't lose the comments; fall through
            # with empty stats for this batch.
            continue

        for item in response.get("items", []):
            video_id = item.get("id")
            if not video_id:
                continue
            statistics = item.get("statistics", {})
            snippet = item.get("snippet", {})
            details = item.get("contentDetails", {})
            stats[video_id] = {
                "video_views": _as_int(statistics.get("viewCount")),
                "video_likes": _as_int(statistics.get("likeCount")),
                "video_comment_count": _as_int(statistics.get("commentCount")),
                "video_title": snippet.get("title", ""),
                "channel_title": snippet.get("channelTitle", ""),
                "video_published_at": snippet.get("publishedAt", ""),
                "video_type": classify_video(details.get("duration")),
            }

    return stats


def get_comments(
    youtube,
    video_id: str,
    max_comments: int = 50,
    include_replies: bool = True,
    report: FetchReport | None = None,
) -> list[dict]:
    """Fetch up to `max_comments` comments for one video.

    Replies come back inside the same commentThreads response, so including them
    costs no extra quota. Videos with comments disabled return [] and log a
    warning rather than raising.
    """
    if max_comments <= 0:
        return []

    comments: list[dict] = []
    page_token: str | None = None
    parts = "snippet,replies" if include_replies else "snippet"

    while len(comments) < max_comments:
        params = {
            "videoId": video_id,
            "part": parts,
            "maxResults": min(MAX_COMMENT_PAGE, max_comments - len(comments)),
            "textFormat": "plainText",
            "order": "relevance",
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            response = youtube.commentThreads().list(**params).execute()
        except HttpError as exc:
            _raise_if_fatal(exc)
            reason = _error_reason(exc)
            if report is not None:
                if reason == "commentsDisabled":
                    report.warn(f"Comments are disabled on video {video_id} - skipped.")
                elif reason == "videoNotFound":
                    report.warn(f"Video {video_id} is unavailable (private/deleted).")
                else:
                    report.warn(f"Could not read comments for {video_id}: {exc}")
            break

        for thread in response.get("items", []):
            top = thread.get("snippet", {}).get("topLevelComment", {})
            top_snippet = top.get("snippet", {})
            top_id = top.get("id", "")
            if top_id:
                comments.append(
                    {
                        "comment_id": top_id,
                        "comment_author": top_snippet.get("authorDisplayName", ""),
                        "comment_text": top_snippet.get("textDisplay", ""),
                        "comment_likes": _as_int(top_snippet.get("likeCount")),
                        "comment_published_at": top_snippet.get("publishedAt", ""),
                        "is_reply": False,
                        "parent_comment_id": "",
                        "reply_count": _as_int(
                            thread.get("snippet", {}).get("totalReplyCount")
                        ),
                    }
                )

            if not include_replies:
                continue
            for reply in thread.get("replies", {}).get("comments", []):
                if len(comments) >= max_comments:
                    break
                reply_snippet = reply.get("snippet", {})
                reply_id = reply.get("id", "")
                if not reply_id:
                    continue
                comments.append(
                    {
                        "comment_id": reply_id,
                        "comment_author": reply_snippet.get("authorDisplayName", ""),
                        "comment_text": reply_snippet.get("textDisplay", ""),
                        "comment_likes": _as_int(reply_snippet.get("likeCount")),
                        "comment_published_at": reply_snippet.get("publishedAt", ""),
                        "is_reply": True,
                        "parent_comment_id": top_id,
                        "reply_count": 0,
                    }
                )

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return comments[:max_comments]


def parse_keywords(raw: str) -> list[str]:
    """Split the comma-separated keyword box into a clean, de-duplicated list."""
    seen: dict[str, None] = {}
    for part in (raw or "").split(","):
        cleaned = part.strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


def run_search(
    api_key: str,
    keywords: Sequence[str],
    max_videos: int = 20,
    max_comments: int = 500,
    order: str = "relevance",
    include_replies: bool = True,
    published_after: _dt.datetime | None = None,
    region_code: str | None = None,
    progress_cb: ProgressCallback | None = None,
    summarizer: Summarizer | None = None,
    known_video_ids: Iterable[str] | None = None,
) -> tuple[list[dict], FetchReport]:
    """Run a full search across every keyword and return flat comment rows.

    Returns (rows, report). Partial results are always returned -- if the quota
    runs out halfway through, whatever was collected up to that point comes back
    with report.quota_exhausted set, instead of the run raising and losing everything.

    Each video new to this run also gets its transcript pulled and summarised
    into report.video_summaries. `known_video_ids` lists videos already
    summarised on a previous run, which are skipped -- a transcript is only ever
    fetched and summarised once per video.
    """
    report = FetchReport()
    rows: list[dict] = []
    fetched_at = _now_iso()
    seen_videos = {str(vid) for vid in (known_video_ids or ())}

    youtube = build_client(api_key)

    # Total steps = one unit of work per (keyword, video) pair, used for the bar.
    total_steps = max(1, len(keywords) * max_videos)
    step = 0

    def tick(label: str) -> None:
        if progress_cb is not None:
            progress_cb(min(step, total_steps), total_steps, label)

    for keyword in keywords:
        tick(f"Searching YouTube for '{keyword}'...")
        try:
            videos = search_videos(
                youtube,
                keyword,
                max_results=max_videos,
                order=order,
                published_after=published_after,
                region_code=region_code,
            )
        except QuotaExceededError as exc:
            report.quota_exhausted = True
            report.warn(str(exc))
            break
        except YouTubeError as exc:
            report.warn(str(exc))
            step += max_videos
            continue

        if not videos:
            report.warn(f"No videos found for '{keyword}'.")
            step += max_videos
            continue

        report.videos_searched += len(videos)

        # One batched stats call per keyword rather than one per video.
        try:
            stats = get_video_stats(youtube, [v["video_id"] for v in videos])
        except QuotaExceededError as exc:
            report.quota_exhausted = True
            report.warn(str(exc))
            break

        for index, video in enumerate(videos, start=1):
            step += 1
            tick(f"'{keyword}' - video {index}/{len(videos)}: {video['video_title'][:60]}")

            # Transcript and summary happen once per video, across all keywords.
            if video["video_id"] not in seen_videos:
                seen_videos.add(video["video_id"])
                summary_row = collect_video_summary(video, summarizer, report)
                if summary_row is not None:
                    report.video_summaries[video["video_id"]] = summary_row

            try:
                comments = get_comments(
                    youtube,
                    video["video_id"],
                    max_comments=max_comments,
                    include_replies=include_replies,
                    report=report,
                )
            except QuotaExceededError as exc:
                report.quota_exhausted = True
                report.warn(str(exc))
                # Bail out of everything, but keep the rows already collected.
                return rows, report

            if not comments:
                continue

            report.videos_with_comments += 1
            video_stats = stats.get(video["video_id"], {})

            for comment in comments:
                rows.append(
                    {
                        "comment_id": comment["comment_id"],
                        "keyword": keyword,
                        "video_id": video["video_id"],
                        "video_title": video_stats.get("video_title")
                        or video["video_title"],
                        "channel_title": video_stats.get("channel_title")
                        or video["channel_title"],
                        "video_url": video["video_url"],
                        "video_published_at": video_stats.get("video_published_at")
                        or video["video_published_at"],
                        "video_views": video_stats.get("video_views", 0),
                        "video_likes": video_stats.get("video_likes", 0),
                        "video_comment_count": video_stats.get("video_comment_count", 0),
                        "video_type": video_stats.get("video_type", ""),
                        "comment_author": comment["comment_author"],
                        "comment_text": comment["comment_text"],
                        "comment_likes": comment["comment_likes"],
                        "comment_published_at": comment["comment_published_at"],
                        "is_reply": comment["is_reply"],
                        "parent_comment_id": comment["parent_comment_id"],
                        "reply_count": comment["reply_count"],
                        "fetched_at": fetched_at,
                    }
                )

    report.comments_fetched = len(rows)
    tick("Done.")
    return rows, report
