/* Pullbox — client-side utilities for toast notifications and HTMX events. */

window.pbResolveCheckboxSelection = function (options) {
  var selectedIds = Array.isArray(options.selectedIds) ? options.selectedIds.slice() : [];
  var visibleIds = Array.isArray(options.visibleIds) ? options.visibleIds.slice() : [];
  var itemKey = String(options.itemId);
  var visibleItemId = visibleIds.find(function (id) {
    return String(id) === itemKey;
  });
  var itemId = visibleItemId === undefined ? options.itemId : visibleItemId;

  function unique(ids) {
    var seen = Object.create(null);
    return ids.filter(function (id) {
      var key = String(id);
      if (seen[key]) {
        return false;
      }
      seen[key] = true;
      return true;
    });
  }

  if (options.shiftKey) {
    var anchorKey = options.anchorId === null ? "" : String(options.anchorId);
    var anchorIndex = visibleIds.findIndex(function (id) {
      return String(id) === anchorKey;
    });
    var itemIndex = visibleIds.findIndex(function (id) {
      return String(id) === itemKey;
    });
    var rangeIds = [itemId];

    if (anchorIndex >= 0 && itemIndex >= 0) {
      rangeIds = visibleIds.slice(
        Math.min(anchorIndex, itemIndex),
        Math.max(anchorIndex, itemIndex) + 1
      );
    }

    return {
      selectedIds: options.additiveKey ? unique(selectedIds.concat(rangeIds)) : unique(rangeIds),
      anchorId: anchorIndex >= 0 ? options.anchorId : itemId,
    };
  }

  if (options.additiveKey) {
    var remainingIds = selectedIds.filter(function (id) {
      return String(id) !== itemKey;
    });
    return {
      selectedIds: options.checked ? unique(remainingIds.concat([itemId])) : remainingIds,
      anchorId: itemId,
    };
  }

  return {
    selectedIds: options.checked ? [itemId] : [],
    anchorId: itemId,
  };
};

/*
 * Poll-cache: suppress redundant HTMX swaps for polling requests.
 * When a polled endpoint returns identical HTML to the previous response,
 * skip the swap entirely. This prevents hover-state flicker and preserves
 * Alpine.js runtime state (x-show, x-data) on elements that haven't changed.
 */
(function () {
  var _pollCache = {};

  function escapeCssId(value) {
    if (window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(value);
    }

    return String(value).replace(/([ !"#$%&'()*+,./:;<=>?@[\\\]^`{|}~])/g, "\\$1");
  }

  function setLazyTableDetailState(button, rowId, expanded) {
    if (button && button.setAttribute) {
      button.setAttribute("aria-expanded", expanded ? "true" : "false");
    }

    window.dispatchEvent(
      new CustomEvent("pb-lazy-table-detail-state", {
        detail: { rowId: rowId, expanded: expanded },
      })
    );
  }

  window.pbToggleLazyTableDetail = function (button, rowId, triggerName) {
    var row = document.getElementById(rowId);
    if (row) {
      button.dataset.lazyDetailDesiredOpen = "false";
      row.remove();
      delete button.dataset.lazyDetailLoading;
      if (!triggerName) {
        button.dataset.skipLazyDetailFetch = "true";
      }
      setLazyTableDetailState(button, rowId, false);
      return false;
    }

    if (button.dataset.lazyDetailLoading === "true") {
      button.dataset.lazyDetailDesiredOpen = "false";
      setLazyTableDetailState(button, rowId, false);
      if (!triggerName) {
        button.dataset.skipLazyDetailFetch = "true";
      }
      return false;
    }

    delete button.dataset.skipLazyDetailFetch;
    if (triggerName) {
      button.dataset.lazyDetailDesiredOpen = "true";
      button.dataset.lazyDetailLoading = "true";
      setLazyTableDetailState(button, rowId, true);
      document.body.dispatchEvent(new CustomEvent(triggerName, { bubbles: true }));
    }
    return true;
  };

  window.pbLazyTableDetailSettled = function (button, rowId) {
    if (button && button.dataset) {
      delete button.dataset.lazyDetailLoading;
    }
    var row = document.getElementById(rowId);
    if (button && button.dataset.lazyDetailDesiredOpen === "false") {
      if (row) {
        row.remove();
      }
      setLazyTableDetailState(button, rowId, false);
      return;
    }
    var expanded = Boolean(row);
    if (button && button.dataset) {
      button.dataset.lazyDetailDesiredOpen = expanded ? "true" : "false";
    }
    setLazyTableDetailState(button, rowId, expanded);
  };

  window.pbLazyTableDetailAfterRequest = function (button, rowId) {
    window.setTimeout(function () {
      window.pbLazyTableDetailSettled(button, rowId);
    }, 0);
  };

  window.pbCancelSkippedLazyDetailFetch = function (event) {
    var button = event.currentTarget;
    if (button && button.dataset.skipLazyDetailFetch === "true") {
      event.preventDefault();
      delete button.dataset.skipLazyDetailFetch;
    }
  };

  function normalizePolledHtml(html) {
    var wrapper = document.createElement("div");
    wrapper.innerHTML = html || "";
    var parts = [];

    for (var i = 0; i < wrapper.childNodes.length; i += 1) {
      var node = wrapper.childNodes[i];
      if (node.nodeType === Node.TEXT_NODE) {
        if (node.textContent && node.textContent.trim()) {
          parts.push(node.textContent.trim());
        }
        continue;
      }

      if (node.nodeType === Node.ELEMENT_NODE) {
        parts.push(node.outerHTML);
      }
    }

    return parts.join("");
  }

  function normalizePollingResponseForTarget(target, html) {
    if (!target || !target.id) {
      return normalizePolledHtml(html);
    }

    var wrapper = document.createElement("div");
    wrapper.innerHTML = html || "";

    var match = wrapper.querySelector("#" + escapeCssId(target.id));
    if (!match) {
      return normalizePolledHtml(html);
    }

    return normalizePolledHtml(match.innerHTML);
  }

  function isPollingElement(elt) {
    if (!elt || !elt.id) return false;
    var trigger = elt.getAttribute("hx-trigger") || "";
    return trigger.indexOf("every") !== -1;
  }

  function seedPollCache(root) {
    if (!root) return;

    var elements = [];
    if (isPollingElement(root)) {
      elements.push(root);
    }
    if (root.querySelectorAll) {
      var found = root.querySelectorAll("[id][hx-trigger*='every']");
      for (var i = 0; i < found.length; i += 1) {
        elements.push(found[i]);
      }
    }

    for (var j = 0; j < elements.length; j += 1) {
      var elt = elements[j];
      _pollCache[elt.id] = normalizePolledHtml(elt.innerHTML);
    }
  }

  seedPollCache(document);
  seedSearchFieldStates(document);

  document.addEventListener("DOMContentLoaded", function () {
    seedPollCache(document);
    seedSearchFieldStates(document);
  });

  document.body.addEventListener("htmx:load", function (evt) {
    var root = (evt.detail && evt.detail.elt) || evt.target || document;
    seedPollCache(root);
    seedSearchFieldStates(root);
  });

  document.body.addEventListener("htmx:beforeSwap", function (evt) {
    var elt = evt.detail.elt;
    if (!isPollingElement(elt)) return;

    // A poll may already be in flight when a lazy detail opens. Preserve the
    // interactive row instead of letting that stale response replace it.
    if (
      elt.querySelector &&
      elt.querySelector("[data-lazy-detail-desired-open='true']")
    ) {
      evt.detail.shouldSwap = false;
      return;
    }

    var response = normalizePollingResponseForTarget(elt, evt.detail.serverResponse);
    var key = elt.id;

    if (_pollCache[key] === response) {
      // Identical to last poll — skip swap, preserve DOM as-is
      evt.detail.shouldSwap = false;
      return;
    }

    // New content — cache it and let the swap proceed
    _pollCache[key] = response;
  });
})();

var _importEventSourceRegistry = (function () {
  var _entries = {};
  var _suspendedResumes = [];
  var _nextId = 1;

  function register(source, options) {
    if (!source || typeof source.close !== "function") {
      return null;
    }

    var opts = options || {};
    var id = String(_nextId);
    _nextId += 1;
    _entries[id] = {
      id: id,
      source: source,
      onClose: typeof opts.onClose === "function" ? opts.onClose : null,
      resume: typeof opts.resume === "function" ? opts.resume : null,
    };
    return id;
  }

  function close(id, source, reason, suspend) {
    var entry = id ? _entries[id] : null;
    if (!entry || (source && entry.source !== source)) {
      return;
    }

    delete _entries[id];
    try {
      entry.source.close();
    } catch (_) {
      // Closing an EventSource is best-effort; stale sockets must not break navigation.
    }

    if (entry.onClose) {
      entry.onClose(entry.source, reason || "closed");
    }

    if (suspend && entry.resume) {
      _suspendedResumes.push(entry.resume);
    }
  }

  function closeAll(reason, suspend) {
    var ids = Object.keys(_entries);
    for (var i = 0; i < ids.length; i += 1) {
      close(ids[i], null, reason || "closed", suspend === true);
    }
  }

  function resumeSuspended() {
    var resumes = _suspendedResumes.slice();
    _suspendedResumes = [];
    for (var i = 0; i < resumes.length; i += 1) {
      try {
        resumes[i]();
      } catch (_) {
        // One stale component should not prevent other live import panels from resuming.
      }
    }
  }

  function clearSuspended() {
    _suspendedResumes = [];
  }

  return {
    register: register,
    close: close,
    closeAll: closeAll,
    resumeSuspended: resumeSuspended,
    clearSuspended: clearSuspended,
    size: function () {
      return Object.keys(_entries).length;
    },
  };
})();

window.PullboxImportEventSources = _importEventSourceRegistry;

/*
 * Auto-tooltips: for elements that may truncate, opt in with data-tooltip-auto.
 * The tooltip can live on a wrapper while measuring an inner text node marked
 * with data-tooltip-measure. Tooltips render through a global overlay host so
 * they can escape clipped/scrolling panes while keeping the same markup API.
 */
(function () {
  var activeAutoEl = null;
  var activeTooltipEl = null;
  var tooltipHost = null;
  var tooltipBubble = null;
  var TOOLTIP_VIEWPORT_MARGIN = 16;
  var TOOLTIP_GAP = 8;

  function ensureTooltipHost() {
    if (tooltipHost && tooltipBubble) return true;

    tooltipHost = document.getElementById("global-tooltip-host");
    if (!tooltipHost) {
      tooltipHost = document.createElement("div");
      tooltipHost.id = "global-tooltip-host";
      tooltipHost.className = "app-tooltip-host";
      tooltipHost.setAttribute("aria-hidden", "true");
      document.body.appendChild(tooltipHost);
    }

    tooltipBubble = tooltipHost.querySelector(".app-tooltip-overlay");
    if (!tooltipBubble) {
      tooltipBubble = document.createElement("div");
      tooltipBubble.className = "app-tooltip-overlay";
      tooltipBubble.hidden = true;
      tooltipHost.appendChild(tooltipBubble);
    }

    document.documentElement.classList.add("tooltips-overlay-enabled");
    return true;
  }

  function findAutoTooltipTarget(node) {
    return node && node.closest ? node.closest("[data-tooltip-auto]") : null;
  }

  function findTooltipTarget(node) {
    return node && node.closest ? node.closest("[data-tip]") : null;
  }

  function resolveTooltipMeasureEl(el) {
    if (!el || !el.querySelector) return el;

    return el.querySelector("[data-tooltip-measure]") || el;
  }

  function isActuallyTruncated(el) {
    var measureEl = resolveTooltipMeasureEl(el);
    if (!measureEl) return false;

    return (
      measureEl.scrollWidth > measureEl.clientWidth + 1 ||
      measureEl.scrollHeight > measureEl.clientHeight + 1
    );
  }

  function resolveTooltipText(el) {
    var measureEl = resolveTooltipMeasureEl(el);
    if (!el && !measureEl) return "";
    return (
      (el && el.getAttribute("data-tooltip")) ||
      (el && el.getAttribute("data-tip")) ||
      (el && el.getAttribute("aria-label")) ||
      (el && el.getAttribute("title")) ||
      (measureEl && measureEl.getAttribute("data-tooltip")) ||
      (measureEl && measureEl.getAttribute("data-tip")) ||
      (measureEl && measureEl.getAttribute("aria-label")) ||
      (measureEl && measureEl.getAttribute("title")) ||
      (measureEl && measureEl.textContent) ||
      (el && el.textContent) ||
      ""
    ).trim();
  }

  function resolveTooltipBaseMaxWidth(el) {
    if (!el || !el.getAttribute) return 360;

    var size = el.getAttribute("data-tip-size") || "";
    if (size === "wide") return 480;
    if (size === "narrow") return 220;
    return 360;
  }

  function resolveTooltipBoundaryRect(el) {
    var viewportWidth = Math.max(
      document.documentElement ? document.documentElement.clientWidth : 0,
      window.innerWidth || 0
    );
    var viewportHeight = Math.max(
      document.documentElement ? document.documentElement.clientHeight : 0,
      window.innerHeight || 0
    );
    var viewportRect = {
      left: TOOLTIP_VIEWPORT_MARGIN,
      right: viewportWidth - TOOLTIP_VIEWPORT_MARGIN,
      top: TOOLTIP_VIEWPORT_MARGIN,
      bottom: viewportHeight - TOOLTIP_VIEWPORT_MARGIN,
    };

    if (!el || !el.closest) return viewportRect;

    var container = el.closest(".utility-tool-table-wrap");
    if (!container || !container.getBoundingClientRect) return viewportRect;

    var rect = container.getBoundingClientRect();
    return {
      left: Math.max(viewportRect.left, rect.left + TOOLTIP_VIEWPORT_MARGIN),
      right: Math.min(viewportRect.right, rect.right - TOOLTIP_VIEWPORT_MARGIN),
      top: viewportRect.top,
      bottom: viewportRect.bottom,
    };
  }

  function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
  }

  function setTooltipMaxWidth(target, maxWidth) {
    if (!target || !target.style) return;
    target.style.setProperty("--pb-tooltip-max-width", Math.max(0, maxWidth) + "px");
  }

  function clearTooltipMaxWidth(target) {
    if (!target || !target.style) return;
    target.style.removeProperty("--pb-tooltip-max-width");
  }

  function resolveTooltipDirectionalMaxWidth(position, rect, boundary, baseMaxWidth) {
    var availableWidth = boundary.right - boundary.left;

    if (position === "left") {
      availableWidth = rect.left - boundary.left - TOOLTIP_GAP;
    } else if (position === "right") {
      availableWidth = boundary.right - rect.right - TOOLTIP_GAP;
    }

    return Math.min(baseMaxWidth, Math.max(0, availableWidth));
  }

  function resolveTooltipPosition(preferred, rect, bubbleRect, boundary) {
    var topSpace = rect.top - boundary.top;
    var bottomSpace = boundary.bottom - rect.bottom;
    var leftSpace = rect.left - boundary.left;
    var rightSpace = boundary.right - rect.right;
    var needsHeight = bubbleRect.height + TOOLTIP_GAP;
    var needsWidth = bubbleRect.width + TOOLTIP_GAP;

    var canTop = topSpace >= needsHeight;
    var canBottom = bottomSpace >= needsHeight;
    var canLeft = leftSpace >= needsWidth;
    var canRight = rightSpace >= needsWidth;

    if (preferred === "left") {
      return canLeft ? "left" : canRight ? "right" : canTop ? "top" : "bottom";
    }
    if (preferred === "right") {
      return canRight ? "right" : canLeft ? "left" : canTop ? "top" : "bottom";
    }
    if (preferred === "bottom") {
      return canBottom ? "bottom" : canTop ? "top" : canRight ? "right" : "left";
    }
    return canTop ? "top" : canBottom ? "bottom" : canRight ? "right" : "left";
  }

  function resolveTooltipCoords(position, rect, bubbleRect, boundary) {
    var left = rect.left;
    var top = rect.top - bubbleRect.height - TOOLTIP_GAP;

    if (position === "bottom") {
      top = rect.bottom + TOOLTIP_GAP;
    } else if (position === "left") {
      left = rect.left - bubbleRect.width - TOOLTIP_GAP;
      top = rect.top + rect.height / 2 - bubbleRect.height / 2;
    } else if (position === "right") {
      left = rect.right + TOOLTIP_GAP;
      top = rect.top + rect.height / 2 - bubbleRect.height / 2;
    } else {
      left = rect.left + rect.width / 2 - bubbleRect.width / 2;
    }

    return {
      left: clamp(left, boundary.left, Math.max(boundary.left, boundary.right - bubbleRect.width)),
      top: clamp(top, boundary.top, Math.max(boundary.top, boundary.bottom - bubbleRect.height)),
    };
  }

  function hideTooltip() {
    clearTooltipMaxWidth(activeTooltipEl);

    if (!tooltipHost || !tooltipBubble) {
      activeTooltipEl = null;
      return;
    }

    delete tooltipHost.dataset.visible;
    tooltipBubble.hidden = true;
    tooltipBubble.textContent = "";
    tooltipBubble.style.visibility = "";
    tooltipBubble.style.removeProperty("--pb-tooltip-max-width");
    activeTooltipEl = null;
  }

  function dismissTooltip() {
    var focused = document.activeElement;
    if (
      focused &&
      focused !== document.body &&
      (findTooltipTarget(focused) || findAutoTooltipTarget(focused)) &&
      typeof focused.blur === "function"
    ) {
      focused.blur();
    }

    if (activeAutoEl) {
      deactivateAutoTooltip(activeAutoEl);
    }
    hideTooltip();
  }

  function positionTooltip(el) {
    if (!el || !el.getBoundingClientRect || !ensureTooltipHost()) return;

    var text = resolveTooltipText(el);
    if (!text) {
      hideTooltip();
      return;
    }

    var rect = el.getBoundingClientRect();
    var boundary = resolveTooltipBoundaryRect(el);
    var preferred = ((el.getAttribute && el.getAttribute("data-tip-pos")) || "top").toLowerCase();
    var baseMaxWidth = resolveTooltipBaseMaxWidth(el);
    var maxWidth = resolveTooltipDirectionalMaxWidth(preferred, rect, boundary, baseMaxWidth);

    tooltipBubble.hidden = false;
    tooltipBubble.textContent = text;
    setTooltipMaxWidth(el, maxWidth);
    setTooltipMaxWidth(tooltipBubble, maxWidth);
    tooltipBubble.style.visibility = "hidden";
    tooltipHost.dataset.visible = "true";

    var bubbleRect = tooltipBubble.getBoundingClientRect();
    var position = resolveTooltipPosition(preferred, rect, bubbleRect, boundary);
    var positionedMaxWidth = resolveTooltipDirectionalMaxWidth(
      position,
      rect,
      boundary,
      baseMaxWidth
    );

    if (positionedMaxWidth !== maxWidth) {
      setTooltipMaxWidth(tooltipBubble, positionedMaxWidth);
      bubbleRect = tooltipBubble.getBoundingClientRect();
    }

    var coords = resolveTooltipCoords(position, rect, bubbleRect, boundary);

    tooltipBubble.style.left = coords.left + "px";
    tooltipBubble.style.top = coords.top + "px";
    tooltipBubble.style.visibility = "";
  }

  function showTooltip(el) {
    if (!el) {
      hideTooltip();
      return;
    }

    activeTooltipEl = el;
    positionTooltip(el);
  }

  function refreshTooltip() {
    if (!activeTooltipEl) return;
    if (!activeTooltipEl.isConnected || !activeTooltipEl.hasAttribute("data-tip")) {
      hideTooltip();
      return;
    }
    positionTooltip(activeTooltipEl);
  }

  function activateAutoTooltip(el) {
    if (!el || activeAutoEl === el) return;
    deactivateAutoTooltip(activeAutoEl);

    if (!isActuallyTruncated(el)) return;

    var text = resolveTooltipText(el);
    if (!text) return;

    el.dataset.tooltipAutoHadTip = el.hasAttribute("data-tip") ? "1" : "0";
    el.dataset.tooltipAutoPrevTip = el.getAttribute("data-tip") || "";
    el.dataset.tooltipAutoHadTitle = el.hasAttribute("title") ? "1" : "0";
    el.dataset.tooltipAutoPrevTitle = el.getAttribute("title") || "";

    // Suppress the native browser tooltip while the shared tooltip is active.
    if (el.hasAttribute("title")) {
      el.removeAttribute("title");
    }

    el.setAttribute("data-tip", text);
    activeAutoEl = el;
  }

  function deactivateAutoTooltip(el) {
    if (!el) return;

    clearTooltipMaxWidth(el);

    if (el.dataset.tooltipAutoHadTip === "1") {
      el.setAttribute("data-tip", el.dataset.tooltipAutoPrevTip || "");
    } else {
      el.removeAttribute("data-tip");
    }

    if (el.dataset.tooltipAutoHadTitle === "1") {
      el.setAttribute("title", el.dataset.tooltipAutoPrevTitle || "");
    }

    delete el.dataset.tooltipAutoHadTip;
    delete el.dataset.tooltipAutoPrevTip;
    delete el.dataset.tooltipAutoHadTitle;
    delete el.dataset.tooltipAutoPrevTitle;

    if (activeAutoEl === el) {
      activeAutoEl = null;
    }
  }

  document.addEventListener("mouseover", function (evt) {
    var autoTarget = findAutoTooltipTarget(evt.target);
    if (autoTarget) {
      activateAutoTooltip(autoTarget);
    }

    var tooltipTarget = findTooltipTarget(evt.target);
    if (!tooltipTarget && autoTarget && autoTarget.hasAttribute("data-tip")) {
      tooltipTarget = autoTarget;
    }
    if (tooltipTarget) {
      showTooltip(tooltipTarget);
    }
  });

  document.addEventListener("mouseout", function (evt) {
    var related = evt.relatedTarget;
    var autoTarget = findAutoTooltipTarget(evt.target);
    if (autoTarget) {
      if (!(related && autoTarget.contains(related))) {
        deactivateAutoTooltip(autoTarget);
      }
    }

    var tooltipTarget = findTooltipTarget(evt.target) || autoTarget;
    if (!tooltipTarget) return;
    if (related && tooltipTarget.contains(related)) return;

    hideTooltip();
  });

  document.addEventListener("focusin", function (evt) {
    var autoTarget = findAutoTooltipTarget(evt.target);
    if (autoTarget) {
      activateAutoTooltip(autoTarget);
    }

    var tooltipTarget = findTooltipTarget(evt.target);
    if (!tooltipTarget && autoTarget && autoTarget.hasAttribute("data-tip")) {
      tooltipTarget = autoTarget;
    }
    if (tooltipTarget) {
      showTooltip(tooltipTarget);
    }
  });

  document.addEventListener("focusout", function (evt) {
    var related = evt.relatedTarget;
    var autoTarget = findAutoTooltipTarget(evt.target);
    if (autoTarget) {
      if (!(related && autoTarget.contains(related))) {
        deactivateAutoTooltip(autoTarget);
      }
    }

    var tooltipTarget = findTooltipTarget(evt.target) || autoTarget;
    if (!tooltipTarget) return;
    if (related && tooltipTarget.contains(related)) return;

    hideTooltip();
  });

  document.addEventListener(
    "scroll",
    function () {
      refreshTooltip();
    },
    true
  );

  window.addEventListener("resize", function () {
    if (activeAutoEl && !isActuallyTruncated(activeAutoEl)) {
      deactivateAutoTooltip(activeAutoEl);
    }
    refreshTooltip();
  });

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      dismissTooltip();
    }
  });

  window.addEventListener("blur", function () {
    dismissTooltip();
  });

  window.addEventListener("keydown", function (evt) {
    if (evt.key === "Escape") {
      hideTooltip();
    }
  });

  window.pbHideTooltip = dismissTooltip;
})();

/** Show a loading skeleton in the issue search results panel. */
function showSearchLoading() {
  var panel = document.getElementById("issue-search-panel");
  if (panel) {
    panel.innerHTML =
      '<div class="rounded-xl bg-pb-card border border-pb-border p-8 text-center space-y-3">' +
      '<svg class="w-8 h-8 mx-auto text-pb-interactive animate-spin" fill="none" viewBox="0 0 24 24">' +
      '<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>' +
      '<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>' +
      "</svg>" +
      '<p class="text-sm text-pb-text-sec">Searching indexers\u2026</p>' +
      '<p class="text-xs text-pb-text-dim">This may take up to 30 seconds depending on your indexer configuration.</p>' +
      "</div>";
  }
}

function readCsrfTokenFromBody() {
  try {
    var raw = document.body.getAttribute("hx-headers");
    return raw ? JSON.parse(raw)["X-CSRF-Token"] || "" : "";
  } catch (_) {
    return "";
  }
}

function readingStateActions() {
  return {
    busy: false,
    readingMenuOpen: false,
    statusMessage: "",
    statusIsError: false,

    issueId: function () {
      return this.$root.getAttribute("data-reading-issue-id") || "";
    },

    setCompletion: function (_button, completed) {
      return this.updateReadingState("completion", { completed: completed });
    },

    setWantToRead: function (_button, wantToRead) {
      return this.updateReadingState("want-to-read", { want_to_read: wantToRead });
    },

    updateReadingState: async function (action, payload) {
      if (this.busy || !this.issueId()) {
        return;
      }

      this.busy = true;
      this.statusMessage = "Saving…";
      this.statusIsError = false;
      try {
        var response = await fetch(
          "/api/v1/reader/issues/" + this.issueId() + "/" + action,
          {
            method: "PUT",
            credentials: "same-origin",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": readCsrfTokenFromBody(),
            },
            body: JSON.stringify(payload),
          }
        );
        if (!response.ok) {
          throw new Error("reading-state-update-failed");
        }
        this.statusMessage = "Saved";
        await this.refreshReadingSurface();
      } catch (_error) {
        this.statusMessage = "That reading update didn’t save. Try again.";
        this.statusIsError = true;
      } finally {
        this.busy = false;
      }
    },

    refreshReadingSurface: function () {
      var root = this.$root.closest("[data-reading-refresh-root]");
      if (!root || !window.htmx) {
        return Promise.resolve();
      }
      var url = root.getAttribute("data-reading-refresh-url");
      if (!url) {
        return Promise.resolve();
      }
      return window.htmx.ajax("GET", url, {
        target: root,
        swap: "outerHTML",
      });
    },
  };
}

function resolveHtmxSwapTarget(target) {
  if (!target) {
    return null;
  }
  if (typeof target === "string") {
    return document.querySelector(target);
  }
  return target && target.nodeType === Node.ELEMENT_NODE ? target : null;
}

function performHtmxSwap(method, url, options) {
  if (typeof htmx === "undefined") {
    return Promise.reject(new Error("HTMX unavailable"));
  }

  var requestOptions = options || {};
  var target = resolveHtmxSwapTarget(requestOptions.target);
  var targetSelector = typeof requestOptions.target === "string" ? requestOptions.target : "";

  return new Promise(function (resolve, reject) {
    var settled = false;
    var timeoutMs =
      typeof requestOptions.timeoutMs === "number" && requestOptions.timeoutMs > 0
        ? requestOptions.timeoutMs
        : 10000;
    var timeoutId = window.setTimeout(function () {
      if (settled) {
        return;
      }
      settled = true;
      cleanup();
      reject(new Error("HTMX swap timed out"));
    }, timeoutMs);

    function cleanup() {
      window.clearTimeout(timeoutId);
      document.removeEventListener("htmx:afterSwap", handleAfterSwap);
      document.removeEventListener("htmx:responseError", handleResponseError);
    }

    function matchesEventTarget(eventTarget) {
      if (!eventTarget) {
        return false;
      }
      if (target && eventTarget === target) {
        return true;
      }
      if (target && target.id && eventTarget.id && target.id === eventTarget.id) {
        return true;
      }
      if (targetSelector && eventTarget.matches && eventTarget.matches(targetSelector)) {
        return true;
      }
      return false;
    }

    function handleAfterSwap(evt) {
      if (!matchesEventTarget(evt && evt.detail && evt.detail.target)) {
        return;
      }
      settled = true;
      cleanup();
      resolve(evt.detail.target);
    }

    function handleResponseError(evt) {
      var eventTarget = (evt && evt.detail && (evt.detail.elt || evt.detail.target)) || null;
      if (!matchesEventTarget(eventTarget)) {
        return;
      }
      settled = true;
      cleanup();
      reject(new Error("HTMX request failed"));
    }

    document.addEventListener("htmx:afterSwap", handleAfterSwap);
    document.addEventListener("htmx:responseError", handleResponseError);

    try {
      htmx.ajax(method, url, requestOptions);
    } catch (err) {
      cleanup();
      reject(err);
    }
  });
}

function searchHistoryExpansionSet() {
  if (!(window._searchHistExpanded instanceof Set)) {
    window._searchHistExpanded = new Set();
  }
  return window._searchHistExpanded;
}

function searchHistoryRowData(config) {
  var cfg = config || {};
  return {
    detailLoaded: false,
    detailLoading: false,
    detailTarget: cfg.detailTarget,
    detailUrl: cfg.detailUrl,
    expanded: false,
    logId: cfg.logId,

    init: function () {
      this.expanded = searchHistoryExpansionSet().has(this.logId);
      if (this.expanded) {
        this.loadDetail();
      }
    },

    toggle: function () {
      var expandedRows = searchHistoryExpansionSet();
      this.expanded = !this.expanded;
      if (this.expanded) {
        expandedRows.add(this.logId);
        this.loadDetail();
      } else {
        expandedRows.delete(this.logId);
      }
    },

    loadDetail: function () {
      var self = this;
      if (self.detailLoaded || !self.detailUrl || !self.detailTarget) {
        return;
      }
      self.detailLoaded = true;
      self.detailLoading = true;
      this.$nextTick(function () {
        if (
          !window.htmx ||
          typeof performHtmxSwap !== "function" ||
          !document.querySelector(self.detailTarget)
        ) {
          self.detailLoaded = false;
          self.detailLoading = false;
          return;
        }
        performHtmxSwap("GET", self.detailUrl, {
          target: self.detailTarget,
          swap: "outerHTML",
        }).catch(function () {
          self.detailLoaded = false;
          self.detailLoading = false;
        });
      });
    },
  };
}

window.searchHistoryRowData = searchHistoryRowData;

function readImportReviewStatusCounts(shell) {
  if (!shell || typeof shell.getAttribute !== "function") {
    return {};
  }
  var raw = shell.getAttribute("data-import-review-status-counts") || "{}";
  try {
    var parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (_) {
    return {};
  }
}

function importReviewStatusCount(shell, view) {
  var counts = readImportReviewStatusCounts(shell);
  return Number(counts && counts[view]) || 0;
}

function importReviewShellHasSeriesBucket(shell, seriesId, bucket) {
  var numericSeriesId = Number(seriesId);
  if (!shell || !Number.isFinite(numericSeriesId) || !bucket) {
    return false;
  }
  var row = shell.querySelector(
    '[data-import-review-series-row="' + String(numericSeriesId) + '"]',
  );
  if (!row) {
    return false;
  }
  var buckets = String(row.getAttribute("data-import-review-row-buckets") || "")
    .split(/\s+/)
    .filter(Boolean);
  return buckets.indexOf(bucket) >= 0;
}

function loadImportReviewShell(url) {
  var shell = document.getElementById("import-step-review-shell");
  if (!shell || !url) {
    return Promise.resolve(null);
  }

  window.__pbImportReviewShellRequestToken =
    (Number(window.__pbImportReviewShellRequestToken) || 0) + 1;
  var requestToken = window.__pbImportReviewShellRequestToken;
  shell.classList.add("htmx-request");

  return fetch(url, {
    method: "GET",
    headers: {
      "X-Requested-With": "XMLHttpRequest",
    },
  })
    .then(function (response) {
      if (!response.ok) {
        throw new Error("Unable to refresh import review.");
      }
      return response.text();
    })
    .then(function (html) {
      if (requestToken !== window.__pbImportReviewShellRequestToken) {
        return null;
      }

      var currentShell = document.getElementById("import-step-review-shell");
      if (!currentShell) {
        return null;
      }

      var wrapper = document.createElement("div");
      wrapper.innerHTML = html.trim();
      var nextShell = wrapper.firstElementChild;
      if (!nextShell || nextShell.id !== "import-step-review-shell") {
        throw new Error("Import review refresh returned an unexpected response.");
      }

      destroyAlpineTree(currentShell);
      currentShell.replaceWith(nextShell);

      if (window.htmx && typeof window.htmx.process === "function") {
        window.htmx.process(nextShell);
      }
      if (window.Alpine) {
        Alpine.initTree(nextShell);
      }
      _syncFooterDockFromResponse(html);
      seedSearchFieldStates(nextShell);
      return nextShell;
    })
    .finally(function () {
      var activeShell = document.getElementById("import-step-review-shell");
      if (activeShell && requestToken === window.__pbImportReviewShellRequestToken) {
        activeShell.classList.remove("htmx-request");
      }
    });
}

function pbFormatDurationMs(value) {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "";
  }
  if (value < 1) {
    return "<1ms";
  }
  if (value < 1000) {
    return Math.round(value) + "ms";
  }
  return (value / 1000).toFixed(1) + "s";
}

window.pbFormatDurationMs = pbFormatDurationMs;

function settingsPage(config) {
  var cfg = config || {};
  return {
    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },

    saveConfig: function (formEl) {
      var formData = new FormData(formEl);
      var values = {};
      formData.forEach(function (value, key) {
        values[key] = value;
      });

      return fetch("/api/v1/config", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": this.csrfToken(),
        },
        body: JSON.stringify({ values: values }),
      })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("Failed to save settings.");
          }
          if (typeof showToast === "function") {
            showToast({ message: "Settings saved.", level: "success" });
          }
        })
        .catch(function (err) {
          if (typeof showToast === "function") {
            showToast({ message: err.message, level: "error" });
          }
          throw err;
        });
    },
  };
}

function whatsNewRefreshControl(config) {
  var cfg = config || {};
  return {
    refreshing: false,
    refreshMessage: "",
    reloadPending: false,

    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },

    dispatchToast: function (message, level) {
      if (typeof showToast === "function") {
        showToast({ message: message, level: level });
      }
    },

    staleEndpoints: function () {
      var endpoints = [];
      if (cfg.currentStale) {
        endpoints.push("/api/v1/whats-new");
      }
      if (cfg.upcomingStale) {
        endpoints.push("/api/v1/whats-new?upcoming=true");
      }
      return endpoints;
    },

    responseMessage: async function (response, fallback) {
      try {
        var body = await response.json();
        if (body && body.error && body.error.message) {
          return body.error.message;
        }
        if (body && body.message) {
          return body.message;
        }
      } catch (_) {
        // The fallback remains more useful than a JSON parsing error.
      }
      return fallback;
    },

    staleScopesAreFresh: async function () {
      var endpoints = this.staleEndpoints();
      if (endpoints.length === 0) {
        return true;
      }
      var responses = await Promise.all(
        endpoints.map(function (endpoint) {
          return fetch(endpoint, {
            headers: {
              Accept: "application/json",
              "Cache-Control": "no-store",
            },
            cache: "no-store",
          });
        })
      );
      for (var i = 0; i < responses.length; i += 1) {
        if (!responses[i].ok) {
          return false;
        }
        var payload = await responses[i].json();
        if (!payload.cache || payload.cache.stale) {
          return false;
        }
      }
      return true;
    },

    waitForFreshData: async function () {
      var maxAttempts = Number(cfg.maxAttempts || 30);
      var pollIntervalMs = Number(cfg.pollIntervalMs || 2000);
      for (var attempt = 0; attempt < maxAttempts; attempt += 1) {
        await new Promise(function (resolve) {
          window.setTimeout(resolve, attempt === 0 ? 500 : pollIntervalMs);
        });
        try {
          if (await this.staleScopesAreFresh()) {
            return true;
          }
        } catch (_) {
          // A transient polling failure should not interrupt the queued refresh.
        }
      }
      return false;
    },

    refreshNow: async function () {
      if (this.refreshing) {
        return;
      }
      this.refreshing = true;
      this.reloadPending = false;
      this.refreshMessage = "Requesting release refresh...";
      try {
        var response = await fetch("/api/v1/whats-new/refresh", {
          method: "POST",
          headers: { "X-CSRF-Token": this.csrfToken() },
        });
        if (response.status !== 202 && response.status !== 409) {
          throw new Error(
            await this.responseMessage(response, "Release data refresh could not be started.")
          );
        }

        this.refreshMessage =
          response.status === 409
            ? "A release refresh is already running..."
            : "Refreshing release data...";
        if (!(await this.waitForFreshData())) {
          this.refreshMessage =
            "The refresh is taking longer than expected. You can try again.";
          this.dispatchToast(this.refreshMessage, "warning");
          return;
        }

        this.reloadPending = true;
        this.refreshMessage = "Release data refreshed.";
        this.dispatchToast(this.refreshMessage, "success");
        window.location.reload();
      } catch (err) {
        this.refreshMessage =
          err && err.message ? err.message : "Release data refresh could not be started.";
        this.dispatchToast(this.refreshMessage, "error");
      } finally {
        if (!this.reloadPending) {
          this.refreshing = false;
        }
      }
    },
  };
}

function healthPage(config) {
  var cfg = config || {};
  return {
    refreshing: false,

    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },

    dispatchToast: function (message, level) {
      if (typeof showToast === "function") {
        showToast({ message: message, level: level });
      }
    },

    currentPath: function () {
      return window.location.pathname + window.location.search;
    },

    currentComponentKey: function () {
      if (cfg.componentKey) {
        return cfg.componentKey;
      }
      var match = window.location.pathname.match(/^\/health\/([^/?#]+)/);
      return match ? decodeURIComponent(match[1]) : "";
    },

    currentSubjectKey: function () {
      if (cfg.subjectKey) {
        return cfg.subjectKey;
      }
      var match = window.location.pathname.match(/^\/health\/[^/?#]+\/([^/?#]+)/);
      return match ? decodeURIComponent(match[1]) : "";
    },

    refreshEndpoint: function () {
      var componentKey = this.currentComponentKey();
      if (componentKey) {
        return "/api/v1/health/" + encodeURIComponent(componentKey) + "/refresh";
      }
      return "/api/v1/health/refresh";
    },

    refreshDetail: function (path, replaceUrl) {
      var target = document.getElementById("health-component-status-region");
      var nextPath = path || this.currentPath();
      if (!target || typeof htmx === "undefined") {
        window.location.assign(nextPath);
        return;
      }

      var nextUrl = new URL(nextPath, window.location.origin);
      var partialPath = nextUrl.pathname.endsWith("/status")
        ? nextUrl.pathname + nextUrl.search
        : nextUrl.pathname + "/status" + nextUrl.search;

      htmx.ajax("GET", partialPath, {
        target: "#".concat(target.id),
        swap: "outerHTML",
      });

      if (replaceUrl === true) {
        history.replaceState({}, "", nextUrl.pathname + nextUrl.search);
      }
    },

    refreshNow: async function () {
      this.refreshing = true;
      try {
        var resp = await fetch(this.refreshEndpoint(), {
          method: "POST",
          headers: { "X-CSRF-Token": this.csrfToken() },
        });
        if (!resp.ok) {
          throw new Error("Health refresh failed.");
        }
        this.dispatchToast("Health checks completed.", "success");
        var regionIds = [
          "health-status-region",
          "health-component-status-region",
          "health-download-clients-status-region",
          "health-indexers-status-region",
        ];
        for (var i = 0; i < regionIds.length; i += 1) {
          var region = document.getElementById(regionIds[i]);
          if (region) {
            htmx.trigger(region, "refresh");
          }
        }
      } catch (err) {
        this.dispatchToast(
          err && err.message ? err.message : "Health refresh failed.",
          "error"
        );
      } finally {
        this.refreshing = false;
      }
    },

    clearHistory: function (btn) {
      var self = this;
      var componentKey = this.currentComponentKey();
      if (!componentKey) {
        self.dispatchToast("Unable to determine which health history to clear.", "error");
        return;
      }

      pbConfirm({
        title: "Clear Health History",
        message:
          this.currentSubjectKey()
            ? "This will permanently delete all recorded history rows for this health subject."
            : "This will permanently delete all recorded history rows for this health component.",
        confirmText: "Clear History",
      }).then(function (ok) {
        if (!ok) return;
        btn.disabled = true;
        var subjectKey = self.currentSubjectKey();
        var url = "/api/v1/health/" + encodeURIComponent(componentKey) + "/history";
        if (subjectKey) {
          url += "?subject_key=" + encodeURIComponent(subjectKey);
        }
        fetch(url, {
          method: "DELETE",
          headers: { "X-CSRF-Token": self.csrfToken() },
        })
          .then(function (res) {
            if (!res.ok) {
              throw new Error("Failed to clear history.");
            }
            return res.json();
          })
          .then(function (data) {
            self.dispatchToast(
              "Cleared " + data.deleted + " history record" + (data.deleted !== 1 ? "s" : "") + ".",
              "success"
            );
            var subjectKey = self.currentSubjectKey();
            self.refreshDetail(
              subjectKey
                ? "/health/" + encodeURIComponent(componentKey) + "/" + encodeURIComponent(subjectKey)
                : "/health/" + encodeURIComponent(componentKey),
              true
            );
          })
          .catch(function (err) {
            btn.disabled = false;
            self.dispatchToast(err.message, "error");
          });
      });
    },
  };
}

function fileBrowserMixin(config) {
  var cfg = config || {};
  function basename(path) {
    if (!path || path === "/") return "/";
    var normalized = path.endsWith("/") && path.length > 1 ? path.slice(0, -1) : path;
    var lastSlash = normalized.lastIndexOf("/");
    return lastSlash >= 0 ? normalized.substring(lastSlash + 1) || "/" : normalized;
  }

  function getFileExtension(name) {
    if (!name) return "";
    var dot = name.lastIndexOf(".");
    return dot > 0 ? name.substring(dot + 1).toLowerCase() : "";
  }

  function isBrowsableAbsolutePath(path) {
    return !!path && path.trim().startsWith("/");
  }

  function looksLikeFilePath(path) {
    if (!path || path === "/") return false;
    return basename(path).indexOf(".") > 0;
  }

  function parentDirectory(path) {
    if (!path || path === "/") return "/";
    var normalized = path.endsWith("/") && path.length > 1 ? path.slice(0, -1) : path;
    var lastSlash = normalized.lastIndexOf("/");
    if (lastSlash <= 0) return "/";
    return normalized.substring(0, lastSlash);
  }

  function isWithinAllowedRoots(path, allowedRoots) {
    if (!path || !allowedRoots || !allowedRoots.length) return true;
    for (var i = 0; i < allowedRoots.length; i++) {
      var root = allowedRoots[i];
      if (path === root || path.indexOf(root + "/") === 0) {
        return true;
      }
    }
    return false;
  }

  function defaultTitleForMode(mode) {
    if (mode === "directories") return "Select Folders";
    if (mode === "files") return "Select Files";
    if (mode === "file") return "Browse Files";
    return "Browse Directories";
  }

  function defaultEmptyMessageForMode(mode) {
    return mode === "directory" || mode === "directories"
      ? "No subdirectories"
      : "No matching files or subdirectories";
  }

  function defaultConfirmLabelForMode(mode) {
    if (mode === "directories") return "Add Folders";
    return mode === "directory" ? "Select This Directory" : "Add Files";
  }

  return {
    fileBrowser: {
      show: false,
      path: "/",
      parent: null,
      dirs: [],
      files: [],
      quickLinks: [],
      loading: false,
      error: "",
      targetField: "",
      selectionMode: "directory",
      title: defaultTitleForMode("directory"),
      emptyMessage: defaultEmptyMessageForMode("directory"),
      confirmLabel: defaultConfirmLabelForMode("directory"),
      onSelectAction: "",
      fileMode: false,
      fileExtensions: "",
      multiSelect: false,
      _multiSelected: new Set(),
      allowedRoots: [],
    },

    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },

    openFileBrowser: function (fieldName, currentValue, opts) {
      var options = opts || {};
      var selectionMode =
        options.selectionMode || (options.multiSelect ? "files" : options.fileMode ? "file" : "directory");
      var trimmedCurrentValue = currentValue && currentValue.trim ? currentValue.trim() : "";
      var fallbackStartPath = options.startPath && options.startPath.trim ? options.startPath.trim() : "";
      var allowedRoots = Array.isArray(options.allowedRoots)
        ? options.allowedRoots.filter(function (root) { return isBrowsableAbsolutePath(root); })
        : [];
      var startPath =
        isBrowsableAbsolutePath(trimmedCurrentValue)
          ? trimmedCurrentValue
          : isBrowsableAbsolutePath(fallbackStartPath)
            ? fallbackStartPath
            : allowedRoots.length > 0
              ? allowedRoots[0]
              : "/";
      this.fileBrowser.targetField = fieldName;
      this.fileBrowser.error = "";
      this.fileBrowser.selectionMode = selectionMode;
      this.fileBrowser.title = options.title || defaultTitleForMode(selectionMode);
      this.fileBrowser.emptyMessage = options.emptyMessage || defaultEmptyMessageForMode(selectionMode);
      this.fileBrowser.confirmLabel = options.confirmLabel || defaultConfirmLabelForMode(selectionMode);
      this.fileBrowser.onSelectAction = options.onSelectAction || "";
      this.fileBrowser.fileMode = selectionMode === "file" || selectionMode === "files";
      this.fileBrowser.fileExtensions = options.extensions || "";
      this.fileBrowser.multiSelect = selectionMode === "files" || selectionMode === "directories";
      this.fileBrowser._multiSelected = new Set();
      this.fileBrowser.allowedRoots = allowedRoots;
      this.fileBrowser.files = [];
      this.fileBrowser.show = true;
      if (
        this.fileBrowser.fileMode &&
        isBrowsableAbsolutePath(trimmedCurrentValue) &&
        looksLikeFilePath(startPath)
      ) {
        startPath = parentDirectory(startPath);
      }
      if (!isWithinAllowedRoots(startPath, allowedRoots) && allowedRoots.length > 0) {
        startPath = allowedRoots[0];
      }
      this.navigateTo(startPath);
    },

    navigateTo: function (path) {
      var self = this;
      self.fileBrowser.loading = true;
      self.fileBrowser.error = "";

      var url;
      if (self.fileBrowser.fileMode) {
        url = "/api/v1/filesystem/browse?path=" + encodeURIComponent(path);
        if (self.fileBrowser.allowedRoots && self.fileBrowser.allowedRoots.length > 0) {
          url += "&roots=" + encodeURIComponent(self.fileBrowser.allowedRoots.join(","));
        }
        if (self.fileBrowser.fileExtensions) {
          url += "&extensions=" + encodeURIComponent(self.fileBrowser.fileExtensions);
        }
      } else {
        url = "/api/v1/filesystem/directories?path=" + encodeURIComponent(path);
        if (self.fileBrowser.allowedRoots && self.fileBrowser.allowedRoots.length > 0) {
          url += "&roots=" + encodeURIComponent(self.fileBrowser.allowedRoots.join(","));
        }
      }

      fetch(url, {
        headers: { "X-CSRF-Token": self.csrfToken() },
      })
        .then(function (response) {
          if (!response.ok) throw new Error("Failed to browse directory");
          return response.json();
        })
        .then(function (data) {
          self.fileBrowser.path = data.path;
          self.fileBrowser.parent = data.parent;
          self.fileBrowser.dirs = data.directories;
          self.fileBrowser.files = data.files || [];
          self.fileBrowser.quickLinks = data.quick_links || [];
        })
        .catch(function (err) {
          self.fileBrowser.error = err.message;
        })
        .finally(function () {
          self.fileBrowser.loading = false;
        });
    },

    applyFileBrowserSelection: function (selection) {
      var action = this.fileBrowser.onSelectAction;
      if (action && typeof this[action] === "function") {
        this[action](selection);
        return;
      }

      var value = selection.mode === "files" || selection.mode === "directories" ? selection.paths : selection.path;
      if (this.fileBrowser.targetField && this.form) {
        this.form[this.fileBrowser.targetField] = value;
      } else if (this.fileBrowser.targetField) {
        this[this.fileBrowser.targetField] = value;
      }
    },

    selectDirectory: function () {
      this.applyFileBrowserSelection({
        mode: "directory",
        path: this.fileBrowser.path,
        name: basename(this.fileBrowser.path),
      });
      this.closeFileBrowser();
    },

    selectFile: function (file) {
      var filePath = typeof file === "string" ? file : file.path;
      var fileName = typeof file === "string" ? basename(filePath) : file.name || basename(filePath);
      this.applyFileBrowserSelection({
        mode: "file",
        path: filePath,
        name: fileName,
        size: typeof file === "object" && file ? file.size || 0 : 0,
        ext: getFileExtension(fileName),
      });
      this.closeFileBrowser();
    },

    toggleFileSelection: function (filePath) {
      if (this.fileBrowser._multiSelected.has(filePath)) {
        this.fileBrowser._multiSelected.delete(filePath);
      } else {
        this.fileBrowser._multiSelected.add(filePath);
      }
      this.fileBrowser._multiSelected = new Set(this.fileBrowser._multiSelected);
    },

    toggleDirectorySelection: function (dirPath) {
      if (this.fileBrowser._multiSelected.has(dirPath)) {
        this.fileBrowser._multiSelected.delete(dirPath);
      } else {
        this.fileBrowser._multiSelected.add(dirPath);
      }
      this.fileBrowser._multiSelected = new Set(this.fileBrowser._multiSelected);
    },

    toggleSelectAllFiles: function (select) {
      var files = this.fileBrowser.files || [];
      for (var i = 0; i < files.length; i++) {
        if (select) {
          this.fileBrowser._multiSelected.add(files[i].path);
        } else {
          this.fileBrowser._multiSelected.delete(files[i].path);
        }
      }
      this.fileBrowser._multiSelected = new Set(this.fileBrowser._multiSelected);
    },

    toggleSelectAllDirectories: function (select) {
      var dirs = this.fileBrowser.dirs || [];
      for (var i = 0; i < dirs.length; i++) {
        if (select) {
          this.fileBrowser._multiSelected.add(dirs[i].path);
        } else {
          this.fileBrowser._multiSelected.delete(dirs[i].path);
        }
      }
      this.fileBrowser._multiSelected = new Set(this.fileBrowser._multiSelected);
    },

    toggleSelectAllCurrentEntries: function (select) {
      if (this.fileBrowser.selectionMode === "directories") {
        this.toggleSelectAllDirectories(select);
        return;
      }
      this.toggleSelectAllFiles(select);
    },

    confirmMultiSelect: function () {
      if (this.fileBrowser.selectionMode === "directories") {
        var dirs = this.fileBrowser.dirs || [];
        var selectedDirPaths = Array.from(this.fileBrowser._multiSelected);
        var selectedDirectories = selectedDirPaths.map(function (dirPath) {
          var dirInfo = dirs.find(function (dir) {
            return dir.path === dirPath;
          });
          return {
            path: dirPath,
            name: dirInfo && dirInfo.name ? dirInfo.name : basename(dirPath),
          };
        });
        this.applyFileBrowserSelection({
          mode: "directories",
          paths: selectedDirPaths,
          directories: selectedDirectories,
        });
        this.closeFileBrowser();
        return;
      }

      var files = this.fileBrowser.files || [];
      var selectedPaths = Array.from(this.fileBrowser._multiSelected);
      var selectedFiles = selectedPaths.map(function (filePath) {
        var fileInfo = files.find(function (file) {
          return file.path === filePath;
        });
        var name = fileInfo && fileInfo.name ? fileInfo.name : basename(filePath);
        return {
          path: filePath,
          name: name,
          size: fileInfo ? fileInfo.size || 0 : 0,
          ext: getFileExtension(name),
        };
      });
      this.applyFileBrowserSelection({
        mode: "files",
        paths: selectedPaths,
        files: selectedFiles,
      });
      this.closeFileBrowser();
    },

    closeFileBrowser: function () {
      this.fileBrowser.show = false;
    },
  };
}

function dispatchImportWizardAdvance(detail) {
  window.dispatchEvent(new CustomEvent("wizard:advance", { detail: detail || {} }));
}

function importReviewAdvanceStorageKey(jobId) {
  return "pb-import-review-advanced:" + String(jobId || "");
}

function wasImportReviewAdvanced(jobId) {
  if (jobId == null) {
    return false;
  }
  try {
    return window.sessionStorage.getItem(importReviewAdvanceStorageKey(jobId)) === "1";
  } catch (_) {
    return false;
  }
}

function setImportReviewAdvanced(jobId, advanced) {
  if (jobId == null) {
    return;
  }
  try {
    if (advanced) {
      window.sessionStorage.setItem(importReviewAdvanceStorageKey(jobId), "1");
    } else {
      window.sessionStorage.removeItem(importReviewAdvanceStorageKey(jobId));
    }
  } catch (_) {
    // Ignore storage availability failures.
  }
}

function importReviewSelectionStorageKey(jobId) {
  return "pb-import-review-selection:" + String(jobId || "");
}

function normalizeImportReviewSelectionState(value) {
  if (Array.isArray(value)) {
    return {
      reviewToken: "",
      ids: normalizeImportReviewSelection(value),
    };
  }

  if (!value || typeof value !== "object") {
    return {
      reviewToken: "",
      ids: [],
    };
  }

  return {
    reviewToken: typeof value.reviewToken === "string" ? value.reviewToken : "",
    ids: normalizeImportReviewSelection(value.ids),
  };
}

function normalizeImportReviewSelection(ids) {
  if (!Array.isArray(ids)) {
    return [];
  }

  var seen = Object.create(null);
  var normalized = [];
  for (var i = 0; i < ids.length; i += 1) {
    var id = Number(ids[i]);
    if (!Number.isFinite(id)) {
      continue;
    }
    var key = String(id);
    if (seen[key]) {
      continue;
    }
    seen[key] = true;
    normalized.push(id);
  }
  return normalized;
}

function readImportReviewSelectionState(jobId) {
  return { reviewToken: "", ids: [] };
}

function readImportReviewSelection(jobId) {
  return readImportReviewSelectionState(jobId).ids;
}

function writeImportReviewSelection(jobId, ids, reviewToken) {
  void jobId;
  void ids;
  void reviewToken;
}

function clearImportReviewSelection(jobId) {
  void jobId;
}

function importReviewExpansionStorageKey(jobId) {
  return "pb-import-review-expanded:" + String(jobId || "");
}

function normalizeImportReviewExpandedRows(value) {
  if (Array.isArray(value)) {
    var rowsFromArray = {};
    for (var i = 0; i < value.length; i += 1) {
      var rowId = String(value[i] || "");
      if (rowId) {
        rowsFromArray[rowId] = true;
      }
    }
    return rowsFromArray;
  }

  if (!value || typeof value !== "object") {
    return {};
  }

  var rows = {};
  var keys = Object.keys(value);
  for (var j = 0; j < keys.length; j += 1) {
    if (value[keys[j]]) {
      rows[String(keys[j])] = true;
    }
  }
  return rows;
}

function readImportReviewExpandedRows(jobId) {
  if (jobId == null) {
    return {};
  }

  try {
    var raw = window.sessionStorage.getItem(importReviewExpansionStorageKey(jobId));
    return raw ? normalizeImportReviewExpandedRows(JSON.parse(raw)) : {};
  } catch (_) {
    return {};
  }
}

function writeImportReviewExpandedRows(jobId, expandedRows) {
  if (jobId == null) {
    return;
  }

  try {
    var rows = normalizeImportReviewExpandedRows(expandedRows);
    var rowIds = Object.keys(rows);
    if (rowIds.length === 0) {
      window.sessionStorage.removeItem(importReviewExpansionStorageKey(jobId));
      return;
    }
    window.sessionStorage.setItem(importReviewExpansionStorageKey(jobId), JSON.stringify(rowIds));
  } catch (_) {
    // Ignore storage availability failures.
  }
}

function isImportReviewRowExpanded(jobId, rowId) {
  if (rowId == null) {
    return false;
  }
  var expandedRows = readImportReviewExpandedRows(jobId);
  return expandedRows[String(rowId)] === true;
}

function setImportReviewRowExpanded(jobId, rowId, expanded) {
  if (rowId == null) {
    return;
  }

  var expandedRows = readImportReviewExpandedRows(jobId);
  var key = String(rowId);
  if (expanded) {
    expandedRows[key] = true;
  } else {
    delete expandedRows[key];
  }
  writeImportReviewExpandedRows(jobId, expandedRows);
}

function importReviewRowExpansionData(config) {
  var cfg = config || {};
  return {
    expanded: false,
    jobId: cfg.jobId,
    rowId: cfg.rowId,

    init: function () {
      this.expanded = isImportReviewRowExpanded(this.jobId, this.rowId);
    },

    toggle: function () {
      this.expanded = !this.expanded;
      setImportReviewRowExpanded(this.jobId, this.rowId, this.expanded);
    },
  };
}

window.importReviewRowExpansionData = importReviewRowExpansionData;

function readImportConflictCommitState(jobId) {
  function normalizeCommittedPages(pages) {
    if (!pages || typeof pages !== "object" || Array.isArray(pages)) {
      return {};
    }

    var normalizedPages = {};
    var pageKeys = Object.keys(pages);
    for (var i = 0; i < pageKeys.length; i += 1) {
      var pageKey = pageKeys[i];
      var pageState = pages[pageKey] || {};
      var groupIds = normalizeImportReviewSelection(pageState.groupIds);
      if (groupIds.length === 0) {
        continue;
      }
      normalizedPages[pageKey] = {
        groupIds: groupIds,
        seriesIds: normalizeImportReviewSelection(pageState.seriesIds),
        autoAddedSeriesIds: normalizeImportReviewSelection(pageState.autoAddedSeriesIds),
      };
    }
    return normalizedPages;
  }

  if (jobId == null) {
    return {
      committedPages: {},
    };
  }

  if (
    window._importConflictCommitState &&
    Object.prototype.hasOwnProperty.call(window._importConflictCommitState, jobId)
  ) {
    var cached = window._importConflictCommitState[jobId] || {};
    return {
      committedPages: normalizeCommittedPages(cached.committedPages),
    };
  }

  return {
    committedPages: {},
  };
}

function writeImportConflictCommitState(jobId, state) {
  if (jobId == null) {
    return;
  }

  if (!window._importConflictCommitState) {
    window._importConflictCommitState = {};
  }

  var next = state || {};
  window._importConflictCommitState[jobId] = {
    committedPages: readImportConflictCommitState(jobId).committedPages,
  };

  if (
    next.committedPages &&
    typeof next.committedPages === "object" &&
    !Array.isArray(next.committedPages)
  ) {
    window._importConflictCommitState[jobId].committedPages = next.committedPages;
  }
}

function clearImportConflictCommitState(jobId) {
  if (jobId == null) {
    return;
  }

  if (window._importConflictCommitState) {
    delete window._importConflictCommitState[jobId];
  }
}

function importCvSearchModalData(config) {
  var cfg = config || {};

  return {
    open: true,
    query: cfg.query || "",
    jobId: cfg.jobId,
    seriesId: cfg.seriesId,
    selecting: false,

    close: function (force) {
      if (this.selecting && !force) {
        return;
      }
      this.open = false;
      var modalHost = document.getElementById("cv-search-modal");
      if (modalHost) {
        modalHost.innerHTML = "";
      }
    },

    search: function () {
      if (typeof htmx === "undefined") {
        window.location.assign("/import");
        return;
      }

      openImportCvSearchLoadingModal({ query: this.query || "" });

      window.requestAnimationFrame(
        function () {
          htmx.ajax(
            "GET",
            "/import/" +
              this.jobId +
              "/series/" +
              this.seriesId +
              "/cv-search?q=" +
              encodeURIComponent(this.query || ""),
            {
              target: "#cv-search-modal",
              swap: "innerHTML",
            },
          );
        }.bind(this),
      );
    },

    refreshReview: function () {
      if (typeof htmx === "undefined") {
        window.location.assign("/import");
        return Promise.resolve();
      }

      var reviewData = this.reviewPanelData();
      if (reviewData && typeof reviewData.refreshSeriesReview === "function") {
        return reviewData.refreshSeriesReview();
      }

      return performHtmxSwap("GET", "/import/" + this.jobId + "/review-partial", {
        target: "#import-step-review-shell",
        swap: "outerHTML",
      });
    },

    collectionPageData: function () {
      var host = document.querySelector("[data-testid='import-collection-page']");
      if (!host) {
        return null;
      }

      try {
        if (window.Alpine && typeof window.Alpine.$data === "function") {
          return window.Alpine.$data(host);
        }
      } catch (_) {
        // fall through to the internal Alpine reference if available
      }

      return host.__x ? host.__x.$data : null;
    },

    reviewPanelData: function () {
      var host = document.querySelector("[data-testid='import-collection-review']");
      if (!host) {
        return null;
      }

      try {
        if (window.Alpine && typeof window.Alpine.$data === "function") {
          return window.Alpine.$data(host);
        }
      } catch (_) {
        // fall through to the internal Alpine reference if available
      }

      return host.__x ? host.__x.$data : null;
    },

    buildPreservedReviewUrl: function (activeReviewView) {
      var activeView = String(activeReviewView || "series");
      var root = document.getElementById("import-step-review-shell");
      var params = new URLSearchParams();
      var statusInput = root ? root.querySelector("input[name='review_status_filter']") : null;
      var sortInput = root ? root.querySelector("input[name='review_sort']") : null;
      var pageInput = root ? root.querySelector("input[name='review_page']") : null;
      var effectiveView = activeView;

      if (statusInput) {
        effectiveView = statusInput.value || "series";
      }
      if (effectiveView && effectiveView !== "series") {
        params.set("status", effectiveView);
      }
      if (sortInput && sortInput.value) {
        params.set("sort", sortInput.value);
      } else {
        params.set("sort", effectiveView === "conflicts" ? "series" : "confidence");
      }
      if (pageInput && pageInput.value) {
        params.set("page", pageInput.value);
      }

      var query = params.toString();
      return "/import/" + this.jobId + "/review-partial" + (query ? "?" + query : "");
    },

    buildOverrideOutcome: function (result, activeReviewView, baselineStatusCounts) {
      var activeView = activeReviewView || "series";
      var normalizedStatus = result && result.status ? String(result.status).toLowerCase() : "";
      var filesNoMatch = Number(result && result.files_no_match) || 0;
      var resultSeriesId = Number(result && result.id);
      var mergedIntoExistingSeries =
        !!result &&
        Number.isFinite(resultSeriesId) &&
        resultSeriesId !== Number(this.seriesId);
      var filesConflict = Number(result && result.files_conflict) || 0;
      var directlyImportable = normalizedStatus === "matched" && filesConflict === 0;
      var diagnostics =
        result && result.diagnostics && typeof result.diagnostics === "object"
          ? result.diagnostics
          : {};
      var rematchPending = diagnostics.rematch_pending === true;
      var hasKnownSeriesTarget =
        !!result && !!(result.cv_id || result.user_selected_cv_id || result.series_id);

      if (rematchPending) {
        return {
          message: "ComicVine match applied. Pullbox is rematching the files in the background.",
          level: "info",
          watchDestinationView: "needs_issue",
          watchSeriesId: Number.isFinite(resultSeriesId) ? resultSeriesId : Number(this.seriesId),
          baselineStatusCounts: baselineStatusCounts || {},
          watchDelays: [500, 1500, 3000, 5000, 10000, 15000, 30000, 60000],
        };
      }

      if (
        (normalizedStatus === "no_match" && hasKnownSeriesTarget) ||
        (normalizedStatus === "duplicate" && filesNoMatch > 0)
      ) {
        return {
          message:
            "ComicVine match applied. Some files still need issue decisions under Needs Issue Match.",
          level: "warning",
          destinationView: "needs_issue",
        };
      }

      if (normalizedStatus === "matched" && filesConflict > 0) {
        return {
          message:
            activeView === "conflicts"
              ? "ComicVine match updated. You are still on Conflicts so you can keep resolving this page."
              : "ComicVine match updated. This series now has file conflicts; use the Conflicts tab if you want to review them.",
          level: "warning",
        };
      }

      if (directlyImportable) {
        return {
          message: mergedIntoExistingSeries
            ? (
                activeView === "matched"
                  ? "ComicVine match merged into an existing matched series. You are still on this page."
                  : "ComicVine match merged into an existing matched series. You are still on your current page; use Matched when you want to review it."
              )
            : (
                activeView === "matched"
                  ? "ComicVine match updated. You are still on the Matched page."
                  : "ComicVine match updated. You are still on your current page; use Matched when you want to review it."
              ),
          level: "success",
        };
      }

      if (normalizedStatus === "duplicate") {
        return {
          message: mergedIntoExistingSeries
            ? (
                activeView === "duplicate"
                  ? "ComicVine match merged into an existing in-library series. You are still on this page."
                  : "ComicVine match merged into an existing in-library series. You are still on your current page; use In Library when you want to review it."
              )
            : (
                activeView === "duplicate"
                  ? "ComicVine match updated, but this series is already in your library. You are still on this page."
                  : "ComicVine match updated, but this series is already in your library. You are still on your current page; use In Library when you want to review it."
              ),
          level: "info",
        };
      }

      return {
        message: "ComicVine match updated.",
        level: "success",
      };
    },

    refreshReviewForOverride: function (outcome, activeReviewView) {
      if (typeof htmx === "undefined") {
        window.location.assign("/import");
        return Promise.resolve();
      }

      var reviewData = this.reviewPanelData();
      if (reviewData) {
        if (
          outcome &&
          outcome.destinationView &&
          typeof reviewData.openReviewView === "function"
        ) {
          return reviewData.openReviewView(outcome.destinationView);
        }
        if (
          activeReviewView &&
          Object.prototype.hasOwnProperty.call(reviewData, "currentView")
        ) {
          reviewData.currentView = activeReviewView;
        }
        if (reviewData && typeof reviewData.refreshSeriesReview === "function") {
          return reviewData.refreshSeriesReview();
        }
      }

      return performHtmxSwap("GET", this.buildPreservedReviewUrl(activeReviewView), {
        target: "#import-step-review-shell",
        swap: "outerHTML",
      });
    },

    overrideDestinationReady: function (outcome, shell) {
      if (!outcome || !outcome.watchDestinationView || !shell) {
        return false;
      }

      var view = outcome.watchDestinationView;
      if (importReviewShellHasSeriesBucket(shell, outcome.watchSeriesId, view)) {
        return true;
      }

      var baselineCounts = outcome.baselineStatusCounts || {};
      var baseline = Number(baselineCounts[view]) || 0;
      return importReviewStatusCount(shell, view) > baseline;
    },

    openWatchedOverrideDestination: function (outcome, shell) {
      var self = this;
      if (!self.overrideDestinationReady(outcome, shell)) {
        return Promise.resolve(false);
      }

      var reviewData = self.reviewPanelData();
      if (!reviewData || typeof reviewData.openReviewView !== "function") {
        return Promise.resolve(false);
      }

      return Promise.resolve(reviewData.openReviewView(outcome.watchDestinationView)).then(
        function () {
          return true;
        },
      );
    },

    watchOverrideReviewState: function (outcome) {
      var self = this;
      if (!outcome || !outcome.watchDestinationView) {
        return;
      }

      var delays = outcome.watchDelays || [];
      var attempt = function (index) {
        if (index >= delays.length) {
          return;
        }
        window.setTimeout(function () {
          var reviewData = self.reviewPanelData();
          if (!reviewData || typeof reviewData.refreshSeriesReview !== "function") {
            return;
          }

          Promise.resolve(reviewData.refreshSeriesReview())
            .then(function (shell) {
              return self.openWatchedOverrideDestination(outcome, shell);
            })
            .then(function (opened) {
              if (!opened) {
                attempt(index + 1);
              }
            })
            .catch(function () {
              attempt(index + 1);
            });
        }, delays[index]);
      };

      attempt(0);
    },

    selectResult: function (cvId) {
      var self = this;
      if (!cvId || self.selecting) {
        return;
      }

      self.selecting = true;
      var activeReviewView = null;
      var reviewData = self.reviewPanelData();
      if (reviewData && reviewData.currentView) {
        activeReviewView = reviewData.currentView;
      }
      var baselineStatusCounts = readImportReviewStatusCounts(
        document.getElementById("import-step-review-shell"),
      );
      fetch("/api/v1/import/" + self.jobId + "/series/" + self.seriesId + "/override", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": readCsrfTokenFromBody(),
        },
        body: JSON.stringify({ cv_id: cvId }),
      })
        .then(function (response) {
          if (!response.ok) {
            return response
              .json()
              .catch(function () {
                return {};
              })
              .then(function (error) {
                throw new Error(error.detail || "Failed to set override");
              });
          }

          return response
            .json()
            .catch(function () {
              return null;
            })
            .then(function (result) {
              var outcome = self.buildOverrideOutcome(
                result,
                activeReviewView,
                baselineStatusCounts,
              );
              self.close(true);
              var reviewRefresh = self.refreshReviewForOverride(outcome, activeReviewView);
              return Promise.resolve(reviewRefresh).then(function (reviewShell) {
                var refreshedReviewData = self.reviewPanelData();
                if (
                  refreshedReviewData &&
                  typeof refreshedReviewData.refreshReviewSummary === "function"
                ) {
                  refreshedReviewData.refreshReviewSummary();
                }
                if (typeof showToast === "function") {
                  showToast({
                    message: outcome.message,
                    level: outcome.level || "success",
                  });
                }
                return self.openWatchedOverrideDestination(outcome, reviewShell).then(
                  function (opened) {
                    if (!opened) {
                      self.watchOverrideReviewState(outcome);
                    }
                  },
                );
              });
            });
        })
        .catch(function (err) {
          var message = "Error: " + (err && err.message ? err.message : "Unable to update match.");
          if (typeof showToast === "function") {
            showToast({ message: message, level: "error" });
          } else {
            window.alert(message);
          }
        })
        .finally(function () {
          self.selecting = false;
        });
    },
  };
}

function renderImportCvSearchLoadingModal(config) {
  var cfg = config || {};
  var query = typeof cfg.query === "string" ? cfg.query : "";
  var label = query ? escapeHtml(query) : "this series";

  return (
    '<div class="modal-shell" ' +
    'data-testid="import-collection-cv-search-loading-modal">' +
    '<div class="modal-backdrop" ' +
    'onclick="closeImportCvSearchModal()"></div>' +
    '<div class="modal-panel max-w-lg">' +
    '<div class="modal-header">' +
    '<div class="space-y-1">' +
    '<p class="text-[11px] font-medium uppercase tracking-[0.14em] text-pb-brand">Series override</p>' +
    '<h3 class="text-base font-semibold text-pb-text">Search ComicVine</h3>' +
    '<p class="text-xs leading-5 text-pb-text-dim">Search for the correct series, then apply the match to this import entry.</p>' +
    "</div>" +
    '<button onclick="closeImportCvSearchModal()" class="icon-btn icon-btn-sm" aria-label="Close search dialog" type="button">' +
    '<svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
    '<path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/>' +
    "</svg>" +
    "</button>" +
    "</div>" +
    '<div class="modal-body">' +
    '<div class="px-5 py-5">' +
    '<div class="flex min-h-56 flex-col items-center justify-center rounded-xl border border-pb-border bg-pb-card-hover/45 px-6 py-10 text-center">' +
    '<svg class="h-8 w-8 animate-spin text-pb-interactive" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
    '<path stroke-linecap="round" stroke-linejoin="round" d="M21 12a9 9 0 1 1-6.219-8.56"/>' +
    "</svg>" +
    '<p class="mt-3 text-sm font-medium text-pb-text">Searching ComicVine...</p>' +
    '<p class="mt-1 text-xs leading-5 text-pb-text-dim">Looking up matches for <span class="font-medium text-pb-text">' +
    label +
    "</span>.</p>" +
    "</div>" +
    "</div>" +
    "</div>" +
    "</div>" +
    "</div>"
  );
}

function closeImportCvSearchModal() {
  var modalHost = document.getElementById("cv-search-modal");
  if (modalHost) {
    modalHost.innerHTML = "";
  }
}

function openImportCvSearchLoadingModal(config) {
  var modalHost = document.getElementById("cv-search-modal");
  if (!modalHost) {
    return;
  }

  modalHost.innerHTML = renderImportCvSearchLoadingModal(config);
}

function openImportCvSearchModal(config) {
  var cfg = config || {};
  if (typeof htmx === "undefined") {
    window.location.assign("/import");
    return false;
  }

  openImportCvSearchLoadingModal(cfg);

  window.requestAnimationFrame(function () {
    htmx.ajax(
      "GET",
      "/import/" +
        cfg.jobId +
        "/series/" +
        cfg.seriesId +
        "/cv-search?q=" +
        encodeURIComponent(cfg.query || ""),
      {
        target: "#cv-search-modal",
        swap: "innerHTML",
      },
    );
  });

  return false;
}

function importCollectionPage(config) {
  var cfg = config || {};
  var initialJobId = cfg.resumeJobId != null ? cfg.resumeJobId : null;
  var initialJobStatus = cfg.resumeJobStatus || "";
  var initialStep = cfg.resumeStep || 1;
  if (initialJobStatus === "review" && initialJobId != null && wasImportReviewAdvanced(initialJobId)) {
    initialStep = 3;
  }

  return {
    step: initialStep,
    jobId: initialJobId,
    jobStatus: initialJobStatus,
    loadedPanels: {},

    init: function () {
      if (this.jobStatus === "review") {
        setImportReviewAdvanced(this.jobId, this.step >= 3);
      }
      this.syncUrl();
      var self = this;
      queueMicrotask(function () {
        self.loadCurrentStep();
      });
    },

    isCollectionRouteActive: function () {
      if (!this.$el || !this.$el.isConnected || window.location.pathname !== "/import") {
        return false;
      }

      var params = new URLSearchParams(window.location.search || "");
      return (params.get("tab") || "collection") === "collection";
    },

    syncUrl: function () {
      if (!this.isCollectionRouteActive()) {
        return;
      }

      var params = new URLSearchParams({ tab: "collection" });
      var effectiveStep = this.step >= 5 ? 5 : this.step;
      if (this.jobId != null && effectiveStep >= 2) {
        params.set("resume_job_id", String(this.jobId));
        params.set("resume_step", String(effectiveStep));
      }
      window.history.replaceState({}, "", "/import?" + params.toString());
    },

    handleWizardAdvance: function (detail) {
      var payload = detail || {};

      if (
        Object.prototype.hasOwnProperty.call(payload, "jobId") &&
        payload.jobId != null &&
        payload.jobId !== this.jobId
      ) {
        this.loadedPanels = {};
      }

      if (payload.jobId != null) {
        this.jobId = payload.jobId;
      }
      if (payload.jobStatus) {
        this.jobStatus = payload.jobStatus;
      }
      if (payload.step != null) {
        var effectiveJobId = payload.jobId != null ? payload.jobId : this.jobId;
        var effectiveStatus = payload.jobStatus || this.jobStatus;
        if (effectiveStatus === "review") {
          if (payload.step >= 3) {
            setImportReviewAdvanced(effectiveJobId, true);
          } else if (payload.step === 2) {
            setImportReviewAdvanced(effectiveJobId, false);
          }
        }
        if (payload.forceReload === true && payload.step === 3) {
          delete this.loadedPanels["import-step-review"];
        }
        if (payload.step === 4) {
          delete this.loadedPanels["import-step-execute"];
        }
        if (payload.step === 5) {
          delete this.loadedPanels["import-step-results"];
        }
        this.step = payload.step;
      }
      this.syncUrl();
    },

    loadPanel: function (targetId, url, key) {
      if (this.loadedPanels[targetId] === key) {
        return;
      }

      var target = document.getElementById(targetId);
      if (!target) {
        return;
      }

      if (typeof htmx === "undefined") {
        window.location.assign("/import");
        return;
      }

      this.loadedPanels[targetId] = key;
      htmx.ajax("GET", url, {
        target: "#" + targetId,
        swap: "innerHTML",
      });
    },

    progressMode: function () {
      if (this.step === 4 && (this.jobStatus === "rolling_back" || this.jobStatus === "rolled_back")) {
        return "rollback";
      }
      if (this.step === 4) {
        return "import";
      }
      return "scan";
    },

    progressCompletionTargetStep: function () {
      return this.progressMode() === "import" ? 5 : 3;
    },

    forceLoadPanel: function (targetId, url, key) {
      delete this.loadedPanels[targetId];
      this.loadPanel(targetId, url, key);
    },

    loadCurrentStep: function () {
      if (!this.jobId || this.step < 2) {
        return;
      }

      if (this.step === 2) {
        this.loadPanel(
          "import-step-progress",
          "/import/" + this.jobId + "/progress-partial?next_step=3&mode=scan",
          "progress:scan:" + this.jobId,
        );
        return;
      }

      if (this.step === 3) {
        this.loadPanel(
          "import-step-review",
          "/import/" + this.jobId + "/review-partial",
          "review:" + this.jobId,
        );
        return;
      }

      if (this.step === 4) {
        var progressMode = this.progressMode();
        this.loadPanel(
          "import-step-execute",
          "/import/" +
            this.jobId +
            "/progress-partial?next_step=" +
            this.progressCompletionTargetStep() +
            "&mode=" +
            encodeURIComponent(progressMode),
          "progress:" + progressMode + ":" + this.jobId,
        );
        return;
      }

      if (this.step === 5) {
        this.forceLoadPanel(
          "import-step-results",
          "/import/" + this.jobId + "/results-partial",
          "results:" + this.jobId,
        );
        this.step = 6;
      }
    },
  };
}

function importCollectionFooterData(config) {
  var cfg = config || {};
  var snapshot = cfg.progressSnapshot || {};
  var snapshotReviewSummary = snapshot.review_summary || {};

  return {
    step: cfg.step || 1,
    resumeJobId: cfg.resumeJobId || null,
    phase: snapshot.phase || "",
    progress:
      typeof snapshot.progress === "number" && !Number.isNaN(snapshot.progress)
        ? snapshot.progress
        : 0,
    matched: Math.max(
      Number(snapshotReviewSummary.series_matched) || 0,
      Number(snapshot.series_matched) || 0,
    ),
    noMatch: Math.max(
      Number(snapshotReviewSummary.series_no_match) || 0,
      Number(snapshot.series_no_match) || 0,
    ),
    duplicates: Math.max(
      Number(snapshotReviewSummary.series_in_library) || 0,
      Number(snapshot.series_duplicate) || 0,
    ),
    imported: Number(snapshot.series_imported) || 0,
    failedCount: Number(snapshot.series_failed) || 0,
    selected: Math.max(
      Number(snapshotReviewSummary.series_total) || 0,
      Number(snapshot.series_found) || 0,
    ),
    recentJobs: Number(cfg.recentJobs) || 0,
    unmatched: Number(cfg.unmatched) || 0,
    libraryRoots: Number(cfg.libraryRoots) || 0,

    footerPhaseLabel: function () {
      var labels = {
        inventory: "Inventory",
        scanning: "Scanning",
        analyzing: "Analyze",
        matching: "Matching",
        file_matching: "File Match",
        review: "Review",
        importing: "Importing",
        completed: "Complete",
        failed: "Failed",
        cancelled: "Cancelled",
        done: "Stopped",
      };
      return labels[this.phase] || "Ready";
    },

    get items() {
      if (this.step === 2) {
        return [
          { label: "phase", value: this.footerPhaseLabel() },
          { label: "progress", value: String(Math.max(0, Math.round(this.progress || 0))) + "%" },
          { label: "matched", value: String(this.matched) },
          { label: "no match", value: String(this.noMatch) },
        ];
      }

      if (this.step === 3) {
        return [
          { label: "phase", value: "Review" },
          { label: "matched", value: String(this.matched) },
          { label: "no match", value: String(this.noMatch) },
          { label: "duplicates", value: String(this.duplicates) },
        ];
      }

      if (this.step === 4) {
        return [
          { label: "phase", value: this.footerPhaseLabel() },
          { label: "progress", value: String(Math.max(0, Math.round(this.progress || 0))) + "%" },
          { label: "imported", value: String(this.imported) },
          { label: "failed", value: String(this.failedCount) },
        ];
      }

      if (this.step >= 5) {
        return [
          { label: "status", value: this.failedCount > 0 ? "Follow-up" : "Complete" },
          { label: "imported", value: String(this.imported) },
          { label: "failed", value: String(this.failedCount) },
          { label: "selected", value: String(this.selected) },
        ];
      }

      return [
        { label: "active import", value: this.resumeJobId ? "ready" : "idle" },
        { label: "recent jobs", value: String(this.recentJobs) },
        { label: "unmatched", value: String(this.unmatched) },
        { label: "library roots", value: String(this.libraryRoots) },
      ];
    },

    applyWizardAdvance: function (detail) {
      var payload = detail || {};
      if (payload.step != null) {
        this.step = payload.step;
      }
      if (payload.jobId != null) {
        this.resumeJobId = payload.jobId;
      }
      if (payload.jobStatus === "review") {
        this.phase = "review";
        this.progress = 100;
      } else if (payload.jobStatus === "completed") {
        this.phase = "completed";
        this.progress = 100;
      } else if (payload.jobStatus === "failed") {
        this.phase = "failed";
      } else if (payload.jobStatus === "cancelled") {
        this.phase = "cancelled";
      }
    },

    applyProgressState: function (detail) {
      var payload = detail || {};
      var isLiveScanStep = Number(this.step || 0) === 2;
      if (payload.step != null) {
        this.step = payload.step;
        isLiveScanStep = Number(this.step || 0) === 2;
      }
      if (payload.phase) {
        this.phase = payload.phase;
      }
      if (payload.progress != null && !Number.isNaN(Number(payload.progress))) {
        this.progress = Math.round(Number(payload.progress));
      }
      if (payload.reviewSummary) {
        if (payload.reviewSummary.series_total != null) {
          this.selected = isLiveScanStep
            ? Math.max(
                Number(payload.reviewSummary.series_total) || 0,
                Number(payload.stats && payload.stats.series_found) || 0,
              )
            : Number(payload.reviewSummary.series_total) || 0;
        }
        if (payload.reviewSummary.series_in_library != null) {
          this.duplicates = isLiveScanStep
            ? Math.max(
                Number(payload.reviewSummary.series_in_library) || 0,
                Number(payload.stats && payload.stats.series_duplicate) || 0,
              )
            : Number(payload.reviewSummary.series_in_library) || 0;
        }
        if (payload.reviewSummary.series_matched != null) {
          this.matched = isLiveScanStep
            ? Math.max(
                Number(payload.reviewSummary.series_matched) || 0,
                Number(payload.stats && payload.stats.series_matched) || 0,
              )
            : Number(payload.reviewSummary.series_matched) || 0;
        }
        if (payload.reviewSummary.series_no_match != null) {
          this.noMatch = isLiveScanStep
            ? Math.max(
                Number(payload.reviewSummary.series_no_match) || 0,
                Number(payload.stats && payload.stats.series_no_match) || 0,
              )
            : Number(payload.reviewSummary.series_no_match) || 0;
        }
      }
      var hasReviewSummary = !!payload.reviewSummary;
      if (payload.stats) {
        if (!hasReviewSummary && payload.stats.series_matched != null) {
          this.matched = Number(payload.stats.series_matched) || 0;
        }
        if (!hasReviewSummary && payload.stats.series_no_match != null) {
          this.noMatch = Number(payload.stats.series_no_match) || 0;
        }
        if (!hasReviewSummary && payload.stats.series_duplicate != null) {
          this.duplicates = Number(payload.stats.series_duplicate) || 0;
        }
        if (payload.stats.series_imported != null) {
          this.imported = Number(payload.stats.series_imported) || 0;
        }
        if (payload.stats.series_failed != null) {
          this.failedCount = Number(payload.stats.series_failed) || 0;
        }
        if (!hasReviewSummary && payload.stats.series_found != null) {
          this.selected = Number(payload.stats.series_found) || 0;
        }
      }
    },
  };
}

function importSourceData(config) {
  var cfg = config || {};
  var libraryRoots = Array.isArray(cfg.libraryRoots) ? cfg.libraryRoots : [];
  var initialTargetRootId = libraryRoots.length ? Number(libraryRoots[0].id) : null;

  return Object.assign(fileBrowserMixin(cfg), {
    sourceType: "",
    sourcePath: "",
    scanning: false,
    scanError: "",
    advancedOpen: false,
    minFilesPerSeries: 1,
    fileFormats: "cbz, cbr, cb7, cbt, pdf, epub",
    cvMatchThreshold: 70,
    fileHandlingMode: "managed_copy",
    layoutChoice: "auto",
    layoutFallbackToAuto: true,
    customSeriesPathTemplate: "{Publisher}/{Series} ({Year})",
    customIssueFilenameTemplate: "{Series} {IssueTitle} Issue {Issue:03d}",
    layoutPreview: null,
    layoutPreviewLoading: false,
    layoutPreviewError: "",
    layoutPreviewTimer: null,
    layoutPreviewController: null,
    layoutPreviewRequestId: 0,
    libraryRoots: libraryRoots,
    targetLibraryRootId: initialTargetRootId,
    futureLayoutRequested: false,
    futureRootPolicy: {
      schema_version: 1,
      series_path_template: "",
      comic_file_template: "",
      annual_file_template: "",
      non_standard_file_template: "",
      single_non_standard_file_template: "",
      replace_illegal_characters: true,
      colon_replacement: "dash",
    },
    futurePolicyComparison: null,
    futurePolicyLoading: false,
    futurePolicyError: "",
    futurePolicyRequestId: 0,

    selectSourceType: function (sourceType) {
      this.sourceType = sourceType;
      if (sourceType !== "filesystem") {
        this.fileHandlingMode = "managed_copy";
      }
      this.clearFuturePolicy();
      this.clearLayoutPreview();
      if (sourceType === "filesystem") {
        this.scheduleLayoutPreview();
      }
    },

    setFileHandlingMode: function (mode) {
      this.fileHandlingMode = mode === "in_place" ? "in_place" : "managed_copy";
      this.scanError = "";
      if (this.fileHandlingMode === "in_place") {
        this.scheduleLayoutPreview();
      }
    },

    setLayoutChoice: function (choice) {
      this.layoutChoice = choice;
      this.scheduleLayoutPreview();
    },

    clearFuturePolicy: function () {
      this.futurePolicyRequestId += 1;
      this.futureLayoutRequested = false;
      this.futurePolicyComparison = null;
      this.futurePolicyLoading = false;
      this.futurePolicyError = "";
    },

    canRequestFutureLayout: function () {
      return !!(
        this.sourceType === "filesystem" &&
        this.targetLibraryRootId &&
        this.layoutPreview &&
        this.layoutPreview.can_apply_future_policy &&
        Array.isArray(this.layoutPreview.clusters) &&
        this.layoutPreview.clusters.length === 1 &&
        this.layoutPreview.clusters[0].proposed_series_path_template
      );
    },

    toggleFutureLayout: function () {
      if (!this.futureLayoutRequested) {
        this.futurePolicyRequestId += 1;
        this.futurePolicyComparison = null;
        this.futurePolicyLoading = false;
        this.futurePolicyError = "";
        return;
      }
      if (!this.canRequestFutureLayout()) {
        this.clearFuturePolicy();
        return;
      }
      this.prepareFuturePolicy();
    },

    futureLayoutRootChanged: function () {
      this.futurePolicyRequestId += 1;
      this.futurePolicyComparison = null;
      this.futurePolicyError = "";
      if (this.futureLayoutRequested) {
        this.prepareFuturePolicy();
      }
    },

    futurePolicyRequest: async function (path, options) {
      var response = await fetch(
        path,
        Object.assign({}, options || {}, {
          headers: Object.assign(
            {
              "Content-Type": "application/json",
              "X-CSRF-Token": this.csrfToken(),
            },
            (options && options.headers) || {},
          ),
        }),
      );
      var payload = await response.json().catch(function () {
        return {};
      });
      if (!response.ok) {
        var detail = payload.detail;
        if (Array.isArray(detail)) {
          detail = detail
            .map(function (item) {
              return item && item.msg ? item.msg : "";
            })
            .filter(Boolean)
            .join(" ");
        }
        throw new Error(
          (payload.error && payload.error.message) ||
            detail ||
            "Pullbox could not prepare this future library policy.",
        );
      }
      return payload;
    },

    prepareFuturePolicy: async function () {
      if (!this.canRequestFutureLayout()) {
        this.clearFuturePolicy();
        return;
      }
      var requestId = ++this.futurePolicyRequestId;
      var rootId = Number(this.targetLibraryRootId);
      this.futurePolicyLoading = true;
      this.futurePolicyError = "";
      this.futurePolicyComparison = null;
      try {
        var current = await this.futurePolicyRequest(
          "/api/v1/config/library-roots/" + rootId + "/naming-policy",
        );
        if (requestId !== this.futurePolicyRequestId || rootId !== this.targetLibraryRootId) {
          return;
        }
        var currentPolicy = current.effective_policy;
        var cluster = this.layoutPreview.clusters[0];
        this.futureRootPolicy = {
          schema_version: 1,
          series_path_template: cluster.proposed_series_path_template,
          comic_file_template:
            cluster.proposed_issue_filename_template || currentPolicy.comic_file_template,
          annual_file_template: currentPolicy.annual_file_template,
          non_standard_file_template: currentPolicy.non_standard_file_template,
          single_non_standard_file_template: currentPolicy.single_non_standard_file_template,
          replace_illegal_characters: currentPolicy.replace_illegal_characters,
          colon_replacement: currentPolicy.colon_replacement,
        };
        await this.previewFuturePolicy(requestId);
      } catch (err) {
        if (requestId === this.futurePolicyRequestId) {
          this.futurePolicyError =
            err && err.message
              ? err.message
              : "Pullbox could not prepare this future library policy.";
        }
      } finally {
        if (requestId === this.futurePolicyRequestId) {
          this.futurePolicyLoading = false;
        }
      }
    },

    futurePolicyExamples: function () {
      if (
        !this.layoutPreview ||
        !Array.isArray(this.layoutPreview.clusters) ||
        this.layoutPreview.clusters.length !== 1 ||
        !Array.isArray(this.layoutPreview.clusters[0].examples)
      ) {
        return [];
      }
      return this.layoutPreview.clusters[0].examples
        .filter(function (example) {
          return (
            example &&
            typeof example.series === "string" &&
            example.series.trim() &&
            example.issue_number !== null &&
            example.issue_number !== "" &&
            Number.isFinite(Number(example.issue_number))
          );
        })
        .slice(0, 5)
        .map(function (example) {
          return {
            publisher: example.publisher || null,
            series: example.series,
            year:
              example.year !== null &&
              example.year !== "" &&
              Number.isFinite(Number(example.year)) &&
              Number(example.year) > 0 &&
              Number(example.year) <= 9999
                ? Number(example.year)
                : null,
            issue_number: Number(example.issue_number),
            issue_title: example.issue_title || null,
          };
        });
    },

    previewFuturePolicy: async function (existingRequestId) {
      if (!this.futureLayoutRequested || !this.targetLibraryRootId) {
        return;
      }
      var requestId = existingRequestId || ++this.futurePolicyRequestId;
      this.futurePolicyLoading = true;
      this.futurePolicyError = "";
      try {
        var comparison = await this.futurePolicyRequest(
          "/api/v1/config/library-roots/" +
            Number(this.targetLibraryRootId) +
            "/naming-policy/preview",
          {
            method: "POST",
            body: JSON.stringify({
              policy: this.futureRootPolicy,
              examples: this.futurePolicyExamples(),
            }),
          },
        );
        if (requestId === this.futurePolicyRequestId) {
          this.futurePolicyComparison = comparison;
        }
      } catch (err) {
        if (requestId === this.futurePolicyRequestId) {
          this.futurePolicyComparison = null;
          this.futurePolicyError =
            err && err.message ? err.message : "Pullbox could not preview this future policy.";
        }
      } finally {
        if (requestId === this.futurePolicyRequestId) {
          this.futurePolicyLoading = false;
        }
      }
    },

    sourceLayoutPayload: function () {
      if (this.layoutChoice === "series_folders") {
        return {
          schema_version: 1,
          mode: "preset",
          preset: "series_folders",
          fallback_to_auto: this.layoutFallbackToAuto,
        };
      }
      if (this.layoutChoice === "publisher_series") {
        return {
          schema_version: 1,
          mode: "preset",
          preset: "publisher_series",
          fallback_to_auto: this.layoutFallbackToAuto,
        };
      }
      if (this.layoutChoice === "custom") {
        return {
          schema_version: 1,
          mode: "custom",
          series_path_template: this.customSeriesPathTemplate.trim(),
          issue_filename_template: this.customIssueFilenameTemplate.trim() || null,
          fallback_to_auto: this.layoutFallbackToAuto,
        };
      }
      return {
        schema_version: 1,
        mode: "auto",
        fallback_to_auto: true,
      };
    },

    canAnalyzeLayout: function () {
      if (this.sourceType !== "filesystem" || !this.sourcePath.trim()) {
        return false;
      }
      return this.layoutChoice !== "custom" || !!this.customSeriesPathTemplate.trim();
    },

    canStartScan: function () {
      if (!this.sourcePath.trim() || this.scanning) {
        return false;
      }
      if (
        this.fileHandlingMode === "in_place" &&
        (this.sourceType !== "filesystem" ||
          !this.layoutPreview ||
          !this.layoutPreview.can_keep_in_place)
      ) {
        return false;
      }
      if (
        this.futureLayoutRequested &&
        (!this.canRequestFutureLayout() ||
          this.futurePolicyLoading ||
          this.futurePolicyError ||
          !this.futurePolicyComparison)
      ) {
        return false;
      }
      return this.layoutChoice !== "custom" || !!this.customSeriesPathTemplate.trim();
    },

    clearLayoutPreview: function () {
      if (this.layoutPreviewTimer) {
        clearTimeout(this.layoutPreviewTimer);
        this.layoutPreviewTimer = null;
      }
      if (this.layoutPreviewController) {
        this.layoutPreviewController.abort();
        this.layoutPreviewController = null;
      }
      this.layoutPreviewRequestId += 1;
      this.layoutPreview = null;
      this.layoutPreviewLoading = false;
      this.layoutPreviewError = "";
      this.clearFuturePolicy();
    },

    scheduleLayoutPreview: function () {
      if (this.layoutPreviewTimer) {
        clearTimeout(this.layoutPreviewTimer);
        this.layoutPreviewTimer = null;
      }
      if (this.layoutPreviewController) {
        this.layoutPreviewController.abort();
        this.layoutPreviewController = null;
      }
      this.layoutPreviewRequestId += 1;
      this.layoutPreview = null;
      this.layoutPreviewLoading = false;
      this.layoutPreviewError = "";
      this.clearFuturePolicy();
      if (!this.canAnalyzeLayout()) {
        return;
      }
      var self = this;
      this.layoutPreviewTimer = setTimeout(function () {
        self.layoutPreviewTimer = null;
        self.previewLayout();
      }, 500);
    },

    previewLayout: async function () {
      if (!this.canAnalyzeLayout()) {
        return;
      }
      if (this.layoutPreviewTimer) {
        clearTimeout(this.layoutPreviewTimer);
        this.layoutPreviewTimer = null;
      }
      if (this.layoutPreviewController) {
        this.layoutPreviewController.abort();
      }
      this.clearFuturePolicy();

      var requestId = ++this.layoutPreviewRequestId;
      var controller = new AbortController();
      this.layoutPreviewController = controller;
      this.layoutPreviewLoading = true;
      this.layoutPreviewError = "";

      try {
        var response = await fetch("/api/v1/import/layout-preview", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          signal: controller.signal,
          body: JSON.stringify({
            source_path: this.sourcePath.trim(),
            source_type: "filesystem",
            layout: this.sourceLayoutPayload(),
          }),
        });
        var payload = await response.json().catch(function () {
          return {};
        });
        if (!response.ok) {
          var detail = payload.detail;
          if (Array.isArray(detail)) {
            detail = detail
              .map(function (item) {
                return item && item.msg ? item.msg : "";
              })
              .filter(Boolean)
              .join(" ");
          }
          var message =
            detail ||
            (payload.error && payload.error.message) ||
            "Pullbox could not analyze this folder layout.";
          throw new Error(message);
        }
        if (requestId !== this.layoutPreviewRequestId) {
          return;
        }
        this.layoutPreview = payload;
      } catch (err) {
        if (err && err.name === "AbortError") {
          return;
        }
        if (requestId === this.layoutPreviewRequestId) {
          this.layoutPreviewError =
            err && err.message ? err.message : "Pullbox could not analyze this folder layout.";
        }
      } finally {
        if (requestId === this.layoutPreviewRequestId) {
          this.layoutPreviewLoading = false;
          this.layoutPreviewController = null;
        }
      }
    },

    layoutClassificationLabel: function (value) {
      return String(value || "needs_review")
        .replace(/_/g, " ")
        .replace(/\b\w/g, function (letter) {
          return letter.toUpperCase();
        });
    },

    layoutPreviewSummary: function () {
      if (!this.layoutPreview) {
        return "";
      }
      return (
        String(this.layoutPreview.files_fitting || 0) +
        " of " +
        String(this.layoutPreview.files_considered || 0) +
        " sampled files fit this interpretation"
      );
    },

    startScan: async function () {
      if (!this.canStartScan()) {
        return;
      }

      this.scanning = true;
      this.scanError = "";

      try {
        var response = await fetch("/api/v1/import", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            source_path: this.sourcePath.trim(),
            source_type: this.sourceType,
            cv_match_threshold: this.cvMatchThreshold / 100,
            min_files_per_series: this.minFilesPerSeries,
            file_formats: this.fileFormats.trim() || null,
            file_handling_mode: this.fileHandlingMode,
            source_layout: this.sourceLayoutPayload(),
            target_library_root_id: this.futureLayoutRequested
              ? Number(this.targetLibraryRootId)
              : null,
            future_layout_requested: this.futureLayoutRequested,
            future_root_policy: this.futureLayoutRequested ? this.futureRootPolicy : null,
          }),
        });

        if (!response.ok) {
          var error = await response
            .json()
            .catch(function () {
              return { detail: "Failed to create import job" };
            });
          throw new Error(error.detail || "Server error (" + response.status + ")");
        }

        var job = await response.json();
        dispatchImportWizardAdvance({
          step: 2,
          jobId: job.id,
          jobStatus: job.status,
        });
      } catch (err) {
        this.scanError = err && err.message ? err.message : "Failed to start scan.";
      } finally {
        this.scanning = false;
      }
    },
  });
}

function importJobLogViewerData(config) {
  var cfg = config || {};
  var _REQUEST_TIMEOUT_MS =
    Number(cfg.requestTimeoutMs || 0) > 0 ? Number(cfg.requestTimeoutMs) : 12000;

  return {
    jobId: Number(cfg.jobId || 0),
    viewerTitle: String(cfg.title || "Import log"),
    isLive: cfg.live !== false,
    loadingContent: false,
    syncingLive: false,
    searchQuery: "",
    levelFilter: "",
    pageSize: String(cfg.pageSize || "50"),
    currentPage: 1,
    expandedIdx: null,
    entries: [],
    totalCount: 0,
    pollMs: Number(cfg.pollMs || 1000),
    _pollTimer: null,
    _requestController: null,
    _requestToken: 0,
    _evtSource: null,
    _evtSourceRegistryId: null,
    _sseConnected: false,

    init: function () {
      this._refreshLogs(true);
      this._syncStream();
      this._syncLiveTimer();
    },

    toggleLive: function () {
      this.isLive = !this.isLive;
      this._syncStream();
      this._syncLiveTimer();
      if (this.isLive) {
        this._refreshLogs(true);
      }
    },

    destroy: function () {
      if (this._pollTimer) {
        window.clearInterval(this._pollTimer);
        this._pollTimer = null;
      }
      if (this._requestController) {
        this._requestController.abort();
        this._requestController = null;
      }
      this._disconnectStream();
    },

    _syncLiveTimer: function () {
      if (this._pollTimer) {
        window.clearInterval(this._pollTimer);
        this._pollTimer = null;
      }
      if (!this.isLive || this.jobId <= 0 || typeof EventSource !== "undefined") {
        return;
      }
      this._pollTimer = window.setInterval(
        function () {
          this._refreshLogs();
        }.bind(this),
        this.pollMs,
      );
    },

    _disconnectStream: function () {
      var source = this._evtSource;
      var registryId = this._evtSourceRegistryId;
      this._evtSource = null;
      this._evtSourceRegistryId = null;
      if (registryId) {
        _importEventSourceRegistry.close(registryId, source, "component-disconnect");
      } else if (source) {
        source.close();
      }
      this._sseConnected = false;
    },

    _syncStream: function () {
      if (!this.isLive || this.jobId <= 0 || typeof EventSource === "undefined") {
        this._disconnectStream();
        return;
      }
      if (this._evtSource) {
        return;
      }
      this._connectStream();
    },

    _connectStream: function () {
      var self = this;
      self._disconnectStream();
      var source = new EventSource("/api/v1/import/" + self.jobId + "/logs/stream");
      var registryId = _importEventSourceRegistry.register(source, {
        onClose: function (closedSource) {
          if (self._evtSource === closedSource) {
            self._evtSource = null;
          }
          if (self._evtSourceRegistryId === registryId) {
            self._evtSourceRegistryId = null;
          }
          self._sseConnected = false;
        },
        resume: function () {
          if (self.isLive && self.jobId > 0) {
            self._syncStream();
            self._syncLiveTimer();
            self._refreshLogs(true);
          }
        },
      });
      self._evtSource = source;
      self._evtSourceRegistryId = registryId;
      source.onopen = function () {
        if (self._evtSource !== source) {
          return;
        }
        self._sseConnected = true;
      };
      source.addEventListener("log", function (event) {
        if (self._evtSource !== source) {
          return;
        }
        self._sseConnected = true;
        self._appendStreamEntry(event);
      });
      source.onerror = function () {
        if (self._evtSource !== source) {
          return;
        }
        self._sseConnected = false;
      };
    },

    setLevel: function (level) {
      this.levelFilter = level || "";
      this.currentPage = 1;
      this.expandedIdx = null;
    },

    _filterEntries: function (entries) {
      var filtered = Array.isArray(entries) ? entries.slice() : [];
      if (this.levelFilter) {
        var target = String(this.levelFilter).toLowerCase();
        filtered = filtered.filter(function (entry) {
          var level = String(entry.level || "").toLowerCase();
          if (target === "error") {
            return level === "error" || level === "critical";
          }
          return level === target;
        });
      }
      if (this.searchQuery) {
        var query = String(this.searchQuery).toLowerCase();
        var self = this;
        filtered = filtered.filter(function (entry) {
          var message = String(entry.message || "").toLowerCase();
          var extra = self.formatExtra(entry.extra).toLowerCase();
          var path = String(entry.file_path || "").toLowerCase();
          return message.includes(query) || extra.includes(query) || path.includes(query);
        });
      }
      return filtered;
    },

    get filteredCount() {
      return this._filterEntries(this.entries).length;
    },

    get displayEntries() {
      var filtered = this._filterEntries(this.entries);
      var limit = Math.max(1, parseInt(this.pageSize, 10) || 50);
      var offset = (this.currentPage - 1) * limit;
      return filtered.slice(offset, offset + limit);
    },

    get totalPages() {
      return Math.max(1, Math.ceil(this.filteredCount / Math.max(1, parseInt(this.pageSize, 10) || 50)));
    },

    get emptyMessage() {
      if (this.loadingContent && this.totalCount === 0) {
        return "Loading log entries...";
      }
      return this.totalCount === 0
        ? "No log entries yet."
        : "No log entries match the current filters.";
    },

    get footerStatusText() {
      if (this.totalCount === 0) {
        return "0 entries";
      }
      if (!this.levelFilter && !this.searchQuery) {
        return this.totalCount + " entries";
      }
      return this.filteredCount + " entries (filtered from " + this.totalCount + ")";
    },

    get downloadHref() {
      return "/api/v1/import/" + this.jobId + "/logs/download";
    },

    get lastEntryId() {
      if (!this.entries.length) {
        return 0;
      }
      for (var idx = this.entries.length - 1; idx >= 0; idx--) {
        var candidate = Number(this.entries[idx].id || 0);
        if (candidate > 0) {
          return candidate;
        }
      }
      return 0;
    },

    _shouldFollowLiveTail: function () {
      return (
        this.isLive &&
        !this.levelFilter &&
        !this.searchQuery &&
        this.currentPage >= this.totalPages
      );
    },

    prevPage: function () {
      if (this.currentPage > 1) {
        this.currentPage--;
        this.expandedIdx = null;
      }
    },

    nextPage: function () {
      if (this.currentPage < this.totalPages) {
        this.currentPage++;
        this.expandedIdx = null;
      }
    },

    formatExtra: function (extra) {
      if (!extra || extra === "{}") {
        return "";
      }
      try {
        var obj = typeof extra === "string" ? JSON.parse(extra) : extra;
        return JSON.stringify(obj, null, 2);
      } catch (_) {
        return String(extra);
      }
    },

    _extractFilePath: function (data) {
      if (!data || typeof data !== "object") {
        return "";
      }
      return String(data.file_path || data.path || data.source_folder || data.source_path || "");
    },

    _normalizeEntry: function (entry) {
      var data = entry && entry.data && typeof entry.data === "object" ? entry.data : {};
      return {
        id: entry.id,
        timestamp: entry.logged_at || "",
        formatted_timestamp: entry.logged_at ? _pb.formatFull(entry.logged_at) : "",
        level: entry.level || "",
        message: entry.message || entry.event || "",
        file_path: this._extractFilePath(data),
        extra: data,
      };
    },

    _normalizeStreamEntry: function (entry) {
      var data = entry && entry.data && typeof entry.data === "object" ? entry.data : {};
      return {
        id: 0,
        _streamToken: String(entry.stream_token || ""),
        timestamp: entry.logged_at || "",
        formatted_timestamp: entry.logged_at ? _pb.formatFull(entry.logged_at) : "",
        level: entry.level || "",
        message: entry.message || entry.event || "",
        file_path: this._extractFilePath(data),
        extra: data,
      };
    },

    _appendStreamEntry: function (event) {
      if (!event || !event.data) {
        return;
      }
      try {
        var payload = JSON.parse(event.data);
        var streamToken = String(payload.stream_token || "");
        if (
          streamToken &&
          this.entries.some(function (entry) {
            return entry && entry._streamToken === streamToken;
          })
        ) {
          return;
        }
        var shouldFollowTail = this._shouldFollowLiveTail();
        this.entries.push(this._normalizeStreamEntry(payload));
        this.totalCount += 1;
        if (shouldFollowTail) {
          this.currentPage = this.totalPages;
        } else if (this.currentPage > this.totalPages) {
          this.currentPage = this.totalPages;
        }
      } catch (_) {
        // Ignore malformed SSE payloads.
      }
    },

    _beginRequest: function () {
      if (this._requestController) {
        this._requestController.abort();
      }
      this._requestController = new AbortController();
      this._requestToken += 1;
      return {
        controller: this._requestController,
        token: this._requestToken,
      };
    },

    _completeRequest: function (token) {
      if (token !== this._requestToken) {
        return;
      }
      this._requestController = null;
      this.loadingContent = false;
      this.syncingLive = false;
    },

    _fetchJson: async function (url, request) {
      var controller = request.controller;
      var timeoutId = window.setTimeout(function () {
        controller.abort("timeout");
      }, _REQUEST_TIMEOUT_MS);
      try {
        var response = await fetch(url, {
          headers: { "X-CSRF-Token": readCsrfTokenFromBody() },
          signal: controller.signal,
        });
        if (!response.ok) {
          return null;
        }
        return await response.json();
      } finally {
        window.clearTimeout(timeoutId);
      }
    },

    _refreshLogs: async function (force) {
      if (!this.jobId) {
        return;
      }
      if ((this.loadingContent || this.syncingLive) && !force) {
        return;
      }

      var incrementalLiveRefresh =
        this.isLive &&
        typeof EventSource === "undefined" &&
        !force &&
        !this.levelFilter &&
        !this.searchQuery &&
        this.entries.length > 0;

      if (incrementalLiveRefresh) {
        this.syncingLive = true;
      } else {
        this.loadingContent = true;
      }
      var request = this._beginRequest();
      try {
        if (incrementalLiveRefresh) {
          var incrementalData = await this._fetchJson(
            "/api/v1/import/" +
              this.jobId +
              "/logs?after_id=" +
              this.lastEntryId +
              "&page_size=500&order=asc",
            request,
          );
          if (request.token !== this._requestToken) {
            return;
          }
          if (incrementalData) {
            var newItems = Array.isArray(incrementalData.items) ? incrementalData.items : [];
            this.totalCount = Number(incrementalData.total || this.totalCount || this.entries.length) || this.entries.length;
            if (newItems.length) {
              for (var n = 0; n < newItems.length; n++) {
                this.entries.push(this._normalizeEntry(newItems[n]));
              }
              if (this.currentPage > this.totalPages) {
                this.currentPage = this.totalPages;
              }
            }
            return;
          }
        }

        var page = 1;
        var pageSize = 500;
        var allEntries = [];
        var total = 0;

        while (true) {
          var data = await this._fetchJson(
            "/api/v1/import/" + this.jobId + "/logs?page=" + page + "&page_size=" + pageSize + "&order=asc",
            request,
          );
          if (request.token !== this._requestToken) {
            return;
          }
          if (!data) {
            break;
          }
          var items = Array.isArray(data.items) ? data.items : [];
          if (page === 1) {
            total = Number(data.total || items.length) || 0;
          }
          for (var i = 0; i < items.length; i++) {
            allEntries.push(this._normalizeEntry(items[i]));
          }
          if (!items.length || allEntries.length >= total) {
            break;
          }
          page += 1;
        }

        var shouldFollowTail = this._shouldFollowLiveTail();
        this.entries = allEntries;
        this.totalCount = total || allEntries.length;
        if (shouldFollowTail) {
          this.currentPage = this.totalPages;
        } else if (this.currentPage > this.totalPages) {
          this.currentPage = this.totalPages;
        }
      } catch (_) {
        // Preserve the existing viewer state if refresh fails.
      } finally {
        this._completeRequest(request.token);
      }
    },
  };
}

function importProgressData(jobId, nextStep, sourceType) {
  var initialSnapshot = arguments.length > 3 ? arguments[3] || null : null;
  var progressMode = arguments.length > 4 ? arguments[4] || null : null;
  var mode = progressMode || ((nextStep || 3) === 5 ? "import" : "scan");
  var _COMPLETED_FILE_DWELL_MS = 160;
  return {
    jobId: jobId,
    nextStep: nextStep || 3,
    progressMode: mode,
    sourceType: sourceType || "filesystem",
    initialSnapshot: initialSnapshot,
    jobStatus:
      initialSnapshot && typeof initialSnapshot.status === "string"
        ? String(initialSnapshot.status).toLowerCase()
        : "",
    phase: "inventory",
    phaseLabel:
      mode === "import"
        ? "Importing series into Pullbox..."
        : mode === "rollback"
          ? "Rolling back import actions..."
        : "Inventorying collection...",
    message:
      mode === "import"
        ? "Adding series via ComicVine..."
        : mode === "rollback"
          ? "Rolling back import actions..."
        : "Preparing scan inventory...",
    progress: 0,
    currentFileName: "",
    currentFileStage: "",
    currentFileProgress: 0,
    currentFileProgressCurrent: null,
    currentFileProgressTotal: null,
    currentFileProgressUnit: "",
    currentSeriesName: "",
    currentSeriesProgress: 0,
    currentItemKind: "",
    currentItemStage: "",
    currentItemStageLabelText: "",
    currentItemDetail: "",
    currentItemProgressValue: null,
    animateCurrentFileProgressBar: false,
    failed: false,
    completed: false,
    _completedJobStatus: null,
    progressRevision: 0,
    fileProgressRevision: 0,
    pendingCurrentFileEvent: null,
    pendingCurrentFileEventType: "",
    pendingCurrentFileTimer: null,
    evtSource: null,
    evtSourceRegistryId: null,
    pollTimer: null,
    clockTimer: null,
    nowMs: Date.now(),
    startedAt: null,
    etaSeconds: null,
    etaCapturedAt: 0,
    elapsedSeconds: null,
    elapsedCapturedAt: 0,
    totalSelected: 0,
    pausing: false,
    optimisticPauseRequested: false,
    resuming: false,
    retryingStoryArcPlacements: false,
    storyArcPlacementRetryError: "",
    storyArcPlacementRetrySuccess: "",
    cancelPrompting: false,
    cancelling: false,
    cancelReturnStarted: false,
    continuing: false,
    controlState: {},
    stats: {
      scan_total_files: 0,
      scan_total_dirs: 0,
      series_found: 0,
      series_duplicate: 0,
      series_matched: 0,
      series_no_match: 0,
      series_new: 0,
      series_imported: 0,
      series_failed: 0,
      total_files_imported: 0,
      total_files_failed: 0,
    },
    phases: [
      { key: "inventory", label: "Inventory" },
      { key: "scanning", label: "Scanning files" },
      { key: "analyzing", label: "Deduplicating" },
      { key: "matching", label: "Matching ComicVine" },
      { key: "file_matching", label: "Matching Files" },
      { key: "review", label: "Review" },
    ],
    fileStats: {
      total: 0,
      matched: 0,
      conflicts: 0,
      no_match: 0,
    },
    reviewSummary: {
      seriesTotal: 0,
      inLibrary: 0,
      matched: 0,
      duplicateCopies: 0,
      noMatch: 0,
      filesTotal: 0,
      conflicts: 0,
    },

    isImportMode: function () {
      return this.progressMode === "import";
    },

    isRollbackMode: function () {
      return this.progressMode === "rollback";
    },

    isScanMode: function () {
      return !this.isImportMode() && !this.isRollbackMode();
    },

    pausedMessage: function () {
      if (this.isImportMode()) {
        return "Import is paused.";
      }
      if (this.isRollbackMode()) {
        return "Rollback is paused.";
      }
      return "Scan is paused.";
    },

    runNoun: function () {
      if (this.isImportMode()) {
        return "import";
      }
      if (this.isRollbackMode()) {
        return "rollback";
      }
      return "scan";
    },

    pauseActionLabel: function () {
      if (this.isImportMode()) {
        return "Pause import";
      }
      if (this.isRollbackMode()) {
        return "Pause rollback";
      }
      return "Pause scan";
    },

    resumeActionLabel: function () {
      if (this.isImportMode()) {
        return "Resume import";
      }
      if (this.isRollbackMode()) {
        return "Resume rollback";
      }
      return "Resume scan";
    },

    cancelActionLabel: function () {
      if (this.isImportMode()) {
        return "Cancel import";
      }
      if (this.isRollbackMode()) {
        return "Cancel rollback";
      }
      return "Cancel scan";
    },

    hasOptimisticPause: function () {
      return (
        this.optimisticPauseRequested &&
        (this.isScanMode() || this.isImportMode() || this.isRollbackMode()) &&
        !this.completed &&
        !this.failed
      );
    },

    hasExplicitControlState: function () {
      return !!this.controlState && Object.keys(this.controlState).length > 0;
    },

    isActiveImportLifecycle: function () {
      if (!this.isImportMode() || this.completed || this.failed) {
        return false;
      }
      if (
        this.jobStatus === "paused" ||
        this.jobStatus === "cancelling" ||
        this.jobStatus === "rolling_back" ||
        this.jobStatus === "cancelled" ||
        this.jobStatus === "rolled_back" ||
        this.jobStatus === "completed" ||
        this.jobStatus === "failed"
      ) {
        return false;
      }
      if (this.jobStatus === "importing" || this.jobStatus === "pausing" || this.phase === "importing") {
        return true;
      }
      return !this.hasExplicitControlState();
    },

    isActiveScanLifecycle: function () {
      if (!this.isScanMode() || this.completed || this.failed) {
        return false;
      }
      if (
        this.jobStatus === "paused" ||
        this.jobStatus === "cancelled" ||
        this.jobStatus === "completed" ||
        this.jobStatus === "failed" ||
        this.jobStatus === "review"
      ) {
        return false;
      }
      if (
        ["pending", "scanning", "pausing", "analyzing", "matching", "file_matching"].indexOf(this.jobStatus) !== -1 ||
        ["inventory", "scanning", "analyzing", "matching", "file_matching"].indexOf(this.phase) !== -1
      ) {
        return true;
      }
      return !this.hasExplicitControlState();
    },

    isActiveRunLifecycle: function () {
      if (this.isImportMode()) {
        return this.isActiveImportLifecycle();
      }
      if (this.isScanMode()) {
        return this.isActiveScanLifecycle();
      }
      return false;
    },

    isPausedLifecycleState: function () {
      if (this.jobStatus === "paused") {
        return true;
      }
      if (!this.controlState.can_resume) {
        return false;
      }
      return !this.isActiveRunLifecycle();
    },

    isPausePresentationState: function () {
      return this.hasOptimisticPause() || this.isPausedLifecycleState();
    },

    isPauseBlockedByLifecycle: function () {
      return (
        this.completed ||
        this.failed ||
        this.isPausePresentationState() ||
        this.jobStatus === "paused" ||
        this.jobStatus === "cancelling" ||
        this.jobStatus === "rolling_back" ||
        this.jobStatus === "cancelled" ||
        this.jobStatus === "rolled_back" ||
        this.jobStatus === "completed" ||
        this.jobStatus === "failed"
      );
    },

    showPauseAction: function () {
      if ((!this.isScanMode() && !this.isImportMode()) || this.isPauseBlockedByLifecycle()) {
        return false;
      }
      if (this.controlState.can_pause) {
        return true;
      }
      return this.isActiveRunLifecycle();
    },

    showResumeAction: function () {
      return (
        (this.isScanMode() || this.isImportMode() || this.isRollbackMode()) &&
        !this.completed &&
        !this.failed &&
        (this.isPausedLifecycleState() || this.hasOptimisticPause())
      );
    },

    showRetryStoryArcPlacementsAction: function () {
      if (!this.isImportMode() || this.completed || this.failed) {
        return false;
      }
      return (
        this.retryingStoryArcPlacements ||
        !!(this.controlState && this.controlState.can_retry_story_arc_placements)
      );
    },

    canResumeAction: function () {
      if (!this.showResumeAction()) {
        return false;
      }
      if (this.resuming) {
        return false;
      }
      if (this.hasOptimisticPause()) {
        return !!this.controlState.can_resume;
      }
      return !!this.controlState.can_resume || this.isPausedLifecycleState();
    },

    isResumeActionDisabled: function () {
      return !this.canResumeAction();
    },

    showCancelAction: function () {
      if (
        (this.isScanMode() || this.isImportMode() || this.isRollbackMode()) &&
        (this.cancelPrompting || this.cancelling)
      ) {
        return true;
      }
      if (
        (this.isScanMode() || this.isImportMode() || this.isRollbackMode()) &&
        !this.completed &&
        !this.failed
      ) {
        if (this.controlState.can_cancel) {
          return true;
        }
        return this.isActiveRunLifecycle();
      }
      return false;
    },

    hasFollowUp: function () {
      return (Number(this.stats.series_failed) || 0) > 0 || (Number(this.stats.total_files_failed) || 0) > 0;
    },

    isTerminalStatus: function (status, modeValue) {
      var currentStatus = String(status || "").toLowerCase();
      var effectiveMode = modeValue || this.progressMode;
      if (currentStatus === "review") {
        return effectiveMode === "scan";
      }
      return ["completed", "failed", "cancelled", "rolled_back"].indexOf(currentStatus) !== -1;
    },

    phaseLabelForKey: function (phaseKey) {
      var labels = {
        inventory: "Inventorying collection...",
        scanning: "Scanning your collection...",
        analyzing: "Analyzing for duplicates...",
        matching: "Matching series against ComicVine...",
        file_matching: "Matching files to issues...",
        importing: "Importing series into Pullbox...",
        story_arc_placements: "Creating Story Arc placements...",
        rollback: "Rolling back import actions...",
        review: "Complete",
        done: "Run stopped",
      };
      return labels[phaseKey] || "Processing...";
    },

    titleText: function () {
      if (this.isRollbackMode()) {
        if (this.completed) {
          return "Rollback complete";
        }
        if (this.failed) {
          return "Rollback failed";
        }
        if (this.isPausePresentationState()) {
          return "Rollback paused";
        }
        return "Rollback in progress";
      }

      if (this.isImportMode()) {
        if (this.failed) {
          return "Import failed";
        }
        if (this.completed) {
          return this.hasFollowUp() ? "Import complete with follow-up" : "Import complete";
        }
        if (this.isPausePresentationState()) {
          return "Import paused";
        }
        return "Import in progress";
      }

      if (this.failed) {
        return "Scan failed";
      }
      if (this.completed) {
        return "Scan complete";
      }
      if (this.isPausePresentationState()) {
        return "Scan paused";
      }
      return "Scan in progress";
    },

    summaryText: function () {
      if (this.isRollbackMode()) {
        if (this.completed) {
          return "Rollback finished cleanly. Return to review when you're ready.";
        }
        if (this.failed) {
          return "Rollback stopped before completion. Review the log details below.";
        }
        if (this.isPausePresentationState()) {
          return "Rollback is paused. Resume when you're ready to continue unwinding the import.";
        }
        return "Rolling back the recorded import actions now.";
      }

      if (this.isImportMode()) {
        if (this.failed) {
          return "The run stopped before completion. Review the error details below.";
        }
        if (this.completed) {
          return this.hasFollowUp()
            ? "The run finished with follow-up still worth reviewing before you move to results."
            : "The run finished cleanly. Review the log and counters before you continue to results.";
        }
        if (this.isPausePresentationState()) {
          return "The import is paused. Resume it or cancel the run when you're ready.";
        }
        return "Watch the live counters and log output as Pullbox works through the selected series.";
      }

      if (this.failed) {
        return "Check the error details below.";
      }
      if (this.completed) {
        return "Review the scan results below before moving into import review.";
      }
      if (this.isPausePresentationState()) {
        return "The scan is paused. Resume it or cancel the scan when you're ready.";
      }
      return "";
    },

    showCurrentFileProgress: function () {
      return (
        this.isImportMode() &&
        !this.failed &&
        !this.completed &&
        !!this.currentFileName &&
        typeof this.currentFileProgress === "number"
      );
    },

    showCurrentItemProgress: function () {
      return (
        !this.failed &&
        !this.completed &&
        (!!this.currentFileName ||
          !!this.currentSeriesName ||
          !!this.currentItemKind ||
          this.currentItemProgressValue != null)
      );
    },

    currentItemName: function () {
      if (this.currentFileName || this.currentSeriesName) {
        return this.currentFileName || this.currentSeriesName;
      }
      if (this.currentItemKind === "scan") {
        return this.phaseLabel || "Scanning import source...";
      }
      return this.isImportMode() ? "Preparing import..." : "Preparing scan...";
    },

    currentItemStageLabel: function () {
      if (this.currentFileName) {
        return this.currentFileStageLabel();
      }
      return (
        this.currentItemStageLabelText ||
        this.message ||
        this.phaseLabel ||
        (this.isImportMode() ? "Preparing series records..." : "Working through scan phase...")
      );
    },

    currentItemDetailText: function () {
      if (this.currentFileName) {
        return this.currentFileDetailText();
      }
      if (this.currentItemDetail) {
        return this.currentItemDetail;
      }
      if (this.currentSeriesName) {
        return this.isImportMode() ? "Series metadata and issue records" : "Series review candidate";
      }
      if (this.currentItemKind === "scan") {
        return "Discovery and matching progress";
      }
      return "";
    },

    currentItemProgress: function () {
      if (this.currentFileName) {
        return this.currentFileProgress;
      }
      if (typeof this.currentItemProgressValue === "number") {
        return this.currentItemProgressValue;
      }
      return this.currentSeriesProgress;
    },

    currentItemProgressBarTransitionClass: function () {
      if (this.currentFileName) {
        return this.currentFileProgressBarTransitionClass();
      }
      return "transition-all duration-300";
    },

    currentFileStageLabel: function () {
      var labels = {
        preparing: "Preparing file",
        extracting: "Extracting archive",
        rendering: "Rendering PDF pages",
        encoding: "Encoding pages",
        packing: "Packing CBZ",
        comicinfo_metadata: "Preparing ComicInfo metadata",
        transferring: "Transferring to library",
        rewriting: "Writing ComicInfo.xml",
        finalizing: "Finalizing imported file",
      };
      return labels[this.currentFileStage] || "Processing file";
    },

    _formatProgressCount: function (value, unit) {
      var numericValue = Number(value);
      if (!Number.isFinite(numericValue)) {
        return "";
      }
      if (unit === "bytes" && window._pb && typeof window._pb.formatBytes === "function") {
        return window._pb.formatBytes(numericValue);
      }
      return String(Math.max(0, Math.round(numericValue)));
    },

    currentFileDetailText: function () {
      if (
        this.currentFileProgressCurrent == null ||
        this.currentFileProgressTotal == null ||
        !this.currentFileProgressUnit
      ) {
        return "";
      }
      if (this.currentFileProgressUnit === "bytes") {
        return (
          this._formatProgressCount(this.currentFileProgressCurrent, this.currentFileProgressUnit) +
          " / " +
          this._formatProgressCount(this.currentFileProgressTotal, this.currentFileProgressUnit)
        );
      }
      return (
        this._formatProgressCount(this.currentFileProgressCurrent, this.currentFileProgressUnit) +
        " / " +
        this._formatProgressCount(this.currentFileProgressTotal, this.currentFileProgressUnit) +
        " " +
        this.currentFileProgressUnit
      );
    },

    resetCurrentFileProgress: function () {
      this.clearPendingCurrentFileTransition();
      this.currentFileName = "";
      this.currentFileStage = "";
      this.currentFileProgress = 0;
      this.currentFileProgressCurrent = null;
      this.currentFileProgressTotal = null;
      this.currentFileProgressUnit = "";
      this.animateCurrentFileProgressBar = false;
      this.fileProgressRevision = 0;
    },

    resetCurrentItemProgress: function () {
      this.resetCurrentFileProgress();
      this.currentSeriesName = "";
      this.currentSeriesProgress = 0;
      this.currentItemKind = "";
      this.currentItemStage = "";
      this.currentItemStageLabelText = "";
      this.currentItemDetail = "";
      this.currentItemProgressValue = null;
    },

    currentSeriesNameFromData: function (data) {
      if (!data) {
        return "";
      }
      var seriesName = data.current_series_name || data.current_series || "";
      return seriesName != null ? String(seriesName) : "";
    },

    applyCurrentSeriesProgressState: function (data) {
      if (this.applyExplicitCurrentItemState(data)) {
        return;
      }
      var nextSeriesName = this.currentSeriesNameFromData(data);
      if (nextSeriesName) {
        var seriesChanged = nextSeriesName !== this.currentSeriesName;
        this.currentSeriesName = nextSeriesName;
        if (seriesChanged) {
          this.currentSeriesProgress = 0;
        }
        this.currentSeriesProgress = this.nextCurrentSeriesProgress(data, seriesChanged);
      }
    },

    applyExplicitCurrentItemState: function (data) {
      if (!data || !data.current_item_kind) {
        return false;
      }
      this.currentItemKind = String(data.current_item_kind || "");
      this.currentItemStage = String(data.current_item_stage || "");
      this.currentItemStageLabelText = String(data.current_item_stage_label || "");
      this.currentItemDetail = String(data.current_item_detail || "");

      if (!data.current_file_name) {
        this.currentFileName = "";
        this.currentFileStage = "";
        this.currentFileProgress = 0;
        this.currentFileProgressCurrent = null;
        this.currentFileProgressTotal = null;
        this.currentFileProgressUnit = "";
      }

      var explicitProgress =
        typeof data.current_item_progress_pct === "number" &&
        !Number.isNaN(data.current_item_progress_pct)
          ? Math.max(0, Math.min(100, Math.round(data.current_item_progress_pct)))
          : null;
      this.currentItemProgressValue = explicitProgress;

      var nextSeriesName = this.currentSeriesNameFromData(data);
      if (nextSeriesName) {
        var seriesChanged = nextSeriesName !== this.currentSeriesName;
        this.currentSeriesName = nextSeriesName;
        this.currentSeriesProgress =
          explicitProgress != null
            ? explicitProgress
            : this.nextCurrentSeriesProgress(data, seriesChanged);
      } else if (this.currentItemKind !== "file") {
        this.currentSeriesName = "";
        this.currentSeriesProgress = explicitProgress != null ? explicitProgress : 0;
      }
      return true;
    },

    nextCurrentSeriesProgress: function (data, seriesChanged) {
      var currentProgress = Number(this.currentSeriesProgress) || 0;
      var message =
        data && typeof data.message === "string" ? String(data.message).toLowerCase() : "";
      if (message.indexOf("still fetching comicvine metadata") !== -1) {
        return seriesChanged ? 24 : Math.min(60, Math.max(24, currentProgress + 12));
      }
      if (message.indexOf("fetching comicvine metadata") !== -1) {
        return 8;
      }
      if (message.indexOf("preparing series records") !== -1) {
        return Math.max(currentProgress, 72);
      }
      if (message.indexOf("processed ") !== -1 && message.indexOf("review groups") !== -1) {
        return 100;
      }
      return seriesChanged ? 8 : Math.max(currentProgress, 8);
    },

    currentFileProgressBarTransitionClass: function () {
      return this.animateCurrentFileProgressBar ? "transition-all duration-300" : "transition-none";
    },

    applyCurrentFileProgressState: function (data, progressRevision) {
      var nextFileName = String(data.current_file_name || "");
      var nextProgress =
        typeof data.current_file_progress_pct === "number" && !Number.isNaN(data.current_file_progress_pct)
          ? Math.max(0, Math.min(100, Math.round(data.current_file_progress_pct)))
          : 0;
      this.animateCurrentFileProgressBar =
        !!this.currentFileName &&
        nextFileName === this.currentFileName &&
        Number(this.currentFileProgress) < 100 &&
        nextProgress < 100;
      this.currentFileName = nextFileName;
      this.currentFileStage = String(data.current_file_stage || "");
      this.currentFileProgress = nextProgress;
      this.currentFileProgressCurrent =
        data.current_file_progress_current != null
          ? Number(data.current_file_progress_current)
          : null;
      this.currentFileProgressTotal =
        data.current_file_progress_total != null
          ? Number(data.current_file_progress_total)
          : null;
      this.currentFileProgressUnit = String(data.current_file_progress_unit || "");
      this.currentItemKind = String(data.current_item_kind || "file");
      this.currentItemStage = String(data.current_item_stage || data.current_file_stage || "");
      this.currentItemStageLabelText = String(data.current_item_stage_label || "");
      this.currentItemDetail = String(data.current_item_detail || "");
      this.currentItemProgressValue = nextProgress;
      if (typeof progressRevision === "number" && progressRevision > 0) {
        this.fileProgressRevision = progressRevision;
      }
    },

    clearPendingCurrentFileTransition: function () {
      if (this.pendingCurrentFileTimer) {
        window.clearTimeout(this.pendingCurrentFileTimer);
        this.pendingCurrentFileTimer = null;
      }
      this.pendingCurrentFileEvent = null;
      this.pendingCurrentFileEventType = "";
    },

    shouldDelayCompletedFileTransition: function (data) {
      if (!this.isImportMode()) {
        return false;
      }
      if (!data || !this.currentFileName) {
        return false;
      }
      if (Number(this.currentFileProgress) < 100) {
        return false;
      }
      if (String(data.current_file_name) === String(this.currentFileName)) {
        return false;
      }
      if (!data.current_file_name && data.current_item_kind) {
        var explicitItemProgress = Number(data.current_item_progress_pct);
        if (Number.isFinite(explicitItemProgress) && explicitItemProgress < 100) {
          return false;
        }
      }
      var currentStatus =
        data && typeof data.status === "string" ? String(data.status).toLowerCase() : "";
      var modeValue =
        data && typeof data.mode === "string" && data.mode.length > 0
          ? data.mode
          : this.progressMode;
      if (this.isTerminalStatus(currentStatus, modeValue)) {
        return false;
      }
      return !!(
        data.current_file_name ||
        this.currentSeriesNameFromData(data) ||
        data.current_item_kind
      );
    },

    flushPendingCurrentFileTransition: function () {
      var pendingEvent = this.pendingCurrentFileEvent;
      var pendingEventType = this.pendingCurrentFileEventType;
      this.pendingCurrentFileEvent = null;
      this.pendingCurrentFileEventType = "";
      this.pendingCurrentFileTimer = null;
      if (!pendingEvent) {
        return;
      }
      if (pendingEventType === "job") {
        this.applyJobStateNow(pendingEvent, pendingEvent.recent_logs || null);
        return;
      }
      this.applyEphemeralFileProgressNow(pendingEvent);
    },

    queueCompletedFileTransition: function (data, eventType) {
      this.pendingCurrentFileEvent = Object.assign({}, data);
      this.pendingCurrentFileEventType = eventType;
      if (this.pendingCurrentFileTimer) {
        return true;
      }

      var self = this;
      this.pendingCurrentFileTimer = window.setTimeout(function () {
        self.flushPendingCurrentFileTransition();
      }, _COMPLETED_FILE_DWELL_MS);
      return true;
    },

    shouldRetainCurrentFileProgress: function (data) {
      if (!this.isImportMode()) {
        return false;
      }
      if (!this.currentFileName) {
        return false;
      }
      if (data && data.current_file_name) {
        return false;
      }
      if (this.currentSeriesNameFromData(data)) {
        return false;
      }

      var currentStatus =
        data && typeof data.status === "string" ? String(data.status).toLowerCase() : "";
      return currentStatus === "importing";
    },

    applyEphemeralFileProgress: function (data) {
      if (!data || !data.ephemeral_progress) {
        return;
      }
      if (this.shouldDelayCompletedFileTransition(data)) {
        this.queueCompletedFileTransition(data, "ephemeral");
        return;
      }
      this.applyEphemeralFileProgressNow(data);
    },

    applyEphemeralFileProgressNow: function (data) {
      if (this.completed || this.failed) {
        return;
      }
      var activeStatus = String(data.status || "").toLowerCase();
      if (
        ["scanning", "analyzing", "matching", "file_matching", "importing", "rolling_back"].indexOf(
          activeStatus,
        ) === -1
      ) {
        return;
      }

      var incomingRevision =
        typeof data.progress_revision === "number" && !Number.isNaN(data.progress_revision)
          ? data.progress_revision
          : 0;
      if (incomingRevision > 0 && this.progressRevision >= incomingRevision) {
        return;
      }
      if (incomingRevision > 0 && this.fileProgressRevision >= incomingRevision) {
        return;
      }
      if (incomingRevision > 0) {
        this.progressRevision = incomingRevision;
        this.fileProgressRevision = incomingRevision;
      }

      this.jobStatus = activeStatus;
      this.phase = String(data.phase || activeStatus);
      this.phaseLabel = this.phaseLabelForKey(this.phase);
      if (typeof data.progress === "number" && !Number.isNaN(data.progress)) {
        this.progress = Math.max(this.progress, Math.max(0, Math.min(100, Math.round(data.progress))));
      }
      if (typeof data.message === "string" && data.message.length > 0) {
        this.message = data.message;
      }
      this.applyCurrentSeriesProgressState(data);
      if (!data.current_file_name) {
        this.resetCurrentFileProgress();
        if (incomingRevision > 0) {
          this.fileProgressRevision = incomingRevision;
        }
        return;
      }
      this.applyCurrentFileProgressState(data, incomingRevision);
    },

    showActionBar: function () {
      if (this.isScanMode() || this.isImportMode() || this.isRollbackMode()) {
        return (
          this.showPauseAction() ||
          this.showResumeAction() ||
          this.showRetryStoryArcPlacementsAction() ||
          this.showCancelAction() ||
          this.failed ||
          this.completed
        );
      }
      return this.failed || this.completed;
    },

    _activeStartedAtIso: function (data) {
      if (this.isImportMode() || this.isRollbackMode()) {
        return (data && data.import_started_at) || this.startedAt;
      }
      return (data && data.scan_started_at) || this.startedAt;
    },

    _computeEtaSeconds: function (startedAtIso, progress) {
      if (!startedAtIso || progress == null || progress <= 0 || progress >= 100) {
        return null;
      }
      var startedAtMs = new Date(startedAtIso).getTime();
      if (!Number.isFinite(startedAtMs)) {
        return null;
      }
      var elapsedSeconds = Math.floor((Date.now() - startedAtMs) / 1000);
      if (elapsedSeconds < 2) {
        return null;
      }
      return Math.max(0, Math.round((elapsedSeconds * (100 - progress)) / progress));
    },

    _computeElapsedSeconds: function (startedAtIso) {
      if (!startedAtIso) {
        return null;
      }
      var startedAtMs = new Date(startedAtIso).getTime();
      if (!Number.isFinite(startedAtMs)) {
        return null;
      }
      return Math.max(0, Math.floor((Date.now() - startedAtMs) / 1000));
    },

    captureEtaState: function (data) {
      var startedAtIso = this._activeStartedAtIso(data);
      if (startedAtIso) {
        this.startedAt = startedAtIso;
      }

      if (data && data.elapsed_seconds != null) {
        this.elapsedSeconds = Number(data.elapsed_seconds);
        this.elapsedCapturedAt = Date.now();
      } else {
        this.elapsedSeconds = this._computeElapsedSeconds(this.startedAt);
        this.elapsedCapturedAt = Date.now();
      }

      if (data && data.estimated_seconds_remaining != null) {
        this.etaSeconds = Number(data.estimated_seconds_remaining);
        this.etaCapturedAt = Date.now();
        return;
      }

      var computed = this._computeEtaSeconds(this.startedAt, this.progress);
      this.etaSeconds = computed;
      this.etaCapturedAt = Date.now();
    },

    formatDurationLabel: function (totalSeconds) {
      if (totalSeconds == null || !Number.isFinite(Number(totalSeconds))) {
        return "";
      }
      var duration = Math.max(0, Math.round(Number(totalSeconds)));
      if (duration < 60) {
        return duration + "s";
      }
      var mins = Math.floor(duration / 60);
      var secs = duration % 60;
      if (mins < 60) {
        return mins + "m " + secs + "s";
      }
      var hrs = Math.floor(mins / 60);
      mins = mins % 60;
      return hrs + "h " + mins + "m";
    },

    formatElapsedLabel: function () {
      if (this.failed || this.completed || this.isPausePresentationState()) {
        return "";
      }
      if (this.elapsedSeconds == null) {
        return "";
      }
      var elapsed =
        Math.max(0, Number(this.elapsedSeconds)) +
        Math.max(0, Math.floor((this.nowMs - this.elapsedCapturedAt) / 1000));
      if (elapsed < 1) {
        return "";
      }
      return "Elapsed: " + this.formatDurationLabel(elapsed);
    },

    formatEtaLabel: function () {
      if (this.failed || this.completed || this.isPausePresentationState()) {
        return "";
      }
      if (this.progress <= 0) {
        return "";
      }
      if (!this.startedAt && this.etaSeconds == null) {
        return "";
      }
      if (this.progress < 2) {
        return "Estimating...";
      }

      var etaSeconds =
        this.etaSeconds != null
          ? Math.max(
              0,
              this.etaSeconds -
                Math.max(0, Math.floor((this.nowMs - this.etaCapturedAt) / 1000)),
            )
          : this._computeEtaSeconds(this.startedAt, this.progress);

      if (etaSeconds == null) {
        return "Estimating...";
      }
      if (etaSeconds <= 0) {
        return "Less than 1m";
      }
      if (etaSeconds < 60) {
        return "~" + etaSeconds + "s left";
      }

      var mins = Math.floor(etaSeconds / 60);
      var secs = etaSeconds % 60;
      if (mins < 60) {
        return "~" + mins + "m " + secs + "s left";
      }

      var hrs = Math.floor(mins / 60);
      mins = mins % 60;
      return "~" + hrs + "h " + mins + "m left";
    },

    get elapsedLabel() {
      return this.formatElapsedLabel();
    },

    get phaseIndex() {
      var idx = this.phases.findIndex(function (phaseDef) {
        return phaseDef.key === this.phase;
      }, this);
      return idx >= 0 ? idx : 0;
    },

    get etaLabel() {
      return this.formatEtaLabel();
    },

    get remaining() {
      var done = (this.stats.series_imported || 0) + (this.stats.series_failed || 0);
      return Math.max(0, this.totalSelected - done);
    },

    emitFooterState: function () {
      window.dispatchEvent(
        new CustomEvent("import:collection-footer", {
          detail: {
            step: this.isImportMode() || this.isRollbackMode() ? 4 : 2,
            phase: this.failed
              ? "failed"
              : this.completed
                ? this.isImportMode() || this.isRollbackMode()
                  ? "completed"
                  : "review"
                : this.phase,
            progress: this.progress,
            reviewSummary: {
              series_total: this.reviewSummary.seriesTotal || 0,
              series_in_library: this.reviewSummary.inLibrary || 0,
              series_matched: this.reviewSummary.matched || 0,
              files_duplicate: this.reviewSummary.duplicateCopies || 0,
              series_no_match: this.reviewSummary.noMatch || 0,
              files_total: this.reviewSummary.filesTotal || 0,
              files_conflict: this.reviewSummary.conflicts || 0,
            },
            stats: {
              series_found: this.stats.series_found || 0,
              series_duplicate: this.stats.series_duplicate || 0,
              series_matched: this.stats.series_matched || 0,
              series_no_match: this.stats.series_no_match || 0,
              series_imported: this.stats.series_imported || 0,
              series_failed: this.stats.series_failed || 0,
            },
          },
        })
      );
    },

    updateStats: function (data) {
      var fields = [
        "scan_total_files",
        "scan_total_dirs",
        "series_found",
        "series_duplicate",
        "series_matched",
        "series_no_match",
        "series_new",
        "series_imported",
        "series_failed",
        "total_files_imported",
        "total_files_failed",
      ];

      for (var i = 0; i < fields.length; i++) {
        if (data[fields[i]] != null) {
          this.stats[fields[i]] = data[fields[i]];
        }
      }

      if (data.total_files_found != null) this.fileStats.total = data.total_files_found;
      if (data.total_files_matched != null) this.fileStats.matched = data.total_files_matched;
      if (data.total_files_conflict != null) this.fileStats.conflicts = data.total_files_conflict;
      if (data.total_files_no_match != null) this.fileStats.no_match = data.total_files_no_match;
    },

    isActiveScanSummaryStatus: function () {
      return (
        !this.isImportMode() &&
        !this.isRollbackMode() &&
        [
          "pending",
          "scanning",
          "pausing",
          "paused",
          "analyzing",
          "matching",
          "file_matching",
        ].indexOf(String(this.jobStatus || "").toLowerCase()) !== -1
      );
    },

    numberOrZero: function (value) {
      return value != null ? Number(value) || 0 : 0;
    },

    mergeScanSummaryMetric: function (summaryValue, liveValue) {
      var summaryMetric = this.numberOrZero(summaryValue);
      if (!this.isActiveScanSummaryStatus()) {
        return summaryMetric;
      }
      return Math.max(summaryMetric, this.numberOrZero(liveValue));
    },

    updateReviewSummary: function (data) {
      var summary = data && data.review_summary ? data.review_summary : null;
      if (summary) {
        if (summary.series_total != null) {
          this.reviewSummary.seriesTotal = this.mergeScanSummaryMetric(
            summary.series_total,
            this.stats.series_found,
          );
        }
        if (summary.series_in_library != null) {
          this.reviewSummary.inLibrary = this.mergeScanSummaryMetric(
            summary.series_in_library,
            this.stats.series_duplicate,
          );
        }
        if (summary.series_matched != null) {
          this.reviewSummary.matched = this.mergeScanSummaryMetric(
            summary.series_matched,
            this.stats.series_matched,
          );
        }
        if (summary.files_duplicate != null) {
          this.reviewSummary.duplicateCopies = this.mergeScanSummaryMetric(
            summary.files_duplicate,
            data && data.total_files_duplicate,
          );
        }
        if (summary.series_no_match != null) {
          this.reviewSummary.noMatch = this.mergeScanSummaryMetric(
            summary.series_no_match,
            this.stats.series_no_match,
          );
        }
        if (summary.files_total != null) {
          this.reviewSummary.filesTotal = this.mergeScanSummaryMetric(
            summary.files_total,
            data && data.scan_total_files,
          );
        }
        if (summary.files_conflict != null) {
          this.reviewSummary.conflicts = this.mergeScanSummaryMetric(
            summary.files_conflict,
            data && data.total_files_conflict,
          );
        }
        return;
      }

      this.reviewSummary.seriesTotal = Number(this.stats.series_found) || 0;
      this.reviewSummary.inLibrary = Number(this.stats.series_duplicate) || 0;
      this.reviewSummary.matched = Number(this.stats.series_matched) || 0;
      this.reviewSummary.duplicateCopies =
        Number(data && data.total_files_duplicate) || Number(this.reviewSummary.duplicateCopies) || 0;
      this.reviewSummary.noMatch = Number(this.stats.series_no_match) || 0;
      this.reviewSummary.filesTotal =
        Number(data && data.review_files_total) ||
        Number(data && data.scan_total_files) ||
        Number(this.reviewSummary.filesTotal) ||
        Number(this.stats.scan_total_files) ||
        0;
      this.reviewSummary.conflicts =
        Number(data && data.total_files_conflict) || Number(this.fileStats.conflicts) || 0;
    },

    latestLogEntry: function (entries) {
      if (!Array.isArray(entries) || entries.length === 0) {
        return null;
      }
      return entries[entries.length - 1];
    },

    jobReadToProgressState: function (job) {
      if (!job || typeof job !== "object") {
        return null;
      }
      var snapshot =
        job.progress_snapshot && typeof job.progress_snapshot === "object"
          ? Object.assign({}, job.progress_snapshot)
          : {};

      snapshot.status =
        typeof job.status === "string" && job.status.length > 0
          ? String(job.status).toLowerCase()
          : String(snapshot.status || "");
      snapshot.mode =
        typeof snapshot.mode === "string" && snapshot.mode.length > 0
          ? snapshot.mode
          : this.progressMode;
      snapshot.error_message =
        typeof job.error_message === "string" ? job.error_message : snapshot.error_message;

      var topLevelFields = [
        "scan_total_files",
        "scan_total_dirs",
        "series_found",
        "series_duplicate",
        "series_matched",
        "series_no_match",
        "series_new",
        "series_imported",
        "series_failed",
        "total_files_imported",
        "total_files_failed",
        "total_files_found",
        "total_files_matched",
        "total_files_duplicate",
        "total_files_already_owned",
        "total_files_conflict",
        "total_files_no_match",
        "progress_revision",
        "import_started_at",
        "scan_started_at",
      ];
      for (var i = 0; i < topLevelFields.length; i++) {
        var field = topLevelFields[i];
        if (job[field] != null && snapshot[field] == null) {
          snapshot[field] = job[field];
        }
      }

      return snapshot;
    },

    phaseMetaForStatus: function (status, data) {
      var currentStatus = String(status || "").toLowerCase();
      var modeValue =
        data && typeof data.mode === "string" && data.mode.length > 0 ? data.mode : this.progressMode;
      var explicitPhase = data && typeof data.phase === "string" ? data.phase : "";
      var explicitProgress =
        data && typeof data.progress === "number" && !Number.isNaN(data.progress) ? data.progress : null;
      var explicitMessage = data && typeof data.message === "string" ? data.message : "";
      var scanMatchTotal =
        (Number(this.stats.series_duplicate) || 0) +
        (Number(this.stats.series_matched) || 0) +
        (Number(this.stats.series_no_match) || 0);
      var fileMatchTotal =
        (Number(this.fileStats.matched) || 0) +
        (Number(this.fileStats.conflicts) || 0) +
        (Number(this.fileStats.no_match) || 0);
      var importTotal = Number(this.totalSelected) || 0;
      var importDone = (Number(this.stats.series_imported) || 0) + (Number(this.stats.series_failed) || 0);

      if (
        explicitPhase &&
        !this.isTerminalStatus(currentStatus, modeValue) &&
        ["pausing", "paused", "cancelling"].indexOf(currentStatus) === -1 &&
        explicitProgress != null
      ) {
        return {
          phase: explicitPhase,
          phaseLabel: this.phaseLabelForKey(explicitPhase),
          progress: explicitProgress,
          message: explicitMessage || this.message,
        };
      }

      switch (currentStatus) {
        case "pending":
        case "scanning":
          return {
            phase: explicitPhase || (Number(this.stats.series_found) > 0 ? "scanning" : "inventory"),
            phaseLabel: this.phaseLabelForKey(
              explicitPhase || (Number(this.stats.series_found) > 0 ? "scanning" : "inventory")
            ),
            progress:
              Number(this.stats.series_found) > 0
                ? Math.min(35, 10 + Number(this.stats.series_found))
                : Number(this.stats.scan_total_files) > 0 || Number(this.stats.scan_total_dirs) > 0
                  ? 10
                  : 0,
            message: explicitMessage || "Preparing scan inventory...",
          };
        case "pausing":
        case "paused":
          return {
            phase: explicitPhase || (Number(this.stats.series_found) > 0 ? "scanning" : "inventory"),
            phaseLabel: "Paused",
            progress:
              explicitProgress != null
                ? explicitProgress
                : Number(this.stats.series_found) > 0
                  ? Math.min(35, 10 + Number(this.stats.series_found))
                  : Number(this.stats.scan_total_files) > 0 || Number(this.stats.scan_total_dirs) > 0
                    ? 10
                    : this.progress,
            message: explicitMessage || this.pausedMessage(),
          };
        case "analyzing":
          return {
            phase: "analyzing",
            phaseLabel: this.phaseLabelForKey("analyzing"),
            progress: explicitProgress != null ? explicitProgress : 35,
            message: explicitMessage || "Analyzing for duplicates...",
          };
        case "matching":
          return {
            phase: "matching",
            phaseLabel: this.phaseLabelForKey("matching"),
            progress:
              explicitProgress != null
                ? explicitProgress
                : Number(this.stats.series_found) > 0
                  ? 45 + Math.min(35, Math.floor((scanMatchTotal / Number(this.stats.series_found)) * 35))
                  : 45,
            message: explicitMessage || "Matching against ComicVine...",
          };
        case "file_matching":
          return {
            phase: "file_matching",
            phaseLabel: this.phaseLabelForKey("file_matching"),
            progress:
              explicitProgress != null
                ? explicitProgress
                : Number(this.fileStats.total) > 0
                  ? 80 + Math.min(19, Math.floor((fileMatchTotal / Number(this.fileStats.total)) * 19))
                  : 80,
            message: explicitMessage || "Matching files to issues...",
          };
        case "importing":
          return {
            phase: "importing",
            phaseLabel: this.phaseLabelForKey("importing"),
            progress:
              explicitProgress != null
                ? explicitProgress
                : importTotal > 0
                  ? Math.min(99, Math.floor((importDone / importTotal) * 100))
                  : this.progress,
            message: explicitMessage || "Adding series via ComicVine...",
          };
        case "cancelling":
          return {
            phase: "importing",
            phaseLabel: "Cancelling import...",
            progress: explicitProgress != null ? explicitProgress : this.progress,
            message: explicitMessage || "Finishing the current safe step before cancelling.",
          };
        case "review":
          return {
            phase: "review",
            phaseLabel: this.phaseLabelForKey("review"),
            progress: 100,
            message: explicitMessage || "Ready for review",
          };
        case "completed":
          return {
            phase: explicitPhase || "done",
            phaseLabel: "Complete",
            progress: 100,
            message: explicitMessage || "Import complete.",
          };
        case "rolling_back":
          return {
            phase: "rollback",
            phaseLabel: this.phaseLabelForKey("rollback"),
            progress: explicitProgress != null ? explicitProgress : this.progress,
            message: explicitMessage || "Rolling back import actions...",
          };
        case "rolled_back":
          return {
            phase: "rollback",
            phaseLabel: "Rollback complete",
            progress: 100,
            message: "Import rollback completed.",
          };
        case "failed":
          return {
            phase: "done",
            phaseLabel: this.phaseLabelForKey("done"),
            progress: 100,
            message: (data && data.error_message) || "Import failed.",
          };
        case "cancelled":
          return {
            phase: "done",
            phaseLabel: this.phaseLabelForKey("done"),
            progress: 100,
            message: "Import cancelled by user.",
          };
        default:
          return {
            phase: this.phase,
            phaseLabel: this.phaseLabel,
            progress: this.progress,
            message: this.message,
          };
      }
    },

    applyJobState: function (data, recentLogs) {
      if (!data) {
        return;
      }
      if (data.ephemeral_progress && data.current_file_name) {
        this.applyEphemeralFileProgress(data);
        return;
      }
      if (this.shouldDelayCompletedFileTransition(data)) {
        var queuedData = Object.assign({}, data);
        if (recentLogs) {
          queuedData.recent_logs = recentLogs;
        }
        this.queueCompletedFileTransition(queuedData, "job");
        return;
      }
      this.applyJobStateNow(data, recentLogs);
    },

    applyJobStateNow: function (data, recentLogs) {
      if (!data) {
        return;
      }
      this.jobStatus =
        typeof data.status === "string" && data.status.length > 0
          ? String(data.status).toLowerCase()
          : this.jobStatus;

      var incomingRevision =
        typeof data.progress_revision === "number" && !Number.isNaN(data.progress_revision)
          ? data.progress_revision
          : 0;
      if (incomingRevision > 0 && this.progressRevision > incomingRevision) {
        return;
      }
      if (incomingRevision > 0) {
        this.progressRevision = incomingRevision;
      }
      if (
        this.pendingCurrentFileTimer &&
        (!this.isImportMode() ||
          this.isTerminalStatus(this.jobStatus, typeof data.mode === "string" ? data.mode : this.progressMode))
      ) {
        this.clearPendingCurrentFileTransition();
      }

      if (typeof data.mode === "string" && data.mode.length > 0) {
        this.progressMode = data.mode;
      }

      if (data.control_state && typeof data.control_state === "object") {
        this.controlState = data.control_state;
      }
      var cancellationAction =
        (data.control_state && typeof data.control_state.requested_action === "string"
          ? data.control_state.requested_action
          : typeof data.requested_action === "string"
            ? data.requested_action
            : ""
        ).toLowerCase();
      if (
        this.jobStatus === "cancelling" ||
        (this.jobStatus === "rolling_back" && cancellationAction === "cancel") ||
        cancellationAction === "cancel"
      ) {
        this.cancelling = true;
      } else if (
        ["review", "completed", "failed", "cancelled", "rolled_back"].indexOf(this.jobStatus) !== -1
      ) {
        this.cancelling = false;
      }
      if (this.optimisticPauseRequested) {
        var requestedAction =
          (data.control_state && typeof data.control_state.requested_action === "string"
            ? data.control_state.requested_action
            : typeof data.requested_action === "string"
              ? data.requested_action
              : ""
          ).toLowerCase();
        var currentStatus = String(data.status || "").toLowerCase();
        if (
          this.controlState.can_resume ||
          currentStatus === "paused" ||
          ["completed", "failed", "cancelled", "rolled_back"].indexOf(currentStatus) !== -1 ||
          requestedAction !== "pause"
        ) {
          this.optimisticPauseRequested = false;
        }
      }
      this.updateStats(data);
      this.updateReviewSummary(data);
      this.captureEtaState(data);

      var lastLog = this.latestLogEntry(recentLogs || data.recent_logs);
      var meta = this.phaseMetaForStatus(data.status, data);

      this.phase = meta.phase || this.phase;
      this.phaseLabel = meta.phaseLabel || this.phaseLabel;
      this.progress = typeof meta.progress === "number" ? meta.progress : this.progress;

      var fallbackMessage = meta.message || "";
      var explicitMessage =
        typeof data.message === "string" && data.message.length > 0 ? data.message : "";
      this.message =
        explicitMessage ||
        data.error_message ||
        fallbackMessage ||
        (lastLog && lastLog.message) ||
        this.message;

      this.applyCurrentSeriesProgressState(data);
      if (data.current_file_name) {
        var shouldApplyDurableFileProgress =
          !this.currentFileName ||
          incomingRevision <= 0 ||
          incomingRevision >= this.fileProgressRevision;
        if (shouldApplyDurableFileProgress) {
          this.applyCurrentFileProgressState(data, incomingRevision);
        }
      } else if (!this.shouldRetainCurrentFileProgress(data)) {
        this.resetCurrentFileProgress();
      }

      if (data.status === "review" && !this.isImportMode() && !this.isRollbackMode()) {
        this.optimisticPauseRequested = false;
        this.completed = true;
        this.failed = false;
        this._completedJobStatus = "review";
        this.emitFooterState();
        this.stopPolling();
        return;
      }

      if (data.status === "completed") {
        this.optimisticPauseRequested = false;
        this.completed = true;
        this.failed = false;
        this._completedJobStatus = "completed";
        this.emitFooterState();
        this.stopPolling();
        return;
      }

      if (data.status === "rolled_back") {
        this.optimisticPauseRequested = false;
        this.completed = true;
        this.failed = false;
        this._completedJobStatus = "rolled_back";
        purgeImportClientState(this.jobId);
        this.emitFooterState();
        this.stopPolling();
        return;
      }

      if (data.status === "cancelled") {
        this.optimisticPauseRequested = false;
        this.failed = false;
        this.completed = false;
        this.resetCurrentItemProgress();
        this.emitFooterState();
        this.returnToImportStartAfterCancellation();
        return;
      }

      if (data.status === "failed") {
        this.optimisticPauseRequested = false;
        this.failed = true;
        this.completed = false;
        this.resetCurrentItemProgress();
        this.emitFooterState();
        this.stopPolling();
        return;
      }

      this.failed = false;
      this.completed = false;
      this._completedJobStatus = null;
      this.emitFooterState();
    },

    hydrateFromSnapshot: function () {
      var snapshot = this.initialSnapshot;
      if (!snapshot) {
        return;
      }
      this.applyJobState(snapshot, snapshot.recent_logs);
    },

    returnToImportStartAfterCancellation: function () {
      if (this.cancelReturnStarted) {
        return;
      }
      this.cancelReturnStarted = true;
      this.cancelling = true;
      this.stopPolling();
      this.stopClock();
      this.disconnectSSE("cancelled");
      purgeImportClientState(this.jobId);
      window.location.replace("/import?tab=collection");
    },

    startClock: function () {
      var self = this;
      if (self.clockTimer || self.completed || self.failed) {
        return;
      }
      self.clockTimer = window.setInterval(function () {
        self.nowMs = Date.now();
      }, 1000);
    },

    stopClock: function () {
      if (this.clockTimer) {
        window.clearInterval(this.clockTimer);
        this.clockTimer = null;
      }
    },

    startPolling: function () {
      var self = this;
      if (self.pollTimer || self.completed || self.failed) {
        return;
      }
      self.pollTimer = window.setInterval(function () {
        self.nowMs = Date.now();
        self.pollJobStatus();
      }, 1000);
    },

    stopPolling: function () {
      if (this.pollTimer) {
        window.clearInterval(this.pollTimer);
        this.pollTimer = null;
      }
    },

    init: function () {
      this.hydrateFromSnapshot();
      this.nowMs = Date.now();
      if (this.completed || this.failed || this.cancelReturnStarted) {
        return;
      }
      this.startClock();
      this.pollJobStatus();
      this.connectSSE();
    },

    disconnectSSE: function (reason) {
      var source = this.evtSource;
      var registryId = this.evtSourceRegistryId;
      this.evtSource = null;
      this.evtSourceRegistryId = null;
      if (registryId) {
        _importEventSourceRegistry.close(registryId, source, reason || "component-disconnect");
      } else if (source) {
        source.close();
      }
    },

    connectSSE: function () {
      var self = this;
      if (self.evtSource) {
        self.disconnectSSE("reconnect");
      }
      var source = new EventSource("/api/v1/import/" + self.jobId + "/stream");
      var registryId = _importEventSourceRegistry.register(source, {
        onClose: function (closedSource) {
          if (self.evtSource === closedSource) {
            self.evtSource = null;
          }
          if (self.evtSourceRegistryId === registryId) {
            self.evtSourceRegistryId = null;
          }
        },
        resume: function () {
          if (!self.completed && !self.failed && self.jobId > 0) {
            self.connectSSE();
            self.pollJobStatus();
          }
        },
      });
      self.evtSource = source;
      self.evtSourceRegistryId = registryId;

      var handleProgressEvent = function (event) {
        if (self.evtSource !== source) {
          return;
        }
        var data = JSON.parse(event.data);
        if (data.heartbeat) {
          return;
        }
        self.nowMs = Date.now();
        self.applyJobState(data, null);

        if (self.cancelReturnStarted) {
          return;
        }

        if (self.nextStep === 5 && self.totalSelected === 0 && data.series_found) {
          self.totalSelected = data.series_found;
        }

        if (data.status === "failed" || data.status === "cancelled") {
          self.failed = true;
          self.stopPolling();
          self.stopClock();
          self.disconnectSSE("terminal");
          return;
        }

        if (self.isTerminalStatus(data.status, data.mode)) {
          self.stopPolling();
          self.stopClock();
          self.disconnectSSE("terminal");
        }
      };

      source.addEventListener("progress", handleProgressEvent);
      source.onmessage = handleProgressEvent;

      source.onerror = function () {
        if (self.evtSource !== source) {
          return;
        }
        self.disconnectSSE("error");
        self.startPolling();
        self.pollJobStatus();
      };
    },

    pollJobStatus: async function () {
      try {
        this.nowMs = Date.now();
        var response = await fetch("/import/" + this.jobId + "/progress-state");

        if (!response.ok) {
          if (response.status === 404 && this.cancelling) {
            this.returnToImportStartAfterCancellation();
          }
          return;
        }

        var job = await response.json();
        this.applyJobState(job, job.recent_logs || []);

        if (this.cancelReturnStarted) {
          return;
        }

        if (this.isTerminalStatus(job.status, job.mode)) {
          this.stopPolling();
          this.stopClock();
        } else if (job.status === "failed" || job.status === "cancelled") {
          this.stopPolling();
          this.stopClock();
        }
      } catch (_) {
        // Ignore polling errors.
      }
    },

    cancelAndRestart: async function () {
      var confirmed = await pbConfirm({
        title: "Cancel Import",
        message: "This will discard the scan results. You can start a new import at any time.",
        confirmText: "Cancel Import",
        destructive: true,
      });
      if (!confirmed) {
        return;
      }

      try {
        var discardCompletedScan = this.completed && this.nextStep === 3;
        var endpoint = discardCompletedScan
          ? "/api/v1/import/" + this.jobId
          : "/api/v1/import/" + this.jobId + "/cancel";
        var method = discardCompletedScan ? "DELETE" : "POST";
        var response = await fetch(endpoint, {
          method: method,
          headers: { "X-CSRF-Token": readCsrfTokenFromBody() },
        });

        if (!response.ok) {
          var error = await response
            .json()
            .catch(function () {
              return { detail: "Failed to discard import job." };
            });
          throw new Error(error.detail || "Failed to discard import job.");
        }

        purgeImportClientState(this.jobId);
        window.location.replace("/import");
      } catch (err) {
        showToast({
          message:
            (err && err.message) || "Failed to discard the scan results. Please try again.",
          level: "error",
        });
      }
    },

    advanceToReview: function () {
      dispatchImportWizardAdvance({
        step: this.nextStep,
        jobStatus: this._completedJobStatus || "review",
      });
    },

    continueToResults: function () {
      if (this.continuing) {
        return;
      }
      this.continuing = true;
      dispatchImportWizardAdvance({
        step: this.nextStep,
        jobStatus: this._completedJobStatus || "completed",
      });
    },

    pauseRun: async function () {
      if (!this.jobId || this.pausing || !this.showPauseAction()) {
        return;
      }
      this.pausing = true;
      try {
        var response = await fetch("/api/v1/import/" + this.jobId + "/pause", {
          method: "POST",
          headers: { "X-CSRF-Token": readCsrfTokenFromBody() },
        });
        if (!response.ok) {
          var error = await response.json().catch(function () {
            return {};
          });
          throw new Error(error.detail || "Failed to pause " + this.runNoun() + ".");
        }
        var job = await response.json().catch(function () {
          return null;
        });
        var snapshot = this.jobReadToProgressState(job);
        if (snapshot) {
          this.applyJobState(snapshot, snapshot.recent_logs || []);
          var responseStatus = String(snapshot.status || "").toLowerCase();
          var requestedAction = String(snapshot.requested_action || "").toLowerCase();
          this.optimisticPauseRequested = responseStatus !== "paused" && requestedAction === "pause";
        } else {
          this.controlState = Object.assign({}, this.controlState, {
            can_pause: false,
            can_resume: true,
            can_cancel: true,
            requested_action: "none",
          });
          this.jobStatus = "paused";
          this.message = this.pausedMessage();
          this.optimisticPauseRequested = false;
        }
      } catch (err) {
        this.optimisticPauseRequested = false;
        showToast({
          message: (err && err.message) || "Failed to pause " + this.runNoun() + ".",
          level: "error",
        });
      } finally {
        this.pausing = false;
      }
    },

    resumeRun: async function () {
      if (!this.jobId || !this.canResumeAction()) {
        return;
      }
      this.resuming = true;
      try {
        var response = await fetch("/api/v1/import/" + this.jobId + "/resume", {
          method: "POST",
          headers: { "X-CSRF-Token": readCsrfTokenFromBody() },
        });
        if (!response.ok) {
          var error = await response.json().catch(function () {
            return {};
          });
          throw new Error(error.detail || "Failed to resume " + this.runNoun() + ".");
        }
        var job = await response.json().catch(function () {
          return null;
        });
        this.optimisticPauseRequested = false;
        this.failed = false;
        this.completed = false;
        var snapshot = this.jobReadToProgressState(job);
        if (snapshot) {
          this.applyJobState(snapshot, snapshot.recent_logs || []);
        }
        this.startClock();
        this.startPolling();
        this.connectSSE();
      } catch (err) {
        showToast({
          message: (err && err.message) || "Failed to resume " + this.runNoun() + ".",
          level: "error",
        });
      } finally {
        this.resuming = false;
      }
    },

    retryStoryArcPlacements: async function () {
      if (
        !this.jobId ||
        this.retryingStoryArcPlacements ||
        !this.showRetryStoryArcPlacementsAction()
      ) {
        return;
      }

      this.retryingStoryArcPlacements = true;
      this.storyArcPlacementRetryError = "";
      this.storyArcPlacementRetrySuccess = "";
      try {
        var response = await fetch(
          "/api/v1/import/" + this.jobId + "/story-arc-placements/retry",
          {
            method: "POST",
            headers: { "X-CSRF-Token": readCsrfTokenFromBody() },
          },
        );
        var payload = await response.json().catch(function () {
          return {};
        });
        if (!response.ok) {
          var detail =
            payload && typeof payload.detail === "string"
              ? payload.detail
              : "Failed to retry Story Arc placements.";
          throw new Error(detail);
        }

        var retryingCount = Math.max(0, Number(payload.retrying_count) || 0);
        var placementLabel = retryingCount === 1 ? "placement" : "placements";
        this.controlState = Object.assign({}, this.controlState, {
          can_pause: false,
          can_resume: false,
          can_retry_story_arc_placements: false,
          can_cancel: true,
          requested_action: "none",
        });
        this.jobStatus = "importing";
        this.phase = "story_arc_placements";
        this.phaseLabel = this.phaseLabelForKey(this.phase);
        this.progress = 99;
        this.failed = false;
        this.completed = false;
        this.message = "Retrying " + retryingCount + " Story Arc " + placementLabel + "...";
        this.storyArcPlacementRetrySuccess =
          "Retry requested for " + retryingCount + " Story Arc " + placementLabel + ".";
        this.emitFooterState();
        this.startClock();
        this.startPolling();
        if (!this.evtSource) {
          this.connectSSE();
        }
      } catch (err) {
        var retryMessage =
          err && err.message
            ? err.message
            : "Failed to retry Story Arc placements. Please try again.";
        this.storyArcPlacementRetryError = retryMessage;
        if (typeof showToast === "function") {
          showToast({ message: retryMessage, level: "error" });
        }
      } finally {
        this.retryingStoryArcPlacements = false;
      }
    },

    cancelRun: async function () {
      if (
        !this.jobId ||
        this.cancelPrompting ||
        this.cancelling ||
        !this.showCancelAction()
      ) {
        return;
      }
      var cancelTitle = this.isScanMode()
        ? "Cancel scan"
        : this.isImportMode()
          ? "Cancel import"
          : "Cancel rollback";
      var cancelMessage = this.isScanMode()
        ? "This will stop the current scan and discard its scan results. You can start a new import at any time."
        : this.isImportMode()
          ? "This will stop the current import, roll back any imported changes, and move the job to history."
          : "This will stop the current rollback run.";
      this.cancelPrompting = true;
      var confirmed = false;
      try {
        confirmed = await pbConfirm({
          title: cancelTitle,
          message: cancelMessage,
          confirmText: this.cancelActionLabel(),
          destructive: true,
        });
      } finally {
        this.cancelPrompting = false;
      }
      if (!confirmed) {
        return;
      }

      this.cancelling = true;
      try {
        this.optimisticPauseRequested = false;
        var response = await fetch("/api/v1/import/" + this.jobId + "/cancel", {
          method: "POST",
          headers: { "X-CSRF-Token": readCsrfTokenFromBody() },
        });
        if (!response.ok) {
          var error = await response.json().catch(function () {
            return {};
          });
          throw new Error(error.detail || "Failed to cancel " + this.runNoun() + ".");
        }
        var job = await response.json().catch(function () {
          return null;
        });
        if (!job) {
          this.returnToImportStartAfterCancellation();
          return;
        }
        var snapshot = this.jobReadToProgressState(job);
        if (snapshot) {
          this.applyJobState(snapshot, snapshot.recent_logs || []);
        }
        if (!this.cancelReturnStarted) {
          this.startClock();
          this.startPolling();
          if (!this.evtSource) {
            this.connectSSE();
          }
        }
      } catch (err) {
        this.cancelling = false;
        showToast({
          message: (err && err.message) || "Failed to cancel " + this.runNoun() + ".",
          level: "error",
        });
      }
    },

    viewImportHistory: function () {
      window.location.assign("/import?tab=history");
    },

    startNewImport: function () {
      purgeImportClientState(this.jobId);
      window.location.assign("/import?tab=collection");
    },

    destroy: function () {
      this.stopPolling();
      this.stopClock();
      this.disconnectSSE("component-destroy");
    },
  };
}

function importSeriesDetailsModalData(config) {
  var cfg = config || {};

  return {
    open: true,
    jobId: cfg.jobId,
    seriesId: cfg.seriesId,

    close: function () {
      this.open = false;
      var modalHost = document.getElementById("import-series-details-modal-host");
      if (modalHost) {
        modalHost.innerHTML = "";
      }
    },

    refresh: function () {
      if (typeof htmx === "undefined") {
        window.location.assign("/import");
        return;
      }
      htmx.ajax(
        "GET",
        "/import/" + this.jobId + "/series/" + this.seriesId + "/details-partial",
        {
          target: "#import-series-details-modal-host",
          swap: "innerHTML",
        },
      );
    },

    toggleDuplicateFileSelection: async function (fileId, includeInImport) {
      try {
        var response = await fetch(
          "/api/v1/import/" + this.jobId + "/files/" + fileId + "/selection",
          {
            method: "PUT",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": readCsrfTokenFromBody(),
            },
            body: JSON.stringify({ include_in_import: includeInImport }),
          },
        );
        if (!response.ok) {
          var error = await response
            .json()
            .catch(function () {
              return { detail: "Failed to update file selection" };
            });
          throw new Error(error.detail || "Server error (" + response.status + ")");
        }

        this.refresh();
        window.dispatchEvent(new CustomEvent("import:review-summary-refresh"));
        window.dispatchEvent(new CustomEvent("import:review-refresh"));
      } catch (err) {
        if (typeof showToast === "function") {
          showToast({
            message:
              err && err.message ? err.message : "Failed to update file selection.",
            level: "error",
          });
        }
      }
    },

    bulkDuplicateFileSelection: async function (includeInImport) {
      try {
        var response = await fetch(
          "/api/v1/import/" + this.jobId + "/files/selection-bulk",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": readCsrfTokenFromBody(),
            },
            body: JSON.stringify({
              include_in_import: includeInImport,
              imported_series_id: this.seriesId,
            }),
          },
        );
        if (!response.ok) {
          var error = await response
            .json()
            .catch(function () {
              return { detail: "Failed to update file selection" };
            });
          throw new Error(error.detail || "Server error (" + response.status + ")");
        }

        this.refresh();
        window.dispatchEvent(new CustomEvent("import:review-summary-refresh"));
        window.dispatchEvent(new CustomEvent("import:review-refresh"));
      } catch (err) {
        if (typeof showToast === "function") {
          showToast({
            message:
              err && err.message ? err.message : "Failed to update file selection.",
            level: "error",
          });
        }
      }
    },

    openConflicts: function () {
      this.close();
      if (typeof showToast === "function") {
        showToast({
          message: "Use the Conflicts tab to review and resolve these files.",
          level: "info",
        });
      }
    },
  };
}

function importReviewData(configOrDefaultRootId, maybeJobId) {
  var cfg =
    configOrDefaultRootId &&
    typeof configOrDefaultRootId === "object" &&
    !Array.isArray(configOrDefaultRootId)
      ? configOrDefaultRootId
      : {
          defaultRootId: configOrDefaultRootId,
          jobId: maybeJobId,
        };

  return {
    matchedSelectedCount: Number(cfg.matchedSelectedCount) || 0,
    duplicateSelectedCount: Number(cfg.duplicateSelectedCount) || 0,
    duplicateImportableCount: Number(cfg.duplicateImportableCount) || 0,
    selectedItemCount:
      Number(cfg.selectedItemCount) ||
      (Number(cfg.matchedSelectedCount) || 0) + (Number(cfg.duplicateSelectedCount) || 0),
    importableItemCount: Number(cfg.importableItemCount) || 0,
    resolvedConflictGroupCount: Number(cfg.resolvedConflictGroupCount) || 0,
    conflictSeriesCount: Number(cfg.conflictSeriesCount) || 0,
    visibleFileConflictGroupCount: 0,
    confirming: false,
    confirmError: "",
    jobId: cfg.jobId,
    currentView: cfg.currentView || "series",
    conflictsCommitted: false,
    showCancelModal: false,
    cancelling: false,
    reviewToken: typeof cfg.reviewToken === "string" ? cfg.reviewToken : "",

    init: function () {
      this.rehydrateAfterShellSwap();
    },

    syncConflictPanelStateFromDom: function () {
      if (this.currentView !== "conflicts") {
        this.visibleFileConflictGroupCount = 0;
        return;
      }

      var root = document.getElementById("import-step-review-shell");
      if (!root) {
        this.visibleFileConflictGroupCount = 0;
        return;
      }

      var rows = root.querySelectorAll(
        "[data-testid='import-collection-conflicts'] " +
          "[data-import-conflict-kind='file_conflict'][data-import-conflict-group-id]",
      );
      var seen = Object.create(null);
      var count = 0;

      for (var i = 0; i < rows.length; i += 1) {
        var groupId = rows[i].getAttribute("data-import-conflict-group-id") || "";
        if (!groupId || seen[groupId]) {
          continue;
        }
        seen[groupId] = true;
        count += 1;
      }

      this.visibleFileConflictGroupCount = count;
    },

    rehydrateAfterShellSwap: function () {
      var root = document.getElementById("import-step-review-shell");
      var statusInput = root
        ? root.querySelector("input[name='review_status_filter']")
        : null;

      this.restoreSelection();
      this.currentView = statusInput && statusInput.value ? statusInput.value : "series";
      this.conflictsCommitted = this.resolvedConflictGroupCount > 0;
      this.syncConflictPanelStateFromDom();
      this.syncSelectionUi();
    },

    restoreSelection: function () {
      this.matchedSelectedCount = Number(this.matchedSelectedCount) || 0;
      this.duplicateSelectedCount = Number(this.duplicateSelectedCount) || 0;
      this.selectedItemCount =
        Number(this.selectedItemCount) ||
        this.matchedSelectedCount + this.duplicateSelectedCount;
    },

    totalSelectionCount: function () {
      return Number(this.selectedItemCount) || 0;
    },

    importSelectionLabel: function () {
      var total = this.totalSelectionCount();
      return total + " items selected for import";
    },

    importActionLabel: function () {
      var total = this.totalSelectionCount();
      return "Import " + total + " items";
    },

    toolbarSelectionLabel: function (overallTotal) {
      return this.totalSelectionCount() + " of " + overallTotal + " selected";
    },

    syncSelectionSummaryUi: function () {
      var root = document.getElementById("import-step-review-shell");
      if (!root) {
        return;
      }

      var total = this.totalSelectionCount();
      var selectionSummary = root.querySelector("[data-import-review-selection-summary]");
      if (selectionSummary) {
        selectionSummary.textContent = this.importSelectionLabel();
      }

      var importLabel = root.querySelector("[data-import-review-import-label]");
      if (importLabel && !this.confirming) {
        importLabel.textContent = this.importActionLabel();
      }

      var toolbarSummary = root.querySelector("[data-import-review-toolbar-selection-summary]");
      if (toolbarSummary) {
        var overallTotal = Number(toolbarSummary.getAttribute("data-overall-total")) || 0;
        toolbarSummary.textContent = this.toolbarSelectionLabel(overallTotal);
      }

      var importButton = root.querySelector("[data-import-review-import-button]");
      if (importButton) {
        importButton.disabled = total === 0 || this.confirming;
      }
    },

    hasVisibleFileConflictGroups: function () {
      if (this.currentView !== "conflicts") {
        return false;
      }

      var root = document.getElementById("import-step-review-shell");
      if (!root) {
        return false;
      }

      return !!root.querySelector(
        "[data-testid='import-collection-conflicts'] " +
          "[data-import-conflict-kind='file_conflict'][data-import-conflict-group-id]",
      );
    },

    currentConflictPanelData: function () {
      var host = document.querySelector("[data-testid='import-collection-conflicts']");
      if (!host) {
        return null;
      }

      try {
        if (window.Alpine && typeof window.Alpine.$data === "function") {
          return window.Alpine.$data(host);
        }
      } catch (_) {
        // fall through to the internal Alpine reference if available
      }

      return host.__x ? host.__x.$data : null;
    },

    currentConflictPageKey: function () {
      var root = document.getElementById("import-step-review-shell");
      var pageInput = root ? root.querySelector("input[name='conflicts_page']") : null;
      var sortInput = root ? root.querySelector("input[name='conflicts_sort']") : null;
      var page = pageInput && pageInput.value ? pageInput.value : "1";
      var sort = sortInput && sortInput.value ? sortInput.value : "";
      return "page=" + page + "&sort=" + sort;
    },

    currentConflictPageGroupIds: function () {
      var root = document.getElementById("import-step-review-shell");
      if (!root) {
        return [];
      }

      var rows = root.querySelectorAll(
        "[data-testid='import-collection-conflicts'] " +
          "[data-import-conflict-kind='file_conflict'][data-import-conflict-group-id]",
      );
      var groupIds = [];
      var seen = Object.create(null);

      for (var i = 0; i < rows.length; i += 1) {
        var groupId = Number(rows[i].getAttribute("data-import-conflict-group-id"));
        if (!Number.isFinite(groupId) || seen[groupId]) {
          continue;
        }
        seen[groupId] = true;
        groupIds.push(groupId);
      }

      return normalizeImportReviewSelection(groupIds);
    },

    currentConflictPageCommitState: function () {
      var state = readImportConflictCommitState(this.jobId);
      var pageState = state.committedPages[this.currentConflictPageKey()] || null;
      if (!pageState) {
        return null;
      }

      var currentGroupIds = this.currentConflictPageGroupIds();
      var savedGroupIds = normalizeImportReviewSelection(pageState.groupIds || []);
      if (currentGroupIds.length === 0 || savedGroupIds.length !== currentGroupIds.length) {
        return null;
      }
      for (var i = 0; i < currentGroupIds.length; i += 1) {
        if (currentGroupIds[i] !== savedGroupIds[i]) {
          return null;
        }
      }

      return pageState;
    },

    hasCommittedConflictChoices: function () {
      var state = readImportConflictCommitState(this.jobId);
      return Object.keys(state.committedPages || {}).length > 0;
    },

    syncConflictCommitFlags: function () {
      this.conflictsCommitted = this.resolvedConflictGroupCount > 0;
    },

    showBulkSelectionControls: function () {
      return ["series", "matched"].indexOf(this.currentView) !== -1;
    },

    toggleSelection: async function (id, checked) {
      var numericId = Number(id);
      if (!Number.isFinite(numericId)) {
        return;
      }

      try {
        var response = await fetch(
          "/api/v1/import/" + this.jobId + "/series/" + numericId + "/selection",
          {
            method: "PUT",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": readCsrfTokenFromBody(),
            },
            body: JSON.stringify({ include_in_import: checked }),
          },
        );

        if (!response.ok) {
          var error = await response
            .json()
            .catch(function () {
              return {};
            });
          throw new Error(error.detail || "Failed to update series selection.");
        }
        await this.refreshReviewSummary();
        await this.refreshSeriesReview();
      } catch (err) {
        await this.refreshSeriesReviewQuietly();
        if (typeof showToast === "function") {
          showToast({
            message:
              err && err.message ? err.message : "Failed to update series selection.",
            level: "error",
          });
        }
      }
    },

    updateStoryArcDecision: async function (id, action, targetElement) {
      var numericId = Number(id);
      if (!Number.isFinite(numericId) || ["select", "skip"].indexOf(action) === -1) {
        return;
      }

      var proposedStoryArcId = null;
      if (action === "select" && targetElement && targetElement.value) {
        proposedStoryArcId = Number(targetElement.value);
        if (!Number.isFinite(proposedStoryArcId)) {
          proposedStoryArcId = null;
        }
      }

      try {
        var response = await fetch(
          "/api/v1/import/" + this.jobId + "/story-arcs/" + numericId + "/decision",
          {
            method: "PUT",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": readCsrfTokenFromBody(),
            },
            body: JSON.stringify({
              action: action,
              proposed_story_arc_id: proposedStoryArcId,
            }),
          },
        );

        if (!response.ok) {
          var error = await response
            .json()
            .catch(function () {
              return {};
            });
          throw new Error(error.detail || "Failed to update story arc decision.");
        }

        await this.refreshReviewSummary();
        await this.refreshSeriesReview();
      } catch (err) {
        if (typeof showToast === "function") {
          showToast({
            message:
              err && err.message ? err.message : "Failed to update story arc decision.",
            level: "error",
          });
        }
      }
    },

    confirmStoryArcPolicy: async function (id, formElement) {
      var numericId = Number(id);
      if (!Number.isFinite(numericId) || !formElement) {
        return;
      }

      var field = function (name) {
        return formElement.querySelector("[name='" + name + "']");
      };
      var checked = function (name) {
        var element = field(name);
        return Boolean(element && element.checked);
      };
      var materialize = checked("materialize_filesystem");
      var modeElement = field("mode");
      var mode = materialize && modeElement ? modeElement.value : "logical";
      var rootElement = field("target_library_root_id");
      var rootId = materialize && rootElement ? Number(rootElement.value) : null;
      if (!Number.isFinite(rootId)) {
        rootId = null;
      }
      var destinationElement = field("destination_root");
      var symlinkElement = field("symlink_style");
      var digestElement = field("expected_policy_digest");
      var folderElement = field("folder_template");
      var fileElement = field("file_template");
      var monitored = checked("monitored");

      var payload = {
        confirm_policy: checked("confirm_policy"),
        expected_policy_digest: digestElement ? digestElement.value : "",
        materialize_filesystem: materialize,
        monitored: monitored,
        search_missing: monitored && checked("search_missing"),
        include_upcoming: monitored && checked("include_upcoming"),
        placement_policy: {
          mode: mode,
          target_library_root_id: materialize ? rootId : null,
          destination_root:
            materialize && destinationElement ? destinationElement.value : null,
          folder_template: folderElement ? folderElement.value : "{StoryArc}",
          file_template:
            fileElement
              ? fileElement.value
              : "{ReadingOrder:03d} - {Series} {IssueNumber}",
          symlink_style:
            materialize && mode === "symlink" && symlinkElement
              ? symlinkElement.value
              : null,
          synchronize: materialize && checked("synchronize"),
        },
      };

      try {
        var response = await fetch(
          "/api/v1/import/" +
            this.jobId +
            "/story-arcs/" +
            numericId +
            "/policy-confirmation",
          {
            method: "PUT",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": readCsrfTokenFromBody(),
            },
            body: JSON.stringify(payload),
          },
        );
        if (!response.ok) {
          var error = await response
            .json()
            .catch(function () {
              return {};
            });
          var detail = error.detail;
          if (Array.isArray(detail)) {
            detail = detail
              .map(function (item) {
                return item && item.msg ? item.msg : "Invalid policy field";
              })
              .join("; ");
          }
          throw new Error(detail || "Failed to confirm story arc policy.");
        }
        await this.refreshSeriesReview();
        if (typeof showToast === "function") {
          showToast({ message: "Story arc policy confirmed.", level: "success" });
        }
      } catch (err) {
        if (typeof showToast === "function") {
          showToast({
            message:
              err && err.message ? err.message : "Failed to confirm story arc policy.",
            level: "error",
          });
        }
      }
    },

    toggleDuplicateSeriesFiles: async function (id, checked, checkboxEl) {
      var numericId = Number(id);
      if (!Number.isFinite(numericId)) {
        return;
      }

      try {
        var response = await fetch(
          "/api/v1/import/" + this.jobId + "/files/selection-bulk",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": readCsrfTokenFromBody(),
            },
            body: JSON.stringify({
              include_in_import: checked,
              imported_series_id: numericId,
            }),
          },
        );

        if (!response.ok) {
          var error = await response
            .json()
            .catch(function () {
              return {};
            });
          throw new Error(error.detail || "Failed to update in-library file selection.");
        }

        await this.refreshReviewSummary();
        await this.refreshSeriesReview();
      } catch (err) {
        if (checkboxEl) {
          checkboxEl.disabled = true;
        }
        await this.refreshSeriesReviewQuietly();
        if (typeof showToast === "function") {
          showToast({
            message:
              err && err.message
                ? err.message
                : "Failed to update in-library file selection.",
            level: "error",
          });
        }
      }
    },

    unmatchSeriesMatch: async function (id) {
      var numericId = Number(id);
      if (!Number.isFinite(numericId)) {
        return;
      }

      try {
        var response = await fetch(
          "/api/v1/import/" + this.jobId + "/series/" + numericId + "/unmatch",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": readCsrfTokenFromBody(),
            },
            body: "{}",
          },
        );

        if (!response.ok) {
          var error = await response
            .json()
            .catch(function () {
              return {};
            });
          throw new Error(error.detail || "Failed to clear ComicVine match.");
        }

        await this.refreshSeriesReview();
        await this.refreshReviewSummary();
        if (typeof showToast === "function") {
          showToast({
            message: "ComicVine match cleared. Review the row under Needs Series Match.",
            level: "success",
          });
        }
      } catch (err) {
        if (typeof showToast === "function") {
          showToast({
            message: err && err.message ? err.message : "Failed to clear ComicVine match.",
            level: "error",
          });
        }
      }
    },

    unmatchDuplicateSeries: async function (id) {
      var numericId = Number(id);
      if (!Number.isFinite(numericId)) {
        return;
      }

      try {
        var response = await fetch(
          "/api/v1/import/" + this.jobId + "/series/" + numericId + "/unmatch-duplicate",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": readCsrfTokenFromBody(),
            },
            body: "{}",
          },
        );

        if (!response.ok) {
          var error = await response
            .json()
            .catch(function () {
              return {};
            });
          throw new Error(error.detail || "Failed to unmatch in-library series.");
        }

        await this.refreshSeriesReview();
        await this.refreshReviewSummary();
        if (typeof showToast === "function") {
          showToast({
            message: "Existing-library match rejected. Review the row under Needs Series Match.",
            level: "success",
          });
        }
      } catch (err) {
        if (typeof showToast === "function") {
          showToast({
            message:
              err && err.message ? err.message : "Failed to unmatch in-library series.",
            level: "error",
          });
        }
      }
    },

    selectAllImportable: async function () {
      try {
        var seriesResponse = await fetch("/api/v1/import/" + this.jobId + "/series/selection-bulk", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": readCsrfTokenFromBody(),
          },
          body: JSON.stringify({
            include_in_import: true,
            imported_series_ids: [],
          }),
        });

        if (!seriesResponse.ok) {
          var seriesError = await seriesResponse
            .json()
            .catch(function () {
              return {};
            });
          throw new Error(seriesError.detail || "Failed to select matched series.");
        }

        var fileResponse = await fetch("/api/v1/import/" + this.jobId + "/files/selection-bulk", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": readCsrfTokenFromBody(),
          },
          body: JSON.stringify({
            include_in_import: true,
          }),
        });

        if (!fileResponse.ok) {
          var fileError = await fileResponse
            .json()
            .catch(function () {
              return {};
            });
          throw new Error(fileError.detail || "Failed to select in-library files.");
        }

        await this.refreshReviewSummary();
        await this.refreshSeriesReview();
      } catch (err) {
        await this.refreshSeriesReviewQuietly();
        if (typeof showToast === "function") {
          showToast({
            message:
              err && err.message ? err.message : "Failed to select matched series.",
            level: "error",
          });
        }
      }
    },

    deselectAllImportable: async function () {
      try {
        var seriesResponse = await fetch("/api/v1/import/" + this.jobId + "/series/selection-bulk", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": readCsrfTokenFromBody(),
          },
          body: JSON.stringify({
            include_in_import: false,
            imported_series_ids: [],
          }),
        });

        if (!seriesResponse.ok) {
          var seriesError = await seriesResponse
            .json()
            .catch(function () {
              return {};
            });
          throw new Error(seriesError.detail || "Failed to clear series selection.");
        }

        var fileResponse = await fetch("/api/v1/import/" + this.jobId + "/files/selection-bulk", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": readCsrfTokenFromBody(),
          },
          body: JSON.stringify({
            include_in_import: false,
          }),
        });

        if (!fileResponse.ok) {
          var fileError = await fileResponse
            .json()
            .catch(function () {
              return {};
            });
          throw new Error(fileError.detail || "Failed to clear in-library file selection.");
        }

        await this.refreshReviewSummary();
        await this.refreshSeriesReview();
      } catch (err) {
        await this.refreshSeriesReviewQuietly();
        if (typeof showToast === "function") {
          showToast({
            message:
              err && err.message ? err.message : "Failed to clear series selection.",
            level: "error",
          });
        }
      }
    },

    handleConflictsSaved: function (detail) {
      var payload = detail || {};
      this.resolvedConflictGroupCount = Math.max(
        this.resolvedConflictGroupCount,
        Number(payload.resolvedConflictGroupCount) || 1,
      );
      if (typeof payload.conflictSeriesCount === "number") {
        this.conflictSeriesCount = payload.conflictSeriesCount;
      }
      this.syncConflictCommitFlags();
      this.refreshReviewSummary();
      this.refreshSeriesReviewQuietly();
    },

    handleConflictsReset: function (detail) {
      var payload = detail || {};
      this.resolvedConflictGroupCount = Number(payload.resolvedConflictGroupCount) || 0;
      if (typeof payload.conflictSeriesCount === "number") {
        this.conflictSeriesCount = payload.conflictSeriesCount;
      }
      this.syncConflictCommitFlags();
      this.refreshReviewSummary();
      this.refreshSeriesReviewQuietly();
    },

    resetConflictChoices: async function () {
      if (this.resolvedConflictGroupCount <= 0) {
        return;
      }

      try {
        var response = await fetch("/api/v1/import/" + this.jobId + "/conflicts/reset", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": readCsrfTokenFromBody(),
          },
          body: JSON.stringify({}),
        });

        if (!response.ok) {
          var error = await response
            .json()
            .catch(function () {
              return { detail: "Failed to reset conflict choices." };
          });
          throw new Error(error.detail || "Failed to reset conflict choices.");
        }

        var payload = await response.json();
        clearImportConflictCommitState(this.jobId);
        this.handleConflictsReset({
          resetSeriesIds: payload.reset_series_ids || [],
          resolvedConflictGroupCount: 0,
          committed: false,
        });
        await this.refreshSeriesReview();
      } catch (err) {
        if (typeof showToast === "function") {
          showToast({
            message:
              err && err.message ? err.message : "Failed to reset conflict choices.",
            level: "error",
          });
        }
      }
    },

    buildReviewUrl: function () {
      var url = "/import/" + this.jobId + "/review-partial";
      var params = new URLSearchParams();
      var statusInput = document.querySelector(
        "#import-step-review-shell input[name='review_status_filter']",
      );
      var sortInput = document.querySelector("#import-step-review-shell input[name='review_sort']");
      var pageInput = document.querySelector("#import-step-review-shell input[name='review_page']");

      if (statusInput && statusInput.value) {
        params.set("status", statusInput.value);
      }
      if (sortInput && sortInput.value) {
        params.set("sort", sortInput.value);
      }
      if (pageInput && pageInput.value) {
        params.set("page", pageInput.value);
      }

      var query = params.toString();
      return query ? url + "?" + query : url;
    },

    refreshSeriesReview: function () {
      return loadImportReviewShell(this.buildReviewUrl());
    },

    refreshSeriesReviewQuietly: function () {
      return this.refreshSeriesReview().then(null, function () {});
    },

    openReviewView: function (view) {
      var nextView = view || "series";
      this.currentView = nextView;
      var url = "/import/" + this.jobId + "/review-partial";
      if (nextView !== "series") {
        url +=
          "?status=" +
          encodeURIComponent(nextView) +
          "&sort=" +
          encodeURIComponent(nextView === "conflicts" ? "series" : "confidence");
      } else {
        url += "?sort=confidence";
      }
      return loadImportReviewShell(url);
    },

    saveConflictChoices: async function () {
      var panel = this.currentConflictPanelData();
      if (!panel || typeof panel.saveAllResolutions !== "function") {
        return;
      }

      await panel.saveAllResolutions();
    },

    refreshReviewSummary: async function () {
      try {
        var response = await fetch("/import/" + this.jobId + "/progress-state");
        if (!response.ok) {
          return;
        }
        var data = await response.json();
        var summary = data && data.review_summary ? data.review_summary : null;
        if (!summary) {
          return;
        }

        this.duplicateSelectedCount =
          Number(summary.duplicate_series_selected) ||
          Number(summary.duplicate_files_selected) ||
          0;
        this.matchedSelectedCount = Number(summary.matched_series_selected) || 0;
        this.duplicateImportableCount =
          Number(summary.duplicate_series_importable) ||
          Number(summary.duplicate_files_importable) ||
          0;
        this.selectedItemCount =
          Number(summary.selected_items_total) ||
          Number(summary.selected_series_total) ||
          this.matchedSelectedCount + this.duplicateSelectedCount;
        this.importableItemCount =
          Number(summary.importable_items_total) ||
          (Number(summary.matched_series_importable) || 0) + this.duplicateImportableCount;
        this.resolvedConflictGroupCount = Number(summary.resolved_file_conflict_groups) || 0;
        this.conflictSeriesCount = Number(summary.series_conflicts_total) || 0;
        this.syncConflictCommitFlags();
        this.syncSelectionSummaryUi();

        window.dispatchEvent(
          new CustomEvent("import:collection-footer", {
            detail: {
              step: 3,
              reviewSummary: summary,
            },
          }),
        );
      } catch (_err) {
        // Keep the current UI state if the refresh fails.
      }
    },

    cancelImport: async function () {
      this.cancelling = true;
      try {
        await fetch("/api/v1/import/" + this.jobId, {
          method: "DELETE",
          headers: { "X-CSRF-Token": readCsrfTokenFromBody() },
        });
        purgeImportClientState(this.jobId);
      } finally {
        window.location.replace("/import");
      }
    },

    syncSelectionUi: function () {
      this.syncSelectionSummaryUi();
    },

    confirmImport: async function () {
      if (this.totalSelectionCount() === 0) {
        return;
      }

      this.confirming = true;
      this.confirmError = "";

      try {
        var response = await fetch("/api/v1/import/" + this.jobId + "/confirm", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": readCsrfTokenFromBody(),
          },
          body: JSON.stringify({
            series_ids: [],
            story_arc_ids: [],
            story_arc_decisions: [],
          }),
        });

        if (!response.ok) {
          var error = await response
            .json()
            .catch(function () {
              return { detail: "Failed to confirm import" };
            });
          throw new Error(error.detail || "Server error (" + response.status + ")");
        }

        clearImportConflictCommitState(this.jobId);
        dispatchImportWizardAdvance({ step: 4, jobStatus: "importing" });
      } catch (err) {
        this.confirmError = err && err.message ? err.message : "Failed to confirm import.";
      } finally {
        this.confirming = false;
      }
    },
  };
}

function importResultsData(config) {
  var cfg = config || {};

  return {
    jobId: cfg.jobId || null,
    canRollback: Boolean(cfg.canRollback),
    showFailedSeries: cfg.failedCount > 0,
    showFailedFiles: cfg.failedFilesCount > 0,
    retrying: false,
    rollingBack: false,
    safetyRetryingFileId: null,
    retryError: "",

    toggleFailedSeries: function () {
      this.showFailedSeries = !this.showFailedSeries;
    },

    toggleFailedFiles: function () {
      this.showFailedFiles = !this.showFailedFiles;
    },

    retryFailed: async function () {
      if (!this.jobId || this.retrying) {
        return;
      }

      this.retrying = true;
      this.retryError = "";

      try {
        var response = await fetch("/api/v1/import/" + this.jobId + "/retry-failed", {
          method: "POST",
          headers: { "X-CSRF-Token": readCsrfTokenFromBody() },
        });

        if (!response.ok) {
          var error = await response
            .json()
            .catch(function () {
              return {};
            });
          throw new Error(error.detail || "Failed to retry");
        }

        dispatchImportWizardAdvance({
          step: 4,
          jobId: this.jobId,
          jobStatus: "importing",
        });
      } catch (err) {
        var message =
          err && err.message ? err.message : "Retry failed. Please try again.";
        this.retryError = message;
        if (typeof showToast === "function") {
          showToast({ message: message, level: "error" });
        }
      } finally {
        this.retrying = false;
      }
    },

    allowSafetyOnceAndRetry: async function (fileId) {
      if (!this.jobId || this.safetyRetryingFileId) {
        return;
      }

      this.safetyRetryingFileId = fileId;
      this.retryError = "";

      try {
        var response = await fetch(
          "/api/v1/import/" +
            this.jobId +
            "/files/" +
            fileId +
            "/safety/allow-once-and-retry",
          {
            method: "POST",
            headers: { "X-CSRF-Token": readCsrfTokenFromBody() },
          }
        );

        if (!response.ok) {
          var error = await response
            .json()
            .catch(function () {
              return {};
            });
          throw new Error(error.detail || "Failed to approve safety retry");
        }

        dispatchImportWizardAdvance({
          step: 4,
          jobId: this.jobId,
          jobStatus: "importing",
        });
      } catch (err) {
        var message =
          err && err.message ? err.message : "Safety retry failed. Please try again.";
        this.retryError = message;
        if (typeof showToast === "function") {
          showToast({ message: message, level: "error" });
        }
      } finally {
        this.safetyRetryingFileId = null;
      }
    },

    rollbackImport: async function () {
      if (!this.jobId || this.rollingBack || !this.canRollback) {
        return;
      }

      var confirmed = await pbConfirm({
        title: "Rollback import",
        message:
          "This will undo the import and reopen review once rollback finishes. Continue?",
        confirmText: "Start rollback",
        destructive: true,
      });
      if (!confirmed) {
        return;
      }

      this.rollingBack = true;
      try {
        var response = await fetch("/api/v1/import/" + this.jobId + "/rollback", {
          method: "POST",
          headers: { "X-CSRF-Token": readCsrfTokenFromBody() },
        });

        if (!response.ok) {
          var error = await response.json().catch(function () {
            return {};
          });
          throw new Error(error.detail || "Failed to start rollback.");
        }

        purgeImportClientState(this.jobId);
        dispatchImportWizardAdvance({
          step: 4,
          jobId: this.jobId,
          jobStatus: "rolling_back",
        });
      } catch (err) {
        var message =
          err && err.message ? err.message : "Rollback failed to start. Please try again.";
        if (typeof showToast === "function") {
          showToast({ message: message, level: "error" });
        }
      } finally {
        this.rollingBack = false;
      }
    },
  };
}

function conflictResolutionData(configOrJobId, totalGroups) {
  var cfg =
    configOrJobId &&
    typeof configOrJobId === "object" &&
    !Array.isArray(configOrJobId)
      ? configOrJobId
      : {
          jobId: configOrJobId,
          totalGroups: totalGroups,
        };

  return {
    jobId: cfg.jobId,
    totalGroups: Number(cfg.totalGroups) || 0,
    refreshMode: cfg.refreshMode || "partial",
    refreshTarget: cfg.refreshTarget || "#conflicts-content",
    refreshSwap: cfg.refreshSwap || "morph:innerHTML",
    resolutions: {},
    saving: false,
    saveError: "",
    saveSuccess: false,
    committed: false,

    init: function () {
      this.syncCommittedState();
      this.dispatchVisibilityState();
    },

    fileConflictGroupCount: function () {
      return this.$el.querySelectorAll("[data-import-conflict-kind='file_conflict']").length;
    },

    dispatchVisibilityState: function () {
      window.dispatchEvent(
        new CustomEvent("import:conflict-visibility", {
          detail: {
            visibleFileConflictGroupCount: this.fileConflictGroupCount(),
          },
        }),
      );
    },

    setResolution: function (groupId, fileId) {
      this.resolutions[groupId] = fileId;
    },

    currentPageKey: function () {
      var pageInput = this.$el.querySelector("input[name='conflicts_page']");
      var sortInput = this.$el.querySelector("input[name='conflicts_sort']");
      var page = pageInput && pageInput.value ? pageInput.value : "1";
      var sort = sortInput && sortInput.value ? sortInput.value : "";
      return "page=" + page + "&sort=" + sort;
    },

    currentPageCommitState: function () {
      var state = readImportConflictCommitState(this.jobId);
      var pageState = state.committedPages[this.currentPageKey()] || null;
      if (!pageState) {
        return null;
      }

      var currentGroupIds = normalizeImportReviewSelection(
        this.readCurrentPageConflictMeta().map(function (meta) {
          return meta.groupId;
        }),
      );
      var savedGroupIds = normalizeImportReviewSelection(pageState.groupIds || []);
      if (currentGroupIds.length === 0 || savedGroupIds.length !== currentGroupIds.length) {
        return null;
      }
      for (var i = 0; i < currentGroupIds.length; i += 1) {
        if (currentGroupIds[i] !== savedGroupIds[i]) {
          return null;
        }
      }

      return pageState;
    },

    readCurrentPageConflictMeta: function () {
      var rows = this.$el.querySelectorAll(
        "[data-import-conflict-kind='file_conflict'][data-import-conflict-group-id]",
      );
      var metas = [];
      var seen = Object.create(null);

      for (var i = 0; i < rows.length; i += 1) {
        var row = rows[i];
        var groupId = Number(row.getAttribute("data-import-conflict-group-id"));
        if (!Number.isFinite(groupId)) {
          continue;
        }
        var key = String(groupId);
        if (seen[key]) {
          continue;
        }
        seen[key] = true;
        var seriesId = Number(row.getAttribute("data-import-conflict-series-id"));
        metas.push({
          groupId: groupId,
          seriesId: Number.isFinite(seriesId) ? seriesId : null,
        });
      }

      return metas;
    },

    persistCurrentPageCommitState: function (pageState) {
      var state = readImportConflictCommitState(this.jobId);
      var committedPages = Object.assign({}, state.committedPages || {});
      var pageKey = this.currentPageKey();

      if (pageState && Array.isArray(pageState.groupIds) && pageState.groupIds.length > 0) {
        committedPages[pageKey] = {
          groupIds: normalizeImportReviewSelection(pageState.groupIds),
          seriesIds: normalizeImportReviewSelection(pageState.seriesIds),
          autoAddedSeriesIds: normalizeImportReviewSelection(pageState.autoAddedSeriesIds),
        };
      } else {
        delete committedPages[pageKey];
      }

      writeImportConflictCommitState(this.jobId, {
        committedPages: committedPages,
      });
      this.syncCommittedState();
    },

    syncCommittedState: function () {
      this.committed = !!this.currentPageCommitState();
      window.dispatchEvent(
        new CustomEvent("import:conflicts-state", {
          detail: {
            committed: this.committed,
            anyCommitted: Object.keys(readImportConflictCommitState(this.jobId).committedPages || {})
              .length > 0,
          },
        }),
      );
    },

    saveAllResolutions: async function () {
      if (this.saving || this.committed) {
        return;
      }

      this.saving = true;
      this.saveError = "";
      this.saveSuccess = false;

      try {
        var beforeSelection = readImportReviewSelection(this.jobId);
        var pageMeta = this.readCurrentPageConflictMeta();
        if (pageMeta.length === 0) {
          throw new Error("No file conflicts on this page are ready to save.");
        }

        var radios = this.$el.querySelectorAll("input[type='radio']:checked");
        var groupResolutions = [];
        var seen = new Set();

        for (var i = 0; i < radios.length; i++) {
          var name = radios[i].getAttribute("name");
          var match = name && name.match(/^conflict_(\d+)$/);
          if (match && !seen.has(match[1])) {
            seen.add(match[1]);
            groupResolutions.push({
              conflict_group_id: parseInt(match[1], 10),
              chosen_file_id: parseInt(radios[i].value, 10),
            });
          }
        }

        if (groupResolutions.length < pageMeta.length) {
          throw new Error("Pick one file for each conflict before saving.");
        }

        var response = await fetch(
          "/api/v1/import/" + this.jobId + "/conflicts/resolve-bulk",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": readCsrfTokenFromBody(),
            },
            body: JSON.stringify({ resolutions: groupResolutions }),
          },
        );

        if (!response.ok) {
          var error = await response.json().catch(function () {
            return {};
          });
          throw new Error(error.detail || "Failed to save conflict resolutions.");
        }

        var payload = await response.json().catch(function () {
          return {};
        });
        var resolvedGroupIds = normalizeImportReviewSelection(
          payload.resolved_group_ids ||
            groupResolutions.map(function (resolution) {
              return resolution.conflict_group_id;
            }),
        );
        var resolvedSeriesIds = normalizeImportReviewSelection(
          payload.resolved_series_ids ||
            pageMeta
              .map(function (meta) {
                return meta.seriesId;
              })
              .filter(function (seriesId) {
                return Number.isFinite(seriesId);
              }),
        );

        var autoAddedSeriesIds = resolvedSeriesIds.filter(function (seriesId) {
          return beforeSelection.indexOf(seriesId) === -1;
        });

        this.persistCurrentPageCommitState({
          groupIds: resolvedGroupIds,
          seriesIds: resolvedSeriesIds,
          autoAddedSeriesIds: autoAddedSeriesIds,
        });
        this.saveSuccess = true;
        window.dispatchEvent(
          new CustomEvent("import:conflicts-saved", {
            detail: {
              seriesIds: resolvedSeriesIds,
              autoAddedSeriesIds: autoAddedSeriesIds,
              resolvedConflictGroupCount:
                Number(payload.resolved_count) || resolvedGroupIds.length,
              committed: true,
            },
          }),
        );
        await performHtmxSwap("GET", this.buildRefreshUrl(), {
          target: this.refreshTarget,
          swap: this.refreshSwap,
        });
      } catch (err) {
        this.saveError = err && err.message ? err.message : "Failed to save conflict resolutions.";
      } finally {
        this.saving = false;
      }
    },

    resetAllResolutions: async function () {
      if (this.saving) {
        return;
      }

      var pageState = this.currentPageCommitState();
      if (!pageState) {
        return;
      }

      this.saving = true;
      this.saveError = "";
      this.saveSuccess = false;

      try {
        var response = await fetch("/api/v1/import/" + this.jobId + "/conflicts/reset", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": readCsrfTokenFromBody(),
          },
          body: JSON.stringify({ group_ids: pageState.groupIds }),
        });

        if (!response.ok) {
          var error = await response.json().catch(function () {
            return {};
          });
          throw new Error(error.detail || "Failed to reset conflict choices.");
        }

        this.persistCurrentPageCommitState(null);
        window.dispatchEvent(
          new CustomEvent("import:conflicts-reset", {
            detail: {
              autoAddedSeriesIds: pageState.autoAddedSeriesIds,
              committed: false,
            },
          }),
        );
        await performHtmxSwap("GET", this.buildRefreshUrl(), {
          target: this.refreshTarget,
          swap: this.refreshSwap,
        });
      } catch (err) {
        this.saveError = err && err.message ? err.message : "Failed to reset conflict choices.";
      } finally {
        this.saving = false;
      }
    },

    buildRefreshUrl: function () {
      var url =
        this.refreshMode === "review"
          ? "/import/" + this.jobId + "/review-partial?status=conflicts"
          : "/import/" + this.jobId + "/conflicts-partial";
      var params = new URLSearchParams();
      var pageInput = this.$el.querySelector("input[name='conflicts_page']");

      if (pageInput && pageInput.value) {
        params.set("page", pageInput.value);
      }
      var sortInput = this.$el.querySelector("input[name='conflicts_sort']");
      if (sortInput && sortInput.value) {
        params.set("sort", sortInput.value);
      }

      var query = params.toString();
      if (!query) {
        return url;
      }
      return url.indexOf("?") >= 0 ? url + "&" + query : url + "?" + query;
    },
  };
}

function utilitiesQueue(config) {
  var cfg = config || {};

  // Hydrate from server-rendered JSON if available
  var initialData = null;
  var dataEl = document.getElementById("utilities-queue-initial-data");
  if (dataEl) {
    try { initialData = JSON.parse(dataEl.textContent || "{}"); } catch (_) { /* ignore */ }
  }

  return {
    stats: (initialData && initialData.stats) || { running: 0, queued: 0, total_completed: 0, paused: 0 },
    jobs: (initialData && initialData.jobs) || [],
    jobLogs: {},
    expandedJob: null,
    _pollInterval: null,
    _pendingRollbacks: {},

    // Log viewer state (shared scope — no separate x-data)
    levelFilter: "",
    searchQuery: "",
    pageSize: "50",
    currentPage: 1,
    expandedIdx: null,
    _logDebounceTimer: null,

    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },

    init: function () {
      var self = this;
      this.logViewer._host = this;
      if (cfg.disableInitialQueueLoad !== true && !this.jobs.length) {
        this.loadQueue();
      }
      if (cfg.pollQueue !== false) {
        this.startPolling();
      }
      // Debounced search for log viewer
      this.$watch("searchQuery", function () {
        clearTimeout(self._logDebounceTimer);
        self._logDebounceTimer = setTimeout(function () {
          self.currentPage = 1;
          self.expandedIdx = null;
        }, 300);
      });
    },

    get activeJobs() {
      return this.jobs.filter(function (job) {
        return ["COMPLETED", "FAILED", "CANCELLED", "ROLLED_BACK"].indexOf(job.state) === -1;
      });
    },

    get runningJobs() {
      return this.activeJobs.filter(function (job) {
        return job.state !== "QUEUED";
      });
    },

    get queuedJobs() {
      return this.activeJobs.filter(function (job) {
        return job.state === "QUEUED";
      });
    },

    elapsedLabel: function (job) {
      if (!job) return "waiting";
      var startValue = job.started_at || job.created_at;
      var endValue = job.completed_at || new Date().toISOString();
      if (job.state === "QUEUED" || !startValue) return "waiting";
      var start = Date.parse(startValue);
      var end = Date.parse(endValue);
      if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return "—";
      var totalSeconds = Math.max(1, Math.round((end - start) / 1000));
      var minutes = Math.floor(totalSeconds / 60);
      var seconds = totalSeconds % 60;
      if (minutes <= 0) return seconds + "s";
      if (minutes < 60) return minutes + "m " + seconds + "s";
      var hours = Math.floor(minutes / 60);
      minutes = minutes % 60;
      return hours + "h " + minutes + "m";
    },

    isRollbackPending: function (jobId) {
      var pendingSince = this._pendingRollbacks[String(jobId)] || 0;
      return Boolean(pendingSince && Date.now() - pendingSince < 15000);
    },

    _formatEta: function (job) {
      var done = (job.completed_items || 0) + (job.failed_items || 0) + (job.skipped_items || 0);
      var remaining = (job.total_items || 0) - done;
      if (done === 0 || remaining <= 0 || !job.started_at) return "";
      var started = new Date(job.started_at).getTime();
      var elapsed = (Date.now() - started) / 1000;
      if (elapsed < 2) return "";
      var secsPerItem = elapsed / done;
      var etaSecs = Math.round(secsPerItem * remaining);
      if (etaSecs < 60) return " · ~" + etaSecs + "s remaining";
      var mins = Math.floor(etaSecs / 60);
      var secs = etaSecs % 60;
      if (mins < 60) return " · ~" + mins + "m " + secs + "s remaining";
      var hrs = Math.floor(mins / 60);
      mins = mins % 60;
      return " · ~" + hrs + "h " + mins + "m remaining";
    },

    stateLabel: function (job) {
      if (job.state === "RUNNING" && job.total_items > 0) {
        var eta = this._formatEta(job);
        return (job.completed_items || 0) + " / " + job.total_items + " items" + eta;
      }
      if (job.state === "PAUSED") {
        return "Paused at " + (job.completed_items || 0) + " / " + (job.total_items || 0) + " items";
      }
      if (job.state === "QUEUED") return "Waiting in queue";
      if (job.state === "PAUSING") return "Pausing after current batch...";
      if (job.state === "CANCELLING") return "Cancelling...";
      if (job.state === "COMPLETED" && this.needsAttention(job)) {
        return "Needs attention — " + (job.error_message || "").replace("NEEDS_ATTENTION:", "");
      }
      if (job.state === "COMPLETED") {
        return (job.completed_items || 0) + " items · " + (job.failed_items || 0) + " errors";
      }
      if (job.state === "FAILED") return "Failed: " + (job.error_message || "Unknown error");
      if (job.state === "CANCELLED") {
        return "Cancelled at " + (job.completed_items || 0) + " / " + (job.total_items || 0);
      }
      if (job.state === "ROLLED_BACK") return "Rolled back";
      return job.state;
    },

    needsAttention: function (job) {
      return job.state === "COMPLETED" && job.error_message && job.error_message.indexOf("NEEDS_ATTENTION:") === 0;
    },

    _parseUnresolvableExtra: function (jobId) {
      var logs = this.jobLogs[jobId] || [];
      for (var i = 0; i < logs.length; i++) {
        var log = logs[i];
        if (log.extra) {
          var extra = typeof log.extra === "string" ? JSON.parse(log.extra) : log.extra;
          if (extra.unresolvable_folders || extra.loose_files) {
            return extra;
          }
        }
      }
      return null;
    },

    getUnresolvableFolders: function (jobId) {
      var extra = this._parseUnresolvableExtra(jobId);
      if (!extra || !extra.unresolvable_folders) return [];
      var folders = extra.unresolvable_folders;
      return Object.keys(folders).map(function (path) {
        var parts = path.split("/");
        return {
          path: path,
          name: parts[parts.length - 1] || path,
          count: folders[path].length,
        };
      });
    },

    getLooseFiles: function (jobId) {
      var extra = this._parseUnresolvableExtra(jobId);
      if (!extra || !extra.loose_files) return [];
      return extra.loose_files.map(function (fp) {
        var parts = fp.split("/");
        return parts[parts.length - 1] || fp;
      });
    },

    getAllUnresolvableItems: function (jobId) {
      var items = [];
      var folders = this.getUnresolvableFolders(jobId);
      for (var i = 0; i < folders.length; i++) {
        items.push({
          name: folders[i].name,
          detail: folders[i].count + " file" + (folders[i].count !== 1 ? "s" : ""),
          type: "folder",
        });
      }
      var loose = this.getLooseFiles(jobId);
      for (var j = 0; j < loose.length; j++) {
        items.push({
          name: loose[j],
          detail: "",
          type: "file",
        });
      }
      return items;
    },

    // Log viewer methods (shared scope with queue component)
    setLevel: function (level) {
      this.levelFilter = level;
      this.currentPage = 1;
      this.expandedIdx = null;
    },

    get displayEntries() {
      var filtered = this.filteredEntries;
      var limit = parseInt(this.pageSize, 10);
      var offset = (this.currentPage - 1) * limit;
      return filtered.slice(offset, offset + limit);
    },

    get filteredEntries() {
      var entries = this.logViewer ? this.logViewer.entries : [];
      if (this.levelFilter) {
        var target = String(this.levelFilter).toLowerCase();
        entries = entries.filter(function (entry) {
          var entryLevel = String(entry.level || "").toLowerCase();
          if (target === "error") {
            return entryLevel === "error" || entryLevel === "critical";
          }
          return entryLevel === target;
        });
      }
      if (this.searchQuery) {
        var q = String(this.searchQuery).toLowerCase();
        var self = this;
        entries = entries.filter(function (entry) {
          var message = String(entry.message || "").toLowerCase();
          var extra = self.formatExtra(entry.extra).toLowerCase();
          var path = String(entry.file_path || "").toLowerCase();
          return message.includes(q) || extra.includes(q) || path.includes(q);
        });
      }
      return entries;
    },

    get totalPages() {
      var count = this.filteredEntries.length;
      return Math.max(1, Math.ceil(count / parseInt(this.pageSize, 10)));
    },

    _refreshLogs: function () {
      if (!this.logViewer || !this.logViewer.fetchPage) return;
      this.logViewer.fetchPage();
    },

    jobLogDownloadHref: function (jobId) {
      var params = [];
      if (this.levelFilter) {
        params.push("level=" + encodeURIComponent(this.levelFilter));
      }
      if (this.searchQuery) {
        params.push("search=" + encodeURIComponent(this.searchQuery));
      }
      var query = params.length ? "?" + params.join("&") : "";
      return "/api/v1/utilities/jobs/" + jobId + "/logs/download" + query;
    },

    prevPage: function () {
      if (this.currentPage > 1) {
        this.currentPage--;
        this.expandedIdx = null;
        this._refreshLogs();
      }
    },

    nextPage: function () {
      if (this.currentPage < this.totalPages) {
        this.currentPage++;
        this.expandedIdx = null;
        this._refreshLogs();
      }
    },

    formatExtra: function (extra) {
      if (!extra || extra === "{}") return "";
      try {
        var obj = typeof extra === "string" ? JSON.parse(extra) : extra;
        return JSON.stringify(obj, null, 2);
      } catch (_) {
        return String(extra);
      }
    },

    _normalizeJobLogEntry: function (log) {
      return {
        id: log.id,
        timestamp: log.timestamp || "",
        formatted_timestamp: log.timestamp ? _pb.formatFull(log.timestamp) : "",
        level: log.level || "",
        message: log.message || "",
        file_path: log.file_path || "",
        extra: log.extra || "",
      };
    },

    logViewer: {
      entries: [],
      totalCount: 0,
      loading: false,
      _jobId: null,
      _csrfToken: null,
      _host: null,
      fetchPage: async function () {
        if (!this._jobId) return;
        var host = this._host;
        this.loading = true;
        try {
          var batchSize = 500;
          var offset = 0;
          var totalCount = 0;
          var mappedEntries = [];

          while (true) {
            var response = await fetch(
              "/api/v1/utilities/jobs/" + this._jobId + "/logs?limit=" + batchSize + "&offset=" + offset,
              { headers: { "X-CSRF-Token": this._csrfToken || "" } },
            );
            if (!response.ok) {
              break;
            }

            var data = await response.json();
            var rawEntries = data.entries || [];
            if (offset === 0) {
              totalCount = data.total_count || rawEntries.length;
            }

            var normalizedBatch = rawEntries.map(function (log) {
              if (host && typeof host._normalizeJobLogEntry === "function") {
                return host._normalizeJobLogEntry(log);
              }
              return log;
            });
            mappedEntries = mappedEntries.concat(normalizedBatch);

            if (!rawEntries.length || mappedEntries.length >= totalCount) {
              break;
            }
            offset += rawEntries.length;
          }

          this.entries = mappedEntries;
          this.totalCount = mappedEntries.length;
          if (host && this._jobId) {
            host.jobLogs[this._jobId] = mappedEntries;
          }
        } catch (_) { /* ignore */ }
        this.loading = false;
      },
    },

    toggleJob: function (jobId) {
      if (this.expandedJob === jobId) {
        this.expandedJob = null;
        this.loadQueue();
        return;
      }
      this.expandedJob = jobId;

      // Reset log viewer state for the new job
      this.levelFilter = "";
      this.searchQuery = "";
      this.currentPage = 1;
      this.expandedIdx = null;
      this.logViewer._jobId = jobId;
      this.logViewer._csrfToken = this.csrfToken();
      this.logViewer.entries = [];
      this.logViewer.totalCount = 0;

      this._refreshLogs();
    },

    importUnresolvable: async function (jobId) {
      // Collect ALL unresolvable file paths (folders + loose files)
      var extra = this._parseUnresolvableExtra(jobId);
      if (!extra) return;

      var allFiles = [];
      if (extra.unresolvable_folders) {
        for (var folderPath in extra.unresolvable_folders) {
          allFiles = allFiles.concat(extra.unresolvable_folders[folderPath]);
        }
      }
      if (extra.loose_files) {
        allFiles = allFiles.concat(extra.loose_files);
      }
      if (!allFiles.length) return;

      try {
        var response = await fetch("/api/v1/import", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            source_path: allFiles[0],
            file_paths: allFiles,
            source_type: "filesystem",
          }),
        });

        if (!response.ok) {
          var error = await response.json().catch(function () { return {}; });
          showToast({
            message: (error.detail || (error.error && error.error.message)) || "Failed to create import job.",
            level: "error",
          });
          return;
        }

        showToast({ message: "Import job created for " + allFiles.length + " files.", level: "success" });
        window.location.href = "/import?tab=collection";
      } catch (_) {
        showToast({ message: "Failed to create import job.", level: "error" });
      }
    },

    buildHistorySectionUrl: function () {
      var form = document.getElementById("utilities-history-filter-form");
      var params = new URLSearchParams();
      params.set("tab", "history");

      if (form && typeof FormData !== "undefined") {
        var data = new FormData(form);
        data.forEach(function (value, key) {
          var stringValue = String(value || "");
          if (key === "tab" || stringValue) {
            params.set(key, stringValue);
          }
        });
      }

      if (!params.get("sort")) {
        params.set("sort", "-completed_at");
      }
      if (!params.get("page")) {
        params.set("page", "1");
      }
      return "/utilities?" + params.toString();
    },

    refreshHistorySection: function (path) {
      var target = document.getElementById("utilities-history-section");
      var nextPath = path || this.buildHistorySectionUrl();
      this.expandedJob = null;

      if (!target || typeof htmx === "undefined") {
        window.location.assign(nextPath);
        return Promise.resolve();
      }
      if (window.history && typeof window.history.replaceState === "function") {
        window.history.replaceState({}, "", nextPath);
      }
      return performHtmxSwap("GET", nextPath, {
        target: "#utilities-history-section",
        swap: "outerHTML",
      }).catch(function () {
        window.location.assign(nextPath);
      });
    },

    startPolling: function () {
      var self = this;
      if (self._pollInterval) {
        clearInterval(self._pollInterval);
      }
      self._pollInterval = window.setInterval(function () {
        if (!self.$el || !document.body.contains(self.$el)) {
          clearInterval(self._pollInterval);
          self._pollInterval = null;
          return;
        }
        // Keep the expanded log viewer stable while the user is inspecting or filtering it.
        if (self.expandedJob) {
          return;
        }
        self.loadQueue();
      }, 5000);
    },

    loadQueue: async function () {
      var self = this;
      try {
        var responses = await Promise.all([
          fetch("/api/v1/utilities/queue", { headers: { "X-CSRF-Token": self.csrfToken() } }),
          fetch("/api/v1/utilities/jobs?limit=50", { headers: { "X-CSRF-Token": self.csrfToken() } }),
        ]);

        if (responses[0].ok) {
          self.stats = await responses[0].json();
        }
        if (responses[1].ok) {
          var data = await responses[1].json();
          self.jobs = data.jobs || [];
          Object.keys(self._pendingRollbacks).forEach(function (parentId) {
            var rollbackJob = self.jobs.find(function (candidate) {
              return candidate && candidate.job_type === "rollback" && String(candidate.parent_job_id) === parentId;
            });
            if (rollbackJob || Date.now() - self._pendingRollbacks[parentId] >= 15000) {
              delete self._pendingRollbacks[parentId];
            }
          });
        }
      } catch (error) {
        console.error("Failed to load utilities queue:", error);
      }
    },

    controlJob: async function (jobId, action) {
      try {
        var response = await fetch("/api/v1/utilities/jobs/" + jobId + "/" + action, {
          method: "POST",
          headers: { "X-CSRF-Token": this.csrfToken() },
        });
        if (!response.ok) {
          var error = await response.json();
          showToast({
            message: (error.error && error.error.message) || ("Failed to " + action + "."),
            level: "error",
          });
          return;
        }
        showToast({ message: "Job " + action + " requested.", level: "success" });
        await this.loadQueue();
      } catch (_) {
        showToast({ message: "Failed to " + action + " job.", level: "error" });
      }
    },

    deleteJob: async function (jobId) {
      try {
        var response = await fetch("/api/v1/utilities/jobs/" + jobId, {
          method: "DELETE",
          headers: { "X-CSRF-Token": this.csrfToken() },
        });
        if (!response.ok && response.status !== 204) {
          var error = await response.json();
          showToast({
            message: (error.error && error.error.message) || "Failed to delete.",
            level: "error",
          });
          return;
        }
        showToast({ message: "Job deleted.", level: "success" });
        await this.loadQueue();
        await this.refreshHistorySection();
      } catch (_) {
        showToast({ message: "Failed to delete job.", level: "error" });
      }
    },

    rollbackJob: async function (jobId) {
      var self = this;
      pbConfirm({
        title: "Queue Rollback",
        message: "This will queue a rollback job for the completed items in this utility run. Failed or skipped items will be left unchanged.",
        confirmText: "Queue Rollback",
      }).then(async function (ok) {
        if (!ok) return;
        try {
          var response = await fetch("/api/v1/utilities/jobs/" + jobId + "/rollback", {
            method: "POST",
            headers: { "X-CSRF-Token": self.csrfToken() },
          });
          if (!response.ok) {
            var error = await response.json();
            showToast({
              message: (error.error && error.error.message) || "Failed to queue rollback.",
              level: "error",
            });
            return;
          }
          self._pendingRollbacks[String(jobId)] = Date.now();
          showToast({ message: "Rollback queued.", level: "success" });
          await self.loadQueue();
          await self.refreshHistorySection();
        } catch (_) {
          delete self._pendingRollbacks[String(jobId)];
          showToast({ message: "Failed to queue rollback.", level: "error" });
        }
      });
    },

    clearHistory: async function (btn) {
      var self = this;
      pbConfirm({
        title: "Clear Utility History",
        message: "This will permanently delete all completed, partial, failed, cancelled, and rolled-back utility job records. Active jobs will not be affected.",
        confirmText: "Clear History",
      }).then(async function (ok) {
        if (!ok) return;
        btn.disabled = true;
        try {
          var response = await fetch("/api/v1/utilities/history", {
            method: "DELETE",
            headers: { "X-CSRF-Token": self.csrfToken() },
          });
          if (!response.ok) {
            throw new Error("Failed to clear history.");
          }
          var data = await response.json();
          self.expandedJob = null;
          showToast({
            message:
              "Cleared " +
              data.deleted +
              " utility history record" +
              (data.deleted !== 1 ? "s" : "") +
              ".",
            level: "success",
          });
          await self.loadQueue();
          await self.refreshHistorySection();
        } catch (error) {
          btn.disabled = false;
          showToast({ message: error.message, level: "error" });
        }
      });
    },
  };
}

function utilitiesSelectedFilesMixin(config) {
  var cfg = config || {};
  return Object.assign(fileBrowserMixin(cfg), {
    selectedFiles: [],
    validationError: "",
    submitting: false,

    applySelectedFiles: function (selection) {
      var files = (selection && selection.files) || [];
      var nextFiles = this.selectedFiles.slice();
      for (var i = 0; i < files.length; i++) {
        var incoming = files[i];
        if (!nextFiles.some(function (existing) { return existing.path === incoming.path; })) {
          nextFiles.push(incoming);
        }
      }
      this.selectedFiles = nextFiles;
      this.validationError = "";
      if (typeof this.onSelectedFilesChanged === "function") {
        this.onSelectedFilesChanged();
      }
    },

    openSelectedFilesBrowser: function (options) {
      var opts = Object.assign(
        {
          selectionMode: "files",
          onSelectAction: "applySelectedFiles",
          emptyMessage: "No matching files or subdirectories",
          confirmLabel: "Add Files",
        },
        options || {}
      );
      var currentValue =
        this.selectedFiles.length > 0
          ? this.selectedFiles[this.selectedFiles.length - 1].path
          : "/";
      this.openFileBrowser("_utilitySelectedFiles", currentValue, opts);
    },

    clearSelectedFiles: function () {
      this.selectedFiles = [];
      this.validationError = "";
      if (typeof this.onSelectedFilesChanged === "function") {
        this.onSelectedFilesChanged();
      }
    },

    removeSelectedFileAt: function (index) {
      this.selectedFiles.splice(index, 1);
      this.selectedFiles = this.selectedFiles.slice();
      this.validationError = "";
      if (typeof this.onSelectedFilesChanged === "function") {
        this.onSelectedFilesChanged();
      }
    },

    getSelectedFilePaths: function () {
      return this.selectedFiles.map(function (file) { return file.path; });
    },

    formatBytes: function (bytes) {
      return _pb.formatBytes(bytes);
    },
  });
}

function utilitiesConverterPage(config) {
  var cfg = config || {};
  return Object.assign(utilitiesSelectedFilesMixin(cfg), {
    sourceFormat: "cbr",
    targetFormat: "cbz",
    scope: "manual",
    pdfQuality: "medium",
    trashFolder: cfg.trashFolder || "",
    trashFolderBrowsePath: cfg.trashFolderBrowsePath || "",
    preview: null,
    previewLoading: false,
    _previewRequestSeq: 0,

    onSelectedFilesChanged: function () {
      if (this.getFilePaths().length === 0) {
        this.clearPreview();
        return;
      }
      if (this.preview) {
        this.refreshPreview({ preserveVisible: true });
        return;
      }
      this.clearPreview();
    },

    openFilePicker: function () {
      var extMap = { cbr: ".cbr", cb7: ".cb7", pdf: ".pdf", cbz: ".cbz" };
      var ext = extMap[this.sourceFormat] || ".cbr,.cb7,.cbz,.pdf";
      this.openSelectedFilesBrowser({
        extensions: ext,
        title: "Select Files",
        confirmLabel: "Add Files",
      });
    },

    clearPreview: function () {
      this._previewRequestSeq += 1;
      this.previewLoading = false;
      this.preview = null;
      this.validationError = "";
    },

    getFilePaths: function () {
      return this.getSelectedFilePaths();
    },

    refreshPreview: async function (options) {
      var opts = options || {};
      var preserveVisible = !!opts.preserveVisible;
      var filePaths = this.getFilePaths();

      this.validationError = "";
      if (filePaths.length === 0) {
        this._previewRequestSeq += 1;
        this.previewLoading = false;
        if (preserveVisible && this.preview) {
          this.preview = {
            source_format: this.sourceFormat,
            target_format: this.targetFormat,
            total_count: 0,
            total_size_bytes: 0,
            lossless: false,
            files: [],
          };
        } else {
          this.preview = null;
        }
        return;
      }

      var requestSeq = ++this._previewRequestSeq;
      this.previewLoading = true;
      try {
        var response = await fetch("/api/v1/utilities/convert/preview", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            source_format: this.sourceFormat,
            target_format: this.targetFormat,
            scope: this.scope,
            file_paths: filePaths,
          }),
        });
        if (requestSeq !== this._previewRequestSeq) {
          return;
        }
        if (!response.ok) {
          var error = await response.json();
          this.validationError = (error.error && error.error.message) || "Preview failed.";
          return;
        }
        this.preview = await response.json();
        this.validationError = "";
      } catch (_) {
        if (requestSeq !== this._previewRequestSeq) {
          return;
        }
        this.validationError = "Failed to load preview.";
      } finally {
        if (requestSeq === this._previewRequestSeq) {
          this.previewLoading = false;
        }
      }
    },

    runPreview: async function () {
      if (this.preview) {
        this.clearPreview();
        return;
      }
      await this.refreshPreview({ preserveVisible: false });
    },

    startConversion: async function () {
      this.submitting = true;
      try {
        var response = await fetch("/api/v1/utilities/jobs", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            job_type: "file_convert",
            display_name: "Convert " + this.sourceFormat.toUpperCase() + " → " + this.targetFormat.toUpperCase(),
            config: {
              source_format: this.sourceFormat,
              target_format: this.targetFormat,
              scope: this.scope,
              file_paths: this.getFilePaths(),
              trash_folder: this.trashFolder || undefined,
              pdf_quality: this.sourceFormat === "pdf" ? this.pdfQuality : undefined,
            },
          }),
        });
        if (!response.ok) {
          var error = await response.json();
          showToast({
            message: (error.error && error.error.message) || "Failed to submit.",
            level: "error",
          });
          return;
        }
        showToast({ message: "Conversion job queued.", level: "success" });
        window.location.href = "/utilities?tab=queue";
      } catch (_) {
        showToast({ message: "Failed to submit job.", level: "error" });
      } finally {
        this.submitting = false;
      }
    },
  });
}

function utilitiesMassConvertPage(config) {
  var cfg = config || {};
  return Object.assign(utilitiesSelectedFilesMixin(cfg), {
    scope: "folder",
    selectedFolder: "",
    selectedFolders: [],
    steps: {
      metadata: true,
      verify: false,
    },
    trashFolder: cfg.trashFolder || cfg.defaultTrashFolder || "",
    defaultTrashFolder: cfg.defaultTrashFolder || cfg.trashFolder || "",
    trashFolderBrowsePath: cfg.trashFolderBrowsePath || "",
    preview: null,
    previewLoaded: false,
    previewLoading: false,
    _previewRequestSeq: 0,
    _previewSyncTimer: 0,

    init: function () {
      var self = this;
      if (typeof this.$watch === "function") {
        this.$watch("scope", function () {
          self.syncFooterDock();
        });
        this.$watch("steps.metadata", function () {
          self.syncFooterDock();
        });
        this.$watch("steps.verify", function () {
          self.syncFooterDock();
        });
      }
      this.syncFooterDock();
    },

    onSelectedFilesChanged: function () {
      this.validationError = "";
      this.syncFooterDock();
      this.scheduleAutoPreview();
    },

    setScope: function (value) {
      if (value === "folder" || value === "files") {
        this.scope = value;
      } else {
        this.scope = "library";
      }
      this.selectedFiles = [];
      this.selectedFolder = "";
      this.selectedFolders = [];
      this.validationError = "";
      this.syncFooterDock();
      this.scheduleAutoPreview({ delay: 0, preserveVisible: false });
    },

    selectedSteps: function () {
      var steps = [1];
      if (this.steps.metadata) steps.push(2);
      if (this.steps.verify) steps.push(4);
      return steps;
    },

    enabledStepCount: function () {
      return this.selectedSteps().length;
    },

    scopeSelectionLabel: function () {
      if (this.scope === "files") {
        return "Select files";
      }
      if (this.scope === "library") {
        return "Entire library";
      }
      return "Select folders";
    },

    inferFileFormat: function (file) {
      var path = ((file && file.path) || (file && file.name) || "").toLowerCase();
      if (path.endsWith(".cbr")) return "CBR";
      if (path.endsWith(".cb7")) return "CB7";
      if (path.endsWith(".pdf")) return "PDF";
      if (path.endsWith(".cbz")) return "CBZ";
      return "FILE";
    },

    previewOutputName: function (file) {
      var name = (file && file.name) || "";
      return name.replace(/\.[^.]+$/, "") + ".cbz";
    },

    previewRows: function () {
      if (!this.previewLoaded || !this.preview || !Array.isArray(this.preview.items)) {
        return [];
      }
      return this.preview.items.map(function (item) {
        return {
          path: item.file_path,
          name: item.source_name,
          format: item.source_format,
          output: item.output_name,
          size: item.size_bytes ? _pb.formatBytes(item.size_bytes) : "—",
        };
      });
    },

    previewFileCount: function () {
      if (!this.previewLoaded || !this.preview) {
        return 0;
      }
      return this.preview.item_count || 0;
    },

    previewEmptyMessage: function () {
      if (this.scope === "files") {
        return "Browse for files to build the conversion preview.";
      }
      if (this.scope === "folder" && this.selectedFolder) {
        return "Pullbox is building the conversion preview for the selected folders.";
      }
      if (this.scope === "library") {
        return "Pullbox is building the conversion preview for the whole tracked library.";
      }
      return "Choose folders or files to build the conversion preview.";
    },

    syncFooterDock: function () {
      window.dispatchEvent(
        new CustomEvent("utilities:mass-convert-footer", {
          detail: {
            scope: this.scopeSelectionLabel(),
            files: String(this.previewFileCount()),
            steps: this.enabledStepCount() + " of 3",
          },
        })
      );
    },

    openFilePicker: function () {
      this.openSelectedFilesBrowser({
        extensions: ".cbr,.cb7,.cbz,.pdf",
        title: "Select Source Files",
        confirmLabel: "Add Files",
      });
    },

    openFolderPicker: function () {
      var currentValue =
        this.selectedFolders.length > 0
          ? this.selectedFolders[this.selectedFolders.length - 1].path
          : this.selectedFolder || "/";
      this.openFileBrowser("_utilityMassConvertFolder", currentValue, {
        selectionMode: "directories",
        onSelectAction: "applySelectedFolders",
        title: "Select Source Folders",
        confirmLabel: "Add Folders",
      });
    },

    applySelectedFolder: function (selection) {
      this.selectedFolder = (selection && selection.path) || "";
      this.selectedFolders = this.selectedFolder
        ? [{ path: this.selectedFolder, name: selection && selection.name ? selection.name : basename(this.selectedFolder) }]
        : [];
      this.validationError = "";
      this.syncFooterDock();
      this.scheduleAutoPreview();
    },

    applySelectedFolders: function (selection) {
      var next = this.selectedFolders.slice();
      if (selection && selection.mode === "directories") {
        var directories = selection.directories || [];
        for (var i = 0; i < directories.length; i++) {
          if (!next.some(function (entry) { return entry.path === directories[i].path; })) {
            next.push({
              path: directories[i].path,
              name: directories[i].name || basename(directories[i].path),
            });
          }
        }
      } else if (selection && selection.mode === "directory" && selection.path) {
        if (!next.some(function (entry) { return entry.path === selection.path; })) {
          next.push({
            path: selection.path,
            name: selection.name || basename(selection.path),
          });
        }
      }
      this.selectedFolders = next;
      this.selectedFolder = next.length > 0 ? next[0].path : "";
      this.validationError = "";
      this.syncFooterDock();
      this.scheduleAutoPreview();
    },

    browseTrashFolder: function () {
      this.openFileBrowser("trashFolder", this.trashFolder, {
        selectionMode: "directory",
        startPath: this.trashFolderBrowsePath,
        title: "Select Trash Folder",
        confirmLabel: "Use Folder",
      });
    },

    effectiveTrashFolder: function () {
      var value = this.trashFolder && this.trashFolder.trim ? this.trashFolder.trim() : "";
      return value || this.defaultTrashFolder || "";
    },

    previewPaths: function () {
      if (this.scope === "library") {
        return [];
      }
      if (this.scope === "folder") {
        if (this.selectedFolders.length > 0) {
          return this.selectedFolders.map(function (entry) { return entry.path; });
        }
        return this.selectedFolder ? [this.selectedFolder] : [];
      }
      return this.getSelectedFilePaths();
    },

    canRunPreview: function () {
      if (this.scope === "library") return true;
      if (this.scope === "folder") return this.previewPaths().length > 0;
      return this.selectedFiles.length > 0;
    },

    clearPreviewState: function () {
      if (this._previewSyncTimer) {
        window.clearTimeout(this._previewSyncTimer);
        this._previewSyncTimer = 0;
      }
      this._previewRequestSeq += 1;
      this.previewLoading = false;
      this.preview = null;
      this.previewLoaded = false;
      this.validationError = "";
      this.syncFooterDock();
    },

    scheduleAutoPreview: function (options) {
      var opts = options || {};
      var preserveVisible = opts.preserveVisible !== false;
      var delay = typeof opts.delay === "number" ? opts.delay : 120;

      if (this._previewSyncTimer) {
        window.clearTimeout(this._previewSyncTimer);
        this._previewSyncTimer = 0;
      }

      if (!this.canRunPreview()) {
        this.clearPreviewState();
        return;
      }

      var self = this;
      var run = function () {
        self._previewSyncTimer = 0;
        self.refreshPreview({ preserveVisible: preserveVisible });
      };

      if (delay <= 0) {
        run();
        return;
      }

      this._previewSyncTimer = window.setTimeout(run, delay);
    },

    refreshPreview: async function (options) {
      var opts = options || {};
      var preserveVisible = !!opts.preserveVisible;
      if (!this.canRunPreview()) {
        this.clearPreviewState();
        return;
      }

      var requestSeq = ++this._previewRequestSeq;
      this.previewLoading = true;
      this.validationError = "";
      try {
        var response = await fetch("/api/v1/utilities/mass-convert/preview", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            scope: this.scope === "files" ? "manual" : this.scope,
            file_paths: this.previewPaths(),
            trash_folder: this.effectiveTrashFolder() || undefined,
          }),
        });
        if (requestSeq !== this._previewRequestSeq) {
          return;
        }
        if (!response.ok) {
          var error = await response.json();
          this.validationError = (error.error && error.error.message) || "Preview failed.";
          if (!preserveVisible) {
            this.preview = null;
            this.previewLoaded = false;
          }
          this.syncFooterDock();
          return;
        }
        this.preview = await response.json();
        this.previewLoaded = true;
        this.syncFooterDock();
      } catch (_) {
        if (requestSeq !== this._previewRequestSeq) {
          return;
        }
        this.validationError = "Failed to load conversion preview.";
        if (!preserveVisible) {
          this.preview = null;
          this.previewLoaded = false;
        }
        this.syncFooterDock();
      } finally {
        if (requestSeq === this._previewRequestSeq) {
          this.previewLoading = false;
        }
      }
    },

    canStart: function () {
      if (this.scope === "files") {
        return this.selectedFiles.length > 0;
      }
      if (this.scope === "folder") {
        return this.previewPaths().length > 0;
      }
      return true;
    },

    runPreview: async function () {
      await this.refreshPreview({ preserveVisible: false });
    },

    startPipeline: async function () {
      var jobConfig = {
        steps: this.selectedSteps(),
        scope: "library",
        trash_folder: this.effectiveTrashFolder(),
      };

      if (this.scope === "files") {
        if (this.selectedFiles.length === 0) {
          this.validationError = "Choose at least one file to convert.";
          return;
        }
        jobConfig.scope = "manual";
        jobConfig.file_paths = this.getSelectedFilePaths();
      } else if (this.scope === "folder") {
        var folderPaths = this.previewPaths();
        if (folderPaths.length === 0) {
          this.validationError = "Choose at least one folder to convert.";
          return;
        }
        jobConfig.scope = "folder";
        if (folderPaths.length === 1) {
          jobConfig.scan_folder = folderPaths[0].trim();
        } else {
          jobConfig.scan_folders = folderPaths.map(function (path) { return path.trim(); });
        }
      } else {
        jobConfig.scope = "library";
      }

      if (!jobConfig.trash_folder) {
        this.validationError = "Trash folder could not be resolved.";
        return;
      }

      this.submitting = true;
      this.validationError = "";
      try {
        var response = await fetch("/api/v1/utilities/jobs", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            job_type: "mass_convert_pipeline",
            display_name: "Mass Convert to CBZ",
            config: jobConfig,
          }),
        });
        if (!response.ok) {
          var error = await response.json();
          this.validationError = (error.error && error.error.message) || "Failed to queue the pipeline.";
          return;
        }
        showToast({ message: "Mass convert job queued.", level: "success" });
        window.location.href = "/utilities?tab=queue";
      } catch (_) {
        this.validationError = "Failed to queue the pipeline.";
      } finally {
        this.submitting = false;
      }
    },
  });
}

function utilitiesMassRenamePage(config) {
  var cfg = config || {};
  return Object.assign(fileBrowserMixin(cfg), {
    target: "files",
    scope: "folder",
    renameTemplates: cfg.renameTemplates || {},
    allowedBrowseRoots: Array.isArray(cfg.allowedBrowseRoots) ? cfg.allowedBrowseRoots : [],
    selectedEntries: [],
    selectedFolder: "",
    selectedFolders: [],
    preview: null,
    previewLoaded: false,
    previewLoading: false,
    _previewRequestSeq: 0,
    _previewSyncTimer: 0,
    validationError: "",
    submitting: false,

    init: function () {
      this.syncFooterDock();
      this.scheduleAutoPreview({ delay: 0, preserveVisible: false });
    },

    setTarget: function (value) {
      this.target = value === "folders" ? "folders" : "files";
      this.scope = this.target === "folders" ? "manual" : "folder";
      this.selectedEntries = [];
      this.selectedFolder = "";
      this.selectedFolders = [];
      this.validationError = "";
      this.syncFooterDock();
      this.scheduleAutoPreview({ delay: 0, preserveVisible: false });
    },

    setScope: function (value) {
      if (value === "folder" && this.target === "folders") {
        this.scope = "manual";
      } else if (value === "library" || value === "folder") {
        this.scope = value;
      } else {
        this.scope = "manual";
      }
      this.selectedEntries = [];
      this.selectedFolder = "";
      this.selectedFolders = [];
      this.validationError = "";
      this.syncFooterDock();
      this.scheduleAutoPreview({ delay: 0, preserveVisible: false });
    },

    browseButtonLabel: function () {
      return "Browse";
    },

    getAllowedBrowseRoots: function () {
      return this.allowedBrowseRoots && this.allowedBrowseRoots.length > 0
        ? this.allowedBrowseRoots
        : [];
    },

    openTargetPicker: function () {
      var allowedRoots = this.getAllowedBrowseRoots();
      var defaultRoot = allowedRoots.length > 0 ? allowedRoots[0] : "/";

      if (this.scope === "folder") {
        var folderStart =
          this.selectedFolders.length > 0
            ? this.selectedFolders[this.selectedFolders.length - 1].path
            : this.selectedFolder || defaultRoot;
        this.openFileBrowser("_utilityRenameFolderScope", folderStart, {
          selectionMode: "directories",
          onSelectAction: "applyRenameFolderSelection",
          title: this.target === "folders" ? "Select Series Folders" : "Select Source Folders",
          confirmLabel: "Add Folders",
          allowedRoots: allowedRoots,
          startPath: defaultRoot,
        });
        return;
      }

      var currentValue =
        this.selectedEntries.length > 0
          ? this.selectedEntries[this.selectedEntries.length - 1].path
          : defaultRoot;
      if (this.target === "folders") {
        this.openFileBrowser("_utilityRenamePaths", currentValue, {
          selectionMode: "directories",
          onSelectAction: "applyRenameSelection",
          title: "Select Series Folders",
          confirmLabel: "Add Folders",
          allowedRoots: allowedRoots,
          startPath: defaultRoot,
        });
        return;
      }

      this.openFileBrowser("_utilityRenamePaths", currentValue, {
        selectionMode: "files",
        onSelectAction: "applyRenameSelection",
        title: "Select Library Files",
        confirmLabel: "Add Files",
        extensions: ".cbz,.cbr,.cb7,.cbt,.pdf,.epub",
        allowedRoots: allowedRoots,
        startPath: defaultRoot,
      });
    },

    applyRenameFolderSelection: function (selection) {
      var next = this.selectedFolders.slice();
      if (selection && selection.mode === "directories") {
        var directories = selection.directories || [];
        for (var i = 0; i < directories.length; i++) {
          if (!next.some(function (entry) { return entry.path === directories[i].path; })) {
            next.push({
              path: directories[i].path,
              name: directories[i].name || basename(directories[i].path),
            });
          }
        }
      } else if (selection && selection.mode === "directory" && selection.path) {
        if (!next.some(function (entry) { return entry.path === selection.path; })) {
          next.push({
            path: selection.path,
            name: selection.name || basename(selection.path),
          });
        }
      }
      this.selectedFolders = next;
      this.selectedFolder = next.length > 0 ? next[0].path : "";
      this.validationError = "";
      this.syncFooterDock();
      this.scheduleAutoPreview();
    },

    applyRenameSelection: function (selection) {
      var next = this.selectedEntries.slice();
      if (this.scope === "library") {
        this.scope = "manual";
      }
      if (selection && selection.mode === "files") {
        var files = selection.files || [];
        for (var i = 0; i < files.length; i++) {
          if (!next.some(function (entry) { return entry.path === files[i].path; })) {
            next.push({
              path: files[i].path,
              name: files[i].name,
            });
          }
        }
      } else if (selection && selection.mode === "directories") {
        var directories = selection.directories || [];
        for (var j = 0; j < directories.length; j++) {
          if (!next.some(function (entry) { return entry.path === directories[j].path; })) {
            next.push({
              path: directories[j].path,
              name: directories[j].name || basename(directories[j].path),
            });
          }
        }
      } else if (selection && selection.mode === "directory" && selection.path) {
        if (!next.some(function (entry) { return entry.path === selection.path; })) {
          next.push({
            path: selection.path,
            name: selection.name || selection.path,
          });
        }
      }
      this.selectedEntries = next;
      this.validationError = "";
      this.syncFooterDock();
      this.scheduleAutoPreview();
    },

    clearSelectedEntries: function () {
      this.selectedEntries = [];
      this.selectedFolder = "";
      this.selectedFolders = [];
      this.validationError = "";
      this.syncFooterDock();
      this.scheduleAutoPreview({ delay: 0, preserveVisible: false });
    },

    removeSelectedEntryAt: function (index) {
      this.selectedEntries.splice(index, 1);
      this.selectedEntries = this.selectedEntries.slice();
      this.validationError = "";
      this.syncFooterDock();
      this.scheduleAutoPreview({ delay: 0, preserveVisible: false });
    },

    getSelectedPaths: function () {
      return this.selectedEntries.map(function (entry) { return entry.path; });
    },

    previewPaths: function () {
      if (this.scope === "folder") {
        if (this.selectedFolders.length > 0) {
          return this.selectedFolders.map(function (entry) { return entry.path; });
        }
        return this.selectedFolder ? [this.selectedFolder] : [];
      }
      if (this.scope === "library") {
        return [];
      }
      return this.getSelectedPaths();
    },

    canRunPreview: function () {
      if (this.scope === "library") return true;
      if (this.scope === "folder") return this.previewPaths().length > 0;
      return this.selectedEntries.length > 0;
    },

    clearPreviewState: function () {
      if (this._previewSyncTimer) {
        window.clearTimeout(this._previewSyncTimer);
        this._previewSyncTimer = 0;
      }
      this._previewRequestSeq += 1;
      this.previewLoading = false;
      this.preview = null;
      this.previewLoaded = false;
      this.validationError = "";
      this.syncFooterDock();
    },

    scheduleAutoPreview: function (options) {
      var opts = options || {};
      var preserveVisible = opts.preserveVisible !== false;
      var delay = typeof opts.delay === "number" ? opts.delay : 120;

      if (this._previewSyncTimer) {
        window.clearTimeout(this._previewSyncTimer);
        this._previewSyncTimer = 0;
      }

      if (!this.canRunPreview()) {
        this.clearPreviewState();
        return;
      }

      var self = this;
      var run = function () {
        self._previewSyncTimer = 0;
        self.refreshPreview({ preserveVisible: preserveVisible });
      };

      if (delay <= 0) {
        run();
        return;
      }

      this._previewSyncTimer = window.setTimeout(run, delay);
    },

    refreshPreview: async function (options) {
      var opts = options || {};
      var preserveVisible = !!opts.preserveVisible;
      if (!this.canRunPreview()) {
        this.clearPreviewState();
        return;
      }

      var requestSeq = ++this._previewRequestSeq;
      this.previewLoading = true;
      this.validationError = "";
      try {
        var response = await fetch("/api/v1/utilities/rename/preview", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            target: this.target,
            scope: this.scope,
            file_paths: this.previewPaths(),
          }),
        });
        if (requestSeq !== this._previewRequestSeq) {
          return;
        }
        if (!response.ok) {
          var error = await response.json();
          this.validationError = (error.error && error.error.message) || "Preview failed.";
          if (!preserveVisible) {
            this.preview = null;
            this.previewLoaded = false;
          }
          this.syncFooterDock();
          return;
        }
        this.preview = await response.json();
        this.previewLoaded = true;
        this.syncFooterDock();
      } catch (_) {
        if (requestSeq !== this._previewRequestSeq) {
          return;
        }
        this.validationError = "Failed to load rename preview.";
        if (!preserveVisible) {
          this.preview = null;
          this.previewLoaded = false;
        }
        this.syncFooterDock();
      } finally {
        if (requestSeq === this._previewRequestSeq) {
          this.previewLoading = false;
        }
      }
    },

    runPreview: async function () {
      await this.refreshPreview({ preserveVisible: false });
    },

    targetLabel: function () {
      return this.target === "folders" ? "Folders" : "Files";
    },

    scopeLabel: function () {
      if (this.scope === "folder") return "Select folders";
      if (this.scope === "manual") return this.target === "folders" ? "Select folders" : "Select files";
      return "Entire library";
    },

    previewChangeCount: function () {
      if (!this.previewLoaded || !this.preview) {
        return 0;
      }
      return this.preview.actionable_count || 0;
    },

    previewEmptyMessage: function () {
      if (this.scope === "library") {
        return "Pullbox will build a rename plan for the whole tracked library.";
      }
      if (this.scope === "folder") {
        return this.selectedFolders.length > 0 || this.selectedFolder
          ? "Pullbox is building the rename plan for the selected folders."
          : "Choose folders to generate the rename plan.";
      }
      return this.target === "files"
        ? "Browse for files to generate the rename plan."
        : "Browse for folders to generate the rename plan.";
    },

    colonReplacementLabel: function () {
      var replacement = this.renameTemplates.colonReplacement || "dash";
      return "→ " + replacement;
    },

    illegalCharLabel: function () {
      return this.renameTemplates.replaceIllegalCharacters === "true" ? "sanitized" : "left as-is";
    },

    syncFooterDock: function () {
      window.dispatchEvent(
        new CustomEvent("utilities:mass-rename-footer", {
          detail: {
            target: this.targetLabel(),
            scope: this.scopeLabel(),
            changes: String(this.previewChangeCount()),
          },
        })
      );
    },

    primaryTemplate: function () {
      if (this.target === "folders") {
        return this.renameTemplates.folder || "{Series} ({Year})";
      }
      return this.renameTemplates.issue || "{Series} ({Year}) #{Issue:03d}";
    },

    canStartRename: function () {
      return !!(
        this.previewLoaded &&
        this.preview &&
        this.preview.actionable_count > 0
      );
    },

    startRename: async function () {
      if (!this.canStartRename()) {
        this.validationError = "Run a preview first so Pullbox can build the rename plan.";
        return;
      }

      var items = (this.preview.items || [])
        .filter(function (item) { return !!item.actionable; })
        .map(function (item) {
          return {
            file_path: item.file_path,
            proposed_name: item.proposed_name,
            operation: "rename",
          };
        });

      if (items.length === 0) {
        this.validationError = "No actionable rename items were found in the preview.";
        return;
      }

      this.submitting = true;
      this.validationError = "";
      try {
        var response = await fetch("/api/v1/utilities/jobs", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            job_type: "mass_rename",
            display_name: "Mass Rename — " + (this.target === "folders" ? "Folders" : "Files"),
            config: {
              target: this.target,
              template: this.primaryTemplate(),
              scope: this.scope,
              file_paths: this.previewPaths(),
              items: items,
            },
          }),
        });
        if (!response.ok) {
          var error = await response.json();
          this.validationError = (error.error && error.error.message) || "Failed to queue the rename job.";
          return;
        }
        showToast({ message: "Mass rename job queued.", level: "success" });
        window.location.href = "/utilities?tab=queue";
      } catch (_) {
        this.validationError = "Failed to queue the rename job.";
      } finally {
        this.submitting = false;
      }
    },
  });
}

function libraryBrowserPage(config) {
  var cfg = config || {};

  function basename(path) {
    if (!path || path === "/") return "/";
    var normalized = path.endsWith("/") && path.length > 1 ? path.slice(0, -1) : path;
    var lastSlash = normalized.lastIndexOf("/");
    return lastSlash >= 0 ? normalized.substring(lastSlash + 1) || "/" : normalized;
  }

  function parentDirectory(path) {
    if (!path || path === "/") return "/";
    var normalized = path.endsWith("/") && path.length > 1 ? path.slice(0, -1) : path;
    var lastSlash = normalized.lastIndexOf("/");
    if (lastSlash <= 0) return "/";
    return normalized.substring(0, lastSlash);
  }

  function splitFileName(name) {
    if (!name) return { base: "", extension: "" };
    var lastDot = name.lastIndexOf(".");
    if (lastDot <= 0) {
      return { base: name, extension: "" };
    }
    return {
      base: name.substring(0, lastDot),
      extension: name.substring(lastDot),
    };
  }

  function boolValue(value) {
    return value === true || value === "true" || value === "1";
  }

  function cloneEntry(entry) {
    return entry ? JSON.parse(JSON.stringify(entry)) : null;
  }

  return {
    csrf: cfg.csrfToken || "",
    renameTemplates: cfg.renameTemplates || {},
    utilityTrashFolder: cfg.utilityTrashFolder || "",
    currentPath: cfg.currentPath || "",
    rootPath: cfg.rootPath || "",
    rootConfigured: !!cfg.rootConfigured,
    contextMenu: { open: false, x: 0, y: 0 },
    contextTarget: null,
    modal: "",
    modalEntry: {},
    modalLoading: false,
    modalError: "",
    actionSubmitting: false,
    renameForm: { name: "" },
    autoRenamePreview: null,
    convertPreview: null,
    deleteFiles: false,
    deleteFolder: false,
    entryCache: {},

    init: function () {
      this.contextMenu.open = false;
    },

    focusRef: function (refName, options) {
      var opts = options || {};
      var self = this;
      var attempts = 0;
      var focusTarget = function () {
        attempts += 1;
        if (!self.$refs || !self.$refs[refName] || typeof self.$refs[refName].focus !== "function") {
          if (attempts < 4) {
            window.setTimeout(focusTarget, 16);
          }
          return;
        }
        self.$refs[refName].focus({ preventScroll: true });
        if (document.activeElement !== self.$refs[refName] && attempts < 4) {
          window.setTimeout(focusTarget, 16);
          return;
        }
        if (opts.select && typeof self.$refs[refName].select === "function") {
          self.$refs[refName].select();
        }
      };
      if (typeof this.$nextTick === "function") {
        this.$nextTick(function () {
          window.requestAnimationFrame(focusTarget);
          window.setTimeout(focusTarget, 40);
          window.setTimeout(focusTarget, 96);
        });
        return;
      }
      window.requestAnimationFrame(focusTarget);
      window.setTimeout(focusTarget, 40);
    },

    csrfToken: function () {
      return this.csrf || "";
    },

    parseEntryDataset: function (dataset) {
      if (!dataset || !dataset.entryPath) return null;
      return {
        name: dataset.entryName || basename(dataset.entryPath),
        path: dataset.entryPath,
        kind: dataset.entryKind || "file",
        kindLabel:
          dataset.entryKind === "root"
            ? "Library Root"
            : dataset.entryKind === "folder"
              ? "Folder"
              : "File",
        rootPath: dataset.entryRootPath || this.rootPath || "",
        fileFormat: (dataset.entryFormat || "").toLowerCase(),
        canConvert: boolValue(dataset.entryConvertible),
        canMutate: boolValue(dataset.entryCanMutate),
      };
    },

    openContextMenu: function (event) {
      var entry = this.parseEntryDataset(
        event && event.currentTarget && event.currentTarget.dataset
          ? event.currentTarget.dataset
          : null
      );
      if (!entry) return;
      this.resetModalState();
      this.contextTarget = entry;
      this.contextMenu.x = event.clientX || 0;
      this.contextMenu.y = event.clientY || 0;
      this.contextMenu.open = true;

      var self = this;
      var position = function () {
        self.positionContextMenu();
      };
      if (typeof this.$nextTick === "function") {
        this.$nextTick(position);
      } else {
        window.requestAnimationFrame(position);
      }
    },

    positionContextMenu: function () {
      if (!this.contextMenu.open || !this.$refs || !this.$refs.contextMenu) return;
      var menu = this.$refs.contextMenu;
      var menuRect = menu.getBoundingClientRect();
      var viewportWidth = Math.max(document.documentElement.clientWidth, window.innerWidth || 0);
      var viewportHeight = Math.max(
        document.documentElement.clientHeight,
        window.innerHeight || 0
      );
      var margin = 12;
      var x = this.contextMenu.x;
      var y = this.contextMenu.y;

      if (x + menuRect.width + margin > viewportWidth) {
        x = Math.max(margin, viewportWidth - menuRect.width - margin);
      }
      if (y + menuRect.height + margin > viewportHeight) {
        y = Math.max(margin, viewportHeight - menuRect.height - margin);
      }
      this.contextMenu.x = x;
      this.contextMenu.y = y;
    },

    contextMenuStyle: function () {
      return "left:" + this.contextMenu.x + "px;top:" + this.contextMenu.y + "px;";
    },

    closeContextMenu: function (options) {
      var opts = options || {};
      this.contextMenu.open = false;
      if (!opts.preserveTarget && !this.modal) {
        this.contextTarget = null;
      }
    },

    handleWindowClick: function (event) {
      if (!this.contextMenu.open) return;
      if (this.$refs && this.$refs.contextMenu && this.$refs.contextMenu.contains(event.target)) {
        return;
      }
      this.closeContextMenu();
    },

    handleEscape: function () {
      if (this.modal) {
        this.closeModal();
        return;
      }
      this.closeContextMenu();
    },

    isContextTarget: function (path) {
      return !!(this.contextTarget && path && this.contextTarget.path === path);
    },

    resetModalState: function () {
      this.modal = "";
      this.modalEntry = {};
      this.modalLoading = false;
      this.modalError = "";
      this.actionSubmitting = false;
      this.renameForm = { name: "", extension: "" };
      this.autoRenamePreview = null;
      this.convertPreview = null;
      this.deleteFiles = false;
      this.deleteFolder = false;
    },

    closeModal: function () {
      this.resetModalState();
      this.contextTarget = null;
    },

    renameHasStaleReference: function () {
      return !!(
        this.modalEntry &&
        this.modalEntry.renameContext &&
        this.modalEntry.renameContext.stale_reference
      );
    },

    renameBlockedMessage: function () {
      return (
        (this.modalEntry &&
          this.modalEntry.renameContext &&
          this.modalEntry.renameContext.message) ||
        "This item has a stale database reference. Run the Database Integrity Check before renaming it from Library."
      );
    },

    openRenameRepairWorkflow: function () {
      var target =
        (this.modalEntry &&
          this.modalEntry.renameContext &&
          this.modalEntry.renameContext.db_check_url) ||
        "/utilities/db-check";
      window.location.href = target;
    },

    dispatchToast: function (message, level) {
      if (typeof showToast === "function") {
        showToast({ message: message, level: level || "info" });
      }
    },

    queueToastForNextPage: function (message, level) {
      if (typeof queueToastForNextPage === "function") {
        queueToastForNextPage({ message: message, level: level || "info" });
      }
    },

    requestJson: async function (url, options) {
      var response = await fetch(url, options || {});
      var data = null;
      try {
        data = await response.json();
      } catch (_) {
        data = null;
      }
      if (!response.ok) {
        throw new Error(
          (data && data.error && data.error.message) ||
            (data && data.message) ||
            "Request failed."
        );
      }
      return data || {};
    },

    normalizeEntryDetails: function (data) {
      var storage = data.storage || {};
      return {
        name: data.name || "",
        path: data.path || "",
        kind: data.kind || "file",
        kindLabel: data.kind_label || "File",
        rootName: data.root_name || "",
        rootPath: data.root_path || "",
        fileFormat: (data.file_format || "").toLowerCase(),
        sizeBytes:
          typeof data.size_bytes === "number" ? data.size_bytes : data.size_bytes === 0 ? 0 : null,
        itemCount:
          typeof data.item_count === "number" ? data.item_count : data.item_count === 0 ? 0 : null,
        modifiedAt: data.modified_at || "",
        permissionsLabel: data.permissions_label || "",
        actions: data.actions || {},
        canMutate: !!(
          data.actions &&
          (data.actions.can_rename ||
            data.actions.can_auto_rename ||
            data.actions.can_delete)
        ),
        canConvert: !!(data.actions && data.actions.can_convert),
        canDelete: !!(data.actions && data.actions.can_delete),
        deleteContext: data.delete_context || {
          mode: data.kind === "root" ? "root" : data.kind === "folder" ? "folder" : "file",
          trash_enabled: false,
          linked_file_count: 0,
          tracked_file_count: 0,
          tracked_series_count: 0,
          managed_file_count: 0,
          referenced_file_count: 0,
          has_linked_issue: false,
          issue_status_after_delete: null,
          issue_status_reason: null,
        },
        renameContext: data.rename_context || {
          stale_reference: false,
          reason_code: null,
          message: null,
          db_check_url: "/utilities/db-check",
        },
        storage: {
          totalBytes:
            typeof storage.total_bytes === "number"
              ? storage.total_bytes
              : storage.total_bytes === 0
                ? 0
                : null,
          usedBytes:
            typeof storage.used_bytes === "number"
              ? storage.used_bytes
              : storage.used_bytes === 0
                ? 0
                : null,
          freeBytes:
            typeof storage.free_bytes === "number"
              ? storage.free_bytes
              : storage.free_bytes === 0
                ? 0
                : null,
          usedPct:
            typeof storage.used_pct === "number"
              ? storage.used_pct
              : storage.used_pct === 0
                ? 0
                : null,
        },
      };
    },

    ensureEntryDetails: async function (path) {
      if (this.entryCache[path]) {
        return cloneEntry(this.entryCache[path]);
      }
      var data = await this.requestJson(
        "/api/v1/library/browser/entry?path=" + encodeURIComponent(path),
        {
          headers: { "X-CSRF-Token": this.csrfToken() },
        }
      );
      var normalized = this.normalizeEntryDetails(data);
      this.entryCache[path] = normalized;
      return cloneEntry(normalized);
    },

    deleteMode: function () {
      if (!this.modalEntry || !this.modalEntry.deleteContext) return "file";
      return this.modalEntry.deleteContext.mode || "file";
    },

    deleteUsesTrash: function () {
      return !!(
        this.modalEntry &&
        this.modalEntry.deleteContext &&
        this.modalEntry.deleteContext.trash_enabled
      );
    },

    deleteSubmitLabel: function () {
      if (this.deleteReferencedFileCount() > 0 && this.deleteManagedFileCount() === 0) {
        return "Remove from Pullbox";
      }
      if (this.deleteMode() === "series") return "Delete Series";
      if (this.deleteMode() === "folder") return "Delete Folder";
      return "Delete File";
    },

    deleteSeriesLinkedFileCount: function () {
      return (
        (this.modalEntry &&
          this.modalEntry.deleteContext &&
          this.modalEntry.deleteContext.linked_file_count) ||
        0
      );
    },

    deleteSeriesModalMessage: function () {
      var title =
        (this.modalEntry &&
          this.modalEntry.deleteContext &&
          this.modalEntry.deleteContext.series_title) ||
        (this.modalEntry && this.modalEntry.name) ||
        "this series";
      var base = "This folder is associated with the series " + title + " in Pullbox.";
      if (this.deleteReferencedFileCount() > 0) {
        return base + " Referenced files will stay on disk and be detached from Pullbox. Any folder containing them will also stay in place.";
      }
      if (this.deleteUsesTrash()) {
        return base + " Deleting it will move the folder into the configured trash folder and remove the series and all issue records from the database.";
      }
      return base + " Deleting it will permanently remove the folder and delete the series and all issue records from the database.";
    },

    deleteSeriesDispositionLabel: function () {
      if (this.deleteReferencedFileCount() > 0) {
        return "Detach referenced files; remove managed files only";
      }
      return this.deleteUsesTrash()
        ? "Move folder to trash and delete series"
        : "Permanent folder delete and series removal";
    },

    deleteFolderMessage: function () {
      if (this.deleteReferencedFileCount() > 0) {
        return "Referenced files and any folder containing them will stay in place. Pullbox will detach their records and remove only managed files in this folder.";
      }
      if (this.deleteUsesTrash()) {
        return "This will move the selected folder and everything inside it into the configured trash folder.";
      }
      return "This will permanently delete the selected folder and everything inside it.";
    },

    deleteFileMessage: function () {
      if (this.deleteReferencedFileCount() > 0) {
        return "This referenced file will stay exactly where it is. Pullbox will remove only its tracked record and update the linked issue state.";
      }
      var disposition = this.deleteUsesTrash()
        ? "move the selected file into the configured trash folder"
        : "permanently delete the selected file";
      if (this.deleteHasLinkedIssue()) {
        return (
          "This will " +
          disposition +
          ", remove its tracked file record from Pullbox, and update the linked issue to " +
          this.deleteFileIssueStatusShortLabel() +
          "."
        );
      }
      if (this.deleteTrackedFileCount() > 0) {
        return (
          "This will " +
          disposition +
          " and remove its tracked file record from Pullbox."
        );
      }
      if (this.deleteUsesTrash()) {
        return "This will move the selected file into the configured trash folder.";
      }
      return "This will permanently delete the selected file.";
    },

    deleteDispositionLabel: function () {
      if (this.deleteReferencedFileCount() > 0 && this.deleteManagedFileCount() === 0) {
        return "Detach from Pullbox";
      }
      return this.deleteUsesTrash() ? "Move to trash" : "Permanent delete";
    },

    deleteFileTrackingLabel: function () {
      if (this.deleteHasLinkedIssue()) {
        return "Tracked file linked to a series issue.";
      }
      if (this.deleteTrackedFileCount() > 0) {
        return "Tracked file record only. No linked issue will be updated.";
      }
      return "No tracked library record will be updated.";
    },

    deleteHasLinkedIssue: function () {
      return !!(
        this.modalEntry &&
        this.modalEntry.deleteContext &&
        this.modalEntry.deleteContext.has_linked_issue
      );
    },

    deleteFileIssueStatusShortLabel: function () {
      var status =
        this.modalEntry &&
        this.modalEntry.deleteContext &&
        this.modalEntry.deleteContext.issue_status_after_delete;
      if (status === "wanted") return "Wanted";
      if (status === "skipped") return "Skipped";
      return "its next tracked state";
    },

    deleteFileIssueStatusLabel: function () {
      if (!this.deleteHasLinkedIssue()) {
        return "No linked issue status change.";
      }
      var status =
        this.modalEntry &&
        this.modalEntry.deleteContext &&
        this.modalEntry.deleteContext.issue_status_after_delete;
      var reason =
        this.modalEntry &&
        this.modalEntry.deleteContext &&
        this.modalEntry.deleteContext.issue_status_reason;
      if (status === "wanted" && reason === "series_monitored") {
        return "Wanted — the series is monitored.";
      }
      if (status === "skipped" && reason === "manual_skip") {
        return "Skipped — the issue is manually skipped.";
      }
      if (status === "skipped" && reason === "series_unmonitored") {
        return "Skipped — the series is not monitored.";
      }
      if (status === "wanted") {
        return "Wanted";
      }
      if (status === "skipped") {
        return "Skipped";
      }
      return "No linked issue status change.";
    },

    deleteTrackedFileCount: function () {
      return (
        (this.modalEntry &&
          this.modalEntry.deleteContext &&
          this.modalEntry.deleteContext.tracked_file_count) ||
        0
      );
    },

    deleteManagedFileCount: function () {
      return (
        (this.modalEntry &&
          this.modalEntry.deleteContext &&
          this.modalEntry.deleteContext.managed_file_count) ||
        0
      );
    },

    deleteReferencedFileCount: function () {
      return (
        (this.modalEntry &&
          this.modalEntry.deleteContext &&
          this.modalEntry.deleteContext.referenced_file_count) ||
        0
      );
    },

    deleteTrackedSeriesCount: function () {
      return (
        (this.modalEntry &&
          this.modalEntry.deleteContext &&
          this.modalEntry.deleteContext.tracked_series_count) ||
        0
      );
    },

    syncDeleteOptions: function () {
      this.deleteFiles = !!this.deleteFolder;
    },

    openProperties: async function () {
      if (!this.contextTarget) return;
      var entry = cloneEntry(this.contextTarget);
      this.closeContextMenu({ preserveTarget: true });
      this.modal = "properties";
      this.modalLoading = true;
      this.modalError = "";
      this.modalEntry = entry || {};
      try {
        this.modalEntry = await this.ensureEntryDetails(entry.path);
      } catch (error) {
        this.modalError = error.message || "Failed to load item details.";
      } finally {
        this.modalLoading = false;
      }
    },

    openRename: async function () {
      if (!this.contextTarget || !this.contextTarget.canMutate) return;
      var entry = cloneEntry(this.contextTarget);
      this.closeContextMenu({ preserveTarget: true });
      this.modalLoading = true;
      this.modalError = "";
      this.modalEntry = entry || {};
      this.renameForm = this.renameFormForEntry(entry);
      try {
        this.modalEntry = await this.ensureEntryDetails(entry.path);
        if (this.renameHasStaleReference()) {
          this.modal = "rename-stale";
          return;
        }
        this.modal = "rename";
        this.renameForm = this.renameFormForEntry(this.modalEntry);
      } catch (error) {
        this.modalError = error.message || "Failed to load item details.";
      } finally {
        this.modalLoading = false;
        if (this.modal === "rename") {
          this.focusRef("renameInput", { select: true });
        }
      }
    },

    openAutoRename: async function () {
      if (!this.contextTarget || !this.contextTarget.canMutate) return;
      var entry = cloneEntry(this.contextTarget);
      this.closeContextMenu({ preserveTarget: true });
      this.modalLoading = true;
      this.modalError = "";
      this.modalEntry = entry || {};
      this.autoRenamePreview = null;
      try {
        this.modalEntry = await this.ensureEntryDetails(entry.path);
        if (this.renameHasStaleReference()) {
          this.modal = "rename-stale";
          return;
        }
        this.modal = "auto-rename";
        this.autoRenamePreview = await this.requestJson("/api/v1/utilities/rename/preview", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            target: this.modalEntry.kind === "folder" ? "folders" : "files",
            scope: "manual",
            file_paths: [this.modalEntry.path],
          }),
        });
      } catch (error) {
        this.modalError = error.message || "Failed to build rename preview.";
      } finally {
        this.modalLoading = false;
      }
    },

    openConvert: async function () {
      if (!this.contextTarget || !this.contextTarget.canConvert) return;
      var entry = cloneEntry(this.contextTarget);
      this.closeContextMenu({ preserveTarget: true });
      this.modal = "convert";
      this.modalLoading = true;
      this.modalError = "";
      this.modalEntry = entry || {};
      this.convertPreview = null;
      try {
        this.modalEntry = await this.ensureEntryDetails(entry.path);
        this.convertPreview = await this.requestJson(
          "/api/v1/utilities/mass-convert/preview",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": this.csrfToken(),
            },
            body: JSON.stringify({
              scope: "manual",
              file_paths: [this.modalEntry.path],
              trash_folder: this.utilityTrashFolder || undefined,
            }),
          }
        );
      } catch (error) {
        this.modalError = error.message || "Failed to build conversion preview.";
      } finally {
        this.modalLoading = false;
      }
    },

    openDelete: async function () {
      if (!this.contextTarget || !this.contextTarget.canMutate) return;
      var entry = cloneEntry(this.contextTarget);
      this.closeContextMenu({ preserveTarget: true });
      this.modalLoading = true;
      this.modalError = "";
      this.modalEntry = entry || {};
      this.deleteFiles = false;
      this.deleteFolder = false;
      try {
        this.modalEntry = await this.ensureEntryDetails(entry.path);
        var mode = this.deleteMode();
        if (mode === "series") {
          this.deleteFolder = true;
          this.deleteFiles = false;
          this.modal = "delete-series";
        } else if (mode === "folder") {
          this.modal = "delete-folder";
        } else {
          this.modal = "delete-file";
        }
      } catch (error) {
        this.modalError = error.message || "Failed to load delete details.";
        this.contextTarget = null;
      } finally {
        this.modalLoading = false;
      }
    },

    formatBytesLabel: function (bytes) {
      if (bytes === null || bytes === undefined || bytes === "") return "—";
      if (window._pb && typeof window._pb.formatBytes === "function") {
        return window._pb.formatBytes(bytes);
      }
      return String(bytes) + " B";
    },

    formatDateTimeLabel: function (iso) {
      if (!iso) return "—";
      if (window._pb && typeof window._pb.formatFull === "function") {
        return window._pb.formatFull(iso);
      }
      return iso;
    },

    formatItemCountLabel: function (count) {
      if (count === null || count === undefined) return "—";
      return String(count);
    },

    renameDialogTitle: function () {
      if (this.modalEntry && this.modalEntry.kind === "folder") return "Rename Folder";
      if (this.modalEntry && this.modalEntry.kind === "file") return "Rename File";
      return "Rename";
    },

    renameFormForEntry: function (entry) {
      var name = "";
      if (entry && entry.name) {
        name = entry.name;
      } else if (entry && entry.path) {
        name = basename(entry.path);
      }
      if (entry && entry.kind === "file") {
        var fileParts = splitFileName(name);
        return {
          name: fileParts.base || name,
          extension: fileParts.extension,
        };
      }
      return {
        name: name,
        extension: "",
      };
    },

    renameCurrentName: function () {
      if (this.modalEntry && this.modalEntry.name) return this.modalEntry.name;
      if (this.modalEntry && this.modalEntry.path) return basename(this.modalEntry.path);
      return "";
    },

    renameFileNameParts: function () {
      return splitFileName(this.renameCurrentName());
    },

    renameEditableName: function () {
      if (this.modalEntry && this.modalEntry.kind === "file") {
        return this.renameFileNameParts().base || this.renameCurrentName();
      }
      return this.renameCurrentName();
    },

    renameSubmittedName: function () {
      var proposed = this.renameForm && typeof this.renameForm.name === "string"
        ? this.renameForm.name.trim()
        : "";
      var extension = this.renameForm && typeof this.renameForm.extension === "string"
        ? this.renameForm.extension
        : "";
      if (extension) {
        if (
          proposed.toLowerCase().endsWith(extension.toLowerCase())
        ) {
          proposed = proposed.slice(0, proposed.length - extension.length).trim();
        }
        if (!proposed) return this.renameCurrentName() || "—";
        return proposed + extension;
      }
      if (proposed) return proposed;
      return this.renameCurrentName() || "—";
    },

    renameTargetPreviewPath: function () {
      if (!this.modalEntry || !this.modalEntry.path) return "—";
      var proposed = this.renameSubmittedName();
      if (!proposed) return this.modalEntry.path;
      var parent = parentDirectory(this.modalEntry.path);
      if (!parent || parent === "/") {
        return "/" + proposed;
      }
      return parent.replace(/\/$/, "") + "/" + proposed;
    },

    fileFormatLabel: function () {
      if (!this.modalEntry || !this.modalEntry.fileFormat) return "—";
      return String(this.modalEntry.fileFormat).toUpperCase();
    },

    autoRenamePreviewItem: function () {
      if (!this.autoRenamePreview || !Array.isArray(this.autoRenamePreview.items)) return null;
      return this.autoRenamePreview.items.length > 0 ? this.autoRenamePreview.items[0] : null;
    },

    convertPreviewItem: function () {
      if (!this.convertPreview || !Array.isArray(this.convertPreview.items)) return null;
      return this.convertPreview.items.length > 0 ? this.convertPreview.items[0] : null;
    },

    canSubmitAutoRename: function () {
      return !!(
        this.autoRenamePreview &&
        typeof this.autoRenamePreview.actionable_count === "number" &&
        this.autoRenamePreview.actionable_count > 0
      );
    },

    canSubmitConvert: function () {
      return !!(
        this.convertPreview &&
        typeof this.convertPreview.item_count === "number" &&
        this.convertPreview.item_count > 0
      );
    },

    renameTemplateForPreviewItem: function (previewItem) {
      var key = previewItem && previewItem.template_key ? previewItem.template_key : "";
      if (this.modalEntry && this.modalEntry.kind === "folder") {
        return this.renameTemplates.folder || "{Series} ({Year})";
      }
      if (key === "annual_file_template") {
        return this.renameTemplates.annual || "{Series} ({Year}) Annual #{Issue:03d}";
      }
      if (key === "non_standard_file_template") {
        return (
          this.renameTemplates.collectionNonStandard ||
          "{Series} ({Year}) {Type} {Volume:02d}"
        );
      }
      if (key === "single_non_standard_file_template") {
        return this.renameTemplates.singleNonStandard || "{Series} ({Year}) {Type}";
      }
      return this.renameTemplates.issue || "{Series} ({Year}) #{Issue:03d}";
    },

    submitManualRename: async function () {
      if (!this.modalEntry || !this.modalEntry.path) return;
      this.actionSubmitting = true;
      this.modalError = "";
      try {
        var result = await this.requestJson(
          "/api/v1/library/browser/rename",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": this.csrfToken(),
            },
            body: JSON.stringify({
              path: this.modalEntry.path,
              proposed_name: this.renameSubmittedName(),
            }),
          }
        );
        var refreshUrl = this.refreshUrlAfterRename(
          result.source_path || this.modalEntry.path,
          result.target_path || this.renameTargetPreviewPath()
        );
        this.queueToastForNextPage("Rename completed.", "success");
        this.dispatchToast("Rename completed.", "success");
        this.closeModal();
        window.setTimeout(function () {
          window.location.href = refreshUrl;
        }, 350);
      } catch (error) {
        this.modalError = error.message || "Failed to rename item.";
      } finally {
        this.actionSubmitting = false;
      }
    },

    submitAutoRename: async function () {
      if (!this.canSubmitAutoRename()) return;
      var previewItem = this.autoRenamePreviewItem();
      if (!previewItem) return;
      this.actionSubmitting = true;
      this.modalError = "";
      try {
        var result = await this.requestJson("/api/v1/library/browser/rename", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            path: this.modalEntry.path,
            proposed_name: previewItem.proposed_name,
          }),
        });
        var refreshUrl = this.refreshUrlAfterRename(
          result.source_path || this.modalEntry.path,
          result.target_path || this.modalEntry.path
        );
        this.queueToastForNextPage("Rename completed.", "success");
        this.dispatchToast("Rename completed.", "success");
        this.closeModal();
        window.setTimeout(function () {
          window.location.href = refreshUrl;
        }, 350);
      } catch (error) {
        this.modalError = error.message || "Failed to rename item.";
      } finally {
        this.actionSubmitting = false;
      }
    },

    submitConvert: async function () {
      if (!this.canSubmitConvert()) return;
      this.actionSubmitting = true;
      this.modalError = "";
      try {
        var result = await this.requestJson("/api/v1/library/browser/convert", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            path: this.modalEntry.path,
          }),
        });
        var targetPath = result.target_path || this.modalEntry.path;
        var refreshPath = this.currentPath || parentDirectory(targetPath) || this.rootPath || "";
        this.queueToastForNextPage("Conversion completed.", "success");
        this.dispatchToast("Conversion completed.", "success");
        this.closeModal();
        window.setTimeout(function () {
          window.location.href = "/library" + (refreshPath ? "?path=" + encodeURIComponent(refreshPath) : "");
        }, 350);
      } catch (error) {
        this.modalError = error.message || "Failed to convert file.";
      } finally {
        this.actionSubmitting = false;
      }
    },

    libraryUrl: function (path) {
      var url = new URL(window.location.href);
      var params = new URLSearchParams();
      if (path) {
        params.set("path", path);
      }
      var sort = url.searchParams.get("sort");
      if (sort) {
        params.set("sort", sort);
      }
      return "/library" + (params.toString() ? "?" + params.toString() : "");
    },

    refreshUrlAfterDelete: function (targetPath) {
      if (!this.currentPath) {
        return this.libraryUrl(this.rootPath || "");
      }
      if (this.currentPath === targetPath || this.currentPath.indexOf(targetPath + "/") === 0) {
        var nextPath = parentDirectory(targetPath);
        if (this.rootPath && nextPath.indexOf(this.rootPath) !== 0) {
          nextPath = this.rootPath;
        }
        return this.libraryUrl(nextPath);
      }
      return this.libraryUrl(this.currentPath);
    },

    refreshUrlAfterRename: function (sourcePath, targetPath) {
      if (!this.currentPath) {
        return this.libraryUrl(this.rootPath || "");
      }
      if (this.currentPath === sourcePath || this.currentPath.indexOf(sourcePath + "/") === 0) {
        var suffix = this.currentPath.slice(sourcePath.length);
        return this.libraryUrl((targetPath || sourcePath) + suffix);
      }
      return this.libraryUrl(this.currentPath);
    },

    submitDelete: async function () {
      if (!this.modalEntry || !this.modalEntry.path) return;
      this.actionSubmitting = true;
      this.modalError = "";
      try {
        await this.requestJson("/api/v1/library/browser/delete", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            path: this.modalEntry.path,
            delete_files: this.deleteFiles,
            delete_folder: this.deleteFolder,
          }),
        });
        this.queueToastForNextPage("Delete completed.", "success");
        this.dispatchToast("Delete completed.", "success");
        var refreshUrl = this.refreshUrlAfterDelete(this.modalEntry.path);
        this.closeModal();
        window.setTimeout(function () {
          window.location.href = refreshUrl;
        }, 350);
      } catch (error) {
        this.modalError = error.message || "Failed to delete item.";
      } finally {
        this.actionSubmitting = false;
      }
    },
  };
}

function utilitiesIntegrityPage(config) {
  var cfg = config || {};
  return Object.assign(utilitiesSelectedFilesMixin(cfg), {
    scanDepth: "quick",
    scope: "library",
    corruptFileAction: "report",
    requeueReplacements: false,
    trashFolder: cfg.trashFolder || "",
    trashFolderBrowsePath: cfg.trashFolderBrowsePath || "",
    selectedFolder: "",
    selectedFolders: [],

    init: function () {
      this.syncFooterDock();
    },

    setScanDepth: function (depth) {
      this.scanDepth = depth === "deep" ? "deep" : "quick";
      this.validationError = "";
      this.syncFooterDock();
    },

    setScope: function (newScope) {
      this.scope = newScope;
      this.selectedFiles = [];
      this.selectedFolder = "";
      this.selectedFolders = [];
      this.validationError = "";
      this.syncFooterDock();
    },

    onSelectedFilesChanged: function () {
      this.syncFooterDock();
    },

    applySelectedFolder: function (selection) {
      var next = this.selectedFolders.slice();
      if (selection && selection.mode === "directories") {
        var directories = selection.directories || [];
        for (var i = 0; i < directories.length; i++) {
          if (!next.some(function (entry) { return entry.path === directories[i].path; })) {
            next.push({
              path: directories[i].path,
              name: directories[i].name || basename(directories[i].path),
            });
          }
        }
      } else if (selection && selection.mode === "directory" && selection.path) {
        if (!next.some(function (entry) { return entry.path === selection.path; })) {
          next.push({
            path: selection.path,
            name: selection.name || basename(selection.path),
          });
        }
      }
      this.selectedFolders = next;
      this.selectedFolder = next.length > 0 ? next[0].path : "";
      this.validationError = "";
      this.syncFooterDock();
    },

    openFilePicker: function () {
      this.openSelectedFilesBrowser({
        extensions: ".cbz,.cbr,.cb7,.cbt,.pdf",
        title: "Select Files",
        confirmLabel: "Add Files",
      });
    },

    openFolderPicker: function () {
      var startPath =
        this.selectedFolders.length > 0
          ? this.selectedFolders[this.selectedFolders.length - 1].path
          : this.selectedFolder || "/";
      this.openFileBrowser("_utilityIntegrityFolder", startPath, {
        selectionMode: "directories",
        title: "Select Folders to Scan",
        confirmLabel: "Add Folders",
        onSelectAction: "applySelectedFolder",
      });
    },

    openBrowse: function () {
      if (this.scope === "files") {
        this.openFilePicker();
        return;
      }
      if (this.scope !== "folder") return;
      this.openFolderPicker();
    },

    modeLabel: function () {
      return this.scanDepth === "deep" ? "Deep" : "Quick";
    },

    scopeLabel: function () {
      if (this.scope === "folder") return "Select folders";
      if (this.scope === "files") return "Select files";
      return "All tracked files";
    },

    selectedFilesLabel: function () {
      var count = this.selectedFiles.length;
      return count + " file" + (count === 1 ? "" : "s") + " selected";
    },

    currentScopeValidationMessage: function () {
      if (this.scope === "folder") {
        return "Select folders to scan.";
      }
      if (this.scope === "files") {
        return "Select at least one file to scan.";
      }
      return "";
    },

    runPreview: function () {
      if (!this.canStart()) {
        this.validationError = this.currentScopeValidationMessage();
        return;
      }
      this.validationError = "";
      var message;
      if (this.scope === "library") {
        message = this.modeLabel() + " scan is ready for all tracked files.";
      } else if (this.scope === "folder") {
        message = this.modeLabel() + " scan is ready for the selected folders.";
      } else {
        message =
          this.modeLabel() +
          " scan is ready for " +
          this.selectedFiles.length +
          " selected file" +
          (this.selectedFiles.length === 1 ? "" : "s") +
          ".";
      }
      showToast({ message: message, level: "info" });
      this.syncFooterDock();
    },

    syncFooterDock: function () {
      window.dispatchEvent(
        new CustomEvent("utilities:integrity-footer", {
          detail: {
            mode: this.modeLabel(),
            scope: this.scopeLabel(),
          },
        })
      );
    },

    canStart: function () {
      if (this.scope === "library") return true;
      if (this.scope === "folder") return this.selectedFolders.length > 0 || !!this.selectedFolder;
      return this.selectedFiles.length > 0;
    },

    startIntegrityCheck: async function () {
      if (!this.canStart()) {
        this.validationError = this.currentScopeValidationMessage();
        return;
      }

      var jobConfig = {
        scan_depth: this.scanDepth,
        scope: this.scope === "files" ? "manual" : this.scope,
        corrupt_action: this.corruptFileAction,
        requeue_search: this.requeueReplacements,
      };

      if (this.trashFolder) {
        jobConfig.trash_folder = this.trashFolder;
      }

      if (this.scope === "files") {
        jobConfig.file_paths = this.getSelectedFilePaths();
      } else if (this.scope === "folder") {
        var folderPaths = this.selectedFolders.length > 0
          ? this.selectedFolders.map(function (entry) { return entry.path; })
          : (this.selectedFolder ? [this.selectedFolder] : []);
        if (folderPaths.length === 1) {
          jobConfig.scan_folder = folderPaths[0];
        } else if (folderPaths.length > 1) {
          jobConfig.scan_folders = folderPaths;
        }
      }

      this.submitting = true;
      this.validationError = "";
      try {
        var response = await fetch("/api/v1/utilities/jobs", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            job_type: "integrity_check",
            display_name: "Integrity Check — " + (this.scanDepth === "deep" ? "Deep Scan" : "Quick Scan"),
            config: jobConfig,
          }),
        });
        if (!response.ok) {
          var error = await response.json();
          this.validationError = (error.error && error.error.message) || "Failed to start integrity check.";
          return;
        }
        showToast({ message: "Integrity check queued.", level: "success" });
        window.location.href = "/utilities?tab=queue";
      } catch (_) {
        this.validationError = "Failed to start integrity check.";
      } finally {
        this.submitting = false;
      }
    },
  });
}

function utilitiesExportPage(config) {
  var cfg = config || {};
  return Object.assign(fileBrowserMixin(cfg), {
    format: cfg.format === "csv" ? "csv" : "json",
    pretty: cfg.pretty !== false,
    exportFolder: cfg.exportFolder || cfg.exportFolderBrowsePath || "",
    exportFolderBrowsePath: cfg.exportFolderBrowsePath || "",
    fieldGroups: cfg.fieldGroups || [],
    selectedFields: (cfg.defaultFields || []).slice(),
    multiValueFields: [],
    exportRecordCounts: cfg.exportRecordCounts || {},
    validationError: "",
    submitting: false,

    init: function () {
      var self = this;
      if (typeof this.$watch === "function") {
        this.$watch("format", function () {
          self.validationError = "";
          self.syncFooterDock();
        });
        this.$watch("pretty", function () {
          self.syncFooterDock();
        });
        this.$watch("selectedFields", function () {
          self.validationError = "";
          self.syncFooterDock();
        });
        this.$watch("exportFolder", function () {
          self.syncFooterDock();
        });
      }
      this.syncFooterDock();
    },

    setFormat: function (value) {
      this.format = value === "json" ? "json" : "csv";
      if (this.format === "json" && this.pretty !== true) {
        this.pretty = true;
      }
      this.validationError = "";
    },

    isFieldSelected: function (fieldKey) {
      return this.selectedFields.indexOf(fieldKey) >= 0;
    },

    toggleField: function (fieldKey) {
      if (this.isFieldSelected(fieldKey)) {
        this.selectedFields = this.selectedFields.filter(function (value) { return value !== fieldKey; });
        this.multiValueFields = this.multiValueFields.filter(function (value) { return value !== fieldKey; });
      } else {
        this.selectedFields = this.selectedFields.concat([fieldKey]);
      }
      this.validationError = "";
    },

    selectAllFields: function () {
      var all = [];
      for (var i = 0; i < this.fieldGroups.length; i++) {
        var fields = this.fieldGroups[i].fields || [];
        for (var j = 0; j < fields.length; j++) {
          all.push(fields[j].key);
        }
      }
      this.selectedFields = all;
      this.validationError = "";
    },

    clearAllFields: function () {
      this.selectedFields = [];
      this.multiValueFields = [];
      this.validationError = "";
    },

    selectedFieldOptions: function () {
      var selected = [];
      for (var i = 0; i < this.fieldGroups.length; i++) {
        var fields = this.fieldGroups[i].fields || [];
        for (var j = 0; j < fields.length; j++) {
          if (this.isFieldSelected(fields[j].key)) {
            selected.push(fields[j]);
          }
        }
      }
      return selected;
    },

    toggleMultiValueField: function (fieldKey) {
      if (this.multiValueFields.indexOf(fieldKey) >= 0) {
        this.multiValueFields = this.multiValueFields.filter(function (value) { return value !== fieldKey; });
      } else {
        this.multiValueFields = this.multiValueFields.concat([fieldKey]);
      }
    },

    selectAllMultiValueFields: function () {
      this.multiValueFields = this.selectedFieldOptions().map(function (field) { return field.key; });
    },

    clearAllMultiValueFields: function () {
      this.multiValueFields = [];
    },

    totalFieldCount: function () {
      var total = 0;
      for (var i = 0; i < this.fieldGroups.length; i++) {
        total += (this.fieldGroups[i].fields || []).length;
      }
      return total;
    },

    selectedFieldSummaryLabel: function () {
      return this.selectedFields.length + " of " + this.totalFieldCount();
    },

    formatLabel: function () {
      if (this.format === "json") {
        return this.pretty ? "JSON (pretty)" : "JSON";
      }
      return "CSV";
    },

    totalCountFor: function (kind) {
      var counts = this.exportRecordCounts || {};
      var bucket = counts[kind] || {};
      return bucket.all || 0;
    },

    estimatedRecordCount: function () {
      var needsFiles = this.selectedFields.some(function (field) {
        return field.indexOf("file_") === 0;
      });
      if (needsFiles) {
        return this.totalCountFor("file");
      }

      var needsIssues = this.selectedFields.some(function (field) {
        return (
          field.indexOf("issue_") === 0 ||
          field === "release_date" ||
          field === "store_date" ||
          field === "page_count"
        );
      });
      if (needsIssues) {
        return this.totalCountFor("issue");
      }
      return this.totalCountFor("series");
    },

    estimatedRecordCountLabel: function () {
      return new Intl.NumberFormat().format(this.estimatedRecordCount());
    },

    syncFooterDock: function () {
      window.dispatchEvent(
        new CustomEvent("utilities:export-footer", {
          detail: {
            format: this.formatLabel(),
            fields: String(this.selectedFields.length),
            dest: this.exportFolder || this.exportFolderBrowsePath || "",
          },
        })
      );
    },

    browseExportFolder: function () {
      this.openFileBrowser("exportFolder", this.exportFolder, {
        selectionMode: "directory",
        startPath: this.exportFolderBrowsePath,
        title: "Select Export Folder",
        confirmLabel: "Use Folder",
      });
    },

    startExport: async function () {
      if (this.selectedFields.length === 0) {
        this.validationError = "Choose at least one field to export.";
        return;
      }

      this.submitting = true;
      this.validationError = "";
      try {
        var response = await fetch("/api/v1/utilities/jobs", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            job_type: "export_library",
            display_name: "Export Library — " + this.format.toUpperCase(),
            config: {
              format: this.format,
              fields: this.selectedFields,
              export_folder: this.exportFolder || undefined,
              pretty: this.format === "json" ? this.pretty : undefined,
              multi_value_fields: this.format === "json" ? this.multiValueFields : [],
            },
          }),
        });
        if (!response.ok) {
          var error = await response.json();
          this.validationError = (error.error && error.error.message) || "Failed to queue the export.";
          return;
        }
        showToast({ message: "Export job queued.", level: "success" });
        window.location.href = "/utilities?tab=queue";
      } catch (_) {
        this.validationError = "Failed to queue the export.";
      } finally {
        this.submitting = false;
      }
    },
  });
}

function utilitiesDbCheckPage(config) {
  var cfg = config || {};
  return Object.assign(fileBrowserMixin(cfg), {
    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },

    checks: {
      orphans: !cfg.defaultOptimize,
      stale: !cfg.defaultOptimize,
      referential: false,
      reindex: false,
      optimize: !!cfg.defaultOptimize,
    },
    libraryRoot: cfg.defaultLibraryRoot || "",
    findings: [],
    previewLoaded: false,
    previewLoading: false,
    validationError: "",
    optimizationResult: "",
    submitting: false,

    init: function () {
      var self = this;
      if (typeof this.$watch === "function") {
        this.$watch("checks.orphans", function () {
          self.validationError = "";
          self.syncFooterDock();
        });
        this.$watch("checks.stale", function () {
          self.validationError = "";
          self.syncFooterDock();
        });
        this.$watch("checks.referential", function () {
          self.validationError = "";
          self.syncFooterDock();
        });
        this.$watch("checks.reindex", function () {
          self.validationError = "";
          self.syncFooterDock();
        });
        this.$watch("checks.optimize", function () {
          self.validationError = "";
          self.optimizationResult = "";
          self.syncFooterDock();
        });
        this.$watch("libraryRoot", function () {
          self.validationError = "";
          self.syncFooterDock();
        });
      }
      this.syncFooterDock();
    },

    selectedChecks: function () {
      return Object.keys(this.checks).filter(
        function (key) { return !!this.checks[key]; }.bind(this)
      );
    },

    selectedChecksLabel: function () {
      return this.selectedChecks().length + " of 5";
    },

    syncFooterDock: function () {
      var root = this.libraryRoot && this.libraryRoot.trim ? this.libraryRoot.trim() : "";
      window.dispatchEvent(
        new CustomEvent("utilities:db-check-footer", {
          detail: {
            checks: this.selectedChecksLabel(),
            root: root || "—",
          },
        })
      );
    },

    browseLibraryRoot: function () {
      this.openFileBrowser("libraryRoot", this.libraryRoot, {
        selectionMode: "directory",
        title: "Select Library Root",
        confirmLabel: "Use Folder",
      });
    },

    formatActionLabel: function (action) {
      if (action === "delete") return "Delete";
      if (action === "add") return "Add";
      if (action === "repair") return "Repair";
      if (action === "reindex") return "Reindex";
      if (action === "optimize") return "Optimize";
      return "Skip";
    },

    formatCheckLabel: function (checkType) {
      if (checkType === "orphans") return "Orphans";
      if (checkType === "stale") return "Untracked";
      if (checkType === "referential") return "Consistency";
      if (checkType === "reindex") return "Metadata";
      if (checkType === "optimize") return "Optimize";
      return checkType;
    },

    hydrateFindings: function (rawFindings) {
      var self = this;
      return (rawFindings || []).map(function (finding) {
        return Object.assign({}, finding, {
          current_action: finding.suggested_action,
          checkTypeLabel: self.formatCheckLabel(finding.check_type),
        });
      });
    },

    runPreview: async function () {
      var checks = this.selectedChecks();
      if (checks.length === 0) {
        this.validationError = "Choose at least one DB check before running preview.";
        return;
      }
      if (
        (checks.indexOf("stale") >= 0 || checks.indexOf("reindex") >= 0) &&
        !this.libraryRoot.trim()
      ) {
        this.validationError =
          "Choose a library root when untracked-file or metadata refresh checks are enabled.";
        return;
      }

      this.previewLoading = true;
      this.validationError = "";
      try {
        var response = await fetch("/api/v1/utilities/db-check/preview", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            checks: checks,
            library_root: this.libraryRoot.trim() || undefined,
          }),
        });
        if (!response.ok) {
          var error = await response.json();
          this.validationError = (error.error && error.error.message) || "Preview failed.";
          this.previewLoaded = false;
          return;
        }
        var data = await response.json();
        this.findings = this.hydrateFindings(data.findings);
        this.previewLoaded = true;
        this.syncFooterDock();
      } catch (_) {
        this.validationError = "Failed to run DB check preview.";
        this.previewLoaded = false;
      } finally {
        this.previewLoading = false;
      }
    },

    setFindingAction: function (findingId, action) {
      this.findings = this.findings.map(function (finding) {
        if (finding.finding_id === findingId) {
          return Object.assign({}, finding, { current_action: action });
        }
        return finding;
      });
    },

    canApplyFixes: function () {
      return this.previewLoaded && this.findings.some(function (finding) {
        return finding.current_action && finding.current_action !== "skip";
      });
    },

    applyFixes: async function () {
      if (!this.canApplyFixes()) {
        this.validationError = "Run preview and choose at least one action before applying fixes.";
        return;
      }

      this.submitting = true;
      this.validationError = "";
      try {
        var optimizeFindings = this.findings.filter(function (finding) {
          return finding.current_action === "optimize";
        });
        var otherActions = this.findings.filter(function (finding) {
          return finding.current_action && finding.current_action !== "skip" && finding.current_action !== "optimize";
        });
        if (optimizeFindings.length > 0) {
          if (otherActions.length > 0) {
            this.validationError = "Run database optimization separately from record cleanup actions.";
            return;
          }
          var optimizeContext = optimizeFindings[0].context || {};
          var reclaimableMb = Number(optimizeContext.reclaimable_bytes || 0) / (1024 * 1024);
          var confirmed = await pbConfirm({
            title: "Optimize Database Storage",
            message:
              "Pullbox will briefly pause database activity, checkpoint its write-ahead log, and compact unused pages. " +
              "About " + reclaimableMb.toFixed(1) + " MB is currently reclaimable. This cannot be cancelled once started.",
            confirmText: "Optimize Database",
          });
          if (!confirmed) {
            return;
          }
          var optimizeResponse = await fetch("/api/v1/health/database/optimize", {
            method: "POST",
            headers: { "X-CSRF-Token": this.csrfToken() },
          });
          if (!optimizeResponse.ok) {
            var optimizeError = await optimizeResponse.json();
            this.validationError = (optimizeError.error && optimizeError.error.message) || "Database optimization failed.";
            return;
          }
          var optimizeData = await optimizeResponse.json();
          var reclaimedBytes = Number(optimizeData.reclaimed_bytes || 0);
          this.optimizationResult =
            "Database optimization completed. Reclaimed " +
            (reclaimedBytes / (1024 * 1024)).toFixed(1) +
            " MB and verified database integrity.";
          this.findings = [];
          this.previewLoaded = false;
          return;
        }
        var response = await fetch("/api/v1/utilities/jobs", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            job_type: "db_check_cleanup",
            display_name: "DB Check — Execute",
            config: {
              checks: this.selectedChecks(),
              mode: "execute",
              library_root: this.libraryRoot.trim() || undefined,
              actions: this.findings.map(function (finding) {
                return {
                  operation: finding.current_action,
                  record_id: finding.record_id,
                  record_type: finding.record_type,
                  file_path: finding.file_path,
                  description: finding.description,
                  context: finding.context || {},
                };
              }),
            },
          }),
        });
        if (!response.ok) {
          var error = await response.json();
          this.validationError = (error.error && error.error.message) || "Failed to queue DB cleanup.";
          return;
        }
        this.findings = [];
        this.previewLoaded = false;
        showToast({ message: "DB cleanup job queued.", level: "success" });
        window.location.href = "/utilities?tab=queue";
      } catch (_) {
        this.validationError = "Failed to queue DB cleanup.";
      } finally {
        this.submitting = false;
      }
    },
  });
}

function utilitiesPermissionsPage(config) {
  var cfg = config || {};
  return Object.assign(utilitiesSelectedFilesMixin(cfg), {
    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },
    libraryRoots: cfg.libraryRoots || [],
    runMode: "dry_run",
    scope: "folder",
    selectedFolder: "",
    selectedFolders: [],
    folderMode: "755",
    fileMode: "644",
    includeFolders: true,
    includeFiles: true,
    confirmApply: false,
    validationError: "",
    submitting: false,
    preview: null,
    previewLoaded: false,
    previewLoading: false,
    _previewRequestSeq: 0,
    _previewSyncTimer: 0,

    initPermissionsPage: function () {
      this.syncFooterDock();
      this.schedulePreview({ delay: 0, preserveVisible: false });
    },

    setScope: function (value) {
      if (value === "folder" || value === "files") {
        this.scope = value;
      } else {
        this.scope = "library";
      }
      this.selectedFiles = [];
      this.selectedFolder = "";
      this.selectedFolders = [];
      this.validationError = "";
      this.syncFooterDock();
      this.schedulePreview({ delay: 0, preserveVisible: false });
    },

    onSelectedFilesChanged: function () {
      this.syncFooterDock();
      this.schedulePreview();
    },

    applySelectedFolders: function (selection) {
      var next = this.selectedFolders.slice();
      if (selection && selection.mode === "directories") {
        var directories = selection.directories || [];
        for (var i = 0; i < directories.length; i++) {
          if (!next.some(function (entry) { return entry.path === directories[i].path; })) {
            next.push({
              path: directories[i].path,
              name: directories[i].name || basename(directories[i].path),
            });
          }
        }
      } else if (selection && selection.mode === "directory" && selection.path) {
        if (!next.some(function (entry) { return entry.path === selection.path; })) {
          next.push({
            path: selection.path,
            name: selection.name || basename(selection.path),
          });
        }
      }
      this.selectedFolders = next;
      this.selectedFolder = next.length > 0 ? next[0].path : "";
      this.validationError = "";
      this.syncFooterDock();
      this.schedulePreview();
    },

    openFolderPicker: function () {
      var startPath =
        this.selectedFolders.length > 0
          ? this.selectedFolders[this.selectedFolders.length - 1].path
          : this.selectedFolder || "/";
      this.openFileBrowser("_utilityPermissionsFolder", startPath, {
        selectionMode: "directories",
        title: "Select Folders",
        confirmLabel: "Add Folders",
        onSelectAction: "applySelectedFolders",
      });
    },

    openFilePicker: function () {
      this.openSelectedFilesBrowser({
        title: "Select Files",
        confirmLabel: "Add Files",
      });
    },

    runModeLabel: function () {
      return this.runMode === "apply" ? "Apply" : "Dry-run";
    },

    scopeLabel: function () {
      if (this.scope === "folder") {
        return "Select folders";
      }
      if (this.scope === "files") {
        return "Select files";
      }
      return "Entire library";
    },

    targetsLabel: function () {
      if (this.includeFolders && this.includeFiles) {
        return "folders + files";
      }
      if (this.includeFolders) {
        return "folders";
      }
      if (this.includeFiles) {
        return "files";
      }
      return "none selected";
    },

    selectedScopePaths: function () {
      if (this.scope === "folder") {
        return this.selectedFolders.map(function (entry) { return entry.path; });
      }
      if (this.scope === "files") {
        return this.getSelectedFilePaths();
      }
      return [];
    },

    canRunPreview: function () {
      if (this.scope === "folder" || this.scope === "files") {
        return this.selectedScopePaths().length > 0;
      }
      return true;
    },

    permissionsErrorMessage: function (message) {
      if (message === "At least one of include_files or include_folders must be true") {
        return "Choose files, folders, or both.";
      }
      return message || "Preview failed.";
    },

    clearPreviewState: function () {
      if (this._previewSyncTimer) {
        window.clearTimeout(this._previewSyncTimer);
        this._previewSyncTimer = 0;
      }
      this._previewRequestSeq += 1;
      this.previewLoading = false;
      this.preview = null;
      this.previewLoaded = false;
      this.syncFooterDock();
    },

    schedulePreview: function (options) {
      var opts = options || {};
      var preserveVisible = opts.preserveVisible !== false;
      var delay = typeof opts.delay === "number" ? opts.delay : 120;

      if (this._previewSyncTimer) {
        window.clearTimeout(this._previewSyncTimer);
        this._previewSyncTimer = 0;
      }

      if (!this.canRunPreview()) {
        this.clearPreviewState();
        return;
      }

      var self = this;
      var run = function () {
        self._previewSyncTimer = 0;
        self.refreshPreview({ preserveVisible: preserveVisible });
      };

      if (delay <= 0) {
        run();
        return;
      }
      this._previewSyncTimer = window.setTimeout(run, delay);
    },

    refreshPreview: async function (options) {
      var opts = options || {};
      var preserveVisible = !!opts.preserveVisible;
      if (!this.canRunPreview()) {
        this.clearPreviewState();
        return;
      }
      if (!this.includeFolders && !this.includeFiles) {
        this.validationError = "Choose files, folders, or both.";
        if (!preserveVisible) {
          this.preview = null;
          this.previewLoaded = false;
        }
        this.previewLoading = false;
        this.syncFooterDock();
        return;
      }

      var requestSeq = ++this._previewRequestSeq;
      this.previewLoading = true;
      this.validationError = "";
      try {
        var response = await fetch("/api/v1/utilities/permissions/preview", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            scope: this.scope,
            file_paths: this.selectedScopePaths(),
            folder_mode: String(this.folderMode || "").trim(),
            file_mode: String(this.fileMode || "").trim(),
            include_folders: !!this.includeFolders,
            include_files: !!this.includeFiles,
          }),
        });
        if (requestSeq !== this._previewRequestSeq) {
          return;
        }
        if (!response.ok) {
          var error = await response.json();
          this.validationError = this.permissionsErrorMessage(
            error.error && error.error.message
          );
          if (!preserveVisible) {
            this.preview = null;
            this.previewLoaded = false;
          }
          this.syncFooterDock();
          return;
        }
        this.preview = await response.json();
        this.previewLoaded = true;
        this.syncFooterDock();
      } catch (_) {
        if (requestSeq !== this._previewRequestSeq) {
          return;
        }
        this.validationError = "Failed to load permissions preview.";
        if (!preserveVisible) {
          this.preview = null;
          this.previewLoaded = false;
        }
        this.syncFooterDock();
      } finally {
        if (requestSeq === this._previewRequestSeq) {
          this.previewLoading = false;
        }
      }
    },

    previewTargetLabel: function (type) {
      if (!this.includeFolders && !this.includeFiles) {
        return "none selected";
      }
      if (type === "File") {
        return this.fileMode;
      }
      if (this.includeFolders && this.includeFiles) {
        return this.folderMode + " / " + this.fileMode;
      }
      return this.includeFolders ? this.folderMode : this.fileMode;
    },

    previewRows: function () {
      if (!this.previewLoaded || !this.preview || !Array.isArray(this.preview.items)) {
        return [];
      }
      return this.preview.items.map(function (item) {
        return {
          name: item.name || basename(item.file_path),
          path: item.file_path,
          type: item.item_type,
          target: item.target_mode,
        };
      });
    },

    previewCount: function () {
      return this.previewLoaded && this.preview ? this.preview.item_count || 0 : 0;
    },

    folderPreviewCount: function () {
      return this.previewLoaded && this.preview ? this.preview.folder_count || 0 : 0;
    },

    filePreviewCount: function () {
      return this.previewLoaded && this.preview ? this.preview.file_count || 0 : 0;
    },

    previewEmptyMessage: function () {
      if (this.previewLoading) {
        return "Building the latest permissions preview…";
      }
      if (this.scope === "folder") {
        return "Browse for folders to preview the permission scope.";
      }
      if (this.scope === "files") {
        return "Browse for files to preview the permission scope.";
      }
      return "No enabled library roots are available.";
    },

    canStart: function () {
      if (this.scope === "folder" || this.scope === "files") {
        return this.selectedScopePaths().length > 0;
      }
      return true;
    },

    syncFooterDock: function () {
      window.dispatchEvent(
        new CustomEvent("utilities:permissions-footer", {
          detail: {
            mode: this.runModeLabel(),
            scope: this.scopeLabel(),
            targets: this.targetsLabel(),
          },
        })
      );
    },

    _modeLooksValid: function (value) {
      return /^0?[0-7]{3}$/.test(String(value || "").trim());
    },

    validate: function () {
      if (!this.includeFolders && !this.includeFiles) {
        return "Choose files, folders, or both.";
      }
      if (this.includeFolders && !this._modeLooksValid(this.folderMode)) {
        return "Folder mode must be a three-digit octal chmod value.";
      }
      if (this.includeFiles && !this._modeLooksValid(this.fileMode)) {
        return "File mode must be a three-digit octal chmod value.";
      }
      if (this.scope === "files" && !this.includeFiles) {
        return "Include files must be enabled when selected files are the scope.";
      }
      if (this.scope === "folder" && this.selectedFolders.length === 0) {
        return "Choose at least one folder.";
      }
      if (this.scope === "files" && this.selectedFiles.length === 0) {
        return "Choose at least one file.";
      }
      if (this.runMode === "apply" && !this.confirmApply) {
        return "Confirm that the dry-run output was reviewed before applying changes.";
      }
      return "";
    },

    buildJobConfig: function () {
      var jobConfig = {
        scope: this.scope,
        run_mode: this.runMode,
        folder_mode: String(this.folderMode || "").trim(),
        file_mode: String(this.fileMode || "").trim(),
        include_folders: !!this.includeFolders,
        include_files: !!this.includeFiles,
      };
      if (this.scope === "folder" || this.scope === "files") {
        jobConfig.scope = "paths";
        jobConfig.file_paths = this.selectedScopePaths();
      }
      if (this.runMode === "apply") {
        jobConfig.confirm_apply = true;
      }
      return jobConfig;
    },

    startPermissionsJob: async function () {
      var validation = this.validate();
      if (validation) {
        this.validationError = validation;
        return;
      }

      this.submitting = true;
      this.validationError = "";
      try {
        var response = await fetch("/api/v1/utilities/jobs", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            job_type: "library_permissions",
            display_name: this.runMode === "apply"
              ? "Library Permissions — Apply"
              : "Library Permissions — Dry-run",
            config: this.buildJobConfig(),
          }),
        });
        if (!response.ok) {
          var error = await response.json();
          this.validationError = this.permissionsErrorMessage(
            (error.error && error.error.message) || "Failed to queue permissions job."
          );
          return;
        }
        showToast({ message: "Library permissions job queued.", level: "success" });
        window.location.href = "/utilities?tab=queue";
      } catch (_) {
        this.validationError = "Failed to queue permissions job.";
      } finally {
        this.submitting = false;
      }
    },
  });
}

function loginPage() {
  return {
    username: "",
    password: "",
    showPassword: false,
    submitting: false,
    error: "",
    theme: "dark",

    init: function () {
      this.theme = getTheme();
    },

    clearError: function () {
      this.error = "";
    },

    toggleTheme: function () {
      this.theme = this.theme === "dark" ? "light" : "dark";
      applyTheme(this.theme);
    },

    submit: async function () {
      if (this.submitting) return;

      this.error = "";
      this.submitting = true;

      try {
        var response = await fetch("/api/v1/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            username: this.username,
            password: this.password,
          }),
        });

        if (!response.ok) {
          var message = "Invalid credentials.";
          try {
            var data = await response.json();
            message =
              (data.error && data.error.message) ||
              data.detail ||
              message;
          } catch (_) {
            // Use generic message when the body is unavailable.
          }
          this.error = message;
          return;
        }

        window.location.href = "/";
      } catch (_) {
        this.error = "Network error. Please try again.";
      } finally {
        this.submitting = false;
      }
    },
  };
}

function appShell() {
  return {
    sidebarOpen: localStorage.getItem("sidebarOpen") !== "false",
    sidebarMobileOpen: false,
    desktop: window.innerWidth >= 1024,
    resizeHandler: null,
    usageStatsConsent: "unknown",
    usageStatsPromptOpen: false,
    usageStatsLoaded: false,
    usageStatsSaving: false,
    donationsOpen: false,
    activityOperations: [],
    activityOpen: false,
    activityActiveCount: 0,
    activitySpinnerCount: 0,
    activityAttentionCount: 0,
    activitySource: null,
    activityPollTimer: null,
    activityRefreshing: false,

    get collapsed() {
      return !this.sidebarOpen && !this.sidebarMobileOpen;
    },

    get showSidebarLabels() {
      return !this.collapsed;
    },

    init: function () {
      var self = this;
      self.resizeHandler = function () {
        self.desktop = window.innerWidth >= 1024;
        if (!self.desktop) {
          self.sidebarMobileOpen = false;
          document.documentElement.style.removeProperty("--sidebar-w");
          return;
        }
        document.documentElement.style.setProperty("--sidebar-w", self.sidebarWidth());
      };

      self.resizeHandler();
      window.addEventListener("resize", self.resizeHandler);

      var preloadStyle = document.getElementById("sidebar-preload");
      if (preloadStyle) {
        preloadStyle.remove();
      }

      window.__pullboxUpdateUsageStatsPreference = function (payload) {
        self.applyUsageStatsPreference(payload);
      };

      self.bootstrapUsageStatsPrompt();
      self.bootstrapActivity();
    },

    destroy: function () {
      if (this.resizeHandler) {
        window.removeEventListener("resize", this.resizeHandler);
      }
      if (window.__pullboxUpdateUsageStatsPreference) {
        delete window.__pullboxUpdateUsageStatsPreference;
      }
      this.disconnectActivityStream();
      this.clearActivityTimer();
    },

    clearActivityTimer: function () {
      if (this.activityPollTimer) {
        window.clearTimeout(this.activityPollTimer);
        this.activityPollTimer = null;
      }
    },

    scheduleActivityPoll: function (delayMs) {
      var self = this;
      self.clearActivityTimer();
      self.activityPollTimer = window.setTimeout(function () {
        self.activityPollTimer = null;
        self.refreshActivity();
      }, delayMs || 3000);
    },

    bootstrapActivity: function () {
      this.connectActivityStream();
      this.refreshActivity();
    },

    refreshActivity: function () {
      var self = this;
      if (self.activityRefreshing) {
        return;
      }
      self.activityRefreshing = true;
      fetch("/api/v1/activity", {
        headers: { Accept: "application/json" },
      })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("Failed to load background activity.");
          }
          return response.json();
        })
        .then(function (payload) {
          self.activityOperations =
            payload && Array.isArray(payload.operations) ? payload.operations : [];
          self.activityActiveCount = Math.max(
            0,
            Number(payload && payload.active_count) || 0,
          );
          self.activitySpinnerCount = Math.max(
            0,
            Number(payload && payload.spinner_count) || 0,
          );
          self.activityAttentionCount = Math.max(
            0,
            Number(payload && payload.attention_count) || 0,
          );
        })
        .catch(function () {
          // Activity is supplemental; a transient failure must not interrupt the page.
        })
        .finally(function () {
          self.activityRefreshing = false;
          self.scheduleActivityPoll(3000);
          if (!self.activitySource) {
            self.connectActivityStream();
          }
        });
    },

    connectActivityStream: function () {
      var self = this;
      if (self.activitySource) {
        return;
      }
      var source = new EventSource("/api/v1/activity/stream");
      self.activitySource = source;
      var refreshFromEvent = function () {
        if (self.activitySource === source) {
          self.refreshActivity();
        }
      };
      source.addEventListener("ready", refreshFromEvent);
      source.addEventListener("progress", refreshFromEvent);
      source.onmessage = refreshFromEvent;
      source.onerror = function () {
        if (self.activitySource !== source) {
          return;
        }
        self.disconnectActivityStream();
      };
    },

    disconnectActivityStream: function () {
      if (!this.activitySource) {
        return;
      }
      try {
        this.activitySource.close();
      } catch (_) {
        // Closing a stale activity stream is best-effort.
      }
      this.activitySource = null;
    },

    acknowledgeActivity: function (operationId) {
      var self = this;
      fetch("/api/v1/activity/" + operationId + "/acknowledge", {
        method: "POST",
        headers: {
          Accept: "application/json",
          "X-CSRF-Token": readCsrfTokenFromBody(),
        },
      })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("Failed to dismiss activity.");
          }
          return response.json();
        })
        .then(function () {
          self.refreshActivity();
        })
        .catch(function () {
          if (typeof showToast === "function") {
            showToast({
              message: "Unable to dismiss this activity right now.",
              level: "error",
            });
          }
        });
    },

    activityBadgeCount: function () {
      return this.activityAttentionCount > 0
        ? this.activityAttentionCount
        : this.activityActiveCount;
    },

    activityHasRecentSuccess: function () {
      return this.activityOperations.some(function (operation) {
        return operation && operation.state === "completed";
      });
    },

    activityButtonLabel: function () {
      if (this.activityAttentionCount > 0) {
        return this.activityAttentionCount === 1
          ? "1 activity needs attention"
          : this.activityAttentionCount + " activities need attention";
      }
      if (this.activityActiveCount > 0) {
        return this.activityActiveCount === 1
          ? "1 background operation active"
          : this.activityActiveCount + " background operations active";
      }
      if (this.activityHasRecentSuccess()) {
        return "Background work completed";
      }
      return "Background activity";
    },

    activitySummaryLabel: function () {
      if (this.activityAttentionCount > 0) {
        return this.activityAttentionCount + " need attention";
      }
      if (this.activityActiveCount > 0) {
        return this.activityActiveCount + " active";
      }
      if (this.activityOperations.length > 0) {
        return "Recently completed";
      }
      return "Idle";
    },

    activitySourceLabel: function (operation) {
      if (operation && operation.source_label) {
        return operation.source_label;
      }
      var labels = {
        import: "Import",
        download: "Download",
        post_processing: "Post-processing",
        issue_import: "Manual import",
        orphan_recovery: "Import recovery",
        utility: "Utility",
      };
      return labels[operation && operation.operation_type] || "Background work";
    },

    activityStateLabel: function (operation) {
      var labels = {
        queued: "Queued",
        running: "Working",
        paused: "Paused",
        retrying: "Retrying",
        completed: "Complete",
        failed: "Failed",
        cancelled: "Cancelled",
      };
      return labels[operation && operation.state] || "Working";
    },

    activityOperationClass: function (operation) {
      if (operation && operation.tone === "danger") {
        return "border-pb-error/40";
      }
      if (operation && operation.tone === "warning") {
        return "border-pb-warning/40";
      }
      if (operation && operation.tone === "success") {
        return "border-pb-success/40";
      }
      return "border-pb-border";
    },

    activityToneTextClass: function (operation) {
      if (operation && operation.tone === "danger") {
        return "text-pb-error";
      }
      if (operation && operation.tone === "warning") {
        return "text-pb-warning";
      }
      if (operation && operation.tone === "success") {
        return "text-pb-success";
      }
      return "text-pb-interactive";
    },

    activityButtonClass: function () {
      if (this.activityAttentionCount > 0) {
        return "bg-pb-error/15 text-pb-error hover:bg-pb-error/25";
      }
      if (this.activitySpinnerCount > 0) {
        return "bg-pb-interactive/15 text-pb-interactive hover:bg-pb-interactive/25";
      }
      if (this.activityHasRecentSuccess()) {
        return "bg-pb-success/15 text-pb-success hover:bg-pb-success/25";
      }
      return "text-pb-text-sec hover:bg-pb-card-hover hover:text-pb-text";
    },

    activityOverallIndeterminate: function (operation) {
      return !operation || !operation.overall || operation.overall.indeterminate === true;
    },

    activityItemIndeterminate: function (operation) {
      return (
        !operation ||
        !operation.item ||
        operation.item.indeterminate === true
      );
    },

    activityOverallPercent: function (operation) {
      var value = Number(operation && operation.overall && operation.overall.percent);
      return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
    },

    activityItemPercent: function (operation) {
      var value = Number(operation && operation.item && operation.item.percent);
      return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
    },

    activityFormatMeasure: function (measure) {
      if (!measure) {
        return "";
      }
      var current = Number(measure.current);
      var total = Number(measure.total);
      var hasCurrent = measure.current != null && Number.isFinite(current);
      var hasTotal = measure.total != null && Number.isFinite(total) && total > 0;
      if (hasCurrent && hasTotal) {
        if (
          measure.unit === "bytes" &&
          window._pb &&
          typeof window._pb.formatBytes === "function"
        ) {
          return window._pb.formatBytes(current) + " / " + window._pb.formatBytes(total);
        }
        return Math.round(current) + " / " + Math.round(total) + (measure.unit ? " " + measure.unit : "");
      }
      var percent = Number(measure.percent);
      if (measure.percent != null && Number.isFinite(percent)) {
        return Math.round(percent) + "%";
      }
      if (hasCurrent) {
        return Math.round(current) + (measure.unit ? " " + measure.unit : "");
      }
      return measure.indeterminate ? "Working" : "";
    },

    activityOverallLabel: function (operation) {
      return this.activityFormatMeasure(operation && operation.overall);
    },

    activityItemLabel: function (operation) {
      return this.activityFormatMeasure(operation && operation.item);
    },

    activityItemPhaseLabel: function (operation) {
      var phase = String(
        (operation && operation.item && operation.item.phase) ||
        (operation && operation.phase) ||
        "Current item",
      );
      return phase
        .replace(/_/g, " ")
        .replace(/\b\w/g, function (letter) {
          return letter.toUpperCase();
        });
    },

    activityEtaLabel: function (seconds) {
      if (seconds == null || seconds === "") {
        return "";
      }
      var value = Number(seconds);
      if (!Number.isFinite(value) || value <= 0) {
        return "";
      }
      if (value < 60) {
        return Math.max(1, Math.round(value)) + " sec remaining";
      }
      if (value < 3600) {
        return Math.max(1, Math.round(value / 60)) + " min remaining";
      }
      var hours = Math.floor(value / 3600);
      var minutes = Math.round((value % 3600) / 60);
      return hours + " hr" + (minutes ? " " + minutes + " min" : "") + " remaining";
    },

    activityRateEtaLabel: function (operation) {
      if (!operation) {
        return "";
      }
      var parts = [];
      var rate = Number(operation.rate);
      if (operation.rate != null && Number.isFinite(rate) && rate >= 0) {
        if (
          operation.rate_unit === "bytes_per_second" &&
          window._pb &&
          typeof window._pb.formatBytes === "function"
        ) {
          parts.push(window._pb.formatBytes(rate) + "/s");
        } else {
          parts.push(Math.round(rate) + (operation.rate_unit ? " " + operation.rate_unit : ""));
        }
      }
      var eta = this.activityEtaLabel(operation.eta_seconds);
      if (eta) {
        parts.push(eta);
      }
      return parts.join(" · ");
    },

    sidebarWidth: function () {
      return this.sidebarOpen ? "15rem" : "4.5rem";
    },

    enableShellTransitions: function () {
      document.documentElement.classList.add("shell-layout-transition");
      var sidebar = document.querySelector("[data-testid='app-sidebar']");
      if (sidebar) {
        sidebar.classList.add("sidebar-transition");
      }
    },

    sidebarInlineStyle: function () {
      if (this.sidebarMobileOpen) return "width:15rem";
      return this.desktop ? "width:" + this.sidebarWidth() : "width:15rem";
    },

    mainAreaStyle: function () {
      return this.desktop ? "--sidebar-w:" + this.sidebarWidth() : "";
    },

    toggleSidebar: function () {
      this.enableShellTransitions();
      this.sidebarOpen = !this.sidebarOpen;
      localStorage.setItem("sidebarOpen", String(this.sidebarOpen));
      if (this.desktop) {
        document.documentElement.style.setProperty("--sidebar-w", this.sidebarWidth());
      }
    },

    openMobileSidebar: function () {
      this.enableShellTransitions();
      this.sidebarMobileOpen = true;
    },

    closeMobileSidebar: function () {
      this.enableShellTransitions();
      this.sidebarMobileOpen = false;
    },

    openDonationsModal: function () {
      var self = this;
      self.donationsOpen = true;
      var focusCloseButton = function () {
        var closeButton =
          (self.$refs && self.$refs.donationsCloseButton) ||
          document.querySelector("[data-testid='donations-modal-close']");
        if (closeButton && typeof closeButton.focus === "function") {
          closeButton.focus({ preventScroll: true });
        }
      };
      if (window.Alpine && typeof window.Alpine.nextTick === "function") {
        window.Alpine.nextTick(function () {
          requestAnimationFrame(focusCloseButton);
        });
        return;
      }
      requestAnimationFrame(focusCloseButton);
    },

    closeDonationsModal: function () {
      this.donationsOpen = false;
    },

    handleSidebarNavClick: function (event) {
      if (event.target.closest("a")) {
        this.closeMobileSidebar();
      }
    },

    applyUsageStatsPreference: function (payload) {
      var consent =
        payload && typeof payload.consent === "string"
          ? String(payload.consent).toLowerCase()
          : "unknown";
      if (["unknown", "enabled", "disabled"].indexOf(consent) === -1) {
        consent = "unknown";
      }
      this.usageStatsConsent = consent;
      this.usageStatsLoaded = true;
      this.usageStatsPromptOpen = consent === "unknown";
      window.dispatchEvent(
        new CustomEvent("pullbox:usage-stats-preference-updated", {
          detail: {
            consent: consent,
            enabled: consent === "enabled",
            prompt_pending: consent === "unknown",
          },
        })
      );
    },

    bootstrapUsageStatsPrompt: function () {
      var self = this;
      fetch("/api/v1/system/usage-stats", {
        headers: { Accept: "application/json" },
      })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("Failed to load usage stats preference.");
          }
          return response.json();
        })
        .then(function (payload) {
          self.applyUsageStatsPreference(payload);
        })
        .catch(function () {
          self.usageStatsLoaded = true;
        });
    },

    submitUsageStatsConsent: function (enabled) {
      var self = this;
      if (self.usageStatsSaving) {
        return;
      }
      self.usageStatsSaving = true;
      fetch("/api/v1/system/usage-stats", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": readCsrfTokenFromBody(),
        },
        body: JSON.stringify({ enabled: Boolean(enabled) }),
      })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("Failed to save usage stats preference.");
          }
          return response.json();
        })
        .then(function (payload) {
          self.applyUsageStatsPreference(payload);
          self.usageStatsPromptOpen = false;
          if (typeof showToast === "function") {
            showToast({
              message: enabled
                ? "Anonymous usage stats enabled."
                : "Anonymous usage stats disabled.",
              level: "success",
            });
          }
        })
        .catch(function (error) {
          if (typeof showToast === "function") {
            showToast({
              message: error.message || "Failed to save usage stats preference.",
              level: "error",
            });
          }
        })
        .finally(function () {
          self.usageStatsSaving = false;
        });
    },
  };
}

function _readJsonBody(response) {
  return response
    .json()
    .catch(function () {
      return {};
    });
}

function _extractApiErrorMessage(response, data, fallback) {
  if (data && typeof data.detail === "string" && data.detail.trim() !== "") {
    return data.detail;
  }

  if (
    data &&
    data.detail &&
    data.detail.error &&
    typeof data.detail.error.message === "string" &&
    data.detail.error.message.trim() !== ""
  ) {
    return data.detail.error.message;
  }

  if (
    data &&
    data.error &&
    typeof data.error.message === "string" &&
    data.error.message.trim() !== ""
  ) {
    return data.error.message;
  }

  if (fallback && fallback.trim() !== "") {
    return fallback;
  }

  return "Request failed (" + response.status + ").";
}

function _isAlreadyBlockedResponse(response, data) {
  return (
    response.status === 409 &&
    _extractApiErrorMessage(response, data, "")
      .toLowerCase()
      .indexOf("already in blocklist") !== -1
  );
}

function _postBlocklist(url, csrfToken, body) {
  var headers = { "X-CSRF-Token": csrfToken };
  var options = { method: "POST", headers: headers };

  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }

  return fetch(url, options).then(function (response) {
    return _readJsonBody(response).then(function (data) {
      if (response.ok) {
        return { alreadyBlocked: false, data: data };
      }
      if (_isAlreadyBlockedResponse(response, data)) {
        return { alreadyBlocked: true, data: data };
      }
      throw new Error(_extractApiErrorMessage(response, data, "Failed to add to blocklist."));
    });
  });
}

function _markBlockedAction(button) {
  if (!button) {
    return;
  }
  button.disabled = true;
  button.setAttribute("aria-disabled", "true");
  button.classList.add("opacity-50", "cursor-not-allowed");
}

function issueSearchResultActions(config) {
  var cfg = config || {};

  return {
    grabbing: false,
    blocking: false,
    blocked: false,

    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },

    dispatchToast: function (message, level) {
      if (typeof showToast === "function") {
        showToast({ message: message, level: level });
      }
    },

    grabRelease: function (button) {
      var self = this;
      if (self.grabbing || self.blocked) {
        return;
      }

      var directAttemptId = parseInt(button.dataset.directAttempt, 10) || 0;
      var dcRouteToken = button.dataset.dcRouteToken || "";
      var endpoint = "/api/v1/issues/" + cfg.issueId + "/grab";
      var payload = {
        download_url: button.dataset.url,
        indexer_name: button.dataset.indexer,
        indexer_id: parseInt(button.dataset.indexerId, 10) || null,
        title: button.dataset.title,
        is_torrent: button.dataset.torrent === "true",
        file_size: parseInt(button.dataset.size, 10) || 0,
        search_log_id: cfg.searchLogId,
      };
      if (directAttemptId) {
        endpoint = "/api/v1/issues/" + cfg.issueId + "/direct-grab";
        payload = { direct_attempt_id: directAttemptId };
      } else if (dcRouteToken) {
        endpoint = "/api/v1/issues/" + cfg.issueId + "/dc-grab";
        payload = { dc_route_token: dcRouteToken };
      }

      self.grabbing = true;
      fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": self.csrfToken(),
        },
        body: JSON.stringify(payload),
      })
        .then(function (response) {
          if (response.ok) {
            self.dispatchToast(
              directAttemptId || dcRouteToken
                ? "Direct download queued"
                : "Grabbed successfully",
              "success"
            );
            self.grabbing = false;
            return;
          }

          return _readJsonBody(response)
            .then(function (data) {
              throw new Error(
                _extractApiErrorMessage(response, data, "Grab failed: " + response.status)
              );
            })
            .catch(function (error) {
              if (error instanceof Error) {
                throw error;
              }
              throw new Error("Grab failed: " + response.status);
            });
        })
        .catch(function (error) {
          self.grabbing = false;
          self.dispatchToast(error.message || "Grab failed", "error");
        })
        .finally(function () {
          self.grabbing = false;
        });
    },

    grabRejectedRelease: function (button) {
      var self = this;
      if (self.grabbing || self.blocked) {
        return;
      }

      var reason = button.dataset.rejectionReason || "Pullbox rejected this result.";
      var isDirect = Boolean(parseInt(button.dataset.directAttempt, 10) || 0);
      pbConfirm({
        title: "Grab Rejected Result",
        message:
          "Pullbox rejected this result: " +
          reason +
          (isDirect
            ? " If you continue, Pullbox will plan and queue this direct result anyway."
            : " If you continue, Pullbox will send it to your download client anyway."),
        confirmText: "Grab anyway",
        destructive: false,
      }).then(function (ok) {
        if (!ok) {
          return;
        }
        self.grabRelease(button);
      });
    },

    blockRelease: function (button) {
      var self = this;
      if (self.grabbing || self.blocking || self.blocked) {
        return;
      }

      pbConfirm({
        title: "Block Release",
        message: "Add this release to the blocklist? It won't appear in future search results.",
        confirmText: "Block",
      }).then(function (ok) {
        if (!ok) {
          return;
        }

        self.blocking = true;
        _postBlocklist("/api/v1/blocklist", self.csrfToken(), {
          release_title: button.dataset.blockTitle,
          series_id: cfg.seriesId,
          issue_id: cfg.issueId,
        })
          .then(function (result) {
            self.blocked = true;
            var row = button.closest("tr");
            if (row) {
              row.style.opacity = "0.35";
              row.style.transition = "opacity 300ms";
            }
            self.dispatchToast(
              result.alreadyBlocked ? "Already blocked." : "Release blocked.",
              "success"
            );
          })
          .catch(function (error) {
            self.dispatchToast(error.message || "Failed to block release.", "error");
          })
          .finally(function () {
            self.blocking = false;
          });
      });
    },
  };
}

window.issueSearchResultActions = issueSearchResultActions;

function downloadsPage(config) {
  var cfg = config || {};

  return {
    sourceModalOpen: false,
    sourceModalLoading: false,
    sourceModalError: "",
    sourceOptions: null,
    sourceDownloadId: null,
    selectedSourceIdentity: "",
    blockCurrentSource: false,
    sourceSwitching: false,
    sourceRequestRevision: 0,

    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },

    dispatchToast: function (message, level) {
      if (typeof showToast === "function") {
        showToast({ message: message, level: level });
      }
    },

    currentPath: function () {
      return window.location.pathname + window.location.search;
    },

    refreshContent: function (path) {
      var target = document.getElementById("downloads-content");
      if (!target || typeof htmx === "undefined") {
        window.location.assign(path || this.currentPath());
        return;
      }

      htmx.ajax("GET", path || this.currentPath(), {
        target: "#downloads-content",
        swap: "outerHTML",
      });
    },

    cancelDownload: function (id, btn) {
      var self = this;
      pbConfirm({
        title: "Cancel Download",
        message: "Are you sure you want to cancel this download?",
        confirmText: "Cancel Download",
      }).then(function (ok) {
        if (!ok) return;
        btn.disabled = true;
        fetch("/api/v1/downloads/" + id, {
          method: "DELETE",
          headers: { "X-CSRF-Token": self.csrfToken() },
        })
          .then(function (res) {
            if (!res.ok && res.status !== 204) {
              throw new Error("Failed to cancel download.");
            }
            self.dispatchToast("Download cancelled.", "success");
            self.refreshContent();
          })
          .catch(function (err) {
            btn.disabled = false;
            self.dispatchToast(err.message, "error");
          });
      });
    },

    parseActionError: function (data, fallback) {
      if (data && typeof data.detail === "string") return data.detail;
      if (data && data.error && typeof data.error.message === "string") {
        return data.error.message;
      }
      return fallback;
    },

    formatSourceBytes: function (value) {
      var bytes = Number(value || 0);
      if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
      var units = ["B", "KB", "MB", "GB", "TB"];
      var index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
      var amount = bytes / Math.pow(1024, index);
      return amount.toFixed(index === 0 ? 0 : 1) + " " + units[index];
    },

    openSourceModal: function (id, btn) {
      var self = this;
      if (self.sourceSwitching) return;
      var requestRevision = ++self.sourceRequestRevision;
      self.sourceModalOpen = true;
      self.sourceModalLoading = true;
      self.sourceModalError = "";
      self.sourceOptions = null;
      self.sourceDownloadId = id;
      self.selectedSourceIdentity = "";
      self.blockCurrentSource = false;
      if (btn) btn.disabled = true;
      fetch("/api/v1/downloads/" + id + "/sources")
        .then(function (res) {
          return res.json().catch(function () { return {}; }).then(function (data) {
            if (!res.ok) {
              throw new Error(self.parseActionError(data, "Available sources could not be loaded."));
            }
            return data;
          });
        })
        .then(function (data) {
          if (requestRevision !== self.sourceRequestRevision || self.sourceDownloadId !== id) {
            return;
          }
          self.sourceOptions = data;
          self.selectedSourceIdentity = data.alternatives.length
            ? data.alternatives[0].artifact_identity
            : "";
        })
        .catch(function (err) {
          if (requestRevision === self.sourceRequestRevision) {
            self.sourceModalError = err.message;
          }
        })
        .finally(function () {
          if (requestRevision === self.sourceRequestRevision) {
            self.sourceModalLoading = false;
          }
          if (btn) btn.disabled = false;
        });
    },

    closeSourceModal: function () {
      if (this.sourceSwitching) return;
      this.sourceRequestRevision += 1;
      this.sourceModalOpen = false;
      this.sourceModalError = "";
      this.sourceOptions = null;
      this.sourceDownloadId = null;
      this.selectedSourceIdentity = "";
      this.blockCurrentSource = false;
    },

    performSourceSwitch: function (id, artifactIdentity, blockCurrent, btn) {
      var self = this;
      self.sourceSwitching = true;
      if (btn) btn.disabled = true;
      return fetch("/api/v1/downloads/" + id + "/switch-source", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": self.csrfToken(),
        },
        body: JSON.stringify({
          artifact_identity: artifactIdentity || null,
          block_current: Boolean(blockCurrent),
        }),
      })
        .then(function (res) {
          return res.json().catch(function () { return {}; }).then(function (data) {
            if (!res.ok) {
              throw new Error(self.parseActionError(data, "The download source could not be changed."));
            }
            return data;
          });
        })
        .then(function (data) {
          self.sourceModalOpen = false;
          self.dispatchToast(
            "Switching from " + data.previous_host + " to " + data.selected_host + ".",
            "success"
          );
          self.refreshContent("/downloads?tab=queue");
          return data;
        })
        .catch(function (err) {
          if (self.sourceModalOpen) {
            self.sourceModalError = err.message;
          } else {
            self.dispatchToast(err.message, "error");
          }
          throw err;
        })
        .finally(function () {
          self.sourceSwitching = false;
          if (btn) btn.disabled = false;
        });
    },

    tryNextSource: function (id, btn) {
      var self = this;
      if (self.sourceSwitching) return;
      pbConfirm({
        title: "Try Next Source",
        message: "Pullbox will stop this transfer, discard its partial data, and restart from the next ranked source.",
        confirmText: "Switch Source",
      }).then(function (ok) {
        if (!ok) return;
        self.dispatchToast(
          "Stopping the current source and starting the next verified route...",
          "info"
        );
        self.performSourceSwitch(id, null, false, btn).catch(function () {});
      });
    },

    switchSelectedSource: function () {
      if (!this.sourceDownloadId || !this.selectedSourceIdentity || this.sourceSwitching) return;
      this.sourceModalError = "";
      this.performSourceSwitch(
        this.sourceDownloadId,
        this.selectedSourceIdentity,
        this.blockCurrentSource,
        null
      ).catch(function () {});
    },

    retryDownload: function (id, btn) {
      var self = this;
      btn.disabled = true;
      fetch("/api/v1/downloads/" + id + "/retry", {
        method: "POST",
        headers: { "X-CSRF-Token": self.csrfToken() },
      })
        .then(function (res) {
          if (!res.ok) {
            return res.json().then(function (data) {
              throw new Error(data.detail || (data.error && data.error.message) || "Retry failed");
            });
          }
          self.dispatchToast("Download re-queued.", "success");
          self.refreshContent("/downloads?tab=queue");
        })
        .catch(function (err) {
          btn.disabled = false;
          self.dispatchToast(err.message, "error");
        });
    },

    blockFailedDownload: function (id, btn) {
      var self = this;
      pbConfirm({
        title: "Block Release",
        message: "Add this failed release to the blocklist? It won't appear in future search results.",
        confirmText: "Block",
      }).then(function (ok) {
        if (!ok) return;
        btn.disabled = true;
        _postBlocklist("/api/v1/downloads/" + id + "/blocklist", self.csrfToken())
          .then(function (result) {
            _markBlockedAction(btn);
            self.dispatchToast(
              result.alreadyBlocked ? "Already blocked." : "Release blocked.",
              "success"
            );
          })
          .catch(function (error) {
            btn.disabled = false;
            self.dispatchToast(error.message, "error");
          });
      });
    },

    removeHistory: function (id, btn) {
      var self = this;
      pbConfirm({
        title: "Remove History Entry",
        message: "Remove this download from history?",
        confirmText: "Remove",
      }).then(function (ok) {
        if (!ok) return;
        btn.disabled = true;
        fetch("/api/v1/downloads/" + id, {
          method: "DELETE",
          headers: { "X-CSRF-Token": self.csrfToken() },
        })
          .then(function (res) {
            if (!res.ok && res.status !== 204) {
              throw new Error("Failed to remove entry.");
            }
            self.dispatchToast("Removed from history.", "success");
            self.refreshContent();
          })
          .catch(function (err) {
            btn.disabled = false;
            self.dispatchToast(err.message, "error");
          });
      });
    },

    clearHistory: function (btn) {
      var self = this;
      pbConfirm({
        title: "Clear Download History",
        message: "This will permanently delete all completed and failed download records. Active downloads will not be affected.",
        confirmText: "Clear All History",
      }).then(function (ok) {
        if (!ok) return;
        btn.disabled = true;
        fetch("/api/v1/downloads/history", {
          method: "DELETE",
          headers: { "X-CSRF-Token": self.csrfToken() },
        })
          .then(function (res) {
            if (!res.ok) throw new Error("Failed to clear history.");
            return res.json();
          })
          .then(function (data) {
            self.dispatchToast(
              "Cleared " + data.deleted + " history record" + (data.deleted !== 1 ? "s" : "") + ".",
              "success"
            );
            self.refreshContent("/downloads?tab=history");
          })
          .catch(function (err) {
            btn.disabled = false;
            self.dispatchToast(err.message, "error");
          });
      });
    },
  };
}

function dropdownSelectData(config) {
  var cfg = config || {};
  var rawOptions = Array.isArray(cfg.options) ? cfg.options : [];
  var normalizedOptions = rawOptions.map(function (option) {
    if (Array.isArray(option)) {
      return {
        value: option[0] == null ? "" : String(option[0]),
        label:
          option[1] == null
            ? option[0] == null
              ? ""
              : String(option[0])
            : String(option[1]),
        disabled: false,
      };
    }

    var item = option || {};
    return {
      value: item.value == null ? "" : String(item.value),
      label:
        item.label == null
          ? item.value == null
            ? ""
            : String(item.value)
          : String(item.label),
      disabled: Boolean(item.disabled),
    };
  });

  function findIndexByValue(value) {
    var normalizedValue = value == null ? "" : String(value);
    for (var i = 0; i < normalizedOptions.length; i += 1) {
      if (normalizedOptions[i].value === normalizedValue) {
        return i;
      }
    }
    return -1;
  }

  var initialValue = cfg.value == null ? "" : String(cfg.value);
  var initialIndex = findIndexByValue(initialValue);
  if (initialIndex === -1 && normalizedOptions.length > 0) {
    initialIndex = 0;
    initialValue = normalizedOptions[0].value;
  }

  var panelControlVars = [
    "--pb-control-min-height",
    "--pb-control-radius",
    "--pb-control-gap",
    "--pb-control-px",
    "--pb-control-py",
    "--pb-control-font-size",
    "--pb-control-line-height",
    "--pb-control-icon-size",
    "--pb-control-panel-item-px",
    "--pb-control-panel-item-py",
  ];

  return {
    open: false,
    panelReady: false,
    panelPlacement: "bottom",
    disabled: Boolean(cfg.disabled),
    fitWidth: Boolean(cfg.fitWidth),
    wrapOptions: Boolean(cfg.wrapOptions),
    changeExpression:
      typeof cfg.changeExpression === "string" ? cfg.changeExpression.trim() : "",
    options: normalizedOptions,
    value: initialValue,
    selectedIndex: initialIndex,
    activeIndex: initialIndex,
    currentLabel: initialIndex >= 0 ? normalizedOptions[initialIndex].label : "",
    listenersBound: false,
    panelRaf: null,
    fitTriggerWidth: 0,
    fitPanelWidth: 0,
    handleDocumentPointerBound: null,
    handleViewportChangeBound: null,

    init: function () {
      this.syncFromValue();
      this.syncInput();
      this.applyFitWidth();
      this.handleDocumentPointerBound = this.handleDocumentPointer.bind(this);
      this.handleViewportChangeBound = this.handleViewportChange.bind(this);
      var self = this;
      window.requestAnimationFrame(function () {
        self.applyFitWidth();
      });
      if (document.fonts && document.fonts.ready && typeof document.fonts.ready.then === "function") {
        document.fonts.ready
          .then(function () {
            self.applyFitWidth();
            if (self.open) {
              self.schedulePanelPositionUpdate();
            }
          })
          .catch(function () {});
      }
    },

    syncFromValue: function () {
      var nextIndex = findIndexByValue(this.value);
      if (nextIndex === -1 && this.options.length > 0) {
        nextIndex = 0;
        this.value = this.options[0].value;
      }

      this.selectedIndex = nextIndex;
      this.activeIndex = nextIndex;
      this.currentLabel = nextIndex >= 0 ? this.options[nextIndex].label : "";
    },

    syncInput: function () {
      if (this.$refs && this.$refs.input) {
        this.$refs.input.value = this.value;
      }
      if (this.$el) {
        this.$el.setAttribute("data-dropdown-value", this.value);
      }
    },

    syncExternalValue: function (nextValue) {
      var normalizedValue = nextValue == null ? "" : String(nextValue);
      if (normalizedValue === this.value) {
        return;
      }
      this.value = normalizedValue;
      this.syncFromValue();
      this.syncInput();
      this.applyFitWidth();
    },

    runChangeExpression: function () {
      if (!this.changeExpression || !this.$el) {
        return;
      }

      var alpine = window.Alpine;
      if (!alpine || typeof alpine.evaluate !== "function") {
        return;
      }

      alpine.evaluate(this.$el, this.changeExpression, {
        scope: {
          $event: {
            detail: {
              value: this.value,
              label: this.currentLabel,
            },
          },
        },
      });
    },

    syncPanelControlVars: function () {
      if (!this.$refs || !this.$refs.panel || !this.$el) {
        return;
      }

      var rootStyles = window.getComputedStyle(this.$el);
      var panel = this.$refs.panel;
      for (var i = 0; i < panelControlVars.length; i += 1) {
        var token = panelControlVars[i];
        var value = rootStyles.getPropertyValue(token);
        if (value) {
          panel.style.setProperty(token, value.trim());
        }
      }
    },

    applyFitWidth: function () {
      if (!this.$el || !this.$refs || !this.$refs.trigger) {
        return;
      }

      var trigger = this.$refs.trigger;
      var triggerLabel = this.$el.querySelector("[data-dropdown-select-trigger-label]");
      var measureEl = document.createElement("span");
      measureEl.style.position = "absolute";
      measureEl.style.visibility = "hidden";
      measureEl.style.pointerEvents = "none";
      measureEl.style.left = "-9999px";
      measureEl.style.top = "0";
      measureEl.style.whiteSpace = "nowrap";
      document.body.appendChild(measureEl);

      var textStyles = window.getComputedStyle(triggerLabel || trigger);
      measureEl.style.font = textStyles.font;
      measureEl.style.fontWeight = textStyles.fontWeight;
      measureEl.style.fontSize = textStyles.fontSize;
      measureEl.style.letterSpacing = textStyles.letterSpacing;
      measureEl.style.textTransform = textStyles.textTransform;

      var maxLabelWidth = 0;
      for (var i = 0; i < this.options.length; i += 1) {
        var option = this.options[i];
        var labelText = option && option.label ? option.label : "";
        measureEl.textContent = labelText;
        var measuredWidth = Math.ceil(measureEl.getBoundingClientRect().width);
        if (measuredWidth > maxLabelWidth) {
          maxLabelWidth = measuredWidth;
        }
      }

      document.body.removeChild(measureEl);

      var triggerStyles = window.getComputedStyle(trigger);
      var rootStyles = window.getComputedStyle(this.$el);
      var chevron = trigger.querySelector(".dropdown-select-chevron");
      var chevronWidth = chevron ? Math.ceil(chevron.getBoundingClientRect().width) : 16;
      var triggerGap = parseFloat(triggerStyles.columnGap || triggerStyles.gap || "0");
      var triggerPadding =
        parseFloat(triggerStyles.paddingLeft || "0") +
        parseFloat(triggerStyles.paddingRight || "0");
      var triggerWidth = Math.ceil(maxLabelWidth + triggerPadding + triggerGap + chevronWidth + 2);

      var optionGap = parseFloat(rootStyles.getPropertyValue("--pb-control-gap") || "0");
      var optionPadding =
        parseFloat(rootStyles.getPropertyValue("--pb-control-panel-item-px") || "0") * 2;
      var checkWidth = parseFloat(rootStyles.getPropertyValue("--pb-control-icon-size") || "16");
      var panelWidth = Math.max(
        triggerWidth,
        Math.ceil(maxLabelWidth + optionPadding + optionGap + checkWidth + 24)
      );

      this.fitTriggerWidth = triggerWidth;
      this.fitPanelWidth = panelWidth;
      if (this.fitWidth) {
        this.$el.style.setProperty("--pb-dropdown-fit-width", triggerWidth + "px");
      } else {
        this.$el.style.removeProperty("--pb-dropdown-fit-width");
      }
      this.$el.style.setProperty("--pb-dropdown-panel-fit-width", panelWidth + "px");
    },

    applyPanelPlacement: function (placement) {
      var nextPlacement = placement === "top" ? "top" : "bottom";
      this.panelPlacement = nextPlacement;
      if (this.$el) {
        this.$el.setAttribute("data-dropdown-placement", nextPlacement);
      }
    },

    updatePanelPosition: function () {
      if (!this.$refs || !this.$refs.trigger || !this.$refs.panel) {
        return;
      }

      var triggerRect = this.$refs.trigger.getBoundingClientRect();
      var panel = this.$refs.panel;
      var gutter = 12;
      var viewportWidth =
        window.innerWidth || document.documentElement.clientWidth || 0;
      var viewportHeight =
        window.innerHeight || document.documentElement.clientHeight || 0;
      var spaceAbove = Math.max(triggerRect.top - gutter, 0);
      var spaceBelow = Math.max(viewportHeight - triggerRect.bottom - gutter, 0);
      var estimatedOptionHeight = Math.max(triggerRect.height || 0, 36);
      var estimatedHeight = Math.min(
        this.options.length * estimatedOptionHeight + 8,
        320
      );
      var naturalWidth = this.wrapOptions
        ? Math.ceil(triggerRect.width)
        : Math.max(Math.ceil(triggerRect.width), Math.ceil(this.fitPanelWidth || 0));
      var maxWidth = Math.max(viewportWidth - gutter * 2, Math.ceil(triggerRect.width));
      var panelWidth = Math.min(naturalWidth, maxWidth);
      var left = triggerRect.left;
      if (left + panelWidth > viewportWidth - gutter) {
        left = viewportWidth - gutter - panelWidth;
      }
      if (left < gutter) {
        left = gutter;
      }

      panel.style.left = Math.round(left) + "px";
      panel.style.width = Math.round(panelWidth) + "px";
      panel.style.maxWidth = maxWidth + "px";

      var measuredHeight = Math.max(
        panel.scrollHeight || 0,
        panel.getBoundingClientRect().height || 0
      );
      var naturalHeight = Math.min(Math.max(measuredHeight, estimatedHeight), 320);
      var preferredPlacement = "bottom";

      if (spaceBelow < Math.min(naturalHeight || 240, 240) && spaceAbove > spaceBelow) {
        preferredPlacement = "top";
      }
      var availableSpace = preferredPlacement === "top" ? spaceAbove : spaceBelow;
      var maxHeight = Math.max(Math.min(availableSpace, 320), 96);
      var panelHeight = Math.min(naturalHeight, maxHeight);
      var top =
        preferredPlacement === "top"
          ? Math.max(gutter, triggerRect.top - 4 - panelHeight)
          : triggerRect.bottom + 4;

      this.panelPlacement = preferredPlacement;
      if (this.$el) {
        this.$el.setAttribute("data-dropdown-placement", preferredPlacement);
      }
      panel.style.top = Math.round(top) + "px";
      panel.style.maxHeight = Math.round(maxHeight) + "px";
      this.panelReady = true;
    },

    estimatePanelPlacement: function () {
      if (!this.$refs || !this.$refs.trigger) {
        return "bottom";
      }

      var triggerRect = this.$refs.trigger.getBoundingClientRect();
      var gutter = 12;
      var viewportHeight =
        window.innerHeight || document.documentElement.clientHeight || 0;
      var spaceAbove = Math.max(triggerRect.top - gutter, 0);
      var spaceBelow = Math.max(viewportHeight - triggerRect.bottom - gutter, 0);
      var estimatedOptionHeight = Math.max(triggerRect.height || 0, 36);
      var estimatedHeight = Math.min((this.options.length * estimatedOptionHeight) + 8, 320);

      if (spaceBelow < Math.min(estimatedHeight, 240) && spaceAbove > spaceBelow) {
        return "top";
      }

      return "bottom";
    },

    schedulePanelPositionUpdate: function () {
      var self = this;
      if (this.panelRaf != null) {
        window.cancelAnimationFrame(this.panelRaf);
      }
      this.panelRaf = window.requestAnimationFrame(function () {
        self.panelRaf = null;
        self.updatePanelPosition();
      });
    },

    bindOpenListeners: function () {
      if (this.listenersBound) {
        return;
      }
      document.addEventListener(
        "pointerdown",
        this.handleDocumentPointerBound,
        true
      );
      window.addEventListener("resize", this.handleViewportChangeBound);
      window.addEventListener("scroll", this.handleViewportChangeBound, true);
      this.listenersBound = true;
    },

    unbindOpenListeners: function () {
      if (!this.listenersBound) {
        return;
      }
      document.removeEventListener(
        "pointerdown",
        this.handleDocumentPointerBound,
        true
      );
      window.removeEventListener("resize", this.handleViewportChangeBound);
      window.removeEventListener("scroll", this.handleViewportChangeBound, true);
      if (this.panelRaf != null) {
        window.cancelAnimationFrame(this.panelRaf);
        this.panelRaf = null;
      }
      this.listenersBound = false;
    },

    handleDocumentPointer: function (event) {
      if (!this.open) {
        return;
      }
      var target = event.target;
      if (!target) {
        return;
      }
      if (this.$el && this.$el.contains(target)) {
        return;
      }
      if (this.$refs && this.$refs.panel && this.$refs.panel.contains(target)) {
        return;
      }
      this.close(false);
    },

    handleViewportChange: function () {
      if (!this.open) {
        return;
      }
      this.schedulePanelPositionUpdate();
    },

    optionElements: function () {
      if (!this.$refs || !this.$refs.panel) {
        return [];
      }
      return Array.from(this.$refs.panel.querySelectorAll("[data-dropdown-option]"));
    },

    focusOption: function (index) {
      var options = this.optionElements();
      var option = options[index];
      if (!option) {
        return;
      }

      option.focus({ preventScroll: true });
      option.scrollIntoView({ block: "nearest" });
    },

    nextIndex: function (startIndex, step) {
      if (this.options.length === 0) {
        return -1;
      }

      var next = startIndex;
      if (next < 0) {
        next = step > 0 ? -1 : 0;
      }

      for (var i = 0; i < this.options.length; i += 1) {
        next = (next + step + this.options.length) % this.options.length;
        if (!this.options[next].disabled) {
          return next;
        }
      }

      return startIndex;
    },

    openPanel: function () {
      if (this.disabled || this.options.length === 0) {
        return;
      }

      this.syncFromValue();
      this.applyFitWidth();
      this.panelReady = false;
      this.open = true;
      this.bindOpenListeners();

      var self = this;
      this.$nextTick(function () {
        requestAnimationFrame(function () {
          self.syncPanelControlVars();
          self.updatePanelPosition();
          self.focusOption(self.activeIndex);
        });
      });
    },

    toggle: function () {
      if (this.disabled) {
        return;
      }
      if (this.open) {
        this.close();
        return;
      }
      this.openPanel();
    },

    close: function (focusTrigger) {
      this.open = false;
      this.panelReady = false;
      this.unbindOpenListeners();

      if (focusTrigger === false) {
        return;
      }

      var self = this;
      this.$nextTick(function () {
        if (self.$refs && self.$refs.trigger) {
          self.$refs.trigger.focus();
        }
      });
    },

    openAndFocus: function (step) {
      if (this.disabled) {
        return;
      }

      if (!this.open) {
        this.openPanel();
        if (step) {
          this.activeIndex = this.nextIndex(this.activeIndex, step);
          var self = this;
          this.$nextTick(function () {
            self.focusOption(self.activeIndex);
          });
        }
        return;
      }

      this.move(step);
    },

    move: function (step) {
      if (!this.open) {
        this.openAndFocus(step);
        return;
      }

      this.activeIndex = this.nextIndex(this.activeIndex, step);
      this.focusOption(this.activeIndex);
    },

    focusBoundary: function (position) {
      if (!this.open || this.options.length === 0) {
        return;
      }

      this.activeIndex = position === "end" ? this.options.length - 1 : 0;
      this.focusOption(this.activeIndex);
    },

    isSelected: function (index) {
      return index === this.selectedIndex;
    },

    setActive: function (index) {
      this.activeIndex = index;
    },

    optionClasses: function (index) {
      var classes = [];
      if (this.activeIndex === index) {
        classes.push("dropdown-select-option-active");
      }
      if (this.selectedIndex === index) {
        classes.push("dropdown-select-option-selected");
      }
      return classes.join(" ");
    },

    requestRemoteSelection: function () {
      if (!this.$refs || !this.$refs.input) {
        return false;
      }

      var input = this.$refs.input;
      var form = input.form || (input.closest ? input.closest("form") : null);
      if (form && form.getAttribute("hx-get")) {
        var submitter = form.querySelector("button[type='submit'], input[type='submit']");
        if (submitter && !submitter.disabled) {
          submitter.click();
          return true;
        }

        if (typeof form.requestSubmit === "function") {
          form.requestSubmit();
          return true;
        }

        form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
        return true;
      }

      var hxGet = input.getAttribute("hx-get");
      if (!hxGet || !window.htmx) {
        return false;
      }

      var params = new URLSearchParams();

      function appendControlValue(control) {
        if (!control || !control.name || control.disabled) {
          return;
        }
        if ((control.type === "checkbox" || control.type === "radio") && !control.checked) {
          return;
        }

        var value = control.value == null ? "" : String(control.value);
        if (value === "") {
          return;
        }

        params.delete(control.name);
        params.append(control.name, value);
      }

      appendControlValue(input);

      var hxInclude = input.getAttribute("hx-include");
      if (hxInclude && document.querySelectorAll) {
        document.querySelectorAll(hxInclude).forEach(function (node) {
          if (!node) {
            return;
          }
          if (node.tagName === "FORM") {
            Array.from(node.elements || []).forEach(appendControlValue);
            return;
          }
          appendControlValue(node);
        });
      }

      var requestPath = params.toString() ? hxGet + "?" + params.toString() : hxGet;
      var ajaxOptions = {};
      var target = input.getAttribute("hx-target");
      var swap = input.getAttribute("hx-swap");
      var select = input.getAttribute("hx-select");

      if (target) ajaxOptions.target = target;
      if (swap) ajaxOptions.swap = swap;
      if (select) ajaxOptions.select = select;

      window.htmx.ajax("GET", requestPath, ajaxOptions);

      if (input.getAttribute("hx-push-url") === "true") {
        history.pushState({}, "", requestPath);
      }

      return true;
    },

    selectByIndex: function (index) {
      var option = this.options[index];
      if (!option || option.disabled) {
        return;
      }

      this.value = option.value;
      this.selectedIndex = index;
      this.activeIndex = index;
      this.currentLabel = option.label;
      this.syncInput();
      this.close(false);

      this.runChangeExpression();

      if (!this.requestRemoteSelection() && this.$refs && this.$refs.input) {
        this.$refs.input.dispatchEvent(new Event("change", { bubbles: true }));
      }

      if (this.$el) {
        this.$el.dispatchEvent(
          new CustomEvent("dropdown-select-change", {
            bubbles: true,
            detail: {
              value: this.value,
              label: this.currentLabel,
            },
          })
        );
      }

      var self = this;
      this.$nextTick(function () {
        if (self.$refs && self.$refs.trigger) {
          self.$refs.trigger.focus();
        }
      });
    },

    selectActive: function () {
      if (this.activeIndex < 0) {
        return;
      }
      this.selectByIndex(this.activeIndex);
    },

    selectByValue: function (value) {
      var nextIndex = findIndexByValue(value);
      if (nextIndex !== -1) {
        this.selectByIndex(nextIndex);
      }
    },
  };
}

var _SEARCH_HISTORY_MAX_ITEMS = 12;

function _readSearchHistory(storageKey) {
  try {
    return JSON.parse(localStorage.getItem(storageKey) || "[]");
  } catch (_) {
    return [];
  }
}

function _writeSearchHistory(storageKey, history) {
  if (!history || history.length === 0) {
    localStorage.removeItem(storageKey);
    return;
  }
  localStorage.setItem(storageKey, JSON.stringify(history));
}

function _rememberSearchTerm(storageKey, term, maxItems) {
  var nextTerm = (term || "").trim();
  if (!nextTerm) {
    return _readSearchHistory(storageKey);
  }

  var history = _readSearchHistory(storageKey).filter(function (item) {
    return item.toLowerCase() !== nextTerm.toLowerCase();
  });
  history.unshift(nextTerm);
  if (history.length > maxItems) {
    history = history.slice(0, maxItems);
  }
  _writeSearchHistory(storageKey, history);
  return history;
}

function _searchFieldDisplayValue(el) {
  if (!el) {
    return "";
  }
  return (el.textContent || "")
    .replace(/\u00a0/g, " ")
    .replace(/\r?\n/g, " ");
}

function _cancelSearchFieldTimer(root) {
  if (!root || !root.__searchFieldTimer) {
    return;
  }

  window.clearTimeout(root.__searchFieldTimer);
  root.__searchFieldTimer = null;
}

function _getSearchFieldHistoryKey(root) {
  if (!root || !root.getAttribute) {
    return "";
  }
  return root.getAttribute("data-search-history-key") || "";
}

function _rememberSearchFieldTerm(root, term) {
  var storageKey = _getSearchFieldHistoryKey(root);
  if (!storageKey) {
    return [];
  }
  return _rememberSearchTerm(storageKey, term, _SEARCH_HISTORY_MAX_ITEMS);
}

function _hideSearchFieldHistory(root) {
  if (!root || !root.querySelector) {
    return;
  }

  var panel = root.querySelector("[data-search-history-panel]");
  if (!panel) {
    return;
  }

  panel.style.display = "none";
  panel.setAttribute("aria-hidden", "true");
}

function _renderSearchFieldHistory(root) {
  if (!root || !root.querySelector) {
    return [];
  }

  var storageKey = _getSearchFieldHistoryKey(root);
  var panel = root.querySelector("[data-search-history-panel]");
  var list = root.querySelector("[data-search-history-list]");
  var clear = root.querySelector("[data-search-history-clear]");

  if (!storageKey || !panel || !list) {
    return [];
  }

  var history = _readSearchHistory(storageKey);
  list.innerHTML = "";

  for (var i = 0; i < history.length; i += 1) {
    (function (term, idx) {
      var item = document.createElement("li");
      item.className = "group search-history-item";

      var selectButton = document.createElement("button");
      selectButton.type = "button";
      selectButton.className = "search-history-item-button";
      selectButton.setAttribute("data-search-history-item", "");

      var icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.setAttribute("class", "h-3.5 w-3.5 shrink-0 text-pb-text-dim");
      icon.setAttribute("fill", "none");
      icon.setAttribute("stroke", "currentColor");
      icon.setAttribute("stroke-width", "2");
      icon.setAttribute("viewBox", "0 0 24 24");
      icon.setAttribute("aria-hidden", "true");

      var iconPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
      iconPath.setAttribute("stroke-linecap", "round");
      iconPath.setAttribute("stroke-linejoin", "round");
      iconPath.setAttribute("d", "M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z");
      icon.appendChild(iconPath);

      var label = document.createElement("span");
      label.className = "truncate";
      label.textContent = term;

      selectButton.appendChild(icon);
      selectButton.appendChild(label);
      selectButton.addEventListener("click", function () {
        _selectSearchHistoryTerm(root, term);
      });

      var removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "search-history-item-remove";
      removeButton.setAttribute("data-search-history-remove", "");

      var removeIcon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      removeIcon.setAttribute("class", "h-3 w-3");
      removeIcon.setAttribute("fill", "none");
      removeIcon.setAttribute("stroke", "currentColor");
      removeIcon.setAttribute("stroke-width", "2.5");
      removeIcon.setAttribute("viewBox", "0 0 24 24");
      removeIcon.setAttribute("aria-hidden", "true");

      var removePath = document.createElementNS("http://www.w3.org/2000/svg", "path");
      removePath.setAttribute("stroke-linecap", "round");
      removePath.setAttribute("stroke-linejoin", "round");
      removePath.setAttribute("d", "M6 18L18 6M6 6l12 12");
      removeIcon.appendChild(removePath);

      removeButton.appendChild(removeIcon);
      removeButton.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        var nextHistory = _readSearchHistory(storageKey);
        nextHistory.splice(idx, 1);
        _writeSearchHistory(storageKey, nextHistory);
        _renderSearchFieldHistory(root);
        _maybeOpenSearchFieldHistory(root, true);
      });

      item.appendChild(selectButton);
      item.appendChild(removeButton);
      list.appendChild(item);
    })(history[i], i);
  }

  if (clear) {
    clear.disabled = history.length === 0;
    clear.style.visibility = history.length > 0 ? "" : "hidden";
    if (!clear.__searchHistoryBound) {
      clear.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        _writeSearchHistory(storageKey, []);
        _renderSearchFieldHistory(root);
        _hideSearchFieldHistory(root);
      });
      clear.__searchHistoryBound = true;
    }
  }

  return history;
}

function _maybeOpenSearchFieldHistory(root, force) {
  if (!root || !root.querySelector) {
    return;
  }

  var input = root.querySelector("[data-search-field-input]");
  var panel = root.querySelector("[data-search-history-panel]");
  if (!input || !panel) {
    return;
  }

  var history = _renderSearchFieldHistory(root);
  var shouldOpen = history.length > 0 && ((input.value || "").trim() === "") && (force || document.activeElement === input);

  panel.style.display = shouldOpen ? "" : "none";
  panel.setAttribute("aria-hidden", shouldOpen ? "false" : "true");
}

function _setSearchFieldSubmitMode(root, value) {
  if (!root || !root.closest) {
    return;
  }

  var form = root.closest("form");
  var modeInput = form ? form.querySelector("[data-search-field-submit-mode-input]") : null;
  if (!modeInput) {
    return;
  }

  if (value === "preview") {
    modeInput.name = "search_mode";
    modeInput.value = "preview";
    return;
  }

  modeInput.removeAttribute("name");
  modeInput.value = "";
}

function _resetSearchFieldSubmitModeSoon(root, submitMode) {
  if (!submitMode) {
    return;
  }

  window.setTimeout(function () {
    _setSearchFieldSubmitMode(root, "full");
  }, 0);
}

function _submitSearchFieldForm(root, input, submitMode) {
  if (!root || !input) {
    return;
  }

  _cancelSearchFieldTimer(root);
  _rememberSearchFieldTerm(root, input.value);
  _hideSearchFieldHistory(root);
  _setSearchFieldSubmitMode(root, submitMode || "full");

  var form = input.form || (input.closest ? input.closest("form") : null);
  if (!form) {
    return;
  }

  var submitter = form.querySelector("button[type='submit'], input[type='submit']");
  if (submitter && !submitter.disabled) {
    submitter.click();
    _resetSearchFieldSubmitModeSoon(root, submitMode);
    return;
  }

  if (typeof form.requestSubmit === "function") {
    form.requestSubmit();
    _resetSearchFieldSubmitModeSoon(root, submitMode);
    return;
  }

  form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  _resetSearchFieldSubmitModeSoon(root, submitMode);
}

function _resetRemoteSearchField(root, input) {
  if (!root || !input) {
    return;
  }

  var form = input.form || (input.closest ? input.closest("form") : null);
  if (!form) {
    input.focus();
    return;
  }

  _cancelSearchFieldTimer(root);

  var hxGet = form.getAttribute("hx-get");
  if (hxGet && window.htmx) {
    var formData = new FormData(form);
    if (input.name) {
      formData.set(input.name, "");
    }

    var params = new URLSearchParams();
    formData.forEach(function (rawValue, key) {
      var value = rawValue == null ? "" : String(rawValue);
      if (value !== "") {
        params.append(key, value);
      }
    });

    var requestPath = params.toString() ? hxGet + "?" + params.toString() : hxGet;
    var ajaxOptions = {};
    var target = form.getAttribute("hx-target");
    var swap = form.getAttribute("hx-swap");
    var select = form.getAttribute("hx-select");

    if (target) ajaxOptions.target = target;
    if (swap) ajaxOptions.swap = swap;
    if (select) ajaxOptions.select = select;

    htmx.ajax("GET", requestPath, ajaxOptions);

    if (form.getAttribute("hx-push-url") === "true") {
      history.replaceState({}, "", requestPath);
    }
  } else {
    var submitter = form.querySelector("button[type='submit'], input[type='submit']");
    if (submitter) {
      submitter.click();
    } else if (typeof form.requestSubmit === "function") {
      form.requestSubmit();
    } else {
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    }
  }

  window.setTimeout(function () {
    input.focus();
    _maybeOpenSearchFieldHistory(root, true);
  }, 0);
}

function _selectSearchHistoryTerm(root, term) {
  if (!root || !root.querySelector) {
    return;
  }

  var input = root.querySelector("[data-search-field-input]");
  if (!input) {
    return;
  }

  input.value = term;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
  _rememberSearchFieldTerm(root, term);
  _hideSearchFieldHistory(root);
  input.focus();

  var mode = (root.getAttribute("data-search-field-mode") || "submit").toLowerCase();
  if (mode === "local") {
    return;
  }

  _submitSearchFieldForm(root, input);
}

function clearSearchField(button) {
  if (!button || !button.closest) {
    return;
  }

  var root = button.closest("[data-search-field]");
  var input = root ? root.querySelector("[data-search-field-input]") : null;
  if (!input) {
    return;
  }

  var mode = (root.getAttribute("data-search-field-mode") || "submit").toLowerCase();

  var hadValue = (input.value || "").trim().length > 0;
  input.value = "";
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));

  if (!hadValue) {
    input.focus();
    return;
  }

  if (mode === "local") {
    input.focus();
    syncSearchFieldState(input);
    _maybeOpenSearchFieldHistory(root, true);
    return;
  }

  _resetRemoteSearchField(root, input);
}

function handleSearchFieldInput(source) {
  if (!source || !source.closest) {
    return;
  }

  var root = source.closest("[data-search-field]");
  if (!root) {
    return;
  }

  var mode = (root.getAttribute("data-search-field-mode") || "submit").toLowerCase();
  var nextValue = (source.value || "").trim();
  var previousValue = root.__searchFieldLastValue || "";
  root.__searchFieldLastValue = nextValue;

  if (nextValue === "") {
    _maybeOpenSearchFieldHistory(root, true);
  } else {
    _hideSearchFieldHistory(root);
  }

  if (mode !== "remote") {
    return;
  }

  if (nextValue === "") {
    _cancelSearchFieldTimer(root);
    if (previousValue !== "") {
      _resetRemoteSearchField(root, source);
    }
    return;
  }

  _cancelSearchFieldTimer(root);

  var debounceMs = Number(root.getAttribute("data-search-field-debounce") || 300);
  if (!Number.isFinite(debounceMs) || debounceMs < 0) {
    debounceMs = 300;
  }

  root.__searchFieldTimer = window.setTimeout(function () {
    _submitSearchFieldForm(root, source, "preview");
  }, debounceMs);
}

function handleSearchFieldKeydown(event) {
  if (!event || !event.target || !event.target.closest) {
    return;
  }

  var root = event.target.closest("[data-search-field]");
  if (!root) {
    return;
  }

  var mode = (root.getAttribute("data-search-field-mode") || "submit").toLowerCase();

  if (event.key === "Escape") {
    _hideSearchFieldHistory(root);
    return;
  }

  if (mode === "remote" && event.key === "Enter") {
    _cancelSearchFieldTimer(root);
  }

  if (mode === "local" && event.key === "Enter") {
    _rememberSearchFieldTerm(root, event.target.value || "");
    _hideSearchFieldHistory(root);
  }
}

function handleSearchFieldFocus(source) {
  if (!source || !source.closest) {
    return;
  }

  var root = source.closest("[data-search-field]");
  if (!root) {
    return;
  }

  _maybeOpenSearchFieldHistory(root, true);
}

function handleSearchFieldBlur(source) {
  if (!source || !source.closest) {
    return;
  }

  var root = source.closest("[data-search-field]");
  if (!root) {
    return;
  }

  window.setTimeout(function () {
    if (root.contains(document.activeElement)) {
      return;
    }

    var input = root.querySelector("[data-search-field-input]");
    var mode = (root.getAttribute("data-search-field-mode") || "submit").toLowerCase();
    if (mode === "local" && input) {
      _rememberSearchFieldTerm(root, input.value || "");
    }
    _hideSearchFieldHistory(root);
  }, 0);
}

function syncSearchFieldState(source) {
  if (!source || !source.closest) {
    return;
  }

  var root = source.closest("[data-search-field]");
  if (!root) {
    return;
  }

  var input = root.querySelector("[data-search-field-input]");
  var clear = root.querySelector("[data-search-field-clear]");
  if (!input || !clear) {
    return;
  }

  var show = (input.value || "").trim().length > 0;
  clear.style.display = show ? "" : "none";
  clear.setAttribute("aria-hidden", show ? "false" : "true");
}

function seedSearchFieldStates(root) {
  if (!root) {
    return;
  }

  var fields = [];
  if (root.matches && root.matches("[data-search-field]")) {
    fields.push(root);
  }
  if (root.querySelectorAll) {
    var found = root.querySelectorAll("[data-search-field]");
    for (var i = 0; i < found.length; i += 1) {
      fields.push(found[i]);
    }
  }

  for (var j = 0; j < fields.length; j += 1) {
    var field = fields[j];
    var input = field.querySelector("[data-search-field-input]");
    if (input) {
      syncSearchFieldState(input);
      field.__searchFieldLastValue = (input.value || "").trim();
    }

    _renderSearchFieldHistory(field);

    var form = input ? input.form || (input.closest ? input.closest("form") : null) : null;
    if (!form || field.__searchFieldFormBound) {
      continue;
    }

    form.addEventListener("submit", function (evt) {
      var anyField = evt.target.querySelector
        ? evt.target.querySelector("[data-search-field]")
        : null;
      if (!anyField) {
        return;
      }

      var mode = (anyField.getAttribute("data-search-field-mode") || "submit").toLowerCase();
      var activeInput = anyField.querySelector("[data-search-field-input]");

      _rememberSearchFieldTerm(anyField, activeInput ? activeInput.value || "" : "");
      _hideSearchFieldHistory(anyField);

      if (mode === "remote") {
        _cancelSearchFieldTimer(anyField);
      }
    });
    field.__searchFieldFormBound = true;
  }
}

function searchHistoryField(config) {
  var cfg = config || {};
  var storageKey = cfg.storageKey || "";
  var maxItems = Number(cfg.maxItems || _SEARCH_HISTORY_MAX_ITEMS);

  return {
    open: false,
    query: "",
    history: [],
    formListener: null,
    submitClickListener: null,
    boundForm: null,

    init: function () {
      var self = this;
      self.$nextTick(function () {
        self.refreshHistory();
        self.query = self.$refs.hiddenInput ? self.$refs.hiddenInput.value || "" : "";
        self.setEditorValue(self.query);
        self.syncHiddenValue(self.query);
        var form = self.currentForm();
        if (form) {
          self.bindForm(form);
        }
      });
    },

    bindForm: function (form) {
      if (!form || this.boundForm === form) {
        return;
      }
      var self = this;
      this.boundForm = form;
      this.formListener = this.rememberCurrentQuery.bind(this);
      this.submitClickListener = function (event) {
        var trigger = event && event.target && event.target.closest
          ? event.target.closest("button[type='submit'], input[type='submit']")
          : null;
        if (trigger) {
          self.rememberCurrentQuery();
        }
      };
      form.addEventListener("submit", this.formListener);
      form.addEventListener("click", this.submitClickListener, true);
    },

    currentForm: function () {
      if (this.$refs.hiddenInput && this.$refs.hiddenInput.form) {
        return this.$refs.hiddenInput.form;
      }
      if (this.$refs.editor && this.$refs.editor.closest) {
        return this.$refs.editor.closest("form");
      }
      return null;
    },

    refreshHistory: function () {
      this.history = storageKey ? _readSearchHistory(storageKey) : [];
    },

    syncHiddenValue: function (value) {
      if (!this.$refs.hiddenInput) {
        return;
      }
      this.$refs.hiddenInput.value = value != null ? value : this.query;
    },

    rememberCurrentQuery: function () {
      return this.rememberTerm(this.query);
    },

    rememberTerm: function (term) {
      if (!storageKey) {
        return;
      }
      this.history = _rememberSearchTerm(storageKey, term, maxItems);
    },

    editorHasFocus: function () {
      return !!(this.$refs.editor && document.activeElement === this.$refs.editor);
    },

    setEditorValue: function (value) {
      if (!this.$refs.editor) {
        return;
      }
      var nextValue = value || "";
      if (_searchFieldDisplayValue(this.$refs.editor) !== nextValue) {
        this.$refs.editor.textContent = nextValue;
      }
    },

    syncQueryFromEditor: function () {
      this.query = _searchFieldDisplayValue(this.$refs.editor);
      this.syncHiddenValue(this.query);
    },

    handleFocus: function () {
      this.refreshHistory();
      this.syncQueryFromEditor();
      if (this.history.length > 0 && this.query.trim() === "") {
        this.open = true;
      }
    },

    handleInput: function () {
      this.syncQueryFromEditor();
      if (this.query.trim() === "") {
        this.refreshHistory();
        this.open = this.editorHasFocus() && this.history.length > 0;
      } else {
        this.open = false;
      }
    },

    clearQuery: function () {
      if (!this.$refs.editor) {
        return;
      }
      var hadValue = this.query.trim() !== "";
      this.setEditorValue("");
      this.syncHiddenValue("");
      this.query = "";
      this.refreshHistory();
      this.open = this.history.length > 0;
      this.$refs.editor.focus();
      if (hadValue) {
        this.submitSearch();
      }
    },

    removeTerm: function (idx) {
      this.history.splice(idx, 1);
      _writeSearchHistory(storageKey, this.history);
      if (this.history.length === 0) {
        this.open = false;
      }
    },

    clearHistory: function () {
      this.history = [];
      _writeSearchHistory(storageKey, []);
      this.open = false;
    },

    selectTerm: function (term) {
      if (!this.$refs.editor) {
        return;
      }
      this.setEditorValue(term);
      this.syncHiddenValue(term);
      this.query = term;
      this.open = false;
      this.rememberTerm(term);
      this.submitSearch();
      this.$refs.editor.focus();
    },

    submitSearch: function () {
      var form = this.currentForm();
      if (!form) {
        return;
      }
      this.open = false;
      if (this.$refs.editor && typeof this.$refs.editor.blur === "function") {
        this.$refs.editor.blur();
      }
      if (typeof form.requestSubmit === "function") {
        form.requestSubmit();
        return;
      }
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    },
  };
}

function blocklistPage(config) {
  var cfg = config || {};

  return {
    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },

    dispatchToast: function (message, level) {
      if (typeof showToast === "function") {
        showToast({ message: message, level: level });
      }
    },

    currentPath: function () {
      return window.location.pathname + window.location.search;
    },

    refreshResults: function (path, pushUrl) {
      var target =
        document.getElementById("blocklist-results-body") ||
        document.getElementById("blocklist-results");
      var nextPath = path || this.currentPath();
      if (!target || typeof htmx === "undefined") {
        window.location.assign(nextPath);
        return;
      }

      htmx.ajax("GET", nextPath, {
        target: "#".concat(target.id),
        swap: "outerHTML",
      });
      if (pushUrl === true) {
        history.replaceState({}, "", nextPath);
      }
    },

    removeEntry: function (id, btn) {
      var self = this;
      pbConfirm({
        title: "Remove from Blocklist",
        message: "Remove this release from the blocklist? It will become eligible for download again.",
        confirmText: "Remove",
      }).then(function (ok) {
        if (!ok) return;

        btn.disabled = true;
        fetch("/api/v1/blocklist/" + id, {
          method: "DELETE",
          headers: { "X-CSRF-Token": self.csrfToken() },
        })
          .then(function (response) {
            if (!response.ok && response.status !== 204) {
              throw new Error("Failed to remove entry.");
            }

            var refreshUrl = new URL(window.location.href);
            var rowCount = document.querySelectorAll("#blocklist-results-body tr[id^='bl-row-']").length;
            if (rowCount <= 1) {
              refreshUrl.searchParams.delete("page");
            }

            self.dispatchToast("Entry removed from blocklist.", "success");
            self.refreshResults(refreshUrl.pathname + refreshUrl.search, true);
          })
          .catch(function (error) {
            btn.disabled = false;
            self.dispatchToast(error.message, "error");
          });
      });
    },

    clearEntries: function (btn) {
      var self = this;
      pbConfirm({
        title: "Clear Blocklist",
        message:
          "This will permanently remove every release from the blocklist. Blocked releases will become eligible again.",
        confirmText: "Clear Blocklist",
      }).then(function (ok) {
        if (!ok) return;

        btn.disabled = true;
        fetch("/api/v1/blocklist/clear", {
          method: "DELETE",
          headers: { "X-CSRF-Token": self.csrfToken() },
        })
          .then(function (response) {
            if (!response.ok) {
              throw new Error("Failed to clear blocklist.");
            }
            return response.json();
          })
          .then(function (data) {
            self.dispatchToast(
              "Cleared " + data.removed + " blocked release" + (data.removed !== 1 ? "s" : "") + ".",
              "success"
            );
            self.refreshResults("/blocklist", true);
          })
          .catch(function (error) {
            btn.disabled = false;
            self.dispatchToast(error.message, "error");
          });
      });
    },
  };
}

function postProcessingPage(config) {
  var cfg = config || {};

  return {
    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },

    dispatchToast: function (message, level) {
      if (typeof showToast === "function") {
        showToast({ message: message, level: level });
      }
    },

    currentPath: function () {
      return window.location.pathname + window.location.search;
    },

    refreshContent: function (path) {
      var target = document.getElementById("post-processing-content");
      if (!target || typeof htmx === "undefined") {
        window.location.assign(path || this.currentPath());
        return;
      }

      htmx.ajax("GET", path || this.currentPath(), {
        target: "#post-processing-content",
        swap: "outerHTML",
      });
    },

    refreshPanels: function () {
      this.refreshContent();
    },

    retryProcessing: function (id, btn) {
      var self = this;
      btn.disabled = true;
      fetch("/api/v1/downloads/" + id + "/retry-processing", {
        method: "POST",
        headers: { "X-CSRF-Token": self.csrfToken() },
      })
        .then(function (response) {
          if (!response.ok) {
            return response
              .json()
              .catch(function () {
                return {};
              })
              .then(function (data) {
                throw new Error(
                  data.detail || (data.error && data.error.message) || "Retry failed"
                );
              });
          }
          self.dispatchToast("Re-queued for processing.", "success");
          self.refreshContent("/post-processing?tab=queue");
        })
        .catch(function (error) {
          btn.disabled = false;
          self.dispatchToast(error.message, "error");
        });
    },

    blockFailedDownload: function (id, btn) {
      var self = this;
      pbConfirm({
        title: "Block Release",
        message: "Add this failed release to the blocklist? It won't appear in future search results.",
        confirmText: "Block",
      }).then(function (ok) {
        if (!ok) return;
        btn.disabled = true;
        _postBlocklist("/api/v1/downloads/" + id + "/blocklist", self.csrfToken())
          .then(function (result) {
            _markBlockedAction(btn);
            self.dispatchToast(
              result.alreadyBlocked ? "Already blocked." : "Release blocked.",
              "success"
            );
          })
          .catch(function (error) {
            btn.disabled = false;
            self.dispatchToast(error.message, "error");
          });
      });
    },

    removeHistory: function (id, btn) {
      var self = this;
      pbConfirm({
        title: "Remove History Entry",
        message: "Remove this post-processing record from history?",
        confirmText: "Remove",
      }).then(function (ok) {
        if (!ok) return;
        btn.disabled = true;
        fetch("/api/v1/downloads/" + id, {
          method: "DELETE",
          headers: { "X-CSRF-Token": self.csrfToken() },
        })
          .then(function (res) {
            if (!res.ok && res.status !== 204) {
              throw new Error("Failed to remove entry.");
            }
            self.dispatchToast("Removed from history.", "success");
            self.refreshContent();
          })
          .catch(function (err) {
            btn.disabled = false;
            self.dispatchToast(err.message, "error");
          });
      });
    },

    clearHistory: function (btn) {
      var self = this;
      pbConfirm({
        title: "Clear Post-Processing History",
        message:
          "This will permanently delete all imported and failed post-processing records. Active processing items will not be affected.",
        confirmText: "Clear History",
      }).then(function (ok) {
        if (!ok) return;
        btn.disabled = true;
        fetch("/api/v1/downloads/history/post-processing", {
          method: "DELETE",
          headers: { "X-CSRF-Token": self.csrfToken() },
        })
          .then(function (res) {
            if (!res.ok) {
              throw new Error("Failed to clear history.");
            }
            return res.json();
          })
          .then(function (data) {
            self.dispatchToast(
              "Cleared " + data.deleted + " history record" + (data.deleted !== 1 ? "s" : "") + ".",
              "success"
            );
            self.refreshContent("/post-processing?tab=history");
          })
          .catch(function (err) {
            btn.disabled = false;
            self.dispatchToast(err.message, "error");
          });
      });
    },
  };
}

function searchHistoryPage(config) {
  var cfg = config || {};

  return {
    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },

    dispatchToast: function (message, level) {
      if (typeof showToast === "function") {
        showToast({ message: message, level: level });
      }
    },

    currentPath: function () {
      return window.location.pathname + window.location.search;
    },

    refreshResults: function (path, pushUrl) {
      var target =
        document.getElementById("search-history-results-body") ||
        document.getElementById("search-history-results");
      var nextPath = path || this.currentPath();
      if (!target || typeof htmx === "undefined") {
        window.location.assign(nextPath);
        return;
      }

      htmx.ajax("GET", nextPath, {
        target: "#".concat(target.id),
        swap: "outerHTML",
      });
      if (pushUrl === true) {
        history.replaceState({}, "", nextPath);
      }
    },

    removeHistory: function (id, btn) {
      var self = this;
      pbConfirm({
        title: "Remove Search History Entry",
        message: "Remove this search log from history?",
        confirmText: "Remove",
      }).then(function (ok) {
        if (!ok) return;
        btn.disabled = true;
        fetch("/api/v1/search/history/" + id, {
          method: "DELETE",
          headers: { "X-CSRF-Token": self.csrfToken() },
        })
          .then(function (res) {
            if (!res.ok && res.status !== 204) {
              throw new Error("Failed to remove entry.");
            }
            var refreshUrl = new URL(window.location.href);
            var rowCount = document.querySelectorAll(
              "#search-history-results-body tr[id^='search-history-row-']"
            ).length;
            if (rowCount <= 1) {
              refreshUrl.searchParams.delete("page");
            }
            self.dispatchToast("Removed from history.", "success");
            self.refreshResults(refreshUrl.pathname + refreshUrl.search, true);
          })
          .catch(function (err) {
            btn.disabled = false;
            self.dispatchToast(err.message, "error");
          });
      });
    },

    clearHistory: function (btn) {
      var self = this;
      pbConfirm({
        title: "Clear Search History",
        message: "This will permanently delete all search history records.",
        confirmText: "Clear History",
      }).then(function (ok) {
        if (!ok) return;
        btn.disabled = true;
        fetch("/api/v1/search/history", {
          method: "DELETE",
          headers: { "X-CSRF-Token": self.csrfToken() },
        })
          .then(function (res) {
            if (!res.ok) {
              throw new Error("Failed to clear history.");
            }
            return res.json();
          })
          .then(function (data) {
            self.dispatchToast(
              "Cleared " + data.deleted + " history record" + (data.deleted !== 1 ? "s" : "") + ".",
              "success"
            );
            self.refreshResults("/search-history", true);
          })
          .catch(function (err) {
            btn.disabled = false;
            self.dispatchToast(err.message, "error");
          });
      });
    },
  };
}

function importHistoryPage(config) {
  var cfg = config || {};
  function buildHistoryPath() {
    var form = document.getElementById("import-history-filter-form");
    var params = new URLSearchParams();
    params.set("tab", "history");

    if (form && typeof FormData !== "undefined") {
      var formData = new FormData(form);
      formData.forEach(function (value, key) {
        if (key === "tab") {
          return;
        }
        var text = value == null ? "" : String(value);
        if (!text) {
          return;
        }
        if (key === "page" && text === "1") {
          return;
        }
        params.set(key, text);
      });
    } else {
      var current = new URLSearchParams(window.location.search || "");
      var sort = current.get("sort");
      var search = current.get("search");
      var page = current.get("page");
      if (sort) {
        params.set("sort", sort);
      }
      if (search) {
        params.set("search", search);
      }
      if (page && page !== "1") {
        params.set("page", page);
      }
    }

    if (!params.get("sort")) {
      params.set("sort", "-created_at");
    }

    return "/import?" + params.toString();
  }

  function syncHistoryUrl(path) {
    var nextPath = path || buildHistoryPath();
    if (window.history && typeof window.history.replaceState === "function") {
      window.history.replaceState({}, "", nextPath);
    }
    return nextPath;
  }

  return {
    deleteJobId: null,
    deleting: false,
    openLogPanels: {},

    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },

    dispatchToast: function (message, level) {
      if (typeof showToast === "function") {
        showToast({ message: message, level: level });
      }
    },

    currentPath: function () {
      return window.location.pathname + window.location.search;
    },

    historyPath: function () {
      return buildHistoryPath();
    },

    syncBrowserUrl: function (path) {
      return syncHistoryUrl(path);
    },

    buildResumeUrl: function (jobId, resumeStep, jobStatus, progressPhase) {
      var params = new URLSearchParams({ tab: "collection" });
      if (jobId != null) {
        params.set("resume_job_id", String(jobId));
      }

      var explicitStep = Number(resumeStep);
      if (Number.isFinite(explicitStep) && explicitStep > 0) {
        params.set("resume_step", String(explicitStep));
        return "/import?" + params.toString();
      }

      var status = String(jobStatus || "");
      var phase = String(progressPhase || "");
      var step = 1;
      if (
        status === "pending" ||
        status === "scanning" ||
        status === "pausing" ||
        status === "analyzing" ||
        status === "matching" ||
        status === "file_matching"
      ) {
        step = 2;
      } else if (status === "review") {
        step = wasImportReviewAdvanced(jobId) ? 3 : 2;
      } else if (
        status === "importing" ||
        status === "cancelling" ||
        status === "rolling_back" ||
        (status === "paused" && (phase === "importing" || phase === "rollback"))
      ) {
        step = 4;
      } else if (status === "paused") {
        step = 2;
      }
      params.set("resume_step", String(step));
      return "/import?" + params.toString();
    },

    resumeActiveImport: function (jobId, resumeStep, jobStatus, progressPhase) {
      window.location.assign(
        this.buildResumeUrl(jobId, resumeStep, jobStatus, progressPhase)
      );
    },

    retryJob: async function (jobId) {
      try {
        var response = await fetch("/api/v1/import/" + jobId + "/retry", {
          method: "POST",
          headers: { "X-CSRF-Token": this.csrfToken() },
        });

        if (!response.ok) {
          var error = await response.json().catch(function () {
            return {};
          });
          throw new Error(error.detail || "Failed to start a fresh retry.");
        }

        var data = await response.json();
        if (data && typeof data.redirect_url === "string" && data.redirect_url.length > 0) {
          window.location.assign(data.redirect_url);
          return;
        }

        if (data && data.job_id != null) {
          window.location.assign(
            "/import?tab=collection&resume_job_id=" +
              encodeURIComponent(data.job_id) +
              "&resume_step=2",
          );
          return;
        }

        throw new Error("Retry did not return a new import job.");
      } catch (error) {
        this.dispatchToast(error.message || "Failed to start a fresh retry.", "error");
      }
    },

    rollbackJob: async function (jobId) {
      var confirmed = await pbConfirm({
        title: "Rollback import",
        message:
          "This will undo the import and reopen the collection workspace while rollback runs.",
        confirmText: "Start rollback",
        destructive: true,
      });
      if (!confirmed) {
        return;
      }

      try {
        var response = await fetch("/api/v1/import/" + jobId + "/rollback", {
          method: "POST",
          headers: { "X-CSRF-Token": this.csrfToken() },
        });

        if (!response.ok) {
          var error = await response.json().catch(function () {
            return {};
          });
          throw new Error(error.detail || "Failed to start rollback.");
        }

        purgeImportClientState(jobId);
        window.location.assign(
          "/import?tab=collection&resume_job_id=" + encodeURIComponent(jobId) + "&resume_step=4",
        );
      } catch (error) {
        this.dispatchToast(error.message || "Failed to start rollback.", "error");
      }
    },

    openDeleteModal: function (jobId) {
      this.deleteJobId = jobId;
    },

    closeDeleteModal: function () {
      if (this.deleting) {
        return;
      }
      this.deleteJobId = null;
    },

    isLogPanelOpen: function (jobId) {
      return Boolean(this.openLogPanels[jobId]);
    },

    removeJobRow: function (jobId) {
      var row = document.getElementById("import-job-row-" + jobId);
      if (!row || !row.closest) {
        return;
      }
      var group = row.closest("tbody");
      if (group && group.parentNode) {
        group.parentNode.removeChild(group);
      }
    },

    syncClearHistoryButtonVisibility: function () {
      var clearButton = document.querySelector("[data-testid='import-history-clear']");
      var clearShell = document.querySelector("[data-testid='import-history-clear-shell']");
      if (!clearButton && !clearShell) {
        return;
      }

      var hasClearableRows = Boolean(
        document.querySelector("[data-import-history-clearable='true']")
      );
      if (clearButton) {
        clearButton.hidden = !hasClearableRows;
      }
      if (clearShell) {
        clearShell.hidden = !hasClearableRows;
      }
    },

    refreshResults: function (path) {
      var target = document.getElementById("import-history-results");
      var nextPath = path || buildHistoryPath();
      this.openLogPanels = {};

      if (!target || typeof htmx === "undefined") {
        window.location.assign(nextPath);
        return;
      }

      syncHistoryUrl(nextPath);
      htmx.ajax("GET", nextPath, {
        target: "#import-history-results",
        swap: "outerHTML",
      });
    },

    refreshPanel: function (path) {
      var target = document.getElementById("import-history-page");
      var nextPath = path || buildHistoryPath();
      this.openLogPanels = {};

      if (!target || typeof htmx === "undefined") {
        window.location.assign(nextPath);
        return;
      }

      syncHistoryUrl(nextPath);
      htmx.ajax("GET", nextPath, {
        target: "#import-history-page",
        swap: "outerHTML",
      });
    },

    toggleLogPanel: function (jobId) {
      var panel = document.getElementById("log-panel-" + jobId);
      if (!panel) {
        return;
      }

      if (this.isLogPanelOpen(jobId)) {
        panel.innerHTML = "";
        this.openLogPanels[jobId] = false;
        return;
      }

      if (typeof htmx === "undefined") {
        window.location.assign("/import?tab=history");
        return;
      }

      htmx.ajax("GET", "/import/" + jobId + "/log-panel", {
        target: "#log-panel-" + jobId,
        swap: "innerHTML",
      });
      this.openLogPanels[jobId] = true;
    },

    deleteJob: function () {
      var self = this;
      if (!self.deleteJobId || self.deleting) {
        return;
      }

      self.deleting = true;
      fetch("/api/v1/import/" + self.deleteJobId, {
        method: "DELETE",
        headers: { "X-CSRF-Token": self.csrfToken() },
      })
        .then(function (response) {
          if (response.status === 202) {
            return response.json().then(function (data) {
              return {
                rollbackPending: true,
                message:
                  data.message ||
                  "Rollback is still finishing. This import remains in history.",
              };
            });
          }
          if (response.ok || response.status === 204) {
            return { rollbackPending: false };
          }
          return response
            .json()
            .catch(function () {
              return {};
            })
            .then(function (data) {
              throw new Error(data.detail || "Unable to delete this import job right now.");
            });
        })
        .then(function (result) {
          var deletedJobId = self.deleteJobId;
          self.deleteJobId = null;
          self.deleting = false;
          if (result && result.rollbackPending) {
            self.dispatchToast(result.message, "info");
            self.refreshResults(buildHistoryPath());
            return;
          }
          self.dispatchToast("Import job deleted.", "success");
          self.removeJobRow(deletedJobId);
          self.syncClearHistoryButtonVisibility();
          self.refreshResults(buildHistoryPath());
        })
        .catch(function (error) {
          self.deleting = false;
          self.dispatchToast(error.message, "error");
        });
    },

    clearHistory: function (btn) {
      var self = this;
      pbConfirm({
        title: "Clear Import History",
        message:
          "This will permanently delete completed, failed, and cancelled import records. Active imports will not be affected.",
        confirmText: "Clear History",
      }).then(function (ok) {
        if (!ok) return;
        btn.disabled = true;
        fetch("/api/v1/import/history", {
          method: "DELETE",
          headers: { "X-CSRF-Token": self.csrfToken() },
        })
          .then(function (res) {
            if (!res.ok) {
              throw new Error("Failed to clear history.");
            }
            return res.json();
          })
          .then(function (data) {
            self.dispatchToast(
              "Cleared " + data.deleted + " history record" + (data.deleted !== 1 ? "s" : "") + ".",
              "success"
            );
            self.refreshPanel(buildHistoryPath());
          })
          .catch(function (err) {
            btn.disabled = false;
            self.dispatchToast(err.message, "error");
          });
      });
    },
  };
}

function interventionPage() {
  var cfg = arguments.length > 0 && arguments[0] ? arguments[0] : {};
  return {
    selectedIds: [],
    selectionAnchorId: null,
    toolbarMode: "browse",
    bulkActionBusy: null,
    selectAllMatchingBusy: false,
    bulkActionsEnabled: cfg.bulkActionsEnabled !== false,
    totalMatchingCount: Number(cfg.totalMatchingCount || 0),
    selectionFilterSignature: "",
    afterSettleHandler: null,

    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },

    dispatchToast: function (message, level) {
      if (typeof showToast === "function") {
        showToast({ message: message, level: level });
      }
    },

    currentPath: function () {
      return window.location.pathname + window.location.search;
    },

    refreshContent: function (path, pushUrl) {
      var target = document.getElementById("intervention-page");
      var nextPath = path || this.currentPath();
      if (!target || typeof htmx === "undefined") {
        window.location.assign(nextPath);
        return;
      }

      htmx.ajax("GET", nextPath, {
        target: "#intervention-page",
        swap: "outerHTML",
      });
      if (pushUrl === true) {
        history.replaceState({}, "", nextPath);
      }
    },

    syncTotalMatchingCount: function () {
      var queueResults = document.getElementById("intervention-queue-results");
      if (!queueResults) {
        return;
      }
      var total = Number(queueResults.getAttribute("data-intervention-total") || "0");
      if (Number.isFinite(total)) {
        this.totalMatchingCount = total;
      }
    },

    queueFilterSignature: function () {
      var params = new URLSearchParams(window.location.search);
      var signature = new URLSearchParams();
      ["reason", "confidence", "protocol", "search"].forEach(function (key) {
        var value = params.get(key);
        if (value) {
          signature.set(key, value);
        }
      });
      return signature.toString();
    },

    resetSelectionForFilterChange: function () {
      var nextSignature = this.queueFilterSignature();
      if (
        this.selectionFilterSignature &&
        this.selectionFilterSignature !== nextSignature &&
        this.selectedIds.length > 0
      ) {
        this.clearSelection();
      }
      this.selectionFilterSignature = nextSignature;
    },

    init: function () {
      var self = this;
      if (Array.isArray(window._interventionSelectedIds)) {
        self.selectedIds = window._interventionSelectedIds.slice();
      }
      self.selectionFilterSignature = self.queueFilterSignature();

      self.afterSettleHandler = function (evt) {
        if (!evt || !evt.detail || !evt.detail.target) {
          return;
        }
        var targetId = evt.detail.target.id;
        if (
          targetId !== "intervention-list" &&
          targetId !== "intervention-page" &&
          targetId !== "intervention-queue-results"
        ) {
          return;
        }
        window.requestAnimationFrame(function () {
          if (Array.isArray(window._interventionSelectedIds)) {
            self.selectedIds = window._interventionSelectedIds.slice();
          }
          self.syncTotalMatchingCount();
          if (targetId === "intervention-page") {
            var hasQueueResults =
              document.getElementById("intervention-list") ||
              document.getElementById("intervention-queue-results");
            var hasHistoryPanel = document.querySelector(
              "[data-testid='intervention-history-panel']"
            );
            if (!hasQueueResults && hasHistoryPanel) {
              self.clearSelection();
              return;
            }
            if (!hasQueueResults) {
              return;
            }
          }
          self.resetSelectionForFilterChange();
          self.pruneSelection();
          self.syncSelectionUi();
        });
      };
      document.body.addEventListener("htmx:afterSettle", self.afterSettleHandler);

      self.syncTotalMatchingCount();
      self.pruneSelection();
      self.syncSelectionUi();
    },

    destroy: function () {
      if (this.afterSettleHandler) {
        document.body.removeEventListener("htmx:afterSettle", this.afterSettleHandler);
      }
    },

    persistSelection: function () {
      window._interventionSelectedIds = this.selectedIds.slice();
    },

    visibleIds: function () {
      var list = document.getElementById("intervention-list");
      if (!list) {
        return [];
      }

      return Array.prototype.map.call(
        list.querySelectorAll("[data-intervention-id]"),
        function (el) {
          return parseInt(el.getAttribute("data-intervention-id"), 10);
        }
      ).filter(function (id) {
        return !isNaN(id);
      });
    },

    selectModeButtonLabel: function () {
      return this.selectedIds.length > 0 ? "Select (" + this.selectedIds.length + ")" : "Select";
    },

    canEnterSelectMode: function () {
      return this.bulkActionsEnabled && this.totalMatchingCount > 0;
    },

    enterSelectMode: function () {
      if (!this.canEnterSelectMode()) {
        return;
      }
      this.toolbarMode = "select";
    },

    doneSelectMode: function () {
      this.toolbarMode = "browse";
      this.clearSelection();
    },

    exitSelectMode: function () {
      this.toolbarMode = "browse";
    },

    allMatchingSelected: function () {
      return this.totalMatchingCount > 0 && this.selectedIds.length >= this.totalMatchingCount;
    },

    hasMoreMatchingThanVisible: function () {
      var visibleIds = this.visibleIds();
      return this.totalMatchingCount > 0 && this.totalMatchingCount > visibleIds.length;
    },

    allVisibleSelected: function () {
      var visibleIds = this.visibleIds();
      return (
        visibleIds.length > 0 &&
        visibleIds.every(function (id) {
          return this.isSelected(id);
        }, this)
      );
    },

    selectAllVisible: function () {
      this.setAllSelection(this.visibleIds(), true);
    },

    selectAllMatching: async function () {
      if (this.selectAllMatchingBusy) {
        return;
      }

      this.selectAllMatchingBusy = true;
      try {
        var url = new URL("/intervention/selection-ids", window.location.origin);
        var params = new URLSearchParams(window.location.search);
        var reason = params.get("reason");
        var confidence = params.get("confidence");
        var protocol = params.get("protocol");
        var search = params.get("search");
        if (reason) {
          url.searchParams.set("reason", reason);
        }
        if (confidence) {
          url.searchParams.set("confidence", confidence);
        }
        if (protocol) {
          url.searchParams.set("protocol", protocol);
        }
        if (search) {
          url.searchParams.set("search", search);
        }

        var response = await fetch(url.toString(), {
          headers: { "X-Requested-With": "XMLHttpRequest" },
        });
        if (!response.ok) {
          throw new Error("Selection fetch failed");
        }

        var payload = await response.json();
        var ids = Array.isArray(payload.ids)
          ? payload.ids
              .map(function (id) {
                return Number(id);
              })
              .filter(function (id) {
                return Number.isFinite(id);
              })
          : [];
        this.selectedIds = ids;
        this.selectionAnchorId = null;
        this.selectionFilterSignature = this.queueFilterSignature();
        if (Number.isFinite(payload.total)) {
          this.totalMatchingCount = payload.total;
        }
        this.persistSelection();
        this.syncSelectionUi();
      } catch (_) {
        this.dispatchToast("Unable to select all matching releases right now", "error");
      } finally {
        this.selectAllMatchingBusy = false;
      }
    },

    syncSelectionUi: function () {
      var list = document.getElementById("intervention-list");
      var visibleIds = this.visibleIds();

      if (!this.canEnterSelectMode() && this.toolbarMode === "select") {
        this.toolbarMode = "browse";
      }

      if (list) {
        Array.prototype.forEach.call(
          list.querySelectorAll("[data-intervention-id]"),
          function (el) {
            var id = parseInt(el.getAttribute("data-intervention-id"), 10);
            el.checked = !isNaN(id) && this.isSelected(id);
          }.bind(this)
        );
      }

      var selectAll = document.querySelector("[data-testid='intervention-select-all']");
      if (selectAll) {
        selectAll.checked = this.allVisibleSelected();
      }

      var selectionCount = document.querySelector("[data-testid='intervention-selection-count']");
      if (selectionCount) {
        selectionCount.textContent = this.selectedIds.length + " selected";
        selectionCount.style.display = "";
      }

      var selectModeToggle = document.querySelector(
        "[data-testid='intervention-select-mode-toggle']"
      );
      if (selectModeToggle) {
        selectModeToggle.disabled = !this.canEnterSelectMode();
      }

      var approveButton = document.querySelector("[data-testid='intervention-bulk-approve']");
      if (approveButton) {
        approveButton.disabled = Boolean(this.bulkActionBusy) || this.selectedIds.length === 0;
      }

      var rejectButton = document.querySelector("[data-testid='intervention-bulk-reject']");
      if (rejectButton) {
        rejectButton.disabled = Boolean(this.bulkActionBusy) || this.selectedIds.length === 0;
      }
    },

    isSelected: function (id) {
      return this.selectedIds.indexOf(id) !== -1;
    },

    clearSelection: function () {
      this.selectedIds = [];
      this.selectionAnchorId = null;
      this.persistSelection();
      this.syncSelectionUi();
    },

    handleSelectionClick: function (id, checked, shiftKey, additiveKey) {
      var resolved = window.pbResolveCheckboxSelection({
        selectedIds: this.selectedIds,
        visibleIds: this.visibleIds(),
        itemId: id,
        anchorId: this.selectionAnchorId,
        checked: checked,
        shiftKey: shiftKey,
        additiveKey: additiveKey,
      });
      this.selectedIds = resolved.selectedIds;
      this.selectionAnchorId = resolved.anchorId;
      this.toolbarMode = "select";
      this.selectionFilterSignature = this.queueFilterSignature();
      this.persistSelection();
      this.syncSelectionUi();
    },

    setAllSelection: function (ids, checked) {
      if (!checked) {
        this.clearSelection();
        return;
      }
      var merged = this.selectedIds.slice();
      ids.forEach(function (id) {
        if (merged.indexOf(id) === -1) {
          merged.push(id);
        }
      });
      this.selectedIds = merged;
      this.selectionAnchorId = null;
      this.toolbarMode = "select";
      this.selectionFilterSignature = this.queueFilterSignature();
      this.persistSelection();
      this.syncSelectionUi();
    },

    deselectAll: function () {
      this.clearSelection();
      this.exitSelectMode();
    },

    pruneSelection: function () {
      var list = document.getElementById("intervention-list");
      if (!list) {
        this.clearSelection();
        return;
      }

      if (list.querySelector("[data-testid='intervention-empty']")) {
        this.clearSelection();
        return;
      }
      this.persistSelection();
      this.syncSelectionUi();
    },

    removeSelection: function (id) {
      var numericId = Number(id);
      if (!Number.isFinite(numericId)) {
        return;
      }
      this.selectedIds = this.selectedIds.filter(function (itemId) {
        return itemId !== numericId;
      });
      if (String(this.selectionAnchorId) === String(numericId)) {
        this.selectionAnchorId = null;
      }
      this.persistSelection();
      this.syncSelectionUi();
    },

    removeHistory: function (id, btn) {
      var self = this;
      pbConfirm({
        title: "Remove Intervention History Entry",
        message: "Remove this resolved intervention entry from history?",
        confirmText: "Remove",
      }).then(function (ok) {
        if (!ok) return;
        btn.disabled = true;
        fetch("/api/v1/intervention/history/" + id, {
          method: "DELETE",
          headers: { "X-CSRF-Token": self.csrfToken() },
        })
          .then(function (res) {
            if (!res.ok && res.status !== 204) {
              throw new Error("Failed to remove entry.");
            }
            self.dispatchToast("Removed from history.", "success");
            self.refreshContent(self.currentPath(), true);
          })
          .catch(function (err) {
            btn.disabled = false;
            self.dispatchToast(err.message, "error");
          });
      });
    },

    clearHistory: function (btn) {
      var self = this;
      pbConfirm({
        title: "Clear Intervention History",
        message: "This will permanently delete all approved, rejected, and expired intervention records. Pending matches will not be affected.",
        confirmText: "Clear History",
      }).then(function (ok) {
        if (!ok) return;
        btn.disabled = true;
        fetch("/api/v1/intervention/history", {
          method: "DELETE",
          headers: { "X-CSRF-Token": self.csrfToken() },
        })
          .then(function (res) {
            if (!res.ok) {
              throw new Error("Failed to clear history.");
            }
            return res.json();
          })
          .then(function (data) {
            self.dispatchToast(
              "Cleared " + data.deleted + " history record" + (data.deleted !== 1 ? "s" : "") + ".",
              "success"
            );
            self.refreshContent("/intervention?tab=history", true);
          })
          .catch(function (err) {
            btn.disabled = false;
            self.dispatchToast(err.message, "error");
          });
      });
    },
  };
}

function orphanedSeriesPage(config) {
  var cfg = config || {};

  return {
    dismissingIds: {},

    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },

    dispatchToast: function (message, level) {
      if (typeof showToast === "function") {
        showToast({ message: message, level: level });
      }
    },

    currentPath: function () {
      return window.location.pathname + window.location.search;
    },

    refreshResults: function (path) {
      var target = document.getElementById("import-orphaned-results");
      var nextPath = path || this.currentPath();
      if (!target || typeof htmx === "undefined") {
        window.location.assign(nextPath);
        return;
      }

      htmx.ajax("GET", nextPath, {
        target: "#import-orphaned-results",
        swap: "outerHTML",
      });
    },

    closeOrphanedModal: function () {
      var host = document.getElementById("cv-search-modal");
      if (host) {
        host.innerHTML = "";
      }
    },

    closeCvSearchModal: function () {
      this.closeOrphanedModal();
    },

    searchCv: function (importedSeriesId, query) {
      if (typeof htmx === "undefined") {
        window.location.assign("/import?tab=unmatched");
        return;
      }

      htmx.ajax(
        "GET",
        "/import/orphaned/" + importedSeriesId + "/cv-search?q=" + encodeURIComponent(query || ""),
        { target: "#cv-search-modal", swap: "innerHTML" }
      );
    },

    openRecovery: function (importedSeriesId) {
      if (typeof htmx === "undefined") {
        window.location.assign("/import?tab=unmatched");
        return;
      }

      htmx.ajax("GET", "/import/orphaned/" + importedSeriesId + "/recovery", {
        target: "#cv-search-modal",
        swap: "innerHTML",
      });
    },

    isDismissingOrphan: function (importedSeriesId) {
      return !!this.dismissingIds[importedSeriesId];
    },

    setDismissingOrphan: function (importedSeriesId, dismissing) {
      var next = Object.assign({}, this.dismissingIds);
      if (dismissing) {
        next[importedSeriesId] = true;
      } else {
        delete next[importedSeriesId];
      }
      this.dismissingIds = next;
    },

    dismissOrphan: async function (importedSeriesId, seriesName, message) {
      var self = this;
      if (self.isDismissingOrphan(importedSeriesId)) {
        return;
      }

      var confirmed = await pbConfirm({
        title: "Dismiss unmatched series",
        message: message || "Hide this series from the active unmatched queue?",
        confirmText: "Dismiss",
        destructive: true,
      });
      if (!confirmed) {
        return;
      }

      self.setDismissingOrphan(importedSeriesId, true);
      try {
        var response = await fetch("/api/v1/import/orphaned/" + importedSeriesId + "/dismiss", {
          method: "POST",
          headers: {
            "X-CSRF-Token": self.csrfToken(),
          },
        });
        if (!response.ok) {
          var data = await response.json().catch(function () {
            return {};
          });
          throw new Error(data.detail || "Failed to dismiss unmatched series.");
        }

        self.dispatchToast(
          (seriesName || "Series") + " dismissed.",
          "success"
        );
        self.refreshResults();
      } catch (error) {
        self.dispatchToast(
          (error && error.message) || "Failed to dismiss unmatched series.",
          "error"
        );
      } finally {
        self.setDismissingOrphan(importedSeriesId, false);
      }
    },

    assignCv: function (importedSeriesId, cvId) {
      var self = this;
      fetch("/api/v1/import/orphaned/" + importedSeriesId + "/assign", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": self.csrfToken(),
        },
        body: JSON.stringify({ cv_id: cvId }),
      })
        .then(function (response) {
          if (response.ok) {
            return response.json().catch(function () {
              return {};
            });
          }
          return response
            .json()
            .catch(function () {
              return {};
            })
            .then(function (data) {
              throw new Error(data.detail || "Failed to assign series.");
            });
        })
        .then(function (data) {
          self.closeOrphanedModal();
          self.dispatchToast(
            "Identified " +
              ((data && data.cv_title) || "series") +
              ". Finish the file recovery to remove it from Unmatched.",
            "success"
          );
          self.refreshResults();
          self.openRecovery(importedSeriesId);
        })
        .catch(function (error) {
          self.dispatchToast(error.message, "error");
        });
    },
  };
}

function importReconcileModal(config) {
  var cfg = config || {};

  return {
    open: true,
    saving: false,
    skippedFileIds: {},
    provisionalFileIds: {},

    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },

    dispatchToast: function (message, level) {
      if (typeof showToast === "function") {
        showToast({ message: message, level: level });
      }
    },

    closeModal: function () {
      closeImportCvSearchModal();
    },

    isFileSkipped: function (fileId) {
      return !!this.skippedFileIds[fileId];
    },

    isFileProvisional: function (fileId) {
      return !!this.provisionalFileIds[fileId];
    },

    setFileSkipState: function (fileId, skipped) {
      var row = document.querySelector(
        "[data-reconcile-file-row][data-imported-file-id='" + fileId + "']",
      );
      var select = document.getElementById("import-reconcile-issue-" + fileId);
      var trigger = document.getElementById("import-reconcile-issue-trigger-" + fileId);
      var next = Object.assign({}, this.skippedFileIds);

      if (skipped) {
        next[fileId] = true;
        if (row) {
          row.setAttribute("data-reconcile-local-action", "skip");
        }
        this.setFileProvisionalState(fileId, false);
      } else {
        delete next[fileId];
        if (row) {
          row.removeAttribute("data-reconcile-local-action");
        }
      }

      if (select) {
        select.disabled = skipped;
      }
      if (trigger) {
        trigger.disabled = skipped;
        trigger.setAttribute("aria-disabled", skipped ? "true" : "false");
      }
      this.skippedFileIds = next;
    },

    setFileProvisionalState: function (fileId, provisional) {
      var row = document.querySelector(
        "[data-reconcile-file-row][data-imported-file-id='" + fileId + "']",
      );
      var select = document.getElementById("import-reconcile-issue-" + fileId);
      var trigger = document.getElementById("import-reconcile-issue-trigger-" + fileId);
      var next = Object.assign({}, this.provisionalFileIds);

      if (provisional) {
        if (!row || row.getAttribute("data-can-create-provisional") !== "true") {
          return;
        }
        next[fileId] = true;
        if (row) {
          row.setAttribute("data-reconcile-local-action", "provisional");
        }
        if (this.isFileSkipped(fileId)) {
          this.setFileSkipState(fileId, false);
        }
      } else {
        delete next[fileId];
        if (row && row.getAttribute("data-reconcile-local-action") === "provisional") {
          row.removeAttribute("data-reconcile-local-action");
        }
      }

      if (select) {
        select.disabled = provisional;
      }
      if (trigger) {
        trigger.disabled = provisional;
        trigger.setAttribute("aria-disabled", provisional ? "true" : "false");
      }
      this.provisionalFileIds = next;
    },

    toggleSkip: function (fileId) {
      this.setFileSkipState(fileId, !this.isFileSkipped(fileId));
    },

    toggleProvisional: function (fileId) {
      this.setFileProvisionalState(fileId, !this.isFileProvisional(fileId));
    },

    clearSkip: function (fileId) {
      if (!this.isFileSkipped(fileId)) {
        this.setFileProvisionalState(fileId, false);
        return;
      }
      this.setFileSkipState(fileId, false);
      this.setFileProvisionalState(fileId, false);
    },

    collectDecisions: function () {
      var self = this;
      var rows = document.querySelectorAll("[data-reconcile-file-row]");
      var decisions = [];
      var missing = [];

      Array.prototype.forEach.call(rows, function (row) {
        var fileId = parseInt(row.getAttribute("data-imported-file-id") || "", 10);
        var locked = row.getAttribute("data-decision-locked") === "true";
        if (locked || !Number.isFinite(fileId)) {
          return;
        }

        var issueSelect = row.querySelector("[data-reconcile-issue-select]");
        if (self.isFileSkipped(fileId)) {
          decisions.push({
            imported_file_id: fileId,
            action: "skip",
          });
          return;
        }

        if (self.isFileProvisional(fileId)) {
          var provisionalIssueNumber = parseFloat(
            row.getAttribute("data-provisional-issue-number") || "",
          );
          decisions.push({
            imported_file_id: fileId,
            action: "provisional",
            provisional_issue_number: Number.isFinite(provisionalIssueNumber)
              ? provisionalIssueNumber
              : null,
          });
          return;
        }

        var issueCvId = issueSelect ? parseInt(issueSelect.value || "", 10) : NaN;
        if (Number.isFinite(issueCvId) && issueCvId > 0) {
          decisions.push({
            imported_file_id: fileId,
            action: "assign",
            issue_cv_id: issueCvId,
          });
          return;
        }

        var labelNode = row.querySelector(".downloads-release-name [data-tooltip-measure]");
        missing.push(labelNode ? labelNode.textContent || "file" : "file");
      });

      if (missing.length > 0) {
        return {
          error:
            "Every unresolved file needs an issue assignment or an explicit skip: " +
            missing.slice(0, 2).join(", "),
        };
      }

      if (decisions.length === 0) {
        return {
          error: "No unresolved files need reconciliation.",
        };
      }

      return {
        decisions: decisions,
      };
    },

    successMessage: function (data) {
      var status = data && data.status ? String(data.status) : "";
      if (status === "matched") {
        return "Reconciliation saved. This series is ready for import.";
      }
      if (status === "skipped") {
        return "Reconciliation saved. This row was skipped for this import.";
      }
      return "Reconciliation saved. Some files still need attention.";
    },

    save: function () {
      var self = this;
      if (self.saving) {
        return;
      }

      var payload = self.collectDecisions();
      if (!payload || payload.error) {
        self.dispatchToast(
          (payload && payload.error) || "Reconciliation choices are incomplete.",
          "error",
        );
        return;
      }

      self.saving = true;
      fetch(
        "/api/v1/import/" +
          cfg.jobId +
          "/series/" +
          cfg.importedSeriesId +
          "/reconcile",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": self.csrfToken(),
          },
          body: JSON.stringify({
            decisions: payload.decisions,
          }),
        },
      )
        .then(function (response) {
          if (response.ok) {
            return response.json();
          }
          return response
            .json()
            .catch(function () {
              return {};
            })
            .then(function (data) {
              throw new Error(data.detail || "Failed to save reconciliation.");
            });
        })
        .then(function (data) {
          window.dispatchEvent(new CustomEvent("import:review-summary-refresh"));
          window.dispatchEvent(new CustomEvent("import:review-refresh"));
          self.dispatchToast(self.successMessage(data), data.status === "matched" ? "success" : "info");
          self.open = false;
          self.closeModal();
        })
        .catch(function (error) {
          self.dispatchToast(error.message, "error");
        })
        .finally(function () {
          self.saving = false;
        });
    },
  };
}

function orphanedRecoveryModal(config) {
  var cfg = config || {};

  return {
    open: true,
    saving: false,
    recovering: false,
    skippedFileIds: {},
    recoveryPollTimer: null,
    recoveryState: "idle",
    recoveryMessage: "",
    recoveryCurrentFileName: "",
    recoveryCurrentFileStage: "",
    recoveryCurrentFileProgress: 0,
    recoveryCurrentFileProgressCurrent: null,
    recoveryCurrentFileProgressTotal: null,
    recoveryCurrentFileProgressUnit: "",
    recoveryFileIndex: null,
    recoveryTotalFiles: null,
    libraryRootId:
      cfg.selectedLibraryRootId != null && cfg.selectedLibraryRootId !== false
        ? String(cfg.selectedLibraryRootId)
        : "",

    init: function () {
      if (cfg.initialRecoveryProgress && typeof cfg.initialRecoveryProgress === "object") {
        this.applyRecoveryProgress(cfg.initialRecoveryProgress);
        if (this.recoveryState === "running") {
          this.beginRecoveryPolling();
        }
      }
    },

    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },

    dispatchToast: function (message, level) {
      if (typeof showToast === "function") {
        showToast({ message: message, level: level });
      }
    },

    closeOrphanedModal: function () {
      this.stopRecoveryPolling();
      var host = document.getElementById("cv-search-modal");
      if (host) {
        host.innerHTML = "";
      }
    },

    showRecoveryProgress: function () {
      return this.recovering || this.recoveryState === "running";
    },

    recoveryCurrentFileStageLabel: function () {
      var labels = {
        preparing: "Preparing file",
        extracting: "Extracting archive",
        rendering: "Rendering PDF pages",
        encoding: "Encoding pages",
        packing: "Packing CBZ",
        comicinfo_metadata: "Preparing ComicInfo metadata",
        transferring: "Transferring to library",
        rewriting: "Writing ComicInfo.xml",
        finalizing: "Finalizing imported file",
      };
      return labels[this.recoveryCurrentFileStage] || "Processing file";
    },

    recoveryCurrentFileDetailText: function () {
      if (
        this.recoveryCurrentFileProgressCurrent == null ||
        this.recoveryCurrentFileProgressTotal == null ||
        !this.recoveryCurrentFileProgressUnit
      ) {
        return "";
      }
      if (
        this.recoveryCurrentFileProgressUnit === "bytes" &&
        window._pb &&
        typeof window._pb.formatBytes === "function"
      ) {
        return (
          window._pb.formatBytes(this.recoveryCurrentFileProgressCurrent) +
          " / " +
          window._pb.formatBytes(this.recoveryCurrentFileProgressTotal)
        );
      }
      return (
        String(this.recoveryCurrentFileProgressCurrent) +
        " / " +
        String(this.recoveryCurrentFileProgressTotal) +
        " " +
        this.recoveryCurrentFileProgressUnit
      );
    },

    recoveryFileOrdinalText: function () {
      if (
        !Number.isFinite(Number(this.recoveryFileIndex)) ||
        !Number.isFinite(Number(this.recoveryTotalFiles)) ||
        Number(this.recoveryTotalFiles) <= 0
      ) {
        return "";
      }
      return (
        "File " +
        String(Number(this.recoveryFileIndex)) +
        " of " +
        String(Number(this.recoveryTotalFiles))
      );
    },

    applyRecoveryProgress: function (data) {
      var next = data || {};
      this.recoveryState = String(next.state || "idle");
      this.recoveryMessage = String(next.message || "");
      this.recoveryCurrentFileName = String(next.current_file_name || "");
      this.recoveryCurrentFileStage = String(next.current_file_stage || "");
      this.recoveryCurrentFileProgress =
        typeof next.current_file_progress_pct === "number" && !Number.isNaN(next.current_file_progress_pct)
          ? Math.max(0, Math.min(100, Math.round(next.current_file_progress_pct)))
          : 0;
      this.recoveryCurrentFileProgressCurrent =
        next.current_file_progress_current != null
          ? Number(next.current_file_progress_current)
          : null;
      this.recoveryCurrentFileProgressTotal =
        next.current_file_progress_total != null
          ? Number(next.current_file_progress_total)
          : null;
      this.recoveryCurrentFileProgressUnit = String(next.current_file_progress_unit || "");
      this.recoveryFileIndex =
        next.file_index != null ? Number(next.file_index) : null;
      this.recoveryTotalFiles =
        next.total_files != null ? Number(next.total_files) : null;
      this.recovering = this.recoveryState === "running";
    },

    stopRecoveryPolling: function () {
      if (this.recoveryPollTimer) {
        window.clearTimeout(this.recoveryPollTimer);
        this.recoveryPollTimer = null;
      }
    },

    scheduleRecoveryPoll: function (delayMs) {
      var self = this;
      self.stopRecoveryPolling();
      self.recoveryPollTimer = window.setTimeout(function () {
        self.recoveryPollTimer = null;
        self.pollRecoveryProgress();
      }, typeof delayMs === "number" ? delayMs : 400);
    },

    beginRecoveryPolling: function () {
      this.recovering = true;
      this.scheduleRecoveryPoll(200);
    },

    pollRecoveryProgress: function () {
      var self = this;
      fetch("/api/v1/import/orphaned/" + cfg.importedSeriesId + "/recover/progress", {
        method: "GET",
        headers: {
          "X-CSRF-Token": self.csrfToken(),
        },
      })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("Failed to load recovery progress.");
          }
          return response.json();
        })
        .then(function (data) {
          self.applyRecoveryProgress(data);
          if (self.recoveryState === "running") {
            self.scheduleRecoveryPoll(350);
            return;
          }
          self.handleTerminalRecoveryProgress(data);
        })
        .catch(function (error) {
          self.recovering = false;
          self.stopRecoveryPolling();
          self.dispatchToast(error.message || "Failed to load recovery progress.", "error");
        });
    },

    handleTerminalRecoveryProgress: function (data) {
      this.stopRecoveryPolling();
      this.recovering = false;

      if ((data && data.state) === "completed") {
        window.dispatchEvent(new CustomEvent("import:orphaned-refresh-results"));
        if ((data && data.result_status) === "imported" || Number(data.files_remaining || 0) === 0) {
          this.closeOrphanedModal();
          this.dispatchToast(
            "Recovery complete. The series has left the active unmatched queue.",
            "success"
          );
          return;
        }

        this.dispatchToast(
          "Recovery saved. Some files still need attention before this series is resolved.",
          "info"
        );
        this.reload();
        return;
      }

      if ((data && data.state) === "failed") {
        this.dispatchToast(
          (data && data.error_message) || "Recovery failed.",
          "error"
        );
        this.reload();
      }
    },

    isFileSkipped: function (fileId) {
      return !!this.skippedFileIds[fileId];
    },

    setFileSkipState: function (fileId, skipped) {
      var row = document.querySelector(
        "[data-recovery-file-row][data-imported-file-id='" + fileId + "']"
      );
      var select = document.getElementById("orphaned-recovery-issue-" + fileId);
      var trigger = document.getElementById("orphaned-recovery-issue-trigger-" + fileId);
      var next = Object.assign({}, this.skippedFileIds);

      if (skipped) {
        next[fileId] = true;
        if (row) {
          row.setAttribute("data-recovery-local-action", "skip");
        }
      } else {
        delete next[fileId];
        if (row) {
          row.removeAttribute("data-recovery-local-action");
        }
      }

      if (select) {
        select.disabled = skipped;
      }
      if (trigger) {
        trigger.disabled = skipped;
        trigger.setAttribute("aria-disabled", skipped ? "true" : "false");
      }
      this.skippedFileIds = next;
    },

    toggleSkip: function (fileId) {
      this.setFileSkipState(fileId, !this.isFileSkipped(fileId));
    },

    clearSkip: function (fileId) {
      if (!this.isFileSkipped(fileId)) {
        return;
      }
      this.setFileSkipState(fileId, false);
    },

    reload: function () {
      if (typeof htmx === "undefined") {
        window.location.assign("/import?tab=unmatched");
        return;
      }
      htmx.ajax("GET", "/import/orphaned/" + cfg.importedSeriesId + "/recovery", {
        target: "#cv-search-modal",
        swap: "innerHTML",
      });
    },

    collectDecisions: function () {
      var self = this;
      var rows = document.querySelectorAll("[data-recovery-file-row]");
      var decisions = [];
      var missing = [];
      var hasExistingImported = false;

      Array.prototype.forEach.call(rows, function (row) {
        var fileId = parseInt(row.getAttribute("data-imported-file-id") || "", 10);
        var status = row.getAttribute("data-status") || "";
        var locked = row.getAttribute("data-decision-locked") === "true";
        if (status === "imported") {
          hasExistingImported = true;
        }
        if (locked || !Number.isFinite(fileId)) {
          return;
        }

        var issueSelect = row.querySelector("[data-recovery-issue-select]");
        if (self.isFileSkipped(fileId)) {
          decisions.push({
            imported_file_id: fileId,
            action: "skip",
          });
          return;
        }

        var issueCvId = issueSelect ? parseInt(issueSelect.value || "", 10) : NaN;
        if (Number.isFinite(issueCvId) && issueCvId > 0) {
          decisions.push({
            imported_file_id: fileId,
            action: "assign",
            issue_cv_id: issueCvId,
          });
          return;
        }

        var labelNode = row.querySelector(".downloads-release-name [data-tooltip-measure]");
        missing.push(labelNode ? labelNode.textContent || "file" : "file");
      });

      if (missing.length > 0) {
        return {
          error:
            "Every remaining file needs an issue assignment or an explicit skip: " +
            missing.slice(0, 2).join(", "),
        };
      }

      var assignCount = decisions.filter(function (decision) {
        return decision.action === "assign";
      }).length;
      if (assignCount === 0 && !hasExistingImported) {
        return {
          error: "Assign at least one file before completing recovery.",
        };
      }

      return {
        decisions: decisions,
      };
    },

    save: function () {
      var self = this;
      if (self.saving || self.recovering) {
        return;
      }

      if (cfg.requiresLibraryRoot && !String(self.libraryRootId || "").trim()) {
        self.dispatchToast("Choose a library root before completing recovery.", "error");
        return;
      }

      var payload = self.collectDecisions();
      if (!payload || payload.error) {
        self.dispatchToast((payload && payload.error) || "Recovery choices are incomplete.", "error");
        return;
      }

      self.saving = true;
      fetch("/api/v1/import/orphaned/" + cfg.importedSeriesId + "/recover/start", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": self.csrfToken(),
        },
        body: JSON.stringify({
          target_library_root_id: cfg.requiresLibraryRoot && self.libraryRootId
            ? parseInt(self.libraryRootId, 10)
            : null,
          decisions: payload.decisions,
        }),
      })
        .then(function (response) {
          if (response.ok) {
            return response.json();
          }
          return response
            .json()
            .catch(function () {
              return {};
            })
            .then(function (data) {
              throw new Error(data.detail || "Failed to start orphan recovery.");
            });
        })
        .then(function (data) {
          self.applyRecoveryProgress(data);
          self.beginRecoveryPolling();
        })
        .catch(function (error) {
          self.dispatchToast(error.message, "error");
        })
        .finally(function () {
          self.saving = false;
        });
    },
  };
}

function readerMixin(config) {
  var cfg = config || {};
  var zoomSteps = [50, 67, 80, 100, 125, 150, 200, 300];

  function clampPage(index, count) {
    if (!count) return 0;
    return Math.max(0, Math.min(count - 1, index));
  }

  function isTypingTarget(target) {
    if (!target) return false;
    var tagName = String(target.tagName || "").toLowerCase();
    return (
      tagName === "input" ||
      tagName === "select" ||
      tagName === "textarea" ||
      target.isContentEditable
    );
  }

  function responseMessage(response, fallback) {
    return response
      .json()
      .catch(function () {
        return {};
      })
      .then(function (data) {
        if (typeof data.detail === "string") return data.detail;
        if (data.detail && typeof data.detail.message === "string") {
          return data.detail.message;
        }
        return fallback;
      });
  }

  return {
    readerOpen: false,
    readerLoading: false,
    readerPageLoading: false,
    readerFatalError: false,
    readerErrorTitle: "Pullbox could not open this comic.",
    readerErrorMessage: "",
    readerManifest: null,
    readerActiveIssueId: null,
    readerTitle: "",
    readerIssueLabel: "",
    readerIssueTransitioning: false,
    readerIssueStatusMessage: "",
    readerIssueSwitchError: "",
    readerCompletionVisible: false,
    readerCompletionUpdating: false,
    readerPageIndex: 0,
    readerPageCount: 0,
    readerPageDraft: "1",
    readerPageInputError: "",
    readerImageUrl: "",
    readerImageNaturalWidth: 0,
    readerImageNaturalHeight: 0,
    readerFitMode: "page",
    readerZoomPercent: 100,
    readerDirection: "ltr",
    readerControlsVisible: true,
    readerHelpVisible: false,
    readerFullscreenAvailable: false,
    readerFullscreenActive: false,
    readerProgressSaveFailed: false,
    readerLastSettledPage: null,
    readerLastSettledCompletion: false,
    readerRereadPending: false,
    readerLastSavedSignature: "",
    readerCurrentUserInitiated: false,
    readerFailedPageIndex: null,
    readerOpener: null,
    readerSavedScrollX: 0,
    readerSavedScrollY: 0,
    readerManifestController: null,
    readerProgressController: null,
    readerIssueGeneration: 0,
    readerLoadGeneration: 0,
    readerSettledTimer: null,
    readerControlsTimer: null,
    readerPrefetchTimer: null,
    readerPrefetchIdleHandle: null,
    readerPrefetchImages: [],
    readerPointer: null,
    readerVisibilityHandler: null,

    init: function () {
      var self = this;
      self.readerVisibilityHandler = function () {
        if (!self.readerOpen) return;
        if (document.visibilityState === "hidden") {
          self.clearReaderTimer("readerSettledTimer");
          self.saveReaderProgress(true).catch(function () {
            return null;
          });
          return;
        }
        if (!self.readerPageLoading && !self.readerFatalError && self.readerPageCount) {
          self.scheduleReaderSettled(
            self.readerPageIndex,
            self.readerCurrentUserInitiated
          );
        }
      };
      document.addEventListener("visibilitychange", self.readerVisibilityHandler);
      if (cfg.openReaderOnLoad && typeof self.$nextTick === "function") {
        cfg.openReaderOnLoad = false;
        self.$nextTick(function () {
          var opener = self.$root
            ? self.$root.querySelector("[data-testid='issue-action-read']")
            : null;
          if (!opener || !self.$refs.readerDialog) return;
          var currentUrl = new URL(window.location.href);
          currentUrl.searchParams.delete("read");
          window.history.replaceState(
            window.history.state,
            "",
            currentUrl.pathname + currentUrl.search + currentUrl.hash
          );
          self.openReader({ currentTarget: opener });
        });
      }
    },

    destroy: function () {
      if (this.readerVisibilityHandler) {
        document.removeEventListener("visibilitychange", this.readerVisibilityHandler);
      }
      this.clearReaderWork();
    },

    openReader: function (event) {
      var self = this;
      var dialog = self.$refs.readerDialog;
      if (!dialog || !cfg.readerManifestUrl || self.readerOpen) return;

      self.readerOpener = event && event.currentTarget ? event.currentTarget : document.activeElement;
      self.readerSavedScrollX = window.scrollX;
      self.readerSavedScrollY = window.scrollY;
      self.resetReaderSession();
      self.readerOpen = true;
      self.readerLoading = true;
      var fullscreenTarget = self.$refs.readerShell;
      self.readerFullscreenAvailable =
        !!fullscreenTarget && typeof fullscreenTarget.requestFullscreen === "function";
      dialog.showModal();
      self.showReaderControls();

      if (typeof self.$nextTick === "function") {
        self.$nextTick(function () {
          if (self.$refs.readerViewport) self.$refs.readerViewport.focus();
        });
      }

      var generation = self.readerIssueGeneration + 1;
      self.readerIssueGeneration = generation;
      self.fetchReaderManifest(cfg.readerManifestUrl, generation)
        .then(function (manifest) {
          if (!manifest) return false;
          return self.activateReaderManifest(manifest, false);
        })
        .catch(function (error) {
          if (error && error.name === "AbortError") return;
          self.readerLoading = false;
          self.readerPageLoading = false;
          self.readerFatalError = true;
          self.readerErrorMessage =
            (error && error.message) || "The comic could not be prepared.";
          self.showReaderControls();
        });
    },

    resetReaderSession: function () {
      this.clearReaderWork();
      this.readerFitMode = "page";
      this.readerZoomPercent = 100;
      this.readerDirection = "ltr";
      this.readerControlsVisible = true;
      this.readerHelpVisible = false;
      this.resetReaderIssue();
    },

    resetReaderIssue: function () {
      this.readerLoading = false;
      this.readerPageLoading = false;
      this.readerFatalError = false;
      this.readerErrorTitle = "Pullbox could not open this comic.";
      this.readerErrorMessage = "";
      this.readerManifest = null;
      this.readerActiveIssueId = null;
      this.readerTitle = "";
      this.readerIssueLabel = "";
      this.readerIssueTransitioning = false;
      this.readerIssueStatusMessage = "";
      this.readerIssueSwitchError = "";
      this.readerCompletionVisible = false;
      this.readerCompletionUpdating = false;
      this.readerPageIndex = 0;
      this.readerPageCount = 0;
      this.readerPageDraft = "1";
      this.readerPageInputError = "";
      this.readerImageUrl = "";
      this.readerImageNaturalWidth = 0;
      this.readerImageNaturalHeight = 0;
      this.readerProgressSaveFailed = false;
      this.readerLastSettledPage = null;
      this.readerLastSettledCompletion = false;
      this.readerRereadPending = false;
      this.readerLastSavedSignature = "";
      this.readerCurrentUserInitiated = false;
      this.readerFailedPageIndex = null;
      this.readerPointer = null;
    },

    clearReaderWork: function () {
      this.readerIssueGeneration += 1;
      this.clearReaderIssueWork();
      if (this.readerProgressController) {
        this.readerProgressController.abort();
        this.readerProgressController = null;
      }
    },

    clearReaderIssueWork: function () {
      if (this.readerManifestController) {
        this.readerManifestController.abort();
        this.readerManifestController = null;
      }
      this.readerLoadGeneration += 1;
      this.clearReaderTimer("readerSettledTimer");
      this.clearReaderTimer("readerControlsTimer");
      this.clearReaderTimer("readerPrefetchTimer");
      if (
        this.readerPrefetchIdleHandle !== null &&
        typeof window.cancelIdleCallback === "function"
      ) {
        window.cancelIdleCallback(this.readerPrefetchIdleHandle);
      }
      this.readerPrefetchIdleHandle = null;
      this.readerPrefetchImages = [];
    },

    clearReaderTimer: function (name) {
      if (this[name]) {
        window.clearTimeout(this[name]);
        this[name] = null;
      }
    },

    closeReader: function () {
      var self = this;
      if (!self.readerOpen) return;

      if (document.fullscreenElement) {
        Promise.resolve(document.exitFullscreen())
          .catch(function () {
            return null;
          })
          .then(function () {
            self.closeReaderDialog();
          });
        return;
      }
      self.closeReaderDialog();
    },

    closeReaderDialog: function () {
      var dialog = this.$refs.readerDialog;
      this.saveReaderProgress(true).catch(function () {
        return null;
      });
      this.readerOpen = false;
      this.clearReaderWork();
      if (dialog && dialog.open) dialog.close();
    },

    finishReaderClose: function () {
      var opener = this.readerOpener;
      var scrollX = this.readerSavedScrollX;
      var scrollY = this.readerSavedScrollY;
      this.readerOpen = false;
      window.requestAnimationFrame(function () {
        window.scrollTo(scrollX, scrollY);
        if (opener && document.contains(opener) && typeof opener.focus === "function") {
          opener.focus({ preventScroll: true });
        }
      });
    },

    retryReader: function () {
      var opener = this.readerOpener;
      this.closeReaderDialog();
      var self = this;
      window.setTimeout(function () {
        self.openReader({ currentTarget: opener });
      }, 0);
    },

    readerPageUrl: function (pageIndex) {
      if (!this.readerManifest) return "";
      return String(this.readerManifest.page_url_template).replace(
        "{page_index}",
        encodeURIComponent(String(pageIndex))
      );
    },

    fetchReaderManifest: async function (url, generation) {
      if (!url) throw new Error("The comic could not be prepared.");
      if (this.readerManifestController) this.readerManifestController.abort();
      var controller = new AbortController();
      this.readerManifestController = controller;
      try {
        var response = await fetch(url, {
          method: "GET",
          signal: controller.signal,
        });
        if (!response.ok) {
          var message = await responseMessage(response, "The comic could not be prepared.");
          throw new Error(message);
        }
        var manifest = await response.json();
        if (generation !== this.readerIssueGeneration || !this.readerOpen) return null;
        var pageCount = Number(manifest.page_count);
        if (!Number.isInteger(pageCount) || pageCount < 1 || !manifest.page_url_template) {
          throw new Error("This comic does not contain any readable pages.");
        }
        return manifest;
      } finally {
        if (this.readerManifestController === controller) {
          this.readerManifestController = null;
        }
      }
    },

    activateReaderManifest: async function (manifest, announce) {
      this.clearReaderIssueWork();
      this.resetReaderIssue();
      this.readerManifest = manifest;
      this.readerRereadPending = Boolean(
        manifest.state && manifest.state.completed_at
      );
      this.readerActiveIssueId = Number(manifest.issue_id) || null;
      this.readerTitle = String(manifest.title || cfg.seriesTitle || "Comic reader");
      this.readerIssueLabel = String(manifest.issue_label || cfg.issueLabel || "");
      this.readerPageCount = Number(manifest.page_count);
      this.readerLoading = true;
      var initialPage = clampPage(
        Number(manifest.initial_page_index) || 0,
        this.readerPageCount
      );
      var loaded = await this.loadReaderPage(initialPage, false);
      if (!loaded) return false;
      if (announce) {
        this.readerIssueStatusMessage =
          "Opened " + this.readerIssueLabel + ", page " +
          String(this.readerPageIndex + 1) + " of " + String(this.readerPageCount);
      }
      if (this.$refs.readerViewport) this.$refs.readerViewport.focus();
      return true;
    },

    loadReaderPage: function (pageIndex, userInitiated) {
      var self = this;
      if (!self.readerManifest || !self.readerOpen) return Promise.resolve(false);
      var nextIndex = clampPage(Number(pageIndex), self.readerPageCount);
      if (!Number.isInteger(nextIndex)) return Promise.resolve(false);

      self.clearReaderTimer("readerSettledTimer");
      self.readerLastSettledCompletion = false;
      self.readerPageLoading = true;
      self.readerFatalError = false;
      self.showReaderControls();
      var generation = self.readerLoadGeneration + 1;
      self.readerLoadGeneration = generation;
      var pageUrl = self.readerPageUrl(nextIndex);

      return new Promise(function (resolve) {
        var image = new Image();
        var settleLoadedImage = function () {
          if (generation !== self.readerLoadGeneration || !self.readerOpen) {
            resolve(false);
            return;
          }
          self.readerImageNaturalWidth = image.naturalWidth || 0;
          self.readerImageNaturalHeight = image.naturalHeight || 0;
          self.readerImageUrl = pageUrl;
          self.readerPageIndex = nextIndex;
          self.readerPageDraft = String(nextIndex + 1);
          self.readerPageLoading = false;
          self.readerLoading = false;
          self.readerFatalError = false;
          self.readerFailedPageIndex = null;
          self.readerCurrentUserInitiated = Boolean(userInitiated);
          if (self.$refs.readerViewport) {
            self.$refs.readerViewport.scrollTop = 0;
            self.$refs.readerViewport.scrollLeft = 0;
          }
          self.scheduleReaderSettled(nextIndex, Boolean(userInitiated));
          self.prefetchReaderNeighbors(nextIndex);
          self.scheduleReaderControlsHide();
          resolve(true);
        };

        image.onload = function () {
          if (typeof image.decode === "function") {
            image.decode().catch(function () {
              return null;
            }).then(settleLoadedImage);
            return;
          }
          settleLoadedImage();
        };
        image.onerror = function () {
          if (generation !== self.readerLoadGeneration || !self.readerOpen) {
            resolve(false);
            return;
          }
          self.readerLoading = false;
          self.readerPageLoading = false;
          self.readerFatalError = true;
          self.readerFailedPageIndex = nextIndex;
          self.readerErrorTitle = "Page " + (nextIndex + 1) + " could not be displayed.";
          self.readerErrorMessage =
            "Try this page again, navigate to another page, or download the original comic.";
          self.showReaderControls();
          resolve(false);
        };
        image.src = pageUrl;
      });
    },

    scheduleReaderSettled: function (pageIndex, userInitiated) {
      var self = this;
      self.clearReaderTimer("readerSettledTimer");
      self.readerSettledTimer = window.setTimeout(function () {
        self.readerSettledTimer = null;
        if (
          !self.readerOpen ||
          document.visibilityState !== "visible" ||
          self.readerPageIndex !== pageIndex ||
          self.readerPageLoading
        ) {
          return;
        }
        self.readerLastSettledPage = pageIndex;
        self.readerLastSettledCompletion =
          Boolean(userInitiated) && pageIndex === self.readerPageCount - 1;
        self.saveReaderProgress(false).catch(function () {
          return null;
        });
      }, 750);
    },

    saveReaderProgress: async function (keepalive) {
      var self = this;
      var manifest = self.readerManifest;
      if (
        !manifest ||
        self.readerLastSettledPage === null ||
        !manifest.progress_url ||
        !manifest.revision
      ) {
        return manifest && manifest.state ? manifest.state : null;
      }
      var payload = {
        revision: String(manifest.revision),
        page_index: self.readerLastSettledPage,
        page_count: self.readerPageCount,
        completion_candidate: Boolean(self.readerLastSettledCompletion),
        reread_started: Boolean(
          self.readerRereadPending &&
          self.readerLastSettledPage === 0 &&
          !self.readerLastSettledCompletion
        ),
      };
      var signature = JSON.stringify(payload);
      if (!self.readerProgressSaveFailed && signature === self.readerLastSavedSignature) {
        return manifest.state || null;
      }

      if (!keepalive && self.readerProgressController) {
        self.readerProgressController.abort();
      }
      var controller = keepalive ? null : new AbortController();
      if (controller) self.readerProgressController = controller;
      try {
        var response = await fetch(manifest.progress_url, {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": self.csrfToken(),
          },
          body: signature,
          keepalive: Boolean(keepalive),
          signal: controller ? controller.signal : undefined,
        });
        if (!response.ok) throw new Error("Reading position was not saved.");
        var canonical = await response.json();
        self.readerProgressSaveFailed = false;
        if (canonical.state) manifest.state = canonical.state;
        if (
          payload.reread_started &&
          canonical.state &&
          !canonical.state.completed_at
        ) {
          self.readerRereadPending = false;
          self.readerCompletionVisible = false;
          self.readerLastSavedSignature = JSON.stringify({
            revision: payload.revision,
            page_index: payload.page_index,
            page_count: payload.page_count,
            completion_candidate: payload.completion_candidate,
            reread_started: false,
          });
        } else {
          self.readerLastSavedSignature = signature;
        }
        if (
          payload.completion_candidate &&
          canonical.state &&
          canonical.state.completed_at
        ) {
          self.readerCompletionVisible = true;
          self.readerIssueStatusMessage = "Finished " + self.readerIssueLabel;
        }
        return canonical.state || canonical;
      } catch (error) {
        if (!error || error.name !== "AbortError") self.readerProgressSaveFailed = true;
        throw error;
      } finally {
        if (controller && self.readerProgressController === controller) {
          self.readerProgressController = null;
        }
      }
    },

    readerAdjacentIssue: function (direction) {
      if (!this.readerManifest) return null;
      return direction === "previous"
        ? this.readerManifest.previous_issue
        : this.readerManifest.next_issue;
    },

    readerIssueControlLabel: function (direction) {
      var adjacent = this.readerAdjacentIssue(direction);
      var action = direction === "previous" ? "Previous issue" : "Next issue";
      return adjacent && adjacent.issue_label
        ? action + ", " + String(adjacent.issue_label)
        : action;
    },

    readerPreviousIssue: function () {
      return this.switchReaderIssue("previous");
    },

    readerNextIssue: function () {
      return this.switchReaderIssue("next");
    },

    captureReaderIssue: function () {
      return {
        manifest: this.readerManifest,
        activeIssueId: this.readerActiveIssueId,
        title: this.readerTitle,
        issueLabel: this.readerIssueLabel,
        pageIndex: this.readerPageIndex,
        pageCount: this.readerPageCount,
        pageDraft: this.readerPageDraft,
        imageUrl: this.readerImageUrl,
        imageNaturalWidth: this.readerImageNaturalWidth,
        imageNaturalHeight: this.readerImageNaturalHeight,
        lastSettledPage: this.readerLastSettledPage,
        lastSettledCompletion: this.readerLastSettledCompletion,
        lastSavedSignature: this.readerLastSavedSignature,
        currentUserInitiated: this.readerCurrentUserInitiated,
        completionVisible: this.readerCompletionVisible,
        rereadPending: this.readerRereadPending,
      };
    },

    restoreReaderIssue: function (snapshot) {
      this.readerManifest = snapshot.manifest;
      this.readerActiveIssueId = snapshot.activeIssueId;
      this.readerTitle = snapshot.title;
      this.readerIssueLabel = snapshot.issueLabel;
      this.readerPageIndex = snapshot.pageIndex;
      this.readerPageCount = snapshot.pageCount;
      this.readerPageDraft = snapshot.pageDraft;
      this.readerImageUrl = snapshot.imageUrl;
      this.readerImageNaturalWidth = snapshot.imageNaturalWidth;
      this.readerImageNaturalHeight = snapshot.imageNaturalHeight;
      this.readerLastSettledPage = snapshot.lastSettledPage;
      this.readerLastSettledCompletion = snapshot.lastSettledCompletion;
      this.readerLastSavedSignature = snapshot.lastSavedSignature;
      this.readerCurrentUserInitiated = snapshot.currentUserInitiated;
      this.readerCompletionVisible = snapshot.completionVisible;
      this.readerRereadPending = snapshot.rereadPending;
      this.readerLoading = false;
      this.readerPageLoading = false;
      this.readerFatalError = false;
      this.readerFailedPageIndex = null;
      this.readerErrorTitle = "Pullbox could not open this comic.";
      this.readerErrorMessage = "";
      if (this.readerManifest) this.prefetchReaderNeighbors(this.readerPageIndex);
    },

    switchReaderIssue: async function (direction) {
      if (
        this.readerIssueTransitioning ||
        this.readerCompletionUpdating ||
        this.readerLoading ||
        this.readerPageLoading
      ) {
        return false;
      }
      var adjacent = this.readerAdjacentIssue(direction);
      if (!adjacent || !adjacent.manifest_url) return false;

      this.readerIssueTransitioning = true;
      this.readerIssueSwitchError = "";
      this.readerIssueStatusMessage =
        "Opening " + String(adjacent.issue_label || "another issue") + "…";
      this.clearReaderTimer("readerSettledTimer");
      try {
        await this.saveReaderProgress(false);
      } catch (_saveError) {
        this.readerIssueSwitchError =
          "Your reading position hasn’t saved yet. Try again before changing issues.";
        this.readerIssueStatusMessage = this.readerIssueSwitchError;
        this.readerIssueTransitioning = false;
        this.showReaderControls();
        return false;
      }

      var previousIssue = this.captureReaderIssue();
      var generation = this.readerIssueGeneration + 1;
      this.readerIssueGeneration = generation;
      try {
        var manifest = await this.fetchReaderManifest(adjacent.manifest_url, generation);
        if (!manifest) return false;
        var loaded = await this.activateReaderManifest(manifest, true);
        if (!loaded) {
          this.clearReaderIssueWork();
          this.restoreReaderIssue(previousIssue);
          throw new Error("The next comic page could not be displayed.");
        }
        this.readerIssueTransitioning = false;
        this.showReaderControls();
        return loaded;
      } catch (error) {
        if (error && error.name === "AbortError") return false;
        this.readerIssueSwitchError =
          (error && error.message) || "The next comic could not be opened.";
        this.readerIssueStatusMessage = this.readerIssueSwitchError;
        this.readerIssueTransitioning = false;
        this.showReaderControls();
        return false;
      }
    },

    readerMarkUnread: async function () {
      if (
        this.readerCompletionUpdating ||
        !this.readerManifest ||
        !this.readerManifest.completion_url
      ) {
        return;
      }
      this.readerCompletionUpdating = true;
      try {
        var response = await fetch(this.readerManifest.completion_url, {
          method: "PUT",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({ completed: false }),
        });
        if (!response.ok) throw new Error("Reading status was not saved.");
        var canonical = await response.json();
        if (canonical.state) this.readerManifest.state = canonical.state;
        this.readerRereadPending = false;
        this.readerCompletionVisible = false;
        this.readerIssueStatusMessage = this.readerIssueLabel + " marked unread";
      } catch (error) {
        this.readerIssueSwitchError =
          (error && error.message) || "Reading status was not saved.";
      } finally {
        this.readerCompletionUpdating = false;
      }
    },

    prefetchReaderNeighbors: function (pageIndex) {
      var self = this;
      self.clearReaderTimer("readerPrefetchTimer");
      if (
        self.readerPrefetchIdleHandle !== null &&
        typeof window.cancelIdleCallback === "function"
      ) {
        window.cancelIdleCallback(self.readerPrefetchIdleHandle);
      }
      self.readerPrefetchIdleHandle = null;
      self.readerPrefetchImages = [];

      var candidates = [];
      if (pageIndex + 1 < self.readerPageCount) candidates.push(pageIndex + 1);
      if (pageIndex - 1 >= 0) candidates.push(pageIndex - 1);
      if (!candidates.length) return;

      var first = new Image();
      first.decoding = "async";
      first.src = self.readerPageUrl(candidates[0]);
      self.readerPrefetchImages.push(first);

      if (candidates.length > 1) {
        var loadOtherNeighbor = function () {
          self.readerPrefetchIdleHandle = null;
          if (!self.readerOpen || self.readerPageIndex !== pageIndex) return;
          var second = new Image();
          second.decoding = "async";
          second.src = self.readerPageUrl(candidates[1]);
          self.readerPrefetchImages.push(second);
        };
        if (typeof window.requestIdleCallback === "function") {
          self.readerPrefetchIdleHandle = window.requestIdleCallback(loadOtherNeighbor, {
            timeout: 900,
          });
        } else {
          self.readerPrefetchTimer = window.setTimeout(loadOtherNeighbor, 350);
        }
      }
    },

    readerPrevious: function () {
      if (
        this.readerIssueTransitioning ||
        this.readerPageLoading ||
        this.readerPageIndex <= 0
      ) return;
      this.loadReaderPage(this.readerPageIndex - 1, true);
    },

    readerNext: function () {
      if (
        this.readerIssueTransitioning ||
        this.readerPageLoading ||
        this.readerPageIndex >= this.readerPageCount - 1
      ) return;
      this.loadReaderPage(this.readerPageIndex + 1, true);
    },

    readerTapZone: function (side) {
      if (side === "left") {
        if (this.readerDirection === "rtl") this.readerNext();
        else this.readerPrevious();
        return;
      }
      if (this.readerDirection === "rtl") this.readerPrevious();
      else this.readerNext();
    },

    restoreReaderPageDraft: function () {
      this.readerPageDraft = String(this.readerPageIndex + 1);
    },

    commitReaderPageJump: function () {
      var pageNumber = Number.parseInt(String(this.readerPageDraft), 10);
      if (!Number.isInteger(pageNumber) || pageNumber < 1 || pageNumber > this.readerPageCount) {
        this.readerPageInputError =
          "Enter a page from 1 to " + String(this.readerPageCount) + ".";
        this.restoreReaderPageDraft();
        return;
      }
      this.readerPageInputError = "";
      this.loadReaderPage(pageNumber - 1, true);
      if (this.$refs.readerViewport) this.$refs.readerViewport.focus();
    },

    toggleReaderDirection: function () {
      this.readerDirection = this.readerDirection === "ltr" ? "rtl" : "ltr";
      this.showReaderControls();
    },

    setReaderFit: function (mode) {
      if (["page", "width", "height", "actual"].indexOf(mode) === -1) return;
      this.readerFitMode = mode;
      if (mode !== "actual") this.readerZoomPercent = 100;
      this.showReaderControls();
    },

    readerImageClass: function () {
      return "comic-reader__page--" + this.readerFitMode;
    },

    readerImageStyle: function () {
      if (this.readerFitMode !== "actual" || !this.readerImageNaturalWidth) return "";
      return "width: " + Math.round(
        this.readerImageNaturalWidth * (this.readerZoomPercent / 100)
      ) + "px; height: auto;";
    },

    readerZoomIn: function () {
      var current = this.readerFitMode === "actual" ? this.readerZoomPercent : 100;
      this.readerFitMode = "actual";
      for (var i = 0; i < zoomSteps.length; i += 1) {
        if (zoomSteps[i] > current) {
          this.readerZoomPercent = zoomSteps[i];
          this.showReaderControls();
          return;
        }
      }
      this.readerZoomPercent = zoomSteps[zoomSteps.length - 1];
    },

    readerZoomOut: function () {
      var current = this.readerFitMode === "actual" ? this.readerZoomPercent : 100;
      this.readerFitMode = "actual";
      for (var i = zoomSteps.length - 1; i >= 0; i -= 1) {
        if (zoomSteps[i] < current) {
          this.readerZoomPercent = zoomSteps[i];
          this.showReaderControls();
          return;
        }
      }
      this.readerZoomPercent = zoomSteps[0];
    },

    resetReaderZoom: function () {
      this.readerFitMode = "actual";
      this.readerZoomPercent = 100;
      this.showReaderControls();
    },

    resetReaderSizing: function () {
      this.readerFitMode = "page";
      this.readerZoomPercent = 100;
      this.showReaderControls();
    },

    readerPageAlt: function () {
      if (!this.readerPageCount) return "Comic page";
      return "Page " + (this.readerPageIndex + 1) + " of " + this.readerPageCount;
    },

    readerPageStatus: function () {
      if (!this.readerPageCount) return "Preparing pages";
      return "Page " + (this.readerPageIndex + 1) + " of " + this.readerPageCount;
    },

    showReaderControls: function () {
      this.readerControlsVisible = true;
      this.scheduleReaderControlsHide();
    },

    toggleReaderControls: function () {
      this.readerControlsVisible = !this.readerControlsVisible;
      if (this.readerControlsVisible) this.scheduleReaderControlsHide();
      else this.clearReaderTimer("readerControlsTimer");
    },

    scheduleReaderControlsHide: function () {
      var self = this;
      self.clearReaderTimer("readerControlsTimer");
      if (
        !self.readerOpen ||
        self.readerLoading ||
        self.readerPageLoading ||
        self.readerFatalError ||
        self.readerHelpVisible ||
        (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches)
      ) {
        return;
      }
      self.readerControlsTimer = window.setTimeout(function () {
        var active = document.activeElement;
        var focusedControl = active && active.closest
          ? active.closest("[data-reader-controls]")
          : null;
        if (focusedControl) {
          self.scheduleReaderControlsHide();
          return;
        }
        self.readerControlsVisible = false;
      }, 3000);
    },

    handleReaderKeydown: function (event) {
      if (!this.readerOpen || !event || event.defaultPrevented) return;
      if (isTypingTarget(event.target)) {
        if (event.key === "Escape") {
          event.target.blur();
          event.preventDefault();
        }
        return;
      }

      var key = event.key;
      var handled = true;
      if (key === "ArrowLeft") this.readerTapZone("left");
      else if (key === "ArrowRight") this.readerTapZone("right");
      else if (key === "PageUp" || (key === " " && event.shiftKey)) this.readerPrevious();
      else if (key === "PageDown" || key === " ") this.readerNext();
      else if (key === "Home") this.loadReaderPage(0, true);
      else if (key === "End") this.loadReaderPage(this.readerPageCount - 1, true);
      else if (key === "g" || key === "G") {
        var input = this.$refs.readerDialog
          ? this.$refs.readerDialog.querySelector(".comic-reader__page-jump input")
          : null;
        if (input) {
          input.focus();
          input.select();
        }
      } else if (key === "w" || key === "W") this.setReaderFit("width");
      else if (key === "h" || key === "H") this.setReaderFit("height");
      else if (key === "0") this.resetReaderSizing();
      else if (key === "+" || key === "=") this.readerZoomIn();
      else if (key === "-" || key === "_") this.readerZoomOut();
      else if (key === "f" || key === "F") this.toggleReaderFullscreen();
      else if (key === "r" || key === "R") this.toggleReaderDirection();
      else if (key === "?") {
        this.readerHelpVisible = !this.readerHelpVisible;
        this.showReaderControls();
      } else if (key === "Escape") {
        if (this.readerHelpVisible) this.readerHelpVisible = false;
        else if (document.fullscreenElement) this.toggleReaderFullscreen();
        else this.closeReader();
      } else handled = false;

      if (handled) {
        event.preventDefault();
        this.showReaderControls();
      }
    },

    beginReaderPointer: function (event) {
      if (
        !event ||
        event.pointerType !== "touch" ||
        !event.isPrimary ||
        this.readerFitMode === "actual"
      ) {
        this.readerPointer = null;
        return;
      }
      this.readerPointer = {
        id: event.pointerId,
        x: event.clientX,
        y: event.clientY,
        time: Date.now(),
      };
    },

    endReaderPointer: function (event) {
      var pointer = this.readerPointer;
      this.readerPointer = null;
      if (!pointer || !event || pointer.id !== event.pointerId) return;
      var deltaX = event.clientX - pointer.x;
      var deltaY = event.clientY - pointer.y;
      var elapsed = Date.now() - pointer.time;
      if (elapsed > 700 || Math.abs(deltaX) < 48 || Math.abs(deltaX) < Math.abs(deltaY) * 1.25) {
        return;
      }
      this.readerTapZone(deltaX < 0 ? "right" : "left");
    },

    cancelReaderPointer: function () {
      this.readerPointer = null;
    },

    toggleReaderFullscreen: function () {
      var fullscreenTarget = this.$refs.readerShell;
      if (!fullscreenTarget) return;
      if (document.fullscreenElement) {
        Promise.resolve(document.exitFullscreen()).catch(function () {
          return null;
        });
        return;
      }
      if (typeof fullscreenTarget.requestFullscreen === "function") {
        Promise.resolve(fullscreenTarget.requestFullscreen()).catch(function () {
          return null;
        });
      }
    },

    syncReaderFullscreen: function () {
      this.readerFullscreenActive = document.fullscreenElement === this.$refs.readerShell;
    },

    retryReaderPage: function () {
      if (this.readerManifest && this.readerFailedPageIndex !== null) {
        this.loadReaderPage(this.readerFailedPageIndex, true);
        return;
      }
      this.retryReader();
    },
  };
}

function issueDetailPage(config) {
  var cfg = config || {};

  return Object.assign(fileBrowserMixin(cfg), readerMixin(cfg), {
    coverModalOpen: false,
    coverModalUrl: "",
    searching: false,
    togglingStatus: false,
    deletingIssueFile: false,
    importing: false,
    cancellingImport: false,
    importReplacementConfirmed: false,
    importModalOpen: false,
    importPollTimer: null,
    importState: "idle",
    importMessage: "",
    importErrorMessage: "",
    importSafetyException: null,
    importAllowSafetyException: false,
    importCurrentFileName: "",
    importCurrentFileStage: "",
    importCurrentFileProgress: 0,
    importCurrentFileProgressCurrent: null,
    importCurrentFileProgressTotal: null,
    importCurrentFileProgressUnit: "",
    form: {
      file_path: "",
      file_name: "",
      file_size: 0,
      file_ext: "",
      move_to_library: true,
    },

    dispatchToast: function (message, level) {
      if (typeof showToast === "function") {
        showToast({ message: message, level: level });
      }
    },

    openManualSearchModal: function () {
      window.dispatchEvent(
        new CustomEvent("open-search", {
          detail: {
            issueId: cfg.issueId,
            issueNum: cfg.issueNum,
            seriesTitle: cfg.seriesTitle,
            seriesYear: cfg.seriesYear,
          },
        })
      );
    },

    triggerIssueSearch: function () {
      var self = this;
      if (self.searching) return;
      self.searching = true;
      fetch(cfg.searchUrl, {
        method: "POST",
        headers: { "X-CSRF-Token": self.csrfToken() },
      })
        .then(function (response) {
          if (!response.ok) throw new Error("Search failed");
          self.dispatchToast("Search initiated for " + (cfg.issueLabel || "this issue"), "success");
        })
        .catch(function () {
          self.dispatchToast("Search failed", "error");
        })
        .finally(function () {
          self.searching = false;
        });
    },

    toggleIssueStatus: function () {
      var self = this;
      if (self.togglingStatus) return;
      self.togglingStatus = true;
      fetch(cfg.toggleUrl, {
        method: "POST",
        headers: { "X-CSRF-Token": self.csrfToken() },
      })
        .then(function (response) {
          if (!response.ok) throw new Error("Toggle failed");
          window.location.reload();
        })
        .catch(function () {
          self.dispatchToast("Unable to update this issue right now", "error");
          self.togglingStatus = false;
        });
    },

    openImportFileBrowser: function () {
      if (this.importing) return;
      this.openFileBrowser("_issueImportFile", this.form.file_path, {
        selectionMode: "file",
        title: "Select Comic File",
        emptyMessage: "No comic files or subdirectories",
        onSelectAction: "applyImportFileSelection",
      });
    },

    applyImportFileSelection: function (selection) {
      this.form.file_path = selection.path;
      this.form.file_name = selection.name;
      this.form.file_size = selection.size || 0;
      this.form.file_ext = selection.ext || "";
      this.importReplacementConfirmed = false;
      this.submitImport();
    },

    showImportModal: function () {
      return this.importModalOpen;
    },

    closeImportModal: function () {
      if (this.importState === "running") {
        return;
      }
      this.stopImportPolling();
      this.importModalOpen = false;
    },

    importStatusLabel: function () {
      var labels = {
        idle: "Ready",
        running: "Importing",
        completed: "Complete",
        failed: "Needs attention",
        safety_blocked: "Safety review",
        cancelled: "Cancelled",
      };
      return labels[this.importState] || "Importing";
    },

    importCurrentFileStageLabel: function () {
      var labels = {
        preparing: "Preparing file",
        extracting: "Extracting archive",
        rendering: "Rendering PDF pages",
        encoding: "Encoding pages",
        packing: "Packing CBZ",
        comicinfo_metadata: "Preparing ComicInfo metadata",
        transferring: "Transferring to library",
        rewriting: "Writing ComicInfo.xml",
        finalizing: "Finalizing imported file",
      };
      return labels[this.importCurrentFileStage] || "Processing file";
    },

    importCurrentFileDetailText: function () {
      if (
        this.importCurrentFileProgressCurrent == null ||
        this.importCurrentFileProgressTotal == null ||
        !this.importCurrentFileProgressUnit
      ) {
        return "";
      }
      if (
        this.importCurrentFileProgressUnit === "bytes" &&
        window._pb &&
        typeof window._pb.formatBytes === "function"
      ) {
        return (
          window._pb.formatBytes(this.importCurrentFileProgressCurrent) +
          " / " +
          window._pb.formatBytes(this.importCurrentFileProgressTotal)
        );
      }
      return (
        String(this.importCurrentFileProgressCurrent) +
        " / " +
        String(this.importCurrentFileProgressTotal) +
        " " +
        this.importCurrentFileProgressUnit
      );
    },

    importDetailFallbackText: function () {
      if (this.importState === "failed") {
        return "The import stopped before Pullbox could finish placing this file.";
      }
      if (this.importState === "safety_blocked") {
        return "Approve this resource safety exception only if you trust the file.";
      }
      if (this.importState === "completed") {
        return "The file finished importing and the page will refresh.";
      }
      if (this.importState === "cancelled") {
        return "The import was cancelled before Pullbox finished placing this file.";
      }
      return "Applying your current import settings to the selected file.";
    },

    applyImportProgress: function (data) {
      var next = data || {};
      this.importState = String(next.state || "idle");
      this.importMessage = String(next.message || "");
      this.importErrorMessage = String(next.error_message || "");
      this.importSafetyException = next.safety_exception || null;
      this.importCurrentFileName = String(
        next.current_file_name || this.form.file_name || ""
      );
      this.importCurrentFileStage = String(next.current_file_stage || "");
      this.importCurrentFileProgress =
        typeof next.current_file_progress_pct === "number" &&
        !Number.isNaN(next.current_file_progress_pct)
          ? Math.max(0, Math.min(100, Math.round(next.current_file_progress_pct)))
          : 0;
      this.importCurrentFileProgressCurrent =
        next.current_file_progress_current != null
          ? Number(next.current_file_progress_current)
          : null;
      this.importCurrentFileProgressTotal =
        next.current_file_progress_total != null
          ? Number(next.current_file_progress_total)
          : null;
      this.importCurrentFileProgressUnit = String(next.current_file_progress_unit || "");
      this.importing = this.importState === "running";
    },

    stopImportPolling: function () {
      if (this.importPollTimer) {
        window.clearTimeout(this.importPollTimer);
        this.importPollTimer = null;
      }
    },

    scheduleImportPoll: function (delayMs) {
      var self = this;
      self.stopImportPolling();
      self.importPollTimer = window.setTimeout(function () {
        self.importPollTimer = null;
        self.pollImportProgress();
      }, typeof delayMs === "number" ? delayMs : 400);
    },

    beginImportPolling: function () {
      this.importing = true;
      this.scheduleImportPoll(200);
    },

    pollImportProgress: function () {
      var self = this;
      fetch(cfg.importProgressUrl, {
        method: "GET",
        headers: {
          "X-CSRF-Token": self.csrfToken(),
        },
      })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("Failed to load import progress.");
          }
          return response.json();
        })
        .then(function (data) {
          self.applyImportProgress(data);
          if (self.importState === "running") {
            self.scheduleImportPoll(350);
            return;
          }
          self.handleTerminalImportProgress(data);
        })
        .catch(function (error) {
          self.importing = false;
          self.stopImportPolling();
          self.importState = "failed";
          self.importMessage = "Import failed.";
          self.importErrorMessage =
            error.message || "Failed to load import progress.";
          self.dispatchToast(self.importErrorMessage, "error");
        });
    },

    handleTerminalImportProgress: function (data) {
      this.stopImportPolling();
      this.importing = false;

      if ((data && data.state) === "completed") {
        this.dispatchToast("File imported successfully", "success");
        window.location.reload();
        return;
      }

      if ((data && data.state) === "safety_blocked") {
        this.dispatchToast(
          (data && data.error_message) || "Safety approval required.",
          "warning"
        );
        return;
      }

      if ((data && data.state) === "failed") {
        this.dispatchToast(
          (data && data.error_message) || "Import failed.",
          "error"
        );
        return;
      }

      if ((data && data.state) === "cancelled") {
        this.importModalOpen = false;
        this.dispatchToast("Import cancelled", "warning");
      }
    },

    cancelIssueImport: async function () {
      if (this.cancellingImport || this.importState === "completed") return;

      this.cancellingImport = true;
      this.stopImportPolling();

      try {
        var response = await fetch(cfg.importCancelUrl, {
          method: "POST",
          headers: { "X-CSRF-Token": this.csrfToken() },
        });
        if (!response.ok) {
          var data = await response.json().catch(function () {
            return {};
          });
          throw new Error(data.detail || "Failed to cancel import.");
        }

        var progress = await response.json();
        this.applyImportProgress(progress);
        if (this.importState === "running") {
          this.beginImportPolling();
          return;
        }
        this.handleTerminalImportProgress(progress);
      } catch (error) {
        if (this.importState === "running") {
          this.beginImportPolling();
        }
        this.dispatchToast(error.message || "Failed to cancel import.", "error");
      } finally {
        this.cancellingImport = false;
      }
    },

    formatSize: function (bytes) {
      if (bytes < 1024) return bytes + " B";
      if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
      return (bytes / 1048576).toFixed(1) + " MB";
    },

    submitImport: async function () {
      if (!this.form.file_path || this.importing) return;

      if (cfg.hasLibraryFile && !this.importReplacementConfirmed) {
        var replaceConfirmed = await pbConfirm({
          title: "Replace Issue File",
          message:
            "Pullbox will replace the current file for this issue using your import settings. This action cannot be rolled back.",
          confirmText: "Replace File",
          destructive: true,
        });
        if (!replaceConfirmed) {
          return;
        }
        this.importReplacementConfirmed = true;
      }

      this.importing = true;
      this.importModalOpen = true;
      var allowSafetyException = Boolean(this.importAllowSafetyException);
      this.importAllowSafetyException = false;
      this.applyImportProgress({
        state: "running",
        message: "Preparing import...",
        current_file_name: this.form.file_name || this.form.file_path,
        current_file_stage: "preparing",
        current_file_progress_current: 0,
        current_file_progress_total: 1,
        current_file_progress_pct: 0,
        current_file_progress_unit: "steps",
      });

      try {
        var response = await fetch(cfg.importStartUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": this.csrfToken(),
          },
          body: JSON.stringify({
            file_path: this.form.file_path,
            move_to_library: this.form.move_to_library,
            allow_resource_safety_exception: allowSafetyException,
          }),
        });

        if (!response.ok) {
          var data = await response.json().catch(function () {
            return {};
          });
          throw new Error(data.detail || "Import failed");
        }

        var data = await response.json();
        this.applyImportProgress(data);
        if (this.importState === "running") {
          this.beginImportPolling();
        } else {
          this.handleTerminalImportProgress(data);
        }
      } catch (error) {
        this.stopImportPolling();
        this.importing = false;
        this.importState = "failed";
        this.importMessage = "Import failed.";
        this.importErrorMessage =
          error.message || "An error occurred during import";
        this.dispatchToast(this.importErrorMessage, "error");
      }
    },

    retryImportWithSafetyException: function () {
      if (!this.form.file_path || this.importing) return;
      this.importAllowSafetyException = true;
      this.submitImport();
    },

    deleteIssueFile: async function () {
      if (this.deletingIssueFile || !cfg.deleteFileUrl) return;

      var confirmed = await pbConfirm({
        title: "Delete Issue File",
        message:
          "This removes the linked file from your library and makes the issue importable again. This action cannot be rolled back.",
        confirmText: "Delete File",
        destructive: true,
      });
      if (!confirmed) {
        return;
      }

      this.deletingIssueFile = true;
      try {
        var response = await fetch(cfg.deleteFileUrl, {
          method: "DELETE",
          headers: { "X-CSRF-Token": this.csrfToken() },
        });
        if (!response.ok) {
          var data = await response.json().catch(function () {
            return {};
          });
          throw new Error(data.detail || "Failed to delete issue file.");
        }
        this.dispatchToast("Issue file deleted", "success");
        window.location.reload();
      } catch (error) {
        this.deletingIssueFile = false;
        this.dispatchToast(
          error.message || "Failed to delete issue file.",
          "error"
        );
      }
    },
  });
}

function seriesDetailPage(config) {
  var cfg = config || {};

  return {
    showDeleteModal: false,
    deleteFiles: false,
    deleteFolder: false,
    deleting: false,
    coverModalOpen: false,
    coverModalUrl: "",
    monitored: false,
    saving: false,
    statusSaving: false,
    refreshing: false,
    searching: false,
    issueSearchState: {},

    init: function () {
      this.monitored = !!cfg.monitored;

      var self = this;
      var runNormalize = function () {
        self.normalizeIssuesPanelAfterRestore();
      };

      if (typeof this.$nextTick === "function") {
        this.$nextTick(function () {
          requestAnimationFrame(runNormalize);
        });
      } else {
        window.setTimeout(runNormalize, 0);
      }
    },

    csrfToken: function () {
      return cfg.csrfToken || readCsrfTokenFromBody();
    },

    dispatchToast: function (message, level) {
      if (typeof showToast === "function") {
        showToast({ message: message, level: level });
      }
    },

    currentDetailUrl: function () {
      return window.location.pathname + window.location.search;
    },

    refreshIssuesPanel: function () {
      var issuesPanel = document.getElementById("series-issues-panel");
      if (!issuesPanel || typeof htmx === "undefined") {
        return;
      }

      htmx.ajax("GET", issuesPanel.getAttribute("hx-get"), {
        target: "#series-issues-panel",
        swap: "morph:outerHTML",
      });
    },

    normalizeIssuesPanelAfterRestore: function () {
      if (!cfg.seriesId) {
        return;
      }

      var issuesPanel = document.getElementById("series-issues-panel");
      if (!issuesPanel) {
        return;
      }

      var dropdownRoot = document.querySelector(
        "[data-testid='series-detail-issues-status-select']"
      );
      var hiddenInput = dropdownRoot
        ? dropdownRoot.querySelector("[data-dropdown-select-input]")
        : null;
      var panel = dropdownRoot
        ? dropdownRoot.querySelector("[data-dropdown-select-panel]")
        : null;
      var trigger = dropdownRoot
        ? dropdownRoot.querySelector("[data-dropdown-select-trigger]")
        : null;
      var hxGet = issuesPanel.getAttribute("hx-get") || "";
      var panelDisplay = panel ? window.getComputedStyle(panel).display : "none";
      var panelVisible =
        !!panel &&
        panelDisplay !== "none" &&
        panelDisplay !== "contents" &&
        trigger &&
        trigger.getAttribute("aria-expanded") === "true";
      var dirty =
        (dropdownRoot &&
          (
            (dropdownRoot.getAttribute("data-dropdown-value") || "") !== "" ||
            (hiddenInput && hiddenInput.value !== "") ||
            panelVisible
          )) ||
        hxGet.indexOf("issue_status=") !== -1;

      if (!dirty) {
        return;
      }

      _sanitizeSeriesDetailHistoryRoot(document, window.location.pathname, true, false);

      var hxTrigger = issuesPanel.getAttribute("data-pb-history-hx-trigger");
      if (!issuesPanel.getAttribute("hx-trigger")) {
        issuesPanel.setAttribute("hx-trigger", hxTrigger || "every 3s");
      }
      issuesPanel.removeAttribute("data-pb-history-hx-trigger");

      if (typeof htmx === "undefined") {
        return;
      }

      htmx.ajax("GET", "/htmx/series/" + cfg.seriesId + "/issues", {
        target: "#series-issues-panel",
        swap: "outerHTML",
      });
    },

    isIssueAutoSearching: function (issueId) {
      return !!this.issueSearchState[issueId];
    },

    setIssueAutoSearching: function (issueId, isSearching) {
      var next = Object.assign({}, this.issueSearchState);
      if (isSearching) {
        next[issueId] = true;
      } else {
        delete next[issueId];
      }
      this.issueSearchState = next;
    },

    openIssueManualSearch: function (detail) {
      window.dispatchEvent(new CustomEvent("open-search", { detail: detail }));
    },

    runIssueAutoSearch: function (issue) {
      var self = this;
      if (!issue || !issue.id || self.isIssueAutoSearching(issue.id)) return;
      var toastId = "auto-search-" + issue.id;

      function emitToast(message, level, options) {
        if (typeof showToast !== "function") return;
        showToast(
          Object.assign(
            {
              message: message,
              level: level,
              id: toastId,
            },
            options || {}
          )
        );
      }

      self.setIssueAutoSearching(issue.id, true);
      emitToast("Searching for " + issue.label + "…", "info", {
        persistent: true,
        spinner: true,
      });

      fetch(issue.downloadUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": self.csrfToken(),
        },
      })
        .then(function (response) {
          return response
            .json()
            .catch(function () {
              return {};
            })
            .then(function (data) {
              return { ok: response.ok, data: data };
            });
        })
        .then(function (result) {
          var data = result.data || {};
          if (!result.ok) {
            var message =
              data.error && data.error.message
                ? data.error.message
                : typeof data.error === "string"
                  ? data.error
                  : "Search failed";
            emitToast(message, "error");
            return;
          }

          if (data.status === "downloading") {
            emitToast("Grabbed " + issue.label + " successfully", "success");
          } else if (data.status === "queued") {
            emitToast("Queued " + issue.label + " for review", "info");
          } else if (data.status === "no_results") {
            emitToast("No results found for " + issue.label, "warning");
          } else {
            var infoMessage =
              data.error && data.error.message
                ? data.error.message
                : typeof data.error === "string"
                  ? data.error
                  : "Search completed";
            emitToast(infoMessage, "info");
          }
        })
        .catch(function () {
          emitToast("Search failed", "error");
        })
        .finally(function () {
          self.setIssueAutoSearching(issue.id, false);
        });
    },

    toggleMonitoring: function (enabled) {
      var self = this;
      if (self.saving || self.monitored === enabled) return;
      self.saving = true;
      fetch(cfg.updateUrl, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": self.csrfToken(),
        },
        body: JSON.stringify({ monitored: enabled }),
      })
        .then(function (response) {
          if (!response.ok) throw new Error("Failed to update monitoring");
          self.monitored = enabled;
          self.dispatchToast(
            enabled ? "Monitoring enabled" : "Monitoring disabled",
            enabled ? "success" : "info"
          );
          self.refreshIssuesPanel();
          self.saving = false;
        })
        .catch(function () {
          self.dispatchToast("Failed to update monitoring", "error");
          self.saving = false;
        });
    },

    updateStatusOverride: function (statusOverride) {
      var self = this;
      if (self.statusSaving) return;
      self.statusSaving = true;
      fetch(cfg.updateUrl, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": self.csrfToken(),
        },
        body: JSON.stringify({ status_override: statusOverride }),
      })
        .then(function (response) {
          if (!response.ok) throw new Error("Failed to update series status");
          self.dispatchToast(
            statusOverride === null
              ? "Automatic status restored"
              : statusOverride === "ended"
                ? "Series marked as ended"
                : "Series marked as continuing",
            "success"
          );
          setTimeout(function () {
            window.location.assign(self.currentDetailUrl());
          }, 500);
        })
        .catch(function () {
          self.dispatchToast("Failed to update series status", "error");
          self.statusSaving = false;
        });
    },

    refreshMetadata: function () {
      var self = this;
      if (self.refreshing) return;
      self.refreshing = true;
      fetch(cfg.refreshUrl, {
        method: "POST",
        headers: { "X-CSRF-Token": self.csrfToken() },
      })
        .then(function (response) {
          if (!response.ok) throw new Error("Failed to refresh metadata");
          self.dispatchToast("Metadata refreshed", "success");
          setTimeout(function () {
            window.location.assign(self.currentDetailUrl());
          }, 500);
        })
        .catch(function () {
          self.dispatchToast("Failed to refresh metadata", "error");
          self.refreshing = false;
        });
    },

    searchMissing: function () {
      var self = this;
      if (self.searching) return;
      self.searching = true;
      fetch(cfg.searchUrl, {
        method: "POST",
        headers: { "X-CSRF-Token": self.csrfToken() },
      })
        .then(function (response) {
          if (!response.ok) throw new Error("Search failed");
          return response.json();
        })
        .then(function (data) {
          var message =
            data.message ||
            (data.status === "no_wanted"
              ? "No wanted issues to search"
              : "Search started for " + data.issues_to_search + " issues");
          self.dispatchToast(message, data.status === "no_wanted" ? "info" : "success");
        })
        .catch(function () {
          self.dispatchToast("Search failed", "error");
        })
        .finally(function () {
          self.searching = false;
        });
    },

    openDeleteModal: function () {
      this.showDeleteModal = true;
    },

    closeDeleteModal: function () {
      this.showDeleteModal = false;
      this.deleteFiles = false;
      this.deleteFolder = false;
      this.deleting = false;
    },

    syncDeleteOptions: function () {
      if (this.deleteFolder) {
        this.deleteFiles = true;
        return;
      }
      this.deleteFiles = false;
    },

    submitDelete: function () {
      var self = this;
      if (self.deleting) return;
      self.deleting = true;
      fetch(cfg.deleteUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": self.csrfToken(),
        },
        body: JSON.stringify({
          delete_files: self.deleteFiles,
          delete_folder: self.deleteFolder,
        }),
      })
        .then(function (response) {
          if (!response.ok) throw new Error("Failed to delete series");
          window.location.assign("/series");
        })
        .catch(function () {
          self.dispatchToast("Failed to delete series", "error");
          self.deleting = false;
        });
    },
  };
}

function seriesIssuesPanel() {
  return {
    detailPageData: null,

    init: function () {
      this.detailPageData = this.resolveDetailPageData();
    },

    resolveDetailPageData: function () {
      var host = this.$el && this.$el.closest
        ? this.$el.closest("[data-testid='series-detail-page']")
        : null;
      if (!host) {
        return null;
      }

      try {
        if (window.Alpine && typeof window.Alpine.$data === "function") {
          return window.Alpine.$data(host);
        }
      } catch (_) {
        // fall through to the internal Alpine reference if available
      }

      return host.__x ? host.__x.$data : null;
    },

    pageData: function () {
      if (!this.detailPageData) {
        this.detailPageData = this.resolveDetailPageData();
      }
      return this.detailPageData;
    },

    isIssueAutoSearching: function (issueId) {
      var data = this.pageData();
      if (!data || typeof data.isIssueAutoSearching !== "function") {
        return false;
      }
      return data.isIssueAutoSearching(issueId);
    },

    runIssueAutoSearch: function (issue) {
      var data = this.pageData();
      if (!data || typeof data.runIssueAutoSearch !== "function") {
        return;
      }
      return data.runIssueAutoSearch(issue);
    },

    openIssueManualSearch: function (detail) {
      var data = this.pageData();
      if (!data || typeof data.openIssueManualSearch !== "function") {
        return;
      }
      return data.openIssueManualSearch(detail);
    },
  };
}

window.pullboxSeriesIssuesCanPoll = function () {
  if (!window.pullboxLiveUpdatesEnabled()) return false;
  var active = document.activeElement;
  return !(
    active &&
    active.closest &&
    active.closest("#series-issues-panel [data-reading-interaction]")
  );
};

function issueSearchModal() {
  return {
    searchOpen: false,
    searchIssueId: null,
    searchIssueNum: "",
    searchSeriesTitle: "",
    searchSeriesYear: "",
    searching: false,
    swapListener: null,
    dcSearching: false,
    dcVisualMessage: "",
    dcLiveMessage: "",
    dcAbortController: null,

    clearDcSearch: function () {
      if (this.dcAbortController) {
        this.dcAbortController.abort();
        this.dcAbortController = null;
      }
      this.dcSearching = false;
      this.dcVisualMessage = "";
      this.dcLiveMessage = "";
      var results = document.getElementById("issue-search-dc-results");
      if (results) {
        results.innerHTML = "";
      }
    },

    applyDcProgress: function (progress) {
      var state = progress && progress.state;
      if (state === "cooldown") {
        var seconds = Number(progress.remaining_seconds) || 0;
        var template = "Direct Connect search will resume in {seconds} seconds to respect the 45-second hub cooldown.";
        this.dcVisualMessage = template.replace("{seconds}", String(seconds));
        if (!this.dcLiveMessage || this.dcLiveMessage.indexOf("cooldown") === -1) {
          this.dcLiveMessage = this.dcVisualMessage;
        }
        return;
      }
      if (state === "starting") {
        this.dcVisualMessage = "Waiting for AirDC++ to send the search…";
        this.dcLiveMessage = "Direct Connect search started.";
      } else if (state === "queued") {
        this.dcVisualMessage = "AirDC++ queued this search to protect its hub connections. Pullbox will keep collecting results when it is sent.";
      } else if (state === "collecting") {
        this.dcVisualMessage = "Collecting Direct Connect results…";
      } else if (state === "finishing") {
        this.dcVisualMessage = "Finishing the search…";
      } else if (state === "zero_hubs") {
        this.dcVisualMessage = "AirDC++ is connected, but it doesn't have any open hubs to search. Open AirDC++ to check your hub connections.";
      } else if (state === "failed") {
        this.dcVisualMessage = "Direct Connect search is temporarily unavailable. Existing results are still available.";
      }
    },

    consumeDcStream: async function (response) {
      if (!response.body) {
        throw new Error("Direct Connect search stream is unavailable");
      }
      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        var frames = buffer.split("\n\n");
        buffer = frames.pop() || "";
        for (var i = 0; i < frames.length; i += 1) {
          var dataLine = frames[i].split("\n").find(function (line) {
            return line.indexOf("data: ") === 0;
          });
          if (!dataLine) continue;
          var event = JSON.parse(dataLine.slice(6));
          if (event.kind === "progress") {
            this.applyDcProgress(event.progress);
          } else if (event.kind === "results") {
            var target = document.getElementById("issue-search-dc-results");
            if (target) {
              target.innerHTML = event.html;
              if (window.htmx) window.htmx.process(target);
              if (window.Alpine && typeof window.Alpine.initTree === "function") {
                window.Alpine.initTree(target);
              }
            }
            this.dcLiveMessage = event.summary;
          }
        }
      }
    },

    startDcSearch: async function () {
      this.clearDcSearch();
      this.dcAbortController = new AbortController();
      var signal = this.dcAbortController.signal;
      var base = "/htmx/issues/" + this.searchIssueId;
      try {
        var statusResponse = await fetch(base + "/dc-search-status", {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
          signal: signal,
        });
        if (!statusResponse.ok) return;
        var status = await statusResponse.json();
        if (!status.available) return;
        this.dcSearching = true;
        if (status.remaining_seconds > 0) {
          this.applyDcProgress({
            state: "cooldown",
            remaining_seconds: status.remaining_seconds,
          });
        } else {
          this.applyDcProgress({ state: "starting" });
        }
        var response = await fetch(base + "/dc-search-results", {
          credentials: "same-origin",
          headers: { Accept: "text/event-stream" },
          signal: signal,
        });
        if (!response.ok) throw new Error("Direct Connect search failed");
        await this.consumeDcStream(response);
      } catch (error) {
        if (!signal.aborted) {
          this.applyDcProgress({ state: "failed" });
        }
      } finally {
        if (!signal.aborted) {
          this.dcSearching = false;
        }
        this.dcAbortController = null;
      }
    },

    resetMeta: function () {
      var stats = document.getElementById("issue-search-modal-stats");
      if (stats) {
        stats.innerHTML = "";
      }
      var footerMeta = document.getElementById("issue-search-modal-footer-meta");
      if (footerMeta) {
        footerMeta.textContent = "";
      }
    },

    clearSwapListener: function () {
      if (!this.swapListener) {
        return;
      }
      document.removeEventListener("htmx:afterSwap", this.swapListener);
      this.swapListener = null;
    },

    open: function (detail) {
      if (typeof window.pbHideTooltip === "function") {
        window.pbHideTooltip();
      }
      this.clearSwapListener();
      this.clearDcSearch();
      this.searchIssueId = detail.issueId;
      this.searchIssueNum = detail.issueNum;
      this.searchSeriesTitle = detail.seriesTitle;
      this.searchSeriesYear = detail.seriesYear;
      this.searchOpen = true;
      this.searching = true;
      this.resetMeta();
      var body = document.getElementById("issue-search-modal-body");
      if (body) {
        body.innerHTML = "";
      }
      var self = this;
      this.swapListener = function (event) {
        if (event.detail && event.detail.target && event.detail.target.id === "issue-search-modal-body") {
          self.searching = false;
          self.clearSwapListener();
          self.startDcSearch();
        }
      };
      document.addEventListener("htmx:afterSwap", this.swapListener);
      setTimeout(function () {
        htmx.ajax("GET", "/htmx/issues/" + self.searchIssueId + "/search-results", {
          target: "#issue-search-modal-body",
          swap: "innerHTML",
        });
      }, 0);
    },

    close: function () {
      this.searchOpen = false;
      this.searching = false;
      this.clearSwapListener();
      this.clearDcSearch();
      this.resetMeta();
    },
  };
}

/* ── Theme Management ─────────────────────────────────────────────── */

/**
 * Get the current active theme.
 * @returns {"dark"|"light"}
 */
function getTheme() {
  return document.documentElement.getAttribute("data-theme") || "dark";
}

/**
 * Apply a theme. Accepts "dark", "light", or "system".
 * "system" resolves to dark/light based on OS preference and clears
 * the localStorage override so future visits follow OS changes.
 * @param {"dark"|"light"|"system"} theme
 */
function applyTheme(theme) {
  var resolved = theme;
  if (theme === "system") {
    resolved = window.matchMedia("(prefers-color-scheme: light)").matches
      ? "light"
      : "dark";
    localStorage.removeItem("pullbox-theme");
  } else {
    localStorage.setItem("pullbox-theme", resolved);
  }
  // Suppress transitions so theme swap is instant (no color "fade")
  document.documentElement.classList.add("no-transitions");
  document.documentElement.setAttribute("data-theme", resolved);
  // Re-enable after a frame so hover transitions still work
  requestAnimationFrame(function () {
    requestAnimationFrame(function () {
      document.documentElement.classList.remove("no-transitions");
    });
  });
}

/* ── Live Updates Management ───────────────────────────────────────── */

var LIVE_UPDATES_STORAGE_KEY = "pullbox-live-updates";

function areLiveUpdatesPaused() {
  return localStorage.getItem(LIVE_UPDATES_STORAGE_KEY) === "paused";
}

function pullboxLiveUpdatesEnabled() {
  return !areLiveUpdatesPaused();
}

function searchHistoryRefreshEnabled() {
  if (!pullboxLiveUpdatesEnabled()) {
    return false;
  }

  return !document.querySelector("[data-search-history-expanded='true']");
}

function importHistoryToolbarActive() {
  var toolbar = document.querySelector("[data-testid='import-history-toolbar']");
  if (!toolbar) {
    return false;
  }

  var activeElement = document.activeElement;
  if (activeElement && toolbar.contains(activeElement)) {
    return true;
  }

  var openTrigger = toolbar.querySelector(
    "[data-dropdown-select-trigger][aria-expanded='true']"
  );
  if (openTrigger) {
    return true;
  }

  return false;
}

function importHistoryResultsActive() {
  var results = document.querySelector("[data-testid='import-history-results']");
  if (!results || !results.matches) {
    return false;
  }

  try {
    return results.matches(":hover");
  } catch (_) {
    return false;
  }
}

function importHistoryRefreshEnabled() {
  if (!pullboxLiveUpdatesEnabled()) {
    return false;
  }

  if (document.querySelector("[data-import-history-expanded='true']")) {
    return false;
  }

  if (importHistoryResultsActive()) {
    return false;
  }

  return !importHistoryToolbarActive();
}

function downloadsHistoryToolbarActive() {
  var toolbar = document.querySelector("[data-testid='downloads-history-toolbar']");
  if (!toolbar) {
    return false;
  }

  var activeElement = document.activeElement;
  if (activeElement && toolbar.contains(activeElement)) {
    return true;
  }

  var openTrigger = toolbar.querySelector(
    "[data-dropdown-select-trigger][aria-expanded='true']"
  );
  if (openTrigger) {
    return true;
  }

  return false;
}

function downloadsHistoryResultsActive() {
  var results = document.querySelector("[data-testid='downloads-history-results']");
  if (!results || !results.matches) {
    return false;
  }

  try {
    return results.matches(":hover");
  } catch (_) {
    return false;
  }
}

function downloadsHistoryRefreshEnabled() {
  if (!pullboxLiveUpdatesEnabled()) {
    return false;
  }

  if (document.querySelector("[data-downloads-history-expanded='true']")) {
    return false;
  }

  if (downloadsHistoryResultsActive()) {
    return false;
  }

  return !downloadsHistoryToolbarActive();
}

function postProcessingHistoryToolbarActive() {
  var toolbar = document.querySelector("[data-testid='pp-history-toolbar']");
  if (!toolbar) {
    return false;
  }

  var activeElement = document.activeElement;
  if (activeElement && toolbar.contains(activeElement)) {
    return true;
  }

  var openTrigger = toolbar.querySelector(
    "[data-dropdown-select-trigger][aria-expanded='true']"
  );
  if (openTrigger) {
    return true;
  }

  return false;
}

function postProcessingHistoryResultsActive() {
  var results = document.querySelector("[data-testid='pp-history-results']");
  if (!results || !results.matches) {
    return false;
  }

  try {
    return results.matches(":hover");
  } catch (_) {
    return false;
  }
}

function postProcessingHistoryRefreshEnabled() {
  if (!pullboxLiveUpdatesEnabled()) {
    return false;
  }

  if (document.querySelector("[data-post-processing-history-expanded='true']")) {
    return false;
  }

  if (postProcessingHistoryResultsActive()) {
    return false;
  }

  return !postProcessingHistoryToolbarActive();
}

function postProcessingQueueRowsActive() {
  if (!document.querySelector) {
    return false;
  }

  try {
    return Boolean(
      document.querySelector(
        "[data-testid='pp-queue-active-table'] tbody:hover"
      )
    );
  } catch (_) {
    return false;
  }
}

function postProcessingQueueRefreshEnabled() {
  if (!pullboxLiveUpdatesEnabled()) {
    return false;
  }

  return !postProcessingQueueRowsActive();
}

function healthComponentHistoryToolbarActive() {
  var toolbar = document.querySelector("[data-testid='health-history-toolbar']");
  if (!toolbar) {
    return false;
  }

  var activeElement = document.activeElement;
  if (activeElement && toolbar.contains(activeElement)) {
    return true;
  }

  return false;
}

function healthComponentHistoryResultsActive() {
  var results = document.querySelector("[data-testid='health-history-results']");
  if (!results || !results.matches) {
    return false;
  }

  try {
    return results.matches(":hover");
  } catch (_) {
    return false;
  }
}

function healthComponentRefreshEnabled() {
  if (!pullboxLiveUpdatesEnabled()) {
    return false;
  }

  if (healthComponentHistoryResultsActive()) {
    return false;
  }

  return !healthComponentHistoryToolbarActive();
}

function interventionQueueToolbarActive() {
  var toolbar = document.querySelector("[data-testid='intervention-filters']");
  if (!toolbar) {
    return false;
  }

  var activeElement = document.activeElement;
  if (activeElement && toolbar.contains(activeElement)) {
    return true;
  }

  var openTrigger = toolbar.querySelector(
    "[data-dropdown-select-trigger][aria-expanded='true']"
  );
  if (openTrigger) {
    return true;
  }

  return false;
}

function interventionQueueResultsActive() {
  var results = document.querySelector("[data-testid='intervention-queue-results']");
  if (!results || !results.matches) {
    return false;
  }

  try {
    return results.matches(":hover");
  } catch (_) {
    return false;
  }
}

function interventionQueueRefreshEnabled() {
  if (!pullboxLiveUpdatesEnabled()) {
    return false;
  }

  if (document.querySelector("[data-intervention-queue-expanded='true']")) {
    return false;
  }

  if (interventionQueueResultsActive()) {
    return false;
  }

  return !interventionQueueToolbarActive();
}

function applyLiveUpdatesState(paused) {
  document.documentElement.setAttribute("data-live-updates", paused ? "paused" : "running");
  if (paused) {
    localStorage.setItem(LIVE_UPDATES_STORAGE_KEY, "paused");
  } else {
    localStorage.removeItem(LIVE_UPDATES_STORAGE_KEY);
  }

  document.dispatchEvent(
    new CustomEvent("pullbox:live-updates-changed", {
      detail: { paused: paused },
    }),
  );
}

function toggleLiveUpdates() {
  applyLiveUpdatesState(!areLiveUpdatesPaused());
}

function liveUpdatesController() {
  return {
    paused: areLiveUpdatesPaused(),
    init() {
      var self = this;
      this.paused = areLiveUpdatesPaused();
      document.addEventListener("pullbox:live-updates-changed", function (event) {
        self.paused = Boolean(event.detail && event.detail.paused);
      });
    },
    toggle() {
      toggleLiveUpdates();
    },
  };
}

applyLiveUpdatesState(areLiveUpdatesPaused());
window.pullboxLiveUpdatesEnabled = pullboxLiveUpdatesEnabled;
window.searchHistoryRefreshEnabled = searchHistoryRefreshEnabled;
window.downloadsHistoryRefreshEnabled = downloadsHistoryRefreshEnabled;
window.postProcessingHistoryRefreshEnabled = postProcessingHistoryRefreshEnabled;
window.postProcessingQueueRefreshEnabled = postProcessingQueueRefreshEnabled;
window.healthComponentRefreshEnabled = healthComponentRefreshEnabled;
window.interventionQueueRefreshEnabled = interventionQueueRefreshEnabled;

// Follow OS preference changes when no manual override is saved
window
  .matchMedia("(prefers-color-scheme: light)")
  .addEventListener("change", function () {
    if (!localStorage.getItem("pullbox-theme")) {
      applyTheme("system");
    }
  });

/* ── Toast Notifications ─────────────────────────────────────────── */

var _pbAuthRedirectState = {
  active: false,
  timer: null,
};

function _getAuthRedirectUrl(defaultUrl) {
  return typeof defaultUrl === "string" && defaultUrl ? defaultUrl : "/login";
}

function _isAuthRedirectPage() {
  var path = window.location && window.location.pathname ? window.location.pathname : "";
  return path === "/login" || path === "/setup";
}

function _dismissAllToasts() {
  const container = document.getElementById("toast-container");
  if (!container) return;
  container.innerHTML = "";
}

function _stopHtmxPolling() {
  if (typeof htmx === "undefined") {
    return;
  }

  document
    .querySelectorAll("[hx-trigger*='every'], [data-hx-trigger*='every']")
    .forEach(function (el) {
      if (el.hasAttribute("hx-trigger")) {
        el.removeAttribute("hx-trigger");
      }
      if (el.hasAttribute("data-hx-trigger")) {
        el.removeAttribute("data-hx-trigger");
      }
      htmx.process(el);
    });
}

function _beginAuthExpiryRedirect(redirectUrl) {
  if (_pbAuthRedirectState.active || _isAuthRedirectPage()) {
    return true;
  }

  _pbAuthRedirectState.active = true;
  _dismissAllToasts();
  _stopHtmxPolling();

  if (_pbAuthRedirectState.timer) {
    window.clearTimeout(_pbAuthRedirectState.timer);
  }

  _pbAuthRedirectState.timer = window.setTimeout(function () {
    window.location.replace(_getAuthRedirectUrl(redirectUrl));
  }, 0);

  return true;
}

function _isSameOriginResponse(response) {
  if (!response || !response.url) {
    return true;
  }

  try {
    var responseUrl = new URL(response.url, window.location.origin);
    return responseUrl.origin === window.location.origin;
  } catch (_) {
    return true;
  }
}

function _responseRedirectedToLogin(response) {
  if (!response || !response.redirected || !response.url) {
    return false;
  }

  try {
    var responseUrl = new URL(response.url, window.location.origin);
    return responseUrl.origin === window.location.origin && responseUrl.pathname === "/login";
  } catch (_) {
    return false;
  }
}

function _authRedirectUrlFromResponse(response) {
  if (!response || !response.headers || typeof response.headers.get !== "function") {
    return "";
  }

  return response.headers.get("X-Pullbox-Auth-Redirect") || "";
}

function _handleAuthExpiryResponse(response) {
  if (_isAuthRedirectPage() || !response || !_isSameOriginResponse(response)) {
    return false;
  }

  var redirectUrl = _authRedirectUrlFromResponse(response);
  if (redirectUrl || _responseRedirectedToLogin(response)) {
    return _beginAuthExpiryRedirect(redirectUrl);
  }

  if (response.status === 401) {
    return _beginAuthExpiryRedirect("/login");
  }

  return false;
}

(function () {
  if (typeof window.fetch !== "function" || window.__pbAuthExpiryFetchWrapped) {
    return;
  }

  var nativeFetch = window.fetch.bind(window);
  window.fetch = function () {
    return nativeFetch.apply(window, arguments).then(function (response) {
      _handleAuthExpiryResponse(response);
      return response;
    });
  };
  window.__pbAuthExpiryFetchWrapped = true;
})();

const PULLBOX_QUEUED_TOAST_KEY = "pullbox:queued-toast";
const PULLBOX_QUEUED_TOAST_TTL_MS = 30000;

function queueToastForNextPage(detail) {
  if (!detail || !detail.message) return;

  try {
    window.sessionStorage.setItem(
      PULLBOX_QUEUED_TOAST_KEY,
      JSON.stringify({
        message: String(detail.message),
        level: detail.level || "info",
        id: detail.id || "",
        persistent: !!detail.persistent,
        createdAt: Date.now(),
      })
    );
  } catch (_) {
    // Storage can be unavailable in private browsing or locked-down webviews.
  }
}

function replayQueuedToast() {
  try {
    var raw = window.sessionStorage.getItem(PULLBOX_QUEUED_TOAST_KEY);
    if (!raw) return;

    window.sessionStorage.removeItem(PULLBOX_QUEUED_TOAST_KEY);
    var detail = JSON.parse(raw);
    if (!detail || !detail.message) return;

    var createdAt = Number(detail.createdAt || 0);
    if (createdAt && Date.now() - createdAt > PULLBOX_QUEUED_TOAST_TTL_MS) return;

    showToast({
      message: detail.message,
      level: detail.level || "info",
      id: detail.id || undefined,
      persistent: !!detail.persistent,
    });
  } catch (_) {
    try {
      window.sessionStorage.removeItem(PULLBOX_QUEUED_TOAST_KEY);
    } catch (__) {
      // Ignore storage cleanup failures.
    }
  }
}

/**
 * Show a toast notification.
 * @param {object} detail - { message: string, level: "success"|"error"|"warning"|"info", id?: string, persistent?: boolean, spinner?: boolean }
 */
function showToast(detail) {
  if (_pbAuthRedirectState.active) return;

  const container = document.getElementById("toast-container");
  if (!container) return;

  // If an id is provided, dismiss any existing toast with that id first
  if (detail.id) {
    dismissToastById(detail.id);
  }

  const level = detail.level || "info";
  const colors = {
    success: "bg-pb-success",
    error: "bg-pb-error",
    warning: "bg-pb-warning",
    info: "bg-pb-interactive",
  };

  const icons = {
    success: "M5 13l4 4L19 7",
    error: "M6 18L18 6M6 6l12 12",
    warning: "M12 9v4m0 4h.01M12 2a10 10 0 100 20 10 10 0 000-20z",
    info: "M13 16h-1v-4h-1m1-4h.01M12 2a10 10 0 100 20 10 10 0 000-20z",
  };

  const el = document.createElement("div");
  if (detail.id) el.dataset.toastId = detail.id;
  el.className =
    "toast-enter flex items-center gap-3 px-4 py-3 rounded-lg shadow-lg text-white text-sm " +
    (colors[level] || colors.info);

  var iconHtml;
  if (detail.spinner) {
    iconHtml =
      '<svg class="w-5 h-5 shrink-0 animate-spin" fill="none" viewBox="0 0 24 24">' +
      '<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>' +
      '<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>' +
      "</svg>";
  } else {
    iconHtml =
      '<svg class="w-5 h-5 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">' +
      '<path stroke-linecap="round" stroke-linejoin="round" d="' +
      (icons[level] || icons.info) +
      '"/></svg>';
  }

  el.innerHTML =
    iconHtml +
    '<span class="flex-1">' +
    escapeHtml(detail.message || "") +
    "</span>" +
    '<button onclick="dismissToast(this.parentElement)" class="ml-2 opacity-70 hover:opacity-100">&times;</button>';

  container.appendChild(el);

  if (!detail.persistent) {
    setTimeout(function () {
      dismissToast(el);
    }, 5000);
  }
}

/**
 * Dismiss a toast by its id.
 * @param {string} id
 */
function dismissToastById(id) {
  const container = document.getElementById("toast-container");
  if (!container) return;
  var el = container.querySelector('[data-toast-id="' + id + '"]');
  if (el) {
    el.remove();
  }
}

/**
 * Update an existing toast in-place (morph spinner → result).
 * Falls back to showToast if the toast id isn't found.
 * @param {string} id - toast id to update
 * @param {object} detail - { message, level }
 */
function updateToast(id, detail) {
  const container = document.getElementById("toast-container");
  if (!container) { showToast(detail); return; }

  var el = container.querySelector('[data-toast-id="' + id + '"]');
  if (!el) { showToast(detail); return; }

  const level = detail.level || "info";
  const colors = {
    success: "bg-pb-success",
    error: "bg-pb-error",
    warning: "bg-pb-warning",
    info: "bg-pb-interactive",
  };
  const icons = {
    success: "M5 13l4 4L19 7",
    error: "M6 18L18 6M6 6l12 12",
    warning: "M12 9v4m0 4h.01M12 2a10 10 0 100 20 10 10 0 000-20z",
    info: "M13 16h-1v-4h-1m1-4h.01M12 2a10 10 0 100 20 10 10 0 000-20z",
  };

  Object.values(colors).forEach((className) => {
    el.classList.remove(className);
  });
  el.classList.add(colors[level] || colors.info);

  // Replace icon (spinner → status icon) and message
  el.innerHTML =
    '<svg class="w-5 h-5 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">' +
    '<path stroke-linecap="round" stroke-linejoin="round" d="' +
    (icons[level] || icons.info) +
    '"/></svg>' +
    '<span class="flex-1">' +
    escapeHtml(detail.message || "") +
    "</span>" +
    '<button onclick="dismissToast(this.parentElement)" class="ml-2 opacity-70 hover:opacity-100">&times;</button>';

  // Auto-dismiss after 5s
  setTimeout(function () {
    dismissToast(el);
  }, 5000);
}

/**
 * Dismiss a toast element with exit animation.
 * @param {HTMLElement} el
 */
function dismissToast(el) {
  if (!el || el.classList.contains("toast-exit")) return;
  el.classList.remove("toast-enter");
  el.classList.add("toast-exit");
  el.addEventListener("animationend", function () {
    el.remove();
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", replayQueuedToast, { once: true });
} else {
  replayQueuedToast();
}

/**
 * Escape HTML to prevent XSS in toast messages.
 * @param {string} str
 * @returns {string}
 */
function escapeHtml(str) {
  const div = document.createElement("div");
  div.appendChild(document.createTextNode(str));
  return div.innerHTML;
}

/* ── Settings form dirty tracking ────────────────────────── */

/**
 * Alpine.js component for tracking unsaved changes in a <form>.
 * Usage: <form x-data="dirtyForm()" @input="checkDirty()" @change="checkDirty()">
 *   <button type="submit" :disabled="!isDirty" :class="isDirty ? '...' : '...'">Save</button>
 *   <button type="button" x-show="isDirty" @click="resetForm()">Reset</button>
 * </form>
 */
function dirtyForm() {
  return {
    isDirty: false,
    _entries: [],

    init: function () {
      var self = this;
      self.$nextTick(function () {
        self._entries = self._capture();
      });
    },

    _capture: function () {
      var entries = [];
      this.$el.querySelectorAll("input, select, textarea").forEach(function (el) {
        entries.push({
          el: el,
          val: el.type === "checkbox" ? el.checked : el.value,
        });
      });
      return entries;
    },

    checkDirty: function () {
      var dirty = false;
      this._entries.forEach(function (entry) {
        var current = entry.el.type === "checkbox" ? entry.el.checked : entry.el.value;
        if (String(current) !== String(entry.val)) dirty = true;
      });
      this.isDirty = dirty;
    },

    resetForm: function () {
      this._entries.forEach(function (entry) {
        var el = entry.el;
        var val = entry.val;
        // Restore DOM value
        if (el.type === "checkbox") {
          el.checked = val;
        } else {
          el.value = val;
        }
        // Sync Alpine x-model binding via its internal setter
        if (el._x_model) {
          el._x_model.set(val);
        }
      });
      this.isDirty = false;
    },

    markClean: function () {
      this._entries = this._capture();
      this.isDirty = false;
    },
  };
}

/* ── Sidebar active-state management (hx-boost navigation) ── */

function normalizePath(url) {
  if (!url) return "";
  try {
    return new URL(url, window.location.origin).pathname;
  } catch (_) {
    return url.split("?")[0];
  }
}

function getSavedSeriesUrl() {
  var lastUrl = localStorage.getItem("series_last_url");
  if (lastUrl) {
    try {
      var parsed = new URL(lastUrl, window.location.origin);
      if (parsed.pathname === "/series") {
        return parsed.pathname + parsed.search;
      }
    } catch (_) {
      /* fall through to filter reconstruction */
    }
  }

  var raw = localStorage.getItem("series_filters");
  if (!raw) return "/series";

  try {
    var saved = JSON.parse(raw);
    var params = new URLSearchParams();
    if (saved.q) params.set("q", saved.q);
    if (saved.status) params.set("status", saved.status);
    if (saved.monitored) params.set("monitored", saved.monitored);
    if (saved.sort && saved.sort !== "title") params.set("sort", saved.sort);
    if (saved.per_page && saved.per_page !== "25") params.set("per_page", saved.per_page);
    if (saved.page && saved.page !== "1") params.set("page", saved.page);
    var qs = params.toString();
    return qs ? "/series?" + qs : "/series";
  } catch (_) {
    return "/series";
  }
}

function persistSeriesStateFromLocation() {
  if (window.location.pathname !== "/series") return;

  var params = new URLSearchParams(window.location.search);
  var filters = {};
  var keys = ["q", "status", "monitored", "sort", "per_page", "page"];

  for (var i = 0; i < keys.length; i++) {
    var key = keys[i];
    var value = params.get(key);
    if (value) filters[key] = value;
  }

  localStorage.setItem("series_last_url", window.location.pathname + window.location.search);
  localStorage.setItem("series_filters", JSON.stringify(filters));
  syncAppShellNavigation(document);
}

function isSeriesIndexLink(link) {
  return !!(link && link.hasAttribute && link.getAttribute("data-series-index-link") === "true");
}

function getSeriesIndexLinks(root) {
  var scope = root || document;
  return scope.querySelectorAll('a[data-series-index-link="true"]');
}

function applySeriesIndexUrl(link, seriesUrl) {
  if (!link) {
    return;
  }

  var href = link.getAttribute("href");
  var hxGet = link.getAttribute("hx-get");

  if (href !== null) {
    link.setAttribute("href", seriesUrl);
  }

  if (hxGet !== null) {
    link.setAttribute("hx-get", seriesUrl);
  }
}

/**
 * Update sidebar nav links to reflect the current URL.
 * Called after hx-boost swaps the main area (sidebar stays in DOM).
 */
function getSidebarNavLinks(root) {
  var scope = root || document;
  return scope.querySelectorAll("[data-nav-link='true']");
}

function splitClassList(value) {
  return (value || "")
    .split(/\s+/)
    .filter(function (token) {
      return !!token;
    });
}

function sidebarLinkIsActive(link, path) {
  var href = link.getAttribute("data-nav-path") || normalizePath(link.getAttribute("href"));
  var match = link.getAttribute("data-nav-match") || "prefix";
  return match === "exact" ? path === href : path.startsWith(href);
}

function applySidebarLinkActiveState(link, isActive) {
  var activeClasses =
    link.getAttribute("data-nav-active-classes") || "bg-pb-interactive-dim text-pb-interactive";
  var inactiveClasses =
    link.getAttribute("data-nav-inactive-classes") ||
    "text-pb-text-sec hover:bg-pb-card-hover hover:text-pb-text";

  splitClassList(activeClasses).forEach(function (c) {
    link.classList.toggle(c, isActive);
  });
  splitClassList(inactiveClasses).forEach(function (c) {
    link.classList.toggle(c, !isActive);
  });

  if (isActive) {
    link.setAttribute("aria-current", "page");
  } else {
    link.removeAttribute("aria-current");
  }
}

function updateSidebarActiveState(root) {
  var path = window.location.pathname;
  var links = getSidebarNavLinks(root);
  for (var i = 0; i < links.length; i++) {
    var link = links[i];
    applySidebarLinkActiveState(link, sidebarLinkIsActive(link, path));
  }
}

function syncSeriesIndexLinks(root) {
  var links = getSeriesIndexLinks(root);
  var seriesUrl = getSavedSeriesUrl();

  for (var i = 0; i < links.length; i++) {
    applySeriesIndexUrl(links[i], seriesUrl);
  }
}

function syncContentHistoryPolicy() {
  var content = document.getElementById("content");
  if (!content) {
    return;
  }

  if (_isDetailHistoryRestorePath(window.location.pathname)) {
    content.setAttribute("hx-history", "false");
    purgeHtmxHistoryEntry(window.location.pathname + window.location.search);
  } else {
    content.removeAttribute("hx-history");
  }
}

function normalizeHistoryUrl(url) {
  if (!url) {
    return "";
  }

  try {
    var parsed = new URL(url, window.location.origin);
    var pathname = parsed.pathname || "/";
    if (pathname.length > 1) {
      pathname = pathname.replace(/\/+$/, "");
    }
    return pathname + (parsed.search || "");
  } catch (_) {
    return String(url);
  }
}

function purgeHtmxHistoryEntry(url) {
  try {
    var key = "htmx-history-cache";
    var normalized = normalizeHistoryUrl(url);
    if (!normalized) {
      return;
    }

    var raw = localStorage.getItem(key);
    if (!raw) {
      return;
    }

    var entries = JSON.parse(raw);
    if (!Array.isArray(entries)) {
      return;
    }

    var filtered = entries.filter(function (entry) {
      return normalizeHistoryUrl(entry && entry.url) !== normalized;
    });

    if (filtered.length === entries.length) {
      return;
    }

    localStorage.setItem(key, JSON.stringify(filtered));
  } catch (_) {
    // Ignore malformed history cache; htmx will repopulate it as needed.
  }
}

function purgeHtmxHistoryEntries(predicate) {
  if (typeof predicate !== "function") {
    return;
  }

  try {
    var key = "htmx-history-cache";
    var raw = localStorage.getItem(key);
    if (!raw) {
      return;
    }

    var entries = JSON.parse(raw);
    if (!Array.isArray(entries)) {
      return;
    }

    var filtered = entries.filter(function (entry) {
      return !predicate(normalizeHistoryUrl(entry && entry.url), entry);
    });

    if (filtered.length === entries.length) {
      return;
    }

    localStorage.setItem(key, JSON.stringify(filtered));
  } catch (_) {
    // Ignore malformed history cache; htmx will repopulate it as needed.
  }
}

function purgeImportClientState(jobId) {
  setImportReviewAdvanced(jobId, false);
  clearImportReviewSelection(jobId);
  clearImportConflictCommitState(jobId);
  purgeHtmxHistoryEntries(function (normalizedUrl) {
    return (
      normalizedUrl === "/import" ||
      normalizedUrl.indexOf("/import?") === 0 ||
      normalizedUrl.indexOf("/import/") === 0
    );
  });
}

function destroyAlpineTree(root) {
  if (!root || !window.Alpine || typeof window.Alpine.destroyTree !== "function") {
    return;
  }

  try {
    window.Alpine.destroyTree(root);
  } catch (_) {
    // Leave teardown best-effort so one bad subtree cannot break shell navigation.
  }
}

function resolveHtmxLiveTarget(target) {
  if (!target || target.isConnected !== false || !target.id) {
    return target;
  }

  // outerHTML swaps leave event.detail.target pointing at the detached node.
  // Resolve its replacement so interactive directives bind to the live DOM.
  return document.getElementById(target.id) || target;
}

var _htmxRequestsNeedingAlpineInit = new WeakSet();

function prepareAlpineSwap(detail, target) {
  if (!detail || detail.shouldSwap === false) {
    return false;
  }

  if (detail.xhr) {
    _htmxRequestsNeedingAlpineInit.add(detail.xhr);
  }
  destroyAlpineTree(target);
  return true;
}

function _purgeDetailHistoryRestoreEntry(pathname, search) {
  var normalizedPath = normalizePath(pathname || window.location.pathname);
  if (!_isDetailHistoryRestorePath(normalizedPath)) {
    return;
  }

  purgeHtmxHistoryEntry(normalizedPath + (search || ""));
}

function syncAppShellNavigation(root) {
  updateSidebarActiveState(root);
  syncSeriesIndexLinks(root);
  syncContentHistoryPolicy();
}

function _syncAdminWorkspaceNav(navId, tabPrefix, defaultTab, root) {
  var scope = root && root.querySelector ? root : document;
  var nav = scope.querySelector("#" + navId) || document.getElementById(navId);
  if (!nav) {
    return;
  }

  var params = new URLSearchParams(window.location.search);
  var activeTab = params.get("tab") || defaultTab;
  var links = nav.querySelectorAll("[data-testid^='" + tabPrefix + "']");

  for (var i = 0; i < links.length; i += 1) {
    var link = links[i];
    var testid = link.getAttribute("data-testid") || "";
    var key = testid.replace(tabPrefix, "");
    var isActive = key === activeTab;

    link.classList.toggle("admin-nav-link-active", isActive);
    if (isActive) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  }
}

function syncSettingsWorkspaceNav(root) {
  if (window.location.pathname !== "/settings") {
    return;
  }

  _syncAdminWorkspaceNav("settings-tabs", "settings-tab-", "general", root);
}

function syncSecurityWorkspaceNav(root) {
  if (window.location.pathname !== "/security") {
    return;
  }

  _syncAdminWorkspaceNav("security-tabs", "security-tab-", "authentication", root);
}

function syncSystemWorkspaceNav(root) {
  if (window.location.pathname !== "/system") {
    return;
  }

  _syncAdminWorkspaceNav("system-tabs", "system-tab-", "about", root);
}

syncAppShellNavigation(document);
syncSettingsWorkspaceNav(document);
syncSecurityWorkspaceNav(document);
syncSystemWorkspaceNav(document);
persistSeriesStateFromLocation();

/* ── HTMX event listeners ────────────────────────────────── */

// Boosted nav links should get full-page responses (not HTMX partials).
// Remove HX-Request so server routes don't return fragment templates.
// For /series navigation, restore the last canonical series URL so the server
// returns the current list state in a single request.
document.body.addEventListener("htmx:configRequest", function (e) {
  if (e.detail.boosted) {
    e.detail.indicator = document.querySelector("[data-testid='app-header'] .htmx-indicator");
    delete e.detail.headers["HX-Request"];

    var requestedPath = e.detail.path || (e.detail.pathInfo && e.detail.pathInfo.requestPath) || "";
    var pathname = normalizePath(requestedPath);
    var triggerElt = e.detail.elt;

    // Inject saved series filters into sidebar /series navigation
    if (pathname === "/series" && isSeriesIndexLink(triggerElt)) {
      e.detail.path = getSavedSeriesUrl();
    }
  }
});

// Boosted links inside the content region default to targeting <body>.
// Redirect the swap to #content so the shell remains stable.
// Also serves as safety fallback: if response lacks #content, do a full page load.
document.body.addEventListener("htmx:beforeSwap", function (e) {
  // Content-area boosted links target <body> — redirect to #content
  if (e.detail.boosted && e.detail.target === document.body) {
    var responseText = e.detail.xhr && e.detail.xhr.responseText;
    var content = document.getElementById("content");
    if (responseText && content && responseText.indexOf('id="content"') !== -1) {
      _importEventSourceRegistry.closeAll("content-before-swap");
      _importEventSourceRegistry.clearSuspended();
      prepareAlpineSwap(e.detail, content);
      e.detail.target = content;
      e.detail.selectOverride = "#content";
      e.detail.swapOverride = "outerHTML";
      e.detail.ignoreTitle = true;
      return;
    }
    e.detail.shouldSwap = false;
    window.location.href = e.detail.pathInfo.requestPath;
    return;
  }

  // Safety fallback for shell content swaps: if response has no #content
  // (e.g. error page or redirect to login), do a full page load.
  if (
    e.detail.target &&
    e.detail.target.id === "content" &&
    e.detail.xhr &&
    e.detail.xhr.responseText &&
    e.detail.xhr.responseText.indexOf('id="content"') === -1
  ) {
    e.detail.shouldSwap = false;
    window.location.href = e.detail.pathInfo.requestPath;
    return;
  }

  if (e.detail && e.detail.target) {
    if (e.detail.target === document.body || e.detail.target.id === "content") {
      _importEventSourceRegistry.closeAll("content-before-swap");
      _importEventSourceRegistry.clearSuspended();
    }
    prepareAlpineSwap(e.detail, e.detail.target);
  }
});

document.addEventListener("htmx:afterRequest", function (e) {
  var detail = e.detail || {};
  if (!detail.xhr || !_htmxRequestsNeedingAlpineInit.has(detail.xhr)) {
    return;
  }

  _htmxRequestsNeedingAlpineInit.delete(detail.xhr);
  var target = resolveHtmxLiveTarget(detail.target);
  if (!target || target.isConnected === false) {
    return;
  }

  if (window.Alpine) {
    Alpine.initTree(target);
  }
  seedSearchFieldStates(target);
});

// After a shell content swap, update the header title from the full-page response.
function _decodeHtmlEntities(value) {
  if (typeof value !== "string" || value.indexOf("&") === -1) {
    return value || "";
  }

  var textarea = document.createElement("textarea");
  textarea.innerHTML = value;
  return textarea.value;
}

function _syncFooterDockFromResponse(responseText) {
  var dock = document.getElementById("page-footer-dock");
  if (!dock || typeof responseText !== "string" || responseText.trim() === "") {
    return;
  }

  try {
    var parser = new DOMParser();
    var doc = parser.parseFromString(responseText, "text/html");
    var nextDock = doc.getElementById("page-footer-dock");
    if (!nextDock) {
      return;
    }

    var replacementDock = nextDock.cloneNode(true);
    destroyAlpineTree(dock);
    dock.replaceWith(replacementDock);
    if (window.htmx && typeof window.htmx.process === "function") {
      window.htmx.process(replacementDock);
    }
    if (window.Alpine) {
      Alpine.initTree(replacementDock);
    }
  } catch (_) {
    // No-op; the current dock content is safer than a broken shell update.
  }
}

document.addEventListener("htmx:afterSwap", function (e) {
  if (e.detail.target && e.detail.target.id === "content" && e.detail.xhr) {
    _startContentSwapEnter();
    _syncFooterDockFromResponse(e.detail.xhr.responseText);

    var match = e.detail.xhr.responseText.match(/<title>([\s\S]*?)<\/title>/);
    if (match) {
      var title = _decodeHtmlEntities(match[1].trim());
      document.title = title;
    }
  }

  if (e.detail.target && e.detail.target.id === "settings-content") {
    syncSettingsWorkspaceNav(document);
  }

  if (e.detail.target && e.detail.target.id === "security-content") {
    syncSecurityWorkspaceNav(document);
  }

  if (e.detail.target && e.detail.target.id === "system-content") {
    syncSystemWorkspaceNav(document);
  }
});

function _isPrimaryUnmodifiedClick(event) {
  return !!(
    event &&
    event.type === "click" &&
    event.button === 0 &&
    !event.defaultPrevented &&
    !event.metaKey &&
    !event.ctrlKey &&
    !event.shiftKey &&
    !event.altKey
  );
}

function _setSeriesPaginationPendingMarker(isPending) {
  var target = document.getElementById("series-results-body");
  if (!target) {
    return;
  }

  if (isPending) {
    target.classList.add("htmx-request");
  } else {
    target.classList.remove("htmx-request");
  }
}

function _parseSeriesPaginationBundle(responseText) {
  if (typeof responseText !== "string" || responseText.trim() === "") {
    return null;
  }

  var wrapper = document.createElement("div");
  wrapper.innerHTML = responseText;

  var summary = wrapper.querySelector("#series-summary");
  var dock = wrapper.querySelector("#page-footer-dock");
  var resultsBody = wrapper.querySelector("#series-results-body");

  if (!summary || !dock || !resultsBody) {
    return null;
  }

  var summaryHtml = summary.innerHTML;
  var dockHtml = dock.innerHTML;
  var resultsHtml = resultsBody.outerHTML.trim();
  if (!resultsHtml) {
    return null;
  }

  return {
    summaryHtml: summaryHtml,
    dockHtml: dockHtml,
    resultsHtml: resultsHtml,
  };
}

function _extractSeriesPaginationCoverUrls(resultsHtml) {
  if (typeof resultsHtml !== "string" || resultsHtml.trim() === "") {
    return [];
  }

  var wrapper = document.createElement("div");
  wrapper.innerHTML = resultsHtml;

  var images = wrapper.querySelectorAll(
    "[data-testid='series-grid-cover'], [data-testid='series-compact-cover-link'] img"
  );
  var urls = [];
  var seen = {};

  for (var i = 0; i < images.length; i += 1) {
    var src = images[i].getAttribute("src") || images[i].src || "";
    if (!src || seen[src]) {
      continue;
    }

    seen[src] = true;
    urls.push(src);

    if (urls.length >= 18) {
      break;
    }
  }

  return urls;
}

function _warmSeriesPaginationCovers(resultsHtml) {
  var urls = _extractSeriesPaginationCoverUrls(resultsHtml);
  if (!urls.length) {
    return Promise.resolve();
  }

  var waits = urls.map(function (src) {
    return new Promise(function (resolve) {
      var img = new Image();
      var settled = false;

      function done() {
        if (settled) {
          return;
        }
        settled = true;
        resolve();
      }

      function finishAfterPaint() {
        if (typeof img.decode === "function") {
          img
            .decode()
            .catch(function () {
              return null;
            })
            .finally(function () {
              requestAnimationFrame(done);
            });
          return;
        }

        requestAnimationFrame(done);
      }

      img.addEventListener("load", finishAfterPaint, { once: true });
      img.addEventListener("error", done, { once: true });
      img.src = src;

      if (img.complete) {
        if (img.naturalWidth > 0) {
          finishAfterPaint();
        } else {
          done();
        }
      }
    });
  });

  return Promise.race([
    Promise.allSettled(waits),
    new Promise(function (resolve) {
      window.setTimeout(resolve, 500);
    }),
  ]);
}

function _applySeriesPaginationBundle(bundle) {
  var resultsBody = document.getElementById("series-results-body");
  var summary = document.getElementById("series-summary");
  var dock = document.getElementById("page-footer-dock");

  if (!resultsBody || !summary || !dock) {
    return null;
  }

  var resultsWrapper = document.createElement("div");
  resultsWrapper.innerHTML = bundle.resultsHtml;
  var replacementResultsBody = resultsWrapper.querySelector("#series-results-body");
  if (!replacementResultsBody || !resultsBody.parentNode) {
    return null;
  }

  summary.innerHTML = bundle.summaryHtml;
  dock.innerHTML = bundle.dockHtml;

  var resultsParent = resultsBody.parentNode;
  var resultsNextSibling = resultsBody.nextSibling;
  if (window.htmx && typeof window.htmx.remove === "function") {
    window.htmx.remove(resultsBody);
  } else {
    resultsBody.remove();
  }
  resultsParent.insertBefore(replacementResultsBody, resultsNextSibling);

  if (window.htmx && typeof window.htmx.process === "function") {
    window.htmx.process(summary);
    window.htmx.process(dock);
    window.htmx.process(replacementResultsBody);
  }

  if (window.Alpine) {
    Alpine.initTree(summary);
    Alpine.initTree(dock);
    Alpine.initTree(replacementResultsBody);
  }

  seedSearchFieldStates(summary);
  seedSearchFieldStates(dock);

  return replacementResultsBody;
}

function _dispatchSyntheticHtmxAfterSettle(target) {
  document.dispatchEvent(
    new CustomEvent("htmx:afterSettle", {
      detail: { target: target },
    })
  );
}

function _finishSeriesPaginationRequest(href) {
  if (href) {
    history.pushState({}, "", href);
    persistSeriesStateFromLocation();
  }

  window.__pbSeriesPaginationPending = false;
  window.__pbSeriesPaginationNextUrl = "";
  _setSeriesPaginationPendingMarker(false);
}

function _fetchSeriesPaginationBundle(href) {
  return fetch(href, {
    headers: {
      "HX-Request": "true",
      "X-Requested-With": "XMLHttpRequest",
    },
    credentials: "same-origin",
  }).then(function (response) {
    if (!response.ok) {
      throw new Error("Series pagination request failed");
    }

    return response.text();
  });
}

function _runSeriesResultsRequest(requestUrl, options) {
  var settings = options || {};
  var historyUrl = typeof settings.historyUrl === "string" ? settings.historyUrl : "";
  var shouldScrollToTop = settings.scrollToTop === true;

  return _fetchSeriesPaginationBundle(requestUrl)
    .then(function (responseText) {
      var bundle = _parseSeriesPaginationBundle(responseText);
      if (!bundle) {
        throw new Error("Series results response was incomplete");
      }

      return _warmSeriesPaginationCovers(bundle.resultsHtml).then(function () {
        return bundle;
      });
    })
    .then(function (bundle) {
      var resultsBody = _applySeriesPaginationBundle(bundle);
      if (!resultsBody) {
        throw new Error("Series results targets were missing");
      }

      if (shouldScrollToTop) {
        _scrollSeriesResultsToTop();
      }

      if (historyUrl) {
        history.pushState({}, "", historyUrl);
      }

      persistSeriesStateFromLocation();
      _dispatchSyntheticHtmxAfterSettle(resultsBody);
      return resultsBody;
    });
}

function _runSeriesPaginationRequest(href) {
  return _runSeriesResultsRequest(href, {
    historyUrl: href,
    scrollToTop: true,
  }).then(function (resultsBody) {
      _finishSeriesPaginationRequest(href);
      return resultsBody;
    });
}

window._pbRunSeriesResultsRequest = _runSeriesResultsRequest;

function _refreshSeriesResultLinks(root) {
  if (!root || !root.querySelectorAll) {
    return;
  }

  var links = root.querySelectorAll(
    "[data-testid='series-item-link'], [data-testid='series-grid-cover-link']"
  );
  for (var i = 0; i < links.length; i += 1) {
    var link = links[i];
    var parent = link.parentNode;
    if (!parent) {
      continue;
    }
    var clone = link.cloneNode(true);
    parent.replaceChild(clone, link);
  }

  if (window.htmx && typeof window.htmx.process === "function") {
    window.htmx.process(root);
  }
}

function _refreshSeriesIssueLinks(root) {
  if (!root || !root.querySelectorAll) {
    return;
  }

  var links = root.querySelectorAll("[data-testid='series-issue-link']");
  for (var i = 0; i < links.length; i += 1) {
    var link = links[i];
    var parent = link.parentNode;
    if (!parent) {
      continue;
    }
    var clone = link.cloneNode(true);
    parent.replaceChild(clone, link);
  }

  if (window.htmx && typeof window.htmx.process === "function") {
    window.htmx.process(root);
  }
}

function _scrollSeriesResultsToTop() {
  if (window.location.pathname !== "/series") {
    return;
  }
  var content = document.querySelector("#content");
  if (content) {
    content.scrollTo({ top: 0, behavior: "auto" });
    return;
  }
  window.scrollTo({ top: 0, behavior: "auto" });
}

function _scrollImportContentToTop() {
  if (window.location.pathname !== "/import") {
    return;
  }

  var content = document.getElementById("content");

  if (content) {
    content.scrollTo({ top: 0, behavior: "auto" });
    content.dispatchEvent(new Event("scroll"));
    return;
  }

  window.scrollTo({ top: 0, behavior: "auto" });
}

function _scrollAddSeriesContentToTop() {
  if (window.location.pathname !== "/series/add") {
    return;
  }

  var content = document.getElementById("content");
  var app = document.getElementById("add-series-app");

  if (app && typeof app.scrollIntoView === "function") {
    app.scrollIntoView({ block: "start", behavior: "auto" });
  }

  var mainArea = document.getElementById("main-area");
  if (mainArea) {
    mainArea.scrollTop = 0;
  }

  var scrollingElement = document.scrollingElement || document.documentElement;
  if (scrollingElement) {
    scrollingElement.scrollTop = 0;
  }
  window.scrollTo({ top: 0, behavior: "auto" });

  if (content) {
    content.scrollTo({ top: 0, behavior: "auto" });
    content.scrollTop = 0;
    content.dispatchEvent(new Event("scroll"));
  }
}

function _scrollAddSeriesContentToTopSoon() {
  _scrollAddSeriesContentToTop();
  requestAnimationFrame(function () {
    _scrollAddSeriesContentToTop();
    window.setTimeout(_scrollAddSeriesContentToTop, 40);
  });
}

function _scrollDownloadsContentToTop() {
  if (window.location.pathname !== "/downloads") {
    return;
  }

  var content = document.getElementById("content");

  if (content) {
    content.scrollTo({ top: 0, behavior: "auto" });
    content.dispatchEvent(new Event("scroll"));
    return;
  }

  window.scrollTo({ top: 0, behavior: "auto" });
}

function _scrollWhatsNewContentToTop() {
  if (window.location.pathname !== "/whats-new") {
    return;
  }

  var content = document.getElementById("content");

  if (content) {
    content.scrollTo({ top: 0, behavior: "auto" });
    content.dispatchEvent(new Event("scroll"));
    return;
  }

  window.scrollTo({ top: 0, behavior: "auto" });
}

function _scrollSettingsContentToTop() {
  if (window.location.pathname !== "/settings") {
    return;
  }

  var content = document.getElementById("content");

  if (content) {
    content.scrollTo({ top: 0, behavior: "auto" });
    content.dispatchEvent(new Event("scroll"));
    return;
  }

  window.scrollTo({ top: 0, behavior: "auto" });
}

function _scrollSecurityContentToTop() {
  if (window.location.pathname !== "/security") {
    return;
  }

  var content = document.getElementById("content");

  if (content) {
    content.scrollTo({ top: 0, behavior: "auto" });
    content.dispatchEvent(new Event("scroll"));
    return;
  }

  window.scrollTo({ top: 0, behavior: "auto" });
}

function _scrollSystemContentToTop() {
  if (window.location.pathname !== "/system") {
    return;
  }

  var content = document.getElementById("content");

  if (content) {
    content.scrollTo({ top: 0, behavior: "auto" });
    content.dispatchEvent(new Event("scroll"));
    return;
  }

  window.scrollTo({ top: 0, behavior: "auto" });
}

function _shouldScrollDownloadsContent(event) {
  if (!event || !event.detail || !event.detail.target || event.detail.target.id !== "downloads-content") {
    return false;
  }

  var requestPath =
    (event.detail.requestConfig && event.detail.requestConfig.path) ||
    (event.detail.pathInfo && event.detail.pathInfo.requestPath) ||
    (event.detail.xhr && event.detail.xhr.responseURL) ||
    "";

  if (!requestPath) {
    return false;
  }

  try {
    var parsed = new URL(requestPath, window.location.origin);
    return parsed.pathname === "/downloads" && parsed.searchParams.has("page");
  } catch (_) {
    return requestPath.indexOf("/downloads") !== -1 && requestPath.indexOf("page=") !== -1;
  }
}

function _shouldScrollWhatsNewContent(event) {
  if (
    !event ||
    !event.detail ||
    !event.detail.target ||
    event.detail.target.id !== "whats-new-results-body"
  ) {
    return false;
  }

  var requestPath =
    (event.detail.requestConfig && event.detail.requestConfig.path) ||
    (event.detail.pathInfo && event.detail.pathInfo.requestPath) ||
    (event.detail.xhr && event.detail.xhr.responseURL) ||
    "";

  if (!requestPath) {
    return false;
  }

  try {
    var parsed = new URL(requestPath, window.location.origin);
    return parsed.pathname === "/whats-new" && parsed.searchParams.has("page");
  } catch (_) {
    return requestPath.indexOf("/whats-new") !== -1 && requestPath.indexOf("page=") !== -1;
  }
}

function _shouldScrollSettingsContent(event) {
  if (!event || !event.detail || !event.detail.target || event.detail.target.id !== "settings-content") {
    return false;
  }

  var requestPath =
    (event.detail.requestConfig && event.detail.requestConfig.path) ||
    (event.detail.pathInfo && event.detail.pathInfo.requestPath) ||
    (event.detail.xhr && event.detail.xhr.responseURL) ||
    "";

  if (!requestPath) {
    return false;
  }

  try {
    var parsed = new URL(requestPath, window.location.origin);
    return parsed.pathname === "/settings" || parsed.pathname.indexOf("/htmx/settings/") === 0;
  } catch (_) {
    return requestPath.indexOf("/settings") !== -1 || requestPath.indexOf("/htmx/settings/") !== -1;
  }
}

function _shouldScrollSecurityContent(event) {
  if (!event || !event.detail || !event.detail.target || event.detail.target.id !== "security-content") {
    return false;
  }

  var requestPath =
    (event.detail.requestConfig && event.detail.requestConfig.path) ||
    (event.detail.pathInfo && event.detail.pathInfo.requestPath) ||
    (event.detail.xhr && event.detail.xhr.responseURL) ||
    "";

  if (!requestPath) {
    return false;
  }

  try {
    var parsed = new URL(requestPath, window.location.origin);
    return parsed.pathname === "/security" || parsed.pathname.indexOf("/htmx/security/") === 0;
  } catch (_) {
    return requestPath.indexOf("/security") !== -1 || requestPath.indexOf("/htmx/security/") !== -1;
  }
}

function _shouldScrollSystemContent(event) {
  if (!event || !event.detail || !event.detail.target || event.detail.target.id !== "system-content") {
    return false;
  }

  var requestPath =
    (event.detail.requestConfig && event.detail.requestConfig.path) ||
    (event.detail.pathInfo && event.detail.pathInfo.requestPath) ||
    (event.detail.xhr && event.detail.xhr.responseURL) ||
    "";

  if (!requestPath) {
    return false;
  }

  try {
    var parsed = new URL(requestPath, window.location.origin);
    return parsed.pathname === "/system" || parsed.pathname.indexOf("/htmx/system/") === 0;
  } catch (_) {
    return requestPath.indexOf("/system") !== -1 || requestPath.indexOf("/htmx/system/") !== -1;
  }
}

function _isBoostedContentSwapRequest(event) {
  if (!event || !event.detail || !event.detail.boosted) {
    return false;
  }

  var target = event.detail.target;
  return target === document.body || (target && target.id === "content");
}

function _setContentSwapPhase(phase) {
  var content = document.getElementById("content");
  if (!content) {
    return;
  }

  if (!phase) {
    content.removeAttribute("data-page-swap-phase");
    return;
  }

  content.setAttribute("data-page-swap-phase", phase);
}

function _startContentSwapEnter() {
  var content = document.getElementById("content");
  if (!content) {
    return;
  }

  content.setAttribute("data-page-swap-phase", "entering");

  requestAnimationFrame(function () {
    requestAnimationFrame(function () {
      if (content.isConnected) {
        content.removeAttribute("data-page-swap-phase");
      }
    });
  });
}

document.addEventListener("htmx:beforeRequest", function (e) {
  if (_isBoostedContentSwapRequest(e)) {
    _importEventSourceRegistry.closeAll("content-navigation", true);
    _setContentSwapPhase("leaving");
  }
});

document.addEventListener("htmx:afterRequest", function (e) {
  if (!_isBoostedContentSwapRequest(e)) {
    return;
  }

  if (e.detail.failed) {
    _setContentSwapPhase(null);
    _importEventSourceRegistry.resumeSuspended();
    return;
  }

  _importEventSourceRegistry.clearSuspended();
});

document.addEventListener(
  "click",
  function (event) {
    if (window.location.pathname !== "/series") {
      return;
    }

    var control =
      event.target && event.target.closest
        ? event.target.closest("#series-pagination [data-page-url]")
        : null;
    if (!control || !_isPrimaryUnmodifiedClick(event)) {
      return;
    }

    if (window.__pbSeriesPaginationPending) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }

    if (typeof htmx === "undefined") {
      return;
    }

    event.preventDefault();
    event.stopPropagation();

    var href = control.getAttribute("data-page-url") || control.getAttribute("href");
    if (!href) {
      return;
    }

    window.__pbSeriesPaginationPending = true;
    window.__pbSeriesPaginationNextUrl = href;
    _setSeriesPaginationPendingMarker(true);
    _runSeriesPaginationRequest(href).catch(function () {
      window.__pbSeriesPaginationPending = false;
      window.__pbSeriesPaginationNextUrl = "";
      _setSeriesPaginationPendingMarker(false);
      window.location.assign(href);
    });
  },
  true,
);

document.addEventListener(
  "click",
  function (event) {
    if (window.location.pathname !== "/import") {
      return;
    }

    var reviewControl =
      event.target && event.target.closest
        ? event.target.closest("[data-import-review-nav-url]")
        : null;
    if (!reviewControl || !_isPrimaryUnmodifiedClick(event)) {
      return;
    }

    var reviewUrl =
      reviewControl.getAttribute("data-import-review-nav-url") ||
      reviewControl.getAttribute("hx-get");
    var reviewTarget = reviewControl.getAttribute("hx-target") || "#import-step-review-shell";
    if (!reviewUrl || !document.querySelector(reviewTarget)) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();

    if (window.__pbImportReviewNavPending) {
      return;
    }

    if (typeof htmx === "undefined") {
      window.location.assign("/import");
      return;
    }

    var nextView = reviewControl.getAttribute("data-import-review-view") || "";
    var reviewRoot = document.querySelector("[data-testid='import-collection-review']");
    if (
      nextView &&
      reviewRoot &&
      window.Alpine &&
      typeof window.Alpine.$data === "function"
    ) {
      try {
        var reviewData = window.Alpine.$data(reviewRoot);
        if (reviewData && Object.prototype.hasOwnProperty.call(reviewData, "currentView")) {
          reviewData.currentView = nextView;
        }
      } catch (_) {
        // Leave the server-rendered shell authoritative if Alpine lookup fails.
      }
    }

    window.__pbImportReviewNavPending = true;
    loadImportReviewShell(reviewUrl)
      .then(function () {
        _scrollImportContentToTop();
      })
      .catch(function () {
        if (typeof showToast === "function") {
          showToast({
            message: "Unable to load the requested review view.",
            level: "error",
          });
        }
      })
      .finally(function () {
        window.__pbImportReviewNavPending = false;
      });
  },
  true,
);

document.addEventListener(
  "click",
  function (event) {
    if (window.location.pathname !== "/import") {
      return;
    }

    var control =
      event.target && event.target.closest
        ? event.target.closest(
            "[data-testid='import-review-pagination'] [data-page-url], " +
              "[data-testid='import-conflicts-pagination'] [data-page-url]",
          )
        : null;
    if (!control || !_isPrimaryUnmodifiedClick(event)) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();

    if (window.__pbImportFooterPaginationPending) {
      return;
    }

    if (typeof htmx === "undefined") {
      window.location.assign("/import");
      return;
    }

    var url = control.getAttribute("hx-get") || control.getAttribute("data-page-url");
    var target = control.getAttribute("hx-target");
    var swap = control.getAttribute("hx-swap") || "innerHTML";
    if (!url || !target || !document.querySelector(target)) {
      return;
    }

    window.__pbImportFooterPaginationPending = true;
    Promise.resolve(
      htmx.ajax("GET", url, {
        target: target,
        swap: swap,
      }),
    )
      .then(function () {
        _scrollImportContentToTop();
      })
      .catch(function () {
        if (typeof showToast === "function") {
          showToast({
            message: "Unable to load the requested import page.",
            level: "error",
          });
        }
      })
      .finally(function () {
        window.__pbImportFooterPaginationPending = false;
      });
  },
  true,
);

document.addEventListener(
  "click",
  function (event) {
    if (window.location.pathname !== "/series/add") {
      return;
    }

    var clickTarget = event.target;
    if (clickTarget && clickTarget.nodeType !== 1) {
      clickTarget = clickTarget.parentElement;
    }
    var control =
      clickTarget && clickTarget.closest
        ? clickTarget.closest("[data-page-url]")
        : null;
    var footer = control && control.closest ? control.closest("#page-footer-dock") : null;
    if (
      !control ||
      !footer ||
      !footer.querySelector("[data-testid='add-series-footer-dock']") ||
      !_isPrimaryUnmodifiedClick(event)
    ) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();

    if (window.__pbAddSeriesPaginationPending) {
      return;
    }

    var url = control.getAttribute("hx-get") || control.getAttribute("data-page-url");
    var historyUrl = control.getAttribute("data-page-url") || url;
    var target = control.getAttribute("hx-target") || "#add-series-results";
    var swap = control.getAttribute("hx-swap") || "outerHTML";
    if (!url || !target || !document.querySelector(target)) {
      if (historyUrl) {
        window.location.assign(historyUrl);
      }
      return;
    }

    if (typeof htmx === "undefined") {
      window.location.assign(historyUrl || url);
      return;
    }

    window.__pbAddSeriesPaginationPending = true;
    _scrollAddSeriesContentToTopSoon();
    Promise.resolve(
      htmx.ajax("GET", url, {
        target: target,
        swap: swap,
      }),
    )
      .then(function () {
        if (historyUrl) {
          history.pushState({}, "", historyUrl);
        }
        _scrollAddSeriesContentToTopSoon();
      })
      .catch(function () {
        if (typeof showToast === "function") {
          showToast({
            message: "Unable to load the requested search page.",
            level: "error",
          });
        }
      })
      .finally(function () {
        window.__pbAddSeriesPaginationPending = false;
      });
  },
  true,
);

// After hx-boost swaps #content: update sidebar active state + initialize Alpine
document.addEventListener("htmx:afterSettle", function (e) {
  var settledTarget = resolveHtmxLiveTarget(e.detail.target);

  if (settledTarget && (settledTarget.id === "main-area" || settledTarget.id === "content")) {
    if (settledTarget.id === "content") {
      window.__pbDetailHistoryRefreshPending = false;
    }
    if (settledTarget.id === "content") {
      settledTarget.scrollTop = 0;
      settledTarget.dispatchEvent(new Event("scroll"));
    }
    syncAppShellNavigation(document);

    _ensureUtilityWorkflowBackstop();
  }

  if (_shouldScrollDownloadsContent(e)) {
    _scrollDownloadsContentToTop();
  }

  if (_shouldScrollWhatsNewContent(e)) {
    _scrollWhatsNewContentToTop();
  }

  if (_shouldScrollSettingsContent(e)) {
    _scrollSettingsContentToTop();
    syncSettingsWorkspaceNav(document);
  }

  if (_shouldScrollSecurityContent(e)) {
    _scrollSecurityContentToTop();
    syncSecurityWorkspaceNav(document);
  }

  if (_shouldScrollSystemContent(e)) {
    _scrollSystemContentToTop();
    syncSystemWorkspaceNav(document);
  }

  if (
    window.location.pathname === "/series" &&
    settledTarget &&
    (
      settledTarget.id === "content" ||
      settledTarget.id === "series-results-body" ||
      settledTarget.id === "series-summary" ||
      settledTarget.id === "series-pagination"
    )
  ) {
    persistSeriesStateFromLocation();
  }

  if (
    window.location.pathname === "/series" &&
    settledTarget &&
    settledTarget.id === "series-results-body"
  ) {
    _refreshSeriesResultLinks(settledTarget);
  }

  if (
    window.location.pathname.indexOf("/series/") === 0 &&
    settledTarget &&
    (settledTarget.id === "content" || settledTarget.id === "series-issues-panel")
  ) {
    _refreshSeriesIssueLinks(settledTarget);
  }

  if (
    window.htmx &&
    typeof window.htmx.process === "function" &&
    settledTarget &&
    (settledTarget.id === "import-step-review" ||
      settledTarget.id === "import-step-review-shell" ||
      settledTarget.id === "conflicts-content")
  ) {
    window.htmx.process(settledTarget);
  }

  // Re-initialize Alpine components in the primary HTMX swap target.
  if (window.Alpine && settledTarget) {
    Alpine.initTree(settledTarget);
  }

  if (
    settledTarget &&
    (settledTarget.id === "import-step-review" ||
      settledTarget.id === "import-step-review-shell")
  ) {
    var reviewRoot =
      settledTarget.matches &&
      settledTarget.matches("[data-testid='import-collection-review']")
        ? settledTarget
        : settledTarget.querySelector("[data-testid='import-collection-review']");
    if (reviewRoot && window.Alpine && typeof window.Alpine.$data === "function") {
      try {
        var reviewData = window.Alpine.$data(reviewRoot);
        if (reviewData) {
          if (typeof reviewData.rehydrateAfterShellSwap === "function") {
            reviewData.rehydrateAfterShellSwap();
          } else {
            if (typeof reviewData.restoreSelection === "function") {
              reviewData.restoreSelection();
            }
            if (typeof reviewData.syncSelectionUi === "function") {
              reviewData.syncSelectionUi();
            }
          }
        }
      } catch (_) {
        // If Alpine scope lookup fails, leave the swapped shell intact.
      }
    }
  }

  if (settledTarget) {
    seedSearchFieldStates(settledTarget);
  }
});

document.addEventListener("htmx:oobAfterSwap", function (e) {
  var target = (e.detail && (e.detail.elt || e.detail.target)) || null;
  if (window.htmx && typeof window.htmx.process === "function" && target) {
    window.htmx.process(target);
  }
  if (window.Alpine && target) {
    Alpine.initTree(target);
  }
  if (target) {
    seedSearchFieldStates(target);
  }
});

function _sanitizeSidebarMobileOverlay(allowAlpine) {
  if (allowAlpine && window.Alpine && document.body && typeof window.Alpine.$data === "function") {
    try {
      var shellData = window.Alpine.$data(document.body);
      if (shellData && Object.prototype.hasOwnProperty.call(shellData, "sidebarMobileOpen")) {
        shellData.sidebarMobileOpen = false;
      }
    } catch (_) {
      // Fall through to DOM cleanup below.
    }
  }

  var backdrop = document.querySelector("[data-testid='sidebar-mobile-backdrop']");
  if (backdrop) {
    backdrop.style.display = "none";
    backdrop.setAttribute("x-cloak", "");
  }
}

function _findFileBrowserScope(element) {
  var current = element;
  while (current) {
    if (window.Alpine && typeof window.Alpine.$data === "function") {
      try {
        var data = window.Alpine.$data(current);
        if (data && data.fileBrowser) {
          return data;
        }
      } catch (_) {
        // Keep walking up until we find the owning Alpine scope.
      }
    }
    current = current.parentElement;
  }
  return null;
}

function _sanitizeFileBrowserOverlays(root, allowAlpine) {
  var queryRoot = root && root.querySelector ? root : document;
  var modals = queryRoot.querySelectorAll("[data-testid='file-browser-modal']");
  for (var i = 0; i < modals.length; i += 1) {
    var modal = modals[i];
    if (allowAlpine) {
      var scope = _findFileBrowserScope(modal);
      if (scope && scope.fileBrowser) {
        scope.fileBrowser.show = false;
        scope.fileBrowser.loading = false;
        scope.fileBrowser.error = "";
        scope.fileBrowser._multiSelected = new Set();
      }
    }
    modal.style.display = "none";
    modal.setAttribute("x-cloak", "");
  }
}

function _sanitizeTransientHistoryOverlays(root, allowAlpine) {
  _sanitizeSidebarMobileOverlay(allowAlpine);
  _sanitizeFileBrowserOverlays(root, allowAlpine);
}

function _isUtilityWorkflowPath(pathname) {
  var normalizedPath = normalizePath(pathname || window.location.pathname);
  return normalizedPath.indexOf("/utilities/") === 0 && normalizedPath !== "/utilities";
}

function _cloneHistoryStateObject(state) {
  if (!state || typeof state !== "object") {
    return {};
  }

  var clone = {};
  for (var key in state) {
    if (Object.prototype.hasOwnProperty.call(state, key)) {
      clone[key] = state[key];
    }
  }
  return clone;
}

function _ensureUtilityWorkflowBackstop() {
  if (
    !_isUtilityWorkflowPath(window.location.pathname) ||
    typeof history === "undefined" ||
    typeof history.replaceState !== "function" ||
    typeof history.pushState !== "function"
  ) {
    return;
  }

  var currentState = history.state;
  if (currentState && typeof currentState === "object" && currentState.__pbUtilityWorkflowTrap) {
    return;
  }

  var entryState = _cloneHistoryStateObject(currentState);
  entryState.__pbUtilityWorkflowEntry = true;
  entryState.__pbUtilityWorkflowReturnTo = "/utilities?tab=utilities";
  history.replaceState(entryState, "", window.location.href);

  var trapState = _cloneHistoryStateObject(entryState);
  trapState.__pbUtilityWorkflowTrap = true;
  history.pushState(trapState, "", window.location.href);
}

function _shouldRedirectUtilityWorkflowBack(event) {
  if (!_isUtilityWorkflowPath(window.location.pathname)) {
    return false;
  }

  var state = event && event.state;
  return !!(
    state &&
    typeof state === "object" &&
    state.__pbUtilityWorkflowEntry &&
    !state.__pbUtilityWorkflowTrap
  );
}

function _clearContentSwapPhase() {
  var content = document.getElementById("content");
  if (!content) {
    return;
  }

  content.removeAttribute("data-page-swap-phase");
}

function _rehydrateHistoryRestoredContent() {
  var content = document.getElementById("content");
  if (!content) {
    return;
  }

  _clearDetailHistoryHidden();
  _clearContentSwapPhase();
  _sanitizeTransientHistoryOverlays(content, true);

  if (window.Alpine) {
    Alpine.initTree(content);
  }

  if (window.location.pathname === "/series") {
    _refreshSeriesResultLinks(content);
  }

  if (/^\/series\/\d+$/.test(normalizePath(window.location.pathname))) {
    _refreshSeriesIssueLinks(content);
  }

  seedSearchFieldStates(content);
}

function _isDetailHistoryRestorePath(pathname) {
  var normalizedPath = normalizePath(pathname || window.location.pathname);
  return /^\/series\/\d+$/.test(normalizedPath) || /^\/issues\/\d+$/.test(normalizedPath);
}

function _refreshDetailContentAfterHistoryRestore() {
  var content = document.getElementById("content");
  var nextUrl = window.location.pathname + window.location.search;

  if (window.__pbDetailHistoryRefreshPending) {
    return;
  }

  window.__pbDetailHistoryRefreshPending = true;

  if (!content || typeof htmx === "undefined") {
    window.location.assign(nextUrl);
    return;
  }

  _setContentSwapPhase("leaving");

  var dock = document.getElementById("page-footer-dock");
  if (dock) {
    dock.innerHTML = "";
  }

  htmx.ajax("GET", nextUrl, {
    target: "#content",
    select: "#content",
    swap: "outerHTML",
  });
}

function _clearDetailHistoryHidden() {
  if (!_isDetailHistoryRestorePath(window.location.pathname)) {
    return;
  }

  var content = document.getElementById("content");
  if (content) {
    content.removeAttribute("data-detail-history-hidden");
  }
}

function _sanitizeSeriesDetailHistoryRoot(root, pathname, allowAlpine, stripRuntimeState) {
  if (!root || !/^\/series\/\d+$/.test(normalizePath(pathname || window.location.pathname))) {
    return;
  }

  var queryRoot = root.querySelector ? root : document;

  var dropdownRoot = queryRoot.querySelector(
    "[data-testid='series-detail-issues-status-select']"
  );
  if (dropdownRoot) {
    if (allowAlpine) {
      try {
        if (window.Alpine && typeof window.Alpine.$data === "function") {
          var dropdownData = window.Alpine.$data(dropdownRoot);
          if (dropdownData) {
            dropdownData.value = "";
            if (typeof dropdownData.syncFromValue === "function") {
              dropdownData.syncFromValue();
            }
            if (typeof dropdownData.syncInput === "function") {
              dropdownData.syncInput();
            }
            if (typeof dropdownData.applyPanelPlacement === "function") {
              dropdownData.applyPanelPlacement("bottom");
            }
            dropdownData.open = false;
          }
        }
      } catch (_) {
        // Fall through to DOM-level cleanup below.
      }
    }

    dropdownRoot.setAttribute("data-dropdown-value", "");
    dropdownRoot.setAttribute("data-dropdown-placement", "bottom");
    dropdownRoot.classList.remove("htmx-request");

    var trigger = dropdownRoot.querySelector("[data-dropdown-select-trigger]");
    if (trigger) {
      trigger.setAttribute("aria-expanded", "false");
    }

    var triggerLabel = dropdownRoot.querySelector(
      "[data-dropdown-select-trigger-label]"
    );
    if (triggerLabel) {
      triggerLabel.textContent = "All Status";
    }

    var hiddenInput = dropdownRoot.querySelector("[data-dropdown-select-input]");
    if (hiddenInput) {
      hiddenInput.value = "";
    }

    var panel = dropdownRoot.querySelector("[data-dropdown-select-panel]");
    if (panel) {
      panel.style.display = "none";
      panel.style.top = "calc(100% + 0.25rem)";
      panel.style.bottom = "auto";
      panel.style.maxHeight = "";
    }
  }

  var issuesPanel = queryRoot.querySelector("#series-issues-panel");
  if (issuesPanel) {
    var hxTrigger = issuesPanel.getAttribute("hx-trigger") || "";
    if (hxTrigger) {
      issuesPanel.setAttribute("data-pb-history-hx-trigger", hxTrigger);
      issuesPanel.removeAttribute("hx-trigger");
    }

    var hxGet = issuesPanel.getAttribute("hx-get") || "";
    if (hxGet) {
      try {
        var nextUrl = new URL(hxGet, window.location.origin);
        nextUrl.searchParams.delete("issue_status");
        nextUrl.searchParams.delete("page");
        issuesPanel.setAttribute(
          "hx-get",
          nextUrl.pathname + (nextUrl.search || "")
        );
      } catch (_) {
        // Ignore malformed relative URLs; a fresh detail fetch will correct them.
      }
    }

    if (stripRuntimeState && issuesPanel.parentNode) {
      var sanitizedClone = issuesPanel.cloneNode(true);
      issuesPanel.parentNode.replaceChild(sanitizedClone, issuesPanel);
    }
  }
}

function _sanitizeSeriesDetailHistoryState() {
  _sanitizeSeriesDetailHistoryRoot(document, window.location.pathname, true, false);
}

function _markDetailHistoryHidden() {
  if (!_isDetailHistoryRestorePath(window.location.pathname)) {
    return;
  }

  var content = document.getElementById("content");
  if (content) {
    content.setAttribute("data-detail-history-hidden", "true");
  }

  var dock = document.getElementById("page-footer-dock");
  if (dock) {
    dock.innerHTML = "";
  }
}

function _installDetailHistoryPopstateInterceptor() {
  var currentHandler = window.onpopstate;
  if (typeof currentHandler !== "function" || currentHandler.__pbDetailHistoryWrapped) {
    return false;
  }

  var wrappedHandler = function (event) {
    var pathname = window.location.pathname;
    var search = window.location.search;
    _purgeDetailHistoryRestoreEntry(pathname, search);

    if (_isDetailHistoryRestorePath(pathname)) {
      _refreshDetailContentAfterHistoryRestore();
      return;
    }

    return currentHandler.call(this, event);
  };
  wrappedHandler.__pbDetailHistoryWrapped = true;
  window.onpopstate = wrappedHandler;
  return true;
}

if (!_installDetailHistoryPopstateInterceptor()) {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      window.setTimeout(_installDetailHistoryPopstateInterceptor, 0);
    });
  } else {
    window.setTimeout(_installDetailHistoryPopstateInterceptor, 0);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", function () {
    window.setTimeout(_ensureUtilityWorkflowBackstop, 0);
  });
} else {
  window.setTimeout(_ensureUtilityWorkflowBackstop, 0);
}

// Browser back/forward — re-sync sidebar active state after restored navigation settles.
window.addEventListener(
  "popstate",
  function () {
    _purgeDetailHistoryRestoreEntry(window.location.pathname, window.location.search);
  },
  true,
);

window.addEventListener("popstate", function (event) {
  _clearContentSwapPhase();
  if (_shouldRedirectUtilityWorkflowBack(event)) {
    window.location.replace("/utilities?tab=utilities");
    return;
  }
  setTimeout(function () {
    _clearContentSwapPhase();
    syncAppShellNavigation(document);
    syncSettingsWorkspaceNav(document);
    syncSecurityWorkspaceNav(document);
    syncSystemWorkspaceNav(document);
  }, 0);
});

document.body.addEventListener("htmx:historyRestore", function (event) {
  _importEventSourceRegistry.clearSuspended();
  _clearContentSwapPhase();
  _sanitizeTransientHistoryOverlays(document, true);
  syncAppShellNavigation(document);
  syncSettingsWorkspaceNav(document);
  syncSecurityWorkspaceNav(document);
  syncSystemWorkspaceNav(document);
  if (_isDetailHistoryRestorePath(window.location.pathname)) {
    if (event && event.detail && event.detail.cacheMiss) {
      return;
    }
    _refreshDetailContentAfterHistoryRestore();
    return;
  }
  if (event && event.detail && event.detail.cacheMiss) {
    return;
  }
  _rehydrateHistoryRestoredContent();
});

document.body.addEventListener("htmx:beforeHistorySave", function (event) {
  var detail = event && event.detail ? event.detail : null;
  var historyElt = detail && detail.historyElt ? detail.historyElt : null;
  var path = detail && detail.path ? detail.path : window.location.pathname;
  _importEventSourceRegistry.closeAll("history-save");
  _importEventSourceRegistry.clearSuspended();
  _clearContentSwapPhase();
  _sanitizeTransientHistoryOverlays(historyElt || document, true);
  _sanitizeSeriesDetailHistoryRoot(historyElt || document, path, false, true);
});

window.addEventListener("pagehide", function () {
  _importEventSourceRegistry.closeAll("pagehide");
  _importEventSourceRegistry.clearSuspended();
  _clearContentSwapPhase();
  _sanitizeTransientHistoryOverlays(document, true);
  _markDetailHistoryHidden();
  _sanitizeSeriesDetailHistoryState();
});

document.addEventListener("visibilitychange", function () {
  if (document.visibilityState === "hidden") {
    _clearContentSwapPhase();
    _sanitizeTransientHistoryOverlays(document, true);
    _sanitizeSeriesDetailHistoryState();
    return;
  }

  _clearDetailHistoryHidden();
  _clearContentSwapPhase();
  syncAppShellNavigation(document);
});

window.addEventListener("pageshow", function (event) {
  if (!event.persisted) {
    return;
  }

  _clearContentSwapPhase();
  _sanitizeTransientHistoryOverlays(document, true);
  syncAppShellNavigation(document);
  if (_isDetailHistoryRestorePath(window.location.pathname)) {
    _markDetailHistoryHidden();
    _refreshDetailContentAfterHistoryRestore();
    return;
  }
  _rehydrateHistoryRestoredContent();
});

// Server-triggered toast via HX-Trigger response header: {"toast": {"message": "...", "level": "..."}}
document.addEventListener("toast", function (e) {
  showToast(e.detail || {});
});

// Show error toast on HTMX response errors
document.addEventListener("htmx:responseError", function (e) {
  var detail = e.detail || {};
  var redirectUrl =
    detail.xhr && typeof detail.xhr.getResponseHeader === "function"
      ? detail.xhr.getResponseHeader("X-Pullbox-Auth-Redirect")
      : "";
  if (
    detail.xhr &&
    (redirectUrl || detail.xhr.status === 401) &&
    _beginAuthExpiryRedirect(redirectUrl || "/login")
  ) {
    return;
  }
  var message = "Request failed";
  if (detail.xhr && detail.xhr.status) {
    message = "Error " + detail.xhr.status + ": " + (detail.xhr.statusText || "Request failed");
  }
  showToast({ message: message, level: "error" });
});
