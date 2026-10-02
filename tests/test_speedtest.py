import asyncio
import copy
import json
import math
from urllib.parse import urlsplit
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
import pytest
import pytest_asyncio
import yaml

from connectivity.config import ConfigStore, Problem, validate
from connectivity.speed_protocol import CHUNK, WINDOW, Meter, Profile, SpeedError, rtt_summary, validate_metric, validate_rtt
from connectivity.web import COOKIE, STATE, create_app
from .helpers import Certificates, configuration, resource

FAST = Profile(warmup_seconds=.05, measurement_seconds=.3, timeout_seconds=8, setup_seconds=3,
               channel_seconds=2, idle_seconds=.4, idle_samples=3, idle_interval=.02,
               loaded_interval=.03, rtt_timeout=.2, liveness_seconds=2)


def peer(pid="peer", url="http://127.0.0.1:9999/", password="secret-peer-password"):
    return {"id":pid,"name":pid,"url":url,"password":password}


def test_meter_receiver_window_and_wide_counters():
    meter=Meter(now=100)
    meter.add(123,101)
    meter.add(2**32,102)
    meter.add(100,111.999)
    meter.add(456,112)
    result=validate_metric(meter.snapshot(112,complete=True))
    assert result["received_bytes"]==2**32+100
    assert result["warmup_bytes"]==123 and result["total_bytes"]==2**32+679
    assert result["mbps"]==pytest.approx((2**32+100)*8/10/1e6)
    assert meter.snapshot(111,complete=True)["mbps"] is None


@pytest.mark.parametrize("change", [{"measurement_seconds":8},{"received_bytes":float('nan')},{"complete":False},{"total_bytes":0},{"bucket_bytes":[0]},{"received_bytes":True}])
def test_invalid_receiver_claims(change):
    meter=Meter(now=0);meter.add(100,3)
    data=meter.snapshot(12,complete=True);data.update(change)
    with pytest.raises(SpeedError):validate_metric(data)


def test_rtt_statistics_and_missing_are_not_zero():
    assert rtt_summary([])["median_ms"] is None
    assert rtt_summary([1,3,100])["median_ms"]==3
    assert rtt_summary([1,3,4,100])["median_ms"]==3.5
    assert validate_rtt(rtt_summary([0,1,2]))["timer_limited"]
    invalid=rtt_summary([1,2]);invalid["median_ms"]=100
    with pytest.raises(SpeedError):validate_rtt(invalid)


@pytest.mark.parametrize("field,value", [("password",None),("password",123),("password",""),("password","CHANGE_ME_BEFORE_STARTING"),("url","file:///tmp/"),("url","https://user:secret@example.com/"),("url","https://example.com/#speedtest"),("url","https://example.com/a/../"),("url","https://example.com/?secret=x"),("name","\nname"),("unknown",1)])
def test_peer_validation(field,value):
    data=configuration();data["speedtest"]={"peers":[{**peer(),field:value}]}
    with pytest.raises(Problem):validate(data)


def test_peer_canonical_duplicates():
    data=configuration();data["speedtest"]={"peers":[peer('a','https://EXAMPLE.com:443/monitor'),peer('b','https://example.com/monitor/')]}
    with pytest.raises(Problem):validate(data)


@pytest_asyncio.fixture
async def nodes(tmp_path):
    clients=[]
    for name in ("a","b"):
        path=tmp_path/f"{name}.yml"
        path.write_text(yaml.safe_dump(configuration([resource()])),encoding='utf-8')
        store=ConfigStore(path)
        app=create_app(store,start_engine=False)
        app[STATE].speed.profile=FAST
        client=TestClient(TestServer(app));await client.start_server()
        store.data['server']['port']=client.port
        client.origin=f"http://127.0.0.1:{client.port}"
        response=await client.post('/api/v1/session',json={'password':'test-only-password'},headers={'Origin':client.origin})
        assert response.status==200
        client.auth={'Origin':client.origin,'X-CSRF-Token':(await response.json())['csrf_token']}
        clients.append(client)
    try:yield clients
    finally:
        for client in clients:
            await client.close();client.app[STATE].store.close()


async def configure(client, peers):
    current=await (await client.get('/api/v1/speedtest/peers')).json()
    response=await client.put('/api/v1/speedtest/peers',json={'peers':peers},headers={**client.auth,'If-Match':current['revision']})
    assert response.status==200,await response.text()
    return await response.json()


async def terminal(client, rid):
    for _ in range(200):
        response=await client.get('/api/v1/speedtest/runs/'+rid)
        assert response.status==200,await response.text()
        result=await response.json()
        if result['outcome']!='running':return result
        await asyncio.sleep(.03)
    raise AssertionError('Run did not terminate')


async def control(client):
    response=await client.post('/api/v1/speedtest/runs',json={'mode':'browser'},headers=client.auth)
    assert response.status==202
    rid=(await response.json())['id']
    ws=await client.ws_connect('/api/v1/speedtest/channel',origin=client.origin,compress=15,max_msg_size=CHUNK+1)
    assert not ws._response.headers.get('Sec-WebSocket-Extensions')
    await ws.send_json({'protocol':1,'run_id':rid,'csrf':client.auth['X-CSRF-Token'],'channel':'control'})
    assert (await ws.receive_json())['type']=='ready'
    return rid,ws


async def command(ws,sequence,op,data=None):
    await ws.send_json({'id':sequence,'op':op,'data':data or {}})
    while True:
        value=await ws.receive_json(timeout=3)
        if value.get('id')==sequence:return value['data']
        if value.get('event')=='terminal':raise AssertionError(value)


async def join(client,rid,index,direction='download',csrf=None):
    ws=await client.ws_connect('/api/v1/speedtest/channel',origin=client.origin,compress=0,max_msg_size=CHUNK+1)
    await ws.send_json({'protocol':1,'run_id':rid,'csrf':csrf or client.auth['X-CSRF-Token'],'channel':'data','direction':direction,'index':index})
    return ws,await ws.receive_json(timeout=2)


async def test_peer_save_redaction_and_generic_config_preservation(nodes):
    a,b=nodes
    result=await configure(a,[peer(url=b.origin+'/')])
    assert result['peers'][0]['has_password'] and 'secret-peer-password' not in json.dumps(result)
    check_series=a.app[STATE].engine.checks['probe'].series
    cfg=await (await a.get('/api/v1/config')).json()
    assert 'speedtest' not in cfg
    revision=cfg.pop('revision');cfg['ui']['refresh_interval_seconds']=2
    assert (await a.put('/api/v1/config',json=cfg,headers={**a.auth,'If-Match':revision})).status==200
    assert a.app[STATE].store.data['speedtest']['peers'][0]['password']=='secret-peer-password'
    saved=await configure(a,[{'id':'peer','name':'renamed','url':b.origin+'/'}])
    assert a.app[STATE].store.data['speedtest']['peers'][0]['password']=='secret-peer-password'
    assert a.app[STATE].engine.checks['probe'].series is check_series
    bad=await a.put('/api/v1/speedtest/peers',json={'peers':[{'id':'peer','name':'renamed','url':'http://another.example/'}]},headers={**a.auth,'If-Match':saved['revision']})
    assert bad.status==422


async def test_real_node_run_receiver_metrics_and_no_incoming_history(nodes):
    a,b=nodes
    await configure(a,[peer(url=b.origin+'/',password='test-only-password')])
    start=await a.post('/api/v1/speedtest/runs',json={'mode':'node','peer_id':'peer'},headers=a.auth)
    assert start.status==202,await start.text()
    rid=(await start.json())['id']
    result=await terminal(a,rid)
    assert result['outcome']=='completed',json.dumps(result,indent=2)
    for direction in ('download','upload'):
        m=result['metrics'][direction]
        assert m['complete'] and m['received_bytes']>0
        assert m['mbps']==pytest.approx(m['received_bytes']*8/FAST.measurement_seconds/1e6)
        assert result['metrics'][direction+'_rtt']['samples']>0
    assert result['metrics']['idle_rtt']['samples']==3
    assert not b.app[STATE].speed.latest
    assert a.app[STATE].speed.latest['peer']['id']==rid
    assert a.app[STATE].speed.active is None and b.app[STATE].speed.active is None


async def test_busy_cancel_and_unrelated_monitor_edits(nodes):
    a,b=nodes
    await configure(a,[peer(url=b.origin+'/',password='test-only-password')])
    start=await a.post('/api/v1/speedtest/runs',json={'mode':'node','peer_id':'peer'},headers=a.auth)
    rid=(await start.json())['id']
    assert (await a.post('/api/v1/speedtest/runs',json={'mode':'browser'},headers=a.auth)).status==409
    p=await (await a.get('/api/v1/speedtest/peers')).json()
    edit=await a.put('/api/v1/speedtest/peers',json={'peers':[]},headers={**a.auth,'If-Match':p['revision']})
    assert edit.status==409
    cfg=await (await a.get('/api/v1/config')).json();revision=cfg.pop('revision');cfg['resources'][0]['name']='Renamed'
    assert (await a.put('/api/v1/config',json=cfg,headers={**a.auth,'If-Match':revision})).status==200
    cancelled=await a.delete('/api/v1/speedtest/runs/'+rid,json={},headers=a.auth)
    assert cancelled.status==200,await cancelled.text()
    assert (await cancelled.json())['outcome']=='cancelled'
    assert a.app[STATE].speed.active is None


async def test_failed_auth_and_self_target_latest_attempt(nodes):
    a,b=nodes
    await configure(a,[peer(url=b.origin+'/')])
    response=await a.post('/api/v1/speedtest/runs',json={'mode':'node','peer_id':'peer'},headers=a.auth)
    result=await terminal(a,(await response.json())['id'])
    assert result['outcome']=='failed' and result['error']['code']=='peer_auth_failed'
    assert 'secret-peer-password' not in json.dumps(result)
    await configure(a,[peer(url=a.origin+'/',password='test-only-password')])
    response=await a.post('/api/v1/speedtest/runs',json={'mode':'node','peer_id':'peer'},headers=a.auth)
    result=await terminal(a,(await response.json())['id'])
    assert result['error']['code']=='self_test'


async def test_channel_authorization_and_idle_deadline(nodes):
    a,b=nodes
    assert (await a.get('/api/v1/speedtest/channel',headers={'Origin':'https://evil.example'})).status==403
    assert (await a.get('/api/v1/speedtest/channel',headers={'Origin':a.origin})).status==409
    response=await a.post('/api/v1/speedtest/runs',json={'mode':'browser'},headers=a.auth)
    rid=(await response.json())['id']
    async with a.ws_connect('/api/v1/speedtest/channel',origin=a.origin,compress=0) as ws:
        await ws.send_json({'protocol':1,'run_id':rid,'csrf':'wrong','channel':'control'})
        value=await ws.receive_json()
        assert value['error']['code']=='csrf'
    assert a.app[STATE].speed.active.id==rid
    cancelled=await a.delete('/api/v1/speedtest/runs/'+rid,json={},headers=a.auth)
    assert cancelled.status==200
    assert a.app[STATE].speed.active is None


async def test_responder_cancel_propagates_to_initiator(nodes):
    a,b=nodes
    await configure(a,[peer(url=b.origin+'/',password='test-only-password')])
    response=await a.post('/api/v1/speedtest/runs',json={'mode':'node','peer_id':'peer'},headers=a.auth)
    rid=(await response.json())['id']
    for _ in range(100):
        incoming=b.app[STATE].speed.active
        if incoming and incoming.phase=='download':break
        await asyncio.sleep(.01)
    assert incoming is not None
    response=await b.delete('/api/v1/speedtest/runs/'+incoming.id,json={},headers=b.auth)
    assert response.status==200
    result=await terminal(a,rid)
    assert result['outcome']=='cancelled',json.dumps(result)
    assert result['metrics']['download'] is None
    assert b.app[STATE].speed.active is None


async def test_node_run_survives_observer_logout(nodes):
    a,b=nodes
    await configure(a,[peer(url=b.origin+'/',password='test-only-password')])
    response=await a.post('/api/v1/speedtest/runs',json={'mode':'node','peer_id':'peer'},headers=a.auth)
    rid=(await response.json())['id']
    assert (await a.delete('/api/v1/session',json={},headers=a.auth)).status==200
    run=a.app[STATE].speed.active
    assert run and run.id==rid
    await asyncio.wait_for(run.finished.wait(),5)
    assert a.app[STATE].speed.latest['peer']['outcome']=='completed'


async def test_local_busy_does_not_replace_previous_latest(nodes):
    a,b=nodes
    await configure(a,[peer(url=b.origin+'/')])
    response=await a.post('/api/v1/speedtest/runs',json={'mode':'node','peer_id':'peer'},headers=a.auth)
    latest=await terminal(a,(await response.json())['id'])
    response=await a.post('/api/v1/speedtest/runs',json={'mode':'browser'},headers=a.auth)
    current=(await response.json())['id']
    assert (await a.post('/api/v1/speedtest/runs',json={'mode':'node','peer_id':'peer'},headers=a.auth)).status==409
    assert a.app[STATE].speed.latest['peer']['id']==latest['id']
    await a.delete('/api/v1/speedtest/runs/'+current,json={},headers=a.auth)


async def test_peer_edit_failures_leave_configuration_and_results(nodes,monkeypatch):
    a,b=nodes
    first=await configure(a,[peer(url=b.origin+'/')])
    start=await a.post('/api/v1/speedtest/runs',json={'mode':'node','peer_id':'peer'},headers=a.auth)
    saved=await terminal(a,(await start.json())['id'])
    candidate={'peers':[peer(url=b.origin+'/',password='new-password')]}
    assert (await a.put('/api/v1/speedtest/peers',json=candidate,headers=a.auth)).status==428
    assert (await a.put('/api/v1/speedtest/peers',json=candidate,headers={**a.auth,'If-Match':'old'})).status==412
    original=a.app[STATE].store.path.read_bytes()
    def fail(_):raise Problem('persistence','Controlled failure',503)
    monkeypatch.setattr(a.app[STATE].store,'persist',fail)
    response=await a.put('/api/v1/speedtest/peers',json=candidate,headers={**a.auth,'If-Match':first['revision']})
    assert response.status==503
    assert a.app[STATE].store.path.read_bytes()==original
    assert a.app[STATE].speed.latest['peer']==saved


async def test_unjoined_channels_are_closed_on_cancel(nodes):
    a,b=nodes
    response=await a.post('/api/v1/speedtest/runs',json={'mode':'browser'},headers=a.auth)
    rid=(await response.json())['id']
    assert (await a.get('/api/v1/speedtest/channel',headers={'Origin':a.origin})).status==400
    sockets=[await a.ws_connect('/api/v1/speedtest/channel',origin=a.origin,compress=0) for _ in range(5)]
    run=a.app[STATE].speed.active
    assert len(run.connections)==5
    response=await a.delete('/api/v1/speedtest/runs/'+rid,json={},headers=a.auth)
    assert response.status==200
    for ws in sockets:
        message=await asyncio.wait_for(ws.receive(),2)
        assert message.type in (WSMsgType.CLOSE,WSMsgType.CLOSED,WSMsgType.CLOSING)
        await ws.close()
    assert a.app[STATE].speed.active is None and not run.connections


@pytest.mark.parametrize('path',['state','peers','capabilities','runs/unknown','channel'])
async def test_speedtest_reads_require_authentication(nodes,path):
    a,b=nodes;a.session.cookie_jar.clear()
    assert (await a.get('/api/v1/speedtest/'+path)).status==401


@pytest.mark.parametrize('body',[{'mode':'browser','url':'http://example.com/'},{'mode':'browser','seconds':1},{'mode':'other'},{'mode':'node','peer_id':'missing'}])
async def test_bad_starts_do_not_reserve(nodes,body):
    a,b=nodes
    response=await a.post('/api/v1/speedtest/runs',json=body,headers=a.auth)
    assert response.status in (404,422)
    assert a.app[STATE].speed.active is None


async def test_incompatible_reservation_and_self_never_allocate(nodes):
    a,b=nodes
    body={'protocol':1,'profile':FAST.wire(),'instance':'other-node','remaining_seconds':5}
    for delta,status in [({'protocol':2},422),({'remaining_seconds':99999},400),({'instance':a.app[STATE].speed.instance},422),({'profile':{}},422)]:
        response=await a.post('/api/v1/speedtest/accept',json={**body,**delta},headers=a.auth)
        assert response.status==status
        assert a.app[STATE].speed.active is None


async def test_duplicates_extra_channels_and_credit_bound(nodes):
    a,b=nodes;rid,ctrl=await control(a)
    await command(ctrl,1,'idle_done',{'rtt':rtt_summary([1,2,3])})
    await command(ctrl,2,'prepare',{'direction':'download'})
    sockets=[]
    for index in range(4):
        ws,ready=await join(a,rid,index);sockets.append(ws);assert ready['type']=='ready'
    duplicate,error=await join(a,rid,0)
    assert error['error']['code']=='protocol_error';await duplicate.close()
    await command(ctrl,3,'go')
    await asyncio.sleep(.1)
    run=a.app[STATE].speed.active
    assert run and len(run.channels)==4
    assert all(0<c['sender'].sent<=WINDOW for c in run.channels.values())
    assert all(c['sender'].acked==0 for c in run.channels.values())
    assert sum(c['sender'].sent for c in run.channels.values())<=8*1024*1024
    response=await a.delete('/api/v1/speedtest/runs/'+rid,json={},headers=a.auth)
    assert response.status==200
    await asyncio.gather(*(ws.close() for ws in sockets));await ctrl.close()
    assert not run.connections and not run.tasks and a.app[STATE].speed.active is None


async def test_unknown_control_fields_fail_and_release(nodes):
    a,b=nodes;rid,ws=await control(a)
    await ws.send_json({'id':1,'op':'go','data':{'duration_seconds':999}})
    result=await terminal(a,rid)
    assert result['outcome']=='failed' and result['error']['code']=='protocol_error'
    await ws.close()


async def test_authorized_wrong_session_cannot_join_but_can_cancel(nodes):
    a,b=nodes;rid,ws=await control(a)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as other:
        response=await other.post(a.origin+'/api/v1/session',json={'password':'test-only-password'},headers={'Origin':a.origin})
        auth=await response.json()
        with pytest.raises(aiohttp.WSServerHandshakeError) as error:
            await other.ws_connect(a.origin+'/api/v1/speedtest/channel',origin=a.origin)
        assert error.value.status==403
        response=await other.delete(a.origin+'/api/v1/speedtest/runs/'+rid,json={},headers={'Origin':a.origin,'X-CSRF-Token':auth['csrf_token']})
        assert response.status==200
    await ws.close()


async def test_revoked_browser_session_cancels_and_releases(nodes):
    a,b=nodes;rid,ws=await control(a)
    run=a.app[STATE].speed.active
    a.app[STATE].sessions[run.owner]['expires']=0
    await asyncio.wait_for(run.finished.wait(),3)
    assert run.outcome=='cancelled' and run.error['code']=='session_expired'
    assert a.app[STATE].speed.active is None
    await ws.close()


async def test_abandoned_setup_expires_and_terminal_receipt_prunes(nodes):
    a,b=nodes
    response=await a.post('/api/v1/speedtest/runs',json={'mode':'browser'},headers=a.auth)
    run=a.app[STATE].speed.active
    run.clock-=FAST.setup_seconds+.1
    await asyncio.wait_for(run.finished.wait(),2)
    assert run.outcome=='failed' and run.error['code']=='setup_timeout'
    assert a.app[STATE].speed.active is None
    assert not a.app[STATE].speed.latest
    expiry,result=a.app[STATE].speed.receipt
    a.app[STATE].speed.receipt=(0,result)
    with pytest.raises(Problem):a.app[STATE].speed.result(run.id)


async def test_overall_deadline_cannot_be_kept_alive(nodes):
    a,b=nodes;rid,ws=await control(a)
    run=a.app[STATE].speed.active
    run.clock-=FAST.timeout_seconds+1
    await asyncio.wait_for(run.finished.wait(),2)
    assert run.error['code']=='timeout' and a.app[STATE].speed.active is None
    await ws.close()


async def test_opposite_starts_cannot_deadlock(nodes):
    a,b=nodes
    await configure(a,[peer(url=b.origin+'/',password='test-only-password')])
    await configure(b,[peer(url=a.origin+'/',password='test-only-password')])
    ra=a.app[STATE].speed.start_node('peer');rb=b.app[STATE].speed.start_node('peer')
    await asyncio.wait_for(asyncio.gather(ra.finished.wait(),rb.finished.wait()),5)
    assert ra.error['code']=='peer_busy' and rb.error['code']=='peer_busy'
    assert a.app[STATE].speed.active is None and b.app[STATE].speed.active is None


async def test_http_and_websocket_redirects_never_forward_credentials(nodes):
    a,b=nodes
    seen=[]
    async def sink_handler(request):
        seen.append(request.path)
        return web.json_response({})
    sink=web.Application();sink.router.add_route('*','/{path:.*}',sink_handler)
    sink_server=TestServer(sink);await sink_server.start_server()
    fake=web.Application()
    async def fake_login(request):
        response=web.json_response({'authenticated':True,'csrf_token':'fixture-csrf'})
        response.set_cookie(COOKIE,'fixture-session')
        return response
    fake.router.add_post('/api/v1/session',fake_login)
    async def fake_caps(request):return web.json_response({'protocol':1,'profile':FAST.wire(),'instance':'other-instance'})
    async def fake_accept(request):return web.json_response({'id':'fixture-run'},status=202)
    fake.router.add_get('/api/v1/speedtest/capabilities',fake_caps)
    fake.router.add_post('/api/v1/speedtest/accept',fake_accept)
    async def redirect(request):raise web.HTTPFound(str(sink_server.make_url('/capture')))
    fake.router.add_get('/api/v1/speedtest/channel',redirect)
    fake.router.add_post('/redirect/api/v1/session',redirect)
    fake_server=TestServer(fake);await fake_server.start_server()
    try:
        for path in ('/','/redirect/'):
            await configure(a,[peer(url=str(fake_server.make_url(path)))])
            response=await a.post('/api/v1/speedtest/runs',json={'mode':'node','peer_id':'peer'},headers=a.auth)
            result=await terminal(a,(await response.json())['id'])
            assert result['error']['code']=='peer_redirect',result
        assert not seen
    finally:
        await fake_server.close();await sink_server.close()


@pytest.mark.parametrize('variant', ['trusted','untrusted','mismatch','expired'])
async def test_node_https_prefix_and_certificate_validation(nodes,tmp_path,variant):
    a,_=nodes
    certs=Certificates(tmp_path)
    options={'dns':'other.example','ip':None} if variant=='mismatch' else {'before':-3,'after':-1} if variant=='expired' else {}
    context=certs.context(variant,**options)
    if variant!='untrusted':certs.trust(a.app[STATE].speed.ssl_context)
    path=tmp_path/'tls-peer.yml'
    cfg=configuration();cfg['server']['public_url']='https://127.0.0.1/monitor/'
    path.write_text(yaml.safe_dump(cfg),encoding='utf-8')
    store=ConfigStore(path);app=create_app(store,start_engine=False);app[STATE].speed.profile=FAST
    runner=web.AppRunner(app,access_log=None);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0,ssl_context=context);await site.start()
    port=site._server.sockets[0].getsockname()[1]
    url=f'https://127.0.0.1:{port}/monitor/'
    store.data['server']['public_url']=url
    app[STATE].public_url=urlsplit(url);app[STATE].public_origin=f'https://127.0.0.1:{port}'
    try:
        await configure(a,[peer(url=url,password='test-only-password')])
        start=await a.post('/api/v1/speedtest/runs',json={'mode':'node','peer_id':'peer'},headers=a.auth)
        result=await terminal(a,(await start.json())['id'])
        if variant=='trusted':
            assert result['outcome']=='completed',json.dumps(result,indent=2)
            assert result['metrics']['upload']['received_bytes']>0
            assert not app[STATE].speed.latest
        else:
            assert result['outcome']=='failed' and result['error']['code']=='peer_tls',result
        assert not app[STATE].sessions
    finally:
        await runner.cleanup();store.close()


async def test_terminal_commit_is_not_rewritten_by_late_cancel(nodes):
    a,b=nodes
    await a.post('/api/v1/speedtest/runs',json={'mode':'browser'},headers=a.auth)
    run=a.app[STATE].speed.active
    run.phase='upload_done'
    meter=Meter(FAST,now=0);meter.add(100,FAST.warmup_seconds+.01)
    sample=validate_metric(meter.snapshot(1,complete=True),FAST)
    run.metrics.update(idle_rtt=rtt_summary([1]),download_rtt=rtt_summary([1]),upload_rtt=rtt_summary([1]),download=sample,upload=sample)
    assert (await run.command('finish',{}))['outcome']=='completed'
    result=await a.app[STATE].speed.cancel(run.id)
    assert result['outcome']=='completed'
    assert a.app[STATE].speed.result(run.id)['outcome']=='completed'


async def test_late_result_cannot_publish_after_peer_delete_recreate(nodes):
    a,b=nodes
    await configure(a,[peer(url=b.origin+'/')])
    manager=a.app[STATE].speed;store=a.app[STATE].store
    original=copy.deepcopy(store.data)
    run=manager._admit(mode='node',peer=original['speedtest']['peers'][0])
    without=copy.deepcopy(original);without['speedtest']['peers']=[]
    manager.reconcile(without);store.publish(without)
    manager.reconcile(original);store.publish(original)
    assert run.peer_generation!=manager.generations['peer']
    await manager.finish(run,'failed','late','Old generation')
    assert not manager.latest


@pytest.mark.parametrize('kind',['binary_control','oversized_text','oversized_binary','compressed_control','unknown_field'])
async def test_bad_control_frames_release_run(nodes,kind):
    a,b=nodes;rid,ws=await control(a)
    run=a.app[STATE].speed.active
    if kind=='binary_control':await ws.send_bytes(b'x')
    elif kind=='oversized_text':await ws.send_str('x'*5000)
    elif kind=='oversized_binary':await ws.send_bytes(b'x'*(CHUNK+10))
    elif kind=='compressed_control':await ws.send_str(json.dumps({'id':1,'op':'keepalive','data':{}}),compress=15)
    else:await ws.send_json({'id':1,'op':'keepalive','data':{},'unknown':True})
    result=await terminal(a,rid)
    assert result['outcome']=='failed'
    await asyncio.wait_for(run.finished.wait(),3)
    assert a.app[STATE].speed.active is None
    await ws.close()


async def test_stale_channel_cannot_join_or_stop_new_run(nodes):
    a,b=nodes
    response=await a.post('/api/v1/speedtest/runs',json={'mode':'browser'},headers=a.auth)
    old=(await response.json())['id']
    await a.delete('/api/v1/speedtest/runs/'+old,json={},headers=a.auth)
    response=await a.post('/api/v1/speedtest/runs',json={'mode':'browser'},headers=a.auth)
    new=(await response.json())['id']
    ws=await a.ws_connect('/api/v1/speedtest/channel',origin=a.origin)
    await ws.send_json({'protocol':1,'run_id':old,'csrf':a.auth['X-CSRF-Token'],'channel':'control'})
    assert 'error' in await ws.receive_json()
    await ws.close()
    await a.delete('/api/v1/speedtest/runs/'+old,json={},headers=a.auth)
    assert a.app[STATE].speed.active.id==new
    await a.delete('/api/v1/speedtest/runs/'+new,json={},headers=a.auth)
