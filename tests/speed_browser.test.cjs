"use strict";

const {test}=require("node:test");
const assert=require("node:assert/strict");
const {PROFILE,Meter,metric,summary,profileValid,Runner}=require("../connectivity/static/speed-browser.js");

test("receiver clock excludes warm-up and the exclusive upper boundary",()=>{
  const meter=new Meter(PROFILE,100);
  meter.add(100,101.99);meter.add(200,102);meter.add(300,111.99);meter.add(400,112);
  const result=metric(meter.snapshot(112,true));
  assert.equal(result.received_bytes,500);
  assert.equal(result.warmup_bytes,100);
  assert.equal(result.total_bytes,1000);
  assert.equal(result.mbps,500*8/10/1e6);
});

test("four streams use one shared counter/window and do not overflow 32 bits",()=>{
  const meter=new Meter(PROFILE,0);
  for(let stream=0;stream<4;stream++)meter.add(2**32,3+stream);
  const result=metric(meter.snapshot(12,true));
  assert.equal(result.received_bytes,4*2**32);
  assert.equal(result.mbps,4*2**32*8/10/1e6);
});

test("partial results have no final speed",()=>{
  const meter=new Meter(PROFILE,0);meter.add(100,4);
  assert.equal(meter.snapshot(9,true).mbps,null);
  assert.throws(()=>metric(meter.snapshot(9,true)),/complete measurement window/);
});

for(const [name,change] of [
  ["missing receiver confirmation",{complete:false}],
  ["short window",{measurement_seconds:8}],
  ["extended window",{measurement_seconds:12}],
  ["nonfinite clock",{measurement_seconds:NaN}],
  ["unsafe counter",{received_bytes:2**54}],
  ["negative counter",{received_bytes:-1}],
  ["boolean counter",{received_bytes:true}],
  ["inconsistent buckets",{bucket_bytes:Array(10).fill(0)}],
  ["inconsistent total",{total_bytes:0}],
])test(`reject ${name}`,()=>{
  const meter=new Meter(PROFILE,0);meter.add(100,4);
  assert.throws(()=>metric({...meter.snapshot(12,true),...change}));
});

test("untrusted rate is recomputed from receiver bytes; overclaim is rejected",()=>{
  const meter=new Meter(PROFILE,0);meter.add(1e6,4);
  const result=meter.snapshot(12,true);result.mbps=999999;
  assert.equal(metric(result).mbps,.8);
  assert.throws(()=>metric(result,PROFILE,1),/byte counts/);
});

test("median, extrema, and missing latency have honest values",()=>{
  assert.deepEqual(summary([]),{samples:0,median_ms:null,min_ms:null,max_ms:null,timeouts:0,discarded:0,timer_limited:false});
  assert.equal(summary([1,3,100]).median_ms,3);
  assert.equal(summary([1,3,4,100]).median_ms,3.5);
  assert.equal(summary([0,1]).timer_limited,true);
  assert.equal(summary([1],2,3).timeouts,2);
  assert.equal(summary([1],2,3).discarded,3);
});

test("profile compatibility is exact",()=>{
  assert.equal(profileValid(PROFILE),true);
  assert.equal(profileValid({...PROFILE,streams:8}),false);
  assert.equal(profileValid({...PROFILE,measurement_seconds:5}),false);
  assert.equal(profileValid({...PROFILE,extra:true}),false);
});

test("unsupported profile terminates without opening data channels",async()=>{
  const actions=[];
  const run=new Runner({id:"test",started_at:new Date().toISOString(),profile:{...PROFILE,streams:8},metrics:{}},async(path,options)=>actions.push([path,options.method]),"test-csrf",()=>{});
  const result=await run.execute();
  assert.equal(result.outcome,"failed");
  assert.equal(result.error.code,"profile_unsupported");
  assert.equal(run.active,false);
  assert.deepEqual(actions,[["/speedtest/runs/test","DELETE"]]);
});

test("explicit cancellation wins a pending WebSocket connection failure",async()=>{
  let rejectConnection,closed=false;
  const actions=[];
  const run=new Runner({id:"test",started_at:new Date().toISOString(),profile:PROFILE,metrics:{}},async(path,options)=>{
    assert.equal(closed,false);
    actions.push([path,options.method]);
    rejectConnection(Object.assign(new Error("A speedtest connection failed"),{code:"connection_lost"}));
  },"test-csrf",()=>{});
  run.channels.add({close(){closed=true;}});
  run.connect=()=>new Promise((resolve,reject)=>{rejectConnection=reject;});
  const execution=run.execute();
  await run.cancel();
  const result=await execution;
  assert.equal(result.outcome,"cancelled");
  assert.equal(result.error.code,"cancelled");
  assert.equal(run.active,false);
  assert.equal(closed,true);
  assert.deepEqual(actions,[["/speedtest/runs/test","DELETE"],["/speedtest/runs/test","DELETE"]]);
});

test("rejected cancellation clears the local cancellation intent",async()=>{
  const run=new Runner({id:"test",started_at:new Date().toISOString(),profile:PROFILE,metrics:{}},async()=>{throw new Error("Cancellation rejected");},"test-csrf",()=>{});
  try{
    await assert.rejects(run.cancel(),/Cancellation rejected/);
    assert.equal(run.cancelRequested,false);
    assert.equal(run.aborted,false);
  }finally{clearTimeout(run.deadline);}
});
