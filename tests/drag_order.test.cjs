"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {insertionIndex} = require("../connectivity/static/drag-order.js");

for (const [label, midpoints, y, expected] of [
  ["empty", [], 0, 0],
  ["before first", [100, 200, 300], -50, 0],
  ["before second", [100, 200, 300], 150, 1],
  ["after center", [100, 200, 300], 200, 2],
  ["after last", [100, 200, 300], 900, 3],
  ["unequal card heights", [35, 330, 800], 400, 2],
  ["scrolled list", [-250, -40, 170], 10, 2],
]) {
  test(`drop location: ${label}`, () => assert.equal(insertionIndex(midpoints, y), expected));
}
