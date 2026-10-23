import asyncio
import copy
import json
import math
import time

from aiohttp.test_utils import TestClient, TestServer
import pytest
import pytest_asyncio
import yaml

from connectivity.config import ConfigStore, load
from connectivity.config import Problem
from connectivity.history import Series, stamp
from connectivity.web import STATE, create_app
from .helpers import check, configuration, resource
from .test_history_engine import observation


@pytest_asyncio.fixture
async def client(tmp_path):
    path=tmp_path/"config.yml";path.write_text(yaml.safe_dump(configuration([resource()])),encoding="utf-8")
    store=ConfigStore(path)
    app=create_app(store,start_engine=False)
    test=TestClient(TestServer(app))
    await test.start_server()
    store.data["server"]["port"]=test.port
    test.origin=f"http://127.0.0.1:{test.port}"
    try:yield test
    finally:
        await test.close();store.close()


async def sign_in(client):
    response=await client.post("/api/v1/session",json={"password":"test-only-password"},headers={"Origin":client.origin})
    assert response.status==200
    data=await response.json()
    return {"Origin":client.origin,"X-CSRF-Token":data["csrf_token"]}


@pytest.mark.parametrize("path",["/session","/config","/status","/history","/resources/resource/history","/checks/probe/results"])
async def test_reads_require_auth(client,path):
    r=await client.get("/api/v1"+path)
    assert r.status==401
    text=await r.text();assert "test-only-password" not in text and '"host"' not in text


async def test_login_cookie_csrf_origin_logout_expiry(client):
    r=await client.post("/api/v1/session",json={"password":"test-only-password"})
    assert r.status==403
    headers=await sign_in(client)
    r=await client.get("/api/v1/session")
    assert (await r.json())["csrf_token"]==headers["X-CSRF-Token"]
    cookie=client.session.cookie_jar.filter_cookies(client.make_url("/"))["connectivity_session"]
    assert len(cookie.value)>=43
    for bad in [{"Origin":client.origin},{**headers,"Origin":"http://evil.example"},{**headers,"Origin":"null"}]:
        r=await client.delete("/api/v1/session",json={},headers=bad);assert r.status==403
    r=await client.delete("/api/v1/session",json={},headers=headers);assert r.status==200
    assert (await client.get("/api/v1/status")).status==401
    await sign_in(client)
    for session in client.app[STATE].sessions.values():session["expires"]=0
    assert (await client.get("/api/v1/status")).status==401


async def test_cookie_flags_session_rotation_and_cap(client):
    response=await client.post("/api/v1/session",json={"password":"test-only-password"},headers={"Origin":client.origin})
    cookie=response.cookies["connectivity_session"]
    assert cookie["httponly"] and cookie["samesite"]=="Strict" and cookie["path"]=="/"
    assert not cookie["secure"] and not cookie["domain"]
    old=cookie.value
    await sign_in(client)
    assert old not in client.app[STATE].sessions
    for _ in range(23):
        client.session.cookie_jar.clear();await sign_in(client)
    assert len(client.app[STATE].sessions)==20


async def test_session_expires_after_suspend_even_when_monotonic_did_not_advance(client):
    await sign_in(client)
    for value in client.app[STATE].sessions.values():
        value["expires_wall"]=0
    assert (await client.get("/api/v1/status")).status==401


async def test_rate_limits_generic_failures(client):
    for _ in range(5):
        r=await client.post("/api/v1/session",json={"password":"do-not-echo-this"},headers={"Origin":client.origin})
        assert r.status==401 and "do-not-echo-this" not in await r.text()
    r=await client.post("/api/v1/session",json={"password":"test-only-password"},headers={"Origin":client.origin})
    assert r.status==429 and r.headers["Retry-After"]=="60"


async def test_direct_listener_does_not_trust_proxy_client_headers(client):
    for index in range(6):
        result=await client.post('/api/v1/session',json={'password':'wrong'},headers={'Origin':client.origin,'X-Real-IP':f'198.51.100.{index+1}','X-Forwarded-For':f'203.0.113.{index+1}'})
        assert result.status==(401 if index<5 else 429)


async def test_host_port_forwarding_and_security_headers(client):
    for host in [f"evil.example:{client.port}","127.0.0.1:999",f"user@127.0.0.1:{client.port}"]:
        r=await client.get("/",headers={"Host":host,"X-Forwarded-Host":f"127.0.0.1:{client.port}"});assert r.status==403
    r=await client.get("/")
    assert r.status==200 and r.headers["X-Frame-Options"]=="DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert "unsafe-inline" not in r.headers["Content-Security-Policy"]
    assert "test-only-password" not in await r.text()
    client.app[STATE].store.data["server"]["allowed_hosts"]=["monitor.example"]
    assert (await client.get("/",headers={"Host":f"monitor.example:{client.port}"})).status==200


async def test_latency_chart_is_bundled_and_loaded_before_dashboard(client):
    html = await (await client.get("/")).text()
    assert html.index('static/latency-chart.js') < html.index('static/app.js')
    script = await client.get("/static/latency-chart.js")
    assert script.status == 200 and script.content_type == "text/javascript"
    assert "const LatencyCharts" in await script.text()


async def test_drag_reordering_is_bundled_and_inline(client):
    html = await (await client.get("/")).text()
    assert html.index('static/drag-order.js') < html.index('static/app.js')
    assert 'id="resource-order-panel"' in html and 'id="resource-order-dialog"' not in html
    assert 'id="reorder-checks"' in html
    script = await client.get("/static/drag-order.js")
    assert script.status == 200 and script.content_type == "text/javascript"
    assert "const DragOrder" in await script.text()


async def test_select_control_is_bundled_and_loaded_before_clients(client):
    html = await (await client.get("/")).text()
    assert html.index('static/select-control.js') < html.index('static/speed-ui.js') < html.index('static/app.js')
    script = await client.get("/static/select-control.js")
    assert script.status == 200 and script.content_type == "text/javascript"
    assert "const SelectControl" in await script.text()
    assert 'id="app-shell"' in html and 'class="rail"' in html


async def test_config_update_revision_secret_and_persistence(client):
    headers=await sign_in(client)
    r=await client.get("/api/v1/config");body=await r.json()
    assert "auth" not in body and "server" not in body and "password" not in json.dumps(body)
    assert r.headers["Cache-Control"]=="no-store"
    revision=body.pop("revision");body["resources"][0]["name"]="<script>alert(1)</script>"
    assert (await client.put("/api/v1/config",json=body,headers=headers)).status==428
    headers["If-Match"]=revision
    r=await client.put("/api/v1/config",json=body,headers=headers);assert r.status==200,await r.text()
    assert yaml.safe_load(client.app[STATE].store.path.read_bytes())["resources"][0]["name"]==body["resources"][0]["name"]
    assert (await client.put("/api/v1/config",json=body,headers=headers)).status==412
    result=await r.json();headers["If-Match"]=result["revision"]
    assert (await client.put("/api/v1/config",json={**body,"auth":{"password":"other"}},headers=headers)).status==422
    client.app[STATE].store.path.write_bytes(client.app[STATE].store.path.read_bytes()+b"\n# externally changed")
    assert (await client.put("/api/v1/config",json=body,headers=headers)).status==409


async def test_body_limit_json_and_content_type(client):
    headers=await sign_in(client)
    assert (await client.put("/api/v1/config",data="{}",headers=headers)).status==400
    headers["Content-Type"]="application/json"
    assert (await client.put("/api/v1/config",data='{"x":NaN}',headers=headers)).status==400
    r=await client.put("/api/v1/config",data="x"*1048577,headers=headers)
    assert r.status==413


async def test_range_bounds_status_and_results(client):
    await sign_in(client)
    state=client.app[STATE];engine=state.engine;now=engine.now()
    s=engine.checks["probe"].series
    for i in range(10):s.record(observation(now-10+i))
    r=await client.get("/api/v1/checks/probe/results?limit=3");data=await r.json()
    assert len(data["results"])==3 and data["next_cursor"]
    r=await client.get("/api/v1/checks/probe/results",params={"limit":3,"cursor":data["next_cursor"]});page=await r.json()
    assert page["results"][0]["completed_at"]<data["results"][-1]["completed_at"]
    s.generation="changed"
    assert (await client.get("/api/v1/checks/probe/results",params={"cursor":data["next_cursor"]})).status==409
    for query in [{"max_points":"1201"},{"from":"2026-01-01T00:00:00"},{"from":stamp(now),"to":stamp(now-1)},{"to":stamp(now+300)},{"from":stamp(now-1209601),"to":stamp(now)},{"max_points":"NaN"}]:
        assert (await client.get("/api/v1/history",params=query)).status==400
    r=await client.get("/api/v1/history?max_points=13");data=await r.json()
    assert len(data["resources"]["resource"]["timeline"])==13 and not data["checks"]
    r=await client.get("/api/v1/resources/resource/history?max_points=7");data=await r.json()
    assert len(data["checks"]["probe"]["timeline"])==7
    r=await client.get("/api/v1/status");data=await r.json()
    assert data["resources"][0]["state"]=="unknown"
    for key in ["server_time","started_at","config_revision","retention_from","raw_from","health"]:assert key in data
    assert (await client.get("/api/v1/resources/missing/history")).status==404
    assert (await client.get("/api/v1/checks/missing/results")).status==404


async def test_history_concurrency_limit(client):
    await sign_in(client)
    client.app[STATE].history_active=2
    r=await client.get("/api/v1/history")
    assert r.status==429 and r.headers["Retry-After"]=="5"
    client.app[STATE].history_active=0


@pytest.mark.parametrize("path", ["/api/v1/history", "/api/v1/resources/resource/history"])
@pytest.mark.parametrize("age", [120, 7200], ids=["raw", "summarized"])
async def test_history_api_reports_offline_without_inflating_downtime(client, path, age):
    await sign_in(client)
    engine = client.app[STATE].engine
    start = math.floor(engine.now() / 60) * 60 - age
    resource_series = Series(start)
    check_series = Series(start, check=True)
    for series in (resource_series, check_series):
        series.change(1, start)
        series.change(2, start + 30)
        series.change(1, start + 31)
    engine.resources["resource"] = resource_series
    engine.checks["probe"].series = check_series
    response = await client.get(path, params={"from":stamp(start), "to":stamp(start+60), "max_points":1})
    assert response.status == 200
    body = await response.json()
    for series in [*body["resources"].values(), *body["checks"].values()]:
        assert series["timeline"][0]["state"] == "offline"
        assert series["offline_seconds"] == 1
        assert series["availability"] == pytest.approx(59/60)
        assert series["coverage"] == 1


async def test_failed_persistence_preserves_live_history(client, monkeypatch):
    headers=await sign_in(client)
    state=client.app[STATE]
    series=state.engine.checks["probe"].series
    series.record(observation(state.engine.now()-1))
    original=state.store.path.read_bytes()
    body=await (await client.get("/api/v1/config")).json()
    headers["If-Match"]=body.pop("revision")
    body["resources"][0]["checks"][0]["port"]=443
    def fail(candidate):
        raise Problem("persistence","Controlled replacement error",503)
    monkeypatch.setattr(state.store,"persist",fail)
    result=await client.put("/api/v1/config",json=body,headers=headers)
    assert result.status==503
    assert state.store.path.read_bytes()==original
    assert state.engine.checks["probe"].series is series and len(series.raw.values())==1
    assert state.engine.checks["probe"].config["port"]==80


async def order_fixture(client):
    headers = await sign_in(client)
    snapshot = await (await client.get("/api/v1/config")).json()
    initial = {"resources":[
        resource(check("a"),check("b"),rid="alpha",name="Alpha"),
        resource(check("c"),check("d"),rid="beta",name="Beta"),
    ],"ui":snapshot["ui"]}
    response = await client.put("/api/v1/config",json=initial,headers={**headers,"If-Match":snapshot["revision"]})
    assert response.status == 200
    snapshot = await response.json()
    for c in client.app[STATE].engine.checks.values():
        c.series.record(observation(client.app[STATE].engine.now() - .01))
    reordered = {"ui":snapshot["ui"],"resources":copy.deepcopy(snapshot["resources"][::-1])}
    for r in reordered["resources"]:
        r["checks"].reverse()
    return headers,snapshot,reordered


async def test_resource_and_check_order_persists_without_history_reset(client):
    headers, snapshot, reordered = await order_fixture(client)
    state = client.app[STATE]
    old_checks = dict(state.engine.checks)
    old_resources = dict(state.engine.resources)
    response = await client.put("/api/v1/config",json=reordered,headers={**headers,"If-Match":snapshot["revision"]})
    assert response.status == 200
    saved = await response.json()
    assert saved["resources"] == reordered["resources"]
    assert saved["revision"] != snapshot["revision"]
    assert load(state.store.path.read_bytes())["resources"] == reordered["resources"]
    status = await (await client.get("/api/v1/status")).json()
    assert [r["id"] for r in status["resources"]] == ["beta","alpha"]
    assert [[c["id"] for c in r["checks"]] for r in status["resources"]] == [["d","c"],["b","a"]]
    for cid,c in state.engine.checks.items():
        assert c is old_checks[cid] and len(c.series.raw.values()) == 1
    for rid,s in state.engine.resources.items():
        assert s is old_resources[rid]
    state.store.close()
    reopened = ConfigStore(state.store.path)
    try:
        assert reopened.data["resources"] == reordered["resources"]
    finally:
        reopened.close()


@pytest.mark.parametrize("failure,expected", [("revision",412),("external",409),("persistence",503)])
async def test_failed_reorder_preserves_saved_order_and_history(client, monkeypatch, failure, expected):
    headers, snapshot, reordered = await order_fixture(client)
    state = client.app[STATE]
    previous_checks = dict(state.engine.checks)
    previous_resources = dict(state.engine.resources)
    revision = snapshot["revision"]
    if failure == "revision":
        revision = "stale-revision"
    elif failure == "external":
        state.store.path.write_bytes(state.store.path.read_bytes()+b"\n# external change\n")
    else:
        def fail(candidate):
            raise Problem("persistence","Controlled write failure",503)
        monkeypatch.setattr(state.store,"persist",fail)
    original = state.store.path.read_bytes()
    response = await client.put("/api/v1/config",json=reordered,headers={**headers,"If-Match":revision})
    assert response.status == expected
    assert state.store.path.read_bytes() == original
    assert state.store.data["resources"] == snapshot["resources"]
    assert state.store.revision == snapshot["revision"]
    assert list(state.engine.resources) == ["alpha","beta"]
    assert list(state.engine.checks) == ["a","b","c","d"]
    for cid,c in state.engine.checks.items():
        assert c is previous_checks[cid] and len(c.series.raw.values()) == 1
    for rid,s in state.engine.resources.items():
        assert s is previous_resources[rid]
