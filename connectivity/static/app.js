"use strict";
const $ = id => document.getElementById(id);
const node = (tag, attrs = {}, ...children) => {
  const result = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) result.addEventListener(key.slice(2), value);
    else if (key === "class") result.className = value;
    else if (key in result) result[key] = value;
    else result.setAttribute(key, value);
  }
  result.append(...children.filter(c => c !== null && c !== undefined));
  return result;
};
const svgNode = (tag, attrs = {}) => {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
  return element;
};
const local = value => value ? new Date(value).toLocaleString(undefined, {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",second:"2-digit"}) : "No result yet";
const percent = value => value === null ? "N/A" : value === 1 ? "100%" : value === 0 ? "0%" : value > .9999 ? "<100%" : value < .0001 ? "<0.01%" : `${new Intl.NumberFormat(undefined,{maximumFractionDigits:2}).format(value*100)}%`;
const ms = value => value === null || value === undefined ? "—" : `${value.toFixed(2)} ms`;
const seconds = value => `${value.toFixed(3)} s`;
const id = prefix => `${prefix}-${crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)), b=>b.toString(16).padStart(2,"0")).join("")}`;
let csrf = null, config = null, statusData = null, historyData = null, selected = {seconds:3600};
let busy = false, again = false, timer = null, editing = null, editSnapshot = null, settingsSnapshot = null, lastRange = null;
let orderSnapshot = null, orderSaving = false, orderLoading = false;
let resourceDrag = null, checkDrag = null, checkOrderStart = null;
let orderRequest = 0;
const detailId = () => location.hash.startsWith("#resource/") ? decodeURIComponent(location.hash.slice(10)) : null;
const speedPage = () => location.hash === "#speedtest";
function rememberRange() {
  try {sessionStorage.setItem("connectivity-range",JSON.stringify({csrf,selected}));} catch {}
}
function restoreRange() {
  try {
    const saved=JSON.parse(sessionStorage.getItem("connectivity-range"));
    if(saved?.csrf!==csrf)return;
    const range=saved.selected;
    if([3600,86400,1209600].includes(range?.seconds))selected={seconds:range.seconds};
    else if(typeof range?.from==="string"&&typeof range?.to==="string"&&Date.parse(range.from)<Date.parse(range.to)&&Date.parse(range.to)-Date.parse(range.from)<=1209600000)selected={from:range.from,to:range.to};
  } catch {}
}

async function api(path, options = {}) {
  const headers = {...options.headers};
  if (options.method && options.method !== "GET") Object.assign(headers, {"Content-Type":"application/json", "X-CSRF-Token":csrf || ""});
  const controller = new AbortController();
  const deadline = setTimeout(()=>controller.abort(),10000);
  let result, body;
  try {
    result = await fetch(`api/v1${path}`, {...options, headers, cache:"no-store", signal:controller.signal});
    body = await result.json();
  } catch (error) {
    if (controller.signal.aborted) throw new Error("API request timed out. For a save, reload configuration to check whether it committed.");
    throw error;
  } finally { clearTimeout(deadline); }
  if (!result.ok) {
    const failure = new Error(body.error?.message || "Request failed");
    failure.code = body.error?.code; failure.status = result.status;
    if (result.status === 401 && path !== "/session") showLogin();
    throw failure;
  }
  return body;
}
function showLogin() {
  SpeedUI.reset();
  orderRequest++;orderLoading=false;
  resourceDrag?.destroy();resourceDrag=null;orderSnapshot=null;
  endCheckOrder(false);
  csrf = null; clearTimeout(timer);
  statusData=null;historyData=null;
  $("content").replaceChildren(node("p",{class:"muted"},"Loading current observations…"));
  $("summary").replaceChildren();$("health").replaceChildren();setStale(null);
  $("dashboard").hidden = true; $("navigation").hidden = true; $("login").hidden = false;
  $("editor").close(); $("settings-dialog").close(); $("resource-order-panel").hidden=true;
}
function showDashboard() {
  $("login").hidden = true; $("navigation").hidden = false;
  navigate();
}
function navigate() {
  if (!csrf) return;
  for (const [name, active] of [["nav-resources",!speedPage()],["nav-speedtest",speedPage()]]) {
    if (active) $(name).setAttribute("aria-current","page"); else $(name).removeAttribute("aria-current");
  }
  document.querySelector(".skip-link").href=speedPage()?"#speedtest":"#dashboard";
  $("dashboard").hidden = speedPage();$("live").hidden=speedPage();
  if(speedPage()) {if(orderSnapshot)endResourceOrder();clearTimeout(timer);setStale(null);SpeedUI.show();return;}
  SpeedUI.hide();
  $("live").textContent="Connecting";
  refresh();
}
function setStale(message) {
  $("banner").textContent = message ? `Dashboard disconnected. ${message} Retrying; monitoring may still be running.` : "";
  $("banner").hidden = !message;
  $("live").textContent = message ? "Disconnected" : "Live";
  $("live").classList.toggle("stale", Boolean(message));
}
function getRange(data) {
  if (selected.from) return {from:selected.from, to:selected.to};
  const to = new Date(data.server_time);
  return {from:new Date(to.getTime() - selected.seconds * 1000).toISOString(), to:to.toISOString()};
}
async function refresh() {
  if (!csrf || document.hidden || speedPage()) return;
  if (busy) { again = true; return; }
  busy = true; clearTimeout(timer);
  const rid = detailId();
  const selection = JSON.stringify(selected);
  try {
    const current = await api("/status");
    const range = getRange(current);
    const historical = await api(`${rid ? `/resources/${encodeURIComponent(rid)}` : ""}/history?${new URLSearchParams({...range,max_points:600})}`);
    if (speedPage()) return;
    if (rid !== detailId() || selection !== JSON.stringify(selected) || historical.config_revision !== current.config_revision) { again = true; return; }
    statusData = current; historyData = historical; lastRange = range;
    setStale(null); render();
    $("updated").textContent = `Updated ${local(current.server_time)} · refresh ${current.refresh_interval_seconds}s`;
  } catch (error) {
    if (error.status === 404 && rid) { location.hash = ""; again = true; }
    else if (csrf) setStale(error.message);
  } finally {
    busy = false;
    if (again) { again = false; if (csrf) timer = setTimeout(refresh, 0); }
    else if (csrf) timer = setTimeout(refresh, (statusData?.refresh_interval_seconds || 15) * 1000);
  }
}
function badge(state) { return node("span", {class:`badge ${state}`}, state); }
function timeline(series, previous) {
  if (previous) { previous.update(series); return previous; }
  const container = node("div", {class:"timeline"});
  const svg = svgNode("svg", {preserveAspectRatio:"none",role:"slider",tabindex:0,"aria-label":"Availability timeline. Inspect with arrow keys, hover, or tap.","aria-valuemin":0});
  const inspection=node("div",{class:"timeline-inspection",hidden:true});
  const labels=node("div",{class:"timeline-labels"});
  let active=0;
  const show=index=>{
    active=Math.max(0,Math.min(series.timeline.length-1,index));
    const point=series.timeline[active];
    const text=`${local(point.from)} – ${local(point.to)}\n${point.state} · online ${seconds(point.online_seconds)} · offline ${seconds(point.offline_seconds)} · unknown ${seconds(point.unknown_seconds)}\n${series.precision}${series.approximate?" · approximate boundaries":""}`;
    inspection.textContent=text;inspection.hidden=false;
    svg.setAttribute("aria-valuenow",active);svg.setAttribute("aria-valuetext",text);
  };
  const inspect=event=>{const box=svg.getBoundingClientRect();show(Math.floor((event.clientX-box.left)/box.width*series.timeline.length));};
  svg.addEventListener("pointermove",inspect);
  svg.addEventListener("pointerdown",event=>{inspect(event);svg.focus({preventScroll:true});});
  svg.addEventListener("pointerleave",()=>inspection.hidden=true);
  svg.addEventListener("focus",()=>show(active));
  svg.addEventListener("blur",()=>inspection.hidden=true);
  svg.addEventListener("keydown",event=>{
    if(event.key==="Escape"){inspection.hidden=true;return;}
    const index=event.key==="Home"?0:event.key==="End"?series.timeline.length-1:event.key==="ArrowLeft"?active-1:event.key==="ArrowRight"?active+1:null;
    if(index!==null){event.preventDefault();show(index);}
  });
  container.append(svg,labels,inspection);
  container.update=value=>{
    series=value;
    const paths=new Map();
    series.timeline.forEach((point,i)=>paths.set(point.state,(paths.get(point.state)||"")+`M${i} 0h1.05v24h-1.05Z`));
    svg.setAttribute("viewBox",`0 0 ${series.timeline.length} 24`);
    svg.setAttribute("aria-valuemax",series.timeline.length-1);
    svg.replaceChildren(...[...paths].map(([state,d])=>svgNode("path",{class:`timeline-${state}`,d})));
    labels.replaceChildren(...LatencyCharts.timeTicks(series.requested_range).map(tick=>node("time",{dateTime:tick.at,title:tick.full,"aria-label":tick.full},tick.date?node("span",{class:"tick-date"},tick.date):null,node("span",{},tick.time))));
    if(!inspection.hidden)show(active);
    else {svg.setAttribute("aria-valuenow",0);svg.setAttribute("aria-valuetext",series.timeline[0].state);}
  };
  container.update(series);
  return container;
}
function stat(label, value) { return node("div", {class:"stat"}, node("div", {class:"stat-label"}, label), node("div", {class:"stat-value"}, value)); }
function precision(series) {
  return `${series.precision}${series.approximate ? " · approximate partial-minute durations" : ""}. Unknown time is excluded from availability, included in coverage.`;
}
function resourceCard(resource, detailed = false, previous) {
  const series = historyData.resources[resource.id];
  if (!series) return node("p", {class:"muted"}, "Waiting for a coherent history snapshot…");
  const card=previous||node("article",{class:"resource-card","data-id":resource.id});
  const oldTitle=card.querySelector(".resource-name");
  const linked=!detailed&&!orderSnapshot;
  const title=oldTitle?.tagName===(linked?"A":"SPAN")?oldTitle:node(linked?"a":"span",{class:"resource-name",...(linked?{href:`#resource/${encodeURIComponent(resource.id)}`}:{})});
  title.textContent=resource.name;
  const indicators = resource.checks.filter(c=>c.reason === "overdue" || c.latest?.outcome === "error");
  const meta = node("div", {class:"resource-meta"}, badge(resource.state), node("span",{},`${resource.checks.length} ${resource.checks.length===1?"check":"checks"}`), indicators.length ? node("span",{class:"offline"},`${indicators.length} need attention`) : null);
  const head=card.querySelector(".resource-head")||node("div",{class:"resource-head"});
  updateChildren(head,[title,meta]);
  const actions = card.querySelector(".card-actions")||node("div",{class:"card-actions"},node("button",{class:"quiet","aria-label":`Edit ${resource.name}`,onclick:()=>openEditor(resource.id)},"Edit"));
  actions.querySelector("button").setAttribute("aria-label",`Edit ${resource.name}`);
  actions.hidden = Boolean(orderSnapshot);
  const body=card.querySelector(".resource-body")||node("div",{class:"resource-body"});
  updateChildren(body,[timeline(series,body.querySelector(".timeline")),stat("Availability",percent(series.availability)),stat("Coverage",percent(series.coverage))]);
  const children=[head,body,actions];
  if(detailed){
    const details=card.querySelector(".resource-details")||node("details",{class:"resource-details"},node("summary",{},"Resource details"),node("div"));
    const counts=resource.check_counts;
    details.lastElementChild.replaceChildren(node("p",{class:"precision"},`${counts.online} online · ${counts.offline} offline · ${counts.unknown} unknown checks`),node("p",{class:"precision"},precision(series)),node("p",{class:"precision"},`History since ${local(resource.reset_at)} · ${resource.reset_reason}`));
    children.push(details);
  }
  updateChildren(card,children);
  return card;
}
function latencyChart(check, series, previous) {
  const rangeKey = JSON.stringify(selected);
  if (previous && previous.dataset.generation === check.generation && previous.dataset.range === rangeKey) {
    previous.update(check, series);
    return previous;
  }
  const chart = LatencyCharts.create(check, series);
  chart.dataset.generation = check.generation;
  chart.dataset.range = rangeKey;
  return chart;
}
function updateChildren(parent, children) {
  for (const child of [...parent.children]) if (!children.includes(child)) child.remove();
  children.forEach((child, index) => {
    if (parent.children[index] !== child) parent.insertBefore(child, parent.children[index] || null);
  });
}
function checkCard(check, previousDetails, previousChart, previousCard) {
  const series = historyData.checks[check.id];
  if (!series) return node("div");
  const latest = check.latest;
  const target = check.type === "http" ? `${check.method} ${check.url} → expected ${check.expected_status}` : `${check.host}${check.port ? `:${check.port}` : ""}`;
  const card = previousCard || node("article",{class:"check-card","data-check":check.id});
  const children = [node("div",{class:"check-header"},node("div",{},node("div",{class:"check-title"},node("h3",{},check.name),node("span",{class:"type-tag"},check.type.toUpperCase()),badge(check.state)),node("div",{class:"target"},target)),node("div",{class:"latest-metric"},node("strong",{},`${latest?.latency_upper_bound?"≤ ":""}${latest?.outcome==="success"?ms(latest.latency_ms):"—"}`),node("span",{class:"muted"},"Latest latency")))];
  const info = node("dl",{class:"detail-grid"});
  const detail=(label,value)=>info.append(node("div",{},node("dt",{},label),node("dd",{},value)));
  detail("Interval / timeout",`${check.interval_seconds}s / ${check.timeout_seconds}s`);
  detail("Last result",local(check.latest_completion));
  detail("State reason",check.reason);
  detail("Metric",check.metric);
  if (latest) {
    detail("Total duration",seconds(latest.elapsed_seconds));
    if (latest.destination_address) detail("Destination",latest.destination_address);
    if (latest.http_status) detail("HTTP status",`${latest.http_status} / expected ${check.expected_status}`);
    if (latest.certificate_expires_at) detail("Certificate expiry",`${local(latest.certificate_expires_at)} (${latest.certificate_expires_at}) · ${(latest.certificate_remaining_seconds/86400).toFixed(1)} days left`);
  }
  if (latest?.message) children.push(node("div",{class:latest.outcome === "error" ? "notice" : "warning"},`${latest.code}: ${latest.message}`));
  const body=card.querySelector(".resource-body")||node("div",{class:"resource-body"});
  updateChildren(body,[timeline(series,body.querySelector(".timeline")),stat("Availability",percent(series.availability)),stat("Coverage",percent(series.coverage))]);
  children.push(body,latencyChart(check,series,previousChart));
  const lat = series.latency, counts = series.counts;
  children.push(node("div",{class:"latency-stats"},node("span",{},"Min ",node("strong",{},ms(lat.min_ms))),node("span",{},"Mean ",node("strong",{},ms(lat.mean_ms))),node("span",{},"Max ",node("strong",{},ms(lat.max_ms)))));
  const diagnostics=card.querySelector(".diagnostics")||node("details",{class:"diagnostics"},node("summary",{},"Check details"),node("div"));
  diagnostics.lastElementChild.replaceChildren(info,node("p",{class:"precision"},`${lat.samples} latency samples · ${counts.success} successes · ${counts.failure} failures · ${counts.error} monitor errors`),node("p",{class:"precision"},precision(series)),node("p",{class:"precision"},`Count/latency range: ${local(series.count_latency_effective_range.from)} – ${local(series.count_latency_effective_range.to)}`),node("p",{class:"precision"},`Check history since ${local(check.reset_at)} · ${check.reset_reason}`),...(series.diagnostic_codes.length?[node("p",{class:"precision"},`Diagnostic codes: ${series.diagnostic_codes.join(", ")}`)]:[]));
  children.push(diagnostics);
  const rangeKey = JSON.stringify(selected);
  if (previousDetails && previousDetails.dataset.generation === check.generation && previousDetails.dataset.range === rangeKey) {
    const errors = previousDetails.querySelector(".error-list");
    errors.replaceChildren(...check.recent_errors.slice().reverse().map(e=>node("li",{},`${local(e.time)} · ${e.code}: ${e.message}`)));
    children.push(previousDetails);
    updateChildren(card, children);
    return card;
  }
  const details = node("details",{class:"execution-details","data-check":check.id,"data-generation":check.generation,"data-range":rangeKey},node("summary",{},"Recent executions & errors"));
  const errors = node("ul",{class:"error-list"});
  check.recent_errors.slice().reverse().forEach(e=>errors.append(node("li",{},`${local(e.time)} · ${e.code}: ${e.message}`)));
  details.append(node("p",{class:"raw-note"},"Executions: a snapshot of the selected range within the last hour. Reload for new results. Errors: latest 20, updated live."),errors);
  const records = node("div",{class:"records"});
  const load = node("button",{type:"button",onclick:()=>loadRecords(check.id,records,load,null)},"Load recent executions");
  details.append(load,records); children.push(details);
  updateChildren(card, children);
  return card;
}
async function loadRecords(cid, container, button, cursor) {
  button.disabled = true;
  try {
    const query = new URLSearchParams({...lastRange,limit:100}); if(cursor)query.set("cursor",cursor);
    const data = await api(`/checks/${encodeURIComponent(cid)}/results?${query}`);
    if (!cursor) container.replaceChildren();
    if (!data.results.length) container.append(node("p",{class:"muted"},"No raw executions in this range."));
    else {
      let table = container.querySelector("table");
      if (!table) {table=node("table",{},node("thead",{},node("tr",{},...(["Completed (local)","Outcome","Code","Latency","Duration"].map(t=>node("th",{scope:"col"},t))))),node("tbody"));container.append(table);}
      data.results.forEach(r=>table.querySelector("tbody").append(node("tr",{},node("td",{},local(r.completed_at)),node("td",{class:r.outcome==="success"?"online":r.outcome==="failure"?"offline":"unknown"},r.outcome),node("td",{},r.code),node("td",{},`${r.latency_upper_bound?"≤ ":""}${ms(r.latency_ms)}`),node("td",{},seconds(r.elapsed_seconds)))));
    }
    button.textContent = data.next_cursor ? "Load more" : "Reload recent executions";
    button.onclick = null;
    const replacement = node("button",{type:"button",onclick:()=>loadRecords(cid,container,replacement,data.next_cursor)},button.textContent);
    button.replaceWith(replacement);
  } catch(error) {container.append(node("p",{class:"error"},error.message));} finally {button.disabled=false;}
}
function render() {
  if (!statusData || !historyData) return;
  const rid = detailId(), resources = statusData.resources, resource = resources.find(r=>r.id===rid);
  $("reorder-resources").disabled = resources.length < 2 || orderLoading;
  $("reorder-resources").hidden = Boolean(rid || orderSnapshot);
  $("resource-order-panel").hidden = !orderSnapshot;
  for (const id of ["name-filter","state-filter","add-resource","settings"]) $(id).disabled = Boolean(orderSnapshot);
  $("page-title").textContent = resource ? resource.name : "Resources";
  document.title=resource?`${resource.name} · Connectivity`:"Connectivity";
  $("page-subtitle").textContent = resource ? `${resource.checks.length} checks · last result ${local(resource.latest_completion)}` : "Reachability from this node.";
  $("back-resources").hidden=!resource;
  $("add-resource").hidden=Boolean(resource);
  $("filters").hidden = Boolean(rid); $("summary").hidden = Boolean(rid);
  $("summary").replaceChildren(...[["resources",resources.length,""],["online",resources.filter(r=>r.state==="online").length,"online"],["offline",resources.filter(r=>r.state==="offline").length,"offline"],["unknown",resources.filter(r=>r.state==="unknown").length,"unknown"]].map(([label,value,kind])=>node("div",{class:"summary-item"},node("strong",{class:kind},String(value)),node("span",{},label))));
  $("health").replaceChildren(...statusData.health.notices.slice(-2).map(n=>node("p",{class:"notice"},`${n.code} · ${local(n.at)} — ${n.message}`)));
  if (statusData.health.skipped_starts) $("health").append(node("p",{class:"precision"},`${statusData.health.skipped_starts} skipped cadence starts since startup (busy probes, monitor gaps, or capacity). These are not target failures.`));
  if(!$("health").children.length)$("health").append(node("p",{class:"precision"},"No monitor notices."));
  $("monitor-health").dataset.notice=String(Boolean(statusData.health.notices.length||statusData.health.skipped_starts));
  $("monitor-health").querySelector("summary").textContent=statusData.health.notices.length?"Monitor notices":"Monitor health";
  $("range-label").textContent = `${selected.from ? "Fixed" : "Rolling"} · ${local(lastRange.from)} – ${local(lastRange.to)}`;
  document.querySelectorAll("[data-range]").forEach(b=>{const active=selected.from?b.dataset.range==="custom":+b.dataset.range===selected.seconds;b.classList.toggle("selected",active);b.setAttribute("aria-pressed",String(active));});
  const content = $("content");
  if (orderSnapshot && resourceDrag) {
    if (statusData.config_revision !== orderSnapshot.revision && !$("resource-order-error").textContent) {
      $("resource-order-error").textContent = "Configuration changed in another tab. Your order is unsaved; reload before saving.";
      $("reload-resource-order").hidden = false;
    }
    return;
  }
  const previousDetails = new Map([...content.querySelectorAll(".execution-details")].map(d=>[d.dataset.check,d]));
  const previousCharts = new Map([...content.querySelectorAll(".latency-chart")].map(chart=>[chart.dataset.check,chart]));
  const previousCards = new Map([...content.querySelectorAll(".check-card")].map(card=>[card.dataset.check,card]));
  const previousResources = new Map([...content.querySelectorAll(".resource-card")].map(card=>[card.dataset.id,card]));
  if (resource) {
    updateChildren(content, [resourceCard(resource,true,previousResources.get(resource.id)),...resource.checks.map(c=>checkCard(c,previousDetails.get(c.id),previousCharts.get(c.id),previousCards.get(c.id)))]);
  } else if (!resources.length) {
    content.replaceChildren(node("div",{class:"empty"},node("h2",{},"No resources yet"),node("p",{},"Add a website, server, or network device to start monitoring."),node("button",{class:"primary",onclick:()=>openEditor()},"Add your first resource")));
  } else {
    const shown = orderSnapshot ? orderSnapshot.resources.map(r=>resources.find(current=>current.id===r.id)) : resources.filter(r=>r.name.toLowerCase().includes($("name-filter").value.toLowerCase()) && (!$("state-filter").value || r.state===$("state-filter").value));
    updateChildren(content,shown.map(r=>resourceCard(r,false,previousResources.get(r.id))));
    if (!shown.length) content.append(node("div",{class:"empty"},node("h2",{},"No matching resources"),node("p",{},"Try another name or current-state filter.")));
  }
}
function field(label, name, value, options={}) {
  const input = node("input",{name,value:value??"",required:true,...options});
  return node("label",{},label,input);
}
function updateCheckPositions() {
  const items = [...$("check-fields").children];
  items.forEach((item,index)=>item.querySelector(".order-position").textContent=`Check ${index+1} of ${items.length}`);
  $("reorder-checks").disabled = items.length < 2;
}
function startCheckOrder() {
  if (checkDrag || $("save-resource").disabled || $("check-fields").children.length < 2) return;
  $("check-fields").querySelectorAll(".check-editor").forEach(details=>details.open=false);
  checkOrderStart = [...$("check-fields").children];
  checkDrag = DragOrder.attach($("check-fields"), {
    name:item=>`check ${item.querySelector('input[name="name"]').value.trim() || "Unnamed check"}`,
    announce:text=>$("check-order-status").textContent=text,
    changed:updateCheckPositions,
  });
  $("reorder-checks").hidden = true;
  for (const id of ["done-check-order","cancel-check-order","check-order-help"]) $(id).hidden=false;
  $("add-check").disabled=true;
  $("check-fields").querySelectorAll(".remove-check").forEach(button=>button.disabled=true);
}
function endCheckOrder(cancel) {
  checkDrag?.destroy();checkDrag=null;
  if (cancel && checkOrderStart) checkOrderStart.forEach(item=>$("check-fields").append(item));
  checkOrderStart=null;
  $("reorder-checks").hidden=false;
  for (const id of ["done-check-order","cancel-check-order","check-order-help"]) $(id).hidden=true;
  $("add-check").disabled=false;
  $("check-fields").querySelectorAll(".remove-check").forEach(button=>button.disabled=false);
  updateCheckPositions();
  $("check-order-status").textContent=cancel?"Starting order restored. Field edits kept.":"";
}
function endResourceOrder() {
  orderRequest++;orderLoading=false;
  resourceDrag?.destroy();resourceDrag=null;orderSnapshot=null;
  $("resource-order-panel").hidden=true;
  render();
}
async function openResourceOrder() {
  if (orderLoading || orderSaving) return;
  const request=++orderRequest;
  orderLoading=true;$("reorder-resources").disabled=true;
  try {
    const snapshot = await api("/config");
    if (request!==orderRequest || detailId() || orderSaving || !csrf) return;
    if (!statusData || snapshot.revision !== statusData.config_revision) {
      const current=await api("/status"),range=getRange(current);
      const historical=await api(`/history?${new URLSearchParams({...range,max_points:600})}`);
      if (snapshot.revision!==current.config_revision || current.config_revision!==historical.config_revision) throw new Error("Configuration changed while opening reorder mode. Try again.");
      if (request!==orderRequest || detailId() || !csrf) return;
      statusData=current;historyData=historical;lastRange=range;
    }
    if (snapshot.resources.length < 2) {endResourceOrder();refresh();return;}
    resourceDrag?.destroy();resourceDrag=null;orderSnapshot=snapshot;
    $("resource-order-status").textContent = "";
    $("resource-order-error").textContent = "";
    $("reload-resource-order").hidden = true;
    render();
    resourceDrag=DragOrder.attach($("content"), {
      name:item=>`resource ${item.querySelector(".resource-name").textContent}`,
      announce:text=>$("resource-order-status").textContent=text,
    });
    $("save-resource-order").disabled=snapshot.resources.length<2;
  } catch (error) {
    if (request!==orderRequest) return;
    if (orderSnapshot) $("resource-order-error").textContent = error.message;
    else if (csrf) setStale(error.message);
  } finally {
    if (request===orderRequest) {orderLoading=false;$("reorder-resources").disabled=(statusData?.resources.length||0)<2;}
  }
}
function addCheckFields(check = null) {
  const c = check || {id:id("check"),name:"",type:"http",interval_seconds:30,timeout_seconds:5};
  const box = node("fieldset",{class:"check-fields","data-id":c.id},node("legend",{class:"order-position"},"Check"));
  if(!check)$("check-fields").querySelectorAll(".check-editor").forEach(details=>details.open=false);
  const summary=node("summary");
  const details=node("details",{class:"check-editor",open:!check||!$("check-fields").children.length},summary);
  const fields=node("div",{class:"check-edit-fields"});
  const name=field("Check name","name",c.name,{maxlength:100,oninput:()=>{updateSummary();checkDrag?.refresh();}});
  const type = node("select",{name:"type",disabled:Boolean(check)});
  ["http","ping","tcp","tls"].forEach(t=>type.append(node("option",{value:t,selected:t===c.type},t.toUpperCase())));
  function updateSummary(){summary.textContent=`${name.querySelector("input").value.trim()||"New check"} · ${type.value.toUpperCase()}`;}
  const targets = node("div",{class:"target-fields wide"});
  function targetsForType() {
    targets.replaceChildren();
    if (type.value === "http") {
      targets.append(field("URL","url",c.url||"",{type:"url",placeholder:"https://service.example/",maxlength:2048,class:""}));
      const method=node("select",{name:"method"});["GET","HEAD","POST","PUT","PATCH","DELETE","OPTIONS"].forEach(m=>method.append(node("option",{value:m,selected:m===(c.method||"GET")},m)));
      targets.append(node("label",{},"Method",method),field("Expected status","expected_status",c.expected_status??200,{type:"number",min:200,max:599,step:1}));
      const warning=node("p",{class:"warning wide"},"This method can modify the target on every check, even with an empty body.");
      const update=()=>warning.hidden=!["POST","PUT","PATCH","DELETE"].includes(method.value);method.addEventListener("change",update);update();targets.append(warning);
    } else {
      targets.append(field("Host or IP","host",c.host||"",{maxlength:253,placeholder:"192.168.1.1 or service.example"}));
      if(type.value!=="ping")targets.append(field("Port","port",c.port??(type.value==="tls"?443:""),{type:"number",min:1,max:65535,step:1}));
    }
  }
  type.addEventListener("change",()=>{targetsForType();updateSummary();});targetsForType();updateSummary();
  type.title=check?"Type cannot change. Remove and recreate this check to use another type.":"Check type";
  fields.append(name,node("label",{},"Type",type),targets,field("Interval (s)","interval_seconds",c.interval_seconds,{type:"number",min:1,max:86400,step:"any"}),field("Timeout (s)","timeout_seconds",c.timeout_seconds,{type:"number",min:.1,max:300,step:"any"}),node("button",{type:"button",class:"remove-check danger wide",onclick:()=>{if($("check-fields").children.length===1){$("editor-error").textContent="A resource needs at least one check. Delete the resource instead.";return;}box.remove();updateCheckPositions();$("check-order-status").textContent="Check removed. Save to apply.";}},"Remove check"));
  details.append(fields);box.append(details);
  $("check-fields").append(box);
  updateCheckPositions();
}
async function openEditor(rid=null) {
  try {
    endCheckOrder(false);
    config=await api("/config");editSnapshot=structuredClone(config);
    editing=config.resources.find(r=>r.id===rid)||null;
    if(rid&&!editing)throw new Error("Resource no longer exists. Refresh the dashboard.");
    $("editor-title").textContent=editing?"Edit resource":"Add resource";
    $("delete-resource").hidden=!editing;
    $("resource-name").value=editing?.name||"";$("check-fields").replaceChildren();$("check-order-status").textContent="";
    if(editing)editing.checks.forEach(addCheckFields);else addCheckFields();
    $("editor-error").textContent="";$("reload-config").hidden=true;
    $("editor").showModal();$("resource-name").focus();
  }catch(error){setStale(error.message);}
}
function readResource() {
  return {id:editing?.id||id("resource"),name:$("resource-name").value,checks:[...$("check-fields").children].map(box=>{
    const value={id:box.dataset.id};
    box.querySelectorAll("input,select").forEach(input=>value[input.name]=input.type==="number"?Number(input.value):input.value);
    return value;
  })};
}
async function saveConfig(candidate,snapshot) {return api("/config",{method:"PUT",headers:{"If-Match":snapshot.revision},body:JSON.stringify({resources:candidate.resources,ui:candidate.ui})});}
function changedMeasurements(a,b) {
  if (a.checks.length !== b.checks.length) return true;
  const previous = new Map(a.checks.map(check => [check.id, check]));
  return b.checks.some(check => {
    const old = previous.get(check.id);
    return !old || [...new Set([...Object.keys(old), ...Object.keys(check)])].some(key => key !== "name" && old[key] !== check[key]);
  });
}
async function deleteResource(rid) {
  $("delete-resource").disabled=true;
  try {
    const snapshot=await api("/config"), resource=snapshot.resources.find(r=>r.id===rid);
    if(!resource)return refresh();
    if(!confirm(`Delete “${resource.name}” and all its checks? All associated history will be discarded. This cannot be undone.`))return;
    await saveConfig({...snapshot,resources:snapshot.resources.filter(r=>r.id!==rid)},snapshot);
    $("editor").close();
    if(detailId()===rid)location.hash="";refresh();
  }catch(error){if($("editor").open)$("editor-error").textContent=error.message;else setStale(error.message);}
  finally{$("delete-resource").disabled=false;}
}
$("editor-form").addEventListener("invalid",event=>{
  const details=event.target.closest(".check-editor");
  if(details)details.open=true;
  if(event.target===$("editor-form").querySelector("input:invalid,select:invalid"))requestAnimationFrame(()=>event.target.focus());
},true);
$("editor-form").addEventListener("submit",async event=>{
  event.preventDefault();if($("save-resource").disabled)return;const resource=readResource();
  if(editing&&changedMeasurements(editing,resource)&&!confirm("Measurement/check changes permanently reset affected check history and this resource’s aggregate history. Save these changes?"))return;
  const candidate=structuredClone(editSnapshot), index=candidate.resources.findIndex(r=>r.id===resource.id);
  if(index<0)candidate.resources.push(resource);else candidate.resources[index]=resource;
  $("save-resource").disabled=true;$("editor-error").textContent="";
  checkDrag?.setDisabled(true);
  for(const id of ["reorder-checks","done-check-order","cancel-check-order"])$(id).disabled=true;
  try{config=await saveConfig(candidate,editSnapshot);$("editor").close();refresh();}
  catch(error){$("editor-error").textContent=error.message;$("reload-config").hidden=![409,412].includes(error.status);}
  finally{$("save-resource").disabled=false;checkDrag?.setDisabled(false);for(const id of ["done-check-order","cancel-check-order"])$(id).disabled=false;updateCheckPositions();}
});
$("reload-config").addEventListener("click",()=>{if(confirm("Reload the saved configuration? Unsaved form changes will be discarded. External file conflicts require reconciliation and a server restart.")){const rid=editing?.id;$("editor").close();openEditor(rid);}});
$("add-check").addEventListener("click",()=>{if($("check-fields").children.length>=20){$("editor-error").textContent="At most 20 checks per resource.";return;}addCheckFields();});
$("add-resource").addEventListener("click",()=>openEditor());
$("delete-resource").addEventListener("click",()=>{if(editing)deleteResource(editing.id);});
$("reorder-resources").addEventListener("click",openResourceOrder);
$("cancel-resource-order").addEventListener("click",()=>{if(!orderSaving){endResourceOrder();refresh();}});
$("reload-resource-order").addEventListener("click",()=>{if(confirm("Reload the saved resource order? Unsaved ordering changes will be discarded. External file conflicts require reconciliation and a server restart."))openResourceOrder();});
$("save-resource-order").addEventListener("click",async()=>{
  if (orderSaving || !orderSnapshot) return;
  resourceDrag?.setDisabled(true);
  const resources = new Map(orderSnapshot.resources.map(resource=>[resource.id,resource]));
  const candidate = {...orderSnapshot,resources:[...$("content").children].map(item=>resources.get(item.dataset.id))};
  orderSaving = true;
  $("resource-order-panel").querySelectorAll("button").forEach(button=>button.disabled=true);
  $("resource-order-error").textContent = "";
  try {
    config = await saveConfig(candidate,orderSnapshot);
    endResourceOrder();
    refresh();
  } catch (error) {
    $("resource-order-error").textContent = error.message;
    $("reload-resource-order").hidden = ![409,412].includes(error.status);
  } finally {
    orderSaving = false;
    $("resource-order-panel").querySelectorAll("button").forEach(button=>button.disabled=false);
    resourceDrag?.setDisabled(false);
  }
});
$("reorder-checks").addEventListener("click",startCheckOrder);
$("done-check-order").addEventListener("click",()=>endCheckOrder(false));
$("cancel-check-order").addEventListener("click",()=>endCheckOrder(true));
$("editor").addEventListener("close",()=>endCheckOrder(false));
[$("close-editor"),$("cancel-editor")].forEach(button=>button.addEventListener("click",()=>$("editor").close()));
$("settings").addEventListener("click",async()=>{try{settingsSnapshot=await api("/config");$("refresh-seconds").value=settingsSnapshot.ui.refresh_interval_seconds;$("settings-error").textContent="";$("settings-dialog").showModal();}catch(error){setStale(error.message);}});
$("close-settings").addEventListener("click",()=>$("settings-dialog").close());
$("settings-form").addEventListener("submit",async event=>{event.preventDefault();const candidate={...settingsSnapshot,ui:{refresh_interval_seconds:Number($("refresh-seconds").value)}};const button=event.submitter;button.disabled=true;try{await saveConfig(candidate,settingsSnapshot);$("settings-dialog").close();refresh();}catch(error){$("settings-error").textContent=`${error.message}${error.status===412?" Close and reopen settings to reload.":""}`;}finally{button.disabled=false;}});
$("login-form").addEventListener("submit",async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;$("login-error").textContent="";try{const data=await api("/session",{method:"POST",body:JSON.stringify({password:$("password").value})});csrf=data.csrf_token;$("password").value="";selected={seconds:3600};rememberRange();$("custom-range").hidden=true;if(!speedPage())location.hash="";showDashboard();}catch(error){$("login-error").textContent=error.message;}finally{button.disabled=false;}});
$("logout").addEventListener("click",async()=>{try{await api("/session",{method:"DELETE",body:"{}"});showLogin();setStale(null);}catch(error){setStale(error.message);}});
$("name-filter").addEventListener("input",render);$("state-filter").addEventListener("change",render);
window.addEventListener("hashchange",()=>{if(orderSnapshot && (detailId()||speedPage()))endResourceOrder();navigate();});
document.addEventListener("visibilitychange",()=>{if(!document.hidden)refresh();else clearTimeout(timer);});
function offset(date) {const n=-date.getTimezoneOffset();return `${n<0?"-":"+"}${String(Math.floor(Math.abs(n)/60)).padStart(2,"0")}:${String(Math.abs(n)%60).padStart(2,"0")}`;}
function localInput(date) {return new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,19);}
$("timezone").textContent=`Local time · ${Intl.DateTimeFormat().resolvedOptions().timeZone}`;
if (location.protocol === "https:") {
  document.querySelector(".security-note").textContent="Encrypted connection. Keep this private monitor restricted to trusted users.";
  document.querySelector("#settings-dialog .warning").textContent="HTTPS encrypts this connection. Passwords remain plaintext in YAML. Restrict network and file access.";
}
document.querySelectorAll("[data-range]").forEach(button=>button.addEventListener("click",()=>{
  const custom=button.dataset.range==="custom";$("custom-range").hidden=!custom;
  if(custom){const end=new Date(selected.to||statusData?.server_time||Date.now()),start=selected.from?new Date(selected.from):new Date(end-3600000);$("range-start").value=localInput(start);$("range-end").value=localInput(end);$("offset-start").value=offset(start);$("offset-end").value=offset(end);}
  else{selected={seconds:Number(button.dataset.range)};rememberRange();refresh();}
}));
function parseLocal(value,zone) {
  if(!/^[+-](?:0\d|1\d|2[0-3]):[0-5]\d$/.test(zone))throw new Error("Use an explicit UTC offset, for example +02:00 or -04:00.");
  const date=new Date(value+zone);const expected=value.length===16?value+":00":value;
  if(!Number.isFinite(date.getTime())||localInput(date)!==expected||offset(date)!==zone)throw new Error("That local time and offset do not exist in your browser timezone. Check the date, DST transition, and offset.");
  return date.toISOString();
}
$("custom-range").addEventListener("submit",event=>{event.preventDefault();try{const from=parseLocal($("range-start").value,$("offset-start").value),to=parseLocal($("range-end").value,$("offset-end").value);if(from>=to||Date.parse(to)>Date.parse(statusData.server_time)||Date.parse(to)-Date.parse(from)>1209600000)throw new Error("Start must precede end, end cannot exceed current server time, and maximum span is 14 days.");selected={from,to};rememberRange();$("range-error").textContent="";refresh();}catch(error){$("range-error").textContent=error.message;}});
SpeedUI.setup({api,node,id,local,csrf:()=>csrf});
document.querySelector(".skip-link").addEventListener("click",event=>{event.preventDefault();(csrf?(speedPage()?$("speedtest"):$("dashboard")):$("password")).focus();});
(async()=>{try{const data=await api("/session");csrf=data.csrf_token;restoreRange();showDashboard();}catch{showLogin();}})();
