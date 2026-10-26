import asyncio
import copy
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest
import pytest_asyncio

from connectivity import speed_client
from connectivity.speed_protocol import Profile
from connectivity.speedtest import SpeedManager


@pytest_asyncio.fixture
async def outgoing(monkeypatch):
    profile = Profile(streams=1, warmup_seconds=0, measurement_seconds=1, timeout_seconds=.3,
                      idle_seconds=.02, idle_samples=1, idle_interval=.001)
    state = SimpleNamespace(ready=asyncio.Event(), sessions=[], connectors=[], sockets=[], controls=[], data=[], latency=[], payload_bytes=0, hold_handshake=False)

    async def session(request):
        return web.json_response({'authenticated': True, 'csrf_token': 'test-csrf'})

    async def capabilities(request):
        return web.json_response({'protocol': 1, 'profile': profile.wire(), 'instance': 'test-peer'})

    async def accept(request):
        return web.json_response({'id': 'test-run'}, status=202)

    async def remove(request):
        return web.json_response({})

    async def channel(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        hello = await ws.receive_json()
        if state.hold_handshake:
            state.ready.set()
            await ws.receive()
            return ws
        await ws.send_json({'type': 'ready', 'instance': 'test-peer', 'run_id': 'test-run'})
        async for message in ws:
            if hello['channel'] == 'data':
                if message.type == aiohttp.WSMsgType.BINARY:
                    state.payload_bytes += len(message.data)
                continue
            command = message.json()
            data = command['data'] if command['op'] == 'echo' else {}
            await ws.send_json({'id': command['id'], 'data': data})
            if command['op'] == 'go':
                state.ready.set()
        return ws

    app = web.Application()
    app.router.add_post('/api/v1/session', session)
    app.router.add_get('/api/v1/speedtest/capabilities', capabilities)
    app.router.add_post('/api/v1/speedtest/accept', accept)
    app.router.add_delete('/api/v1/speedtest/runs/test-run', remove)
    app.router.add_delete('/api/v1/session', remove)
    app.router.add_get('/api/v1/speedtest/channel', channel)
    server = TestServer(app)
    await server.start_server()
    peer = {'id': 'test-peer', 'name': 'Test peer', 'url': str(server.make_url('/')), 'password': 'test-only-password'}
    store = SimpleNamespace(data={'speedtest': {'peers': [peer]}})
    manager = SpeedManager(store, None, ssl.create_default_context(), lambda _: True, profile)
    state.manager = manager

    session_class = aiohttp.ClientSession

    def client_session(**kwargs):
        client = session_class(**kwargs)
        state.sessions.append(client)
        state.connectors.append(client.connector)
        original_connect = client.ws_connect

        async def connect(*args, **options):
            ws = await original_connect(*args, **options)
            state.sockets.append(ws)
            return ws

        monkeypatch.setattr(client, 'ws_connect', connect)
        return client

    monkeypatch.setattr(speed_client.aiohttp, 'ClientSession', client_session)
    for name, records in [('Control', state.controls), ('DataChannels', state.data), ('Latency', state.latency)]:
        original = getattr(speed_client, name)

        def record(*args, original=original, records=records, **kwargs):
            value = original(*args, **kwargs)
            records.append(value)
            return value

        monkeypatch.setattr(speed_client, name, record)
    try:
        yield state
    finally:
        await manager.stop()
        for client in state.sessions:
            await client.close()
        await server.close()


def assert_disposed(state, run):
    assert state.manager.active is None
    assert run.finished.is_set()
    assert all(client.closed for client in state.sessions)
    assert all(connector.closed for connector in state.connectors)
    assert all(ws._response.closed and ws._response.connection is None for ws in state.sockets)
    assert all(control.reader.done() and control.heartbeat.done() for control in state.controls)
    assert all(task.done() for data in state.data for task in data.tasks)
    assert all(latency.task is None or latency.task.done() for latency in state.latency)
    assert state.payload_bytes == 0


@pytest.mark.parametrize('blocked_channel', ['data', 'control'])
async def test_cancel_before_deadline_waits_for_bounded_cleanup(outgoing, monkeypatch, blocked_channel):
    state = outgoing
    close_started, close_cancelled = asyncio.Event(), asyncio.Event()
    original_close = aiohttp.ClientWebSocketResponse.close

    async def close(ws, **kwargs):
        selected = state.controls[0].ws is ws if blocked_channel == 'control' else any(ws in data.sockets for data in state.data)
        if selected:
            close_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                close_cancelled.set()
        return await original_close(ws, **kwargs)

    monkeypatch.setattr(aiohttp.ClientWebSocketResponse, 'close', close)
    run = state.manager.start_node('test-peer')
    await asyncio.wait_for(state.ready.wait(), 1)
    retained = copy.deepcopy(run.metrics)
    cancellation = asyncio.create_task(state.manager.cancel(run.id))
    await asyncio.wait_for(close_started.wait(), 1)
    assert state.manager.active is run
    assert not run.finished.is_set()
    assert not state.sessions[0].closed
    result = await asyncio.wait_for(cancellation, 1)
    await run.task
    assert close_cancelled.is_set()
    assert result['outcome'] == 'cancelled'
    assert result['error']['code'] == 'cancelled'
    assert result['metrics'] == retained
    assert result['metrics']['idle_rtt']['samples'] == 1
    assert_disposed(state, run)


async def test_control_close_error_preserves_cancellation_and_releases_slot(outgoing, monkeypatch):
    state = outgoing
    run = state.manager.start_node('test-peer')
    await asyncio.wait_for(state.ready.wait(), 1)
    retained = copy.deepcopy(run.metrics)
    close = AsyncMock(side_effect=RuntimeError('Controlled close failure'))
    monkeypatch.setattr(state.controls[0].ws, 'close', close)
    result = await asyncio.wait_for(state.manager.cancel(run.id), 1)
    await run.task
    close.assert_awaited_once()
    assert result['outcome'] == 'cancelled'
    assert result['error']['code'] == 'cancelled'
    assert result['metrics'] == retained
    assert_disposed(state, run)


async def test_repeated_cancellation_keeps_slot_until_session_disposal(outgoing, monkeypatch):
    state = outgoing
    run = state.manager.start_node('test-peer')
    await asyncio.wait_for(state.ready.wait(), 1)
    close_started, release = asyncio.Event(), asyncio.Event()
    original_close = state.sessions[0].close

    async def close():
        close_started.set()
        await release.wait()
        await original_close()

    monkeypatch.setattr(state.sessions[0], 'close', close)
    cancellation = asyncio.create_task(state.manager.cancel(run.id))
    try:
        await asyncio.wait_for(close_started.wait(), 1)
        for _ in range(2):
            run.task.cancel()
            await asyncio.sleep(0)
        assert state.manager.active is run
        assert not run.finished.is_set()
        assert not state.sessions[0].closed
    finally:
        release.set()
        await asyncio.wait_for(cancellation, 1)
        await run.task
    assert run.outcome == 'cancelled'
    assert_disposed(state, run)


async def test_cancel_during_handshake_disposes_unregistered_socket(outgoing, monkeypatch):
    state = outgoing
    state.hold_handshake = True
    run = state.manager.start_node('test-peer')
    await asyncio.wait_for(state.ready.wait(), 1)
    assert not state.controls
    close = AsyncMock(side_effect=RuntimeError('Handshake cleanup must not wait for WebSocket close'))
    monkeypatch.setattr(state.sockets[0], 'close', close)
    result = await asyncio.wait_for(state.manager.cancel(run.id), 1)
    await run.task
    assert result['outcome'] == 'cancelled'
    close.assert_not_awaited()
    assert_disposed(state, run)


async def test_control_close_error_always_stops_reader_and_heartbeat():
    class Socket:
        close = AsyncMock(side_effect=RuntimeError('Controlled close failure'))

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

    control = speed_client.Control(Socket(), Profile(), lambda *_: None)
    await asyncio.sleep(0)
    try:
        with pytest.raises(RuntimeError, match='Controlled close failure'):
            await control.close()
        assert control.reader.done()
        assert control.heartbeat.done()
    finally:
        control.reader.cancel()
        control.heartbeat.cancel()
        await asyncio.gather(control.reader, control.heartbeat, return_exceptions=True)
