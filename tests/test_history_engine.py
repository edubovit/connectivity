import asyncio
import copy
import math
from types import SimpleNamespace

import pytest

from connectivity.config import validate
from connectivity.engine import Engine
from connectivity.history import Series, RETENTION
from connectivity.probes import Result
from .helpers import check, configuration, resource


def observation(t, outcome="success", latency=2, code="ok"):
    return Result(t-.01,t,.01,outcome,code,latency)


def test_availability_unknown_and_raw_precision():
    s = Series(100, True)
    s.change(1,110);s.change(2,150);s.change(0,160)
    q=s.query(90,200,10,200)
    assert q["online_seconds"] == 40 and q["offline_seconds"] == 10
    assert q["availability"] == .8 and q["coverage"] == 50/110
    assert q["unknown_seconds"] == 60
    assert Series(200).query(0,100,10,200)["availability"] is None


def test_outage_survives_compaction_and_downsampling():
    s=Series(0,True)
    s.change(1,0);s.record(observation(1));s.change(2,30);s.record(observation(30,"failure",None,"timeout"));s.change(1,31);s.record(observation(32));s.change(0,60)
    s.flush(5000);s.prune(5000)
    q=s.query(0,60,1,5000)
    assert q["offline_seconds"] == 1 and q["online_seconds"] == 59
    assert q["timeline"][0]["state"] == "offline"
    assert q["availability"] == pytest.approx(59/60)
    assert q["coverage"] == 1
    assert q["counts"] == {"success":2,"failure":1,"error":0}
    assert q["latency"]["samples"] == 2
    assert s.recent(5000)[0] == []


@pytest.mark.parametrize("is_check", [False, True], ids=["resource", "check"])
@pytest.mark.parametrize("now", [60, 5000], ids=["raw", "summarized"])
@pytest.mark.parametrize("transitions,expected,durations", [
    ([(0, 1)], "online", (60, 0, 0)),
    ([(0, 2)], "offline", (0, 60, 0)),
    ([], "unknown", (0, 0, 60)),
    ([(0, 1), (59, 2)], "offline", (59, 1, 0)),
    ([(0, 2), (1, 1)], "offline", (59, 1, 0)),
    ([(0, 1), (30, 0), (31, 1)], "unknown", (59, 0, 1)),
    ([(1, 2), (2, 1)], "offline", (58, 1, 1)),
], ids=["online", "offline", "unknown", "failure", "recovery", "observation-gap", "failure-and-gap"])
def test_timeline_uses_worst_state_without_changing_durations(is_check, now, transitions, expected, durations):
    s = Series(0, is_check)
    for time, state in transitions:
        s.change(state, time)
    s.flush(now)
    s.prune(now)
    result = s.query(0, 60, 1, now)
    point = result["timeline"][0]
    assert point["state"] == expected
    for name, duration in zip(("online_seconds", "offline_seconds", "unknown_seconds"), durations):
        assert point[name] == pytest.approx(duration)
        assert result[name] == pytest.approx(duration)
    on, off, unknown = durations
    assert result["coverage"] == pytest.approx((on + off) / 60)
    if on + off:
        assert result["availability"] == pytest.approx(on / (on + off))
    else:
        assert result["availability"] is None
    assert s.state == (transitions[-1][1] if transitions else 0)


def test_partial_minutes_weighted_latency_bounds():
    s=Series(0,True);s.change(1,0)
    for t, latency in [(1,1),(2,3),(61,100)]:
        r=observation(t,latency=latency);r.bounded=t==1;s.record(r)
    s.change(0,120);s.flush(5000)
    q=s.query(30,90,2,5000)
    assert q["approximate"]
    assert q["online_seconds"] == 60
    assert q["counts"]["success"] == 3
    assert q["latency"]["mean_ms"] == pytest.approx(104/3)
    assert q["latency"]["approximate"]
    assert q["effective_summary_range"]["from"].endswith("00:00:00.000Z")


def test_no_double_count_at_raw_summary_seam():
    now=7230
    s=Series(0,True);s.change(1,0)
    for t in range(1,now):s.record(observation(t))
    s.flush(now);s.prune(now)
    q=s.query(0,now,17,now)
    assert q["counts"]["success"] == now-1
    assert q["online_seconds"] == pytest.approx(now)
    assert q["coverage"] == pytest.approx(1)
    assert len(s.recent(now,limit=500)[0]) == 500


def test_retention_cutoff_omits_straddling_minute_and_raw_filter():
    now=RETENTION+30
    s=Series(0,True);s.change(1,0);s.record(observation(1));s.record(observation(61));s.flush(now)
    q=s.query(0,120,2,now)
    assert q["online_seconds"] == 60 and q["unknown_seconds"] == 60
    assert q["counts"]["success"] == 1
    assert s.recent(now)[0] == []
    s.prune(now)
    assert s.state == 1


def test_errors_bounded_and_expire_and_snapshot_independent():
    s=Series(0,True)
    for t in range(30):
        r=observation(t,"error",None,"executor");r.message="é"*400;s.record(r)
    assert len(s.errors)==20 and len(s.errors[-1]["message"].encode())<=512
    snapshot=s.snapshot(30,0,30);s.record(observation(31))
    assert len(snapshot.raw.values())==30
    s.prune(4000);assert not s.errors


def test_long_interval_drops_old_raw_but_retains_carry_state():
    s=Series(0,True)
    s.record(observation(1));s.change(1,1)
    s.prune(4000)
    assert not len(s.raw.values())
    assert len(s.events.values())==1
    assert s.query(1000,4000,10,4000)["availability"]==1


def test_old_partial_creation_minute_never_invents_precreation_uptime():
    s=Series(10,True)
    s.change(1,10);s.record(observation(11));s.change(0,60)
    before=s.query(0,5,5,5000)
    assert before["availability"] is None and before["coverage"]==0
    assert before["counts"]["success"]==0 and before["precision"]=="unavailable"
    assert before["count_latency_effective_range"]["from"]<=before["count_latency_effective_range"]["to"]
    crossing=s.query(0,20,2,5000)
    assert crossing["online_seconds"]==10 and crossing["unknown_seconds"]==10
    assert crossing["approximate"]
    complete=s.query(0,60,6,5000)
    assert complete["online_seconds"]==50 and complete["unknown_seconds"]==10


class Clock:
    def __init__(self):self.mono=0;self.wall=10000
    def advance(self,n,wall=None):self.mono+=n;self.wall+=n if wall is None else wall


class FakeProbes:
    def __init__(self):self.resolver=SimpleNamespace(active=0);self.result="success";self.delay=0;self.active=0;self.maximum=0
    async def run(self,c):
        self.active+=1;self.maximum=max(self.maximum,self.active)
        try:
            if self.delay:await asyncio.sleep(self.delay)
            return observation(0,self.result,2,"ok" if self.result=="success" else "tcp")
        finally:self.active-=1
    def close(self):pass


def engine(clock=None, checks=None):
    c=validate(configuration([resource(*(checks or [check("a"),check("b")]))]))
    return Engine(c,FakeProbes(),**({"monotonic":lambda:clock.mono,"wall":lambda:clock.wall} if clock else {}))


async def publish(e,cid):
    c=e.checks[cid];await e.execute(cid,c,c.token,e.mono())


async def test_state_precedence_and_expiry_pending():
    clock=Clock();e=engine(clock)
    assert e.resources["resource"].state==0
    await publish(e,"a");assert e.resources["resource"].state==0
    e.probes.result="failure";await publish(e,"b");assert e.resources["resource"].state==2
    e.probes.result="success";await publish(e,"b");assert e.resources["resource"].state==1
    c=e.checks["a"];deadline=c.expires
    clock.advance(deadline+.01);e.expire(e.now())
    assert c.series.state==0 and c.reason=="overdue"
    assert e.resources["resource"].state==0


async def test_freshness_uses_completion_not_later_bookkeeping(monkeypatch):
    clock=Clock();e=engine(clock)
    c=e.checks["a"]
    c.base=0
    clock.advance(.99)
    original=c.series.record
    def record(result):
        original(result)
        clock.advance(.02)
    monkeypatch.setattr(c.series,"record",record)
    await publish(e,"a")
    assert c.expires==pytest.approx(3.5)


async def test_resource_overlapping_failures_not_average():
    clock=Clock();e=engine(clock)
    for c in e.checks.values():c.series.change(1,10000)
    e.aggregate("resource",10000)
    e.checks["a"].series.change(2,10010);e.aggregate("resource",10010)
    e.checks["b"].series.change(2,10015);e.aggregate("resource",10015)
    e.checks["a"].series.change(1,10020);e.aggregate("resource",10020)
    e.checks["b"].series.change(1,10025);e.aggregate("resource",10025)
    q=e.resources["resource"].query(10000,10030,30,10030)
    assert q["offline_seconds"]==15 and q["availability"]==.5


async def test_renames_preserve_and_measurement_resets_only_affected():
    e=engine();await publish(e,"a");await publish(e,"b")
    a,b,r=e.checks["a"],e.checks["b"],e.resources["resource"]
    candidate=copy.deepcopy(e.config);candidate["resources"][0]["name"]="New";candidate["resources"][0]["checks"][0]["name"]="New check"
    await e.reconcile(candidate)
    assert e.checks["a"] is a and e.resources["resource"] is r
    candidate=copy.deepcopy(candidate);candidate["resources"][0]["checks"][0]["port"]=1234
    await e.reconcile(candidate)
    assert e.checks["a"] is not a and not e.checks["a"].latest
    assert e.checks["b"] is b and e.resources["resource"] is not r
    assert e.resources["resource"].state==0


async def test_reordering_preserves_histories_generations_cadence_and_active_tasks():
    clock = Clock()
    config = validate(configuration([
        resource(check("a"), check("b"), rid="first", name="First"),
        resource(check("c"), check("d"), rid="second", name="Second"),
    ]))
    e = Engine(config, FakeProbes(), monotonic=lambda:clock.mono, wall=lambda:clock.wall)
    for cid in e.checks:
        await publish(e, cid)
    checks = dict(e.checks)
    resources = dict(e.resources)
    metadata = {cid:(c.token,c.series.generation,c.base,c.due,c.expires,c.latest) for cid,c in checks.items()}
    e.probes.delay = 10
    active = e.checks["a"]
    active.task = asyncio.create_task(e.execute("a",active,active.token,e.mono()))
    task = active.task
    await asyncio.sleep(0)
    try:
        candidate = copy.deepcopy(config)
        candidate["resources"].reverse()
        for r in candidate["resources"]:
            r["checks"].reverse()
        await e.reconcile(candidate)
        assert list(e.resources) == ["second","first"]
        assert list(e.checks) == ["d","c","b","a"]
        assert active.task is task and not task.done()
        for cid,c in e.checks.items():
            assert c is checks[cid]
            assert (c.token,c.series.generation,c.base,c.due,c.expires,c.latest) == metadata[cid]
            assert len(c.series.raw.values()) == 1
        for rid,s in e.resources.items():
            assert s is resources[rid] and s.state == 1
    finally:
        await e.stop()


async def test_old_generation_cannot_publish():
    e=engine();e.probes.delay=.05
    old=e.checks["a"];task=asyncio.create_task(e.execute("a",old,old.token,e.mono()))
    await asyncio.sleep(.01)
    candidate=copy.deepcopy(e.config);candidate["resources"][0]["checks"][0]["port"]=4321
    await e.reconcile(candidate);await task
    assert e.checks["a"].latest is None and not len(e.checks["a"].series.raw.values())


async def test_gap_unknown_and_clock_resets():
    clock=Clock();e=engine(clock)
    await publish(e,"a");await publish(e,"b")
    clock.advance(1);e.clock()
    clock.advance(10);e.clock()
    assert e.notices[-1]["code"]=="monitor_gap"
    q=e.resources["resource"].query(10000,10011,11,e.now())
    assert q["online_seconds"]==1 and q["unknown_seconds"]==10
    assert all(c.latest is not None and c.expires is None for c in e.checks.values())
    await publish(e,"a")
    clock.advance(1,wall=-1);e.clock()
    assert e.notices[-1]["code"]=="clock_reset"
    assert not len(e.checks["a"].series.raw.values())
    clock.advance(1,wall=20);e.clock()
    assert e.notices[-1]["code"]=="clock_reset"


def test_housekeeping_deadline_does_not_follow_backward_wall_clock():
    clock=Clock();e=engine(clock)
    assert e.pruned_at==clock.mono
    e.error_logged["a"]=clock.mono
    clock.advance(1,wall=-86400);e.clock()
    assert e.pruned_at==clock.mono and not e.error_logged


async def test_real_scheduler_no_overlap_timeout_larger_than_interval():
    e=engine(checks=[check("only",timeout_seconds=2)])
    e.probes.delay=1.2
    await e.start();await asyncio.sleep(3.3);await e.stop()
    assert e.probes.maximum==1 and e.skipped>=1
    assert len(e.checks["only"].series.raw.values())>=1
