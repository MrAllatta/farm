/**
 * Crop planner: drag planting bars onto week cells to change block + plant week.
 */
(function () {
  "use strict";

  function getCsrfToken() {
    var input = document.querySelector("#csrf-helper input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  function initCropPlannerDrag() {
    var moveUrl = window.CROP_PLANNER_MOVE_URL;
    var warnEl = document.getElementById("drag-warnings");
    if (!moveUrl || !warnEl) {
      return;
    }

    var dragged = null;

    document.querySelectorAll(".planting-bar[draggable=true]").forEach(function (bar) {
      bar.addEventListener("dragstart", function (e) {
        dragged = {
          plantingId: bar.getAttribute("data-planting"),
          fromBlockId: bar.getAttribute("data-block-id"),
        };
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", dragged.plantingId);
      });
      bar.addEventListener("dragend", function () {
        dragged = null;
      });
    });

    document.querySelectorAll(".week-cell").forEach(function (cell) {
      cell.addEventListener("dragover", function (e) {
        if (!dragged) {
          return;
        }
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
      });
      cell.addEventListener("drop", function (e) {
        if (!dragged) {
          return;
        }
        e.preventDefault();
        var blockId = cell.getAttribute("data-block");
        var week = cell.getAttribute("data-week");
        if (!blockId || !week) {
          return;
        }
        warnEl.textContent = "Saving…";
        fetch(moveUrl, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": getCsrfToken(),
          },
          body: JSON.stringify({
            planting_id: parseInt(dragged.plantingId, 10),
            block_id: parseInt(blockId, 10),
            week: parseInt(week, 10),
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
            window.location.reload();
          })
          .catch(function () {
            warnEl.textContent = "Move failed (network).";
          });
      });
    });
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  document.addEventListener("DOMContentLoaded", initCropPlannerDrag);
})();
