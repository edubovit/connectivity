"use strict";

const SelectControl = (() => {
  const controls = new WeakMap();
  let opened = null, serial = 0;

  function nextEnabled(options, index, direction) {
    for (let i = index + direction; i >= 0 && i < options.length; i += direction) {
      if (!options[i].disabled) return i;
    }
    return index;
  }

  function matchingOption(options, text, after = -1) {
    for (let offset = 1; offset <= options.length; offset++) {
      const index = (after + offset) % options.length;
      if (!options[index].disabled && options[index].text.toLocaleLowerCase().startsWith(text.toLocaleLowerCase())) return index;
    }
    return -1;
  }

  function close() {
    if (!opened) return;
    if (opened.menu.matches(":popover-open")) opened.menu.hidePopover();
    opened.trigger.setAttribute("aria-expanded", "false");
    opened.trigger.removeAttribute("aria-activedescendant");
    opened = null;
  }

  function create(select) {
    const wrapper = document.createElement("div");
    wrapper.className = "select-control";
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "select-trigger";
    trigger.id = `select-trigger-${++serial}`;
    trigger.setAttribute("role", "combobox");
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    const labels = [...select.labels];
    const name = select.getAttribute("aria-label") || labels.map(label => {
      const copy = label.cloneNode(true);
      copy.querySelectorAll("input,select,button,.select-control").forEach(e => e.remove());
      return copy.textContent.trim();
    }).join(" ");
    trigger.setAttribute("aria-label", name);
    trigger.innerHTML = '<span class="select-value"></span><svg viewBox="0 0 12 12" aria-hidden="true"><path d="m2 4 4 4 4-4"/></svg>';
    const value = trigger.firstElementChild;
    const menu = document.createElement("div");
    menu.className = "select-menu";
    menu.id = `select-menu-${serial}`;
    menu.popover = "manual";
    menu.setAttribute("role", "listbox");
    menu.setAttribute("aria-label", name);
    trigger.setAttribute("aria-controls", menu.id);
    let options = [], rows = [], signature = "", active = -1, search = "", typedAt = 0;

    function update() {
      const current = [...select.options];
      const next = JSON.stringify(current.map(option => [option.value, option.text, option.disabled]));
      options = current;
      if (next !== signature) {
        if (opened?.trigger === trigger) close();
        signature = next;
        rows = options.map((option, index) => {
          const row = document.createElement("div");
          row.className = "select-option";
          row.id = `${menu.id}-${index}`;
          row.setAttribute("role", "option");
          row.setAttribute("aria-disabled", String(option.disabled));
          const text = document.createElement("span"), check = document.createElement("span");
          text.textContent = option.text;
          check.className = "option-check";
          check.setAttribute("aria-hidden", "true");
          row.append(text, check);
          row.addEventListener("pointermove", () => {if (!option.disabled) highlight(index);});
          row.addEventListener("pointerdown", event => event.preventDefault());
          row.addEventListener("click", () => choose(index));
          return row;
        });
        menu.replaceChildren(...rows);
      }
      const text = select.selectedOptions[0]?.text || "No options";
      if (value.textContent !== text) value.textContent = text;
      const disabled = select.matches(":disabled") || !options.some(option => !option.disabled);
      if (trigger.disabled !== disabled) trigger.disabled = disabled;
      trigger.title = select.title;
      trigger.setAttribute("aria-required", String(select.required));
      if (select.validity.valid) trigger.removeAttribute("aria-invalid");
      if (trigger.disabled && opened?.trigger === trigger) close();
      rows.forEach((row, index) => {
        const selected = index === select.selectedIndex;
        row.setAttribute("aria-selected", String(selected));
        const mark = selected ? "✓" : "";
        if (row.lastElementChild.textContent !== mark) row.lastElementChild.textContent = mark;
      });
    }

    function highlight(index, scroll = false) {
      if (index < 0 || index >= rows.length) return;
      active = index;
      rows.forEach((row, i) => row.classList.toggle("is-active", i === active));
      trigger.setAttribute("aria-activedescendant", rows[active].id);
      if (scroll) rows[active].scrollIntoView({block:"nearest"});
    }

    function show() {
      close();
      update();
      if (trigger.disabled) return;
      const box = trigger.getBoundingClientRect();
      const width = Math.min(innerWidth - 16, Math.max(box.width, 160));
      const below = innerHeight - box.bottom - 12, above = box.top - 12;
      const up = below < Math.min(280, options.length * 40 + 12) && above > below;
      menu.style.width = `${width}px`;
      menu.style.maxHeight = `${Math.min(320, Math.max(80, up ? above : below))}px`;
      menu.style.left = `${Math.max(8, Math.min(box.left, innerWidth - width - 8))}px`;
      menu.style.top = `${box.bottom + 5}px`;
      menu.showPopover();
      if (up) menu.style.top = `${Math.max(8, box.top - menu.getBoundingClientRect().height - 5)}px`;
      opened = {trigger, menu, wrapper, top:box.top, left:box.left};
      trigger.setAttribute("aria-expanded", "true");
      const selected = select.selectedIndex;
      highlight(selected >= 0 && !options[selected].disabled ? selected : nextEnabled(options, -1, 1), true);
    }

    function choose(index) {
      if (select.matches(":disabled") || !options[index] || options[index].disabled) return;
      const changed = select.selectedIndex !== index;
      select.selectedIndex = index;
      update();
      close();
      trigger.focus({preventScroll:true});
      if (changed) {
        select.dispatchEvent(new Event("input", {bubbles:true}));
        select.dispatchEvent(new Event("change", {bubbles:true}));
      }
    }

    trigger.addEventListener("click", () => opened?.trigger === trigger ? close() : show());
    trigger.addEventListener("keydown", event => {
      const isOpen = opened?.trigger === trigger;
      if (event.key === "Escape" && isOpen) {event.preventDefault();event.stopPropagation();close();return;}
      if (event.key === "Tab") {close();return;}
      if (event.key === "Enter" || event.key === " ") {event.preventDefault();isOpen ? choose(active) : show();return;}
      if (["ArrowDown","ArrowUp","Home","End"].includes(event.key)) {
        event.preventDefault();
        if (!isOpen) {show();return;}
        const index = event.key === "Home" ? nextEnabled(options, -1, 1) : event.key === "End" ? nextEnabled(options, options.length, -1) : nextEnabled(options, active, event.key === "ArrowDown" ? 1 : -1);
        highlight(index, true);
        return;
      }
      if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
        event.preventDefault();
        const now = Date.now(), key = event.key.toLocaleLowerCase();
        search = now - typedAt < 700 ? search + key : key;
        typedAt = now;
        const repeated = [...search].every(char => char === key);
        const index = matchingOption(options, repeated ? key : search, repeated ? active : -1);
        if (index >= 0) {if (!isOpen) show();highlight(index, true);}
      }
    });
    select.hidden = true;
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    wrapper.append(trigger, menu);
    select.after(wrapper);
    select.addEventListener("change", update);
    select.addEventListener("invalid", event => {
      event.preventDefault();
      const details = select.closest("details");
      if (details) details.open = true;
      trigger.setAttribute("aria-invalid", "true");
      trigger.title = select.validationMessage;
      requestAnimationFrame(() => trigger.focus());
    });
    labels.forEach(label => label.addEventListener("click", event => {if (event.target === label) trigger.focus();}));
    update();
    return {update, trigger};
  }

  function sync(select) {
    if (!select?.isConnected || !("showPopover" in HTMLElement.prototype)) return;
    if (!controls.has(select)) controls.set(select, create(select));
    else controls.get(select).update();
  }
  function focus(element) { (controls.get(element)?.trigger || element).focus(); }
  function scan(root) {
    if (root.matches?.("select")) sync(root);
    root.querySelectorAll?.("select").forEach(sync);
  }
  if (typeof document !== "undefined") {
    scan(document);
    new MutationObserver(records => {
      if (opened && !opened.trigger.isConnected) close();
      for (const record of records) {
        const select = record.target.closest?.("select");
        if (select) sync(select);
        if (record.type === "childList") record.addedNodes.forEach(scan);
      }
    }).observe(document.body, {childList:true, subtree:true, attributes:true, attributeFilter:["disabled","selected","label","title","required"]});
    document.addEventListener("pointerdown", event => {if (opened && !opened.wrapper.contains(event.target)) close();});
    document.addEventListener("focusin", event => {if (opened && !opened.wrapper.contains(event.target)) close();});
    document.addEventListener("scroll", event => {
      if (!opened || opened.menu.contains(event.target)) return;
      const box = opened.trigger.getBoundingClientRect();
      if (Math.abs(box.top - opened.top) > .5 || Math.abs(box.left - opened.left) > .5) close();
    }, true);
    document.addEventListener("close", close, true);
    document.addEventListener("visibilitychange", () => {if (document.hidden) close();});
    window.addEventListener("resize", close);
    window.addEventListener("hashchange", close);
  }
  return {sync, focus, close, nextEnabled, matchingOption};
})();

if (typeof module !== "undefined" && module.exports) module.exports = SelectControl;
