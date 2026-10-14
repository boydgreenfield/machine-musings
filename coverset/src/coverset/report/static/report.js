/* coverset report: behaviour of the static pages (no inline scripts, works from file://).
   - theme toggle (auto / light / dark) and the view settings (highlight mode, inlay chips, map mode
     and module weight), applied before first paint: ?highlight= / ?mode= / ?modules= in the page's
     URL win, else the reader's own choice from localStorage, else the default (the live pages put
     the scoped settings between the two; a static page has none)
   - the line-tests popover, built from the page's JSON data block (#report-data)
   - the data-flow lens: dim the lines the selected test did not run (#lens=<test hash> preselects);
     its options are built from the data block when the menu is first used
   - shape grids under def lines (key i) and the grid cell -> input-class row link
   - treemap Gap / Lines toggle and module weight (inert <template> variants swapped in), the status
     line above it (a tile's data-info on hover and focus), sortable tables, search over files /
     functions / tests, filter boxes (input[data-filter])
   - keys, from one table (KEYS) that mirrors app.js's: the help dialog lists the same keys and
     shows the rows that apply to the page (tests/web/test_keys_table.py checks both scripts)
   - document.body.dataset.ready = "1" once all of the above is initialised (screenshots)
   Every listener is delegated from document: no per-line or per-tile handler. */
(function () {
  "use strict";

  var THEME_KEY = "coverset-theme";
  var THEMES = ["auto", "light", "dark"];

  function storedTheme() {
    try {
      var value = window.localStorage.getItem(THEME_KEY);
      return THEMES.indexOf(value) >= 0 ? value : "auto";
    } catch (e) {
      return "auto";
    }
  }

  function applyTheme(theme) {
    var root = document.documentElement;
    if (theme === "auto") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    var button = document.getElementById("theme-toggle");
    if (button) {
      button.textContent = "Theme: " + theme;
      button.setAttribute("aria-label", "Colour theme: " + theme + " (click to change)");
    }
  }

  applyTheme(storedTheme());

  var HIGHLIGHT_KEY = "coverset-highlight";
  var HIGHLIGHTS = ["all", "missing"];
  var CHIPS_KEY = "coverset-chips";
  var MAP_KEY = "coverset-map";
  var MAP_MODES = ["gap", "lines"];
  var WEIGHTS = ["full", "thin", "hidden"];
  var html = document.documentElement;

  function load(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }

  function save(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (e) {
      /* storage blocked (or file:// without it): the choice still applies to this page */
    }
  }

  function param(name) {
    var match = new RegExp("[?&]" + name + "=([^&#]*)").exec(window.location.search || "");
    return match ? decodeURIComponent(match[1]) : null;
  }

  function savedMap() {
    try {
      return JSON.parse(load(MAP_KEY) || "{}") || {};
    } catch (e) {
      return {};
    }
  }

  function pick(value, stored, choices, fallback) {
    if (choices.indexOf(value) >= 0) return value;
    return choices.indexOf(stored) >= 0 ? stored : fallback;
  }

  var view = {
    highlight: pick(param("highlight"), load(HIGHLIGHT_KEY), HIGHLIGHTS, "all"),
    mode: pick(param("mode"), savedMap().mode, MAP_MODES, "gap"),
    modules: pick(param("modules"), savedMap().modules, WEIGHTS, "thin")
  };
  html.setAttribute("data-highlight", view.highlight);
  if (load(CHIPS_KEY) === "off") html.setAttribute("data-chips", "off");

  var data = null;
  var lineIndex = {};
  var importLines = {};
  var root = "";

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function readData() {
    var block = document.getElementById("report-data");
    if (!block) return;
    try {
      data = JSON.parse(block.textContent || "null");
    } catch (e) {
      data = null;
    }
    if (!data || !data.tests) return;
    data.tests.forEach(function (test, index) {
      (test[3] || []).forEach(function (line) {
        (lineIndex[line] = lineIndex[line] || []).push(index);
      });
    });
    (data.import || []).forEach(function (line) {
      importLines[line] = true;
    });
  }

  function testHref(hash) {
    return root + "tests/" + hash + ".html";
  }

  // -- line tests popover ---------------------------------------------------------------------
  function fillLineTests(lineNumber) {
    var body = document.getElementById("line-tests-body");
    if (!body) return;
    body.replaceChildren();
    var ids = lineIndex[lineNumber] || [];
    var where = (data && data.path ? data.path : "") + ":" + lineNumber;
    var head = el("h3", null, ids.length + (ids.length === 1 ? " test ran " : " tests ran "));
    head.appendChild(el("code", null, where));
    body.appendChild(head);
    if (ids.length) {
      var list = el("ul", "test-list");
      ids.forEach(function (index) {
        var test = data.tests[index];
        var item = el("li");
        var outcome = test[1] || "not run";
        item.appendChild(el("span", "oc oc-" + outcome.replace(" ", "_"), outcome));
        var link = el("a", "nodeid", test[0]);
        link.href = testHref(test[2]);
        item.appendChild(link);
        list.appendChild(item);
      });
      body.appendChild(list);
    }
    if (importLines[lineNumber]) body.appendChild(el("p", "muted", "Also executed at import time."));
    if (!ids.length && !importLines[lineNumber]) body.appendChild(el("p", "empty", "No test executed this line."));
  }

  function placePopover(anchor) {
    var popover = document.getElementById("line-tests");
    if (!popover) return;
    var rect = anchor.getBoundingClientRect();
    var width = Math.min(560, window.innerWidth * 0.92);
    var left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
    var below = window.innerHeight - rect.bottom;
    var above = rect.top;
    popover.style.position = "fixed";
    popover.style.left = left + "px";
    if (below < 280 && above > below) {
      popover.style.top = "auto";
      popover.style.bottom = window.innerHeight - rect.top + 6 + "px";
      popover.style.maxHeight = Math.max(120, above - 18) + "px";
    } else {
      popover.style.bottom = "auto";
      popover.style.top = rect.bottom + 6 + "px";
      popover.style.maxHeight = Math.max(120, below - 18) + "px";
    }
  }

  function showLineTests(line, anchor) {
    var popover = document.getElementById("line-tests");
    if (!popover) return;
    fillLineTests(line.getAttribute("data-line"));
    placePopover(anchor || line.querySelector(".gn") || line);
    if (popover.showPopover && !popover.matches(":popover-open")) popover.showPopover();
  }

  // -- the data-flow lens -----------------------------------------------------------------------
  var lineNodes = null;

  function lineMap() {
    // data-line -> its block, and statement line -> its continuation lines (built once)
    if (lineNodes) return lineNodes;
    lineNodes = { line: {}, cont: {} };
    Array.prototype.forEach.call(document.querySelectorAll(".src-lines .ln[data-line]"), function (line) {
      lineNodes.line[line.getAttribute("data-line")] = line;
      var first = line.getAttribute("data-first");
      if (first) (lineNodes.cont[first] = lineNodes.cont[first] || []).push(line);
    });
    return lineNodes;
  }

  function fillLens(select) {
    // the menu's options, from the data block, the first time it is used
    if (!select || select.getAttribute("data-filled") || !data || !data.tests) return;
    var fragment = document.createDocumentFragment();
    data.tests.forEach(function (test, index) {
      var n = (test[3] || []).length;
      var option = document.createElement("option");
      option.value = String(index);
      option.textContent = test[0] + " · " + (test[1] || "not run") + " · " + n + (n === 1 ? " line" : " lines");
      fragment.appendChild(option);
    });
    select.appendChild(fragment);
    select.setAttribute("data-filled", "1");
  }

  function applyLens(index) {
    var shell = document.querySelector(".src-lines");
    var select = document.getElementById("lens");
    var note = document.getElementById("lens-note");
    var bar = document.querySelector(".lens-bar");
    if (!shell || !data) return;
    Array.prototype.forEach.call(shell.querySelectorAll(".ln.lens-hit"), function (line) {
      line.classList.remove("lens-hit");
    });
    if (index === "" || index === null || index === undefined || !data.tests[index]) {
      shell.classList.remove("lens-on");
      if (bar) bar.classList.remove("is-active");
      if (note) note.textContent = "";
      if (select) select.value = "";
      return;
    }
    var test = data.tests[index];
    var map = lineMap();
    var hits = 0;
    (test[3] || []).forEach(function (n) {
      var line = map.line[n];
      if (line) {
        line.classList.add("lens-hit");
        hits += 1;
      }
      // the continuation lines of a multi-line statement follow its first line
      (map.cont[n] || []).forEach(function (cont) {
        cont.classList.add("lens-hit");
      });
    });
    shell.classList.add("lens-on");
    if (bar) bar.classList.add("is-active");
    if (select) {
      fillLens(select);
      select.value = String(index);
    }
    if (note) note.textContent = test[0] + " ran " + hits + (hits === 1 ? " line" : " lines") + " here; the rest is dimmed.";
  }

  function lensFromHash() {
    var match = /(?:^#|&)lens=([0-9a-f]{16})/.exec(window.location.hash || "");
    if (!match || !data || !data.tests) return;
    for (var i = 0; i < data.tests.length; i++) {
      if (data.tests[i][2] === match[1]) {
        applyLens(i);
        return;
      }
    }
  }

  // -- shape grids ------------------------------------------------------------------------------
  function fillGrid(details) {
    var body = details.querySelector(".grid-body");
    if (!body || body.getAttribute("data-filled")) return;
    var region = details.closest(".fn-region");
    var key = region ? region.getAttribute("data-fn") : null;
    var templates = document.querySelectorAll("template.grid-tpl");
    for (var i = 0; i < templates.length; i++) {
      if (templates[i].getAttribute("data-fn") === key) {
        body.replaceChildren(templates[i].content.cloneNode(true));
        body.setAttribute("data-filled", "1");
        return;
      }
    }
    body.textContent = "No shape grid for this function.";
  }

  function showCell(cell) {
    var block = cell.closest(".shape-grid-block");
    if (!block) return;
    var cells = Array.prototype.slice.call(block.querySelectorAll(".sg-cell"));
    var index = cells.indexOf(cell);
    var scope = block.closest(".grid-body") || document;
    var table = scope.querySelector(".class-table") || document.querySelector("#classes .class-table");
    if (!table || index < 0) return;
    var row = table.querySelector('tr[data-cell-index="' + index + '"]');
    if (!row) return;
    Array.prototype.forEach.call(table.querySelectorAll("tr.is-flash"), function (r) {
      r.classList.remove("is-flash");
    });
    row.classList.add("is-flash");
    row.scrollIntoView({ block: "nearest", behavior: "auto" });
  }

  // -- source navigation (j/k n/N t i [ ]) ---------------------------------------------------------
  function lines() {
    return Array.prototype.slice.call(document.querySelectorAll(".src-lines .ln[data-line]"));
  }

  var current = null;

  function setCurrent(line, scroll) {
    if (!line) return;
    if (current) current.classList.remove("is-current");
    current = line;
    line.classList.add("is-current");
    if (scroll) {
      line.scrollIntoView({ block: "center", behavior: "auto" });
      if (line.id && window.history && window.history.replaceState) {
        try {
          window.history.replaceState(null, "", "#" + line.id);
        } catch (e) {
          /* file:// may refuse; the highlight is enough */
        }
      }
    }
  }

  function currentIndex(all) {
    if (current && all.indexOf(current) >= 0) return all.indexOf(current);
    if (window.location.hash) {
      var target = document.getElementById(window.location.hash.slice(1));
      if (target && all.indexOf(target) >= 0) return all.indexOf(target);
    }
    // the first line whose top is below the viewport top (a binary search: lines are in order)
    var lo = 0;
    var hi = all.length;
    while (lo < hi) {
      var mid = (lo + hi) >> 1;
      if (all[mid].getBoundingClientRect().top >= 80) hi = mid;
      else lo = mid + 1;
    }
    return lo < all.length ? lo - 1 : -1;
  }

  function step(selector, direction) {
    var all = lines();
    if (!all.length) return;
    var index = currentIndex(all);
    for (var i = index + direction; i >= 0 && i < all.length; i += direction) {
      if (all[i].matches(selector)) {
        setCurrent(all[i], true);
        return;
      }
    }
  }

  function stepFunction(direction) {
    var all = lines();
    var heads = Array.prototype.slice.call(
      document.querySelectorAll('.fn-region:not([data-hollow="0"]) > .ln.fn-head')
    );
    if (!heads.length) return;
    var index = currentIndex(all);
    var position = index >= 0 ? all[index] : null;
    var candidates = heads.filter(function (head) {
      if (!position) return direction > 0;
      var order = position.compareDocumentPosition(head);
      return direction > 0
        ? (order & Node.DOCUMENT_POSITION_FOLLOWING) !== 0
        : (order & Node.DOCUMENT_POSITION_PRECEDING) !== 0 && head !== position;
    });
    var next = direction > 0 ? candidates[0] : candidates[candidates.length - 1];
    if (next) setCurrent(next, true);
  }

  function statementLine(line) {
    // a continuation line of a multi-line statement answers for its first line
    var first = line && line.getAttribute("data-first");
    var shell = first && line.closest(".src-lines");
    return (shell && shell.querySelector('.ln[data-line="' + first + '"]')) || line;
  }

  function currentLine() {
    var all = lines();
    var index = currentIndex(all);
    return index >= 0 ? all[index] : all[0];
  }

  function toggleTests() {
    var popover = document.getElementById("line-tests");
    if (popover && popover.matches && popover.matches(":popover-open")) {
      popover.hidePopover();
      return;
    }
    var line = statementLine(currentLine());
    if (line) showLineTests(line, line.querySelector("button.gt") || line.querySelector(".gn"));
  }

  function toggleGrid() {
    var line = currentLine();
    var region = line ? line.closest(".fn-region") : null;
    if (!region || !region.querySelector(":scope > .inlay details.fn-grid")) {
      region = document.querySelector(".fn-region .inlay details.fn-grid");
      region = region ? region.closest(".fn-region") : null;
    }
    if (!region) return;
    var details = region.querySelector(":scope > .inlay details.fn-grid");
    if (details) {
      if (!details.open) fillGrid(details);
      details.open = !details.open;
    }
  }

  function follow(rel) {
    var link = document.querySelector('a[rel="' + rel + '"][data-key-nav]');
    if (link) window.location.assign(link.href);
  }

  // -- search -----------------------------------------------------------------------------------
  var searchItems = null;
  var selected = -1;

  function searchResults(query) {
    var items = searchItems || (searchItems = window.COVERSET_SEARCH || []);
    var words = query.toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return [];
    var found = [];
    for (var i = 0; i < items.length && found.length < 40; i++) {
      var hay = (items[i][1] + " " + items[i][2]).toLowerCase();
      var ok = true;
      for (var w = 0; w < words.length; w++) {
        if (hay.indexOf(words[w]) < 0) {
          ok = false;
          break;
        }
      }
      if (ok) found.push(items[i]);
    }
    return found;
  }

  function renderSearch() {
    var input = document.getElementById("search");
    var list = document.getElementById("search-results");
    if (!input || !list) return;
    var found = searchResults(input.value);
    list.replaceChildren();
    selected = -1;
    if (!input.value.trim()) {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      return;
    }
    if (!found.length) list.appendChild(el("li", "none", "Nothing matches."));
    found.forEach(function (item) {
      var li = el("li");
      li.setAttribute("role", "option");
      var a = el("a");
      a.href = root + item[3];
      a.appendChild(el("span", "kind", item[0]));
      a.appendChild(el("span", "label", item[1]));
      if (item[2]) a.appendChild(el("span", "sub", item[2]));
      li.appendChild(a);
      list.appendChild(li);
    });
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  function moveSelection(delta) {
    var list = document.getElementById("search-results");
    if (!list) return;
    var options = list.querySelectorAll('li[role="option"]');
    if (!options.length) return;
    if (selected >= 0 && options[selected]) options[selected].removeAttribute("aria-selected");
    selected = (selected + delta + options.length) % options.length;
    options[selected].setAttribute("aria-selected", "true");
    options[selected].scrollIntoView({ block: "nearest" });
  }

  // -- tables -------------------------------------------------------------------------------------
  function sortTable(button) {
    var th = button.closest("th");
    var table = th.closest("table");
    var body = table.tBodies[0];
    if (!body) return;
    var column = Array.prototype.indexOf.call(th.parentNode.children, th);
    var numeric = button.getAttribute("data-sort") === "num";
    var ascending = th.getAttribute("aria-sort") !== "ascending";
    Array.prototype.forEach.call(th.parentNode.children, function (cell) {
      cell.removeAttribute("aria-sort");
    });
    th.setAttribute("aria-sort", ascending ? "ascending" : "descending");
    var rows = Array.prototype.slice.call(body.rows);
    rows.sort(function (a, b) {
      var x = a.cells[column] ? a.cells[column].getAttribute("data-value") || "" : "";
      var y = b.cells[column] ? b.cells[column].getAttribute("data-value") || "" : "";
      var result = numeric ? parseFloat(x) - parseFloat(y) : x.localeCompare(y);
      return ascending ? result : -result;
    });
    rows.forEach(function (row) {
      body.appendChild(row);
    });
  }

  // -- the map: colour mode (m) and module weight (M) -------------------------------------------
  function showMap(mode, modules) {
    if (MAP_MODES.indexOf(mode) < 0) mode = "gap";
    if (!document.querySelector('[data-map-modules="' + modules + '"]')) modules = "thin";
    // swap in the module-weight variant: the drawn maps go back to inert templates, so each
    // variant is parsed once and never kept live twice
    Array.prototype.forEach.call(document.querySelectorAll("[data-map-view]"), function (box) {
      var which = box.getAttribute("data-map-view");
      var shown = box.getAttribute("data-modules") || "thin";
      if (shown !== modules) {
        var want = document.querySelector('template[data-map-variant="' + which + ":" + modules + '"]');
        if (want) {
          var stash = document.querySelector('template[data-map-variant="' + which + ":" + shown + '"]');
          if (!stash) {
            stash = document.createElement("template");
            stash.className = "map-variant";
            stash.setAttribute("data-map-variant", which + ":" + shown);
            while (box.firstChild) stash.content.appendChild(box.firstChild);
            box.parentNode.appendChild(stash);
          }
          box.replaceChildren(want.content.cloneNode(true));
          box.setAttribute("data-modules", modules);
        }
      }
      box.hidden = which !== mode;
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-map]"), function (button) {
      button.setAttribute("aria-pressed", button.getAttribute("data-map") === mode ? "true" : "false");
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-map-modules]"), function (button) {
      button.setAttribute("aria-pressed", button.getAttribute("data-map-modules") === modules ? "true" : "false");
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-map-legend]"), function (node) {
      var weight = node.getAttribute("data-map-weight");
      node.hidden = node.getAttribute("data-map-legend") !== mode || (weight !== null && weight !== modules);
    });
    view.mode = mode;
    view.modules = modules;
  }

  function chooseMap(mode, modules) {
    showMap(mode, modules);
    save(MAP_KEY, JSON.stringify({ mode: view.mode, modules: view.modules }));
  }

  function cycleMap(attribute) {
    var buttons = Array.prototype.slice.call(document.querySelectorAll("button[" + attribute + "]"));
    if (!buttons.length) return;
    var at = 0;
    buttons.forEach(function (button, i) {
      if (button.getAttribute("aria-pressed") === "true") at = i;
    });
    var next = buttons[(at + 1) % buttons.length].getAttribute(attribute);
    if (attribute === "data-map") chooseMap(next, view.modules);
    else chooseMap(view.mode, next);
  }

  // -- highlight mode (h) and inlay chips (.) ------------------------------------------------------
  function withParam(href, name, value) {
    // set one query parameter of a relative or absolute link, keeping it as it was otherwise
    var hash = "";
    var at = href.indexOf("#");
    if (at >= 0) {
      hash = href.slice(at);
      href = href.slice(0, at);
    }
    var q = href.indexOf("?");
    var base = q >= 0 ? href.slice(0, q) : href;
    var parts = (q >= 0 ? href.slice(q + 1) : "").split("&").filter(function (part) {
      return part && part.split("=")[0] !== name;
    });
    parts.push(name + "=" + encodeURIComponent(value));
    return base + "?" + parts.join("&") + hash;
  }

  function keepView(value) {
    // the links this page builds carry the mode, so the next page paints it too
    Array.prototype.forEach.call(document.querySelectorAll("a[data-keep-view]"), function (link) {
      link.setAttribute("href", withParam(link.getAttribute("href"), "highlight", value));
    });
  }

  function setHighlight(value) {
    view.highlight = value;
    html.setAttribute("data-highlight", value);
    Array.prototype.forEach.call(document.querySelectorAll("[data-highlight-toggle]"), function (button) {
      button.setAttribute("aria-pressed", value === "missing" ? "true" : "false");
    });
    keepView(value);
    if (window.history && window.history.replaceState) {
      try {
        window.history.replaceState(null, "", withParam(window.location.href, "highlight", value));
      } catch (e) {
        /* file:// may refuse; the links carry it */
      }
    }
  }

  function toggleHighlight() {
    var next = view.highlight === "missing" ? "all" : "missing";
    save(HIGHLIGHT_KEY, next);
    setHighlight(next);
  }

  function toggleChips() {
    var off = html.getAttribute("data-chips") !== "off";
    if (off) html.setAttribute("data-chips", "off");
    else html.removeAttribute("data-chips");
    save(CHIPS_KEY, off ? "off" : "on");
  }

  // -- go to (g then a letter), filter boxes --------------------------------------------------------
  var chord = null;

  function goTarget(letter) {
    var nodes = document.querySelectorAll('[data-go="' + letter + '"]');
    for (var i = 0; i < nodes.length; i++) {
      var link = nodes[i].matches("a[href]") ? nodes[i] : nodes[i].querySelector("a[href]");
      if (link) return link;
    }
    return null;
  }

  function go(letter) {
    var link = goTarget(letter);
    if (link) window.location.assign(link.href);
  }

  function chordHint(show) {
    var hint = document.getElementById("key-chord");
    if (!show) {
      if (hint) hint.hidden = true;
      return;
    }
    if (!hint) {
      hint = el("div", "key-chord");
      hint.id = "key-chord";
      hint.setAttribute("role", "status");
      document.body.appendChild(hint);
    }
    var names = [];
    Array.prototype.forEach.call(document.querySelectorAll("[data-go]"), function (node) {
      var letter = node.getAttribute("data-go");
      if (goTarget(letter) && names.indexOf(letter) < 0) names.push(letter);
    });
    hint.textContent = "g … " + names.join(" ");
    hint.hidden = false;
  }

  function endChord() {
    if (chord !== null) window.clearTimeout(chord);
    chord = null;
    chordHint(false);
  }

  function filterList(input) {
    var words = (input.value || "").toLowerCase().split(/\s+/).filter(Boolean);
    var items = document.querySelectorAll(input.getAttribute("data-filter"));
    var shown = 0;
    Array.prototype.forEach.call(items, function (item) {
      var text = (item.getAttribute("data-filter-text") || item.textContent || "").toLowerCase();
      var hit = words.every(function (word) {
        return text.indexOf(word) >= 0;
      });
      item.hidden = !hit;
      if (hit) shown += 1;
    });
    var count = input.getAttribute("data-filter-count");
    var note = count ? document.querySelector(count) : null;
    if (note) note.textContent = words.length ? shown + " of " + items.length : "";
  }

  function focusSearch() {
    var box = document.getElementById("search") || document.querySelector("input[data-filter]");
    if (!box) return false;
    box.focus();
    if (box.select) box.select();
    return true;
  }

  function copyText(button) {
    var source = document.querySelector(button.getAttribute("data-copy-target"));
    if (!source) return;
    var text = source.textContent || "";
    function done() {
      button.setAttribute("data-copied", "");
      button.textContent = "Copied";
      window.setTimeout(function () {
        button.removeAttribute("data-copied");
        button.textContent = "Copy";
      }, 1600);
    }
    function fallback() {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.className = "sr";
      document.body.appendChild(area);
      area.select();
      try {
        if (document.execCommand("copy")) done();
      } catch (e) {
        /* nothing more to try */
      }
      area.remove();
    }
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback);
    else fallback();
  }

  // -- the keys -------------------------------------------------------------------------------------
  function typing(event) {
    var target = event.target;
    if (!target || !target.closest) return false;
    return !!target.closest("input, textarea, select, [contenteditable], dialog[open]") &&
      !(target.matches && target.matches("dialog"));
  }

  // scope: where a key applies; the help dialog's rows carry the same data-scope (app.js has the
  // same table for the live pages)
  var SCOPES = {
    any: function () { return true; },
    source: function () { return !!document.querySelector(".src-lines"); },
    files: function () { return !!document.querySelector("a[data-key-nav]"); },
    map: function () { return !!document.querySelector("button[data-map]"); },
    modules: function () { return !!document.querySelector("button[data-map-modules]"); },
    search: function () { return !!(document.getElementById("search") || document.querySelector("input[data-filter]")); },
    go: function () { return !!document.querySelector("[data-go]"); }
  };

  function openHelp() {
    var dialog = document.getElementById("keys-help");
    if (!dialog || !dialog.showModal) return false;
    Array.prototype.forEach.call(dialog.querySelectorAll("[data-scope]"), function (row) {
      var test = SCOPES[row.getAttribute("data-scope")];
      var letter = row.getAttribute("data-go-row");
      row.hidden = !(test ? test() : true) || (letter ? !goTarget(letter) : false);
    });
    Array.prototype.forEach.call(dialog.querySelectorAll("table"), function (table) {
      var empty = !table.querySelector("tr:not([hidden])");
      table.hidden = empty;
      var head = table.previousElementSibling;
      if (head && head.tagName === "H3") head.hidden = empty;
    });
    dialog.showModal();
    return true;
  }

  function dismiss() {
    endChord();
    var popover = document.getElementById("line-tests");
    if (popover && popover.matches && popover.matches(":popover-open")) popover.hidePopover();
    var results = document.getElementById("search-results");
    if (results) results.hidden = true;
    return false; // Escape keeps its native work too (closing a dialog)
  }

  var KEYS = {
    "?": { scope: "any", run: openHelp },
    "Escape": { scope: "any", run: dismiss },
    "/": { scope: "search", run: focusSearch },
    "g": { scope: "go", run: function () { chord = window.setTimeout(endChord, 1500); chordHint(true); } },
    "j": { scope: "source", run: function () { step(".ln.mis:not(.cont), .ln.par:not(.cont)", 1); } },
    "k": { scope: "source", run: function () { step(".ln.mis:not(.cont), .ln.par:not(.cont)", -1); } },
    "n": { scope: "source", run: function () { stepFunction(1); } },
    "N": { scope: "source", run: function () { stepFunction(-1); } },
    "t": { scope: "source", run: toggleTests },
    "i": { scope: "source", run: toggleGrid },
    "h": { scope: "source", run: toggleHighlight },
    ".": { scope: "source", run: toggleChips },
    "[": { scope: "files", run: function () { follow("prev"); } },
    "]": { scope: "files", run: function () { follow("next"); } },
    "m": { scope: "map", run: function () { cycleMap("data-map"); } },
    "M": { scope: "modules", run: function () { cycleMap("data-map-modules"); } }
  };
  // after "g": overview, tests, map (the report has no economy or invariants pages)
  var GO_KEYS = ["o", "t", "m"];

  document.addEventListener("keydown", function (event) {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
    var key = event.key;
    var target = event.target;
    if (target && target.id === "search") {
      if (key === "ArrowDown") { moveSelection(1); event.preventDefault(); }
      else if (key === "ArrowUp") { moveSelection(-1); event.preventDefault(); }
      else if (key === "Enter") {
        var list = document.getElementById("search-results");
        var pick = list && (list.querySelector('li[aria-selected="true"] a') || list.querySelector("li a"));
        if (pick) { window.location.assign(pick.href); event.preventDefault(); }
      } else if (key === "Escape") {
        target.value = "";
        renderSearch();
        target.blur();
      }
      return;
    }
    if (target && target.matches && target.matches("input[data-filter]")) {
      if (key === "Escape") {
        target.value = "";
        filterList(target);
        target.blur();
      } else if (key === "Enter") {
        var first = Array.prototype.find.call(document.querySelectorAll(target.getAttribute("data-filter")), function (item) {
          return !item.hidden;
        });
        var link = first && (first.matches("a[href]") ? first : first.querySelector("a[href]"));
        if (link) {
          event.preventDefault();
          window.location.assign(link.href);
        }
      }
      return;
    }
    if (typing(event)) return;
    if ((key === "Enter" || key === " ") && target && target.matches && target.matches('[role="button"].sg-cell')) {
      event.preventDefault();
      showCell(target);
      return;
    }
    if (chord !== null) {
      endChord();
      if (GO_KEYS.indexOf(key) >= 0) {
        event.preventDefault();
        go(key);
        return;
      }
    }
    var binding = Object.prototype.hasOwnProperty.call(KEYS, key) ? KEYS[key] : null;
    if (!binding || !SCOPES[binding.scope]()) return;
    if (binding.run() !== false) event.preventDefault();
  });

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || !target.closest) return;
    if (target.closest("#theme-toggle")) {
      var next = THEMES[(THEMES.indexOf(storedTheme()) + 1) % THEMES.length];
      save(THEME_KEY, next);
      applyTheme(next);
      return;
    }
    if (target.closest("[data-highlight-toggle]")) { toggleHighlight(); return; }
    var copy = target.closest("button.copy[data-copy-target]");
    if (copy) { copyText(copy); return; }
    var sorter = target.closest("table.sortable th button[data-sort]");
    if (sorter) { sortTable(sorter); return; }
    var map = target.closest("button[data-map]");
    if (map) { chooseMap(map.getAttribute("data-map"), view.modules); return; }
    var weight = target.closest("button[data-map-modules]");
    if (weight) { chooseMap(view.mode, weight.getAttribute("data-map-modules")); return; }
    var cell = target.closest(".sg-cell");
    if (cell) { showCell(cell); return; }
    var gutter = target.closest("button.gt");
    if (gutter) {
      var line = gutter.closest(".ln");
      if (line) {
        setCurrent(line, false);
        fillLineTests(line.getAttribute("data-line"));
        placePopover(gutter);
      }
      return;
    }
    var row = target.closest(".src-lines .ln[data-line]");
    if (row && !target.closest("a")) setCurrent(row, false);
    if (!target.closest(".search")) {
      var results = document.getElementById("search-results");
      if (results) results.hidden = true;
    }
  });

  // -- treemap status line: the hovered or focused tile's numbers, at once ---------------------
  function tileStatus(event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var box = target.closest("[data-tm-box]");
    if (!box) return;
    var status = box.querySelector("[data-tm-status]");
    if (!status) return;
    var tile = target.closest("a[data-info]");
    if (tile && box.contains(tile)) {
      status.textContent = tile.getAttribute("data-info") || "";
      status.classList.add("is-live");
    }
  }

  function tileStatusReset(event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var box = target.closest("[data-tm-box]");
    if (!box) return;
    var next = event.relatedTarget;
    if (next && box.contains(next) && next.closest && next.closest("a[data-info]")) return;
    var status = box.querySelector("[data-tm-status]");
    if (!status || !status.classList.contains("is-live")) return;
    var idle = document.createElement("span");
    idle.className = "tm-status-idle";
    idle.textContent = "hover a tile";
    status.replaceChildren(idle);
    status.classList.remove("is-live");
  }

  document.addEventListener("mouseover", tileStatus);
  document.addEventListener("focusin", tileStatus);
  document.addEventListener("mouseout", tileStatusReset);
  document.addEventListener("focusout", tileStatusReset);

  document.addEventListener("toggle", function (event) {
    var details = event.target;
    if (details && details.matches && details.matches("details.fn-grid") && details.open) fillGrid(details);
  }, true);

  document.addEventListener("input", function (event) {
    var target = event.target;
    if (!target) return;
    if (target.id === "search") renderSearch();
    else if (target.matches && target.matches("input[data-filter]")) filterList(target);
  });

  document.addEventListener("change", function (event) {
    if (event.target && event.target.id === "lens") applyLens(event.target.value);
  });

  // the lens menu gets its options just before it opens (mouse, touch or keyboard focus)
  function primeLens(event) {
    if (event.target && event.target.id === "lens") fillLens(event.target);
  }
  document.addEventListener("pointerdown", primeLens, true);
  document.addEventListener("focusin", primeLens);

  window.addEventListener("hashchange", lensFromHash);

  document.addEventListener("DOMContentLoaded", function () {
    root = document.body.getAttribute("data-root") || "";
    applyTheme(storedTheme());
    readData();
    lensFromHash();
    if (window.location.hash && window.location.hash.indexOf("lens=") < 0) {
      var target = document.getElementById(window.location.hash.slice(1));
      if (target && target.classList.contains("ln")) setCurrent(target, false);
    }
    Array.prototype.forEach.call(document.querySelectorAll("[data-highlight-toggle]"), function (button) {
      button.setAttribute("aria-pressed", view.highlight === "missing" ? "true" : "false");
    });
    if (param("highlight")) keepView(view.highlight);
    if (document.querySelector("[data-map-view]") && (view.mode !== "gap" || view.modules !== "thin")) {
      showMap(view.mode, view.modules);
    }
    document.body.dataset.ready = "1";
  });
})();
