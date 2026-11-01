"""#74: one status per host and agent, the same in the Home tab and the web UI.
#70: a host that runs only managers is idle-stopped by their activity."""
import asyncio
import datetime as dt
from pathlib import Path

from agentic_lab import registry
from agentic_lab.secretary import home as H
from agentic_lab.secretary import overview as OV
from agentic_lab.secretary import power as P
from agentic_lab.secretary import web as W

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"
T0 = dt.datetime(2026, 10, 23, 12, tzinfo=dt.timezone.utc)


def lab():
    lb = registry.load(EXAMPLE)
    lb.hosts["onprem-1"] = {"kind": "machine"}
    return lb


def sec_host(lb):
    return next(a["host"] for a in lb.agents.values() if a.get("role") == "secretary")


def snapshot(lb, agents, hosts=None):
    base = {aid: {"id": aid, "name": lb.display_name(aid), "role": a.get("role"),
                  "host": a.get("host"), "human": a.get("human"), "up": True}
            for aid, a in lb.agents.items()}
    for aid, extra in agents.items():
        base[aid].update(extra)
    return {"lab": "lab", "at": T0.isoformat(), "agents": base, "accounts": {},
            "approvals": {"pending": [], "grants": {}}, "hosts": hosts or {}}


# ── #74: one status ───────────────────────────────────────────────────────────────

def test_every_host_gets_a_status_including_the_unpowered_ones():
    lb = lab()
    s = OV.annotate(snapshot(lb, {}, {"gpu-a": {"state": "STOPPED"}}), lb, sec_host(lb))
    assert set(s["hosts"]) == set(lb.hosts)                 # all of lab.yaml's
    assert s["hosts"][sec_host(lb)]["status"]["word"] == "always on"
    assert s["hosts"]["gpu-a"]["status"]["word"] == "stopped"
    assert s["hosts"]["onprem-1"]["status"]["word"] == "no agents"


def test_a_host_without_power_control_is_judged_by_its_agents():
    lb = lab()
    w = next(a for a, x in lb.agents.items() if x.get("role") == "worker")
    lb.agents[w]["host"] = "onprem-1"
    up = OV.annotate(snapshot(lb, {}), lb, sec_host(lb))
    assert up["hosts"]["onprem-1"]["status"] == {"word": "running", "level": "ok",
                                                 "hint": "no power control"}
    down = OV.annotate(snapshot(lb, {w: {"up": False, "error": "ConnectTimeout"}}), lb,
                       sec_host(lb))
    assert down["hosts"]["onprem-1"]["status"]["word"] == "unreachable"


def test_stopped_is_not_down():
    lb = lab()
    w = next(a for a, x in lb.agents.items() if x.get("role") == "worker")
    host = lb.agents[w]["host"]
    off = {w: {"up": False, "error": "unreachable: ConnectTimeout"}}
    s = OV.annotate(snapshot(lb, off, {host: {"state": "STOPPED"}}), lb, sec_host(lb))
    assert s["agents"][w]["status"]["word"] == "host stopped"
    assert s["agents"][w]["status"]["level"] == "muted"
    s = OV.annotate(snapshot(lb, off, {host: {"state": "RUNNING"}}), lb, sec_host(lb),
                    now="2026-10-23T12:00:00+00:00")
    st = s["agents"][w]["status"]
    assert st["word"] == "down" and st["level"] == "bad"
    assert st["reason"] == "unreachable: ConnectTimeout" and st["since"] == "2026-10-23T12:00:00+00:00"
    # still down a round later: since when it went down, not now
    later = OV.annotate(snapshot(lb, off, {host: {"state": "RUNNING"}}), lb, sec_host(lb),
                        prev=s, now="2026-10-23T12:05:00+00:00")
    assert later["agents"][w]["status"]["since"] == "2026-10-23T12:00:00+00:00"


def test_both_views_say_the_same():
    lb = lab()
    w = next(a for a, x in lb.agents.items() if x.get("role") == "worker")
    m = next(a for a, x in lb.agents.items() if x.get("role") == "manager")
    host = lb.agents[w]["host"]
    s = OV.annotate(snapshot(lb, {w: {"up": False, "error": "x"}, m: {"up": False}},
                             {host: {"state": "STOPPED"}}), lb, sec_host(lb))
    page = W.render(s)
    home = str(H.view(s, lb.agents[m].get("human"), "http://x"))
    for text in ("host stopped", "ask the secretary to power it on", "always on"):
        assert text in page and text in home
    assert "*Managers*" in home and lb.display_name(m) in home     # managers shown too
    assert "onprem-1" in page and "onprem-1" in home                 # every host, both


def test_strangers_see_the_words_not_the_details():
    from agentic_lab.secretary import visibility as V
    lb = lab()
    w = next(a for a, x in lb.agents.items() if x.get("role") == "worker")
    s = OV.annotate(snapshot(lb, {w: {"up": False, "error": "secret-ish detail"}}), lb,
                    sec_host(lb))
    shaped = V.shape(s, lb, None)
    statuses = [a.get("status") for a in shaped["agents"].values()]
    assert {"word": "down", "level": "bad"} in statuses
    assert "secret-ish detail" not in str(shaped)


# ── #70: managers-only hosts ─────────────────────────────────────────────────────

def mgr_lab(stop_min=30):
    lb = registry.load(EXAMPLE)
    m = next(a for a, x in lb.agents.items() if x.get("role") == "manager")
    host = lb.agents[m]["host"]
    lb.hosts[host]["idle_stop_min"] = stop_min
    return lb, m, host


def quiet(**kw):
    return {"up": True, "activity": {"started_at": T0.isoformat(), "last_turn_at": None,
                                     "turns_in_progress": 0, "followed_tasks_open": 0, **kw}}


def stoppable(lb, host, snap, after=31):
    w = P.IdleWatch()
    w.candidates(lb, snap, now=T0)
    return host in w.candidates(lb, snap, now=T0 + dt.timedelta(minutes=after))


def test_an_idle_managers_only_host_is_stopped():
    lb, m, host = mgr_lab()
    assert P.managers_only(lb, host) == [m]
    assert stoppable(lb, host, {"agents": {m: quiet()}})


def test_what_keeps_a_manager_busy():
    lb, m, host = mgr_lab()
    for seen, why in ((quiet(turns_in_progress=1), "is answering"),
                      (quiet(followed_tasks_open=2), "follows 2 unfinished task(s)"),
                      ({"up": False}, "can't be measured")):
        snap = {"agents": {m: seen}}
        assert why in P.busy(lb, host, snap)
        assert not stoppable(lb, host, snap)
    pending = {"agents": {m: quiet()}, "approvals": {"pending": [{"agent": m}]}}
    assert "1 approval(s) pending" in P.busy(lb, host, pending)
    assert not stoppable(lb, host, pending)


def test_a_recent_turn_restarts_the_idle_clock():
    lb, m, host = mgr_lab()
    turned = quiet(last_turn_at=(T0 + dt.timedelta(minutes=20)).isoformat())
    w = P.IdleWatch()
    w.candidates(lb, {"agents": {m: quiet()}}, now=T0)
    assert host not in w.candidates(lb, {"agents": {m: turned}}, now=T0 + dt.timedelta(minutes=31))
    assert host in w.candidates(lb, {"agents": {m: turned}}, now=T0 + dt.timedelta(minutes=51))


def test_off_by_default_and_hosts_with_workers_unchanged():
    lb, m, host = mgr_lab(stop_min=0)                       # idle_stop_min 0: never
    assert not stoppable(lb, host, {"agents": {m: quiet()}})
    lb, m, host = mgr_lab()
    w = next(a for a, x in lb.agents.items() if x.get("role") == "worker")
    lb.agents[w]["host"] = host                             # a worker there: judged by it
    assert P.managers_only(lb, host) == []
    assert P.busy(lb, host, {"agents": {w: {"up": True, "queue": {}}, m: {"up": False}}}) is None


def test_right_before_the_stop_the_manager_is_asked_again():
    lb, m, host = mgr_lab()
    snap = {"agents": {m: quiet()}}

    async def answering(aid):
        return {**quiet()["activity"], "turns_in_progress": 1}

    async def unreachable(aid):
        raise RuntimeError("timeout")
    assert "is answering" in asyncio.run(P.recheck(lb, host, None, snap, answering))
    assert "couldn't re-check" in asyncio.run(P.recheck(lb, host, None, snap, unreachable))


def test_the_manager_reports_its_activity():
    import types
    from agentic_lab.manager.service import Manager
    mgr = Manager.__new__(Manager)
    mgr._locks, mgr.started_at, mgr.last_turn_at = {}, "2026-10-23T12:00:00+00:00", None
    mgr.state = types.SimpleNamespace(open_tasks=lambda: [1, 2])
    lock = asyncio.Lock()

    async def busy():
        async with lock:
            mgr._locks["c"] = lock
            return mgr.activity()
    act = asyncio.run(busy())
    assert act == {"started_at": "2026-10-23T12:00:00+00:00", "last_turn_at": None,
                   "turns_in_progress": 1, "followed_tasks_open": 2}


def test_a_managers_only_host_whose_manager_is_down_is_announced():
    lb, m, host = mgr_lab()
    w = P.IdleWatch()
    assert w.candidates(lb, {"agents": {m: {"up": False}}}, now=T0,
                        power={host: "RUNNING"}) == []
    assert w.down_since[host] == T0                       # so it's said once it lasts
    assert host in w.down_too_long(now=T0 + dt.timedelta(minutes=P.DOWN_GRACE_MIN))
    w.candidates(lb, {"agents": {m: {"up": False}}}, now=T0, power={host: "STOPPED"})
    assert host not in w.down_since                       # stopped: of course it's down


def test_idle_counts_from_the_end_of_the_last_turn():
    import types
    from agentic_lab.manager import service as MS
    mgr = MS.Manager.__new__(MS.Manager)
    mgr._locks, mgr._told_busy, mgr.last_turn_at = {}, set(), None
    mgr.state = types.SimpleNamespace(save=lambda: None)
    mgr._ctx = lambda conv, requester, private: types.SimpleNamespace(guest=False)
    times = iter(["2026-10-23T12:00:00+00:00", "2026-10-23T12:04:30+00:00"])
    seen = []

    class Brain:
        async def turn(self, ctx, prompt):
            seen.append(mgr.last_turn_at)                 # set when the turn started
            return "ok"
    mgr.brain = Brain()
    orig = MS._now
    MS._now = lambda: next(times)
    try:
        conv = types.SimpleNamespace(key="c", last_requester=None, meanwhile=[])
        assert asyncio.run(mgr._turn(conv, "alice", True, "hi")) == "ok"
    finally:
        MS._now = orig
    assert seen == ["2026-10-23T12:00:00+00:00"]
    assert mgr.last_turn_at == "2026-10-23T12:04:30+00:00"   # and again when it ended


# ── a worker's short tasks between two looks ─────────────────────────────────

def test_a_short_task_between_two_looks_restarts_a_workers_idle_clock():
    """The secretary looks at a worker's queue only every few minutes: a task that started
    and ended between two looks never shows as busy, so the clock goes by the worker's
    latest task instead (its health's activity)."""
    lb = registry.load(EXAMPLE)
    w = next(a for a, x in lb.agents.items() if x.get("role") == "worker")
    host = lb.agents[w]["host"]
    lb.hosts[host]["idle_stop_min"] = 30
    idle = {"up": True, "queue": {"done": 1}, "activity": {"last_turn_at": None}}
    ran = {**idle, "activity": {"last_turn_at": (T0 + dt.timedelta(minutes=20)).isoformat()}}
    watch = P.IdleWatch()
    watch.candidates(lb, {"agents": {w: idle}}, now=T0)
    assert host not in watch.candidates(lb, {"agents": {w: ran}}, now=T0 + dt.timedelta(minutes=31))
    assert host in watch.candidates(lb, {"agents": {w: ran}}, now=T0 + dt.timedelta(minutes=51))


def test_a_workers_health_says_when_its_latest_task_started_or_ended(tmp_path):
    from agentic_lab.worker import server as S
    from agentic_lab.worker import tasks as T
    store = T.TaskStore(tmp_path)
    assert S.default_health("w", store)["activity"] == {"last_turn_at": None}
    t = store.submit("a", requester="alice", manager="m")
    store.update(t.id, state=T.RUNNING, started="2026-11-01T15:51:07+00:00")
    store.update(t.id, state=T.DONE, finished="2026-11-01T15:52:50+00:00")
    u = store.submit("b", requester="alice", manager="m")
    store.update(u.id, state=T.RUNNING, started="2026-11-01T15:54:37+00:00")
    assert S.default_health("w", store)["activity"] == {"last_turn_at": "2026-11-01T15:54:37+00:00"}


def test_the_secretarys_snapshot_carries_a_workers_activity():
    from agentic_lab.secretary import status as ST
    lb = registry.load(EXAMPLE)
    w = next(a for a, x in lb.agents.items() if x.get("role") == "worker")
    act = {"last_turn_at": "2026-11-01T15:56:47+00:00"}

    class Workers:
        def known(self):
            return [w]

        async def call(self, aid, tool, args=None, retries=None):
            return {"tasks": {"done": 2}, "activity": act} if tool == "health_check" else []
    snap = asyncio.run(ST.collect(lb, Workers()))
    assert snap["agents"][w]["activity"] == act
