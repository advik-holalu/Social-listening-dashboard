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

import altair as alt

import analysis
import classify
import insights
import projects
import reddit_fetcher
import relevance
import usage
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
# What each ordering sorts the source sections on. "Most viewed" and
# "Highest rated" read the shared engagement column and the metrics blob
# respectively, so a second platform sorts without new columns.
# Two orderings read a number that is not a column of its own any more: it
# lives in platform_metrics, under a name that differs per platform. These
# stand in for a column name and are resolved per row.
_METRIC_LIKES = "metric:likes"
_METRIC_COMMENTS = "metric:comments"

VIDEO_SORT_COLUMNS = {
    "Newest first": "source_published_at",
    "Most viewed": "engagement_score",
    "Highest rated": _METRIC_LIKES,
    "Highest comments": _METRIC_COMMENTS,
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
    st.session_state.setdefault("show_usage", False)
    # What Claude made of a typed request: the filter it implies, and
    # anything it asked for that a search cannot give.
    st.session_state.setdefault("intent", {})
    st.session_state.setdefault("intent_note", "")
    # Written summaries, keyed by the fingerprint of the set they describe.
    st.session_state.setdefault("digests", {})
    # The project currently open, and the saved list as last read.
    st.session_state.setdefault("open_project", "")
    st.session_state.setdefault("projects", None)
    st.session_state.setdefault("project_note", "")


def _stamp_refresh() -> None:
    st.session_state["last_refreshed"] = _dt.datetime.now()


# How long a Sheets read is reused before going back to the network. Long
# enough that reopening the app or refreshing the tab is instant, short enough
# that a search run in another tab shows up quickly.
HISTORY_TTL_SECONDS = 60


@st.cache_data(ttl=HISTORY_TTL_SECONDS, show_spinner=False)
def _fetch_projects() -> list:
    """Saved projects, cached like everything else the Sheet holds."""
    return sheets_store.load_projects()


def _known_projects() -> list:
    """The projects list, read once per session unless something changed it."""
    if st.session_state["projects"] is None:
        st.session_state["projects"] = (
            _fetch_projects() if sheets_store.is_configured() else []
        )
    return st.session_state["projects"]


def _remember(project: dict) -> None:
    """Store a project and put it at the top of the list in hand.

    The copy kept in session carries the same stamps the row does, so the
    list reads the same before and after a reload.
    """
    sheets_store.save_project(project)
    stamped = dict(projects.from_row(
        projects.PROJECT_COLUMNS, projects.to_row(project)
    ) or project)
    project.update(stamped)
    _fetch_projects.clear()
    others = [
        p for p in (st.session_state["projects"] or [])
        if p["project_id"] != project["project_id"]
    ]
    st.session_state["projects"] = [project] + others


@st.cache_data(ttl=HISTORY_TTL_SECONDS, show_spinner=False)
def _fetch_digests() -> dict:
    """Stored summaries, cached like the comments themselves."""
    return sheets_store.load_digests()


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
        # A fresh load lands on the last thing collected rather than on an
        # empty page. Anything else stored stays a CSV in Past searches, and
        # the moment a search runs it takes over.
        if not st.session_state["session_keywords"]:
            latest = most_recent_keyword(stored)
            if latest:
                st.session_state["session_keywords"] = [latest]
        # Summaries written in an earlier session, so a refresh does not send
        # an unchanged set to Claude again.
        st.session_state["digests"] = {
            **_fetch_digests(), **st.session_state["digests"]
        }
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

    # The platform choice lives above the report, not here, because it says
    # what you are looking at as well as what to collect. This panel only
    # shows the options that apply to what is selected.
    platforms = chosen_platforms()
    youtube_on = YOUTUBE in platforms
    reddit_on = REDDIT in platforms

    # The box and the button share a form so that Enter in the box runs the
    # search, which is what the box says it will do. A plain text input only
    # commits its value on Enter; the run itself hung off the button, so the
    # keystroke did nothing visible. A form submits on Enter by design, and
    # the button is that form's submit, so both routes are the same route.
    # A request typed in full is replaced by the keyword it parsed to, so the
    # box shows what was actually searched. Written before the widget exists,
    # which is the only moment its state can be set.
    filled = st.session_state.pop("pending_keyword", "")
    if filled:
        st.session_state["keywords_raw"] = filled

    with st.sidebar.form("search_form", border=False, enter_to_submit=True):
        # The box starts empty on purpose. A pre-filled value hides the
        # placeholder, and the placeholder is where the guidance lives:
        # people were typing whole questions like "khakra negative feedback"
        # and getting nothing, because this searches YouTube for the words as
        # typed rather than interpreting them.
        keywords_raw = st.text_input(
            "Which keyword do you want to search for?",
            value="",
            key="keywords_raw",
            placeholder="Khakra, GO DESi",
            help=(
                "Type product or brand names only, e.g. Khakra, GO DESi. "
                "Separate several with commas. Filtering by sentiment or "
                "feedback type is coming with paid plans."
            ),
        )
        exclude_raw = ""
        if youtube_on:
            exclude_raw = st.text_input(
                "Exclude keywords (YouTube)",
                value="",
                key="exclude_raw",
                placeholder="recipe, how to make",
                help=(
                    "Optional, and YouTube only. Videos whose title or "
                    "description contains any of these are skipped. Reddit "
                    "results are filtered by reading them instead."
                ),
            )
        run = st.form_submit_button(
            "Run Search", type="primary", width="stretch"
        )
    keywords = parse_keywords(keywords_raw)
    exclude = parse_keywords(exclude_raw)

    if not platforms:
        st.sidebar.warning(
            "No platform is selected. Pick one above the report.",
            icon=":material/warning:",
        )

    if reddit_on:
        _reddit_notes()

    if not youtube_on:
        # Reddit alone: none of what follows applies to it, so none of it is
        # shown. Defaults stand in for the values the config still carries.
        past_slot = st.sidebar.container()
        _help_section()
        return {
            "platforms": platforms, "keywords": keywords, "exclude": [],
            "run": run, "past_slot": past_slot,
            "match": youtube_fetcher.MATCH_ALL,
            "order_label": list(ORDER_OPTIONS)[0],
            "order": "relevance",
            "videos_per_keyword": DEFAULT_VIDEOS_PER_KEYWORD,
            "published_after": None,
            "include_replies": True,
        }

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
        "platforms": platforms, "keywords": keywords, "exclude": exclude,
        "run": run, "past_slot": past_slot, "match": match, **options,
    }


def _reddit_notes() -> None:
    """What Reddit collection costs and how its results are filtered."""
    queries = relevance.DEFAULT_VARIANTS if relevance.is_configured() else 1
    cost = queries * reddit_fetcher.COST_PER_QUERY_USD
    searches = int(reddit_fetcher.MONTHLY_CREDIT_USD / cost) if cost else 0
    st.sidebar.caption(
        f"Reddit: up to {reddit_fetcher.POSTS_PER_QUERY} posts per query, "
        f"{queries} quer{'y' if queries == 1 else 'ies'} per search. That is "
        f"about ${cost:.2f} a search, or roughly {searches:,} searches a "
        "month within the plan's credit."
    )
    if relevance.is_configured():
        st.sidebar.caption(
            f"Your keyword is searched {relevance.DEFAULT_VARIANTS} ways, "
            "aimed at food, and posts about something else that shares the "
            "word are read and dropped."
        )
    else:
        st.sidebar.warning(
            "Reddit is searched site-wide and its search is loose. Without an "
            "Anthropic key the results cannot be filtered, so expect posts "
            "that only share the word.",
            icon=":material/warning:",
        )


# The manual is a page of its own, not a dialog: it is long enough to read
# properly, and a reader who wants to check one thing mid-search can go and
# come back with the Back button rather than losing a modal behind a click.
#
# It is laid out as cards across the full width. One narrow column of prose
# down the middle of a wide monitor wastes most of the screen and turns a
# five minute read into a long scroll.
def _back_button(key: str, flag: str, kind: str = "secondary") -> None:
    """The way out of a full-page view, shared by all of them."""
    if st.button(
        "Back to the app",
        icon=":material/arrow_back:",
        key=key,
        type=kind,
    ):
        st.session_state[flag] = False
        st.rerun()


def _help_back(key: str, kind: str = "secondary") -> None:
    _back_button(key, "show_help", kind)


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


@st.cache_data(ttl=60, show_spinner=False)
def _apify_account() -> dict:
    """Apify's own numbers. It publishes them, so they are pulled, not counted."""
    try:
        client = reddit_fetcher.build_client()
        limits = client.user("me").limits()
        data = limits if isinstance(limits, dict) else limits.model_dump()
        cycle = data.get("monthly_usage_cycle", {})
        runs = client.actor(reddit_fetcher.ACTOR).runs().list(limit=200).items
        start = str(cycle.get("start_at", ""))[:10]
        this_cycle = [r for r in runs if str(r.started_at)[:10] >= start]
        return {
            "ok": True,
            "used": float(data.get("current", {}).get("monthly_usage_usd", 0)),
            "limit": float(data.get("limits", {}).get("max_monthly_usage_usd", 0)),
            "cycle_start": start,
            "cycle_end": str(cycle.get("end_at", ""))[:10],
            "runs": len(this_cycle),
            "spent_on_runs": sum(
                float(getattr(r, "usage_total_usd", 0) or 0) for r in this_cycle
            ),
        }
    except Exception as exc:
        _LOG.warning("Could not read the Apify account: %s", exc)
        return {"ok": False, "error": str(exc)}


def _usage_page() -> None:
    """Live operational numbers, for whoever runs this.

    Each section says where its number comes from, because they do not come
    from the same kind of place: one is published by the provider, two are
    counted by this app because the provider does not publish them.
    """
    _back_button("usage_back_top", "show_usage", "primary")

    st.title("Usage")
    st.caption(
        "Internal. API spend and quota for whoever administers this app, not "
        "something a person searching comments needs to see."
    )
    st.divider()

    log = sheets_store.load_usage() if sheets_store.is_configured() else []

    # ---- YouTube ------------------------------------------------------
    st.header("YouTube quota", anchor="youtube")
    today = usage.summarise(log, since=usage.today(), service=usage.YOUTUBE)
    used = int(today["units"])
    left = max(usage.YOUTUBE_DAILY_LIMIT - used, 0)
    one, two, three = st.columns(3)
    one.metric("Units used today", f"{used:,}")
    two.metric("Of the daily limit", f"{usage.YOUTUBE_DAILY_LIMIT:,}")
    three.metric("Units left", f"{left:,}")
    st.progress(min(used / usage.YOUTUBE_DAILY_LIMIT, 1.0))
    st.caption(
        ":gray[Counted by this app, from the calls it makes, priced at "
        "YouTube's published costs: 100 units a search page, 1 a batched "
        "video-details call, 1 a page of comments. The Data API does not "
        "report usage, and the Cloud Monitoring metric that would is refused "
        "to this service account, so counting the calls is the accurate "
        "number rather than an estimate of one. Quota resets at midnight "
        "Pacific.]"
    )
    if today["by_action"]:
        st.caption(
            "Today: " + ", ".join(
                f"{name} {int(v['units']):,} units"
                for name, v in sorted(today["by_action"].items())
            )
        )

    # ---- Apify --------------------------------------------------------
    st.divider()
    st.header("Apify", anchor="apify")
    account = _apify_account()
    if not account.get("ok"):
        st.warning(f"Could not reach the Apify account: {account.get('error', '')}")
    else:
        left_usd = max(account["limit"] - account["used"], 0)
        one, two, three = st.columns(3)
        one.metric("Spent this cycle", f"${account['used']:.2f}")
        two.metric("Plan credit", f"${account['limit']:.2f}")
        three.metric("Reddit searches", f"{account['runs']:,}")
        st.progress(min(account["used"] / max(account["limit"], 1), 1.0))
        st.caption(
            f":gray[Pulled live from Apify, which publishes account spend. "
            f"Cycle {account['cycle_start']} to {account['cycle_end']}, "
            f"${left_usd:.2f} left. The searches figure counts actor runs in "
            f"this cycle, ${account['spent_on_runs']:.2f} of the total.]"
        )

    # ---- Claude -------------------------------------------------------
    st.divider()
    st.header("Claude", anchor="claude")
    claude = usage.summarise(log, service=usage.CLAUDE)
    cycle_start = (account.get("cycle_start") or "") if account.get("ok") else ""
    this_cycle = usage.summarise(log, since=cycle_start, service=usage.CLAUDE)
    one, two, three = st.columns(3)
    one.metric("Since the log began", f"${claude['cost']:.2f}")
    two.metric("This cycle", f"${this_cycle['cost']:.2f}")
    three.metric("Calls", f"{claude['events']:,}")
    st.warning(
        "This is a running total this app keeps itself, not a live figure "
        "from Anthropic. Anthropic's usage and cost reports need an Admin API "
        "key, which this app's key is refused for, so account spend cannot be "
        "read. Each call is priced from the token counts it returns, at the "
        "model's published rates, and summed. It counts only what this app "
        "spends: anything else on the same key is invisible here.",
        icon=":material/info:",
    )
    if claude["by_action"]:
        rows = pd.DataFrame(
            [
                {
                    "Feature": name,
                    "Calls": int(v["events"]),
                    "Tokens": int(v["units"]),
                    "Cost": f"${v['cost']:.4f}",
                }
                for name, v in sorted(
                    claude["by_action"].items(), key=lambda kv: -kv[1]["cost"]
                )
            ]
        )
        st.dataframe(rows, width="stretch", hide_index=True)
    rates = ", ".join(
        f"{model} ${p['input']:.2f} in / ${p['output']:.2f} out per million"
        for model, p in usage.PRICING.items()
        if model == relevance.MODEL
    )
    st.caption(f":gray[Priced at {rates}.]")

    # ---- Translation --------------------------------------------------
    st.divider()
    st.header("Translation", anchor="translation")
    month = usage.summarise(log, since=usage.month_start(), service=usage.TRANSLATE)
    chars = int(month["units"])
    left_chars = max(usage.TRANSLATE_FREE_CHARS - chars, 0)
    low, high = usage.COMMENT_CHARS
    one, two, three = st.columns(3)
    one.metric("Characters this month", f"{chars:,}")
    two.metric("Free tier", f"{usage.TRANSLATE_FREE_CHARS:,}")
    three.metric("Characters left", f"{left_chars:,}")
    st.progress(min(chars / usage.TRANSLATE_FREE_CHARS, 1.0))
    st.caption(
        f"Roughly {left_chars // high:,} to {left_chars // low:,} more "
        f"comments this month, at {low} to {high} characters each. "
        f"{month['events']:,} call(s) so far."
    )
    st.warning(
        "This is a running total this app keeps itself, not a live figure "
        "from Google. Translation usage is not readable with the credentials "
        "here: Cloud Monitoring and Service Usage both refuse this service "
        "account, and the Cloud Billing API is not enabled on the project. "
        "Characters are counted as they are sent, which is how Google bills "
        "them. The free tier resets on the first of the month.",
        icon=":material/info:",
    )

    if not log:
        st.info(
            "Nothing logged yet. The counters fill as searches, "
            "classification and summaries run."
        )

    st.divider()
    _back_button("usage_back_bottom", "show_usage", "primary")


def _help_section() -> None:
    """The two pages out of the app, as a pair at the bottom of the sidebar."""
    st.sidebar.divider()
    if st.sidebar.button(
        "How it works",
        icon=":material/help:",
        use_container_width=True,
        key="how_it_works",
    ):
        st.session_state["show_help"] = True
        st.rerun()

    # Directly below and the same shape, but secondary: this one is for
    # whoever runs the app, not for the person using it.
    if st.sidebar.button(
        "Usage",
        icon=":material/monitoring:",
        use_container_width=True,
        key="show_usage_button",
        help="Live API spend and quota. For whoever administers this app.",
    ):
        st.session_state["show_usage"] = True
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
    """One search action, however many platforms are selected.

    Each platform collects on its own terms and the rows are stored together,
    so one run produces one summary and one report rather than two of each.
    """
    platforms = config.get("platforms") or []
    if not platforms:
        st.warning("Select at least one platform above the report.")
        return
    if not config["keywords"]:
        st.warning("Enter at least one keyword to search.")
        return

    config = _understand_request(config)
    if not config["keywords"]:
        return

    collected: list[dict] = []
    reports = []
    extras = {"excluded": 0, "irrelevant": 0, "off_topic_posts": 0}

    if YOUTUBE in platforms:
        result = _youtube_rows(config)
        if result is not None:
            rows, report = result
            collected.extend(rows)
            reports.append(report)
            extras["excluded"] += report.videos_excluded
            extras["irrelevant"] += report.videos_irrelevant

    if REDDIT in platforms:
        result = _reddit_rows(config)
        if result is not None:
            rows, report = result
            collected.extend(rows)
            reports.append(report)
            extras["off_topic_posts"] += report.posts_off_topic

    if not reports:
        return

    merged = youtube_fetcher.FetchReport()
    for report in reports:
        merged.videos_searched += report.videos_searched
        merged.videos_with_comments += report.videos_with_comments
        merged.comments_fetched += report.comments_fetched
        merged.quota_exhausted = merged.quota_exhausted or report.quota_exhausted
        for warning in report.warnings:
            merged.warn(warning)

    if not collected:
        st.info("Nothing came back. Try a different keyword.")
        return

    # Scope the report to what the rows actually say they are filed under, not
    # to what was typed. Matching all together files them under the joined
    # query, so using the typed keywords would leave the report empty.
    filed = list(dict.fromkeys(str(row.get("keyword", "")) for row in collected))
    _save_rows(collected, merged, [f for f in filed if f], extras)


def _understand_request(config: dict) -> dict:
    """Turn a typed request into a keyword, and note what it also asked for.

    Only runs when the text reads as a request rather than a name, so an
    ordinary search never pays for it. Whatever it cannot do is said plainly
    rather than silently dropped.
    """
    st.session_state["intent"] = {}
    st.session_state["intent_note"] = ""

    raw = " ".join(config["keywords"])
    if not relevance.is_configured() or not relevance.looks_like_a_request(raw):
        return config

    with st.spinner("Reading your request..."):
        read = relevance.understand(raw)
    if not read["parsed"]:
        return config

    keyword = read["keyword"].strip()
    if not keyword:
        st.warning(
            f"Could not find a product or brand name in {raw!r}. Try the name "
            "on its own."
        )
        config = dict(config)
        config["keywords"] = []
        return config

    config = dict(config)
    config["keywords"] = parse_keywords(keyword)
    # Fill the box with it on the next run, so what is on screen is what ran.
    st.session_state["pending_keyword"] = keyword

    intent = {k: read[k] for k in ("sentiment", "kind") if read[k]}
    if intent:
        st.session_state["intent"] = {**intent, "raw": raw}

    parts = [f"Searched for **{keyword}**."]
    if intent:
        parts.append(
            "Wanted: " + " and ".join(f"{v} {k}" for k, v in intent.items()) + "."
        )
    if read["unfulfilled"]:
        parts.append(f"Cannot do: {read['unfulfilled'].rstrip('.')}.")
    st.session_state["intent_note"] = " ".join(parts)
    return config


def _intent_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Apply a sentiment or kind the request asked for, when it can be.

    Those labels come from classification, so they cannot narrow comments
    nobody has read yet. When none of what is in view is labelled, the filter
    is not silently skipped: it says what it is waiting for.
    """
    intent = st.session_state.get("intent") or {}
    wanted = {k: v for k, v in intent.items() if k in ("sentiment", "kind")}
    if df.empty or not wanted:
        return df

    asked = " and ".join(f"{value} {name}" for name, value in wanted.items())
    labelled = classify.ensure_columns(df)
    known = labelled[labelled[classify.KIND_COLUMN] != ""]

    if known.empty:
        st.info(
            f"You asked for {asked}, which comes from classifying the "
            "comments. None of these have been classified yet, so this is "
            "every comment for the keyword. Press Classify below, then the "
            "filter applies itself.",
            icon=":material/info:",
        )
        return df

    keep = known
    for name, value in wanted.items():
        column = (classify.SENTIMENT_COLUMN if name == "sentiment"
                  else classify.KIND_COLUMN)
        keep = keep[keep[column] == value]

    unread = len(labelled) - len(known)
    line = f"Showing {len(keep):,} {asked} comment(s)."
    if unread:
        line += (
            f" {unread:,} more have not been classified yet and are not "
            "counted; press Classify below to include them."
        )
    left, right = st.columns([4, 1], vertical_alignment="center")
    left.caption(line)
    if right.button("Clear filter", key="clear_intent", width="stretch"):
        st.session_state["intent"] = {}
        st.rerun()
    return keep


def _youtube_rows(config: dict):
    """Search YouTube and read the comments. None when it could not run."""
    api_key = str(st.secrets.get("YOUTUBE_API_KEY", "")).strip()
    if not api_key:
        st.error(
            "No `YOUTUBE_API_KEY` found in secrets. Copy "
            "`.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` "
            "and add your key."
        )
        return None

    progress = st.progress(0.0)
    status = st.empty()

    def on_progress(current: int, total: int, label: str) -> None:
        progress.progress(min(1.0, current / max(total, 1)))
        status.markdown(f"{label}")

    # The same expansion Reddit uses, read here as context rather than as
    # extra queries: it says which sense of a generic name was meant, and the
    # strict filter then requires the video to carry that sense too.
    context = []
    if relevance.is_configured() and len(config["keywords"]) == 1:
        variants = relevance.expand_keyword(config["keywords"][0])
        context = youtube_fetcher.context_terms(config["keywords"], variants)
        if context:
            _LOG.info("YouTube relevance context: %s", context)

    try:
        videos, report = youtube_fetcher.find_videos(
            api_key=api_key,
            keywords=config["keywords"],
            max_videos=config["videos_per_keyword"],
            order=config["order"],
            published_after=config["published_after"],
            match=config["match"],
            exclude=config["exclude"],
            context=context,
            progress_cb=on_progress,
        )
    except InvalidAPIKeyError as exc:
        progress.empty()
        status.empty()
        st.error(str(exc))
        return None
    except QuotaExceededError as exc:
        progress.empty()
        status.empty()
        st.error(str(exc))
        return None
    except YouTubeError as exc:
        progress.empty()
        status.empty()
        st.error(f"Search failed: {exc}")
        return None

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
                "passed the relevance check: your keyword was missing from "
                "the title and description, or appeared with nothing to say "
                "it was about food. Try a more specific keyword, or match any "
                "of them instead of all together."
            )
        elif YOUTUBE in (config.get("platforms") or []) and len(
            config.get("platforms") or []
        ) == 1:
            st.info("No videos matched. Try a different keyword.")
        return None

    # Straight on to the comments, reusing the same progress bar so the run
    # reads as one continuous job rather than two.
    rows, comment_report = _fetch_comments(
        videos,
        {
            # What the rows are filed under: the queries actually sent, which
            # in All mode is the single joined string.
            "keywords": youtube_fetcher.build_queries(
                config["keywords"], config["match"]
            ),
            "include_replies": config["include_replies"],
        },
        on_progress,
    )

    progress.empty()
    status.empty()

    if rows is None:
        return None
    spent = report.quota_units + comment_report.quota_units
    usage.record_youtube(spent, "search", ", ".join(config["keywords"])[:80])
    # The counts belong to the search half; the comments half knows nothing
    # about what was filtered out before it ran.
    comment_report.videos_excluded = report.videos_excluded
    comment_report.videos_irrelevant = report.videos_irrelevant
    return rows, comment_report


def _reddit_rows(config: dict):
    """Search Reddit and read the posts. None when it could not run.

    One actor call per query brings back posts with their comments already
    attached, so there is no separate comment fetch.
    """
    if not reddit_fetcher.is_configured():
        st.error(
            f"No `{reddit_fetcher.SECRET_TOKEN}` found in secrets, so Reddit "
            "cannot be searched."
        )
        return None

    keyword = config["keywords"][0] if config["keywords"] else ""
    if not keyword:
        return None

    progress = st.progress(0.0)
    status = st.empty()

    def on_progress(current: int, total: int, label: str) -> None:
        progress.progress(min(1.0, current / max(total, 1)))
        status.markdown(label)

    try:
        rows, report = reddit_fetcher.fetch(
            keyword=keyword,
            max_posts=reddit_fetcher.POSTS_PER_QUERY,
            progress_cb=on_progress,
        )
    except reddit_fetcher.RedditError as exc:
        progress.empty()
        status.empty()
        _LOG.exception("Reddit search failed")
        st.error(str(exc))
        return None
    finally:
        progress.empty()
        status.empty()

    if report.warnings:
        with st.expander(f"{len(report.warnings)} notice(s) from Reddit"):
            for warning in report.warnings:
                st.write(f"- {warning}")

    return rows, report


def _fetch_comments(videos: list[dict], config: dict, on_progress):
    """Pull the comments for every video the search found. None on failure."""
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
        return None, None

    return rows, report


def _save_rows(
    rows: list[dict], report, keywords: Sequence[str], extras: dict
) -> None:
    """Store fetched rows and record the run, whatever platform they came from.

    Everything here works on the shared schema, so auto-save, the keyword
    retention limit and the run summary behave the same for both platforms.
    """
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
                    sheets_store.record_search(keywords)
                    retired = sheets_store.apply_retention()
                if retired:
                    _fetch_history.clear()
            except SheetsError as exc:
                _LOG.exception("Keeping the Sheet to its keyword limit failed")

    for keyword in keywords:
        if keyword not in st.session_state["session_keywords"]:
            st.session_state["session_keywords"].append(keyword)

    st.session_state["last_run"] = {
        "comments": report.comments_fetched,
        "videos": report.videos_with_comments or report.videos_searched,
        "added": added,
        "written": written,
        "keywords": list(keywords),
        "excluded": int(extras.get("excluded", 0)),
        "irrelevant": int(extras.get("irrelevant", 0)),
        "off_topic_posts": int(extras.get("off_topic_posts", 0)),
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

    not_food = int(last.get("off_topic_posts", 0))
    if not_food:
        st.caption(
            f"{not_food:,} post(s) dropped after reading them: they turned "
            "out to be about something else that shares your keyword."
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

YOUTUBE = "YouTube"
REDDIT = "Reddit"
PLATFORM_LABELS = {
    YOUTUBE: youtube_fetcher.PLATFORM_YOUTUBE,
    REDDIT: reddit_fetcher.PLATFORM,
}

# What one source is called on each platform, for a report that holds both.
SOURCE_NOUN = {
    youtube_fetcher.PLATFORM_YOUTUBE: "Video",
    reddit_fetcher.PLATFORM: "Post",
}

# Where the platform choice lives. Read before the widget is drawn, because
# the search runs earlier in the script than the results area it sits in.
PLATFORM_KEY = "platforms"


def chosen_platforms() -> list[str]:
    """The platforms selected, both by default."""
    picked = st.session_state.get(PLATFORM_KEY)
    if picked is None:
        return list(PLATFORM_LABELS)
    return [p for p in PLATFORM_LABELS if p in picked]


def _search_budget_line() -> None:
    """One muted line about how many more searches the plan will carry.

    A reminder, not an alert: it sits above the report in ordinary use and
    says nothing louder than a caption. The spend behind it is pulled live
    from Apify; the per-search figure is the same one the Usage page uses.
    """
    if not reddit_fetcher.is_configured():
        return
    account = _apify_account()
    if not account.get("ok"):
        return

    queries = relevance.DEFAULT_VARIANTS if relevance.is_configured() else 1
    per_search = queries * reddit_fetcher.COST_PER_QUERY_USD
    used = account["runs"] // max(queries, 1)
    left = int(max(account["limit"] - account["used"], 0) / max(per_search, 0.0001))
    st.caption(
        f":gray[{used:,} search(es) with Reddit this cycle, about {left:,} "
        f"more before the plan's credit runs out at roughly "
        f"${per_search:.2f} each.]"
    )


def _platform_picker() -> list[str]:
    """The top control of the results area: which platforms to search and show."""
    # Only supply the default when nothing is stored. Passing both a default
    # and a session value is what Streamlit warns about, and opening a saved
    # keyword or a project sets that value.
    default = (
        {} if PLATFORM_KEY in st.session_state
        else {"default": list(PLATFORM_LABELS)}
    )
    picked = st.multiselect(
        "Platforms",
        list(PLATFORM_LABELS),
        key=PLATFORM_KEY,
        **default,
        label_visibility="collapsed",
        placeholder="Choose at least one platform",
        help=(
            "What Run Search collects, and what the report below shows. "
            "Both are searched unless you narrow it."
        ),
    )
    return [p for p in PLATFORM_LABELS if p in (picked or [])]

BY_VIDEO_VIEW = "Report by source"
BY_KIND_VIEW = "Report by kind"
ANALYSIS_VIEW = "Analysis"
PROJECTS_VIEW = "My projects"

# Sentiment is a polarity, so it takes the diverging pair: two opposed hues
# with a neutral gray in the middle. Kind is identity, so it takes categorical
# slots in a fixed order, which never shifts when a bucket is missing.
# Both were run through the palette validator; the gray midpoint is the
# diverging rule rather than a failed categorical slot.
SENTIMENT_COLOURS = {
    "positive": "#2a78d6",
    "neutral": "#898781",
    "negative": "#d03b3b",
}
KIND_COLOURS = {
    "question": "#2a78d6",
    "compliment": "#1baf7a",
    "complaint": "#d03b3b",
    "suggestion": "#eda100",
    "other": "#898781",
}
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
    "kind",
    "sentiment",
    "source_title",
    "channel_or_subreddit",
    "platform",
    "engagement_score",
    "video_type",
    "source_published_at",
    "source_url",
]

RAW_TABLE_CONFIG = {
    "keyword": ("Keyword", "small"),
    "kind": ("Kind", "small"),
    "sentiment": ("Sentiment", "small"),
    "platform": ("Platform", "small"),
    "engagement_score": ("Reach", "small"),
    "comment_text": ("Comment", "large"),
    "comment_translation": ("Translation", "large"),
    "comment_language": ("Lang", "small"),
    "comment_author": ("Author", "small"),
    "source_title": ("Video", "medium"),
    "channel_or_subreddit": ("Channel", "small"),
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


# Said beside the progress bar while it runs, and promised beside the button
# before it starts, because the moment someone needs to know is before they
# wander off.
CLASSIFY_WARNING = (
    "Do not switch tabs or navigate away while this runs. Leaving the page "
    "stops it partway. Everything classified up to that point is saved, and "
    "pressing Classify comments again resumes from there, so nothing is read "
    "or charged for twice."
)


def _classify_section(df: pd.DataFrame) -> None:
    """The Classify action, scoped to exactly what is on screen.

    Never automatic and never the whole Sheet: it reads the frame the filters
    above have already narrowed, so the cost is the cost of what you can see.
    """
    if not classify.is_configured() or df.empty:
        return

    pending = classify.pending_ids(df)
    if not pending:
        return

    batches = -(-len(pending) // classify.BATCH_SIZE)
    left, right = st.columns([2, 3], vertical_alignment="center")
    with left:
        pressed = st.button(
            f"Classify {len(pending):,} comment(s)",
            key="classify_now",
            icon=":material/label:",
            help=(
                "Reads each comment and labels what it is and how it reads. "
                "Only the comments in view, and only ones not done already. "
                + CLASSIFY_WARNING
            ),
        )
    with right:
        st.caption(
            f":gray[Sends these {len(pending):,} comment(s) to Claude in "
            f"{batches} batch(es). Each is read once and the labels are kept, "
            "so this is not repeated. Stay on this page while it runs: "
            "leaving stops it, though anything done is saved and pressing "
            "Classify again resumes from there.]"
        )

    if not pressed:
        return

    # Beside the bar, not above the button: this is the thing to read while
    # it is running. The rerun at the end clears it.
    st.warning(CLASSIFY_WARNING, icon=":material/hourglass_top:")
    progress = st.progress(0.0)
    status = st.empty()

    def on_progress(done_batches: int, total: int, done_rows: int) -> None:
        progress.progress(min(1.0, done_batches / max(total, 1)))
        status.markdown(
            f"Labelled {done_rows:,} of {len(pending):,} comment(s)..."
        )

    try:
        with st.spinner("Reading the comments..."):
            updated, done, problems = classify.classify_rows(
                insights.ensure_columns(st.session_state["data"]),
                pending,
                progress_cb=on_progress,
            )
    except classify.ClassifyError as exc:
        progress.empty()
        status.empty()
        _LOG.exception("Classification failed")
        st.session_state["insight_status"] = f"Classification failed: {exc}"
        st.rerun()
        return
    finally:
        progress.empty()
        status.empty()

    st.session_state["data"] = updated

    saved = ""
    if done and sheets_store.is_configured():
        try:
            touched = updated[updated["comment_id"].astype(str).isin(pending)]
            with st.spinner("Saving the labels..."):
                sheets_store.update_analysis(touched)
            _fetch_history.clear()
        except SheetsError as exc:
            _LOG.exception("Saving the labels failed")
            saved = f" They could not be saved: {exc}"

    left_over = len(pending) - done
    note = f"Labelled {done:,} comment(s)."
    if left_over:
        note += (
            f" {left_over:,} could not be read and are still waiting; run it "
            "again to pick up only those."
        )
    st.session_state["insight_status"] = note + saved
    st.rerun()


def _save_project(df: pd.DataFrame, config: dict) -> None:
    """Name the search that produced what is on screen.

    Stores the definition, never the rows: reopening it asks the comments
    table the same question again.
    """
    if not sheets_store.is_configured() or df.empty:
        return

    open_id = st.session_state["open_project"]
    current = next(
        (p for p in _known_projects() if p["project_id"] == open_id), None
    )

    with st.expander(
        f"Project: {current['name']}" if current else "Save as project",
        expanded=False,
        icon=":material/bookmark:",
    ):
        with st.form("project_form", border=False):
            name = st.text_input(
                "Project name",
                value=current["name"] if current else "",
                placeholder="Chikki category perception",
                help=(
                    "Saves what defines this search: its keywords and "
                    "platforms. The comments stay where they are."
                ),
            )
            keywords = st.session_state["session_keywords"] or config["keywords"]
            platforms = [PLATFORM_LABELS[p] for p in config.get("platforms", [])]
            st.caption(
                f":gray[Keywords: {', '.join(keywords) or 'none'} - "
                f"platforms: {', '.join(platforms) or 'none'}]"
            )
            if not st.form_submit_button(
                "Update project" if current else "Save as project",
                type="primary", width="stretch",
            ):
                return

        if not name.strip():
            st.warning("Give the project a name.")
            return

        project = {
            "project_id": (
                current["project_id"] if current else projects.new_id(name)
            ),
            "name": name.strip(),
            "keywords": list(keywords),
            "platforms": platforms,
            "exclude": list(config.get("exclude") or []),
            "match": config.get("match", ""),
            "created_at": current["created_at"] if current else "",
            "snapshot": projects.snapshot(df),
        }
        try:
            _remember(project)
        except SheetsError as exc:
            _LOG.exception("Saving the project failed")
            st.error(f"Could not save the project: {exc}")
            return

        st.session_state["open_project"] = project["project_id"]
        st.session_state["project_note"] = f"Saved {project['name']}."
        st.rerun()


def _open_project(project: dict) -> None:
    """Ask for a project to be opened on the next run.

    The platform picker and the view toggle are widgets that have already been
    drawn by the time a project is clicked, and Streamlit forbids writing a
    widget's state after it exists. So the request is parked and applied at
    the top of the next run, before any of them are made.
    """
    st.session_state["pending_open"] = {
        "project_id": project["project_id"],
        "keywords": list(project["keywords"]),
        "platforms": [
            label for label, value in PLATFORM_LABELS.items()
            if value in project["platforms"]
        ],
    }


def _apply_pending_open() -> None:
    """Put a requested project into effect, before any widget is drawn."""
    waiting = st.session_state.pop("pending_open", None)
    if not waiting:
        return
    st.session_state["open_project"] = waiting["project_id"]
    st.session_state["session_keywords"] = list(waiting["keywords"])
    if waiting["platforms"]:
        st.session_state[PLATFORM_KEY] = waiting["platforms"]
    st.session_state["main_view"] = BY_VIDEO_VIEW
    st.session_state["comment_search"] = ""
    st.session_state["intent"] = {}


def _refresh_project(project: dict) -> None:
    """Run the project's search again and say what changed.

    Deliberate: it costs a search. Everything else about it is the ordinary
    path, so dedupe, auto-save and the keyword limit all apply as usual.
    """
    before = dict(project.get("snapshot") or {})
    if not before:
        before = projects.snapshot(
            projects.rows_for(
                insights.ensure_columns(st.session_state["data"]), project
            )
        )

    config = {
        "platforms": [
            label for label, value in PLATFORM_LABELS.items()
            if value in project["platforms"]
        ] or list(PLATFORM_LABELS),
        "keywords": list(project["keywords"]),
        "exclude": list(project.get("exclude") or []),
        "match": project.get("match") or youtube_fetcher.MATCH_ALL,
        "order": "relevance",
        "order_label": list(ORDER_OPTIONS)[0],
        "videos_per_keyword": DEFAULT_VIDEOS_PER_KEYWORD,
        "published_after": None,
        "include_replies": True,
    }
    _run_search(config)

    after_rows = projects.rows_for(
        insights.ensure_columns(st.session_state["data"]), project
    )
    updated = {**project, "snapshot": projects.snapshot(after_rows)}
    try:
        _remember(updated)
    except SheetsError as exc:
        _LOG.exception("Updating the project failed")

    changes = projects.describe_change(before, updated["snapshot"])
    st.session_state["project_note"] = " ".join(changes)
    _open_project(updated)


def _my_projects() -> None:
    """The saved projects, most recently updated first."""
    saved = _known_projects()
    if not sheets_store.is_configured():
        st.info("Projects are stored in the Sheet, which is not connected.")
        return
    if not saved:
        st.info(
            "No projects yet. Run a search, then use Save as project below "
            "the report to name it."
        )
        return

    data = insights.ensure_columns(st.session_state["data"])
    for project in saved:
        rows = projects.rows_for(data, project)
        shot = projects.snapshot(rows)
        with st.container(border=True):
            head, act = st.columns([3, 2], vertical_alignment="center")
            with head:
                st.subheader(project["name"])
                st.caption(
                    f":gray[{', '.join(project['keywords'])} on "
                    f"{', '.join(project['platforms']) or 'any platform'} - "
                    f"updated {str(project.get('updated_at', ''))[:16] or 'never'}]"
                )
            with act:
                one, two = st.columns(2)
                if one.button("Open", key=f"open_{project['project_id']}",
                              width="stretch"):
                    _open_project(project)
                    st.rerun()
                if two.button("Refresh", key=f"refresh_{project['project_id']}",
                              width="stretch", type="primary"):
                    _refresh_project(project)
                    st.rerun()

            counts = st.columns(3)
            counts[0].metric("Comments", f"{shot['comments']:,}")
            counts[1].metric("Classified", f"{shot['classified']:,}")
            top = max(shot["kind"], key=shot["kind"].get) if shot["kind"] else "-"
            counts[2].metric("Most common", top.title())

            if shot["sentiment"]:
                st.caption(
                    "Sentiment: " + ", ".join(
                        f"{count:,} {name}"
                        for name, count in shot["sentiment"].items()
                    )
                )
            written = st.session_state["digests"].get(
                analysis.fingerprint(rows)
            )
            if written:
                st.caption(f":gray[{written[:300]}]")


def _classified_only(df: pd.DataFrame) -> pd.DataFrame | None:
    """The labelled part of what is in view, or None with the usual prompt.

    Shared by the two views that need labels, so the prompt and the wording
    are written once.
    """
    labelled = classify.ensure_columns(df)
    done = labelled[labelled[classify.KIND_COLUMN] != ""]
    if done.empty:
        st.info(
            "None of these comments have been classified yet. Use the "
            "Classify button above.",
            icon=":material/label:",
        )
        return None

    waiting = len(labelled) - len(done)
    if waiting:
        st.caption(
            f"{waiting:,} comment(s) in view have not been classified and are "
            "not counted here."
        )
    return done


def _by_kind(df: pd.DataFrame) -> None:
    """Comments grouped under what they are, whichever platform they came from."""
    done = _classified_only(df)
    if done is None:
        return

    counts = done[classify.KIND_COLUMN].value_counts()
    for kind in classify.KINDS:
        rows = done[done[classify.KIND_COLUMN] == kind]
        if rows.empty:
            continue
        share = counts.get(kind, 0)
        with st.expander(
            f"{kind.title()} - {share:,} comment(s)",
            key=f"kind_open_{kind}",
        ):
            spread = rows[classify.SENTIMENT_COLUMN].value_counts()
            st.caption(
                ", ".join(
                    f"{spread.get(name, 0):,} {name}" for name in classify.SENTIMENTS
                )
            )
            height = min(560, 90 + 40 * max(len(rows), 1))
            _comment_table(rows, height=height, key=f"kind_table_{kind}")


@st.cache_data(show_spinner=False)
def _cached_summary(print_: str, frame: pd.DataFrame) -> str:
    """One summary per fingerprint. The frame rides along; the print is the key."""
    return analysis.write_summary(frame)


def _digest(df: pd.DataFrame) -> None:
    """The written brief, once per exact set of comments and labels."""
    if not analysis.is_configured():
        return

    print_ = analysis.fingerprint(df)
    written = st.session_state["digests"].get(print_)

    if written:
        st.markdown(written)
        st.caption(
            ":gray[Written for exactly these comments and kept with them, so "
            "it survives a refresh. It updates when the set or its labels "
            "change.]"
        )
        return

    left, right = st.columns([2, 3], vertical_alignment="center")
    with left:
        pressed = st.button(
            "Write the summary",
            key=f"digest_{print_}",
            icon=":material/edit_note:",
            type="primary",
        )
    with right:
        st.caption(
            ":gray[One Claude call reading the counts and the most visible "
            "comments. Kept until this set changes.]"
        )
    if not pressed:
        return

    try:
        with st.spinner("Reading the comments..."):
            written = _cached_summary(print_, df)
    except analysis.AnalysisError as exc:
        _LOG.exception("Summary failed")
        st.error(str(exc))
        return

    st.session_state["digests"][print_] = written
    if sheets_store.is_configured():
        try:
            sheets_store.save_digest(
                print_,
                written,
                keywords=", ".join(st.session_state["session_keywords"]),
                comments=len(df),
            )
            _fetch_digests.clear()
        except SheetsError as exc:
            # The summary is on screen either way; only its persistence failed.
            _LOG.exception("Saving the summary failed")
            st.session_state["insight_status"] = (
                f"The summary was written but not saved: {exc}"
            )
    st.rerun()


def _quote_wall(df: pd.DataFrame) -> None:
    """The most visible complaints and compliments, as quotes.

    First on the page and given the width, because it is the part somebody
    acts on: a table of the same rows reads as data to be processed later.
    """
    left, right = st.columns(2, gap="medium")
    for column, kind, heading in (
        (left, "complaint", "Loudest complaints"),
        (right, "compliment", "Loudest compliments"),
    ):
        with column:
            st.subheader(heading)
            rows = analysis.top_quotes(df, kind, limit=5)
            if rows.empty:
                st.caption(f"No {kind}s in this set.")
                continue
            for _, row in rows.iterrows():
                with st.container(border=True):
                    text = " ".join(str(row.get("comment_text", "")).split())
                    english = insights.translation_of(row)
                    st.markdown(f"> {text[:400]}")
                    if english:
                        st.caption(f"English: {english[:300]}")
                    platform = str(row.get("platform", "") or "")
                    where = str(row.get("channel_or_subreddit", "") or "")
                    title = str(row.get("source_title", "") or "")
                    reach = pd.to_numeric(
                        row.get("engagement_score", 0), errors="coerce"
                    )
                    reach = int(reach) if pd.notna(reach) else 0
                    noun = "upvotes" if platform == reddit_fetcher.PLATFORM else "views"
                    st.caption(
                        f":gray-badge[{SOURCE_NOUN.get(platform, 'Source')}] "
                        f"{title[:60]} - {where} - {reach:,} {noun}"
                    )


def _share_chart(frame: pd.DataFrame, colours: dict, title: str, pie: bool):
    """One chart. Pie for the three-way split, bars for the five-way one."""
    order = list(colours)
    scale = alt.Scale(domain=order, range=[colours[name] for name in order])
    base = alt.Chart(frame, title=title)

    if pie:
        return base.mark_arc(
            innerRadius=42, stroke="#fcfcfb", strokeWidth=2, cornerRadius=2
        ).encode(
            theta=alt.Theta("count:Q", stack=True),
            color=alt.Color("label:N", scale=scale, sort=order,
                            legend=alt.Legend(title=None, orient="bottom")),
            order=alt.Order("label:N", sort="ascending"),
            tooltip=["label:N", "count:Q", alt.Tooltip("share:Q", format=".0%")],
        ).properties(height=200)

    bars = base.mark_bar(cornerRadiusEnd=4, height=14).encode(
        x=alt.X("count:Q", title=None, axis=alt.Axis(grid=True, tickCount=3)),
        y=alt.Y("label:N", sort=order, title=None),
        color=alt.Color("label:N", scale=scale, sort=order, legend=None),
        tooltip=["label:N", "count:Q", alt.Tooltip("share:Q", format=".0%")],
    )
    # Direct labels: the light-mode contrast warning is relieved by numbers on
    # the marks, and it saves reading values off an axis.
    text = base.mark_text(align="left", dx=4, color="#898781").encode(
        x="count:Q", y=alt.Y("label:N", sort=order), text="count:Q",
    )
    return (bars + text).properties(height=24 * len(order) + 24)


def _breakdown(df: pd.DataFrame, column: str, colours: dict, label: str,
               pie: bool) -> None:
    """One chart per platform when both are present, never merged into one."""
    platforms = [p for p in df["platform"].astype(str).unique() if p.strip()]
    places = st.columns(len(platforms), gap="medium") if len(platforms) > 1 else [st]

    for place, platform in zip(places, sorted(platforms)):
        rows = df[df["platform"].astype(str) == platform]
        counts = rows[column].value_counts()
        frame = pd.DataFrame({
            "label": list(colours),
            "count": [int(counts.get(name, 0)) for name in colours],
        })
        frame["share"] = frame["count"] / max(int(frame["count"].sum()), 1)
        title = f"{label}, {SOURCE_NOUN.get(platform, platform)}s" \
            if len(platforms) > 1 else label
        place.altair_chart(_share_chart(frame, colours, title, pie), width="stretch")


def _analysis(df: pd.DataFrame) -> None:
    """What the comments add up to: the quotes, the splits, the brief."""
    done = _classified_only(df)
    if done is None:
        return

    _quote_wall(done)

    st.divider()
    _digest(done)

    st.divider()
    _breakdown(done, classify.SENTIMENT_COLUMN, SENTIMENT_COLOURS,
               "Sentiment", pie=True)
    st.divider()
    _breakdown(done, classify.KIND_COLUMN, KIND_COLOURS,
               "What the comments are", pie=False)


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


def most_recent_keyword(df: pd.DataFrame) -> str:
    """The keyword collected most recently, by when its rows were fetched.

    Dates that do not look like dates are ignored rather than sorted as text,
    where "97" would beat "2026-09-09". A frame with no usable dates falls
    back to the last keyword in the sheet, which is where the newest rows are.
    """
    if df.empty or "keyword" not in df.columns:
        return ""

    stamps = (
        df["fetched_at"].astype(str).str.strip()
        if "fetched_at" in df.columns
        else pd.Series("", index=df.index)
    )
    dated = df.assign(_when=stamps.where(stamps.str.match(r"^\d{4}-\d{2}-\d{2}"), ""))
    known = dated[dated["_when"] != ""]
    if known.empty:
        return str(df["keyword"].astype(str).iloc[-1] or "")
    newest = known.groupby(known["keyword"].astype(str))["_when"].max()
    return str(newest.sort_values(ascending=False).index[0])


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


def _open_keyword(keyword: str, rows: pd.DataFrame) -> None:
    """Point the report at a keyword the Sheet already holds.

    Nothing is fetched and nothing is sent anywhere: the comments, their
    labels and any summary written for them are already stored, so this only
    changes what is on screen. Safe to write the widget keys here because the
    sidebar draws before the report, so none of them exist yet this run.
    """
    st.session_state["session_keywords"] = [str(keyword)]
    # It is a keyword, not a project, so nothing should claim otherwise.
    st.session_state["open_project"] = ""

    if "platform" in rows.columns:
        present = {
            str(value).strip().lower()
            for value in rows["platform"].unique()
            if str(value).strip()
        }
        picked = [
            label for label, value in PLATFORM_LABELS.items() if value in present
        ]
        if picked:
            st.session_state[PLATFORM_KEY] = picked

    st.session_state["main_view"] = BY_VIDEO_VIEW
    st.session_state["comment_search"] = ""
    st.session_state["intent"] = {}
    st.session_state["intent_note"] = ""


def _past_searches() -> None:
    """Every keyword ever saved, newest first, to download or to open.

    Opening one is free: it reads what the Sheet already holds, labels and
    summary included, and calls nothing.
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
            "Every keyword saved to the Sheet. Open one to read it here, or "
            "download it to work with elsewhere. Opening costs nothing: the "
            "comments, their labels and any summary are already saved."
        )
        showing = st.session_state["session_keywords"]
        for keyword in names:
            rows = stored[stored["keyword"] == keyword]
            here = list(showing) == [str(keyword)]
            st.caption(
                f"**{keyword}** ({len(rows):,})"
                + (" :gray-badge[open]" if here else "")
            )
            left, right = st.columns(2)
            with left:
                if st.button(
                    "Open in app",
                    key=f"past_open_{keyword}",
                    width="stretch",
                    type="primary" if not here else "secondary",
                    disabled=here,
                    help="Loads what is saved. No search, no Claude call.",
                ):
                    _open_keyword(str(keyword), rows)
                    st.rerun()
            with right:
                st.download_button(
                    "Download CSV",
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

    numbers = {"comment_likes": "Likes", "engagement_score": "Reach"}
    for column, label in numbers.items():
        if column in present:
            config[column] = st.column_config.NumberColumn(label, format="%d")

    dates = {"comment_published_at": "Commented", "source_published_at": "Posted"}
    for column, label in dates.items():
        if column in present:
            config[column] = st.column_config.DatetimeColumn(
                label, format="YYYY-MM-DD HH:mm"
            )

    if "source_url" in present:
        config["source_url"] = st.column_config.LinkColumn("Link", display_text="Watch")
    return config


def _metric_of(record, key: str) -> float:
    """One number out of a row's platform_metrics blob.

    Platforms name their counts differently, so the platform column decides
    which key to read. A missing count is 0, never an error.
    """
    metrics = youtube_fetcher.unpack_metrics(record.get("platform_metrics"))
    if key == _METRIC_LIKES:
        wanted = ("likes", "ups", "score")
    else:
        platform = str(record.get("platform", "") or "").strip().lower()
        named = youtube_fetcher.SOURCE_COMMENT_COUNT.get(platform)
        wanted = tuple(x for x in (named, "comment_count", "num_comments") if x)
    for name in wanted:
        try:
            return float(metrics[name])
        except (KeyError, TypeError, ValueError):
            continue
    return 0.0


def _video_order(df: pd.DataFrame, order_label: str) -> list:
    """Source ids in the order the chosen sort puts them, best first.

    "Most relevant" has no local column to sort on, so it keeps the order the
    sources were collected in -- which is the order the platform returned.
    """
    ids = list(dict.fromkeys(df["source_id"]))
    column = VIDEO_SORT_COLUMNS.get(order_label)
    if column is None:
        return ids

    first_rows = df.groupby("source_id").first()
    if column in (_METRIC_LIKES, _METRIC_COMMENTS):
        values = first_rows.apply(lambda row: _metric_of(row, column), axis=1)
        if column == _METRIC_COMMENTS:
            # A platform that reports no total still has the comments actually
            # collected, which is a fair stand-in and never zero.
            collected = df.groupby("source_id").size()
            values = values.where(values > 0, collected)
    elif column in df.columns:
        values = first_rows[column]
    else:
        return ids

    values = values.reindex(ids)
    return list(values.sort_values(ascending=False, na_position="last").index)


def _drilldown(df: pd.DataFrame, order_label: str) -> None:
    """One dropdown per video, holding that video's comments.

    Same table as the flat report, so the two read alike; the difference is
    only the grouping.
    """
    if "source_id" not in df.columns:
        st.caption("These rows carry no video id, so they cannot be grouped.")
        return

    # With one platform the kind is obvious and the label stays as it was.
    # With both, every section says which it is, so a post is never read as a
    # video or the other way round.
    kinds = (
        df.groupby("source_id")["platform"].first()
        if "platform" in df.columns else None
    )
    mixed = kinds is not None and kinds.astype(str).str.strip().nunique() > 1

    titles = df.groupby("source_id")["source_title"].first()
    counts = df.groupby("source_id").size()
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
        "Sources to show" if mixed else "Videos to show",
        min_value=VIDEO_PAGE_STEP,
        max_value=ceiling,
        step=VIDEO_PAGE_STEP,
        key="videos_to_show",
        **default,
    )
    page = ordered[: int(page_size)]
    noun = "source" if mixed else (
        SOURCE_NOUN.get(
            str(kinds.iloc[0]).strip().lower() if kinds is not None and len(kinds)
            else "", "Video",
        ).lower()
    )
    st.caption(
        f"Showing {len(page):,} of {total:,} {noun}(s), sorted by "
        f"{order_label.lower()}."
    )

    for video_id in page:
        title = str(titles.get(video_id, "") or video_id)
        count = int(counts.get(video_id, 0))
        open_key = video_open_key(str(video_id))
        prefix = ""
        if mixed:
            platform = str(kinds.get(video_id, "") or "").strip().lower()
            prefix = SOURCE_NOUN.get(platform, "Source") + ": "
        # Set before the expander is made, which is the only moment a widget's
        # state can be written. Closed unless something asked for it to stay.
        st.session_state.setdefault(open_key, False)

        with st.expander(
            f"{prefix}{title[:90]} - {count:,} comment(s)", key=open_key
        ):
            rows = df[df["source_id"] == video_id]
            url = str(rows["source_url"].iloc[0]) if "source_url" in rows else ""
            if url:
                # The link says where it goes: these sections hold YouTube
                # videos and Reddit posts side by side.
                platform = str(rows["platform"].iloc[0] or "").strip().lower()
                label = (
                    "Open on Reddit"
                    if platform == reddit_fetcher.PLATFORM
                    else "Watch on YouTube"
                )
                st.caption(f"[{label}]({url})")

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
    _apply_pending_open()

    # The manual takes the whole page, sidebar included, so nothing competes
    # with it and nobody starts a search by accident while reading.
    if st.session_state["show_help"]:
        _help_page()
        return

    if st.session_state["show_usage"]:
        _usage_page()
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
        # A parsed request leaves the keyword it ran under waiting for the box.
        # One more pass puts it there, so the field matches what was searched.
        if st.session_state.get("pending_keyword"):
            st.rerun()

    if st.session_state["fetch_error"]:
        st.error(st.session_state["fetch_error"])
        st.session_state["fetch_error"] = ""

    if st.session_state["intent_note"]:
        st.caption(st.session_state["intent_note"])

    if st.session_state["last_run"]:
        _run_summary()
        st.divider()

    # The report is about this session's search. Everything else the Sheet
    # holds is downloadable from Past searches in the sidebar.
    data = _live_rows(insights.ensure_columns(st.session_state["data"]))

    # The top control of the results area: what to search, and what to show.
    _search_budget_line()
    platforms = _platform_picker()
    wanted = {PLATFORM_LABELS[p] for p in platforms}
    if "platform" in data.columns and wanted:
        known = data["platform"].astype(str).str.strip().str.lower()
        # Rows collected before the platform column existed are YouTube's.
        known = known.replace("", youtube_fetcher.PLATFORM_YOUTUBE)
        data = data[known.isin(wanted)]

    if st.session_state["project_note"]:
        st.info(st.session_state["project_note"], icon=":material/bookmark:")
        st.session_state["project_note"] = ""

    if data.empty:
        # Saved projects are worth reaching even before a search has run.
        if _known_projects():
            view = st.segmented_control(
                "View",
                [PROJECTS_VIEW],
                default=PROJECTS_VIEW,
                key="empty_view",
                label_visibility="collapsed",
                width="stretch",
            )
            _my_projects()
            return
        st.info(
            "Search any product or competitor name to see what people are "
            "saying about it."
        )
        return

    if st.session_state["translation_status"]:
        st.caption(st.session_state["translation_status"])

    # The view sits above the filters: it says what you are looking at, and the
    # filters below narrow it. Full width, matching the type filter row.
    view = st.segmented_control(
        "View",
        [BY_VIDEO_VIEW, ALL_COMMENTS_VIEW, BY_KIND_VIEW, ANALYSIS_VIEW,
         PROJECTS_VIEW],
        default=BY_VIDEO_VIEW,
        key="main_view",
        label_visibility="collapsed",
        width="stretch",
    )

    # Shorts and Videos are a YouTube distinction with no Reddit equivalent,
    # so the filter only appears when YouTube is all that is selected.
    if platforms == [YOUTUBE]:
        data = _type_filter(data)
    _text_display(data)
    if data.empty:
        st.info("No comments match the selected keyword(s).")
        return

    data = _comment_search(data)
    if data.empty:
        return

    # The requested sentiment or kind, if the comments can answer for it yet.
    data = _intent_filter(data)
    if data.empty:
        st.info("No comments match that, in what has been classified so far.")
        return

    # Below every filter, so what it offers to label is what is on screen.
    _classify_section(data)
    if st.session_state["insight_status"]:
        st.caption(st.session_state["insight_status"])
        st.session_state["insight_status"] = ""

    if view == ALL_COMMENTS_VIEW:
        _all_comments(data)
    elif view == BY_KIND_VIEW:
        _by_kind(data)
    elif view == ANALYSIS_VIEW:
        _analysis(data)
    elif view == PROJECTS_VIEW:
        _my_projects()
    else:
        _drilldown(data, config["order_label"])

    st.divider()
    _save_project(data, config)

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
