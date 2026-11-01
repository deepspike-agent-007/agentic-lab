"""Runs one task through Claude (DESIGN.md §10).

`SdkRunner` drives Claude Code through the Agent SDK, headless:
  - the agent's settings come from `lab.yaml` (the sandbox model) plus its active grants;
  - no user/project settings are loaded (a repo's own `.claude/` can't add hooks);
  - the agent reports through two lab tools, `report_progress` and `finish_task`;
  - anything that needs a human reaches `can_use_tool`, which asks the approver (the
    broker, DESIGN.md §6.3). What the sandbox refused just before is sent along, so the
    human can be offered the right grant.
  - a grant of a tool applies at once; a grant that widens the sandbox (a domain, a
    writable folder) restarts the session with the new settings, and the agent resumes
    where it was (a running session doesn't pick up new sandbox settings).
  - a task's session id is saved, so after a restart the task resumes its session.
  - long jobs are runs (runs.py): `start_run` always goes to a human (a run is outside the
    sandbox and outlives the session); `wait_for_runs` parks the task, and the session
    resumes when a run ends or at the agent's check-in time.

`AgentRunner` is the interface the worker service uses, so a different runner (or a fake,
in tests) can stand in.
"""
import asyncio
import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              HookMatcher, PermissionResultAllow, PermissionResultDeny, RateLimitEvent,
                              ResultMessage, SystemMessage, TextBlock, ToolResultBlock,
                              ToolUseBlock, UserMessage, create_sdk_mcp_server, tool)

from ..eventlog import tool_summary

from ..policy.confine import approved_input
from ..policy.grants import Grant, grantable_tool
from . import runs as R
from . import tasks as T

CANCEL_POLL_S = 2.0
MAX_SESSION_RESTARTS = 8        # grants applied within one task

LAB_TOOLS = ("mcp__lab__report_progress", "mcp__lab__finish_task", "mcp__lab__run_status",
             "mcp__lab__stop_run", "mcp__lab__wait_for_runs")
# a human approves each one, whatever grants say: a run outlives the session and its
# network isn't limited
ALWAYS_ASK = ("mcp__lab__start_run",)
KEEP_FREE_GB = 30          # a worker prunes its caches when free disk falls below this
RUN_LOG_LINES = 30          # log lines a resumed session sees per run
# commands that try to outlive themselves: in the sandbox they can't (runs.py)
_DETACH = re.compile(r"\bnohup\b|\bsetsid\b|\bdisown\b|\bscreen\s+-d|\btmux\s+new|"
                     r"(?<![&>|<])&(?![&>|])")      # a lone & (not &&, 2>&1, &>, |&)
DETACH_HINT = ("Note: a process left running by this command is killed when the command "
               "returns (the sandbox), and anything else you start ends with this task's "
               "session. For a long job, use start_run.")
_VIOLATIONS = re.compile(r"<sandbox_violations>(.*?)</sandbox_violations>", re.S)


def allowed_without_asking(tool_name, tool_input):
    """Calls our permission callback allows on its own.

    Claude Code sends some sandboxed Bash commands for approval anyway: `export X=...`, an
    env prefix (`FOO=bar cmd`), some `python3 -c` one-liners, loops over variables. Our
    sandbox confines every command that runs in it (verified on a worker host: writes
    outside, unlisted hosts and the systemd bus all stay refused), so a Bash command that
    stays inside the sandbox needs no human. Only one asking to run OUTSIDE it
    (`dangerouslyDisableSandbox`) does.
    """
    if tool_name in LAB_TOOLS:
        return True
    return tool_name == "Bash" and not tool_input.get("dangerouslyDisableSandbox")


def sandbox_violations(text):
    """The lines of `<sandbox_violations>` blocks in a tool result."""
    out = []
    for block in _VIOLATIONS.findall(text or ""):
        out += [ln.strip() for ln in block.splitlines() if ln.strip()]
    return out


@dataclass
class Decision:
    kind: str                     # "allow" | "grant" | "deny"
    message: str = ""
    grants: list = field(default_factory=list)   # for "grant": [{"kind", "value", ...}]


class Approver(Protocol):
    async def ask(self, task: T.Task, tool_name: str, tool_input: dict,
                  violations: list, stop=None) -> Decision: ...

    async def active_grants(self) -> list[dict]: ...


class NoChannelApprover:
    """No approval channel: every request outside the sandbox is denied, with guidance."""

    async def ask(self, task, tool_name, tool_input, violations, stop=None):
        return Decision("deny", (
            "No human approval channel is connected, so actions outside the sandbox are "
            "denied. Find a way to do this inside the sandbox (your working folder, /tmp, "
            "allowed hosts), or report in finish_task what you would need."))

    async def active_grants(self):
        return []

    async def withdraw(self, rid):
        pass


class AgentRunner(Protocol):
    async def run(self, task: T.Task, store: T.TaskStore) -> None:
        """Run `task` to a terminal state, recording progress and the result in `store`."""


def _system_prompt(agent_id, task, keep_free_gb=KEEP_FREE_GB):
    return f"""
You are {agent_id}, a worker agent in an agentic lab: a team of humans and AI agents.
This task comes from the human {task.requester}, through the manager agent {task.manager}.

How you work here:
- Your Bash commands run in a sandbox: no internet except allowed hosts, the lab's tailnet,
  writes only in your working folder and /tmp. Inside it you never need to ask.
- If the sandbox refuses something, the error says why. Prefer a way inside the sandbox.
  Running something outside it, or writing elsewhere, needs a human's permission: the lab
  asks the human (in Slack), and it may be denied or time out. If such an action runs,
  a human approved it. A human may also widen your sandbox for a while instead.
- Your sandbox and permissions are managed by the lab, not by you: don't suggest
  /sandbox, settings files or permission rules. To get more access, ask through the
  lab: retry outside the sandbox. Only if that is denied or times out, say what you need
  in finish_task.
- Many programs (uploaders, SDKs, installers) retry a refused connection forever instead
  of failing: give network commands a timeout (`timeout 120 ...`), so a refusal shows up
  in minutes. A program may need several hosts and reveal them one at a time: a widened
  sandbox covers only the hosts seen so far, so a refusal for another host means asking
  again (retry outside the sandbox), not giving up.
- Report progress with the report_progress tool at meaningful steps (a few per task, not
  every command).
- When the task is done (or can't be done), call finish_task exactly once: a summary for
  the human, and the artifacts (file paths, commit ids, URLs) worth keeping.
- Files for the human (plots, reports, results to look at) go in tasks/{task.id}/ in your
  working folder: on a shared worker those are the only files their manager can show them.

Long jobs (training, big evaluations, anything over ~10 minutes):
- Start them with start_run, never with nohup, &, setsid, screen or tmux: whatever you start
  in a command is killed when the command returns, and everything else when this task's
  session ends. A run goes on by itself (worker restarts don't stop it), outside the
  sandbox, so a human approves each run: give it a clear name and a command that says what
  it does (e.g. `python train.py --config big.yaml`).
- First try a short version inside the sandbox (a few steps), then start_run the real one,
  report_progress what you started, and call wait_for_runs: your session ends and the task
  waits (it stays in progress). You're woken when one of its runs ends, or at the check-in
  time you set (e.g. every 120 minutes, to look at the loss). Its output goes to a log in
  your working folder; run_status shows its tail.
- While a task waits, you may get other tasks. Before using the GPU, check run_status:
  don't start GPU work that would compete with a running run unless the task says so.
- finish_task only once the task's runs are over (stop_run the ones you don't need).
- A run has the same rules as your commands here (it reads what you read, writes only
  your working folder, /tmp and granted folders, sees only its own processes), with its
  own /tmp and no network limit. Caches are shared with your commands. A stop gives the
  job a TERM and a minute to save.
- The cache variables set for you are defaults: a run's command can export its own, and a
  tool not covered should cache in work/.cache/<tool> ($XDG_CACHE_HOME/<tool>), so runs
  and commands share it.

Warm environments: setting up a project often takes longer than the experiment, so keep
what you build and reuse it.
- Tools' caches (uv, pip, torch extensions, Triton, Hugging Face, CUDA) already go to
  .cache/ in your working folder and stay between tasks.
- Checkouts in repos/<owner>/<repo> (reuse: git fetch and check out, don't clone again);
  virtual environments in envs/<name>.
- ENVIRONMENTS.md in your working folder lists them: for each, the repo, the commit or lock
  file it was built from, and how to use it. Read it first, keep it up to date. Reuse
  before rebuilding; rebuild an environment only if its lock file changed.
- Keep at least {keep_free_gb} GB free on disk: below that, before installing anything
  big, prune caches and old environments (least recently used first), and say what you
  removed in the task's result. Never delete anything else.
"""


def _grant_line(g):
    what = {"domain": "access to {}", "write": "writes to {}", "tool": "the {} tool"}
    until = g.get("expires", "")[:16].replace("T", " ")
    return what.get(g["kind"], "{}").format(g["value"]) + (f" until {until} UTC" if until else "")


class SdkRunner:
    def __init__(self, agent_id, workdir, settings_for: Callable[[list], dict],
                 approver: Approver = None, model=None, env=None, cli_path=None, usage=None,
                 events=None, prompt_extra="", runs=None, keep_free_gb=KEEP_FREE_GB):
        """`settings_for(grants)` returns the agent's settings given its active Grants;
        `prompt_extra` is added to its system prompt (e.g. its GitHub identity); `runs`: the
        worker's RunStore (long runs; None: no run tools)."""
        self.agent_id = agent_id
        self.workdir = workdir
        self.settings_for = settings_for
        self.approver = approver or NoChannelApprover()
        self.model = model
        self.env = env or {}
        self.cli_path = cli_path
        self.usage = usage          # a UsageLog, or None
        self.events = events        # an EventLog, or None
        self.prompt_extra = prompt_extra
        self.runs = runs
        self.keep_free_gb = keep_free_gb

    def _log(self, kind, text="", **meta):
        if self.events is not None:
            self.events.add(kind, text, **meta)

    def _lab_server(self, task, store, outcome, fs=None):
        @tool("report_progress", "Report progress on the current task to the humans "
              "following it.", {"message": str})
        async def report_progress(args):
            store.add_progress(task.id, args["message"])
            return {"content": [{"type": "text", "text": "reported"}]}

        @tool("finish_task", "Finish the current task: a summary for the human, and the "
              "artifacts worth keeping (paths, commits, run URLs).",
              {"type": "object",
               "properties": {"summary": {"type": "string"},
                              "artifacts": {"type": "array", "items": {"type": "string"}},
                              "success": {"type": "boolean"}},
               "required": ["summary"]})
        async def finish_task(args):
            busy = self.runs.active(task.id) if self.runs else []
            if busy:
                return _text("Not finished: this task's runs are still going ("
                             + ", ".join(f"{r['id']} {r['name']}" for r in busy)
                             + "). wait_for_runs, or stop_run the ones you don't need.", True)
            outcome["result"] = {"summary": args["summary"],
                                 "artifacts": list(args.get("artifacts") or [])}
            outcome["success"] = bool(args.get("success", True))
            return {"content": [{"type": "text", "text": "task finished; stop here"}]}

        tools = [report_progress, finish_task]
        if self.runs is not None:
            tools += self._run_tools(task, store, outcome, fs)
        return create_sdk_mcp_server("lab", tools=tools)

    def _run_tools(self, task, store, outcome, fs=None):
        runs = self.runs

        @tool("start_run", "Start a long job (training, a big evaluation...) as a run: it goes "
              "on by itself after this command and after this task's session, until it ends "
              "or is stopped. It has the same file rules as your commands here (writes only "
              "your working folder, /tmp and your write grants, a timed one into your home "
              "excepted; the lab's folders hidden), but its network isn't limited and it "
              "outlives your session, so a human approves each run. Its output (stdout and "
              "stderr) goes to a log in your working folder. "
              "`cwd`: a folder in your working folder (default: the working folder).",
              {"type": "object",
               "properties": {"name": {"type": "string"}, "command": {"type": "string"},
                              "cwd": {"type": "string"}},
               "required": ["name", "command"]})
        async def start_run(args):
            try:
                # the run is held to the session's own sandbox policy (same rules, longer)
                r = runs.start(task.id, args.get("name"), args["command"], args.get("cwd"),
                               requester=task.requester, policy=fs)
            except R.RunError as e:
                return _text(str(e), True)
            store.add_progress(task.id, f"started run {r['id']} ({r['name']}): {r['command']}")
            self._log("run", f"started {r['id']} {r['name']}: {r['command']}", task=task.id)
            return _text(json.dumps(r) + "\nNext: report_progress, then wait_for_runs.")

        @tool("run_status", "Your runs (all of this worker's, any task): state (starting, "
              "running, exited with its exit code, failed, stopped, lost), started/ended, log "
              "path. With `run_id`: that run, with the last `tail_lines` lines of its log.",
              {"type": "object",
               "properties": {"run_id": {"type": "string"}, "tail_lines": {"type": "integer"}}})
        async def run_status(args):
            try:
                if args.get("run_id"):
                    return _text(json.dumps(runs.view(args["run_id"],
                                                      tail=min(args.get("tail_lines") or 40, 400))))
                return _text(json.dumps(runs.all()[-30:]))
            except R.RunError as e:
                return _text(str(e), True)

        @tool("stop_run", "Stop one of this task's runs.", {"run_id": str})
        async def stop_run(args):
            try:
                r = runs.get(args["run_id"])
                if r["task"] != task.id:
                    return _text(f"{args['run_id']} belongs to another task", True)
                r = runs.stop(args["run_id"])
            except (R.RunError, OSError, ValueError) as e:
                return _text(f"couldn't stop it: {e}", True)
            self._log("run", f"stopped {r['id']}", task=task.id)
            return _text(json.dumps(r))

        @tool("wait_for_runs", "Park this task until one of its runs ends, or until "
              "`check_in_minutes` pass (0: only when a run ends). The task stays in progress; "
              "your session ends now and resumes then, with the runs' state and log tails. "
              "`note`: what you're waiting for, for the humans following the task.",
              {"type": "object",
               "properties": {"note": {"type": "string"},
                              "check_in_minutes": {"type": "integer"}},
               "required": ["note"]})
        async def wait_for_runs(args):
            active = runs.active(task.id)
            if not active:
                return _text("This task has no run going: nothing to wait for.", True)
            mins = max(0, int(args.get("check_in_minutes") or 0))
            wake_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=max(mins, 5))
                       ).isoformat(timespec="seconds") if mins else None
            outcome["wait"] = {"note": args["note"], "runs": [r["id"] for r in active],
                               "since": T._now(), "wake_at": wake_at}
            return _text("The task waits now. End your turn here (no more tool calls).")

        return [start_run, run_status, stop_run, wait_for_runs]

    async def _grants_for(self, task):
        """Active grants that apply to this task (task-bound grants of other tasks don't)."""
        try:
            gs = await self.approver.active_grants()
        except Exception:
            gs = []
        return [g for g in gs if g.get("task_id") in (None, task.id)]

    def _options(self, task, store, outcome, session, grants):
        tool_grants = {g["value"]: g.get("expires") for g in grants if g["kind"] == "tool"}

        def granted(tool_name, tool_input):
            until = tool_grants.get(tool_name, False)
            return (until is not False and tool_name not in ALWAYS_ASK
                    and grantable_tool(tool_name)
                    and not (tool_input or {}).get("dangerouslyDisableSandbox")
                    and (until is None or _now() < dt.datetime.fromisoformat(until)))

        async def can_use_tool(tool_name, tool_input, context):
            if allowed_without_asking(tool_name, tool_input) or granted(tool_name, tool_input):
                return PermissionResultAllow()
            if store.get(task.id).cancel_requested:
                return PermissionResultDeny(message="The task was cancelled.", interrupt=True)
            store.update(task.id, state=T.AWAITING)
            session["asking"] = session.get("asking", 0) + 1
            self._log("approval", "asked: " + tool_summary(tool_name, tool_input), task=task.id)
            mine = []

            def filed(rid):                 # kept on the task: withdrawn after a crash (#58)
                mine.append(rid)
                store.update(task.id, pending_requests=store.get(task.id).pending_requests + [rid])
            try:
                d = await self.approver.ask(task, tool_name, tool_input,
                                            list(session["violations"]),
                                            stop=lambda: store.get(task.id).cancel_requested,
                                            filed=filed)
            finally:
                session["asking"] -= 1
                t = store.get(task.id)
                left = [r for r in t.pending_requests if r not in mine]
                # running again only when no other approval is still pending
                if t.state == T.AWAITING and session["asking"] == 0:
                    store.update(task.id, state=T.RUNNING, pending_requests=left)
                else:
                    store.update(task.id, pending_requests=left)
            self._log("approval", f"decided: {d.kind}" + (f" ({d.message[:200]})" if d.message
                                                         else ""), task=task.id)
            if d.kind == "allow":           # as approved; started confined (policy/confine)
                return PermissionResultAllow(
                    updated_input=approved_input(tool_name, tool_input))
            if d.kind == "grant":
                if all(g["kind"] == "tool" for g in d.grants):
                    if tool_name in ALWAYS_ASK:     # approved this once, never as a grant
                        return PermissionResultAllow()
                    tool_grants.update({g["value"]: g.get("expires") for g in d.grants})
                    return PermissionResultAllow()
                session["restart_for"] = d.grants
                return PermissionResultDeny(interrupt=True, message=(
                    "Granted: " + "; ".join(_grant_line(g) for g in d.grants) +
                    ". Your session restarts now with the wider sandbox."))
            return PermissionResultDeny(message=d.message)

        settings = self.settings_for([Grant(g["kind"], g["value"],
                                            dt.datetime.fromisoformat(g["expires"]),
                                            g.get("granted_by")) for g in grants])
        return ClaudeAgentOptions(
            cwd=self.workdir,
            settings=json.dumps(settings),
            setting_sources=[],
            permission_mode="default",
            can_use_tool=can_use_tool,
            mcp_servers={"lab": self._lab_server(
                task, store, outcome, (settings.get("sandbox") or {}).get("filesystem"))},
            allowed_tools=list(LAB_TOOLS),
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": _system_prompt(self.agent_id, task, self.keep_free_gb)
                           + (self.prompt_extra() if callable(self.prompt_extra)
                              else self.prompt_extra)},
            model=self.model,
            env=self.env,
            cli_path=self.cli_path,
            resume=store.get(task.id).session_id,
            hooks={"PostToolUse": [HookMatcher(matcher="Bash", hooks=[_detach_hint])]},
        )

    async def run(self, task, store):
        self._log("task", f"started for {task.requester} via {task.manager}: {task.instruction}",
                  task=task.id)
        try:                        # its files for the member (#62), in the agent's folder
            from .. import safefs
            safefs.makedirs(self.workdir, os.path.join("tasks", task.id))
        except OSError:
            pass
        outcome = {}
        if task.wake:
            prompt = self._wake_prompt(task)
            task = store.update(task.id, wake=None, waiting=None)
        elif task.session_id is None:
            prompt = self._disk_note() + task.instruction
        else:
            prompt = ("The worker restarted while you were on this task. Continue it from where "
                      "you were; check the state of your working folder (and run_status) first.")
        result_msg, last_text = None, None
        restarts = expiries = 0
        while True:
            # grants already past their expiry here aren't used (the broker's clock may lag)
            grants = [g for g in await self._grants_for(task)
                      if not g.get("expires") or dt.datetime.fromisoformat(g["expires"]) > _now()]
            # a timed grant ends the session when it expires: the next one goes without it
            session = {"violations": [], "restart_for": None, "expired": False,
                       # tool grants are checked on each call: no restart for them
                       "narrow_at": _earliest([g for g in grants if g["kind"] != "tool"])}
            try:
                result_msg, last_text = await self._session(
                    task, store, outcome, session, grants, prompt)
            except Exception as e:   # the agent process failed: record it, keep the worker up
                err = f"{type(e).__name__}: {e}"
                if "result" in outcome or store.get(task.id).cancel_requested:
                    # it had finished (finish_task), or the cancel interrupted it: not a failure
                    self._finish(task, store, outcome, None, None)
                    return
                if self.runs and self.runs.active(task.id):     # its runs go on: wait for them
                    store.add_progress(task.id, f"session failed ({err[:200]}); waiting for "
                                       "its runs")
                    store.update(task.id, state=T.WAITING, waiting={
                        "note": "its runs (the session failed)", "since": T._now(),
                        "runs": [r["id"] for r in self.runs.active(task.id)]})
                else:
                    store.update(task.id, state=T.FAILED, error=err)
                return
            if session["restart_for"] and "result" not in outcome \
                    and not store.get(task.id).cancel_requested:
                prompt = ("Instead of running your last action outside the sandbox, a human "
                          "widened your sandbox: " +
                          "; ".join(_grant_line(g) for g in session["restart_for"]) +
                          ". (Your session was restarted to apply it; that's why the action "
                          "showed as not run.) Retry what you were doing, inside the sandbox "
                          "(without dangerouslyDisableSandbox). If it's then refused for "
                          "another host, ask again: retry outside the sandbox.")
                restarts += 1
                if restarts <= MAX_SESSION_RESTARTS:
                    continue
            elif session["expired"] and not ({"result", "wait"} & outcome.keys()) \
                    and not store.get(task.id).cancel_requested \
                    and (expiries := expiries + 1) <= MAX_SESSION_RESTARTS:
                prompt = ("A time-limited grant expired, so your session was restarted without "
                          "it (that's why your last action may show as not run). Continue the "
                          "task from where you were; check your working folder first.")
                continue
            break
        self._finish(task, store, outcome, result_msg, last_text)
        t = store.get(task.id)
        self._log("task", f"{t.state}: " + ((t.result or {}).get("summary") or t.error or ""),
                  task=task.id)

    async def _session(self, task, store, outcome, session, grants, prompt):
        last_text, result_msg = None, None
        async with ClaudeSDKClient(self._options(task, store, outcome, session, grants)) as client:
            watcher = asyncio.create_task(self._watch_cancel(task, store, client, session))
            try:
                await client.query(prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, SystemMessage) and msg.subtype == "init":
                        sid = msg.data.get("session_id")
                        if sid and sid != store.get(task.id).session_id:
                            store.update(task.id, session_id=sid)
                    elif isinstance(msg, AssistantMessage):
                        for b in msg.content:
                            if isinstance(b, TextBlock):
                                last_text = b.text
                                self._log("reply", b.text, task=task.id)
                            elif isinstance(b, ToolUseBlock):
                                self._log("tool", tool_summary(b.name, b.input), task=task.id)
                    elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                        for b in msg.content:
                            if isinstance(b, ToolResultBlock):
                                text = b.content if isinstance(b.content, str) else " ".join(
                                    x.get("text", "") for x in (b.content or [])
                                    if isinstance(x, dict))
                                self._log("result", text[:1000], task=task.id,
                                          error=bool(b.is_error) or None)
                                session["violations"] = (session["violations"] +
                                                         sandbox_violations(text))[-10:]
                    elif isinstance(msg, RateLimitEvent) and self.usage:
                        self.usage.rate_limit(msg.rate_limit_info)
                    elif isinstance(msg, ResultMessage):
                        result_msg = msg
                        if self.usage:
                            self.usage.run(task.id, msg.model_usage)
                            if msg.is_error and "limit" in (msg.result or "").lower():
                                self.usage.limit_hit(msg.result)
            finally:
                watcher.cancel()
        return result_msg, last_text

    def _disk_note(self):
        """A line before a new task when free disk is below keep_free_gb (#45)."""
        import shutil
        try:
            free = shutil.disk_usage(self.workdir).free / 1e9
        except OSError:
            return ""
        if free >= self.keep_free_gb:
            return ""
        return (f"[lab] Disk is low: {free:.0f} GB free, below the {self.keep_free_gb} GB to "
                "keep. Before installing anything, prune caches and old environments (least "
                "recently used first) and say what you removed in the result.\n\n")

    def _wake_prompt(self, task):
        lines = [f"You're back on this task: {task.wake}."]
        if task.waiting:
            lines.append(f"You were waiting for: {task.waiting.get('note')}")
        for r in (self.runs.all(task.id) if self.runs else []):
            r = self.runs.view(r["id"], tail=RUN_LOG_LINES)
            how = f"exit code {r.get('exit_code')}" if r["state"] == "exited" else r["state"]
            lines.append(f"\nRun {r['id']} ({r['name']}): {how}; log {r['log']}\n"
                         f"--- last lines ---\n{r.get('log_tail') or '(empty)'}")
        lines.append("\nCheck the results. Then finish_task, start another run, or "
                     "wait_for_runs again.")
        return "\n".join(lines)

    def _finish(self, task, store, outcome, result_msg, last_text):
        if store.get(task.id).cancel_requested:
            store.update(task.id, state=T.CANCELLED)
        elif "wait" in outcome and "result" not in outcome:     # woken by the task loop
            w = outcome["wait"]
            store.add_progress(task.id, f"waiting: {w['note']}")
            store.update(task.id, state=T.WAITING, waiting=w)
        elif "result" in outcome:
            state = T.DONE if outcome.get("success", True) else T.FAILED
            store.update(task.id, state=state, result=outcome["result"])
        elif self.runs and self.runs.active(task.id):
            # it stopped without finishing or waiting, its runs going: they decide
            store.add_progress(task.id, "waiting for its runs")
            store.update(task.id, state=T.WAITING, waiting={
                "note": "its runs", "since": T._now(),
                "runs": [r["id"] for r in self.runs.active(task.id)]})
        elif result_msg is not None and not result_msg.is_error:
            # the agent stopped without finish_task: keep its last words as the summary
            store.update(task.id, state=T.DONE,
                         result={"summary": result_msg.result or last_text or "", "artifacts": []})
        else:
            err = "; ".join(result_msg.errors or []) if result_msg else "no result"
            store.update(task.id, state=T.FAILED,
                         error=f"agent ended with an error: {err or result_msg.subtype}")

    async def _watch_cancel(self, task, store, client, session=None):
        """Interrupts the session when the task is cancelled or a timed grant expires."""
        while True:
            await asyncio.sleep(CANCEL_POLL_S)
            if store.get(task.id).cancel_requested:
                await client.interrupt()
                return
            at = (session or {}).get("narrow_at")
            if at and _now() >= at:
                session["expired"] = True
                await client.interrupt()
                return


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _earliest(grants, current=None):
    """The earliest expiry among timed grants (None: none expire)."""
    times = [dt.datetime.fromisoformat(g["expires"]) for g in grants if g.get("expires")]
    if current:
        times.append(current)
    return min(times) if times else None


def _text(text, error=False):
    return {"content": [{"type": "text", "text": text}], **({"is_error": True} if error else {})}


async def _detach_hint(hook_input, tool_use_id, context):
    """After a Bash command that tries to leave something running: say it won't last."""
    cmd = ((hook_input or {}).get("tool_input") or {}).get("command", "")
    if not _DETACH.search(cmd):
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                   "additionalContext": DETACH_HINT}}
