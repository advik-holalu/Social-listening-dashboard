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

Search results are then held to a stricter test than YouTube's own. A video is
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

## Region

Every `search.list` call carries `regionCode`, set by `DEFAULT_REGION_CODE` in
`youtube_fetcher.py`, which is `IN`. Generic brand names collide badly without
it: "candyman" returns a Jamaican rapper long before the Indian sweet. It is a
bias applied to relevance, not a hard filter, so a genuinely relevant video from
elsewhere still comes back, and comments in any language are still collected.

Change that one constant for another country, or pass `region_code=None` to a
search to send no preference at all.

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

## Startup

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
