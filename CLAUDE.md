# CLAUDE.md

Operational notes for Claude working on this repo. Read alongside
`README.md` (user-facing) and `ARCHITECTURE.md` (design rationale).

## What this is

Single-file Python 3 tool (`sc-mon.py`) that polls the Launch Library 2
API hourly for upcoming launches at Cape Canaveral SFS (id 12) and
Kennedy Space Center (id 27), diffs against `data/state.json`, appends
events to `data/history.jsonl`, and emails a digest via `msmtp -t`.

Goal of the tool: notify the user (currently visiting the Space Coast)
of any schedule change, and durably log the moment each SpaceX mission
first transitions to a precise T-0 so `--report` can answer "how far
ahead does SpaceX really commit?"

## Critical operational facts

- **Data source is LL2, not nextspaceflight.com.** nextspaceflight has
  no usable public API; LL2 (`ll.thespacedevs.com`) is its upstream and
  exposes `net_precision`, the field that makes the analysis possible.
- **Rate limit: ~15 req/hour on prod.** Cron uses 1. For *any*
  iterative testing, point at the dev mirror:
  `SC_MON_API_HOST=lldev.thespacedevs.com python3 sc-mon.py --dry-run`.
  Do not loop-test against prod.
- **Email goes via system msmtp.** `/etc/msmtprc` is preconfigured
  with Gmail SMTP on this box. There is no `~/.msmtprc` and one is not
  needed. Do not pre-check for the user rc file — that pattern was
  removed in commit `f2cff65`. Just exec `msmtp -t` and trust its
  config resolution; fall back to `data/pending-email.txt` only on a
  non-zero exit code.
- **No third-party dependencies.** Stdlib only. Adding `requests` or
  similar is a regression — the whole point is that cron deployment
  needs no venv.

## State model invariants

- `data/state.json` is keyed by LL2 launch UUID.
- `first_pinned_at` is set the **first time** `net_precision` becomes
  `SEC` or `MIN` for a UUID, and is **never overwritten** even if the
  launch later regresses to `M`/`TBD` (which happens). This is the
  headline timestamp for the SpaceX lead-time analysis.
- The pinned set is `{"SEC", "MIN"}`. `HR`, `DAY`, `WK` are *not*
  considered pinned. If the user wants to widen the threshold, change
  `PINNED_PRECISIONS` in `sc-mon.py` — and be aware it changes the
  semantics of historical events too (re-running `--report` against
  the existing `history.jsonl` will produce different lead times).
- First-ever run is a special case: state is empty, every launch is
  technically "appeared", so the script seeds silently and emails
  nothing. Subsequent runs email when there's a change to a launch
  whose T-0 is more than `NOTIFY_MIN_HOURS_AHEAD` (24h) in the future.
- The 24h horizon filter lives in `render_digest`, not in
  `diff_launches`. `history.jsonl` always captures every event so
  `--report` has the full record; only the email is filtered. Do not
  move the filter into `diff_launches` — that would corrupt the
  analytical dataset.
- Terminal-status disappearance (Success/Failure/Partial Failure that
  drops off `/upcoming/`) is suppressed at the diff level — no event,
  no history entry, just a silent `gone_at` stamp. The
  `status_changed → Success` event already exists from the moment the
  rocket flew; the follow-up "REMOVED" carries no new info.

## File layout

```
sc-mon.py            # whole tool, ~250 lines
README.md            # user docs
ARCHITECTURE.md      # design rationale
plan.md              # the original plan, committed for posterity
CLAUDE.md            # this file
.gitignore           # data/, .claude/, __pycache__/
data/                # gitignored runtime state
  state.json
  history.jsonl
  cron.log
  pending-email.txt  # only written when msmtp fails
```

## Cron entry

```
17 * * * * cd /home/trick/src/github.com/trickv/sc-mon && /usr/bin/python3 sc-mon.py >> data/cron.log 2>&1
```

`:17` to avoid the top-of-hour upstream spike. Script is idempotent.

## Net precision enumeration (from LL2 `/config/netprecision/`)

`SEC`, `MIN`, `HR`, `AM`, `PM`, `DAY`, `WK`, `M`, `Q1`-`Q4`, `H1`/`H2`,
`Y`, `FY`, `DEC`. Document this in `ARCHITECTURE.md` if a user asks
again — these abbrevs are not self-explanatory (`M` is Month, not
Minute).
