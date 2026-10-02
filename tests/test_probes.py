import asyncio
import socket
import ssl
import time
import sys
import threading

import pytest

from connectivity.probes import Probes, ProbeError, Resolver, parse_ping, ping_args
from .helpers import Certificates, check, server


@pytest.fixture
def probes():
    p = Probes()
    yield p
    p.close()


@pytest.mark.parametrize("status", [200, 204, 301, 302, 401, 404, 500, 599])
async def test_http_exact_status_no_redirect_no_body(probes, status):
    requests = []
    async def handler(reader, writer):
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(f"HTTP/1.1 {status} Fixture\r\nLocation: http://invalid.example/\r\nContent-Length: 999999999\r\n\r\n".encode())
        await writer.drain()
        await asyncio.sleep(10)
    async with server(handler) as port:
        result = await probes.run(check(kind="http", url=f"http://127.0.0.1:{port}/", expected_status=status))
    assert result.outcome == "success" and result.details["http_status"] == status
    assert result.elapsed < .5 and len(requests) == 1


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def test_methods_empty_body_and_time_to_headers(probes, method):
    async def handler(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        assert headers.startswith(method.encode() + b" /path?q=hello%20world HTTP/1.1")
        assert b"Content-Length: 0" in headers
        await asyncio.sleep(.03)
        writer.write(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 204 No Content\r\n\r\n")
        await writer.drain()
    async with server(handler) as port:
        result = await probes.run(check(kind="http", url=f"http://127.0.0.1:{port}/path?q=hello%20world", method=method, expected_status=204))
    assert result.outcome == "success"
    assert result.latency >= 25


@pytest.mark.parametrize("payload,code", [(b"HTTP/1.1 503 Fixture\r\n\r\n", "http_status"), (b"GARBAGE\r\n\r\n", "http_protocol"), (b"HTTP/1.1 200 OK\r\nBad header\r\n\r\n", "http_protocol"), (b"HTTP/1.1 200 OK\r\nX: " + b"a"*66000 + b"\r\n\r\n", "http_protocol"), (b"HTTP/1.1 101 Switching\r\n\r\n", "http_protocol")], ids=["status", "malformed", "header", "oversized", "switching"])
async def test_bad_http(probes, payload, code):
    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(payload)
        await writer.drain()
    async with server(handler) as port:
        result = await probes.run(check(kind="http", url=f"http://127.0.0.1:{port}/"))
    assert result.outcome == "failure" and result.code == code


async def test_tcp_dns_excluded_closes_connection(probes, monkeypatch):
    closed = asyncio.Event()
    async def handler(reader, writer):
        assert await reader.read() == b""
        closed.set()
    original = probes.resolver.resolve
    async def slow(host, port):
        await asyncio.sleep(.06)
        return await original(host, port)
    monkeypatch.setattr(probes.resolver, "resolve", slow)
    async with server(handler) as port:
        result = await probes.run(check(port=port))
        await asyncio.wait_for(closed.wait(), 1)
    assert result.outcome == "success" and result.elapsed >= .06
    assert result.latency < result.elapsed*1000 - 40
    result = await probes.run(check(port=port, timeout_seconds=5))
    assert result.outcome == "failure" and result.code == "tcp"


async def test_outer_deadline_includes_dns(probes, monkeypatch):
    async def slow(*args):
        await asyncio.sleep(3)
    monkeypatch.setattr(probes.resolver, "resolve", slow)
    result = await probes.run(check(timeout_seconds=.1))
    assert result.code == "timeout" and result.elapsed < .5


async def test_dns_failure(probes, monkeypatch):
    async def fail(*args):
        raise socket.gaierror()
    monkeypatch.setattr(probes.resolver, "resolve", fail)
    result = await probes.run(check())
    assert result.code == "dns" and result.outcome == "failure"


@pytest.mark.parametrize("variant,kwargs,trust,expected", [("valid",{},True,"success"),("near",{"after":.001},True,"success"),("expired",{"before":-3,"after":-1},True,"failure"),("future",{"before":1,"after":2},True,"failure"),("mismatch",{"dns":"other.example","ip":None},True,"failure"),("untrusted",{},False,"failure")])
async def test_tls_validation_expiry_and_metric(probes, tmp_path, variant, kwargs, trust, expected):
    certs = Certificates(tmp_path)
    ctx = certs.context(variant, **kwargs)
    if trust:
        certs.trust(probes.tls_context)
    async def handler(reader, writer):
        await reader.read()
    async with server(handler, ctx) as port:
        result = await probes.run(check(kind="tls", port=port, timeout_seconds=2))
    assert result.outcome == expected, (result.code, result.message)
    if expected == "success":
        assert result.details["certificate_expires_at"] > time.time()
        assert 0 < result.latency < result.elapsed*1000
    else:
        assert result.code == "tls"


async def test_https_invalid_cert(probes, tmp_path):
    certs = Certificates(tmp_path)
    async def handler(reader, writer):
        await reader.read()
    async with server(handler, certs.context()) as port:
        result = await probes.run(check(kind="http", url=f"https://127.0.0.1:{port}/", timeout_seconds=2))
    assert result.code == "tls"


async def test_handshake_timeout_and_cancellation(probes):
    closed = asyncio.Event()
    async def handler(reader, writer):
        while await reader.read(4096):
            pass
        closed.set()
    async with server(handler) as port:
        result = await probes.run(check(kind="tls", port=port, timeout_seconds=.1))
        assert result.code == "timeout"
        await asyncio.wait_for(closed.wait(), 1)
        task = asyncio.create_task(probes.run(check(kind="tls", port=port, timeout_seconds=10)))
        await asyncio.sleep(.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("text,ip,code,latency,bound", [("Reply from 127.0.0.1: bytes=32 time<1ms TTL=128", "127.0.0.1", 0, 1, True),("64 bytes from 127.0.0.1: icmp_seq=1 ttl=64 time=0.032 ms", "127.0.0.1", 0, .032, False),("Antwort von 127.0.0.1: Bytes=32 Zeit=1,25ms TTL=128", "127.0.0.1", 0, 1.25, False),("Réponse de 127.0.0.1: octets=32 temps<1ms TTL=128", "127.0.0.1", 0, 1, True),("Reply from ::1: time<1ms", "::1", 0, 1, True),("Reply from 127.0.0.1: bytes=32 TTL=128", "127.0.0.1", 0, None, False)])
def test_ping_parsing(text, ip, code, latency, bound):
    result = parse_ping(text.encode(), ip, code)
    assert result == (latency, bound)


@pytest.mark.parametrize("text,latency,bound", [("64 bytes from 2001:db8::: icmp_seq=1 ttl=115 time=7.64 ms", 7.64, False), ("Reply from 2001:db8:: time<1ms", 1, True)])
def test_ping_address_ending_in_compressed_zeroes(text, latency, bound):
    assert parse_ping(text.encode(), "2001:db8::", 0) == (latency, bound)


@pytest.mark.parametrize("text,code,expected", [("Reply from 127.0.0.1: Destination host unreachable.",0,"ping_no_reply"),("Request timed out.",1,"ping_no_reply"),("100% packet loss",1,"ping_no_reply"),("unexpected output",0,"ping_output"),("Operation not permitted",2,"ping_permission")])
def test_ping_bad_output(text, code, expected):
    with pytest.raises(ProbeError) as error:
        parse_ping(text.encode(), "127.0.0.1", code)
    assert error.value.code == expected


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
async def test_live_os_ping_loopback(probes, host):
    result = await probes.run(check(kind="ping",host=host,timeout_seconds=2))
    assert result.outcome == "success", (result.code,result.message)
    assert not probes.children


async def test_ping_missing(probes):
    probes.ping_paths = {True:None,False:None}
    result = await probes.run(check(kind="ping"))
    assert result.code == "ping_missing" and result.outcome == "error"


@pytest.mark.parametrize("mode", ["overflow", "timeout", "cancel"])
async def test_ping_child_cleanup_on_overflow_deadline_cancel(probes, monkeypatch, mode):
    script = "import sys,time;sys.stdout.write('x'*100000);sys.stdout.flush();time.sleep(30)" if mode == "overflow" else "import time;time.sleep(30)"
    monkeypatch.setattr("connectivity.probes.ping_args", lambda *args: [sys.executable, "-c", script])
    task = asyncio.create_task(probes.run(check(kind="ping", timeout_seconds=.2 if mode == "timeout" else 5)))
    if mode == "cancel":
        await asyncio.sleep(.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    else:
        result = await asyncio.wait_for(task, 2)
        assert result.code == ("ping_output" if mode == "overflow" else "timeout")
    await probes.drain()
    assert not probes.children and not probes.reapers


async def test_resolver_work_remains_bounded_after_timeouts(monkeypatch):
    resolver = Resolver(limit=2)
    release = threading.Event()
    def slow(*args):
        release.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
    monkeypatch.setattr(socket, "getaddrinfo", slow)
    try:
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(resolver.resolve("fixture.example", 80), .05)
        assert resolver.active == 2 and len(resolver.workers) == 2
        with pytest.raises(ProbeError) as error:
            await resolver.resolve("fixture.example", 80)
        assert error.value.code == "resolver_busy"
        resolver.close()
        assert all(t.daemon for t in resolver.workers)
    finally:
        release.set()
        await asyncio.sleep(.1)


@pytest.mark.parametrize("text,address", [("Reply from 127.0.0.10: bytes=32 time=1ms TTL=128", "127.0.0.1"), ("64 bytes from 2001:db8:1::: icmp_seq=1 ttl=64 time=1 ms", "2001:db8::")])
def test_ping_wrong_address_cannot_establish_success(text, address):
    with pytest.raises(ProbeError):
        parse_ping(text.encode(), address, 0)


@pytest.mark.parametrize("platform,ip,expected", [("win32","127.0.0.1",["ping","-4","-n","1","-w","500","127.0.0.1"]),("win32","::1",["ping","-6","-n","1","-w","500","::1"]),("linux","::1",["ping","-6","-n","-c","1","-W","1","::1"]),("darwin","127.0.0.1",["ping","-n","-c","1","-W","500","127.0.0.1"]),("darwin","::1",["ping","-n","-c","1","::1"])])
def test_platform_ping_arguments(monkeypatch, platform, ip, expected):
    monkeypatch.setattr(sys,"platform",platform)
    assert ping_args("ping",ip,.5)==expected


@pytest.mark.parametrize("headers", [b"Content-Length: wrong\r\n", b"Content-Length: 2\r\nContent-Length: 3\r\n", b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n"])
async def test_http_invalid_framing(probes, headers):
    async def handler(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\n"+headers+b"\r\n")
        await writer.drain()
    async with server(handler) as port:
        result=await probes.run(check(kind="http",url=f"http://127.0.0.1:{port}/"))
    assert result.outcome=="failure" and result.code=="http_protocol"


async def test_scoped_ipv6_uses_bounded_os_resolver(monkeypatch):
    calls=[]
    def resolve(host, port, family, kind):
        calls.append(host)
        return [(socket.AF_INET6,socket.SOCK_STREAM,6,"",("fe80::1",port,0,7))]
    monkeypatch.setattr(socket,"getaddrinfo",resolve)
    resolver=Resolver()
    try:
        family,address=await resolver.resolve("fe80::1%fixture",443)
        assert family==socket.AF_INET6 and address==("fe80::1",443,0,7)
        assert calls==["fe80::1%fixture"]
    finally:
        resolver.close()


@pytest.mark.parametrize("host", ["localhost", "localhost."])
async def test_tls_dns_identity_sends_sni(probes, tmp_path, monkeypatch, host):
    certs=Certificates(tmp_path)
    context=certs.context()
    certs.trust(probes.tls_context)
    names=[]
    context.set_servername_callback(lambda ssl_socket, name, ssl_context: names.append(name))
    async def resolve(host, port):
        return socket.AF_INET,("127.0.0.1",port)
    monkeypatch.setattr(probes.resolver,"resolve",resolve)
    async def handler(reader, writer):
        await reader.read()
    async with server(handler,context) as port:
        result=await probes.run(check(kind="tls",host=host,port=port))
    assert result.outcome=="success" and names==["localhost"]


async def test_tcp_connect_timeout(probes, monkeypatch):
    async def stall(*args):
        await asyncio.sleep(10)
    monkeypatch.setattr(asyncio.get_running_loop(),"sock_connect",stall)
    result=await probes.run(check(timeout_seconds=.1))
    assert result.outcome=="failure" and result.code=="timeout" and result.elapsed<.5


async def test_ping_process_permission_error(probes, monkeypatch):
    async def denied(*args, **kwargs):
        raise PermissionError()
    probes.ping_paths[False]="/trusted/ping"
    monkeypatch.setattr(asyncio,"create_subprocess_exec",denied)
    result=await probes.run(check(kind="ping"))
    assert result.outcome=="error" and result.code=="ping_permission"
