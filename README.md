# Social Listening - YouTube (GO DESi)

Search YouTube by keyword, pull video stats and comments, store them in a Google
Sheet, browse everything in a filterable table, and tag comments with Claude for
sentiment, category, and actionable asks.

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
| `VIDEO_WORKSHEET_NAME` | Tab for video summaries, defaults to `videos`. Created automatically |
| `DIGEST_WORKSHEET_NAME` | Tab for written summaries, defaults to `digests`. Created automatically |
| `[gcp_service_account]` | Every field from the service account JSON key file |
| `ANTHROPIC_API_KEY` | console.anthropic.com -> API keys. Needed for transcript summaries and comment tagging |

**The step people miss:** after creating the service account, open the Google
Sheet and share it with the `client_email` from the JSON, with **Editor** access.
Without that, every write returns 403 regardless of how correct the credentials are.

## Run

```bash
streamlit run app.py
```

## Using it

The sidebar holds the whole search: what to look for, how to collect it
(**Which videos** and **What to collect**), and the **Run Search** button.
Housekeeping - last refresh time, the quota estimate, the link to the Google
Sheet - sits in a collapsed **Details** box at the bottom of it. The main area
is only results.

The main area has two views, switched with the toggle above the list:

| View | What it shows |
|---|---|
| **By Video** (default) | One dropdown per video, ordered by **Sort videos by**. Open one and you get what the video was about (from its transcript summary), then its comments in the same table |
| **All Comments** | Every comment in one table, across all the selected keywords |
| **By Kind** | Comments grouped into Questions, Compliments, Complaints, Suggestions and Other - only after you ask it to classify |
| **Analysis** | Sentiment split for the selected keywords, side by side when more than one is selected, with example comments |

Both views use the same table, so they differ only in the grouping. Columns that
are empty for the current rows are not drawn - an untagged sheet does not show
three blank tag columns, and the translation columns appear once something has
been translated.

**Keywords to show** at the top of the results scopes the view. A new search
switches it to whatever was just searched; opening the app cold shows the most
recently *saved* keyword, since an unsaved search leaves nothing behind. Add the
others back from the same control to compare.

## Shorts and videos

`videos.list` already returns `contentDetails.duration` in the same call as the
statistics, so classifying a video costs no extra quota. A minute or less is a
**Short**, anything longer is a **Video**, and an unreadable duration counts as
a Video so nothing hides behind a Shorts-only filter. The result is stored per
row as `video_type`, and the **All / Shorts only / Videos only** control at the
top of the results filters every view.

Rows collected before this existed have no `video_type`; they show under **All**
and the app says how many rather than dropping them silently.

## By Kind and Analysis

One Claude call fills both views. It returns a bucket (`question`,
`compliment`, `complaint`, `suggestion`, `other`) and a sentiment
(`positive`, `neutral`, `negative`) for each comment in the same response, so
running either view leaves the other with nothing to pay for.

**By Kind** groups comments under their bucket. **Analysis** reads top to bottom:

| Section | Form | Why that form |
|---|---|---|
| Written digest | Callout | Behind a **Write summary** button. One Claude call over the aggregates and a 60-comment sample; stored in the Sheet, so it is bought once |
| Overall sentiment | Donut, one per keyword | A part-to-whole split of a single dimension. Two keywords get two donuts, never one merged ring |
| Sentiment within each type | Stacked bar | Comparing categories, which bars do and arcs do not |
| Shorts against full videos | Grouped bar | Same reason, two groups side by side |
| Videos drawing negative comments | Ranked text | About specific videos, not a distribution. Minimum 10 analysed comments so one grumpy viewer cannot top the list; the transcript summary sits under each so the reason is visible |
| What resonated most | Quoted callouts | About two specific comments, which a sentence carries better than a chart |

Sentiment colours are fixed per sentiment and never cycled: blue for positive,
gray for neutral, orange for negative. Blue and orange rather than green and red
because green/red is the pair colour blindness hits hardest; the split is also
written out in words under each donut, so nothing depends on hue alone.

Nothing runs on arrival - this is the one pass that costs money per comment, so
it states the price and waits:

- It runs on whatever is in view, saved or not, so you can classify first and
  decide afterwards whether the search is worth keeping. Kinds for an unsaved
  search hold for the session only; save the search to store them.
- Comments that already carry a kind display straight away. The disclaimer and
  the button only appear while something in view is unclassified.
- Batches of 50, six in flight at once. The work is network wait, so running
  batches concurrently is most of the speed: about 190s sequential for 1,000
  comments against about 29s now.
- Cached in the `kind` and `sentiment` columns and never re-classified. A
  comment is only sent when one of the two is missing, so an interrupted run
  resumes from where it stopped rather than starting again.
- Classification is synchronous: leaving the view stops it. A note beside the
  progress bar says so, and says that stopping is safe.
- Results are written to the Sheet every four batches, and the part-finished
  frame is handed back to the session as each batch lands, so an interrupted
  run keeps its work in both places and the next click only sends what is
  genuinely still missing.
- Only what is on screen is classified. The keyword and Shorts/Videos filters
  scope the run; a keyword you are not looking at is never sent.
- Progress reads "Classifying... 340/612 comments" and climbs as each batch
  lands.

## The written summary

The Analysis digest costs a Claude call, so it is stored on a **`digests`** tab
rather than in memory: `fingerprint`, `keywords`, `comments`, `digest`,
`written_at`. The fingerprint is a hash of the comment ids and their verdicts,
so the same set of analysed comments always finds its stored summary, and any
change -- a new comment, a re-classification -- asks for a fresh one.

A restart therefore costs nothing. As with the tags, a summary is only stored
when every comment it describes is already in the Sheet; for an unsaved search
it is shown with a note that saving the search will keep it.

Nothing is written until someone presses **Write summary**, matching Classify
and Translate: every Claude call in the app is behind a button. Once written,
the summary shows on every later visit without the button returning, and a
changed comment set offers the button again rather than quietly re-buying.

Opening **Analysis** before classifying says so plainly -- "Classify comments
first to generate a summary" -- and the Classify button sits directly above it,
since that one pass fills both this tab and **By Kind**.

## Saving

A search writes nothing. Results appear in the session, and a green
**Save these results to Google Sheets** button sits under the run summary with
the note that nothing is saved yet. It only appears when there is something new
to save, so browsing previously stored data shows no button. Red is reserved for
**Run Search**, the one action that spends quota. Press it and the search's comments and its
video summaries go to the Sheet - deduped on `comment_id` and `video_id` as
before - and the button becomes a disabled **Saved to Google Sheets**.

Search again without saving and the previous results are simply gone at the end
of the session. There is no auto-save and no partial save.

While an unsaved search carries results that cost money to produce - kinds and
sentiments, translations, a written summary - a reminder sits at the top of the
results in every view, naming what is at risk. It never blocks anything; it just
stops the work being lost quietly. Saving clears it.

**Save is the only action that puts anything in the Sheet.** Classification and
translation write their columns onto rows the Sheet already holds and skip
everything else, so tagging an unsaved search changes nothing there -- not even
the header. The results still apply in the session; save the search to keep
them. Unsaved comment ids are tracked for the whole session, so an earlier
unsaved search does not become writable just because a later one was saved.

One consequence: translations and classifications of comments that were never
saved cannot persist. Save the search first if you want them to survive.



**Sort videos by** offers five orderings. The first four - Most relevant,
Newest first, Most viewed, Highest rated - are also YouTube search orders, so
they decide which videos a search collects *and* how the sections are ordered.
**Highest comments** has no YouTube equivalent (the Data API cannot search by
comment count), so it searches by relevance and sorts by `video_comment_count`
once the comments are stored.

Comment tagging (sentiment, category, flagged asks) and transcript summarising
both still run and are still stored - transcript summaries appear at the top of
each video section. Sentiment and flagged asks have no screen of their own in
this build; the code for them lives in `insights.py`, ready to resurface.

## Files

| File | Role |
|---|---|
| `app.py` | Streamlit UI - sidebar plus the single per-video drilldown |
| `youtube_fetcher.py` | `search_videos()`, `get_video_stats()`, `get_comments()`, `run_search()` |
| `sheets_store.py` | gspread read/write, dedupe on `comment_id`, tag write-back, videos tab |
| `insights.py` | Claude batch tagging, transcript summaries, sentiment counts, flagged asks |
| `transcripts.py` | Caption fetching via youtube-transcript-api (no YouTube quota) |

## API quota

The default YouTube quota is **10,000 units/day**:

- `search.list` - **100 units** per keyword (per page of 50)
- `videos.list` - 1 unit per batch of 50 videos
- `commentThreads.list` - 1 unit per page of 100 comments

Collection limits are fixed at **20 videos per keyword** and **500 comments per
video**, which puts the worst case at about **201 units per keyword** (100 search
+ up to 100 comment pages + 1 batched stats call) - roughly 49 keywords a day.
The sidebar shows a live estimate. Quota resets at midnight Pacific Time.

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

## Claude usage

Exactly two things call Claude, and nothing else does:

1. **Transcript summaries** - once per new video, during a search, when
   *Summarise transcripts* is ticked.
2. **Translation** - only when someone presses a Translate button.

Comment tagging (sentiment, category, flagged ask) is **not** run. The code for
it is still in `insights.py` and its three columns are still in the Sheet, but
nothing in the app invokes it, so it costs nothing. If it is ever wired back up,
`insights.analyze_dataframe()` sends untagged comments in batches of 25 and
writes these back:

| Column | Values |
|---|---|
| `sentiment` | `positive`, `neutral`, `negative` |
| `category` | `praise`, `complaint`, `recipe_request`, `price_mention`, `availability_question`, `health_question`, `nostalgia`, `other` |
| `flagged_ask` | Short phrase naming a specific request or complaint, e.g. "wants sugar-free version". Empty when the comment has no specific ask |

Only rows with a blank `sentiment` or `category` are sent, so tagged comments are
never paid for twice. An empty `flagged_ask` is a real result, not a gap, so it
does not re-queue a comment. `sentiment_counts()` and `flagged_asks()` turn the
stored tags into a per-keyword breakdown and an actionable list.

A sheet created before this feature existed is widened in place - the three
columns are appended to the header row on the next write, and existing rows fill
in as they get analysed.

## Translation

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

## Video transcript context

Each video new to a search gets its captions pulled through
`youtube-transcript-api`. That library scrapes the caption track the YouTube
player uses, so it is **separate from the Data API and costs no quota** - but it
is also unofficial and rate limited by IP. English captions are preferred; a
video captioned only in another language is still used, untranslated.

Failures are not all the same, and the app no longer pretends they are:

| Outcome | Meaning | What you see |
|---|---|---|
| `found` | Captions read | The summary |
| `unavailable` | No captions, disabled, private, age-gated. Permanent | "No transcript available for this video." |
| `blocked` | YouTube is throttling this IP. Temporary | "Transcript temporarily unavailable (rate limited). Try again later." |
| `error` | Network trouble, or the scraped page changed shape | "Transcript could not be read for this video." |

The run summary counts them separately, so a line like
`Transcripts: 0 found, 3 unavailable, 17 blocked (YouTube rate limit)` makes a
block obvious instead of looking like twenty videos without captions.

Two things guard against provoking a block: fetches are spaced
`TRANSCRIPT_DELAY_SECONDS` apart (1.5s) rather than fired back to back, and the
first block halts transcript fetching for the rest of that run. The remaining
videos are recorded as blocked rather than each being retried against an IP that
is already refusing.

Claude turns each transcript into a 2-3 sentence summary saying what the video is
about and what claims it makes, stored on the **`videos`** tab:

| Column | Meaning |
|---|---|
| `video_id` | The key. One row per video, never per comment |
| `video_summary` | The 2-3 sentence summary. Blank when there was no transcript |
| `transcript_language` | Language of the captions actually used |
| `transcript_is_english` | Whether those captions were English |

That summary then rides along with every comment on the video when Claude tags
it, so "not sweet enough" reads differently on a low-sugar product video than on
a regular one. In the main view it sits at the top of the video's section, so the
comments below it read in context.

The tab is append-only and keyed on `video_id`: a video already summarised is
skipped on later runs, so each transcript is fetched and paid for once.

## Known MVP limits

- Comments are fetched by `relevance`, so the 500-comment cap takes the top 500,
  not the newest 500.
- Comments longer than 2,000 characters are clipped before tagging; sentiment and
  intent sit in the opening lines, and this bounds the batch prompt.
- Transcripts longer than 12,000 characters are clipped before summarising.
- Transcripts are fetched one video at a time during a search, so a first run over
  fresh keywords is noticeably slower than one over videos already summarised.
- Non-English transcripts are summarised into English but not otherwise translated.
- No word-frequency or topic-cluster extraction.
- Reads the full id column on every save; fine to roughly ~50k rows, would need
  a local cache beyond that.
