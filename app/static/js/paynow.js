(function () {
  "use strict";
  var isOwner = document.body.getAttribute("data-owner") === "1";

  var amount = document.getElementById("pay-amount");
  var sheet = amount ? amount.form : null;
  var hint = document.getElementById("amt-hint");
  var errBox = document.getElementById("amt-error");
  var submit = sheet ? sheet.querySelector('button[type="submit"]') : null;
  var remaining = sheet ? parseInt(sheet.getAttribute("data-remaining"), 10) : NaN;
  var total = sheet ? parseInt(sheet.getAttribute("data-total"), 10) : NaN;
  var already = sheet ? parseInt(sheet.getAttribute("data-paid"), 10) || 0 : 0;

  function inr(n) { return "\u20B9" + n.toLocaleString("en-IN"); }

  function pct(part) {
    if (!(total > 0)) return "";
    var p = Math.round(part / total * 100);
    if (part > 0 && p < 1) return " (<1%)";
    return " (" + p + "%)";
  }

  function parseAmount(raw) {
    var t = String(raw).replace(/[\s,\u20B9]/g, "");
    if (t === "") return { error: "Enter the amount to pay." };
    if (!/^\d+(\.\d+)?$/.test(t)) return { error: "Enter a number, for example 5000." };
    var n = Number(t);
    if (!(n > 0)) return { error: "Amount must be more than \u20B90." };
    if (n !== Math.floor(n)) return { error: "Enter whole rupees (no paise)." };
    if (remaining > 0 && n > remaining) return { error: "Amount cannot be more than the remaining " + inr(remaining) + "." };
    return { value: n };
  }

  function render() {
    var res = parseAmount(amount.value);
    var ok = res.value !== undefined;
    amount.classList.toggle("invalid", !ok);
    amount.setAttribute("aria-invalid", ok ? "false" : "true");
    if (errBox) { errBox.hidden = ok; errBox.textContent = ok ? "" : res.error; }
    if (hint) {
      hint.hidden = !ok;
      if (ok) {
        var left = remaining - res.value;
        hint.textContent = left === 0
          ? "Pays this off in full"
          : "Paying " + inr(res.value) + " of " + inr(total > 0 ? total : remaining) + pct(res.value) +
            " \u00B7 Remaining after this payment: " + inr(left) + pct(left) +
            (already > 0 ? " \u00B7 Already paid " + inr(already) : "");
      }
    }
    if (submit) submit.disabled = !ok;
  }

  if (amount && sheet && sheet.hasAttribute("data-partial")) {
    amount.addEventListener("input", render);
    sheet.addEventListener("submit", function (e) {
      if (parseAmount(amount.value).value === undefined) { e.preventDefault(); amount.focus(); }
    });
    render();
  }

  // Owners must quote the UTR for UPI payments (the server checks too).
  if (!isOwner) return;
  document.querySelectorAll("form").forEach(function (form) {
    var mode = form.querySelector('[name="payment_mode"]');
    var note = form.querySelector('input[name="payment_note"]');
    if (!mode || !note) return;
    var label = form.querySelector("#utr-label");
    var original = note.getAttribute("placeholder") || "";
    function apply() {
      var upi = mode.value === "UPI";
      note.required = upi;
      if (upi) {
        note.setAttribute("pattern", "[A-Za-z0-9]{8,30}");
        note.setAttribute("minlength", "8");
        note.setAttribute("maxlength", "30");
        note.title = "UPI reference (UTR): 8 to 30 letters or digits, no spaces";
        note.placeholder = "UTR (required for UPI)";
      } else {
        note.removeAttribute("pattern");
        note.removeAttribute("minlength");
        note.setAttribute("maxlength", "120");
        note.removeAttribute("title");
        note.placeholder = label ? "Optional note" : original;
      }
      if (label) label.textContent = upi ? "UPI reference (UTR) \u2014 required" : "Note (optional)";
    }
    mode.addEventListener("change", apply);
    apply();
  });
})();
