"""Reddit collection, through the epctex/reddit-scraper actor on Apify.

Rows come out in the same schema youtube_fetcher emits, so everything
downstream -- the Sheet, both reports, translation, retention -- treats a
Reddit comment exactly like a YouTube one. Only the collecting differs.

Two things about Reddit shape the module:

- One call returns posts with their comments already nested, so there is no
  two-stage find-then-fetch. Comments come free with the post.
- The search is site-wide and reads post titles and bodies, which YouTube's
  cannot. Scoping to one subreddit is not offered: this actor ignores the
  query inside a subreddit URL and returns that community's listing instead,
  so a field for it would promise something it does not do.
- Its results collide with unrelated real things that share the word, which no
  keyword rule can separate. Claude brackets the search instead: it aims the
  queries at food first, then reads what came back and drops what is not. See
  relevance.py. YouTube's own filters are untouched by any of this.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
from typing import Sequence
from urllib.parse import quote_plus

from apify_client import ApifyClient

import relevance
from youtube_fetcher import COLUMNS, FetchReport, ProgressCallback, pack_metrics

_LOG = logging.getLogger(__name__)

PLATFORM = "reddit"

ACTOR = "epctex/reddit-scraper"

SECRET_TOKEN = "APIFY_API_TOKEN"

# Posts asked for per query. Our own choice, not a limit: the account is on
# Apify's Starter plan, which lifts the free tier's Demo Mode (5 runs a month,
# 10 items a run). Verified by asking for 18 and receiving 18. It stays at 10
# because every post is billed and three queries run per search.
POSTS_PER_QUERY = 10

DEFAULT_POSTS = POSTS_PER_QUERY

# Measured, not quoted: a query with comments billed $0.025 to $0.035, being
# one list query plus a fraction of a cent per post. Comments are billed per
# page at $0.00001, so they are effectively free.
COST_PER_QUERY_USD = 0.035

# What the plan grants each month, for the "how many searches is that" line.
MONTHLY_CREDIT_USD = 19.0

# What the actor calls its sorts and windows. Same names Reddit uses.
SORT_OPTIONS = ["relevance", "hot", "top", "new", "comments"]
TIME_OPTIONS = ["all", "year", "month", "week", "day", "hour"]

# Comment bodies Reddit leaves behind when something is taken down. They carry
# no opinion, so they never reach the Sheet.
DELETED_BODIES = {"[deleted]", "[removed]"}

# Reddit's rules bot. Its sticky comment is first on almost every post in a
# moderated subreddit and says nothing about the brand.
BOT_AUTHOR = "automoderator"


class RedditError(Exception):
    """Raised for configuration or Apify problems the user needs to see."""


def is_configured() -> bool:
    """True when an Apify token is available."""
    if os.environ.get(SECRET_TOKEN, "").strip():
        return True
    try:
        import streamlit as st

        return bool(str(st.secrets.get(SECRET_TOKEN, "")).strip())
    except Exception:
        # st.secrets raises when there is no secrets.toml at all.
        return False


def _token() -> str:
    token = os.environ.get(SECRET_TOKEN, "").strip()
    if token:
        return token
    try:
        import streamlit as st

        token = str(st.secrets.get(SECRET_TOKEN, "")).strip()
    except Exception:
        token = ""
    if not token:
        raise RedditError(
            f"No {SECRET_TOKEN} found in secrets, so Reddit cannot be searched."
        )
    return token


def build_client() -> ApifyClient:
    """The Apify client. Separated so tests can hand back a fake."""
    return ApifyClient(_token())


def _actor_input(keyword: str, max_posts: int, sort: str, time_range: str) -> dict:
    """What the actor is asked for: one site-wide search for posts."""
    return {
        "search": keyword,
        "searchMode": "link",      # posts, not communities or users
        "sort": sort,
        "time": time_range,
        "includeComments": True,
        "maxItems": int(max_posts),
    }


def _as_int(value: object) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _iso(epoch: object) -> str:
    """Reddit's epoch seconds as the ISO text every other row uses."""
    try:
        seconds = float(epoch)
    except (TypeError, ValueError):
        return ""
    return _dt.datetime.fromtimestamp(
        seconds, _dt.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_placeholder(post: object) -> bool:
    """True for the stand-in records the actor emits instead of results.

    The actor answers with {"demo": true} rows rather than posts when it will
    not serve the account, which on the free tier meant the monthly run
    allowance. They carry no id and no title, and silently produced an empty
    search, so they are detected and reported.
    """
    if not isinstance(post, dict):
        return True
    if post.get("demo"):
        return True
    return not str(post.get("id", "") or "").strip()


def is_noise(comment: dict, position: int) -> bool:
    """True for comments that carry nothing worth storing.

    Deleted and removed bodies are placeholders, not opinions. AutoModerator's
    sticky is the subreddit's rules, and it sits first on almost every post.
    """
    body = str(comment.get("body", "") or "").strip()
    if not body or body.lower() in DELETED_BODIES:
        return True
    author = str(comment.get("author", "") or "").strip().lower()
    return position == 0 and author == BOT_AUTHOR


def walk_comments(
    comments: Sequence[dict], parent: str = "", top_level: bool = True
) -> list[tuple[dict, str]]:
    """A post's comment tree flattened to (comment, parent id) pairs.

    Reddit nests replies inside their parent; the Sheet is flat and marks a
    reply with is_reply and the id it answers, exactly as YouTube's are.

    Noise is skipped but not followed by its replies: a real answer under a
    deleted comment is still worth reading, so it is re-parented to whatever
    the dropped comment hung from rather than lost with it.
    """
    out: list[tuple[dict, str]] = []
    for position, comment in enumerate(comments or []):
        if not isinstance(comment, dict):
            continue
        comment_id = str(comment.get("id", "") or "")
        replies = comment.get("replies") or []

        # Position only matters at the top: the rules bot's sticky is the
        # first top-level comment, and a bot deeper in a thread is left alone.
        if not comment_id or is_noise(comment, position if top_level else 1):
            out.extend(walk_comments(replies, parent, top_level=False))
            continue

        out.append((comment, parent))
        out.extend(walk_comments(replies, comment_id, top_level=False))
    return out


def post_rows(post: dict, keyword: str, fetched_at: str) -> list[dict]:
    """One post's comments as rows in the shared schema."""
    subreddit = str(post.get("subreddit", "") or "")
    metrics = pack_metrics({
        "upvotes": _as_int(post.get("score")),
        "upvote_ratio": post.get("upvoteRatio"),
        "num_comments": _as_int(post.get("commentCount")),
    })

    rows: list[dict] = []
    for comment, parent in walk_comments(post.get("comments") or []):
        rows.append({
            "comment_id": str(comment.get("id", "")),
            "platform": PLATFORM,
            "keyword": keyword,
            "source_id": str(post.get("id", "")),
            "source_title": str(post.get("title", "") or ""),
            "channel_or_subreddit": subreddit,
            "source_url": str(post.get("url", "") or ""),
            "source_published_at": _iso(post.get("createdAt")),
            # Upvotes are Reddit's headline number, the way views are YouTube's.
            "engagement_score": _as_int(post.get("score")),
            "platform_metrics": metrics,
            # A YouTube distinction. Reddit posts have no equivalent.
            "video_type": "",
            "comment_author": str(comment.get("author", "") or ""),
            "comment_text": str(comment.get("body", "") or ""),
            "comment_likes": _as_int(comment.get("score")),
            "comment_published_at": _iso(comment.get("createdAt")),
            "is_reply": bool(parent),
            "parent_comment_id": parent,
            "reply_count": len(comment.get("replies") or []),
            "fetched_at": fetched_at,
        })
    return rows


def fetch(
    keyword: str,
    max_posts: int = DEFAULT_POSTS,
    sort: str = "relevance",
    time_range: str = "all",
    client: ApifyClient | None = None,
    progress_cb: ProgressCallback | None = None,
    expand: bool = True,
    classify: bool = True,
    variants: int = relevance.DEFAULT_VARIANTS,
) -> tuple[list[dict], FetchReport]:
    """Search Reddit and return comment rows in the shared schema.

    Each query is one actor call, and the posts come back with their comments
    already attached, so there is no separate comment fetch to pay for.

    `expand` aims the keyword at food before searching and `classify` drops
    what came back anyway. Both need an Anthropic key; without one the search
    still runs, unfiltered, rather than failing.
    """
    report = FetchReport()
    keyword = str(keyword or "").strip()
    if not keyword:
        return [], report

    # A bare keyword collides with whatever else shares the word, so the
    # search is aimed at food before it is sent. Each query is its own actor
    # run, which is why the count is small and stated in the UI.
    queries = [keyword]
    if expand and relevance.is_configured():
        queries = relevance.expand_keyword(keyword, variants=variants)
    report.queries = list(queries)

    client = client or build_client()
    steps = len(queries) + 1
    posts: list[dict] = []
    seen_posts: set[str] = set()
    placeholder_runs = 0
    capped_runs = 0

    for index, query in enumerate(queries):
        if progress_cb is not None:
            progress_cb(index, steps, f"Searching Reddit for '{query}'...")

        run_input = _actor_input(query, max_posts, sort, time_range)
        _LOG.info("Reddit search: %s", run_input)
        try:
            run = client.actor(ACTOR).call(run_input=run_input)
            items = list(client.dataset(run.default_dataset_id).iterate_items())
        except Exception as exc:
            raise RedditError(f"The Reddit search failed: {exc}") from exc

        if len(items) >= POSTS_PER_QUERY:
            capped_runs += 1
        if items and all(is_placeholder(item) for item in items):
            placeholder_runs += 1
            continue
        for item in items:
            post_id = str(item.get("id", "") or "") if isinstance(item, dict) else ""
            if is_placeholder(item) or post_id in seen_posts:
                continue
            seen_posts.add(post_id)
            posts.append(item)

    if placeholder_runs == len(queries):
        raise RedditError(
            f"Reddit returned placeholder results for all "
            f"{len(queries)} quer{'y' if len(queries) == 1 else 'ies'} "
            "instead of posts. That means the Apify account will not serve "
            "the run: check its plan and usage at console.apify.com."
        )
    if placeholder_runs:
        report.warn(
            f"{placeholder_runs} of {len(queries)} searches came back as "
            "placeholders rather than posts. Check the Apify account's usage."
        )

    # What came back still holds unrelated things that share the word, so
    # every post is read before its comments are kept.
    if classify and posts and relevance.is_configured():
        if progress_cb is not None:
            progress_cb(len(queries), steps, f"Checking {len(posts)} post(s)...")
        try:
            verdicts = relevance.classify_posts(posts, keyword=keyword)
        except relevance.RelevanceError as exc:
            _LOG.warning("Relevance check failed (%s); keeping every post", exc)
            report.warn(f"Posts could not be checked for relevance: {exc}")
        else:
            # A several-word keyword must appear as a whole thing. Reddit
            # matches any word in it, so "DESi POPz" returns posts carrying
            # only "desi", which is a common word and not a result.
            phrase = relevance.is_phrase(keyword)

            def keep(post: dict) -> bool:
                verdict = verdicts.get(str(post.get("id")), {})
                if not verdict.get("is_food", True):
                    return False
                return not phrase or verdict.get("mentions_keyword", True)

            kept = [p for p in posts if keep(p)]
            report.posts_off_topic = len(posts) - len(kept)
            for post in posts:
                if keep(post):
                    continue
                verdict = verdicts.get(str(post.get("id")), {})
                why = (
                    "not about food" if not verdict.get("is_food", True)
                    else f"never mentions {keyword!r} as a whole"
                )
                _LOG.info(
                    "Reddit: dropped %r (%s) as %r",
                    str(post.get("title"))[:70], why, verdict.get("subject"),
                )
            posts = kept

    if progress_cb is not None:
        progress_cb(steps - 1, steps, f"Reading comments from {len(posts)} post(s)...")

    fetched_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows: list[dict] = []
    seen: set[str] = set()
    for post in posts:
        if is_placeholder(post) or post.get("type") == "comment":
            continue
        report.videos_searched += 1
        post_comments = post_rows(post, keyword, fetched_at)
        if post_comments:
            report.videos_with_comments += 1
        for row in post_comments:
            if row["comment_id"] in seen:
                continue
            seen.add(row["comment_id"])
            rows.append(row)

    report.comments_fetched = len(rows)
    if capped_runs:
        # Counted per run: de-duplication across queries can leave fewer
        # unique posts than the ceiling even when every run reached it.
        report.warn(
            f"{capped_runs} of {len(queries)} search(es) returned the full "
            f"{POSTS_PER_QUERY} posts asked for, so Reddit may hold more."
        )

    if progress_cb is not None:
        progress_cb(2, 2, "Done.")

    return rows, report
