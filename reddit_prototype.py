"""Standalone probe: what does Reddit data look like, via Apify?

Nothing in the app imports this and it imports nothing from the app. It exists
to answer one question before any integration work starts: for a keyword like
"GO DESi" or "Chikki", what actually comes back, and is it worth having?

Run it from the project root:

    python reddit_prototype.py                     # both keywords, 20 posts each
    python reddit_prototype.py "imli pop"          # one keyword of your own
    python reddit_prototype.py --posts 5           # smaller and cheaper
    python reddit_prototype.py --no-comments       # posts only
    python reddit_prototype.py --raw               # dump one whole record

The actor bills per item and comments are billed as items too, so the post
count is capped and asked for explicitly rather than defaulted to everything.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap

from apify_client import ApifyClient

# The same secrets file the rest of the project reads. Streamlit is only used
# for that, and the environment wins so this can run anywhere.
SECRET_TOKEN = "APIFY_API_TOKEN"

ACTOR = "epctex/reddit-scraper"

KEYWORDS = ["GO DESi", "Chikki"]

# Kept small on purpose. Each post is a billed item and so is each comment.
DEFAULT_POSTS = 20


def api_token() -> str:
    """The Apify token, from the environment or .streamlit/secrets.toml."""
    token = os.environ.get(SECRET_TOKEN, "").strip()
    if token:
        return token

    try:
        import streamlit as st

        token = str(st.secrets.get(SECRET_TOKEN, "")).strip()
    except Exception:
        token = ""

    if not token:
        sys.exit(
            f"No {SECRET_TOKEN} found. Add it to .streamlit/secrets.toml or "
            f"export {SECRET_TOKEN} before running."
        )
    return token


def fetch(keyword: str, posts: int, comments: bool, sort: str, time: str) -> list[dict]:
    """One search, returned as a list of raw records exactly as Apify gives them."""
    client = ApifyClient(api_token())
    run_input = {
        "search": keyword,
        "searchMode": "link",     # posts, rather than subreddits or users
        "sort": sort,
        "time": time,
        "includeComments": comments,
        "maxItems": posts,
    }

    print(f"\n{'=' * 78}\nSEARCHING REDDIT FOR: {keyword!r}")
    print(f"  actor {ACTOR}, input {json.dumps(run_input)}")

    run = client.actor(ACTOR).call(run_input=run_input)
    dataset_id = run.default_dataset_id
    status = run.status

    items = list(client.dataset(dataset_id).iterate_items())
    print(f"  run {run.id} finished as {status}, {len(items)} item(s) returned")
    usage = getattr(run, "usage_total_usd", None)
    if usage is not None:
        print(f"  cost: ${usage:.4f}")
    return items


def shape(items: list[dict]) -> None:
    """What fields came back, and how often. The point of the whole exercise."""
    fields: dict[str, int] = {}
    for item in items:
        for name in item:
            fields[name] = fields.get(name, 0) + 1

    print(f"\n  FIELDS PRESENT ({len(fields)} across {len(items)} items)")
    for name, count in sorted(fields.items(), key=lambda kv: (-kv[1], kv[0])):
        sample = next(
            (item[name] for item in items if item.get(name) not in (None, "", [], {})),
            None,
        )
        kind = type(sample).__name__ if sample is not None else "empty"
        preview = str(sample).replace("\n", " ")[:52]
        print(f"    {name:<22} {count:>3}/{len(items):<3} {kind:<8} {preview}")


def first(item: dict, *names: str, default=None):
    """The first of these keys the record actually carries."""
    for name in names:
        if item.get(name) not in (None, "", [], {}):
            return item[name]
    return default


def show_post(item: dict, index: int, comment_limit: int) -> None:
    """One post as a human would want to read it, plus its comments."""
    title = first(item, "title", "postTitle", default="(no title)")
    body = first(item, "body", "text", "selftext", "postText", default="")
    subreddit = first(item, "communityName", "subreddit", "sr", default="?")
    ups = first(item, "upVotes", "upvotes", "score", "ups", default=0)
    author = first(item, "username", "author", default="?")
    url = first(item, "url", "link", "postUrl", default="")
    number = first(item, "numberOfComments", "numComments", "commentCount", default="?")

    print(f"\n  {'-' * 74}")
    print(f"  POST {index}. {str(title)[:88]}")
    print(f"     {subreddit} | u/{author} | {ups} upvotes | {number} comments")
    if url:
        print(f"     {url}")
    if body:
        wrapped = textwrap.fill(
            str(body).strip().replace("\n", " "), width=72,
            initial_indent="     ", subsequent_indent="     ",
        )
        print(f"\n{wrapped[:600]}")

    comments = first(item, "comments", "postComments", default=[]) or []
    if not isinstance(comments, list):
        print(f"     comments field is {type(comments).__name__}, not a list")
        return

    print(f"\n     COMMENTS ({len(comments)} attached)")
    for comment in comments[:comment_limit]:
        if not isinstance(comment, dict):
            print(f"       {str(comment)[:70]}")
            continue
        text = first(comment, "body", "text", "comment", default="")
        cups = first(comment, "upVotes", "upvotes", "score", "ups", default=0)
        who = first(comment, "username", "author", default="?")
        line = textwrap.fill(
            str(text).strip().replace("\n", " "), width=66,
            initial_indent="       ", subsequent_indent="         ",
        )
        print(f"       u/{who} | {cups} upvotes")
        print(line[:400] or "       (empty)")
    if len(comments) > comment_limit:
        print(f"       ... and {len(comments) - comment_limit} more")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keywords", nargs="*", default=None,
                        help=f"defaults to {KEYWORDS}")
    parser.add_argument("--posts", type=int, default=DEFAULT_POSTS,
                        help=f"max items per keyword (default {DEFAULT_POSTS})")
    parser.add_argument("--no-comments", action="store_true",
                        help="skip comments, which are billed as items too")
    parser.add_argument("--sort", default="relevance",
                        choices=["relevance", "hot", "top", "new", "comments"])
    parser.add_argument("--time", default="all",
                        choices=["all", "year", "month", "week", "day", "hour"])
    parser.add_argument("--show", type=int, default=3,
                        help="how many posts to print in full (default 3)")
    parser.add_argument("--comments-shown", type=int, default=5,
                        help="comments printed per post (default 5)")
    parser.add_argument("--raw", action="store_true",
                        help="also dump the first record as raw JSON")
    args = parser.parse_args()

    keywords = args.keywords or KEYWORDS
    everything: dict[str, list[dict]] = {}

    for keyword in keywords:
        items = fetch(
            keyword,
            posts=args.posts,
            comments=not args.no_comments,
            sort=args.sort,
            time=args.time,
        )
        everything[keyword] = items
        if not items:
            print("  nothing came back for this keyword")
            continue

        shape(items)
        for index, item in enumerate(items[: args.show], start=1):
            show_post(item, index, args.comments_shown)

        if args.raw:
            print("\n  RAW FIRST RECORD")
            print(textwrap.indent(json.dumps(items[0], indent=2)[:4000], "    "))

    print(f"\n{'=' * 78}\nSUMMARY")
    for keyword, items in everything.items():
        comments = sum(
            len(first(item, "comments", "postComments", default=[]) or [])
            for item in items
            if isinstance(first(item, "comments", "postComments", default=[]), list)
        )
        print(f"  {keyword:<16} {len(items):>3} post(s), {comments:>4} comment(s)")


if __name__ == "__main__":
    main()
