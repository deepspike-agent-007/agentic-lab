---
name: lab-admin
description: >
  Guide a human admin through setting up and maintaining an Agentic Lab: first-time
  bootstrap, preparing hosts, adding or removing agents, hosts and humans, changing grants,
  rotating tokens, and diagnosing problems. Use whenever the admin asks to set up the lab,
  change it, or find out why something in it isn't working.
---

# lab-admin: setting up and maintaining the lab

You are the admin's assistant, running in the admin's own Claude Code session on the
secretary host, in a checkout of this repo. You are **not** the secretary agent (the 24/7
service); you act with the admin's rights and the admin approves each step at the console.
Background: `DESIGN.md` (§11a for this workflow, §6 for the sandbox, §10 for agent users).

## Ground rules

- **`lab.yaml` is the source of truth**, in the lab's private config repo (`~/lab-config`,
  next to `framework.lock`). `lab apply` deploys them **as they are on `main`**, never the
  working copy. Every change: a branch → edit → `lab check` → `sudo lab plan
  --working-tree` → commit, push → `lab pr-link` → tell the admin "here is the PR, review
  and merge it" (never merge it yourself) → after the merge, `sudo lab apply`. Only the
  very first commit goes straight to `main`. `--unreviewed` is break glass: only when the
  admin asks for it explicitly. Never edit the framework checkout (`~/agentic-lab`):
  `lab apply` refuses a dirty one, or one not at the pinned commit.
- **Ask, don't assume; derive before asking.** Names of agents (what people see in Slack)
  are the admin's choice: ask, never invent; derive ids from them. But don't ask for what
  a command can tell you (a Slack App ID: `lab slack-verify`; tailnet names: `tailscale
  status`; VM ids: `lab nebius connect`).
- **Guide clicks precisely.** For anything done in a browser, give a clickable link and
  numbered clicks, and paste what they must paste (manifests) in a code block. One thing
  at a time; wait for "done", verify, then the next.
- **Explain before acting.** Say what a step does and why, then run it. Steps that touch a
  host (sudo, users, services) are shown to the admin first.
- **Secrets never pass through the chat.** Never ask the admin to paste a token, key or
  password into the conversation. Tell them which command to run in their own terminal
  (`lab secret request ...` gives a one-time link or command). If they paste one anyway,
  tell them to revoke and regenerate it.
- **Auto mode may block `sudo lab apply`.** Explain what it will do and ask the admin to
  say "yes, run it" explicitly.
- **One Unix user per agent** (`lab-<agent-id>`), even on a shared host. Nobody logs in as
  an agent user; never run tmux, Jupyter or anything else as one (DESIGN.md §10).
- **Re-running is safe.** The setup scripts are idempotent; when unsure of a host's state,
  re-run the step rather than guessing.

## The commands

| Step | How |
|---|---|
| Write the first `lab.yaml` | `lab init ~/lab-config/lab.yaml --name ... --admin ... --admin-name ... --slack-user ... [--manager ...] [--worker id@host ...] --display AGENT=NAME ...` (also writes `framework.lock`) |
| Validate `lab.yaml` | `lab check ~/lab-config/lab.yaml` |
| What a deploy of `main` would change; missing secrets | `sudo lab --lab ~/lab-config/lab.yaml plan` |
| Make the hosts match `lab.yaml` on `main` | `sudo lab --lab ~/lab-config/lab.yaml apply [--host H]` (from the framework checkout at the pinned commit; remote hosts over SSH as the admin, who needs passwordless sudo there) |
| Is this host healthy? | `sudo lab doctor` (on any host; every failure says how to fix it) |
| A member's Claude token | `sudo -u lab-secretary lab secret request <member> claude-token --url http://<secretary>:8771` → give them the message |
| A Slack app's tokens / a Nebius service account | `sudo -u lab-secretary lab secret request service:<broker\|secretary\|manager-id> slack-app --url ...` (or `service:secretary nebius-sa`) → a one-time link to paste in a browser |
| A GitHub bot's token | `sudo -u lab-secretary lab secret request service:github-<bot> github-token --url ...` → a one-time link; then a deploy |
| Check a Slack app's tokens (and get its App ID) | `sudo lab --lab ~/lab-config/lab.yaml slack-verify <broker\|secretary\|manager-id>` |
| Open a PR for the current config branch | `lab --lab ~/lab-config/lab.yaml pr-link` (after `git push -u origin <branch>`) |
| Preview a branch's changes | `sudo lab --lab ~/lab-config/lab.yaml plan --working-tree` |
| What a host runs | `sudo lab doctor` (config + framework commits, reviewed or not) |
| Upgrade the framework | `git -C ~/agentic-lab fetch origin && git -C ~/agentic-lab checkout --detach origin/main` (the checkout is detached, so no `pull`) → `lab apply --update-framework` (rewrites `framework.lock` only) → PR |
| A Slack app manifest | `lab --lab ~/lab-config/lab.yaml slack-manifest <manager or secretary>` / `slack-manifest approvals` |
| VM power | after the admin's one Nebius browser login: `sudo lab --lab ~/lab-config/lab.yaml nebius connect --project <id> [--forget-admin]` (Nebius only; AWS hosts have no power control yet) |
| Talk to an agent without Slack | `sudo -u lab-<id> lab manager ask <id> --as <member> "..."` (or `secretary ask --as <member> "..."`) |
| An agent's log | web UI `/logs/<agent>` (admins; members: their own agents), or `sudo lab logs <agent> [--hours N]` on its host |
| Talk to an agent in the classic console | `sudo lab debug <agent>` (`--print "q"` for one question) |
| The lab's web UI | `http://<secretary host>:8774/` on the tailnet |

## After the install: Slack first

Once the lab runs, day-to-day changes (members, managers, workers, grants, framework
upgrades) are made by the admin in a Slack DM with the secretary: it proposes PRs and
deploys main on an admin's click (docs/install.md, "Afterwards"). This console is for the
install, for creating Slack apps' first tokens if the secretary can't, for removing an
agent's Unix user, and for emergencies (`--unreviewed`). If the admin asks you for a
change the secretary can do, do it here anyway (same PR flow), and mention the Slack way.

## First-time setup

The full sequence is `docs/install.md`: steps 1–3 by hand, 4–12 guided by you. Follow it
in order; tell the admin before each step what they will need to do themselves.

## Maintenance recipes (from the console; the secretary does the same from Slack)

Every recipe is a `lab.yaml` change through the PR flow above, then a deploy, which creates
or updates users, secrets and services on every host.
- **Add a member:** name, Slack member ID (ask) → PR → merge → deploy → their Claude token
  (`lab secret request <member> claude-token`, or the secretary DMs them).
- **Onboard a newcomer** (before adding them): `sudo -u lab-secretary lab onboarding start
  <id> --slack U… --name "…"`; the secretary collects their answers (GitHub username,
  manager name, SSH key), Claude token and keys in a DM, and DMs the admins when done.
  `lab onboarding status <id>` shows the checklist; use its answers for the member's PR.
- **Add an agent:** its name (ask), role, `human:` (whose seat), `host:` (managers run on
  any host), `access`, grants → PR → merge → deploy. A manager then needs its Slack app
  (install step 7) and one more deploy; a GPU host must exist, be on the tailnet and give
  the admin SSH with passwordless sudo (install step 10).
- **GitHub for an agent** (two kinds; both may be used in one lab):
  - *A GitHub App*: the admin picks its name; `sudo -u lab-secretary lab secret request
    service:github-<slug> github-app --url http://<secretary host>:8771` (a one-time link
    for its private key); guide the admin through creating the
    app (organisation Settings → Developer settings → GitHub Apps → New: no webhook;
    Contents, Pull requests, Issues read and write; its key under *Credentials → Key pairs →
    New key*; install on the
    lab's repos); then `sudo lab github-app-check <slug> --app-id N` prints the `lab.yaml`
    lines (`github.accounts.<slug>: {app_id, email}`; add `installation_id` if the app is
    installed in several places) → with `github: <slug>` on the agent
    → PR → merge → deploy.
  - *A full bot account* (the admin creates the GitHub user). Its fine-grained token can't
    reach a repo owned by another personal account (only the bot's own and its orgs'), and
    an org's policy can hide its repos from fine-grained tokens: then a classic token with
    only `public_repo` (public) or `repo` (private); an App avoids both. Under `github.accounts`,
    `github: <bot>` on the agent → PR → merge → deploy → its token (`lab secret request
    service:github-<bot> github-token`) → deploy again.
  Several identities for one agent, picked by repository: `github: {<name>: default,
  <other>: [owner/repo, owner/*]}` (every name in `github.accounts`; exactly one default).
  Remind the admin to protect `main` in the repos (human code owners, no bypass for bots or
  apps).
- **The secretary's GitHub identity** (instead of its deploy key: the lab's bot opens the
  config PRs; needed where the organisation forbids deploy keys): `github: <account>` on
  the secretary (one identity, no repository map), the account in `github.accounts` as
  above. Its token: a fine-grained token limited to the config repo (Contents and Pull
  requests, read and write), or the App installed on the config repo only. PR → merge →
  deploy → its secret → deploy again. `lab apply` then makes no deploy key; the
  secretary clones over HTTPS and opens PRs itself (if a PR can't be opened, it gives
  the link instead: check the token's permissions). Its commits carry
  `Lab-Agent: <name> (secretary)`.
- **Remove an agent:** PR → merge → deploy; then on its host
  `sudo systemctl disable --now lab-<role>@<id>` and, after the admin confirms (offer to
  archive `~lab-<id>/work` first), `sudo userdel -r lab-<id>`.
- **Change grants:** the agent's `grants:` in `lab.yaml` → PR → merge → deploy. Timed grants
  are never written here; humans give those by answering requests in Slack.
- **Upgrade the framework:** see the command table (a PR with the new `framework.lock`).

## Uninstalling

Only here, at the console, with the admin: follow `docs/uninstall.md` stage by stage,
asking before each one. The whole lab, one host, or one agent.

## Diagnosis

- **See what an agent is doing or did:** its log in the web UI (`/logs/<agent>`: admins all; members their own agents, with
  `tailscale_login` in `lab.yaml`)
  or `sudo lab logs <agent>` on its host; the service itself: `sudo journalctl -u
  lab-worker@<id>` (or `lab-manager@<id>`, `lab-secretary`, `lab-broker`, `lab-intake`;
  the intake runs only while a one-time code is open, started by `lab-intake.path`).
- **Talk to an agent directly:** `sudo lab debug <agent>`
  stops its service and opens its latest session in the classic Claude Code console, as
  the agent's user with its sandbox; the service restarts on exit. `--print "question"`
  asks one question without opening the console; `--new` starts a fresh session.

- **Sandbox won't start, `nested userns is capability-restricted`:** the host is missing
  the AppArmor fix. Re-run `setup-sandbox.sh`.
- **No GPU inside the sandbox:** check `/etc/claude-code/managed-settings.json` points
  `sandbox.bwrapPath` at `/usr/local/libexec/agentic-lab/bwrap-gpu`.
- **`cuInit` returns 304 inside the sandbox:** the agent's settings lack
  `allowAllUnixSockets` (set automatically for agents with `gpu: true`).
- **A host on AWS (EC2)** (non-GPU: the secretary, managers): `provider: aws,
  idle_stop_min: 0, ssh_user: ubuntu` in `lab.yaml`; no power control (start/stop in the
  AWS console). Advise a public IPv4 with a security group allowing no inbound at all
  (outbound is all a host needs; a VM without a public IP needs a NAT gateway, ~$32/month
  plus per-GB) and an EBS root volume (instance store is wiped on every stop).
- **Someone on a host wants to see an agent's files without sudo:** add their Linux user to
  the host's `readers:` in `lab.yaml` (PR → merge → deploy); they then read the agents'
  working folders and their `tmp` (never secrets or state), after logging in again.
- **A long run** (a worker's job that outlives its session, approved by a human each time):
  `systemctl list-units 'lab-run-*'` on the host; its log is in the worker's
  `~/work/runs/<name>-<id>/log` (`sudo cat`); stop one with
  `sudo systemctl stop lab-run-<agent>@<id>` (or cancel its task: that stops its runs).
  A run that won't start: `lab doctor` (its unit template and sudo rule come from
  `lab apply`). A run has the same filesystem rules as the agent's sandboxed commands
  (writes only in the working folder, /tmp and write grants): docs/long-runs.md lists what
  that means and what doesn't work in runs.
- **The secretary refuses to power a machine on or off:** a machine "dedicated to" a
  member (every agent on it theirs and `access: dedicated`) is powered on and off by that
  member or an admin, busy or not; any other machine (a shared agent, agents of more than
  one member, the secretary, or no agents) by an admin only (#91). The refusal names the
  cause. An idle machine is still stopped by the secretary after its `idle_stop_min`.
- **A machine wasn't stopped when idle** (or the secretary refuses to power it off): it's
  *busy*. The secretary says why: a worker task running, queued, awaiting approval or
  waiting on its long runs; a long run going; a
  process on a GPU (anyone's); a GPU above 0% in its last 5 readings (~75 s); someone's
  `~/.lab-keep-awake` (the web UI shows who); or a manager runs there and the host has
  `idle_stop_with_managers: false`; or, on a host with only managers, one is answering,
  had a turn recently, follows an unfinished task, has an approval pending or can't be
  reached (#70). Ask the person, or
  its member (if it's dedicated to them) or an admin powers it off anyway (`force_off`,
  after confirming). Managers don't keep a
  host on by default (it's judged by its workers alone; a manager there goes offline with
  it); remove `idle_stop_with_managers: false` from the host in `lab.yaml` (PR → merge →
  deploy) if the manager may go offline.
- **A host can't be reached from the sandbox:** use its tailnet name, not its IP; raw IPs
  are refused.
- Known limits and what was verified: `docs/security-notes.md`.
