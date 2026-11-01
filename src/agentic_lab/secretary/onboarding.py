"""Onboarding a newcomer (DESIGN.md §4.7): the secretary guides them in a DM through what they
can do on their own, before any admin work: a few answers, their Claude token, their keys
for outside services, and the admins' own steps. It's a Claude agent: it follows the lab's
instructions (`onboarding:` in lab.yaml) and works from a checklist that code computes from
the stored answers and secrets, never from what the agent remembers. When every item is
done, the admins are told, with what was collected: the rest (lab.yaml, machines, the
manager's Slack app) is theirs.

A newcomer needn't be in lab.yaml yet: an admin starts the onboarding from the console
with the lab id they'll get, and their secrets are stored under it, ready for the deploy.
"""
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import tempfile
from dataclasses import dataclass

from ..member_env import MemberEnvError, check_name

STORE = "~/.local/state/agentic-lab/onboarding.json"
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SLACK_USER = re.compile(r"^[UW][A-Z0-9]{6,}$")
_GITHUB = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{1,29}$")
_SSH_KEY = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|"
                      r"sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) "
                      r"[A-Za-z0-9+/]{40,}={0,2}(?: [^\n]{0,200})?$")
_ITEM_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")

# what a newcomer may be asked: key -> what it is (lab.yaml `onboarding.ask`)
ANSWERS = {
    "github": "their GitHub username",
    "manager_name": "a name for their manager (their Slack bot)",
    "ssh_key": "their SSH public key (to log in to their machines)",
}
DEFAULT_ASK = ["github", "manager_name"]
_CONFIG = {"instructions": str, "ask": list, "keys": list, "items": list}


class OnboardingError(ValueError):
    pass


def config(lab):
    """The lab's `onboarding:` section, with defaults."""
    c = lab.data.get("onboarding") or {}
    return {"instructions": (c.get("instructions") or "").strip(),
            "ask": list(c.get("ask") if c.get("ask") is not None else DEFAULT_ASK),
            "keys": list(c.get("keys") or []),
            "items": [dict(i) for i in c.get("items") or []]}


def check_config(data):
    """Problems with an `onboarding:` section (for `lab check`): [(where, message)]."""
    out = []
    if not isinstance(data, dict):
        return [("onboarding", "must be a mapping")]
    for k, v in data.items():
        if k not in _CONFIG:
            out.append(("onboarding", f"unknown key {k!r} (known: {', '.join(_CONFIG)})"))
        elif not isinstance(v, _CONFIG[k]):
            out.append((f"onboarding.{k}", f"must be a {_CONFIG[k].__name__}"))
    for a in data.get("ask") or [] if isinstance(data.get("ask"), list) else []:
        if a not in ANSWERS:
            out.append(("onboarding.ask", f"{a!r}: one of {', '.join(ANSWERS)}"))
    for k in data.get("keys") or [] if isinstance(data.get("keys"), list) else []:
        try:
            check_name(k)
        except MemberEnvError as e:
            out.append(("onboarding.keys", str(e)))
    seen = set()
    for i, it in enumerate(data.get("items") or [] if isinstance(data.get("items"), list)
                           else []):
        where = f"onboarding.items[{i}]"
        if not isinstance(it, dict) or set(it) - {"id", "text"}:
            out.append((where, "must be {id: ..., text: ...}"))
            continue
        if not isinstance(it.get("id"), str) or not _ITEM_ID.match(it["id"]):
            out.append((where, "id: lowercase letters, digits and '-'"))
        elif it["id"] in seen:
            out.append((where, f"id {it['id']!r} twice"))
        seen.add(it.get("id"))
        if not isinstance(it.get("text"), str) or not it["text"].strip():
            out.append((where, "text: what the newcomer does, for them to confirm"))
    return out


def _github_exists(login, timeout=10):
    """Does this GitHub user exist? None when GitHub can't be asked (then it's accepted)."""
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.github.com/users/{login}",
            headers={"Accept": "application/vnd.github+json"}), timeout=timeout).close()
        return True
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except (urllib.error.URLError, OSError):
        return None


def check_answer(lab, key, value, github_exists=_github_exists):
    """The answer as it's stored, or OnboardingError saying what's wrong with it."""
    if key not in ANSWERS:
        raise OnboardingError(f"no question {key!r} (questions: {', '.join(ANSWERS)})")
    v = " ".join(str(value or "").split()) if key != "ssh_key" else str(value or "").strip()
    if key == "github":
        v = v.lstrip("@")
        if not _GITHUB.match(v):
            raise OnboardingError(f"{v!r} isn't a GitHub username (letters, digits, '-')")
        if github_exists(v) is False:
            raise OnboardingError(f"GitHub has no user {v!r}: check the spelling")
    elif key == "manager_name":
        if not _NAME.match(v):
            raise OnboardingError("a name of 2 to 30 letters, digits, spaces, '-' or '_', "
                                  "starting with a letter")
        taken = {lab.display_name(a).lower() for a in lab.agents}
        if v.lower() in taken:
            raise OnboardingError(f"{v!r} is already an agent's name in this lab")
    elif key == "ssh_key":
        if "PRIVATE KEY" in v:
            raise OnboardingError("that's a PRIVATE key: it must never be shared. They should "
                                  "delete the message, make a new key pair, and send only "
                                  "the .pub file's line")
        if "\n" in v or not _SSH_KEY.match(v):
            raise OnboardingError("not an SSH public key: one line starting with ssh-ed25519 "
                                  "(or ssh-rsa, ecdsa-...), the contents of ~/.ssh/id_*.pub")
    return v


def shown(key, value):
    """An answer as the checklist shows it (an SSH key shortened)."""
    if key == "ssh_key" and value:
        parts = value.split(" ", 2)
        return f"{parts[0]} …{parts[1][-12:]}" + (f" {parts[2]}" if len(parts) > 2 else "")
    return value


@dataclass
class Item:
    id: str
    what: str
    done: bool
    detail: str = ""


def items(lab, rec, intake):
    """The checklist of one onboarding, computed now. Everything on it is the newcomer's to
    do; none of it waits for an admin."""
    c, out = config(lab), []
    for a in c["ask"]:
        v = (rec.get("answers") or {}).get(a)
        out.append(Item(f"answer:{a}", ANSWERS[a][0].upper() + ANSWERS[a][1:], bool(v),
                        shown(a, v) or ""))
    out.append(Item("claude-token", "Their Claude token, stored and checked with Anthropic "
                    "(this also shows Tailscale and Claude Code work on their computer)",
                    intake.has(rec["id"], "claude-token")))
    for k in c["keys"]:
        out.append(Item(f"key:{k}", f"Their key {k}, stored", intake.has(rec["id"], f"env:{k}")))
    for it in c["items"]:
        out.append(Item(f"item:{it['id']}", it["text"].strip(),
                        it["id"] in (rec.get("confirmed") or [])))
    return out


def finished(checklist):
    return all(i.done for i in checklist)


def table(rec, checklist):
    """The checklist as plain text, for the console, the agent and the admins."""
    head = f"{rec.get('name') or rec['id']} ({rec['id']}), started {rec.get('started')}"
    if rec.get("stopped"):
        head += f", stopped {rec['stopped']}"
    elif rec.get("finished"):
        head += f", finished {rec['finished']}"
    lines = [head]
    for i in checklist:
        lines.append(f"  [{'x' if i.done else ' '}] {i.what}" + (f": {i.detail}" if i.detail else ""))
    return "\n".join(lines)


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Onboardings:
    """The onboardings, in one JSON file shared by the secretary's service and the console
    (both as the secretary's user): every change is a locked read-modify-write."""

    def __init__(self, path=STORE):
        self.path = os.path.expanduser(path)

    def all(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @contextlib.contextmanager
    def _edit(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        with open(self.path + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self.all()
            yield data
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path))
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, self.path)

    def get(self, oid):
        return self.all().get(oid)

    def active(self):
        """{id: record} of the onboardings under way (not finished, not stopped)."""
        return {k: r for k, r in self.all().items()
                if not r.get("finished") and not r.get("stopped")}

    def by_slack(self, slack_user):
        return next((r for r in self.active().values() if r.get("slack_user") == slack_user),
                    None)

    def start(self, lab, oid, slack_user, name, by):
        if not _ID.match(oid or ""):
            raise OnboardingError(f"{oid!r}: a lab id is lowercase letters, digits, '-', '_'")
        if not _SLACK_USER.match(slack_user or ""):
            raise OnboardingError(f"{slack_user!r} isn't a Slack member ID (U…)")
        for hid, h in lab.humans.items():
            if hid == oid:
                raise OnboardingError(f"{oid!r} is already a human in lab.yaml")
            if (h or {}).get("slack_user") == slack_user:
                raise OnboardingError(f"Slack user {slack_user} is already {hid} in lab.yaml")
        with self._edit() as data:
            for r in data.values():
                if r["id"] != oid and r.get("slack_user") == slack_user and not (
                        r.get("finished") or r.get("stopped")):
                    raise OnboardingError(f"{slack_user} is already being onboarded as {r['id']}")
            old = data.get(oid)
            if old and not (old.get("finished") or old.get("stopped")):
                raise OnboardingError(f"{oid} is already being onboarded (lab onboarding stop "
                                      f"{oid} first, to start again)")
            data[oid] = {"id": oid, "slack_user": slack_user, "name": name or oid,
                         "started": _now(), "started_by": by, "answers": {}, "confirmed": [],
                         "greeted": False, "seen": [], "finished": None, "stopped": None}
            return data[oid]

    def update(self, oid, fn):
        """Apply fn(record) under the lock; returns the record."""
        with self._edit() as data:
            if oid not in data:
                raise OnboardingError(f"no onboarding {oid!r}")
            fn(data[oid])
            return data[oid]

    def answer(self, lab, oid, key, value, github_exists=_github_exists):
        if key not in config(lab)["ask"]:
            raise OnboardingError(f"this lab doesn't ask for {key!r} (it asks: "
                                  f"{', '.join(config(lab)['ask']) or 'nothing'})")
        v = check_answer(lab, key, value, github_exists)
        return self.update(oid, lambda r: r.setdefault("answers", {}).__setitem__(key, v))

    def confirm(self, lab, oid, item):
        known = [i["id"] for i in config(lab)["items"]]
        if item not in known:
            raise OnboardingError(f"no item {item!r} (items: {', '.join(known) or 'none'})")
        return self.update(oid, lambda r: r.setdefault("confirmed", []).append(item)
                           if item not in r.get("confirmed", []) else None)

    def stop(self, oid):
        return self.update(oid, lambda r: r.__setitem__("stopped", r.get("stopped") or _now()))
