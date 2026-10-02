import asyncio
from collections import deque
from dataclasses import dataclass
import hashlib
import logging
import math
import time
import uuid

from .history import Series, STATES, RETENTION, stamp
from .probes import METRICS, Probes

log = logging.getLogger("connectivity")


@dataclass
class Check:
    config: dict
    resource_id: str
    series: Series
    base: float
    due: float
    task: asyncio.Task | None = None
    latest: object = None
    expires: float | None = None
    reason: str = "awaiting_first_result"
    token: str = ""


def measurement(config):
    return {key: value for key, value in config.items() if key != "name"}


class Engine:
    def __init__(self, config, probes=None, monotonic=time.monotonic, wall=time.time):
        self.mono, self.wall = monotonic, wall
        self.anchor_mono, self.anchor_wall = self.mono(), self.wall()
        self.last_mono, self.last_wall = self.anchor_mono, self.anchor_wall
        self.started = self.anchor_wall
        self.probes = probes or Probes()
        self.config = config
        self.checks, self.resources = {}, {}
        self.notices = deque(maxlen=20)
        self.skipped = 0
        self.delays = deque(maxlen=10000)
        self.running = False
        self.scheduler = None
        self.pruned_at = self.anchor_mono
        self.error_logged = {}
        self.reconcile_now(config, self.started, self.anchor_mono)

    def now(self):
        return self.anchor_wall + self.mono() - self.anchor_mono

    def new_check(self, c, rid, now, mono, reason="created"):
        offset = int.from_bytes(hashlib.sha256(c["id"].encode()).digest()[:4], "big") / 2**32 * .9
        return Check(c, rid, Series(now, True, reason), mono + offset, mono + offset, token=uuid.uuid4().hex)

    def reconcile_now(self, config, now, mono):
        old = self.checks
        updated = {}
        resources = {}
        cancelled = []
        for r in config["resources"]:
            changed = False
            old_ids = {cid for cid, c in old.items() if c.resource_id == r["id"]}
            new_ids = {c["id"] for c in r["checks"]}
            changed |= old_ids != new_ids
            for c in r["checks"]:
                previous = old.get(c["id"])
                if previous and measurement(previous.config) == measurement(c):
                    previous.config = c
                    updated[c["id"]] = previous
                else:
                    changed = True
                    updated[c["id"]] = self.new_check(c, r["id"], now, mono, "measurement settings changed" if previous else "created")
                    if previous:
                        log.info("Check %s history reset: measurement settings changed", c["id"])
                    if previous and previous.task:
                        previous.task.cancel()
                        cancelled.append(previous.task)
            resources[r["id"]] = Series(now, reason="check collection or measurement settings changed" if r["id"] in self.resources else "created") if changed or r["id"] not in self.resources else self.resources[r["id"]]
            if changed and r["id"] in self.resources:
                log.info("Resource %s aggregate history reset", r["id"])
        for cid, check in old.items():
            if cid not in updated and check.task:
                check.task.cancel()
                cancelled.append(check.task)
        self.checks, self.resources, self.config = updated, resources, config
        for rid in resources:
            self.aggregate(rid, now)
        return cancelled

    async def reconcile(self, config, publish=None):
        now = self.clock()
        self.expire(now)
        cancelled = self.reconcile_now(config, now, self.mono())
        if publish:
            publish()
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)

    def aggregate(self, rid, now):
        states = [c.series.state for c in self.checks.values() if c.resource_id == rid]
        state = 2 if 2 in states else 0 if 0 in states or not states else 1
        self.resources[rid].change(state, now)

    def clock(self):
        mono, wall = self.mono(), self.wall()
        elapsed, wall_elapsed = mono - self.last_mono, wall - self.last_wall
        reset = wall_elapsed < 0 or abs(elapsed - wall_elapsed) > 5
        gap = elapsed > 5 or wall_elapsed > 5
        if reset or gap:
            before = self.anchor_wall + self.last_mono - self.anchor_mono
            if reset:
                self.anchor_mono, self.anchor_wall = mono, wall
                self.pruned_at = mono
                self.error_logged.clear()
            now = self.now()
            for check in self.checks.values():
                if check.task:
                    check.task.cancel()
                check.token = uuid.uuid4().hex
                check.expires = None
                check.reason = "clock_reset" if reset else "monitor_gap"
                if reset:
                    check.latest = None
                    check.series = Series(now, True, "clock_reset")
                else:
                    check.series.change(0, before)
                    check.series.flush(now)
                check.base = mono + int.from_bytes(hashlib.sha256(check.config["id"].encode()).digest()[:2], "big") / 65536 * .9
                check.due = check.base
            for rid, series in list(self.resources.items()):
                if reset:
                    self.resources[rid] = Series(now, reason="clock_reset")
                else:
                    series.change(0, before)
                    series.flush(now)
            code = "clock_reset" if reset else "monitor_gap"
            self.notices.append({"code": code, "at": stamp(now), "from": stamp(before), "message": "Clock discontinuity: retained history cleared" if reset else "Inferred unobserved monitor gap; old results invalidated"})
            log.warning("%s: old observations invalidated", code)
        self.last_mono, self.last_wall = mono, wall
        return self.anchor_wall + mono - self.anchor_mono

    def expire(self, now):
        mono = self.anchor_mono + now - self.anchor_wall
        changed = set()
        for c in sorted(self.checks.values(), key=lambda c: c.expires if c.expires is not None else math.inf):
            if c.expires is not None and mono >= c.expires:
                deadline = self.anchor_wall + c.expires - self.anchor_mono
                c.series.change(0, deadline)
                c.expires, c.reason = None, "overdue"
                changed.add(c.resource_id)
                self.aggregate(c.resource_id, deadline)
        return changed

    async def execute(self, cid, check, token, scheduled):
        start_mono = self.mono()
        start = self.now()
        self.delays.append(max(0, start_mono - scheduled) * 1000)
        try:
            result = await self.probes.run(check.config)
            now = self.clock()
            completed_mono = self.last_mono
            if self.checks.get(cid) is not check or check.token != token:
                return
            self.expire(now)
            result.started, result.completed = start, now
            check.latest = result
            check.series.record(result)
            check.series.change(1 if result.outcome == "success" else 2 if result.outcome == "failure" else 0, now)
            interval = check.config["interval_seconds"]
            next_slot = check.base + (math.floor((completed_mono - check.base) / interval) + 1) * interval
            check.expires = next_slot + check.config["timeout_seconds"] + 2
            check.reason = result.code
            self.aggregate(check.resource_id, now)
            if result.outcome == "error" and self.mono() - self.error_logged.get(cid, -math.inf) > 60:
                log.warning("Check %s monitor error: %s", cid, result.code)
                self.error_logged[cid] = self.mono()
        finally:
            if check.task is asyncio.current_task():
                check.task = None

    async def loop(self):
        while self.running:
            now = self.clock()
            self.expire(now)
            mono = self.mono()
            active = sum(c.task is not None for c in self.checks.values())
            for cid, c in self.checks.items():
                if mono < c.due:
                    continue
                interval = c.config["interval_seconds"]
                missed = math.floor((mono - c.due) / interval)
                scheduled = c.due
                c.due += (missed + 1) * interval
                if missed or c.task or active >= 200:
                    self.skipped += missed + 1
                    continue
                c.task = asyncio.create_task(self.execute(cid, c, c.token, scheduled))
                active += 1
            if mono - self.pruned_at >= 30:
                for series in [*self.resources.values(), *(c.series for c in self.checks.values())]:
                    series.prune(now)
                self.error_logged = {cid: t for cid, t in self.error_logged.items() if cid in self.checks}
                self.pruned_at = mono
            await asyncio.sleep(.02)

    async def start(self):
        self.running = True
        self.scheduler = asyncio.create_task(self.loop())

    async def stop(self):
        self.running = False
        tasks = [c.task for c in self.checks.values() if c.task]
        if self.scheduler:
            tasks.append(self.scheduler)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.probes.close()
        if hasattr(self.probes, "drain"):
            await self.probes.drain()

    def status(self, revision):
        now = self.clock()
        self.expire(now)
        resources = []
        for r in self.config["resources"]:
            checks = []
            for config in r["checks"]:
                c = self.checks[config["id"]]
                result = c.latest
                details = dict(result.details) if result else {}
                if "certificate_expires_at" in details:
                    details["certificate_remaining_seconds"] = details["certificate_expires_at"] - now
                    details["certificate_expires_at"] = stamp(details["certificate_expires_at"])
                checks.append({**config, "state": STATES[c.series.state], "reason": c.reason, "generation": c.series.generation, "history_from": stamp(max(c.series.created, now - RETENTION)), "reset_at": stamp(c.series.created), "reset_reason": c.series.reset_reason, "latest_completion": stamp(result.completed) if result else None, "fresh_until": stamp(self.anchor_wall + c.expires - self.anchor_mono) if c.expires else None, "metric": METRICS[config["type"]], "latest": {"outcome": result.outcome, "code": result.code, "message": result.message, "elapsed_seconds": result.elapsed, "latency_ms": result.latency, "latency_upper_bound": result.bounded, **details} if result else None, "recent_errors": [{**error, "time": stamp(error["time"])} for error in c.series.errors if error["time"] >= now - 3600]})
            s = self.resources[r["id"]]
            resources.append({"id": r["id"], "name": r["name"], "state": STATES[s.state], "checks": checks, "check_counts": {state: sum(c["state"] == state for c in checks) for state in STATES}, "latest_completion": max((c["latest_completion"] for c in checks if c["latest_completion"]), default=None), "history_from": stamp(max(s.created, now - RETENTION)), "reset_at": stamp(s.created), "reset_reason": s.reset_reason})
        return {"server_time": stamp(now), "started_at": stamp(self.started), "config_revision": revision, "refresh_interval_seconds": self.config["ui"]["refresh_interval_seconds"], "retention_from": stamp(now - RETENTION), "raw_from": stamp(now - 3600), "resources": resources, "health": {"skipped_starts": self.skipped, "active_checks": sum(c.task is not None for c in self.checks.values()), "dns_workers_active": self.probes.resolver.active, "notices": list(self.notices)}}
