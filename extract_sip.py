#!/usr/bin/env python3
"""
extract_sip.py — Process INDmoney mutual fund emails → Google Sheet

Searches Gmail for unprocessed MF transaction emails, extracts NAV/units/date
data, inserts rows into the sheet in date order, and relabels processed
threads to "Waiting".

All account-specific settings live in config.json and the fund→ticker mapping
in stockToTicker.json. Neither file is committed — copy the *.example.json
files and fill in your own values.

Usage:
    python3 extract_sip.py                      # live run
    python3 extract_sip.py --dry-run            # preview only, no changes made
    python3 extract_sip.py --config config2.json
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG: dict = {}  # populated in main() by load_config()
GOG = "gog"  # CLI binary, may be overridden via config "gog_path"

REQUIRED_KEYS = (
    "sheet_id",
    "sheet_name",
    "gog_path",
    "gmail_search",
    "label_waiting_id",
    "label_next_actions_id",
    "ticker_map_path",
)


def load_config(explicit: str | None) -> None:
    """Load config.json. Search order: --config, $SIP_TRACKER_CONFIG, ./config.json."""
    global CONFIG, GOG
    candidates = [
        explicit,
        os.environ.get("SIP_TRACKER_CONFIG"),
        str(Path(__file__).with_name("config.json")),
    ]
    cfg_path = next((c for c in candidates if c), None)
    if not cfg_path or not Path(cfg_path).exists():
        sys.exit(
            "config.json not found.\n"
            "Copy config.example.json → config.json and fill in your values\n"
            "(or pass --config path/to/config.json)."
        )
    with open(cfg_path) as f:
        loaded = json.load(f)
    missing = [k for k in REQUIRED_KEYS if not loaded.get(k)]
    if missing:
        sys.exit(
            f"Config is missing value(s): {', '.join(missing)}\n"
            f"Look at {cfg_path} — see config.example.json for what goes where."
        )
    CONFIG = loaded
    GOG = CONFIG["gog_path"]

    # Resolve ticker_map_path relative to the config file itself
    tpath = Path(CONFIG["ticker_map_path"])
    if not tpath.is_absolute():
        tpath = Path(cfg_path).parent / tpath
    if not tpath.exists():
        sys.exit(
            f"Ticker map not found: {tpath}\n"
            "Copy stockToTicker.example.json → stockToTicker.json and add your funds."
        )
    CONFIG["ticker_map_path"] = str(tpath)


def load_ticker_map(path: str) -> dict:
    """Load the fund→ticker map with names normalized for forgiving lookup."""
    with open(path) as f:
        raw = json.load(f)
    return {normalize_fund(k): v for k, v in raw.items()}


def normalize_fund(name: str) -> str:
    """Collapse whitespace + lowercase so ticker matching survives template drift."""
    return " ".join(name.split()).lower()


# ── HTML → plain text ─────────────────────────────────────────────────────────
class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    return re.sub(r"\s+", " ", " ".join(p.parts)).strip()


# ── Shell helpers ─────────────────────────────────────────────────────────────
def _run(*args) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([GOG] + list(args), capture_output=True, text=True)
    except FileNotFoundError as e:
        raise RuntimeError(
            f"'{GOG}' not found — install the gog CLI and make sure it is on your PATH"
        ) from e


def _run_json(*args) -> dict | list:
    r = _run(*args)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"unexpected output from gog ({e}): {r.stdout[:300]!r}"
        ) from e


# ── Gmail helpers ─────────────────────────────────────────────────────────────
def search_threads() -> list[dict]:
    return _run_json("gmail", "search", "-j", "--results-only", "--all", CONFIG["gmail_search"])


def get_thread_message_ids(thread_id: str) -> list[str]:
    data = _run_json("gmail", "thread", "get", "-j", thread_id)
    msgs = data.get("thread", {}).get("messages", [])
    return [m["id"] for m in msgs]


def fetch_message_text(msg_id: str) -> str:
    data = _run_json("gmail", "get", "-j", msg_id)
    return html_to_text(data.get("body", ""))


def relabel_thread(thread_id: str) -> bool:
    r = _run(
        "gmail",
        "thread",
        "modify",
        thread_id,
        "--add",
        CONFIG["label_waiting_id"],
        "--remove",
        CONFIG["label_next_actions_id"],
    )
    return r.returncode == 0


# ── Template classification ───────────────────────────────────────────────────
def classify(subject: str) -> str:
    sl = subject.lower()
    if "units allocated" in sl or "sip installment" in sl:
        return "sip_installment"
    if "units allotted" in sl and ("buy order" in sl or "mutual fund" in sl):
        return "order_successful"
    if "auto payment" in sl:
        return "skip_auto_payment"
    return "skip_unknown"


# ── Field parsers ─────────────────────────────────────────────────────────────
def _dd_mm_yyyy(s: str) -> str:
    """'05/03/2026' → '2026-03-05'. Zero-padded so string sort and dedup work.

    Raises ValueError on anything unexpected so the caller reports it instead
    of silently writing a malformed date to the sheet.
    """
    return datetime.strptime(s.strip(), "%d/%m/%Y").strftime("%Y-%m-%d")


def _dd_mon_yyyy(s: str) -> str:
    return datetime.strptime(s.strip(), "%d %b %Y").strftime("%Y-%m-%d")


def parse_sip_installment(text: str, ticker_map: dict) -> dict | None:
    fund_m = re.search(r" in (.+?) is successful\.", text)
    date_m = re.search(r"SIP Date\s+([\d/]+)", text)
    nav_m = re.search(r"NAV\s+([\d.]+)", text)
    units_m = re.search(r"Units allocated\s+([\d.]+)", text)
    money_m = re.search(r"SIP Amount received\s+₹([\d,]+)", text)

    if not all([fund_m, date_m, nav_m, units_m, money_m]):
        return None

    fund = fund_m.group(1).strip()
    return {
        "date": _dd_mm_yyyy(date_m.group(1).strip()),
        "rate": nav_m.group(1),
        "fund": fund,
        "units": units_m.group(1),
        "ticker": ticker_map.get(normalize_fund(fund), "UNKNOWN"),
        "money": money_m.group(1).replace(",", ""),
    }


def parse_order_successful(text: str, ticker_map: dict) -> dict | None:
    fund_m = re.search(r" in (.+?) have been successfully allotted", text)
    date_m = re.search(r"Order date\s+(\d{1,2}\s+\w+\s+\d{4})", text)
    nav_m = re.search(r"Allotted NAV\s+([\d.]+)", text)
    units_m = re.search(r"Units allot(?:t?)ed\s+([\d.]+)", text)
    money_m = re.search(r"Buy Amount\s+₹([\d,]+)", text)

    if not all([fund_m, date_m, nav_m, units_m, money_m]):
        return None

    fund = fund_m.group(1).strip()
    return {
        "date": _dd_mon_yyyy(date_m.group(1)),
        "rate": nav_m.group(1),
        "fund": fund,
        "units": units_m.group(1),
        "ticker": ticker_map.get(normalize_fund(fund), "UNKNOWN"),
        "money": money_m.group(1).replace(",", ""),
    }


# ── Sheet helpers ─────────────────────────────────────────────────────────────
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def read_sheet_rows() -> list[dict]:
    """Return [{row (1-indexed), date, fund}] for every data row in the sheet."""
    raw = _run_json(
        "sheets",
        "get",
        "-j",
        "--results-only",
        CONFIG["sheet_id"],
        f"{CONFIG['sheet_name']}!A:C",
    )
    result = []
    for i, r in enumerate(raw, start=1):
        if r and _DATE_RE.match(str(r[0])):
            result.append(
                {
                    "row": i,
                    "date": str(r[0]),
                    "fund": str(r[2]) if len(r) > 2 else "",
                }
            )
    return result


def find_insert_row(sheet_rows: list[dict], target_date: str) -> int:
    """Row number AFTER the last existing row whose date <= target_date.

    If target_date predates every existing row, insert directly below the
    header (row 2) so the sheet stays in date order.
    """
    best = None
    for r in sheet_rows:
        if r["date"] <= target_date:
            best = r["row"]
    if best is not None:
        return best + 1
    return 2  # older than everything → top of sheet, just under the header


def _write_rows_at(values: list[list], at_row: int) -> bool:
    n = len(values)
    r = _run(
        "sheets",
        "update",
        CONFIG["sheet_id"],
        f"{CONFIG['sheet_name']}!A{at_row}:F{at_row + n - 1}",
        "--values-json",
        json.dumps(values),
        "--input=USER_ENTERED",
    )
    return r.returncode == 0


def _insert_blank_rows(at_row: int, count: int) -> bool:
    r = _run(
        "sheets",
        "insert",
        CONFIG["sheet_id"],
        CONFIG["sheet_name"],
        "ROWS",
        str(at_row),
        f"--count={count}",
    )
    return r.returncode == 0


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--config",
        help="Path to config file (default: config.json next to this script)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written without making any changes",
    )
    args = ap.parse_args()
    dry = args.dry_run

    load_config(args.config)
    ticker_map = load_ticker_map(CONFIG["ticker_map_path"])

    if dry:
        print("── DRY RUN — no changes will be made ──────────────────────────")

    # ── Phase 1: Discover ──────────────────────────────────────────────────────
    print("\n[1/6] Searching Gmail …")
    threads = search_threads()
    print(f"      {len(threads)} thread(s) matched")

    # ── Phase 2: Extract ───────────────────────────────────────────────────────
    print("\n[2/6] Extracting fields from messages …")

    class _Thread:
        def __init__(self, tid, subject):
            self.tid = tid
            self.subject = subject
            self.rows = []
            self.fully_ok = True  # flipped if any message fails to parse

    processable = []  # _Thread objects with at least one row
    skipped = []  # (tid, subject, reason) — not processed at all

    for t in threads:
        tid = t["id"]
        subject = t.get("subject", "")
        count = t.get("messageCount", 1)
        kind = classify(subject)

        if kind == "skip_auto_payment":
            skipped.append((tid, subject, "SIP Auto Payment — units not yet allocated"))
            continue
        if kind == "skip_unknown":
            skipped.append((tid, subject, "unrecognised subject"))
            continue

        parse_fn = (
            parse_sip_installment
            if kind == "sip_installment"
            else parse_order_successful
        )

        try:
            msg_ids = [tid] if count == 1 else get_thread_message_ids(tid)
        except RuntimeError as e:
            skipped.append((tid, subject, f"thread fetch error: {e}"))
            continue

        thread = _Thread(tid, subject)
        for mid in msg_ids:
            try:
                row = parse_fn(fetch_message_text(mid), ticker_map)
            except Exception as e:
                print(f"  [!] Parse error in message {mid}: {e}")
                thread.fully_ok = False
                continue

            if row is None:
                print(f"  [!] Missing fields in message {mid} (thread {tid})")
                thread.fully_ok = False
                continue
            if row["ticker"] == "UNKNOWN":
                print(
                    f"  [!] UNKNOWN ticker for fund: {row['fund']!r} — "
                    f"add it to stockToTicker.json"
                )
                thread.fully_ok = False
                continue

            thread.rows.append(row)

        if thread.rows:
            processable.append(thread)
        else:
            skipped.append(
                (
                    tid,
                    subject,
                    (
                        "parse failures on all messages"
                        if not thread.fully_ok
                        else "no rows extracted"
                    ),
                )
            )

    total_extracted = sum(len(p.rows) for p in processable)
    print(
        f"      {total_extracted} row(s) from {len(processable)} processable thread(s)"
    )

    if not processable:
        print("\nNothing to process.")
        _print_skipped(skipped)
        return

    # ── Phase 3: Deduplicate ───────────────────────────────────────────────────
    print("\n[3/6] Reading sheet for deduplication …")
    sheet_rows = read_sheet_rows()
    existing = {(r["date"], r["fund"]) for r in sheet_rows}
    last_row = sheet_rows[-1]["row"] if sheet_rows else 1
    last_date = sheet_rows[-1]["date"] if sheet_rows else ""
    print(
        f"      {len(sheet_rows)} data row(s) in sheet; last is row {last_row} ({last_date})"
    )

    # Items to add: {data, tid}
    new_items = []
    dupe_count = 0
    # Track thread IDs that have at least one new (non-dupe) row
    tids_with_new_rows = set()
    # Dedup against rows already found in *this* batch too (same txn in 2 threads)
    seen_new = set()

    for p in processable:
        for row in p.rows:
            key = (row["date"], row["fund"])
            if key in existing or key in seen_new:
                dupe_count += 1
            else:
                seen_new.add(key)
                new_items.append({"data": row, "tid": p.tid})
                tids_with_new_rows.add(p.tid)

    if dupe_count:
        print(f"      {dupe_count} duplicate(s) already in sheet — skipped")
    print(f"      {len(new_items)} new row(s) to add")

    if not new_items:
        # Nothing new to write, but still relabel fully-processed threads
        fully_ok_tids = {p.tid for p in processable if p.fully_ok}
        print(
            f"\nAll rows already in sheet. Relabeling {len(fully_ok_tids)} thread(s) …"
        )
        _do_relabel(fully_ok_tids, dry)
        _print_skipped(skipped)
        return

    # ── Phase 4: Insert in date order ─────────────────────────────────────────
    print("\n[4/6] Inserting rows in date order …")

    new_items.sort(key=lambda x: (x["data"]["date"], x["data"]["fund"]))

    # Classify each item: end-of-sheet append vs mid-sheet insert
    end_items = []
    mid_groups = defaultdict(list)  # insert_pos → [item, ...]

    for item in new_items:
        pos = find_insert_row(sheet_rows, item["data"]["date"])
        if pos > last_row:
            end_items.append(item)
        else:
            mid_groups[pos].append(item)

    written_tids = set()
    written_rows = 0
    mid_rows_inserted = 0  # tracks actual blank rows added (to offset next_row)

    # Mid-sheet insertions — process bottom-to-top so row numbers stay valid
    for pos in sorted(mid_groups.keys(), reverse=True):
        group = sorted(
            mid_groups[pos], key=lambda x: (x["data"]["date"], x["data"]["fund"])
        )
        values = _to_sheet_values(group)
        _print_group("Mid-insert", values, pos, dry)

        if dry:
            mid_rows_inserted += len(group)
            written_rows += len(group)
            for item in group:
                written_tids.add(item["tid"])
            continue

        ok_ins = _insert_blank_rows(pos, len(group))
        if ok_ins:
            mid_rows_inserted += len(group)
            if _write_rows_at(values, pos):
                written_rows += len(group)
                for item in group:
                    written_tids.add(item["tid"])
            else:
                print(
                    f"        [!] Blank rows inserted but write failed — "
                    f"sheet has {len(group)} blank row(s) at {pos}"
                )
        else:
            print(f"        [!] Insert failed at row {pos}")

    # End-of-sheet append
    if end_items:
        # All mid insertions above last_row shift it down
        next_row = last_row + mid_rows_inserted + 1
        values = _to_sheet_values(end_items)
        _print_group("Append", values, next_row, dry)

        success = True
        if not dry and not _write_rows_at(values, next_row):
            success = False
            print("        [!] Append failed")
        if success:
            written_rows += len(end_items)
            for item in end_items:
                written_tids.add(item["tid"])

    # ── Phase 5: Relabel ───────────────────────────────────────────────────────
    fully_ok_tids = {p.tid for p in processable if p.fully_ok}
    # Relabel if: fully parsed AND (rows written OR all rows were already in sheet)
    all_dupe_tids = fully_ok_tids - tids_with_new_rows
    tids_to_relabel = (written_tids | all_dupe_tids) & fully_ok_tids

    print(f"\n[5/6] Relabeling {len(tids_to_relabel)} thread(s) …")
    _do_relabel(tids_to_relabel, dry)

    # ── Phase 6: Report ────────────────────────────────────────────────────────
    print(f"\n[6/6] Done{' (dry-run)' if dry else ''}")
    print(f"      New rows added    : {written_rows} of {len(new_items)}")
    print(
        f"      Threads relabeled : {len(tids_to_relabel)}"
        + (" (simulated)" if dry else "")
    )
    _print_skipped(skipped)
    print()


# ── Helpers ───────────────────────────────────────────────────────────────────
def _to_sheet_values(items: list[dict]) -> list[list]:
    return [
        [
            i["data"]["date"],
            i["data"]["rate"],
            i["data"]["fund"],
            i["data"]["units"],
            i["data"]["ticker"],
            i["data"]["money"],
        ]
        for i in items
    ]


def _print_group(label: str, values: list[list], at_row: int, dry: bool):
    prefix = "[dry-run] " if dry else ""
    print(f"      {prefix}{label} {len(values)} row(s) at row {at_row}:")
    for v in values:
        print(f"        {v[0]}  {v[2]}")


def _do_relabel(tids: set, dry: bool):
    ok = fail = 0
    for tid in tids:
        if dry:
            ok += 1
            continue
        if relabel_thread(tid):
            ok += 1
        else:
            fail += 1
            print(f"  [!] Failed to relabel thread {tid}")
    status = " (simulated)" if dry else ""
    print(f"      {ok} relabeled{status}" + (f", {fail} failed" if fail else ""))


def _print_skipped(skipped: list):
    if skipped:
        print(f"\n      Skipped {len(skipped)} thread(s):")
        for tid, subj, reason in skipped:
            print(f"        [{reason}]  {subj[:70]}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nAborted.")
    except RuntimeError as e:
        sys.exit(f"Error: {e}")
