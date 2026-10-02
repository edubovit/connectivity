import asyncio
import base64
from collections import OrderedDict, deque
from datetime import datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import math
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web

from .config import LIMIT, CommittedError, Problem
from .engine import Engine
from .history import RETENTION, stamp
from .speedtest import SpeedManager

log = logging.getLogger("connectivity")
COOKIE = "connectivity_session"
STATE = web.AppKey("state", object)
STATIC = Path(__file__).parent / "static"


def response(data, status=200):
    return web.Response(text=json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":")), content_type="application/json", status=status)


def error(code, message, status, fields=()):
    return response({"error": {"code": code, "message": message, "fields": list(fields)}}, status)


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        result = parsed.timestamp()
        if not math.isfinite(result):
            raise ValueError()
        return result
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise Problem("range", "Use RFC 3339 timestamps with an explicit UTC offset", 400) from None


class State:
    def __init__(self, store, engine):
        self.store, self.engine = store, engine
        self.sessions = OrderedDict()
        self.failures = OrderedDict()
        self.global_failures = deque(maxlen=100)
        self.mutation_lock = asyncio.Lock()
        self.history_active = 0
        self.cursor_key = secrets.token_bytes(32)
        self.last_conflict_log = -math.inf
        self.last_internal_log = -math.inf
        self.public_url = urlsplit(store.data["server"]["public_url"]) if "public_url" in store.data["server"] else None
        self.base_path = self.public_url.path if self.public_url else "/"
        self.api_path = self.base_path + "api/v1"
        self.public_origin = f"https://{self.public_url.netloc}" if self.public_url else None
        self.speed = SpeedManager(store, engine.probes.resolver, engine.probes.tls_context, self.token_valid) if engine else None

    def token_valid(self, token):
        session = self.sessions.get(token)
        return bool(session and session["expires"] > time.monotonic() and session["expires_wall"] > time.time())

    def session(self, request):
        now = time.monotonic()
        for token in list(self.sessions):
            if self.sessions[token]["expires"] <= now or self.sessions[token]["expires_wall"] <= time.time():
                del self.sessions[token]
        return self.sessions.get(request.cookies.get(COOKIE))

    def origin(self, request):
        value = request.headers.get("Host", "")
        try:
            if any(c.isspace() for c in value):
                raise ValueError()
            split = urlsplit("//" + value)
            name = split.hostname
            if not name or split.username is not None or split.password is not None or split.path or split.query or split.fragment or "%" in value:
                raise ValueError()
            peer = ipaddress.ip_address(request.transport.get_extra_info("peername")[0].split("%")[0])
            local = ipaddress.ip_address(request.transport.get_extra_info("sockname")[0].split("%")[0])
            if self.public_url:
                port = split.port if split.port is not None else 443
                if not peer.is_loopback or not local.is_loopback or name.lower() != self.public_url.hostname or port != (self.public_url.port or 443):
                    raise ValueError()
                return self.public_origin
            port = split.port if split.port is not None else 80
            if port != self.store.data["server"]["port"]:
                raise ValueError()
            allowed = name.lower() in [s.encode("idna").decode("ascii").lower() for s in self.store.data["server"]["allowed_hosts"]]
            if name.lower() == "localhost" and peer.is_loopback:
                allowed = True
            try:
                address = ipaddress.ip_address(name)
                allowed |= address == local or (address.is_loopback and peer.is_loopback and local.is_loopback)
            except ValueError:
                pass
            if not allowed:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise Problem("host", "Host is not allowed for this listener", 403) from None
        return f"{request.scheme}://{value}"

    def client_ip(self, request):
        if self.public_url and "X-Real-IP" in request.headers:
            try:
                return str(ipaddress.ip_address(request.headers["X-Real-IP"]))
            except ValueError:
                raise Problem("proxy_ip", "Invalid client address from local proxy", 403) from None
        return request.remote

    async def body(self, request):
        try:
            data = await request.read()
            return json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError):
            raise Problem("json", "Malformed JSON body", 400) from None

    def encode_cursor(self, cid, generation, before, now):
        payload = json.dumps([cid, generation, before, now + 3600], separators=(",", ":")).encode()
        signed = payload + hmac.digest(self.cursor_key, payload, "sha256")
        return base64.urlsafe_b64encode(signed).decode().rstrip("=")

    def decode_cursor(self, token, cid, generation, now):
        try:
            if len(token) > 1024:
                raise ValueError()
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            payload, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(hmac.digest(self.cursor_key, payload, "sha256"), signature):
                raise ValueError()
            check, gen, before, expires = json.loads(payload)
            if check != cid or gen != generation or expires < now or before <= now - 3600:
                raise ValueError()
            return before
        except (ValueError, TypeError):
            raise Problem("cursor_expired", "Result cursor expired or belongs to a reset/restarted history. Reload results.", 409) from None


@web.middleware
async def guard(request, handler):
    state = request.app[STATE]
    origin = None
    try:
        origin = state.origin(request)
        if request.content_length is not None and request.content_length > LIMIT:
            raise Problem("body_size", "Request body exceeds 1 MiB", 413)
        mutation = request.method not in ("GET", "HEAD", "OPTIONS")
        if mutation:
            if request.headers.get("Origin") != origin:
                raise Problem("origin", "Mutation requires the exact same Origin as this listener", 403)
            if request.content_type != "application/json":
                raise Problem("content_type", "Mutations require application/json", 400)
        is_login = request.path == state.api_path + "/session" and request.method == "POST"
        if request.path.startswith(state.base_path + "api/") and not is_login:
            session = state.session(request)
            if not session:
                raise Problem("unauthorized", "Sign in to continue", 401)
            if mutation and not hmac.compare_digest(request.headers.get("X-CSRF-Token", "").encode(), session["csrf"].encode()):
                raise Problem("csrf", "Invalid or missing CSRF token; reload and try again", 403)
        result = await handler(request)
    except Problem as exc:
        result = error(exc.code, exc.message, exc.status, exc.fields)
        if exc.status == 429:
            result.headers["Retry-After"] = "5" if exc.code == "history_busy" else "60"
    except web.HTTPRequestEntityTooLarge:
        result = error("body_size", "Request body exceeds 1 MiB", 413)
    except web.HTTPException as exc:
        result = error("http", exc.reason, exc.status)
    except Exception:
        if time.monotonic() - state.last_internal_log >= 60:
            log.error("Unexpected HTTP handler failure (details withheld to protect configuration)")
            state.last_internal_log = time.monotonic()
        result = error("internal", "Unexpected server error", 500)
    if request.path == state.api_path + "/config" and result.status in (409, 412) and time.monotonic() - state.last_conflict_log >= 60:
        log.warning("Configuration conflict (%d); reload or reconcile and restart", result.status)
        state.last_conflict_log = time.monotonic()
    if result.status >= 400 and request.can_read_body:
        result.force_close()
    ws_origin = ("wss" if origin.startswith("https:") else "ws") + origin[origin.index(":"):] if origin else ""
    result.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY", "Content-Security-Policy": f"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self' {ws_origin}; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"})
    return result


async def login(request):
    state = request.app[STATE]
    now = time.monotonic()
    ip = state.client_ip(request)
    for source in list(state.failures):
        q = state.failures[source]
        while q and q[0] <= now - 60:
            q.popleft()
        if not q:
            del state.failures[source]
    while state.global_failures and state.global_failures[0] <= now - 60:
        state.global_failures.popleft()
    if len(state.failures.get(ip, ())) >= 5 or len(state.global_failures) >= 100:
        raise Problem("login_rate", "Too many failed sign-ins. Try again in one minute.", 429)
    body = await state.body(request)
    value = body.get("password") if isinstance(body, dict) else None
    try:
        supplied = value.encode("utf-8") if isinstance(value, str) else b""
        valid = len(supplied) <= 1024 and hmac.compare_digest(supplied, state.store.data["auth"]["password"].encode("utf-8"))
    except UnicodeError:
        valid = False
    if not valid:
        if ip not in state.failures:
            if len(state.failures) >= 1024:
                state.failures.popitem(last=False)
            state.failures[ip] = deque(maxlen=5)
        state.failures[ip].append(now)
        state.global_failures.append(now)
        raise Problem("login_failed", "Invalid password", 401)
    state.session(request)
    state.sessions.pop(request.cookies.get(COOKIE), None)
    while len(state.sessions) >= 20:
        state.sessions.popitem(last=False)
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    state.sessions[token] = {"csrf": csrf, "expires": now + 86400, "expires_wall": time.time() + 86400}
    result = response({"authenticated": True, "csrf_token": csrf})
    result.set_cookie(COOKIE, token, httponly=True, samesite="Strict", path=state.base_path, secure=bool(state.public_url), max_age=86400)
    return result


async def session(request):
    return response({"authenticated": True, "csrf_token": request.app[STATE].session(request)["csrf"]})


async def logout(request):
    state = request.app[STATE]
    state.sessions.pop(request.cookies.get(COOKIE), None)
    run = state.speed.active
    if run and run.owner == request.cookies.get(COOKIE):
        await state.speed.cancel(run.id, "cancelled", "session_expired", "Test session was logged out")
    result = response({"authenticated": False})
    result.set_cookie(COOKIE, "", path=state.base_path, secure=bool(state.public_url), httponly=True, samesite="Strict", max_age=0, expires="Thu, 01 Jan 1970 00:00:00 GMT")
    return result


async def get_config(request):
    store = request.app[STATE].store
    result = response(store.editable())
    result.headers["ETag"] = f'"{store.revision}"'
    return result


async def put_config(request):
    state = request.app[STATE]
    body = await state.body(request)
    async def commit():
        async with state.mutation_lock:
            candidate = state.store.prepare(body, request.headers.get("If-Match"))
            try:
                await asyncio.to_thread(state.store.persist, candidate)
            except CommittedError:
                log.critical("Configuration committed but snapshot verification failed; stopping")
                raise SystemExit(1) from None
            try:
                await state.engine.reconcile(candidate, lambda: state.store.publish(candidate))
            except BaseException:
                log.critical("Configuration committed but activation failed; stopping to avoid a file/runtime mismatch")
                raise SystemExit(1) from None
            log.info("Configuration saved: %d resources", len(candidate["resources"]))
            return response(state.store.editable())
    return await asyncio.shield(asyncio.create_task(commit()))


async def status(request):
    state = request.app[STATE]
    return response(state.engine.status(state.store.revision))


def query_range(request, now):
    try:
        points = int(request.query.get("max_points", "600"))
    except ValueError:
        raise Problem("range", "max_points must be an integer", 400) from None
    if not 1 <= points <= 1200:
        raise Problem("range", "max_points must be from 1 to 1200", 400)
    start = parse_time(request.query["from"]) if "from" in request.query else now - 3600
    end = parse_time(request.query["to"]) if "to" in request.query else now
    if start >= end or end > now or end - start > RETENTION:
        raise Problem("range", "Start must precede end; end cannot be in the future; maximum span is 14 days", 400)
    return start, end, points


async def history(request):
    state = request.app[STATE]
    if state.history_active >= 2:
        raise Problem("history_busy", "Two history queries are already running. Retry shortly.", 429)
    state.history_active += 1
    calculating = False
    try:
        engine = state.engine
        now = engine.clock()
        engine.expire(now)
        start, end, points = query_range(request, now)
        revision = state.store.revision
        rid = request.match_info.get("id")
        if rid and rid not in engine.resources:
            raise Problem("not_found", "Resource not found", 404)
        resources = {key: s.snapshot(now, start, end) for key, s in engine.resources.items() if rid is None or rid == key}
        checks = {key: c.series.snapshot(now, start, end) for key, c in engine.checks.items() if c.resource_id == rid}
        def calculate():
            def encode(value):
                return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            def section(series):
                return ",".join(encode(key) + ":" + encode(s.query(start, end, points, now)) for key, s in series.items())
            head = encode({"server_time": stamp(now), "config_revision": revision, "requested_range": {"from": stamp(start), "to": stamp(end)}})
            text = head[:-1] + ',"resources":{' + section(resources) + '},"checks":{' + section(checks) + "}}"
            return web.Response(text=text, content_type="application/json")
        work = asyncio.create_task(asyncio.to_thread(calculate))
        calculating = True
        def released(task):
            state.history_active -= 1
            if not task.cancelled():
                task.exception()
        work.add_done_callback(released)
        return await asyncio.shield(work)
    finally:
        if not calculating:
            state.history_active -= 1


async def results(request):
    state = request.app[STATE]
    now = state.engine.clock()
    cid = request.match_info["id"]
    check = state.engine.checks.get(cid)
    if not check:
        raise Problem("not_found", "Check not found", 404)
    try:
        limit = int(request.query.get("limit", "100"))
        if not 1 <= limit <= 500:
            raise ValueError()
    except ValueError:
        raise Problem("page_size", "limit must be from 1 to 500", 400) from None
    before = state.decode_cursor(request.query["cursor"], cid, check.series.generation, now) if "cursor" in request.query else None
    start, end, _ = query_range(request, now)
    rows, next_before = check.series.recent(now, before, limit, start, end)
    return response({"check_id": cid, "generation": check.series.generation, "raw_from": stamp(now - 3600), "results": rows, "next_cursor": state.encode_cursor(cid, check.series.generation, next_before, now) if next_before else None})


async def static(request):
    name = request.match_info.get("name", "index.html")
    if request.match_info.get("name") == "index.html":
        return await canonical_root(request)
    if name not in {"index.html", "app.js", "latency-chart.js", "drag-order.js", "speed-browser.js", "speed-ui.js", "app.css", "icon.svg"}:
        raise web.HTTPNotFound()
    types = {"html": "text/html", "js": "text/javascript", "css": "text/css", "svg": "image/svg+xml"}
    return web.Response(body=(STATIC / name).read_bytes(), content_type=types[name.rsplit(".", 1)[1]])


async def canonical_root(request):
    return web.Response(status=308, headers={"Location": request.app[STATE].base_path})


def create_app(store, engine=None, start_engine=True):
    app = web.Application(middlewares=[guard], client_max_size=LIMIT)
    state = State(store, engine or Engine(store.data))
    app[STATE] = state
    api = state.api_path
    app.add_routes([web.post(api + "/session", login), web.get(api + "/session", session), web.delete(api + "/session", logout), web.get(api + "/config", get_config), web.put(api + "/config", put_config), web.get(api + "/status", status), web.get(api + "/history", history), web.get(api + "/resources/{id}/history", history), web.get(api + "/checks/{id}/results", results), web.get(state.base_path, static), web.get(state.base_path + "static/{name}", static)])
    if state.base_path != "/":
        app.router.add_get(state.base_path.rstrip("/"), canonical_root)
    from .speed_web import add_routes
    add_routes(app, api)
    async def shutdown(app):
        await state.speed.stop()
    app.on_shutdown.append(shutdown)
    async def lifetime(app):
        if start_engine:
            await state.engine.start()
        yield
        await state.speed.stop()
        await state.engine.stop()
        state.sessions.clear()
    app.cleanup_ctx.append(lifetime)
    return app
