# Social Listening - YouTube (GO DESi)

Search YouTube by keyword, pull video stats and comments, store them in a Google
Sheet, and read them back as two reports, with on-demand translation for
non-English comments.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# then fill in the real values
```

### What goes in `secrets.toml`

| Key | Where it comes from |
|---|---|
| `YOUTUBE_API_KEY` | Google Cloud Console -> Credentials -> API key, with **YouTube Data API v3** enabled |
| `SHEET_KEY` | The long id in your Sheet URL: `docs.google.com/spreadsheets/d/`**`<SHEET_KEY>`**`/edit` |
| `WORKSHEET_NAME` | Tab name, defaults to `comments`. Created automatically if missing |
| `[gcp_service_account]` | Every field from the service account JSON key file |


**The step people miss:** after creating the service account, open the Google
Sheet and share it with the `client_email` from the JSON, with **Editor** access.
Without that, every write returns 403 regardless of how correct the credentials are.

## Run

```bash
streamlit run app.py
```

## Platforms

The multiselect at the top of the results area is both the search scope and the
report scope: it says what **Run Search** collects and what the report below
shows. Both platforms are selected by default, and one search action runs both
pipelines and stores the rows together, so a run produces one summary rather
than one per platform.

The report scopes itself to the keyword the rows were actually filed under, not
to what was typed. Matching all together files rows under the joined query, so
scoping to the typed keywords would leave the report empty.

What changes with the selection:

| | Both | YouTube only | Reddit only |
|---|---|---|---|
| Source sections | prefixed `Video:` / `Post:` | unprefixed | unprefixed |
| Shorts filter | hidden | shown | hidden |
| Exclude keywords | shown, YouTube only | shown | hidden |
| Videos to query, date range, sort, Match | shown, YouTube only | shown | hidden |
| Reddit cost and filtering notes | shown | hidden | shown |

Shorts and Videos are a YouTube distinction with no Reddit equivalent, so that
filter only appears when YouTube is the only platform selected.

## Using it

The sidebar holds the whole search: what to look for, how to collect it
(**Which videos** and **What to collect**), and the **Run Search** button. A
**How it works** button at the bottom opens the user manual, which takes over
the whole page, sidebar included, and comes back with a **Back to the app**
button. The main area is otherwise only results.

The main area has two views, switched with the toggle above the list:

| View | What it shows |
|---|---|
| **Report by video** (default) | One dropdown per video, ordered by **Sort videos by**, holding that video's comments |
| **Report by all comments** | Every comment in one table, across all the selected keywords |

Both views use the same table, so they differ only in the grouping. Columns that
are empty for the current rows are not drawn - an untagged sheet does not show
three blank tag columns, and the translation columns appear once something has
been translated.

The report shows only the keyword or keywords searched in the current session.
Everything else the Sheet holds is listed under **Past searches** in the
sidebar, newest first, each with a **Download CSV** button. Those are for export
and reference: nothing there loads back into the live report.

**Search comments** above the report is a find-within-results box: it filters
the comments already loaded by a case-insensitive substring of the comment text
or its English translation, so an English term finds a comment written in
another language. It makes no API calls. It applies to both reports, and in the per-video report
a video with no matching comment drops out of the list. Clear it to see
everything again.

Opening the app cold shows no report until a search is run; the Sheet is still
read, so **Past searches** lists what is already stored.

## Shorts and videos

`videos.list` already returns `contentDetails.duration` in the same call as the
statistics, so telling a Short from a full video costs no extra quota. A minute or less is a
**Short**, anything longer is a **Video**, and an unreadable duration counts as
a Video so nothing hides behind a Shorts-only filter. The result is stored per
row as `video_type`, and the **All / Shorts only / Videos only** control at the
top of the results filters every view.

Rows collected before this existed have no `video_type`; they show under **All**
and the app says how many rather than dropping them silently.

## Searching

The keyword box and **Run Search** sit in one small form, so pressing Enter in
the box runs the search exactly as clicking the button does. A bare text input
only commits its value on Enter, and the run hung off the button, so the
keystroke did nothing. Everything below the button stays outside the form and
applies as soon as you change it.

Search results are then held to a stricter test than YouTube's own, in two
gates. The first is the keyword itself. The second is context: the same
expansion Reddit uses turns "chikki" into "chikki snack" and "chikki peanut",
and the leftover words, "snack" and "peanut", become terms the video must also
carry. A video that says "chikki" about a person rather than a snack passes the
first gate and fails the second. The gate only bites when expansion produced
context words, and `Exclude keywords` remains a manual override on top of both,
not a replacement.

The keyword gate itself: A video is
kept only when the keywords really appear in its title or description, matched
case-insensitively as a substring with runs of whitespace collapsed, so a term
typed with one space still matches a title that wrapped across a line. The Match
mode decides how many have to appear: **any** keeps a video that carries at
least one term, **all together** requires every one of them. `FetchReport`
counts the drops in `videos_irrelevant` and the run summary reports them.

**Exclude keywords** sits under the search box and is parsed the same way, on
commas. A video is dropped when its title or its full description contains any
of those terms, matched case-insensitively as a substring, so `recipe` also
catches `Recipes`. The drop happens after the batched stats call, which is
where the full description arrives, and before any comments are read, so an
excluded video costs nothing beyond the search that found it. The run summary
says how many were skipped.

**Run Search** is the whole job in one click. It finds the videos (one
`search.list` page per keyword plus one batched `videos.list` for their stats),
reads the comments on every video it found, and saves them. The progress bar
runs through both halves and the report appears when it finishes.

A new search replaces the one before it in the report. Earlier searches stay in
the Sheet and are downloadable from **Past searches** in the sidebar.

## Saving

A search saves itself at the end of the second step. Comments are appended to the Sheet as the last step of a
successful run, deduped on `comment_id` as before, and the run summary reports
how many rows were new. There is no separate Save step and nothing is left
pending.

If the write fails, the comments still arrive in the session and the failure is
shown rather than swallowed, so a Sheets outage does not look like a successful
save.

Translations write onto rows the Sheet already holds and skip anything else.
## Files

| File | Role |
|---|---|
| `app.py` | Streamlit UI - sidebar plus the single per-video drilldown |
| `youtube_fetcher.py` | `search_videos()`, `get_video_stats()`, `get_comments()`, `run_search()` |
| `sheets_store.py` | gspread read/write, dedupe on `comment_id`, translation write-back |
| `insights.py` | Translation: language detection, batching, caching |

## API quota

The default YouTube quota is **10,000 units/day**:

- `search.list` - **100 units** per keyword (per page of 50)
- `videos.list` - 1 unit per batch of 50 videos
- `commentThreads.list` - 1 unit per page of 100 comments

**Videos to query** is the ceiling for the whole search (5 to 50, default 20),
not per keyword: adding a term splits the same budget rather than multiplying
it. **From the last N days** limits how far back to look (default 365, 0 for no
limit), and comments are capped at **500 per video**.

**Match** decides how the comma-separated terms become search queries:

| Mode | Queries sent | Use it for |
|---|---|---|
| **Match all together** (default) | one query joining every term | you know what you are looking for: `SKC, Spicy Chikki, SKC Mysore Pak` becomes the single query `SKC Spicy Chikki SKC Mysore Pak` |
| **Match any of these** | one per term, results pooled and shared across the cap | you are unsure what people call it: brand name plus nicknames plus misspellings |

The full explanation is in the toggle's tooltip. Under **Match any of these**
the terms take turns filling the cap, so a term with few results does not waste
its share and a busy term cannot crowd the others out. A video found by two
terms is listed once, carrying both.

Cost, for whoever owns the API key: searching is 100 units per query plus 1 for
a single batched stats call, so three terms matched any is 301 while matching
all together is 101 however many terms it joins. Reading comments is 5 units per
video, so the ceiling follows Videos to query, out of 10,000 a day that reset at
midnight Pacific Time. None of this is shown in the app - it is not the end
user's problem, and the user-facing message when the day's allowance runs out
says only that YouTube has stopped returning results.

## Reddit

Reddit is collected through the `epctex/reddit-scraper` actor on Apify, using
`APIFY_API_TOKEN` from secrets. One actor call returns posts with their
comments already nested, so there is no separate comment fetch. Rows land in
the same schema as YouTube's, with `platform` "reddit", `engagement_score` the
post's upvotes, `channel_or_subreddit` the subreddit, and `platform_metrics`
carrying `upvotes`, `upvote_ratio` and `num_comments`.

Noise is dropped before saving: bodies of `[deleted]` or `[removed]`, and the
AutoModerator sticky when it is the first top-level comment. A real reply
under a dropped comment is kept and re-parented rather than lost with it.

**The account is on Apify's Starter plan** ($19 a month, $19 of usage credit,
cycle the 9th to the 8th). That lifts the free tier's Demo Mode, which had
capped things at 5 runs a month and 10 items a run whatever `maxItems` asked
for. Verified rather than assumed: a run asking for 18 posts returned 18.

`POSTS_PER_QUERY` stays at 10 anyway. It is now a cost control of ours, not a
plan ceiling, because every post is billed and three queries run per search.

Cost is pay-per-event and measured, not quoted: a query with comments billed
$0.025 to $0.035, being one list query at $0.025 plus $0.001 per post. Comments
bill per page at $0.00001, so they are effectively free. At three queries that
is about **$0.11 a search, roughly 180 searches a month** inside the credit,
which is what the sidebar says.

The actor still answers with `{"demo": true}` placeholder rows when it will not
serve the account. `is_placeholder` detects them and the run reports that the
Apify account needs checking, rather than letting it pass as an empty search.

### Relevance, by Claude

Reddit's noise is structurally different from YouTube's. YouTube returns recipe
videos that really do mention the brand, so a keyword rule can sort them.
Reddit returns unrelated real things that share the word: "chikki" is a peanut
brittle, a Bollywood surname and a D&D player race, and the keyword is present
in all three. No keyword rule can separate those, so `relevance.py` brackets
the search with two Claude passes.

**Before**: the keyword is expanded into `DEFAULT_VARIANTS` (3) queries aimed
at food, the bare keyword always first, e.g. "Chikki" becomes "Chikki", "chikki
snack", "chikki peanut". A variant that drops the keyword is discarded. Each
query is a separate actor run, so one search spends three of the plan's five
monthly runs, which the sidebar states.

**After**: every post that comes back is read in batches of `BATCH_SIZE` (20)
and judged on two things, in the one call: is it about food, and does it
mention the keyword as a whole thing.

The second question exists because Reddit matches any word in a phrase. A
search for "DESi POPz" returns posts carrying only "desi", a common word for
South Asian that says nothing about the product, so a fragment alone is not a
result. The whole keyword is passed to Claude as the context for what counts,
and close variants pass: capitalisation, spacing, "desipopz", ordinary plurals,
small misspellings. Fragments do not: "desi food", "desi parents", "ABCDesis".
The rule only applies to keywords of more than one word (`is_phrase`), so a
single-word search behaves exactly as before.

That catches posts the food question alone would keep. "Best desi snacks for a
party?" is genuinely about food and still goes, because it never names the
product.

Posts are judged food or not food. Posts that are not are dropped
before anything is shown or saved, and the run summary says how many went. On
30 real posts from stored runs it kept 12 and dropped 18, correctly rejecting
the Bollywood Panday posts and three D&D race posts while keeping the
r/IndianFood threads.

Both passes fail soft: without `ANTHROPIC_API_KEY` the search runs unfiltered,
and any error at all leaves as a `RelevanceError` that is logged and warned
about rather than failing the search. A post Claude does not answer for is
kept, because dropping a real result is worse than keeping a doubtful one.

None of this touches YouTube, whose exclude list and strict keyword filter are
unchanged and call no LLM.

**The search is keyword-only and site-wide, because that is all the actor can
do.** Its `search` field searches all of Reddit; a subreddit search URL in
`startUrls` is crawled as a listing with the query ignored (searching "chikki"
and "biryani" in r/IndianFood returned byte-identical posts), and Reddit's own
`subreddit:` operator returns placeholder rows. There is therefore no subreddit
field, and the sidebar says the search is site-wide and loose rather than
implying a precision it does not have.

## Region

Every `search.list` call carries `regionCode`, set by `DEFAULT_REGION_CODE` in
`youtube_fetcher.py`, which is `IN`. Generic brand names collide badly without
it: "candyman" returns a Jamaican rapper long before the Indian sweet. It is a
bias applied to relevance, not a hard filter, so a genuinely relevant video from
elsewhere still comes back, and comments in any language are still collected.

Change that one constant for another country, or pass `region_code=None` to a
search to send no preference at all.

## Past searches

Each keyword in the sidebar list offers two things. **Download CSV** is
unchanged. **Open in app** points the whole report at that keyword's saved
comments: by source, by all comments, by kind and Analysis all fill from what
the Sheet already holds, including any classification labels and any summary
written for exactly that set.

Each entry is one bordered row, the way a source section reads in the report:
the keyword and its count on a line, the two actions beneath on one row. Two
full-width buttons stacked under a caption took four lines each and wrapped
"Download CSV" in half in a sidebar that narrow.

It calls nothing. No YouTube, no Reddit, no Claude - it only changes what is on
screen, because the rows, their labels and the digest are already stored. The
platform picker narrows to wherever that keyword's comments actually came from,
and the keyword currently open is marked and its button disabled.

A keyword that has never been classified behaves exactly like a fresh search:
Report by kind and Analysis show the usual prompt and the Classify button
offers the work.

## Retention

The Sheet keeps the `KEYWORD_LIMIT` (20) most recently searched keywords. After
every search that saves, anything past that is deleted permanently: the rows go
from the comments tab, and there is no archive.

Recency is the last-searched date, not the first. A `searches` tab holds one row
per keyword with a timestamp, refreshed on every run whether or not that run
wrote any new comments. That tab is why a repeat search protects a keyword: a
second search of an old keyword usually finds nothing new to write, so the
newest `fetched_at` in the comments would still say weeks ago, and the keyword
could be deleted minutes after someone searched it. Keywords collected before
that tab existed fall back to their newest `fetched_at`.

Every deletion is logged at WARNING before it happens, naming the keyword, its
last-searched date and its row count, followed by a total. That log is the only
record left once the rows are gone.

Rows are deleted in contiguous blocks from the bottom of the Sheet up, so
earlier deletions cannot shift the rows still queued. The cleanup runs in its
own try block after the save: the comments are already stored by then, so a
failure here is logged and never reported as a failed save.

**Past searches** needs no rule of its own. It lists what the Sheet holds, and
the Sheet now genuinely holds 20 keywords.

## Usage page

A second sidebar button under **How it works**, same shape, labelled internal:
API spend and quota for whoever runs the app. What each number is, and where it
comes from, was established by asking each provider rather than assumed:

| | Source | Why |
|---|---|---|
| Apify | **pulled live** | The account API publishes cycle spend and the run list. |
| Claude | **self-tracked** | Anthropic's usage and cost reports need an Admin API key; a normal key gets `401 The Admin API requires an Admin API key`. Each call is priced from the `usage` token counts it returns, at published rates, and summed. |
| YouTube | **self-counted** | The Data API does not report quota, and the Cloud Monitoring metric that would is `403` to this service account. Units are counted per call at published costs: 100 a search page, 1 a batched details call, 1 a comments page. |
| Translation | **self-counted** | Checked three ways, all refused with the credentials in use: Cloud Monitoring `403`, Service Usage consumer quota `403`, and the Cloud Billing API is not enabled on the project. Characters are counted as they are sent, which is how Google bills them, against the 500,000 free characters a calendar month. Shown with what is left and roughly how many comments that is at 50 to 100 characters each. |

Rows go to a `usage` tab so totals survive a restart, and a failed write is
logged rather than raised: nobody loses a search because the meter could not be
updated. The page says on each section which kind of number it is showing, and
the Claude section states plainly that it counts only what this app spends.

Above the report, in ordinary use, one muted line says how many searches with
Reddit the plan's credit still carries: the spend behind it is pulled live from
Apify and the per-search figure is the same one the Usage page quotes. It is a
caption, never a banner.

If you want YouTube quota pulled live instead of counted, enable the Cloud
Monitoring API on the project and grant the service account `roles/monitoring.viewer`;
the metric is `serviceruntime.googleapis.com/quota/rate/net_usage` filtered to
`youtube.googleapis.com`.

## Startup

A cold load lands on the most recently collected keyword rather than an empty
page: `most_recent_keyword` picks it by the newest `fetched_at`, ignoring dates
that do not look like dates so a corrupted "97" cannot beat a real timestamp.
Everything else stored stays a CSV download in Past searches, and the moment a
search runs it takes over. A session that is already scoped, by a search or by
opening a project, is left alone.

This is new as of today. Before it, `_live_rows` returned nothing until a
search had run in that session, which was the deliberate result of replacing
multi-keyword browsing with the export list.



The page draws before the Sheet is touched: title, sidebar and search box are
created first, and the Sheets read runs afterwards with its spinner confined to
the results area, so nothing is frozen behind it.

The read itself is cached for 60 seconds (`HISTORY_TTL_SECONDS`), so reopening
or refreshing inside that window costs no network at all. The spreadsheet and
worksheet handles are cached per session too - opening the file used to be a
round trip paid separately by the comments read and the video-summaries read.

Note that Google's values API has no server-side filter: gspread cannot ask for
"just the Laddoo rows", so the sheet is read whole and scoped in pandas. On a
9,700-row sheet the read is about 1.5s of the cold start; the rest was
connection setup, which is what the handle caching removes.

## How dedupe works

Every row is keyed on YouTube's own `comment_id`. On each run the app reads the
existing id column from the Sheet and only appends ids it hasn't seen - so
re-running the same search adds nothing, and overlapping keywords don't
double-count a comment.

## Query understanding

People type what they want to know, not what a search engine accepts. "khakra
negative feedback" sent to YouTube as three words finds nothing, which is the
confusion the placeholder copy was written to head off. Claude now reads such a
request instead, returning a keyword, an optional sentiment and kind, and a
plain note about anything neither can deliver.

It is gated on `looks_like_a_request`, a word list: a plain product name never
reaches Claude, so an ordinary search costs nothing extra. The parsed keyword
is what gets searched and is written back into the search box, so the field
shows what actually ran.

    khakra negative feedback           -> keyword khakra, sentiment negative
    chikki complaints about packaging  -> keyword chikki, complaint, negative
    khakra reviews from last month by Ranveer
        -> keyword khakra, and "Results from last month only, comments by
           Ranveer only" as unfulfilled

A sentiment or kind is a **filter on classified comments**, so it cannot narrow
comments nobody has read yet. When none of what is in view is classified the
filter is not silently skipped: the app says so and points at the Classify
button, then applies itself once labels exist. When only some are classified it
says how many are not counted. A Clear filter button drops it.

Anything unfulfilled is stated rather than ignored, and a failure at any point
searches the text as typed rather than refusing.

## Classification

**Classify comments** labels what a comment is and how it reads, in one Claude
call per batch since both judgements come from the same reading:

- `kind`: question, compliment, complaint, suggestion, other
- `sentiment`: positive, neutral, negative

Both are stored on the row, so the Sheet is 23 columns now. `kind` is the
marker for "done": a comment carrying one is never sent again, and pressing the
button twice costs nothing.

It is never automatic. The button sits below every filter and offers exactly
what is on screen, so the platform multiselect, the type filter and the comment
search all narrow it. It never reaches the whole Sheet, and the caption says
how many comments and how many batches before you press.

While it runs, a warning sits beside the progress bar: do not switch tabs or
navigate away, because leaving stops it partway. The same promise is on the
button's tooltip and its caption, before anyone starts, since that is the
moment it matters. Nothing is lost either way, which is what the message says:
whatever finished is saved and pressing the button again resumes from there.

Batches of `BATCH_SIZE` (25) run `MAX_WORKERS` (4) at a time and are written
onto the frame as they land, on the calling thread. A batch that fails leaves
its own rows unlabelled and the rest stand, so an interrupted run keeps what
finished and the next one sends only what is missing. Only a run where every
batch failed raises.

**Report by kind** is the fourth view, grouping comments under their kind with
the sentiment spread per bucket. It reads `comment_text`, which every row has
whatever platform it came from, so a Reddit comment and a YouTube one are the
same job.

## Projects

A project is a named search: its keywords, its platforms, and the settings it
ran under, stored in a **`projects`** tab. It owns no comments.

**Why there is no project_id column and no mapping tab.** The rows already
carry `keyword` and `platform`, which is exactly what defines membership, so
either would restate a relationship that is already there and then go stale the
moment a refresh adds rows. `projects.rows_for` asks the comments table the
same question every time instead, which keeps one source of truth and lets two
projects legitimately share a keyword. The trade is that retention clearing an
old keyword shrinks the project that referenced it, which the change note says
out loud rather than hiding.

**Save as project** sits under the report it names. **My projects** is the
sixth view, listing them most recently updated first with each one's live
comment count, classified count, most common kind, sentiment split, and its
digest if one exists. Opening one points the report at its rows and regenerates
nothing.

**Refresh this project** is a button, never automatic. It re-runs the same
search config through the ordinary path, so dedupe, auto-save and the keyword
limit all apply unchanged, then compares the snapshot stored on the project
against the new one and says what moved: "2 new comment(s) since 2026-09-09",
"Sentiment shifted from 50% positive to 33% positive", "2 more comment(s) have
been classified".

Opening a project writes widget state (the platform picker, the view toggle),
which Streamlit forbids once those widgets exist, so the request is parked in
`pending_open` and applied at the top of the next run.

## Analysis

The fifth view, and the one the rest feeds. It needs classification to have run
and shares `_classified_only` with Report by kind, so the prompt and its
wording exist once.

**Loudest complaints and compliments** come first and take the width, as quote
callouts with the source, the community or channel, and the reach: views on
YouTube, upvotes on Reddit. They are ranked by `engagement_score` with
`comment_likes` as the tiebreak, and both platforms rank together in one list.
This is the part somebody acts on, so it is not a table.

**The written brief** is one Claude call over counts already computed, plus the
most visible quotes per bucket. It is keyed by `analysis.fingerprint`, a hash of
the comment ids and their two labels, so re-ordering the rows changes nothing
and re-classifying or filtering produces a new summary. An unchanged set is
never sent twice: the button only appears when there is no summary for that
fingerprint.

Summaries are stored in a **`digests`** tab beside the comments, one row per
fingerprint holding the keywords, the comment count, the text and when it was
written. They are read once per session alongside the comments and merged under
anything already in session state, so a refresh or a new session finds an
existing summary instead of paying for it again. Writing the same fingerprint
twice replaces the row rather than appending. A tab that cannot be read or
written costs the summary, never the view: the read returns nothing and the
write reports itself while the text stays on screen.

**The charts** follow the palette rules and were run through the validator.
Sentiment is a polarity, so it takes the diverging pair, blue against red with
a neutral gray midpoint (poles separate at dE 23.8 under CVD simulation). Kind
is identity, so it takes categorical slots in a fixed order that never shifts
when a bucket is empty. Bars carry direct labels, which is also what relieves
the light-surface contrast warning on the lighter slots.

With both platforms present each breakdown draws one chart per platform side by
side, never merged, so YouTube and Reddit can be compared directly.

## Translation

Translation is Google Cloud Translation, authenticated with the **same service
account as the Sheet**, so there is no second key in `secrets.toml`. That
account needs the Cloud Translation API enabled on its project and the Cloud
Translation API User role. Nothing in the app calls an LLM.

Billing is per character, against a 500,000 character monthly free tier. Only
comments someone asks for are sent, clipped at 2,000 characters each, and a
comment is translated once and then read from the Sheet forever after, so real
usage sits far inside the free tier.

Comments are sent with the source language auto-detected, with one exception.
Hinglish, meaning an Indian language typed in Latin letters, is detected as
English and handed straight back untranslated, so anything the marker-word
heuristic reads as Hinglish is sent with the source declared as Hindi instead.
That costs one extra call per batch and only when such comments are present.

Each video section in **Report by video** is a keyed expander, its open state
held in `st.session_state` under `video_open_<video_id>`. Translating reruns the
script, and without a key the section the reader was inside collapsed, leaving
them to find it and open it again. Both translate routes set that key before
their rerun, so the section they were working in is still open, with the
translation in place, when the page comes back. Streamlit owns the scroll
position; what this controls is that nothing has to be reopened.

Translation is on demand, by either of two routes that share one code path
(`_translate_ids`), so whichever runs first the other finds nothing left to do:

- **A whole video** - open it in **By Video** and press **Translate N
  comment(s)** above its table.
- **One comment** - select its row in any table and press **Translate 1
  selected comment(s)**. Streamlit has no hover action inside a dataframe and
  this app uses no custom HTML, so selecting the row is the native equivalent.

Once anything is translated, a **Comment text** control appears offering
**Both / Original / Translation** - the table's version of flipping one comment
between its original and its English, since a dataframe cannot hold a per-row
toggle.

Which comments get a link is decided locally, with no API call - non-Latin
script always counts, and Latin-script text is checked against a list of common
Hindi/Tamil/Telugu/Kannada words, because much of the audience writes those
languages in Latin letters and a character-set test alone would wave them
through as English. It is deliberately approximate; the bulk pass catches the
rest.

There is no bulk pass in the UI: comments are translated only when someone asks
for one. `insights.translate_dataframe()` still does a full sweep in batches of
25 if you want one from a script.

| Column | Meaning |
|---|---|
| `comment_language` | BCP-47 code of the language the comment is written in. This is what marks a comment as checked |
| `comment_translation` | English translation. Empty for English comments - a real result, not a gap |

Only rows with a blank `comment_language` are ever sent, whether from a
Translate link or the bulk pass, so nothing is translated twice.

## Known MVP limits

- Comments are fetched by `relevance`, so the 500-comment cap takes the top 500,
  not the newest 500.
- Comments longer than 2,000 characters are clipped before translation, which
  bounds the batch prompt.
- No word-frequency or topic-cluster extraction.
- Reads the full id column on every save; fine to roughly ~50k rows, would need
  a local cache beyond that.
