"""Simulate one live class run against a running API. It spends real AI money.

Publishes the seeded mission to the seeded class as a fresh live run, joins N seeded students,
starts the run, plays every session to its end, waits for each evaluation, closes the run and
prints the latency budgets. Needs NALAR_SEED_PASSWORD and NALAR_SUPABASE_ANON_KEY.
"""

import argparse
import asyncio
import contextlib
import json
import os
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

SEED = json.loads(
    (Path(__file__).resolve().parents[1] / "supabase/seed/gaya_dan_gerak.json").read_text(
        encoding="utf-8"
    )
)
ANSWERS = [
    "Karena ada gaya gesek antara kelereng dan lantai.",
    "Menurutku gayanya habis, jadi kelereng berhenti.",
    "Kalau lantainya licin, kelereng bergerak lebih jauh karena gesekannya kecil.",
    "Aku belum yakin, mungkin karena lantainya kasar.",
]
BUDGETS = {
    "ack_p95": 0.3,
    "ack_server_p95": 0.3,
    "turn_p50": 3.0,
    "turn_p95": 5.0,
    "evaluation_p95": 60.0,
    "monitor_p95": 1.0,
}
POLL_S = 0.7
MONITOR_POLL_S = 3.0
TURN_GIVE_UP_S = 90.0
EVALUATION_GIVE_UP_S = 400.0
LOGIN_ATTEMPTS = 10
LOGIN_BACKOFF_S = 30.0


@dataclass
class Timings:
    acks: list[float] = field(default_factory=list)
    server_acks: list[float] = field(default_factory=list)
    turns: list[float] = field(default_factory=list)
    evaluations: list[float] = field(default_factory=list)
    monitor: list[float] = field(default_factory=list)
    endings: Counter[str] = field(default_factory=Counter)


def pct(values: list[float], q: int) -> float:
    if not values:
        return float("nan")
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


async def login(auth: httpx.AsyncClient, email: str, password: str) -> dict[str, str]:
    for _ in range(LOGIN_ATTEMPTS):
        response = await auth.post(
            "/auth/v1/token",
            params={"grant_type": "password"},
            json={"email": email, "password": password},
        )
        if response.status_code != 429:
            break
        # Supabase Auth limits sign-ins per IP; wait out the window instead of failing the run.
        wait = float(response.headers.get("Retry-After", LOGIN_BACKOFF_S))
        print(f"sign-in rate limited, waiting {wait:.0f}s")
        await asyncio.sleep(wait)
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def server_seconds(response: httpx.Response) -> float:
    timing = response.headers.get("Server-Timing", "")
    _, _, duration = timing.partition(";dur=")
    return float(duration) / 1000 if duration else float("nan")


async def settled_state(
    api: httpx.AsyncClient, url: str, headers: dict[str, str]
) -> dict[str, Any]:
    deadline = time.perf_counter() + TURN_GIVE_UP_S
    while True:
        response = await api.get(f"{url}/state", headers=headers)
        if response.is_error:
            return {"status": f"state_http_{response.status_code}"}
        body: dict[str, Any] = response.json()
        if body["status"] != "processing" or time.perf_counter() > deadline:
            return body
        await asyncio.sleep(POLL_S)


async def play(
    api: httpx.AsyncClient, headers: dict[str, str], run_id: str, turns: int, t: Timings
) -> None:
    while True:
        lobby = (await api.get(f"/student/runs/{run_id}/lobby", headers=headers)).json()
        if lobby.get("session_id"):
            break
        await asyncio.sleep(POLL_S)
    url = f"/student/sessions/{lobby['session_id']}"
    current = await settled_state(api, url, headers)
    for n in range(turns + 1):
        if current["status"] != "awaiting_answer":
            break
        started = time.perf_counter()
        response = await api.post(
            f"{url}/answers",
            json={
                "turn_index": current["turn_index"],
                "answer_text": ANSWERS[n % len(ANSWERS)],
                "client_submission_id": str(uuid4()),
            },
            headers=headers,
        )
        t.acks.append(time.perf_counter() - started)
        t.server_acks.append(server_seconds(response))
        if response.is_error:
            t.endings[f"answer_http_{response.status_code}"] += 1
            return
        current = await settled_state(api, url, headers)
        t.turns.append(time.perf_counter() - started)
    if current["status"] == "processing":
        t.endings["stuck_processing"] += 1
        return
    ended = time.perf_counter()
    deadline = ended + EVALUATION_GIVE_UP_S
    while (reflection := await api.get(f"{url}/reflection", headers=headers)).status_code == 202:
        if time.perf_counter() > deadline:
            t.endings["evaluation_timed_out"] += 1
            return
        await asyncio.sleep(1)
    t.evaluations.append(time.perf_counter() - ended)
    t.endings[f"{current['status']}/reflection_{reflection.status_code}"] += 1


async def watch_monitor(
    api: httpx.AsyncClient,
    teacher: dict[str, str],
    publication_id: str,
    t: Timings,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        started = time.perf_counter()
        response = await api.get(f"/publications/{publication_id}/monitor", headers=teacher)
        t.monitor.append(time.perf_counter() - started)
        if response.is_error:
            t.endings[f"monitor_http_{response.status_code}"] += 1
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), MONITOR_POLL_S)


async def publish(api: httpx.AsyncClient, teacher: dict[str, str]) -> tuple[str, str]:
    response = await api.post(
        "/publications",
        json={
            "mission_version_id": SEED["mission"]["version_id"],
            "class_id": SEED["class"]["id"],
            "run": {"mode": "live", "planner_mode": "table"},
        },
        headers=teacher,
    )
    response.raise_for_status()
    body = response.json()
    return str(body["publication_id"]), str(body["run_id"])


async def main(args: argparse.Namespace) -> bool:
    password = os.environ["NALAR_SEED_PASSWORD"]
    anon = os.environ["NALAR_SUPABASE_ANON_KEY"]
    timings = Timings()
    async with (
        httpx.AsyncClient(base_url=args.supabase_url, headers={"apikey": anon}, timeout=30) as auth,
        httpx.AsyncClient(base_url=f"{args.api_url}/api/v1", timeout=30) as api,
    ):
        teacher = await login(auth, "guru@demo.nalar.id", password)
        students = [
            await login(auth, f"siswa{n:02d}@demo.nalar.id", password)
            for n in range(1, args.students + 1)
        ]
        publication_id, run_id = (None, args.run_id) if args.run_id else await publish(api, teacher)
        lobby = await api.post(f"/runs/{run_id}/open-lobby", headers=teacher)
        if lobby.status_code == 409 and not args.run_id:
            # Publishing is idempotent, so an interrupted test hands back its still-open run.
            await api.post(f"/runs/{run_id}/close", headers=teacher)
            publication_id, run_id = await publish(api, teacher)
            lobby = await api.post(f"/runs/{run_id}/open-lobby", headers=teacher)
        lobby.raise_for_status()
        stop = asyncio.Event()
        projector = (
            asyncio.create_task(watch_monitor(api, teacher, publication_id, timings, stop))
            if publication_id
            else None
        )
        if projector is None:
            print("monitor poll skipped: --run-id gives no publication id")
        try:
            for headers in students:
                joined = await api.post(
                    "/student/runs/join",
                    json={"join_code": lobby.json()["join_code"]},
                    headers=headers,
                )
                joined.raise_for_status()
            started = await api.post(f"/runs/{run_id}/start", headers=teacher)
            started.raise_for_status()
            print(f"run {run_id}: started {started.json()['started_count']} sessions")
            await asyncio.gather(
                *(play(api, headers, run_id, args.turns, timings) for headers in students)
            )
        finally:
            stop.set()
            if projector:
                await projector
            await api.post(f"/runs/{run_id}/close", headers=teacher)
    report = {
        "ack_p50": pct(timings.acks, 50),
        "ack_p95": pct(timings.acks, 95),
        "ack_server_p50": pct(timings.server_acks, 50),
        "ack_server_p95": pct(timings.server_acks, 95),
        "turn_p50": pct(timings.turns, 50),
        "turn_p95": pct(timings.turns, 95),
        "evaluation_p50": pct(timings.evaluations, 50),
        "evaluation_p95": pct(timings.evaluations, 95),
        "monitor_p50": pct(timings.monitor, 50),
        "monitor_p95": pct(timings.monitor, 95),
    }
    within = True
    for name, value in report.items():
        budget = BUDGETS.get(name)
        missed = budget is not None and not value <= budget
        within = within and not missed
        verdict = "" if budget is None else (f"  OVER {budget}s" if missed else "  ok")
        print(f"{name:>15}: {value:6.2f}s{verdict}")
    print(f"{'answers':>15}: {len(timings.acks)}")
    print(f"{'endings':>15}: {dict(timings.endings)}")
    return within


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--supabase-url", required=True)
    parser.add_argument(
        "--run-id", help="an existing scheduled live run; default: publish a fresh one"
    )
    parser.add_argument("--students", type=int, default=32)
    parser.add_argument("--turns", type=int, default=6)
    parser.add_argument("--yes-spend", action="store_true", help="the user approved the AI spend")
    parsed = parser.parse_args()
    if parsed.students > 3 and not parsed.yes_spend:
        raise SystemExit(
            "more than 3 students costs real money: get approval, then pass --yes-spend"
        )
    sys.exit(0 if asyncio.run(main(parsed)) else 1)
