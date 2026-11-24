"""MCP server exposing Claude Science to Claude Code.

Reads come from the local database (read-only); every change goes through the
daemon's own API, exactly as if typed in the Claude Science window.
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from . import api, db
from .api import DaemonError

mcp = FastMCP(
    "science-bridge",
    instructions=(
        "Tools for the user's local Claude Science (a research workbench). Sessions are long-running "
        "agent conversations. Read with list_sessions / read_session; act with send_message, "
        "approve_plan, list_cards / answer_card (approval cards), cancel_session, resume_session, "
        "new_session. Never grant 'always' unless the user says so. Session refs accept a full id, "
        "an 8-char id prefix, or part of the session name. Messages you send appear in Claude "
        "Science as if the user typed them, so only send what the user asked you to send, and "
        "confirm destructive actions (cancel) with the user first."
    ),
)


def _resolve(ref: str) -> db.Session:
    with db.connect() as conn:
        s = db.get_session(conn, ref)
    if not s:
        raise ValueError(f"no Claude Science session matches {ref!r}; call list_sessions first")
    return s


def _j(x) -> str:
    return json.dumps(x, indent=1, ensure_ascii=False, default=str)


@mcp.tool()
def science_status() -> str:
    """Is Claude Science running? Daemon version, health, and counts of running / waiting sessions."""
    st = api.daemon_status()
    out = {
        "running": st.get("running"),
        "version": st.get("version"),
        "port": st.get("port"),
        "uptime_h": round(((st.get("daemon") or {}).get("uptime_ms") or 0) / 3.6e6, 1),
    }
    with db.connect() as conn:
        active = db.list_sessions(conn, status="active", limit=50)
    out["running_sessions"] = [s.as_dict() for s in active if s.status == "processing"]
    out["waiting_for_user"] = [s.as_dict() for s in active if s.status in db.NEEDS_YOU]
    return _j(out)


@mcp.tool()
def list_sessions(status: str | None = None, project: str | None = None, limit: int = 15) -> str:
    """List Claude Science sessions, most recently updated first.

    status: 'active' (running or waiting on the user), or an exact status such as
    'processing', 'completed', 'awaiting_user_response', 'awaiting_plan_approval', 'failed'.
    project: filter by project name substring.
    """
    with db.connect() as conn:
        return _j([s.as_dict() for s in db.list_sessions(conn, status=status, project=project, limit=limit)])


@mcp.tool()
def list_projects() -> str:
    """List Claude Science projects (needed for new_session)."""
    with db.connect() as conn:
        return _j(db.list_projects(conn))


@mcp.tool()
def read_session(session: str, last: int = 12, include_tools: bool = False, show_subagents: bool = True) -> str:
    """Read a session: its status, what it is doing now, sub-agents, and the last `last` turns.

    include_tools=True also shows code/tool calls and their outputs (truncated).
    """
    s = _resolve(session)
    with db.connect() as conn:
        out = {
            "session": s.as_dict(),
            "subagents": db.subagents(conn, s.id) if show_subagents else None,
            "transcript": db.transcript(conn, s.id, last=last, include_tools=include_tools),
        }
    return _j(out)


@mcp.tool()
def send_message(session: str, text: str) -> str:
    """Send a message to a session as the user. If it is running the message is queued and
    picked up at the next step; if it is completed it starts a new turn. Fails if the session
    is waiting on an approval card — use list_cards / answer_card for those, approve_plan for plans."""
    s = _resolve(session)
    try:
        r = api.Client().send_message(s.id, text)
    except DaemonError as e:
        return f"NOT SENT: {e}"
    return _j({"sent_to": s.name, "session_id": s.id, "result": r})


@mcp.tool()
def approve_plan(session: str, edited_plan: str | None = None) -> str:
    """Approve the plan a session is waiting on (status awaiting_plan_approval). Optionally
    pass an edited plan text to approve instead."""
    s = _resolve(session)
    if s.status != "awaiting_plan_approval":
        return f"NOT DONE: session is {s.status}, not awaiting_plan_approval"
    try:
        return _j(api.Client().approve_plan(s.id, edited_plan))
    except DaemonError as e:
        return f"NOT DONE: {e}"


@mcp.tool()
def discard_plan(session: str) -> str:
    """Reject the plan a session is waiting on."""
    s = _resolve(session)
    try:
        return _j(api.Client().discard_plan(s.id))
    except DaemonError as e:
        return f"NOT DONE: {e}"


DECISIONS = ("allow_once", "allow", "allow_always", "deny", "answer", "decide_for_me", "discuss", "cancel")


def build_response(card: dict, decision: str, scope: str | None, answers: dict | None, message: str | None) -> dict:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    resp: dict = {"action": decision}
    if card.get("tool_id"):
        resp["tool_id"] = card["tool_id"]
    if card.get("card", {}).get("requestId"):
        resp["requestId"] = card["card"]["requestId"]
    if decision == "allow":
        if scope not in ("conversation", "project", "always"):
            raise ValueError("decision 'allow' needs scope: conversation | project | always")
        resp["scope"] = scope
    elif scope:
        resp["scope"] = scope
    if decision == "answer":
        if not answers:
            raise ValueError("decision 'answer' needs answers: {question text: answer}")
        resp["answers"] = answers
    if decision == "discuss" and not message:
        raise ValueError("decision 'discuss' needs a message")
    if message:
        resp["message"] = message
    return resp


@mcp.tool()
def list_cards(session: str | None = None) -> str:
    """List open approval cards (network, credential, local-exec, file, cost checkpoints, and
    questions from ask_user) waiting on the user, across all sessions or one session.
    Cards raised by sub-agents are included. Show the user `summary` and the relevant `card`
    fields before answering."""
    root = _resolve(session).id if session else None
    with db.connect() as conn:
        return _j(db.pending_cards(conn, root))


@mcp.tool()
def answer_card(
    request_id: str,
    decision: str,
    scope: str | None = None,
    answers: dict[str, str] | None = None,
    message: str | None = None,
) -> str:
    """Answer an approval card exactly as the buttons in Claude Science would.

    request_id: from list_cards (a unique prefix is enough).
    decision:
      allow_once     approve just this request
      allow          approve with a remembered grant; scope = 'conversation' | 'project' | 'always'
      allow_always   standing grant for everything (only if the user explicitly says "always")
      deny           refuse
      answer         reply to an ask_user question card; answers = {question text: answer}
      decide_for_me  let the agent choose (question cards)
      discuss        reply with `message` instead of answering (question cards)
      cancel         dismiss a question card
    """
    with db.connect() as conn:
        cards = [c for c in db.pending_cards(conn) if str(c["request_id"]).startswith(request_id)]
    if len(cards) != 1:
        return f"NOT DONE: request_id {request_id!r} matched {len(cards)} open cards; call list_cards"
    card = cards[0]
    try:
        resp = build_response(card, decision, scope, answers, message)
        r = api.Client().resolve_input(card["frame_id"], [resp])
    except (ValueError, DaemonError) as e:
        return f"NOT DONE: {e}"
    return _j({"answered": card["summary"], "session": card["session_name"], "decision": decision,
               "scope": resp.get("scope"), "result": r})


@mcp.tool()
def cancel_session(session: str, reason: str | None = None) -> str:
    """Stop a running session. Confirm with the user before calling."""
    s = _resolve(session)
    try:
        return _j(api.Client().cancel(s.id, reason))
    except DaemonError as e:
        return f"NOT DONE: {e}"


@mcp.tool()
def resume_session(session: str) -> str:
    """Resume a cancelled or failed session."""
    s = _resolve(session)
    try:
        return _j(api.Client().resume(s.id))
    except DaemonError as e:
        return f"NOT DONE: {e}"


@mcp.tool()
def new_session(project: str, text: str) -> str:
    """Start a new session in a project (name substring or id) with an opening message."""
    with db.connect() as conn:
        projs = db.list_projects(conn)
    match = [p for p in projs if p["id"] == project] or [
        p for p in projs if project.lower() in (p["name"] or "").lower()
    ]
    if len(match) != 1:
        return f"NOT DONE: project {project!r} matched {len(match)} projects: {[p['name'] for p in match][:8]}"
    try:
        return _j(api.Client().new_session(match[0]["id"], text))
    except DaemonError as e:
        return f"NOT DONE: {e}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
