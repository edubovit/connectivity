import argparse
import asyncio
from collections import deque
from contextlib import AsyncExitStack
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp
from aiohttp import web
import numpy as np
import psutil
import yaml

from connectivity.config import ConfigStore
from connectivity.engine import Engine
from connectivity.history import BUCKET, EVENT, NumericLog, RAW, RESOURCE_BUCKET, RETENTION, stamp
from connectivity.web import create_app
from tests.helpers import Certificates, check, configuration, resource, server


def populate(engine):
    now = engine.now()
    start = math.floor((now - RETENTION) / 3600)
    end = math.floor(now / 3600)
    raw_times = np.arange(now - 3600, now, 1)
    for series in [*engine.resources.values(), *(c.series for c in engine.checks.values())]:
        series.created = now - RETENTION
        series.reset_reason = "benchmark synthetic prepopulation"
        for hour in range(start, end + 1):
            a = np.zeros(60, dtype=BUCKET if series.check else RESOURCE_BUCKET)
            times = hour * 3600 + np.arange(60) * 60
            durations = np.clip(now - times, 0, 60)
            a["online"] = durations / 2
            a["offline"] = durations / 2
            if series.check:
                a["success"] = (durations / 2).astype("u4")
                a["failure"] = (durations / 2).astype("u4")
                a["n"] = a["success"]
                a["sum"] = a["n"] * 2
                a["min"], a["max"], a["codes"] = 1, 3, 3
            series.hours[hour] = a
        events = np.empty(len(raw_times), dtype=EVENT)
        events["time"] = raw_times
        events["state"] = np.arange(len(raw_times)) % 2 + 1
        series.events.blocks = deque([[events[i:i+256].copy(), min(256, len(events)-i)] for i in range(0,len(events),256)])
        series.state, series.last = int(events[-1]["state"]), now
        if series.check:
            raw = np.zeros(len(raw_times), dtype=RAW)
            raw["time"], raw["start"], raw["elapsed"] = raw_times, raw_times - .002, .002
            raw["outcome"] = np.arange(len(raw_times)) % 2
            raw["code"] = raw["outcome"]
            raw["latency"] = np.where(raw["outcome"] == 0, 2, np.nan)
            series.raw.blocks = deque([[raw[i:i+256].copy(), min(256, len(raw)-i)] for i in range(0,len(raw),256)])
    return sum(a.nbytes for s in [*engine.resources.values(), *(c.series for c in engine.checks.values())] for a in s.hours.values())


def summary(values):
    return {"samples":len(values),"p50":float(np.percentile(values,50)) if values else None,"p95":float(np.percentile(values,95)) if values else None,"max":max(values,default=None)}


async def benchmark(args):
    report_path = Path(args.report).absolute()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    process = psutil.Process()
    with tempfile.TemporaryDirectory(prefix="connectivity-benchmark-") as temp:
        path = Path(temp)
        certs = Certificates(path)
        async def tcp(reader, writer):
            await reader.read()
        async def http(reader, writer):
            header = await reader.readuntil(b"\r\n\r\n")
            target = header.split(b" ")[1]
            if b"hang" in target:
                await asyncio.sleep(20)
            code = 503 if b"fail" in target or (b"flap" in target and int(time.monotonic()) % 2) else 200
            writer.write(f"HTTP/1.1 {code} Fixture\r\nContent-Length: 100000000\r\n\r\n".encode())
            await writer.drain()
            await reader.read()
        async with AsyncExitStack() as stack:
            tcp_port = await stack.enter_async_context(server(tcp))
            http_port = await stack.enter_async_context(server(http))
            tls_port = await stack.enter_async_context(server(tcp, certs.context()))
            bad_tls_port = await stack.enter_async_context(server(tcp, certs.context("mismatch", dns="wrong.example", ip=None)))
            resources = []
            for i in range(50):
                route = ["healthy", "fail", "hang", "flap", "healthy"][i % 5]
                resources.append(resource(check(f"ping-{i}","ping",host="::1" if i%2 else "127.0.0.1",timeout_seconds=.8),check(f"http-{i}","http",url=f"http://127.0.0.1:{http_port}/{route}",timeout_seconds=.4),check(f"tls-{i}","tls",port=tls_port if i%5<3 else bad_tls_port if i%5==3 else tcp_port,timeout_seconds=.4),check(f"tcp-{i}","tcp",port=tcp_port if i%5 else 1,timeout_seconds=.4),rid=f"r-{i}",name=f"Fixture {i+1:02d}"))
            config = configuration(resources,port=args.port)
            if args.speedtest_fixture_url:
                config["speedtest"]={"peers":[{"id":"fixture-peer","name":"Controlled speedtest peer","url":args.speedtest_fixture_url,"password":"test-only-password"}]}
            config_path = path / "config.yml"
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            store = ConfigStore(config_path)
            engine = Engine(store.data)
            certs.trust(engine.probes.tls_context)
            summary_bytes = populate(engine)
            app = create_app(store,engine,start_engine=False)
            runner = web.AppRunner(app,access_log=None)
            await runner.setup()
            await web.TCPSite(runner,"127.0.0.1",args.port).start()
            for c in engine.checks.values():
                c.base = c.due = engine.mono() + (c.base-engine.anchor_mono)
            engine.last_mono, engine.last_wall = engine.mono(), engine.wall()
            await engine.start()
            print(f"Benchmark listener http://127.0.0.1:{args.port}; test-only password: test-only-password",flush=True)
            print(f"Populated {summary_bytes/2**20:.1f} MiB summary arrays, 720000 raw results; {args.seconds}s real-time load",flush=True)
            status_ms, chart_ms, chart_by_range, rss, child_rss, children, threads, handles, tasks, cpu = [],[],{},[],[],[],[],[],[],[]
            errors=[]
            speed_results=[]
            done=asyncio.Event()
            async def sample():
                process.cpu_percent()
                while not done.is_set():
                    rss.append(process.memory_info().rss/2**20)
                    procs=process.children()
                    child_rss.append(sum(p.memory_info().rss for p in procs if p.is_running())/2**20)
                    children.append(len(procs));threads.append(process.num_threads())
                    handles.append(process.num_handles() if os.name=="nt" else process.num_fds())
                    tasks.append(len(asyncio.all_tasks()));cpu.append(process.cpu_percent())
                    await asyncio.sleep(.2)
            sampler=asyncio.create_task(sample())
            began=time.monotonic()
            try:
                async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
                    origin=f"http://127.0.0.1:{args.port}"
                    async with client.post(origin+"/api/v1/session",json={"password":"test-only-password"},headers={"Origin":origin}) as r:
                        assert r.status==200
                        csrf=(await r.json())["csrf_token"]
                    async def speed_runs():
                        for _ in range(args.speedtest_runs):
                            async with client.post(origin+"/api/v1/speedtest/runs",json={"mode":"node","peer_id":"fixture-peer"},headers={"Origin":origin,"X-CSRF-Token":csrf}) as response:
                                if response.status!=202:
                                    errors.append(f"speedtest start status {response.status}");return
                                run=await response.json()
                            while True:
                                async with client.get(origin+"/api/v1/speedtest/runs/"+run["id"]) as response:
                                    run=await response.json()
                                if run.get("outcome")!="running":
                                    speed_results.append(run);break
                                await asyncio.sleep(1)
                            await asyncio.sleep(1)
                    speed_task=asyncio.create_task(speed_runs()) if args.speedtest_fixture_url else None
                    iteration=0
                    while time.monotonic()-began<args.seconds:
                        t=time.monotonic()
                        async with client.get(origin+"/api/v1/status") as r:
                            data=await r.json()
                            if r.status!=200:errors.append(data)
                        status_ms.append((time.monotonic()-t)*1000)
                        seconds=[3600,86400,RETENTION][iteration%3]
                        end=engine.now()
                        route="/api/v1/history" if iteration%2==0 else "/api/v1/resources/r-1/history"
                        params={"from":stamp(end-seconds),"to":stamp(end),"max_points":"600"}
                        t=time.monotonic()
                        async with client.get(origin+route,params=params) as r:
                            await r.read()
                            if r.status!=200:errors.append(f"history status {r.status}")
                        elapsed=(time.monotonic()-t)*1000
                        chart_ms.append(elapsed);chart_by_range.setdefault(f"{route}/{seconds}",[]).append(elapsed)
                        iteration+=1
                        await asyncio.sleep(max(0,min(15,args.seconds-(time.monotonic()-began))))
                    if speed_task:
                        if not speed_task.done():speed_task.cancel()
                        await asyncio.gather(speed_task,return_exceptions=True)
            finally:
                done.set();await sampler
                report={"platform":platform.platform(),"python":platform.python_version(),"cpu":platform.processor(),"logical_cpus":os.cpu_count(),"host_ram_mib":psutil.virtual_memory().total/2**20,"dependencies":{p:importlib.metadata.version(p) for p in ["aiohttp","numpy","PyYAML","psutil","cryptography"]},"elapsed_seconds":time.monotonic()-began,"fixture_mix":"50 each OS ping (IPv4/IPv6 loopback), HTTP (healthy/status failure/header timeout/flapping), TLS (trusted/mismatch/handshake timeout), TCP (open/closed timeout); all 1-second intervals","prepopulation":"Full 14-day minute arrays, 720000 raw samples, per-second transitions; synthetic history then real-time rollover. Local fixtures and load client run inside measured process.","summary_storage_mib":summary_bytes/2**20,"backend_rss_mib":summary(rss),"ping_children_aggregate_rss_mib":summary(child_rss),"child_process_count":summary(children),"thread_count":summary(threads),"handles_or_fds":summary(handles),"asyncio_tasks":summary(tasks),"cpu_percent_one_core_100":summary(cpu),"status_response_ms":summary(status_ms),"chart_response_ms":summary(chart_ms),"chart_by_route_ms":{key:summary(v) for key,v in chart_by_range.items()},"scheduled_start_delay_ms":summary(list(engine.delays)),"skipped_starts":engine.skipped,"monitor_notices":list(engine.notices),"max_error_entries":max(len(c.series.errors) for c in engine.checks.values()),"errors":errors,"raw_oldest_age_seconds":max(engine.now()-float(c.series.raw.values()[0]["time"]) for c in engine.checks.values()),"browser_memory":"not measured by this script; record separately in live browser verification"}
                report["backend_rss_first_last_mib"] = [rss[0], rss[-1]]
                report["speedtest_results"]=speed_results
                if os.name != "nt":
                    import resource as resource_usage
                    report["backend_os_peak_rss_mib"]=resource_usage.getrusage(resource_usage.RUSAGE_SELF).ru_maxrss/(2**20 if sys.platform=="darwin" else 1024)
                if os.name == "nt":
                    report["backend_os_peak_rss_mib"] = process.memory_info().peak_wset / 2**20
                    parent = process.parent()
                    if parent and parent.name().lower() in ("python.exe", "connectivity.exe"):
                        report["launcher_os_peak_rss_mib"] = parent.memory_info().peak_wset / 2**20
                        report["backend_plus_launcher_peak_upper_bound_mib"] = report["backend_os_peak_rss_mib"] + report["launcher_os_peak_rss_mib"]
                report_path.write_text(json.dumps(report,indent=2),encoding="utf-8")
                print(json.dumps(report,indent=2),flush=True)
                await runner.cleanup();store.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--seconds",type=int,default=180)
    parser.add_argument("--port",type=int,default=8089)
    parser.add_argument("--report",default=".local/benchmark.json")
    parser.add_argument("--speedtest-fixture-url",help="Controlled compatible peer using the test-only-password fixture credential; starts explicit benchmark runs")
    parser.add_argument("--speedtest-runs",type=int,default=1)
    asyncio.run(benchmark(parser.parse_args()))
