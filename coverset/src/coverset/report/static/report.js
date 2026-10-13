/* coverset report: behaviour of the static pages (no inline scripts, works from file://).
   - theme toggle (auto / light / dark) persisted in localStorage, applied before first paint
   - the line-tests popover, built from the page's JSON data block (#report-data)
   - the data-flow lens: dim the lines the selected test did not run (#lens=<test hash> preselects)
   - shape grids under def lines (key i) and the grid cell -> input-class row link
   - treemap Gap / Lines toggle, the status line above it (a tile's data-info on hover and focus),
     sortable tables, search over files / functions / tests
   - keys: j/k n/N t i [ ] / ? (j/k skip the continuation lines of a multi-line statement)
   - document.body.dataset.ready = "1" once all of the above is initialised (screenshots) */
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
    var hits = 0;
    (test[3] || []).forEach(function (n) {
      var line = shell.querySelector('.ln[data-line="' + n + '"]');
      if (line) {
        line.classList.add("lens-hit");
        hits += 1;
      }
      // the continuation lines of a multi-line statement follow its first line
      Array.prototype.forEach.call(shell.querySelectorAll('.ln.cont[data-first="' + n + '"]'), function (cont) {
        cont.classList.add("lens-hit");
      });
    });
    shell.classList.add("lens-on");
    if (bar) bar.classList.add("is-active");
    if (select) select.value = String(index);
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
    for (var i = 0; i < all.length; i++) {
      if (all[i].getBoundingClientRect().top >= 80) return i - 1;
    }
    return -1;
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

  function showMap(mode) {
    Array.prototype.forEach.call(document.querySelectorAll("[data-map]"), function (button) {
      button.setAttribute("aria-pressed", button.getAttribute("data-map") === mode ? "true" : "false");
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-map-view], [data-map-legend]"), function (node) {
      var which = node.getAttribute("data-map-view") || node.getAttribute("data-map-legend");
      node.hidden = which !== mode;
    });
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

  // -- events -------------------------------------------------------------------------------------
  function typing(event) {
    var target = event.target;
    if (!target || !target.closest) return false;
    return !!target.closest("input, textarea, select, [contenteditable], dialog[open]") &&
      !(target.matches && target.matches("dialog"));
  }

  document.addEventListener("keydown", function (event) {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
    var key = event.key;
    if (event.target && event.target.id === "search") {
      if (key === "ArrowDown") { moveSelection(1); event.preventDefault(); }
      else if (key === "ArrowUp") { moveSelection(-1); event.preventDefault(); }
      else if (key === "Enter") {
        var list = document.getElementById("search-results");
        var pick = list && (list.querySelector('li[aria-selected="true"] a') || list.querySelector("li a"));
        if (pick) { window.location.assign(pick.href); event.preventDefault(); }
      } else if (key === "Escape") {
        event.target.value = "";
        renderSearch();
        event.target.blur();
      }
      return;
    }
    if (typing(event)) return;
    if ((key === "Enter" || key === " ") && event.target && event.target.matches &&
        event.target.matches('[role="button"].sg-cell')) {
      event.preventDefault();
      showCell(event.target);
      return;
    }
    var handled = true;
    if (key === "?") {
      var dialog = document.getElementById("keys-help");
      if (dialog && dialog.showModal) dialog.showModal();
    } else if (key === "/") {
      var input = document.getElementById("search");
      if (input) input.focus();
    } else if (key === "[") follow("prev");
    else if (key === "]") follow("next");
    else if (!document.querySelector(".src-lines")) handled = false;
    else if (key === "j") step(".ln.mis:not(.cont), .ln.par:not(.cont)", 1);
    else if (key === "k") step(".ln.mis:not(.cont), .ln.par:not(.cont)", -1);
    else if (key === "n") stepFunction(1);
    else if (key === "N") stepFunction(-1);
    else if (key === "t") toggleTests();
    else if (key === "i") toggleGrid();
    else handled = false;
    if (handled) event.preventDefault();
  });

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || !target.closest) return;
    if (target.closest("#theme-toggle")) {
      var next = THEMES[(THEMES.indexOf(storedTheme()) + 1) % THEMES.length];
      try {
        window.localStorage.setItem(THEME_KEY, next);
      } catch (e) {
        /* storage blocked: the theme still applies for this page */
      }
      applyTheme(next);
      return;
    }
    var copy = target.closest("button.copy[data-copy-target]");
    if (copy) { copyText(copy); return; }
    var sorter = target.closest("table.sortable th button[data-sort]");
    if (sorter) { sortTable(sorter); return; }
    var map = target.closest("[data-map]");
    if (map) { showMap(map.getAttribute("data-map")); return; }
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
    if (event.target && event.target.id === "search") renderSearch();
  });

  document.addEventListener("change", function (event) {
    if (event.target && event.target.id === "lens") applyLens(event.target.value);
  });

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
    document.body.dataset.ready = "1";
  });
})();
