import asyncio
import hmac
import time

from aiohttp import WSMsgType, web

from .config import CommittedError, Problem, identifier, mapping
from .speed_protocol import ACK_STEP, CHUNK, VERSION, SpeedError, decode, finite, integer
from .web import COOKIE, STATE, response


def instance(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or not value.isascii() or not all(c.isalnum() or c in '_-' for c in value):
        raise Problem("protocol_error", "Invalid initiating instance ID", 400)
    return value


async def peers(request):
    return response(request.app[STATE].store.editable_peers())


async def save_peers(request):
    state = request.app[STATE]
    body = await state.body(request)
    async def commit():
        async with state.mutation_lock:
            candidate = state.store.prepare_peers(body, request.headers.get("If-Match"))
            state.speed.validate_edit(candidate)
            try:
                await asyncio.to_thread(state.store.persist, candidate)
            except CommittedError:
                raise SystemExit("Committed peer configuration could not be verified") from None
            try:
                state.speed.reconcile(candidate)
                await state.engine.reconcile(candidate, lambda: state.store.publish(candidate))
            except BaseException:
                raise SystemExit("Peer configuration committed but activation failed") from None
            return response(state.store.editable_peers())
    return await asyncio.shield(asyncio.create_task(commit()))


async def capabilities(request):
    return response(request.app[STATE].speed.capabilities())


async def state_view(request):
    return response(request.app[STATE].speed.state())


async def start(request):
    state = request.app[STATE]
    body = await state.body(request)
    mapping(body, {"mode", "peer_id"}, {"mode"}, "speedtest")
    async with state.mutation_lock:
        if body["mode"] == "browser" and "peer_id" not in body:
            run = state.speed.start_browser(request.cookies[COOKIE])
        elif body["mode"] == "node" and isinstance(body.get("peer_id"), str):
            identifier(body["peer_id"], "peer_id")
            run = state.speed.start_node(body["peer_id"])
        else:
            raise Problem("validation", "Choose browser mode or node mode with a configured peer ID", 422)
    return response(run.view(), 202)


async def accept(request):
    state = request.app[STATE]
    body = await state.body(request)
    mapping(body, {"protocol", "profile", "instance", "remaining_seconds"}, {"protocol", "profile", "instance", "remaining_seconds"}, "reservation")
    if type(body["protocol"]) is not int or body["protocol"] != VERSION or body["profile"] != state.speed.profile.wire():
        raise Problem("protocol_version", "Incompatible speedtest protocol or profile", 422)
    if not finite(body["remaining_seconds"], .1, state.speed.profile.timeout_seconds):
        raise Problem("protocol_error", "Invalid run deadline", 400)
    run = state.speed.accept(request.cookies[COOKIE], instance(body["instance"]), body["remaining_seconds"])
    return response(run.view(), 202)


async def run_view(request):
    return response(request.app[STATE].speed.result(request.match_info["id"]))


async def cancel(request):
    return response(await request.app[STATE].speed.cancel(request.match_info["id"]))


async def channel(request):
    state = request.app[STATE]
    if request.headers.get("Origin") != state.origin(request):
        raise Problem("origin", "Speedtest channels require the exact application Origin", 403)
    manager, owner = state.speed, request.cookies[COOKIE]
    run = manager.active
    if not run or run.mode == "node" or run.finishing:
        raise Problem("no_run", "No accepted receiving run", 409)
    if run.owner != owner:
        raise Problem("channel_owner", "Channel does not belong to this test session", 403)
    if run.pending_sockets + len(run.channels) + int(run.control is not None) >= 8:
        raise Problem("channel_limit", "Too many speedtest channel connections", 429)
    ws = web.WebSocketResponse(compress=False, max_msg_size=CHUNK+1, writer_limit=CHUNK, timeout=.5)
    if not ws.can_prepare(request).ok:
        raise Problem("websocket_required", "This endpoint requires a WebSocket upgrade", 400)
    run.pending_sockets += 1
    connection = {"ws": ws, "sender": None, "transport": request.transport, "close_task": None}
    connection["receive_done"] = asyncio.Event()
    async def receive(timeout=None):
        connection["receive_done"].clear()
        async with asyncio.timeout(timeout) as deadline:
            connection["receive_timeout"] = deadline
            try:
                return await ws.receive()
            finally:
                connection["receive_timeout"] = None
                connection["receive_done"].set()
    run.connections[ws] = connection
    ws.headers["Cache-Control"] = "no-store, no-transform"
    registered = False
    direction = None
    try:
        await ws.prepare(request)
        first = await receive(5)
        if first.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
            return ws
        if first.type != WSMsgType.TEXT or len(first.data) > 4096:
            raise SpeedError("protocol_error", "Expected a bounded channel handshake")
        hello = decode(first.data)
        allowed = {"protocol", "run_id", "csrf", "channel", "direction", "index"}
        session = state.session(request)
        token = hello.get("csrf")
        if set(hello)-allowed or type(hello.get("protocol")) is not int or hello["protocol"] != VERSION or hello.get("run_id") != run.id or manager.active is not run or run.finishing:
            raise SpeedError("protocol_error", "Stale or invalid channel handshake")
        if not session or not isinstance(token, str) or not hmac.compare_digest(token.encode(), session["csrf"].encode()):
            raise SpeedError("csrf", "Invalid channel CSRF token")
        if hello.get("channel") == "control":
            if run.control is not None or set(hello) != {"protocol", "run_id", "csrf", "channel"}:
                raise SpeedError("protocol_error", "Control channel already assigned")
            run.control, run.phase = ws, "idle"
            registered = True
        elif hello.get("channel") == "data":
            direction, index = hello.get("direction"), hello.get("index")
            if not integer(index, manager.profile.streams-1) or direction not in ("download", "upload") or direction != run.direction or run.phase != direction+"_setup" or (direction,index) in run.joined:
                raise SpeedError("protocol_error", "Unexpected or duplicate payload stream")
            run.joined.add((direction,index))
            run.channels[index] = connection
            registered = True
        else:
            raise SpeedError("protocol_error", "Unknown channel role")
        run.pending_sockets -= 1
        run.last_activity = time.monotonic()
        await ws.send_json({"type": "ready", "protocol": VERSION, "run_id": run.id, "instance": manager.instance})
        received = acked = 0
        while connection["close_task"] is None:
            message = await receive()
            if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED): break
            if manager.active is not run or run.finishing: break
            if time.monotonic()-run.clock >= run.limit or time.time()-run.started >= run.limit:
                raise SpeedError("timeout", "Overall speedtest deadline exceeded")
            if direction is not None and (run.ending_data or direction != run.direction or run.channels.get(index, {}).get("ws") is not ws): break
            run.last_activity = time.monotonic()
            if direction is None:
                if message.type != WSMsgType.TEXT or len(message.data) > 4096:
                    raise SpeedError("protocol_error", "Invalid control frame")
                payload = decode(message.data)
                if set(payload) != {"id", "op", "data"} or not integer(payload["id"]) or not isinstance(payload["op"], str) or not isinstance(payload["data"], dict):
                    raise SpeedError("protocol_error", "Invalid control request")
                data = await run.command(payload["op"], payload["data"])
                await ws.send_json({"id": payload["id"], "data": data})
                if payload["op"] == "finish":
                    complete = manager.complete_metrics(run.metrics)
                    await manager.finish(run, "completed" if complete else "failed", None if complete else "latency_unavailable", "A required latency metric is unavailable")
                    break
            elif direction == "download":
                if message.type != WSMsgType.TEXT or len(message.data) > 128:
                    raise SpeedError("protocol_error", "Expected a flow-control acknowledgement")
                payload = decode(message.data)
                sender = run.channels.get(index, {}).get("sender")
                if set(payload) != {"ack"} or sender is None:
                    raise SpeedError("protocol_error", "Invalid acknowledgement phase")
                sender.acknowledge(payload["ack"])
            else:
                if message.type != WSMsgType.BINARY or len(message.data) != CHUNK or run.meter is None or run.phase not in ("upload", "upload_warmup"):
                    raise SpeedError("protocol_error", "Unexpected upload payload")
                run.meter.add(len(message.data))
                received += len(message.data)
                if received-acked >= ACK_STEP:
                    acked = received
                    await ws.send_json({"ack": received})
            await asyncio.sleep(0)
    except SpeedError as exc:
        if registered and not run.finishing:
            await manager.finish(run, "failed", exc.code, exc.message)
        elif ws.prepared and not ws.closed:
            try: await ws.send_json({"error": {"code": exc.code, "message": exc.message}})
            except ConnectionError: pass
    except (TimeoutError, ConnectionError, RuntimeError, UnicodeError):
        pass
    except Exception:
        if registered and not run.finishing:
            await manager.finish(run, "failed", "protocol_error", "Invalid speedtest channel operation")
    finally:
        if not registered: run.pending_sockets -= 1
        unexpected_data_close = direction == run.direction and run.channels.get(hello.get("index"), {}).get("ws") is ws and not run.ending_data if registered and direction else False
        if registered and manager.active is run and not run.finishing and (direction is None or unexpected_data_close):
            await manager.finish(run, "cancelled" if run.mode == "browser" else "failed", "connection_lost", "A test channel disconnected before completion")
        try:
            await run.close_connection(connection)
        finally:
            run.connections.pop(ws, None)
    return ws


def add_routes(app, api):
    root = api + "/speedtest"
    app.add_routes([web.get(root+"/peers", peers), web.put(root+"/peers", save_peers),
                    web.get(root+"/state", state_view), web.get(root+"/capabilities", capabilities),
                    web.post(root+"/runs", start), web.post(root+"/accept", accept),
                    web.get(root+"/runs/{id}", run_view), web.delete(root+"/runs/{id}", cancel),
                    web.get(root+"/channel", channel)])
