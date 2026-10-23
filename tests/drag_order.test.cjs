"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {insertionIndex, gridInsertionIndex} = require("../connectivity/static/drag-order.js");

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

const grid = [
  {left:0,right:200,top:0,bottom:100}, {left:216,right:416,top:0,bottom:100},
  {left:0,right:200,top:116,bottom:260}, {left:216,right:416,top:116,bottom:260},
];
for (const [x,y,index] of [[0,30,0],[180,30,1],[280,30,1],[400,30,2],[0,140,2],[180,140,3],[400,140,4],[10,300,4]]) {
  test(`grid drop at ${x},${y}`,()=>assert.equal(gridInsertionIndex(grid,x,y),index));
}
test("grid insertion handles the gap left by the dragged tile",()=>{
  assert.equal(gridInsertionIndex(grid.slice(1),40,30),0);
  assert.equal(gridInsertionIndex(grid.slice(1),400,30),1);
  assert.equal(gridInsertionIndex([],40,30),0);
});
