import asyncio
import copy
import logging
import secrets
import time
import uuid

from .config import Problem
from .history import stamp
from .speed_protocol import PROFILE, CHUNK, Sender, Meter, SpeedError, integer, validate_metric, validate_rtt

log = logging.getLogger("connectivity.speedtest")


def peer_settings(peer):
    return peer["url"], peer["password"]


class Run:
    def __init__(self, manager, mode, owner=None, peer=None, initiator=None, budget=None):
        self.manager, self.mode, self.owner = manager, mode, owner
        self.id = uuid.uuid4().hex
        self.started = time.time()
        self.clock = time.monotonic()
        self.limit = min(budget or manager.profile.timeout_seconds, manager.profile.timeout_seconds)
        self.peer = {k: peer[k] for k in ("id", "name", "url")} if peer else None
        self.peer_generation = manager.generations.get(peer["id"]) if peer else None
        self.phase, self.outcome = "preparing", "running"
        self.initiator = initiator or manager.instance
        self.responder = manager.instance if mode != "node" else None
        self.metrics = {key: None for key in ("idle_rtt", "download", "download_rtt", "upload", "upload_rtt")}
        self.live = None
        self.error = None
        self.completed = None
        self.elapsed = None
        self.task = None
        self.control = None
        self.connections = {}
        self.channels = {}
        self.joined = set()
        self.tasks = set()
        self.pending_sockets = 0
        self.direction = None
        self.phase_clock = self.clock
        self.last_activity = self.clock
        self.last_progress = 0
        self.meter = None
        self.sent = 0
        self.ending_data = False
        self.finishing = False
        self.finished = asyncio.Event()
        self.finish_task = None
        self.window_announced = self.end_announced = False
        self.cancel_reason = None
        self.terminal_choice = None
        self.control_count = 0
        self.control_tick = self.clock

    def view(self, detail=True):
        metrics = copy.deepcopy(self.metrics)
        if not detail:
            for key in ("download", "upload"):
                if metrics[key]: metrics[key].pop("bucket_bytes", None)
        return {"id": self.id, "mode": self.mode, "peer": self.peer, "peer_generation": self.peer_generation, "phase": self.phase, "outcome": self.outcome,
                "initiator_instance": self.initiator, "responder_instance": self.responder,
                "profile": self.manager.profile.wire(), "started_at": stamp(self.started),
                "deadline_at": stamp(self.started+self.limit), "completed_at": stamp(self.completed),
                "elapsed_seconds": self.elapsed if self.elapsed is not None else max(0,time.monotonic()-self.clock),
                "metrics": metrics, "live": copy.deepcopy(self.live), "error": self.error}

    async def notify(self, event, **data):
        if self.control is not None and not self.control.closed:
            async with asyncio.timeout(1):
                await self.control.send_json({"event": event, **data})

    async def close_data(self, code=1000, force=False):
        self.ending_data = True
        for channel in self.channels.values():
            if channel["sender"]: channel["sender"].stop()
        for task in list(self.tasks): task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        channels = list(self.channels.values())
        if force:
            for channel in channels:
                if channel["transport"]: channel["transport"].abort()
        try:
            async with asyncio.timeout(1):
                await asyncio.gather(*(c["ws"].close(code=code, drain=False) for c in channels), return_exceptions=True)
        except TimeoutError:
            pass
        finally:
            for channel in channels:
                if channel["transport"]: channel["transport"].abort()
            self.channels.clear()

    async def pump(self, channel):
        try:
            await channel["sender"].pump()
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self.ending_data and not self.finishing:
                asyncio.create_task(self.manager.finish(self, "failed", "stream_failed", "A payload stream failed"))

    async def command(self, op, data):
        if self.finishing or self.outcome != "running":
            raise SpeedError("run_finished", "Run is no longer active")
        fields = {"keepalive":set(), "echo":{"nonce"}, "idle_done":{"rtt"}, "prepare":{"direction"}, "go":set(), "stop_download":{"metric","rtt"}, "stop_upload":{"rtt"}, "finish":set()}
        if op not in fields or set(data) != fields[op]:
            raise SpeedError("protocol_error", "Unsupported command fields")
        now = time.monotonic()
        if now-self.clock >= self.limit or time.time()-self.started >= self.limit:
            raise SpeedError("timeout", "Overall speedtest deadline exceeded")
        self.last_activity = now
        if now-self.control_tick >= 1:
            self.control_tick, self.control_count = now, 0
        self.control_count += 1
        if self.control_count > 64:
            raise SpeedError("protocol_error", "Control message rate exceeded")
        if op == "keepalive": return {}
        if op == "echo":
            nonce = data.get("nonce")
            if not isinstance(nonce, str) or len(nonce) > 64:
                raise SpeedError("protocol_error", "Invalid echo nonce")
            active = self.meter and self.meter.begin <= now < self.meter.end
            return {"nonce": nonce, "window": "upload" if active else None,
                    "remaining_seconds": self.meter.end-now if active else 0}
        if op == "idle_done" and self.phase == "idle":
            self.metrics["idle_rtt"] = validate_rtt(data.get("rtt"), self.manager.profile)
            if not self.metrics["idle_rtt"]["samples"]:
                raise SpeedError("no_idle_samples", "No valid idle latency samples")
            self.phase = "idle_done"
            return {}
        if op == "prepare" and data.get("direction") in ("download", "upload"):
            direction = data["direction"]
            if self.phase != ("idle_done" if direction == "download" else "download_done"):
                raise SpeedError("protocol_error", "Unexpected direction or phase")
            self.direction, self.phase = direction, direction+"_setup"
            self.phase_clock, self.ending_data = now, False
            self.channels = {}
            self.meter = None
            return {}
        if op == "go" and self.direction and self.phase == self.direction+"_setup":
            if len(self.channels) != self.manager.profile.streams or any(c["ws"].closed for c in self.channels.values()):
                raise SpeedError("stream_setup", "All four streams must be ready")
            self.phase_clock, self.phase = now, self.direction+"_warmup"
            if self.direction == "download":
                for channel in self.channels.values():
                    channel["sender"] = Sender(channel["ws"], secrets.token_bytes(CHUNK))
                    task = asyncio.create_task(self.pump(channel))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
            else:
                self.meter = Meter(self.manager.profile, now)
                self.window_announced = self.end_announced = False
            return {}
        if op == "stop_download" and self.direction == "download" and self.phase in ("download", "download_warmup"):
            sent = sum(c["sender"].sent for c in self.channels.values())
            self.metrics["download"] = validate_metric(data.get("metric"), self.manager.profile, sent)
            self.metrics["download_rtt"] = validate_rtt(data.get("rtt"), self.manager.profile)
            await self.close_data()
            self.phase, self.live = "download_done", None
            return {"metric": self.metrics["download"]}
        if op == "stop_upload" and self.direction == "upload" and self.meter and now >= self.meter.end:
            self.metrics["upload"] = validate_metric(self.meter.snapshot(now, complete=True), self.manager.profile)
            self.metrics["upload_rtt"] = validate_rtt(data.get("rtt"), self.manager.profile)
            await self.close_data()
            self.meter, self.live, self.phase = None, None, "upload_done"
            return {"metric": self.metrics["upload"]}
        if op == "finish" and self.phase == "upload_done":
            complete = self.manager.complete_metrics(self.metrics)
            self.terminal_choice = ("completed", None, "") if complete else ("failed", "latency_unavailable", "A required latency metric is unavailable")
            return {"metrics": copy.deepcopy(self.metrics), "outcome": self.terminal_choice[0]}
        raise SpeedError("protocol_error", "Unexpected speedtest command or phase")


class SpeedManager:
    def __init__(self, store, resolver, ssl_context, owner_valid, profile=PROFILE):
        self.store, self.resolver, self.ssl_context = store, resolver, ssl_context
        self.owner_valid, self.profile = owner_valid, profile
        self.instance = uuid.uuid4().hex
        self.active = None
        self.latest = {}
        self.generations = {p["id"]:uuid.uuid4().hex for p in store.data["speedtest"]["peers"]}
        self.receipt = None
        self.watchdog = None
        self.stopping = False

    def capabilities(self):
        return {"instance": self.instance, "protocol": 1, "profile": self.profile.wire()}

    def _admit(self, **options):
        if self.stopping:
            raise Problem("stopping", "Node is shutting down", 503)
        if self.active:
            raise Problem("test_busy", "This node is already participating in a speed test", 409)
        run = Run(self, **options)
        self.active = run
        if self.watchdog is None:
            self.watchdog = asyncio.create_task(self.watch())
        log.info("Speedtest %s accepted (%s)", run.id, run.mode)
        return run

    def start_browser(self, owner):
        return self._admit(mode="browser", owner=owner, initiator="browser-"+uuid.uuid4().hex)

    def accept(self, owner, instance, budget):
        if instance == self.instance:
            raise Problem("self_test", "A node cannot test itself", 422)
        return self._admit(mode="incoming", owner=owner, initiator=instance, budget=budget)

    def start_node(self, peer_id):
        peer = next((p for p in self.store.data["speedtest"]["peers"] if p["id"] == peer_id), None)
        if not peer: raise Problem("not_found", "Peer not found", 404)
        run = self._admit(mode="node", peer=peer)
        self.latest.pop(peer_id, None)
        run.task = asyncio.create_task(self.outgoing(run, copy.deepcopy(peer)))
        return run

    async def outgoing(self, run, peer):
        from .speed_client import run_peer
        try:
            async with asyncio.timeout(run.limit):
                await run_peer(self, run, peer)
            outcome = "completed" if self.complete_metrics(run.metrics) else "failed"
            await self.finish(run, outcome, None if outcome == "completed" else "latency_unavailable", "One or more required latency measurements are unavailable")
        except asyncio.CancelledError:
            reason = run.cancel_reason or ("cancelled", "cancelled", "Test canceled")
            await self.finish(run, *reason)
        except SpeedError as exc:
            await self.finish(run, "cancelled" if exc.code == "peer_cancelled" else "failed", exc.code, exc.message)
        except TimeoutError:
            await self.finish(run, "failed", "timeout", "Overall speedtest deadline exceeded")
        except Exception:
            await self.finish(run, "failed", "executor", "Speedtest executor could not complete the measurement")

    @staticmethod
    def complete_metrics(metrics):
        return all(metrics.get(key) and metrics[key].get("samples", 0) for key in ("idle_rtt", "download_rtt", "upload_rtt")) and all(metrics.get(key) and metrics[key].get("complete") for key in ("download", "upload"))

    def result(self, run_id):
        if self.active and self.active.id == run_id: return self.active.view()
        if self.receipt and self.receipt[0] <= time.monotonic(): self.receipt = None
        if self.receipt and self.receipt[1]["id"] == run_id: return copy.deepcopy(self.receipt[1])
        for result in self.latest.values():
            if result["id"] == run_id: return copy.deepcopy(result)
        raise Problem("run_expired", "Run not found or no longer retained", 404)

    def state(self):
        latest = []
        for peer in self.store.data["speedtest"]["peers"]:
            value = self.latest.get(peer["id"])
            if value:
                item = copy.deepcopy(value)
                for key in ("download", "upload"):
                    if item["metrics"][key]: item["metrics"][key].pop("bucket_bytes", None)
                latest.append(item)
        return {**self.capabilities(), "config_revision": self.store.revision, "active": self.active.view(False) if self.active else None, "latest": latest}

    def validate_edit(self, candidate):
        run = self.active
        if run and run.peer:
            old = next(p for p in self.store.data["speedtest"]["peers"] if p["id"] == run.peer["id"])
            new = next((p for p in candidate["speedtest"]["peers"] if p["id"] == old["id"]), None)
            if new is None or peer_settings(old) != peer_settings(new):
                raise Problem("peer_in_use", "Finish or cancel this peer's active test before changing its URL/password or deleting it", 409)

    def reconcile(self, candidate):
        old = {p["id"]: p for p in self.store.data["speedtest"]["peers"]}
        new = {p["id"]: p for p in candidate["speedtest"]["peers"]}
        for pid in list(self.generations):
            if pid not in new: del self.generations[pid]
        for pid,peer in new.items():
            if pid not in old or peer_settings(old[pid]) != peer_settings(peer): self.generations[pid] = uuid.uuid4().hex
        for pid in list(self.latest):
            if pid not in new or pid not in old or peer_settings(old[pid]) != peer_settings(new[pid]):
                del self.latest[pid]

    async def cancel(self, run_id, outcome="cancelled", code="cancelled", message="Test canceled"):
        run = self.active
        if not run or run.id != run_id: return self.result(run_id)
        if run.mode == "node" and run.task and run.task is not asyncio.current_task():
            if not run.cancel_reason:
                run.cancel_reason = (outcome, code, message)
                run.task.cancel()
            await asyncio.shield(run.finished.wait())
        else:
            await self.finish(run, outcome, code, message)
        return run.view()

    async def finish(self, run, outcome, code=None, message=""):
        if run.finish_task is None:
            run.finishing = True
            run.finish_task = asyncio.create_task(self._finish(run, outcome, code, message))
            run.finish_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await asyncio.shield(run.finish_task)

    async def _finish(self, run, outcome, code, message):
        if run.terminal_choice:
            outcome, code, message = run.terminal_choice
        else:
            run.terminal_choice = (outcome, code, message)
        force = time.monotonic()-run.clock >= run.limit or time.time()-run.started >= run.limit
        try:
            await run.close_data(code=4001 if outcome == "cancelled" else 1011 if outcome == "failed" else 1000, force=force)
            run.outcome, run.phase = outcome, "finished"
            run.elapsed = max(0,time.monotonic()-run.clock)
            run.completed = run.started+run.elapsed
            run.live = None
            run.error = {"code": code, "message": message[:512]} if code else None
            result = run.view()
            try:
                if not force: await run.notify("terminal", result=result)
            except (TimeoutError, ConnectionError, RuntimeError):
                pass
            connections = list(run.connections.items())
            if force:
                for _,transport in connections:
                    if transport: transport.abort()
            try:
                async with asyncio.timeout(1):
                    await asyncio.gather(*(ws.close(code=4001 if outcome == "cancelled" else 1000, drain=False) for ws,_ in connections if ws.prepared), return_exceptions=True)
            except TimeoutError:
                pass
            finally:
                for _,transport in connections:
                    if transport: transport.abort()
            if run.mode == "node" and run.peer and self.generations.get(run.peer["id"]) == run.peer_generation:
                self.latest[run.peer["id"]] = result
            elif run.mode != "node":
                self.receipt = (time.monotonic()+5, result)
            log.info("Speedtest %s finished: %s (%s)", run.id, outcome, code or "ok")
        finally:
            if self.active is run: self.active = None
            run.finished.set()

    async def watch(self):
        last_mono, last_wall = time.monotonic(), time.time()
        while not self.stopping:
            await asyncio.sleep(.05)
            now, wall = time.monotonic(), time.time()
            run = self.active
            if run and not run.finishing:
                reason = None
                if now-last_mono > 5 or wall-last_wall < 0 or abs((wall-last_wall)-(now-last_mono)) > 5:
                    reason = ("failed", "timing_invalid", "Clock or process gap invalidated measurement timing")
                elif now-run.clock >= run.limit or wall-run.started >= run.limit:
                    reason = ("failed", "timeout", "Overall speedtest deadline exceeded")
                elif run.mode != "node":
                    if not self.owner_valid(run.owner):
                        reason = ("cancelled", "session_expired", "Test session expired or was revoked")
                    elif run.control is None and now-run.clock > self.profile.setup_seconds:
                        reason = ("failed", "setup_timeout", "Control connection was not established")
                    elif run.control is not None and now-run.last_activity > self.profile.liveness_seconds:
                        reason = ("cancelled" if run.mode == "browser" else "failed", "connection_lost", "Test participant stopped responding")
                    elif run.phase.endswith("_setup") and now-run.phase_clock > self.profile.channel_seconds:
                        reason = ("failed", "stream_setup", "Four payload streams were not started in time")
                    elif run.phase in ("download_warmup", "download", "upload_warmup", "upload"):
                        if now-run.phase_clock > self.profile.warmup_seconds+self.profile.measurement_seconds+self.profile.liveness_seconds:
                            reason = ("failed", "phase_timeout", "The measured phase was not finalized in time")
                        elif now-run.phase_clock >= self.profile.warmup_seconds:
                            run.phase = run.direction
                if reason:
                    await self.cancel(run.id, *reason)
                elif run.mode != "node" and run.control is not None:
                    try:
                        if run.meter:
                            run.live = run.meter.snapshot(now)
                            if now >= run.meter.begin and not run.window_announced:
                                run.window_announced = True
                                await run.notify("window", direction="upload", state="start", remaining_seconds=max(0,run.meter.end-now))
                            if now >= run.meter.end and not run.end_announced:
                                run.end_announced = True
                                await run.notify("window", direction="upload", state="end")
                        if now-run.last_progress >= 1:
                            run.last_progress = now
                            await run.notify("progress", phase=run.phase, live=run.live)
                    except (TimeoutError, ConnectionError, RuntimeError):
                        await self.cancel(run.id, "cancelled" if run.mode == "browser" else "failed", "connection_lost", "Control connection was lost")
            last_mono, last_wall = now, wall
            if self.receipt and self.receipt[0] <= now: self.receipt = None

    async def stop(self):
        if self.stopping: return
        self.stopping = True
        if self.active: await self.cancel(self.active.id)
        if self.watchdog:
            self.watchdog.cancel()
            await asyncio.gather(self.watchdog, return_exceptions=True)
        self.latest.clear(); self.receipt = None
