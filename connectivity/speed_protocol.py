import asyncio
from dataclasses import asdict, dataclass
import json
import math
import re
import statistics
import time

VERSION = 1
CHUNK = 65536
WINDOW = 2097152
ACK_STEP = 262144
MAX_SAFE = 2**53 - 1


class SpeedError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


@dataclass(frozen=True)
class Profile:
    streams: int = 4
    warmup_seconds: float = 2
    measurement_seconds: float = 10
    timeout_seconds: float = 60
    setup_seconds: float = 10
    channel_seconds: float = 5
    idle_seconds: float = 5
    idle_samples: int = 20
    idle_interval: float = .1
    loaded_interval: float = .2
    rtt_timeout: float = 2
    liveness_seconds: float = 5

    def wire(self):
        return {"version": VERSION, **asdict(self), "chunk_bytes": CHUNK, "window_bytes": WINDOW}


PROFILE = Profile()


def decode(text):
    try:
        value = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError, RecursionError):
        raise SpeedError("protocol_error", "Malformed speedtest control message") from None


def integer(value, maximum=MAX_SAFE):
    return type(value) is int and 0 <= value <= maximum


def finite(value, minimum=0, maximum=MAX_SAFE):
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is not None


class Meter:
    def __init__(self, profile=PROFILE, now=None):
        self.profile = profile
        self.start = time.monotonic() if now is None else now
        self.begin = self.start + profile.warmup_seconds
        self.end = self.begin + profile.measurement_seconds
        self.buckets = [0] * math.ceil(profile.measurement_seconds)
        self.total = self.warm = 0

    def add(self, size, now=None):
        now = time.monotonic() if now is None else now
        self.total += size
        if now < self.begin:
            self.warm += size
        elif now < self.end:
            self.buckets[min(len(self.buckets)-1, int(now-self.begin))] += size

    def snapshot(self, now=None, complete=False):
        now = time.monotonic() if now is None else now
        elapsed = max(0, min(self.profile.measurement_seconds, now-self.begin))
        complete = complete and now >= self.end
        bins = list(self.buckets)
        value = {"complete": complete, "measurement_seconds": self.profile.measurement_seconds if complete else elapsed,
                 "received_bytes": sum(bins), "warmup_bytes": self.warm, "total_bytes": self.total, "bucket_bytes": bins}
        value["mbps"] = value["received_bytes"]*8/self.profile.measurement_seconds/1e6 if complete else None
        index = min(len(bins)-1, int(elapsed)-1)
        value["current_mbps"] = bins[index]*8/min(1,self.profile.measurement_seconds-index)/1e6 if index >= 0 else None
        return value


def validate_metric(value, profile=PROFILE, sent=MAX_SAFE):
    if not isinstance(value, dict) or value.get("complete") is not True or not finite(value.get("measurement_seconds")) or abs(value["measurement_seconds"]-profile.measurement_seconds) > .000001:
        raise SpeedError("invalid_measurement", "Receiver did not confirm the complete measurement window")
    if any(not integer(value.get(key)) for key in ("received_bytes", "warmup_bytes", "total_bytes")):
        raise SpeedError("protocol_error", "Invalid receiver byte counts")
    bins = value.get("bucket_bytes")
    if not isinstance(bins, list) or len(bins) != math.ceil(profile.measurement_seconds) or any(not integer(b) for b in bins) or sum(bins) != value["received_bytes"]:
        raise SpeedError("protocol_error", "Invalid receiver time buckets")
    if value["received_bytes"] + value["warmup_bytes"] > value["total_bytes"] or value["total_bytes"] > sent:
        raise SpeedError("protocol_error", "Receiver counts exceed transferred payload")
    result = {key: value[key] for key in ("complete", "measurement_seconds", "received_bytes", "warmup_bytes", "total_bytes")}
    result["bucket_bytes"] = list(bins)
    result["mbps"] = value["received_bytes"]*8/profile.measurement_seconds/1e6
    return result


def rtt_summary(samples, timeouts=0, discarded=0):
    return {"samples": len(samples), "median_ms": statistics.median(samples) if samples else None,
            "min_ms": min(samples) if samples else None, "max_ms": max(samples) if samples else None,
            "timeouts": timeouts, "discarded": discarded, "timer_limited": 0 in samples}


def validate_rtt(value, profile=PROFILE):
    if not isinstance(value, dict) or any(not integer(value.get(key), 100) for key in ("samples", "timeouts", "discarded")):
        raise SpeedError("protocol_error", "Invalid latency summary")
    result = {key: value[key] for key in ("samples", "timeouts", "discarded")}
    for key in ("median_ms", "min_ms", "max_ms"):
        number = value.get(key)
        if (value["samples"] and not finite(number, 0, profile.rtt_timeout*1000)) or (not value["samples"] and number is not None):
            raise SpeedError("protocol_error", "Invalid latency value")
        result[key] = number
    if value["samples"] and not result["min_ms"] <= result["median_ms"] <= result["max_ms"]:
        raise SpeedError("protocol_error", "Inconsistent latency summary")
    result["timer_limited"] = bool(value.get("timer_limited"))
    return result


def validate_live(value, profile=PROFILE):
    if not isinstance(value, dict) or not finite(value.get("measurement_seconds"), 0, profile.measurement_seconds):
        raise SpeedError("protocol_error", "Invalid progress timing")
    if any(not integer(value.get(key)) for key in ("received_bytes", "warmup_bytes", "total_bytes")):
        raise SpeedError("protocol_error", "Invalid progress counters")
    if value.get("current_mbps") is not None and not finite(value["current_mbps"]):
        raise SpeedError("protocol_error", "Invalid live transfer rate")
    return {key: value.get(key) for key in ("measurement_seconds", "received_bytes", "warmup_bytes", "total_bytes", "current_mbps")}


class Sender:
    def __init__(self, ws, payload):
        self.ws, self.payload = ws, payload
        self.sent = self.acked = 0
        self.credit = asyncio.Event()
        self.stopped = False

    def acknowledge(self, value):
        if not integer(value) or not self.acked <= value <= self.sent:
            raise SpeedError("protocol_error", "Invalid flow-control acknowledgement")
        self.acked = value
        self.credit.set()

    async def pump(self):
        batch = 0
        while not self.stopped:
            if self.sent-self.acked >= WINDOW:
                self.credit.clear()
                await self.credit.wait()
                continue
            self.sent += len(self.payload)
            await self.ws.send_bytes(self.payload)
            batch += 1
            if batch % 4 == 0:
                await asyncio.sleep(0)

    def stop(self):
        self.stopped = True
        self.credit.set()
