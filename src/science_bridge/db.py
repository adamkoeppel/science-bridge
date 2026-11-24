"""Read-only access to Claude Science's SQLite database.

Never writes. Opens with mode=ro so the daemon stays the only writer; all
changes go through the daemon's HTTP API (see api.py).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import paths

ACTIVE = ("processing",)
NEEDS_YOU = ("awaiting_user_response", "awaiting_plan_approval")
TERMINAL_BAD = ("failed", "error", "cancelled")


def connect(db: Path | None = None) -> sqlite3.Connection:
    p = db or paths.db_path()
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


def iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class Session:
    id: str
    name: str | None
    status: str
    status_description: str | None
    project_id: str | None
    project_name: str | None
    model: str | None
    created_at: int
    updated_at: int
    completed_at: int | None
    total_cost: float | None
    active_children: int = 0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "status_description": self.status_description,
            "project": self.project_name,
            "project_id": self.project_id,
            "model": self.model,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "completed_at": iso(self.completed_at),
            "cost_usd": round((self.total_cost or 0), 2),
            "active_subagents": self.active_children,
        }


_SESSION_SQL = """
SELECT f.id, f.name, f.status, f.status_description, f.project_id, p.name AS project_name,
       f.model, f.created_at, f.updated_at, f.completed_at,
       COALESCE(f.total_cost,0) + COALESCE(f.aux_cost,0) AS total_cost,
       (SELECT COUNT(*) FROM frames c WHERE c.root_frame_id = f.id AND c.id != f.id
          AND c.status = 'processing') AS active_children
FROM frames f LEFT JOIN projects p ON p.id = f.project_id
WHERE f.parent_frame_id IS NULL AND f.conversation_type = 'agent'
  AND COALESCE(f.is_hidden, 0) = 0
"""


def _row_to_session(r: sqlite3.Row) -> Session:
    return Session(**{k: r[k] for k in r.keys()})


def list_sessions(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    project: str | None = None,
    limit: int = 20,
) -> list[Session]:
    sql, args = _SESSION_SQL, []
    if status == "active":
        sql += f" AND f.status IN ({','.join('?' * len(ACTIVE + NEEDS_YOU))})"
        args += list(ACTIVE + NEEDS_YOU)
    elif status:
        sql += " AND f.status = ?"
        args.append(status)
    if project:
        sql += " AND (p.name LIKE ? OR f.project_id = ?)"
        args += [f"%{project}%", project]
    sql += " ORDER BY f.updated_at DESC LIMIT ?"
    args.append(limit)
    return [_row_to_session(r) for r in conn.execute(sql, args)]


def get_session(conn: sqlite3.Connection, ref: str) -> Session | None:
    """Look up by full id, id prefix (>=6 chars), or case-insensitive name match."""
    r = conn.execute(_SESSION_SQL + " AND f.id = ?", [ref]).fetchone()
    if r is None and len(ref) >= 6:
        r = conn.execute(_SESSION_SQL + " AND f.id LIKE ? ORDER BY f.updated_at DESC", [ref + "%"]).fetchone()
    if r is None:
        r = conn.execute(
            _SESSION_SQL + " AND f.name LIKE ? ORDER BY f.updated_at DESC", [f"%{ref}%"]
        ).fetchone()
    return _row_to_session(r) if r else None


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT p.id, p.name, p.updated_at,
                  (SELECT COUNT(*) FROM frames f WHERE f.project_id = p.id
                     AND f.parent_frame_id IS NULL AND f.conversation_type='agent') AS sessions
           FROM projects p WHERE p.archived_at IS NULL ORDER BY p.updated_at DESC"""
    )
    return [
        {"id": r["id"], "name": r["name"], "sessions": r["sessions"], "updated_at": iso(r["updated_at"])}
        for r in rows
    ]


# ---------------------------------------------------------------- transcripts


def _clip(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def render_message(msg: dict, *, tool_chars: int = 300, include_tools: bool = True) -> str | None:
    """Render one stored message as compact plain text; None if nothing to show."""
    role = msg.get("role", "?")
    content = msg.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    parts: list[str] = []
    for block in content or []:
        t = block.get("type")
        if t == "text" and block.get("text", "").strip():
            parts.append(block["text"].strip())
        elif t in ("tool_use", "tool_result") and not include_tools:
            continue
        elif t == "tool_use":
            inp = json.dumps(block.get("input", {}), ensure_ascii=False)
            parts.append(f"[tool {block.get('name')}] {_clip(inp, tool_chars)}")
        elif t == "tool_result":
            c = block.get("content")
            if isinstance(c, list):
                c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
            parts.append(f"[result] {_clip(str(c or ''), tool_chars)}")
    if not parts:
        return None
    label = {"user": "USER", "assistant": "CLAUDE"}.get(role, role.upper())
    # tool_result-only "user" turns are machine turns, not the human
    if role == "user" and all(p.startswith("[result]") for p in parts):
        label = "TOOL"
    return f"{label}: " + "\n".join(parts)


def transcript(
    conn: sqlite3.Connection,
    frame_id: str,
    *,
    last: int = 20,
    include_tools: bool = False,
    tool_chars: int = 300,
) -> list[str]:
    """Last `last` rendered turns of a frame's main transcript."""
    rows = conn.execute(
        "SELECT msg_json FROM frame_messages WHERE frame_id = ? ORDER BY idx DESC LIMIT ?",
        [frame_id, max(last * 6, 60)],
    ).fetchall()
    out: list[str] = []
    for r in rows:
        try:
            msg = json.loads(r["msg_json"])
        except json.JSONDecodeError:
            continue
        text = render_message(msg, tool_chars=tool_chars, include_tools=include_tools)
        if not text:
            continue
        if not include_tools and text.startswith("USER: [System]"):
            continue  # daemon-injected context, not something the human typed
        out.append(text)
        if len(out) >= last:
            break
    return list(reversed(out))


def subagents(conn: sqlite3.Connection, root_id: str, limit: int = 15) -> list[dict]:
    rows = conn.execute(
        """SELECT id, agent_name, delegate_name, name, status, status_description, updated_at
           FROM frames WHERE root_frame_id = ? AND id != ? ORDER BY updated_at DESC LIMIT ?""",
        [root_id, root_id, limit],
    )
    return [
        {
            "id": r["id"],
            "agent": r["delegate_name"] or r["agent_name"],
            "name": r["name"],
            "status": r["status"],
            "doing": r["status_description"],
            "updated_at": iso(r["updated_at"]),
        }
        for r in rows
    ]


def snapshot(conn: sqlite3.Connection) -> dict[str, dict]:
    """Minimal per-root-session state used by the watcher to diff."""
    rows = conn.execute(
        """SELECT f.id, f.name, f.status, f.status_description, f.updated_at, p.name AS project
           FROM frames f LEFT JOIN projects p ON p.id = f.project_id
           WHERE f.parent_frame_id IS NULL AND f.conversation_type='agent'"""
    )
    return {
        r["id"]: {
            "name": r["name"],
            "status": r["status"],
            "doing": r["status_description"],
            "updated_at": r["updated_at"],
            "project": r["project"],
        }
        for r in rows
    }


# ---------------------------------------------------------------- approval cards

_SECRETISH = ("token", "secret", "password", "passwd", "api_key", "apikey", "private_key", "cookie", "value")
_SUMMARY_KEYS = (
    "target", "host", "hostname", "url", "domain",
    "human_description", "title", "description", "summary", "reason", "message", "command", "cmd", "path", "paths", "folder",
    "credential", "credential_name", "secret_name", "name", "server", "tool", "tool_name",
    "action", "cost", "estimate", "limit", "boundary", "email",
)


def _clean(v, depth: int = 0):
    """Drop internal keys, redact secret-looking values, truncate long strings."""
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if k.startswith("_"):
                continue
            if any(s in k.lower() for s in _SECRETISH) and isinstance(x, str) and k.lower() != "secret_name":
                out[k] = "<redacted>"
            else:
                out[k] = _clean(x, depth + 1) if depth < 4 else "…"
        return out
    if isinstance(v, list):
        return [_clean(x, depth + 1) for x in v[:20]]
    if isinstance(v, str) and len(v) > 600:
        return v[:599] + "…"
    return v


def card_summary(card: dict) -> str:
    """One human line describing an approval card, whatever its kind."""
    kind = card.get("kind") or card.get("type") or "approval"
    bits: list[str] = []
    qs = card.get("questions")
    if isinstance(qs, list) and qs:
        bits.append("; ".join(str(q.get("question", q)) if isinstance(q, dict) else str(q) for q in qs[:3]))
    stack = [card]
    while stack and len(bits) < 3:
        d = stack.pop(0)
        for k in _SUMMARY_KEYS:
            x = d.get(k)
            if isinstance(x, (str, int, float)) and str(x).strip() and str(x) not in bits:
                bits.append(f"{k}={x}" if k not in ("target", "human_description", "title", "description", "summary", "message", "reason") else str(x))
                if len(bits) >= 3:
                    break
        stack += [x for x in d.values() if isinstance(x, dict)]
    return f"{kind}: " + (" | ".join(b[:160] for b in bits) if bits else json.dumps(_clean(card))[:200])


def _has_blobs(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='frame_blobs'").fetchone() is not None


# Frames in these states can carry leftover cards that will never be answered.
_DEAD = ("completed", "cancelled", "failed", "error")


def pending_cards(conn: sqlite3.Connection, root_id: str | None = None) -> list[dict]:
    """Open approval cards across all live frames (root sessions and their sub-agents).

    Since ~v0.1.7x the daemon keeps frame input/output/context JSON in
    frame_blobs(kind='output'), leaving frames.output_data NULL; read either."""
    out = ("COALESCE(b.body, f.output_data)" if _has_blobs(conn) else "f.output_data")
    join = ("LEFT JOIN frame_blobs b ON b.frame_id = f.id AND b.kind = 'output'" if _has_blobs(conn) else "")
    dead = ",".join("'%s'" % d for d in _DEAD)
    sql = f"""SELECT f.id, f.root_frame_id, f.status, {out} AS output_data,
                     r.name AS session_name, p.name AS project
             FROM frames f
             {join}
             LEFT JOIN frames r ON r.id = COALESCE(f.root_frame_id, f.id)
             LEFT JOIN projects p ON p.id = r.project_id
             WHERE f.status NOT IN ({dead}) AND COALESCE(r.status, '') NOT IN ({dead})
               AND {out} LIKE '%pending_input_requests%' AND json_valid({out})
               AND json_array_length(json_extract({out}, '$.pending_input_requests')) > 0"""
    args: list = []
    if root_id:
        sql += " AND (f.root_frame_id = ? OR f.id = ?)"
        args = [root_id, root_id]
    cards = []
    for r in conn.execute(sql, args):
        for c in json.loads(r["output_data"]).get("pending_input_requests") or []:
            if not isinstance(c, dict):
                continue
            rid = c.get("requestId") or c.get("tool_id")
            cards.append({
                "request_id": rid,
                "tool_id": c.get("tool_id"),
                "kind": c.get("kind"),
                "summary": card_summary(c),
                "frame_id": c.get("frameId") or r["id"],
                "session_id": c.get("rootFrameId") or r["root_frame_id"] or r["id"],
                "session_name": r["session_name"],
                "project": r["project"],
                "created_at": c.get("createdAt"),
                "card": _clean(c),
            })
    return cards


def latest_activity_ms(conn: sqlite3.Connection, root_id: str) -> int:
    """Most recent update across the root and all its sub-agent frames."""
    r = conn.execute(
        "SELECT MAX(updated_at) FROM frames WHERE id = ? OR root_frame_id = ?", [root_id, root_id]
    ).fetchone()
    return r[0] or 0
