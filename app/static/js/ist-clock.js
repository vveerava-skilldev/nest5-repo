(function () {
  var el = document.getElementById("ist-clock");
  if (!el) return;
  var days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function render() {
    // IST is a fixed UTC+5:30: shift the clock and read the UTC fields.
    var t = new Date(Date.now() + 330 * 60000);
    var h = t.getUTCHours();
    var m = t.getUTCMinutes();
    el.textContent = days[t.getUTCDay()] + " " + pad(t.getUTCDate()) + "-" + pad(t.getUTCMonth() + 1) + "-" + t.getUTCFullYear() +
      ", " + (h % 12 || 12) + ":" + (m < 10 ? "0" : "") + m + " " + (h < 12 ? "AM" : "PM") + " IST";
  }
  render();
  setTimeout(function tick() { render(); setTimeout(tick, 60000 - (Date.now() % 60000)); }, 60000 - (Date.now() % 60000));
})();
