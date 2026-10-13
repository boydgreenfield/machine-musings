/* coverset UI behaviour (no inline scripts: the CSP allows only same-origin script files).
   - theme toggle (auto / light / dark) persisted in localStorage, applied before first paint
   - document.body.dataset.ready = "1" whenever htmx has no request in flight (screenshots)
   - a failed htmx request writes "Could not load this section (HTTP n)" into its target
   - morphs keep the reader's open/closed state of <details data-keep-open>
   - copy buttons, keyboard activation of SVG buttons, the line-tests popover position
   - source-view keys: j/k n/N t i [ ] ? (j/k skip the continuation lines of a multi-line statement)
   - the treemap status line: a tile's data-info on hover and keyboard focus (map, overview thumbnail) */
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
    if (theme === "auto") {
      root.removeAttribute("data-theme");
    } else {
      root.setAttribute("data-theme", theme);
    }
    var button = document.getElementById("theme-toggle");
    if (button) {
      button.textContent = "Theme: " + theme;
      button.setAttribute("aria-label", "Colour theme: " + theme + " (click to change)");
    }
  }

  applyTheme(storedTheme());

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
    // first line whose top is below the viewport top
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

  document.addEventListener("keydown", function (event) {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
    if (typing(event)) return;
    var key = event.key;
    if (key === "?") {
      var dialog = document.getElementById("keys-help");
      if (dialog && dialog.showModal) {
        event.preventDefault();
        dialog.showModal();
      }
      return;
    }
    if ((key === "Enter" || key === " ") && event.target && event.target.matches &&
        event.target.matches('[role="button"][hx-get]')) {
      event.preventDefault();
      event.target.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      return;
    }
    if (!document.querySelector(".src-lines")) {
      if (key === "[") follow("prev");
      if (key === "]") follow("next");
      return;
    }
    var handled = true;
    if (key === "j") step(".ln.mis:not(.cont), .ln.par:not(.cont)", 1);
    else if (key === "k") step(".ln.mis:not(.cont), .ln.par:not(.cont)", -1);
    else if (key === "n") stepFunction(1);
    else if (key === "N") stepFunction(-1);
    else if (key === "t") openTests();
    else if (key === "i") toggleGrid();
    else if (key === "[") follow("prev");
    else if (key === "]") follow("next");
    else handled = false;
    if (handled) event.preventDefault();
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

  // -- clicks: theme, copy, line selection, popover placement --------------------------------
  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var toggle = target.closest("#theme-toggle");
    if (toggle) {
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
    if (window.location.hash) {
      var target = document.getElementById(window.location.hash.slice(1));
      if (target && target.classList.contains("ln")) setCurrent(target, false);
    }
  });
})();
