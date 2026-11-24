from science_bridge.watch import diff

MIN = 60_000


def s(status, updated=0, name="Run", doing=None, project="P"):
    return {"status": status, "updated_at": updated, "name": name, "doing": doing, "project": project}


def run(prev, cur, now=100 * MIN, activity=None, stalled=None):
    stalled = set() if stalled is None else stalled
    return diff(prev, cur, now_ms=now, stall_ms=45 * MIN, activity=activity or {}, stalled=stalled), stalled


def test_completion_from_processing():
    ev, _ = run({"a": s("processing")}, {"a": s("completed", doing="wrote report")})
    assert len(ev) == 1 and ev[0].startswith("DONE") and "wrote report" in ev[0]


def test_needs_you_plan_and_card():
    ev, _ = run({"a": s("processing"), "b": s("processing")},
                {"a": s("awaiting_plan_approval"), "b": s("awaiting_user_response")},
                activity={})
    assert [e.split()[0] for e in ev] == ["NEEDS_YOU", "NEEDS_YOU"]
    assert "plan approval" in ev[0] and "approval card" in ev[1]


def test_failed_and_cancelled():
    ev, _ = run({"a": s("processing"), "b": s("processing")}, {"a": s("failed"), "b": s("cancelled")})
    assert all(e.startswith("FAILED") for e in ev) and len(ev) == 2


def test_no_event_when_unchanged_or_new_completed():
    ev, _ = run({"a": s("completed")}, {"a": s("completed"), "new": s("completed")})
    assert ev == []


def test_new_session_already_waiting_is_reported():
    ev, _ = run({}, {"x": s("awaiting_user_response")})
    assert ev and ev[0].startswith("NEEDS_YOU")


def test_stall_fires_once_then_clears_on_activity():
    prev = {"a": s("processing", updated=0)}
    ev, st = run(prev, {"a": s("processing", updated=0)}, activity={"a": 0})
    assert len(ev) == 1 and ev[0].startswith("STALLED")
    ev2, st = run(prev, {"a": s("processing")}, activity={"a": 0}, stalled=st)
    assert ev2 == []  # not repeated
    ev3, st = run(prev, {"a": s("processing")}, activity={"a": 99 * MIN}, stalled=st)
    assert ev3 == [] and "a" not in st  # cleared, can fire again later


def test_subagent_activity_prevents_false_stall():
    ev, _ = run({"a": s("processing", updated=0)}, {"a": s("processing", updated=0)}, activity={"a": 90 * MIN})
    assert ev == []
