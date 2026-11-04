/* Login page only: the login button gently moves away from the mouse while Username or
   Password is empty, and stays still once both are filled. It is still the normal submit
   button of the existing form, so clicking it, Tab and Enter always log in as before. */
(function () {
  'use strict';
  document.addEventListener('DOMContentLoaded', function () {
    var loginBtn = document.getElementById('loginBtn');
    var loginDock = document.getElementById('loginDock');
    var form = loginBtn && loginBtn.form;
    var username = form && form.querySelector('input[name="username"], input[name="email"]');
    var password = form && form.querySelector('input[name="password"]');
    if (!loginBtn || !loginDock || !username || !password) return;

    var mq = function (q) { return window.matchMedia && window.matchMedia(q).matches; };
    // Only a real mouse on a device that can hover; never on touch screens or with reduced motion.
    var canRun = function () { return mq('(hover: hover) and (pointer: fine)') && !mq('(prefers-reduced-motion: reduce)'); };

    var THRESHOLD = 100;   // px from the button centre where it starts to move
    var MAX_MOVE = 28;     // px — small, so the button is always reachable

    function isReady() { return username.value.trim().length > 0 && password.value.length > 0; }

    function resetButton() {
      loginBtn.style.setProperty('--x', '0px');
      loginBtn.style.setProperty('--y', '0px');
      loginBtn.style.setProperty('--rot', '0deg');
    }

    function updateButton() {
      var ready = isReady();
      loginBtn.classList.toggle('is-ready', ready);
      loginDock.classList.toggle('is-ready', ready);
      if (ready) resetButton();
    }

    var pressed = false;   // while the mouse button is down the login button holds still, so the click lands

    function moveButton(event) {
      if (pressed) return;
      if (isReady() || !canRun() || loginBtn.disabled || loginBtn.classList.contains('is-loading')) {
        resetButton();
        return;
      }
      // Measure from the button's resting place, not where it has already moved to.
      var rect = loginBtn.getBoundingClientRect();
      var cs = getComputedStyle(loginBtn);
      var curX = parseFloat(cs.getPropertyValue('--x')) || 0;
      var curY = parseFloat(cs.getPropertyValue('--y')) || 0;
      var centerX = rect.left + rect.width / 2 - curX;
      var centerY = rect.top + rect.height / 2 - curY;
      var dx = event.clientX - centerX;
      var dy = event.clientY - centerY;
      var distance = Math.hypot(dx, dy);
      if (distance >= THRESHOLD) { resetButton(); return; }
      var strength = Math.min((THRESHOLD - distance) / THRESHOLD, 1);
      var angle = Math.atan2(dy, dx);
      var moveX = -Math.cos(angle) * strength * MAX_MOVE;
      var moveY = -Math.sin(angle) * strength * MAX_MOVE * 0.6;   // the dock is short: move less vertically
      loginBtn.style.setProperty('--x', moveX.toFixed(1) + 'px');
      loginBtn.style.setProperty('--y', moveY.toFixed(1) + 'px');
      loginBtn.style.setProperty('--rot', (moveX * 0.12).toFixed(2) + 'deg');
    }

    ['input', 'change'].forEach(function (ev) {
      username.addEventListener(ev, updateButton);
      password.addEventListener(ev, updateButton);
    });
    loginDock.addEventListener('pointermove', function (event) {
      if (event.pointerType === 'mouse') moveButton(event);
    });
    loginDock.addEventListener('pointerleave', function () { if (!pressed) resetButton(); });
    loginBtn.addEventListener('focus', resetButton);
    loginBtn.addEventListener('pointerdown', function () { pressed = true; });
    window.addEventListener('pointerup', function () { setTimeout(function () { pressed = false; }, 0); });

    // Busy look while the existing form posts. The browser's own "required" checks run first:
    // the submit event only fires when the form is valid. Nothing here stops the submit.
    form.addEventListener('submit', function () {
      resetButton();
      loginBtn.classList.add('is-loading');
      loginBtn.setAttribute('aria-busy', 'true');
    });
    // Coming back with the browser's Back button: clear the busy look.
    window.addEventListener('pageshow', function () {
      loginBtn.classList.remove('is-loading');
      loginBtn.removeAttribute('aria-busy');
      updateButton();
    });

    updateButton();
    // Saved passwords filled in by the browser do not always fire "input".
    setTimeout(updateButton, 400);
    setTimeout(updateButton, 1200);
  });
})();
