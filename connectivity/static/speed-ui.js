"use strict";

const SpeedUI = (() => {
  let api, getCsrf, node, id, local, visible=false, mode="browser", peers=[], revision=null, selectedPeer="", server=null, latestBrowser=null, runner=null, details=null, timer=null, polling=false, epoch=0, starting=false;
  let chartKey=null;
  const $=name=>document.getElementById(name);
  const numeric=value=>Number.isFinite(value)?new Intl.NumberFormat(undefined,{maximumSignificantDigits:5}).format(value):"N/A";
  const bytes=value=>!Number.isFinite(value)?"—":`${numeric(value/1e6)} MB`;
  const phase=value=>({preparing:"Preparing",idle:"Measuring idle ping",download_setup:"Opening download streams",download_warmup:"Download warm-up",download:"Measuring download",download_done:"Download complete",upload_setup:"Opening upload streams",upload_warmup:"Upload warm-up",upload:"Measuring upload",upload_done:"Upload complete",finalizing:"Finalizing",finished:"Finished"}[value]||value);
  function active(){return runner?.active?runner.result:server?.active;}
  function result(){if(mode==="browser")return runner?.active?runner.result:latestBrowser;const running=server?.active;if(running?.peer?.id===selectedPeer)return running;const latest=server?.latest.find(r=>r.peer.id===selectedPeer);return latest&&details?.id===latest.id?details:latest;}
  function metricBox(label,value,unit,rate=false) {return node("div",{class:`speed-metric${rate?" rate":""}`},node("span",{class:"stat-label"},label),node("strong",{},numeric(value)),node("span",{class:"speed-unit"},unit));}
  function chart(title,measurement){
    const box=node("div",{class:"speed-chart"},node("h3",{},title));
    if(!measurement?.bucket_bytes?.length){box.append(node("p",{class:"muted"},measurement?.complete?"The detailed series is available once this attempt finishes.":"No complete measurement series."));return box;}
    const rates=measurement.bucket_bytes.map((b,i)=>b*8/Math.min(1,measurement.measurement_seconds-i)/1e6),maximum=Math.max(1,...rates);
    const svg=(tag,attrs={})=>{const e=document.createElementNS("http://www.w3.org/2000/svg",tag);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,v);return e;};
    const plot=svg("svg",{viewBox:"0 0 600 120",preserveAspectRatio:"none","aria-label":`${title}: receiver rate in Mbit/s over the measured window`});
    for(const fraction of [0,.5,1]){const y=110-fraction*100;plot.append(svg("path",{d:`M0 ${y}H600`,class:"speed-grid"}));}
    const path=svg("path",{class:"speed-rate-line","vector-effect":"non-scaling-stroke",d:rates.map((r,i)=>`${i?"L":"M"}${(i+.5)/rates.length*600} ${110-r/maximum*100}`).join("")});plot.append(path);
    const info=node("p",{class:"speed-point-info",role:"status"},"Hover, tap, or use arrow keys to inspect a point.");
    const dots=[];
    const descriptions=rates.map((rate,i)=>`${i}–${Math.min(i+1,measurement.measurement_seconds)} s: ${numeric(rate)} Mbit/s · ${bytes(measurement.bucket_bytes[i])} received`);
    rates.forEach((rate,i)=>{const description=descriptions[i],dot=svg("circle",{cx:(i+.5)/rates.length*600,cy:110-rate/maximum*100,r:5,tabindex:0,role:"button","aria-label":description});const show=()=>info.textContent=description;dot.addEventListener("pointerenter",show);dot.addEventListener("click",show);dot.addEventListener("focus",show);dot.addEventListener("keydown",event=>{if(event.key==="ArrowRight"||event.key==="ArrowLeft"){event.preventDefault();dots[Math.max(0,Math.min(rates.length-1,i+(event.key==="ArrowRight"?1:-1)))].focus();}});plot.append(dot);dots.push(dot);});
    const inspect=event=>{const rect=plot.getBoundingClientRect(),index=Math.max(0,Math.min(rates.length-1,Math.floor((event.clientX-rect.left)/rect.width*rates.length)));info.textContent=descriptions[index];};
    plot.addEventListener("pointermove",inspect);plot.addEventListener("pointerdown",inspect);
    const axes=node("div",{class:"speed-axes"},node("div",{class:"speed-y-axis"},...([maximum,maximum/2,0].map(v=>node("span",{},numeric(v))))),plot,node("div",{class:"speed-x-axis"},...([0,measurement.measurement_seconds/2,measurement.measurement_seconds].map(v=>node("span",{},`${v} s`)))));
    box.append(node("p",{class:"chart-title"},"Mbit/s · one-second receiver rates"),axes,info);return box;
  }
  function render(){
    if(!visible)return;
    const busy=active();
    const own=busy&&runner?.result.id===busy.id,cleaning=busy&&(busy.outcome!=="running"||(own&&!runner.active));
    $("speed-start").disabled=starting||Boolean(busy)||(mode==="node"&&!selectedPeer);
    $("speed-cancel").hidden=!busy||cleaning;$("speed-cancel").disabled=starting;
    $("speed-peer-select").hidden=mode!=="node";
    SelectControl.sync($("speed-peer"));
    document.querySelectorAll("[data-speed-mode]").forEach(b=>{const selected=b.dataset.speedMode===mode;b.classList.toggle("selected",selected);b.setAttribute("aria-pressed",String(selected));});
    const peer=peers.find(p=>p.id===selectedPeer);
    $("speed-direction").textContent=mode==="browser"?"Download to this browser; upload to this node.":peer?`Download from ${peer.name}; upload to ${peer.name}. Runs independently of this browser.`:"Add a peer in Configure peers to test another node.";
    $("speed-http-warning").hidden=!(mode==="node"&&peer?.url.startsWith("http:"));
    $("speed-activity").textContent=busy?`${busy.mode==="incoming"?"Incoming test":busy.mode==="node"?`Test with ${busy.peer?.name}`:own?"Browser test":"Test in another session"} · ${cleaning?"Releasing connections":phase(busy.phase)} · ${numeric(busy.elapsed_seconds)} s`:"Ready";
    const current=result();
    if(current?.outcome==="running")$("speed-result-heading").textContent=`${phase(current.phase)} — not a completed result`;
    else $("speed-result-heading").textContent=current?`Latest attempt: ${current.outcome} · ${local(current.completed_at)}`:"No result yet";
    const metrics=current?.metrics||{},idle=metrics.idle_rtt,down=metrics.download,up=metrics.upload,drtt=metrics.download_rtt,urtt=metrics.upload_rtt;
    const latencyDetail=m=>m?`${m.samples} samples · min ${numeric(m.min_ms)} / max ${numeric(m.max_ms)} ms · ${m.timeouts} timeouts / ${m.discarded} discarded${m.timer_limited?" · timer-limited":""}`:"No valid samples";
    $("speed-metrics").replaceChildren(metricBox("Download",down?.mbps,"Mbit/s",true),metricBox("Upload",up?.mbps,"Mbit/s",true),metricBox("Idle latency",idle?.median_ms,"ms"),metricBox("Download latency",drtt?.median_ms,"ms"),metricBox("Upload latency",urtt?.median_ms,"ms"));
    $("speed-statistics").replaceChildren(...[["Idle RTT",latencyDetail(idle)],["Download RTT",latencyDetail(drtt)],["Upload RTT",latencyDetail(urtt)],["Download",down?.complete?`${bytes(down.received_bytes)} / ${down.measurement_seconds} s`:"Incomplete · N/A"],["Upload",up?.complete?`${bytes(up.received_bytes)} / ${up.measurement_seconds} s`:"Incomplete · N/A"]].map(([label,value])=>node("div",{},node("strong",{},label),node("p",{},value))));
    $("speed-result-error").textContent=current?.error?`${current.error.code}: ${current.error.message}`:"";
    const live=current?.live;
    $("speed-live").textContent=live?`Current receiver rate: ${numeric(live.current_mbps)} Mbit/s · ${bytes(live.total_bytes)} payload received · ${numeric(live.measurement_seconds)} / 10 s measured`:"";
    const progress=$("speed-progress");progress.hidden=!busy||cleaning;progress.value=live?.measurement_seconds||0;
    const key=JSON.stringify([current?.id,down?.bucket_bytes,up?.bucket_bytes]);
    if(key!==chartKey){$("speed-charts").replaceChildren(chart("Download · received payload",down),chart("Upload · received payload",up));chartKey=key;}
    $("speed-payload").textContent=current?`Receiver payload totals, including warm-up: download ${bytes(down?.total_bytes)}, upload ${bytes(up?.total_bytes)}. These are not exact on-wire/billed bytes. ${mode==="browser"?"This result lives only in this tab.":"Only this peer’s latest attempt is retained in node RAM."}`:"Browser results live in this tab; peer results live in the initiating node’s RAM. No result history is saved.";
    const previous=new Map([...$("speed-peer-results").children].map(row=>[row.dataset.peer,row]));
    const rows=peers.map(p=>{
      const r=server?.latest.find(r=>r.peer.id===p.id);
      const row=previous.get(p.id)||node("div",{class:"speed-peer-result","data-peer":p.id},node("span",{class:"peer-name"}),node("span",{class:"muted"}),node("button",{class:"quiet",onclick:()=>{mode="node";selectedPeer=p.id;$("speed-peer").value=p.id;details=null;render();poll();$("speed-start").focus();}},"Select"));
      row.children[0].textContent=p.name;
      row.children[1].textContent=r?`${r.outcome} · ↓ ${numeric(r.metrics.download?.mbps)} / ↑ ${numeric(r.metrics.upload?.mbps)} Mbit/s · ${local(r.completed_at)}`:"Not tested";
      row.lastElementChild.setAttribute("aria-label",`Select ${p.name}`);
      return row;
    });
    if(!rows.length)rows.push(node("p",{class:"muted"},"No peers configured."));
    const parent=$("speed-peer-results");
    for(const child of [...parent.children])if(!rows.includes(child))child.remove();
    rows.forEach((row,index)=>{if(parent.children[index]!==row)parent.insertBefore(row,parent.children[index]||null);});
  }
  function schedule(){clearTimeout(timer);if(getCsrf()&&(visible||runner?.active))timer=setTimeout(poll,1000);}
  async function poll(){
    if(polling||!getCsrf()||(!visible&&!runner?.active))return;
    polling=true;clearTimeout(timer);const generation=epoch;
    try{
      const state=await api("/speedtest/state");if(generation!==epoch)return;
      const cfg=await api("/speedtest/peers");if(generation!==epoch)return;
      if(state.config_revision!==cfg.revision)return;
      server=state;
      if(revision!==cfg.revision){revision=cfg.revision;peers=cfg.peers;if(!peers.some(p=>p.id===selectedPeer))selectedPeer=peers[0]?.id||"";$("speed-peer").replaceChildren(...peers.map(p=>node("option",{value:p.id,selected:p.id===selectedPeer},p.name)));}
      const latest=server.latest.find(r=>r.peer.id===selectedPeer);
      if(!latest)details=null;
      if(mode==="node"&&latest&&details?.id!==latest.id){try{details=await api(`/speedtest/runs/${latest.id}`);}catch(e){if(e.status!==404)throw e;details=null;}}
      if(generation!==epoch)return;
      $("speed-error").textContent="";render();
    }catch(error){if(generation===epoch&&getCsrf())$("speed-error").textContent=`Speedtest view is stale: ${error.message} Node tests may still be running.`;}
    finally{polling=false;schedule();}
  }
  async function start(){
    if(starting||active())return;starting=true;render();const generation=epoch;
    try{
      const accepted=await api("/speedtest/runs",{method:"POST",body:JSON.stringify(mode==="browser"?{mode:"browser"}:{mode:"node",peer_id:selectedPeer})});
      if(generation!==epoch)return;
      server={...(server||{latest:[]}),active:accepted};details=null;
      if(accepted.mode==="browser"){
        latestBrowser=accepted;
        runner=new SpeedBrowser.Runner(accepted,api,getCsrf(),result=>{if(generation===epoch){latestBrowser=result;render();}});
        runner.execute().then(result=>{if(generation===epoch){latestBrowser=result;server.active=null;render();poll();}});
      }
      $("speed-action-error").textContent="";
    }catch(error){$("speed-action-error").textContent=error.message;poll();}
    finally{starting=false;render();schedule();}
  }
  async function cancel(){
    const run=active();if(!run)return;
    $("speed-cancel").disabled=true;
    try{if(runner?.active&&runner.result.id===run.id)await runner.cancel();else await api(`/speedtest/runs/${run.id}`,{method:"DELETE",body:"{}"});await poll();}
    catch(error){$("speed-action-error").textContent=error.message;}
    finally{$("speed-cancel").disabled=false;}
  }
  let editSnapshot=null;
  function peerRow(peer=null){
    const p=peer||{id:id("peer"),name:"",url:"",has_password:false};
    const row=node("fieldset",{class:"peer-fields","data-id":p.id},node("legend",{},p.has_password?"Configured peer":"New peer"));
    const input=(label,name,value,extra={})=>node("label",{},label,node("input",{name,value,required:true,...extra}));
    row.append(input("Name","name",p.name,{maxlength:100}),input("Node base URL","url",p.url,{type:"url",maxlength:2048,placeholder:"https://node.example/"}),input(p.has_password?"Peer owner password (leave blank to keep)":"Peer owner password","password","",{type:"password",autocomplete:"new-password",required:!p.has_password,maxlength:1024}),node("button",{type:"button",class:"danger",onclick:()=>{if(confirm(`Remove ${row.querySelector('[name=name]').value||"this peer"}? Its latest result and stored credential will be discarded when you save.`))row.remove();}},"Remove peer"));
    $("peer-fields").append(row);
  }
  async function editPeers(){
    try{editSnapshot=await api("/speedtest/peers");$("peer-fields").replaceChildren();editSnapshot.peers.forEach(peerRow);$("peer-error").textContent="";$("peer-reload").hidden=true;$("peers-dialog").showModal();}
    catch(error){$("speed-action-error").textContent=error.message;}
  }
  async function savePeers(event){
    event.preventDefault();const button=event.submitter;button.disabled=true;
    const candidates=[...$("peer-fields").children].map(row=>{const p={id:row.dataset.id};for(const input of row.querySelectorAll("input"))if(input.name!=="password"||input.value)p[input.name]=input.value;return p;});
    try{await api("/speedtest/peers",{method:"PUT",headers:{"If-Match":editSnapshot.revision},body:JSON.stringify({peers:candidates})});$("peers-dialog").close();details=null;revision=null;await poll();}
    catch(error){$("peer-error").textContent=error.message;$("peer-reload").hidden=![409,412].includes(error.status);}
    finally{button.disabled=false;}
  }
  function setup(options){
    ({api,node,id,local}=options);getCsrf=options.csrf;
    document.querySelectorAll("[data-speed-mode]").forEach(button=>button.addEventListener("click",()=>{mode=button.dataset.speedMode;details=null;render();poll();}));
    $("speed-peer").addEventListener("change",()=>{selectedPeer=$("speed-peer").value;details=null;render();poll();});
    $("speed-start").addEventListener("click",start);$("speed-cancel").addEventListener("click",cancel);$("speed-configure").addEventListener("click",editPeers);
    $("peer-add").addEventListener("click",()=>{if($("peer-fields").children.length>=50){$("peer-error").textContent="At most 50 peers.";return;}peerRow();});
    $("peer-form").addEventListener("submit",savePeers);
    $("peer-close").addEventListener("click",()=>$("peers-dialog").close());$("peer-cancel").addEventListener("click",()=>$("peers-dialog").close());
    $("peers-dialog").addEventListener("close",()=>{$("peer-fields").replaceChildren();editSnapshot=null;});
    $("peer-reload").addEventListener("click",()=>{if(confirm("Discard unsaved peer changes and reload? External file conflicts require reconciliation and restart.")){$("peers-dialog").close();editPeers();}});
  }
  function show(){visible=true;document.title="Speedtest · Connectivity";$("speedtest").hidden=false;render();poll();}
  function hide(){visible=false;$("speedtest").hidden=true;if(!runner?.active)clearTimeout(timer);}
  function reset(){epoch++;runner?.abortLocal();runner=null;latestBrowser=null;server=null;details=null;peers=[];revision=null;starting=false;selectedPeer="";mode="browser";chartKey=null;clearTimeout(timer);hide();$("peers-dialog").close();$("speed-details").open=false;for(const name of ["speed-metrics","speed-statistics","speed-charts","speed-peer-results","speed-peer"])$(name).replaceChildren();for(const name of ["speed-direction","speed-result-error","speed-live","speed-payload","speed-activity"])$(name).textContent="";$("speed-result-heading").textContent="No result yet";}
  return {setup,show,hide,reset};
})();
