import json
import sqlite3

from science_bridge import db


def make_db(tmp_path):
    p = tmp_path / "t.db"
    c = sqlite3.connect(p)
    c.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, updated_at INT, archived_at INT);
        CREATE TABLE frames (id TEXT PRIMARY KEY, parent_frame_id TEXT, root_frame_id TEXT, agent_name TEXT,
          delegate_name TEXT, status TEXT, status_description TEXT, project_id TEXT, name TEXT, model TEXT,
          conversation_type TEXT, is_hidden INT, created_at INT, updated_at INT, completed_at INT,
          total_cost REAL, aux_cost REAL);
        CREATE TABLE frame_messages (frame_id TEXT, idx INT, msg_json TEXT);
        INSERT INTO projects VALUES ('p1','Modeling',1,NULL);
        INSERT INTO frames VALUES ('aaaaaaaa-1','', 'aaaaaaaa-1','OPERON',NULL,'processing','fitting','p1',
          'Model retrain','m','agent',0,1,5,NULL,1.0,0.5);
        INSERT INTO frames VALUES ('child-1','aaaaaaaa-1','aaaaaaaa-1','sub',NULL,'processing','x','p1',
          NULL,'m','agent',0,1,9,NULL,0,0);
        """
    )
    c.execute("UPDATE frames SET parent_frame_id=NULL WHERE id='aaaaaaaa-1'")
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "rebuild the model"}]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "..."},
                                          {"type": "tool_use", "name": "bash", "input": {"cmd": "ls"}},
                                          {"type": "text", "text": "Started."}]},
        {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
    ]
    for i, m in enumerate(msgs):
        c.execute("INSERT INTO frame_messages VALUES ('aaaaaaaa-1',?,?)", (i, json.dumps(m)))
    c.commit()
    return p


def test_sessions_and_lookup(tmp_path):
    conn = db.connect(make_db(tmp_path))
    ss = db.list_sessions(conn, status="active")
    assert [x.name for x in ss] == ["Model retrain"] and ss[0].active_children == 1
    assert ss[0].as_dict()["cost_usd"] == 1.5
    assert db.get_session(conn, "aaaaaaaa").id == "aaaaaaaa-1"
    assert db.get_session(conn, "retrain").id == "aaaaaaaa-1"
    assert db.latest_activity_ms(conn, "aaaaaaaa-1") == 9


def test_transcript_hides_tools_by_default(tmp_path):
    conn = db.connect(make_db(tmp_path))
    t = db.transcript(conn, "aaaaaaaa-1")
    assert t == ["USER: rebuild the model", "CLAUDE: Started."]
    t2 = db.transcript(conn, "aaaaaaaa-1", include_tools=True)
    assert any("[tool bash]" in x for x in t2) and t2[-1].startswith("TOOL:")


def test_db_is_read_only(tmp_path):
    conn = db.connect(make_db(tmp_path))
    try:
        conn.execute("DELETE FROM frames")
        raise AssertionError("write should fail")
    except sqlite3.OperationalError:
        pass
