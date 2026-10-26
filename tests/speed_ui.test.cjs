"use strict";

const {test}=require("node:test");
const assert=require("node:assert/strict");
const {readFileSync}=require("node:fs");
const vm=require("node:vm");

const source=readFileSync(require.resolve("../connectivity/static/speed-ui.js"),"utf8");
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};

class Element extends EventTarget {
  constructor(){super();this.children=[];this.dataset={};this.textContent="";this.classList={toggle(){}};}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  insertBefore(child,before){child.parent=this;this.children=this.children.filter(e=>e!==child);const index=this.children.indexOf(before);this.children.splice(index<0?this.children.length:index,0,child);}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(e=>e!==this);}
  setAttribute(){}
  close(){this.dispatchEvent(new Event("close"));}
  get lastElementChild(){return this.children.at(-1);}
}

function harness(){
  const elements=new Map(),window=new EventTarget(),timers=new Map(),frames=new Map(),calls=[],runners=[];
  const element=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const buttons=["browser","node"].map(mode=>Object.assign(new Element(),{dataset:{speedMode:mode}}));
  const node=(tag,attrs={},...children)=>{const e=Object.assign(new Element(),attrs);if(attrs["data-peer"])e.dataset.peer=attrs["data-peer"];e.append(...children);return e;};
  let csrf="fixture-csrf",sequence=0,hold=null;
  const peer={id:"peer",name:"Fixture",url:"http://127.0.0.1/"};
  const state={config_revision:"fixture",active:null,latest:[]},cfg={revision:"fixture",peers:[peer]};
  class Runner {
    constructor(accepted,api,token,progress){this.result=accepted;this.progress=progress;this.active=true;this.execution=deferred();runners.push(this);}
    execute(){return this.execution.promise;}
    abortLocal(){this.aborted=true;}
    finish(){this.active=false;this.result={...this.result,outcome:"completed",phase:"finished"};this.progress(this.result);this.execution.resolve(this.result);}
  }
  const api=async(path,options={})=>{
    calls.push({path,method:options.method||"GET"});
    if(hold?.path===path){const pending=hold;hold=null;return pending.promise;}
    if(path==="/speedtest/state")return state;
    if(path==="/speedtest/peers")return cfg;
    if(path==="/speedtest/runs")return {id:"browser-run",mode:"browser",outcome:"running",phase:"idle",metrics:{}};
    if(path.startsWith("/speedtest/runs/"))return state.latest[0];
    throw new Error(`Unexpected API: ${path}`);
  };
  const document=Object.assign(new EventTarget(),{getElementById:element,querySelectorAll:()=>buttons,hidden:false});
  const context=vm.createContext({window,document,Intl,SelectControl:{sync(){}},SpeedBrowser:{Runner},
    setTimeout(callback){const id=++sequence;timers.set(id,callback);return id;},clearTimeout:id=>timers.delete(id),
    requestAnimationFrame(callback){const id=++sequence;frames.set(id,callback);return id;},cancelAnimationFrame:id=>frames.delete(id)});
  const ui=vm.runInContext(source+"\nSpeedUI",context);
  ui.setup({api,node,id:()=>"fixture",local:value=>value||"fixture time",csrf:()=>csrf});
  const dispatch=type=>window.dispatchEvent(new Event(type));
  return {ui,element,window,document,state,cfg,peer,calls,timers,frames,runners,dispatch,
    click:id=>element(id).dispatchEvent(new Event("click")),
    mode:mode=>buttons.find(button=>button.dataset.speedMode===mode).dispatchEvent(new Event("click")),
    hold(path){const pending=deferred();hold={path,...pending};return pending;},
    tick(){const callbacks=[...timers.values()];timers.clear();for(const callback of callbacks)callback();},
    frame(){const callbacks=[...frames.values()];frames.clear();for(const callback of callbacks)callback();},
    logout(){csrf=null;ui.reset();},
  };
}

for(const boundary of ["state","peers"])test(`navigation invalidates a pending ${boundary} poll before its next fetch`,async()=>{
  const h=harness();h.state.latest=[{id:"old",peer:h.peer,metrics:{},outcome:"completed"}];
  const pending=h.hold(`/speedtest/${boundary}`);
  h.ui.show();h.mode("node");await flush();
  const before=h.calls.length;
  h.dispatch("beforeunload");
  pending.resolve(boundary==="state"?h.state:h.cfg);await flush();
  assert.equal(h.calls.length,before);
  assert.equal(h.timers.size,0);
  assert.equal(h.element("speed-error").textContent,"");
  h.dispatch("pagehide");assert.equal(h.frames.size,0);
  h.dispatch("pageshow");await flush();
  assert.equal(h.calls.at(-1).path,"/speedtest/runs/old");
  assert.equal(h.timers.size,1);
});

test("a cancelled navigation resumes on the next rendered frame without overlapping the old poll",async()=>{
  const h=harness(),pending=h.hold("/speedtest/state");h.ui.show();
  h.dispatch("beforeunload");h.frame();
  assert.equal(h.calls.length,1);
  pending.resolve(h.state);await flush();
  assert.equal(h.calls.length,1);
  assert.equal(h.timers.size,1);
  h.tick();await flush();
  assert.deepEqual(h.calls.map(call=>call.path),["/speedtest/state","/speedtest/state","/speedtest/peers"]);
});

test("failed navigation resumes polling after its pending response has settled",async()=>{
  const h=harness(),pending=h.hold("/speedtest/state");h.ui.show();
  h.dispatch("beforeunload");pending.reject(new Error("Navigation interrupted request"));await flush();
  assert.equal(h.element("speed-error").textContent,"");assert.equal(h.timers.size,0);
  h.frame();await flush();
  assert.equal(h.calls.at(-1).path,"/speedtest/peers");assert.equal(h.timers.size,1);
});

test("stale detailed results cannot leak through a restored poll",async()=>{
  const h=harness();h.state.latest=[{id:"old",peer:h.peer,metrics:{},outcome:"completed"}];
  const pending=h.hold("/speedtest/runs/old");h.ui.show();h.mode("node");await flush();
  h.dispatch("beforeunload");h.dispatch("pagehide");h.dispatch("pageshow");
  pending.resolve({...h.state.latest[0],error:{code:"obsolete",message:"Must not publish"}});await flush();
  h.tick();await flush();
  assert.equal(h.calls.filter(call=>call.path==="/speedtest/runs/old").length,2);
  assert.equal(h.element("speed-result-error").textContent,"");
});

test("ordinary API failures remain visible and retry normally",async()=>{
  const h=harness(),pending=h.hold("/speedtest/state");h.ui.show();
  pending.reject(new Error("Fixture offline"));await flush();
  assert.match(h.element("speed-error").textContent,/Fixture offline/);
  h.tick();await flush();assert.equal(h.element("speed-error").textContent,"");
});

test("a hidden tab and cancelled navigation preserve browser-run progress and completion",async()=>{
  const h=harness();h.ui.show();await flush();h.click("speed-start");await flush();
  const run=h.runners[0];
  h.document.hidden=true;h.document.dispatchEvent(new Event("visibilitychange"));
  h.tick();await flush();assert.equal(h.timers.size,1);assert.equal(run.aborted,undefined);
  h.dispatch("beforeunload");h.frame();await flush();
  run.finish();await flush();
  assert.equal(run.aborted,undefined);
  assert.match(h.element("speed-result-heading").textContent,/Latest attempt: completed/);
  assert.equal(h.calls.some(call=>call.method==="DELETE"),false);
});

test("a node run is not cancelled or cleared by page departure and restoration",async()=>{
  const h=harness();h.state.active={id:"node-run",mode:"node",peer:h.peer,metrics:{},phase:"download",outcome:"running"};
  h.ui.show();h.mode("node");await flush();
  h.dispatch("beforeunload");h.dispatch("pagehide");h.dispatch("pageshow");await flush();
  assert.match(h.element("speed-activity").textContent,/Test with Fixture/);
  assert.equal(h.calls.some(call=>call.method!=="GET"),false);
});

test("logout still invalidates pending polls and aborts the local runner",async()=>{
  const h=harness();h.ui.show();await flush();h.click("speed-start");await flush();
  const pending=h.hold("/speedtest/state");h.tick();h.logout();
  const before=h.calls.length;pending.resolve(h.state);await flush();
  assert.equal(h.calls.length,before);assert.equal(h.timers.size,0);
  assert.equal(h.runners[0].aborted,true);
});
