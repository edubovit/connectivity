"use strict";

const DragOrder = (() => {
  function insertionIndex(midpoints, y) {
    const index = midpoints.findIndex(midpoint => y < midpoint);
    return index < 0 ? midpoints.length : index;
  }

  function gridInsertionIndex(boxes, x, y) {
    if (!boxes.length) return 0;
    const rows = [];
    boxes.forEach((box, index) => {
      if (!rows.length || Math.abs(rows.at(-1)[0].box.top - box.top) > 1) rows.push([]);
      rows.at(-1).push({box, index});
    });
    const row = rows.find(items => y < Math.max(...items.map(item => item.box.bottom))) || rows.at(-1);
    if (y > row[0].box.bottom && row === rows.at(-1)) return boxes.length;
    return row.find(item => x < (item.box.left + item.box.right) / 2)?.index ?? row.at(-1).index + 1;
  }

  function attach(list, {name, announce, changed = () => {}}) {
    let drag = null, disabled = false, frame = null, moving = false;
    const items = () => [...list.children];
    const scrollArea = list.closest("dialog") || document.scrollingElement;
    const handles = new Map();
    const animations = new Set();
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const icon = () => {
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("focusable", "false");
      for (const x of [9, 15]) for (const y of [6, 12, 18]) {
        const dot = document.createElementNS(svg.namespaceURI, "circle");
        dot.setAttribute("cx", x); dot.setAttribute("cy", y); dot.setAttribute("r", "1.5");
        dot.setAttribute("fill", "currentColor"); dot.setAttribute("stroke", "none");
        svg.append(dot);
      }
      return svg;
    };
    for (const item of items()) {
      const handle = document.createElement("button");
      handle.type = "button";
      handle.className = "drag-handle";
      handle.title = "Drag to move. Keyboard: Space to pick up, arrows to move, Space to drop, Escape to undo.";
      handle.setAttribute("aria-pressed", "false");
      handle.append(icon());
      const legend = item.firstElementChild?.tagName === "LEGEND" ? item.firstElementChild : null;
      item.insertBefore(handle, legend ? legend.nextSibling : item.firstChild);
      item.classList.add("reorder-item");
      handles.set(item, handle);
    }
    list.classList.add("drag-order-list");

    function refresh() {
      items().forEach((item, index) => {
        const handle = handles.get(item);
        handle.disabled = disabled || list.children.length < 2;
        handle.setAttribute("aria-label", `Move ${name(item)}, position ${index + 1} of ${list.children.length}`);
      });
      changed();
    }
    function clearMarker() {
      for (const item of items()) item.classList.remove("drop-before", "drop-after");
    }
    function stopAnimations() {
      for (const animation of animations) {
        animation.onfinish = null;
        animation.cancel();
      }
      animations.clear();
    }
    function positions() {
      const bounds = list.getBoundingClientRect();
      return new Map(items().map(item => {
        const box = item.getBoundingClientRect();
        return [item, {left: box.left - bounds.left, top: box.top - bounds.top}];
      }));
    }
    function reorder(update) {
      const before = !disabled && !reducedMotion.matches ? positions() : null;
      stopAnimations();
      moving = true;
      update();
      moving = false;
      if (!before) return;
      for (const [item, box] of positions()) {
        const previous = before.get(item), x = previous.left - box.left, y = previous.top - box.top;
        if (!x && !y) continue;
        const animation = item.animate([
          {transform: `translate(${x}px, ${y}px)`},
          {transform: "translate(0, 0)"},
        ], {duration: 180, easing: "cubic-bezier(.2,.7,.2,1)"});
        animations.add(animation);
        animation.onfinish = () => animations.delete(animation);
      }
    }
    function motionChanged() { if (reducedMotion.matches) stopAnimations(); }
    function preview() {
      if (!drag || drag.keyboard || !drag.moved) return;
      clearMarker();
      const remaining = items().filter(item => item !== drag.item);
      const boxes = remaining.map(item => item.getBoundingClientRect());
      const style = getComputedStyle(list);
      drag.slot = style.display === "grid" && style.gridTemplateColumns.split(" ").length > 1
        ? gridInsertionIndex(boxes, drag.currentX, drag.y)
        : insertionIndex(boxes.map(box => box.top + box.height / 2), drag.y);
      if (drag.slot === items().indexOf(drag.item)) return;
      const target = remaining[drag.slot] || remaining.at(-1);
      target?.classList.add(drag.slot < remaining.length ? "drop-before" : "drop-after");
    }
    function autoScroll() {
      if (!drag || drag.keyboard) return;
      if (drag.moved) {
        const bounds = scrollArea === document.scrollingElement ? {top: 0, bottom: innerHeight} : scrollArea.getBoundingClientRect();
        const top = Math.max(0, bounds.top), bottom = Math.min(innerHeight, bounds.bottom);
        const speed = drag.y < top + 56 ? -Math.min(18, (top + 56 - drag.y) / 3) : drag.y > bottom - 56 ? Math.min(18, (drag.y - bottom + 56) / 3) : 0;
        if (speed) scrollArea.scrollTop += speed;
        preview();
      }
      frame = requestAnimationFrame(autoScroll);
    }
    function finish(cancel = false, refocus = true) {
      if (!drag) return;
      const state = drag;
      drag = null;
      cancelAnimationFrame(frame);
      frame = null;
      clearMarker();
      state.item.classList.remove("dragging");
      state.handle.setAttribute("aria-pressed", "false");
      if (state.pointer !== undefined && state.handle.hasPointerCapture(state.pointer)) state.handle.releasePointerCapture(state.pointer);
      if (cancel) {
        stopAnimations();
        if (state.keyboard) state.original.forEach(item => list.append(item));
      }
      else if (!cancel && !state.keyboard && state.moved) {
        const remaining = items().filter(item => item !== state.item);
        if (state.slot !== items().indexOf(state.item)) reorder(() => list.insertBefore(state.item, remaining[state.slot] || null));
      }
      refresh();
      if (refocus) state.handle.focus({preventScroll: true});
      announce(cancel ? "Move canceled." : `${name(state.item)} is at position ${items().indexOf(state.item) + 1} of ${list.children.length}. Save to apply.`);
    }
    function pointerDown(event) {
      const handle = event.target.closest(".drag-handle");
      if (!handle || !list.contains(handle) || disabled || handle.disabled || event.button !== 0 || event.isPrimary === false) return;
      if (drag) finish(true);
      stopAnimations();
      event.preventDefault();
      handle.focus({preventScroll: true});
      const item = handle.parentElement;
      drag = {item, handle, pointer: event.pointerId, x: event.clientX, currentX: event.clientX, startY: event.clientY, y: event.clientY, moved: false, slot: items().indexOf(item)};
      handle.setPointerCapture(event.pointerId);
      frame = requestAnimationFrame(autoScroll);
    }
    function pointerMove(event) {
      if (!drag || drag.keyboard || drag.pointer !== event.pointerId) return;
      drag.y = event.clientY;
      drag.currentX = event.clientX;
      if (!drag.moved && Math.hypot(event.clientX - drag.x, event.clientY - drag.startY) >= 5) {
        drag.moved = true;
        drag.item.classList.add("dragging");
        drag.handle.setAttribute("aria-pressed", "true");
        announce(`Moving ${name(drag.item)}. Release at the insertion marker; Escape cancels.`);
      }
      preview();
    }
    function pointerUp(event) { if (drag?.pointer === event.pointerId) finish(); }
    function pointerCancel(event) { if (drag?.pointer === event.pointerId) finish(true); }
    function keyDown(event) {
      const handle = event.target.closest(".drag-handle");
      if (!handle || !list.contains(handle) || disabled || handle.disabled) return;
      if (event.key === "Escape" && drag) {
        event.preventDefault(); event.stopPropagation(); finish(true); return;
      }
      if (![" ", "Enter", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      if (drag && !drag.keyboard) return;
      const item = handle.parentElement;
      if (event.key === " " || event.key === "Enter") {
        if (drag) finish();
        else {
          drag = {item, handle, keyboard: true, original: items()};
          item.classList.add("dragging"); handle.setAttribute("aria-pressed", "true");
          announce(`Picked up ${name(item)}. Use arrows or Home/End, Space to drop, Escape to undo.`);
        }
        return;
      }
      if (drag && drag.item !== item) finish();
      const current = items().indexOf(item), remaining = items().filter(other => other !== item);
      const index = event.key === "Home" ? 0 : event.key === "End" ? remaining.length : Math.max(0, Math.min(remaining.length, current + (["ArrowUp","ArrowLeft"].includes(event.key) ? -1 : 1)));
      if (index !== current) reorder(() => {
        list.insertBefore(item, remaining[index] || null);
        refresh(); handle.focus({preventScroll: true}); item.scrollIntoView({block: "nearest", behavior: "instant"});
      });
      announce(`${name(item)} moved to position ${index + 1} of ${list.children.length}. ${drag ? "Space to drop; Escape to undo." : "Save to apply."}`);
    }
    function focusOut(event) {
      if (!moving && drag?.keyboard && !drag.handle.contains(event.relatedTarget)) finish(false, false);
    }
    function blur() { finish(true, false); }
    const listeners = {pointerdown: pointerDown, pointermove: pointerMove, pointerup: pointerUp, pointercancel: pointerCancel, lostpointercapture: pointerCancel, keydown: keyDown, focusout: focusOut};
    for (const [type, listener] of Object.entries(listeners)) list.addEventListener(type, listener);
    window.addEventListener("blur", blur);
    reducedMotion.addEventListener("change", motionChanged);
    refresh();
    return {
      refresh,
      setDisabled(value) { disabled = value; finish(); if (disabled) stopAnimations(); refresh(); },
      destroy() {
        finish(true, false);
        stopAnimations();
        for (const [type, listener] of Object.entries(listeners)) list.removeEventListener(type, listener);
        window.removeEventListener("blur", blur);
        reducedMotion.removeEventListener("change", motionChanged);
        for (const [item, handle] of handles) { handle.remove(); item.classList.remove("reorder-item"); }
        list.classList.remove("drag-order-list");
      },
    };
  }
  return {attach, insertionIndex, gridInsertionIndex};
})();

if (typeof module !== "undefined" && module.exports) module.exports = DragOrder;
