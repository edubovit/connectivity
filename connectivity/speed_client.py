import asyncio
import json
import secrets
import socket
import ssl
import time
from urllib.parse import urlsplit

import aiohttp

from .speed_protocol import ACK_STEP, CHUNK, VERSION, Meter, Sender, SpeedError, decode, finite, rtt_summary, validate_metric, validate_live, valid_id


class PeerResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, resolver):
        self.resolver = resolver

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        selected, address = await self.resolver.resolve(host, port)
        return [{"hostname": host, "host": address[0], "port": port, "family": selected, "proto": socket.IPPROTO_TCP, "flags": socket.AI_NUMERICHOST}]

    async def close(self):
        pass


class Control:
    def __init__(self, ws, profile, update):
        self.ws, self.profile, self.update = ws, profile, update
        self.pending = {}
        self.sequence = 0
        self.finish_request = None
        self.window_start, self.window_end = asyncio.Event(), asyncio.Event()
        self.closed = False
        self.failure = None
        self.reader = asyncio.create_task(self.read())
        self.heartbeat = asyncio.create_task(self.keepalive())

    async def read(self):
        try:
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT or len(message.data) > 32768:
                    raise SpeedError("protocol_error", "Invalid peer control frame")
                value = decode(message.data)
                if "id" in value:
                    future = self.pending.get(value["id"])
                    if future and not future.done():
                        if not isinstance(value.get("data"), dict): raise SpeedError("protocol_error", "Invalid control response")
                        future.set_result(value["data"])
                elif value.get("event") == "window" and value.get("direction") == "upload":
                    if value.get("state") == "start": self.window_start.set()
                    elif value.get("state") == "end": self.window_end.set()
                elif value.get("event") == "progress":
                    self.update(value.get("phase"), value.get("live"))
                elif value.get("event") == "terminal":
                    result = value.get("result", {})
                    finish = self.pending.get(self.finish_request)
                    if finish is not None and not finish.done():
                        finish.set_result({"metrics": result.get("metrics"), "outcome": result.get("outcome")})
                    elif result.get("outcome") != "completed":
                        raise SpeedError("peer_cancelled" if result.get("outcome") == "cancelled" else "peer_stopped", "The peer stopped the test; completed local measurements are retained")
                elif "error" in value:
                    raise SpeedError("protocol_error", "Peer rejected the control operation")
        except Exception as exc:
            self.failure = exc if isinstance(exc, SpeedError) else SpeedError("connection_lost", "Peer control connection failed")
        finally:
            self.closed = True
            self.window_start.set(); self.window_end.set()
            for future in self.pending.values():
                if not future.done(): future.set_exception(self.failure or SpeedError("connection_lost", "Peer control connection closed"))

    def check(self):
        if self.failure: raise self.failure
        if self.closed: raise SpeedError("connection_lost", "Peer control connection closed")

    async def command(self, op, data=None, timeout=3):
        self.check()
        if len(self.pending) >= 8: raise SpeedError("protocol_error", "Too many pending control operations")
        self.sequence += 1
        sequence = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[sequence] = future
        if op == "finish": self.finish_request = sequence
        try:
            async with asyncio.timeout(timeout):
                await self.ws.send_json({"id": sequence, "op": op, "data": data or {}})
                return await future
        finally:
            self.pending.pop(sequence, None)

    async def keepalive(self):
        try:
            while not self.closed:
                await asyncio.sleep(1)
                await self.command("keepalive", timeout=2)
        except (SpeedError, TimeoutError, ConnectionError):
            self.failure = self.failure or SpeedError("connection_lost", "Peer stopped answering control requests")

    async def close(self):
        self.heartbeat.cancel()
        await asyncio.gather(self.heartbeat, return_exceptions=True)
        await self.ws.close()
        self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)


class Latency:
    def __init__(self, control, profile, window=None, upload=False):
        self.control, self.profile, self.window, self.upload = control, profile, window, upload
        self.samples = []
        self.timeouts = self.discarded = 0
        self.inflight = False
        self.task = None

    def summary(self):
        return rtt_summary(self.samples, self.timeouts, self.discarded)

    async def measure(self, idle=False):
        deadline = time.monotonic()+self.profile.idle_seconds if idle else self.window.end if self.window else time.monotonic()+self.profile.measurement_seconds
        interval = self.profile.idle_interval if idle else self.profile.loaded_interval
        count = self.profile.idle_samples if idle else 100
        if self.window and not idle:
            await asyncio.sleep(max(0,self.window.begin-time.monotonic()))
        attempts = 0
        while time.monotonic() < deadline and attempts < count:
            start = time.monotonic()
            nonce = secrets.token_hex(8)
            attempts += 1
            self.inflight = True
            try:
                reply = await self.control.command("echo", {"nonce": nonce}, self.profile.rtt_timeout)
                end = time.monotonic()
                if reply.get("nonce") != nonce: raise SpeedError("protocol_error", "Echo nonce did not match")
                valid = end <= deadline
                if self.upload:
                    valid &= reply.get("window") == "upload" and finite(reply.get("remaining_seconds")) and reply["remaining_seconds"] >= end-start
                if end-start > self.profile.rtt_timeout: self.timeouts += 1
                elif valid: self.samples.append((end-start)*1000)
                else: self.discarded += 1
            except TimeoutError:
                self.timeouts += 1
            except asyncio.CancelledError:
                self.discarded += 1
                raise
            finally:
                self.inflight = False
            await asyncio.sleep(max(0,interval-(time.monotonic()-start)))

    def start(self):
        self.task = asyncio.create_task(self.measure())
        return self

    async def stop(self):
        if self.task:
            if not self.task.done(): self.task.cancel()
            results = await asyncio.gather(self.task, return_exceptions=True)
            if results and isinstance(results[0], Exception): raise results[0]
        return self.summary()


class DataChannels:
    def __init__(self, connect, direction):
        self.connect, self.direction = connect, direction
        self.sockets, self.tasks, self.senders = [], [], []
        self.meter = None
        self.failure = None
        self.closing = False

    async def open(self, count):
        for index in range(count):
            ws = await self.connect("data", self.direction, index)
            self.sockets.append(ws)
            sender = Sender(ws, secrets.token_bytes(CHUNK)) if self.direction == "upload" else None
            if sender: self.senders.append(sender)
            self.tasks.append(asyncio.create_task(self.read(ws, sender)))

    async def read(self, ws, sender):
        total = acked = 0
        try:
            async for message in ws:
                if self.closing: break
                if self.direction == "download":
                    if message.type != aiohttp.WSMsgType.BINARY or len(message.data) != CHUNK or self.meter is None:
                        raise SpeedError("protocol_error", "Invalid download payload")
                    self.meter.add(len(message.data))
                    total += len(message.data)
                    if total-acked >= ACK_STEP:
                        acked = total
                        await ws.send_json({"ack": total})
                else:
                    if message.type != aiohttp.WSMsgType.TEXT or len(message.data) > 128:
                        raise SpeedError("protocol_error", "Invalid upload acknowledgement")
                    value = decode(message.data)
                    if set(value) != {"ack"}: raise SpeedError("protocol_error", "Invalid acknowledgement")
                    sender.acknowledge(value["ack"])
                await asyncio.sleep(0)
        except Exception as exc:
            if not self.closing:
                self.failure = exc if isinstance(exc, SpeedError) else SpeedError("stream_failed", "A peer payload stream failed")
        finally:
            if not self.closing and not self.failure:
                self.failure = SpeedError("peer_cancelled", "The peer canceled the test") if ws.close_code == 4001 else SpeedError("stream_failed", "A peer payload stream closed early")

    async def pump(self, sender):
        try: await sender.pump()
        except asyncio.CancelledError: raise
        except Exception:
            if not self.closing: self.failure = SpeedError("stream_failed", "Upload stream failed")

    def upload(self):
        self.tasks.extend(asyncio.create_task(self.pump(sender)) for sender in self.senders)

    def check(self):
        if self.failure: raise self.failure

    async def close(self):
        self.closing = True
        for sender in self.senders: sender.stop()
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.gather(*(ws.close() for ws in self.sockets), return_exceptions=True)


async def run_peer(manager, run, peer):
    url = peer["url"]
    parts = urlsplit(url)
    origin = parts.scheme+"://"+parts.netloc
    api = url+"api/v1"
    trace = aiohttp.TraceConfig()
    async def reject_redirect(*_):
        raise SpeedError("peer_redirect", "Peer redirected the request; configure its canonical base URL")
    trace.on_request_redirect.append(reject_redirect)
    connector = aiohttp.TCPConnector(resolver=PeerResolver(manager.resolver), ssl=manager.ssl_context, limit=8, ttl_dns_cache=60)
    session = aiohttp.ClientSession(connector=connector, cookie_jar=aiohttp.CookieJar(unsafe=True), trust_env=False, trace_configs=[trace], timeout=aiohttp.ClientTimeout(total=5), headers={"Origin": origin})
    csrf = None
    remote_id = None
    control = None
    data = None
    latency = None

    async def call(path, method="GET", body=None):
        headers = {"X-CSRF-Token": csrf or ""}
        async with session.request(method, api+path, json=body, headers=headers, allow_redirects=False) as response:
            if response.status in (401,403): raise SpeedError("peer_auth_failed", "Peer rejected authentication or requires an unsupported external login")
            if response.status == 429: raise SpeedError("peer_rate_limited", "Peer rate limit reached; wait before manually retrying")
            if response.status == 409: raise SpeedError("peer_busy", "Peer is already participating in a speed test")
            if 300 <= response.status < 400: raise SpeedError("peer_redirect", "Peer redirected the request; use its canonical base URL")
            if response.status == 404: raise SpeedError("peer_unsupported", "Peer does not support this speedtest protocol")
            if response.status not in (200,202): raise SpeedError("peer_protocol", "Peer could not accept this protocol operation")
            payload = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                if len(payload)+len(chunk) > 65536: raise SpeedError("peer_protocol", "Peer control response exceeded its limit")
                payload.extend(chunk)
            try: return decode(payload.decode("utf-8"))
            except UnicodeError: raise SpeedError("peer_protocol", "Peer did not return valid JSON") from None

    async def connect(role, direction=None, index=None):
        ws = await session.ws_connect(api+"/speedtest/channel", origin=origin, compress=0, max_msg_size=CHUNK+1, timeout=aiohttp.ClientWSTimeout(ws_close=.5))
        try:
            if ws._response.headers.get("Sec-WebSocket-Extensions"):
                raise SpeedError("compression", "Compressed speedtest channels are not supported")
            hello = {"protocol": VERSION, "run_id": remote_id, "csrf": csrf, "channel": role}
            if role == "data": hello.update(direction=direction,index=index)
            await ws.send_json(hello)
            ready = await ws.receive_json(timeout=3)
            if ready.get("type") != "ready" or ready.get("instance") != run.responder or ready.get("run_id") != remote_id:
                raise SpeedError("peer_protocol", "Peer channel identity did not match")
            return ws
        except BaseException:
            await ws.close()
            raise

    def update(phase, live):
        if phase in ("upload", "upload_warmup"):
            run.phase = phase
            if live is not None: run.live = validate_live(live, manager.profile)

    try:
        async with asyncio.timeout(manager.profile.setup_seconds):
            result = await call("/session", "POST", {"password":peer["password"]})
            if result.get("authenticated") is not True or not isinstance(result.get("csrf_token"),str) or not 1<=len(result["csrf_token"])<=128:
                raise SpeedError("peer_protocol", "Peer returned invalid session data")
            csrf = result["csrf_token"]
            caps = await call("/speedtest/capabilities")
            if type(caps.get("protocol")) is not int or caps["protocol"] != VERSION or caps.get("profile") != manager.profile.wire() or not valid_id(caps.get("instance")):
                raise SpeedError("peer_unsupported", "Peer protocol/profile is not compatible")
            if caps["instance"] == manager.instance: raise SpeedError("self_test", "A node cannot speed-test itself")
            run.responder = caps["instance"]
            accepted = await call("/speedtest/accept", "POST", {"protocol":VERSION,"profile":manager.profile.wire(),"instance":manager.instance,"remaining_seconds":max(.1,run.limit-(time.monotonic()-run.clock))})
            remote_id = accepted.get("id")
            if not valid_id(remote_id): raise SpeedError("peer_protocol", "Invalid peer run identifier")
            control = Control(await connect("control"),manager.profile,update)
            await control.command("echo", {"nonce":"warm-control"})
        run.phase = "idle"
        idle = Latency(control,manager.profile)
        await idle.measure(idle=True)
        run.metrics["idle_rtt"] = idle.summary()
        await control.command("idle_done", {"rtt":idle.summary()})
        for direction in ("download","upload"):
            run.phase,run.live = direction+"_setup",None
            async with asyncio.timeout(manager.profile.channel_seconds):
                await control.command("prepare", {"direction":direction})
                data = DataChannels(connect,direction)
                await data.open(manager.profile.streams)
            if direction == "download":
                meter = Meter(manager.profile)
                data.meter = meter
                run.phase = "download_warmup"
                await control.command("go")
                latency = Latency(control,manager.profile,window=meter).start()
                while time.monotonic() < meter.end:
                    control.check();data.check()
                    run.live=meter.snapshot()
                    run.phase="download" if time.monotonic()>=meter.begin else "download_warmup"
                    await asyncio.sleep(.02)
                data.check()
                rtt = await latency.stop();latency=None
                metric = validate_metric(meter.snapshot(complete=True),manager.profile)
                run.metrics["download"],run.metrics["download_rtt"] = metric,rtt
                data.closing=True
                await control.command("stop_download", {"metric":metric,"rtt":rtt})
            else:
                await control.command("go")
                data.upload()
                while not control.window_start.is_set():
                    control.check();data.check();await asyncio.sleep(.02)
                control.check()
                latency = Latency(control,manager.profile,upload=True).start()
                while not control.window_end.is_set():
                    control.check();data.check();await asyncio.sleep(.02)
                control.check();data.check()
                rtt = await latency.stop();latency=None
                data.closing=True
                for sender in data.senders:sender.stop()
                reply=await control.command("stop_upload", {"rtt":rtt})
                metric=validate_metric(reply.get("metric"),manager.profile,sum(s.sent for s in data.senders))
                run.metrics["upload"],run.metrics["upload_rtt"]=metric,rtt
            await data.close();data=None
            run.live=None
        run.phase="finalizing"
        final = await control.command("finish")
        if final.get("outcome") == "cancelled": raise SpeedError("peer_cancelled", "Peer canceled the test before completion")
        if final.get("outcome") not in ("completed", "failed"): raise SpeedError("peer_protocol", "Missing terminal outcome")
        if final["outcome"] == "failed" and manager.complete_metrics(run.metrics): raise SpeedError("peer_stopped", "Peer could not finalize the test")
    except aiohttp.ClientConnectorCertificateError:
        raise SpeedError("peer_tls", "Peer certificate trust, validity, or hostname verification failed") from None
    except TimeoutError:
        raise SpeedError("peer_timeout", "Peer operation did not finish before its deadline") from None
    except (aiohttp.ClientError, OSError, ssl.SSLError):
        raise SpeedError("peer_network", "Could not communicate with the configured peer") from None
    finally:
        if latency:
            try: await latency.stop()
            except Exception: pass
        if data:
            data.closing = True
            for sender in data.senders: sender.stop()
            for task in data.tasks: task.cancel()
            await asyncio.gather(*data.tasks, return_exceptions=True)
        try:
            async with asyncio.timeout(max(.001,min(2,run.limit-(time.monotonic()-run.clock)))):
                if remote_id:
                    try: await call("/speedtest/runs/"+remote_id,"DELETE",{})
                    except Exception: pass
                if csrf: await call("/session","DELETE",{})
        except (Exception, asyncio.CancelledError):
            pass
        if time.monotonic()-run.clock >= run.limit:
            await session.close()
        if data: await data.close()
        if control: await control.close()
        await session.close()
