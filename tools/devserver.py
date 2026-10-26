import argparse
import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
import signal
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import WSMsgType, web
import yaml

from connectivity.config import ConfigStore
from connectivity.engine import Engine
from connectivity.web import create_app
from tests.helpers import Certificates, configuration


async def main(args):
    if args.speedtest_frame_delay:
        class PacedWebSocket(web.WebSocketResponse):
            async def send_bytes(self, data, compress=None):
                await asyncio.sleep(args.speedtest_frame_delay)
                await super().send_bytes(data, compress=compress)

            async def receive(self, timeout=None):
                message = await super().receive(timeout=timeout)
                if message.type == WSMsgType.BINARY:
                    await asyncio.sleep(args.speedtest_frame_delay)
                return message
        web.WebSocketResponse = PacedWebSocket
    path = Path(args.config).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(yaml.safe_dump(configuration(port=args.port)), encoding="utf-8")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    for sig in signals:
        signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    async def http(request):
        if request.path == "/hanging":
            await asyncio.sleep(30)
        status = 503 if request.path == "/unhealthy" else 302 if request.path == "/redirect" else 200
        return web.Response(text="Controlled Connectivity fixture", status=status, headers={"Location":"/ok"} if status == 302 else {})
    with tempfile.TemporaryDirectory(prefix="connectivity-browser-certs-") as temp:
        certs = Certificates(Path(temp))
        fixture = web.Application(); fixture.router.add_route("*", "/{tail:.*}", http)
        fixtures = web.AppRunner(fixture, access_log=None)
        await fixtures.setup()
        await web.TCPSite(fixtures, "127.0.0.1", args.http_port).start()
        await web.TCPSite(fixtures, "127.0.0.1", args.tls_port, ssl_context=certs.context()).start()
        store = ConfigStore(path)
        engine = Engine(store.data)
        certs.trust(engine.probes.tls_context)
        runner = web.AppRunner(create_app(store, engine), access_log=None)
        try:
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", args.port).start()
            print(f"Browser fixture http://127.0.0.1:{args.port}; password test-only-password; HTTP :{args.http_port}, trusted fixture TLS :{args.tls_port}", flush=True)
            await stop.wait()
        finally:
            await runner.cleanup(); await fixtures.cleanup(); store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=".local/browser.yml")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--http-port", type=int, default=8091)
    parser.add_argument("--tls-port", type=int, default=8092)
    parser.add_argument("--speedtest-frame-delay", type=float, default=0, help="Test-only delay per binary frame to bound browser recorder traffic")
    args = parser.parse_args()
    if not 0 <= args.speedtest_frame_delay <= 1:
        parser.error("--speedtest-frame-delay must be between 0 and 1 second")
    asyncio.run(main(args))
