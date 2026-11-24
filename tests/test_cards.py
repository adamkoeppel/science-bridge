import json
import sqlite3

import pytest

from science_bridge import db
from science_bridge.mcp_server import build_response
from science_bridge.watch import card_events

NET = {"requestId": "req-net-1", "tool_id": "toolu_1", "kind": "network", "frameId": "child-1",
       "rootFrameId": "root-1", "host": "api.example.org", "human_description": "Pulling raw readings",
       "_internal": "x"}
CRED = {"requestId": "req-cred-1", "kind": "credential_access", "frameId": "root-1", "rootFrameId": "root-1",
        "credential_name": "DATA_API_KEY", "token": "sk-should-not-leak"}
ASK = {"requestId": "req-ask-1", "tool_id": "toolu_9", "kind": "ask", "frameId": "root-1", "rootFrameId": "root-1",
       "questions": [{"question": "Which dataset?", "options": ["v3", "v4"]}]}


def make_db(tmp_path):
    p = tmp_path / "c.db"
    c = sqlite3.connect(p)
    c.executescript("""
      CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT);
      CREATE TABLE frames (id TEXT PRIMARY KEY, root_frame_id TEXT, status TEXT, output_data TEXT, name TEXT, project_id TEXT);
      INSERT INTO projects VALUES ('p1', 'Modeling');
    """)
    c.execute("INSERT INTO frames VALUES ('root-1','root-1','processing',?, 'Model retrain','p1')",
              [json.dumps({"pending_input_requests": [CRED, ASK]})])
    c.execute("INSERT INTO frames VALUES ('child-1','root-1','processing',?, NULL, NULL)",
              [json.dumps({"pending_input_requests": [NET]})])
    c.execute("INSERT INTO frames VALUES ('other','other','completed',?, 'Old', NULL)",
              [json.dumps({"pending_input_requests": []})])
    c.commit()
    return p


def test_pending_cards_found_including_subagents_and_redacted(tmp_path):
    cards = db.pending_cards(db.connect(make_db(tmp_path)))
    by = {c["request_id"]: c for c in cards}
    assert set(by) == {"req-net-1", "req-cred-1", "req-ask-1"}
    net = by["req-net-1"]
    assert net["frame_id"] == "child-1" and net["session_name"] == "Model retrain" and net["project"] == "Modeling"
    assert "_internal" not in net["card"]
    assert "Pulling raw readings" in net["summary"] and "api.example.org" in net["summary"]
    assert by["req-cred-1"]["card"]["token"] == "<redacted>"
    assert "sk-should-not-leak" not in json.dumps(cards)
    assert "Which dataset?" in by["req-ask-1"]["summary"]


def test_pending_cards_scoped_to_session(tmp_path):
    conn = db.connect(make_db(tmp_path))
    assert len(db.pending_cards(conn, "root-1")) == 3
    assert db.pending_cards(conn, "other") == []


def test_card_events_fire_once():
    cards = [{"request_id": "r1", "session_id": "root-1", "session_name": "S", "project": None, "summary": "network: x"}]
    ev, seen = card_events(cards, set())
    assert len(ev) == 1 and ev[0].startswith("CARD")
    ev2, seen = card_events(cards, seen)
    assert ev2 == []
    ev3, seen = card_events([], seen)
    assert seen == set()


def card(c):
    return {"tool_id": c.get("tool_id"), "card": c}


def test_build_response_shapes():
    assert build_response(card(NET), "allow_once", None, None, None) == {
        "action": "allow_once", "tool_id": "toolu_1", "requestId": "req-net-1"}
    assert build_response(card(CRED), "allow", "project", None, None)["scope"] == "project"
    r = build_response(card(ASK), "answer", None, {"Which dataset?": "v4"}, None)
    assert r["answers"] == {"Which dataset?": "v4"} and r["tool_id"] == "toolu_9"
    assert build_response(card(CRED), "deny", None, None, None)["action"] == "deny"


@pytest.mark.parametrize("decision,scope,answers,message", [
    ("allow", None, None, None),          # allow needs a scope
    ("answer", None, None, None),         # answer needs answers
    ("discuss", None, None, None),        # discuss needs a message
    ("approve", None, None, None),        # unknown decision
])
def test_build_response_rejects(decision, scope, answers, message):
    with pytest.raises(ValueError):
        build_response(card(NET), decision, scope, answers, message)


def test_pending_cards_read_from_frame_blobs_and_skip_dead_frames(tmp_path):
    """Current daemons keep output JSON in frame_blobs and leave frames.output_data NULL."""
    p = tmp_path / "b.db"
    c = sqlite3.connect(p)
    c.executescript("""
      CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT);
      CREATE TABLE frames (id TEXT PRIMARY KEY, root_frame_id TEXT, status TEXT, output_data TEXT, name TEXT, project_id TEXT);
      CREATE TABLE frame_blobs (frame_id TEXT, kind TEXT, updated_at INTEGER, body TEXT, PRIMARY KEY(frame_id, kind));
      INSERT INTO frames VALUES ('live','live','awaiting_user_response',NULL,'Live',NULL);
      INSERT INTO frames VALUES ('dead','dead','cancelled',NULL,'Dead',NULL);
    """)
    live = {"target": "httpbin.org", "reason": "test fetch", "kind": "network", "requestId": "r-live",
            "frameId": "live", "rootFrameId": "live", "tool_id": "toolu_x"}
    dead = {"kind": "ask", "requestId": "r-dead", "frameId": "dead", "rootFrameId": "dead",
            "questions": [{"question": "stale?"}]}
    c.execute("INSERT INTO frame_blobs VALUES ('live','output',0,?)", [json.dumps({"pending_input_requests": [live]})])
    c.execute("INSERT INTO frame_blobs VALUES ('dead','output',0,?)", [json.dumps({"pending_input_requests": [dead]})])
    c.commit()
    cards = db.pending_cards(db.connect(p))
    assert [x["request_id"] for x in cards] == ["r-live"]
    assert cards[0]["summary"].startswith("network: httpbin.org")
