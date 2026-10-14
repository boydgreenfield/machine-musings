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
   - the report's own keys (t, i, m, M), bound into keys.js (a copy of the live pages' file, loaded
     first: storage, navigation, the shared KEYS table, filter boxes, copy buttons, the treemap
     status line)
   - document.body.dataset.ready = "1" once all of the above is initialised (screenshots)
   Every listener is delegated from document: no per-line or per-tile handler. */
(function () {
  "use strict";

  var keys = window.coversetKeys;
  var load = keys.load;
  var save = keys.save;
  var THEME_KEY = "coverset-theme";
  var THEMES = ["auto", "light", "dark"];

  function storedTheme() {
    var value = load(THEME_KEY);
    return THEMES.indexOf(value) >= 0 ? value : "auto";
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

  var HIGHLIGHTS = ["all", "missing"];
  var MAP_KEY = "coverset-map";
  var MAP_MODES = ["gap", "lines"];
  var WEIGHTS = ["full", "thin", "hidden"];
  var html = document.documentElement;

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
    highlight: pick(param("highlight"), load("coverset-highlight"), HIGHLIGHTS, "all"),
    mode: pick(param("mode"), savedMap().mode, MAP_MODES, "gap"),
    modules: pick(param("modules"), savedMap().modules, WEIGHTS, "thin")
  };
  html.setAttribute("data-highlight", view.highlight);

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

  function showLineTests(line, anchor) {
    var popover = document.getElementById("line-tests");
    if (!popover) return;
    fillLineTests(line.getAttribute("data-line"));
    keys.placePopover(anchor || line.querySelector(".gn") || line);
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

  // -- the report's keys: t (tests of the current line), i (its shape grid) ------------------------
  function toggleTests() {
    var popover = document.getElementById("line-tests");
    if (popover && popover.matches && popover.matches(":popover-open")) {
      popover.hidePopover();
      return;
    }
    var line = keys.statementLine(keys.currentLine());
    if (line) showLineTests(line, line.querySelector("button.gt") || line.querySelector(".gn"));
  }

  function toggleGrid() {
    var line = keys.currentLine();
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

  var KEYS = {
    "t": { scope: "source", run: toggleTests },
    "i": { scope: "source", run: toggleGrid },
    "m": { scope: "map", run: function () { cycleMap("data-map"); } },
    "M": { scope: "modules", run: function () { cycleMap("data-map-modules"); } }
  };
  // after "g": overview, tests, map (the report has no economy or invariants pages)
  var GO_KEYS = ["o", "t", "m"];
  keys.bind(KEYS, GO_KEYS, {
    map: function () { return !!document.querySelector("button[data-map]"); },
    modules: function () { return !!document.querySelector("button[data-map-modules]"); }
  });

  keys.hooks.dismiss = function () {
    var results = document.getElementById("search-results");
    if (results) results.hidden = true;
  };

  // the search box's own keys come before the shared table
  keys.hooks.onKey = function (event) {
    var target = event.target;
    if (!target || target.id !== "search") return false;
    var key = event.key;
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
    return true;
  };

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || !target.closest) return;
    if (target.closest("#theme-toggle")) {
      var next = THEMES[(THEMES.indexOf(storedTheme()) + 1) % THEMES.length];
      save(THEME_KEY, next);
      applyTheme(next);
      return;
    }
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
        fillLineTests(line.getAttribute("data-line"));
        keys.placePopover(gutter);
      }
      return;
    }
    if (!target.closest(".search")) {
      var results = document.getElementById("search-results");
      if (results) results.hidden = true;
    }
  });

  document.addEventListener("toggle", function (event) {
    var details = event.target;
    if (details && details.matches && details.matches("details.fn-grid") && details.open) fillGrid(details);
  }, true);

  document.addEventListener("input", function (event) {
    var target = event.target;
    if (!target) return;
    if (target.id === "search") renderSearch();
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
    if (param("highlight")) keys.keepView(view.highlight);
    if (document.querySelector("[data-map-view]") && (view.mode !== "gap" || view.modules !== "thin")) {
      showMap(view.mode, view.modules);
    }
    document.body.dataset.ready = "1";
  });
})();
