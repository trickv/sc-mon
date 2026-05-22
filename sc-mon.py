#!/usr/bin/env python3
"""Hourly monitor for Florida Space Coast launches via the Launch Library 2 API.

Default API host is the LL2 production host. For iterative testing set
SC_MON_API_HOST=lldev.thespacedevs.com to avoid the 15 req/hour budget.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
STATE_FILE = DATA / "state.json"
HISTORY_FILE = DATA / "history.jsonl"
PENDING_EMAIL = DATA / "pending-email.txt"
CRON_LOG_HINT = DATA / "cron.log"

API_HOST = os.environ.get("SC_MON_API_HOST", "ll.thespacedevs.com")
API_URL = (
    f"https://{API_HOST}/2.2.0/launch/upcoming/"
    "?location__ids=12,27&limit=50&mode=detailed"
)
USER_AGENT = "sc-mon/0.1 (trick@vanstaveren.us)"

# net_precision abbrevs considered "pinned to a specific date/time".
PINNED_PRECISIONS = {"SEC", "MIN"}

# Launch statuses that mean the flight is over. Disappearance from
# /upcoming/ after one of these is normal LL2 housekeeping, not a real
# scheduling change, so we silently age the entry out instead of paging.
TERMINAL_STATUSES = {"Success", "Failure", "Partial Failure"}

# Notifications are only sent for launches whose T-0 is at least this far
# out. Within this horizon the user is physically at the Space Coast and
# is following imminent launches in real time. history.jsonl still
# captures everything regardless — only the email digest is filtered.
NOTIFY_MIN_HOURS_AHEAD = 24

EMAIL_TO = "trick@vanstaveren.us"
EMAIL_FROM = "sc-mon <trick@vanstaveren.us>"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch() -> list[dict]:
    req = urllib.request.Request(API_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    return body.get("results", [])


def snapshot(launch: dict) -> dict:
    pad = launch.get("pad") or {}
    loc = pad.get("location") or {}
    lsp = launch.get("launch_service_provider") or {}
    status = launch.get("status") or {}
    precision = launch.get("net_precision") or {}
    return {
        "name": launch.get("name"),
        "lsp": lsp.get("name"),
        "pad": pad.get("name"),
        "location": loc.get("name"),
        "net": launch.get("net"),
        "net_precision": precision.get("abbrev"),
        "status": status.get("abbrev"),
        "window_start": launch.get("window_start"),
        "window_end": launch.get("window_end"),
        "last_updated_remote": launch.get("last_updated"),
    }


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    return json.loads(STATE_FILE.read_text())


def save_state(state: dict) -> None:
    DATA.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)


def append_history(events: list[dict]) -> None:
    if not events:
        return
    DATA.mkdir(exist_ok=True)
    with HISTORY_FILE.open("a") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def diff_launches(state: dict, fresh: list[dict], observed_at: str):
    """Return (new_state, events, per_launch_diffs).

    per_launch_diffs is a list of (snap, [(field, old, new), ...], flags)
    used to render the email digest; flags is a set including "appeared",
    "first_pinned", "disappeared" as applicable.
    """
    events: list[dict] = []
    diffs = []
    new_state = dict(state)
    seen_ids = set()

    for launch in fresh:
        uid = launch["id"]
        seen_ids.add(uid)
        snap = snapshot(launch)
        prev = state.get(uid)
        flags: set[str] = set()
        field_changes: list[tuple[str, object, object]] = []

        if prev is None:
            flags.add("appeared")
            entry = dict(snap)
            entry["first_seen_at"] = observed_at
            entry["first_pinned_at"] = None
            if snap["net_precision"] in PINNED_PRECISIONS:
                entry["first_pinned_at"] = observed_at
                flags.add("first_pinned")
            events.append({
                "observed_at": observed_at, "event": "appeared", "id": uid,
                "name": snap["name"], "lsp": snap["lsp"],
                "net": snap["net"], "net_precision": snap["net_precision"],
                "status": snap["status"],
            })
            if "first_pinned" in flags:
                events.append({
                    "observed_at": observed_at, "event": "first_pinned",
                    "id": uid, "name": snap["name"], "lsp": snap["lsp"],
                    "net": snap["net"], "net_precision": snap["net_precision"],
                })
            new_state[uid] = entry
            diffs.append((snap, field_changes, flags))
            continue

        for field in ("net", "net_precision", "status"):
            old, new = prev.get(field), snap.get(field)
            if old != new:
                field_changes.append((field, old, new))
                events.append({
                    "observed_at": observed_at,
                    "event": f"{field}_changed",
                    "id": uid, "name": snap["name"], "lsp": snap["lsp"],
                    "old": old, "new": new,
                })

        # first_pinned: precision crosses into SEC/MIN for the first time
        was_pinned_ever = bool(prev.get("first_pinned_at"))
        is_pinned_now = snap["net_precision"] in PINNED_PRECISIONS
        entry = dict(prev)
        entry.update(snap)
        entry.setdefault("first_seen_at", prev.get("first_seen_at") or observed_at)
        entry["first_pinned_at"] = prev.get("first_pinned_at")
        if is_pinned_now and not was_pinned_ever:
            entry["first_pinned_at"] = observed_at
            flags.add("first_pinned")
            events.append({
                "observed_at": observed_at, "event": "first_pinned",
                "id": uid, "name": snap["name"], "lsp": snap["lsp"],
                "net": snap["net"], "net_precision": snap["net_precision"],
            })

        new_state[uid] = entry
        if field_changes or flags:
            diffs.append((snap, field_changes, flags))

    # disappeared: in state but not in this response
    for uid, prev in list(state.items()):
        if uid in seen_ids:
            continue
        if prev.get("gone_at"):
            continue  # already noted
        # Silent age-out: a launch with terminal status (it flew) or whose
        # NET is more than 7 days in the past is expected to vanish from
        # /upcoming/ — that's just LL2 housekeeping, not a real change.
        # The user already got the status_changed notification when the
        # rocket actually launched; the follow-up "REMOVED" is noise.
        if prev.get("status") in TERMINAL_STATUSES:
            entry = dict(prev)
            entry["gone_at"] = observed_at
            new_state[uid] = entry
            continue
        try:
            net = dt.datetime.fromisoformat((prev.get("net") or "").replace("Z", "+00:00"))
            age = (dt.datetime.now(dt.timezone.utc) - net).total_seconds()
            stale = age > 7 * 24 * 3600
        except (TypeError, ValueError):
            stale = False
        if stale:
            entry = dict(prev)
            entry["gone_at"] = observed_at
            new_state[uid] = entry
            continue
        events.append({
            "observed_at": observed_at, "event": "disappeared",
            "id": uid, "name": prev.get("name"), "lsp": prev.get("lsp"),
            "last_net": prev.get("net"),
        })
        entry = dict(prev)
        entry["gone_at"] = observed_at
        new_state[uid] = entry
        diffs.append((prev, [], {"disappeared"}))

    return new_state, events, diffs


def _is_far_future(snap: dict, now_dt: dt.datetime, hours: int) -> bool:
    """True if the snapshot's net is at least `hours` ahead of now_dt.
    Unknown/unparseable net counts as far-future (worth notifying)."""
    net = snap.get("net")
    if not net:
        return True
    try:
        t0 = dt.datetime.fromisoformat(net.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (t0 - now_dt).total_seconds() > hours * 3600


def render_digest(diffs, observed_at: str) -> str | None:
    if not diffs:
        return None
    now_dt = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    digestable = [d for d in diffs if _is_far_future(d[0], now_dt, NOTIFY_MIN_HOURS_AHEAD)]
    if not digestable:
        return None
    lines = [
        f"sc-mon — {len(digestable)} change(s) observed at {observed_at} (only T-0 > {NOTIFY_MIN_HOURS_AHEAD}h shown)",
        f"Source: {API_URL}",
        "",
    ]
    for snap, field_changes, flags in digestable:
        header = f"{snap.get('name')}  ({snap.get('lsp') or '?'}, {snap.get('pad') or '?'} @ {snap.get('location') or '?'})"
        lines.append(header)
        if "appeared" in flags:
            lines.append(
                f"  NEW on schedule.  net={snap.get('net')}  precision={snap.get('net_precision')}  status={snap.get('status')}"
            )
        if "disappeared" in flags:
            lines.append(
                f"  REMOVED from upcoming schedule (last seen net={snap.get('net')}, status={snap.get('status')})"
            )
        for field, old, new in field_changes:
            marker = ""
            if field == "net_precision" and new in PINNED_PRECISIONS and old not in PINNED_PRECISIONS:
                marker = "   <- first pinned!"
            lines.append(f"  {field:14s} {old!r} -> {new!r}{marker}")
        if "first_pinned" in flags and not any(f == "net_precision" for f, _, _ in field_changes):
            lines.append(f"  first_pinned at precision={snap.get('net_precision')} (T-0 {snap.get('net')})")
        lines.append("")
    return "\n".join(lines)


def build_email(body_text: str, n_changes: int) -> str:
    subject = f"[sc-mon] {n_changes} change(s) on the Space Coast schedule"
    headers = "\n".join([
        f"From: {EMAIL_FROM}",
        f"To: {EMAIL_TO}",
        f"Subject: {subject}",
        "Content-Type: text/plain; charset=utf-8",
    ])
    return headers + "\n\n" + body_text


def send_email(message: str) -> None:
    """Hand the digest to msmtp. msmtp finds its own config (user or
    /etc/msmtprc); we don't second-guess that. If msmtp is missing or
    fails for any reason, fall back to writing data/pending-email.txt
    with the failure surfaced on stderr so cron MAILTO sees it."""
    if not shutil.which("msmtp"):
        DATA.mkdir(exist_ok=True)
        PENDING_EMAIL.write_text(message)
        print(f"warning: msmtp binary not found; digest written to {PENDING_EMAIL}", file=sys.stderr)
        return
    proc = subprocess.run(
        ["msmtp", "-t"],
        input=message.encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode != 0:
        DATA.mkdir(exist_ok=True)
        PENDING_EMAIL.write_text(message)
        print(
            f"warning: msmtp failed (rc={proc.returncode}): {proc.stderr.decode(errors='replace').strip()}\n"
            f"  digest written to {PENDING_EMAIL}",
            file=sys.stderr,
        )


def cmd_run(args) -> int:
    try:
        fresh = fetch()
    except urllib.error.HTTPError as e:
        print(f"error: HTTP {e.code} fetching {API_URL}: {e.reason}", file=sys.stderr)
        return 2
    except urllib.error.URLError as e:
        print(f"error: network failure fetching {API_URL}: {e.reason}", file=sys.stderr)
        return 2

    state = load_state()
    observed_at = now_iso()
    new_state, events, diffs = diff_launches(state, fresh, observed_at)
    digest = render_digest(diffs, observed_at)

    if args.dry_run:
        print(f"[dry-run] would record {len(events)} event(s), update state for {len(new_state)} launch(es)")
        if digest:
            print()
            print(digest)
        return 0

    if not state:
        # first ever run: still record everything, but don't blast a giant
        # email full of "appeared" rows the user already knows about.
        # Save state + history silently, print a one-line summary.
        save_state(new_state)
        append_history(events)
        print(f"first run: seeded state with {len(fresh)} upcoming launch(es); no email sent.")
        return 0

    save_state(new_state)
    append_history(events)
    if digest is None:
        if diffs:
            print(f"observed {len(diffs)} change(s), all within {NOTIFY_MIN_HOURS_AHEAD}h of T-0; no email sent.")
        return 0
    # render_digest filtered to far-future entries; reuse the same predicate
    # to count for the subject line.
    now_dt = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    n_changes = sum(1 for d in diffs if _is_far_future(d[0], now_dt, NOTIFY_MIN_HOURS_AHEAD))
    email = build_email(digest, n_changes)
    send_email(email)
    print(f"sent digest: {n_changes} change(s) (T-0 > {NOTIFY_MIN_HOURS_AHEAD}h).")
    return 0


def cmd_report(args) -> int:
    if not HISTORY_FILE.exists():
        print("no history yet; run sc-mon.py at least once.")
        return 0

    first_seen: dict[str, dict] = {}
    first_pinned: dict[str, dict] = {}
    for line in HISTORY_FILE.read_text().splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        if ev.get("event") == "appeared":
            first_seen.setdefault(ev["id"], ev)
        elif ev.get("event") == "first_pinned":
            first_pinned.setdefault(ev["id"], ev)

    state = load_state()
    rows = []
    for uid, app in first_seen.items():
        if (app.get("lsp") or "") != "SpaceX":
            continue
        pin = first_pinned.get(uid)
        snap = state.get(uid, {})
        net = snap.get("net") or app.get("net")
        rows.append({
            "name": app.get("name") or uid,
            "first_seen": app.get("observed_at"),
            "first_pinned": (pin or {}).get("observed_at"),
            "net": net,
        })

    rows.sort(key=lambda r: r["first_seen"] or "")
    print(f"{'Mission':45s} {'First seen':20s} {'First pinned':20s} {'Lead time':12s} {'T-0':20s}")
    for r in rows:
        lead = ""
        if r["first_pinned"] and r["net"]:
            try:
                pinned = dt.datetime.fromisoformat(r["first_pinned"].replace("Z", "+00:00"))
                t0 = dt.datetime.fromisoformat(r["net"].replace("Z", "+00:00"))
                delta = t0 - pinned
                days = delta.days
                hours = delta.seconds // 3600
                lead = f"{days}d {hours}h"
            except ValueError:
                lead = "?"
        print(f"{(r['name'] or '')[:45]:45s} {(r['first_seen'] or '')[:20]:20s} {(r['first_pinned'] or '-')[:20]:20s} {lead:12s} {(r['net'] or '')[:20]:20s}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="fetch and diff, but write nothing and send no email")
    p.add_argument("--report", action="store_true", help="print SpaceX lead-time table from history")
    args = p.parse_args()

    DATA.mkdir(exist_ok=True)
    if args.report:
        return cmd_report(args)
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
