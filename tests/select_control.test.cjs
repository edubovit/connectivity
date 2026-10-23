"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {nextEnabled, matchingOption} = require("../connectivity/static/select-control.js");
const options = [{text:"HTTP"},{text:"HEAD",disabled:true},{text:"PING"},{text:"POST"},{text:"TCP"}];

test("arrows and endpoints skip disabled options without escaping the list",()=>{
  assert.equal(nextEnabled(options,0,1),2);
  assert.equal(nextEnabled(options,2,-1),0);
  assert.equal(nextEnabled(options,-1,1),0);
  assert.equal(nextEnabled(options,options.length,-1),4);
  assert.equal(nextEnabled(options,4,1),4);
  assert.equal(nextEnabled(options,0,-1),0);
  assert.equal(nextEnabled([],-1,1),-1);
});
test("type-ahead matches case-insensitively, skips disabled options and cycles",()=>{
  assert.equal(matchingOption(options,"p"),2);
  assert.equal(matchingOption(options,"p",2),3);
  assert.equal(matchingOption(options,"p",3),2);
  assert.equal(matchingOption(options,"PO"),3);
  assert.equal(matchingOption(options,"hea"),-1);
  assert.equal(matchingOption(options,"missing"),-1);
  assert.equal(matchingOption([],"p"),-1);
});
