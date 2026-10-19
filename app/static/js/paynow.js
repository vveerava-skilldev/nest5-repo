(function () {
  "use strict";
  var isOwner = document.body.getAttribute("data-owner") === "1";

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  function flash(btn, ok) {
    var old = btn.getAttribute("data-label") || btn.textContent;
    btn.setAttribute("data-label", old);
    btn.textContent = ok ? "Copied" : "Press and hold to copy";
    btn.classList.toggle("copied", ok);
    setTimeout(function () { btn.textContent = old; btn.classList.remove("copied"); }, 1600);
  }

  function copy(text, btn) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        function () { flash(btn, true); },
        function () { flash(btn, fallbackCopy(text)); }
      );
    } else {
      flash(btn, fallbackCopy(text));
    }
  }

  var amount = document.getElementById("pay-amount");
  var topAmt = document.getElementById("pay-amount-top");
  var box = document.querySelector(".sv-paynow");
  var hint = document.getElementById("amt-hint");
  var errBox = document.getElementById("amt-error");
  var submit = amount && amount.form ? amount.form.querySelector('button[type="submit"]') : null;
  var remaining = box ? parseInt(box.getAttribute("data-remaining"), 10) : NaN;
  var total = box ? parseInt(box.getAttribute("data-total"), 10) : NaN;
  var already = box ? parseInt(box.getAttribute("data-paid"), 10) || 0 : 0;

  function inr(n) { return "\u20B9" + n.toLocaleString("en-IN"); }

  function pct(part) {
    if (!(total > 0)) return "";
    var p = Math.round(part / total * 100);
    if (part > 0 && p < 1) return " (<1%)";
    return " (" + p + "%)";
  }

  // The editable top box and the form's Amount field are one value: whichever is edited writes the other.
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

  function currentAmount() { return parseAmount(amount ? amount.value : ""); }

  function render(source) {
    var raw = source.value;
    if (topAmt && source !== topAmt && topAmt.value !== raw) topAmt.value = raw;
    if (amount && source !== amount && amount.value !== raw) amount.value = raw;
    var res = parseAmount(raw);
    var ok = res.value !== undefined;
    if (topAmt) topAmt.classList.toggle("invalid", !ok);
    if (topAmt) topAmt.setAttribute("aria-invalid", ok ? "false" : "true");
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
    var qrImg = document.getElementById("upi-qr");
    if (qrImg) qrImg.classList.toggle("stale", !ok);
    if (!ok) return;
    var v = res.value;
    var qr = document.getElementById("upi-qr");
    if (qr) qr.setAttribute("src", qr.getAttribute("src").replace(/([?&]amount=)[^&]*/, "$1" + v));
    var shown = document.getElementById("copy-amount-text");
    if (shown) shown.textContent = "\u20B9" + v;
  }

  document.querySelectorAll(".sv-copy").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy-text");
      if (text === null) {
        var res = currentAmount();
        if (res.value === undefined) { if (topAmt) topAmt.focus(); return; }
        text = String(res.value);
      }
      if (text) copy(text, btn);
    });
  });

  if (amount) {
    amount.addEventListener("input", function () { render(amount); });
    if (topAmt) {
      topAmt.addEventListener("input", function () { render(topAmt); });
      topAmt.addEventListener("focus", function () { setTimeout(function () { topAmt.select(); }, 0); });
      if (amount.form) amount.form.addEventListener("submit", function (e) {
        if (currentAmount().value === undefined) { e.preventDefault(); topAmt.focus(); }
      });
    }
  }

  var qrBox = document.getElementById("qr-box");
  if (qrBox && window.matchMedia && window.matchMedia("(min-width: 769px)").matches) qrBox.open = true;

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
