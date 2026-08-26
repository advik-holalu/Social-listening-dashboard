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

import altair as alt
import pandas as pd
import streamlit as st

import insights
import sheets_store
import transcripts
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
MAX_VIDEOS_PER_KEYWORD = 20
MAX_COMMENTS_PER_VIDEO = 500

# Worst-case quota per keyword, derived from the fetcher's page sizes so the two
# cannot drift: one search page (100 units) + one commentThreads page per 100
# comments per video (1 unit each) + one batched videos.list call (1 unit).
_COMMENT_PAGES_PER_VIDEO = -(-MAX_COMMENTS_PER_VIDEO // youtube_fetcher.MAX_COMMENT_PAGE)
QUOTA_UNITS_PER_KEYWORD = (
    100 + MAX_VIDEOS_PER_KEYWORD * _COMMENT_PAGES_PER_VIDEO + 1
)

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
    # video_id -> transcript summary, shared by every comment on that video.
    st.session_state.setdefault("video_summaries", {})
    # video_id -> why a transcript did not arrive, so the drilldown can say
    # "blocked, try later" instead of "this video has no captions".
    st.session_state.setdefault("transcript_outcomes", {})
    st.session_state.setdefault("last_run", None)
    # The most recent search's rows and summaries, waiting on the Save button.
    st.session_state.setdefault("pending_rows", [])
    # Every comment_id fetched this session that is not in the Sheet. Saving
    # clears the ids it wrote. Nothing outside this set may ever be written by
    # a side effect: Save is the only action that puts data in the Sheet.
    st.session_state.setdefault("unsaved_ids", set())
    # Fingerprints the reader has asked for a summary of, so the button does
    # not reappear once its answer is on screen.
    st.session_state.setdefault("digest_requested", set())
    # Summaries written this session that the Sheet has not taken, because the
    # comments they describe are not saved.
    st.session_state.setdefault("digest_unstored", set())
    st.session_state.setdefault("pending_summaries", [])
    st.session_state.setdefault("save_state", "unsaved")
    st.session_state.setdefault("text_display", SHOW_BOTH)
    st.session_state.setdefault("translation_status", "")
    st.session_state.setdefault("classify_status", "")
    # Which keywords the results are narrowed to. None means "not chosen yet",
    # which _keyword_filter reads as "show everything".
    st.session_state.setdefault("keyword_filter", None)


def _stamp_refresh() -> None:
    st.session_state["last_refreshed"] = _dt.datetime.now()


# How long a Sheets read is reused before going back to the network. Long
# enough that reopening the app or refreshing the tab is instant, short enough
# that a search run in another tab shows up quickly.
HISTORY_TTL_SECONDS = 60


@st.cache_data(ttl=HISTORY_TTL_SECONDS, show_spinner=False)
def _fetch_history() -> tuple[pd.DataFrame, pd.DataFrame]:
    """The two Sheets reads, cached so a refresh inside the TTL costs nothing.

    Kept separate from _load_history so the caching sits on the network call
    alone, with no Streamlit state mixed in.
    """
    return sheets_store.load_all(), sheets_store.load_video_summaries()


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
            stored, summaries = _fetch_history()
        st.session_state["data"] = stored
        st.session_state["video_summaries"] = sheets_store.summary_lookup(summaries)
        st.session_state["sheet_status"] = (
            f"Loaded {len(stored):,} saved comments and "
            f"{len(st.session_state['video_summaries']):,} video summaries."
        )
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
    st.sidebar.header("Search")

    keywords_raw = st.sidebar.text_input(
        "Target keyword",
        value="GO DESi",
        key="keywords_raw",
        help="Separate several with commas, e.g. GO DESi, imli pop, chikki",
    )
    keywords = parse_keywords(keywords_raw)
    if len(keywords) > 1:
        st.sidebar.caption(f"Searching {len(keywords)}: {', '.join(keywords)}")

    options = _search_options()

    run = st.sidebar.button("Run Search", type="primary", use_container_width=True)

    _sidebar_details(keywords)

    return {"keywords": keywords, "run": run, **options}


def _sidebar_details(keywords: list[str]) -> None:
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

        st.caption(
            f"Each keyword collects up to {MAX_VIDEOS_PER_KEYWORD} videos and "
            f"{MAX_COMMENTS_PER_VIDEO} comments per video."
        )

        # Worst-case quota bill per keyword, before the user spends it:
        #   search.list         100 units (one page covers 20 results)
        # + commentThreads.list  up to 100 units (20 videos x 5 pages of 100)
        # + videos.list            1 unit  (all 20 ids in one batched call)
        estimated = len(keywords) * QUOTA_UNITS_PER_KEYWORD
        st.caption(
            f"Estimated API cost: up to ~{estimated:,} of 10,000 daily YouTube "
            f"quota units (~{QUOTA_UNITS_PER_KEYWORD} per keyword)."
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
    days_back = st.sidebar.number_input(
        "From the last N days",
        min_value=0,
        max_value=3650,
        value=0,
        step=30,
        key="opt_days_back",
        help="0 means no date limit.",
    )

    st.sidebar.markdown("**What to collect**")
    with st.sidebar.container(border=True, gap="xxsmall"):
        include_replies = st.checkbox(
            "Include replies", value=True, key="opt_replies"
        )
        summarize_videos = st.checkbox(
            "Summarise transcripts",
            value=insights.is_configured(),
            disabled=not insights.is_configured(),
            key="opt_summarize",
            help=(
                "Reads each new video's transcript (no YouTube quota) and has "
                "Claude summarise it, so each video section says what people "
                "are reacting to. Adds one Claude call per new video."
            ),
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
        "published_after": published_after,
        "include_replies": include_replies,
        "summarize_videos": summarize_videos,
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

    # Videos already summarised are skipped, so a transcript is fetched and
    # summarised once per video no matter how often it resurfaces in a search.
    summarizer = None
    if config["summarize_videos"] and insights.is_configured():
        try:
            summarizer = insights.make_summarizer()
        except InsightsError as exc:
            st.warning(f"Video summaries are off for this run: {exc}")

    try:
        rows, report = youtube_fetcher.run_search(
            api_key=api_key,
            keywords=config["keywords"],
            max_videos=MAX_VIDEOS_PER_KEYWORD,
            max_comments=MAX_COMMENTS_PER_VIDEO,
            order=config["order"],
            include_replies=config["include_replies"],
            published_after=config["published_after"],
            progress_cb=on_progress,
            summarizer=summarizer,
            known_video_ids=set(st.session_state["video_summaries"]),
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

    added = _merge(rows)
    st.session_state["last_report"] = report
    _stamp_refresh()

    st.session_state["transcript_outcomes"].update(report.transcript_outcomes)

    fresh_summaries = report.video_summaries
    if fresh_summaries:
        st.session_state["video_summaries"].update(
            {
                video_id: row["video_summary"]
                for video_id, row in fresh_summaries.items()
                if row.get("video_summary")
            }
        )

    # Nothing is written here. The results live in this session until someone
    # presses Save in the results area; a new search replaces what is on offer,
    # so an unsaved run simply ends with the session.
    st.session_state["pending_rows"] = rows
    # Union, not replace: a second unsaved search does not make the first one
    # saved, and its rows must stay out of the Sheet too.
    st.session_state["unsaved_ids"].update(
        str(row.get("comment_id", "")) for row in rows
    )
    st.session_state["pending_summaries"] = list(fresh_summaries.values())
    st.session_state["save_state"] = "unsaved"

    if report.quota_exhausted:
        st.warning(
            "The daily YouTube quota ran out mid-run. The results below are "
            "partial - quota resets at midnight Pacific Time.",
        )

    # Handed to _run_summary() so the headline survives later reruns instead of
    # scrolling away with the progress bar.
    st.session_state["last_run"] = {
        "comments": report.comments_fetched,
        "videos": report.videos_with_comments or report.videos_searched,
        "added": added,
        "keywords": list(config["keywords"]),
        "transcripts_found": report.transcripts_found,
        "transcripts_missing": report.transcripts_missing,
        "transcripts_blocked": report.transcripts_blocked,
        "transcripts_error": report.transcripts_error,
        "summaries": len(fresh_summaries),
        "ids": {str(row.get("comment_id", "")) for row in rows},
    }

    if report.warnings:
        with st.expander(f"{len(report.warnings)} notice(s) from this run"):
            for warning in report.warnings:
                st.write(f"- {warning}")


def _sentiment_phrase(rows: pd.DataFrame) -> str:
    """"mostly positive sentiment" for the rows just collected, when known."""
    if rows.empty or "sentiment" not in rows.columns:
        return ""
    scored = rows[rows["sentiment"].astype(str).str.strip() != ""]
    if scored.empty:
        return ""
    share = scored["sentiment"].value_counts(normalize=True)
    leader = share.idxmax()
    return f"mostly {leader} sentiment ({share.max() * 100:.0f}%)"


def _save_run() -> None:
    """Write the current search's comments and video summaries to the Sheet.

    Dedupe is unchanged -- append_rows still skips any comment_id the sheet
    already holds, and save_video_summaries skips videos already summarised.
    """
    rows = st.session_state["pending_rows"]
    summaries = st.session_state["pending_summaries"]

    try:
        written = skipped = 0
        if rows:
            written, skipped = sheets_store.append_rows(rows)
        if summaries:
            sheets_store.save_video_summaries(summaries)
    except SheetsError as exc:
        st.session_state["sheet_status"] = f"Could not save to Sheets: {exc}"
        return

    st.session_state["save_state"] = "saved"
    # These rows are in the Sheet now, so tags may be written onto them.
    st.session_state["unsaved_ids"].difference_update(
        str(row.get("comment_id", "")) for row in rows
    )
    # Any summary held back for these rows can be stored on the next render.
    st.session_state["digest_unstored"].clear()
    st.session_state["sheet_status"] = (
        f"Saved {written:,} new comment(s) to Sheets "
        f"({skipped:,} were already there)."
    )
    # The next reload should see what was just written, not the cached read.
    _fetch_history.clear()


def _save_controls() -> None:
    """One button, one before-and-after state, right under the headline."""
    if not st.session_state["pending_rows"]:
        return

    if not sheets_store.is_configured():
        st.caption(
            "Google Sheets is not connected, so these results live only in "
            "this session."
        )
        return

    if st.session_state["save_state"] == "saved":
        st.button(
            ":green[Saved to Google Sheets]",
            icon=":material/check:",
            disabled=True,
            key="save_run_done",
        )
        return

    # Green via the label's colour directive: Streamlit buttons have no colour
    # parameter, and primary red belongs to Run Search alone.
    if st.button(
        ":green[**Save these results to Google Sheets**]",
        icon=":material/save:",
        type="secondary",
        key="save_run",
    ):
        with st.spinner("Saving to Google Sheets..."):
            _save_run()
        st.rerun()

    st.caption(
        "The comments and video summaries from this search are not saved yet."
    )


def _run_summary() -> None:
    """The one-line "here is what just happened" block in the Search tab."""
    last = st.session_state["last_run"]
    if not last:
        return

    data = insights.ensure_columns(st.session_state["data"])
    collected = data[data["comment_id"].isin(last["ids"])] if last["ids"] else data.iloc[0:0]

    headline = (
        f"{last['comments']:,} comments collected from {last['videos']:,} videos"
    )
    phrase = _sentiment_phrase(collected)
    if phrase:
        headline += f" - {phrase}"
    st.success(headline)

    one, two, three = st.columns(3)
    one.metric("Comments collected", f"{last['comments']:,}")
    two.metric("New in this session", f"{last['added']:,}")
    three.metric("Videos", f"{last['videos']:,}")

    _save_controls()

    _transcript_line(last)


def _transcript_line(last: dict) -> None:
    """Spell out what happened to the transcripts, blocks included.

    A blanket "unavailable" once hid a total IP block behind a message about
    missing captions, so a block is now named as its own outcome.
    """
    parts = [f"{last['transcripts_found']:,} found"]
    if last.get("transcripts_missing"):
        parts.append(f"{last['transcripts_missing']:,} unavailable")
    if last.get("transcripts_blocked"):
        parts.append(
            f"{last['transcripts_blocked']:,} blocked (YouTube rate limit)"
        )
    if last.get("transcripts_error"):
        parts.append(f"{last['transcripts_error']:,} failed")

    if len(parts) == 1 and not last["transcripts_found"]:
        return

    st.caption(
        f"Transcripts: {', '.join(parts)} "
        f"({last['summaries']:,} new video summaries)."
    )
    if last.get("transcripts_blocked"):
        st.warning(
            "YouTube is rate limiting transcript downloads from this IP. Those "
            "videos have captions; we just could not read them right now. "
            "Wait a while and search again to pick them up."
        )


# --------------------------------------------------------------------------
# Raw Data -- the "By Video" view
# --------------------------------------------------------------------------
# Rendering comments as real widgets costs far more than a dataframe does, so
# the grouped view draws a page at a time rather than every row at once.
BY_VIDEO_VIEW = "By Video"
ALL_COMMENTS_VIEW = "All Comments"
BY_KIND_VIEW = "By Kind"
ANALYSIS_VIEW = "Analysis"

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
    "kind",
    "sentiment",
    "category",
    "flagged_ask",
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
    "sentiment": ("Sentiment", "small"),
    "category": ("Category", "small"),
    "flagged_ask": ("Flagged ask", "medium"),
    "video_type": ("Type", "small"),
    "kind": ("Kind", "small"),
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
                touched = _saved_rows(updated, batch)
                if not touched.empty:
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


def _latest_keyword(df: pd.DataFrame, available: list[str]) -> str:
    """The keyword collected most recently, by when its rows were fetched."""
    if "fetched_at" not in df.columns:
        return available[-1]
    stamps = pd.to_datetime(df["fetched_at"], errors="coerce", utc=True)
    latest = df.assign(_fetched=stamps).groupby("keyword")["_fetched"].max()
    latest = latest.dropna()
    if latest.empty:
        return available[-1]
    return str(latest.idxmax())


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


def _unsaved_work_notice(df: pd.DataFrame) -> None:
    """Point out AI work that will vanish with the session.

    Streamlit cannot intercept navigation, and blocking it would be worse than
    the problem, so this is a standing reminder rather than a prompt: while
    unsaved comments carry results that cost money to produce, it says so at
    the top of the results, whichever view you are in.
    """
    if not sheets_store.is_configured():
        return

    unsaved = st.session_state["unsaved_ids"]
    if not unsaved or df.empty or "comment_id" not in df.columns:
        return

    rows = df[df["comment_id"].astype(str).isin(unsaved)]
    if rows.empty:
        return

    at_risk = []
    tagged = (rows[insights.KIND_COLUMN].astype(str).str.strip() != "") | (
        rows[insights.SENTIMENT_COLUMN].astype(str).str.strip() != ""
    )
    if tagged.any():
        at_risk.append(f"classification results for {int(tagged.sum()):,} comment(s)")
    if (rows[insights.TRANSLATION_COLUMN].astype(str).str.strip() != "").any():
        at_risk.append("translations")
    if st.session_state["digest_unstored"]:
        at_risk.append("a written summary")

    if not at_risk:
        return

    if len(at_risk) == 1:
        what = at_risk[0]
    else:
        what = ", ".join(at_risk[:-1]) + " and " + at_risk[-1]
    st.warning(
        f"This search has unsaved {what}. Click **Save these results to Google "
        "Sheets** above to keep them."
    )


def _saved_rows(frame: pd.DataFrame, ids: Sequence[str] | None = None) -> pd.DataFrame:
    """The subset of `frame` that actually exists in the Sheet.

    Tag writes go through here so classification and translation can never put
    anything in the Sheet that Save did not put there first. Without it, a
    write for rows the Sheet has never seen still widens its header, which is
    a change nobody asked for.
    """
    if frame.empty or "comment_id" not in frame.columns:
        return frame.iloc[0:0]

    keys = frame["comment_id"].astype(str)
    if ids is not None:
        frame = frame[keys.isin({str(i) for i in ids})]
        keys = frame["comment_id"].astype(str)

    unsaved = st.session_state["unsaved_ids"]
    if not unsaved:
        return frame
    return frame[~keys.isin(unsaved)]


def _unsaved_in(df: pd.DataFrame) -> int:
    """How many of these rows are still waiting on the Save button."""
    unsaved = st.session_state["unsaved_ids"]
    if not unsaved or df.empty:
        return 0
    return int(df["comment_id"].astype(str).isin(unsaved).sum())


# Completed batches to gather before writing to the Sheet. Flushing on every
# batch would pay a Sheets round trip per 50 comments; flushing never would
# lose the lot to a refresh. This keeps at most a few batches at risk.
CLASSIFY_FLUSH_EVERY = 4

# Measured latency of one 50-comment classification call, used only to tell
# the reader roughly how long to stay put.
SECONDS_PER_BATCH = 7.5

# Shown beside the progress bar. Classification is synchronous, so leaving the
# tab stops it; this says so, and says that stopping costs nothing.
CLASSIFY_NOTICE = (
    "This takes about {seconds} seconds - please stay on this tab until it"
    " finishes. If you navigate away, progress made so far is saved and you"
    " can resume by clicking Classify comments again."
)


def _classify_seconds(count: int) -> int:
    """Rough wall clock for a run, from measured batch latency."""
    batches = max(1, -(-count // insights.CLASSIFY_BATCH_SIZE))
    waves = max(1, -(-batches // insights.CLASSIFY_WORKERS))
    return max(5, int(waves * SECONDS_PER_BATCH))


def _classify_now(ids: Sequence[str]) -> None:
    """Classify these comment_ids, showing progress and saving as it goes."""
    progress = st.progress(0.0)
    status = st.empty()
    notice = st.empty()
    unflushed: list[str] = []
    since_flush = {"n": 0}
    # A failed save must not be overwritten by the success line below.
    save_error = {"msg": ""}

    notice.caption(CLASSIFY_NOTICE.format(seconds=_classify_seconds(len(ids))))

    def on_progress(done: int, total: int) -> None:
        progress.progress(min(1.0, done / max(total, 1)))
        status.markdown(f"Classifying... {done:,}/{total:,} comments")

    def flush(frame) -> None:
        """Persist what has landed so far, so a refresh does not lose it."""
        if not unflushed or not sheets_store.is_configured():
            unflushed.clear()
            return
        touched = _saved_rows(frame, unflushed)
        if touched.empty:
            # Nothing here is in the Sheet, so there is nothing to write onto.
            unflushed.clear()
            return
        try:
            sheets_store.update_analysis(touched)
        except SheetsError as exc:
            _LOG.exception("Saving classifications failed")
            save_error["msg"] = str(exc)
        unflushed.clear()

    def on_batch(frame, ids_done: list) -> None:
        # Hand the part-finished frame to the session as each batch lands. The
        # assignment at the end of this function only happens if the run gets
        # to finish; without this, navigating away mid-run would leave the
        # session believing comments already written to the Sheet are still
        # pending, and the next click would pay for them a second time.
        st.session_state["data"] = frame
        unflushed.extend(ids_done)
        since_flush["n"] += 1
        if since_flush["n"] >= CLASSIFY_FLUSH_EVERY:
            since_flush["n"] = 0
            flush(frame)

    try:
        updated, count = insights.classify_rows(
            st.session_state["data"],
            ids=ids,
            progress_cb=on_progress,
            on_batch=on_batch,
        )
    except InsightsError as exc:
        # st.error here would be wiped by the rerun that follows this call, so
        # the message goes into state and is drawn on the next run instead.
        _LOG.exception("Classification failed")
        st.session_state["classify_status"] = f"Classification failed: {exc}"
        return
    except Exception as exc:
        _LOG.exception("Classification failed unexpectedly")
        st.session_state["classify_status"] = (
            f"Classification failed: {type(exc).__name__}: {exc}"
        )
        return
    finally:
        progress.empty()
        status.empty()
        notice.empty()

    st.session_state["data"] = updated
    flush(updated)                       # whatever the last partial group holds

    if count < len(ids):
        done_line = (
            f"Classified {count:,} of {len(ids):,}. The rest are still "
            "unclassified; click again to finish them."
        )
    else:
        done_line = f"Classified {count:,} comment(s)."

    if save_error["msg"]:
        # The work happened; it just did not reach the Sheet. Say both.
        done_line += (
            f" Saving to Sheets failed: {save_error['msg']}. The results are "
            "in this session only."
        )
    st.session_state["classify_status"] = done_line


def _classification_controls(df: pd.DataFrame) -> None:
    """The cost warning and the button, shared by By Kind and Analysis.

    One Claude call fills both the bucket and the sentiment, so whichever view
    triggers it, the other has its answers already.
    """
    status = st.session_state["classify_status"]
    if status:
        # A failure is not a footnote: show it as one.
        if status.startswith("Classification failed"):
            st.error(status)
        else:
            st.caption(status)

    # Only what is on screen: the frame handed in has already been through the
    # keyword and type filters, so a hidden keyword is never classified.
    pending = df[insights.kind_pending_mask(df)]
    if pending.empty:
        return

    if not insights.is_configured():
        st.caption("Add an `ANTHROPIC_API_KEY` to classify comments.")
        return

    cost = insights.estimated_cost(len(pending))
    st.caption(
        "This groups comments by type and scores their sentiment using AI "
        f"classification. Costs roughly ${insights.COST_PER_COMMENT:.4f} per "
        f"comment (about ${cost:,.2f} for the {len(pending):,} unclassified "
        "comment(s) here), and fills both this view and the other one in a "
        "single pass."
    )
    if _unsaved_in(pending):
        st.caption(
            "Comments from an unsaved search keep their result only for this "
            "session. Save the search to store it."
        )
    if st.button("Classify comments", key="classify_now"):
        st.session_state["classify_status"] = ""
        _classify_now([str(i) for i in pending["comment_id"].astype(str)])
        st.rerun()


# Minimum comments before a video can be called a problem. Below this, one
# grumpy viewer swings the percentage and the ranking means nothing.
PROBLEM_MIN_COMMENTS = 10


def _digest_fingerprint(df: pd.DataFrame, keywords: list[str]) -> str:
    """A stable id for exactly this set of analysed comments.

    Built from the comment ids and their verdicts, so the same set always
    resolves to the same stored summary and any change -- a new comment, a
    re-classification -- asks for a fresh one.
    """
    parts = []
    for record in df.sort_values("comment_id").itertuples():
        parts.append(
            f"{record.comment_id}:{getattr(record, insights.SENTIMENT_COLUMN, '')}"
            f":{getattr(record, insights.KIND_COLUMN, '')}"
        )
    body = "|".join(parts).encode("utf-8")
    return f"{','.join(sorted(keywords))}#{hashlib.sha256(body).hexdigest()[:16]}"


@st.cache_data(ttl=HISTORY_TTL_SECONDS, show_spinner=False)
def _stored_digests() -> dict:
    """Summaries already in the Sheet, so a reboot does not re-buy them."""
    if not sheets_store.is_configured():
        return {}
    try:
        return sheets_store.load_digests()
    except SheetsError:
        return {}


@st.cache_data(ttl=3600, show_spinner=False)
def _generated_digest(fingerprint: str, payload: str) -> str:
    """A digest written this session. The fingerprint is the cache key."""
    return insights.write_digest(payload)


def _digest_section(df: pd.DataFrame, keywords: list[str]) -> None:
    """The written summary, at the top and in a callout rather than a chart.

    Looked up in the Sheet first: it costs a Claude call to produce, so it is
    stored beside the comments it describes rather than living in a cache that
    a restart empties.
    """
    payload = insights.digest_payload(df, keywords)
    if not payload:
        return

    fingerprint = _digest_fingerprint(df, keywords)

    stored = _stored_digests().get(fingerprint, "")
    if stored:
        st.info(stored)
        return

    if not insights.is_configured():
        return

    # Like Classify and Translate, this spends money, so it waits to be asked.
    # The request is remembered per fingerprint: once written, the summary
    # renders on every later visit without the button coming back.
    if fingerprint not in st.session_state["digest_requested"]:
        if st.button("Write summary", key="write_digest", icon=":material/edit_note:"):
            st.session_state["digest_requested"].add(fingerprint)
            st.rerun()
        st.caption(
            f"One Claude call over these {len(df):,} analysed comments, about a "
            "cent. Written once and kept, so this is not asked again."
        )
        return

    try:
        summary = _generated_digest(fingerprint, payload)
    except InsightsError as exc:
        _LOG.exception("Digest failed")
        st.error(f"Summary unavailable: {exc}")
        return
    except Exception as exc:
        # The digest is a nicety. Whatever goes wrong writing it, the charts
        # and tables below are the substance and must still render.
        _LOG.exception("Digest failed unexpectedly")
        st.error(f"Summary unavailable: {type(exc).__name__}: {exc}")
        return

    if not summary:
        return

    st.info(summary)

    # Store it only when every comment it describes is in the Sheet. Saving a
    # summary of rows that were never saved would put data there that Save did
    # not, and would describe comments nobody can look up later.
    if not sheets_store.is_configured():
        return
    if _unsaved_in(df):
        st.session_state["digest_unstored"].add(fingerprint)
        st.caption(
            "This summary is not stored yet. Save the search to keep it."
        )
        return
    try:
        if sheets_store.save_digest(
            fingerprint, ", ".join(sorted(keywords)), len(df), summary
        ):
            st.session_state["digest_unstored"].discard(fingerprint)
            _stored_digests.clear()
    except SheetsError as exc:
        st.caption(f"Summary could not be stored: {exc}")


def _sentiment_donuts(df: pd.DataFrame, keywords: list[str]) -> None:
    """Share of sentiment, one donut per keyword.

    A donut is only honest for a single part-to-whole split, so two keywords
    get two donuts rather than one merged ring that answers neither question.
    """
    share = insights.sentiment_share(df, "keyword")
    if share.empty:
        return

    st.subheader("Overall sentiment")
    colour = alt.Color(
        "sentiment:N",
        scale=alt.Scale(
            domain=insights.SENTIMENTS,
            range=[insights.SENTIMENT_COLORS[s] for s in insights.SENTIMENTS],
        ),
        legend=alt.Legend(title="Sentiment", orient="bottom"),
    )
    # A 2px surface gap between segments, per the mark spec.
    base = alt.Chart(share).mark_arc(innerRadius=55, stroke=None, strokeWidth=2)
    chart = base.encode(
        theta=alt.Theta("pct:Q", stack=True),
        color=colour,
        tooltip=["keyword:N", "sentiment:N", alt.Tooltip("pct:Q", title="% of comments"),
                 alt.Tooltip("count:Q", title="comments")],
    ).properties(width=190, height=190)

    if len(keywords) >= 2:
        st.altair_chart(chart.facet(column=alt.Column("keyword:N", title=None)))
    else:
        st.altair_chart(chart, use_container_width=True)

    # Numbers as well as arcs: a reader should never have to judge an angle,
    # and it is the relief the palette's contrast warning asks for.
    for keyword in keywords:
        row = share[share["keyword"] == keyword]
        if row.empty:
            continue
        parts = ", ".join(
            f"{int(r.pct)}% {r.sentiment}"
            for r in row.sort_values("sentiment").itertuples()
        )
        st.caption(f"**{keyword}**: {parts} of {int(row['count'].sum()):,} comments")


def _kind_sentiment_chart(df: pd.DataFrame) -> None:
    """How each kind of comment splits by sentiment."""
    matrix = insights.kind_sentiment_matrix(df)
    if matrix.empty:
        return

    st.subheader("Sentiment within each type")
    chart = (
        alt.Chart(matrix)
        .mark_bar(stroke=None, strokeWidth=2, cornerRadiusEnd=4)
        .encode(
            x=alt.X("kind:N", title=None, sort=insights.KINDS,
                    axis=alt.Axis(labelAngle=0)),
            y=alt.Y("count:Q", title="Comments", stack=True),
            color=alt.Color(
                "sentiment:N",
                scale=alt.Scale(
                    domain=insights.SENTIMENTS,
                    range=[insights.SENTIMENT_COLORS[s] for s in insights.SENTIMENTS],
                ),
                legend=alt.Legend(title="Sentiment", orient="bottom"),
            ),
            tooltip=["kind:N", "sentiment:N", alt.Tooltip("count:Q", title="comments")],
        )
        .properties(height=280)
    )
    st.altair_chart(chart, use_container_width=True)


def _problem_videos(df: pd.DataFrame) -> None:
    """Which videos are drawing the unhappiest comments, and what they were."""
    ranked = insights.problem_videos(df, min_comments=PROBLEM_MIN_COMMENTS)
    if ranked.empty:
        st.subheader("Videos drawing the most negative comments")
        st.caption(
            f"No video has {PROBLEM_MIN_COMMENTS} or more analysed comments yet, "
            "which is too few to rank fairly."
        )
        return

    st.subheader("Videos drawing the most negative comments")
    st.caption(
        f"Ranked by share of negative comments, among videos with at least "
        f"{PROBLEM_MIN_COMMENTS} analysed comments."
    )

    summaries = st.session_state["video_summaries"]
    for record in ranked.itertuples():
        title = str(record.video_title or record.video_id)
        st.markdown(
            f"**{record.pct_negative:.0f}% negative** - {title} "
            f":gray[:small[({record.negative} of {record.comments} comments)]]"
        )
        summary = str(summaries.get(record.video_id, "") or "").strip()
        if summary:
            st.caption(f"What the video is about: {summary}")


def _highlights(df: pd.DataFrame) -> None:
    """The single loudest complaint and compliment, quoted."""
    pairs = [("complaint", "Most-liked complaint"), ("compliment", "Most-liked compliment")]
    found = [(insights.top_comment(df, kind), label) for kind, label in pairs]
    if not any(record is not None for record, _ in found):
        return

    st.subheader("What resonated most")
    for record, label in found:
        if record is None:
            continue
        text = " ".join(str(record.get("comment_text", "")).split())
        likes = int(record.get("comment_likes", 0) or 0)
        author = str(record.get("comment_author", "") or "unknown")
        video = str(record.get("video_title", "") or "")
        st.markdown(f"**{label}**")
        st.markdown(f"> {text}")
        st.caption(
            f":gray[:small[{author} - {likes:,} like"
            f"{'' if likes == 1 else 's'}"
            f"{' - ' + video if video else ''}]]"
        )


def _shorts_vs_videos(df: pd.DataFrame) -> None:
    """Whether Shorts and full videos draw different feelings."""
    if "video_type" not in df.columns:
        return
    share = insights.sentiment_share(
        df[df["video_type"].astype(str).str.strip() != ""], "video_type"
    )
    if share.empty or share["video_type"].nunique() < 2:
        return

    st.subheader("Shorts against full videos")
    chart = (
        alt.Chart(share)
        .mark_bar(cornerRadiusEnd=4)
        .encode(
            x=alt.X("video_type:N", title=None, axis=alt.Axis(labelAngle=0)),
            xOffset=alt.XOffset("sentiment:N", sort=insights.SENTIMENTS),
            y=alt.Y("pct:Q", title="% of comments"),
            color=alt.Color(
                "sentiment:N",
                scale=alt.Scale(
                    domain=insights.SENTIMENTS,
                    range=[insights.SENTIMENT_COLORS[s] for s in insights.SENTIMENTS],
                ),
                legend=alt.Legend(title="Sentiment", orient="bottom"),
            ),
            tooltip=["video_type:N", "sentiment:N",
                     alt.Tooltip("pct:Q", title="% of comments"),
                     alt.Tooltip("count:Q", title="comments")],
        )
        .properties(height=280)
    )
    st.altair_chart(chart, use_container_width=True)


def _analysis(df: pd.DataFrame) -> None:
    """Sentiment across the selected keywords, with the evidence behind it."""
    _classification_controls(df)

    scored = df[df[insights.SENTIMENT_COLUMN].astype(str).str.strip() != ""]
    if scored.empty:
        # Everything here is built on the sentiment and kind of each comment,
        # so there is nothing to draw until those exist. Say which action
        # produces them rather than leaving a blank tab.
        if df.empty:
            st.caption("No comments in view.")
        elif insights.is_configured():
            st.info(
                "Classify comments first to generate a summary. Use the "
                "**Classify comments** button above; it fills this tab and "
                "**By Kind** in the same pass."
            )
        else:
            st.info(
                "Classify comments first to generate a summary. That needs an "
                "`ANTHROPIC_API_KEY` in `.streamlit/secrets.toml`."
            )
        return

    keywords = sorted({str(k) for k in scored["keyword"].dropna() if str(k).strip()})

    _digest_section(scored, keywords)
    _sentiment_donuts(scored, keywords)
    st.divider()
    _kind_sentiment_chart(scored)
    st.divider()
    _shorts_vs_videos(scored)
    st.divider()
    _problem_videos(scored)
    st.divider()
    _highlights(scored)


def _by_kind(df: pd.DataFrame) -> None:
    """Comments grouped into question / compliment / complaint / suggestion.

    Nothing runs on arrival: this is the one view that spends money per
    comment, so it states the cost and waits to be asked.
    """
    _classification_controls(df)

    classified = df[df[insights.KIND_COLUMN].astype(str).str.strip() != ""]

    if classified.empty:
        st.caption("No comments have been classified yet.")
        return

    # Buckets in a fixed order, so the page does not reshuffle between runs.
    for kind in insights.KINDS:
        rows = classified[classified[insights.KIND_COLUMN] == kind]
        if rows.empty:
            continue
        label = insights.KIND_LABELS.get(kind, kind.title())
        with st.expander(f"{label} - {len(rows):,} comment(s)", expanded=False):
            height = min(560, 90 + 40 * max(len(rows), 1))
            _comment_table(rows, height=height, key=f"kind_{kind}")


def _keyword_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Narrow the results to the keywords worth looking at right now.

    The sheet accumulates every search ever run, so without this the newest
    search is just more rows in a pile of older ones. A fresh search points
    this at whatever was searched -- see main().
    """
    if "keyword" not in df.columns:
        return df

    available = sorted({k for k in df["keyword"].dropna().astype(str) if k.strip()})
    if len(available) < 2:
        return df

    # Keep the selection valid when the stored data changes underneath it.
    stored = st.session_state["keyword_filter"]
    if stored is None:
        # Opening the app cold: show the last thing searched, not the pile.
        st.session_state["keyword_filter"] = [_latest_keyword(df, available)]
    else:
        kept = [k for k in stored if k in available]
        st.session_state["keyword_filter"] = kept or available

    chosen = st.multiselect(
        "Keywords to show",
        available,
        key="keyword_filter",
        help="The sheet keeps every search. Pick the ones you want to read.",
    )

    if not chosen:
        st.caption("No keyword selected, so everything stored is showing.")
        return df

    filtered = df[df["keyword"].isin(chosen)]
    if len(chosen) < len(available):
        hidden = sorted(set(available) - set(chosen))
        st.caption(
            f"{len(filtered):,} of {len(df):,} stored comment(s). "
            f"Also collected: {', '.join(hidden)} - add them above to compare."
        )
    return filtered


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
    """One dropdown per video: what the video was about, then its comments.

    Same table as All Comments, so the two views read alike -- the difference
    is that here each table sits under the summary of the video its comments
    are reacting to, which is the whole point of the split.
    """
    if "video_id" not in df.columns:
        st.caption("These rows carry no video id, so they cannot be grouped.")
        return

    summaries = st.session_state["video_summaries"]
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
            summary = str(summaries.get(video_id, "") or "").strip()
            if summary:
                st.info(f"**What this video is about:** {summary}")
            else:
                outcome = st.session_state["transcript_outcomes"].get(video_id)
                if outcome == transcripts.BLOCKED:
                    st.caption(
                        "Transcript temporarily unavailable (rate limited). "
                        "Try again later."
                    )
                elif outcome == transcripts.ERROR:
                    st.caption(
                        "Transcript could not be read for this video. "
                        "Try again later."
                    )
                else:
                    st.caption("No transcript available for this video.")

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

    if config["run"]:
        _run_search(config)
        # Land on what was just searched instead of everything ever collected.
        st.session_state["keyword_filter"] = list(config["keywords"])

    # The second rule only earns its place when the run summary sits between
    # the two; without it they just fence off an empty strip.
    if st.session_state["last_run"]:
        _run_summary()
        st.divider()

    data = st.session_state["data"]
    if data.empty:
        st.info(
            "Search any product or competitor name to see what people are "
            "saying about it on YouTube."
        )
        return

    data = insights.ensure_columns(data)

    if st.session_state["translation_status"]:
        st.caption(st.session_state["translation_status"])

    _unsaved_work_notice(data)

    # The view sits above the filters: it says what you are looking at, and the
    # filters below narrow it. Full width, matching the type filter row.
    view = st.segmented_control(
        "View",
        [BY_VIDEO_VIEW, ALL_COMMENTS_VIEW, BY_KIND_VIEW, ANALYSIS_VIEW],
        default=BY_VIDEO_VIEW,
        key="main_view",
        label_visibility="collapsed",
        width="stretch",
    )

    data = _keyword_filter(data)
    data = _type_filter(data)
    _text_display(data)
    if data.empty:
        st.info("No comments match the selected keyword(s).")
        return

    if view == ALL_COMMENTS_VIEW:
        _all_comments(data)
    elif view == BY_KIND_VIEW:
        _by_kind(data)
    elif view == ANALYSIS_VIEW:
        _analysis(data)
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
