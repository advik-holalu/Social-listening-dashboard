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
# every run costs a predictable amount of the daily YouTube allowance.
DEFAULT_VIDEOS_PER_KEYWORD = 20
MIN_VIDEOS_PER_KEYWORD = 5
MAX_VIDEOS_PER_KEYWORD = 50
MAX_COMMENTS_PER_VIDEO = 500

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
    st.session_state.setdefault("insight_status", "")
    st.session_state.setdefault("last_run", None)
    st.session_state.setdefault("text_display", SHOW_BOTH)
    st.session_state.setdefault("translation_status", "")
    # Keywords searched in this session. The live report shows these and only
    # these; everything else in the Sheet is reachable as a CSV download.
    st.session_state.setdefault("session_keywords", [])
    st.session_state.setdefault("fetch_error", "")
    st.session_state.setdefault("show_help", False)


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
        _stamp_refresh()
    except SheetsError as exc:
        _LOG.exception("Loading saved comments failed")
        st.session_state["fetch_error"] = (
            f"Could not load your saved comments: {exc}"
        )
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

    # The box and the button share a form so that Enter in the box runs the
    # search, which is what the box says it will do. A plain text input only
    # commits its value on Enter; the run itself hung off the button, so the
    # keystroke did nothing visible. A form submits on Enter by design, and
    # the button is that form's submit, so both routes are the same route.
    with st.sidebar.form("search_form", border=False, enter_to_submit=True):
        # The box starts empty on purpose. A pre-filled value hides the
        # placeholder, and the placeholder is where the guidance lives:
        # people were typing whole questions like "khakra negative feedback"
        # and getting nothing, because this searches YouTube for the words as
        # typed rather than interpreting them.
        keywords_raw = st.text_input(
            "Which keyword do you want to search for on YouTube?",
            value="",
            key="keywords_raw",
            placeholder="Khakra, GO DESi",
            help=(
                "Type product or brand names only, e.g. Khakra, GO DESi. "
                "Separate several with commas. Filtering by sentiment or "
                "feedback type is coming with paid plans."
            ),
        )
        exclude_raw = st.text_input(
            "Exclude keywords",
            value="",
            key="exclude_raw",
            placeholder="recipe, how to make",
            help=(
                "Optional. Videos whose title or description contains any of "
                "these are skipped. Separate several with commas, the same as "
                "above."
            ),
        )
        run = st.form_submit_button(
            "Run Search", type="primary", width="stretch"
        )
    keywords = parse_keywords(keywords_raw)
    exclude = parse_keywords(exclude_raw)

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

    # The Sheet has not been read yet at this point in the run -- the page
    # draws before the fetch on purpose -- so the past-searches list gets a
    # reserved slot here and is filled once the history arrives.
    past_slot = st.sidebar.container()

    _help_section()

    return {
        "keywords": keywords, "exclude": exclude, "run": run,
        "past_slot": past_slot, "match": match, **options,
    }


# The manual is a page of its own, not a dialog: it is long enough to read
# properly, and a reader who wants to check one thing mid-search can go and
# come back with the Back button rather than losing a modal behind a click.
#
# It is laid out as cards across the full width. One narrow column of prose
# down the middle of a wide monitor wastes most of the screen and turns a
# five minute read into a long scroll.
def _help_back(key: str, kind: str = "secondary") -> None:
    if st.button(
        "Back to the app",
        icon=":material/arrow_back:",
        key=key,
        type=kind,
    ):
        st.session_state["show_help"] = False
        st.rerun()


def _help_card(column, title: str, body: str) -> None:
    """One titled card. Cards in a row stretch to a common height."""
    with column:
        with st.container(border=True, height="stretch"):
            st.subheader(title)
            st.markdown(body)


def _help_page() -> None:
    """The user manual. Plain language, no jargon, nothing about the plumbing."""
    # The way out sits top left, where a back control belongs, and carries the
    # same weight as the one at the end of the page.
    _help_back("help_back_top", "primary")

    st.title("How it works")
    st.caption(
        "A guide to everything in this tool. It takes about five minutes to "
        "read, and you only need to read it once."
    )

    st.divider()

    # ---- what it is, and the four steps --------------------------------
    left, right = st.columns(2, gap="medium")
    _help_card(
        left,
        "What this tool is for",
        "People talk about brands and products in YouTube comments all day "
        "long, spread across hundreds of videos. Reading that by hand is "
        "impossible.\n\n"
        "This tool searches YouTube for you, collects the comments from the "
        "videos it finds, and puts them all in one place you can read, filter "
        "and download. You can use it on your own brand, on a competitor, on "
        "a product category, or on anything else people might be talking "
        "about.",
    )
    _help_card(
        right,
        "A search in four steps",
        "1. Type what you want to search for in the sidebar on the left.\n"
        "2. Choose whether your keywords should be searched separately or "
        "together.\n"
        "3. Press Enter, or click **Run Search**, and wait. It usually takes "
        "under a minute.\n"
        "4. Read the results. They are saved for you automatically.\n\n"
        ":gray[Everything else on this page is detail. If you only remember "
        "these four steps, you can use the tool.]",
    )

    st.header("Setting up a search", anchor="setting-up")
    left, right = st.columns(2, gap="medium")
    _help_card(
        left,
        "Choosing your keywords",
        "The box at the top of the sidebar is where you say what to look "
        "for.\n\n"
        "**Separate keywords with commas.** Spaces are part of a keyword, not "
        "a separator. So `Sweet Karam Coffee, SKC, Mysore pak` is three "
        "keywords, not five.\n\n"
        "**Capital letters do not matter.** YouTube treats `imli pop` and "
        "`Imli Pop` the same way.\n\n"
        "**Names, not questions.** Type what a product or brand is called, "
        "not what you want to know about it. `khakra negative feedback` "
        "searches YouTube for those three words together and finds close to "
        "nothing. Search `khakra`, then read the results or use the search "
        "box above them. Filtering by sentiment or feedback type is coming "
        "with paid plans.\n\n"
        "**Think about what real people type.** They shorten names, they "
        "misspell them, and they use nicknames. Adding those as extra "
        "keywords finds comments you would otherwise miss.\n\n"
        "**Only videos that actually say it are read.** YouTube calls a lot "
        "of videos relevant that never mention what you searched for. Those "
        "are dropped, and the summary tells you how many. A video counts as "
        "a match when the words appear in its title or its description.\n\n"
        "**Exclude keywords** is the opposite, and it is optional. Anything "
        "you put there is thrown out: a video is skipped when its title or "
        "its description contains any of those words. Searching `chikki` "
        "while excluding `recipe, how to make` drops the cooking tutorials "
        "and leaves you the people actually eating it.",
    )
    _help_card(
        right,
        "Match any of these, or match all together",
        "This choice changes what gets searched, and it makes the biggest "
        "difference to your results.\n\n"
        f"**{MATCH_ANY_LABEL}** runs a separate search for each keyword and "
        "pools the results. With `GO DESi, imli pop, imly pop` you get videos "
        "for each of the three, whichever way people spell it. Use this when "
        "you are not sure what people call the thing.\n\n"
        f"**{MATCH_ALL_LABEL}** joins your keywords into one search, as "
        "though you typed them all into YouTube at once. `GO DESi imli pop` "
        "finds videos about that specific thing and skips everything that "
        "only matches one word.\n\n"
        "If a search brings back a lot of things that have nothing to do with "
        "you, switch to matching all together. If it brings back almost "
        "nothing, switch to matching any.",
    )

    st.subheader("The rest of the settings", anchor="settings")
    one, two, three, four = st.columns(4, gap="medium")
    _help_card(
        one,
        "Sort videos by",
        "Decides which videos are picked, and the order they appear in.\n\n"
        "- **Most relevant** is YouTube's own idea of the best match, and a "
        "good default.\n"
        "- **Newest first** is for launches and campaigns.\n"
        "- **Most viewed** finds the videos the most people have seen.\n"
        "- **Highest rated** finds the best liked videos.\n"
        "- **Highest comments** puts the busiest comment sections first, "
        "which is usually where the conversation is.",
    )
    _help_card(
        two,
        "Videos to query",
        "How many videos the search reads comments from, in total. Not per "
        "keyword. If you ask for 20 videos with four keywords, the four share "
        "those 20 between them.\n\n"
        "Twenty is a sensible default. Raise it when a topic is busy and you "
        "want more ground covered. More videos takes longer.",
    )
    _help_card(
        three,
        "From the last N days",
        "Only looks at videos published in that window. It starts at 365 "
        "days, which is a year. Set it to 0 to search everything ever "
        "posted.\n\n"
        "This is the age of the **video**, not of the comment. A video from "
        "ten months ago can still be collecting comments today, and you will "
        "get those.",
    )
    _help_card(
        four,
        "Include replies",
        "On by default. Replies are where people argue, correct each other "
        "and answer questions, so they are usually worth having.\n\n"
        "Turn it off if you only want top level comments.",
    )

    st.header("Reading what comes back", anchor="reading")
    left, right = st.columns(2, gap="medium")
    _help_card(
        left,
        "What happens when you press Run Search",
        "The tool finds the videos first, then reads the comments on each "
        "one. A progress bar tells you where it has got to. Leave the tab "
        "open while it runs.\n\n"
        "When it finishes you get two numbers:\n\n"
        "- **New comments found** is how many comments this search added that "
        "you did not already have. Search the same thing twice and this will "
        "be small the second time, because the same comment is never kept "
        "twice.\n"
        "- **Videos searched** is how many videos those comments came "
        "from.\n\n"
        "Under them is a line telling you how many comments you now hold for "
        "this search in total, counting everything collected before.\n\n"
        "A new search replaces the one on screen. The old one is not lost. It "
        "stays in **Past searches** in the sidebar, ready to download.",
    )
    _help_card(
        right,
        "Videos and Shorts",
        f"Above the results is a filter for **{ALL_TYPES}**, "
        f"**{SHORTS_ONLY}** and **{VIDEOS_ONLY}**.\n\n"
        "Shorts are the vertical clips under a minute. They pull a different "
        "crowd and a different tone from long videos, so it is worth looking "
        "at each on its own before you draw a conclusion.",
    )

    left, right = st.columns(2, gap="medium")
    _help_card(
        left,
        BY_VIDEO_VIEW,
        "Groups the comments under the video they came from. Each video is a "
        "section you can open, showing how many comments it has and a link to "
        "watch it on YouTube.\n\n"
        "This is the view for understanding **why** people are saying "
        "something. A run of complaints makes a lot more sense once you can "
        "see the video that prompted them.\n\n"
        "Use the **Videos to show** control at the top to load more sections.",
    )
    _help_card(
        right,
        ALL_COMMENTS_VIEW,
        "Puts every comment in one big table, whatever video it came "
        "from.\n\n"
        "This is the view for scanning quickly, sorting, and downloading. "
        "Sort it with the **Sort comments by** buttons, or click any column "
        "header in the table.\n\n"
        "The small toolbar at the top right of the table lets you search "
        "inside it and make it full screen.",
    )

    st.header("Working with your results", anchor="working")
    one, two, three = st.columns(3, gap="medium")
    _help_card(
        one,
        "Comments in other languages",
        "Plenty of comments come in Hindi, Tamil, Telugu, Kannada, Bengali "
        "and Marathi, and plenty more in those languages typed out in English "
        "letters.\n\n"
        "The tool spots those and offers a **Translate** button. In the "
        "report by video it translates a whole video's comments at once. In "
        "the table you can tick the rows you want and translate only "
        "those.\n\n"
        "A comment is only translated once. After that the English is kept "
        "with it, for you and for everyone else.\n\n"
        "Once something has been translated you get a **Comment text** "
        "choice: the original, the English, or both side by side.",
    )
    _help_card(
        two,
        "Finding something specific",
        "The **Search comments** box above the results filters what you have "
        "already collected. It does not go back to YouTube for more.\n\n"
        "Type a word or a phrase and you get only the comments containing it. "
        "It looks at the English translations as well as the original text, "
        "so searching for `expensive` also finds a comment that said it in "
        "Hindi.\n\n"
        "It is the fastest way to answer a specific question. Search `price` "
        "to see who is complaining about cost, or a flavour name to see what "
        "people think of it. Clear the box to get everything back.",
    )
    _help_card(
        three,
        "Downloading",
        "At the bottom of the results is a button to download what you are "
        "looking at as a CSV file, which opens in Excel. It includes every "
        "column, even the ones the table on screen does not show.\n\n"
        "Whatever you have filtered is what you get. Filter to Shorts and "
        "search for a word, and the download has exactly those comments in "
        "it.",
    )

    left, right = st.columns(2, gap="medium")
    _help_card(
        left,
        "Saving and past searches",
        "You never have to save anything. Every search is stored the moment "
        "it finishes.\n\n"
        "**Past searches** in the sidebar lists everything anyone has "
        "searched before, with the number of comments held for each one. "
        "Click any of them to download it as a CSV file. That is how you get "
        "back to a search from last week, or one a colleague ran.\n\n"
        f"**Only the {sheets_store.KEYWORD_LIMIT} most recent searches are "
        "kept.** When a new keyword takes the list past that, the one nobody "
        "has searched for the longest is deleted along with its comments, and "
        "it cannot be brought back. Searching an old keyword again makes it "
        "recent, so it is safe. Download anything you want to keep for "
        "good.\n\n"
        "The **Reload saved results** button at the very bottom of the page "
        "fetches the latest stored comments. Press it if someone else has "
        "been running searches while you had the tab open.",
    )
    _help_card(
        right,
        "Tips for a good search",
        "- **Start wide, then narrow.** Search the brand name on its own "
        "first to see what is out there, then add keywords to focus.\n"
        "- **Search how people talk, not how the pack is printed.** "
        "Nicknames and misspellings find real conversations.\n"
        "- **Sort by Highest comments** when you want opinions. A video with "
        "500 comments tells you more than ten videos with 3.\n"
        "- **Look at Shorts separately.** The audience and the tone are "
        "usually not the same.\n"
        "- **Search your competitors too.** The complaints under their videos "
        "are the openings for you.",
    )

    st.header("If something looks wrong", anchor="troubleshooting")
    one, two, three = st.columns(3, gap="medium")
    _help_card(
        one,
        "No videos matched",
        "Your keywords may be too specific together. Try matching any of them "
        "instead of all together, use fewer words, or widen the number of "
        "days.",
    )
    _help_card(
        two,
        "A message about YouTube stopping for today",
        "There is a daily limit on how much can be collected, shared by "
        "everyone using the tool. Anything already collected is saved, and "
        "the limit clears overnight.",
    )
    _help_card(
        three,
        "A video you expected is missing",
        "The tool only reads the videos your settings asked for. Raise Videos "
        "to query, or change how they are sorted.",
    )

    one, two = st.columns(2, gap="medium")
    _help_card(
        one,
        "Fewer comments than YouTube shows",
        "Some channels turn comments off, and some comments get deleted. Very "
        "busy videos are read up to a limit rather than to the last comment.",
    )
    _help_card(
        two,
        "Something else",
        "Take a screenshot of the message and send it to whoever set the tool "
        "up for you.",
    )

    st.divider()
    _help_back("help_back_bottom", "primary")


def _help_section() -> None:
    """The way into the help, at the bottom of the sidebar."""
    st.sidebar.divider()
    if st.sidebar.button(
        "How it works",
        icon=":material/help:",
        use_container_width=True,
        key="how_it_works",
    ):
        st.session_state["show_help"] = True
        st.rerun()

    if not sheets_store.is_configured():
        st.sidebar.caption(
            "Results are not being saved right now, so they will be gone when "
            "you close this tab."
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
            "How many videos this search reads comments from in total, "
            "shared across your keywords. More videos takes longer."
        ),
    )

    # A nudge, not an error: going higher is allowed, it just costs time and
    # eats into what everyone shares for the day.
    if int(videos_per_keyword) > DEFAULT_VIDEOS_PER_KEYWORD:
        st.sidebar.warning(
            f"We recommend keeping this at {DEFAULT_VIDEOS_PER_KEYWORD}. "
            "Going higher searches more videos, which takes longer and can "
            "use up daily limits faster.",
            icon=":material/warning:",
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
        videos, report = youtube_fetcher.find_videos(
            api_key=api_key,
            keywords=config["keywords"],
            max_videos=config["videos_per_keyword"],
            order=config["order"],
            published_after=config["published_after"],
            match=config["match"],
            exclude=config["exclude"],
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

    st.session_state["last_report"] = report

    if report.quota_exhausted:
        st.warning(
            "YouTube stopped returning results part way through, so this is "
            "only some of what is out there. Try again tomorrow."
        )
    if report.warnings:
        with st.expander(f"{len(report.warnings)} notice(s) from this search"):
            for warning in report.warnings:
                st.write(f"- {warning}")

    if not videos:
        progress.empty()
        status.empty()
        if report.videos_excluded and not report.videos_irrelevant:
            st.info(
                f"All {report.videos_excluded:,} video(s) found were skipped "
                "by your excluded words. Remove one of them, or search for "
                "something else."
            )
        elif report.videos_irrelevant:
            st.info(
                f"{report.videos_irrelevant:,} video(s) came back, but none "
                "of them mentioned your keywords in the title or "
                "description, so none were read. Try a shorter keyword, or "
                "match any of them instead of all together."
            )
        else:
            st.info("No videos matched. Try a different keyword.")
        return

    # Straight on to the comments, reusing the same progress bar so the run
    # reads as one continuous job rather than two.
    _fetch_and_save(
        videos,
        {
            # What the rows are filed under: the queries actually sent, which
            # in All mode is the single joined string.
            "keywords": youtube_fetcher.build_queries(
                config["keywords"], config["match"]
            ),
            "include_replies": config["include_replies"],
            "excluded": report.videos_excluded,
            "irrelevant": report.videos_irrelevant,
        },
        on_progress,
    )

    progress.empty()
    status.empty()


def _fetch_and_save(videos: list[dict], config: dict, on_progress) -> None:
    """Pull the comments for every video the search found, then store them."""
    api_key = str(st.secrets.get("YOUTUBE_API_KEY", "")).strip()

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

    added = _merge(rows)
    st.session_state["last_report"] = report
    _stamp_refresh()

    written = 0
    retired: dict[str, int] = {}
    if rows and sheets_store.is_configured():
        try:
            with st.spinner("Saving your results..."):
                written, _already = sheets_store.append_rows(rows)
            _fetch_history.clear()
        except SheetsError as exc:
            _LOG.exception("Saving the search failed")
            st.session_state["fetch_error"] = (
                f"The comments were fetched but could not be saved: {exc}"
            )
        else:
            # Housekeeping, in its own hands: the comments are already safely
            # stored, so a problem here is for the log, not for the reader.
            try:
                with st.spinner("Tidying up old searches..."):
                    # Stamped whether or not anything was written: a repeat
                    # search that finds no new comments still counts as a
                    # fresh search, and that is what keeps it out of the
                    # cleanup that follows.
                    sheets_store.record_search(config["keywords"])
                    retired = sheets_store.apply_retention()
                if retired:
                    _fetch_history.clear()
            except SheetsError as exc:
                _LOG.exception("Keeping the Sheet to its keyword limit failed")

    for keyword in config["keywords"]:
        if keyword not in st.session_state["session_keywords"]:
            st.session_state["session_keywords"].append(keyword)

    st.session_state["last_run"] = {
        "comments": report.comments_fetched,
        "videos": report.videos_with_comments or report.videos_searched,
        "added": added,
        "written": written,
        "keywords": list(config["keywords"]),
        "excluded": int(config.get("excluded", 0)),
        "irrelevant": int(config.get("irrelevant", 0)),
        "retired": retired,
        "ids": {str(row.get("comment_id", "")) for row in rows},
    }


def _run_summary() -> None:
    """What this search just added, and nothing else at the same weight.

    Two cards, both about this run. The running total sits below them as a
    line of text: side by side with the new count it read as a rival figure,
    and people took the bigger one for the result of their search.
    """
    last = st.session_state["last_run"]
    if not last:
        return

    saved = sheets_store.is_configured()
    # Without a store nothing is written, so "new" means new to this session.
    found = int(last.get("written", 0)) if saved else int(last.get("added", 0))

    one, two = st.columns(2)
    one.metric("New comments found", f"{found:,}")
    two.metric("Videos searched", f"{last['videos']:,}")

    total = len(_live_rows(insights.ensure_columns(st.session_state["data"])))
    if total:
        st.caption(
            f"You now have {total:,} total comments "
            + ("saved for this search." if saved else "for this search.")
        )

    skipped = int(last.get("excluded", 0))
    if skipped:
        st.caption(
            f"{skipped:,} video(s) skipped because they matched your excluded "
            "words."
        )

    off_topic = int(last.get("irrelevant", 0))
    if off_topic:
        st.caption(
            f"{off_topic:,} video(s) dropped because your keywords did not "
            "appear in the title or description."
        )

    retired = last.get("retired") or {}
    if retired:
        names = ", ".join(sorted(retired))
        rows = sum(retired.values())
        st.caption(
            f"Room is kept for the {sheets_store.KEYWORD_LIMIT} most recent "
            f"searches, so the oldest {len(retired)} ({names}) and their "
            f"{rows:,} comments were removed."
        )


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

# Comments per translation call when a Translate button is pressed.
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


def video_open_key(video_id: str) -> str:
    """Session key holding whether one video's section is open.

    A keyed expander keeps its own open state in session state, which is the
    point: translating reruns the script, and without a key the section the
    reader was inside collapses and has to be found and opened again.
    """
    return f"video_open_{video_id}"


def _keep_open(open_key: str | None) -> None:
    """Leave a video's section open across the rerun that is about to happen."""
    if open_key:
        st.session_state[open_key] = True


def _translate_video(rows: pd.DataFrame) -> None:
    """Translate every untranslated non-English comment on one video."""
    ids = [
        str(record["comment_id"])
        for _, record in rows.iterrows()
        if insights.needs_translation(record)
    ]
    if ids:
        _translate_ids(ids)


def _translate_button(
    rows: pd.DataFrame, video_id: str, open_key: str | None = None
) -> None:
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
        _keep_open(open_key)
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


def _translate_selected(
    df: pd.DataFrame, key: str, open_key: str | None = None
) -> None:
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
        _keep_open(open_key)
        st.rerun()


def _comment_table(
    df: pd.DataFrame, height: int = 560, key: str | None = None,
    selectable: bool = True, open_key: str | None = None,
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
        _translate_selected(df, key, open_key)


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
        open_key = video_open_key(str(video_id))
        # Set before the expander is made, which is the only moment a widget's
        # state can be written. Closed unless something asked for it to stay.
        st.session_state.setdefault(open_key, False)

        with st.expander(f"{title[:90]} - {count:,} comment(s)", key=open_key):
            rows = df[df["video_id"] == video_id]
            url = str(rows["video_url"].iloc[0]) if "video_url" in rows else ""
            if url:
                st.caption(f"[Watch on YouTube]({url})")

            _translate_button(rows, str(video_id), open_key)

            # Short videos get a short table rather than a fixed pane of blank.
            height = min(560, 90 + 40 * max(len(rows), 1))
            _comment_table(
                rows, height=height, key=f"table_{video_id}", open_key=open_key
            )


# --------------------------------------------------------------------------
# Main -- one view, no tabs
# --------------------------------------------------------------------------
def main() -> None:
    _init_state()

    # The manual takes the whole page, sidebar included, so nothing competes
    # with it and nobody starts a search by accident while reading.
    if st.session_state["show_help"]:
        _help_page()
        return

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
        # A new search replaces what you were looking at, rather than piling
        # onto the previous keyword's report.
        st.session_state["last_run"] = None
        st.session_state["session_keywords"] = []
        # The find-within box belongs to the results it was typed against.
        # Left alone it would quietly filter the next keyword's comments by
        # the last one's word, and an empty report looks like a failed search.
        # Safe to set here: the box is drawn further down this same run.
        st.session_state["comment_search"] = ""
        _run_search(config)

    if st.session_state["fetch_error"]:
        st.error(st.session_state["fetch_error"])
        st.session_state["fetch_error"] = ""

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
        if st.button("Reload saved results"):
            st.session_state["loaded_from_sheet"] = False
            st.rerun()


if __name__ == "__main__":
    main()
