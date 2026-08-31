"""Google Sheets persistence for the Social Listening app.

Credentials come from Streamlit secrets -- never from a file path on disk, so the
app deploys to Streamlit Cloud unchanged.

The store is deliberately dumb: one flat worksheet, `comment_id` as the primary
key, and an append-only write path that filters out ids the sheet already has.
"""

from __future__ import annotations

import datetime as _dt
from typing import Sequence

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound

from insights import TAG_COLUMNS
from youtube_fetcher import COLUMNS, ID_COLUMN

# The sheet carries the fetched columns plus everything Claude writes back --
# the sentiment tags and the translation. Those go last so a sheet written
# before they existed can be widened in place.
SHEET_COLUMNS: list[str] = COLUMNS + TAG_COLUMNS

# Read/write on Sheets only. Drive scope is required for open_by_key on some
# service accounts, so it is included read-only.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

SECRET_SERVICE_ACCOUNT = "gcp_service_account"
SECRET_SHEET_KEY = "SHEET_KEY"
SECRET_WORKSHEET = "WORKSHEET_NAME"
DEFAULT_WORKSHEET = "comments"


# Columns that should come back as numbers, not strings, after a round-trip
# through Sheets (everything arrives as text).
_NUMERIC_COLUMNS = [
    "video_views",
    "video_likes",
    "video_comment_count",
    "comment_likes",
    "reply_count",
]
_DATETIME_COLUMNS = ["comment_published_at", "video_published_at", "fetched_at"]

# Sheets rejects very large single requests; append and update in slices.
_APPEND_CHUNK = 500
_UPDATE_CHUNK = 300


class SheetsError(Exception):
    """Raised for configuration or access problems the user needs to fix."""


def is_configured() -> bool:
    """True when secrets carry everything needed to reach the sheet."""
    try:
        return (
            SECRET_SERVICE_ACCOUNT in st.secrets
            and bool(st.secrets.get(SECRET_SHEET_KEY, ""))
        )
    except Exception:
        # st.secrets raises if no secrets.toml exists at all.
        return False


@st.cache_resource(show_spinner=False)
def _get_client() -> gspread.Client:
    """Authorise gspread from the service account block in secrets.

    Cached as a resource so we authorise once per session rather than per rerun.
    """
    try:
        info = dict(st.secrets[SECRET_SERVICE_ACCOUNT])
    except (KeyError, FileNotFoundError) as exc:
        raise SheetsError(
            f"Missing [{SECRET_SERVICE_ACCOUNT}] in .streamlit/secrets.toml. "
            "Copy secrets.toml.example and fill in the service account fields."
        ) from exc

    # TOML escapes newlines in the private key as literal backslash-n; undo that.
    if "private_key" in info:
        info["private_key"] = str(info["private_key"]).replace("\\n", "\n")

    try:
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        return gspread.authorize(creds)
    except Exception as exc:
        raise SheetsError(
            f"Could not authenticate the Google service account: {exc}"
        ) from exc


@st.cache_resource(show_spinner=False)
def _get_spreadsheet(sheet_key: str) -> gspread.Spreadsheet:
    """Open the spreadsheet once per session.

    open_by_key is a network round trip, and every read used to pay it again --
    loading the comments and the video summaries opened the same file twice for
    about two seconds each. The handle is just an id, so caching it is safe.
    """
    client = _get_client()
    try:
        return client.open_by_key(sheet_key)
    except SpreadsheetNotFound as exc:
        raise SheetsError(
            f"No Sheet found with key '{sheet_key}'. Check {SECRET_SHEET_KEY} in secrets."
        ) from exc
    except APIError as exc:
        if "PERMISSION_DENIED" in str(exc) or "403" in str(exc):
            email = st.secrets.get(SECRET_SERVICE_ACCOUNT, {}).get(
                "client_email", "the service account"
            )
            raise SheetsError(
                f"Access denied to the Sheet. Share it with {email} as an Editor."
            ) from exc
        raise SheetsError(f"Google Sheets API error: {exc}") from exc


def _sheet_key() -> str:
    key = str(st.secrets.get(SECRET_SHEET_KEY, "")).strip()
    if not key:
        raise SheetsError(
            f"{SECRET_SHEET_KEY} is not set in secrets. Paste the long id from the "
            "Sheet URL: docs.google.com/spreadsheets/d/<SHEET_KEY>/edit"
        )
    return key


@st.cache_resource(show_spinner=False)
def _get_worksheet_cached(sheet_key: str, worksheet_name: str) -> gspread.Worksheet:
    """The comments tab, header checked and widened once per session."""
    spreadsheet = _get_spreadsheet(sheet_key)

    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=worksheet_name, rows=1000, cols=len(SHEET_COLUMNS)
        )
        worksheet.update(range_name="A1", values=[SHEET_COLUMNS])
        return worksheet

    # An existing but empty tab still needs its header.
    header = worksheet.row_values(1)
    if not header:
        worksheet.update(range_name="A1", values=[SHEET_COLUMNS])
        return worksheet

    _widen_header(worksheet, header)
    return worksheet


def _get_worksheet() -> gspread.Worksheet:
    """Open the configured worksheet, creating it with a header row if absent."""
    worksheet_name = str(
        st.secrets.get(SECRET_WORKSHEET, DEFAULT_WORKSHEET)
    ).strip() or DEFAULT_WORKSHEET
    return _get_worksheet_cached(_sheet_key(), worksheet_name)


def _column_letter(index: int) -> str:
    """1-based column number to its A1 letter, e.g. 1 -> A, 27 -> AA."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _widen_header(worksheet: gspread.Worksheet, header: Sequence[str]) -> list[str]:
    """Append any missing columns to an existing header row.

    Sheets written before the Insights tab existed have no tag columns. Missing
    ones are appended rather than the header being rewritten, so a sheet whose
    columns were reordered by hand keeps working.
    """
    missing = [column for column in SHEET_COLUMNS if column not in header]
    if not missing:
        return list(header)

    widened = list(header) + missing
    if worksheet.col_count < len(widened):
        worksheet.add_cols(len(widened) - worksheet.col_count)
    start = _column_letter(len(header) + 1)
    end = _column_letter(len(widened))
    try:
        worksheet.update(range_name=f"{start}1:{end}1", values=[missing])
    except APIError as exc:
        raise SheetsError(f"Could not add the tag columns to the Sheet: {exc}") from exc
    return widened


@st.cache_resource(show_spinner=False)


@st.cache_resource(show_spinner=False)


def to_dataframe(rows: Sequence[dict]) -> pd.DataFrame:
    """Build a schema-stable, correctly typed DataFrame from raw row dicts."""
    df = pd.DataFrame(list(rows), columns=SHEET_COLUMNS)
    return _coerce_types(df)


def _coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    """Sheets hands everything back as text -- restore numbers, dates, and bools.

    Sorting by likes or date only behaves if these are real types rather than strings.
    """
    if df.empty:
        return df

    for column in _NUMERIC_COLUMNS:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0).astype(int)

    for column in _DATETIME_COLUMNS:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], errors="coerce", utc=True, format="mixed")

    for column in TAG_COLUMNS:
        if column in df.columns:
            df[column] = df[column].fillna("").astype(str).str.strip()

    if "is_reply" in df.columns:
        df["is_reply"] = (
            df["is_reply"].astype(str).str.strip().str.lower().isin(["true", "1", "yes"])
        )

    return df


def load_all() -> pd.DataFrame:
    """Read every stored row back into a DataFrame.

    Returns an empty, correctly-shaped frame when the sheet has only a header,
    so callers never need to special-case the first run.
    """
    worksheet = _get_worksheet()
    try:
        values = worksheet.get_all_values()
    except APIError as exc:
        raise SheetsError(f"Could not read the Sheet: {exc}") from exc

    if len(values) < 2:
        return _coerce_types(pd.DataFrame(columns=SHEET_COLUMNS))

    header, *data = values
    df = pd.DataFrame(data, columns=header)

    # Tolerate a sheet whose columns were reordered or extended by hand.
    for column in SHEET_COLUMNS:
        if column not in df.columns:
            df[column] = ""
    df = df[SHEET_COLUMNS]

    df = df[df[ID_COLUMN].astype(str).str.strip() != ""]
    return _coerce_types(df.reset_index(drop=True))


def existing_ids() -> set[str]:
    """Read just the id column -- far cheaper than pulling the whole sheet."""
    worksheet = _get_worksheet()
    try:
        column_index = COLUMNS.index(ID_COLUMN) + 1
        values = worksheet.col_values(column_index)
    except APIError as exc:
        raise SheetsError(f"Could not read existing comment ids: {exc}") from exc
    # Drop the header cell and any blanks.
    return {value.strip() for value in values[1:] if value and value.strip()}


def append_rows(rows: Sequence[dict]) -> tuple[int, int]:
    """Append rows to the sheet, skipping any comment_id already stored.

    Dedupe happens on two levels: against the ids already in the sheet, and
    within the incoming batch itself (the same comment can surface under two
    different keywords in one run).

    Returns (appended_count, skipped_count).
    """
    if not rows:
        return 0, 0

    worksheet = _get_worksheet()
    known = existing_ids()

    # Write in the order the Sheet's own header is in, not the order this
    # module happens to list. _widen_header appends new columns at the end to
    # avoid disturbing a hand-edited sheet, so the two orders drift apart the
    # moment a column is added anywhere but the end. Writing by position
    # against the wrong order silently files every value one column across.
    header = _widen_header(worksheet, worksheet.row_values(1))

    fresh: list[list] = []
    seen_in_batch: set[str] = set()
    skipped = 0

    for row in rows:
        comment_id = str(row.get(ID_COLUMN, "")).strip()
        if not comment_id or comment_id in known or comment_id in seen_in_batch:
            skipped += 1
            continue
        seen_in_batch.add(comment_id)
        fresh.append([_cell(row.get(column, "")) for column in header])

    if not fresh:
        return 0, skipped

    try:
        for start in range(0, len(fresh), _APPEND_CHUNK):
            worksheet.append_rows(
                fresh[start : start + _APPEND_CHUNK],
                # RAW matters: a comment starting with "=" or "+" would otherwise
                # be parsed by Sheets as a formula.
                value_input_option="RAW",
            )
    except APIError as exc:
        raise SheetsError(f"Could not write to the Sheet: {exc}") from exc

    return len(fresh), skipped


def _cell(value: object) -> str | int | float:
    """Coerce a value into something the Sheets API accepts."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return str(value)


def update_analysis(df: pd.DataFrame) -> int:
    """Write the Claude tags back onto rows that already exist in the Sheet.

    Matches on comment_id and writes only the analysis columns, so a concurrent
    fetch appending new rows cannot be clobbered. Returns the number of rows
    updated. Rows whose tags are all blank are skipped -- there is nothing to
    save, and blanking a cell would just re-queue the comment for analysis.
    """
    if df.empty:
        return 0

    present = [column for column in TAG_COLUMNS if column in df.columns]
    if not present:
        return 0

    worksheet = _get_worksheet()
    try:
        header = _widen_header(worksheet, worksheet.row_values(1))
        id_column = header.index(ID_COLUMN) + 1
        ids = worksheet.col_values(id_column)
    except ValueError as exc:
        raise SheetsError(
            f"The Sheet has no '{ID_COLUMN}' column - cannot match rows to update."
        ) from exc
    except APIError as exc:
        raise SheetsError(f"Could not read the Sheet before updating: {exc}") from exc

    # comment_id -> 1-based sheet row. Later duplicates lose to the first row.
    row_of: dict[str, int] = {}
    for offset, value in enumerate(ids[1:], start=2):
        key = str(value).strip()
        if key and key not in row_of:
            row_of[key] = offset

    # The tag columns are contiguous in SHEET_COLUMNS but need not be in a
    # hand-edited sheet, so each one is written as its own single-cell range.
    updates: list[dict] = []
    touched: set[int] = set()
    for _, record in df.iterrows():
        row_number = row_of.get(str(record.get(ID_COLUMN, "")).strip())
        if row_number is None:
            continue
        values = {column: str(record.get(column, "") or "").strip() for column in present}
        if not any(values.values()):
            continue
        for column, value in values.items():
            letter = _column_letter(header.index(column) + 1)
            updates.append({"range": f"{letter}{row_number}", "values": [[value]]})
        touched.add(row_number)

    if not updates:
        return 0

    try:
        for start in range(0, len(updates), _UPDATE_CHUNK):
            worksheet.batch_update(
                updates[start : start + _UPDATE_CHUNK],
                value_input_option="RAW",
            )
    except APIError as exc:
        raise SheetsError(f"Could not write the tags to the Sheet: {exc}") from exc

    return len(touched)


def sheet_url() -> str:
    """Direct link to the configured Sheet, for a 'view in Sheets' link in the UI."""
    key = str(st.secrets.get(SECRET_SHEET_KEY, "")).strip()
    return f"https://docs.google.com/spreadsheets/d/{key}/edit" if key else ""
