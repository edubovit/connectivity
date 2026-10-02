from collections import deque
from dataclasses import fields, is_dataclass
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from connectivity.config import validate
from connectivity.engine import Engine
from connectivity.history import EVENT, NumericLog, Series
from connectivity.probes import Result
from tests.helpers import check, configuration, resource
from tools.benchmark import populate


def owned_size(roots):
    seen = set()
    def size(value):
        if id(value) in seen:
            return 0
        seen.add(id(value))
        result = sys.getsizeof(value)
        if isinstance(value, dict):
            result += sum(size(k) + size(v) for k, v in value.items())
        elif isinstance(value, (list, tuple, deque)):
            result += sum(size(v) for v in value)
        elif is_dataclass(value):
            if hasattr(value, "__dict__"):
                result += size(value.__dict__)
            else:
                result += sum(size(getattr(value, f.name)) for f in fields(value))
        elif isinstance(value, (Series, NumericLog)):
            result += size(value.__dict__)
        return result
    return size(roots)


def main():
    resources = [resource(*[check(f"c-{i}-{j}", "http", name=f"Check {i}-{j}".ljust(100,"x"), url="http://127.0.0.1/"+str(i*4+j).zfill(2031)) for j in range(4)], rid=f"r-{i}", name=f"Resource {i}".ljust(100,"x")) for i in range(50)]
    engine = Engine(validate(configuration(resources)))
    try:
        populate(engine)
        now = engine.now()
        for s in [*engine.resources.values(), *(c.series for c in engine.checks.values())]:
            times = np.arange(now-3600, now, .5 if s.check else .125)
            events = np.empty(len(times), dtype=EVENT)
            events["time"], events["state"] = times, np.arange(len(times)) % 3
            s.events.blocks = deque([[events[i:i+256].copy(), min(256,len(events)-i)] for i in range(0,len(events),256)])
        for cid, c in engine.checks.items():
            for index in range(20):
                message = f"{cid}-{index}".ljust(508,"x") + "😀"
                c.series.errors.append({"time":now-index,"code":"executor","message":message})
            c.latest = Result(now-.002,now,.002,"error","executor",message=c.series.errors[-1]["message"],details={"destination_address":"127.0.0.1","http_status":599})
        size = owned_size((engine.checks, engine.resources))
        report = {"history_owned_mib":size/2**20,"budget_mib":350,"within_budget":size<=350*2**20,"shape":"50 resources / 200 checks; full 14-day summaries; 720000 raw results; 2 events/second/check and 8 events/second/resource; 20 distinct 512-byte UTF-8 errors/check using wide Unicode; maximum names/URLs; current-result metadata","method":"sys.getsizeof over the owned Python container graph and owning NumPy arrays, deduplicating shared objects; excludes runtime, task/socket state and shared dtype internals (covered separately by process RSS benchmark)"}
        print(json.dumps(report,indent=2))
        if not report["within_budget"]:
            raise SystemExit(1)
    finally:
        engine.probes.close()


if __name__ == "__main__":
    main()
