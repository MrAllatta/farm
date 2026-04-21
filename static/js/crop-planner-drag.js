/**
 * Crop planner: drag planting bars onto week cells to change block + plant week.
 * Uses document-level delegation so replaced bars stay draggable without duplicate listeners.
 */
(function () {
  "use strict";

  var dragged = null;

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

  function initCropPlannerDrag() {
    var moveUrl = window.CROP_PLANNER_MOVE_URL;
    var warnEl = document.getElementById("drag-warnings");
    if (!moveUrl || !warnEl || window.__cropPlannerDragDelegationBound) {
      return;
    }
    window.__cropPlannerDragDelegationBound = true;

    document.addEventListener("dragstart", function (e) {
      var bar = e.target.closest(".planting-bar[draggable=true]");
      if (!bar) {
        return;
      }
      dragged = {
        plantingId: bar.getAttribute("data-planting"),
        fromBlockId: bar.getAttribute("data-block-id"),
      };
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", dragged.plantingId);
    });

    document.addEventListener("dragend", function (e) {
      if (e.target.closest(".planting-bar[draggable=true]")) {
        dragged = null;
      }
    });

    document.addEventListener("dragover", function (e) {
      var cell = e.target.closest(".week-cell");
      if (!cell || !dragged) {
        return;
      }
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
    });

    document.addEventListener("drop", function (e) {
      var cell = e.target.closest(".week-cell");
      if (!cell || !dragged) {
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
              "td.planting-bar[data-planting=\"" + payload.plantingId + "\"]"
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

  document.addEventListener("DOMContentLoaded", initCropPlannerDrag);
})();
