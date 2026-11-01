"""A host that missed a deploy is brought up to what the lab runs when it's back (#61)."""
import asyncio
import datetime as dt
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_lab import apply as A
from agentic_lab import configrepo as CR
from agentic_lab import registry
from agentic_lab.secretary import catchup as CU
from agentic_lab.secretary.web import render

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"
NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc)
NEW, OLD, NEWER, FW = "c" * 40, "a" * 40, "d" * 40, "f" * 40


@pytest.fixture(autouse=True)
def history(monkeypatch):
    """OLD comes before NEW; NEWER after it."""
    order = [OLD, NEW, NEWER]
    monkeypatch.setattr(CU, "is_ancestor", lambda repo, old, new: (
        old in order and new in order and order.index(old) < order.index(new)))


def lab(**top):
    lb = registry.load(EXAMPLE)
    lb.data.update(top)
    return lb


def state(applied=OLD, queue=None, up=True, unreviewed=False):
    """The secretary's host runs NEW; worker-a's host (gpu-a) reports `applied`."""
    ap = {"config_commit": applied, "framework_commit": FW}
    if unreviewed:
        ap["unreviewed"] = True
    return {"applied": {"config_commit": NEW, "framework_commit": FW},
            "agents": {"worker-a": {"host": "gpu-a", "up": up, "queue": queue or {},
                                    "applied": ap}}}


class Deploys:
    """A fake `lab apply-main --catch-up`: its log says what happened to each host."""

    def __init__(self, tmp, outcome="done", busy=False):
        self.tmp, self.started, self.outcome, self.busy, self.said = tmp, 0, outcome, busy, []

    def start(self):
        if self.busy:
            raise RuntimeError("couldn't start the deployment: a deploy is already running")
        self.started += 1
        log = self.tmp / f"apply-{self.started}.log"
        log.write_text(f"CATCH-UP gpu-a {self.outcome}\n" if self.outcome else "")
        return str(log)

    def result(self, log):
        return None if self.outcome is None else (self.outcome == "done", "")

    async def announce(self, text):
        self.said.append(text)


def run(cu, lb, st, d, now=NOW):
    asyncio.run(CU.step(cu, lb, st, "sec-host", d.start, d.result, d.announce, now=now))


def later(minutes):
    return NOW + dt.timedelta(minutes=minutes)


def test_a_stale_host_is_caught_up_and_announced(tmp_path):
    cu, d, lb = CU.CatchUp(str(tmp_path / "c.json")), Deploys(tmp_path), lab()
    assert CU.behind(state(), "sec-host", "repo") == {"gpu-a": {"config_commit": OLD,
                                                                "framework_commit": FW}}
    run(cu, lb, state(), d)
    assert d.started == 1 and cu.running["hosts"] == ["gpu-a"]
    run(cu, lb, state(applied=NEW), d, now=later(3))      # its worker now reports NEW
    assert d.said == [f":white_check_mark: gpu-a is up (updated to {NEW[:7]})."]
    assert d.started == 1 and CU.CatchUp(str(tmp_path / "c.json")).attempts == {}


def test_newer_or_unreviewed_hosts_are_left_alone(tmp_path):
    d = Deploys(tmp_path)
    for st in (state(applied=NEWER), state(applied=OLD, unreviewed=True)):
        run(CU.CatchUp(str(tmp_path / "c.json")), lab(), st, d)
        assert CU.behind(st, "sec-host", "repo") == {}
    assert d.started == 0
    info = CU.stale_info(state(applied=NEWER), "sec-host", "repo")["gpu-a"]
    assert info["ahead"] and info["config_behind"] is None


def test_busy_hosts_wait_and_nothing_runs_when_switched_off(tmp_path):
    d = Deploys(tmp_path)
    run(CU.CatchUp(str(tmp_path / "a.json")), lab(), state(queue={"running": 1}), d)
    run(CU.CatchUp(str(tmp_path / "b.json")), lab(auto_catch_up=False), state(), d)
    assert d.started == 0


def test_an_unreviewed_lab_is_never_spread(tmp_path):
    st = state()
    st["applied"]["unreviewed"] = True
    d = Deploys(tmp_path)
    run(CU.CatchUp(str(tmp_path / "c.json")), lab(), st, d)
    assert d.started == 0


def test_only_failures_count_and_are_limited(tmp_path):
    cu, lb = CU.CatchUp(str(tmp_path / "c.json")), lab()
    d = Deploys(tmp_path, outcome="unreachable")          # still booting: not an attempt
    for i in range(5):
        run(cu, lb, state(), d, now=later(3 * i))
    assert cu.attempts.get(f"gpu-a {NEW}", {"n": 0})["n"] == 0
    d.outcome = "failed"
    t = later(30)
    for _ in range(8):
        run(cu, lb, state(), d, now=t)
        t += dt.timedelta(minutes=30)
    assert cu.attempts[f"gpu-a {NEW}"]["n"] == CU.MAX_ATTEMPTS
    assert len([s for s in d.said if "stays stale" in s]) == 1
    n = d.started
    run(cu, lb, state(), d, now=t + dt.timedelta(hours=CU.FORGET_AFTER_H + 1))   # a day on
    assert d.started == n + 1


def test_pauses_between_attempts(tmp_path):
    cu, d, lb = CU.CatchUp(str(tmp_path / "c.json")), Deploys(tmp_path, "failed"), lab()
    run(cu, lb, state(), d)
    run(cu, lb, state(), d, now=later(3))
    assert d.started == 1
    run(cu, lb, state(), d, now=later(CU.PAUSE_MIN[0] + 1))
    assert d.started == 2


def test_a_catch_up_that_never_reports_is_given_up(tmp_path):
    cu, d, lb = CU.CatchUp(str(tmp_path / "c.json")), Deploys(tmp_path, outcome=None), lab()
    run(cu, lb, state(), d)
    run(cu, lb, state(), d, now=later(10))
    assert cu.running is not None and d.started == 1
    run(cu, lb, state(), d, now=later(CU.RUN_TIMEOUT_MIN + 1))
    assert cu.attempts[f"gpu-a {NEW}"]["n"] == 1


def test_another_deploy_running_is_waited_for(tmp_path):
    cu, d = CU.CatchUp(str(tmp_path / "c.json")), Deploys(tmp_path, busy=True)
    run(cu, lab(), state(), d)
    assert cu.attempts == {} and cu.running is None


def test_the_dashboard_shows_stale_hosts():
    st = state()
    st.update(lab="L", at=NOW.isoformat(), accounts={}, approvals={})
    st["agents"]["worker-a"].update(name="worker-a", role="worker")
    st["hosts"] = {"gpu-a": {"state": "RUNNING",
                             "stale": CU.stale_info(state(), "sec-host")["gpu-a"]}}
    assert "stale: config differs" in render(st)


def test_only_a_commit_of_main_is_deployed(tmp_path):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}

    def git(cwd, *a):
        return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                              text=True, env=env).stdout.strip()
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(work))
    (work / "lab.yaml").write_text("lab: one\n")
    git(work, "add", "lab.yaml")
    git(work, "commit", "-qm", "one")
    first = git(work, "rev-parse", "HEAD")
    (work / "lab.yaml").write_text("lab: two\n")
    git(work, "commit", "-qam", "two")
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-q", "-b", "side")
    (work / "lab.yaml").write_text("lab: side\n")
    git(work, "commit", "-qam", "side")
    side = git(work, "rev-parse", "HEAD")
    path, info = CR.fetch_main(str(work / "lab.yaml"), first)
    assert info["config_commit"] == first and Path(path).read_text() == "lab: one\n"
    with pytest.raises(CR.ConfigError, match="isn't a commit of main"):
        CR.fetch_main(str(work / "lab.yaml"), side)


def test_a_catch_up_deploys_only_older_hosts(monkeypatch, capsys):
    from agentic_lab import cli, doctor, lock
    plans = [{"host": h, "name": h, "missing": [], "units": [1]} for h in ("gpu-a", "gpu-b")]
    shipped = []
    monkeypatch.setattr(A, "plan_all", lambda *a, **k: (
        SimpleNamespace(), SimpleNamespace(save=lambda: None), plans))
    monkeypatch.setattr(cli.registry, "check", lambda lb: [])
    monkeypatch.setattr(cli.registry, "load", lambda path: SimpleNamespace())
    monkeypatch.setattr(lock, "current", lambda src: (None, FW))
    monkeypatch.setattr(A, "secretary_host", lambda lb: ("sec-host", "secretary"))
    monkeypatch.setattr(doctor, "this_host", lambda lb: "sec-host")
    monkeypatch.setattr(A, "describe", lambda p: p["host"])
    monkeypatch.setattr(A, "has_work", lambda p: True)
    plans.append({"host": "gpu-c", "name": "gpu-c", "missing": [], "units": [1]})
    plans.append({"host": "gpu-new", "name": "gpu-new", "missing": [], "units": [1]})
    runs = {"gpu-a": NEW, "gpu-b": OLD, "gpu-c": NEWER}
    monkeypatch.setattr(A, "applied_on", lambda p, local, admin: {
        "config_commit": runs[p["host"]], "framework_commit": FW} if p["host"] in runs
        else {})                                            # never deployed
    monkeypatch.setattr(A, "run_bundle", lambda p, local, admin: shipped.append(
        (p["host"], p.get("catch_up"))) or 0)
    monkeypatch.setattr(cli, "deploy_lock", lambda: None)
    older = lambda old: old == OLD                          # noqa: E731
    assert cli.deploy("lab.yaml", {"config_commit": NEW}, ".", "admin", only_behind=older) == 0
    # not the current one, never the newer one; a host that never got a deploy gets one
    assert shipped == [("gpu-b", True), ("gpu-new", True)]
    out = capsys.readouterr().out
    assert "CATCH-UP gpu-a current" in out and "CATCH-UP gpu-c ahead" in out \
        and "CATCH-UP gpu-b done" in out and "CATCH-UP gpu-new done" in out


def test_the_host_checks_for_active_work_itself(tmp_path):
    lb = registry.load(EXAMPLE)
    plan = A.host_plan(lb, "gpu-a", SimpleNamespace(get=lambda k: "t"),
                       lambda owner, name: "secret")
    plan["catch_up"] = True
    s = A.script(plan)
    line = next(ln for ln in s.splitlines() if ln.startswith("if runuser -u lab-worker-a"))
    assert f"exit {A.BUSY}" in line
    assert subprocess.run(["bash", "-n"], input=s, text=True).returncode == 0
    # nothing on the host changes before it: only the head (variables, a check) comes first
    lines = s.splitlines()
    before = lines[:lines.index(line)]
    changes = ("mkdir", "rm ", "tar ", "install ", "mv ", "uv ", "systemctl", "ln ", "chown",
               "chmod", "cat >", "curl", "apt", "useradd", "runuser -u lab-worker-a -- sh -c")
    assert not [ln for ln in before if ln.lstrip().startswith(changes)
                and not ln.startswith("if runuser")]
    tasks = tmp_path / "home/lab-worker-a/.local/state/agentic-lab/tasks"
    tasks.mkdir(parents=True)
    import json
    import shlex
    check = shlex.split(line.split(" -- ", 1)[1].split(" >/dev/null")[0])[2]
    check = check.replace("/home/", f"{tmp_path}/home/")

    def busy():
        return subprocess.run(["sh", "-c", check], capture_output=True).returncode == 0
    (tasks / "t-1.json").write_text(json.dumps({"id": "t-1", "state": "done"}, indent=1))
    assert not busy()
    (tasks / "t-2.json").write_text(json.dumps({"id": "t-2", "state": "queued"}, indent=1))
    assert busy()


def test_the_secretary_may_run_the_catch_up():
    s = A.self_admin_script({"config_owner": "lab-secretary", "config_url": "u",
                             "lab_rel": "lab.yaml", "framework_source": "/src",
                             "framework_owner": "admin"})
    line = next(ln for ln in s if ln.startswith("printf"))
    out = subprocess.run(["bash", "-c", line.split(" > ")[0]], capture_output=True,
                         text=True).stdout
    assert out.splitlines() == [
        "lab-secretary ALL=(root) NOPASSWD: /usr/local/bin/lab apply-main",
        "lab-secretary ALL=(root) NOPASSWD: /usr/local/bin/lab apply-main --catch-up"]


def test_a_host_busy_for_long_is_said_once(tmp_path):
    cu, d, lb = CU.CatchUp(str(tmp_path / "c.json")), Deploys(tmp_path), lab()
    busy = state(queue={"waiting": 1})                     # parked on its runs: busy too
    run(cu, lb, busy, d)
    run(cu, lb, busy, d, now=later(60 * (CU.BUSY_NOTICE_H + 1)))
    run(cu, lb, busy, d, now=later(60 * (CU.BUSY_NOTICE_H + 2)))
    assert d.started == 0 and len([s for s in d.said if "busy for over" in s]) == 1


def test_current_hosts_arent_stale_when_the_lab_runs_something_unreviewed(monkeypatch):
    st = state(applied=NEW)
    st["applied"]["unreviewed"] = True
    monkeypatch.setattr(CU, "main_commit", lambda repo: NEW)
    assert CU.stale_info(st, "sec-host", "repo") == {}


def test_ssh_that_answers_but_fails_isnt_unreachable(monkeypatch):
    plan = {"name": "gpu-a", "ssh_user": None}
    err = {"text": "Host key verification failed."}
    monkeypatch.setattr(A.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 255, "", err["text"]))
    monkeypatch.setattr(A.os, "geteuid", lambda: 1000)
    assert A.applied_on(plan, local=False, admin="admin") == A.SSH_FAILED
    err["text"] = "ssh: connect to host gpu-a port 22: Connection timed out"
    assert A.applied_on(plan, local=False, admin="admin") == A.UNREACHABLE


def test_tool_grants_dont_restart_the_session(tmp_path, monkeypatch):
    from agentic_lab.worker import tasks as T
    from agentic_lab.worker.runner import SdkRunner
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat()

    class Approver:
        async def active_grants(self):
            return [{"kind": "tool", "value": "WebFetch", "expires": soon}]
    store = T.TaskStore(tmp_path / "tasks")
    t = store.update(store.submit("x", "alice", "m").id, state=T.RUNNING)
    r = SdkRunner("w", str(tmp_path), lambda g: {}, approver=Approver())
    seen = []

    async def session(task, store_, outcome, sess, grants, prompt):
        seen.append(sess["narrow_at"])
        outcome.update(result={"summary": "ok", "artifacts": []}, success=True)
        return None, None
    monkeypatch.setattr(r, "_session", session)
    asyncio.run(r.run(t, store))
    assert seen == [None]                   # checked on each call instead
