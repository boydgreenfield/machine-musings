/* coverset UI behaviour (no inline scripts: the CSP allows only same-origin script files).
   - theme toggle (auto / light / dark) and the view settings (highlight mode, inlay chips),
     applied to <html> before first paint: the server's choice (a query parameter or the scoped
     settings, announced in <meta name="coverset-view">) wins, else the reader's own from
     localStorage, else the default; a click on the theme button beats the settings' view.theme in
     this browser, and the map's mode / module weight also ride in the coverset-map cookie, read by
     the server (no reload)
   - document.body.dataset.ready = "1" whenever htmx has no request in flight (screenshots)
   - a failed htmx request writes "Could not load this section (HTTP n)" into its target
   - morphs keep the reader's open/closed state of <details data-keep-open>
   - copy buttons, keyboard activation of SVG buttons, the line-tests popover position
   - keys, from one table (KEYS): the help dialog (#keys-help) lists the same keys and shows the
     rows that apply to the page; tests/web/test_keys_table.py checks that the dialog, docs/ui.md
     and this table agree, and that report.js binds the same keys
   - filter boxes (input[data-filter]) over a list or table, "/" focuses them
   - the treemap status line: a tile's data-info on hover and keyboard focus (map, overview thumbnail)
   Every listener is delegated from document: no per-line or per-tile handler. */
(function () {
  "use strict";

  var THEME_KEY = "coverset-theme";
  var THEMES = ["auto", "light", "dark"];
  var HIGHLIGHT_KEY = "coverset-highlight";
  var HIGHLIGHTS = ["all", "missing"];
  var CHIPS_KEY = "coverset-chips";
  var MAP_KEY = "coverset-map";
  var root = document.documentElement;

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
      /* storage blocked: the choice still applies to this page */
    }
  }

  // -- view settings, before first paint -------------------------------------------------------
  // <meta name="coverset-view" data-highlight="all" data-highlight-from="default|query|prefs" ...>
  var viewMeta = document.querySelector('meta[name="coverset-view"]');

  function fromServer(name) {
    // the server's value when it chose it (query or settings), else null
    if (!viewMeta) return null;
    var from = viewMeta.getAttribute("data-" + name + "-from");
    return from && from !== "default" ? viewMeta.getAttribute("data-" + name) : null;
  }

  // The theme a reader picks with the toggle beats the scoped settings' view.theme (users may
  // override view settings): the click saves the pick together with the settings value it
  // overrode (THEME_OVER_KEY), and the pick holds while the settings still say that; once someone
  // changes the setting, the new setting wins again until the reader clicks once more.
  var THEME_OVER_KEY = "coverset-theme-over";
  var currentTheme = "auto";

  function storedTheme() {
    var server = fromServer("theme");
    var own = load(THEME_KEY);
    var value = server && load(THEME_OVER_KEY) !== server ? server : own;
    return THEMES.indexOf(value) >= 0 ? value : "auto";
  }

  function pickTheme(theme) {
    save(THEME_KEY, theme);
    save(THEME_OVER_KEY, fromServer("theme") || "");
    applyTheme(theme);
  }

  function applyTheme(theme) {
    currentTheme = theme;
    if (theme === "auto") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    var button = document.getElementById("theme-toggle");
    if (button) {
      button.textContent = "Theme: " + theme;
      button.setAttribute("aria-label", "Colour theme: " + theme + " (click to change)");
    }
  }

  function startHighlight() {
    var value = fromServer("highlight") || load(HIGHLIGHT_KEY);
    return HIGHLIGHTS.indexOf(value) >= 0 ? value : "all";
  }

  applyTheme(storedTheme());
  root.setAttribute("data-highlight", startHighlight());
  if (load(CHIPS_KEY) === "off") root.setAttribute("data-chips", "off");

  // The map is drawn on the server, so a reader's own map mode / module weight (from the keys or
  // the switches) travels in a small cookie, "coverset-map=<mode>.<modules>", that the map page and
  // the overview's Gap thumbnail read after the query and the scoped settings (source "reader"):
  // the first request already draws the reader's map, no reload. localStorage keeps the same
  // choice (older pages saved only there): when the cookie is missing it is written from it, and
  // that one time the page reloads if the server drew something else.
  function savedMap() {
    try {
      return JSON.parse(load(MAP_KEY) || "{}") || {};
    } catch (e) {
      return {};
    }
  }

  function writeMapCookie(saved) {
    var value = (saved.mode || "") + "." + (saved.modules || "");
    if (!/^[a-z]*\.[a-z]*$/.test(value)) return;
    try {
      document.cookie = "coverset-map=" + value + "; path=/; max-age=31536000; SameSite=Lax";
    } catch (e) {
      /* cookies blocked: the switch links still carry the parameters */
    }
  }

  (function mapFromStorage() {
    var saved = savedMap();
    if (!saved.mode && !saved.modules) return;
    if (/(?:^|;\s*)coverset-map=/.test(document.cookie || "")) return;
    writeMapCookie(saved);
    if (!viewMeta || !viewMeta.hasAttribute("data-map-mode")) return;
    var url = new URL(window.location.href);
    var changed = false;
    [["map-mode", "mode"], ["modules", "modules"]].forEach(function (pair) {
      var from = viewMeta.getAttribute("data-" + pair[0] + "-from");
      var want = saved[pair[1]];
      if (from === "default" && want && want !== viewMeta.getAttribute("data-" + pair[0]) && !url.searchParams.has(pair[1])) {
        changed = true;
      }
    });
    if (changed && /(?:^|;\s*)coverset-map=/.test(document.cookie || "")) window.location.reload();
  })();

  // -- ready flag -------------------------------------------------------------------------
  var pending = 0;
  var loaded = false;

  function updateReady() {
    if (!document.body) return;
    document.body.dataset.ready = loaded && pending === 0 ? "1" : "0";
  }

  document.addEventListener("htmx:beforeRequest", function () {
    pending += 1;
    updateReady();
  });
  // htmx fires afterRequest once per request, also after an error, abort or timeout (it fires
  // sendError / sendAbort / timeout as well), so only afterRequest settles a request.
  document.addEventListener("htmx:afterRequest", function () {
    pending = Math.max(0, pending - 1);
    window.setTimeout(updateReady, 0);
  });

  // -- failed fragments: say so instead of staying on "Loading…" ----------------------------
  function showLoadError(event, text) {
    var target = event.detail && event.detail.target;
    if (!target || target === document.body) return;
    if (target.tagName === "TR") target = target.lastElementChild || target;
    var note = document.createElement("span");
    note.className = "load-error";
    note.setAttribute("role", "status");
    note.textContent = text;
    target.replaceChildren(note);
  }
  document.addEventListener("htmx:responseError", function (event) {
    var xhr = event.detail && event.detail.xhr;
    showLoadError(event, "Could not load this section (HTTP " + (xhr ? xhr.status : "error") + ")");
  });
  document.addEventListener("htmx:sendError", function (event) {
    showLoadError(event, "Could not load this section (the server did not answer)");
  });

  // -- morphs leave a reader's open/closed <details data-keep-open> alone ---------------------
  function keepOpenState() {
    var morph = window.Idiomorph;
    if (!morph || !morph.defaults || !morph.defaults.callbacks) return;
    var previous = morph.defaults.callbacks.beforeAttributeUpdated;
    morph.defaults.callbacks.beforeAttributeUpdated = function (name, node, kind) {
      if (name === "open" && node.tagName === "DETAILS" && node.hasAttribute("data-keep-open")) return false;
      return previous ? previous(name, node, kind) : undefined;
    };
  }
  window.addEventListener("load", function () {
    window.setTimeout(function () {
      loaded = true;
      updateReady();
    }, 50);
  });

  // -- helpers ------------------------------------------------------------------------------
  function typing(event) {
    var target = event.target;
    if (!target || !target.closest) return false;
    return !!target.closest("input, textarea, select, [contenteditable], dialog[open]") && !(target.matches && target.matches("dialog"));
  }

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
        window.history.replaceState(null, "", "#" + line.id);
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

  function openTests() {
    var line = statementLine(currentLine());
    if (!line) return;
    var button = line.querySelector("button.gt");
    if (button) {
      button.click();
      return;
    }
    var popover = document.getElementById("line-tests");
    var body = document.getElementById("line-tests-body");
    if (!popover || !body || !popover.showPopover) return;
    body.textContent = "No test ran line " + line.getAttribute("data-line") + ".";
    placePopover(line.querySelector(".gn") || line, true);
    popover.showPopover();
  }

  function toggleGrid() {
    var line = currentLine();
    var region = line ? line.closest(".fn-region") : null;
    if (!region) region = document.querySelector(".fn-region");
    if (!region) return;
    var details = region.querySelector(":scope > details.fn-grid, :scope > .inlay details.fn-grid");
    if (details) details.open = !details.open;
  }

  function follow(rel) {
    var link = document.querySelector('a[rel="' + rel + '"][data-key-nav]');
    if (link) window.location.assign(link.href);
  }

  // -- view settings: highlight mode (h), inlay chips (.), map mode (m) and module weight (M) ---
  function withParam(href, name, value) {
    var url = new URL(href, window.location.href);
    url.searchParams.set(name, value);
    return url.pathname + url.search + url.hash;
  }

  function setHighlight(value) {
    root.setAttribute("data-highlight", value);
    Array.prototype.forEach.call(document.querySelectorAll(".src-frame[data-highlight]"), function (frame) {
      frame.setAttribute("data-highlight", value);
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-highlight-toggle]"), function (button) {
      button.setAttribute("aria-pressed", value === "missing" ? "true" : "false");
    });
    // the links this page built carry the mode, so the next page paints it too
    Array.prototype.forEach.call(document.querySelectorAll("a[data-keep-view]"), function (link) {
      link.setAttribute("href", withParam(link.getAttribute("href"), "highlight", value));
    });
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, "", withParam(window.location.href, "highlight", value));
    }
  }

  function toggleHighlight() {
    var next = root.getAttribute("data-highlight") === "missing" ? "all" : "missing";
    save(HIGHLIGHT_KEY, next);
    setHighlight(next);
  }

  function toggleChips() {
    var off = root.getAttribute("data-chips") !== "off";
    if (off) root.setAttribute("data-chips", "off");
    else root.removeAttribute("data-chips");
    save(CHIPS_KEY, off ? "off" : "on");
  }

  function rememberMap(link) {
    var saved = savedMap();
    if (link.hasAttribute("data-map-mode")) saved.mode = link.getAttribute("data-map-mode");
    if (link.hasAttribute("data-map-modules")) saved.modules = link.getAttribute("data-map-modules");
    save(MAP_KEY, JSON.stringify(saved));
    writeMapCookie(saved);
  }

  function cycle(attribute) {
    // the next of the page's switch links ([data-map-mode] or [data-map-modules]) after the current one
    var links = Array.prototype.slice.call(document.querySelectorAll("a[" + attribute + "]"));
    if (!links.length) return;
    var at = 0;
    for (var i = 0; i < links.length; i++) {
      if (links[i].getAttribute("aria-current") === "true") at = i;
    }
    var next = links[(at + 1) % links.length];
    rememberMap(next);
    window.location.assign(next.href);
  }

  // -- go to (g then a letter), search boxes ---------------------------------------------------
  var chord = null;

  function chordHint(show) {
    var hint = document.getElementById("key-chord");
    if (!show) {
      if (hint) hint.hidden = true;
      return;
    }
    if (!hint) {
      hint = document.createElement("div");
      hint.id = "key-chord";
      hint.className = "key-chord";
      hint.setAttribute("role", "status");
      document.body.appendChild(hint);
    }
    var names = [];
    Array.prototype.forEach.call(document.querySelectorAll("[data-go]"), function (node) {
      var letter = node.getAttribute("data-go");
      if (letter !== "r" && goTarget(letter) && names.indexOf(letter) < 0) names.push(letter);
    });
    hint.textContent = "g … " + (names.length ? names.join(" ") : "nothing to go to here");
    hint.hidden = false;
  }

  function goTarget(letter) {
    // a link marked data-go (or the first link inside an element marked so: the nav includes)
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

  function focusSearch() {
    var box = document.querySelector("input[data-filter], input[type=search]");
    if (!box) return false;
    box.focus();
    box.select();
    return true;
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

  // -- the keys -------------------------------------------------------------------------------
  // scope: where a key applies; the help dialog's rows carry the same data-scope and are shown
  // only when the page has that kind of content (SCOPES).
  var SCOPES = {
    any: function () { return true; },
    source: function () { return !!document.querySelector(".src-lines"); },
    files: function () { return !!document.querySelector('a[data-key-nav]'); },
    map: function () { return !!document.querySelector("a[data-map-mode]"); },
    modules: function () { return !!document.querySelector("a[data-map-modules]"); },
    search: function () { return !!document.querySelector("input[data-filter], input[type=search]"); },
    go: function () { return !!document.querySelector("[data-go]"); },
    run: function () { return !!goTarget("r"); }
  };

  var KEYS = {
    "?": { scope: "any", run: openHelp },
    "Escape": { scope: "any", run: dismiss },
    "/": { scope: "search", run: focusSearch },
    "g": { scope: "go", run: function () { chord = window.setTimeout(endChord, 1500); chordHint(true); } },
    "r": { scope: "run", run: function () { go("r"); } },
    "j": { scope: "source", run: function () { step(".ln.mis:not(.cont), .ln.par:not(.cont)", 1); } },
    "k": { scope: "source", run: function () { step(".ln.mis:not(.cont), .ln.par:not(.cont)", -1); } },
    "n": { scope: "source", run: function () { stepFunction(1); } },
    "N": { scope: "source", run: function () { stepFunction(-1); } },
    "t": { scope: "source", run: openTests },
    "i": { scope: "source", run: toggleGrid },
    "h": { scope: "source", run: toggleHighlight },
    ".": { scope: "source", run: toggleChips },
    "[": { scope: "files", run: function () { follow("prev"); } },
    "]": { scope: "files", run: function () { follow("next"); } },
    "m": { scope: "map", run: function () { cycle("data-map-mode"); } },
    "M": { scope: "modules", run: function () { cycle("data-map-modules"); } }
  };
  // after "g": overview, tests, map, economy, invariants (the links come from the page, never a URL here)
  var GO_KEYS = ["o", "t", "m", "e", "i"];

  function endChord() {
    if (chord !== null) window.clearTimeout(chord);
    chord = null;
    chordHint(false);
  }

  function openHelp() {
    var dialog = document.getElementById("keys-help");
    if (!dialog || !dialog.showModal) return false;
    Array.prototype.forEach.call(dialog.querySelectorAll("[data-scope]"), function (row) {
      var test = SCOPES[row.getAttribute("data-scope")];
      var letter = row.getAttribute("data-go-row");
      row.hidden = !(test ? test() : true) || (letter ? !goTarget(letter) : false);
    });
    // a section whose rows are all hidden goes too, with its heading
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
    return false; // let Escape do its native work too (closing a dialog)
  }

  document.addEventListener("keydown", function (event) {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
    var key = event.key;
    var target = event.target;
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
    if ((key === "Enter" || key === " ") && target && target.matches && target.matches('[role="button"][hx-get]')) {
      event.preventDefault();
      target.dispatchEvent(new MouseEvent("click", { bubbles: true }));
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

  document.addEventListener("input", function (event) {
    var target = event.target;
    if (target && target.matches && target.matches("input[data-filter]")) filterList(target);
  });

  // -- clicks: theme, highlight, copy, map switches, line selection, popover placement ----------
  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var toggle = target.closest("#theme-toggle");
    if (toggle) {
      // cycle from the theme on screen, not from storage: the settings value would pin it
      pickTheme(THEMES[(THEMES.indexOf(currentTheme) + 1) % THEMES.length]);
      return;
    }
    if (target.closest("[data-highlight-toggle]")) {
      toggleHighlight();
      return;
    }
    var mapLink = target.closest("a[data-map-mode], a[data-map-modules]");
    if (mapLink) {
      rememberMap(mapLink);
      return;
    }
    var copy = target.closest("button.copy[data-copy-target]");
    if (copy) {
      var source = document.querySelector(copy.getAttribute("data-copy-target"));
      if (source && navigator.clipboard) {
        navigator.clipboard.writeText(source.textContent || "").then(function () {
          copy.setAttribute("data-copied", "");
          copy.textContent = "Copied";
          window.setTimeout(function () {
            copy.removeAttribute("data-copied");
            copy.textContent = "Copy";
          }, 1600);
        });
      }
      return;
    }
    var gutter = target.closest("button.gt");
    if (gutter) {
      var line = gutter.closest(".ln");
      if (line) setCurrent(line, false);
      placePopover(gutter);
      return;
    }
    var row = target.closest(".src-lines .ln[data-line]");
    if (row && !target.closest("a")) setCurrent(row, false);
  });

  function placePopover(anchor, keepText) {
    var popover = document.getElementById("line-tests");
    if (!popover) return;
    var body = document.getElementById("line-tests-body");
    if (body && !keepText) body.textContent = "Loading…";
    window.requestAnimationFrame(function () {
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
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyTheme(storedTheme());
    keepOpenState();
    updateReady();
    var mode = root.getAttribute("data-highlight");
    Array.prototype.forEach.call(document.querySelectorAll("[data-highlight-toggle]"), function (button) {
      button.setAttribute("aria-pressed", mode === "missing" ? "true" : "false");
    });
    if (window.location.hash) {
      var target = document.getElementById(window.location.hash.slice(1));
      if (target && target.classList.contains("ln")) setCurrent(target, false);
    }
  });
})();
