"""Onboarding a newcomer (onboarding.py): the secretary guides them in a DM through what they
can do without an admin, from a checklist computed from what's stored; the admins are told
when it's done."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_lab import registry
from agentic_lab.registry.lab import check
from agentic_lab.secretary import brain as B
from agentic_lab.secretary import onboarding as O
from agentic_lab.secrets import intake as I
from agentic_lab.secretary.service import Secretary
from agentic_lab.slackio import Incoming

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBFUEipX0Ap+IlOqWKh+ORvQHmo89UUSCVIfWKSa46Nn dana@x"
CONFIG = {"instructions": "W&B: your profile → User settings → API keys.",
          "ask": ["github", "manager_name", "ssh_key"], "keys": ["WANDB_TEAM_API_KEY"],
          "items": [{"id": "read-guide", "text": "Read the newcomer guide"}]}


def lab(cfg=CONFIG):
    lb = registry.load(EXAMPLE)
    if cfg is not None:
        lb.data["onboarding"] = cfg
    return lb


def store(tmp_path):
    return O.Onboardings(tmp_path / "onboarding.json")


def started(tmp_path, lb=None):
    st = store(tmp_path)
    st.start(lb or lab(), "dana", "U0DANA001", "Dana Scully", "alice")
    return st


# ── the config ───────────────────────────────────────────────────────────────

def test_a_good_onboarding_section_passes_lab_check():
    assert not [i for i in check(lab()) if i.level == "error" and "onboarding" in i.where]


@pytest.mark.parametrize("bad,where", [
    ({"welcome": "hi"}, "unknown key 'welcome'"),
    ({"ask": ["github", "shoe_size"]}, "'shoe_size'"),
    ({"keys": ["PATH"]}, "reserved"),
    ({"items": [{"id": "a", "text": "x"}, {"id": "a", "text": "y"}]}, "twice"),
    ({"items": [{"id": "a"}]}, "text"),
    ({"instructions": ["not", "text"]}, "must be a str"),
])
def test_lab_check_catches_a_bad_onboarding_section(bad, where):
    msgs = [str(i) for i in check(lab(bad)) if i.level == "error"]
    assert any(where in m for m in msgs), msgs


# ── starting ─────────────────────────────────────────────────────────────────

def test_start_refuses_bad_ids_and_people_already_in_the_lab(tmp_path):
    st, lb = store(tmp_path), lab()
    for oid, slack, what in (("Dana!", "U0DANA001", "lab id"),
                             ("dana", "dana", "Slack member ID"),
                             ("alice", "U0DANA001", "already a human"),
                             ("dana", "U00000001", "already alice")):
        with pytest.raises(O.OnboardingError, match=what):
            st.start(lb, oid, slack, "Dana", "alice")
    st.start(lb, "dana", "U0DANA001", "Dana", "alice")
    with pytest.raises(O.OnboardingError, match="already being onboarded"):
        st.start(lb, "dana", "U0DANA001", "Dana", "alice")
    st.stop("dana")
    st.start(lb, "dana", "U0DANA001", "Dana", "alice")      # again, after a stop


# ── answers ──────────────────────────────────────────────────────────────────

def test_answers_are_checked_before_they_are_recorded(tmp_path):
    st, lb = started(tmp_path), lab()
    ok, nobody = (lambda u: True), (lambda u: False)
    with pytest.raises(O.OnboardingError, match="isn't a GitHub username"):
        st.answer(lb, "dana", "github", "dana scully", ok)
    with pytest.raises(O.OnboardingError, match="no user"):
        st.answer(lb, "dana", "github", "dana-scully", nobody)
    st.answer(lb, "dana", "github", "@dana-scully", ok)
    with pytest.raises(O.OnboardingError, match="already an agent's name"):
        st.answer(lb, "dana", "manager_name", "flash")
    st.answer(lb, "dana", "manager_name", "DeepTruth")
    assert st.get("dana")["answers"] == {"github": "dana-scully", "manager_name": "DeepTruth"}


def test_a_private_ssh_key_is_refused_and_a_public_one_is_shown_short(tmp_path):
    st, lb = started(tmp_path), lab()
    with pytest.raises(O.OnboardingError, match="PRIVATE"):
        st.answer(lb, "dana", "ssh_key", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Bl\n")
    with pytest.raises(O.OnboardingError, match="not an SSH public key"):
        st.answer(lb, "dana", "ssh_key", "my key is on my laptop")
    st.answer(lb, "dana", "ssh_key", PUB)
    shown = O.items(lb, st.get("dana"), SimpleNamespace(has=lambda *a: False))[2].detail
    assert PUB not in shown
    assert shown.startswith("ssh-ed25519 …") and shown.endswith("dana@x")


def test_only_the_labs_questions_and_items_are_accepted(tmp_path):
    lb = lab({"ask": ["github"]})
    st = started(tmp_path, lb)
    with pytest.raises(O.OnboardingError, match="doesn't ask"):
        st.answer(lb, "dana", "ssh_key", PUB)
    with pytest.raises(O.OnboardingError, match="no item"):
        st.confirm(lb, "dana", "read-guide")


# ── the checklist ────────────────────────────────────────────────────────────

def test_the_checklist_comes_from_whats_stored_and_says_when_its_finished(tmp_path):
    st, lb, intake = started(tmp_path), lab(), I.Intake(tmp_path)
    left = lambda: [i.id for i in O.items(lb, st.get("dana"), intake) if not i.done]  # noqa
    assert left() == ["answer:github", "answer:manager_name", "answer:ssh_key",
                      "claude-token", "key:WANDB_TEAM_API_KEY", "item:read-guide"]
    st.answer(lb, "dana", "github", "dana", lambda u: None)     # GitHub unreachable: accepted
    st.answer(lb, "dana", "manager_name", "DeepTruth")
    st.answer(lb, "dana", "ssh_key", PUB)
    intake.store("dana", "claude-token", "sk-ant-oat-x")       # stored before she's in lab.yaml
    intake.store("dana", "env:WANDB_TEAM_API_KEY", "w")
    assert left() == ["item:read-guide"]
    st.confirm(lb, "dana", "read-guide")
    checklist = O.items(lb, st.get("dana"), intake)
    assert O.finished(checklist)
    text = O.table(st.get("dana"), checklist)
    assert "[x] Their Claude token" in text and "(onboarding_confirm: read-guide)" in text and "sk-ant" not in text and "DeepTruth" in text


# ── the agent ────────────────────────────────────────────────────────────────

def test_a_newcomers_session_has_the_onboarding_tools_and_nothing_else(tmp_path):
    brain = B.SecretaryBrain("Iris", "lab", str(tmp_path), {})
    conv = SimpleNamespace(session_id=None)
    opts = brain.options(SimpleNamespace(), conv, None, onboarding=("Dana", "be kind"))
    assert opts.tools == [] and set(opts.allowed_tools) == set(B.ONBOARDING_TOOLS)
    assert list(opts.mcp_servers) == ["onboarding"]
    assert "Dana" in opts.system_prompt["append"] and "be kind" in opts.system_prompt["append"]
    member = brain.options(SimpleNamespace(), conv, None)
    assert member.tools is None and "mcp__lab__lab_state" in member.allowed_tools


class FakeSlack:
    def __init__(self, members):
        self.members, self.posts = members, []
        self.web = SimpleNamespace(conversations_open=self._open)

    async def _open(self, users):
        return {"channel": {"id": f"D-{users}"}}

    async def post(self, channel, thread_ts, text):
        self.posts.append((channel, text))


def secretary(tmp_path, lb=None):
    lb = lb or lab()
    sec = Secretary.__new__(Secretary)
    sec.lab, sec.events, sec._locks, sec.via = lb, None, {}, "in Slack"
    sec.intake = I.Intake(tmp_path)
    sec.onboardings = store(tmp_path)
    sec.intake_url = "http://sec:8771"
    sec.slack = FakeSlack({"U00000001": "alice"})
    sec.turns = []

    async def turn(ctx, conv, prompt, onboarding=None):
        sec.turns.append((prompt, onboarding, ctx))
        return "hello from the secretary"
    sec.brain = SimpleNamespace(turn=turn)
    sec.state = SimpleNamespace(
        conversation=lambda ch, ts: SimpleNamespace(key=ch, channel=ch, thread_ts=ts),
        save=lambda: None)

    async def announce(text):
        return True
    sec.announce = announce
    return sec


def test_a_newcomers_dm_goes_to_the_onboarding_agent_without_the_labs_layout(tmp_path):
    sec = secretary(tmp_path)
    sec.onboardings.start(sec.lab, "dana", "U0DANA001", "Dana Scully", "alice")
    sec.slack.members["U0DANA001"] = "dana"
    asyncio.run(sec.on_message(Incoming("D-U0DANA001", None, "1", "U0DANA001", "dana",
                                        "hi, what now?", True, True)))
    prompt, onboarding, _ = sec.turns[-1]
    assert onboarding == ("Dana Scully", CONFIG["instructions"])
    assert prompt == "[Dana Scully]: hi, what now?" and "lab, as deployed" not in prompt
    # in a channel: not there
    out = asyncio.run(sec.on_message(Incoming("C1", "5", "6", "U0DANA001", "dana", "hi",
                                              False, True)))
    assert "DM" in out and len(sec.turns) == 1


def test_links_go_only_for_the_checklists_keys(tmp_path, monkeypatch):
    from agentic_lab.secretary import service as S

    async def up(url, wait_s=15):
        return True
    monkeypatch.setattr(S, "intake_up", up)
    sec = secretary(tmp_path)
    sec.onboardings.start(sec.lab, "dana", "U0DANA001", "Dana", "alice")
    ctx = sec.onboarding_ctx("dana")
    assert "isn't on the checklist" in asyncio.run(ctx.key("AWS_SECRET_ACCESS_KEY"))
    assert "sent" in asyncio.run(ctx.key("WANDB_TEAM_API_KEY"))
    assert "sent" in asyncio.run(ctx.token())
    assert [c for c, _ in sec.slack.posts] == ["D-U0DANA001", "D-U0DANA001"]
    assert "refused" in ctx.answer("github", "not a user name")


def test_the_loop_greets_once_carries_on_when_a_secret_arrives_and_tells_the_admins(tmp_path):
    sec = secretary(tmp_path)
    lb = sec.lab
    sec.onboardings.start(lb, "dana", "U0DANA001", "Dana", "alice")
    asyncio.run(sec.onboarding_tick())
    assert sec.slack.members["U0DANA001"] == "dana"
    assert len(sec.turns) == 1 and "just started" in sec.turns[0][0]
    asyncio.run(sec.onboarding_tick())
    assert len(sec.turns) == 1                          # nothing new: quiet
    sec.intake.store("dana", "claude-token", "t")
    asyncio.run(sec.onboarding_tick())
    assert len(sec.turns) == 2 and "Claude token" in sec.turns[1][0]
    for k, v in (("github", "dana"), ("manager_name", "DeepTruth"), ("ssh_key", PUB)):
        sec.onboardings.answer(lb, "dana", k, v, lambda u: True)
    sec.onboardings.confirm(lb, "dana", "read-guide")
    sec.intake.store("dana", "env:WANDB_TEAM_API_KEY", "w")
    asyncio.run(sec.onboarding_tick())
    admin = [t for c, t in sec.slack.posts if c == "D-U00000001"]
    assert len(admin) == 1 and "finished onboarding" in admin[0] and "DeepTruth" in admin[0]
    assert sec.onboardings.get("dana")["finished"]
    asyncio.run(sec.onboarding_tick())
    assert "U0DANA001" not in sec.slack.members         # done: not a member until lab.yaml
    assert len([t for c, t in sec.slack.posts if c == "D-U00000001"]) == 1


def test_a_stopped_onboarding_no_longer_reaches_the_secretary(tmp_path):
    sec = secretary(tmp_path)
    convs = {}
    sec.state = SimpleNamespace(save=lambda: None, conversation=lambda ch, ts: convs.setdefault(
        ch, SimpleNamespace(key=ch, channel=ch, thread_ts=ts, session_id=None)))
    sec.onboardings.start(sec.lab, "dana", "U0DANA001", "Dana", "alice")
    asyncio.run(sec.onboarding_tick())
    convs["D-U0DANA001"].session_id = "onboarding-session"
    sec.onboardings.stop("dana")
    asyncio.run(sec.onboarding_tick())
    assert "U0DANA001" not in sec.slack.members and "alice" in sec.slack.members.values()
    assert convs["D-U0DANA001"].session_id is None      # as a member later: a fresh session


# ── the console ──────────────────────────────────────────────────────────────

def test_the_console_starts_and_shows_an_onboarding(tmp_path):
    lab_path = tmp_path / "lab.yaml"
    lab_path.write_text(EXAMPLE.read_text() + "\nonboarding:\n  ask: [ssh_key]\n  keys: [WANDB_TEAM_API_KEY]\n")
    run = lambda *a: subprocess.run(  # noqa: E731
        [sys.executable, "-m", "agentic_lab.cli", "--lab", str(lab_path), "onboarding", *a,
         "--state", str(tmp_path)], capture_output=True, text=True)
    out = run("start", "dana", "--slack", "U0DANA001", "--name", "Dana Scully")
    assert out.returncode == 0, out.stderr
    out = run("status", "dana")
    assert "[ ] Their key WANDB_TEAM_API_KEY" in out.stdout and "Dana Scully" in out.stdout
    assert run("start", "dana", "--slack", "U0DANA001", "--name", "D").returncode == 1
    O.Onboardings(tmp_path / "onboarding.json").update(
        "dana", lambda r: r["answers"].__setitem__("ssh_key", PUB))
    assert PUB in run("status").stdout                 # whole, for the member's PR
    assert json.loads((tmp_path / "onboarding.json").read_text())["dana"]["started_by"]


# ── end to end, through the real intake ──────────────────────────────────────

def test_a_newcomers_secrets_arrive_through_the_intake_before_they_are_in_lab_yaml(
        tmp_path, monkeypatch, free_port):
    """The token command and the key link the secretary DMs work against the real intake
    for an id that isn't in lab.yaml yet; the loop then carries on, and the admins hear."""
    import importlib.util
    import re
    import threading
    import time

    import httpx2
    import uvicorn

    from agentic_lab.secretary import service as S
    from agentic_lab.secrets.server import app
    root = Path(__file__).parent.parent

    async def up(url, wait_s=15):
        return True
    monkeypatch.setattr(S, "intake_up", up)
    sec = secretary(tmp_path)
    url = f"http://127.0.0.1:{free_port}"
    sec.intake_url = url
    lb = sec.lab
    assert "dana" not in lb.humans
    sec.onboardings.start(lb, "dana", "U0DANA001", "Dana", "alice")
    asyncio.run(sec.onboarding_tick())                          # the greeting
    server = uvicorn.Server(uvicorn.Config(app(sec.intake, root / "client" / "lab-member.py"),
                                           host="127.0.0.1", port=free_port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.02)
    try:
        ctx = sec.onboarding_ctx("dana")
        asyncio.run(ctx.token())
        dm = sec.slack.posts[-1][1]
        code = re.search(r"--code ([A-Z2-9]{4}-[A-Z2-9]{4})", dm).group(1)
        spec = importlib.util.spec_from_file_location("lab_member",
                                                      root / "client" / "lab-member.py")
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        monkeypatch.setattr(client.getpass, "getpass", lambda prompt: "sk-ant-oat01-" + "x" * 60)
        client.main(["claude-token", "--paste", "--code", code, "--to", url])
        asyncio.run(sec.onboarding_tick())
        assert "Claude token" in sec.turns[-1][0]               # carried on by itself

        asyncio.run(ctx.key("WANDB_TEAM_API_KEY"))
        link = re.search(r"(http://\S+)", sec.slack.posts[-1][1]).group(1)
        r = httpx2.post(link, data={"value": "w" * 40})
        assert "Stored" in r.text
        assert sec.intake.member_env("dana") == {"WANDB_TEAM_API_KEY": "w" * 40}
    finally:
        server.should_exit = True
        th.join()
    for k, v in (("github", "dana"), ("manager_name", "DeepTruth"), ("ssh_key", PUB)):
        sec.onboardings.answer(lb, "dana", k, v, lambda u: True)
    sec.onboardings.confirm(lb, "dana", "read-guide")
    asyncio.run(sec.onboarding_tick())
    told = [t for c, t in sec.slack.posts if c == "D-U00000001"]
    assert len(told) == 1 and "[x] Their key WANDB_TEAM_API_KEY" in told[0]
    assert "w" * 40 not in told[0] and "sk-ant" not in told[0]
