"""The lab's state, as the secretary sees it: collected from every agent, every few seconds.

Workers are asked over MCP with the secretary's read-only tokens; the broker for pending
approvals and grants; systemd for the services on this host. Usage is aggregated per
account (each member's Claude seat): the seat's limit windows are shared by all its
agents, so the freshest reading wins, and every number keeps when and from which agent it
came.
"""
import asyncio
import datetime as dt
import subprocess

import httpx2

from ..manager.workers import WorkerError, Workers


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _unit_state(unit):
    try:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True,
                           timeout=5)
        return r.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


async def broker_overview(url, token):
    try:
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                                      timeout=10) as c:
            r = await c.get(f"{url.rstrip('/')}/v1/overview")
            r.raise_for_status()
            return r.json()
    except (httpx2.HTTPError, ValueError) as e:
        return {"error": str(e)}


async def manager_status(lab, manager, token, hours=0):
    """A manager's /v1/status (usage, and events for the last `hours`)."""
    name, port = lab.address(manager)
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10) as c:
        r = await c.get(f"http://{name}:{port}/v1/status", params={"hours": hours})
        r.raise_for_status()
        return r.json()


async def check_seat(lab, human, workers: Workers, managers, own=None):
    """Have one agent on `human`'s seat read the seat's limits now: the secretary itself
    (`own`: (its id, async check)), else a manager, else a worker. Returns the agent asked."""
    errors = []
    for aid, a in lab.agents.items():
        if a.get("human") != human:
            continue
        try:
            if own is not None and aid == own[0]:
                await own[1]()
            elif a.get("role") == "manager" and aid in managers:
                name, port = lab.address(aid)
                async with httpx2.AsyncClient(timeout=40, headers={
                        "Authorization": f"Bearer {managers[aid]}"}) as c:
                    r = await c.post(f"http://{name}:{port}/v1/usage/check")
                    if r.status_code != 200:
                        raise ValueError((r.json() or {}).get("error") or r.status_code)
            elif a.get("role") == "worker" and aid in workers.known():
                await workers.call(aid, "check_usage", retries=0)
            else:
                continue
            return aid
        except Exception as e:
            errors.append(f"{lab.display_name(aid)}: {e}")
    raise RuntimeError("; ".join(errors) or "no running agent on this seat to ask")


RECENT = 10                 # a worker's latest tasks on the dashboard


def task_summary(t):
    """A task as the dashboard shows it: what it is, its state and timing, and (for its
    member and admins only: visibility.TASK_PUBLIC keeps the rest from others) why it waits
    or failed and its latest progress line."""
    w = t.get("waiting") or {}
    last = (t.get("progress") or [{}])[-1] or {}
    return {**{k: t.get(k) for k in ("id", "state", "requester", "manager", "created",
                                      "started", "finished", "instruction")},
            "waiting": {k: w.get(k) for k in ("note", "since", "wake_at", "runs") if w.get(k)}
            or None,
            "error": (t.get("error") or "")[:300] or None,
            "last_progress": (last.get("message") or "")[:300] or None,
            "last_progress_at": last.get("at")}

async def collect(lab, workers: Workers, broker=None, local_units=None, managers=None,
                  own=None):
    """One snapshot of the lab. `managers`: {manager-id: the secretary's token there};
    `own`: (secretary id, its UsageLog), since the secretary counts too."""
    """One snapshot of the lab (a dict, ready for JSON and the web page)."""
    agents = {}
    for aid, a in lab.agents.items():
        agents[aid] = {"id": aid, "name": lab.display_name(aid), "role": a.get("role"),
                       "human": a.get("human"), "host": a.get("host"),
                       "access": a.get("access")}
    if own is not None:
        sec_id, own_usage = own
        agents[sec_id].update(up=True, usage=own_usage.load())
    for m, token in (managers or {}).items():
        try:
            st = await manager_status(lab, m, token)
            agents[m].update(up=True, usage=st.get("usage"), github=st.get("github") or {},
                             applied=st.get("applied"), activity=st.get("activity"))
        except (httpx2.HTTPError, ValueError) as e:
            agents[m].update(up=False, error=str(e))
    for w in workers.known():
        try:
            h = await workers.call(w, "health_check", retries=0)
            tasks = await workers.call(w, "list_tasks", retries=0)
            agents[w].update(up=True, gpu=h.get("gpu"), gpus=h.get("gpus"),
                             gpu_processes=h.get("gpu_processes"), queue=h.get("tasks"),
                             keep_awake=h.get("keep_awake"), runs=h.get("runs"),
                             github=h.get("github") or {}, applied=h.get("applied"),
                             activity=h.get("activity"),
                             usage=h.get("usage"), disk_free_gb=h.get("disk_free_gb"),
                             recent=[task_summary(t) for t in (tasks or [])[-RECENT:]][::-1])
        except WorkerError as e:
            agents[w].update(up=False, error=str(e))
    for aid, unit in (local_units or {}).items():
        agents.setdefault(aid, {})["service"] = await asyncio.to_thread(_unit_state, unit)

    accounts = {}
    for hid, h in lab.humans.items():
        mine = [aid for aid, a in agents.items() if a.get("human") == hid]
        limits, tokens_today, today = {}, {}, dt.date.today().isoformat()
        measured = {}               # kind -> (the freshest reading with a percentage, when)
        for aid in mine:
            u = agents[aid].get("usage") or {}
            for kind, lim in (u.get("limits") or {}).items():
                if kind not in limits or lim.get("observed_at", "") > limits[kind].get("observed_at", ""):
                    limits[kind] = {**lim, "from": aid}
                if lim.get("utilization") is not None:
                    pct_at = lim.get("utilization_at") or lim.get("observed_at", "")
                    best = measured.get(kind)
                    if best is None or pct_at > best[1]:
                        measured[kind] = (lim, pct_at)
            for model, t in ((u.get("days") or {}).get(today) or {}).items():
                acc = tokens_today.setdefault(aid, {"new": 0, "cached": 0})
                acc["cached"] += t.get("cacheReadInputTokens", 0)
                acc["new"] += (t.get("inputTokens", 0) + t.get("outputTokens", 0)
                               + t.get("cacheCreationInputTokens", 0))
        for kind, lim in limits.items():
            m = measured.get(kind)
            if (lim.get("utilization") is None and m
                    and m[0].get("resets_at") == lim.get("resets_at")):
                lim.update(utilization=m[0]["utilization"], utilization_at=m[1])
        accounts[hid] = {"name": (h or {}).get("name") or hid, "agents": mine,
                         "limits": limits, "tokens_today": tokens_today}

    approvals = await broker_overview(*broker) if broker else {"error": "no broker configured"}
    return {"lab": lab.name, "at": _now(), "agents": agents, "accounts": accounts,
            "approvals": approvals, "applied": _applied()}


def _applied(path="/etc/agentic-lab/applied.yaml"):
    """What this host runs: the config and framework commits of the last `lab apply`."""
    import yaml
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except (OSError, ValueError):
        return {}
