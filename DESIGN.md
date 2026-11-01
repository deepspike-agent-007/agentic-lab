# Agentic Lab — design

A framework that lets a team of **humans and AI agents run a scientific lab together**:
members talk to **manager** agents in Slack, managers delegate work to **worker** agents on
GPU machines, and a **secretary** agent keeps the lab running. Humans stay in control of
anything that crosses a boundary, through a permission channel that **agents cannot spoof**.

> Status (2026-10-22): implemented and on `main`; running at a first lab (secretary, managers,
> GPU workers on several VMs).
> `docs/architecture/architecture.pdf` is the illustrated reference; this file is the
> design with its reasoning and the decisions log (§13). Section numbers are stable: the
> code refers to them.

This document is deliberately free of hostnames, tokens and account ids. Those belong in a
deployment's own `lab.yaml` (in its private config repository) and secret files.

---

## 1. Goals and non-goals

**Goals**
- Three agent roles (**secretary**, **manager**, **worker**) with clear jobs and guardrails.
- **Frictionless inside, approval outside.** An agent does nearly all its work in a sandbox
  with no prompts. Anything that crosses the sandbox boundary needs a real human's yes.
- **The lab is visible, the work is private.** A shared **web UI** (§9) shows agents,
  machines, who keeps them busy, task states and owners, grants and Claude usage per
  account. Transparency replaces spend limits and quotas. What a member asked for (task
  instructions, results, files, approval details) is theirs and the admins' (§9).
- **Humans are never bypassed.** Agents relay approval requests; they never answer them, and
  they never approve or merge pull requests. **Code enforces this, not prompts.**
- **One description of the lab.** `lab.yaml` lists the humans, agents, hosts, grants, GitHub
  bots and expiring credentials. It lives in the lab's private config repository; only what
  is merged to its `main` is deployed (§11a).
- **Easy to set up and maintain.** The admin's own Claude Code guides the install; after it,
  admins administer the lab in a Slack DM with the secretary (§11a).
- **Portable and generic.** No hard-coded hosts, organisations or providers. Nebius, Slack
  and Tailscale come first. Project tools (experiment trackers, data stores...) are
  extensions a lab adds, not part of the framework.

**Non-goals (for now)**
- A general multi-tenant SaaS. One lab = one deployment = one tailnet.
- Replacing a project's own tools: code and results stay in git, experiment tracking in
  whatever the project uses.
- **Per-token (API) billing.** Every agent runs on a human member's Claude seat.
- Agents that approve each other. There is no agent-to-agent authority over boundary
  crossings.

---

## 2. Prerequisites

- A **Tailscale tailnet**: every host is on it; no lab service listens on a public interface.
- A **Slack workspace** where the admin can create apps: one app per Slack-facing agent (the
  secretary, each manager) plus one **Lab Approvals** app for the broker.
- A **Claude seat** (Team or Max) for each member whose agents will run.
- A **cloud account** (Nebius first) for GPU workers, and any VMs for managers. Ordinary
  hosts may also be AWS EC2 VMs (`provider: aws`), powered like Nebius ones (#68).
- An **always-on host** for the secretary (small, no GPU).
- A **GitHub** account or organisation for the lab's **private config repository**, and
  optionally bot accounts for agents (§4.4).

---

## 3. Topology

```
                 ┌──────────────────────── Slack ─────────────────────────┐
   members ──────┤ one app per Slack-facing agent  +  Lab Approvals app    │
   (browsers:    └────▲──────────────────────▲──────────────────────▲─────┘
    web UI,           │ (all connect out: Socket Mode)              │
    intake)  ┌────────┴──────────── SECRETARY HOST (always on) ─────┴──────┐
       ─────►│ secretary + web UI :8774   broker :8772   intake :8771       │──► Nebius API
             │ deploys (lab apply / apply-main, over SSH)                   │──► GitHub (config repo)
             └──▲───────────▲───────────────────────────────────▲───────────┘
   monitoring   │           │ permission requests (every agent) │
   (read-only)  │   ┌───────┴────────────┐                      │
                │   │ MANAGERS (any host)│                      │
                │   │ status :8773       │                      │
                │   └───────┬────────────┘                      │
                │           │ tasks, files (MCP over the tailnet)
            ┌───┴───────────▼──┐                     ┌──────────┴─────┐
            │ GPU VM: worker   │        ...          │ GPU VM: worker │
            │ MCP :8770        │                     │ MCP :8770      │
            └──────────────────┘                     └────────────────┘
       every agent → Claude (its member's seat) · agents with a bot → GitHub
```

- **Secretary host**: always on (on-prem or a small VM). Runs the secretary, the broker,
  the intake and the web UI; deploys run from here.
- **Managers**: on any host — the secretary host, a VM of their own, or a GPU VM next to
  their workers (`host:` per manager).
- **Workers**: GPU VMs, stopped once idle for `idle_stop_min` minutes (required on every cloud host; `0`: never).
- Several agents on one host each have their own Unix user (§10).

---

## 4. Roles

### 4.1 Secretary — "keeps the lab running"

| Does | Doesn't |
|---|---|
| Collect the lab's state every 15 s; serve the **web UI** (§9) and a Slack Home tab | Research or experiments (it points to the member's manager) |
| Power cloud VMs on and off; **stop idle ones** | Approve anything for anyone |
| Show Claude usage per account; warn about expiring tokens and credentials | Read the content of secrets it doesn't need |
| Answer questions about the lab in Slack (DMs, threads it's tagged in) | Power off its own host |
| Administer the lab with admins: propose changes as PRs, deploy `main` (§11a) | Merge anything |

- **Its GitHub identity** (optional, `github: <account>`): its config-repo work is then done
  as that bot account or GitHub App, over HTTPS, and it opens the pull requests itself.
  Without one, a deploy key (write access) and the admin opens the PR from its link. §4.4.
- **State**: workers over MCP (a read-only monitoring token), managers' status endpoints,
  the broker's overview, local systemd units, machine power states (Nebius through its
  CLI; AWS EC2 through boto3 with the secretary's own access key, never an instance
  role).
- **Power** (#91), on and off alike: a machine **dedicated** to a member (every agent on
  it a worker or manager with `access: dedicated`, all that member's, however many) by that
  member or an admin, **busy or not**; any other machine (a shared agent, agents of more
  than one member, the secretary, or no agents) by an admin only. The secretary's own
  actions (the idle stop) aren't a member's and aren't checked. A **busy** machine
  (a worker task running, queued, awaiting approval or waiting on its long runs; a long
  run going, even CPU-only; any process on a GPU, anyone's; a GPU
  above 0% in any of its last 5 readings, about 75 s; someone's `~/.lab-keep-awake`; a
  manager on it, if the host has `idle_stop_with_managers: false`) is powered off only with `force_off`, after the secretary has said what's busy and the member asking has confirmed. Idle hosts with workers are stopped after their `idle_stop_min` minutes (required on every cloud host, the only switch; `0` means never).
  Managers on a host don't keep it on: it's judged by its workers alone (e.g. a member's
  manager beside its only worker on an on-demand GPU VM). Stopping it only takes the manager
  offline in Slack until its member or an admin asks the secretary to power the host on. A manager's own
  work (a turn, following a task on another host) isn't measured, so a host where that
  matters sets `idle_stop_with_managers: false`: any manager there then counts as busy.
  A host that runs **only managers** is judged by their activity (#70): it's idle when no
  manager there is answering, none has had a turn (or started) within `idle_stop_min`,
  none follows an unfinished task, and none has an approval pending; then it's stopped
  like a worker host (its `idle_stop_min` above 0 opts it in). A manager that can't be
  reached, or doesn't report its activity, keeps the host busy (it goes off only with `force_off`).
- **Status in both views** (#74): the secretary computes one status per host (running,
  stopped, starting/stopping, always on for its own host; for a host without power
  control, running or unreachable from its agents) and per agent (up; "host stopped" when
  its host is powered off; "down" with the reason and since when otherwise) into the
  snapshot, and the Home tab and the web UI both show it. Every host in lab.yaml is
  listed, and the Home tab lists managers, the person's own first.
- **Keep-awake**: someone working on a machine directly (not through agents, e.g. a
  CPU-only job) creates `~/.lab-keep-awake` in their home (first line, optional: why); while
  it's there the machine is busy, and the web UI shows who keeps it awake. A root timer
  (`lab-keep-awake.timer`) lists these files every minute in `/var/lib/agentic-lab/`, since
  homes are private.
- **Notices**: daily, credentials and GitHub bot tokens expiring within 14 days; lab-wide
  notices go to an optional `announcements` channel.
- **Chat**: a Claude agent with tools over the lab's state and the power, token-request and
  admin tools; admin-only tools are checked in code.

### 4.2 Worker — "does the science"

- Runs as a **service** exposing the **worker MCP server** (§5) on its tailnet address.
- Executes tasks inside the **sandbox** (§6.1), without prompts. Anything outside it is a
  **permission request to the broker** (§6.3), for the task's requester.
- **Attached to one human for tokens** (whose seat it runs on). **Access** is separate:
  `dedicated` (only its member's managers may submit tasks) or `shared` (any manager).
- **No fixed results contract**: a result is a summary plus whatever artifacts fit (files,
  commits, links). The worker follows the project's conventions.
- **Long runs** (training for hours, big evaluations). Nothing the agent starts in its
  session outlives it: a sandboxed command's processes die when it returns (the sandbox is
  a PID namespace), background commands when the session ends (`--die-with-parent`), and
  anything under the worker service when it restarts (its cgroup). So the agent asks for a
  **run** (`start_run`): a human approves **each** run (it outlives the session and its
  network isn't limited; never granted for a while), and the worker service starts it as its own unit,
  `lab-run-<agent>@<run-id>.service`, as the agent's user, output to a log in its working
  folder. The agent then calls `wait_for_runs`: the task becomes `waiting` (in progress,
  parked) and the queue moves on, so the worker can do other tasks meanwhile. The task is
  queued again when one of its runs ends, or at a check-in time the agent chose, and its
  session resumes with the runs' state and log tails. `finish_task` waits until its runs
  are over; cancelling the task stops them. Runs survive deploys and worker restarts, not
  a powered-off machine (they show as `lost`). The worker's service starts and stops only
  its own runs, through one sudo rule for `lab-run-ctl`; a run has `NoNewPrivileges` and
  can't see the agent's lab secrets (`~/.config/agentic-lab`: its member's Claude seat
  token, lab tokens).
- **Warm environments**: setting a project up often takes longer than the experiment, so
  a worker keeps what it builds in its working folder. The service points every common
  tool's cache there (`XDG_CACHE_HOME`, `UV_CACHE_DIR`, `PIP_CACHE_DIR`,
  `TORCH_EXTENSIONS_DIR`, `TRITON_CACHE_DIR`, `HF_HOME`, `CUDA_CACHE_PATH` → `.cache/…`,
  for its session, its commands and its long runs; `~/.cache` isn't writable in the
  sandbox and `/tmp` is lost on restarts). Its prompt sets a convention: checkouts in
  `repos/<owner>/<repo>` (fetch, don't clone again), virtual environments in `envs/<name>`,
  both listed in `ENVIRONMENTS.md` (repo, commit or lock file, how to use), read first and
  kept up to date; rebuild only if the lock file changed. Below `keep_free_gb` of free
  disk (per worker, default 30) it prunes caches and old environments, least recently used
  first, and says so in the task's result; a new task is told when disk is low.
- **Watching and debugging** (the same for every agent): the agent's own **event log**
  (messages, tool calls, results, approvals, task events; for managers and the secretary,
  each turn's timing, however it ended (ok, cut off, error, cancelled): Claude Code's start-up, its
  first output, the first text the member sees, the total), shown to admins in the web UI;
  `sudo lab debug <agent>` stops its service and opens **the same session** in the classic
  Claude Code console, as the agent's user, with its sandbox; the service restarts on exit.

### 4.3 Manager — "the team member humans talk to"

- Its own **Slack identity** (Socket Mode). A DM is one conversation; in channels it joins
  only threads where it's mentioned or already involved, reading the thread so far on first
  mention (its first 5 messages, which often set the topic, and the last 50 before the
  mention). Only lab members are answered.
- **DM catch-up** (#70): on a DM's first new message since the manager started, it first
  reads the member's messages there that it missed while it wasn't running: those after
  the last message a turn saw, at most 72 hours back, the last 20, each cut to 1000
  characters (attachments aren't downloaded). They come before the new message, marked as
  missed, and are answered in the one reply. A DM it has never seen a message in has
  nothing to catch up on; channel threads aren't caught up on.
- Attached to a human **for tokens**; **access** `dedicated` (its member gets everything;
  other members are **guests**) or `shared` (every member gets everything, with a
  separate memory per member, enforced in code).
- **Guests** only talk: questions, explanations, scientific discussion. In a guest's
  conversation code removes every tool (commands, files, worker tasks, GitHub — the bot's
  token is left out — anything outside the sandbox, memory), and turns are cut off after
  2 minutes. Asked for real work, the manager points to the guest's own manager or a
  shared one. In the owner's thread a guest's turn **never resumes the owner's session**
  (its tool outputs, files read, unposted details, memory): it runs in a fresh session
  that starts from the thread as everyone sees it (first 5 and last 50 messages), with a
  "guest" notice in the message itself, so missing tools don't look like a fault. The
  owner's next turn starts with a short "meanwhile in this thread" note (the guest's
  messages and the replies). In their own DM with the manager, a guest keeps that DM's
  session (only they are in it).
- **Turns requests into worker tasks**, follows them and reports back (progress, plots,
  summaries) in the conversation that asked.
- **Relays approvals**: it can show what a task waits for and add a note to the request; it
  **cannot answer** one (§6.3). Its own actions outside its sandbox are permission requests
  for the member whose message started the work.
- **Stays reachable**: one turn at a time *per conversation* (other threads and members are
  never blocked); it does work itself only when it takes a few minutes; a turn longer than
  `turn_budget_min` (default 5) is cut off, and it gets one short turn to hand the rest to a
  worker; a message arriving meanwhile gets an immediate "still working" reply.
- Auto-compacts its context early (17% of the window) to keep long conversations cheap.

### 4.4 GitHub for agents

A manager or worker may have a GitHub identity (`github: <name>`), or **several, picked
by repository**:
```yaml
github:
  acme-flash: default                     # everything else
  spikyclaudebot: [anatoli-starostin/spiky]    # these repositories (owner/* too)
```
git picks the identity by the repository by itself (a credential helper for the token,
git's conditional config for the commit author; the service writes the agent's git config,
`GIT_CONFIG_GLOBAL`). `gh` is a small shim first on the agent's `PATH`
(`deploy/worker/gh-shim`): it reads the current token at call time (the identity mapped to
`-R owner/repo`, or to a repository named in the command: `gh repo view owner/repo`,
`gh api repos/owner/repo/...`, a github.com URL; else the default; a `GH_TOKEN` the agent
sets wins; `LAB_GH_DEBUG=1` says which) and runs the real
`gh`. gh's own config, which it writes on many commands, is in the agent's working folder
(`<workdir>/.config/gh`, `GH_CONFIG_DIR`); the tokens stay where the agent can read but not
write them. Without `-R`, it takes the identity from the clone it runs in (its `origin`).

**Which repositories each identity can reach** (#53): the service lists them, an App's
installation repositories with each new token (hourly) and a bot token's repositories at
start and every 6 hours, into `<identity>/repos.txt` next to the token (readable, not
writable). The GitHub paragraph of the agent's prompt is rebuilt at every session: it names
the reachable `owner/repo`s per identity (as of when), points to the files as the current
version, says to clone `https://github.com/<owner>/<repo>.git` with exact names and never
guess an owner, and that `repos.tsv` lists only repositories mapped to a *non-default*
identity (empty: one identity for everything, not "no access"). A failed listing (no
network, revoked token, rate limit) keeps the last list and never touches the agent's
access.
Several agents may share an identity. Identities live in `github.accounts`; two kinds, side
by side:
- **A GitHub App** (`app_id`; `installation_id` too if the app is installed in several
  places): appears as `<slug>[bot]`, no account or email to create, so
  every agent can easily have its own. Tokens last an hour: the agent's service holds the
  app's private key (unreadable by the agent), mints a token, keeps a fresh one where git
  and the `gh` shim read it (`github_app.py`), and mints the next before it expires.
- **A full bot account** (a GitHub user with its own email): a fine-grained token, read the
  same way, its expiry tracked.

Either way git and `gh` are set up, commits carry the identity's name and email, and
`github.com` is reachable from the sandbox. The agent can read its current token: the
safety is GitHub's — private repositories, a fine-grained token limited to the lab's repos
(contents, pull requests, issues), and **branch protection** on `main` (pull requests,
reviews by human code owners, no bypass for bots). Bots push branches and open pull
requests, issues and comments; they never approve or merge.

**The secretary** may have one identity too (`github: <account>`, a bot account or an App;
no per-repository map: it only works on the config repo). It's for its *service*, not its
agent: the config clone goes over HTTPS with that identity's token, commits are authored as
it, and `propose_lab_change` opens the pull request through GitHub's API; `lab apply` then
makes no deploy key (some organisations forbid them). The token and the git setup sit in
the service's state folder, which the agent's sandbox can't read, and the agent's sandbox
doesn't get `github.com`. Without `github:` it keeps its deploy key, as before. Every
secretary commit carries a `Lab-Agent: <name> (secretary)` trailer, since an identity may be
shared by several agents. Recommended: a fine-grained token limited to the config repo
(Contents and Pull requests: read and write); branch protection keeps it off `main`.

### 4.5 Onboarding a newcomer

The secretary guides a newcomer through **what they can do on their own**, before any admin
work, so that by the time the admins set up their agents everything the newcomer owes is
in: a few answers, their Claude token, their keys for outside services, and the lab's own
steps. Admin work (lab.yaml, machines, the manager's Slack app, GitHub) isn't on it: it
stays with the admins and their tools.

- **Started from the console** by an admin, as the secretary's user: `lab onboarding start
  <id> --slack U… --name "…"`, with the lab id the newcomer will have. They needn't be in
  lab.yaml: their secrets are stored under that id through the intake, ready for the deploy
  that adds them. Within a minute the secretary DMs them.
- **A Claude agent with instructions, not a scripted flow.** The lab's own `onboarding:`
  section in lab.yaml says what to ask (`ask`: github, manager_name, ssh_key), which keys
  (`keys`: member-key names, e.g. a W&B key), the admins' own steps (`items`: `{id, text}`,
  confirmed by the newcomer) and free-form `instructions` (tips, links, where to get each
  key). The agent follows them, one step per message.
- **The checklist is code**, computed every time from the stored answers and secrets,
  never from what the agent remembers. Answers are checked before they're stored (a GitHub
  user that exists, a public SSH key and never a private one, a manager name not taken).
  `lab onboarding status [id]` shows it on the console.
- **When every item is done**, the secretary DMs the admins with what was collected. When a
  secret arrives through the intake (not the chat), the secretary carries on by itself.
- **A newcomer's session is narrow**: only the onboarding tools (their own checklist,
  answers, one-time links for their own token and the checklist's keys), no built-in tools,
  no lab state; the deployed layout isn't in their prompts. **Until it finishes, that's all
  they get**, even if they're added to lab.yaml midway: no member chat, no Home tab. It ends
  when the onboarding finishes or an admin stops it (`lab onboarding stop <id>`): until they're in lab.yaml
  the secretary then treats them as anyone outside the lab.

---

## 5. The worker protocol (MCP over the tailnet)

Each worker runs an MCP server (streamable HTTP) on **its tailnet address**, port 8770. Every
request carries a bearer token: one per manager allowed on the worker (a dedicated worker
accepts only its member's managers), plus the secretary's **read-only** monitoring token.

| Tool | Purpose |
|---|---|
| `submit_task(instruction, requester, project?)` | Queue a task (first come, first served). The manager also registers the task with the broker, so the requester is known for approvals. |
| `task_status(task_id)` | `queued \| running \| awaiting_approval \| waiting \| done \| failed \| cancelled`, plus the latest progress line and its long runs (`waiting`: in progress, parked on them, §4.2). |
| `task_result(task_id)` | All progress lines, its runs with their log tails, and, when finished, the result (summary + artifacts). |
| `cancel_task(task_id)` | A queued task at once; a running one as soon as the agent can stop; a waiting one with its runs. |
| `list_tasks(state?)` | The queue. |
| `get_guardrails()` | The worker's sandbox settings, standing and active grants. |
| `read_file(path)`, `write_file(name, data)` | Files in its working folder (up to 20 MB): results out, attachments in. |
| `health_check()` | Queue, long runs going, GPUs and the processes on them, disk, usage. |
| `check_usage()` | Read its seat's limits now (the web UI's "Check now"). |
| `recent_events(hours)` | Its event log: **the secretary's monitoring token only** (the admins' log viewer). |

Managers follow tasks by polling. The queue is durable on disk; after a restart a running
task is requeued and its Claude session resumed. One task runs at a time per worker; a
task waiting on its long runs doesn't count (§4.2).

---

## 6. Guardrails and the trust model

### 6.1 One simple model for every agent

| | **Inside the sandbox: auto-allowed** | **Outside the sandbox: only with permission** |
|---|---|---|
| Internet | none (plus granted hosts; github.com for an agent with a bot) | allowed after a human says yes |
| Tailnet (lab resources) | **allowed**, by tailnet name (raw IPs refused) | — |
| Local files | write the **working folder and /tmp**; read anything except `/run` and its service's secrets and state (`~/.config/agentic-lab`, `~/.local/state/agentic-lab`, `~/.nebius`) | writes elsewhere need permission |
| GPU | available to workers | — |

Enforcement is **Claude Code's built-in sandbox** (bubblewrap + seccomp + a network proxy),
rendered per agent from `lab.yaml` (`lab settings <agent>`):
- **Every Bash command runs sandboxed** and is allowed without asking. There is no command
  parsing. The sandbox protects `.git/hooks`, `.git/config` and Claude's own settings.
- `failIfUnavailable`: an agent never runs unsandboxed by accident. Strict network allowlist:
  unknown hosts are refused, not prompted.
- **GPU**: a `bwrap-gpu` wrapper (set as `sandbox.bwrapPath` in Claude Code's managed
  settings) binds the NVIDIA devices. CUDA needs Unix sockets, so GPU agents allow them and
  `/run` is hidden instead (closing a `systemd-run` escape). Host setup:
  `deploy/worker/setup-sandbox.sh` (AppArmor profile for bubblewrap, the wrapper, the
  managed settings).
- **Outside the sandbox**: Bash re-run unsandboxed, writes outside the allowed folders, other
  tools — each is a permission request (§6.3).
- **The tailnet is trusted by design for now**; this may tighten to per-host lists.

**The usual flow:** the agent runs a command → the sandbox refuses it (say, a host that isn't
allowed) → the agent sees why and retries it outside the sandbox → that's a permission
request → the human decides (§6.2).

### 6.2 Grants: permissions customised on the fly

When a human answers a request, they choose how far the answer reaches:
- **Run once**: this one action runs.
- **Allow *X* for 1 hour** or **for this task**: a **grant** that widens the sandbox (a
  domain, a writable path, or a tool) with an expiry. The agent's session restarts with
  the wider sandbox and is told what changed (a running session doesn't pick up a changed
  allowlist; resuming is seamless).
- **Deny**, or a typed reply: "no, do it this way" (the reply goes back to the agent).

A grant always widens the **sandbox**; it never means "trust unsandboxed commands for an
hour". A long run (§4.2) is the one action that can only ever be approved once: no grant
is offered for it, and `lab check` refuses one in `lab.yaml`. **Standing grants** are in `lab.yaml` per agent (changed by a reviewed PR):
```yaml
agents:
  worker-a:
    grants:
      - {domain: pypi.org}
      - {domain: files.pythonhosted.org}
      - {write: ~/datasets}
  mgr-flash:
    grants:
      - {tool: WebFetch}
```
Active grants, their expiry and who gave them are shown in the web UI.

### 6.3 Approvals: the broker decides who answered

The rule "agents never approve" holds even if an agent is confused or prompt-injected,
because **approval is a code path outside every LLM**:

```
agent's service ──request──► BROKER (secretary host, user lab-broker, no LLM)
                                 ├─► managers may see it and add a note (no decision)
                                 └─► the human's DM from the "Lab Approvals" app
                                 ◄── the human's click or reply (identity from Slack)
agent's service ◄── decision ─── broker (its own token, over the tailnet)
```

- **An approval goes to the human the action is for**: a worker's task — the requester the
  manager registered (not what the worker claims); a manager's own action — the member whose
  message started the work (taken by the manager's *code* from the Slack event); no
  requester — the agent's attached human.
- Only **Lab Approvals** posts approval requests; only the broker holds its tokens. A
  decision is recorded only after a verified interaction of the asked human.
- **No timeout by default**: a request waits until its human answers. A member may set one
  (`timeout_s`); then an unanswered request is closed as denied when it runs out: silence
  counts as "no", never as "yes" (`on_timeout` can only be `deny`), and the agent is told
  it was a timeout, not a refusal. A request past its deadline can't be decided any more,
  even by a click that arrives before the broker's sweep. Members change their own timeout
  from Slack ("auto-deny my approvals after 15 minutes until 18:00", "no timeout", "back to
  default"; admins anyone's) through the secretary's `approval_timeout` tool: the broker
  keeps the override in its state, `lab.yaml` stays the default, and it applies to new
  requests only (#77). If the agent stops waiting (its task is cancelled, a
  manager's turn is cut off), the request is **withdrawn**, so nobody is asked any more.
- Grants are created only by the broker, after a verified human answer.

### 6.4 Secrets

- **Agents can't read their lab secrets.** Each agent's service keeps them in
  `~/.config/agentic-lab` (and its state in `~/.local/state/agentic-lab`): the agent's Read
  tool is denied there, its sandbox hides them, and the Claude token is stripped from
  sandboxed commands. One exception by design: a GitHub bot's token (§4.4).
- **Secrets never pass through a chat.** They arrive through the **intake**: one-time codes
  (single use, active **5 minutes**, bad attempts limited per device) for web forms, and a
  small client that runs `claude setup-token` on a member's laptop and sends the token
  straight to the lab. The intake checks Claude tokens with Anthropic and GitHub tokens
  with GitHub. (#63:) Links and commands go **only by DM**, to the person who pastes the
  secret (the member for their Claude token, the asking admin for a service's): asked in
  a channel, the secretary says there that it sent a DM. They're sent when that person is
  ready (the secretary first posts the steps, or what to have ready, and sends the link
  when they say so); a missed one is simply asked for again. The intake **runs only while
  a code is open**: creating a code creates a flag file that makes systemd start it
  (`lab-intake.path`), and it stops itself when none is open. It and the broker run
  **hardened** (no privileges, a read-only system, only their own folders, no devices).
  It still runs as the secretary's user; a user of its own is left for if it ever has to
  run continuously. An intake that fails to start removes its flag (so it isn't started
  again and again); the person asking is told, and so are the admins.
- **A member's own keys** for outside services (W&B, Hugging Face, an S3 bucket...;
  `member_env.py`): the member asks the secretary ("store my W&B key as WANDB_API_KEY"),
  and the value comes through the intake (`env:<NAME>`, a one-time link by DM). They go to
  **all of that member's dedicated agents**, managers and workers, never a shared one, as
  environment variables for their Claude Code sessions and long runs. Unlike the lab's
  secrets these are meant to be used, so the agent's commands can read them; what keeps
  them in check is that only the member's own agents get them, a key gives no network
  access (that's a grant), and the key should be one of the lab's own (revocable). Names
  are checked (the lab's own variables and those that change how programs run are
  refused). **Delivery is a deploy:** each host records the revision of the keys it got
  (`member_env` in applied.yaml), and the catch-up (#61) redeploys a host that should
  have others, now or when it next starts. No `lab.yaml` entry and no PR: a member
  manages their own keys, an admin anyone's.
- **Lab tokens** (manager↔worker, agent↔broker, the secretary's monitoring tokens) are
  generated by `lab apply` and never shown.
- `lab.yaml` holds no secrets: only GitHub bots' and other credentials' expiry dates.

---

## 7. Permission channels

**This version: Slack DM only**, from the **Lab Approvals** app, with buttons **Run once ·
Allow *X* · 1 hour · Allow *X* · this task · Deny**, or a typed reply in the request's
thread (a denial with a reason).

```yaml
humans:
  alice:
    slack_user: U0123ABC
    channels:
      - {type: slack_dm, on_timeout: deny}           # optional: timeout_s: 600
```
- No timeout unless `timeout_s` is set; `on_timeout` is always `deny`.
- The broker's channel interface (`ask(request) -> decision`) leaves room for other channels
  later without touching its trust rules.

---

## 8. The lab registry: `lab.yaml`

The one description every component reads, kept in the lab's private config repository:

```yaml
lab: example-lab
tailnet: example.ts.net
announcements: "#lab"                    # optional: lab-wide notices
auto_catch_up: true                      # optional (default): hosts that missed a deploy catch up
visibility: private                      # optional (default); team: members see each other's work
humans:
  alice:
    name: Alice
    slack_user: U0123ABC
    admin: true                          # may read every agent's log, administer the lab
    tailscale_login: alice@example.org   # how the web UI recognises her (her agents' logs)
    channels:
      - {type: slack_dm, on_timeout: deny}
agents:
  secretary:
    role: secretary
    human: alice                         # whose Claude seat it runs on
    host: sec-01
    display_name: Iris
    model: claude-haiku-5-5              # lab init's choice for the secretary; any agent
                                         #   may set model (default: Claude Code's)
  mgr-flash:
    role: manager
    human: alice
    host: sec-01                         # managers run on any host
    access: dedicated
    display_name: Flash
    github: flash-bot                    # its GitHub identity (§4.4)
    turn_budget_min: 5
  worker-a:
    role: worker
    human: alice
    host: gpu-a
    access: shared
    grants:                              # standing grants (§6.2)
      - {domain: pypi.org}
    # gpu: true                          # the GPU in its sandbox (default: workers yes)
    # port: 8780                         # its MCP port (default 8770 for workers, 8773
    #                                    #   for managers): set one per agent of a role
    #                                    #   on the same host (lab check rejects clashes)
hosts:
  sec-01: {kind: onprem}
  mgr-01: {kind: cloud, provider: aws, instance: i-..., region: <region>,
           idle_stop_min: 0, ssh_user: ubuntu}
                                   # AWS: powered with the secretary's access key;
                                   #   public IPv4, zero inbound, EBS root (docs/install.md)
  gpu-a:  {kind: cloud, provider: nebius, instance: computeinstance-...,
           idle_stop_min: 30, tailnet_name: gpu-a.example.ts.net}
                                   # + idle_stop_with_managers: false, if a manager
                                   #   there should keep it on (§4.1)
                                   # + readers: [alice]: Linux users here who may read
                                   #   its agents' working folders (§10)
github:
  accounts:
    flash-bot: {expires: 2027-10-01}     # its token's expiry, for reminders
credentials:
  - {name: some/other-key, host: gpu-a, expires: 2027-03-01}
```

`lab check` validates all of it at once (unknown keys are errors); agent ids are lowercase,
since they become Unix user names (`lab-<id>`). Names humans see (`display_name`) are the
admin's choice; the guide never invents them.

---

## 9. Web UI — the lab's shared window

Served by the secretary on the tailnet (`:8774`), refreshed every 20 s. **Monitoring only**:
permissions are answered in Slack (§7).

- **Claude usage, by account** (each member's seat): the 5-hour and weekly windows (the
  freshest reading of any agent on that seat, with *as of when* and *from which agent*), and
  today's tokens split by agent. Percentages come from a **probe**: each agent's service
  reads the seat's utilization headers from one tiny request every 10 minutes, only while the
  seat's 5-hour window is open; **Check now** probes at once.
- **Workers**: busy or idle, GPU (utilization, memory, processes), disk, recent tasks.
- **Approvals and grants**: pending requests and who decides; active grants and expiry;
  each member's approval timeout, worded as the secretary says it in Slack (the value,
  lab.yaml's or their own, and an override's end in their time zone and UTC).
- **Machines**: power state, policy, idle since.
- **Agents**: role, seat, host, status (the one status of §4.1; a managers-only host's
  managers also busy or idle, and why).
- **Deployed**: the config and framework commits every host runs (flagged if unreviewed).
- **Agents' logs** (`/logs/<agent>`): admins see every agent's; a member sees the whole log
  of their **dedicated** agents (guest turns in their manager's threads included), and only
  **their own entries** (their messages, their tasks) of shared agents and the secretary,
  whose logs hold other members' work; anyone else gets 403. The request's tailnet address
  must not be a lab host; `tailscale whois` must name a person (not a tagged device) whose
  login is a member's `tailscale_login` in `lab.yaml`. Tailscale authenticates every packet with its device's key, so the
  address can't be forged.

**Who sees what** (#62; the same for `/`, `/state.json`, the Slack Home tab and the
secretary's tools), from the viewer's Tailscale identity as for the logs:

- **Admins** see everything.
- **A member** sees what the lab is doing: machines, busy or idle and why (whose task, who
  keeps a machine awake), queues, every task's state and owner, Claude usage per seat,
  and the grants of their own and of shared agents (of other members' own agents, only
  their kinds). Of other members' work, nothing of its content: no instructions, results,
  long runs' names or approval details.
- **Anyone else** (a device that isn't a member's, and every lab host, i.e. every agent):
  machines, states and counts, without names or text.
- `visibility: team` in `lab.yaml` lets members see each other's work too (small trusted
  teams); `private` is the default.

On a **shared worker**, a manager posts a member only files of that member's own tasks:
each task's folder, `tasks/<task id>/` in the worker's working folder, where the agent is
told to put what the member should see. Admins (or `visibility: team`) may fetch any file.

## 10. Agent runtime

**Every agent runs on the Claude Agent SDK as a systemd service** (`lab-worker@<id>`,
`lab-manager@<id>`, `lab-secretary`), with Claude Code's built-in sandbox. The SDK's
permission callback *is* the gate: sandboxed Bash is allowed, active grants are checked,
anything else goes to the broker.

- **One Unix user per agent**, `lab-<agent-id>`, even when several agents share a host: no
  password, no SSH keys, a `nologin` shell, lingering off, home folder private (files created
  `0077`; a host's `readers` may read its agents' working folders, #43). Agents on one
  host are separated by Unix permissions. Nobody runs anything else as these users (GPU
  agents allow Unix sockets, so a socket server running as the agent's user would be a way
  out). Humans log in as themselves; `sudo lab debug <agent>` switches to the agent's user.
- Sessions persist on disk and resume after restarts. A resumed session may remember an
  older layout of the lab (agents move between machines), so every secretary turn starts
  with the layout as deployed, and its `power` tool takes an agent and looks its machine
  up in `lab.yaml` (#99).
- **Every agent is attached to a human, in terms of tokens**: `human:` names the member whose
  Claude seat it runs on (a `claude setup-token` token, valid a year). Who may *use* the
  agent is set separately (`access`). No per-token billing in this version.
- **Models**: per agent in `lab.yaml` (`model:`), else Claude Code's default.
- **Context**: managers auto-compact at 17% of the window (`compact_percent`); others at
  Claude's default.

---

## 11a. Setup and maintenance

**Two Claudes on the secretary host:**

| | Admin assistant | Secretary agent |
|---|---|---|
| What | the admin's own interactive Claude Code session, in a checkout of this repo | the lab's 24/7 service (§4.1) |
| Runs as | the admin's Unix user (SSH and sudo across hosts) | `lab-secretary` |
| Used for | the install, emergencies, uninstalling | everyday administration, in a Slack DM |

**The install** follows `docs/install.md`, guided by the repo's `lab-admin` skill: the
private config repo, `lab init`, the secretary's services, the Lab Approvals and secretary
Slack apps (one at a time), the admin's Claude token, VM power, GPU hosts, the first deploy.

**Only reviewed configuration is deployed.** `lab apply` fetches the config repo and deploys
`lab.yaml` and `framework.lock` (the framework's pinned commit) **as they are on `main`**,
never the working copy, and ships the framework only from a clean checkout at the pinned
commit. Every change: a branch → `lab plan --working-tree` → a pull request (`lab pr-link`) →
a human reviews and merges → deploy. Every host records what it runs
(`/etc/agentic-lab/applied.yaml`). Break glass: `lab apply --unreviewed` (flagged).

**After the install, admins work with the secretary in Slack**, with admin-only tools checked
in code: `propose_lab_change` (a checked branch, pushed, and with the secretary's GitHub
identity the pull request opened; replies with the diff and "here is the PR, review and merge
it"), `offer_deploy` (a **Deploy** button; only an admin's click
deploys), `request_slack_app` (with an app icon Claude draws from the agent's name, role and the lab's name;
`redraw_app_icon` for another), `request_github_token`, `request_github_app` and `check_github_app` (a GitHub App as an
agent's identity), `check_slack_app`. The secretary's root command is **`lab apply-main`**
(two exact sudoers rules, no free arguments): it deploys only `main`, as its own systemd
unit. Even a misled secretary can only deploy what a human merged.

**Hosts that missed a deploy** (a GPU host stopped at the time, the usual case) are caught
up when they're back (#61): every agent's health reading says what its host runs
(`/etc/agentic-lab/applied.yaml`); when a host runs something other than the secretary's
host, the secretary runs `lab apply-main --catch-up`, which deploys the commit of `main` the
secretary's host runs (never a newer one, never an `--unreviewed` apply) to the hosts on an
**older** reviewed commit of `main`. A host on a newer or unreviewed commit is left alone
(shown as "ahead", never rolled back). Not while a worker there has tasks running, queued,
awaiting approval or waiting on their runs: the secretary waits for that, and the host
checks again itself first thing, before anything on it changes (as each worker's user); a
host busy for over 6 hours is announced once and asked every 15 minutes. Only a failed
deploy (ssh failing on a host that answers counts, a host not answering doesn't) counts as an attempt (a
host not reachable yet while it boots, or busy, doesn't): at most 3 per host and commit (5
and 20 minutes apart), then the host stays stale and the admins are told; failures older
than a day are forgotten, so it tries again. A catch-up that never reports back is given up
after 30 minutes. The deploy lock applies (a running deploy is waited for). The web UI
marks stale hosts ("config N commits behind"), compared with `main` when the lab itself
runs something unreviewed. `auto_catch_up: false` in `lab.yaml` turns the catch-up off;
stale hosts are still shown.

| Command | Does |
|---|---|
| `lab init` | the first `lab.yaml` (and `framework.lock`), with names the admin chose |
| `lab check` | validate `lab.yaml` |
| `lab plan [--working-tree]` | what a deploy would change; missing secrets |
| `lab apply` / `lab apply-main` | deploy `main` (idempotent, over SSH to other hosts) |
| `lab pr-link` | the link to open a pull request for the config branch |
| `lab doctor` | check a host; every failure says how to fix it |
| `lab secret request` | a one-time code or link for a secret |
| `lab slack-manifest`, `lab slack-verify` | a Slack app's manifest; check its tokens |
| `lab nebius connect` | VM power with one admin login |
| `lab logs`, `lab debug` | an agent's log; its session in the console |

**Secrets never pass through the chat** (§6.4). **Uninstalling**: `docs/uninstall.md`, at
the console only.

## 11. Repo layout

```
src/agentic_lab/
  registry/      lab.yaml schema, loader, `lab check`
  policy/        per-agent sandbox settings, grants
  broker/        approval broker + Slack DM channel (Lab Approvals)
  worker/        worker service: MCP server, durable queue, SDK runner, usage
  manager/       manager service: Slack, brain, tools, memory, follow-up, status
  secretary/     state collection, web UI, Home tab, power, chat
  secrets/       intake (one-time codes, web forms)
  apply.py, configrepo.py, lock.py, selfadmin.py   deploying (lab apply / apply-main)
  github.py, slackio.py, slack_manifest.py, eventlog.py, doctor.py, init.py, cli.py, ...
deploy/          systemd units, sandbox setup (AppArmor, bwrap-gpu), user creation
client/          lab-member.py: members hand their Claude token to the lab
docs/            install, uninstall, security notes, architecture (PDF)
examples/        example lab.yaml files
tests/
```

Python ≥ 3.12, managed with `uv`; dependencies: Claude Agent SDK, MCP SDK, slack_sdk,
PyYAML, Starlette/uvicorn.

---

## 12. Milestones

All of M0–M4 are built and were rehearsed end to end on one GPU VM (the whole lab on one
host, each agent its own user: `examples/single-vm/lab.yaml`).

| # | Milestone | Status |
|---|---|---|
| M0 | Skeleton: `lab.yaml` schema, `lab check`, per-agent sandbox settings | done |
| M1 | Worker: MCP server, durable queue, SDK runner, built-in sandbox with GPU, `lab debug` | done |
| M2 | Broker + Lab Approvals in Slack DM: buttons, grants, timeouts | done |
| M3 | Manager: Slack, tasks, follow-up, memory, approvals relay | done |
| M3a | Guided setup: `lab-admin` skill, `lab init/plan/apply/doctor`, intake; config repo; Slack admin | done |
| M4 | Secretary + web UI: state, usage, power and idle stop, notices, Home tab | done |
| M5 | First real lab: install and use it with a real team | next |

---

## 13. Decisions log

| Date | Decision |
|---|---|
| 2026-10-17 | The secretary runs on an always-on host. Other hosts are cloud VMs, as close to real use as possible. |
| 2026-10-17 | Repo is private now and public later, so it stays generic. MIT license. |
| 2026-10-17 | Shared workers: **first come, first served**. No quotas or priorities. |
| 2026-10-17 | **No fixed results contract**: a result is whatever fits (report, commit, link, files). |
| 2026-10-17 | **Agent SDK runtime.** (An `AgentRunner` interface with a tmux fallback was planned; not built, the SDK was enough.) Humans watch agents in the web UI and debug with `lab debug <agent>`. |
| 2026-10-17 | **Every agent is attached to a human member in terms of tokens** (runs on their seat). Access is set separately. |
| 2026-10-17 | **One simple sandbox model for all agents** (§6.1), enforced by Claude Code's built-in sandbox, no command parsing. **Grants** (once / for a while / standing) customise it (§6.2). |
| 2026-10-17 | ~~Guardrail changes initiated by the manager over MCP (`change_guardrails`).~~ Not built: grants come from answering requests, standing grants from `lab.yaml` PRs. |
| 2026-10-17 | ~~All managers share one cloud CPU VM.~~ Superseded 2026-10-18. |
| 2026-10-17 | **Permissions only in Slack DM** in this version. The **web UI is for monitoring only**. |
| 2026-10-17 | **Agents run as system services, each under its own Unix user** (`lab-<agent-id>`). Nobody logs into these users. |
| 2026-10-17 | **Setup is guided by Claude**: the `lab-admin` skill and `docs/install.md`, over declarative, idempotent `lab` commands. Secrets never pass through the chat. |
| 2026-10-17 | **No `lab console`.** Each agent's event log is shown to admins in the web UI; `lab debug <agent>` opens its session in the console. |
| 2026-10-17 | **Approvals go to the human the action is for**, from one Lab Approvals app held by the broker. Managers, like workers, are `dedicated` or `shared`. |
| 2026-10-17 | **The secretary is in Slack** (DMs, threads it's tagged in, a Home tab); lab-wide notices go to an optional `announcements` channel. |
| 2026-10-17 | **Names**: an id (also the Unix user) plus a `display_name` humans see. |
| 2026-10-17 | **Auto-compaction per agent**: managers at 17%; others at Claude's default. |
| 2026-10-17 | **Agents can't read their lab secrets** (§6.4). |
| 2026-10-17 | **No spend limits.** Transparency through the web UI instead. |
| 2026-10-17 | ~~**Agents' logs are for admins only** (`admin: true` + Tailscale identity).~~ Superseded 2026-10-20. |
| 2026-10-20 | **Members see their own agents' logs**: dedicated ones whole, their own entries of shared agents and the secretary (Tailscale identity). Admins see all. |
| 2026-10-17 | **Usage percentages from a probe** of the seat's rate-limit headers, only while a window is open; "Check now" in the web UI. |
| 2026-10-17 | **Admin-only actions, checked in code**: ~~powering off a busy machine (after confirming)~~ (superseded 2026-10-23: who may power is in §4.1 Power, #91), asking for someone else's token. **Busy** includes any process on a GPU or any GPU utilization (idle only after 0% in several readings in a row). |
| 2026-10-17 | **Private config repository**: `lab.yaml` + `framework.lock` (the framework pinned by commit). **Only `main` is deployed**; every change is a reviewed PR. |
| 2026-10-17 | **Administering from Slack**: the secretary proposes PRs and deploys `main` on an admin's click, with one sudo command (`lab apply-main`). The console is for the install and emergencies. |
| 2026-10-18 | **GitHub for agents**: one bot account per agent (may be shared); safety from GitHub (private repos, fine-grained tokens, branch protection: bots never approve or merge). Its token was in the agent's environment; since 2026-10-22 it's a file read by git's credential helper and the `gh` shim. |
| 2026-10-18 | **No experiment tracker in the framework**: project tools are a lab's extensions. |
| 2026-10-18 | **Managers run on any host**: the secretary host, a VM of their own, or a GPU VM next to their workers. |
| 2026-10-18 | **Managers stay reachable**: a work-size rule, a per-turn time limit (`turn_budget_min`, default 5), and a "still working" reply. |
| 2026-10-18 | **A worker's event log goes only to the secretary's monitoring token.** |
| 2026-10-18 | **Anyone in the lab may talk to any manager.** On a dedicated manager, other members are guests: conversation only, no tools, no work, no memory, 2-minute turns. |
| 2026-10-18 | **`~/.lab-keep-awake`**: anyone working on a lab machine directly keeps it from being stopped. |
| 2026-10-18 | **Approval requests have no timeout by default** (a member may set `timeout_s`); a request the agent stops waiting for is withdrawn. |
| 2026-10-19 | **Several GitHub identities per agent, picked by repository** (e.g. a lab identity by default, an open-source project's bot for that project). |
| 2026-10-19 | **GitHub Apps as agents' identities**, alongside full bot accounts: `<slug>[bot]`, nothing to create but the app; the service mints its hourly tokens. |
| 2026-10-19 | **The secretary may have a GitHub identity** (its service's, for the config repo): HTTPS instead of a deploy key, PRs opened by it, a `Lab-Agent:` trailer on its commits. Without one, the deploy key as before. |
| 2026-10-19 | **`power:` removed**: `idle_stop_min` is the only switch (required on cloud hosts; `0` = never). Managers don't keep a host on unless it says `idle_stop_with_managers: false`. |
| 2026-10-19 | **Long runs**: jobs that outlive the session run as their own units, outside the sandbox, approved one at a time by a human; the task waits (parked) and the worker takes other tasks meanwhile. Not a second sandbox runtime (`srt` would mean Node and another versioned copy of the sandbox to maintain). |
| 2026-10-20 | **Guests in a member's thread get a session of their own**, built from what the thread shows; never the owner's session. |
| 2026-10-20 | **Warm environments**: tools' caches in the working folder, checkouts and environments reused; `keep_free_gb` bounds the disk they take. |
| 2026-10-20 | **Host `readers`**: Linux users who may read a host's agents' working folders (a group and ACLs), never their secrets or state. |
| 2026-10-21 | **AWS EC2 hosts**, without power control for now (public IPv4 with no inbound, EBS root). |
| 2026-10-22 | **`gh` through a shim** that reads the identity's current token at call time; gh's config in the working folder. Each identity's **reachable repositories** are listed in the agent's prompt. |
| 2026-10-22 | **Intake on demand** (#63): one-time links only by DM, sent when the person is ready, active 5 minutes; the intake runs only while a code is open (a systemd path unit); intake and broker hardened. Its own Unix user: later, only if it must run continuously. |
| 2026-10-22 | **Transparent about the lab, private about the work** (#62): members see machines, states, owners and usage, not each other's instructions, results, files or approvals; anonymous viewers and agents see states and counts; admins everything. `visibility: team` to share work. Files from shared workers: the member's own `tasks/<id>/` only. Supersedes "everything is visible". |
| 2026-11-01 | **The secretary onboards newcomers** (§4.5): only what they can do without an admin; a Claude agent following the lab's instructions, with a checklist computed in code; secrets stored under their future id before they're in lab.yaml. |
| 2026-10-22 | **Hosts that missed a deploy catch up automatically** (#61), to the commit of `main` the rest of the lab runs: triggered by the secretary, deferred while tasks are active, 3 attempts then admins are told; `auto_catch_up: false` to opt out. |

## 14. Open questions

None right now.
