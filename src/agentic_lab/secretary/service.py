"""`lab secretary serve`: the secretary's service (DESIGN.md §4.1).

Collects the lab's state every few seconds and serves the web UI on the tailnet. With its
Slack app configured, it also talks in Slack (DMs, and channel threads it's tagged in),
asks members for secrets, and posts notices (expiring credentials) to the lab's
announcements channel. Secrets, readable by this service only:

    ~/.config/agentic-lab/monitor.yaml        {worker-id: read-only token}
    ~/.config/agentic-lab/broker.yaml         {url, token} (read-only overview)
    ~/.config/agentic-lab/slack.env           its Slack app (optional)
    ~/.config/agentic-lab/claude-oauth-token  the seat token, for its chat agent
"""
import asyncio
import json
import re
import logging
import os
import tempfile

import uvicorn
import yaml

from ..registry import load as load_lab
from ..worker.service import _read_secret
from ..manager.workers import Workers
from ..broker.service import _env_file
from ..manager.state import ManagerState
from ..secrets.intake import CODE_MINUTES, PREREQUISITES, Intake, form_url, message, window
from ..slackio import SILENT, SlackIO
from .brain import SecretaryBrain, expiry_notices
from ..eventlog import EventLog
from .. import github as GH
from .. import selfadmin as SA
from ..worker.usage import UsageLog, probe_loop, probe_now
from . import home as H
from . import onboarding as O
from . import catchup as CU
from . import overview as OV
from . import power as P
from . import visibility as V
from .status import check_seat, collect, manager_status
from .web import app, own_entries, timeout_text
import datetime as dt
from types import SimpleNamespace

CONFIG = "~/.config/agentic-lab"
STATE = "~/.local/state/agentic-lab"
COLLECT_EVERY_S = 15
CHECK_MIN_GAP_S = 60            # the web UI's "check now": at most once a minute per seat
log = logging.getLogger("agentic_lab.secretary")


INTAKE_PORT = 8771
DASHBOARD_PORT = 8774
HOME_REFRESH_ROUNDS = 20        # Home tabs are republished every ~5 min (20 x 15 s)


class IntakeDown(RuntimeError):
    pass


async def intake_up(url, wait_s=15):
    """Does the intake answer (it starts within seconds of a code being opened)?"""
    from urllib.parse import urlparse
    u = urlparse(url)
    for _ in range(int(wait_s * 2)):
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(u.hostname, u.port or 80), 2)
            w.close()
            return True
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(0.5)
    return False


def sec_id_of(lab):
    return next(a for a, x in lab.agents.items() if x.get("role") == "secretary")


class Secretary:
    """The secretary's Slack side: answers members, asks them for secrets, posts notices."""
    via = "in Slack"            # how members reach it; the console says so (#41)
    onboardings = None          # newcomers being onboarded (onboarding.py), once serving

    def __init__(self, lab, latest, workers, brain, state, slack=None):
        self.lab, self.latest, self.workers = lab, latest, workers
        self.brain, self.state, self.slack = brain, state, slack
        self._locks = {}
        self.events = None
        self.home_viewers = set()
        self.dashboard_url = f"http://{lab.address(sec_id_of(lab))[0]}:{DASHBOARD_PORT}/"
        sec = next(a for a, x in lab.agents.items() if x.get("role") == "secretary")
        name, _ = lab.address(sec)
        self.intake_url = f"http://{name}:{INTAKE_PORT}"
        self.intake = Intake(os.path.expanduser(STATE))     # the same store the intake serves
        self.onboardings = O.Onboardings(f"{STATE}/onboarding.json")

    def ctx(self, human=None, conv=None):
        """The tools' view of the lab, for one turn on behalf of `human` (a lab member), in
        conversation `conv` (where a deploy button goes)."""
        who = (self.lab.humans.get(human) or {}).get("name") or human
        return SimpleNamespace(
            state=lambda: V.shape(self.latest["state"], self.lab, human),
            worker_tasks=lambda worker: self.worker_tasks(worker, human),
            task_result=lambda worker, task_id: self.task_result(worker, task_id, human),
            workers=self.workers,
            request_token=lambda member: self.request_token(member, asked_by=human),
            announce=self.announce, save=self.state.save,
            power=lambda host, action: self.power(host, action, f"asked by {who} {self.via}",
                                                  asked_by=human),
            host_of=lambda agent: P.host_of(self.lab, agent),
            member_secret=lambda action, name=None, member=None: self.member_secret(
                action, name, member or human, asked_by=human, conv=conv),
            power_history=lambda host=None, hours=48: self.power_history(host, hours),
            approval_timeout=lambda member=None, minutes=None, until=None:
                self.approval_timeout(member or human, minutes, until, asked_by=human),
            read_config=lambda: self.read_config(asked_by=human),
            propose_change=lambda summary, lab_yaml, framework_commit=None:
                self.propose_change(summary, lab_yaml, framework_commit, asked_by=human),
            offer_apply=lambda redeploy=False: self.offer_apply(asked_by=human, conv=conv,
                                                                redeploy=redeploy),
            request_slack_app=lambda agent, ready=False: self.request_slack_app(
                agent, asked_by=human, conv=conv, ready=ready),
            redraw_app_icon=lambda agent: self.redraw_app_icon(agent, asked_by=human,
                                                               conv=conv),
            slack_verify=lambda owner: self.slack_verify(owner, asked_by=human),
            request_github_token=lambda account, ready=False: self.request_github_token(
                account, asked_by=human, conv=conv, ready=ready),
            request_github_app=lambda name, ready=False: self.request_github_app(
                name, asked_by=human, conv=conv, ready=ready),
            check_github_app=lambda slug, app_id: self.check_github_app(slug, app_id,
                                                                        asked_by=human))

    # ── administering the lab from Slack (selfadmin.py) ──────────────────────

    def _admin_only(self, human, what):
        if not self.is_admin(human):
            return f"refused: only an admin ({self.admins()}) may {what}."
        return None

    def config_copy(self):
        """The config repo, as the secretary's GitHub identity if it has one (HTTPS, the
        service's token; it opens the PRs), else with its deploy key."""
        sa = SA.applied(SA.APPLY_MAIN)          # where lab.yaml sits in the config repo
        sec = sec_id_of(self.lab)
        agent = f"{self.lab.display_name(sec)} (secretary)"
        name = self.lab.github_identities(sec)[0]
        if name and sa.get("config_url"):
            # its git setup, where its agent can't read it (the service's alone)
            env = GH.env_for(self.lab, sec, root=GH.SERVICE_ROOT)
            return SA.ConfigCopy(
                sa.get("lab_rel") or "lab.yaml", agent=agent,
                author=(self.lab.github_login(name), self.lab.github_email(name)),
                github={"env": env, "url": sa["config_url"], "token_file": os.path.join(
                    os.path.expanduser(GH.SERVICE_ROOT), name, "token")})
        return SA.ConfigCopy(sa.get("lab_rel") or "lab.yaml", agent=agent, author=(
            f"{self.lab.display_name(sec)} (lab secretary)",
            f"{self.lab.unix_user(sec)}@agentic-lab"))

    async def read_config(self, asked_by=None):
        refused = self._admin_only(asked_by, "read the lab's configuration")
        if refused:
            return refused
        try:
            m = await asyncio.to_thread(self.config_copy().read_main)
        except SA.AdminError as e:
            return f"couldn't read the config repo: {e}"
        return (f"config repo main at {m['commit'][:10]}\n--- lab.yaml ---\n{m['lab.yaml']}"
                f"\n--- framework.lock ---\n{m.get('framework.lock') or '(none)'}")

    async def propose_change(self, summary, lab_yaml, framework_commit=None, asked_by=None):
        refused = self._admin_only(asked_by, "change the lab's configuration")
        if refused:
            return refused
        try:
            r = await asyncio.to_thread(self.config_copy().propose, summary, lab_yaml,
                                        framework_commit)
        except SA.AdminError as e:
            return f"not proposed: {e}"
        if self.events is not None:
            self.events.add("note", f"proposed {r['branch']}: {summary}", member=asked_by)
        if r.get("pr"):
            return (f"opened pull request {r['pr']} (branch {r['branch']}): give it to the "
                    f"admin to review and merge; you never merge.\ndiff:\n{r['diff'][:6000]}")
        return (f"pushed branch {r['branch']}. PR link (give it to the admin to open, review "
                f"and merge; you never merge): {r['link'] or '(not GitHub: open it by hand)'}\n"
                f"diff:\n{r['diff'][:6000]}")

    async def _dm(self, human, conv):
        """Where something for `human` alone goes (#63): a DM with them, never a channel.
        Returns (where to post, moved: whether that isn't this conversation), or None."""
        uid = (self.lab.humans.get(human) or {}).get("slack_user")
        if self.slack is None or not uid:
            return None
        im = await self.slack.web.conversations_open(users=uid)
        ch = im["channel"]["id"]
        if conv is not None and conv.channel == ch:
            return SimpleNamespace(channel=ch, thread_ts=conv.thread_ts), False
        return SimpleNamespace(channel=ch, thread_ts=None), True

    async def _link(self, owner, name):
        """A one-time link for a secret, and the line saying how long it's active. The
        intake starts on demand (#63): if it doesn't come up, say so (and tell the admins)
        rather than hand out a link that leads nowhere."""
        code = self.intake.request(owner, name)
        if not await intake_up(self.intake_url):
            await self.announce(":warning: the secret intake failed to start (sudo journalctl "
                                "-u lab-intake on the secretary host).")
            raise IntakeDown("the secret intake failed to start: the admins are told; try "
                             "again in a while")
        return form_url(code, self.intake_url), window(self.intake.expires(code))

    @staticmethod
    def _sent(what, moved):
        return what + (" in a DM to the admin (one-time links never go in a channel): say "
                       "here only that you sent them a DM" if moved else "")

    async def request_slack_app(self, agent, asked_by=None, conv=None, ready=False):
        """Hand an admin what they need to create a manager's (or the secretary's, or the
        approvals') Slack app: the manifest, the link, the clicks; the one-time link for
        its tokens when they're ready to paste them (`ready`: it's active 5 minutes, #63).
        In a DM with them (#63). Posted as is (no retyping by the model)."""
        refused = self._admin_only(asked_by, "set up Slack apps")
        if refused:
            return refused
        from .. import slack_manifest as M
        if agent in ("broker", "approvals"):
            owner, manifest = "service:broker", M.approvals(self.lab)
        elif self.lab.agents.get(agent, {}).get("role") in ("manager", "secretary"):
            owner = f"service:{agent}"
            manifest = M.for_agent(self.lab, agent)
        else:
            return (f"{agent!r} isn't a manager or the secretary in the deployed lab.yaml: "
                    "propose it, have it merged, deploy it, then ask again")
        if self.slack is None or conv is None:
            return "this needs Slack; from the console: lab slack-manifest + lab secret request"
        where = await self._dm(asked_by, conv)
        if where is None:
            return "I can't DM you (no slack_user for you in lab.yaml)"
        dm, moved = where
        name = manifest["display_information"]["name"]
        if ready:
            try:
                link, note = await self._link(owner, "slack-app")
            except IntakeDown as e:
                return str(e)
            await self.slack.post(dm.channel, dm.thread_ts,
                                  f"*{name}*: paste both tokens here (never in Slack): {link}\n"
                                  f"{note}")
            return self._sent(f"sent the one-time link for {name}'s tokens", moved) + \
                ". When the admin says done: check_slack_app, then offer_deploy with redeploy."
        steps = (f"*Slack app for {name}*\n"
                 "1. Open https://api.slack.com/apps?new_app=1 → *From a manifest* → this "
                 "workspace → *Next*.\n"
                 "2. *YAML* tab, replace everything with the manifest below → *Next* → *Create*.\n"
                 "3. *Install App* → *Install to Workspace* → *Allow*. Copy the *Bot User "
                 "OAuth Token* (xoxb-…).\n"
                 "4. *Basic Information* → *App-Level Tokens* → *Generate Token and Scopes* → "
                 "any name → scope `connections:write` → *Generate*. Copy it (xapp-…).\n"
                 "5. Tell me *ready*: I send you a one-time link to paste both into (never "
                 "in Slack). It's active for 5 minutes, so ask when you have both.\n"
                 "6. Optional, its icon: *Basic Information* → *Display Information* → *App "
                 "icon*: upload the image I post below.\n"
                 "Then tell me \"done\": I check the tokens and offer the deploy that starts it.")
        await self.slack.post(dm.channel, dm.thread_ts, steps)
        await self.slack.post(dm.channel, dm.thread_ts, f"```{M.render(manifest)}```")
        role = ("approvals" if owner == "service:broker"
                else self.lab.agents.get(agent, {}).get("role", ""))
        await self.post_icon(name, role, dm, "The app icon (step 6)")
        return self._sent(f"posted the steps, the manifest and the icon for {name}'s app",
                          moved) + (". When the admin says ready: request_slack_app again "
                                    "with ready=true (the one-time link).")

    async def request_github_token(self, account, asked_by=None, conv=None, ready=False):
        """How to make a GitHub bot account's token, and (`ready`) a one-time link for it,
        active 5 minutes; in a DM with the admin (#63)."""
        refused = self._admin_only(asked_by, "set up GitHub bot accounts")
        if refused:
            return refused
        if account not in self.lab.github_accounts:
            return (f"{account!r} isn't in github.accounts of the deployed lab.yaml: propose "
                    "it, have it merged, deploy, then ask again")
        if self.slack is None or conv is None:
            return ("this needs Slack; from the console: lab secret request "
                    f"service:github-{account.lower()} github-token")
        where = await self._dm(asked_by, conv)
        if where is None:
            return "I can't DM you (no slack_user for you in lab.yaml)"
        dm, moved = where
        if ready:
            try:
                link, note = await self._link(f"service:github-{account.lower()}",
                                              "github-token")
            except IntakeDown as e:
                return str(e)
            await self.slack.post(dm.channel, dm.thread_ts,
                                  f"*{account}'s token*: paste it here (never in Slack): "
                                  f"{link}\n{note}")
            return self._sent(f"sent the one-time link for {account}'s token", moved) + \
                ". Then: the expiry date into lab.yaml (a PR), and offer_deploy with redeploy."
        await self.slack.post(dm.channel, dm.thread_ts, (
            f"*GitHub token for the bot {account}*\n"
            f"1. Log in to GitHub *as {account}* (a private window helps).\n"
            "2. https://github.com/settings/personal-access-tokens/new (fine-grained token).\n"
            "3. Resource owner: the organisation; Repository access: only the repos the "
            "lab's agents work on.\n"
            "4. Permissions: *Contents* read and write, *Pull requests* read and write, "
            "*Issues* read and write (Metadata read comes with them). Nothing else.\n"
            "5. Expiration: up to a year; tell me the date, I'll add it to lab.yaml so I can "
            "remind you before it runs out.\n"
            "   If the repos belong to *another person's account* (fine-grained tokens only "
            "reach the bot's own and its organisations' repos), or the organisation's policy "
            "hides its repos from fine-grained tokens: a classic token instead "
            "(https://github.com/settings/tokens/new) with only `public_repo` (public repos) "
            "or `repo` (private). A GitHub App avoids both.\n"
            "6. Tell me *ready* when you've copied it: I send you a one-time link to paste "
            "it into (never in Slack), active for 5 minutes.\n"
            "Then: offer a redeploy, so the agents that use this bot get it.\n"
            "On GitHub, protect each repo's main: require a pull request with a review from "
            "code owners (people only), and don't let bots bypass it."))
        return self._sent(f"posted how to make {account}'s token", moved) + (
            ". When the admin says ready: request_github_token again with ready=true.")

    async def request_github_app(self, name, asked_by=None, conv=None, ready=False):
        """Steps to create a GitHub App as an agent's GitHub identity, and a one-time link
        for its private key. lab.yaml can name the app only once it exists (its App ID):
        after "done", check_github_app, then propose the change."""
        refused = self._admin_only(asked_by, "set up GitHub identities")
        if refused:
            return refused
        slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
        if not slug:
            return "give the app a name (letters, digits, '-')"
        if self.slack is None or conv is None:
            return ("this needs Slack; from the console: lab secret request "
                    f"service:github-{slug} github-app")
        where = await self._dm(asked_by, conv)
        if where is None:
            return "I can't DM you (no slack_user for you in lab.yaml)"
        dm, moved = where
        if ready:
            try:
                link, note = await self._link(f"service:github-{slug}", "github-app")
            except IntakeDown as e:
                return str(e)
            await self.slack.post(dm.channel, dm.thread_ts,
                                  f"*{slug}[bot]'s private key*: paste the whole .pem here "
                                  f"(never in Slack): {link}\n{note}")
            return self._sent(f"sent the one-time link for {slug}'s key", moved) + (
                ". When the admin says done with the App ID: check_github_app, then "
                "propose_lab_change (github.accounts, the agent's github:), then offer_deploy.")
        await self.slack.post(dm.channel, dm.thread_ts, (
            f"*A GitHub App for an agent: {name}* (it will appear on GitHub as "
            f"`{slug}[bot]`)\n"
            "1. GitHub: your organisation → *Settings* → *Developer settings* → *GitHub Apps* "
            "→ *New GitHub App* (https://github.com/organizations/<org>/settings/apps/new).\n"
            f"2. *GitHub App name*: `{name}`. *Homepage URL*: anything (e.g. the lab's repo).\n"
            "3. *Webhook*: untick *Active*.\n"
            "4. *Repository permissions*: *Contents* read and write, *Pull requests* read and "
            "write, *Issues* read and write (Metadata read comes with them). Nothing else.\n"
            "5. *Only on this account* → *Create GitHub App*.\n"
            "6. On the app's page: note the *App ID*; then *Credentials* → *Key pairs* → "
            "*New key* (a .pem file downloads). Tell me *ready*: I send you a one-time link "
            "to paste its whole contents into (never in Slack), active for 5 minutes.\n"
            "7. *Install App* → your organisation → *Only select repositories*: the lab's repos.\n"
            "Then tell me \"done\" and the App ID: I check the app and propose the lab.yaml "
            "change.\n"
            "On GitHub, protect each repo's main: require a pull request with a review from "
            "code owners (people only), and don't let apps or bots bypass it."))
        return self._sent(f"posted the steps for the app {slug}[bot]", moved) + (
            ". When the admin is at step 6 and says ready: request_github_app again with "
            "ready=true (the link for its key).")

    async def check_github_app(self, slug, app_id, asked_by=None):
        refused = self._admin_only(asked_by, "set up GitHub identities")
        if refused:
            return refused
        slug = re.sub(r"[^a-z0-9]+", "-", str(slug).strip().lower()).strip("-")
        try:
            key = self.intake.load(f"service:github-{slug}", "github-app")
        except Exception as e:
            return f"not a usable app name ({e})"
        if key is None:
            return f"no private key for {slug} yet: the one-time link hasn't been used"
        from .. import github_app as GA
        try:
            info = await asyncio.to_thread(GA.check, int(app_id), key)
        except Exception as e:
            return f"the app doesn't work yet: {e}"
        if info["slug"] != slug:
            return (f"that App ID is the app {info['slug']!r}, not {slug!r}: the key and the "
                    "App ID must be the same app's")
        if not info["installations"]:
            return "the app works but isn't installed: step 7 (Install App → the lab's repos)"
        return (f"the app works: {info['bot']}, installed on "
                + ", ".join(f"{i['account']} ({i['repositories']} repositories)"
                            for i in info["installations"])
                + f". For lab.yaml: github.accounts.{slug}: {{app_id: {app_id}, email: "
                f"\"{info['email']}\"}}" + (" (and installation_id, since it's installed in "
                "several places: " + ", ".join(str(i["id"]) for i in info["installations"])
                + ")" if len(info["installations"]) > 1 else ""))

    async def post_icon(self, name, role, conv, comment):
        """Draw an app icon (Claude, or a pattern if that fails) and post it in `conv`."""
        try:
            from ..icon import drawn
            data, how = await drawn(name, role, env=getattr(self.brain, "env", None),
                                    lab=self.lab.name)
            # the bytes drawn, never a file (a path could be swapped before the upload)
            await self.slack.upload(conv.channel, conv.thread_ts, None,
                                    f"{name}-icon.png", f"{comment}: {how}. Want another? "
                                    "Just ask.", data=data)
            return f"posted {name}'s icon ({how})"
        except Exception as e:
            log.exception("posting the app icon failed")
            return f"couldn't post the icon: {e}"

    async def redraw_app_icon(self, agent, asked_by=None, conv=None):
        refused = self._admin_only(asked_by, "set up Slack apps")
        if refused:
            return refused
        if self.slack is None or conv is None:
            return "this needs Slack; from the console: lab slack-icon <agent> --claude"
        if agent in ("broker", "approvals"):
            name, role = "Lab Approvals", "approvals"
        elif agent in self.lab.agents:
            name, role = self.lab.display_name(agent), self.lab.agents[agent].get("role", "")
        else:
            return f"no agent {agent!r} in the deployed lab.yaml"
        return await self.post_icon(name, role, conv, "Another icon")

    async def offer_apply(self, asked_by=None, conv=None, redeploy=False):
        """Post a 'Deploy' button for main's current commit. Nothing is deployed until an
        admin clicks it (checked again on the click). `redeploy`: even if the hosts already
        run main (new secrets arrived: a held service can start)."""
        refused = self._admin_only(asked_by, "deploy the lab's configuration")
        if refused:
            return refused
        if self.slack is None or conv is None:
            return "deploying needs Slack (a button an admin clicks); from the console: sudo lab apply"
        current = SA.applied()
        try:
            main, todo = await asyncio.to_thread(self.config_copy().not_applied,
                                                 current.get("config_commit"))
        except SA.AdminError as e:
            return f"couldn't read the config repo: {e}"
        if not todo and not redeploy:
            return (f"nothing to deploy: the hosts already run main ({main[:10]}). If new "
                    "secrets arrived (a Slack app, a token), offer a redeploy.")
        text = (f"*Deploy the lab's configuration?* main at `{main[:10]}`; "
                + ("not deployed yet:\n" + "\n".join(f"• {line}" for line in todo[:15])
                   if todo else "already deployed: a redeploy, to install new secrets")
                + "\nServices restart; it takes a few minutes.")
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
                  {"type": "actions", "elements": [
                      {"type": "button", "style": "primary", "action_id": "lab_apply",
                       "text": {"type": "plain_text", "text": "Deploy"}, "value": main},
                      {"type": "button", "action_id": "lab_apply_cancel",
                       "text": {"type": "plain_text", "text": "Not now"}, "value": main}]},
                  {"type": "context", "elements": [{"type": "mrkdwn", "text":
                      f"Only an admin's click counts ({self.admins()})."}]}]
        await self.slack.post_blocks(conv.channel, conv.thread_ts, "Deploy the lab?", blocks)
        return ("posted a Deploy button in this conversation. Tell the admin to click it; "
                "you can't deploy by yourself.")

    async def on_action(self, slack_user, human, action_id, value, where):
        if action_id not in ("lab_apply", "lab_apply_cancel"):
            return

        async def say(text):
            await self.slack.web.chat_update(channel=where["channel"], ts=where["ts"],
                                             text=text, blocks=[{"type": "section", "text": {
                                                 "type": "mrkdwn", "text": text}}])
        if not self.is_admin(human):
            await self.slack.post(where["channel"], where.get("thread_ts") or where["ts"],
                                  f"<@{slack_user}> only an admin ({self.admins()}) may "
                                  "deploy; the button is still there for them.")
            return
        who = (self.lab.humans.get(human) or {}).get("name") or human
        if action_id == "lab_apply_cancel":
            await say(f"Not deployed ({who} said not now).")
            return
        try:
            main = (await asyncio.to_thread(self.config_copy().read_main))["commit"]
            if main != value:
                await say(f"main moved to `{main[:10]}` since this button was posted: ask "
                          "me again to deploy, so you see what's in it.")
                return
            log_path = await asyncio.to_thread(SA.start_apply)
        except SA.AdminError as e:
            await say(f"Couldn't deploy: {e}")
            return
        await say(f":rocket: Deploying `{value[:10]}` (started by {who}). I'll post the "
                  "result here.")
        if self.events is not None:
            self.events.add("note", f"deploy {value[:10]} started by {human}: {log_path}")
        self._pending_applies(add={"log": log_path, "channel": where["channel"],
                                   "thread_ts": where.get("thread_ts") or where["ts"],
                                   "commit": value, "by": who})

    def _pending_applies(self, add=None, remove=None):
        path = os.path.expanduser(f"{STATE}/pending-applies.json")
        try:
            with open(path) as f:
                items = json.load(f)
        except (OSError, ValueError):
            items = []
        if add:
            items.append(add)
        if remove:
            items = [i for i in items if i["log"] != remove]
        if add or remove:
            with open(path, "w") as f:
                json.dump(items, f)
        return items

    async def check_applies(self):
        """Post the result of each deployment that finished (also after this service was
        restarted by that very deployment)."""
        for item in self._pending_applies():
            r = SA.apply_result(item["log"])
            if r is None:
                continue
            ok, tail = r
            self._pending_applies(remove=item["log"])      # report once, whatever happens next
            try:
                if self.slack is not None:
                    await self.slack.post(
                        item["channel"], item["thread_ts"],
                        (":white_check_mark: Deployed" if ok else ":x: Deploy FAILED")
                        + f" `{item['commit'][:10]}` (started by {item['by']}).\n"
                        f"```{tail[-1200:]}```")
                if ok:
                    await self.announce(f":rocket: The lab now runs config "
                                        f"`{item['commit'][:10]}` (deployed by {item['by']}).")
            except Exception:
                log.exception("reporting deployment %s failed", item["log"])

    async def slack_verify(self, owner, asked_by=None):
        refused = self._admin_only(asked_by, "check the lab's Slack apps")
        if refused:
            return refused
        from ..slack_verify import VerifyError, verify
        try:
            r = await asyncio.to_thread(verify, owner if owner.startswith("service:")
                                        else f"service:{owner}")
        except (VerifyError, OSError) as e:
            return f"check failed: {e}"
        return (f"bot token works: app {r['name']!r} in {r['team']!r}, App ID {r['app_id']}; "
                f"app-level token: {'ok (Socket Mode)' if r['socket_ok'] else r['socket_error']}")

    def is_admin(self, human):
        return bool((self.lab.humans.get(human) or {}).get("admin"))

    def admins(self):
        return " or ".join((h or {}).get("name") or hid for hid, h in self.lab.humans.items()
                           if (h or {}).get("admin")) or "the lab's admin"

    def _power_refused(self, host, action, asked_by):
        """Who may power a machine on or off (#91): one dedicated to a member (P.owner),
        that member or an admin, busy or not; any other, an admin only."""
        if self.is_admin(asked_by):
            return None
        verb = "on" if action == "on" else "off"
        mine = P.owner(self.lab, host)
        if mine is None:
            return (f"refused: {host} isn't dedicated to one member "
                    f"({P.why_not_dedicated(self.lab, host)}), so only an admin "
                    f"({self.admins()}) may power it {verb}.")
        if asked_by == mine:
            return None
        name = (self.lab.humans.get(mine) or {}).get("name") or mine
        if verb == "on":
            return (f"refused: {host} is dedicated to {name}: only {name} or an admin "
                    f"({self.admins()}) may power it on.")
        stop = (self.lab.hosts.get(host) or {}).get("idle_stop_min")
        return (f"refused: {host} is dedicated to {name}: only {name} or an admin "
                f"({self.admins()}) may power it off. "
                + (f"I stop it myself once it has been idle for {stop} min."
                   if stop else "It has no idle stop (idle_stop_min: 0)."))

    async def power(self, host, action, why="asked in Slack", asked_by=None):
        """`asked_by`: the member asking (None: the secretary itself, e.g. an idle stop).
        Who may: _power_refused. A busy machine is powered off only with `force_off`, once
        the member asking has confirmed losing what it names."""
        if action in ("on", "off", "force_off") and asked_by is not None:
            refused = self._power_refused(host, action, asked_by)
            if refused:
                return refused
            busy = action == "off" and P.busy(self.lab, host, self.latest["state"])
            if busy:
                return (f"{host} is busy ({busy}): powering it off interrupts that and loses "
                        "its work. Tell them so, and once they confirm, use force_off.")
        try:
            if action == "status":
                return f"{host}: {await P.status(self.lab, host)}"
            if action == "on":
                await P.start(self.lab, host)
            else:
                await P.stop(self.lab, host)
        except P.PowerError as e:
            return f"couldn't: {e}"
        if self.events is not None:
            self.events.add("power", f"{host} {action} ({why})", host=host, action=action,
                            member=asked_by, why=why)
        verb = "on" if action == "on" else "off"
        await self.announce(f":electric_plug: {host} powering {verb} ({why}).")
        return f"{host}: powering {verb}; it takes a minute or two"

    async def member_secret(self, action, name, member, asked_by=None, conv=None):
        """A member's own keys for outside services (member_env.py): `list` (names and
        when received, never values), `set` (a one-time link in a DM to whoever asked, for
        the value), `delete`. A member manages their own; an admin anyone's. Delivered to
        the member's dedicated agents by the catch-up (a deploy of their hosts)."""
        from .. import member_env as ME
        if member not in self.lab.humans:
            return f"no member {member!r} in lab.yaml"
        if asked_by is not None and asked_by != member and not self.is_admin(asked_by):
            return (f"refused: only {member} themselves or an admin ({self.admins()}) may "
                    "manage their keys.")
        agents = ME.agents_of(self.lab, member)
        to = ", ".join(self.lab.display_name(a) for a in agents) or "no dedicated agents"
        if action == "list":
            have = self.intake.member_env_info(member)
            if not have:
                return f"{member} has no keys stored. They'd go to: {to}."
            return (f"{member}'s keys (values are never shown): "
                    + ", ".join(f"{n} (received {w or '?'})" for n, w in have)
                    + f". They go to: {to}.")
        if action not in ("set", "delete"):
            return "action must be list, set or delete"
        try:
            name = ME.check_name(name)
        except ME.MemberEnvError as e:
            return f"refused: {e}"
        delivery = (f"It reaches {to} with the next catch-up, in a few minutes (that "
                    "restarts those agents once they have nothing running); a stopped "
                    "machine gets it when it next starts.")
        own = [self.lab.display_name(a) for a in agents
               if self.lab.agents[a].get("host") == P.secretary_host_of(self.lab)]
        if own:                 # the catch-up never redeploys the secretary's own host
            delivery += (f" {', '.join(own)} run(s) on my own machine, which the catch-up "
                         "doesn't redeploy: an admin deploys it (offer_deploy).")
        if action == "delete":
            if not self.intake.delete_member_env(member, name):
                return f"{member} has no key {name}."
            if self.events is not None:
                self.events.add("member_env", f"{member}'s key {name} removed by {asked_by}",
                                member=member, by=asked_by)
            return f"removed {member}'s {name}. " + delivery.replace("It reaches", "Its removal reaches")
        if not agents:
            return (f"{member} has no dedicated agents, so a key would reach no one "
                    "(shared agents never get members' keys).")
        if self.slack is None or conv is None:
            return ("this needs Slack; from the console: lab secret request "
                    f"{member} env:{name}")
        where = await self._dm(asked_by or member, conv)
        if where is None:
            return "I can't DM you (no slack_user for you in lab.yaml)"
        dm, moved = where
        try:
            link, note = await self._link(member, f"env:{name}")
        except IntakeDown as e:
            return str(e)
        await self.slack.post(dm.channel, dm.thread_ts,
                              f"*{name}* for {member}'s agents ({to}): paste the value here "
                              f"(never in Slack): {link}\n{note}\nUse a key of its own for "
                              "the lab (one you can revoke), not a personal one.")
        if self.events is not None:
            self.events.add("member_env", f"{member}'s key {name}: link sent to {asked_by}",
                            member=member, by=asked_by)
        return (self._sent(f"sent the one-time link for {name}", moved)
                + ". Once it's submitted: " + delivery
                + " A key gives no network access by itself: if a sandboxed command needs "
                "to reach the service, that's a grant (an approval, or lab.yaml).")

    async def approval_timeout(self, member, minutes=None, until=None, asked_by=None,
                               call=None, tz_of=None):
        """A member's approval timeout (#77): shown, or set (`minutes`: a number, "none" or
        "default"; `until`: when the override ends). A member changes their own, an admin
        anyone's. It only ever denies, so it's changed here, not through lab.yaml."""
        if member not in self.lab.humans:
            return f"no member {member!r} in lab.yaml"
        body = None
        if until and minutes is None:
            return "`until` goes with `minutes`"
        if minutes is not None:
            if asked_by is not None and asked_by != member and not self.is_admin(asked_by):
                return (f"refused: only {member} themselves or an admin ({self.admins()}) may "
                        "change their approval timeout.")
            m = str(minutes).strip().lower()
            if m not in ("none", "default") and not m.isdigit():
                return "minutes must be a number, 'none' or 'default'"
            body = {"human": member, "by": asked_by or "the secretary", "until": None,
                    "timeout_s": None if m == "none" else m if m == "default" else int(m) * 60}
            if until:
                # the member's own time, from their Slack profile; the answer shows it in
                # both, so a mistake shows at once
                try:
                    u = dt.datetime.fromisoformat(until)
                except ValueError:
                    return "`until` must be a time like 2026-10-23T18:00"
                tz = await (tz_of or self.member_tz)(member)
                local = u if u.tzinfo else u.replace(tzinfo=tz or dt.timezone.utc)
                body["until"] = local.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
        try:
            r = await (call or _broker_timeouts)(body)
        except Exception as e:
            return f"couldn't: {e}"
        if "error" in r:
            return f"couldn't: {r['error']}"
        if body and self.events is not None:
            self.events.add("approval_timeout", f"approval timeout for {member} set to "
                            f"{minutes} by {body['by']}", member=member, by=body["by"])
        t = r.get(member) or {}
        tz = await (tz_of or self.member_tz)(member) if t.get("until") else None
        return (f"{member}'s approval timeout: {timeout_text(t, tz)}. It applies to new "
                "requests; pending ones keep theirs.")

    async def timeout_zones(self, state):
        """The members' Slack time zones beside their approval timeouts that end at a set
        time, for the web page (each looked up once)."""
        cache = self.__dict__.setdefault("_zones", {})
        for h, t in ((state or {}).get("approvals") or {}).get("timeouts", {}).items():
            if t.get("until"):
                if h not in cache:
                    z = await self.member_tz(h)
                    cache[h] = getattr(z, "key", None)
                t["tz"] = cache[h]

    async def member_tz(self, member):
        """A member's time zone from their Slack profile (users.info), or None."""
        from zoneinfo import ZoneInfo
        uid = (self.lab.humans.get(member) or {}).get("slack_user")
        if getattr(self, "slack", None) is None or not uid:
            return None
        try:
            tz = ((await self.slack.web.users_info(user=uid)).get("user") or {}).get("tz")
            return ZoneInfo(tz) if tz else None
        except Exception:
            return None

    def power_history(self, host=None, hours=48):
        """Recent power actions (who or what, when, why), oldest first: idle stops, members'
        requests in Slack or from the console, admins' force-offs."""
        if self.events is None:
            return "no event log here"
        rows = []
        for e in self.events.read(since=dt.timedelta(hours=hours), limit=2000):
            if e.get("kind") != "power":
                continue
            h = e.get("host") or (e.get("text") or "").split(" ", 1)[0]
            if host and h != host:
                continue
            rows.append(f"{e['at']}  {e.get('text')}")
        return "\n".join(rows) or f"no power actions{' on ' + host if host else ''} in the " \
                                   f"last {hours} h"

    async def worker_tasks(self, worker, human):
        """A worker's tasks as `human` may see them (#62): others' without their content."""
        tasks = await self.workers.call(worker, "list_tasks")
        if V.sees_all(self.lab, human):
            return tasks
        return [V.task_view(t, human) for t in tasks or []]

    async def task_result(self, worker, task_id, human):
        t = await self.workers.call(worker, "task_result", {"task_id": task_id})
        if V.sees_all(self.lab, human) or t.get("requester") == human:
            return t
        return {"error": f"task {task_id} is {t.get('requester')}'s: you can see its state "
                         f"({t.get('state')}) and owner, not its content"}

    async def request_token(self, member, asked_by=None):
        """A member's Claude-token command, by DM to them (#63). The member asking for it
        themselves gets it at once (active 5 minutes); asked by someone else (an admin, a
        notice), they get what to have ready and ask for the command when they are."""
        if member not in self.lab.humans:
            return f"no member {member!r} in lab.yaml"
        if asked_by is not None and asked_by != member and not self.is_admin(asked_by):
            return (f"refused: only {member} themselves or an admin ({self.admins()}) may "
                    "ask for their Claude token.")
        h = self.lab.humans[member] or {}
        name = h.get("name") or member
        if asked_by != member:
            text = (f"Hi {name}! Your lab agents need your Claude seat token. {PREREQUISITES} "
                    "When you have that, tell me *send my token command*: you get a one-time "
                    f"command, active for {CODE_MINUTES} minutes.")
            sent = "what to have ready; they ask for the command when they are"
        else:
            code = self.intake.request(member, "claude-token")
            if not await intake_up(self.intake_url):
                await self.announce(":warning: the secret intake failed to start (sudo "
                                    "journalctl -u lab-intake on the secretary host).")
                return "the secret intake failed to start: the admins are told; try again later"
            text = message(name, code, self.intake_url, until=self.intake.expires(code))
            sent = f"a one-time command (active {CODE_MINUTES} minutes)"
        if self.slack is None or not h.get("slack_user"):
            return f"no Slack for {member}; give them this message:\n{text}"
        im = await self.slack.web.conversations_open(users=h["slack_user"])
        await self.slack.post(im["channel"]["id"], None, text)
        return f"sent {member} a DM with {sent}"

    async def announce(self, text):
        channel = self.lab.data.get("announcements")
        if not channel or self.slack is None:
            return False
        await self.slack.post(channel.lstrip("#"), None, text)
        return True

    # ── onboarding a newcomer (onboarding.py) ───────────────────────────────────

    def onboarding_ctx(self, oid):
        """The onboarding tools' view: this one newcomer, nothing else of the lab."""
        def guard(fn):
            try:
                return fn()
            except O.OnboardingError as e:
                return f"refused: {e}"
        return SimpleNamespace(
            status=lambda: self.onboarding_status(oid),
            answer=lambda key, value: guard(lambda: (
                self.onboardings.answer(self.lab, oid, key, value),
                f"recorded {key}. " + self.onboarding_status(oid))[1]),
            confirm=lambda item: guard(lambda: (
                self.onboardings.confirm(self.lab, oid, item),
                f"confirmed {item}. " + self.onboarding_status(oid))[1]),
            token=lambda: self.onboarding_secret(oid, "claude-token"),
            key=lambda name: self.onboarding_secret(oid, f"env:{name}"),
            save=self.state.save)

    def onboarding_status(self, oid):
        rec = self.onboardings.get(oid)
        if rec is None:
            return f"no onboarding {oid!r}"
        checklist = O.items(self.lab, rec, self.intake)
        left = [i for i in checklist if not i.done]
        return (O.table(rec, checklist) + "\n"
                + (f"Next: {left[0].what}" if left else "All done: the admins are told."))

    async def onboarding_secret(self, oid, name):
        """A one-time command (the Claude token) or link (a key on the checklist), by DM to
        the newcomer: stored under the lab id they'll have, ready for their deploy."""
        rec = self.onboardings.active().get(oid)
        if rec is None:
            return "this onboarding isn't active"
        if name.startswith("env:") and name[4:] not in O.config(self.lab)["keys"]:
            return f"refused: {name[4:]} isn't on the checklist"
        if self.slack is None:
            return "no Slack: an admin runs lab secret request from the console"
        try:
            if name == "claude-token":
                code = self.intake.request(oid, name)
                if not await intake_up(self.intake_url):
                    await self.announce(":warning: the secret intake failed to start (sudo "
                                        "journalctl -u lab-intake on the secretary host).")
                    raise IntakeDown("the secret intake failed to start: the admins are "
                                     "told; try again in a while")
                text = message(rec.get("name") or oid, code, self.intake_url,
                               until=self.intake.expires(code))
            else:
                link, note = await self._link(oid, name)
                text = f"*{name[4:]}*: paste it here (never in Slack): {link}\n{note}"
        except IntakeDown as e:
            return str(e)
        im = await self.slack.web.conversations_open(users=rec["slack_user"])
        await self.slack.post(im["channel"]["id"], None, text)
        if self.events is not None:
            self.events.add("onboarding", f"{oid}: {name} link sent", member=oid)
        return "sent in this DM (a separate message): tell them to use it within 5 minutes"

    async def onboarding_turn(self, rec, conv, prompt):
        """One turn of the onboarding agent for this newcomer; returns its reply."""
        lock = self._locks.setdefault(conv.key, asyncio.Lock())
        async with lock:
            try:
                return await self.brain.turn(
                    self.onboarding_ctx(rec["id"]), conv, prompt,
                    onboarding=(rec.get("name") or rec["id"],
                                O.config(self.lab)["instructions"]))
            except Exception as e:
                log.exception("onboarding turn failed")
                return f"Sorry, something went wrong on my side ({type(e).__name__})."

    async def onboarding_tick(self):
        """Every round: greet newcomers just started, carry on when a secret arrives (it
        comes through the intake, not the chat), and tell the admins when one is done."""
        if self.slack is None:
            return
        for oid, rec in self.onboardings.all().items():
            if rec.get("dm") and (rec.get("finished") or rec.get("stopped")) and not rec.get(
                    "closed"):
                # their DM starts afresh: as a member later, not in the onboarding session
                self.state.conversation(rec["dm"], None).session_id = None
                self.state.save()
                self.onboardings.update(oid, lambda r: r.__setitem__("closed", True))
        active = self.onboardings.active()
        for u, h in list(self.slack.members.items()):
            if h not in self.lab.humans and h not in active:
                del self.slack.members[u]       # a stopped or finished onboarding: not a member
        for oid, rec in active.items():
            self.slack.members.setdefault(rec["slack_user"], oid)
            checklist = O.items(self.lab, rec, self.intake)
            done = {i.id for i in checklist if i.done}
            if rec.get("greeted") and done == set(rec.get("seen") or []) and not O.finished(
                    checklist):
                continue                        # nothing new: no Slack call, no write
            if not rec.get("dm"):
                im = await self.slack.web.conversations_open(users=rec["slack_user"])
                rec = self.onboardings.update(
                    oid, lambda r: r.__setitem__("dm", im["channel"]["id"]))
            conv = self.state.conversation(rec["dm"], None)
            arrived = [i for i in checklist if i.id in done - set(rec.get("seen") or [])
                       and (i.id == "claude-token" or i.id.startswith("key:"))]
            prompt = None
            if not rec.get("greeted"):
                prompt = ("[The onboarding just started. Greet them: who you are, that you'll "
                          "walk them through setting up, one step at a time, about 20 minutes "
                          "of their time; then the first step.]")
            elif arrived:
                prompt = ("[The lab just received: " + ", ".join(i.what for i in arrived)
                          + ". Tell them briefly it worked, then the next step.]")
            self.onboardings.update(oid, lambda r: (r.__setitem__("greeted", True),
                                                    r.__setitem__("seen", sorted(done))))
            if prompt:
                reply = await self.onboarding_turn(rec, conv, prompt)
                if reply and reply.strip() != SILENT:
                    await self.slack.post(conv.channel, None, reply)
            if O.finished(checklist):
                rec = self.onboardings.update(
                    oid, lambda r: r.__setitem__("finished", O._now()))
                await self.tell_admins(
                    f":white_check_mark: *{rec.get('name') or oid}* finished onboarding "
                    f"(lab id `{oid}`):\n```{O.table(rec, checklist)}```\nNext, for the "
                    f"admins: add them to lab.yaml, their machines and their manager's Slack "
                    f"app. Console: `lab onboarding status {oid}`.")
                if self.events is not None:
                    self.events.add("onboarding", f"{oid}: finished", member=oid)

    async def tell_admins(self, text):
        """A DM to every admin with a Slack user."""
        for hid, h in self.lab.humans.items():
            if (h or {}).get("admin") and (h or {}).get("slack_user") and self.slack:
                im = await self.slack.web.conversations_open(users=h["slack_user"])
                await self.slack.post(im["channel"]["id"], None, text)

    async def on_message(self, msg):
        rec = self.onboardings.active().get(msg.human) if self.onboardings else None
        if rec is not None and rec.get("slack_user") == msg.user:
            if not msg.private:
                reply = "Let's carry on in our DM :slightly_smiling_face:"
                if self.slack is not None:
                    await self.slack.post(msg.channel, msg.thread_ts, reply)
                return reply
            conv = self.state.conversation(msg.channel, msg.thread_ts)
            name = rec.get("name") or rec["id"]
            reply = await self.onboarding_turn(rec, conv, f"[{name}]: {msg.text}")
            if self.slack is not None:
                await self.slack.post(conv.channel, conv.thread_ts, reply)
            return reply
        conv = self.state.conversation(msg.channel, msg.thread_ts)
        lock = self._locks.setdefault(conv.key, asyncio.Lock())
        who = (self.lab.humans.get(msg.human) or {}).get("name") or msg.human
        prompt = "\n\n".join(filter(None, [
            P.layout(self.lab),         # never a remembered layout (#99)
            ("Earlier in this thread:\n" + "\n".join(msg.history)) if msg.history else "",
            f"[{who} ({msg.human})]: {msg.text}",
            "" if msg.mentioned else f"(Not addressed to you: answer only if useful from "
                                     f"you, otherwise reply exactly {SILENT}.)"]))
        async with lock:
            try:
                reply = await self.brain.turn(self.ctx(msg.human, conv), conv, prompt)
            except Exception as e:
                log.exception("secretary turn failed")
                reply = f"Sorry, something went wrong on my side ({type(e).__name__})."
        if self.slack is not None:
            await self.slack.post(conv.channel, conv.thread_ts, reply)
        return reply

    async def publish_home(self, slack_user):
        """Publish one person's Home tab (and remember them, to keep it fresh)."""
        if self.slack is None:
            return
        human_id = next((h for h, x in self.lab.humans.items()
                         if (x or {}).get("slack_user") == slack_user), None)
        if human_id is None:
            return
        self.home_viewers.add(slack_user)
        admin = bool((self.lab.humans[human_id] or {}).get("admin"))
        try:
            await self.slack.web.views_publish(user_id=slack_user, view=H.view(
                V.shape(self.latest["state"], self.lab, human_id), human_id,
                self.dashboard_url, admin=admin))
        except Exception:
            log.exception("publishing the Home tab for %s failed", human_id)

    async def notices(self):
        """Once a day: credentials expiring within 14 days, and GitHub identities that
        don't work, announced."""
        today = dt.date.today().isoformat()
        path = os.path.expanduser(f"{STATE}/noticed.json")   # survives restarts (deploys)
        try:
            with open(path) as f:
                done = {k: v for k, v in json.load(f).items() if k.endswith(today)}
        except (OSError, ValueError):
            done = {}
        broken = {}                 # identity -> (its problem, the agents using it)
        for aid, a in ((self.latest.get("state") or {}).get("agents") or {}).items():
            for ident, problem in (a.get("github") or {}).items():     # #64
                broken.setdefault(ident, (problem, []))[1].append(a.get("name") or aid)
        for ident, (problem, users) in broken.items():
            key = f"github:{ident}:{today}"     # once a day per identity, however shared
            if not done.get(key):
                await self.announce(f":warning: GitHub identity *{ident}* (used by "
                                    f"{', '.join(users)}) doesn't work: {problem}")
                done[key] = True
        for cred, left in expiry_notices(self.lab):
            key = f"{cred['name']}:{today}"
            if done.get(key):
                continue
            when = "expired" if left < 0 else f"expires in {left} days"
            await self.announce(f":hourglass: *{cred['name']}* {when} ({cred['expires']}).")
            done[key] = True
        try:
            with open(path, "w") as f:
                json.dump(done, f)
        except OSError:
            log.exception("remembering today's notices failed")


async def _broker_timeouts(body=None):
    """GET (body None) or POST the broker's members' timeouts, with the secretary's token."""
    import httpx2
    cfg = yaml.safe_load(_read_secret(f"{CONFIG}/broker.yaml") or "") or {}
    if not (cfg.get("url") and cfg.get("token")):
        raise RuntimeError("no broker configured for the secretary")
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {cfg['token']}"},
                                  timeout=10) as c:
        url = f"{cfg['url'].rstrip('/')}/v1/timeouts"
        r = await (c.post(url, json=body) if body else c.get(url))
        return r.json()


async def _activity(lab, manager, token):
    """A manager's activity right now (its status endpoint; #70)."""
    act = (await manager_status(lab, manager, token)).get("activity")
    if act is None:
        raise RuntimeError("it doesn't report its activity")
    return act


async def serve(lab_path, host, port):
    lab = load_lab(lab_path)
    mon = yaml.safe_load(_read_secret(f"{CONFIG}/monitor.yaml") or "") or {}
    role = lambda a: lab.agents.get(a, {}).get("role")     # noqa: E731
    workers = Workers({w: lab.worker_url(w) for w in mon if role(w) == "worker"},
                      {w: t for w, t in mon.items() if role(w) == "worker"}, timeout=15)
    managers = {m: t for m, t in mon.items() if role(m) == "manager"}
    bcfg = yaml.safe_load(_read_secret(f"{CONFIG}/broker.yaml") or "") or {}
    broker = (bcfg["url"], bcfg["token"]) if bcfg.get("url") and bcfg.get("token") else None
    events = EventLog(os.path.expanduser(f"{STATE}/events"))
    events.prune()
    usage = UsageLog(os.path.expanduser(f"{STATE}/usage.json"))
    sec_id = next(a for a, x in lab.agents.items() if x.get("role") == "secretary")
    logins = {(h or {}).get("tailscale_login"): hid for hid, h in lab.humans.items()
              if (h or {}).get("tailscale_login")}

    async def get_events(agent, hours, member=None):
        """An agent's events; with `member`, only theirs (their messages, their tasks)."""
        r = role(agent)
        if r == "worker":
            evs = (await workers.call(agent, "recent_events", {"hours": hours}))["events"]
        elif r == "manager" and agent in managers:
            evs = (await manager_status(lab, agent, managers[agent], hours))["events"]
        elif r == "secretary":
            import datetime as _dt
            evs = events.read(since=_dt.timedelta(hours=hours), limit=2000)
        else:
            raise ValueError(f"no log for {agent!r}")
        if member is None:
            return evs
        mine = set()
        if r == "worker":
            mine = {t["id"] for t in await workers.call(agent, "list_tasks", retries=0) or []
                    if t.get("requester") == member}
        return own_entries(evs, member, mine)

    def lab_host_ips():
        """The tailnet addresses of the lab's own hosts: agents run there, never admins."""
        import socket
        ips = set()
        for hid in lab.hosts:
            try:
                ips.add(socket.gethostbyname(lab.address_of_host(hid)))
            except OSError:
                pass
        return ips

    async def viewer(ip):
        """Who's behind this tailnet address? A person (not a tagged machine, not one of
        the lab's hosts) whose login is a member's tailscale_login in lab.yaml; else None."""
        if ip in lab_host_ips():
            return None
        try:
            p = await asyncio.create_subprocess_exec(
                "tailscale", "whois", "--json", ip, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 10)
            who = json.loads(out or b"{}")
        except (OSError, ValueError, asyncio.TimeoutError):
            return None
        if (who.get("Node") or {}).get("Tags"):
            return None
        return logins.get((who.get("UserProfile") or {}).get("LoginName"))
    here = {aid for aid, a in lab.agents.items()
            if a.get("host") == next((x.get("host") for x in lab.agents.values()
                                      if x.get("role") == "secretary"), None)}
    units = {aid: {"worker": f"lab-worker@{aid}", "manager": f"lab-manager@{aid}"}.get(
        lab.agents[aid].get("role")) for aid in here}
    units = {k: v for k, v in units.items() if v}
    snapshot_path = os.path.expanduser(f"{STATE}/lab-state.json")
    os.makedirs(os.path.dirname(snapshot_path), mode=0o700, exist_ok=True)
    latest = {"state": None}
    try:
        with open(snapshot_path) as f:
            latest["state"] = json.load(f)
    except (OSError, ValueError):
        pass

    # uvicorn handles SIGTERM/SIGINT (a graceful shutdown); when it returns, the rest stops
    stop = asyncio.Event()

    idle = P.IdleWatch(os.path.expanduser(f"{STATE}/idle.json"))     # kept across restarts
    catch = CU.CatchUp(os.path.expanduser(f"{STATE}/catch-up.json"))  # missed deploys (#61)
    sec_host = lab.agents[sec_id].get("host")
    hosts_power = {}
    down_noted = {}                 # host -> the day its agents being down was announced
    rounds = {"n": 0}

    async def check_power():
        """Every few rounds: the power state of the machines the secretary may power, and
        stopping the on-demand ones that have been idle long enough."""
        for host in lab.hosts:
            if not P.powerable(lab, host)[0]:
                continue
            try:
                hosts_power[host] = {"state": await P.status(lab, host),
                                     "at": dt.datetime.now(dt.timezone.utc).isoformat(
                                         timespec="seconds")}
            except P.PowerError as e:
                hosts_power[host] = {"state": "unknown", "error": str(e)[:200]}
        power = {h: v.get("state") for h, v in hosts_power.items() if v.get("state")}
        for host in idle.candidates(lab, latest["state"] or {"agents": {}}, power=power):
            if hosts_power.get(host, {}).get("state") != "RUNNING":
                continue
            # still idle right now? (a task may have arrived since the snapshot, #60)
            why = await P.recheck(lab, host, workers, latest["state"], manager_activity=(
                lambda m: _activity(lab, m, managers.get(m))))
            if why:
                log.info("idle stop of %s called off: %s", host, why)
                idle.idle_since.pop(host, None)
                continue
            result = await sec.power(host, "off",
                                     f"idle for {lab.hosts[host]['idle_stop_min']} min")
            if result.startswith(("couldn't", "refused")):   # retried next round
                log.warning("idle stop of %s failed: %s", host, result)
                await P.release(lab, host, workers)
                if sec.events is not None:
                    sec.events.add("power", f"{host} off failed: {result}", host=host,
                                   action="off", why="idle stop")
                await sec.announce(f":warning: couldn't stop idle {host}: {result}")
            else:
                idle.idle_since.pop(host, None)
        today = dt.date.today().isoformat()
        for host in idle.down_too_long():           # billed, and nothing can tell if idle
            if (hosts_power.get(host, {}).get("state") == "RUNNING"
                    and down_noted.get(host) != today):
                down_noted[host] = today
                await sec.announce(
                    f":warning: {host} is running, but its agents have been down or unreachable "
                    f"since {idle.down_since[host]:%Y-%m-%d %H:%M} UTC: it can't be "
                    "idle-stopped like this. Check it (lab doctor), or its member or an admin "
                    "powers it off.")
        idle.save()
        try:                    # hosts back from a stop that missed a deploy (#61)
            await CU.step(catch, lab, latest["state"] or {}, sec_host,
                          lambda: SA.start_apply(catch_up=True), SA.apply_result, sec.announce,
                          repo=os.path.expanduser(SA.CLONE),
                          refresh=lambda c: sec.config_copy().ensure_commit(c),
                          keys=member_keys())
        except Exception:
            log.exception("catching up hosts failed")
        await intake_watch()

    def member_keys():
        """{host: the revision of the member keys it should run} (member_env.py)."""
        from .. import member_env as ME
        return {h: ME.revision(lab, h, sec.intake.member_env) for h in lab.hosts}

    intake_failed = {"said": False}

    async def intake_watch():
        """The intake dying while a code is open strands that code: say so, once."""
        try:
            p = await asyncio.create_subprocess_exec(
                "systemctl", "is-failed", "--quiet", "lab-intake",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            failed = await asyncio.wait_for(p.wait(), 10) == 0
        except (OSError, asyncio.TimeoutError):
            return
        if failed and not intake_failed["said"]:
            log.error("the secret intake failed")
            await sec.announce(":warning: the secret intake stopped with an error; one-time "
                               "links handed out just before don't work: ask for new ones "
                               "(admins: sudo journalctl -u lab-intake on the secretary host).")
        intake_failed["said"] = failed

    async def collector():
        while not stop.is_set():
            try:
                prev = latest["state"]
                latest["state"] = await collect(lab, workers, broker, units, managers,
                                                own=(sec_id, usage))
                await sec.timeout_zones(latest["state"])
                P.note_gpu(latest["state"])     # a GPU is idle only after several 0% readings
                if rounds["n"] % 4 == 0:
                    await check_power()
                rounds["n"] += 1
                if rounds["n"] % HOME_REFRESH_ROUNDS == 0:
                    for user in list(sec.home_viewers):
                        await sec.publish_home(user)
                stale = CU.stale_info(latest["state"], sec_host,
                                      os.path.expanduser(SA.CLONE), catch.gave_up,
                                      catch.blocked)
                latest["state"]["hosts"] = {
                    h: {**(hosts_power.get(h) or {}), "idle_stop_min": (lab.hosts[h] or {}).get("idle_stop_min"),
                        "idle_since": (idle.idle_since.get(h).isoformat(timespec="seconds")
                                       if idle.idle_since.get(h) else None),
                        "stale": stale.get(h)}
                    for h in lab.hosts}
                OV.annotate(latest["state"], lab, sec_host, prev)    # one status for all views
                fd, tmp = tempfile.mkstemp(dir=os.path.dirname(snapshot_path))
                with os.fdopen(fd, "w") as f:
                    json.dump(latest["state"], f)
                os.replace(tmp, snapshot_path)
            except Exception:
                log.exception("collecting the lab's state failed")
            try:
                await asyncio.wait_for(stop.wait(), COLLECT_EVERY_S)
            except asyncio.TimeoutError:
                pass

    sec_id = next(a for a, x in lab.agents.items() if x.get("role") == "secretary")
    sa = lab.agents[sec_id]
    env = {}
    seat = _read_secret(f"{CONFIG}/claude-oauth-token")
    if seat:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = seat
    workdir = os.path.expanduser(sa.get("workdir", "~/work"))
    os.makedirs(workdir, exist_ok=True)
    from .. import safefs
    safefs.pin(workdir)         # resolved once: its file operations stay there
    brain = SecretaryBrain(lab.display_name(sec_id), lab.name, workdir,
                           lab.agent_settings(sec_id), model=sa.get("model"), env=env,
                           events=events, usage=usage)
    sec = Secretary(lab, latest, workers, brain,
                    ManagerState(os.path.expanduser(f"{STATE}/chat")))
    sec.events = events
    slack_env = _env_file(_read_secret(f"{CONFIG}/slack.env"))
    if slack_env.get("SLACK_BOT_TOKEN") and slack_env.get("SLACK_APP_TOKEN"):
        members = {(h or {}).get("slack_user"): hid for hid, h in lab.humans.items()
                   if (h or {}).get("slack_user")}
        sec.slack = SlackIO(slack_env["SLACK_BOT_TOKEN"], slack_env["SLACK_APP_TOKEN"],
                            members, lambda human: (True, ""), sec.on_message,
                            os.path.join(workdir, "inbox"), engaged=sec.state.engaged,
                            on_home=sec.publish_home, on_action=sec.on_action)
        await sec.slack.start()

    async def notice_loop():
        while not stop.is_set():
            try:
                await sec.notices()
            except Exception:
                log.exception("notices failed")
            try:
                await asyncio.wait_for(stop.wait(), 3600)
            except asyncio.TimeoutError:
                pass

    checks = {}                     # human -> {at, agent | error}: the web UI's "check now"

    async def check_usage(human):
        last = checks.get(human)
        now = dt.datetime.now(dt.timezone.utc)
        if human not in lab.humans or (last and now - dt.datetime.fromisoformat(last["at"])
                                       < dt.timedelta(seconds=CHECK_MIN_GAP_S)):
            return
        at = now.isoformat(timespec="seconds")
        try:
            agent = await check_seat(lab, human, workers, managers,
                                     own=(sec_id, lambda: probe_now(usage, seat)) if seat else None)
            checks[human] = {"at": at, "agent": agent}
        except Exception as e:
            checks[human] = {"at": at, "error": f"check failed: {str(e)[:300]}"}
        try:
            prev = latest["state"]
            hosts = (latest["state"] or {}).get("hosts")
            latest["state"] = await collect(lab, workers, broker, units, managers,
                                            own=(sec_id, usage))
            if hosts is not None:
                latest["state"]["hosts"] = hosts
            OV.annotate(latest["state"], lab, sec_host, prev)
            await sec.timeout_zones(latest["state"])
        except Exception:
            log.exception("collecting after a usage check failed")

    def state_with_checks():
        s = latest["state"]
        return {**s, "checks": checks} if s else s

    from .web import log_access
    server = uvicorn.Server(uvicorn.Config(app(state_with_checks, get_events, viewer,
                                               check_usage,
                                               access=lambda m, a: log_access(lab, m, a),
                                               shape=lambda st, m: V.shape(st, lab, m),
                                               may_check=lambda m, h: m is not None and (
                                                   m == h or bool((lab.humans.get(m) or {})
                                                                  .get("admin")))),
                                           host=host, port=port, log_level="warning"))
    log.info("secretary: watching %d worker(s), slack=%s, web UI on http://%s:%d/",
             len(workers.known()), sec.slack is not None, host, port)
    async def apply_watch():
        """Deployments started from Slack: post each one's result when it finishes."""
        while not stop.is_set():
            try:
                await sec.check_applies()
            except Exception:
                log.exception("checking deployments failed")
            try:
                await asyncio.wait_for(stop.wait(), 10)
            except asyncio.TimeoutError:
                pass

    async def onboarding_loop():
        """Newcomers being onboarded (onboarding.py): its own loop, since a greeting is an
        agent's turn and mustn't hold up collecting the lab's state."""
        while not stop.is_set():
            try:
                await sec.onboarding_tick()
            except Exception:
                log.exception("onboarding failed")
            try:
                await asyncio.wait_for(stop.wait(), COLLECT_EVERY_S)
            except asyncio.TimeoutError:
                pass

    tasks = [asyncio.create_task(collector()), asyncio.create_task(notice_loop()),
             asyncio.create_task(onboarding_loop()),
             asyncio.create_task(probe_loop(usage, seat, stop)),
             asyncio.create_task(apply_watch())]
    GH.env_for(lab, sec_id, root=GH.SERVICE_ROOT)   # its GitHub identity's git setup, if any
    tasks += GH.start(lab, sec_id, stop, root=GH.SERVICE_ROOT)  # an App: keep its token fresh
    await server.serve()
    stop.set()
    if sec.slack is not None:
        await sec.slack.stop()
    for t in tasks:
        t.cancel()


def console_user():
    """Who ran the command: the admin behind sudo, else this user."""
    import getpass
    return os.environ.get("SUDO_USER") or getpass.getuser()


async def ask(lab_path, human, text):
    """One message to the secretary from the command line, as `human` (no Slack)."""
    from ..slackio import Incoming
    lab = load_lab(lab_path)
    mon = yaml.safe_load(_read_secret(f"{CONFIG}/monitor.yaml") or "") or {}
    role = lambda a: lab.agents.get(a, {}).get("role")     # noqa: E731
    workers = Workers({w: lab.worker_url(w) for w in mon if role(w) == "worker"},
                      {w: t for w, t in mon.items() if role(w) == "worker"}, timeout=15)
    try:
        with open(os.path.expanduser(f"{STATE}/lab-state.json")) as f:
            latest = {"state": json.load(f)}
    except (OSError, ValueError):
        latest = {"state": None}
    sec_id = next(a for a, x in lab.agents.items() if x.get("role") == "secretary")
    sa = lab.agents[sec_id]
    env = {}
    seat = _read_secret(f"{CONFIG}/claude-oauth-token")
    if seat:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = seat
    workdir = os.path.expanduser(sa.get("workdir", "~/work"))
    os.makedirs(workdir, exist_ok=True)
    from .. import safefs
    safefs.pin(workdir)         # resolved once: its file operations stay there
    # the same event log as the service: what's done from the console is on record (#41)
    events = EventLog(os.path.expanduser(f"{STATE}/events"))
    brain = SecretaryBrain(lab.display_name(sec_id), lab.name, workdir,
                           lab.agent_settings(sec_id), model=sa.get("model"), env=env,
                           events=events,
                           usage=UsageLog(os.path.expanduser(f"{STATE}/usage.json")))
    sec = Secretary(lab, latest, workers, brain,
                    ManagerState(os.path.expanduser(f"{STATE}/chat-cli")))
    sec.events = events
    sec.via = f"from the console (Unix user {console_user()})"
    print(await sec.on_message(Incoming("cli", None, "0", None, human, text, True, True)))
