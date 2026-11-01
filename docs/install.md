# Installing an Agentic Lab

Three steps by hand, then Claude guides the rest: the admin's own Claude Code session on
the secretary host, following the `lab-admin` skill (DESIGN.md §11a). Each step says who
acts:

- **Admin**: the human who runs the lab's infrastructure.
- **Member**: any human in the team.
- **Claude**: the admin's Claude Code session on the secretary host, in this repo.
- **Secretary**: the lab's 24/7 secretary agent, once it runs.

**The install has one human: the admin.** The lab starts with the admin as its only
member. Other members join afterwards, one at a time ("Add a member" below).

**Accounts the admin needs:** a Tailscale tailnet; a Nebius account with GPU quota; a
Slack workspace where they can create apps; a Claude seat (Team or Max) for each member
whose agents will run; a GitHub account (or organisation) for the lab's private
configuration repository.

**Two repositories:**
- **The framework** (this repository): code, read-only for the lab. Never edited on the
  secretary host.
- **The lab's configuration** (private, one per lab, e.g. `<org>/lab-config`): `lab.yaml`
  and `framework.lock`, which pins the framework to one commit. Every change to the lab is
  a commit here, so the lab's history is reviewable and a lost secretary host is rebuilt
  from it. It holds no secrets (those stay on the hosts).

On the secretary host the framework is checked out in `~/agentic-lab` (set up with
`uv venv && uv pip install -e .`) and the configuration in `~/lab-config`. Until the first
`lab apply`, `lab` means the checkout's own, `~/agentic-lab/.venv/bin/lab`, also under sudo.
`lab apply` then puts `lab` on every lab host's PATH (`/usr/local/bin/lab`). Without
`--lab`, it reads `./lab.yaml` if there's one in the current folder, else
`/etc/agentic-lab/lab.yaml` (the deployed one): that's what commands run as the lab's users
use. Pass `--lab` when in doubt.
**Always run `lab plan` / `lab apply` from the checkout, on the config repo's lab.yaml**,
so they ship the pinned code:
`cd ~/agentic-lab && sudo .venv/bin/lab --lab ~/lab-config/lab.yaml plan`.
Below, `lab` is short for `~/agentic-lab/.venv/bin/lab --lab ~/lab-config/lab.yaml`, run
from `~/agentic-lab`. Only the commands that read the lab's secrets or change hosts need
`sudo`: `plan`, `apply`, `doctor`, `slack-verify`, `github-app-check`, `nebius connect`.
Run `lab init` and `lab check` **without** sudo, so the files in `~/lab-config` stay yours.

**Only reviewed configuration is deployed.** `lab apply` fetches the config repo and
deploys `lab.yaml` and `framework.lock` **as they are on `main`**, never the working copy.
Every change goes: Claude edits on a branch → `lab plan --working-tree` (preview) → commit,
push → `lab pr-link` → Claude tells the admin "here is the PR, review and merge it" → once
merged, `sudo lab apply` deploys exactly that commit. Each host records which config and
framework commits it runs (`/etc/agentic-lab/applied.yaml`; shown by `lab doctor` and on the
web UI). Break glass: `sudo lab apply --unreviewed` deploys the working copy, and every host
records it as unreviewed until a reviewed apply replaces it.

**Framework pinning.** `lab apply` ships the framework checkout only if it is clean and at
the commit in `framework.lock` on `main`. To upgrade:
`git -C ~/agentic-lab fetch origin && git -C ~/agentic-lab checkout --detach origin/main`
(a deploy from Slack leaves the checkout at the pinned commit, not on a branch, so `git
pull` won't do), then `lab apply --update-framework` (it only rewrites `framework.lock` in
the working copy), then a PR as above.

**Claude Code's auto mode** may refuse `sudo lab apply` (it changes the system) until the
admin says so explicitly in the chat: "yes, run sudo lab apply". That's expected.

---

## By hand

1. **Allocate the secretary VM** (Admin). Small and always on, Ubuntu 24.04 or newer, on
   the tailnet. An on-prem box or a small cloud VM (on AWS: see "Hosts on AWS" below).
2. **Install Claude Code** (Admin) and log in with the admin's own account.
3. **Give Claude the first message** (Admin):
   > Set up an Agentic Lab on this machine: https://github.com/anatoli-starostin/agentic-lab

   Claude clones the repo, reads `BOOTSTRAP.md`, and takes over. (While the repository is
   private, Claude first asks the admin to add a read-only deploy key for this machine.)

## Guided by Claude

4. **The lab's configuration repository** (Admin creates it, Claude explains and connects)
   - Claude explains the two repositories (above), then asks the admin to create an
     **empty private** repository, e.g. `https://github.com/organizations/<org>/repositories/new`
     (or `https://github.com/new`), named `lab-config`.
   - Claude generates a deploy key for it on this machine
     (`ssh-keygen -t ed25519 -N "" -f ~/.ssh/lab_config_deploy`), shows the **public**
     half, and asks the admin to add it at
     `https://github.com/<org>/lab-config/settings/keys` with **Allow write access**
     ticked. Claude clones it to `~/lab-config` with that key (`core.sshCommand` set).
   - Suggest protecting `main` (Settings → Branches: require a pull request) **after** the
     first commit is pushed (step 5), so the first push can go straight to `main`.

5. **The lab's description** (Claude asks, Admin answers)
   - **Names first.** Claude asks how each agent should be called: the secretary, the
     admin's manager, each worker. That name is what people see in Slack and in the web
     UI. Claude never invents names, and derives the ids from them (`Flash` →
     `mgr-flash`); the admin may also pick ids. Avoid naming an agent "Admin" (people
     confuse it with the human admin) or after a Slack built-in ("Slackbot").
   - Then: the lab's name, the admin's member id and name, their Slack member id (Slack:
     profile → ⋯ → Copy member ID). Claude runs (the minimal lab: managers and workers
     are added afterwards, from Slack):
     `lab init ~/lab-config/lab.yaml --name ... --admin ... --admin-name ... --slack-user ... --display secretary=Iris`
     (To create the admin's manager and first workers right away, add
     `--manager mgr-flash --worker worker-a@gpu-a --display mgr-flash=Flash --display
     worker-a=Bolt`; their machines must then exist before step 11.)
   - `lab init` marks the admin `admin: true`, fills in their Tailscale login (for the
     web UI's logs) from this machine's owner, and writes `framework.lock`. The manager on
     another host (managers run on any host): `--manager-host <host>`. Optionally add an
     `announcements` channel.
     `lab check` must pass.
   - Claude commits both files and pushes them **directly to `main`**
     (`git push -u origin HEAD:main`; the one exception: the repo is empty and
     unprotected). From now on, every change is a PR.

6. **The secretary's own services** (Claude)
   - `sudo lab apply --host <secretary host> --allow-missing`: installs the framework in
     `/opt/agentic-lab`, creates the users `lab-secretary` and `lab-broker`, sets up the
     secret intake (it starts by itself whenever a one-time code is open, and stops when
     none is) and starts the secretary (state + web UI on `http://<secretary host>:8774/`).
     Services whose Slack app doesn't exist yet (the broker's, a manager's) are installed
     but held back, and listed; other missing secrets (Claude tokens) are listed too. The
     next steps provide them, and the final `lab apply` starts everything.
   - **How the secretary reaches the config repo**, one of:
     - *A deploy key* (default). This first deploy prints a public key for the secretary:
       the admin adds it to the config repo (Settings → Deploy keys) **with write access**
       (it pushes branches; branch protection keeps it off `main`). The next deploy clones
       the config repo for the secretary, and the admin opens its PRs from the links it
       gives.
     - *A GitHub identity* (when the organisation wants the lab's bot to open the PRs, or
       forbids deploy keys): `github: <account>` on the secretary in `lab.yaml`, the
       account in `github.accounts` (a bot account or a GitHub App, as for other agents).
       Its token: `lab secret request service:github-<account> github-token` (a
       fine-grained token limited to the config repo: Contents and Pull requests, read and
       write), or for an App the `github-app` key. No deploy key is made; the secretary
       clones over HTTPS itself and opens its PRs.

     From then on the lab can be administered from Slack (see "Afterwards").

7. **Slack apps** (Admin creates them, Claude guides; ~3 minutes each)
   - Two apps at install: **Lab Approvals** (the broker's) and **the secretary**.
     Managers (the admin's own included) and workers are best added afterwards, from
     Slack (see "Afterwards"); `lab init` without `--manager`/`--worker` gives that
     minimal lab. **One at a time**: finish, verify, then the next. For each app Claude
     puts in the chat, in this order:
     1. the manifest itself, pasted in full in a code block (`lab slack-manifest
        approvals | secretary | <manager id>`; a manifest holds no secrets);
     2. the link to create it: https://api.slack.com/apps?new_app=1
     3. the clicks below, as a numbered list;
     4. when the admin is at step 5 below (both tokens copied): the one-time link for its
        tokens, from
        `sudo -u lab-secretary lab secret request service:<broker|secretary|manager id> slack-app --url http://<secretary host>:8771`.
        It's active for 5 minutes; a missed one is simply requested again.
   - Admin, in the browser:
     1. *Create New App* → **From a manifest** → pick the workspace → *Next*.
     2. Select the **YAML** tab, delete what's there, paste the manifest → *Next* →
        *Create*.
     3. Left menu **Install App** → *Install to <workspace>* → *Allow*. Copy the **Bot
        User OAuth Token** (`xoxb-…`).
     4. Left menu **Basic Information** → *App-Level Tokens* → *Generate Token and
        Scopes* → any name (`socket`) → *Add Scope* → `connections:write` → *Generate*.
        Copy the token (`xapp-…`).
     5. Open the one-time link, paste both tokens, *Send*. Then tell Claude "done".
        **Never paste a token into the chat.**
     6. Optional, its icon: `lab slack-icon <agent> --claude` has Claude draw one from the
        agent's name (falling back to a pattern made from the name; without `--claude`,
        the pattern). The pattern is also at `http://<secretary host>:8774/icon/<agent>.png`
        once the secretary runs. *Basic Information* → *Display Information* → *App icon*
        → upload it. (After the install the secretary posts a drawn icon itself, and draws
        another on request.)
   - The secretary's app has **Interactivity** on (its manifest sets it): that's what makes
     its Deploy button work.
   - Claude checks it at once: `sudo lab slack-verify <broker|secretary|manager id>`
     asks Slack whether both tokens work and prints the app's name, workspace and App ID;
     for a manager, Claude adds `slack_app: <App ID>` to `lab.yaml` in the next PR. If
     a check fails, it says which token and how to fix it; a new one-time link replaces
     the tokens.

8. **The admin's Claude token** (Admin, once a year)
   - First on the admin's computer: Claude Code installed and logged in, on the tailnet,
     `python3`. Then `sudo -u lab-secretary lab secret request <admin> claude-token --url
     http://<secretary host>:8771` prints a one-time command, active for 5 minutes. The
     admin runs it on their own computer: it runs
     `claude setup-token` and sends the token straight to the lab, which checks it
     with Anthropic before storing it.
   - The token is a password. **Never paste it, or the terminal's output, into a chat**
     (including Claude's). If the script shows a token that doesn't look right (cut short,
     extra characters), answer `n` and paste it into the hidden prompt instead.

9. **VM power** (Admin logs in once, Claude does the rest)
   - Install the Nebius CLI for the admin, then one browser login:
     `nebius profile create lab-admin --endpoint api.nebius.cloud --federation-endpoint auth.nebius.com --no-browser --parent-id <project-id>`
     (open the printed link; on a remote host, forward its local port with `ssh -L`).
   - `sudo lab nebius connect --project <project-id> --forget-admin`: a service account
     that may only view, start and stop VMs (`compute.instance-power-operator`), its key
     generated on this host, the secretary's CLI profile; it lists the project's VMs.
   - Claude adds the GPU VMs to `lab.yaml` (`kind: cloud`, `provider: nebius`,
     `instance: <id>`, `idle_stop_min: 30` (required on cloud hosts; `0`: never stopped), `tailnet_name`), as a PR
     for the admin to merge. A host is idle-stopped when its workers are idle, even if
     a manager runs there too (it's offline in Slack until the host is powered on again);
     `idle_stop_with_managers: false` on the host makes its managers keep it on.
   - Fallback, an existing service account: `... secret request service:secretary nebius-sa`
     (a form with the service account id, key id and private key PEM).

10. **GPU hosts** (Admin + Claude, per host)
   - Create the VM (Nebius console, Ubuntu 24.04 or newer with NVIDIA drivers), join it to
     the tailnet, and let the admin SSH in with passwordless sudo. If the image's login user
     isn't the admin's name, add `ssh_user: <user>` to the host in `lab.yaml`.
   - **SSH must not wait for a browser.** With Tailscale SSH on and the tailnet's default
     SSH rule (action *check*), `ssh` to the host waits for a confirmation in a browser, so
     `lab apply` and the secretary's deploys hang. Either give the admin an *accept* rule
     in the tailnet's SSH policy, or leave Tailscale SSH off on lab hosts and use OpenSSH
     (key-only) over the tailnet.
   - **Let the deploy know the host's key.** Deploys ssh with `BatchMode=yes` and refuse an
     unknown host key, so once, as the admin on the secretary's host, `ssh
     <admin>@<tailnet_name> true` and check the key it shows (e.g. against `ssh-keyscan`
     over the cloud's private network, or the VM's console). A host that missed its first
     deploy gets it from the next catch-up.
   - **A worker without a GPU** (a CPU VM): set `gpu: false` on its agent, so its sandbox
     doesn't look for one and `lab doctor` doesn't report a missing GPU.
   - **Turn off what the image runs but the lab doesn't need.** Cloud GPU images may run
     services on all interfaces (e.g. `rpcbind` on port 111); sandboxed agents can reach
     every tailnet host, so disable them (`sudo systemctl disable --now rpcbind
     rpcbind.socket`) and check with `sudo ss -tlnp`.
   - **No public IP needed (Nebius).** Once a VM is on the tailnet it needs no public IP: on Nebius,
     a VM without one still reaches the internet through the default egress gateway.
     Better to create it without one: removing the public IP of a running VM left it
     without network until it was stopped and started.

    **Hosts on AWS (EC2)**, for ordinary non-GPU hosts (the secretary, managers):
   - In `lab.yaml`: `{kind: cloud, provider: aws, instance: <instance-id>, region:
     <region>, idle_stop_min: 0, ssh_user: ubuntu, tailnet_name: ...}` (`lab init
     --provider aws`, and `--secretary-provider aws` when the secretary's own host is an
     EC2 VM). `ubuntu` is the login user of the standard Ubuntu AMIs.
   - **Power.** The secretary starts and stops AWS hosts with `instance:` like Nebius ones
     (members from Slack, idle stop after `idle_stop_min`; never its own host). It uses
     **its own access key**, never an instance role, wherever it runs. Tag the lab's
     power-managed instances, create an IAM user (console: IAM → Users → Create user, no
     console access) with only this policy, and create an access key for it:
     ```json
     {"Version": "2012-10-17", "Statement": [
       {"Effect": "Allow", "Action": ["ec2:StartInstances", "ec2:StopInstances"],
        "Resource": "arn:aws:ec2:<region>:*:instance/*",
        "Condition": {"StringEquals": {"aws:ResourceTag/<tag-key>": "<tag-value>"}}},
       {"Effect": "Allow", "Action": "ec2:DescribeInstances", "Resource": "*"}]}
     ```
     Give the key to the secretary with `sudo -u lab-secretary lab secret request
     service:secretary aws-key` (a one-time link: access key id and secret), then `lab
     apply`: it's installed for the secretary's service only, hidden from its agent like
     the Nebius key. Record its rotation date in `lab.yaml` so the secretary warns before
     it's due: `credentials: [{name: aws-key, host: <secretary host>, expires:
     <YYYY-MM-DD>, scope: "secretary's AWS key (VM power)"}]`. `region:` is required on a
     powered AWS host. `lab apply` installs the AWS SDK (boto3, the `aws` extra) on the
     secretary's host when `lab.yaml` has AWS hosts; `lab doctor` there checks the key
     with `DescribeInstances` for each AWS host. The cloud's metadata service stays closed
     to every lab user.
   - **Stopping changes the public IPv4** (an auto-assigned one), which is fine: the lab
     reaches hosts by tailnet name. A stopped instance costs only its disk.
   - **A public IPv4 and a security group with zero inbound rules.** The host only needs
     outbound traffic (Tailscale, apt and pip, GitHub, the Anthropic API), and every real
     access goes through the tailnet, so nothing is exposed. A public IPv4 costs about
     $3.60 a month. Don't follow the "no public IP" advice above on AWS: there, a VM
     without one needs a **NAT gateway** to reach the internet, about $32 a month plus a
     charge per GB. That's the trap to avoid.
   - **The root volume must be EBS**, not instance store: instance-store data is wiped on
     every stop, which would destroy the agents' state, memory and warm-environment
     caches.

11. **Everything else** (Claude)
    - A GPU host that's stopped can't be set up: ask the secretary to power it on first
      ("power on gpu-a"), or start it in the Nebius console.
    - `sudo lab plan` (nothing missing?), then `sudo lab apply`: every host gets the
      framework, the sandbox setup (AppArmor, GPU passthrough), its agents' users,
      secrets and services. The tokens agents use to reach each other are generated and
      never shown.
    - `sudo lab doctor` on each host: every check passes, or says how to fix it.

12. **First task** (Admin)
    - If the install had no manager yet, add one from Slack first (see "Afterwards").
    - DM the manager: *"Run a quick GPU smoke test on worker-a and show me a loss
      curve."* Progress, the plot and a summary come back in Slack; any approval comes as a
      DM from Lab Approvals. The web UI shows it all.

## Afterwards: the lab is administered from Slack

After the install, the admin works with **the secretary in a Slack DM**; the console is for
emergencies. The rule is the same: only what's merged to the config repo's `main` is
deployed, and the secretary checks in code that the person asking is an admin.

Workers' long runs are held to the same rules as the agents' sandboxed commands (writes only
in the working folder, /tmp and write grants): tell
members who set up projects about [docs/long-runs.md](long-runs.md), which lists what
doesn't work in runs and the workaround for each.

- **Any change** (a member, an agent, a GPU machine, grants, names, a framework upgrade):
  tell the secretary. It reads `lab.yaml` from `main`, writes the change on a branch
  (checked), and replies with the diff and "here is the PR, review and merge it". After
  the merge, tell it so: it posts a **Deploy** button; only an admin's click deploys
  (`lab apply-main`, its one sudo command). The result is posted back in the DM.
- **Hosts that were stopped during a deploy** are skipped (not a failure) and caught up by
  the secretary when they're back on (`lab apply-main --catch-up`, the same deploy for the
  hosts that missed it), once their workers have no active tasks. Only hosts on an older
  commit are caught up: one you deployed something newer to by hand is left alone. It says
  "<host> is up (updated to <commit>)"; after 3 failed deploys it tells the admins and the
  web UI marks the host stale (it tries again a day later). `auto_catch_up: false` in
  `lab.yaml` turns this off.
- **Add a member:** their name and Slack member ID → PR → merge → deploy; the secretary
  then DMs them their Claude-token command.
- **Who talks to which manager:** every member may talk to any manager. A *dedicated*
  manager (`access: dedicated`, the default) does real work only for its own member;
  others are guests there: questions and discussion, no tasks, no tools. A *shared* manager
  works for everyone.
- **Add a manager:** its name → PR → merge → deploy (its service waits for its Slack
  app) → the secretary DMs you the app's manifest and the clicks → you create the app
  (~3 minutes, in the browser) → "ready" → it DMs you a one-time link for its tokens
  (active 5 minutes) → "done" → it checks the tokens and offers a redeploy, which starts
  the manager. One-time links always come by DM, never in a channel.
- **Add a GPU worker:** first create the VM (Nebius console, Ubuntu with NVIDIA drivers),
  join it to the tailnet and give the admin SSH with passwordless sudo (step 10); then tell
  the secretary its tailnet name (and Nebius instance id for power) → PR → merge → deploy.
- **GitHub for an agent:** each manager or worker may have one GitHub identity, of either
  kind (both can be used in one lab):
  - **A GitHub App** (simplest: nothing to create but the app; it appears as
    `<name>[bot]`): tell the secretary the app's name → it posts the steps (create the app
    in the organisation's settings, permissions, install it on the lab's repos) → at the
    key step you say "ready" and it DMs you a one-time link for the private key (active 5
    minutes) → you say "done" with the App ID → it checks the
    app and proposes the `lab.yaml` change → merge → deploy. Console:
    `sudo -u lab-secretary lab secret request service:github-<slug> github-app --url
    http://<secretary host>:8771`, `sudo lab github-app-check
    <slug> --app-id N` (prints the `lab.yaml` lines).
  - **A full bot account** (a GitHub user with its own email, e.g. `flash-bot`, added to
    the org with access to the lab's repos only): the secretary adds it to `lab.yaml` →
    PR → merge → deploy; then it posts how to make the bot's fine-grained token (contents,
    pull requests and issues of those repos); "ready" → a one-time link for it by DM
    (active 5 minutes); then a redeploy.
    Two traps of fine-grained tokens: they can't reach a repository owned by *another
    personal account*, even as a collaborator (only the bot's own and its organisations'
    repos), and an organisation's policy on fine-grained tokens can hide all its
    repositories from an otherwise approved token. Then use a classic token with the least
    scope (`public_repo` for public repos, `repo` for private). A GitHub App has neither
    problem.

  The agent then uses git and `gh` as that identity, and its prompt lists the repositories
  each of its identities can reach (an App: the repositories it's installed on; a bot: those
  its token reaches). If an agent says it can't reach a repository, give its identity access
  there (install the App on it, or widen the token); the list updates within the hour (an
  App) or 6 hours (a bot), or at the next restart. An agent can also have **several,
  picked by repository**, e.g. a lab identity by default and an open-source
  project's bot for that project:
  `github: {acme-flash: default, spikyclaudebot: [anatoli-starostin/spiky]}`. **Protect `main`** in each repo (pull requests only, reviews by human
  code owners, no bypass for bots): that is what keeps bots from approving or merging.
- **People working on a lab machine directly** (not through agents): tell them to create
  `~/.lab-keep-awake` while they need the machine (`echo "CPU sweep until Friday" >
  ~/.lab-keep-awake`) and delete it when done; otherwise an idle machine may be stopped
  under them. The web UI shows who keeps a machine awake. To let them **look at what its
  agents do** with ordinary tools (`ls`, `less`, `tail -f`, no `sudo`), list their Linux
  user names on the host in `lab.yaml` (`readers: [alice, bob]`), PR → merge → deploy:
  they can then read each worker's and manager's working folder (`~lab-<id>/work`,
  including what the agent writes later) and its temporary files (`~lab-<id>/work/tmp`,
  the agent's `TMPDIR` on such hosts), never its secrets or state, and never write. They
  may need to log in again for the group to apply.
- **Workers' warm environments**: a worker keeps what it builds in its working folder
  (`~lab-<id>/work`): tools' caches in `.cache/` (uv, pip, torch extensions, Triton,
  Hugging Face, CUDA), checkouts in `repos/`, virtual environments in `envs/`, listed in
  `ENVIRONMENTS.md`, so the next task on a project starts warm. It prunes caches and old
  environments when free disk falls below `keep_free_gb` (per worker in `lab.yaml`,
  default 30) and says what it removed. To wipe them: ask the worker in a task ("delete
  your caches and environments"), or at the console `sudo rm -rf ~lab-<id>/work/.cache
  ~lab-<id>/work/envs`.
- **Remove an agent:** PR → merge → deploy. Its Unix user and work folder stay on its host
  until removed at the console (`sudo userdel -r lab-<id>`, after archiving its work).
- **From the console** (break glass, or if Slack is down): the same flow with the admin's
  Claude: a branch → `lab pr-link` → merge → `sudo lab apply`.
- **Uninstalling** (the whole lab, a host or an agent): at the console only, with the
  admin's Claude, following `docs/uninstall.md`.
- **Watching and debugging:** the web UI (`:8774`; logs: admins all, members their own agents'); `sudo lab logs
  <agent>`; `sudo lab debug <agent>` (the agent's own session, in the classic console);
  `lab manager ask` / `lab secretary ask` (talk without Slack).
