/* coverset keys and the behaviour the live pages (app.js) and the static report (report.js, which
   ships a copy of this file) share; no inline scripts, works from file:// too.
   - the reader's storage (localStorage, blocked storage tolerated) and the inlay chips (.), applied
     to <html> before first paint
   - source navigation: the current line, j/k (missed and partial lines), n/N (functions), [ ] (the
     previous and next file), the highlight mode (h) that rides along in a[data-keep-view] links
   - go to (g then a letter: the page's [data-go] links), filter boxes (input[data-filter]), "/"
   - keys, from one table (KEYS) plus each page script's own (bind): the help dialog (#keys-help)
     lists the same keys and shows the rows that apply to the page; tests/web/test_keys_table.py
     checks that the dialogs, docs/ui.md, docs/report.md and the tables agree
   - copy buttons, keyboard activation of SVG buttons, the treemap status line (a tile's data-info
     on hover and keyboard focus)
   Every listener is delegated from document: no per-line or per-tile handler. The page scripts
   reach the shared pieces through window.coversetKeys. */
(function () {
  "use strict";

  var HIGHLIGHT_KEY = "coverset-highlight";
  var CHIPS_KEY = "coverset-chips";
  var root = document.documentElement;
  var hooks = { onKey: null, dismiss: null };

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

  function replaceUrl(url) {
    if (!window.history || !window.history.replaceState) return;
    try {
      window.history.replaceState(null, "", url);
    } catch (e) {
      /* file:// may refuse; the page and its links still carry the state */
    }
  }

  if (load(CHIPS_KEY) === "off") root.setAttribute("data-chips", "off");

  // -- source navigation (j/k n/N [ ]) ------------------------------------------------------------
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
      if (line.id) replaceUrl("#" + line.id);
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

  function placePopover(anchor) {
    // the line-tests popover next to ``anchor``: below it, or above when there is more room there
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

  function follow(rel) {
    var link = document.querySelector('a[rel="' + rel + '"][data-key-nav]');
    if (link) window.location.assign(link.href);
  }

  // -- highlight mode (h) and inlay chips (.) -------------------------------------------------------
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

  function pressHighlight(value) {
    Array.prototype.forEach.call(document.querySelectorAll("[data-highlight-toggle]"), function (button) {
      button.setAttribute("aria-pressed", value === "missing" ? "true" : "false");
    });
  }

  function setHighlight(value) {
    root.setAttribute("data-highlight", value);
    Array.prototype.forEach.call(document.querySelectorAll(".src-frame[data-highlight]"), function (frame) {
      frame.setAttribute("data-highlight", value);
    });
    pressHighlight(value);
    keepView(value);
    replaceUrl(withParam(window.location.href, "highlight", value));
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

  // -- go to (g then a letter), filter boxes --------------------------------------------------------
  var chord = null;
  var GO_KEYS = [];

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
    var names = GO_KEYS.filter(function (letter) {
      return !!goTarget(letter);
    });
    hint.textContent = "g … " + (names.length ? names.join(" ") : "nothing to go to here");
    hint.hidden = false;
  }

  function endChord() {
    if (chord !== null) window.clearTimeout(chord);
    chord = null;
    chordHint(false);
  }

  var SEARCH = "#search, input[data-filter], input[type=search]";

  function focusSearch() {
    var box = document.querySelector(SEARCH);
    if (!box) return false;
    box.focus();
    if (box.select) box.select();
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

  // scope: where a key applies; the help dialog's rows carry the same data-scope and are shown only
  // when the page has that kind of content (the page scripts add theirs)
  var SCOPES = {
    any: function () { return true; },
    source: function () { return !!document.querySelector(".src-lines"); },
    files: function () { return !!document.querySelector("a[data-key-nav]"); },
    search: function () { return !!document.querySelector(SEARCH); },
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
    if (hooks.dismiss) hooks.dismiss();
    return false; // let Escape do its native work too (closing a dialog)
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
    "h": { scope: "source", run: toggleHighlight },
    ".": { scope: "source", run: toggleChips },
    "[": { scope: "files", run: function () { follow("prev"); } },
    "]": { scope: "files", run: function () { follow("next"); } }
  };

  function bind(keys, goKeys, scopes) {
    // a page script's own keys (and the scopes they need) and the letters it allows after "g"
    Object.keys(keys).forEach(function (key) {
      KEYS[key] = keys[key];
    });
    Object.keys(scopes || {}).forEach(function (name) {
      SCOPES[name] = scopes[name];
    });
    GO_KEYS = goKeys;
  }

  document.addEventListener("keydown", function (event) {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
    if (hooks.onKey && hooks.onKey(event)) return;
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
    if ((key === "Enter" || key === " ") && target && target.matches && target.matches('[role="button"]:not(button)')) {
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

  document.addEventListener("input", function (event) {
    var target = event.target;
    if (target && target.matches && target.matches("input[data-filter]")) filterList(target);
  });

  // -- clicks: the highlight toggle, copy buttons, line selection ------------------------------------
  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || !target.closest) return;
    if (target.closest("[data-highlight-toggle]")) {
      toggleHighlight();
      return;
    }
    var copy = target.closest("button.copy[data-copy-target]");
    if (copy) {
      copyText(copy);
      return;
    }
    var row = target.closest(".src-lines .ln[data-line]");
    if (row && !target.closest("a")) setCurrent(row, false);
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

  document.addEventListener("DOMContentLoaded", function () {
    pressHighlight(root.getAttribute("data-highlight"));
    if (window.location.hash && window.location.hash.indexOf("lens=") < 0) {
      var target = document.getElementById(window.location.hash.slice(1));
      if (target && target.classList.contains("ln")) setCurrent(target, false);
    }
  });

  window.coversetKeys = {
    bind: bind,
    hooks: hooks,
    load: load,
    save: save,
    replaceUrl: replaceUrl,
    keepView: keepView,
    setCurrent: setCurrent,
    currentLine: currentLine,
    statementLine: statementLine,
    placePopover: placePopover,
    goTarget: goTarget
  };
})();
