"""Repair comment rows that were written one column out of place.

Rows appended between the addition of `video_type` to the schema and the fix
to `append_rows` were written in this module's canonical column order while
the Sheet's own header had the newer columns appended at the end. Every value
from `comment_author` onward therefore sits one column to the left of where it
belongs: the Author column reads "Video", and the Comment column holds the
author's handle.

The raw cells are intact, just filed wrongly, so each affected row can be read
back by zipping its values against the canonical order and rewritten under the
Sheet's real header.

    python repair_shifted_rows.py            # report only, writes nothing
    python repair_shifted_rows.py --apply    # rewrite the affected rows
"""

from __future__ import annotations

import sys

import sheets_store

# A shifted row has a video_type value sitting in the author column. Real
# authors are handles, so this never matches an intact row.
VIDEO_TYPES = {"Video", "Short"}

CHUNK = 200


def _shifted(row: list[str], at: dict[str, int]) -> bool:
    return row[at["comment_author"]].strip() in VIDEO_TYPES


def main(apply: bool) -> int:
    worksheet = sheets_store._get_worksheet()
    values = worksheet.get_all_values()
    if len(values) < 2:
        print("nothing in the sheet")
        return 0

    header, data = values[0], values[1:]
    at = {name: i for i, name in enumerate(header)}
    for needed in ("comment_author", "comment_text"):
        if needed not in at:
            print(f"the sheet has no {needed} column; nothing to do")
            return 1

    canonical = sheets_store.SHEET_COLUMNS
    updates: list[dict] = []
    shifted = 0

    for offset, raw in enumerate(data, start=2):
        raw = list(raw) + [""] * (len(header) - len(raw))
        if not _shifted(raw, at):
            continue
        shifted += 1
        # The row was written in canonical order, so reading it that way gives
        # the record back exactly as it was meant to be stored.
        record = dict(zip(canonical, raw))
        rebuilt = [sheets_store._cell(record.get(name, "")) for name in header]
        end = sheets_store._column_letter(len(header))
        updates.append({"range": f"A{offset}:{end}{offset}", "values": [rebuilt]})

    print(f"rows in sheet : {len(data):,}")
    print(f"shifted rows  : {shifted:,}")
    if not shifted:
        return 0

    sample = data[[i for i, r in enumerate(data)
                   if _shifted(list(r) + [""] * (len(header) - len(r)), at)][0]]
    sample = list(sample) + [""] * (len(header) - len(sample))
    fixed = dict(zip(canonical, sample))
    print("\nexample repair:")
    for name in ("comment_author", "comment_text", "comment_likes", "video_type"):
        print(f"  {name:<20} {sample[at[name]][:44]!r:<48} -> {str(fixed.get(name, ''))[:44]!r}")

    if not apply:
        print(f"\nreport only. re-run with --apply to rewrite {shifted:,} row(s).")
        return 0

    print(f"\nrewriting {shifted:,} row(s)...")
    for start in range(0, len(updates), CHUNK):
        worksheet.batch_update(
            updates[start : start + CHUNK], value_input_option="RAW"
        )
        print(f"  {min(start + CHUNK, len(updates)):,}/{len(updates):,}")
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
