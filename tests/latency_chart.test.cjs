"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {scale, geometry, describe, pickPoint} = require("../connectivity/static/latency-chart.js");

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
  assert.equal(description.samples, "3 successful latency samples");
  assert.equal(description.mean, "20 ms");
  assert.equal(description.min, "10 ms");
  assert.equal(description.max, "30 ms");
  assert.equal(description.counts, "3 successes · 2 failures · 1 monitor error");
  assert.match(description.precision, /Recent executions grouped/);
  assert.equal(description.missing, "");
  assert.ok(description.window.length > 20);
});

test("missing metrics remain unavailable, including successful ping without RTT", () => {
  const description = describe(empty({success: 1}), {});
  assert.equal(description.mean, "—");
  assert.equal(description.min, "—");
  assert.equal(description.max, "—");
  assert.match(description.missing, /not plotted as zero/);
  assert.match(description.counts, /^1 success ·/);
});

test("bounded ping readings are explicitly approximate", () => {
  const description = describe(point({latency_samples: 1, latency_approximate: true}), {});
  assert.equal(description.samples, "1 successful latency sample");
  assert.match(description.approximate, /Approximate.*upper-bound.*≤ 1 ms/);
});

test("minute summaries disclose precision even at a raw/summary boundary", () => {
  const summary = {effective_summary_range: {from: "2026-09-18T09:59:00Z", to: "2026-09-18T10:00:03Z"}};
  assert.match(describe(point(), summary).precision, /individual execution times are unavailable/);
  assert.match(describe(point({from: "2026-09-18T10:00:03Z"}), summary).precision, /Recent executions/);
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
