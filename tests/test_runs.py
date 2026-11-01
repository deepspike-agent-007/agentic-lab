"""Long runs: jobs that outlive a worker's Claude session, approved one at a time, with the
task parked (waiting) until they end."""
import asyncio
import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from agentic_lab import registry
from agentic_lab.worker import runs as R
from agentic_lab.worker import tasks as T
from agentic_lab.worker.runner import SdkRunner, _DETACH, allowed_without_asking
from agentic_lab.worker.server import wake_waiting

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"
CTL = Path(__file__).parent.parent / "deploy" / "worker" / "lab-run-ctl"


class Units:
    """Stands in for systemd: which run units are active, and what was asked."""

    def __init__(self):
        self.active, self.calls = set(), []

    def __call__(self, verb, agent, run_id):
        self.calls.append((verb, agent, run_id))
        if verb == "start":
            self.active.add(run_id)
        elif verb == "stop":
            self.active.discard(run_id)
        return run_id in self.active


NO_WRAP = lambda *a: []     # noqa: E731  (no bwrap here: the job as is)


def stores(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    units = Units()
    return (T.TaskStore(tmp_path / "tasks"),
            R.RunStore(str(tmp_path / "runs"), str(work), "worker-a", systemctl=units), units)


def test_a_run_runs_logs_and_records_how_it_ended(tmp_path):
    tasks, runs, units = stores(tmp_path)
    r = runs.start("t-1", "train big/model", "echo step 1; echo oops >&2; exit 3", requester="alice",
                   policy={"allowWrite": ["/w"], "denyRead": ["/s"]})
    assert r["state"] == "starting" and r["name"] == "train-big-model"
    assert units.calls[0] == ("start", "worker-a", r["id"])
    assert r["log"].startswith(str(tmp_path / "work" / "runs"))
    R.run_exec(runs, r["id"], wrapper=NO_WRAP)                     # what the unit does
    units.active.discard(r["id"])
    v = runs.view(r["id"], tail=10)
    assert v["state"] == "exited" and v["exit_code"] == 3
    assert "step 1" in v["log_tail"] and "oops" in v["log_tail"]
    assert runs.active() == []
    with pytest.raises(R.RunError):
        runs.start("t-1", "x", "true", cwd="../../etc", requester="alice")


def test_a_run_whose_unit_vanished_is_lost(tmp_path):
    _, runs, units = stores(tmp_path)
    r = runs.start("t-1", "train", "sleep 100", requester="alice")
    runs._write(runs._path(r["id"], ".status"), {"state": "running", "started": "x"})
    assert runs.view(r["id"])["state"] == "running"
    units.active.discard(r["id"])                 # e.g. the machine was powered off
    assert runs.view(r["id"])["state"] == "lost"


def test_waiting_tasks_wake_when_a_run_ends_or_at_check_in(tmp_path):
    tasks, runs, units = stores(tmp_path)
    t = tasks.submit("train for 20 hours", "alice", "mgr-alice")
    tasks.update(t.id, state=T.RUNNING)
    r = runs.start(t.id, "train", "sleep 100", requester="alice")
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat()
    tasks.update(t.id, state=T.WAITING, waiting={"note": "training", "runs": [r["id"]],
                                                 "wake_at": soon})
    assert wake_waiting(tasks, runs) == []                    # still going
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)
    [w] = wake_waiting(tasks, runs, now=later)                # check-in time
    assert w.state == T.QUEUED and "check-in" in w.wake
    tasks.update(t.id, state=T.RUNNING)
    tasks.update(t.id, state=T.WAITING, wake=None, waiting={"runs": [r["id"]]})
    units.active.discard(r["id"])
    runs._write(runs._path(r["id"], ".status"), {"state": "exited", "exit_code": 0})
    [w] = wake_waiting(tasks, runs)                           # the run ended
    assert "exited" in w.wake


def test_cancelling_a_waiting_task_stops_its_runs(tmp_path):
    tasks, runs, units = stores(tmp_path)
    t = tasks.submit("train", "alice", "mgr-alice")
    tasks.update(t.id, state=T.RUNNING)
    r = runs.start(t.id, "train", "sleep 100", requester="alice")
    tasks.update(t.id, state=T.WAITING, waiting={"runs": [r["id"]]})
    tasks.request_cancel(t.id)
    wake_waiting(tasks, runs)
    assert tasks.get(t.id).state == T.CANCELLED
    assert ("stop", "worker-a", r["id"]) in units.calls


def runner_for(tmp_path):
    tasks, runs, units = stores(tmp_path)
    runner = SdkRunner("worker-a", str(tmp_path / "work"), lambda g: {}, runs=runs)
    t = tasks.update(tasks.submit("train", "alice", "mgr-alice").id, state=T.RUNNING)
    return tasks, runs, units, runner, t


def tools(runner, t, tasks, outcome):
    return {x.name: x.handler for x in runner._run_tools(t, tasks, outcome)}, None


def call(handler, args):
    return asyncio.run(handler(args))


def test_run_tools_park_the_task(tmp_path):
    tasks, runs, units, runner, t = runner_for(tmp_path)
    outcome = {}
    h, _ = tools(runner, t, tasks, outcome)
    assert call(h["wait_for_runs"], {"note": "x"}).get("is_error")     # nothing to wait for
    out = call(h["start_run"], {"name": "train", "command": "python train.py"})
    run_id = json.loads(out["content"][0]["text"].splitlines()[0])["id"]
    assert "started run" in tasks.get(t.id).progress[-1]["message"]
    call(h["wait_for_runs"], {"note": "training the big model", "check_in_minutes": 120})
    runner._finish(t, tasks, outcome, None, None)
    w = tasks.get(t.id)
    assert w.state == T.WAITING and w.waiting["runs"] == [run_id]
    assert w.waiting["wake_at"] is not None
    # woken: the resumed session hears about its runs
    w = tasks.update(t.id, state=T.QUEUED, wake="run r-x exited")
    prompt = runner._wake_prompt(w)
    assert "run r-x exited" in prompt and "training the big model" in prompt
    assert run_id in prompt and "finish_task" in prompt


def test_finish_waits_for_runs_and_a_silent_end_parks(tmp_path):
    tasks, runs, units, runner, t = runner_for(tmp_path)
    outcome = {}
    h, _ = tools(runner, t, tasks, outcome)
    call(h["start_run"], {"name": "train", "command": "python train.py"})
    finish = next(x.handler for x in _lab_tools(runner, t, tasks, outcome)
                  if x.name == "finish_task")
    assert call(finish, {"summary": "done"}).get("is_error")
    assert "result" not in outcome
    runner._finish(t, tasks, outcome, None, "I'll let it train")     # ended without a word
    assert tasks.get(t.id).state == T.WAITING


def _lab_tools(runner, t, tasks, outcome):
    """The tools of the lab MCP server (report_progress, finish_task, the run tools)."""
    import claude_agent_sdk
    made = []
    real = claude_agent_sdk.create_sdk_mcp_server

    def capture(name, tools=None, **kw):
        made.extend(tools or [])
        return real(name, tools=tools, **kw)
    import agentic_lab.worker.runner as RN
    RN.create_sdk_mcp_server, old = capture, RN.create_sdk_mcp_server
    try:
        runner._lab_server(t, tasks, outcome)
    finally:
        RN.create_sdk_mcp_server = old
    return made


def test_starting_a_run_always_asks_a_human():
    assert not allowed_without_asking("mcp__lab__start_run", {"command": "python train.py"})
    for name in ("run_status", "stop_run", "wait_for_runs"):
        assert allowed_without_asking(f"mcp__lab__{name}", {})
    from agentic_lab.broker.core import describe, grant_options
    assert grant_options("mcp__lab__start_run", {"command": "x"}, []) == []   # one-off only
    d = describe("mcp__lab__start_run", {"name": "train", "command": "python t.py"})
    assert "until it ends or is stopped" in d and "network not limited" in d
    assert "python t.py" in d


def test_detached_commands_get_a_hint():
    for cmd in ("nohup python train.py > log &", "setsid python t.py", "python t.py &",
                "python t.py & echo started", "tmux new -d 'python t.py'"):
        assert _DETACH.search(cmd), cmd
    for cmd in ("make && make test", "python t.py 2>&1 | tail", "echo a && echo b",
                "cmd &> log", "cmd |& tee log"):
        assert not _DETACH.search(cmd), cmd


def test_lab_check_refuses_granting_start_run(tmp_path):
    import yaml
    d = yaml.safe_load(EXAMPLE.read_text())
    d["agents"]["worker-a"]["grants"].append({"tool": "mcp__lab__start_run"})
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(d))
    errs = [str(i) for i in registry.check(registry.load(p)) if i.level == "error"]
    assert any("approved one at a time" in e for e in errs)


def test_runs_keep_the_machine_busy():
    from agentic_lab.secretary import power as P
    seen = {"up": True, "queue": {"waiting": 1}}
    assert P.worker_busy("w", seen) == "w has 1 waiting"
    seen = {"up": True, "queue": {}, "runs": [{"id": "r-1", "name": "eval-cpu"}]}
    assert P.worker_busy("w", seen) == "w has 1 long run(s) going (eval-cpu)"


def test_apply_installs_the_run_units_and_sudo_rule():
    from agentic_lab.apply import host_plan, script

    class Tok:
        def get(self, k):
            return "t"
    lab = registry.load(EXAMPLE)
    plan = host_plan(lab, "gpu-a", Tok(), lambda owner, name: "secret")
    assert plan["run_units"] == {"worker-a": "lab-worker-a"}
    s = script(plan)
    assert "/etc/systemd/system/lab-run-worker-a@.service" in s
    assert "User=lab-worker-a" in s and "NoNewPrivileges=yes" in s
    assert "lab-worker-a ALL=(root) NOPASSWD: /usr/local/libexec/agentic-lab/lab-run-ctl" in s
    assert "visudo -cqf /etc/sudoers.d/agentic-lab-runs.new" in s


@pytest.mark.parametrize("args,ok", [
    (["start", "r-0123456789ab"], True), (["stop", "r-0123456789ab"], True),
    (["restart", "r-0123456789ab"], False), (["start", "r-0123456789aZ"], False),
    (["start", "r-0123456789abc"], False), (["start", "r-0123456789ab\nfoo"], False),
    (["start", "../x"], False)])
def test_run_ctl_only_starts_and_stops_own_runs(tmp_path, args, ok):
    fake = tmp_path / "ctl"
    fake.write_text(CTL.read_text().replace("exec /usr/bin/systemctl", "echo"))
    r = subprocess.run(["sh", str(fake), *args], capture_output=True, text=True,
                       env={"SUDO_USER": "lab-worker-a", "PATH": "/usr/bin:/bin"})
    assert (r.returncode == 0) == ok
    if ok:
        assert r.stdout.strip() == f"{args[0]} --no-block lab-run-worker-a@{args[1]}.service"
    r = subprocess.run(["sh", str(fake), "start", "r-0123456789ab"], capture_output=True,
                       env={"SUDO_USER": "root", "PATH": "/usr/bin:/bin"})
    assert r.returncode != 0                      # only an agent's service


def test_a_run_cant_see_the_agents_lab_secrets():
    unit = R.unit_template("worker-a", "lab-worker-a")
    assert "InaccessiblePaths=-/home/lab-worker-a/.config/agentic-lab" in unit
    assert "%h" not in unit                     # %h is root's home in a system unit


def test_warm_environments(tmp_path, monkeypatch):
    """#45: tools' caches in the working folder (agent, its commands, its runs); the prompt
    says how to reuse checkouts and envs; low disk is said before a new task."""
    from agentic_lab.policy import sandbox
    from agentic_lab.worker import runner as RN
    env = sandbox.cache_env("/w")
    assert env["TORCH_EXTENSIONS_DIR"] == "/w/.cache/torch_extensions"
    assert {"UV_CACHE_DIR", "PIP_CACHE_DIR", "TRITON_CACHE_DIR", "HF_HOME", "CUDA_CACHE_PATH",
            "XDG_CACHE_HOME"} <= set(env)
    assert sandbox.settings("/w")["env"]["HF_HOME"] == "/w/.cache/huggingface"
    tasks, runs, units = stores(tmp_path)
    r = runs.start("t-1", "env", "echo $TORCH_EXTENSIONS_DIR", requester="alice",
                   policy={"allowWrite": ["/w"], "denyRead": ["/s"]})
    R.run_exec(runs, r["id"], wrapper=NO_WRAP)
    assert f"{tmp_path}/work/.cache/torch_extensions" in R.log_tail(r["log"], 40,
                                                                    str(tmp_path / "work"))
    t = tasks.submit("x", "alice", "mgr")
    prompt = RN._system_prompt("worker-a", t, keep_free_gb=77)
    assert "ENVIRONMENTS.md" in prompt and "77 GB" in prompt and "{" not in prompt
    runner = SdkRunner("worker-a", str(tmp_path), lambda g: {}, keep_free_gb=10**6)
    assert runner._disk_note().startswith("[lab] Disk is low")
    runner.keep_free_gb = 0
    assert runner._disk_note() == ""


def test_the_worker_is_told_to_ask_again_for_each_refused_host(tmp_path):
    """A program that needs several hosts reveals them one at a time, and many hang on a
    refused one: the worker gives network commands a timeout and asks again for each new
    host, instead of finishing the task as blocked after the first approval."""
    from agentic_lab.worker import runner as RN
    t = T.TaskStore(tmp_path).submit("x", "alice", "mgr")
    prompt = " ".join(RN._system_prompt("worker-a", t).split())
    assert "give network commands a timeout" in prompt
    assert "a refusal for another host means asking again" in prompt
    assert "Only if that is denied or times out, say what you need in finish_task" in prompt


def test_keep_free_gb_is_for_workers(tmp_path):
    import yaml
    d = yaml.safe_load(EXAMPLE.read_text())
    d["agents"]["worker-a"]["keep_free_gb"] = 50
    d["agents"]["mgr-alice"]["keep_free_gb"] = 50
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(d))
    errs = [str(i) for i in registry.check(registry.load(p)) if i.level == "error"]
    assert len(errs) == 1 and "agents.mgr-alice: 'keep_free_gb' applies to workers only" in errs[0]


def test_runs_never_outlive_their_task(tmp_path):
    """#59: a run whose task ended or vanished is stopped by the loop's sweep; cancelling
    a queued task (e.g. woken at check-in) stops its runs at once."""
    from agentic_lab.worker.server import stop_orphaned_runs
    tasks, runs, units = stores(tmp_path)
    t = tasks.update(tasks.submit("train", "alice", "m").id, state=T.RUNNING)
    r = runs.start(t.id, "train", "sleep 100")
    tasks.update(t.id, state=T.WAITING, waiting={"runs": [r["id"]]})
    tasks.update(t.id, state=T.QUEUED, wake="check-in time")     # woken, not picked up yet
    assert stop_orphaned_runs(tasks, runs) == []                # its task is still alive
    tasks.request_cancel(t.id)                                  # cancelled while queued
    assert tasks.get(t.id).state == T.CANCELLED
    assert [x["id"] for x in stop_orphaned_runs(tasks, runs)] == [r["id"]]
    assert ("stop", "worker-a", r["id"]) in units.calls
    other = runs.start("t-gone", "x", "sleep 1")                # its task is unknown
    assert [x["id"] for x in stop_orphaned_runs(tasks, runs)] == [other["id"]]


def test_a_run_ending_between_the_reads_is_not_lost(tmp_path):
    _, runs, units = stores(tmp_path)
    r = runs.start("t-1", "train", "true")
    runs._write(runs._path(r["id"], ".status"), {"state": "running"})

    def ends_now(verb, agent, run_id):
        # systemd says inactive because the run just ended and wrote its status
        runs._write(runs._path(run_id, ".status"), {"state": "exited", "exit_code": 0})
        return False
    runs.systemctl = ends_now
    v = runs.view(r["id"])
    assert v["state"] == "exited" and v["exit_code"] == 0


def test_ended_runs_arent_asked_about(tmp_path):
    asked = []

    def systemctl(verb, agent, run_id):
        asked.append(run_id)
        return False
    runs = R.RunStore(str(tmp_path / "state"), str(tmp_path / "work"), "worker-a",
                      systemctl=systemctl)
    (tmp_path / "work").mkdir(exist_ok=True)
    r = runs.start("t-1", "x", "true")
    runs._write(runs._path(r["id"], ".status"), {"state": "exited", "exit_code": 0})
    asked.clear()
    assert runs.view(r["id"])["state"] == "exited" and asked == []
