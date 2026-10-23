"use strict";

const LatencyCharts = (() => {
  const WIDTH = 1000, HEIGHT = 180;
  const number = value => new Intl.NumberFormat(undefined, {maximumSignificantDigits: 5}).format(value);
  const milliseconds = value => Number.isFinite(value) ? `${number(value)} ms` : "—";
  const hasSample = point => point.latency_samples > 0 && [point.latency_min_ms, point.latency_mean_ms, point.latency_max_ms].every(Number.isFinite);

  function timeTicks(range) {
    const start = Date.parse(range.from), end = Date.parse(range.to), span = end - start;
    const dates = span >= 86400000 || new Date(start).toDateString() !== new Date(end).toDateString();
    const years = new Date(start).getFullYear() !== new Date(end).getFullYear();
    return [start, (start + end) / 2, end].map(timestamp => {
      const date = new Date(timestamp);
      return {
        at: date.toISOString(),
        date: dates ? date.toLocaleDateString(undefined, {month: "short", day: "numeric", ...(years ? {year: "numeric"} : {})}) : "",
        time: date.toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit", hourCycle: "h23",
          ...(span < 60000 ? {second: "2-digit"} : {}), ...(span < 1000 ? {fractionalSecondDigits: 3} : {})}),
        full: date.toLocaleString(undefined, {year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
          ...(span < 1000 ? {fractionalSecondDigits: 3} : {}), timeZoneName: "shortOffset"}),
      };
    });
  }

  function scale(maximum) {
    const target = (maximum > 0 ? maximum * 1.1 : 1) / 4;
    const magnitude = 10 ** Math.floor(Math.log10(target));
    const step = [1, 2, 2.5, 5, 10].find(value => value * magnitude >= target) * magnitude;
    return {maximum: step * 4, ticks: [4, 3, 2, 1, 0].map(value => value * step)};
  }

  function geometry(points, maximum) {
    let mean = "", envelope = "", dots = "", failures = "", errors = "";
    let previous = false;
    points.forEach((point, index) => {
      const x = (index + .5) / points.length * WIDTH;
      if (point.failure) failures += `M${x} 0v8`;
      if (point.error) errors += `M${x} 8v8`;
      if (!hasSample(point)) { previous = false; return; }
      const y = value => HEIGHT * (1 - value / maximum);
      mean += `${previous ? "L" : "M"}${x} ${y(point.latency_mean_ms)}`;
      dots += `M${x} ${y(point.latency_mean_ms)}h.001`;
      envelope += `M${x} ${y(point.latency_max_ms)}V${y(point.latency_min_ms)}`;
      previous = true;
    });
    return {mean, envelope, dots, failures, errors};
  }

  function pickPoint(points, fraction, vertical, width, height, maximum, events = false) {
    const fallback = Math.max(0, Math.min(points.length - 1, Math.floor(fraction * points.length)));
    if (!points.length || width <= 0) return fallback;
    const radius = 7, span = Math.ceil(radius / width * points.length);
    let selected = fallback, distance = radius * radius;
    for (let index = Math.max(0, fallback - span); index <= Math.min(points.length - 1, fallback + span); index++) {
      const point = points[index];
      if (events ? !(vertical < .5 ? point.failure : point.error) : !hasSample(point)) continue;
      const dx = ((index + .5) / points.length - fraction) * width;
      const dy = events ? 0 : (1 - point.latency_mean_ms / maximum - vertical) * height;
      const candidate = dx * dx + dy * dy;
      if (candidate < distance) { distance = candidate; selected = index; }
    }
    return selected;
  }

  function describe(point, series) {
    const start = new Date(point.from), end = new Date(point.to), width = end - start;
    const date = value => value.toLocaleDateString(undefined, {month: "short", day: "numeric", ...(start.getFullYear() !== end.getFullYear() ? {year: "numeric"} : {})});
    const time = value => value.toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
      ...(width < 1000 ? {fractionalSecondDigits: 3} : {}), ...(start.getTimezoneOffset() !== end.getTimezoneOffset() ? {timeZoneName: "shortOffset"} : {})});
    const full = value => value.toLocaleString(undefined, {year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
      ...(width < 1000 ? {fractionalSecondDigits: 3} : {}), timeZoneName: "shortOffset"});
    const summary = series.effective_summary_range;
    const summarized = summary && Date.parse(point.from) < Date.parse(summary.to) && Date.parse(point.to) > Date.parse(summary.from);
    const samples = point.latency_samples;
    const counts = [[point.failure, "failure"], [point.error, "monitor error"]].filter(([count]) => count > 0).map(([count, label]) => `${count} ${label}${count === 1 ? "" : "s"}`);
    if (point.success > samples) counts.unshift(`${point.success - samples} OK without latency`);
    return {
      window: `${date(start)} · ${time(start)} – ${start.toDateString() === end.toDateString() ? "" : date(end) + " · "}${time(end)}`,
      fullWindow: `${full(start)} – ${full(end)}`,
      samples: `${samples} ${samples === 1 ? "sample" : "samples"}`,
      mean: hasSample(point) ? milliseconds(point.latency_mean_ms) : "—",
      min: hasSample(point) ? milliseconds(point.latency_min_ms) : "—",
      max: hasSample(point) ? milliseconds(point.latency_max_ms) : "—",
      counts: counts.join(" · "),
      precision: summarized ? "Minute summaries" : "",
      approximate: point.latency_approximate ? "ICMP upper bounds" : "",
      missing: hasSample(point) ? "" : point.success + point.failure + point.error ? "No latency samples" : "No observations",
    };
  }

  function create(check, initialSeries) {
    const element = (tag, className, text) => {
      const result = document.createElement(tag);
      if (className) result.className = className;
      if (text !== undefined) result.textContent = text;
      return result;
    };
    const svgElement = (tag, attributes) => {
      const result = document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [key, value] of Object.entries(attributes)) result.setAttribute(key, value);
      return result;
    };
    const path = className => svgElement("path", {class: className, fill: "none", "vector-effect": "non-scaling-stroke"});
    const chart = element("section", "chart latency-chart");
    chart.dataset.check = check.id;
    const heading = element("div", "chart-title");
    const legend = element("div", "latency-legend");
    for (const [kind, label] of [["mean", "● Mean"], ["envelope", "↕ Min–max"], ["failure", "│ Failures"], ["error", "│ Monitor errors"]]) {
      legend.append(element("span", `latency-key-${kind}`, label));
    }
    const instructions = element("p", "latency-instructions", "Hover, tap, or use ← → to inspect. Gaps mean no latency samples.");
    instructions.id = `latency-help-${check.id}`;
    const axes = element("div", "latency-axes");
    const unit = element("div", "latency-unit", "Latency (ms)");
    const yLabels = element("div", "latency-y-labels");
    const plot = element("div", "latency-plot");
    plot.tabIndex = 0;
    plot.setAttribute("role", "slider");
    plot.setAttribute("aria-orientation", "horizontal");
    plot.setAttribute("aria-valuemin", "0");
    plot.setAttribute("aria-describedby", instructions.id);
    const svg = svgElement("svg", {viewBox: `0 0 ${WIDTH} ${HEIGHT}`, preserveAspectRatio: "none", "aria-hidden": "true"});
    const grid = path("latency-grid");
    grid.setAttribute("d", [0, .25, .5, .75, 1].map(fraction => `M0 ${fraction * HEIGHT}H${WIDTH}`).join("") + [0, .5, 1].map(fraction => `M${fraction * WIDTH} 0V${HEIGHT}`).join(""));
    const envelope = path("latency-envelope"), mean = path("latency-mean"), dots = path("latency-dots");
    const cursor = svgElement("g", {class: "latency-cursor", visibility: "hidden"});
    const cursorLine = path("latency-crosshair"), selectedDot = path("latency-selected-dot");
    cursor.append(cursorLine, selectedDot);
    svg.append(grid, envelope, mean, dots, cursor);
    const empty = element("div", "latency-empty", "No successful latency measurements in this range");
    const tooltip = element("div", "latency-tooltip");
    tooltip.id = `latency-tooltip-${check.id}`;
    tooltip.setAttribute("role", "tooltip");
    tooltip.hidden = true;
    const windowLabel = element("div", "latency-tooltip-window");
    const reading = element("div", "latency-tooltip-reading");
    const meanLabel = element("strong", "latency-tooltip-mean");
    const sampleLabel = element("span", "latency-tooltip-samples");
    reading.append(meanLabel, sampleLabel);
    const stats = element("dl", "latency-tooltip-stats");
    const values = {};
    for (const key of ["min", "max"]) {
      const group = element("div");
      values[key] = element("dd");
      group.append(element("dt", "", key === "min" ? "Min" : "Max"), values[key]);
      stats.append(group);
    }
    const counts = element("div", "latency-tooltip-counts"), precision = element("div", "latency-tooltip-note");
    tooltip.append(windowLabel, reading, stats, counts, precision);
    plot.append(svg, empty);
    const eventRail = svgElement("svg", {class: "latency-event-rail", viewBox: `0 0 ${WIDTH} 16`, preserveAspectRatio: "none", "aria-hidden": "true"});
    const failures = path("latency-failures"), errors = path("latency-errors");
    eventRail.append(failures, errors);
    const xLabels = element("div", "latency-x-labels");
    const timeLabel = element("div", "latency-time-label", `Time · ${Intl.DateTimeFormat().resolvedOptions().timeZone}`);
    axes.append(unit, yLabels, plot, eventRail, xLabels, timeLabel, tooltip);
    chart.append(heading, legend, axes, instructions);

    let series, points = [], axis, active = 0, mode = null, fraction = .5, vertical = .5, onEventRail = false, selectedTime = null;

    function hide() {
      mode = null;
      tooltip.hidden = true;
      cursor.setAttribute("visibility", "hidden");
      plot.setAttribute("aria-describedby", instructions.id);
    }

    function announce(details) {
      plot.setAttribute("aria-valuenow", String(active));
      plot.setAttribute("aria-valuetext", `${details.fullWindow}. ${details.missing || `${details.samples}. Mean ${details.mean}, minimum ${details.min}, maximum ${details.max}`}. ${[details.counts, details.approximate, details.precision].filter(Boolean).join(". ")}`);
    }

    function show(index, preserveTime = false) {
      if (!points.length) return;
      active = Math.max(0, Math.min(points.length - 1, index));
      const point = points[active], details = describe(point, series);
      if (!preserveTime) selectedTime = (Date.parse(point.from) + Date.parse(point.to)) / 2;
      const x = (active + .5) / points.length * WIDTH;
      cursorLine.setAttribute("d", `M${x} 0V${HEIGHT}`);
      selectedDot.setAttribute("d", hasSample(point) ? `M${x} ${HEIGHT * (1 - point.latency_mean_ms / axis.maximum)}h.001` : "");
      cursor.setAttribute("visibility", "visible");
      windowLabel.textContent = details.window;
      windowLabel.title = details.fullWindow;
      meanLabel.textContent = details.mean;
      meanLabel.hidden = Boolean(details.missing);
      sampleLabel.textContent = details.missing || `Mean · ${details.samples}`;
      stats.hidden = Boolean(details.missing);
      for (const key of ["min", "max"]) values[key].textContent = details[key];
      counts.textContent = details.counts;
      counts.hidden = !details.counts;
      precision.textContent = [details.precision, details.approximate].filter(Boolean).join(" · ");
      precision.hidden = !precision.textContent;
      tooltip.classList.toggle("latency-tooltip-right", active < points.length / 2);
      tooltip.hidden = false;
      announce(details);
      plot.setAttribute("aria-describedby", `${instructions.id} ${tooltip.id}`);
    }

    function pointAtPointer(event) {
      const box = event.currentTarget.getBoundingClientRect();
      fraction = Math.max(0, Math.min(1, (event.clientX - box.left) / box.width));
      vertical = (event.clientY - box.top) / box.height;
      onEventRail = event.currentTarget === eventRail;
      mode = event.pointerType === "touch" ? "touch" : "pointer";
      const index = pickPoint(points, fraction, vertical, box.width, box.height, axis.maximum, onEventRail);
      if (index !== active || tooltip.hidden) show(index);
    }

    for (const surface of [plot, eventRail]) {
      surface.addEventListener("pointermove", pointAtPointer);
      surface.addEventListener("pointerdown", event => {
        pointAtPointer(event);
        plot.focus({preventScroll: true});
      });
      surface.addEventListener("pointerleave", () => { if (mode === "pointer") hide(); });
      surface.addEventListener("pointercancel", hide);
    }
    plot.addEventListener("focus", () => {
      if (!mode) { mode = "keyboard"; show(active); }
    });
    plot.addEventListener("blur", hide);
    chart.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); hide(); return; }
      const directions = {ArrowLeft: -1, ArrowRight: 1, ArrowDown: -1, ArrowUp: 1, PageDown: -10, PageUp: 10};
      let index = active;
      if (event.key === "Home") index = 0;
      else if (event.key === "End") index = points.length - 1;
      else if (Object.hasOwn(directions, event.key)) index += directions[event.key];
      else return;
      event.preventDefault();
      mode = "keyboard";
      show(index);
    });

    chart.update = (currentCheck, currentSeries) => {
      series = currentSeries;
      points = series.timeline;
      const valid = points.filter(hasSample);
      axis = scale(Math.max(0, ...valid.map(point => point.latency_max_ms)));
      const paths = geometry(points, axis.maximum);
      heading.textContent = `Latency${series.latency.approximate ? " · includes approximate upper bounds" : ""}`;
      plot.setAttribute("aria-label", `${currentCheck.name}: ${currentCheck.metric}, milliseconds over time`);
      plot.setAttribute("aria-valuemax", String(Math.max(0, points.length - 1)));
      if (!mode) {
        active = Math.max(0, points.findIndex(hasSample));
        if (points[active]) announce(describe(points[active], series));
      }
      for (const [line, value] of [[mean, paths.mean], [envelope, paths.envelope], [dots, paths.dots], [failures, paths.failures], [errors, paths.errors]]) line.setAttribute("d", value);
      empty.hidden = valid.length > 0;
      yLabels.replaceChildren(...axis.ticks.map(tick => element("span", "", valid.length ? number(tick) : "—")));
      xLabels.replaceChildren(...timeTicks(series.requested_range).map(tick => {
        const label = element("time");
        label.dateTime = tick.at;
        label.title = tick.full;
        label.setAttribute("aria-label", tick.full);
        if (tick.date) label.append(element("span", "tick-date", tick.date));
        label.append(element("span", "", tick.time));
        return label;
      }));
      if (mode === "pointer") {
        const box = (onEventRail ? eventRail : plot).getBoundingClientRect();
        show(pickPoint(points, fraction, vertical, box.width, box.height, axis.maximum, onEventRail));
      }
      else if (mode) {
        const index = points.findIndex(point => Date.parse(point.from) <= selectedTime && Date.parse(point.to) > selectedTime);
        if (index >= 0) show(index, true); else hide();
      }
    };
    chart.update(check, initialSeries);
    return chart;
  }

  return {create, scale, geometry, describe, pickPoint, timeTicks};
})();

if (typeof module !== "undefined" && module.exports) module.exports = LatencyCharts;
