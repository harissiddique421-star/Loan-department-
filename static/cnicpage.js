/* The single CNIC DOCUMENTS page that follows the 3 original form pages.
   One A4 page (same 1241x1755 frame as the scanned pages), one row per section:
   Applicant, First Witness, Second Witness and — only when the reviewer requires it —
   Family Member. Each row shows Front Side and Back Side, scaled to fit without overlap.
   The same layout is used for the screen/print page and for the downloaded PDF. */
(function () {
  'use strict';
  var el = document.getElementById('formPayload');
  var page = document.getElementById('cnicPage');
  if (!el || !page) return;
  var rows = (JSON.parse(el.textContent).cnic) || [];
  if (!rows.length) { page.remove(); var t = document.getElementById('cnicTag'); if (t) t.remove(); return; }

  var W = 1241, H = 1755, M = 70, TOP = 210, BOTTOM = 1700, GAP = 26, TITLE = 54, LABEL = 40, COLGAP = 30;
  var FONT = "'Bookman Old Style', Georgia, serif";

  function layout() {
    var n = rows.length, rh = (BOTTOM - TOP - GAP * (n - 1)) / n, colW = (W - 2 * M - COLGAP) / 2;
    return rows.map(function (r, i) {
      var y = TOP + i * (rh + GAP);
      return { r: r, title: [M, y, W - M, y + TITLE], sides: ['front', 'back'].map(function (side, j) {
        var x = M + j * (colW + COLGAP);
        return { side: side, label: [x, y + TITLE, x + colW, y + TITLE + LABEL], box: [x, y + TITLE + LABEL, x + colW, y + rh] };
      }) };
    });
  }
  var L = layout();
  var pct = function (v, of) { return (v / of * 100) + '%'; };
  var cq = function (px) { return (px / W * 100) + 'cqw'; };
  function place(node, r) {
    node.style.left = pct(r[0], W); node.style.top = pct(r[1], H);
    node.style.width = pct(r[2] - r[0], W); node.style.height = pct(r[3] - r[1], H);
    page.appendChild(node); return node;
  }
  function div(cls, text) { var d = document.createElement('div'); d.className = cls; if (text) d.textContent = text; return d; }

  var head = place(div('cp-head', 'CNIC DOCUMENTS'), [M, 80, W - M, 170]);
  head.style.fontSize = cq(64);
  L.forEach(function (row) {
    var t = place(div('cp-title'), row.title);
    t.style.fontSize = cq(26);
    var a = document.createElement('b'); a.textContent = 'SECTION ' + row.r.section + ': ' + row.r.title; t.appendChild(a);
    if (row.r.name) { var nm = document.createElement('span'); nm.textContent = row.r.name; nm.dir = 'auto'; t.appendChild(nm); }
    row.sides.forEach(function (s) {
      var lb = place(div('cp-label', s.side === 'front' ? 'Front Side' : 'Back Side'), s.label);
      lb.style.fontSize = cq(24);
      var box = place(div('cp-box'), s.box);
      var url = row.r[s.side];
      if (url) { var im = document.createElement('img'); im.src = url; im.alt = row.r.title + ' ' + s.side; box.appendChild(im); }
      else { var e = div('cp-empty', 'Not uploaded'); e.style.fontSize = cq(24); box.appendChild(e); }
    });
  });

  /* PDF page — called by loanform.js after the 3 form pages. */
  function load(url) {
    return new Promise(function (res) {
      if (!url) return res(null);
      var im = new Image(); im.onload = function () { res(im); }; im.onerror = function () { res(null); }; im.src = url;
    });
  }
  window.renderCnicPage = async function (SCALE) {
    var c = document.createElement('canvas'); c.width = W * SCALE; c.height = H * SCALE;
    var x = c.getContext('2d'); x.scale(SCALE, SCALE);
    x.imageSmoothingEnabled = true; x.imageSmoothingQuality = 'high';
    x.fillStyle = '#fff'; x.fillRect(0, 0, W, H);
    x.fillStyle = '#111'; x.textAlign = 'center'; x.textBaseline = 'middle';
    x.font = '700 64px ' + FONT; x.fillText('CNIC DOCUMENTS', W / 2, 125);
    x.fillRect(W / 2 - 300, 168, 600, 4);
    for (var i = 0; i < L.length; i++) {
      var row = L[i], tr = row.title;
      x.fillStyle = '#e8f3ec'; x.fillRect(tr[0], tr[1], tr[2] - tr[0], tr[3] - tr[1]);
      x.strokeStyle = '#111'; x.lineWidth = 2; x.strokeRect(tr[0], tr[1], tr[2] - tr[0], tr[3] - tr[1]);
      x.fillStyle = '#111'; x.font = '700 26px ' + FONT; x.textAlign = 'left';
      x.fillText('SECTION ' + row.r.section + ': ' + row.r.title, tr[0] + 16, (tr[1] + tr[3]) / 2);
      if (row.r.name) { x.textAlign = 'right'; x.font = '400 26px ' + FONT; x.fillText(row.r.name, tr[2] - 16, (tr[1] + tr[3]) / 2); }
      for (var j = 0; j < row.sides.length; j++) {
        var s = row.sides[j], lb = s.label, b = s.box;
        x.textAlign = 'center'; x.font = '700 24px ' + FONT; x.fillStyle = '#111';
        x.fillText(s.side === 'front' ? 'Front Side' : 'Back Side', (lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2);
        x.strokeRect(b[0], b[1], b[2] - b[0], b[3] - b[1]);
        var im = await load(row.r[s.side]);
        if (im) {
          var pad = 10, bw = b[2] - b[0] - 2 * pad, bh = b[3] - b[1] - 2 * pad;
          var k = Math.min(bw / im.naturalWidth, bh / im.naturalHeight);
          var w = im.naturalWidth * k, h = im.naturalHeight * k;
          x.drawImage(im, b[0] + pad + (bw - w) / 2, b[1] + pad + (bh - h) / 2, w, h);
        } else {
          x.fillStyle = '#888'; x.font = '400 24px ' + FONT;
          x.fillText('Not uploaded', (b[0] + b[2]) / 2, (b[1] + b[3]) / 2);
        }
      }
    }
    var data = c.toDataURL('image/jpeg', 0.92); c.width = c.height = 0;
    var bin = atob(data.split(',')[1]), jpg = new Uint8Array(bin.length);
    for (var n = 0; n < bin.length; n++) jpg[n] = bin.charCodeAt(n);
    return jpg;
  };
})();
