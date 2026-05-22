# Architecture

## Why LL2 instead of scraping nextspaceflight.com

The original idea was to monitor `nextspaceflight.com`. That site has no
public API (their `api.nextspaceflight.com` is gated; they ask people to
email `api@nextspaceflight.com` first). The HTML is server-rendered
Next.js with minified Tailwind utility classes — scraping it would be a
moving target.

Crucially, **nextspaceflight.com is fed by the public Launch Library 2 API
from TheSpaceDevs** (`ll.thespacedevs.com`). Same data, with structured
fields the website hides — most importantly `net_precision`, which tells
you exactly how committed the schedule entry is ("Second", "Minute", up
through "Month", "Quarter", "Year", "NET"). That's the field that makes
the "how far in advance does SpaceX post real times?" question
answerable.

So we skip the brittle scrape and hit the source.

## Why stdlib-only Python

This script runs from cron once an hour and does ~150 lines of work. A
venv, a `requirements.txt`, or a third-party HTTP client would all be
heavier than the script itself. `urllib.request` does fine for a single
GET with a User-Agent. `subprocess` to `msmtp -t` does fine for one
email. No deps means no maintenance.

## State model

`data/state.json`: a dict keyed by LL2 launch UUID. Each value is the
last-observed snapshot of the launch plus two derived timestamps:

- `first_seen_at` — when the UUID first appeared in any of our runs.
- `first_pinned_at` — when `net_precision` first became `SEC` or `MIN`
  for this UUID. **Once set, never overwritten.** Launches sometimes
  regress from a precise time back to TBD; the analytical question is
  about the *first* commitment, not the latest one.

`data/history.jsonl` is the append-only event log. One JSON object per
line per detected change. This is the durable artifact — `state.json`
is just a cache for diffing.

## Change detection

For each launch in the API response:

1. New UUID -> `appeared` event. If precision is already pinned at
   first sighting, also emit `first_pinned`.
2. Known UUID -> diff `net`, `net_precision`, `status`. Each field that
   changed becomes one event. A precision transition into `SEC`/`MIN`
   while `first_pinned_at` is still null also triggers `first_pinned`.
3. UUID present in state, absent from this response -> `disappeared`,
   stamp `gone_at`. (Suppressed silently for launches whose `net` is
   more than 7 days in the past — those are just stale post-flight
   entries we no longer care about.)

## Email policy

We don't lean on cron's MAILTO because we want a real `Subject:` line and
because most runs produce no events at all — cron MAILTO would still
send empty-output noise when stdout is empty (it doesn't, but explicit
is better here).

**24-hour notification horizon.** The user only wants emails about
launches whose T-0 is more than 24 hours out. When they're physically
at the Space Coast, imminent launches are something they're following
live, and "REMOVED from schedule" the day after a successful flight is
pure housekeeping. So `render_digest` filters by `_is_far_future(snap,
now, 24)` — but `diff_launches` still appends every event to
`history.jsonl`, because the analytical question (`--report`) needs the
complete record including imminent transitions.

A launch with terminal status (`Success` / `Failure` / `Partial
Failure`) that drops off `/upcoming/` is also silently aged out at the
diff level (no event recorded at all). The status_changed → Success
notification was already sent at the moment the rocket actually flew;
the follow-up "REMOVED from schedule" event would carry no information.

The script renders one digest per run with at least one event, hands it
to `msmtp -t`, and falls back to writing `data/pending-email.txt` with a
stderr warning if `msmtp` is missing, unconfigured, or fails. The
warning ends up in `data/cron.log` because cron redirects stderr there.

The **first ever run** is a special case: state is empty, every launch
in the response is technically "appeared", and emailing that 30-row
digest at install time is noise. So the first run silently seeds state
and history, prints a one-line summary, and sends nothing.

## Rate-limit headroom

LL2 free tier is ~15 req/hour. Cron fires once an hour. We use 1
request. Two orders of magnitude of headroom.

For development, point at the dev mirror via `SC_MON_API_HOST` so we
don't consume the prod budget while iterating:

```
SC_MON_API_HOST=lldev.thespacedevs.com python3 sc-mon.py --dry-run
```

## SpaceX-specific analysis

`--report` walks `history.jsonl`, collects `appeared` and `first_pinned`
events per UUID, filters to `lsp == "SpaceX"`, and prints lead time
(`first_pinned_at` -> `net`). This is the question the user actually
wants answered — how many days before T-0 does SpaceX commit to a real
time? It only needs to read the log; no API call.
