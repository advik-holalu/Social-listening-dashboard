"""What the app has spent, and where the numbers come from.

Three services, three different honest answers:

- **Apify** publishes account spend, so it is pulled live and shown as fact.
- **Anthropic** does not. Its usage and cost reports need an Admin API key,
  which a normal key is refused for (401, "The Admin API requires an Admin API
  key"), so the only truthful number is one the app keeps itself. Every call
  returns its own token counts, so each is priced exactly at the moment it is
  made and the total is a sum of real calls, not an estimate.
- **YouTube** does not report quota through the Data API either, and the Cloud
  Monitoring metric that would is refused to this service account (403). So
  units are counted from the calls the app actually makes, against the
  published cost of each one.

Rows go in a `usage` tab so the totals survive a restart. A failure to write
one is logged and dropped: nobody should lose a search because the meter
could not be updated.
"""

from __future__ import annotations

import datetime as _dt
import logging

_LOG = logging.getLogger(__name__)

USAGE_WORKSHEET = "usage"
USAGE_COLUMNS: list[str] = [
    "at", "service", "action", "units", "cost_usd", "detail",
]

YOUTUBE = "youtube"
CLAUDE = "claude"
TRANSLATE = "translate"

# Google Cloud Translation bills per character of text sent, with the first
# 500,000 characters of each calendar month free. Its usage is not readable
# with the credentials this app has: Cloud Monitoring and Service Usage both
# refuse this service account (403), and the Cloud Billing API is not enabled
# on the project. So characters are counted here as they are sent.
TRANSLATE_FREE_CHARS = 500_000

# What a comment costs to translate, for turning characters into something a
# person can picture. A range, because comments vary.
COMMENT_CHARS = (50, 100)

# Dollars per million tokens, from the model's published pricing. A model that
# is not listed is priced at 0 rather than guessed at, and says so.
PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}

# Cached tokens are billed at a fraction of the input rate: a read is a tenth,
# writing to the cache is a quarter more than reading fresh.
CACHE_READ_RATE = 0.1
CACHE_WRITE_RATE = 1.25

# YouTube's published costs, the same numbers the fetcher spends.
YOUTUBE_UNIT_COSTS = {"search": 100, "videos": 1, "comments": 1}
YOUTUBE_DAILY_LIMIT = 10_000


def claude_cost(model: str, usage) -> float:
    """What one Claude call cost, from the token counts it returned."""
    rates = PRICING.get(str(model or "").strip())
    if not rates or usage is None:
        return 0.0

    def count(name: str) -> int:
        value = getattr(usage, name, None)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    per_token_in = rates["input"] / 1_000_000
    per_token_out = rates["output"] / 1_000_000
    return (
        count("input_tokens") * per_token_in
        + count("output_tokens") * per_token_out
        + count("cache_read_input_tokens") * per_token_in * CACHE_READ_RATE
        + count("cache_creation_input_tokens") * per_token_in * CACHE_WRITE_RATE
    )


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def record(service: str, action: str, units: float, cost: float, detail: str = "") -> None:
    """Log one metered event. Never raises: the meter is not the point."""
    try:
        import sheets_store

        if not sheets_store.is_configured():
            return
        sheets_store.record_usage(
            [_now(), service, action, round(float(units), 4),
             round(float(cost), 6), str(detail)[:200]]
        )
    except Exception as exc:
        _LOG.warning("Could not record usage (%s %s): %s", service, action, exc)


def record_claude(action: str, model: str, usage, detail: str = "") -> float:
    """Price one Claude call from its own token counts and log it."""
    cost = claude_cost(model, usage)
    tokens = 0
    for name in ("input_tokens", "output_tokens"):
        try:
            tokens += int(getattr(usage, name, 0) or 0)
        except (TypeError, ValueError):
            pass
    record(CLAUDE, action, tokens, cost, detail or model)
    return cost


def record_translation(characters: int, comments: int = 0) -> None:
    """Log the characters one translation call sent, as Google bills them."""
    if characters:
        record(
            TRANSLATE, "translation", characters, 0.0,
            f"{comments} comment(s)" if comments else "",
        )


def record_youtube(units: int, action: str = "search", detail: str = "") -> None:
    """Log the quota units a run of the fetcher actually spent."""
    if units:
        record(YOUTUBE, action, units, 0.0, detail)


def summarise(rows: list[list], since: str = "", service: str = "") -> dict:
    """Totals over logged rows: {events, units, cost, by_action}."""
    events = 0
    units = 0.0
    cost = 0.0
    by_action: dict[str, dict] = {}

    for row in rows:
        if len(row) < 5:
            continue
        at, kind, action = str(row[0]), str(row[1]), str(row[2])
        if service and kind != service:
            continue
        if since and at < since:
            continue
        try:
            row_units = float(row[3] or 0)
            row_cost = float(row[4] or 0)
        except (TypeError, ValueError):
            continue
        events += 1
        units += row_units
        cost += row_cost
        bucket = by_action.setdefault(action, {"events": 0, "units": 0.0, "cost": 0.0})
        bucket["events"] += 1
        bucket["units"] += row_units
        bucket["cost"] += row_cost

    return {"events": events, "units": units, "cost": cost, "by_action": by_action}


def today() -> str:
    """The UTC date, as the log writes it."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def month_start() -> str:
    """The first of the current UTC month, which is when the free tier resets."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-01")
