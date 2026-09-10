"""Claude-assisted relevance for Reddit search.

Reddit's noise is not YouTube's. YouTube returns recipe videos that really do
mention the brand; Reddit returns unrelated real things that happen to share
the word. "Chikki" is a peanut brittle, a Bollywood surname and a D&D race, and
Reddit's search cannot tell them apart. A keyword filter cannot either, because
the keyword is present in all three.

So two Claude passes bracket the search:

- Before: turn a bare keyword into a few disambiguated queries, so the search
  itself is aimed at food rather than at the word.
- After: read each post and say whether it is actually about food. Posts that
  are not are dropped before anything is shown or saved.

Nothing here touches Streamlit's UI, only st.secrets for the API key. This is
Reddit-only: YouTube's own filters are unchanged and do not call Claude.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Sequence

import anthropic

_LOG = logging.getLogger(__name__)

# The label vocabularies live with the classifier that writes them. Imported
# lazily inside the module body to keep the import graph one way: classify
# imports relevance, never the other way round.
KINDS = ["question", "compliment", "complaint", "suggestion", "other"]
SENTIMENTS = ["positive", "neutral", "negative"]

MODEL = "claude-sonnet-4-6"

SECRET_API_KEY = "ANTHROPIC_API_KEY"

# How many variants a keyword is expanded into, the bare keyword included.
DEFAULT_VARIANTS = 3

# Posts per classification call. The same shape as the translation batching:
# big enough to amortise the prompt, small enough that one bad batch is cheap.
BATCH_SIZE = 20

# Per-post character cap. A post's subject is clear from its opening; the rest
# is paid-for tokens.
_MAX_POST_CHARS = 1200


class RelevanceError(Exception):
    """Raised for configuration or API problems the caller should surface."""


_EXPAND_SYSTEM = """You turn a product or brand keyword into Reddit search
queries for a food company's social listening.

The company sells Indian snacks and sweets. A bare keyword often collides with
unrelated things that share the word: "chikki" is a peanut brittle but also a
Bollywood surname and a fantasy race, "desi" means countless things, "candyman"
is a horror film.

Return short search queries that steer the search towards food, snacks, sweets
and eating, and away from the collisions. Each query should be two or three
words, the original keyword plus a disambiguating word a real person would
type. Do not invent brand names, do not add words that change what is being
searched for, and never return a query that omits the original keyword."""


_EXPAND_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string"},
        }
    },
    "required": ["queries"],
    "additionalProperties": False,
}


_CLASSIFY_SYSTEM = """You decide whether a Reddit post is about food.

You are filtering search results for a company that sells Indian snacks and
sweets. A post counts as food when it is about eating, cooking, buying,
reviewing or discussing food, snacks, sweets, drinks, restaurants, groceries or
food brands. That includes complaints about a snack, questions about where to
buy one, and recipe talk.

A post does not count when the keyword turned out to mean something else: a
person's name, a film, a game, a place, or any other subject where food is
incidental. When a post only mentions food in passing while being about
something else, it does not count.

Judge the post, not the keyword. Answer for every post you are given, using the
index it was given."""


_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "is_food": {"type": "boolean"},
                    "subject": {"type": "string"},
                },
                "required": ["index", "is_food", "subject"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["posts"],
    "additionalProperties": False,
}


def is_configured() -> bool:
    """True when an Anthropic API key is present in secrets."""
    try:
        import streamlit as st

        return bool(str(st.secrets.get(SECRET_API_KEY, "")).strip())
    except Exception:
        # st.secrets raises when there is no secrets.toml at all.
        return False


def _client() -> anthropic.Anthropic:
    try:
        import streamlit as st

        api_key = str(st.secrets.get(SECRET_API_KEY, "")).strip()
    except Exception:
        api_key = ""
    if not api_key:
        raise RelevanceError(
            f"No {SECRET_API_KEY} found in secrets, so Reddit results cannot "
            "be filtered for relevance."
        )
    return anthropic.Anthropic(api_key=api_key)


def ask_json(
    client, system: str, prompt: str, schema: dict, action: str = "claude"
) -> dict:
    """One Claude call returning JSON, with the errors turned into our own.

    Shared with classify.py and analysis.py so all three map failures the same
    way and every call is metered where it is made. `action` names the feature
    in the usage log.
    """
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.AuthenticationError as exc:
        raise RelevanceError(
            f"Anthropic rejected the API key. Check {SECRET_API_KEY} in secrets."
        ) from exc
    except anthropic.RateLimitError as exc:
        raise RelevanceError(
            "Anthropic rate limit hit. Wait a moment and search again."
        ) from exc
    except anthropic.APIStatusError as exc:
        raise RelevanceError(f"Anthropic API error ({exc.status_code}): {exc}") from exc
    except anthropic.APIConnectionError as exc:
        raise RelevanceError(f"Could not reach the Anthropic API: {exc}") from exc
    except Exception as exc:
        # Anything else at all. Relevance is an improvement to a search, never
        # a reason for one to fail, so every failure leaves by the same door.
        raise RelevanceError(
            f"The relevance check failed: {type(exc).__name__}: {exc}"
        ) from exc

    # Priced from the call's own token counts, which is the only honest
    # source: Anthropic does not expose account spend to a normal API key.
    try:
        import usage

        usage.record_claude(action, MODEL, getattr(response, "usage", None))
    except Exception as exc:
        _LOG.warning("Could not meter the Claude call: %s", exc)

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RelevanceError(f"Claude returned unreadable JSON: {exc}") from exc


def _clip(text: object) -> str:
    body = " ".join(str(text or "").split())
    return body[:_MAX_POST_CHARS]


def expand_keyword(
    keyword: str, variants: int = DEFAULT_VARIANTS, client=None
) -> list[str]:
    """A keyword as a few food-flavoured search queries, itself included first.

    The bare keyword always leads: the disambiguated variants are there to add
    relevant results, not to hide what the reader asked for. Falls back to just
    the keyword if Claude cannot be reached, so a search never fails over this.
    """
    keyword = str(keyword or "").strip()
    if not keyword or variants <= 1:
        return [keyword] if keyword else []

    prompt = (
        f"Keyword: {keyword}\n\n"
        f"Give {variants - 1} search queries that aim this keyword at food."
    )
    try:
        payload = ask_json(client or _client(), _EXPAND_SYSTEM, prompt,
                           _EXPAND_SCHEMA, action="keyword expansion")
    except Exception as exc:
        # Including whatever building the client threw. Expansion improves a
        # search; it never stops one.
        _LOG.warning("Keyword expansion failed (%s); searching the keyword alone", exc)
        return [keyword]

    queries = [keyword]
    for raw in payload.get("queries", []):
        query = " ".join(str(raw or "").split())
        # A variant that dropped the keyword is searching for something else.
        if not query or keyword.lower() not in query.lower():
            continue
        if query.lower() not in {q.lower() for q in queries}:
            queries.append(query)
    return queries[:variants]


def classify_posts(posts: Sequence[dict], client=None) -> dict[str, dict]:
    """Which posts are actually about food, keyed by post id.

    Each value is {"is_food": bool, "subject": str}. Batched, never one call
    per post. A post Claude does not answer for is kept, because dropping a
    real result is worse than keeping a doubtful one.
    """
    wanted = [p for p in posts if str(p.get("id", "") or "").strip()]
    if not wanted:
        return {}

    client = client or _client()
    verdicts: dict[str, dict] = {}

    for start in range(0, len(wanted), BATCH_SIZE):
        batch = wanted[start : start + BATCH_SIZE]
        listing = "\n\n".join(
            f"<post index=\"{i}\">\n"
            f"subreddit: {str(post.get('subreddit', '') or '')}\n"
            f"title: {_clip(post.get('title'))}\n"
            f"body: {_clip(post.get('text') or post.get('body'))}\n"
            f"</post>"
            for i, post in enumerate(batch)
        )
        prompt = (
            f"Decide for each of these {len(batch)} Reddit posts whether it is "
            "about food. Give a two or three word subject for each, so the "
            "decision can be checked.\n\n" + listing
        )
        payload = ask_json(client, _CLASSIFY_SYSTEM, prompt, _CLASSIFY_SCHEMA,
                           action="post relevance")

        for entry in payload.get("posts", []):
            try:
                position = int(entry.get("index"))
            except (TypeError, ValueError):
                continue
            if not 0 <= position < len(batch):
                continue
            post_id = str(batch[position].get("id"))
            verdicts[post_id] = {
                "is_food": bool(entry.get("is_food")),
                "subject": str(entry.get("subject", "") or "").strip(),
            }

    # Anything the model skipped stays in. Silence is not a rejection.
    for post in wanted:
        verdicts.setdefault(
            str(post.get("id")), {"is_food": True, "subject": "not judged"}
        )
    return verdicts


# --------------------------------------------------------------------------
# Query understanding
# --------------------------------------------------------------------------
# People type what they want to know, not what a search engine accepts:
# "khakra negative feedback" searches YouTube for those three words together
# and finds nothing. This reads the request instead, and says plainly which
# parts a search can answer and which need classification to have run.
_QUERY_SYSTEM = """You read what someone typed into a social listening tool and
work out what they are asking for.

The tool searches YouTube and Reddit for a keyword, collects the comments, and
can separately label each comment with a kind (question, compliment, complaint,
suggestion, other) and a sentiment (positive, neutral, negative).

Split the request into:

- keyword: what to actually search for. Product or brand names only, no
  question words, no sentiment words, no words like feedback, reviews,
  opinions or complaints. "khakra negative feedback" searches for "khakra".
  Keep it as short as a person would type into YouTube.
- sentiment: positive, neutral or negative, but only when the request clearly
  asks for that. Empty otherwise.
- kind: question, compliment, complaint, suggestion or other, but only when
  the request clearly asks for that. "complaints about packaging" is a
  complaint. Empty otherwise.
- unfulfilled: a short plain sentence naming anything asked for that neither a
  keyword search nor those two labels can deliver, for example a date range, a
  named person, a specific subreddit, or a count. Empty when there is nothing.

Never invent a sentiment or a kind that was not asked for. A plain product name
has neither."""


_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "keyword": {"type": "string"},
        "sentiment": {"type": "string"},
        "kind": {"type": "string"},
        "unfulfilled": {"type": "string"},
    },
    "required": ["keyword", "sentiment", "kind", "unfulfilled"],
    "additionalProperties": False,
}


# Words that make a request more than a name. Cheap gate: a plain product name
# never needs Claude, and most searches are plain product names.
_INTENT_WORDS = frozenset("""
feedback review reviews opinion opinions complaint complaints complaining
negative positive neutral bad good worst best sentiment question questions
asking suggestion suggestions idea ideas praise problem problems issues
about regarding people saying says think thoughts
""".split())


def looks_like_a_request(raw: str) -> bool:
    """True when the text reads as a request rather than a plain name.

    Keeps the common case free: "Khakra" and "GO DESi, imli pop" never reach
    Claude, so query understanding costs nothing until it is needed.
    """
    words = [w for w in re.findall(r"[a-z]+", str(raw or "").lower()) if w]
    if len(words) < 2:
        return False
    return bool(set(words) & _INTENT_WORDS)


def understand(raw: str, client=None) -> dict:
    """A typed request as {keyword, sentiment, kind, unfulfilled, parsed}.

    Falls back to the text as typed if Claude cannot be reached, so a search
    never fails over this. `parsed` says whether anything was changed.
    """
    text = " ".join(str(raw or "").split())
    plain = {"keyword": text, "sentiment": "", "kind": "", "unfulfilled": "",
             "parsed": False}
    if not text:
        return plain

    try:
        payload = ask_json(
            client or _client(), _QUERY_SYSTEM,
            f"Request: {text}", _QUERY_SCHEMA, action="query understanding",
        )
    except Exception as exc:
        # Including whatever building the client threw. A request that cannot
        # be read is searched as typed rather than refused.
        _LOG.warning("Query understanding failed (%s); searching as typed", exc)
        return plain

    keyword = " ".join(str(payload.get("keyword", "") or "").split()) or text
    sentiment = str(payload.get("sentiment", "") or "").strip().lower()
    kind = str(payload.get("kind", "") or "").strip().lower()
    return {
        "keyword": keyword,
        # Only values the labels actually use; anything else is dropped.
        "sentiment": sentiment if sentiment in SENTIMENTS else "",
        "kind": kind if kind in KINDS else "",
        "unfulfilled": " ".join(str(payload.get("unfulfilled", "") or "").split()),
        "parsed": True,
    }
