/* Small UI behaviours shared by every page. */
(function () {
  'use strict';
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  document.addEventListener('click', function (e) {
    var t = e.target.closest('[data-print]');
    if (t) { e.preventDefault(); window.print(); }
    var c = e.target.closest('[data-confirm]');
    if (c && !window.confirm(c.getAttribute('data-confirm'))) { e.preventDefault(); }
    var d = e.target.closest('[data-dismiss]');
    if (d) { hideToast(d.closest('.toast')); }
    var p = e.target.closest('[data-toggle-pw]');
    if (p) {
      var inp = document.getElementById(p.getAttribute('data-toggle-pw'));
      if (inp) { inp.type = inp.type === 'password' ? 'text' : 'password'; inp.focus(); }
    }
  });

  // Live preview for any photo picker that names a target <img>.
  document.addEventListener('change', function (e) {
    var input = e.target;
    if (!input.matches('[data-preview]') || !input.files || !input.files[0]) return;
    var img = document.getElementById(input.getAttribute('data-preview'));
    if (img) img.src = URL.createObjectURL(input.files[0]);
  });

  function hideToast(el) {
    if (!el || el.classList.contains('out')) return;
    el.classList.add('out');
    setTimeout(function () { el.remove(); }, 400);
  }

  document.addEventListener('DOMContentLoaded', function () {
    // Toasts close themselves after a few seconds.
    document.querySelectorAll('.toast').forEach(function (t, i) {
      setTimeout(function () { hideToast(t); }, 4500 + i * 600);
    });
    // Count-up for dashboard numbers (final value is already in the HTML).
    if (reduce) return;
    document.querySelectorAll('[data-count]').forEach(function (el) {
      var end = parseInt(el.getAttribute('data-count'), 10) || 0;
      if (end < 2) return;
      var start = performance.now(), dur = 700 + Math.min(end, 60) * 8;
      function step(now) {
        var k = Math.min(1, (now - start) / dur), eased = 1 - Math.pow(1 - k, 3);
        el.textContent = Math.round(end * eased);
        if (k < 1) requestAnimationFrame(step);
      }
      el.textContent = '0';
      requestAnimationFrame(step);
    });
  });
})();
