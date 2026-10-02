import asyncio
from concurrent.futures import Future
from dataclasses import dataclass, field
import ipaddress
import locale
import math
import os
from pathlib import Path
import re
import queue
import shutil
import socket
import ssl
import sys
import time
import threading
from urllib.parse import quote, urlsplit

OUTCOMES = ("success", "failure", "error")
CODES = ("ok", "timeout", "dns", "tcp", "tls", "http_status", "http_protocol", "ping_no_reply", "ping_missing", "ping_permission", "ping_output", "executor", "resolver_busy")
METRICS = {"ping": "ICMP round-trip", "http": "HTTP time to final headers (including DNS/TCP/TLS)", "tcp": "TCP connection (excluding DNS)", "tls": "TLS handshake (excluding DNS/TCP)"}


@dataclass(slots=True)
class Result:
    started: float
    completed: float
    elapsed: float
    outcome: str = "success"
    code: str = "ok"
    latency: float | None = None
    bounded: bool = False
    message: str = ""
    details: dict = field(default_factory=dict)


class ProbeError(Exception):
    def __init__(self, code, message, outcome="failure"):
        self.code, self.message, self.outcome = code, message, outcome


class Resolver:
    def __init__(self, limit=32):
        self.limit, self.active = limit, 0
        self.queue = queue.Queue(maxsize=limit)
        self.workers = []
        self.closed = False

    def worker(self):
        while True:
            job = self.queue.get()
            if job is None:
                return
            future, host, port = job
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
            except Exception as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

    async def resolve(self, host, port):
        try:
            address = ipaddress.ip_address(host)
            if address.version == 4:
                return socket.AF_INET, (str(address), port)
            if not address.scope_id:
                return socket.AF_INET6, (str(address), port, 0, 0)
        except ValueError:
            pass
        if self.closed or self.active >= self.limit:
            raise ProbeError("resolver_busy", "OS resolver worker limit reached; waiting for previous DNS calls to finish", "error")
        loop = asyncio.get_running_loop()
        self.active += 1
        future = Future()
        if len(self.workers) < self.active:
            worker = threading.Thread(target=self.worker, name=f"dns-{len(self.workers)}", daemon=True)
            self.workers.append(worker)
            worker.start()
        self.queue.put_nowait((future, host, port))
        def finished(_):
            try:
                loop.call_soon_threadsafe(self.release)
            except RuntimeError:
                pass
        future.add_done_callback(finished)
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        results = await asyncio.shield(wrapped)
        if not results:
            raise socket.gaierror()
        family, _, _, _, address = results[0]
        return family, address

    def release(self):
        self.active -= 1

    def close(self):
        if self.closed:
            return
        self.closed = True
        while True:
            try:
                job = self.queue.get_nowait()
            except queue.Empty:
                break
            if job:
                job[0].cancel()
        for _ in self.workers:
            self.queue.put_nowait(None)


def discover_ping(ipv6=False):
    if sys.platform == "win32":
        path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "PING.EXE"
        return str(path) if path.is_file() else None
    names = ["ping6"] if sys.platform == "darwin" and ipv6 else ["ping"]
    for name in names:
        path = shutil.which(name, path="/usr/bin:/bin:/usr/sbin:/sbin")
        if path:
            return os.path.abspath(path)
    return None


def ping_args(executable, address, timeout):
    v6 = ipaddress.ip_address(address).version == 6
    if sys.platform == "win32":
        return [executable, "-6" if v6 else "-4", "-n", "1", "-w", str(max(1, math.ceil(timeout * 1000))), address]
    if sys.platform == "darwin":
        if v6:
            return [executable, "-n", "-c", "1", address]
        return [executable, "-n", "-c", "1", "-W", str(max(1, math.ceil(timeout * 1000))), address]
    return [executable, "-6" if v6 else "-4", "-n", "-c", "1", "-W", str(max(1, math.ceil(timeout))), address]


def parse_ping(output, address, returncode):
    text = output.decode(locale.getpreferredencoding(False), errors="replace")
    if re.search(r"permission|not permitted|access.*denied|general failure|operation not allowed", text, re.I):
        raise ProbeError("ping_permission", "Ping could not run. Check installation and OS ICMP permissions.", "error")
    if re.search(r"usage:|invalid option|unknown option|illegal option", text, re.I):
        raise ProbeError("ping_output", "The installed ping tool does not support this platform adapter", "error")
    for line in text.splitlines():
        matching_address = False
        for token in re.findall(r"[0-9a-fA-F:.]+(?:%[0-9A-Za-z_.-]+)?", line):
            for candidate in (token, token.removesuffix(":")):
                try:
                    matching_address |= ipaddress.ip_address(candidate.split("%")[0]) == ipaddress.ip_address(address.split("%")[0])
                except ValueError:
                    pass
        if not matching_address:
            continue
        rtt = re.search(r"(?:time|temps|zeit|tiempo|tempo|время|时间|時間|시간)\s*([=<])\s*([0-9]+(?:[.,][0-9]+)?)\s*(?:ms|мс|毫秒|ミリ秒)", line, re.I)
        evidence = re.search(r"\bttl\s*[=:]\s*\d+", line, re.I) or (re.search(r"icmp_seq[= ]\d+", line) and re.search(r"bytes from", line, re.I))
        if not evidence and ipaddress.ip_address(address).version == 6:
            evidence = rtt and returncode == 0 and not re.search(r"unreachable|exceeded|expired", line, re.I)
        if evidence:
            latency = float(rtt[2].replace(",", ".")) if rtt else None
            return (latency, rtt[1] == "<") if latency is not None and math.isfinite(latency) else (None, False)
    known_no_reply = (sys.platform.startswith("linux") and returncode == 1) or (sys.platform == "darwin" and returncode == 2)
    if known_no_reply or re.search(r"unreachable|timed out|100%.*loss|100%.*perte|100%.*потерь|Zeitüberschreitung|Tiempo de espera agotado", text, re.I):
        raise ProbeError("ping_no_reply", "No matching ICMP echo reply was received")
    raise ProbeError("ping_output", "Ping output could not establish an echo reply; platform/locale may be unsupported", "error")


class Probes:
    def __init__(self):
        self.resolver = Resolver()
        self.tls_context = ssl.create_default_context()
        self.children = set()
        self.reapers = set()
        self.ping_paths = {False: discover_ping(), True: discover_ping(True)}

    async def run(self, config):
        begin, wall = time.monotonic(), time.time()
        result = Result(wall, wall, 0)
        try:
            async with asyncio.timeout(config["timeout_seconds"]):
                if config["type"] == "ping":
                    await self.ping(config, result)
                else:
                    await self.network(config, result, begin)
        except TimeoutError:
            result.outcome, result.code, result.message = "failure", "timeout", "Total execution deadline exceeded (including DNS and setup)"
        except socket.gaierror:
            result.outcome, result.code, result.message = "failure", "dns", "OS name resolution failed"
        except (ssl.SSLError, ssl.CertificateError):
            result.outcome, result.code, result.message = "failure", "tls", "TLS validation/handshake failed; check trust, hostname and certificate dates"
        except ProbeError as exc:
            result.outcome, result.code, result.message = exc.outcome, exc.code, exc.message
        except (ConnectionError, OSError):
            result.outcome, result.code, result.message = "failure", "tcp", "Network connection failed or was refused"
        except Exception:
            result.outcome, result.code, result.message = "error", "executor", "Monitor executor error; check the installation and target settings"
        result.elapsed = max(0, time.monotonic() - begin)
        result.completed = wall + result.elapsed
        return result

    async def ping(self, c, result):
        _, addr = await self.resolver.resolve(c["host"], 0)
        address = addr[0] + (f"%{addr[3]}" if len(addr) > 3 and addr[3] else "")
        result.details["destination_address"] = address
        executable = self.ping_paths[ipaddress.ip_address(address).version == 6]
        if not executable:
            raise ProbeError("ping_missing", "Install the OS ping executable (iputils-ping on Linux); it was not found in trusted system locations", "error")
        try:
            process = await asyncio.create_subprocess_exec(*ping_args(executable, address, c["timeout_seconds"]), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=16384, env={**os.environ, "LC_ALL": "C", "LANG": "C"}, **({"creationflags": 0x08000000} if os.name == "nt" else {}))
        except (PermissionError, FileNotFoundError, OSError):
            raise ProbeError("ping_permission", "Cannot execute OS ping. Check installation and permissions.", "error") from None
        self.children.add(process)
        try:
            output = bytearray()
            while chunk := await process.stdout.read(4096):
                if len(output) + len(chunk) > 16384:
                    raise ProbeError("ping_output", "Ping output exceeded 16 KiB", "error")
                output.extend(chunk)
            await process.wait()
            result.latency, result.bounded = parse_ping(bytes(output), address, process.returncode)
        finally:
            async def reap():
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                while await process.stdout.read(4096):
                    pass
                await process.wait()
                self.children.discard(process)
            reaper = asyncio.create_task(reap())
            self.reapers.add(reaper)
            reaper.add_done_callback(self.reapers.discard)
            await asyncio.shield(reaper)

    async def network(self, c, result, begin):
        is_http = c["type"] == "http"
        url = urlsplit(c["url"]) if is_http else None
        host = url.hostname if is_http else c["host"]
        if is_http and ":" in host:
            host = host.replace("%25", "%", 1)
        port = (url.port or (443 if url.scheme == "https" else 80)) if is_http else c["port"]
        family, addr = await self.resolver.resolve(host, port)
        result.details["destination_address"] = addr[0]
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setblocking(False)
        writer = None
        try:
            connect_start = time.monotonic()
            await asyncio.get_running_loop().sock_connect(sock, addr)
            tcp_latency = (time.monotonic() - connect_start) * 1000
            if c["type"] == "tcp":
                result.latency = tcp_latency
                return
            reader, writer = await asyncio.open_connection(sock=sock, limit=65536)
            if c["type"] == "tls" or url.scheme == "https":
                tls_start = time.monotonic()
                await writer.start_tls(self.tls_context, server_hostname=host.split("%")[0].removesuffix("."), ssl_handshake_timeout=c["timeout_seconds"], ssl_shutdown_timeout=.1)
                tls_latency = (time.monotonic() - tls_start) * 1000
                cert = writer.get_extra_info("ssl_object").getpeercert()
                if cert.get("notAfter"):
                    result.details["certificate_expires_at"] = ssl.cert_time_to_seconds(cert["notAfter"])
                if c["type"] == "tls":
                    result.latency = tls_latency
                    return
            target = quote(url.path or "/", safe="/%:@!$&'()*+,;=-._~")
            if url.query:
                target += "?" + quote(url.query, safe="/%?:@!$&'()*+,;=-._~")
            http_host = f"[{host.split('%')[0]}]" if ":" in host else host.encode("idna").decode("ascii")
            if url.port:
                http_host += f":{url.port}"
            request = f"{c['method']} {target} HTTP/1.1\r\nHost: {http_host}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
            writer.write(request.encode("ascii"))
            await writer.drain()
            total = 0
            while True:
                try:
                    header = await reader.readuntil(b"\r\n\r\n")
                except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
                    raise ProbeError("http_protocol", "Malformed or oversized HTTP response headers") from None
                received_at = time.monotonic()
                total += len(header)
                lines = header[:-4].split(b"\r\n")
                match = re.fullmatch(rb"HTTP/1\.[01] ([1-5][0-9]{2})(?: [\x20-\x7e\x80-\xff]*)?", lines[0])
                if total > 65536 or not match or any(not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+:[\x09\x20-\x7e\x80-\xff]*", line) for line in lines[1:]):
                    raise ProbeError("http_protocol", "Malformed or oversized HTTP response headers")
                lengths = []
                transfer_encoding = False
                for line in lines[1:]:
                    name, value = line.split(b":", 1)
                    if name.lower() == b"content-length":
                        lengths.extend(part.strip() for part in value.split(b","))
                    transfer_encoding |= name.lower() == b"transfer-encoding"
                if lengths and (transfer_encoding or any(not re.fullmatch(rb"[0-9]+", value) for value in lengths) or len(set(value.lstrip(b"0") or b"0" for value in lengths)) != 1):
                    raise ProbeError("http_protocol", "Invalid or ambiguous HTTP response framing headers")
                status = int(match[1])
                if status == 101:
                    raise ProbeError("http_protocol", "Protocol switching is not supported")
                if status >= 200:
                    break
            result.latency = (received_at - begin) * 1000
            result.details["http_status"] = status
            if status != c["expected_status"]:
                raise ProbeError("http_status", f"HTTP status {status}; expected {c['expected_status']}")
        finally:
            if writer:
                writer.transport.abort()
            sock.close()

    def close(self):
        for child in self.children:
            if child.returncode is None:
                child.kill()
        self.resolver.close()

    async def drain(self):
        if self.reapers:
            await asyncio.gather(*self.reapers, return_exceptions=True)
