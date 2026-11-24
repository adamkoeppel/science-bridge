"""Watch Claude Science and emit one line per event worth a human's attention.

Designed to be the command behind Claude Code's Monitor tool: each stdout line
becomes a notification in the hub session, which decides whether to push it to
the phone. Lines are prefixed with a severity so the hub can triage:

    CARD       an approval card / question opened (network, credential, exec, cost, ask_user…)
    NEEDS_YOU  a session's status became awaiting_user_response / awaiting_plan_approval
    FAILED     a session failed / errored / was cancelled
    DONE       a session finished
    STALLED    a running session hasn't changed in --stall-min minutes
    DAEMON     the Claude Science daemon went down or came back
    BACKUP     the nightly backup reported a failure

State persists in ~/.science-bridge/watch-state.json, so restarting the
watcher (Monitor expires every 30 min) never repeats an old event.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from datetime import datetime

from . import db, paths
from .api import daemon_status

BAD = set(db.TERMINAL_BAD)
NEEDS = set(db.NEEDS_YOU)


def _label(s: dict) -> str:
    name = s.get("name") or "untitled session"
    proj = s.get("project")
    return f'"{name}"' + (f" [{proj}]" if proj else "")


def diff(prev: dict[str, dict], cur: dict[str, dict], *, now_ms: int, stall_ms: int,
         activity: dict[str, int], stalled: set[str]) -> list[str]:
    """Pure function: events implied by moving from prev -> cur snapshots."""
    events: list[str] = []
    for fid, s in cur.items():
        p = prev.get(fid)
        old, new = (p or {}).get("status"), s["status"]
        short = fid[:8]
        if p is None:
            if new in NEEDS:
                events.append(f"NEEDS_YOU {_label(s)} ({short}) is waiting for you: {new}")
            continue
        if old != new:
            stalled.discard(fid)
            doing = f" — {s['doing'][:140]}" if s.get("doing") else ""
            if new in NEEDS:
                kind = "plan approval" if new == "awaiting_plan_approval" else "your answer / an approval card"
                events.append(f"NEEDS_YOU {_label(s)} ({short}) needs {kind}{doing}")
            elif new in BAD:
                events.append(f"FAILED {_label(s)} ({short}) is {new}{doing}")
            elif new == "completed" and old in ("processing", *NEEDS):
                events.append(f"DONE {_label(s)} ({short}) finished{doing}")
        if new == "processing" and fid not in stalled:
            last = activity.get(fid, s["updated_at"])
            if now_ms - last > stall_ms:
                stalled.add(fid)
                mins = (now_ms - last) // 60000
                events.append(f"STALLED {_label(s)} ({short}) running but no activity for {mins} min")
        elif new == "processing" and fid in stalled and activity.get(fid, 0) > now_ms - stall_ms:
            stalled.discard(fid)
    return events


# Only a FAILED line means the backup failed; e.g. a du warning in the summary is harmless.
_FAIL_RE = re.compile(r"\bFAILED\b")


def backup_events(state: dict) -> list[str]:
    """Report a failed nightly backup once per log modification."""
    log = paths.BACKUP_LOG
    if not log.exists():
        return []
    mtime = int(log.stat().st_mtime)
    if state.get("backup_mtime") == mtime:
        return []
    lines = log.read_text(errors="replace").splitlines()
    starts = [i for i, l in enumerate(lines) if l.startswith("=== ") and l.rstrip().endswith(")") and " start " in l]
    last_run = lines[starts[-1]:] if starts else lines[-15:]
    if not any(re.match(r"=== \S+ done", l) for l in last_run) and not any(_FAIL_RE.search(l) for l in last_run):
        return []  # run still in progress; look again when the log changes
    state["backup_mtime"] = mtime
    bad = [l for l in last_run if _FAIL_RE.search(l)]
    if bad:
        return [f"BACKUP nightly Claude Science backup reported a problem: {bad[0][:160]}"]
    return []


def card_events(cards: list[dict], seen: set[str]) -> tuple[list[str], set[str]]:
    """One CARD event per newly opened approval card. Returns (events, ids now open)."""
    events, now = [], set()
    for c in cards:
        rid = str(c["request_id"])
        now.add(rid)
        if rid not in seen:
            name = c.get("session_name") or "untitled session"
            proj = f" [{c['project']}]" if c.get("project") else ""
            events.append(
                f'CARD "{name}"{proj} ({str(c["session_id"])[:8]}) needs approval — {c["summary"][:220]} '
                f"(request {rid[:12]})"
            )
    return events, now


def run(*, interval: float = 20.0, stall_min: int = 45, once: bool = False, check_backup: bool = True) -> None:
    sp = paths.state_dir() / "watch-state.json"
    try:
        state = json.loads(sp.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    prev: dict[str, dict] = state.get("sessions", {})
    stalled: set[str] = set(state.get("stalled", []))
    daemon_up = state.get("daemon_up", True)
    seen_cards: set[str] = set(state.get("cards", []))
    first = not prev

    def emit(line: str) -> None:
        print(f"{datetime.now():%H:%M} {line}", flush=True)

    while True:
        up = bool(daemon_status().get("running"))
        if up != daemon_up:
            emit("DAEMON Claude Science daemon is back up" if up else
                 "DAEMON Claude Science daemon is DOWN — running sessions are paused")
            daemon_up = up
        try:
            with db.connect() as conn:
                cur = db.snapshot(conn)
                activity = {fid: db.latest_activity_ms(conn, fid)
                            for fid, s in cur.items() if s["status"] == "processing"}
                cards = db.pending_cards(conn)
            card_ev, seen_cards = card_events(cards, seen_cards)
            for e in card_ev:
                emit(e)  # cards fire even on the first pass: an open card always needs you
            if not first:
                for e in diff(prev, cur, now_ms=int(time.time() * 1000),
                              stall_ms=stall_min * 60_000, activity=activity, stalled=stalled):
                    emit(e)
            prev, first = cur, False
        except sqlite3.Error as e:
            print(f"watch: db read failed (will retry): {e}", file=sys.stderr, flush=True)
        if check_backup:
            for e in backup_events(state):
                emit(e)
        state.update(sessions=prev, stalled=sorted(stalled), daemon_up=daemon_up, cards=sorted(seen_cards))
        sp.write_text(json.dumps(state))
        if once:
            return
        time.sleep(interval)
