import argparse
import asyncio
from pathlib import Path
import signal
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import web
import yaml

from connectivity.config import ConfigStore
from connectivity.web import create_app


async def main(args):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in [signal.SIGINT, signal.SIGTERM, *([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])]:
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    runners, stores = [], []
    with tempfile.TemporaryDirectory(prefix="connectivity-speedtest-") as temp:
        try:
            for index, port in enumerate((args.port, args.peer_port)):
                other = args.peer_port if index == 0 else args.port
                config = {"version": 1, "server": {"host": "127.0.0.1", "port": port},
                          "auth": {"password": "test-only-password"}, "resources": [{"id":"fixture-monitor","name":"Fixture monitoring", "checks":[
                              {"id":"tcp-heartbeat","name":"TCP heartbeat","type":"tcp","host":"127.0.0.1","port":port,"interval_seconds":1,"timeout_seconds":1},
                              {"id":"http-heartbeat","name":"HTTP heartbeat","type":"http","url":f"http://127.0.0.1:{other}/","method":"GET","expected_status":200,"interval_seconds":1,"timeout_seconds":1}]}],
                          "speedtest": {"peers": [{"id": "fixture-peer", "name": "Other fixture node", "url": f"http://127.0.0.1:{other}/", "password": "test-only-password"}]}}
                path = Path(temp)/f"node-{index}.yml"
                path.write_text(yaml.safe_dump(config), encoding="utf-8")
                store = ConfigStore(path); stores.append(store)
                runner = web.AppRunner(create_app(store), access_log=None)
                runners.append(runner)
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", port).start()
                print(f"Fixture node http://127.0.0.1:{port}/ — password test-only-password", flush=True)
            try:
                await asyncio.wait_for(stop.wait(), args.duration)
            except TimeoutError:
                pass
        finally:
            for runner in runners:
                await runner.cleanup()
            for store in stores:
                store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Two loopback test nodes, isolated from production configuration; shared-process functional fixture, not a cross-host benchmark")
    parser.add_argument("--port", type=int, default=18090)
    parser.add_argument("--peer-port", type=int, default=18091)
    parser.add_argument("--duration", type=int, default=900)
    asyncio.run(main(parser.parse_args()))
