"""GO DESi -- Social Listening (YouTube).

Search YouTube by keyword, pull comments, persist them to a Google Sheet, and
browse the whole history in a filterable table.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
from typing import Sequence

import pandas as pd
import streamlit as st

import insights
import sheets_store
import youtube_fetcher
from insights import InsightsError
from sheets_store import SheetsError
from youtube_fetcher import (
    InvalidAPIKeyError,
    QuotaExceededError,
    YouTubeError,
    parse_keywords,
)

# Failures during a run are also sent here, so the full traceback lands in the
# server log even though the UI only shows a sentence.
_LOG = logging.getLogger(__name__)

st.set_page_config(
    page_title="GO DESi - Social Listening",
    layout="wide",
)

# Fixed collection limits. These used to be sidebar sliders; they are pinned so
# every run costs a predictable amount of quota.
# Default and bounds for the videos-per-keyword control. The ceiling keeps a
# single run inside a sane slice of the daily quota.
DEFAULT_VIDEOS_PER_KEYWORD = 20
MIN_VIDEOS_PER_KEYWORD = 5
MAX_VIDEOS_PER_KEYWORD = 50
MAX_COMMENTS_PER_VIDEO = 500

# Worst-case quota per keyword, derived from the fetcher's page sizes so the two
# cannot drift: one search page (100 units) + one commentThreads page per 100
# comments per video (1 unit each) + one batched videos.list call (1 unit).
_COMMENT_PAGES_PER_VIDEO = -(-MAX_COMMENTS_PER_VIDEO // youtube_fetcher.MAX_COMMENT_PAGE)


def search_cost(queries: int) -> int:
    """Quota for the search step: one page per query plus one batched stats.

    "All" mode is always one query however many terms it joins, so its search
    cost does not grow with the keyword list.
    """
    return int(queries) * 100 + 1


def comment_cost(videos: int) -> int:
    """Quota to read comments for this many videos, at the 500 comment cap."""
    return int(videos) * _COMMENT_PAGES_PER_VIDEO


def quota_per_keyword(videos: int) -> int:
    """Worst case for one keyword end to end, if every video is kept."""
    return search_cost(1) + comment_cost(videos)

# How the video sections are ordered on screen. Four of these are also YouTube
# search orders; "Highest comments" is not -- the Data API has no comment-count
# ordering -- so it searches by relevance and sorts the results here instead.
ORDER_OPTIONS: dict[str, str | None] = {
    "Most relevant": "relevance",
    "Newest first": "date",
    "Most viewed": "viewCount",
    "Highest rated": "rating",
    "Highest comments": None,
}

# The column each ordering sorts on locally, all descending. "Most relevant"
# has no column: it keeps the order the videos were collected in.
VIDEO_SORT_COLUMNS = {
    "Newest first": "video_published_at",
    "Most viewed": "video_views",
    "Highest rated": "video_likes",
    "Highest comments": "video_comment_count",
}


# --------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------
def _init_state() -> None:
    st.session_state.setdefault("data", sheets_store.to_dataframe([]))
    st.session_state.setdefault("last_refreshed", None)
    st.session_state.setdefault("loaded_from_sheet", False)
    st.session_state.setdefault("last_report", None)
    st.session_state.setdefault("sheet_status", "")
    st.session_state.setdefault("insight_status", "")
    st.session_state.setdefault("last_run", None)
    st.session_state.setdefault("text_display", SHOW_BOTH)
    st.session_state.setdefault("translation_status", "")
    # Keywords searched in this session. The live report shows these and only
    # these; everything else in the Sheet is reachable as a CSV download.
    st.session_state.setdefault("session_keywords", [])
    # Stage one's candidates, the ids still ticked, and the settings they were
    # found with. Empty between searches.
    st.session_state.setdefault("preview", [])
    st.session_state.setdefault("preview_keep", set())
    st.session_state.setdefault("preview_config", {})
    st.session_state.setdefault("fetch_error", "")


def _stamp_refresh() -> None:
    st.session_state["last_refreshed"] = _dt.datetime.now()


# How long a Sheets read is reused before going back to the network. Long
# enough that reopening the app or refreshing the tab is instant, short enough
# that a search run in another tab shows up quickly.
HISTORY_TTL_SECONDS = 60


@st.cache_data(ttl=HISTORY_TTL_SECONDS, show_spinner=False)
def _fetch_history() -> pd.DataFrame:
    """The two Sheets reads, cached so a refresh inside the TTL costs nothing.

    Kept separate from _load_history so the caching sits on the network call
    alone, with no Streamlit state mixed in.
    """
    return sheets_store.load_all()


def _load_history() -> None:
    """Pull stored rows out of the Sheet once per session, so a browser refresh
    doesn't lose past searches.

    Called after the page has already drawn, so the fetch fills in the results
    area rather than holding up the title, the sidebar, and the search box.
    """
    if st.session_state["loaded_from_sheet"]:
        return
    if not sheets_store.is_configured():
        st.session_state["loaded_from_sheet"] = True
        return

    try:
        with st.spinner("Loading saved comments..."):
            stored = _fetch_history()
        st.session_state["data"] = stored
        st.session_state["sheet_status"] = f"Loaded {len(stored):,} saved comments."
        _stamp_refresh()
    except SheetsError as exc:
        st.session_state["sheet_status"] = f"Sheets unavailable: {exc}"
    finally:
        st.session_state["loaded_from_sheet"] = True


def _merge(new_rows: list[dict]) -> int:
    """Merge freshly fetched rows into session data, de-duped on comment_id.

    Returns the number of genuinely new rows.
    """
    if not new_rows:
        return 0
    incoming = sheets_store.to_dataframe(new_rows)
    current = st.session_state["data"]
    combined = pd.concat([current, incoming], ignore_index=True)
    combined = combined.drop_duplicates(subset=[youtube_fetcher.ID_COLUMN], keep="first")
    added = len(combined) - len(current)
    st.session_state["data"] = combined.reset_index(drop=True)
    return added


# --------------------------------------------------------------------------
# Sidebar -- keyword, run button, and nothing else prominent
# --------------------------------------------------------------------------
def _sidebar() -> dict:
    """The whole sidebar: what to search for, how to collect it, and go."""
    # "Find videos" would collide with the "Which videos" sub-heading below;
    # this names the whole panel and pairs with the Run Search button ending it.
    st.sidebar.header("Start a search")

    keywords_raw = st.sidebar.text_input(
        "What do you want to search on YouTube?",
        value="GO DESi",
        key="keywords_raw",
        help="Separate several with commas, e.g. GO DESi, imli pop, chikki",
    )
    keywords = parse_keywords(keywords_raw)

    # A radio rather than a segmented control: these labels are sentences, and
    # a stretched segmented control in a narrow sidebar wraps them into ragged
    # pills. A radio stacks them cleanly, one under the other.
    options = list(MATCH_LABELS)
    default = (
        {} if "match_mode" in st.session_state
        else {"index": options.index(MATCH_ALL_LABEL)}
    )
    label = st.sidebar.radio(
        "Match",
        options,
        key="match_mode",
        help=MATCH_HELP,
        **default,
    )
    match = MATCH_LABELS.get(label, youtube_fetcher.MATCH_ALL)

    if len(keywords) > 1:
        if match == youtube_fetcher.MATCH_ALL:
            st.sidebar.caption(
                f"Searching one query: {' '.join(keywords)}"
            )
        else:
            st.sidebar.caption(
                f"Searching {len(keywords)} separately: {', '.join(keywords)}"
            )

    options = _search_options()

    run = st.sidebar.button("Run Search", type="primary", use_container_width=True)

    # The Sheet has not been read yet at this point in the run -- the page
    # draws before the fetch on purpose -- so the past-searches list gets a
    # reserved slot here and is filled once the history arrives.
    past_slot = st.sidebar.container()

    _sidebar_details(keywords, options["videos_per_keyword"], match)

    return {
        "keywords": keywords, "run": run, "past_slot": past_slot,
        "match": match, **options,
    }


def _sidebar_details(
    keywords: list[str], videos_per_keyword: int, match: str
) -> None:
    """Housekeeping that matters when something looks wrong, and not before.

    Everything here is a caption rather than a warning or metric: small, muted,
    and at the very bottom, so the sidebar reads as one input and one button.
    """
    st.sidebar.divider()
    with st.sidebar.expander("Details", expanded=False):
        stamp = st.session_state["last_refreshed"]
        st.caption(
            "Last refreshed: "
            + (stamp.strftime("%d %b %Y, %H:%M:%S") if stamp else "never")
        )

        if st.session_state["sheet_status"]:
            st.caption(st.session_state["sheet_status"])

        queries = len(youtube_fetcher.build_queries(keywords, match))
        st.caption(
            f"Up to {videos_per_keyword} videos in total for this search, "
            f"shared across the terms, and {MAX_COMMENTS_PER_VIDEO} comments "
            "per video."
        )

        # Two stages, so two numbers: the search runs on Run Search, the
        # comments only for videos still ticked afterwards. "All" is one
        # query, so its search cost is flat.
        upfront = search_cost(queries)
        rest = comment_cost(videos_per_keyword)
        st.caption(
            f"Estimated API cost: ~{upfront:,} units to search "
            f"({queries} quer{'y' if queries == 1 else 'ies'}), then up to "
            f"~{rest:,} more to fetch comments for every video found. Of "
            "10,000 daily units; unticking videos lowers the second."
        )

        if sheets_store.is_configured():
            url = sheets_store.sheet_url()
            if url:
                st.caption(f"[Open the Google Sheet]({url})")
        else:
            st.caption(
                "Google Sheets is not connected, so results live only in this "
                "session. See `.streamlit/secrets.toml.example`."
            )


# --------------------------------------------------------------------------
# Search options -- plain labelled inputs, formerly the "Advanced" expander
# --------------------------------------------------------------------------
def _search_options() -> dict:
    """The collection settings, stacked under the search box in the sidebar.

    The sidebar is a single narrow column, so these run vertically rather than
    in the two-column panel they used when they sat in the main area. Widget
    keys carry their own state -- the sidebar renders on every run, so nothing
    needs shadowing.
    """
    st.sidebar.markdown("**Which videos**")

    labels = list(ORDER_OPTIONS)
    order_label = st.sidebar.selectbox(
        "Sort videos by",
        labels,
        index=0,
        key="opt_order",
        help=(
            "Orders the video sections below. The first four also tell YouTube "
            "how to pick videos when you search; YouTube cannot search by "
            "comment count, so that one searches by relevance and sorts here."
        ),
    )
    videos_per_keyword = st.sidebar.number_input(
        "Videos to query",
        min_value=MIN_VIDEOS_PER_KEYWORD,
        max_value=MAX_VIDEOS_PER_KEYWORD,
        value=DEFAULT_VIDEOS_PER_KEYWORD,
        step=5,
        key="opt_videos",
        help=(
            "How many videos each keyword pulls comments from. More videos "
            "means more quota: the estimate is in Details below."
        ),
    )
    days_back = st.sidebar.number_input(
        "From the last N days",
        min_value=0,
        max_value=3650,
        value=365,
        step=30,
        key="opt_days_back",
        help="0 means no date limit.",
    )

    st.sidebar.markdown("**What to collect**")
    with st.sidebar.container(border=True, gap="xxsmall"):
        include_replies = st.checkbox(
            "Include replies", value=True, key="opt_replies"
        )

    published_after = None
    if days_back:
        published_after = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
            days=int(days_back)
        )

    return {
        # YouTube has no comment-count ordering, so that choice searches by
        # relevance and does its sorting once the comments are home.
        "order": ORDER_OPTIONS[order_label] or "relevance",
        "order_label": order_label,
        "videos_per_keyword": int(videos_per_keyword),
        "published_after": published_after,
        "include_replies": include_replies,
    }


# --------------------------------------------------------------------------
# The search run
# --------------------------------------------------------------------------
def _run_search(config: dict) -> None:
    api_key = str(st.secrets.get("YOUTUBE_API_KEY", "")).strip()
    if not api_key:
        st.error(
            "No `YOUTUBE_API_KEY` found in secrets. Copy "
            "`.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` "
            "and add your key."
        )
        return

    if not config["keywords"]:
        st.warning("Enter at least one keyword to search.")
        return

    progress = st.progress(0.0)
    status = st.empty()

    def on_progress(current: int, total: int, label: str) -> None:
        progress.progress(min(1.0, current / max(total, 1)))
        status.markdown(f"{label}")

    try:
        videos, report = youtube_fetcher.preview_videos(
            api_key=api_key,
            keywords=config["keywords"],
            max_videos=config["videos_per_keyword"],
            order=config["order"],
            published_after=config["published_after"],
            match=config["match"],
            progress_cb=on_progress,
        )
    except InvalidAPIKeyError as exc:
        progress.empty()
        status.empty()
        st.error(str(exc))
        return
    except QuotaExceededError as exc:
        progress.empty()
        status.empty()
        st.error(str(exc))
        return
    except YouTubeError as exc:
        progress.empty()
        status.empty()
        st.error(f"Search failed: {exc}")
        return

    progress.empty()
    status.empty()

    # Stage one only. Nothing is fetched in bulk and nothing is written until
    # the reader confirms the list below: an auto-saving search would otherwise
    # commit a bad keyword match to the Sheet with no chance to catch it.
    st.session_state["last_report"] = report
    st.session_state["preview"] = videos
    st.session_state["preview_config"] = {
        # What the rows will be filed under: the queries actually sent, which
        # in All mode is the single joined string.
        "keywords": youtube_fetcher.build_queries(
            config["keywords"], config["match"]
        ),
        "include_replies": config["include_replies"],
    }
    st.session_state["preview_keep"] = {v["video_id"] for v in videos}

    if report.quota_exhausted:
        st.warning(
            "The daily YouTube quota ran out mid-search. The list below is "
            "partial - quota resets at midnight Pacific Time.",
        )
    if not videos:
        st.info("No videos matched. Try a different keyword.")

    if report.warnings:
        with st.expander(f"{len(report.warnings)} notice(s) from this search"):
            for warning in report.warnings:
                st.write(f"- {warning}")


def _preview_frame(videos: list[dict]) -> pd.DataFrame:
    """The candidate list, as the editor wants it."""
    keep = st.session_state["preview_keep"]
    return pd.DataFrame([
        {
            "Fetch": video["video_id"] in keep,
            "video_id": video["video_id"],
            "Video": video.get("video_title", ""),
            "Channel": video.get("channel_title", ""),
            "Views": int(video.get("video_views", 0) or 0),
            "Comments": int(video.get("video_comment_count", 0) or 0),
            "Matched": ", ".join(video.get("keywords", [])),
        }
        for video in videos
    ])


def _preview_section() -> None:
    """Stage one's result: choose which videos are worth reading.

    A generic keyword pulls unrelated videos, and saving happens
    automatically, so this is the one place to drop a bad match before its
    comments reach the Sheet.
    """
    videos = st.session_state["preview"]
    if not videos:
        return

    st.subheader("Videos found")
    st.caption(
        f"{len(videos):,} video(s) from the search. Untick anything irrelevant, "
        "then fetch the comments for the rest. Nothing has been saved yet."
    )

    edited = st.data_editor(
        _preview_frame(videos),
        width="stretch",
        hide_index=True,
        height=min(520, 90 + 36 * max(len(videos), 1)),
        key="preview_editor",
        column_config={
            "Fetch": st.column_config.CheckboxColumn("Fetch", width="small"),
            "video_id": None,
            "Video": st.column_config.TextColumn("Video", width="large"),
            "Channel": st.column_config.TextColumn("Channel", width="medium"),
            "Views": st.column_config.NumberColumn("Views", format="%d"),
            "Comments": st.column_config.NumberColumn("Comments", format="%d"),
            "Matched": st.column_config.TextColumn("Matched keyword", width="small"),
        },
        disabled=["video_id", "Video", "Channel", "Views", "Comments", "Matched"],
    )

    chosen = {
        str(row.video_id) for row in edited.itertuples() if bool(row.Fetch)
    }
    st.session_state["preview_keep"] = chosen

    spent = search_cost(len(st.session_state["preview_config"]["keywords"]))
    st.caption(
        f"{len(chosen):,} of {len(videos):,} video(s) selected. Fetching their "
        f"comments costs up to ~{comment_cost(len(chosen)):,} more quota "
        f"units; the search itself has already cost ~{spent:,}."
    )

    if st.button(
        "Fetch comments and save",
        type="primary",
        disabled=not chosen,
        key="fetch_comments",
    ):
        _fetch_and_save([v for v in videos if v["video_id"] in chosen])
        st.rerun()


def _fetch_and_save(videos: list[dict]) -> None:
    """Stage two: pull comments for the chosen videos, then store them."""
    api_key = str(st.secrets.get("YOUTUBE_API_KEY", "")).strip()
    config = st.session_state["preview_config"]

    progress = st.progress(0.0)
    status = st.empty()

    def on_progress(current: int, total: int, label: str) -> None:
        progress.progress(min(1.0, current / max(total, 1)))
        status.markdown(label)

    try:
        rows, report = youtube_fetcher.fetch_comments_for(
            api_key=api_key,
            videos=videos,
            max_comments=MAX_COMMENTS_PER_VIDEO,
            include_replies=config["include_replies"],
            progress_cb=on_progress,
        )
    except (InvalidAPIKeyError, QuotaExceededError, YouTubeError) as exc:
        st.session_state["fetch_error"] = str(exc)
        return
    finally:
        progress.empty()
        status.empty()

    added = _merge(rows)
    st.session_state["last_report"] = report
    _stamp_refresh()

    written = 0
    if rows and sheets_store.is_configured():
        try:
            with st.spinner("Saving to Google Sheets..."):
                written, already = sheets_store.append_rows(rows)
            st.session_state["sheet_status"] = (
                f"Saved {written:,} new comment(s) to Sheets "
                f"({already:,} were already there)."
            )
            _fetch_history.clear()
        except SheetsError as exc:
            _LOG.exception("Saving the search failed")
            st.session_state["sheet_status"] = f"Could not save to Sheets: {exc}"
            st.session_state["fetch_error"] = (
                f"The comments were fetched but could not be saved: {exc}"
            )

    for keyword in config["keywords"]:
        if keyword not in st.session_state["session_keywords"]:
            st.session_state["session_keywords"].append(keyword)

    st.session_state["last_run"] = {
        "comments": report.comments_fetched,
        "videos": report.videos_with_comments or report.videos_searched,
        "added": added,
        "written": written,
        "keywords": list(config["keywords"]),
        "ids": {str(row.get("comment_id", "")) for row in rows},
    }

    # The candidates have served their purpose.
    st.session_state["preview"] = []
    st.session_state["preview_keep"] = set()


def _run_summary() -> None:
    """The one-line "here is what just happened" block in the Search tab."""
    last = st.session_state["last_run"]
    if not last:
        return

    st.success(
        f"{last['comments']:,} comments collected from {last['videos']:,} videos"
    )

    one, two, three = st.columns(3)
    one.metric("Comments collected", f"{last['comments']:,}")
    two.metric("New to your sheet", f"{last.get('written', 0):,}")
    three.metric("Videos", f"{last['videos']:,}")


# --------------------------------------------------------------------------
# Raw Data -- the "By Video" view
# --------------------------------------------------------------------------
# Rendering comments as real widgets costs far more than a dataframe does, so
# the grouped view draws a page at a time rather than every row at once.
MATCH_ANY_LABEL = "Match any of these"
MATCH_ALL_LABEL = "Match all together"
MATCH_LABELS = {
    MATCH_ANY_LABEL: youtube_fetcher.MATCH_ANY,
    MATCH_ALL_LABEL: youtube_fetcher.MATCH_ALL,
}

# Both explanations live in the one tooltip: a segmented control carries a
# single help string, and this keeps the sidebar itself uncluttered.
MATCH_HELP = (
    f"**{MATCH_ANY_LABEL}**: finds videos matching any single keyword - good "
    "when you are not sure what people call it (e.g. brand name + nicknames + "
    "misspellings).\n\n"
    f"**{MATCH_ALL_LABEL}**: combines your keywords into one specific search - "
    "good when you know exactly what you are looking for and want fewer, more "
    "precise results."
)

BY_VIDEO_VIEW = "Report by video"
ALL_COMMENTS_VIEW = "Report by all comments"

ALL_TYPES = "All"
SHORTS_ONLY = "Shorts only"
VIDEOS_ONLY = "Videos only"

FLAT_SORT_OPTIONS = {
    "Newest first": "comment_published_at",
    "Most liked": "comment_likes",
}

# The table's columns, in reading order. Any that turn out to be empty are
# dropped at render time; everything stays in the CSV either way.
RAW_TABLE_COLUMNS = [
    "keyword",
    "comment_text",
    "comment_translation",
    "comment_language",
    "comment_author",
    "comment_likes",
    "comment_published_at",
    "video_title",
    "channel_title",
    "video_views",
    "video_comment_count",
    "video_type",
    "video_published_at",
    "video_url",
]

RAW_TABLE_CONFIG = {
    "keyword": ("Keyword", "small"),
    "comment_text": ("Comment", "large"),
    "comment_translation": ("Translation", "large"),
    "comment_language": ("Lang", "small"),
    "comment_author": ("Author", "small"),
    "video_title": ("Video", "medium"),
    "channel_title": ("Channel", "small"),
    "video_type": ("Type", "small"),
}

VIDEOS_PER_PAGE = 10

# The stepper's increment. Keeps the control on round numbers.
VIDEO_PAGE_STEP = 10

# Comments per Claude call when a Translate button is pressed.
TRANSLATE_BATCH = 25


def _translate_ids(ids: Sequence[str]) -> int:
    """Translate these comment_ids, in batches, and persist what lands.

    The single entry point for both the per-video button and a row selection,
    so whichever runs first, the other finds nothing left to do.
    """
    done = 0
    try:
        for start in range(0, len(ids), TRANSLATE_BATCH):
            batch = list(ids[start : start + TRANSLATE_BATCH])
            updated, count = insights.translate_rows(st.session_state["data"], batch)
            st.session_state["data"] = updated
            done += count

            if sheets_store.is_configured():
                touched = updated[updated["comment_id"].astype(str).isin(batch)]
                sheets_store.update_analysis(touched)
    except InsightsError as exc:
        _LOG.exception("Translation failed")
        st.session_state["translation_status"] = f"Translation failed: {exc}"
        return done
    except SheetsError as exc:
        _LOG.exception("Saving translations failed")
        st.session_state["translation_status"] = (
            f"Translated {done:,}, but saving to Sheets failed: {exc}"
        )
        return done
    except Exception as exc:
        _LOG.exception("Translation failed unexpectedly")
        st.session_state["translation_status"] = (
            f"Translation failed: {type(exc).__name__}: {exc}"
        )
        return done

    st.session_state["translation_status"] = f"Translated {done:,} comment(s)."
    return done


def _translate_video(rows: pd.DataFrame) -> None:
    """Translate every untranslated non-English comment on one video."""
    ids = [
        str(record["comment_id"])
        for _, record in rows.iterrows()
        if insights.needs_translation(record)
    ]
    if ids:
        _translate_ids(ids)


def _translate_button(rows: pd.DataFrame, video_id: str) -> None:
    """A real button, shown only when this video has something to translate."""
    if not insights.is_configured():
        return

    pending = sum(1 for _, record in rows.iterrows() if insights.needs_translation(record))
    if not pending:
        return

    if st.button(
        f"Translate {pending:,} comment(s)",
        key=f"translate_{video_id}",
        help="Non-English comments on this video. Translated once, then stored.",
    ):
        with st.spinner(f"Translating {pending:,} comment(s)..."):
            _translate_video(rows)
        st.rerun()


SHOW_BOTH = "Both"
SHOW_ORIGINAL = "Original"
SHOW_TRANSLATION = "Translation"


def _text_display(df: pd.DataFrame) -> None:
    """Swap the comment column between the original and its translation.

    A dataframe cannot carry a per-row toggle, so the table's equivalent is one
    control over the column itself. It only appears once something has actually
    been translated, and defaults to showing both side by side.
    """
    if insights.TRANSLATION_COLUMN not in df.columns:
        return
    if df[insights.TRANSLATION_COLUMN].astype(str).str.strip().eq("").all():
        return

    st.radio(
        "Comment text",
        [SHOW_BOTH, SHOW_ORIGINAL, SHOW_TRANSLATION],
        horizontal=True,
        key="text_display",
        help="Translated comments can show the original, the English, or both.",
    )


def _visible_columns(df: pd.DataFrame) -> list[str]:
    """The table's columns, minus any that hold nothing.

    Sentiment, category and flagged ask are only filled by the tagging pass,
    and the translation columns only once something has been translated. An
    empty column is just a stripe of dead space, so it does not get drawn.
    """
    choice = st.session_state.get("text_display", SHOW_BOTH)
    hidden = set()
    if choice == SHOW_ORIGINAL:
        hidden.add(insights.TRANSLATION_COLUMN)
    elif choice == SHOW_TRANSLATION:
        hidden.add("comment_text")

    visible = []
    for column in RAW_TABLE_COLUMNS:
        if column not in df.columns or column in hidden:
            continue
        values = df[column]
        if values.isna().all():
            continue
        if values.astype(str).str.strip().eq("").all():
            continue
        visible.append(column)
    return visible


def _translate_selected(df: pd.DataFrame, key: str) -> None:
    """Translate whichever rows are selected in the table above.

    Streamlit has no hover action inside a dataframe and this app stays clear
    of custom HTML, so picking a row is the native way to reach one comment on
    its own. Rows that already carry a language are skipped by translate_rows,
    so this never repeats work the per-video button has done.
    """
    state = st.session_state.get(key)
    rows = getattr(getattr(state, "selection", None), "rows", None) if state else None
    if not rows:
        return

    picked = df.iloc[[r for r in rows if r < len(df)]]
    pending = [
        str(record["comment_id"])
        for _, record in picked.iterrows()
        if insights.needs_translation(record)
    ]
    if not pending:
        st.caption("The selected comment(s) are English or already translated.")
        return

    if st.button(
        f"Translate {len(pending):,} selected comment(s)",
        key=f"{key}_translate",
    ):
        with st.spinner("Translating..."):
            _translate_ids(pending)
        st.rerun()


def _comment_table(
    df: pd.DataFrame, height: int = 560, key: str | None = None,
    selectable: bool = True,
) -> None:
    """One table of comments, used by every view so they read alike."""
    present = _visible_columns(df)
    if not present:
        st.caption("Nothing to show.")
        return

    st.dataframe(
        df[present],
        width="stretch",
        hide_index=True,
        height=height,
        row_height=40,
        column_config=_table_config(present),
        key=key,
        on_select="rerun" if (selectable and key) else "ignore",
        selection_mode="multi-row" if (selectable and key) else None,
    )

    if selectable and key and insights.is_configured():
        _translate_selected(df, key)


def _all_comments(df: pd.DataFrame) -> None:
    """Every comment across every video as one sortable table."""
    sort_label = st.radio(
        "Sort comments by",
        list(FLAT_SORT_OPTIONS),
        horizontal=True,
        key="flat_sort",
    )
    column = FLAT_SORT_OPTIONS[sort_label]

    ordered = df
    if column in df.columns:
        ordered = df.sort_values(column, ascending=False, na_position="last")

    st.caption(
        f"{len(ordered):,} comment(s), {sort_label.lower()}. Click any column "
        "header to re-sort, or use the toolbar on the table to search and "
        "expand it."
    )
    _comment_table(ordered)


def _type_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Narrow to Shorts or to full videos.

    Rows collected before video_type existed have no value, so they are shown
    under All and called out rather than silently vanishing under either side.
    """
    if "video_type" not in df.columns:
        return df

    choice = st.segmented_control(
        "Video type",
        [ALL_TYPES, SHORTS_ONLY, VIDEOS_ONLY],
        default=ALL_TYPES,
        key="type_filter",
        label_visibility="collapsed",
        width="stretch",
    )
    if choice in (None, ALL_TYPES):
        return df

    wanted = (
        youtube_fetcher.VIDEO_TYPE_SHORT
        if choice == SHORTS_ONLY
        else youtube_fetcher.VIDEO_TYPE_VIDEO
    )
    filtered = df[df["video_type"].astype(str).str.strip() == wanted]

    unknown = int((df["video_type"].astype(str).str.strip() == "").sum())
    if unknown:
        st.caption(
            f"{unknown:,} comment(s) were collected before Shorts were "
            "distinguished and carry no type, so they are only visible under "
            f"{ALL_TYPES}."
        )
    return filtered


def _live_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Only what this session searched for.

    The Sheet accumulates every search ever run. Browsing that pile in the
    live report was the old multi-keyword view; past keywords are now export
    only, so the report stays about the search you just ran.
    """
    searched = st.session_state["session_keywords"]
    if not searched or "keyword" not in df.columns:
        return df.iloc[0:0]
    return df[df["keyword"].astype(str).isin(searched)]


@st.cache_data(show_spinner=False)
def _keyword_csv(keyword: str, rows: int, frame: pd.DataFrame) -> bytes:
    """CSV for one stored keyword. Cached so a rerun does not rebuild it."""
    return frame.to_csv(index=False).encode("utf-8-sig")


def _past_searches() -> None:
    """Every keyword ever saved, newest first, each as a download.

    Reference and export only: nothing here loads into the report above.
    """
    stored = st.session_state["data"]
    if stored.empty or "keyword" not in stored.columns:
        return

    keywords = stored["keyword"].astype(str).str.strip()
    stored = stored[keywords != ""]
    if stored.empty:
        return

    # Newest first, by when each keyword was last collected.
    if "fetched_at" in stored.columns:
        when = pd.to_datetime(stored["fetched_at"], errors="coerce", utc=True)
        order = stored.assign(_when=when).groupby("keyword")["_when"].max()
        order = order.sort_values(ascending=False, na_position="last")
        names = list(order.index)
    else:
        names = sorted(stored["keyword"].unique())

    st.divider()
    with st.expander(f"Past searches ({len(names)})", expanded=False):
        st.caption(
            "Every keyword saved to the Sheet. Download one to work with it "
            "elsewhere; the report above stays on this session's search."
        )
        for keyword in names:
            rows = stored[stored["keyword"] == keyword]
            st.download_button(
                f"{keyword} ({len(rows):,})",
                data=_keyword_csv(str(keyword), len(rows), rows),
                file_name=f"{str(keyword).replace(' ', '_')}_comments.csv",
                mime="text/csv",
                key=f"past_csv_{keyword}",
                width="stretch",
            )


def _comment_search(df: pd.DataFrame) -> pd.DataFrame:
    """Find within the comments already loaded.

    Nothing here touches YouTube: it is a substring match over the rows on
    screen, so typing narrows what is displayed and clearing it restores
    everything. Separate from the sidebar keyword, which decides what gets
    fetched in the first place.

    Matches on the original text or its English translation, so an English
    search term finds a comment that was written in another language.
    """
    query = st.text_input(
        "Search comments",
        key="comment_search",
        placeholder="Find a word or phrase in the comments below",
    ).strip()

    searchable = [
        column for column in ("comment_text", insights.TRANSLATION_COLUMN)
        if column in df.columns
    ]
    if not query or not searchable:
        return df

    hit = pd.Series(False, index=df.index)
    for column in searchable:
        hit |= df[column].astype(str).str.contains(
            query, case=False, na=False, regex=False
        )
    matches = df[hit]
    if matches.empty:
        st.caption(f"No comment contains \"{query}\".")
    else:
        st.caption(
            f"{len(matches):,} of {len(df):,} comment(s) contain \"{query}\"."
        )
    return matches


def _table_config(present: Sequence[str]) -> dict:
    """Streamlit column types, so sorting works on values and not on text."""
    config: dict = {}
    for column in present:
        label, width = RAW_TABLE_CONFIG.get(column, (None, None))
        if label:
            config[column] = st.column_config.TextColumn(label, width=width)

    numbers = {"comment_likes": "Likes", "video_views": "Views",
               "video_comment_count": "Video comments"}
    for column, label in numbers.items():
        if column in present:
            config[column] = st.column_config.NumberColumn(label, format="%d")

    dates = {"comment_published_at": "Commented", "video_published_at": "Video posted"}
    for column, label in dates.items():
        if column in present:
            config[column] = st.column_config.DatetimeColumn(
                label, format="YYYY-MM-DD HH:mm"
            )

    if "video_url" in present:
        config["video_url"] = st.column_config.LinkColumn("Link", display_text="Watch")
    return config


def _video_order(df: pd.DataFrame, order_label: str) -> list:
    """Video ids in the order the chosen sort puts them, best first.

    "Most relevant" has no local column to sort on, so it keeps the order the
    videos were collected in -- which is the order YouTube returned them.
    """
    ids = list(dict.fromkeys(df["video_id"]))
    column = VIDEO_SORT_COLUMNS.get(order_label)
    if column is None or column not in df.columns:
        return ids

    # One value per video, taken from its first row, then sorted descending.
    per_video = df.groupby("video_id")[column].first()
    per_video = per_video.reindex(ids)
    return list(per_video.sort_values(ascending=False, na_position="last").index)


def _drilldown(df: pd.DataFrame, order_label: str) -> None:
    """One dropdown per video, holding that video's comments.

    Same table as the flat report, so the two read alike; the difference is
    only the grouping.
    """
    if "video_id" not in df.columns:
        st.caption("These rows carry no video id, so they cannot be grouped.")
        return

    titles = df.groupby("video_id")["video_title"].first()
    counts = df.groupby("video_id").size()
    ordered = _video_order(df, order_label)

    total = len(ordered)

    # The stepper moves in tens -- 10, 20, 30 -- so the ceiling rounds up to the
    # next ten rather than stopping at the exact video count. With 19 videos you
    # can still press + to 20; the slice below just yields the 19 that exist.
    ceiling = max(VIDEO_PAGE_STEP, -(-total // VIDEO_PAGE_STEP) * VIDEO_PAGE_STEP)

    # A value carried over from a wider result set may sit outside the new
    # range. Clamp it in place, and only supply a default when there is no
    # stored value at all -- passing both is what Streamlit warns about.
    stored = st.session_state.get("videos_to_show")
    if stored is None:
        default = {"value": min(VIDEOS_PER_PAGE, ceiling)}
    else:
        st.session_state["videos_to_show"] = min(
            max(int(stored), VIDEO_PAGE_STEP), ceiling
        )
        default = {}

    page_size = st.number_input(
        "Videos to show",
        min_value=VIDEO_PAGE_STEP,
        max_value=ceiling,
        step=VIDEO_PAGE_STEP,
        key="videos_to_show",
        **default,
    )
    page = ordered[: int(page_size)]
    st.caption(
        f"Showing {len(page):,} of {total:,} video(s), sorted by "
        f"{order_label.lower()}."
    )

    for video_id in page:
        title = str(titles.get(video_id, "") or video_id)
        count = int(counts.get(video_id, 0))
        with st.expander(f"{title[:90]} - {count:,} comment(s)"):
            rows = df[df["video_id"] == video_id]
            url = str(rows["video_url"].iloc[0]) if "video_url" in rows else ""
            if url:
                st.caption(f"[Watch on YouTube]({url})")

            _translate_button(rows, str(video_id))

            # Short videos get a short table rather than a fixed pane of blank.
            height = min(560, 90 + 40 * max(len(rows), 1))
            _comment_table(rows, height=height, key=f"table_{video_id}")


# --------------------------------------------------------------------------
# Main -- one view, no tabs
# --------------------------------------------------------------------------
def main() -> None:
    _init_state()

    # Everything above the results draws before the Sheet is touched. Streamlit
    # streams each element to the browser as the script runs, so the title, the
    # sidebar and the search box are on screen and usable while the fetch below
    # is still in flight -- the page is no longer frozen behind one spinner.
    st.title("Social Listening - YouTube")
    st.caption("See what people are saying about any product or brand on YouTube.")

    config = _sidebar()

    # The spinner lives here, in the results area, and only covers this call.
    results = st.container()
    with results:
        _load_history()

    # Now that the Sheet has been read, the reserved sidebar slot can list it.
    with config["past_slot"]:
        _past_searches()

    if config["run"]:
        _run_search(config)
        # Land on what was just searched instead of everything ever collected.
        for keyword in config["keywords"]:
            if keyword not in st.session_state["session_keywords"]:
                st.session_state["session_keywords"].append(keyword)

    # The second rule only earns its place when the run summary sits between
    # the two; without it they just fence off an empty strip.
    if st.session_state["fetch_error"]:
        st.error(st.session_state["fetch_error"])
        st.session_state["fetch_error"] = ""

    if st.session_state["preview"]:
        _preview_section()
        st.divider()

    if st.session_state["last_run"]:
        _run_summary()
        st.divider()

    # The report is about this session's search. Everything else the Sheet
    # holds is downloadable from Past searches in the sidebar.
    data = _live_rows(insights.ensure_columns(st.session_state["data"]))
    if data.empty:
        st.info(
            "Search any product or competitor name to see what people are "
            "saying about it on YouTube."
        )
        return

    if st.session_state["translation_status"]:
        st.caption(st.session_state["translation_status"])

    # The view sits above the filters: it says what you are looking at, and the
    # filters below narrow it. Full width, matching the type filter row.
    view = st.segmented_control(
        "View",
        [BY_VIDEO_VIEW, ALL_COMMENTS_VIEW],
        default=BY_VIDEO_VIEW,
        key="main_view",
        label_visibility="collapsed",
        width="stretch",
    )

    data = _type_filter(data)
    _text_display(data)
    if data.empty:
        st.info("No comments match the selected keyword(s).")
        return

    data = _comment_search(data)
    if data.empty:
        return

    if view == ALL_COMMENTS_VIEW:
        _all_comments(data)
    else:
        _drilldown(data, config["order_label"])

    st.divider()
    st.download_button(
        f"Download these {len(data):,} comments as CSV",
        data=data.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"godesi_youtube_comments_{_dt.datetime.now():%Y%m%d_%H%M}.csv",
        mime="text/csv",
        help=(
            "The comments for the keywords selected above, with every column "
            "including the ones the table does not show."
        ),
    )

    if sheets_store.is_configured():
        if st.button("Reload from Google Sheets"):
            st.session_state["loaded_from_sheet"] = False
            st.rerun()


if __name__ == "__main__":
    main()
