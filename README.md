# sc-mon

Hourly monitor for launches from Florida's Space Coast (Cape Canaveral SFS
and Kennedy Space Center). Notifies on schedule changes by email, and logs
the moment each SpaceX mission first gets a real T-0, so you can see how
far in advance SpaceX actually commits to a launch time.

## What it does

- Pulls the upcoming-launch list for Cape Canaveral SFS (LL2 location id 12)
  and Kennedy Space Center (id 27) from
  `https://ll.thespacedevs.com/2.2.0/launch/upcoming/`.
- Diffs each launch against `data/state.json`. Emits events on:
  - new launches appearing on the schedule
  - `net` (target time) changes
  - `net_precision` changes — including the headline event
    `first_pinned`, the first time precision becomes `SEC` or `MIN`
  - `status` changes (TBD/TBC/Go/Hold/...)
  - launches disappearing (flew, scrubbed off, or otherwise removed)
- On any change, emails a plain-text digest via `msmtp -t` to
  `trick@vanstaveren.us`.
- Appends every observed event to `data/history.jsonl` for analysis.

## Install

```sh
git clone https://github.com/trickv/sc-mon
cd sc-mon
python3 sc-mon.py            # first run: silently seeds state, no email
```

Add to crontab:

```cron
17 * * * * cd /home/trick/src/github.com/trickv/sc-mon && /usr/bin/python3 sc-mon.py >> data/cron.log 2>&1
```

Minute `:17` avoids the top-of-hour API spike. The script is idempotent;
a missed run just means the next one diffs against an older state.

### Email delivery

`msmtp` must be installed and `~/.msmtprc` must be configured. If either
is missing, the digest is written to `data/pending-email.txt` and a warning
is printed (cron's MAILTO will surface it). The script never silently
drops a notification.

## Flags

- `python3 sc-mon.py` — normal hourly mode.
- `python3 sc-mon.py --dry-run` — fetch and diff, but write nothing and
  send no email. Prints what would happen.
- `python3 sc-mon.py --report` — print the SpaceX lead-time table from
  `data/history.jsonl`:

```
Mission                                       First seen           First pinned         Lead time    T-0
Falcon 9 Block 5 | Starlink Group 10-31       2026-04-29T...       2026-05-19T...       2d 7h        2026-05-21T10:04:20Z
...
```

## Iterative testing

The production LL2 API has a 15 req/hour limit. Point at the dev mirror
for any non-trivial development:

```sh
SC_MON_API_HOST=lldev.thespacedevs.com python3 sc-mon.py --dry-run
```

## Layout

```
sc-mon.py            # the entire tool (stdlib-only)
README.md            # this file
ARCHITECTURE.md      # design rationale
plan.md              # the original plan committed for posterity
data/                # gitignored runtime state
  state.json
  history.jsonl
  cron.log
  pending-email.txt  # only if msmtp delivery failed
```
