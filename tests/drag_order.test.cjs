"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {runInNewContext} = require("node:vm");
const {insertionIndex, gridInsertionIndex} = require("../connectivity/static/drag-order.js");
const source = readFileSync(require.resolve("../connectivity/static/drag-order.js"), "utf8");

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

function fixture({reduced = false, grid = false, count = 3} = {}) {
  class Events {
    listeners = new Map();
    addEventListener(type, listener) { this.listeners.set(type, listener); }
    removeEventListener(type, listener) { if (this.listeners.get(type) === listener) this.listeners.delete(type); }
    fire(type, values = {}) {
      const event = {preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }, ...values};
      this.listeners.get(type)?.(event);
      return event;
    }
  }
  const document = {scrollingElement: {scrollTop: 0}, activeElement: null};
  const animations = [], messages = [], frames = new Map();
  let nextFrame = 0, changes = 0;
  class Element extends Events {
    constructor(tagName) {
      super();
      this.tagName = tagName.toUpperCase();
      this.children = [];
      this.parentElement = null;
      this.attributes = new Map();
      this.classes = new Set();
      this.offset = {x: 0, y: 0};
      this.captured = new Set();
      this.classList = {
        add: (...names) => names.forEach(name => this.classes.add(name)),
        remove: (...names) => names.forEach(name => this.classes.delete(name)),
        contains: name => this.classes.has(name),
      };
    }
    set className(value) { this.classes = new Set(value.split(" ")); }
    get firstChild() { return this.children[0] || null; }
    get firstElementChild() { return this.firstChild; }
    get nextSibling() { return this.parentElement.children[this.parentElement.children.indexOf(this) + 1] || null; }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.get(name); }
    closest(selector) {
      if (selector === "dialog") return null;
      return this.classList.contains(selector.slice(1)) ? this : this.parentElement?.closest(selector);
    }
    contains(element) { return element === this || this.children.some(child => child.contains(element)); }
    insertBefore(element, next) {
      element.remove();
      this.children.splice(next ? this.children.indexOf(next) : this.children.length, 0, element);
      element.parentElement = this;
    }
    append(element) { this.insertBefore(element, null); }
    remove() {
      if (!this.parentElement) return;
      if (this.contains(document.activeElement)) {
        list.fire("focusout", {target: document.activeElement, relatedTarget: null});
        document.activeElement = null;
      }
      this.parentElement.children.splice(this.parentElement.children.indexOf(this), 1);
      this.parentElement = null;
    }
    focus() { document.activeElement = this; }
    scrollIntoView(options) { this.scrollOptions = options; this.onScroll?.(); }
    setPointerCapture(id) { this.captured.add(id); }
    hasPointerCapture(id) { return this.captured.has(id); }
    releasePointerCapture(id) {
      this.captured.delete(id);
      list.fire("lostpointercapture", {pointerId: id});
    }
    getBoundingClientRect() {
      const index = list.children.indexOf(this);
      const top = 100 - document.scrollingElement.scrollTop + (index < 0 ? 0 : grid ? Math.floor(index / 2) * 76 : index * 76) + this.offset.y;
      const left = (grid && index >= 0 ? index % 2 * 216 : 0) + this.offset.x;
      return {top, bottom: top + 64, left, right: left + 200, height: 64};
    }
    animate(keyframes, options) {
      const animation = {
        item: this, keyframes, options, canceled: false,
        cancel: () => { animation.canceled = true; this.offset = {x: 0, y: 0}; },
        finish: () => { this.offset = {x: 0, y: 0}; animation.onfinish?.(); },
      };
      animations.push(animation);
      return animation;
    }
  }
  document.createElement = tag => new Element(tag);
  document.createElementNS = (namespaceURI, tag) => Object.assign(new Element(tag), {namespaceURI});
  const window = new Events(), motion = Object.assign(new Events(), {matches: reduced});
  window.matchMedia = query => { assert.equal(query, "(prefers-reduced-motion: reduce)"); return motion; };
  const list = new Element("div");
  const originals = Array.from({length: count}, (_, index) => {
    const item = new Element("fieldset"), input = new Element("input");
    input.value = `Draft ${index + 1}`;
    item.id = String.fromCharCode(65 + index);
    item.append(new Element("legend")); item.append(input); list.append(item);
    return item;
  });
  const DragOrder = runInNewContext(`${source}\nDragOrder`, {
    document, window, innerHeight: 600,
    getComputedStyle: () => ({display: grid ? "grid" : "block", gridTemplateColumns: grid ? "200px 200px" : "none"}),
    requestAnimationFrame: callback => { frames.set(++nextFrame, callback); return nextFrame; },
    cancelAnimationFrame: id => frames.delete(id),
  });
  const controller = DragOrder.attach(list, {name: item => item.id, announce: text => messages.push(text), changed: () => changes++});
  const handles = originals.map(item => item.children.find(child => child.classList.contains("drag-handle")));
  return {
    list, originals, handles, document, window, motion, animations, messages, frames, controller,
    get changes() { return changes; },
    order: () => list.children.map(item => item.id),
    key: (key, index = 0) => list.fire("keydown", {key, target: handles[index]}),
    pointer: (type, x, y, index = 0, id = 1) => list.fire(type, {target: handles[index], pointerId: id, button: 0, clientX: x, clientY: y}),
    tick() {
      const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(callback => callback());
    },
  };
}

test("six-dot grips preserve legends, field nodes, and accessible position labels", () => {
  const f = fixture();
  for (const [index, handle] of f.handles.entries()) {
    assert.equal(f.originals[index].firstElementChild.tagName, "LEGEND");
    assert.equal(f.originals[index].children[1], handle);
    assert.equal(handle.getAttribute("aria-label"), `Move ${f.originals[index].id}, position ${index + 1} of 3`);
    assert.equal(handle.firstChild.getAttribute("aria-hidden"), "true");
    assert.equal(handle.firstChild.getAttribute("focusable"), "false");
    assert.equal(handle.firstChild.children.length, 6);
    assert.ok(handle.firstChild.children.every(dot => dot.tagName === "CIRCLE" && dot.getAttribute("fill") === "currentColor"));
  }
  f.controller.destroy();
  assert.ok(f.originals.every(item => item.children.length === 2 && item.children[1].value.startsWith("Draft")));
  assert.equal(f.list.classList.contains("drag-order-list"), false);
});

test("keyboard moves commit immediately and animate only displaced nodes", () => {
  const f = fixture(), input = f.originals[0].children[2];
  f.key("ArrowDown");
  assert.deepEqual(f.order(), ["B", "A", "C"]);
  assert.equal(f.list.children[1], f.originals[0]);
  assert.equal(input.value, "Draft 1");
  assert.equal(f.document.activeElement, f.handles[0]);
  assert.equal(f.handles[0].getAttribute("aria-label"), "Move A, position 2 of 3");
  assert.equal(f.changes, 2);
  assert.equal(f.animations.length, 2);
  assert.equal(f.animations.find(animation => animation.item.id === "A").keyframes[0].transform, "translate(0px, -76px)");
  assert.equal(f.animations.find(animation => animation.item.id === "B").keyframes[0].transform, "translate(0px, 76px)");
  assert.ok(f.animations.every(animation => animation.options.duration === 180 && animation.keyframes[1].transform === "translate(0, 0)"));
  f.animations.forEach(animation => animation.finish());
  f.controller.destroy();
  assert.ok(f.animations.every(animation => !animation.canceled));
});

test("keyboard pickup survives focus loss during DOM moves and Escape restores original nodes", () => {
  const f = fixture();
  f.handles[0].focus();
  f.key(" "); f.key("End"); f.key("ArrowUp");
  assert.deepEqual(f.order(), ["B", "A", "C"]);
  assert.equal(f.handles[0].getAttribute("aria-pressed"), "true");
  assert.ok(f.originals[0].classList.contains("dragging"));
  const event = f.key("Escape");
  assert.equal(event.stopped, true);
  assert.deepEqual(f.list.children, f.originals);
  assert.equal(f.handles[0].getAttribute("aria-pressed"), "false");
  assert.equal(f.document.activeElement, f.handles[0]);
  assert.ok(f.animations.every(animation => animation.canceled));
  assert.equal(f.messages.at(-1), "Move canceled.");
});

test("pointer preview leaves saved-array order intact until release", () => {
  const f = fixture();
  f.pointer("pointerdown", 12, 120);
  assert.ok(f.handles[0].hasPointerCapture(1));
  f.pointer("pointermove", 14, 122);
  assert.equal(f.originals[0].classList.contains("dragging"), false);
  f.pointer("pointermove", 12, 150);
  assert.equal(f.originals[0].classList.contains("dragging"), true);
  assert.ok(f.originals.every(item => !item.classList.contains("drop-before") && !item.classList.contains("drop-after")));
  f.pointer("pointermove", 12, 350);
  assert.ok(f.originals[2].classList.contains("drop-after"));
  assert.deepEqual(f.order(), ["A", "B", "C"]);
  assert.equal(f.animations.length, 0);
  f.pointer("pointerup", 12, 350, 0, 99);
  assert.deepEqual(f.order(), ["A", "B", "C"]);
  f.pointer("pointerup", 12, 350);
  assert.deepEqual(f.order(), ["B", "C", "A"]);
  assert.equal(f.animations.length, 3);
  assert.equal(f.handles[0].hasPointerCapture(1), false);
  assert.equal(f.frames.size, 0);
  assert.equal(f.document.activeElement, f.handles[0]);
  assert.ok(f.originals.every(item => !item.classList.contains("dragging") && !item.classList.contains("drop-after")));
});

for (const cancel of ["Escape", "pointercancel", "lostpointercapture", "blur", "destroy"]) {
  test(`${cancel} clears pointer preview, capture, and auto-scroll without changing order`, () => {
    const f = fixture();
    f.pointer("pointerdown", 12, 120); f.pointer("pointermove", 12, 590); f.tick();
    assert.ok(f.document.scrollingElement.scrollTop > 0);
    if (cancel === "Escape") f.key(cancel);
    else if (cancel === "blur") f.window.fire("blur");
    else if (cancel === "destroy") f.controller.destroy();
    else f.pointer(cancel, 12, 590);
    assert.deepEqual(f.order(), ["A", "B", "C"]);
    assert.equal(f.frames.size, 0);
    assert.equal(f.animations.length, 0);
    assert.equal(f.handles[0].hasPointerCapture(1), false);
    assert.ok(f.originals.every(item => !item.classList.contains("dragging") && !item.classList.contains("drop-after")));
  });
}

test("grid moves animate both axes without adding list children", () => {
  const f = fixture({grid: true});
  f.key("End");
  assert.deepEqual(f.order(), ["B", "C", "A"]);
  assert.equal(f.animations.find(animation => animation.item.id === "A").keyframes[0].transform, "translate(0px, -76px)");
  assert.equal(f.animations.find(animation => animation.item.id === "B").keyframes[0].transform, "translate(216px, 0px)");
  assert.equal(f.animations.find(animation => animation.item.id === "C").keyframes[0].transform, "translate(-216px, 76px)");
  assert.equal(f.list.children.length, 3);
});

test("repeated keyboard moves start at the current visual position", () => {
  const f = fixture();
  f.key("ArrowDown");
  f.originals[0].offset.y = -30;
  const previous = [...f.animations];
  f.key("ArrowDown");
  assert.ok(previous.every(animation => animation.canceled));
  assert.equal(f.animations.at(-1).item, f.originals[0]);
  assert.equal(f.animations.at(-1).keyframes[0].transform, "translate(0px, -106px)");
});

test("scrolling to a keyboard destination does not animate unrelated rows", () => {
  const f = fixture();
  f.originals[0].onScroll = () => { f.document.scrollingElement.scrollTop = 76; };
  f.key("ArrowDown");
  assert.equal(f.animations.length, 2);
  assert.equal(f.animations.find(animation => animation.item.id === "A").keyframes[0].transform, "translate(0px, -76px)");
  assert.equal(f.originals[0].scrollOptions.behavior, "instant");
});

test("reduced motion skips animation and cancels in-flight motion when changed", () => {
  const f = fixture({reduced: true});
  f.key("ArrowDown");
  assert.deepEqual(f.order(), ["B", "A", "C"]);
  assert.equal(f.animations.length, 0);
  f.motion.matches = false; f.motion.fire("change"); f.key("ArrowDown");
  assert.equal(f.animations.length, 2);
  f.motion.matches = true; f.motion.fire("change");
  assert.ok(f.animations.every(animation => animation.canceled));
  f.key("Home");
  assert.equal(f.animations.length, 2);
  assert.deepEqual(f.order(), ["A", "B", "C"]);
  f.controller.destroy();
  assert.equal(f.motion.listeners.size, 0);
});

test("no-op moves, pointer clicks, and disabled single-item handles do not animate", () => {
  const f = fixture();
  f.key("Home"); f.key("ArrowUp");
  f.pointer("pointerdown", 12, 120); f.pointer("pointerup", 12, 120);
  assert.deepEqual(f.order(), ["A", "B", "C"]);
  assert.equal(f.animations.length, 0);
  const single = fixture({count: 1});
  assert.equal(single.handles[0].disabled, true);
  single.key(" "); single.key("End"); single.pointer("pointerdown", 12, 120);
  assert.equal(single.handles[0].getAttribute("aria-pressed"), "false");
  assert.equal(single.frames.size, 0);
});

test("disabling commits a pointer drop without animation and blocks further input", () => {
  const f = fixture();
  f.pointer("pointerdown", 12, 120); f.pointer("pointermove", 12, 350);
  f.controller.setDisabled(true);
  assert.deepEqual(f.order(), ["B", "C", "A"]);
  assert.equal(f.animations.length, 0);
  assert.equal(f.frames.size, 0);
  assert.ok(f.handles.every(handle => handle.disabled));
  f.key("Home"); f.pointer("pointerdown", 12, 120);
  assert.deepEqual(f.order(), ["B", "C", "A"]);
  assert.equal(f.frames.size, 0);
  f.controller.setDisabled(false); f.key("Home");
  assert.deepEqual(f.order(), ["A", "B", "C"]);
  assert.equal(f.animations.length, 3);
  f.controller.setDisabled(true);
  assert.ok(f.animations.every(animation => animation.canceled));
});

test("destroy rolls back a keyboard pickup and removes animation and event handlers", () => {
  const f = fixture();
  f.key(" "); f.key("End");
  f.controller.destroy();
  assert.deepEqual(f.order(), ["A", "B", "C"]);
  assert.ok(f.animations.every(animation => animation.canceled));
  assert.equal(f.list.listeners.size, 0);
  assert.equal(f.window.listeners.size, 0);
  assert.equal(f.motion.listeners.size, 0);
  assert.ok(f.originals.every(item => !item.classList.contains("reorder-item")));
});

test("keyboard drop and focus departure keep the committed order", () => {
  for (const drop of ["Enter", "focusout", "disable"]) {
    const f = fixture();
    f.key(" "); f.key("ArrowDown");
    if (drop === "focusout") f.list.fire(drop, {relatedTarget: null});
    else if (drop === "disable") f.controller.setDisabled(true);
    else f.key(drop);
    assert.deepEqual(f.order(), ["B", "A", "C"]);
    assert.equal(f.handles[0].getAttribute("aria-pressed"), "false");
    f.controller.destroy();
    assert.deepEqual(f.order(), ["B", "A", "C"]);
  }
});
