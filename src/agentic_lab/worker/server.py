"""The worker service: the MCP API managers call, and the loop that runs tasks (DESIGN.md §5).

One process, run as the agent's own Unix user by a system service. It:
  - serves MCP (streamable HTTP) on the worker's tailnet address. Every call carries a
    manager's bearer token; the token, not anything the caller says, decides who is
    calling. A dedicated worker accepts only its own human's manager.
  - runs queued tasks one at a time, first come, first served, through an AgentRunner;
    a task waiting on its long runs (runs.py) is queued again when one ends or at its
    check-in time, and resumes its session.
"""
import asyncio
import base64
import hmac
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from . import runs as R
from . import tasks as T

log = logging.getLogger("agentic_lab.worker")
IDLE_POLL_S = 1.0
WAKE_CHECK_S = 15.0         # how often waiting tasks are checked against their runs
RUN_TAIL = 20               # log lines of each run in task_result
MAX_FILE_BYTES = 20 * 1024 * 1024       # read_file: enough for plots, reports, logs
STOP_HOLD_MAX_S = 600       # the longest the secretary may hold new tasks off for a stop


@dataclass(frozen=True)
class Caller:
    manager: str
    human: str       # the human that manager belongs to
    readonly: bool = False      # the secretary: status only (and the hold of an idle stop)


class AccessError(PermissionError):
    pass


class Access:
    """Who may call this worker: bearer token -> manager. `allowed_humans` is None for a
    shared worker, or the set of humans whose managers may use a dedicated one."""

    def __init__(self, tokens: dict[str, Caller], allowed_humans=None):
        self._tokens = dict(tokens)
        self.allowed_humans = allowed_humans

    def caller(self, headers) -> Caller:
        auth = (headers or {}).get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AccessError("missing bearer token")
        for known, who in self._tokens.items():
            if hmac.compare_digest(known.encode(), token.encode()):
                if (self.allowed_humans is not None and not who.readonly
                        and who.human not in self.allowed_humans):
                    raise AccessError(f"this worker is dedicated; {who.manager} may not use it")
                return who
        raise AccessError("unknown token")


def _requester(store, task_id):
    try:
        return store.get(task_id).requester
    except Exception:
        return None


def read_workdir_file(workdir, path):
    """A plain file in the worker's working folder (never outside it, never through a
    link: safefs), as base64."""
    from .. import safefs
    root = os.path.abspath(workdir)
    full = os.path.normpath(path if os.path.isabs(path) else os.path.join(root, path))
    try:
        size = safefs.size(root, full)
        if size > MAX_FILE_BYTES:
            raise ToolError(f"{path} is {size // 1_000_000} MB; the limit is "
                            f"{MAX_FILE_BYTES // 1_000_000} MB")
        data = safefs.read(root, full, limit=MAX_FILE_BYTES)
    except FileNotFoundError:
        raise ToolError(f"no such file: {path}") from None
    except OSError:
        raise ToolError("only plain files in the worker's working folder can be read") from None
    return {"path": full, "name": os.path.basename(full), "size": len(data),
            "base64": base64.b64encode(data).decode()}


def write_inbox_file(workdir, name, b64):
    """Store a file a manager sends (e.g. a member's attachment) in the worker's
    `inbox/`, inside its working folder. Returns its path."""
    name = os.path.basename(name or "")
    if not name or name in (".", ".."):
        raise ToolError("a file name is needed")
    try:
        data = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError):
        raise ToolError("the file must be base64") from None
    if len(data) > MAX_FILE_BYTES:
        raise ToolError(f"the limit is {MAX_FILE_BYTES // 1_000_000} MB")
    from .. import safefs
    try:
        path = safefs.write(os.path.abspath(workdir), os.path.join("inbox", name), data)
    except OSError as e:
        raise ToolError(f"couldn't store it: {e}") from None
    return {"path": path, "size": len(data)}


def build_mcp(agent_id, store: T.TaskStore, access: Access, guardrails, health=None,
              usage=None, workdir=None, events=None, check=None, runs=None, github=None):
    """The worker's MCP server. `guardrails()` returns its current settings and grants;
    `check()` probes the seat's limits now; `runs`: its RunStore (long runs)."""
    mcp = MCPServer(name=f"agentic-lab worker {agent_id}",
                    instructions="Submit tasks to this worker and follow them.")

    def who(ctx: Context, writes=False) -> Caller:
        try:
            c = access.caller(ctx.headers)
        except AccessError as e:
            raise ToolError(f"access denied: {e}") from None
        if writes and c.readonly:
            raise ToolError("access denied: this token is for monitoring only")
        return c

    # an idle stop under way: no new tasks until then (time.monotonic()), so none is
    # accepted between the secretary's last look at the queue and the machine going off
    stopping = {"until": 0.0}
    submitting = threading.Lock()       # a submit and the start of a hold never interleave

    @mcp.tool(description="Queue a task (first come, first served). `requester` is the "
              "human who asked for it. Returns the task.")
    def submit_task(instruction: str, requester: str, ctx: Context,
                    project: str | None = None) -> dict:
        c = who(ctx, writes=True)
        with submitting:
            if time.monotonic() < stopping["until"]:
                raise ToolError(f"{agent_id}'s machine is being stopped (idle): no new tasks "
                                "now. Ask the secretary to power it on again, then submit "
                                "the task again.")
            try:
                return store.submit(instruction, requester=requester, manager=c.manager,
                                    project=project).view()
            except T.TaskError as e:
                raise ToolError(str(e)) from None

    def task(task_id):
        try:
            return store.get(task_id)
        except T.TaskError as e:
            raise ToolError(str(e)) from None

    def with_runs(view, tail=0):
        if runs is not None:
            rs = runs.all(view["id"])
            if rs:
                view["runs"] = [runs.view(r["id"], tail=tail) for r in rs]
        return view

    @mcp.tool(description="A task's state, its latest progress line and timestamps. A task "
              "`waiting` is in progress: parked while its long runs go on (`waiting.note` "
              "says on what), and resumed when one ends; `runs` lists them.")
    def task_status(task_id: str, ctx: Context) -> dict:
        who(ctx)
        return with_runs(task(task_id).view(with_progress=False))

    @mcp.tool(description="A task in full: all progress lines, its long runs with the tail "
              "of their logs, and, when finished, the result.")
    def task_result(task_id: str, ctx: Context) -> dict:
        who(ctx)
        return with_runs(task(task_id).view(), tail=RUN_TAIL)

    @mcp.tool(description="Cancel a task: a queued task at once, a running one as soon as "
              "the agent can stop, a waiting one with its runs (stopped).")
    def cancel_task(task_id: str, ctx: Context) -> dict:
        who(ctx, writes=True)
        task(task_id)
        t = store.request_cancel(task_id)
        if t.state in T.TERMINAL:           # cancelled at once (queued): its runs too (#59)
            stop_runs(runs, task_id)
        return t.view(with_progress=False)

    @mcp.tool(description="All tasks on this worker, oldest first. Optionally only one state.")
    def list_tasks(ctx: Context, state: str | None = None) -> list[dict]:
        who(ctx)
        if state is not None and state not in T.STATES:
            raise ToolError(f"state must be one of {T.STATES}")
        return [t.view(with_progress=False) for t in store.all()
                if state is None or t.state == state]

    @mcp.tool(description="This worker's guardrails: its sandbox settings and grants.")
    async def get_guardrails(ctx: Context) -> dict:
        who(ctx)
        g = guardrails()
        return await g if asyncio.iscoroutine(g) else g

    @mcp.tool(description="A file from this worker's working folder (e.g. a task's artifact: "
              "a plot, a report), as base64. Up to 20 MB.")
    def read_file(path: str, ctx: Context) -> dict:
        who(ctx)
        if workdir is None:
            raise ToolError("this worker doesn't serve files")
        return read_workdir_file(workdir, path)

    @mcp.tool(description="Send this worker a file (e.g. a member's attachment), base64, up to "
              "20 MB. It lands in the worker's inbox/ folder; returns its path there.")
    def write_file(name: str, base64_data: str, ctx: Context) -> dict:
        who(ctx, writes=True)
        if workdir is None:
            raise ToolError("this worker doesn't accept files")
        return write_inbox_file(workdir, name, base64_data)

    @mcp.tool(description="This worker's own event log (messages, tool calls, results, "
              "approvals, task events) for the last `hours`, newest last. For the admin's "
              "log viewer.")
    def recent_events(ctx: Context, hours: float = 24, limit: int = 500) -> dict:
        # the log is for the admins' log viewer only: the secretary's monitoring token,
        # never a manager's
        if not who(ctx).readonly:
            raise ToolError("access denied: the event log is for the secretary only")
        import datetime as _dt
        if events is None:
            return {"events": []}
        return {"events": events.read(since=_dt.timedelta(hours=hours), limit=min(limit, 2000))}

    @mcp.tool(description="Liveness and state of this worker: queue, GPU, disk, versions.")
    async def health_check(ctx: Context) -> dict:
        who(ctx)
        return await health_now()

    @mcp.tool(description="For the secretary's idle stop: hold new tasks off for `seconds` "
              "(0 ends a hold), then report health as health_check does. Every task "
              "submitted before the hold is in the report; any after it is refused.")
    async def hold_for_stop(ctx: Context, seconds: float = 300) -> dict:
        if not who(ctx).readonly:
            raise ToolError("access denied: only the secretary stops machines")
        with submitting:
            stopping["until"] = time.monotonic() + max(0.0, min(seconds, STOP_HOLD_MAX_S))
        return await health_now()

    async def health_now():
        # nvidia-smi can take seconds: not in the event loop serving the other calls
        h = await asyncio.to_thread(health or default_health, agent_id, store)
        if github is not None:
            h["github"] = github()          # identities that don't work right now (#64)
        from ..configrepo import applied_versions
        h["applied"] = applied_versions()   # what this host runs: missed deploys (#61)
        if runs is not None:
            h["runs"] = [{**{k: r.get(k) for k in ("id", "name", "task", "state", "started")},
                          "requester": _requester(store, r.get("task"))}
                         for r in await asyncio.to_thread(runs.active)]
        if usage is not None:
            h["usage"] = usage.load()
        return h

    @mcp.tool(description="Read this worker's Claude seat limits now (one tiny request); "
              "returns the readings. For the web UI's 'check now'.")
    async def check_usage(ctx: Context) -> dict:
        who(ctx)
        if check is None:
            raise ToolError("this worker has no seat to check")
        try:
            return {"limits": await check()}
        except Exception as e:
            raise ToolError(f"usage check failed: {e}") from None

    return mcp


def http_app(mcp: MCPServer, access: Access, allowed_hosts=None):
    """The MCP endpoint behind a bearer-token check: an unknown token gets 401 before any
    MCP handling. (Each tool checks the token again and reads the caller from it.)

    `allowed_hosts` ("name:port" entries) are the names managers use to reach this worker:
    requests with any other Host header are refused (DNS-rebinding protection)."""
    security = None
    if allowed_hosts:
        security = TransportSecuritySettings(allowed_hosts=list(allowed_hosts),
                                             allowed_origins=[])
    app = mcp.streamable_http_app(transport_security=security)

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            try:
                access.caller(headers)
            except AccessError as e:
                body = f"access denied: {e}".encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await app(scope, receive, send)

    return guarded, app


def default_health(agent_id, store):
    counts, last = {}, None
    for t in store.all():
        counts[t.state] = counts.get(t.state, 0) + 1
        last = max([x for x in (last, t.started, t.finished) if x], default=None)
    gpu, gpus, procs = None, None, None
    if shutil.which("nvidia-smi"):
        try:
            gpu = _smi("--query-gpu=name,memory.used,memory.total,utilization.gpu") or None
            gpus = [{"name": r[0], "mem_used_mb": _num(r[1]), "mem_total_mb": _num(r[2]),
                     "util": _num(r[3])} for r in _rows(gpu or "")]
            # every process on the GPUs, the lab's or anyone's: a machine with one is busy
            procs = [{"pid": _num(r[0]), "name": os.path.basename(r[1]), "mem_mb": _num(r[2])}
                     for r in _rows(_smi("--query-compute-apps=pid,process_name,used_memory"))]
        except (OSError, subprocess.SubprocessError):
            gpu = "nvidia-smi failed"
    disk = shutil.disk_usage(os.path.expanduser("~"))
    return {"agent": agent_id, "host": platform.node(), "tasks": counts, "gpu": gpu,
            "gpus": gpus, "gpu_processes": procs, "disk_free_gb": round(disk.free / 1e9, 1),
            "keep_awake": keep_awake(),
            # when its latest task started or ended: the secretary checks a worker's queue
            # only every few minutes, and a short task between two looks still counts
            "activity": {"last_turn_at": last}}


KEEP_AWAKE = "/var/lib/agentic-lab/keep-awake"


def keep_awake(path=KEEP_AWAKE):
    """People keeping this machine awake (their ~/.lab-keep-awake, listed every minute by
    the root lab-keep-awake.timer, since homes are private): [{user, note}]."""
    try:
        with open(path) as f:
            rows = [line.rstrip("\n").split(" ", 1) for line in f if line.strip()]
    except OSError:
        return []
    return [{"user": r[0], "note": r[1].strip() if len(r) > 1 else ""} for r in rows]


def _smi(query):
    return subprocess.run(["nvidia-smi", query, "--format=csv,noheader,nounits"],
                          capture_output=True, text=True, timeout=10).stdout.strip()


def _rows(csv_text):
    return [[c.strip() for c in line.split(",")] for line in csv_text.splitlines() if line.strip()]


def _num(s):
    try:
        return int(float(s))
    except ValueError:
        return None


def stop_orphaned_runs(store: T.TaskStore, runs):
    """Runs whose task is over (done, failed, cancelled) or unknown: stopped, so nothing
    runs for nobody and keeps the machine from being idle-stopped (#59). Returns them."""
    if runs is None:
        return []
    out = []
    for r in runs.active():
        try:
            over = store.get(r["task"]).state in T.TERMINAL
        except T.TaskError:
            over = True
        if over:
            log.warning("run %s outlived its task %s: stopping it", r["id"], r["task"])
            try:
                runs.stop(r["id"])
                out.append(r)
            except (R.RunError, OSError, subprocess.SubprocessError) as e:
                log.warning("couldn't stop run %s: %s", r["id"], e)
    return out


def wake_waiting(store: T.TaskStore, runs, now=None):
    """Waiting tasks: queue again those whose run ended or whose check-in time came;
    cancel (stopping their runs) those a manager cancelled. Returns the tasks woken."""
    import datetime as dt
    now = now or dt.datetime.now(dt.timezone.utc)
    woken = []
    for t in store.all():
        if t.state != T.WAITING:
            continue
        if t.cancel_requested:
            stop_runs(runs, t.id)
            store.update(t.id, state=T.CANCELLED)
            continue
        w = t.waiting or {}
        ended = []
        for rid in w.get("runs") or []:
            try:
                st = runs.view(rid)["state"] if runs else "lost"
            except R.RunError:
                st = "lost"
            if st not in R.ACTIVE:
                ended.append(f"{rid} {st}")
        if ended:
            reason = "run " + ", ".join(ended)
        elif w.get("wake_at") and now >= dt.datetime.fromisoformat(w["wake_at"]):
            reason = "check-in time (its runs are still going)"
        else:
            continue
        woken.append(store.update(t.id, state=T.QUEUED, wake=reason))
    return woken


def stop_runs(runs, task_id):
    for r in (runs.active(task_id) if runs else []):
        try:
            runs.stop(r["id"])
        except (R.RunError, OSError, subprocess.SubprocessError) as e:
            log.warning("couldn't stop run %s: %s", r["id"], e)


async def run_tasks(store: T.TaskStore, runner, stop: asyncio.Event, runs=None):
    """The task loop: recover after a restart, then run queued tasks one at a time, and
    wake waiting tasks when their runs need them."""
    approver = getattr(runner, "approver", None)
    for t in store.recover():
        log.info("requeued %s after restart", t.id)
        if t.pending_requests:              # its old approval requests: no longer waited for
            store.update(t.id, stale_requests=t.stale_requests + t.pending_requests,
                         pending_requests=[])
    # waking parked tasks, stopping orphaned runs, withdrawing old requests: on their own,
    # so they go on while a task runs (#59), and never hold up the start (#58)
    keeper = asyncio.create_task(housekeeping(store, runs, approver, stop))
    try:
        await _task_loop(store, runner, stop, runs)
    finally:
        keeper.cancel()


async def withdraw_stale(store, approver):
    """Withdraw the requests of tasks requeued after a crash; keep those the broker
    couldn't be asked about, for the next round."""
    for t in store.all():
        left = []
        for rid in t.stale_requests:
            try:
                ok = await approver.withdraw(rid) if approver is not None else True
            except Exception:
                ok = False
            if ok is False:
                left.append(rid)
        if left != t.stale_requests:
            store.update(t.id, stale_requests=left)


async def housekeeping(store, runs, approver, stop):
    while not stop.is_set():
        try:
            for t in await asyncio.to_thread(wake_waiting, store, runs):
                log.info("woke %s: %s", t.id, t.wake)
            await asyncio.to_thread(stop_orphaned_runs, store, runs)
            await withdraw_stale(store, approver)
        except Exception:
            log.exception("checking waiting tasks failed")
        try:
            await asyncio.wait_for(stop.wait(), WAKE_CHECK_S)
        except asyncio.TimeoutError:
            pass


async def _task_loop(store, runner, stop, runs=None):
    while not stop.is_set():
        try:
            await _one_task(store, runner, stop, runs)
        except Exception:           # nothing after a task may stop the loop: it would stop
            log.exception("the task loop failed; going on")     # every task here for good
            try:
                await asyncio.wait_for(stop.wait(), IDLE_POLL_S)
            except asyncio.TimeoutError:
                pass


async def _one_task(store, runner, stop, runs):
    """Run the oldest queued task, or wait a little if there's none."""
    task = store.next_queued()
    if task is None:
        try:
            await asyncio.wait_for(stop.wait(), IDLE_POLL_S)
        except asyncio.TimeoutError:
            pass
        return
    log.info("running %s for %s via %s", task.id, task.requester, task.manager)
    task = store.update(task.id, state=T.RUNNING)
    try:
        await runner.run(task, store)
    except Exception as e:      # a runner bug must not stop the worker
        log.exception("runner failed on %s", task.id)
        if store.get(task.id).state not in T.TERMINAL:
            store.update(task.id, state=T.FAILED, error=f"{type(e).__name__}: {e}")
    if store.get(task.id).state == T.CANCELLED:     # a cancelled task's runs stop too (#59)
        await asyncio.to_thread(stop_runs, runs, task.id)
    log.info("%s -> %s", task.id, store.get(task.id).state)
