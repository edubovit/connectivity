"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {scale, geometry, describe, pickPoint, timeTicks} = require("../connectivity/static/latency-chart.js");
process.env.TZ = "UTC";

const point = (changes = {}) => ({
  from: "2026-09-18T10:00:00Z", to: "2026-09-18T10:00:06Z",
  latency_samples: 3, latency_min_ms: 10, latency_mean_ms: 20, latency_max_ms: 30,
  success: 3, failure: 0, error: 0, latency_approximate: false, ...changes,
});
const empty = changes => point({latency_samples: 0, latency_min_ms: null, latency_mean_ms: null, latency_max_ms: null, success: 0, ...changes});

for (const maximum of [0, .00001, .05, .5, 1, 10, 123, 300000]) {
  test(`axis includes zero and all measurements, max=${maximum}`, () => {
    const axis = scale(maximum);
    assert.ok(axis.maximum > maximum);
    assert.equal(axis.ticks.length, 5);
    assert.equal(axis.ticks.at(-1), 0);
    assert.equal(new Set(axis.ticks).size, 5);
    assert.ok(axis.ticks.every(Number.isFinite));
  });
}

test("mean connects adjacent populated windows but never crosses a gap", () => {
  const paths = geometry([point(), point(), empty(), point()], 40);
  assert.equal((paths.mean.match(/M/g) || []).length, 2);
  assert.equal((paths.mean.match(/L/g) || []).length, 1);
  assert.equal((paths.dots.match(/M/g) || []).length, 3);
  assert.equal((paths.envelope.match(/M/g) || []).length, 3);
  assert.ok(!/NaN|Infinity|null/.test(JSON.stringify(paths)));
});

test("isolated sample has a visible dot and min/max whisker", () => {
  const paths = geometry([empty(), point(), empty()], 40);
  assert.equal(paths.mean, "M500 90");
  assert.equal(paths.dots, "M500 90h.001");
  assert.equal(paths.envelope, "M500 45V135");
});

test("a zero-millisecond success is a real sample, not missing", () => {
  const paths = geometry([point({latency_min_ms: 0, latency_mean_ms: 0, latency_max_ms: 0})], 1);
  assert.equal(paths.dots, "M500 180h.001");
  assert.equal(describe(point({latency_min_ms: 0, latency_mean_ms: 0, latency_max_ms: 0}), {}).mean, "0 ms");
});

test("failures and monitor errors have separate event marks, not fake latency", () => {
  const paths = geometry([empty({failure: 1}), empty({error: 1})], 1);
  assert.equal(paths.mean, "");
  assert.equal(paths.dots, "");
  assert.equal(paths.envelope, "");
  assert.equal(paths.failures, "M250 0v8");
  assert.equal(paths.errors, "M750 8v8");
});

test("empty history has no measurements or event marks", () => {
  assert.deepEqual(geometry([], 1), {mean: "", dots: "", envelope: "", failures: "", errors: ""});
});

test("tooltip reports count, weighted mean input, extrema and result counts", () => {
  const description = describe(point({failure: 2, error: 1}), {});
  assert.equal(description.samples, "3 samples");
  assert.equal(description.mean, "20 ms");
  assert.equal(description.min, "10 ms");
  assert.equal(description.max, "30 ms");
  assert.equal(description.counts, "2 failures · 1 monitor error");
  assert.equal(description.precision, "");
  assert.equal(description.missing, "");
  assert.ok(description.window.length < 40);
  assert.match(description.fullWindow, /2026/);
  assert.match(description.fullWindow, /GMT|UTC/);
});

test("missing metrics remain unavailable, including successful ping without RTT", () => {
  const description = describe(empty({success: 1}), {});
  assert.equal(description.mean, "—");
  assert.equal(description.min, "—");
  assert.equal(description.max, "—");
  assert.equal(description.missing, "No latency samples");
  assert.equal(description.counts, "1 OK without latency");
});

test("bounded ping readings are explicitly approximate", () => {
  const description = describe(point({latency_samples: 1, latency_approximate: true}), {});
  assert.equal(description.samples, "1 sample");
  assert.equal(description.approximate, "ICMP upper bounds");
});

test("minute summaries disclose precision even at a raw/summary boundary", () => {
  const summary = {effective_summary_range: {from: "2026-09-18T09:59:00Z", to: "2026-09-18T10:00:03Z"}};
  assert.equal(describe(point(), summary).precision, "Minute summaries");
  assert.equal(describe(point({from: "2026-09-18T10:00:03Z"}), summary).precision, "");
});

test("empty and failed windows stay concise without fake metrics or zero-count clutter", () => {
  const missing = describe(empty(), {});
  assert.equal(missing.missing, "No observations");
  assert.equal(missing.mean, "—");
  assert.equal(missing.counts, "");
  assert.equal(missing.precision, "");
  const failed = describe(empty({failure: 1}), {});
  assert.equal(failed.missing, "No latency samples");
  assert.equal(failed.counts, "1 failure");
  assert.equal(describe(point(), {}).counts, "");
});

for (const seconds of [3600, 86400, 1209600, 30, .6]) {
  test(`time marks cover both edges and the exact midpoint over ${seconds}s`, () => {
    const start = Date.parse("2026-10-23T10:00:00Z"), end = start + seconds * 1000;
    const ticks = timeTicks({from: new Date(start).toISOString(), to: new Date(end).toISOString()});
    assert.deepEqual(ticks.map(tick => Date.parse(tick.at)), [start, (start + end) / 2, end]);
    assert.equal(new Set(ticks.map(tick => tick.date + tick.time)).size, 3);
    assert.equal(new Set(ticks.map(tick => tick.full)).size, 3);
    assert.ok(ticks.every(tick => tick.full.includes("2026")));
    assert.ok(ticks.every(tick => seconds >= 86400 ? tick.date : !tick.date));
  });
}

test("date labels disambiguate midnight and year boundaries", () => {
  const ticks = timeTicks({from: "2026-12-31T23:30:00Z", to: "2027-01-01T00:30:00Z"});
  assert.match(ticks[0].date, /2026/);
  assert.match(ticks[2].date, /2027/);
  const description = describe(point({from: ticks[0].at, to: ticks[2].at}), {});
  assert.match(description.window, /2026/);
  assert.match(description.window, /2027/);
});

test("DST overlap keeps absolute tick times and disambiguates tooltip offsets", () => {
  process.env.TZ = "Europe/Berlin";
  try {
    const range = {from: "2026-10-25T00:30:00Z", to: "2026-10-25T01:30:00Z"};
    const ticks = timeTicks(range);
    assert.match(ticks[0].full, /GMT\+2/);
    assert.match(ticks[2].full, /GMT\+1/);
    const description = describe(point(range), {});
    assert.match(description.window, /GMT\+2/);
    assert.match(description.window, /GMT\+1/);
  } finally { process.env.TZ = "UTC"; }
});

test("maximum chart resolution stays in a few numeric SVG paths", () => {
  const paths = geometry(Array.from({length: 1200}, () => point()), 40);
  assert.equal(Object.keys(paths).length, 5);
  assert.equal((paths.dots.match(/M/g) || []).length, 1200);
  assert.ok(!/NaN|Infinity/.test(JSON.stringify(paths)));
});

test("hovering the edge of a sparse dot selects its measurement, not the neighboring gap", () => {
  const points = Array.from({length: 600}, () => empty());
  points[300] = point();
  assert.equal(pickPoint(points, .508, .5, 300, 180, 40), 300);
  assert.equal(pickPoint(points, .508, .1, 300, 180, 40), 304);
  assert.equal(pickPoint(points, .8, .5, 300, 180, 40), 480);
});

test("event-rail hit testing distinguishes failure and monitor-error marks", () => {
  const points = Array.from({length: 600}, () => empty());
  points[300] = empty({failure: 1});
  points[303] = empty({error: 1});
  assert.equal(pickPoint(points, .501, .2, 300, 16, 40, true), 300);
  assert.equal(pickPoint(points, .501, .8, 300, 16, 40, true), 303);
});
