"""The lab registry: `lab.yaml`, the one description of the lab (DESIGN.md §8).

It lists the humans, agents, hosts and credentials. Every component reads it. It holds
credential *metadata* (name, host, expiry), never secret values.

`load()` parses and type-checks the file. `check()` validates the whole lab (references,
roles, channels, policies, expiring credentials) and returns every issue at once, so one
`lab check` run shows everything that's wrong.
"""
import datetime as dt
import os
import re
from dataclasses import dataclass, field

import yaml

from ..policy import grants as G
from ..policy import sandbox
from .. import github

ROLES = ("worker", "manager", "secretary")
DEFAULT_WORKDIR = "~/work"
# Auto-compaction: managers hold long, chatty Slack conversations, so they compact about 5x
# earlier than Claude's default (which fires just below the full window); others keep it.
DEFAULT_COMPACT_PERCENT = {"manager": 17}
USER_PREFIX = "lab-"
# an agent id becomes part of its Unix user name: lab-<agent-id>, at most 32 characters
_AGENT_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_UNIX_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_HOST_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_DNS_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*$")
_WORKDIR = re.compile(r"^(~/|/)[A-Za-z0-9._/-]*$")


def _in_home_settings(workdir):
    parts = [p for p in os.path.normpath(workdir).split("/") if p]
    if parts[:1] == ["~"]:
        rest = parts[1:]
    elif parts[:1] == ["home"]:
        rest = parts[2:]
    else:
        return False
    return not rest or rest[0].startswith(".")
READERS_GROUP = "lab-readers"
CHANNEL_TYPES = ("slack_dm",)   # first version: Slack DM only (DESIGN.md §7)
ACCESS = ("shared", "dedicated")
HOST_KINDS = ("onprem", "cloud")
PROVIDERS = ("nebius", "aws")       # cloud providers lab.yaml knows
POWERED = ("nebius", "aws")         # ...and those the secretary can power (idle stop, on/off)
EXPIRY_WARN_DAYS = 14

# key -> (type, required). Types are checked; unknown keys are errors.
_TOP = {"lab": (str, True), "tailnet": (str, False), "trust_tailnet": (bool, False),
        "announcements": (str, False),     # Slack channel (id or #name) for lab-wide notices
        "humans": (dict, True),
        "agents": (dict, True), "hosts": (dict, True), "credentials": (list, False),
        "github": (dict, False),            # the agents' GitHub bot accounts
        # how the secretary guides a newcomer (secretary/onboarding.py)
        "onboarding": (dict, False),
        # a host that missed a deploy is brought up to date when it's back (#61)
        "auto_catch_up": (bool, False),
        # what members see of each other's work: "private" (default) or "team" (#62)
        "visibility": (str, False)}
_HUMAN = {"name": (str, False), "slack_user": (str, False), "channels": (list, False),
          "admin": (bool, False),           # may see every agent's log in the web UI
          # how the web UI recognises them (tailscale whois): their own agents' logs
          "tailscale_login": (str, False)}
_CHANNEL = {"type": (str, True), "timeout_s": (int, False), "on_timeout": (str, False)}
_AGENT = {"role": (str, True), "human": (str, True), "host": (str, True),
          "model": (str, False), "access": (str, False), "workdir": (str, False),
          "gpu": (bool, False), "port": (int, False),
          "display_name": (str, False), "compact_percent": (int, False),
          "grants": (list, False),
          "slack_app": (str, False),
          # its GitHub identity: a name in github.accounts, or {name: default | [repos]}
          "github": ((str, dict), False),
          "turn_budget_min": (int, False),  # a manager's turns are cut off after this
          "keep_free_gb": (int, False)}     # a worker prunes its caches below this (#45)
_HOST = {"kind": (str, True), "provider": (str, False), "instance": (str, False),
         "region": (str, False),        # AWS: the instance's region (required with instance:)
         # minutes idle before the secretary stops it (cloud hosts: required; 0: never)
         "idle_stop_min": (int, False), "tailnet_name": (str, False),
         "ssh_user": (str, False),      # who `lab apply` logs in as (default: the admin)
         "idle_stop_with_managers": (bool, False),  # default true; false: managers keep it busy
         # Linux users here who may read its agents' working folders (#43; lab-readers)
         "readers": (list, False)}
_GITHUB = {"accounts": (dict, True)}
# a bot account (a GitHub machine user) an agent acts as
# a GitHub identity: a bot account (a machine user, with a fine-grained token), or a GitHub
# App (app_id; named by its slug, it appears as <slug>[bot]; its private key via the intake)
_GH_ACCOUNT = {"email": (str, False),                       # default: its noreply address
               "expires": ((dt.date, type(None)), False),   # a bot account's token expiry
               "app_id": (int, False), "installation_id": (int, False)}
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/(?:\*|[A-Za-z0-9_.*-]+)$")
_GH_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_CRED = {"name": (str, True), "host": (str, True), "expires": ((dt.date, type(None)), True),
         "scope": (str, False), "agent": (str, False)}


@dataclass
class Issue:
    level: str      # "error" | "warning"
    where: str
    message: str

    def __str__(self):
        return f"{self.level.upper():7} {self.where}: {self.message}"


@dataclass
class Lab:
    path: str
    data: dict
    issues: list = field(default_factory=list)   # parse-time issues

    @property
    def name(self):
        return self.data.get("lab")

    @property
    def humans(self):
        return self.data.get("humans") or {}

    @property
    def agents(self):
        return self.data.get("agents") or {}

    @property
    def hosts(self):
        return self.data.get("hosts") or {}

    @property
    def credentials(self):
        return self.data.get("credentials") or []

    def address(self, agent_id):
        """Where the agent's service is reached on the tailnet: its host's tailnet name
        (`tailnet_name`, or `<host>.<tailnet>`) and its port."""
        a = self.agents[agent_id]
        host = self.hosts.get(a.get("host")) or {}
        name = host.get("tailnet_name") or f"{a.get('host')}.{self.data.get('tailnet')}"
        default = {"worker": 8770, "secretary": 8771}.get(a.get("role"), 8773)
        return name, a.get("port", default)

    def address_of_host(self, host_id):
        h = self.hosts.get(host_id) or {}
        return h.get("tailnet_name") or f"{host_id}.{self.data.get('tailnet')}"

    def worker_url(self, agent_id):
        name, port = self.address(agent_id)
        return f"http://{name}:{port}/mcp"

    def unix_user(self, agent_id):
        """The dedicated Unix user the agent runs as (DESIGN.md §10)."""
        return USER_PREFIX + agent_id

    def standing_grants(self, agent_id):
        """The agent's standing grants from lab.yaml (raises GrantError if malformed)."""
        return [G.from_mapping(m) for m in self.agents[agent_id].get("grants") or []]

    def readers(self, agent_id):
        """The Linux users on the agent's host who may read its working folder (#43)."""
        return list((self.hosts.get(self.agents[agent_id].get("host")) or {}).get("readers") or [])

    def agent_tmpdir(self, agent_id):
        """A stable TMPDIR, `<workdir>/tmp`, when its host has readers (so they find the
        agent's temporary files; the service's own /tmp is private and changes on restart)."""
        a = self.agents[agent_id]
        if not self.readers(agent_id) or a.get("role") not in ("worker", "manager"):
            return None
        return os.path.join(os.path.expanduser(a.get("workdir", DEFAULT_WORKDIR)), "tmp")

    def agent_settings(self, agent_id, extra_grants=(), now=None):
        """The agent's Claude Code settings: the sandbox model + its grants (DESIGN.md §6)."""
        a = self.agents[agent_id]
        return sandbox.settings(
            workdir=a.get("workdir", DEFAULT_WORKDIR),
            tailnet=self.data.get("tailnet"),
            trust_tailnet=self.data.get("trust_tailnet", True),
            grant_list=self.standing_grants(agent_id) + list(extra_grants),
            now=now,
            gpu=a.get("gpu", a.get("role") == "worker"),
            compact_percent=a.get("compact_percent",
                                  DEFAULT_COMPACT_PERCENT.get(a.get("role"))),
            tmpdir=self.agent_tmpdir(agent_id),
            # the secretary's identity is its service's (config repo PRs), not its agent's
            domains=github.DOMAINS if a.get("github") and a.get("role") != "secretary"
            else ())

    @property
    def github_accounts(self):
        return (self.data.get("github") or {}).get("accounts") or {}

    def github_identities(self, agent_id):
        """(default identity, [(repo pattern, identity)]) of an agent; (None, []) if none.
        `github: name` is one identity for everything; `github: {name: default, other:
        [owner/repo, owner/*]}` picks the identity by repository."""
        g = (self.agents.get(agent_id) or {}).get("github")
        if not g:
            return None, []
        if isinstance(g, str):
            return g, []
        default = next((n for n, v in g.items() if v == "default"), None)
        return default, [(p, n) for n, v in g.items() if isinstance(v, list) for p in v]

    def github_names(self, agent_id):
        default, mapped = self.github_identities(agent_id)
        return list(dict.fromkeys(([default] if default else []) + [n for _, n in mapped]))

    def github_is_app(self, account):
        return bool((self.github_accounts.get(account) or {}).get("app_id"))

    def github_login(self, account):
        """How the identity appears on GitHub: an app as `<slug>[bot]`."""
        return f"{account}[bot]" if self.github_is_app(account) else account

    def github_email(self, account):
        """The commit email (its noreply address unless lab.yaml says; for an app,
        `lab github-app-check` prints the exact one, with the bot's id)."""
        a = self.github_accounts.get(account) or {}
        return a.get("email") or f"{self.github_login(account)}@users.noreply.github.com"

    def display_name(self, agent_id):
        """How humans see the agent (Slack, web UI, how it introduces itself)."""
        return self.agents[agent_id].get("display_name") or agent_id


def _fields(obj, spec, where, issues):
    if not isinstance(obj, dict):
        issues.append(Issue("error", where, f"expected a mapping, got {type(obj).__name__}"))
        return False
    for k in obj:
        if k not in spec:
            issues.append(Issue("error", where, f"unknown key {k!r}"))
    for k, (typ, required) in spec.items():
        if k not in obj:
            if required:
                issues.append(Issue("error", where, f"missing required key {k!r}"))
            continue
        v = obj[k]
        # bool is a subclass of int; don't let `true` pass as an int
        if typ is int and isinstance(v, bool) or not isinstance(v, typ):
            names = typ.__name__ if isinstance(typ, type) else " or ".join(t.__name__ for t in typ)
            issues.append(Issue("error", f"{where}.{k}", f"expected {names}, got {v!r}"))
    return True


def load(path):
    with open(path) as f:
        data = yaml.safe_load(f)
    issues = []
    if not isinstance(data, dict):
        return Lab(path, {}, [Issue("error", path, "lab.yaml must be a mapping")])
    _fields(data, _TOP, "lab", issues)
    return Lab(path, data, issues)


def check(lab: Lab, today: dt.date | None = None):
    """Validate the whole lab. Returns a list of Issues (errors and warnings)."""
    today = today or dt.date.today()
    issues = list(lab.issues)
    E = lambda where, msg: issues.append(Issue("error", where, msg))      # noqa: E731
    W = lambda where, msg: issues.append(Issue("warning", where, msg))    # noqa: E731

    for hid, h in lab.humans.items():
        where = f"humans.{hid}"
        if not _fields(h or {}, _HUMAN, where, issues):
            continue
        for i, ch in enumerate((h or {}).get("channels") or []):
            cw = f"{where}.channels[{i}]"
            if not _fields(ch, _CHANNEL, cw, issues):
                continue
            if ch.get("type") not in CHANNEL_TYPES:
                E(cw, f"type must be one of {CHANNEL_TYPES}, got {ch.get('type')!r}")
            if "timeout_s" in ch and isinstance(ch["timeout_s"], int) and ch["timeout_s"] <= 0:
                E(cw, "timeout_s must be positive")
            if ch.get("on_timeout", "deny") != "deny":
                E(cw, "on_timeout must be 'deny' (a timeout never approves)")

    if "onboarding" in lab.data:
        from ..secretary.onboarding import check_config
        for where, msg in check_config(lab.data["onboarding"]):
            E(where, msg)

    if lab.data.get("visibility", "private") not in ("private", "team"):
        E("visibility", "must be private (the default: members see the lab, not each "
                        "other's work) or team (members see each other's work too)")
    if lab.data.get("tailnet") and not _DNS_NAME.match(str(lab.data["tailnet"])):
        E("tailnet", f"{lab.data['tailnet']!r} isn't a DNS name")
    for hid, h in lab.hosts.items():
        where = f"hosts.{hid}"
        if not _HOST_ID.match(str(hid)):
            E(where, "a host id is lowercase letters, digits and '-'")
        for key, pattern, what in (("ssh_user", _UNIX_USER, "a Linux user name"),
                                   ("tailnet_name", _DNS_NAME, "a DNS name")):
            v = (h or {}).get(key)
            if v is not None and not (isinstance(v, str) and pattern.match(v)):
                E(where, f"{key} {v!r} isn't {what}")
        if "power" in (h or {}):            # before 2026-10-19: power: always_on | on_demand
            E(where, "'power' was removed: idle_stop_min decides (above 0: stopped when idle "
                     "that long; 0: never)")
            h = {k: v for k, v in h.items() if k != "power"}
        if not _fields(h or {}, _HOST, where, issues):
            continue
        if h.get("kind") not in HOST_KINDS:
            E(where, f"kind must be one of {HOST_KINDS}, got {h.get('kind')!r}")
        if h.get("kind") == "cloud" and not h.get("provider"):
            E(where, "a cloud host needs a provider")
        prov = h.get("provider")
        if h.get("kind") == "cloud" and prov and prov not in PROVIDERS:
            W(where, f"provider {prov!r} isn't one the lab knows ({', '.join(PROVIDERS)}): the "
                     "host works, but the secretary can't power it")
        if prov == "aws" and h.get("instance") and not h.get("region"):
            # the secretary's service user has no AWS config: without it every call fails
            E(where, "an AWS host the secretary powers (instance:) needs its region:")
        if (h.get("kind") == "cloud" and prov and prov not in POWERED
                and isinstance(h.get("idle_stop_min"), int) and h["idle_stop_min"] > 0):
            W(where, f"no power control for {prov} hosts (yet): idle_stop_min has no effect; "
                     "set it to 0")
        stop = h.get("idle_stop_min")
        if h.get("kind") == "cloud" and stop is None:
            E(where, "a cloud host needs idle_stop_min: minutes idle before the secretary "
                     "stops it (0: never)")
        if isinstance(stop, int) and stop < 0:
            E(where, "idle_stop_min must be 0 (never) or more")
        if h.get("kind") != "cloud" and stop:
            W(where, "idle_stop_min only applies to cloud hosts (the secretary can't power "
                     "this one)")
        for r in h.get("readers") or []:
            if not isinstance(r, str) or not _UNIX_USER.match(r):
                E(where, f"readers: {r!r} isn't a Linux user name")
        if h.get("idle_stop_with_managers") is True:
            W(where, "idle_stop_with_managers: true is the default (managers never keep a "
                     "host on); only false changes anything")

    roles = {}
    for aid, a in lab.agents.items():
        where = f"agents.{aid}"
        if not _fields(a or {}, _AGENT, where, issues):
            continue
        role = a.get("role")
        roles.setdefault(role, []).append(aid)
        if not _AGENT_ID.match(aid) or len(USER_PREFIX + aid) > 32:
            E(where, f"agent id must be lowercase letters, digits and '-', starting with a "
                     f"letter, at most {32 - len(USER_PREFIX)} characters: it becomes the "
                     f"Unix user {USER_PREFIX}{aid}")
        if role not in ROLES:
            E(where, f"role must be one of {ROLES}, got {role!r}")
        if a.get("human") not in lab.humans:
            E(where, f"human {a.get('human')!r} is not in humans "
                     "(every agent is attached to a human member, in terms of tokens)")
        if a.get("host") not in lab.hosts:
            E(where, f"host {a.get('host')!r} is not in hosts")
        if role in ("worker", "manager"):
            if a.get("access", "dedicated") not in ACCESS:
                E(where, f"access must be one of {ACCESS}, got {a.get('access')!r}")
        elif "access" in a:
            E(where, "'access' applies to workers and managers only")
        if role != "manager" and "slack_app" in a:
            E(where, "'slack_app' applies to managers only")
        if role != "manager" and "turn_budget_min" in a:
            E(where, "'turn_budget_min' applies to managers only")
        if role != "worker" and "keep_free_gb" in a:
            E(where, "'keep_free_gb' applies to workers only")
        if isinstance(a.get("keep_free_gb"), int) and a["keep_free_gb"] < 0:
            E(where, "keep_free_gb must be 0 or more")
        tb = a.get("turn_budget_min")
        if isinstance(tb, int) and not isinstance(tb, bool) and not 1 <= tb <= 60:
            E(where, "turn_budget_min must be between 1 and 60")
        # (slack_app is informational: `lab slack-verify <manager>` prints the line for it)
        if role == "secretary":
            host = lab.hosts.get(a.get("host")) or {}
            if host.get("idle_stop_min"):
                E(where, "the secretary's host is never stopped: give it idle_stop_min: 0")
        cp = a.get("compact_percent")
        if isinstance(cp, int) and not isinstance(cp, bool) and not 1 <= cp <= 100:
            E(where, "compact_percent must be between 1 and 100")
        workdir = a.get("workdir", DEFAULT_WORKDIR)
        if isinstance(workdir, str) and (not _WORKDIR.match(workdir)
                                         or ".." in workdir.split("/")):
            E(where, f"workdir must be an absolute or ~/ path of letters, digits and . _ - / "
                     f"(no ..), got {workdir!r}")
        elif isinstance(workdir, str) and _in_home_settings(workdir):
            E(where, f"workdir {workdir!r} is the home itself or one of its dot folders "
                     "(settings, keys, the lab's own): use a plain folder, e.g. ~/work")
        for j, m in enumerate(a.get("grants") or []):
            try:
                g = G.from_mapping(m)
            except G.GrantError as e:
                E(f"{where}.grants[{j}]", str(e))
                continue
            if g.expires is not None:
                E(f"{where}.grants[{j}]", "standing grants in lab.yaml don't expire; "
                                          "timed grants are given by a human through the broker")

    gh = lab.data.get("github")
    if gh is not None and _fields(gh, _GITHUB, "github", issues):
        for login, acc in (gh.get("accounts") or {}).items():
            where = f"github.accounts.{login}"
            if not _GH_LOGIN.match(str(login)):
                E(where, "must be the bot's GitHub login (letters, digits, '-')")
            if not _fields(acc or {}, _GH_ACCOUNT, where, issues):
                continue
            exp = (acc or {}).get("expires")
            if isinstance(exp, dt.date):
                left = (exp - today).days
                if left < 0:
                    E(where, f"its token expired {-left} days ago ({exp})")
                elif left <= EXPIRY_WARN_DAYS:
                    W(where, f"its token expires in {left} days ({exp})")
    for aid, a in lab.agents.items():
        g = (a or {}).get("github")
        if g is None:
            continue
        where = f"agents.{aid}.github"
        if (a or {}).get("role") not in ("manager", "worker", "secretary"):
            E(where, "'github' applies to managers, workers and the secretary")
            continue
        if (a or {}).get("role") == "secretary" and not isinstance(g, str):
            E(where, "the secretary has one identity (github: <account>): it only works on "
                     "the config repo")
            continue
        if isinstance(g, dict):
            defaults = [n for n, v in g.items() if v == "default"]
            if len(defaults) != 1:
                E(where, "name exactly one identity `default` (the others: lists of repos)")
            for n, v in g.items():
                if v != "default" and not (isinstance(v, list) and v and all(
                        isinstance(x, str) and _REPO_PATTERN.match(x) for x in v)):
                    E(where, f"{n}: `default`, or a list of repositories like owner/repo "
                             "or owner/*")
        for n in ([g] if isinstance(g, str) else list(g)):
            if n not in lab.github_accounts:
                E(where, f"github account {n!r} is not in github.accounts")

    for hid, h in lab.humans.items():
        if (h or {}).get("admin") and not (h or {}).get("tailscale_login"):
            W(f"humans.{hid}", "an admin needs tailscale_login to see agents' logs in the web UI")
    if lab.data.get("trust_tailnet", True) and not lab.data.get("tailnet"):
        W("lab", "no tailnet set: sandboxed commands can't reach lab resources")

    ports = {}                      # (host, port) -> the agent using it (#61)
    sec_host = next((x.get("host") for x in lab.agents.values()
                     if (x or {}).get("role") == "secretary"), None)
    for h in lab.hosts:
        if h == sec_host:           # the secretary host's own services
            for p, what in ((8771, "the intake"), (8772, "the broker"), (8774, "the web UI")):
                ports[(h, p)] = what
    for aid, a in lab.agents.items():
        if (a or {}).get("role") not in ("worker", "manager"):
            continue
        port = lab.address(aid)[1]
        other = ports.get((a.get("host"), port))
        if other:
            E(f"agents.{aid}", f"port {port} on host {a.get('host')} is taken by {other}: "
                               "give it its own `port:`")
        ports[(a.get("host"), port)] = aid

    secretaries = roles.get("secretary", [])
    if len(secretaries) != 1:
        E("agents", f"a lab has exactly one secretary, found {len(secretaries)}")
    elif secretaries != ["secretary"]:
        E(f"agents.{secretaries[0]}", "the secretary's id must be `secretary` (its services "
                                      "run as lab-secretary); give it any display_name")
    for hid, h in lab.humans.items():
        mine = [aid for aid in roles.get("manager", []) if lab.agents[aid].get("human") == hid]
        if mine and not (h or {}).get("channels"):
            E(f"humans.{hid}", "has a manager but no permission channel")
        if mine and not (h or {}).get("slack_user"):
            E(f"humans.{hid}", "has a manager but no slack_user (permissions go to their Slack DM)")
        if len(mine) > 1:
            W(f"humans.{hid}", f"has {len(mine)} managers: {mine}")
    for aid in roles.get("worker", []):
        a = lab.agents[aid]
        if a.get("access", "dedicated") == "dedicated":
            owner = a.get("human")
            if not any(lab.agents[m].get("human") == owner for m in roles.get("manager", [])):
                W(f"agents.{aid}", f"dedicated to {owner!r}, who has no manager to submit tasks")

    powered_aws = [h for h, x in lab.hosts.items()
                   if (x or {}).get("provider") == "aws" and (x or {}).get("instance")]
    if powered_aws and not any(c.get("name") == "aws-key" for c in lab.credentials):
        # an AWS key never expires by itself: without an entry, no rotation warning (#68)
        W("credentials", f"no aws-key entry for the secretary's AWS key ({', '.join(powered_aws)}"
                         " powered): add one with its rotation date (docs/install.md)")
    for i, c in enumerate(lab.credentials):
        where = f"credentials[{i}]"
        if not _fields(c, _CRED, where, issues):
            continue
        where = f"credentials.{c.get('name', i)}"
        if c.get("host") not in lab.hosts:
            E(where, f"host {c.get('host')!r} is not in hosts")
        if c.get("agent") is not None and c["agent"] not in lab.agents:
            E(where, f"agent {c['agent']!r} is not in agents")
        exp = c.get("expires")
        if isinstance(exp, dt.date):
            left = (exp - today).days
            if left < 0:
                E(where, f"expired {-left} days ago ({exp})")
            elif left <= EXPIRY_WARN_DAYS:
                W(where, f"expires in {left} days ({exp})")
    return issues
