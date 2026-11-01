"""`lab`: the command-line entry point.

    lab init [lab.yaml] ...     write a lab's first lab.yaml (flags, or questions)
    lab check [lab.yaml]        validate the lab registry
    lab settings <agent>        the agent's generated Claude Code settings (sandbox + grants)
    lab worker serve <agent>    run a worker's service (as its own Unix user, from systemd)
    lab secret request <human> <name> --url URL
                                ask a human for a secret: prints the message to send them
    lab intake serve            the secretary's endpoint where members' clients send secrets
    lab broker serve            the approval broker (on the secretary host, as lab-broker)
    lab secretary serve         the secretary's service: lab state, web UI, Slack
    lab secretary ask --as <member> "message"
                                one message to the secretary, answered on the console
    lab manager serve <agent>   run a manager's service (Slack + its agent)
    lab manager ask <agent> --as <member> "message"
                                one message to a manager, answered on the console
    lab doctor [--host H]       (sudo) check this host against lab.yaml, with fixes
    lab logs <agent> [--hours N] (sudo, on the agent's host) its event log
    lab plan [--host H]         what `lab apply` would do on each host; missing secrets
    lab apply [--host H]        (sudo, on the secretary host) make the hosts match lab.yaml
    lab nebius connect --project ID
                                (sudo) give the secretary VM power: after the admin's one
                                Nebius browser login, set up a narrowly scoped service account
    lab slack-manifest <agent>|approvals
                                the Slack app manifest for an agent (paste into api.slack.com)
    lab debug <agent>           (sudo) stop the agent's service and open its session in the
                                classic Claude Code console; restarts the service on exit
"""
import argparse
import json
import os
import sys

from . import registry
from .registry.lab import ROLES


def _load_lab(path):
    lab = registry.load(path)
    errors = [i for i in lab.issues if i.level == "error"]
    if errors:
        for i in errors:
            print(i, file=sys.stderr)
        sys.exit(1)
    return lab


def _n(k, word):
    if k == 1:
        return f"{k} {word}"
    return f"{k} {word[:-1]}ies" if word.endswith("y") else f"{k} {word}s"


def cmd_init(args):
    from . import lock
    from .apply import find_source
    from .init import run
    path = args.path or args.lab
    run(path, args)
    print(f"wrote {path}")
    try:
        commit = lock.write(path, find_source())
        print(f"wrote {lock.path_for(path)} (framework pinned to {commit[:10]})")
    except Exception as e:      # noqa: BLE001 - the lab.yaml is what matters here
        print(f"couldn't pin the framework ({e}); pin it with `lab apply --update-framework`")
    args.lab = path
    return cmd_check(args)


def cmd_check(args):
    lab = registry.load(args.lab)
    issues = registry.check(lab)
    for i in issues:
        print(i)
    n_err = sum(i.level == "error" for i in issues)
    n_warn = len(issues) - n_err
    a = [x for x in lab.agents.values() if isinstance(x, dict)]
    counts = [_n(len(lab.humans), "human")]
    counts += [_n(sum(x.get("role") == r for x in a), r) for r in ROLES]
    counts += [_n(len(lab.hosts), "host")]
    print(f"{lab.name or args.lab}: {', '.join(counts)} -- "
          f"{_n(n_err, 'error')}, {_n(n_warn, 'warning')}")
    return 1 if n_err else 0


def cmd_settings(args):
    lab = _load_lab(args.lab)
    if args.agent not in lab.agents:
        sys.exit(f"lab: no agent {args.agent!r} (have: {', '.join(lab.agents)})")
    print(json.dumps(lab.agent_settings(args.agent), indent=2))
    return 0


def cmd_worker_serve(args):
    import asyncio
    import logging

    from .worker.service import serve
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    names = [n for n in (args.name or "").split(",") if n]
    asyncio.run(serve(args.lab, args.agent, args.host, args.port, model=args.model,
                      names=names))
    return 0


def cmd_worker_run_exec(args):
    """A long run's unit (lab-run-<agent>@<run>.service): run it, as the agent's user."""
    import os

    from .registry import load
    from .worker.runs import RunStore, run_exec
    lab = load(args.lab)
    a = lab.agents.get(args.agent) or {}
    if a.get("role") != "worker":
        raise SystemExit(f"{args.agent!r} is not a worker")
    from . import github as GH
    workdir = os.path.expanduser(a.get("workdir", "~/work"))
    tmpdir = lab.agent_tmpdir(args.agent)
    # the agent's git setup, bound into the run read-only: git and gh must find it
    from . import member_env as ME      # its member's own keys (member_env.py), under the lab's
    env = {**ME.read(STATE), **GH.environment(lab, args.agent),
           **({"TMPDIR": tmpdir} if tmpdir else {})}
    return run_exec(RunStore(os.path.expanduser(f"{STATE}/runs"), workdir, args.agent),
                    args.run, env=env)


STATE = "~/.local/state/agentic-lab"


def cmd_secret_request(args):
    import os

    from .secrets.intake import Intake, form_url, message
    lab = _load_lab(args.lab)
    owner = args.human
    if owner.startswith("service:"):
        if args.name == "claude-token":
            sys.exit("lab: Claude tokens belong to members, not services")
    elif owner not in lab.humans:
        sys.exit(f"lab: no human {owner!r} in {args.lab}")
    from .secrets.intake import window
    intake = Intake(os.path.expanduser(args.state))
    code = intake.request(owner, args.name)
    if args.form or owner.startswith("service:"):
        print(form_url(code, args.url))
        print(window(intake.expires(code)))
    else:
        name = (lab.humans[owner] or {}).get("name", owner)
        print(message(name, code, args.url, args.name, until=intake.expires(code)))
    return 0


def cmd_intake_serve(args):
    import os

    import uvicorn

    from .secrets.intake import Intake, verify_claude_token, verify_github_token
    from .secrets.server import app
    intake = Intake(os.path.expanduser(args.state), verify={"claude-token": verify_claude_token,
                                                            "github-token": verify_github_token})
    server = uvicorn.Server(uvicorn.Config(app(intake, args.client), host=args.host,
                                           port=args.port, log_level="info"))
    if args.on_demand:              # lab-intake.path started it: stop when no code is open
        import asyncio

        async def until_idle():
            while not server.should_exit:
                await asyncio.sleep(10)
                if intake.close_if_idle():
                    server.should_exit = True

        async def main():
            watch = asyncio.create_task(until_idle())
            try:
                await server.serve()
            finally:
                watch.cancel()
        asyncio.run(main())
    else:
        server.run()
    return 0


def cmd_broker_serve(args):
    import asyncio
    import logging

    from .broker.service import serve
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(serve(args.lab, args.host, args.port))
    return 0


def cmd_manager_serve(args):
    import asyncio
    import logging

    from .manager.service import serve
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(serve(args.lab, args.agent, host=args.host, port=args.port))
    return 0


def cmd_slack_manifest(args):
    from . import slack_manifest as sm
    lab = _load_lab(args.lab)
    if args.agent == "approvals":
        print(sm.render(sm.approvals(lab, args.name or "Lab Approvals")))
    elif args.agent in lab.agents:
        try:
            print(sm.render(sm.for_agent(lab, args.agent)))
        except ValueError as e:
            sys.exit(f"lab: {e}")
    else:
        sys.exit(f"lab: no agent {args.agent!r}")
    return 0


def cmd_manager_ask(args):
    import asyncio
    import logging

    from .manager.service import ask
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(ask(args.lab, args.agent, args.as_human, " ".join(args.message),
                    wait_s=args.wait))
    return 0


def cmd_doctor(args):
    from .doctor import report, run
    text, failed = report(run(args.lab, host=args.host))
    print(text)
    return 1 if failed else 0


def cmd_plan(args):
    from . import configrepo as CR
    from .apply import ApplyError, describe, plan_all
    try:
        if args.working_tree:
            lab_path = args.lab
            print(f"config: the working copy ({args.lab}), as a preview\n")
        else:
            lab_path, info = CR.fetch_main(args.lab)
            print(f"config: {info['config_commit'][:10]} ({CR.BRANCH})"
                  + ("; your working copy differs (preview it: --working-tree)"
                     if info["differs"] else "") + "\n")
        _, _, plans = plan_all(lab_path, hosts=args.host or None)
    except CR.ConfigError as e:
        sys.exit(f"lab plan: {e}")
    except (ApplyError, PermissionError) as e:
        sys.exit(f"lab plan: {e} (run it with sudo on the secretary host)")
    print("\n\n".join(describe(p) for p in plans))
    return 1 if any(p["missing"] for p in plans) else 0


def cmd_apply(args):
    import os

    from . import configrepo as CR
    from . import lock
    from .apply import ApplyError, describe, find_source, has_work, plan_all, run_bundle
    from .doctor import this_host
    admin = args.admin or os.environ.get("SUDO_USER") or "root"
    try:
        source = args.source or find_source()
        if args.update_framework:       # a change like any other: it goes through a PR
            print(lock.check(args.lab, source, update=True))
            print("Nothing applied: commit framework.lock on a branch, open a pull request; "
                  "once it's merged, `sudo lab apply` deploys it.")
            return 0
        if os.geteuid() != 0:
            sys.exit("lab apply needs root (it reads the lab's secrets): sudo lab apply ...")
        if args.unreviewed:
            lab_path, info = args.lab, CR.working_tree_info(args.lab)
            print("!! --unreviewed: deploying the WORKING COPY, not a reviewed commit on "
                  f"{CR.BRANCH}. Every host will record it as unreviewed. Open a PR with these "
                  "changes as soon as you can.\n")
            print(lock.check(lab_path, source) + "\n")
        else:
            lab_path, info = CR.fetch_main(args.lab)
            print(f"config: {info['config_commit'][:10]} ({CR.BRANCH})")
            if info["differs"]:
                print("  note: your working copy differs from it; those changes are NOT "
                      "deployed until they're merged (preview: lab plan --working-tree)")
            if lock.read(lab_path) is None:
                raise CR.ConfigError(f"no framework.lock on {CR.BRANCH}: `lab apply "
                                     "--update-framework`, then a PR")
            print(lock.check(lab_path, source) + "\n")
        try:                        # what the secretary needs to deploy main itself
            top, rel = CR.repo_of(args.lab)
            self_admin = {"config_url": CR.git(top, "remote", "get-url", "origin").strip(),
                          "lab_rel": rel, "framework_source": os.path.abspath(source),
                          "framework_owner": admin}
        except CR.ConfigError:
            self_admin = None
    except (ApplyError, lock.LockError, CR.ConfigError) as e:
        sys.exit(f"lab apply: {e}")
    return deploy(lab_path, info, source, admin, args.host, args.allow_missing, self_admin)


def deploy(lab_path, info, source, admin, hosts=None, allow_missing=False, self_admin=None,
           only_behind=None, locked=False):
    """Ship `lab_path` (already the reviewed one) and framework `source` to the hosts.
    `only_behind(commit)`: a catch-up (#61): only hosts on a reviewed commit for which it
    says True (strictly older), and each host checks for active work before it applies;
    one line per host says what happened (`CATCH-UP <host> <outcome>`). `locked`: the
    caller holds the deploy lock."""
    import os

    from . import configrepo as CR
    from . import lock
    from .apply import (BUSY, SSH_FAILED, UNREACHABLE, ApplyError, applied_on, describe,
                        has_work, plan_all, run_bundle, secretary_host)
    from .doctor import this_host
    errors = [i for i in registry.check(registry.load(lab_path)) if i.level == "error"]
    if errors:                     # never deploy (or even plan) a lab.yaml lab check refuses
        sys.exit("lab apply: lab.yaml doesn't pass lab check:\n"
                 + "\n".join(f"  {i}" for i in errors))
    try:
        record = CR.applied_record(info, lock.current(source)[1], admin)
        lab, tokens, plans = plan_all(lab_path, hosts=hosts or None)
    except (ApplyError, lock.LockError) as e:
        sys.exit(f"lab apply: {e}")
    here, sec_host = this_host(lab), secretary_host(lab)[0]
    lock_fd = None if locked else deploy_lock()     # `locked`: the caller holds it
    if lock_fd is False:
        sys.exit("lab apply: a deploy is already running; try again when it's done")
    tokens.save()                  # generated tokens are kept before anything uses them
    failed, skipped = 0, []
    for p in plans:
        if not has_work(p):
            print(f"host {p['host']}: nothing to install (no agents there)\n")
            continue
        print(describe(p))
        if p["missing"] and not allow_missing:
            print("  skipped: secrets missing (or pass --allow-missing)\n")
            failed += 1
            continue
        if only_behind:
            cur = applied_on(p, local=(p["host"] == here), admin=admin)
            said = None
            if cur == UNREACHABLE:
                said, why = "unreachable", "skipped: unreachable"
                skipped.append(p["host"])
            elif cur == SSH_FAILED:
                said, why = "failed", "ssh failed (see above)"
                failed += 1
            elif (cur.get("config_commit"), cur.get("framework_commit")) == (
                    record.get("config_commit"), record.get("framework_commit")) \
                    and not cur.get("unreviewed"):
                if cur.get("member_env") == p.get("member_env"):
                    said, why = "current", "up to date"
                # else: the same commits, other member keys: deployed (member_env.py)
            elif not cur.get("config_commit"):
                pass        # never deployed (a new host, off or unreachable at its first deploy)
            elif cur.get("unreviewed") or not only_behind(cur.get("config_commit")):
                said, why = "ahead", "runs a newer or unreviewed commit: left alone"
            if said:
                print(f"  -> {why}\nCATCH-UP {p['host']} {said}\n", flush=True)
                continue
            p["catch_up"] = True                # it checks for active work itself
        p["lab_path"], p["source"] = os.path.abspath(lab_path), source
        p["applied"] = {**record, **({"member_env": p["member_env"]} if p.get("member_env")
                                     else {})}     # which member keys it got (member_env.py)
        if p["host"] == sec_host and self_admin:
            sec = secretary_host(lab)[1]
            p["self_admin"] = {**self_admin, "config_owner": lab.unix_user(sec),
                               # its GitHub identity reaches the config repo (no deploy key)
                               "github": lab.github_identities(sec)[0]}
        rc = run_bundle(p, local=(p["host"] == here), admin=admin)
        if rc == UNREACHABLE:
            print("  -> skipped: unreachable (stopped?); deploy again once it's on\n")
            skipped.append(p["host"])
            if only_behind:
                print(f"CATCH-UP {p['host']} unreachable", flush=True)
            continue
        if rc == BUSY and p.get("catch_up"):
            print(f"  -> not now: tasks are active there\nCATCH-UP {p['host']} busy\n",
                  flush=True)
            continue
        print(f"  -> {'done' if rc == 0 else f'FAILED (exit {rc})'}\n")
        if only_behind:
            print(f"CATCH-UP {p['host']} {'done' if rc == 0 else 'failed'}", flush=True)
        failed += rc != 0
    if skipped:
        print(f"not deployed (unreachable): {', '.join(skipped)}")
    return 1 if failed else 0


DEPLOY_LOCK = "/run/agentic-lab-deploy.lock"


def deploy_lock(path=DEPLOY_LOCK):
    """One deploy at a time (#61): a held lock (kept for the process's life), False if
    another deploy holds it, None where it can't be taken (not root)."""
    import fcntl
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    return fd


def cmd_apply_main(args):
    """The secretary's root command (sudoers): deploy the config repo's main. Starts the
    deployment as its own systemd unit (the secretary's service restarts during it) and
    prints the log's path. `--catch-up` (#61): deploy the commit of main this host runs to
    the hosts that run something else (they missed a deploy), nothing newer."""
    import datetime as dt
    import os
    import subprocess

    import yaml

    from . import configrepo as CR
    from . import lock
    from .selfadmin import APPLY_MAIN, LOG_DIR, RESULT
    if os.geteuid() != 0:
        sys.exit("lab apply-main: run it with sudo")
    try:
        with open(APPLY_MAIN) as f:
            cfg = yaml.safe_load(f) or {}
    except OSError:
        sys.exit(f"lab apply-main: no {APPLY_MAIN} (run sudo lab apply once from the console)")
    if not args.run:
        held = deploy_lock()
        if held is False:
            sys.exit("lab apply-main: a deploy is already running")
        if held:
            os.close(held)          # free again for the deploy itself
        os.makedirs(LOG_DIR, mode=0o750, exist_ok=True)
        import pwd
        try:                        # root and the secretary (it reads the result) only
            gid = pwd.getpwnam(cfg["config_owner"]).pw_gid
            os.chown(LOG_DIR, 0, gid)
            os.chmod(LOG_DIR, 0o750)
        except (KeyError, OSError):
            gid = None
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        log = os.path.join(LOG_DIR, f"apply-{stamp}.log")
        open(log, "w").close()
        os.chmod(log, 0o640 if gid is not None else 0o600)
        if gid is not None:
            os.chown(log, 0, gid)
        subprocess.run(["systemd-run", f"--unit=lab-apply-{stamp}", "--collect", "--quiet",
                        f"--property=StandardOutput=append:{log}",
                        f"--property=StandardError=append:{log}",
                        "/usr/local/bin/lab", "apply-main", "--run"]
                       + (["--catch-up"] if args.catch_up else []), check=True)
        print(f"started; log: {log}")
        return 0

    def done(ok):
        print(f"{RESULT} {'ok' if ok else 'failed'}", flush=True)
        return 0 if ok else 1
    held = deploy_lock()        # held from the fetch to the end (#61)
    if held is False:
        print("lab apply-main: a deploy is already running", flush=True)
        return done(False)
    os.environ["LAB_GIT_USER"] = cfg["config_owner"]       # its clone, its deploy key
    if cfg.get("github"):       # or its GitHub identity: the service's git setup (#97)
        from . import github as GH
        home = os.path.expanduser(f"~{cfg['config_owner']}")
        CR.GIT_ENV.update(GH.git_env(os.path.join(home, GH.SERVICE_ROOT.removeprefix("~/"))),
                          GIT_TERMINAL_PROMPT="0")
    config = os.path.join(os.path.expanduser(f"~{cfg['config_owner']}"),
                          ".local/state/agentic-lab/config-repo", cfg.get("lab_rel", "lab.yaml"))
    commit = None
    if args.catch_up:
        own = CR.applied_versions() or {}
        if not own.get("config_commit") or own.get("unreviewed"):
            print("lab apply-main --catch-up: this host doesn't run a reviewed commit of "
                  f"{CR.BRANCH}; nothing to catch up to", flush=True)
            return done(False)
        commit = own["config_commit"]
    try:
        lab_path, info = CR.fetch_main(config, commit)
        print(f"config: {info['config_commit'][:10]} ({CR.BRANCH})"
              + (" catching up the hosts that missed it" if commit else ""), flush=True)
        pin = (lock.read(lab_path) or {}).get("commit")
        if not pin:
            raise CR.ConfigError(f"no framework.lock on {CR.BRANCH}")
        src, owner = cfg["framework_source"], cfg["framework_owner"]
        as_owner = ["sudo", "-u", owner, "-H", "git", "-C", src]
        head = subprocess.run([*as_owner, "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
        if head != pin:                 # move the framework checkout to the pinned commit
            subprocess.run([*as_owner, "fetch", "--quiet", "origin"], check=True)
            subprocess.run([*as_owner, "checkout", "--quiet", "--detach", pin], check=True)
            print(f"framework checkout moved to {pin[:10]}", flush=True)
        print(lock.check(lab_path, src) + "\n", flush=True)
    except (CR.ConfigError, lock.LockError, subprocess.CalledProcessError, KeyError) as e:
        print(f"lab apply-main: {e}", flush=True)
        return done(False)
    try:
        rc = deploy(lab_path, {**info, "via": "catch-up" if commit else "slack"}, src, owner,
                    allow_missing=True,
                    self_admin={k: cfg[k] for k in ("config_url", "lab_rel", "framework_source",
                                                    "framework_owner")},
                    only_behind=(lambda old: CR.is_older(config, old, commit)) if commit
                    else None, locked=held is not None)
    except SystemExit as e:
        print(e, flush=True)
        rc = 1
    return done(rc == 0)


def cmd_secretary_serve(args):
    import asyncio
    import logging

    from .secretary.service import serve
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(serve(args.lab, args.host, args.port))
    return 0


def cmd_secretary_ask(args):
    import asyncio
    import logging

    from .secretary.service import ask
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(ask(args.lab, args.as_human, " ".join(args.message)))
    return 0


def cmd_logs(args):
    import datetime as dt
    import pwd

    from .eventlog import EventLog
    lab = _load_lab(args.lab)
    if args.agent not in lab.agents:
        sys.exit(f"lab: no agent {args.agent!r}")
    try:
        home = pwd.getpwnam(lab.unix_user(args.agent)).pw_dir
    except KeyError:
        sys.exit(f"lab logs: {lab.unix_user(args.agent)} isn't on this host")
    try:
        events = EventLog(f"{home}/.local/state/agentic-lab/events").read(
            since=dt.timedelta(hours=args.hours), limit=5000)
    except PermissionError:
        sys.exit("lab logs needs root: sudo lab logs ...")
    for ev in events:
        tag = ev.get("task") or ev.get("member") or ""
        print(f"{ev['at'][5:19].replace('T', ' ')}  {ev['kind']:<8} {tag:<28} "
              f"{ev.get('text', '').replace(chr(10), ' | ')[:300]}")
    return 0


def cmd_nebius_connect(args):
    import os

    from .nebius_connect import ConnectError, connect
    if os.geteuid() != 0 or not os.environ.get("SUDO_USER"):
        sys.exit("run it with sudo, as the admin: sudo lab nebius connect --project <id>")
    lab = _load_lab(args.lab)
    sec = next((a for a, x in lab.agents.items() if x.get("role") == "secretary"), None)
    if sec is None:
        sys.exit("lab.yaml has no secretary")
    try:
        connect(os.environ["SUDO_USER"], lab.unix_user(sec), args.project,
                forget_admin=args.forget_admin,
                instances={h: x["instance"] for h, x in lab.hosts.items()
                           if (x or {}).get("provider") == "nebius" and (x or {}).get("instance")})
    except ConnectError as e:
        sys.exit(f"lab nebius connect: {e}")
    return 0


def cmd_slack_icon(args):
    from .icon import png
    lab = _load_lab(args.lab)
    if args.agent in ("approvals", "broker"):
        name = "Lab Approvals"
    elif args.agent in lab.agents:
        name = lab.display_name(args.agent)
    else:
        sys.exit(f"lab slack-icon: no agent {args.agent!r} (or 'approvals')")
    out = args.output or f"{args.agent}-icon.png"
    if args.claude:
        import asyncio

        from .icon import drawn
        role = "approvals" if name == "Lab Approvals" else lab.agents[args.agent].get("role")
        data, how = asyncio.run(drawn(name, role, lab=lab.name))
    else:
        data, how = png(name), "a pattern from the name"
    with open(out, "wb") as f:
        f.write(data)
    print(f"wrote {out}: {name}'s icon, {how} (512x512). In the app's settings: Basic "
          "Information → Display Information → App icon.")
    return 0


def cmd_github_app_check(args):
    from . import github_app as GA
    from .apply import intake_secret
    key = intake_secret(f"service:github-{args.slug.lower()}", "github-app")
    if key is None:
        sys.exit(f"lab github-app-check: no private key for {args.slug} yet "
                 f"(lab secret request service:github-{args.slug.lower()} github-app)")
    try:
        info = GA.check(args.app_id, key)
    except Exception as e:
        sys.exit(f"lab github-app-check: the app doesn't work yet: {e}")
    print(f"{info['bot']}: installed on " + (", ".join(
        f"{i['account']} (id {i['id']}, {i['repositories']} repositories)"
        for i in info["installations"]) or "nothing yet (Install App → the lab's repos)"))
    print(f"for lab.yaml:\n  github:\n    accounts:\n      {info['slug']}: "
          f"{{app_id: {args.app_id}, email: \"{info['email']}\"}}")
    return 0 if info["installations"] else 1


def cmd_pr_link(args):
    from . import configrepo as CR
    try:
        top, _ = CR.repo_of(args.lab)
        branch = args.branch or CR.git(top, "branch", "--show-current").strip()
        if branch == CR.BRANCH:
            sys.exit(f"lab pr-link: you're on {CR.BRANCH}; changes go on a branch, then a PR")
        link = CR.pr_link(args.lab, branch)
    except CR.ConfigError as e:
        sys.exit(f"lab pr-link: {e}")
    print(link or "the config repo isn't on GitHub: open the pull request there by hand")
    return 0


def cmd_slack_verify(args):
    from .slack_verify import VerifyError, verify
    owner = args.owner if args.owner.startswith("service:") else f"service:{args.owner}"
    try:
        r = verify(owner)
    except (VerifyError, OSError) as e:
        sys.exit(f"lab slack verify: {e}")
    print(f"✓ bot token works: app {r['name']!r} in workspace {r['team']!r}, "
          f"App ID {r['app_id'] or 'unknown (the app lacks users:read; only managers need it)'}")
    print("✓ app-level token opens Socket Mode" if r["socket_ok"] else
          f"✗ app-level token: {r['socket_error']} (Basic Information → App-Level Tokens, "
          "scope connections:write; Socket Mode on)")
    agent = owner[8:]
    lab = _load_lab(args.lab)
    if lab.agents.get(agent, {}).get("role") == "manager" and r["app_id"]:
        print(f"  for lab.yaml (in the next PR): agents.{agent}.slack_app: {r['app_id']}")
    return 0 if r["socket_ok"] else 1


def cmd_debug(args):
    from .debug import debug
    debug(args.lab, args.agent, session=args.session, new=args.new, ask=args.print)
    return 0


def cmd_debug_session(args):
    from .debug import debug_session
    debug_session(args.lab, args.agent, session=args.session, new=args.new, ask=args.print)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lab", description="Agentic Lab command line")
    default_lab = "lab.yaml"
    if not os.path.exists(default_lab) and os.path.exists("/etc/agentic-lab/lab.yaml"):
        default_lab = "/etc/agentic-lab/lab.yaml"     # on a lab host
    ap.add_argument("--lab", default=default_lab,
                    help="path to lab.yaml (default: ./lab.yaml, else /etc/agentic-lab/lab.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="write a lab's first lab.yaml")
    i.add_argument("path", nargs="?", help="where to write it (default: --lab)")
    i.add_argument("--name", help="the lab's name (lowercase)")
    i.add_argument("--tailnet", help="tailnet DNS suffix (default: detected)")
    i.add_argument("--admin", help="the admin's member id")
    i.add_argument("--admin-name", help="the admin's name")
    i.add_argument("--slack-user", help="the admin's Slack member id (U...)")
    i.add_argument("--secretary-host", help="this machine's host id (default: its tailnet name)")
    i.add_argument("--secretary-name", help="this machine's tailnet name (default: detected)")
    i.add_argument("--manager", help="also create the admin's manager with this id")
    i.add_argument("--manager-host", help="the admin's manager's host: host[=tailnet-name] "
                   "(default: this machine; managers may run on any host)")
    i.add_argument("--tailscale-login", help="the admin's Tailscale login, for the web UI's "
                   "logs (default: this machine's owner)")
    i.add_argument("--worker", action="append", help="a worker: id@host[=tailnet-name] "
                   "(repeatable)")
    i.add_argument("--display", action="append", metavar="AGENT=NAME",
                   help="the name humans see for an agent (repeatable), e.g. mgr-ana=Flash")
    i.add_argument("--provider", choices=("nebius", "aws"), default="nebius",
                   help="the cloud of the manager's and workers' VMs")
    i.add_argument("--secretary-provider", choices=("nebius", "aws"),
                   help="this machine is a cloud VM there (default: an on-prem host)")
    i.add_argument("--force", action="store_true", help="overwrite an existing file")
    i.set_defaults(fn=cmd_init)

    c = sub.add_parser("check", help="validate lab.yaml")
    c.add_argument("path", nargs="?", help="path to lab.yaml (overrides --lab)")
    c.set_defaults(fn=cmd_check)

    st = sub.add_parser("settings", help="print an agent's generated Claude Code settings")
    st.add_argument("agent")
    st.set_defaults(fn=cmd_settings)

    wk = sub.add_parser("worker", help="the worker service")
    wsub = wk.add_subparsers(dest="wcmd", required=True)
    ws = wsub.add_parser("serve", help="run a worker's service")
    ws.add_argument("agent")
    ws.add_argument("--host", default="127.0.0.1", help="address to serve MCP on "
                    "(the host's tailnet IP in production)")
    ws.add_argument("--port", type=int, default=8770)
    ws.add_argument("--name", help="comma-separated host names managers use to reach this "
                    "worker, e.g. its tailnet name (besides --host)")
    ws.add_argument("--model", help="override the agent's model from lab.yaml")
    ws.set_defaults(fn=cmd_worker_serve)
    wr = wsub.add_parser("run-exec", help=argparse.SUPPRESS)    # the unit of a long run
    wr.add_argument("agent")
    wr.add_argument("run")
    wr.set_defaults(fn=cmd_worker_run_exec)

    sc = sub.add_parser("secret", help="secrets from humans")
    ssub = sc.add_subparsers(dest="scmd", required=True)
    sr = ssub.add_parser("request", help="ask a human for a secret (one-time code + message)")
    sr.add_argument("human", help="the owner: a human id, or service:<name> (e.g. service:broker)")
    from .secrets.intake import NAMES as SECRET_NAMES
    def secret_name(v):                 # one of the lab's, or a member's key env:<NAME>
        from .secrets.intake import known
        if not known(v):
            raise argparse.ArgumentTypeError(
                f"{v!r}: one of {', '.join(SECRET_NAMES)}, or env:<NAME> (a member's key)")
        return v
    sr.add_argument("name", type=secret_name, metavar="NAME",
                    help=f"{', '.join(SECRET_NAMES)}, or env:<NAME> (a member's own key)")
    sr.add_argument("--form", action="store_true",
                    help="print a one-time web-form link instead of the client command")
    sr.add_argument("--url", required=True, help="the intake's address as members reach it, "
                    "e.g. http://secretary.example.ts.net:8771")
    sr.add_argument("--state", default=STATE)
    sr.set_defaults(fn=cmd_secret_request)

    it = sub.add_parser("intake", help="the secret intake endpoint")
    isub = it.add_subparsers(dest="icmd", required=True)
    iserve = isub.add_parser("serve", help="serve the intake (as the secretary's user)")
    iserve.add_argument("--host", default="127.0.0.1", help="the tailnet IP in production")
    iserve.add_argument("--port", type=int, default=8771)
    iserve.add_argument("--state", default=STATE)
    iserve.add_argument("--on-demand", action="store_true",
                        help="stop once no one-time code is open (lab-intake.path restarts it)")
    iserve.add_argument("--client", default="/opt/agentic-lab/client/lab-member.py",
                        help="the member client file to serve")
    iserve.set_defaults(fn=cmd_intake_serve)

    bk = sub.add_parser("broker", help="the approval broker")
    bsub = bk.add_subparsers(dest="bcmd", required=True)
    bserve = bsub.add_parser("serve", help="run the broker")
    bserve.add_argument("--host", default="127.0.0.1", help="the tailnet IP in production")
    bserve.add_argument("--port", type=int, default=8772)
    bserve.set_defaults(fn=cmd_broker_serve)

    se = sub.add_parser("secretary", help="the secretary service")
    sesub = se.add_subparsers(dest="secmd", required=True)
    seserve = sesub.add_parser("serve", help="run the secretary's service")
    seserve.add_argument("--host", default="127.0.0.1", help="the tailnet IP in production")
    seserve.add_argument("--port", type=int, default=8774)
    seserve.set_defaults(fn=cmd_secretary_serve)
    seask = sesub.add_parser("ask", help="one message to the secretary, on the console")
    seask.add_argument("message", nargs="+")
    seask.add_argument("--as", dest="as_human", required=True, help="the member asking")
    seask.set_defaults(fn=cmd_secretary_ask)

    mg = sub.add_parser("manager", help="the manager service")
    msub = mg.add_subparsers(dest="mcmd", required=True)
    mserve = msub.add_parser("serve", help="run a manager's service")
    mserve.add_argument("agent")
    mserve.add_argument("--host", help="serve the status endpoint here (the tailnet IP)")
    mserve.add_argument("--port", type=int, default=8773)
    mserve.set_defaults(fn=cmd_manager_serve)
    mask = msub.add_parser("ask", help="one message to a manager, answered on the console")
    mask.add_argument("agent")
    mask.add_argument("message", nargs="+")
    mask.add_argument("--as", dest="as_human", required=True, help="the member asking")
    mask.add_argument("--wait", type=int, default=900,
                      help="seconds to follow the tasks it submits (default 900)")
    mask.set_defaults(fn=cmd_manager_ask)

    pl = sub.add_parser("plan", help="what lab apply would do; missing secrets")
    pl.add_argument("--host", action="append", help="only this host (repeatable)")
    pl.add_argument("--working-tree", action="store_true",
                    help="preview the working copy (e.g. a PR's branch), not main")
    pl.set_defaults(fn=cmd_plan)
    ap_ = sub.add_parser("apply", help="make the hosts match lab.yaml (sudo, secretary host)")
    ap_.add_argument("--host", action="append", help="only this host (repeatable)")
    ap_.add_argument("--admin", help="SSH user for remote hosts (default: the sudo user)")
    ap_.add_argument("--source", help="the framework's source tree (default: detect)")
    ap_.add_argument("--allow-missing", action="store_true",
                     help="apply hosts even if some of their secrets are missing")
    ap_.add_argument("--update-framework", action="store_true",
                     help="move the framework pin (framework.lock, in the working copy) to "
                     "this checkout; deploys nothing (the pin goes through a PR)")
    ap_.add_argument("--unreviewed", action="store_true",
                     help="break glass: deploy the working copy instead of main (recorded "
                     "on every host as unreviewed)")
    ap_.set_defaults(fn=cmd_apply)
    am = sub.add_parser("apply-main", help="deploy the config repo's main (the secretary's "
                        "one sudo command; started from Slack by an admin)")
    am.add_argument("--run", action="store_true", help=argparse.SUPPRESS)
    am.add_argument("--catch-up", action="store_true",
                    help="only the hosts that missed the deploy this host runs (#61)")
    am.set_defaults(fn=cmd_apply_main)

    lg = sub.add_parser("logs", help="an agent's event log (sudo, on its host)")
    lg.add_argument("agent")
    lg.add_argument("--hours", type=float, default=24)
    lg.set_defaults(fn=cmd_logs)

    dr = sub.add_parser("doctor", help="check this host against lab.yaml (run with sudo)")
    dr.add_argument("--host", help="which lab.yaml host this is (default: detect)")
    dr.set_defaults(fn=cmd_doctor)

    nb = sub.add_parser("nebius", help="Nebius (VM power)")
    nbsub = nb.add_subparsers(dest="nbcmd", required=True)
    nc = nbsub.add_parser("connect", help="set up the secretary's VM power (sudo)")
    nc.add_argument("--project", required=True, help="the Nebius project id (project-...)")
    nc.add_argument("--forget-admin", action="store_true",
                    help="remove the admin's own Nebius login afterwards")
    nc.set_defaults(fn=cmd_nebius_connect)

    sv = sub.add_parser("slack-verify", help="check a Slack app's tokens right after they arrive "
                        "(sudo, secretary host)")
    sv.add_argument("owner", help="broker, secretary or a manager's id")
    sv.set_defaults(fn=cmd_slack_verify)

    si = sub.add_parser("slack-icon", help="an icon for an agent's Slack app, generated "
                        "from its name")
    si.add_argument("agent", help="a manager, the secretary, or 'approvals'")
    si.add_argument("-o", "--output", help="the PNG to write (default: <agent>-icon.png)")
    si.add_argument("--claude", action="store_true",
                    help="have Claude draw it (falls back to the pattern if that fails)")
    si.set_defaults(fn=cmd_slack_icon)

    ga = sub.add_parser("github-app-check", help="check a GitHub App identity works and print "
                        "its lab.yaml lines (sudo, secretary host)")
    ga.add_argument("slug", help="the app's slug (its name, lowercase, '-' for spaces)")
    ga.add_argument("--app-id", type=int, required=True)
    ga.set_defaults(fn=cmd_github_app_check)

    pr = sub.add_parser("pr-link", help="the link to open a pull request for the config "
                        "repo's current branch (push it first)")
    pr.add_argument("branch", nargs="?", help="default: the current branch")
    pr.set_defaults(fn=cmd_pr_link)

    sm = sub.add_parser("slack-manifest", help="print a Slack app manifest")
    sm.add_argument("agent", help="a manager or the secretary, or 'approvals'")
    sm.add_argument("--name", help="the approvals app's name (default: Lab Approvals)")
    sm.set_defaults(fn=cmd_slack_manifest)

    for name, fn, hlp in (
            ("debug", cmd_debug, "talk to an agent in the classic Claude Code console (sudo)"),
            ("debug-session", cmd_debug_session, argparse.SUPPRESS)):
        d = sub.add_parser(name, help=hlp)
        d.add_argument("agent")
        d.add_argument("--session", help="open this session instead of the latest task's")
        d.add_argument("--new", action="store_true", help="start a fresh session")
        d.add_argument("--print", metavar="QUESTION",
                       help="ask the session one question, non-interactively")
        d.set_defaults(fn=fn)

    args = ap.parse_args(argv)
    if getattr(args, "path", None) and args.cmd == "check":
        args.lab = args.path
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
