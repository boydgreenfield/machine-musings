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
   - the line-tests popover position, the map switches (m, M) and the live pages' own keys (t, i,
     r), bound into keys.js (loaded first: storage, navigation, the shared KEYS table, filter
     boxes, copy buttons, the treemap status line)
   Every listener is delegated from document: no per-line or per-tile handler. */
(function () {
  "use strict";

  var THEME_KEY = "coverset-theme";
  var THEMES = ["auto", "light", "dark"];
  var HIGHLIGHTS = ["all", "missing"];
  var MAP_KEY = "coverset-map";
  var root = document.documentElement;
  var keys = window.coversetKeys;
  var load = keys.load;
  var save = keys.save;

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
    var value = fromServer("highlight") || load("coverset-highlight");
    return HIGHLIGHTS.indexOf(value) >= 0 ? value : "all";
  }

  applyTheme(storedTheme());
  root.setAttribute("data-highlight", startHighlight());

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

  // -- the live pages' keys: t (tests of the current line), i (its shape grid), m / M (the map) --
  function openTests() {
    var line = keys.statementLine(keys.currentLine());
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
    var line = keys.currentLine();
    var region = line ? line.closest(".fn-region") : null;
    if (!region) region = document.querySelector(".fn-region");
    if (!region) return;
    var details = region.querySelector(":scope > details.fn-grid, :scope > .inlay details.fn-grid");
    if (details) details.open = !details.open;
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

  var KEYS = {
    "r": { scope: "run", run: function () { window.location.assign(keys.goTarget("r").href); } },
    "t": { scope: "source", run: openTests },
    "i": { scope: "source", run: toggleGrid },
    "m": { scope: "map", run: function () { cycle("data-map-mode"); } },
    "M": { scope: "modules", run: function () { cycle("data-map-modules"); } }
  };
  // after "g": overview, tests, map, economy, invariants (the links come from the page, never a URL here)
  var GO_KEYS = ["o", "t", "m", "e", "i"];
  keys.bind(KEYS, GO_KEYS, {
    map: function () { return !!document.querySelector("a[data-map-mode]"); },
    modules: function () { return !!document.querySelector("a[data-map-modules]"); },
    run: function () { return !!keys.goTarget("r"); }
  });

  // -- clicks: theme, map switches, popover placement ----------------------------------------------
  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var toggle = target.closest("#theme-toggle");
    if (toggle) {
      // cycle from the theme on screen, not from storage: the settings value would pin it
      pickTheme(THEMES[(THEMES.indexOf(currentTheme) + 1) % THEMES.length]);
      return;
    }
    var mapLink = target.closest("a[data-map-mode], a[data-map-modules]");
    if (mapLink) {
      rememberMap(mapLink);
      return;
    }
    var gutter = target.closest("button.gt");
    if (gutter) {
      placePopover(gutter);
    }
  });

  function placePopover(anchor, keepText) {
    var body = document.getElementById("line-tests-body");
    if (body && !keepText) body.textContent = "Loading…";
    window.requestAnimationFrame(function () {
      keys.placePopover(anchor);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyTheme(storedTheme());
    keepOpenState();
    updateReady();
  });
})();
