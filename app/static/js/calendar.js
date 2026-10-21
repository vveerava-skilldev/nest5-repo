(function () {
  "use strict";
  var root = document.getElementById("cal");
  if (!root) return;
  var data = {};
  try { data = JSON.parse(document.getElementById("cal-data").textContent); } catch (e) { data = {}; }

  var panel = document.getElementById("cal-panel");
  var els = {
    type: document.getElementById("cal-p-type"),
    title: document.getElementById("cal-p-title"),
    when: document.getElementById("cal-p-when"),
    status: document.getElementById("cal-p-status"),
    lines: document.getElementById("cal-p-lines"),
    link: document.getElementById("cal-p-link"),
    del: document.getElementById("cal-p-delete")
  };
  var STATUS = {
    overdue: ["Overdue", "bad"], paid: ["Paid", "ok"], done: ["Done", "ok"],
    pending: ["Pending", "warn"], open: ["Scheduled", ""]
  };

  function openItem(id) {
    var it = data[id];
    if (!it) return;
    panel.className = "cal-panel t-" + it.type + " s-" + it.status;
    els.type.textContent = it.type_label;
    els.title.textContent = it.title;
    var when = it.date + (it.end_date ? " to " + it.end_date : "") + (it.time ? " · " + it.time : "");
    els.when.textContent = when + (it.projected ? " (expected)" : "");
    var st = it.type === "event" ? ["", ""] : (STATUS[it.status] || ["", ""]);
    els.status.textContent = st[0];
    els.status.className = "badge " + st[1];
    els.status.parentNode.hidden = !st[0];
    els.lines.innerHTML = "";
    it.lines.forEach(function (line) {
      var li = document.createElement("li");
      li.textContent = line;
      els.lines.appendChild(li);
    });
    if (it.link) {
      els.link.href = it.link;
      els.link.textContent = (it.link_label || "Open") + " →";
      els.link.hidden = false;
    } else {
      els.link.hidden = true;
    }
    if (els.del) {
      if (it.event_id) {
        els.del.action = "/calendar/events/" + it.event_id + "/delete";
        els.del.hidden = false;
      } else {
        els.del.hidden = true;
      }
    }
    panel.hidden = false;
  }

  function closePanel() { panel.hidden = true; }

  root.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-id]");
    if (btn && root.contains(btn)) { openItem(btn.getAttribute("data-id")); ev.stopPropagation(); return; }
    if (!ev.target.closest("#cal-panel")) closePanel();
  });
  document.getElementById("cal-p-close").addEventListener("click", closePanel);
  document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") closePanel(); });

  function refreshAgenda() {
    document.querySelectorAll(".cal-agenda-day").forEach(function (day) {
      var visible = Array.prototype.some.call(day.querySelectorAll(".cal-row"), function (row) {
        return row.offsetParent !== null || getComputedStyle(row).display !== "none";
      });
      day.hidden = !visible;
    });
  }

  document.querySelectorAll(".cal-filter").forEach(function (chip) {
    chip.addEventListener("click", function () {
      var on = chip.getAttribute("aria-pressed") !== "true";
      chip.setAttribute("aria-pressed", on ? "true" : "false");
      root.classList.toggle("hide-" + chip.getAttribute("data-type"), !on);
      refreshAgenda();
    });
  });
})();
