"""`scb` — command line for science-bridge.

  scb status                 daemon + running/waiting sessions
  scb ls [--active] [-p P]   list sessions
  scb read REF [-n 12] [-t]  show a session's recent turns
  scb send REF TEXT          send a message as you
  scb watch [--once]         stream attention-worthy events (for Monitor)
  scb doctor                 self-check: db readable, daemon reachable, login works
"""

from __future__ import annotations

import argparse
import json
import sys

from . import api, db, watch


def _table(sessions: list[db.Session]) -> None:
    for s in sessions:
        when = db.iso(s.updated_at)[5:16].replace("T", " ")
        sub = f" +{s.active_children} sub" if s.active_children else ""
        print(f"{s.id[:8]}  {s.status:<24} {when}  {(s.name or '-')[:60]}{sub}")
        if s.status_description and s.status not in ("completed",):
            print(f"{'':10}↳ {s.status_description[:110]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scb", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("ls")
    p.add_argument("--active", action="store_true")
    p.add_argument("-s", "--status")
    p.add_argument("-p", "--project")
    p.add_argument("-n", "--limit", type=int, default=15)
    p = sub.add_parser("read")
    p.add_argument("ref")
    p.add_argument("-n", "--last", type=int, default=12)
    p.add_argument("-t", "--tools", action="store_true")
    p = sub.add_parser("send")
    p.add_argument("ref")
    p.add_argument("text")
    p = sub.add_parser("watch")
    p.add_argument("--interval", type=float, default=20)
    p.add_argument("--stall-min", type=int, default=45)
    p.add_argument("--once", action="store_true")
    p.add_argument("--no-backup-check", action="store_true")
    sub.add_parser("doctor")
    a = ap.parse_args(argv)

    if a.cmd == "status":
        st = api.daemon_status()
        print(f"daemon: {'running' if st.get('running') else 'NOT running'}  v{st.get('version')}  port {st.get('port')}")
        with db.connect() as conn:
            _table(db.list_sessions(conn, status="active", limit=50))
    elif a.cmd == "ls":
        with db.connect() as conn:
            _table(db.list_sessions(conn, status="active" if a.active else a.status, project=a.project, limit=a.limit))
    elif a.cmd == "read":
        with db.connect() as conn:
            s = db.get_session(conn, a.ref)
            if not s:
                print(f"no session matches {a.ref!r}", file=sys.stderr)
                return 1
            print(f"# {s.name}  [{s.status}]  {s.id}\n# {s.status_description or ''}\n")
            for t in db.transcript(conn, s.id, last=a.last, include_tools=a.tools):
                print(t, "\n")
    elif a.cmd == "send":
        with db.connect() as conn:
            s = db.get_session(conn, a.ref)
        if not s:
            print(f"no session matches {a.ref!r}", file=sys.stderr)
            return 1
        print(json.dumps(api.Client().send_message(s.id, a.text), indent=1))
    elif a.cmd == "watch":
        watch.run(interval=a.interval, stall_min=a.stall_min, once=a.once, check_backup=not a.no_backup_check)
    elif a.cmd == "doctor":
        return doctor()
    return 0


def doctor() -> int:
    ok = True

    def check(name: str, fn) -> None:
        nonlocal ok
        try:
            print(f"  ok   {name}: {fn()}")
        except Exception as e:  # noqa: BLE001 — diagnostics
            ok = False
            print(f"  FAIL {name}: {type(e).__name__}: {e}")

    print("science-bridge doctor")
    check("database", lambda: f"{len(db.snapshot(db.connect()))} root sessions readable")
    check("daemon", lambda: (lambda s: f"v{s['version']} on port {s['port']}" if s.get("running") else 1 / 0)(api.daemon_status()))
    check("API login", lambda: "authenticated" if api.Client().auth_status().get("authenticated") else 1 / 0)
    check("API read", lambda: f"{len(api.Client().get('/api/frames', root_only='true', limit=3))} frames via API")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
