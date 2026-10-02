from collections import deque
from datetime import datetime, timedelta, timezone
import math
import copy
import uuid

import numpy as np

from .probes import CODES, OUTCOMES

HOUR = 3600
RETENTION = 14 * 86400
STATES = ("unknown", "online", "offline")
BUCKET = np.dtype([("online", "f8"), ("offline", "f8"), ("success", "u4"), ("failure", "u4"), ("error", "u4"), ("n", "u4"), ("sum", "f8"), ("min", "f4"), ("max", "f4"), ("bounded", "u4"), ("codes", "u4")])
RESOURCE_BUCKET = np.dtype([("online", "f8"), ("offline", "f8")])
RAW = np.dtype([("time", "f8"), ("start", "f8"), ("elapsed", "f4"), ("latency", "f4"), ("outcome", "u1"), ("code", "u1"), ("bounded", "u1")])
EVENT = np.dtype([("time", "f8"), ("state", "u1")])


def stamp(value):
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=float(value))).isoformat(timespec="milliseconds").replace("+00:00", "Z") if value is not None else None


class NumericLog:
    def __init__(self, dtype):
        self.dtype, self.blocks = dtype, deque()

    def append(self, record):
        if not self.blocks or self.blocks[-1][1] == len(self.blocks[-1][0]):
            self.blocks.append([np.empty(256, dtype=self.dtype), 0])
        block = self.blocks[-1]
        block[0][block[1]] = record
        block[1] += 1

    def values(self):
        if not self.blocks:
            return np.empty(0, dtype=self.dtype)
        return np.concatenate([array[:size] for array, size in self.blocks])

    def prune(self, cutoff, carry=False):
        while len(self.blocks) > 1:
            array, size = self.blocks[1] if carry else self.blocks[0]
            if (array[0 if carry else size - 1]["time"]) >= cutoff:
                break
            self.blocks.popleft()
        if self.blocks:
            array, size = self.blocks[0]
            remove = int(np.searchsorted(array["time"][:size], cutoff))
            if carry:
                remove = max(0, remove - 1)
            if remove == size:
                self.blocks.popleft()
            elif remove:
                array[:size-remove] = array[remove:size].copy()
                self.blocks[0][1] -= remove


class Series:
    def __init__(self, now, check=False, reason="created"):
        self.generation = uuid.uuid4().hex
        self.created, self.reset_reason, self.last = now, reason, now
        self.state = 0
        self.check = check
        self.hours = {}
        self.events = NumericLog(EVENT)
        self.events.append((now, 0))
        self.raw = NumericLog(RAW)
        self.errors = deque(maxlen=20)

    def snapshot(self, now, start, end):
        self.flush(now)
        result = copy.copy(self)
        result.hours = {h: a.copy() for h, a in self.hours.items() if (h + 1) * 3600 > start and h * 3600 < min(end + 60, now)}
        for name in ("raw", "events"):
            original = getattr(self, name)
            log = NumericLog(original.dtype)
            log.blocks = deque([[a[:n].copy(), n] for a, n in original.blocks])
            setattr(result, name, log)
        result.errors = deque(self.errors, maxlen=20)
        return result

    def bucket(self, minute):
        hour, index = divmod(minute, 60)
        if hour not in self.hours:
            self.hours[hour] = np.zeros(60, dtype=BUCKET if self.check else RESOURCE_BUCKET)
        return self.hours[hour][index]

    def flush(self, now):
        start = max(self.last, now - RETENTION)
        if self.state:
            while start < now:
                minute = math.floor(start / 60)
                end = min(now, (minute + 1) * 60)
                self.bucket(minute)[STATES[self.state]] += end - start
                start = end
        self.last = max(self.last, now)

    def change(self, state, now):
        self.flush(now)
        if state != self.state:
            self.state = state
            self.events.append((now, state))

    def record(self, result):
        t = result.completed
        code = CODES.index(result.code)
        self.raw.append((t, result.started, result.elapsed, result.latency if result.latency is not None else np.nan, OUTCOMES.index(result.outcome), code, result.bounded))
        bucket = self.bucket(math.floor(t / 60))
        bucket[result.outcome] += 1
        bucket["codes"] |= 1 << code
        if result.outcome == "success" and result.latency is not None:
            if bucket["n"] == 0:
                bucket["min"] = result.latency
            else:
                bucket["min"] = min(bucket["min"], result.latency)
            bucket["max"] = max(bucket["max"], result.latency)
            bucket["n"] += 1
            bucket["sum"] += result.latency
            bucket["bounded"] |= result.bounded
        if result.outcome != "success":
            message = result.message.encode("utf-8")[:512].decode("utf-8", errors="ignore")
            self.errors.append({"time": t, "code": result.code, "message": message})

    def prune(self, now):
        cutoff = now - RETENTION
        for hour in list(self.hours):
            if (hour + 1) * 3600 <= cutoff:
                del self.hours[hour]
            elif hour * 3600 < cutoff:
                self.hours[hour][:min(60, math.ceil((cutoff-hour*3600)/60))] = 0
        self.raw.prune(now - HOUR)
        self.events.prune(now - HOUR, carry=True)
        while self.errors and self.errors[0]["time"] < now - HOUR:
            self.errors.popleft()

    def recent_segments(self, start, end):
        events = self.events.values()
        if not len(events) or start >= end:
            return np.array([]), np.array([]), np.array([], dtype="u1")
        starts = np.maximum(events["time"], max(start, self.created))
        ends = np.minimum(np.r_[events["time"][1:], end], end)
        valid = ends > starts
        return starts[valid], ends[valid], events["state"][valid]

    def query(self, start, end, points, now):
        self.flush(now)
        width = (end - start) / points
        online, offline = np.zeros(points), np.zeros(points)
        count = np.zeros((3, points), dtype="u8")
        latency_n, latency_sum = np.zeros(points, dtype="u8"), np.zeros(points)
        latency_min, latency_max = np.full(points, np.inf), np.full(points, -np.inf)
        bounded = np.zeros(points, dtype=bool)
        codes = 0
        approximate = False
        retained_start = math.ceil((now - RETENTION) / 60) * 60
        raw_boundary = now - HOUR
        older_end = min(end, raw_boundary)
        effective_old_start = max(math.floor(start / 60) * 60, retained_start)
        effective_old_end = math.ceil(older_end / 60) * 60
        available = end > max(self.created, retained_start)
        has_old = available and start < older_end and effective_old_start < effective_old_end

        def add_segments(left, right, on, off):
            valid = right > left
            left, right, on, off = left[valid], right[valid], on[valid], off[valid]
            if not len(left):
                return
            edges = np.linspace(start, end, points + 1)
            index = np.searchsorted(right, edges, side="right")
            partial = index < len(left)
            fractions = np.clip((edges[partial] - left[index[partial]]) / (right[index[partial]] - left[index[partial]]), 0, 1)
            for source, target in ((on, online), (off, offline)):
                integral = np.r_[0, np.cumsum(source)][index]
                integral[partial] += source[index[partial]] * fractions
                target += np.maximum(0, np.diff(integral))

        if has_old:
            hours = sorted(h for h in self.hours if (h + 1) * 3600 > effective_old_start and h * 3600 < effective_old_end)
            if hours:
                times = (np.array(hours)[:, None] * 3600 + np.arange(60) * 60).ravel()
                buckets = np.concatenate([self.hours[h] for h in hours])
                valid = (times >= effective_old_start) & (times < effective_old_end)
                times, buckets = times[valid], buckets[valid]
                available_start = np.maximum(times, self.created)
                left, right = np.maximum(start, available_start), np.minimum(older_end, times + 60)
                available_end = np.minimum(times + 60, raw_boundary)
                full_on, full_off = buckets["online"].copy(), buckets["offline"].copy()
                seam = np.flatnonzero(available_end < times + 60)
                if len(seam):
                    i = seam[0]
                    a, z, states = self.recent_segments(raw_boundary, min(times[i] + 60, now))
                    full_on[i] -= float(np.sum((z - a)[states == 1]))
                    full_off[i] -= float(np.sum((z - a)[states == 2]))
                fractions = np.divide(np.maximum(0, right - left), available_end - available_start, out=np.zeros(len(times)), where=available_end > available_start)
                approximate |= bool(np.any((right > left) & ((left != available_start) | (right != available_end))))
                add_segments(left, right, np.maximum(0, full_on) * fractions, np.maximum(0, full_off) * fractions)
                if self.check:
                    index = np.clip(((times + 30 - start) / width).astype(int), 0, points - 1)
                    for k, outcome in enumerate(OUTCOMES):
                        np.add.at(count[k], index, buckets[outcome])
                    good = buckets["n"] > 0
                    np.add.at(latency_n, index, buckets["n"])
                    np.add.at(latency_sum, index, buckets["sum"])
                    np.minimum.at(latency_min, index[good], buckets["min"][good])
                    np.maximum.at(latency_max, index[good], buckets["max"][good])
                    np.logical_or.at(bounded, index[good], buckets["bounded"][good].astype(bool))
                    codes |= int(np.bitwise_or.reduce(buckets["codes"], initial=0))

        a, z, states = self.recent_segments(max(start, raw_boundary), min(end, now))
        add_segments(a, z, np.where(states == 1, z - a, 0), np.where(states == 2, z - a, 0))
        if self.check:
            raw = self.raw.values()
            count_start = max(start, raw_boundary, effective_old_end if has_old else -math.inf)
            raw = raw[(raw["time"] >= count_start) & (raw["time"] < min(end, now))]
            if len(raw):
                index = np.clip(((raw["time"] - start) / width).astype(int), 0, points - 1)
                for k in range(3):
                    np.add.at(count[k], index[raw["outcome"] == k], 1)
                good = (raw["outcome"] == 0) & np.isfinite(raw["latency"])
                np.add.at(latency_n, index[good], 1)
                np.add.at(latency_sum, index[good], raw["latency"][good])
                np.minimum.at(latency_min, index[good], raw["latency"][good])
                np.maximum.at(latency_max, index[good], raw["latency"][good])
                np.logical_or.at(bounded, index[good], raw["bounded"][good].astype(bool))
                for code in np.unique(raw["code"]):
                    codes |= 1 << int(code)
        observed = float(online.sum() + offline.sum())
        samples = int(latency_n.sum())
        timeline = []
        for i in range(points):
            on, off = float(online[i]), float(offline[i])
            unknown = max(0, width - on - off)
            if off > 0:
                state = "offline"
            elif unknown > .000001 or on == 0:
                state = "unknown"
            else:
                state = "online"
            point = {"from": stamp(start + i * width), "to": stamp(start + (i + 1) * width), "state": state, "online_seconds": on, "offline_seconds": off, "unknown_seconds": unknown}
            if self.check:
                n = int(latency_n[i])
                point.update(success=int(count[0, i]), failure=int(count[1, i]), error=int(count[2, i]), latency_samples=n, latency_mean_ms=float(latency_sum[i] / n) if n else None, latency_min_ms=float(latency_min[i]) if n else None, latency_max_ms=float(latency_max[i]) if n else None, latency_approximate=bool(bounded[i]))
            timeline.append(point)
        answer = {"requested_range": {"from": stamp(start), "to": stamp(end)}, "retention_from": stamp(now - RETENTION), "history_from": stamp(max(self.created, now - RETENTION)), "reset_at": stamp(self.created), "reset_reason": self.reset_reason, "raw_from": stamp(raw_boundary), "effective_summary_range": {"from": stamp(effective_old_start), "to": stamp(effective_old_end)} if has_old else None, "precision": "unavailable" if not available else "minute summaries + exact recent events" if has_old else "exact recent events", "approximate": bool(approximate), "bucket_width_seconds": width, "availability": float(online.sum()) / observed if observed > 0 else None, "coverage": min(1, observed / (end - start)), "online_seconds": float(online.sum()), "offline_seconds": float(offline.sum()), "unknown_seconds": max(0, end - start - observed), "timeline": timeline}
        if self.check:
            answer.update(counts={o: int(count[i].sum()) for i, o in enumerate(OUTCOMES)}, count_latency_effective_range={"from": stamp(min(end, effective_old_start if has_old else max(start, raw_boundary))), "to": stamp(max(end, effective_old_end) if has_old else end)}, diagnostic_codes=[c for i, c in enumerate(CODES) if codes & (1 << i) and i], latency={"samples": samples, "mean_ms": float(latency_sum.sum() / samples) if samples else None, "min_ms": float(latency_min.min()) if samples else None, "max_ms": float(latency_max.max()) if samples else None, "approximate": bool(bounded.any())})
        return answer

    def recent(self, now, before=None, limit=100, start=None, end=None):
        raw = self.raw.values()
        mask = raw["time"] >= max(now - HOUR, start if start is not None else -math.inf)
        if before is not None:
            mask &= raw["time"] < before
        if end is not None:
            mask &= raw["time"] < end
        rows = raw[mask][::-1]
        page = rows[:limit]
        result = [{"started_at": stamp(r["start"]), "completed_at": stamp(r["time"]), "elapsed_seconds": float(r["elapsed"]), "outcome": OUTCOMES[int(r["outcome"])], "code": CODES[int(r["code"])], "latency_ms": float(r["latency"]) if np.isfinite(r["latency"]) else None, "latency_upper_bound": bool(r["bounded"])} for r in page]
        return result, float(page[-1]["time"]) if len(rows) > limit else None
