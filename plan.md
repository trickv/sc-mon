# sc-mon — Space Coast Launch Monitor

## Context

The user is visiting Florida's Space Coast for the next ~3 weeks and wants two
overlapping things from this little tool:

1. **Practical:** be told (hourly) whenever something on the upcoming schedule
   at Cape Canaveral SFS or Kennedy Space Center *changes* — a new launch
   appears, a date/time gets pinned down, or an existing entry slips.
2. **Analytical:** answer the question *"how far in advance does SpaceX
   actually commit to a real T-0?"* by logging the moment each SpaceX mission
   first transitions from a vague NET ("May", "Q2", "TBD") to a precise
   timestamp (Second / Minute precision).

`nextspaceflight.com` itself is server-rendered Next.js with minified Tailwind
markup — fragile to scrape, no public API (their `api.nextspaceflight.com`
is gated; they ask people to email `api@nextspaceflight.com`). However,
nextspaceflight.com **is fed by the public Launch Library 2 API** from
TheSpaceDevs — same data, same field semantics, free, no key required for
modest use, ~15 req/hour rate limit (we'll use 1). That's our source.

Working directory `~/src/github.com/trickv/sc-mon` is empty; this is a green-
field tiny project.

## Data source

- Endpoint: `https://ll.thespacedevs.com/2.2.0/launch/upcoming/?location__ids=12,27&limit=50&mode=detailed`
  - `12` = Cape Canaveral SFS, FL
  - `27` = Kennedy Space Center, FL
  - confirmed via `/2.2.0/location/?search=...`
- Per-launch fields we care about:
  - `id` (UUID, stable key)
  - `name` — "Falcon 9 Block 5 | Starlink Group 10-31"
  - `net` (ISO 8601) — target launch time
  - `net_precision.abbrev` — **the key signal**: `SEC`, `MIN`, `HR`, `DAY`,
    `MONTH`, `QTR`, `YEAR`, `NET` (NET = "no earlier than, fuzzy")
  - `window_start`, `window_end`
  - `status.abbrev` (`Go`, `TBC`, `TBD`, `Success`, `Failure`, `Hold`, …)
  - `launch_service_provider.name` — "SpaceX", "ULA", "Blue Origin", …
  - `pad.name`, `pad.location.name`
  - `last_updated`
- Rate-limit headroom: 1 call/hour out of ~15/hour allowed. Send a polite
  `User-Agent: sc-mon/0.1 (trick@vanstaveren.us)`.

## Design

A single Python 3 script + a small JSON state file. No framework, no deps
outside the stdlib — `urllib.request` for HTTP, `json` for parsing,
`subprocess` for handing the email body to `msmtp`. Stdlib-only keeps cron
deployment trivial (no venv to babysit).

### Files

```
~/src/github.com/trickv/sc-mon/
├── sc-mon.py            # the whole tool
├── README.md            # one-screen: what it does, how to install cron entry
└── data/                # gitignored, created on first run
    ├── state.json       # last seen snapshot of each launch (keyed by UUID)
    ├── history.jsonl    # append-only event log: every observed change
    └── pending-email.txt  # only written if msmtp is unconfigured
```

### State model

`state.json` maps launch UUID → last-seen snapshot:

```json
{
  "ef65c43b-...": {
    "name": "Falcon 9 Block 5 | Starlink Group 10-31",
    "lsp": "SpaceX",
    "pad": "Space Launch Complex 40",
    "location": "Cape Canaveral SFS, FL, USA",
    "net": "2026-05-21T10:04:20Z",
    "net_precision": "SEC",
    "status": "Go",
    "window_start": "2026-05-21T09:26:00Z",
    "window_end": "2026-05-21T13:26:00Z",
    "last_updated_remote": "2026-05-21T11:11:52Z",
    "first_seen_at": "2026-05-18T14:00:00Z",
    "first_pinned_at": "2026-05-20T03:00:00Z"
  }
}
```

`first_pinned_at` is set the **first time** `net_precision` becomes `SEC` or
`MIN` for that UUID — this is the headline number for the SpaceX analysis.
Once set, never overwritten (even if the launch later regresses to TBD,
which does happen).

### `history.jsonl` — append-only event log

One JSON object per line, written every run that detects a change. Event
types: `appeared`, `disappeared` (launch flew or was scrubbed off the
schedule), `net_changed`, `precision_changed`, `status_changed`,
`first_pinned`. Each row carries the UUID, name, lsp, an ISO timestamp of
when we observed it, and the old → new values. This file is the durable
artifact for answering the analysis question.

### Change detection

For each launch in the API response:

1. If UUID not in state → emit `appeared` event. If `net_precision` is
   already `SEC`/`MIN`, also emit `first_pinned` and stamp `first_pinned_at`.
2. If UUID known → diff `net`, `net_precision`, `status`. Emit one event per
   field that changed. If precision crossed into `SEC`/`MIN` for the first
   time, emit `first_pinned` and stamp.
3. Any UUID in state but missing from response for this run → emit
   `disappeared` (don't delete — keep it for the historical record; just
   mark `gone_at`). Skip emitting for entries older than 7 days
   post-`net` to avoid log spam.

### Notification

Build a digest *only* if there is at least one event this run. Subject:
`[sc-mon] N change(s) on the Space Coast schedule`. Body is plain text,
one section per launch with the diff in human form, e.g.:

```
Falcon 9 | Starlink Group 10-47  (SpaceX, SLC-40)
  net:       2026-05-25T11:41:00Z  (unchanged)
  precision: MONTH → MIN           ← first pinned!
  status:    TBD   → Go
```

Send via `msmtp -t` reading From/To/Subject from the body's headers.
- Detect missing `~/.msmtprc` on first run: if absent, write the rendered
  email to `data/pending-email.txt` and `print()` a one-line warning so the
  cron MAILTO surfaces it. Don't silently swallow.
- `To:` is `trick@vanstaveren.us` (from session context).

Cron MAILTO is *not* used as the channel — the script does its own email
because we want a real Subject line and to suppress empty digests.

### SpaceX-specific reporting

A `--report` flag (not run by cron) prints a small text table from
`history.jsonl`:

```
Mission                              First seen    First pinned   Lead time
Starlink Group 10-31                  2026-04-29   2026-05-19    2d 7h before T-0
Starlink Group 10-47                  2026-05-03   2026-05-20    5d 0h before T-0
...
```

This is the deliverable for the "how far in advance does SpaceX post?"
question — you can re-run it any time to see the trend over your 3-week
visit.

### Cron entry

```
17 * * * * cd /home/trick/src/github.com/trickv/sc-mon && /usr/bin/python3 sc-mon.py >> data/cron.log 2>&1
```

Minute `:17` avoids the top-of-hour spike on the upstream API. The script
itself is idempotent — a missed run just means the next run does the diff
against an older state.

## Critical files

- **`/home/trick/src/github.com/trickv/sc-mon/sc-mon.py`** (new) —
  one file, ~250 lines. Sections: `fetch()`, `load_state()`, `diff()`,
  `apply()`, `render_email()`, `send()`, `report()`, `main()`.
- **`/home/trick/src/github.com/trickv/sc-mon/README.md`** (new) — install
  steps: clone, `mkdir data`, add cron entry, configure `~/.msmtprc` if not
  already done.
- **`/home/trick/src/github.com/trickv/sc-mon/.gitignore`** (new) — ignore
  `data/`.
- **`/home/trick/src/github.com/trickv/sc-mon/ARCHITECTURE.md`** (new) — a
  ~one-screen architectural explanation: why LL2 instead of scraping
  nextspaceflight, the state-diff model, the "first_pinned_at" invariant,
  why stdlib-only, why a single file, the rate-limit headroom, the
  msmtp-fallback path. Aimed at a future reader (or future-you) opening
  this repo cold a year from now.

No existing utilities to reuse — the working directory is empty.

## Repository setup

This is a fresh project, so part of the work is bootstrapping the repo:

1. `git init` inside `/home/trick/src/github.com/trickv/sc-mon`.
2. Stage and commit the initial source set:
   - `sc-mon.py`
   - `README.md` (user-facing: install + cron entry + msmtp note)
   - `ARCHITECTURE.md` (the design rationale described above)
   - `.gitignore` (ignores `data/`)
   - `plan.md` — a copy of this plan file, committed so the design
     rationale lives alongside the code.
   Commit message: `initial: hourly Space Coast launch monitor`.
3. Create a **public** GitHub repo via `gh repo create trickv/sc-mon
   --public --source . --remote origin --push --description "Hourly
   monitor for Florida Space Coast launch schedule changes"`.
   - `gh` is already authenticated as `trickv` (verified).
   - This single command creates the remote, wires up `origin`, and pushes
     `main` in one go — no manual `git remote add` / `git push -u` dance.
4. README.md gets the GitHub repo URL added to the top once the remote
   exists.

The `data/` directory (state, history, logs, pending email) is gitignored —
it's machine-local runtime state, not source.

## Verification

1. **Dry run, no state yet:**
   `python3 sc-mon.py --dry-run` — should fetch, print the digest to stdout
   as if it were a fresh-install run (everything is `appeared`), but write
   nothing.
2. **First real run:**
   `python3 sc-mon.py` — populates `data/state.json` and `data/history.jsonl`,
   sends one big "N launches appeared" email.
3. **Second run, immediately after:**
   `python3 sc-mon.py` — should produce zero events, send no email, exit 0.
4. **Simulate a change:**
   Hand-edit `data/state.json` to set one launch's `net_precision` to
   `MONTH` and `net` to a fake fuzzy date, then re-run. Should emit
   `net_changed`, `precision_changed`, and `first_pinned` events and send
   one email.
5. **msmtp-missing path:**
   Temporarily rename `~/.msmtprc` (if it exists) and re-run after a
   forced state edit; verify `data/pending-email.txt` is written and a
   warning is printed.
6. **Report:**
   `python3 sc-mon.py --report` — prints the SpaceX lead-time table.
7. **Install cron:**
   `crontab -e`, add the line above, wait an hour, check `data/cron.log`.
8. **Repo created:**
   `gh repo view trickv/sc-mon --web` (or just the URL printed by
   `gh repo create`) — confirm it's Public, README renders, ARCHITECTURE.md
   and plan.md are committed.
