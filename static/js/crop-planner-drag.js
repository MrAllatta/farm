/**
 * Crop planner: HTML5-drag planting bars onto week cells to change block + plant week.
 * Pointer-drag on fallow cells: range across weeks → new planting (single week) or succession form (multi-week) in drawer.
 * Drag-to-clear: drop planting bar on trash zone → confirm delete.
 * Uses document-level delegation so replaced bars stay draggable without duplicate listeners.
 */
(function () {
  "use strict";

  var dragged = null;
  var dragImageEl = null;
  var DRAG_PX = 6;

  function getCsrfToken() {
    var input = document.querySelector("#csrf-helper input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function pointInTrash(clientX, clientY, trashEl) {
    if (!trashEl) {
      return false;
    }
    var r = trashEl.getBoundingClientRect();
    return clientX >= r.left && clientX <= r.right && clientY >= r.top && clientY <= r.bottom;
  }

  function getPlantingDrawerBackdrop() {
    return document.getElementById("planting-detail-backdrop");
  }

  function openPlantingDrawer() {
    var el = document.getElementById("planting-detail");
    if (!el) {
      return;
    }
    el.classList.add("planting-drawer--open");
    el.setAttribute("aria-hidden", "false");
    var backdrop = getPlantingDrawerBackdrop();
    if (backdrop) {
      backdrop.classList.add("planting-drawer-backdrop--open");
      backdrop.setAttribute("aria-hidden", "false");
    }
  }

  window.cropPlannerCloseDrawer = function () {
    var el = document.getElementById("planting-detail");
    if (!el) {
      return;
    }
    el.classList.remove("planting-drawer--open");
    el.setAttribute("aria-hidden", "true");
    el.innerHTML = "";
    var backdrop = getPlantingDrawerBackdrop();
    if (backdrop) {
      backdrop.classList.remove("planting-drawer-backdrop--open");
      backdrop.setAttribute("aria-hidden", "true");
    }
  };

  function prefillCreateUrl(blockId, week) {
    var tpl = window.CROP_PLANNER_PREFILL_URL_TEMPLATE;
    if (!tpl) {
      return null;
    }
    return tpl.replace("__BLOCK__", String(blockId)).replace("__WEEK__", String(week));
  }

  function successionPrefillUrl(blockId, firstWeek, lastWeek, blockType) {
    var base = window.CROP_PLANNER_SUCCESSION_URL || "/planning/succession/new/";
    var q = new URLSearchParams();
    q.set("block", String(blockId));
    q.set("first_plant_week", String(firstWeek));
    q.set("last_plant_week", String(lastWeek));
    q.set("block_type", blockType || "field");
    var sep = base.indexOf("?") >= 0 ? "&" : "?";
    return base + sep + q.toString();
  }

  function loadDrawer(url) {
    if (typeof htmx !== "undefined") {
      htmx.ajax("GET", url, { target: "#planting-detail", swap: "innerHTML" });
    } else {
      window.location.href = url;
    }
  }

  function weekCellsInRow(tr) {
    return Array.prototype.slice.call(tr.querySelectorAll("td.week-cell"));
  }

  function clearRangeClass(tr) {
    weekCellsInRow(tr).forEach(function (td) {
      td.classList.remove("week-cell--range-selecting");
    });
  }

  function initFallowRangeSelect() {
    if (window.__cropPlannerFallowRangeBound) {
      return;
    }
    if (!document.querySelector(".planning-matrix")) {
      return;
    }
    window.__cropPlannerFallowRangeBound = true;

    var active = null;

    function applyRange(tr, i0, i1) {
      var cells = weekCellsInRow(tr);
      clearRangeClass(tr);
      var lo = Math.min(i0, i1);
      var hi = Math.max(i0, i1);
      var j;
      for (j = lo; j <= hi; j += 1) {
        if (cells[j] && cells[j].classList.contains("fallow")) {
          cells[j].classList.add("week-cell--range-selecting");
        }
      }
    }

    document.addEventListener("mousedown", function (e) {
      if (e.button !== 0) {
        return;
      }
      var cell = e.target.closest("td.week-cell.fallow");
      if (!cell) {
        return;
      }
      if (e.target.closest(".planting-bar")) {
        return;
      }
      var row = cell.closest("tr.planting-row");
      if (!row || !row.closest(".planning-matrix")) {
        return;
      }
      var cells = weekCellsInRow(row);
      var idx = cells.indexOf(cell);
      if (idx < 0) {
        return;
      }
      var table = row.closest(".planning-matrix");
      active = {
        row: row,
        table: table,
        startIdx: idx,
        blockId: cell.getAttribute("data-block"),
        blockType:
          row.getAttribute("data-block-type") ||
          cell.getAttribute("data-block-type") ||
          "field",
        cells: cells,
        startX: e.clientX,
        startY: e.clientY,
      };
      try {
        window.getSelection().removeAllRanges();
      } catch (errSel) {
        /* ignore */
      }
      applyRange(row, idx, idx);
      if (table) {
        table.classList.add("is-range-selecting");
      }
    });

    document.addEventListener("mousemove", function (e) {
      if (!active) {
        return;
      }
      var row = active.row;
      var el = document.elementFromPoint(e.clientX, e.clientY);
      var cell = el && el.closest("td.week-cell");
      if (!cell || !row.contains(cell) || !cell.classList.contains("fallow")) {
        applyRange(row, active.startIdx, active.startIdx);
        return;
      }
      if (cell.getAttribute("data-block") !== active.blockId) {
        applyRange(row, active.startIdx, active.startIdx);
        return;
      }
      var idx = active.cells.indexOf(cell);
      if (idx < 0) {
        return;
      }
      applyRange(row, active.startIdx, idx);
    });

    document.addEventListener("mouseup", function (e) {
      if (!active) {
        return;
      }
      if (e.button !== 0) {
        return;
      }
      var row = active.row;
      var table = active.table;
      var cells = active.cells;
      var startIdx = active.startIdx;
      var sx = active.startX;
      var sy = active.startY;
      var blockId = active.blockId;
      var blockType = active.blockType;
      active = null;
      if (table) {
        table.classList.remove("is-range-selecting");
      }

      var el = document.elementFromPoint(e.clientX, e.clientY);
      var endCell = el && el.closest("td.week-cell.fallow");
      var endIdx = startIdx;
      if (endCell && row.contains(endCell) && endCell.getAttribute("data-block") === blockId) {
        endIdx = cells.indexOf(endCell);
      }
      if (endIdx < 0) {
        endIdx = startIdx;
      }

      var lo = Math.min(startIdx, endIdx);
      var hi = Math.max(startIdx, endIdx);
      var dist = Math.hypot(e.clientX - sx, e.clientY - sy);

      clearRangeClass(row);

      if (lo === hi && dist < DRAG_PX) {
        return;
      }

      window.__cropPlannerSuppressNextClick = true;
      window.setTimeout(function () {
        window.__cropPlannerSuppressNextClick = false;
      }, 400);

      if (hi > lo) {
        var w0 = parseInt(cells[lo].getAttribute("data-week"), 10);
        var w1 = parseInt(cells[hi].getAttribute("data-week"), 10);
        loadDrawer(successionPrefillUrl(blockId, w0, w1, blockType));
        return;
      }

      var week = cells[lo].getAttribute("data-week");
      var url = prefillCreateUrl(blockId, week);
      if (url) {
        loadDrawer(url);
      }
    });

    document.addEventListener(
      "click",
      function (ev) {
        if (!window.__cropPlannerSuppressNextClick) {
          return;
        }
        var t = ev.target.closest("td.week-cell.fallow");
        if (t) {
          ev.preventDefault();
          ev.stopPropagation();
        }
      },
      true
    );
  }

  function initCropPlannerDrag() {
    var moveUrl = window.CROP_PLANNER_MOVE_URL;
    var deleteUrl = window.CROP_PLANNER_DELETE_URL;
    var warnEl = document.getElementById("drag-warnings");
    var trashEl = document.getElementById("drag-trash");
    if (!moveUrl || !warnEl || window.__cropPlannerDragDelegationBound) {
      return;
    }
    window.__cropPlannerDragDelegationBound = true;

    var lastHighlighted = null;
    var trashActive = false;

    function clearDropHighlight() {
      if (lastHighlighted) {
        lastHighlighted.classList.remove("week-cell--drop-target");
        lastHighlighted = null;
      }
      if (trashEl) {
        trashEl.classList.remove("drag-trash--active");
      }
      trashActive = false;
    }

    function resolveDropCell(clientX, clientY) {
      var el = document.elementFromPoint(clientX, clientY);
      if (!el) {
        return null;
      }
      return el.closest(".week-cell");
    }

    document.addEventListener("dragstart", function (e) {
      var bar = e.target.closest(".planting-bar[draggable=true]");
      if (!bar) {
        return;
      }
      if (window.cropPlannerCloseDrawer) {
        window.cropPlannerCloseDrawer();
      }
      window.__cropPlannerDragActive = true;
      dragged = {
        plantingId: bar.getAttribute("data-planting"),
        fromBlockId: bar.getAttribute("data-block-id"),
        barEl: bar,
      };
      bar.classList.add("planting-bar--dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", dragged.plantingId);
      if (dragImageEl) {
        var rect = bar.getBoundingClientRect();
        var gw = Math.max(24, Math.floor(rect.width));
        dragImageEl.style.width = gw + "px";
        dragImageEl.textContent = (bar.querySelector(".planting-label") || {}).textContent || "Planting";
        var gh = dragImageEl.offsetHeight || 26;
        var hotX = Math.round(e.clientX - rect.left);
        var hotY = Math.round(e.clientY - rect.top);
        hotX = Math.max(0, Math.min(hotX, gw - 1));
        if (rect.height > 0.5) {
          hotY = Math.round((hotY / rect.height) * gh);
        } else {
          hotY = Math.round(gh / 2);
        }
        hotY = Math.max(0, Math.min(hotY, gh - 1));
        try {
          e.dataTransfer.setDragImage(dragImageEl, hotX, hotY);
        } catch (err2) {
          /* ignore */
        }
      }
    });

    document.addEventListener("dragend", function () {
      clearDropHighlight();
      if (dragged && dragged.barEl) {
        dragged.barEl.classList.remove("planting-bar--dragging");
      }
      dragged = null;
      window.__cropPlannerDragActive = false;
    });

    document.addEventListener("dragover", function (e) {
      if (!dragged) {
        return;
      }
      if (trashEl && dragged.plantingId !== "new" && pointInTrash(e.clientX, e.clientY, trashEl)) {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        if (!trashActive) {
          clearDropHighlight();
          trashEl.classList.add("drag-trash--active");
          trashActive = true;
        }
        return;
      }
      if (trashActive && trashEl) {
        trashEl.classList.remove("drag-trash--active");
        trashActive = false;
      }

      var cell = resolveDropCell(e.clientX, e.clientY);
      if (cell) {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
      }
      if (cell !== lastHighlighted) {
        clearDropHighlight();
        if (cell) {
          cell.classList.add("week-cell--drop-target");
          lastHighlighted = cell;
        }
      }
    });

    document.addEventListener("drop", function (e) {
      clearDropHighlight();
      if (!dragged) {
        return;
      }

      if (
        trashEl &&
        deleteUrl &&
        dragged.plantingId !== "new" &&
        pointInTrash(e.clientX, e.clientY, trashEl)
      ) {
        e.preventDefault();
        var pid = parseInt(dragged.plantingId, 10);
        if (!pid || !window.confirm("Remove this planting from the plan? This cannot be undone.")) {
          return;
        }
        warnEl.textContent = "Deleting…";
        fetch(deleteUrl, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": getCsrfToken(),
          },
          body: JSON.stringify({ planting_id: pid }),
        })
          .then(function (r) {
            return r.json();
          })
          .then(function (data) {
            if (!data.ok) {
              warnEl.textContent = "Delete failed: " + (data.error || "unknown");
              return;
            }
            window.location.reload();
          })
          .catch(function () {
            warnEl.textContent = "Delete failed (network).";
          });
        return;
      }

      var cell = resolveDropCell(e.clientX, e.clientY);
      if (!cell) {
        cell = e.target.closest(".week-cell");
      }
      if (!cell) {
        return;
      }
      e.preventDefault();

      var blockId = cell.getAttribute("data-block");
      var week = cell.getAttribute("data-week");
      if (!blockId || !week) {
        return;
      }

      warnEl.textContent = "Saving…";
      var payload = dragged;
      fetch(moveUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": getCsrfToken(),
        },
        body: JSON.stringify({
          planting_id: parseInt(payload.plantingId, 10),
          block_id: parseInt(blockId, 10),
          week: parseInt(week, 10),
          from_block_id: parseInt(payload.fromBlockId, 10),
          matrix_date: window.CROP_PLANNER_MATRIX_DATE || null,
        }),
      })
        .then(function (r) {
          return r.json();
        })
        .then(function (data) {
          if (!data.ok) {
            warnEl.textContent = "Move failed: " + (data.error || "unknown");
            return;
          }
          if (data.warnings && data.warnings.length) {
            warnEl.innerHTML =
              "<strong>Warnings:</strong> " + data.warnings.map(escapeHtml).join(" · ");
          } else {
            warnEl.textContent = "";
          }
          if (data.reload) {
            window.location.reload();
            return;
          }
          if (data.html) {
            var barEl = document.querySelector(
              'td.planting-bar[data-planting="' + payload.plantingId + '"]'
            );
            if (barEl) {
              barEl.outerHTML = data.html;
            }
          } else {
            window.location.reload();
          }
        })
        .catch(function () {
          warnEl.textContent = "Move failed (network).";
        });
    });
  }

  dragImageEl = document.createElement("div");
  dragImageEl.className = "crop-planner-drag-ghost";
  dragImageEl.setAttribute("aria-hidden", "true");
  document.addEventListener("DOMContentLoaded", function () {
    document.body.appendChild(dragImageEl);
    initCropPlannerDrag();
    initFallowRangeSelect();
    document.body.addEventListener("htmx:afterSwap", function (ev) {
      var t = ev.detail && ev.detail.target;
      if (t && t.id === "planting-detail" && !window.__cropPlannerDragActive) {
        openPlantingDrawer();
      }
    });

    var backdropEl = getPlantingDrawerBackdrop();
    if (backdropEl && !window.__cropPlannerBackdropClickBound) {
      window.__cropPlannerBackdropClickBound = true;
      backdropEl.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        if (window.cropPlannerCloseDrawer) {
          window.cropPlannerCloseDrawer();
        }
      });
    }
  });
})();
