# indmoney-sip-mail-tracker

Reads INDmoney mutual fund transaction emails from Gmail and logs each SIP
installment / purchase into a Google Sheet.

## How it works

1. Finds Gmail threads under your "Next Actions" label, sent by INDmoney.
2. Classifies each thread by subject: SIP installment, buy order success.
   Auto-payment confirmations (units not yet allocated) and unknown subjects
   are skipped.
3. Parses date, NAV, units, and amount from the email body; maps the fund
   name to an INDMoney ticker code via `stockToTicker.json`.
4. Deduplicates against existing sheet rows, then inserts the new rows in
   date order (mid-sheet inserts or end-of-sheet appends).
5. Relabels fully-processed threads to your "Waiting" label.

Partially-failed threads are left in place and get a second chance on the
next run (already-written rows are skipped as duplicates).

## Requirements

- Python 3.10+
- [gog](https://gogcli.sh/) CLI, signed in to the Google account
  that owns both the Gmail labels and the sheet:

  ```sh
  brew install openclaw/tap/gogcli
  ```

## Setup

1. Copy the example config and fill in your values:

   ```sh
   cp config.example.json config.json
   cp stockToTicker.example.json stockToTicker.json
   ```

2. Fill `config.json`:

   | Key | What to put | Example |
   |---|---|---|
   | `sheet_id` | Spreadsheet ID from its URL | `1HgZfga…0ntd0M` |
   | `sheet_name` | Worksheet (tab) name | `mutual_funds` |
   | `gog_path` | gog binary path or name | `gog` |
   | `gmail_search` | Gmail query for finding unprocessed threads | `label:status-next-actions from:indmoney` |
   | `label_waiting_id` | gog label ID for your "Waiting" label | `Label_4966…` |
   | `label_next_actions_id` | gog label ID for your "Next Actions" label | `Label_3707…` |
   | `ticker_map_path` | Path to the fund→ticker map | `stockToTicker.json` |

   The sheet is expected to have: a header in row 1, and data columns
   `A` date (`YYYY-MM-DD`), `B` NAV, `C` fund name, `D` units, `E` ticker,
   `F` amount.

   Label IDs are per Gmail account — list them with gog's label command
   (e.g. `gog gmail label list`) and pick the IDs that look like
   `Label_4966281429150690975`.

3. Add your funds to `stockToTicker.json`: fund name exactly as INDMoney
   writes it in the email → the INDMoney ticker code. Matching is
   whitespace/case tolerant, but the wording must be identical.

## Usage

```sh
python3 extract_sip.py --dry-run   # preview: shows rows it would insert, no changes
python3 extract_sip.py             # real run
```

Cron example (once per day during market hours):

```cron
0 9 * * * cd /path/to/indmoney-sip-mail-tracker && /usr/bin/python3 extract_sip.py
```

## Privacy notes

`config.json` (your sheet ID, Gmail label IDs) and `stockToTicker.json`
(your fund holdings) are personal — both are gitignored and must not be
committed. There are no separate secrets: authentication is handled by `gog`.
