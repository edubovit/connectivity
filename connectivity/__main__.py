import argparse
import asyncio
import logging
import signal
import sys

from aiohttp import web

from .config import ConfigStore, Problem
from .web import create_app


async def serve(store):
    app = create_app(store)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=10)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    for sig in signals:
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    try:
        await runner.setup()
        server = store.data["server"]
        site = web.TCPSite(runner, server["host"], server["port"])
        await site.start()
        logging.info("Config: %s | listener %s:%d | %d resources / %d checks | memory-only: 1h raw / 14d minutes", store.path, server["host"], server["port"], len(store.data["resources"]), sum(len(r["checks"]) for r in store.data["resources"]))
        if server.get("public_url"):
            logging.info("Public URL: %s | HTTPS terminated by trusted loopback proxy; password remains plaintext in configuration", server["public_url"])
        else:
            logging.info("Trusted private network only: HTTP and plaintext configuration password. Ctrl+C to stop.")
        await stop.wait()
    finally:
        await runner.cleanup()
        logging.info("Stopped; monitoring history and sessions discarded")


def main():
    parser = argparse.ArgumentParser(description="Private connectivity monitoring; no public-internet exposure")
    parser.add_argument("--config", required=True, help="YAML file path (required); see bundled example.yml")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 14):
        parser.exit(1, "Connectivity requires Python 3.14.\n")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = None
    try:
        store = ConfigStore(args.config)
        asyncio.run(serve(store))
    except (Problem, OSError) as exc:
        parser.exit(1, f"Startup failed: {exc}\n")
    except KeyboardInterrupt:
        pass
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    main()
