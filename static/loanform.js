/* Tooba Foundation 3-page loan form.
   Draws the typed values and photos on top of the scanned pages (static/form/page1-3.png),
   saves to the server, and makes Print / PDF output.

   Each page image is 1241 x 1755 px. Coordinates are [x0, y0, x1, y1] in image pixels.
   type "line": text sits on the printed underline; "box": text centred in the printed box.
   fs = font size in image pixels. Field ids must match form_fields.py. */
(function () {
'use strict';
const FORM_W = 1241, FORM_H = 1755, PAGE_COUNT = 3;
const FONT = "'Bookman Old Style', 'Bookman', 'URW Bookman', 'URW Bookman L', Georgia, 'Jameel Noori Nastaleeq', 'Noto Nastaliq Urdu', serif";

const FIELDS = [
  // The loan-number oval on page 1 is left blank: the application ID is kept in the system only.
  // Page 1 — Applicant
  { id: 'amount',          type: 'box',  r: [758, 350, 1070, 410], fs: 40 },
  { id: 'name',            type: 'box',  r: [648, 541, 1108, 597], fs: 36 },
  { id: 'father',          type: 'box',  r: [82, 541, 490, 597],   fs: 36 },
  { id: 'phone',           type: 'line', r: [795, 712, 1072, 760], fs: 32 },
  { id: 'familyPhone',     type: 'line', r: [447, 712, 698, 760],  fs: 32 },
  { id: 'qist',            type: 'line', r: [227, 718, 363, 760],  fs: 28 },
  { id: 'address',         type: 'line', r: [92, 788, 962, 835],   fs: 32 },
  { id: 'business',        type: 'line', r: [786, 855, 1000, 901], fs: 30 },
  { id: 'businessAddress', type: 'line', r: [92, 855, 718, 901],   fs: 30 },
  { id: 'income',          type: 'line', r: [954, 936, 1064, 980], fs: 28 },
  { id: 'installment',     type: 'line', r: [740, 936, 843, 980],  fs: 28 },
  { id: 'eduExpense',      type: 'line', r: [510, 940, 611, 984],  fs: 28 },
  { id: 'jobFamily',       type: 'line', r: [299, 934, 356, 978],  fs: 28 },
  { id: 'expenses',        type: 'line', r: [89, 938, 181, 982],   fs: 28 },
  { id: 'family',          type: 'line', r: [945, 1014, 1016, 1058], fs: 28 },
  { id: 'houseEdu',        type: 'line', r: [726, 1011, 894, 1055], fs: 28 },
  { id: 'relative1',       type: 'line', r: [633, 1088, 1013, 1132], fs: 30 },
  { id: 'relative2',       type: 'line', r: [89, 1088, 458, 1132],  fs: 30 },
  // Page 1 — Guarantor 1
  { id: 'g1name',          type: 'line', r: [657, 1194, 1111, 1238], fs: 30 },
  { id: 'g1father',        type: 'line', r: [92, 1194, 567, 1238],   fs: 30 },
  { id: 'g1phone',         type: 'line', r: [96, 1264, 313, 1308],   fs: 28 },
  { id: 'g1address',       type: 'line', r: [95, 1327, 533, 1371],   fs: 28 },
  { id: 'g1business',      type: 'line', r: [811, 1331, 1108, 1375], fs: 28 },
  // Page 1 — Guarantor 2
  { id: 'g2name',          type: 'line', r: [657, 1424, 1111, 1468], fs: 30 },
  { id: 'g2father',        type: 'line', r: [92, 1424, 567, 1468],   fs: 30 },
  { id: 'g2phone',         type: 'line', r: [96, 1495, 313, 1539],   fs: 28 },
  { id: 'g2address',       type: 'line', r: [95, 1570, 533, 1614],   fs: 28 },
  { id: 'g2business',      type: 'line', r: [811, 1574, 1108, 1618], fs: 28 },
  // Page 3 — Loan details table
  { id: 'detail1', page: 3, type: 'box', r: [62, 1305, 1186, 1386], fs: 34 },
  { id: 'detail2', page: 3, type: 'box', r: [62, 1390, 1186, 1477], fs: 34 },
  { id: 'detail3', page: 3, type: 'box', r: [62, 1482, 1186, 1577], fs: 34 },
  { id: 'detail4', page: 3, type: 'box', r: [62, 1580, 1186, 1666], fs: 34 },
];

/* CNIC boxes: 15 cells (cells 5 and 13 are the printed dashes). */
const DIGIT_CELLS = [0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 14];
const CNICS = [
  { id: 'cnic',   y: [637, 680],   fs: 32,
    edges: [76, 123.5, 171.5, 216.5, 267.5, 318, 371.5, 424.5, 472.5, 526.5, 576.5, 627.5, 678.5, 729.5, 781, 830.5] },
  { id: 'g1cnic', y: [1276, 1319], fs: 30,
    edges: [394.5, 434.5, 474.5, 512.5, 555.5, 597.5, 642.5, 687.5, 727.5, 773, 814.5, 858.5, 901, 944.5, 987, 1028.5] },
  { id: 'g2cnic', y: [1506, 1548], fs: 30,
    edges: [394.5, 434.5, 474.5, 512.5, 555.5, 597.5, 642.5, 687.5, 727.5, 773, 814.5, 858.5, 901, 944.5, 987, 1028.5] },
];

/* Photo boxes on the pages. Ids match PHOTO_SLOTS in form_fields.py. */
const IMAGES = [
  { id: 'photo',   page: 1, fit: 'cover', r: [539, 314, 724, 519] },
  { id: 'g1Photo', page: 2, fit: 'cover', r: [138, 855, 274, 1016] },
  { id: 'g2Photo', page: 2, fit: 'cover', r: [138, 1079, 274, 1240] },
];
const LINE_GAP = 5;

const payloadEl = document.getElementById('formPayload');
const PAYLOAD = payloadEl ? JSON.parse(payloadEl.textContent) : { values: {}, photos: {} };
const form = document.getElementById('loanForm');          // null on the print page
const pctX = v => (v / FORM_W * 100) + '%';
const pctY = v => (v / FORM_H * 100) + '%';
const cqw = v => (v / FORM_W * 100) + 'cqw';

const previews = {}, fieldEls = {}, picEls = {};
const imgData = {};     // id -> loaded HTMLImageElement
const pending = {};     // id -> Blob waiting to be uploaded
for (let p = 1; p <= PAGE_COUNT; p++) previews[p] = document.getElementById('preview' + p);

function val(id) {
  const el = form && form.elements[id];
  if (el) return String(el.value || '').trim();
  return String((PAYLOAD.values || {})[id] || '').trim();
}

function showMsg(text, isErr) {
  const m = document.getElementById('msg');
  if (m) { m.textContent = text; m.style.color = isErr ? '#b42318' : '#126b2d'; }
}

function place(el, x0, y0, x1, y1) {
  el.style.left = pctX(x0); el.style.top = pctY(y0);
  el.style.width = pctX(x1 - x0); el.style.height = pctY(y1 - y0);
}

function buildOverlay() {
  FIELDS.forEach(f => {
    const el = document.createElement('div');
    el.className = 'field ' + f.type;
    const [x0, y0, x1, y1] = f.r;
    place(el, x0, y0, x1, f.type === 'line' ? y1 - LINE_GAP : y1);
    el.style.fontSize = cqw(f.fs);
    previews[f.page || 1].appendChild(el);
    fieldEls[f.id] = el;
  });
  CNICS.forEach(c => {
    c.els = DIGIT_CELLS.map(ci => {
      const d = document.createElement('div');
      d.className = 'digit';
      place(d, c.edges[ci], c.y[0], c.edges[ci + 1], c.y[1]);
      d.style.fontSize = cqw(c.fs);
      previews[1].appendChild(d);
      return d;
    });
  });
  IMAGES.forEach(m => {
    const box = document.createElement('div');
    box.className = 'pic ' + m.fit;
    place(box, ...m.r);
    const img = document.createElement('img');
    img.alt = '';
    box.appendChild(img);
    previews[m.page].appendChild(box);
    picEls[m.id] = box;
  });
}

function setField(f) {
  const el = fieldEls[f.id];
  const text = val(f.id);
  el.innerHTML = '';
  if (!text) return;
  const span = document.createElement('span');
  span.textContent = text;
  span.dir = 'auto';
  el.appendChild(span);
  const avail = el.clientWidth * 0.97, need = span.scrollWidth;
  if (avail > 0 && need > avail) span.style.fontSize = (Math.max(0.45, avail / need) * 100) + '%';
}

function setCnic(c) {
  const digits = val(c.id).replace(/\D/g, '').slice(0, 13);
  c.els.forEach((d, i) => { d.textContent = digits[i] || ''; });
}

function updatePreview() { FIELDS.forEach(setField); CNICS.forEach(setCnic); }

/* ---------- photos ---------- */
function showImage(id, url) {
  return new Promise(resolve => {
    const img = new Image();
    img.onload = () => {
      imgData[id] = img;
      const box = picEls[id];
      box.querySelector('img').src = url;
      box.style.display = 'block';
      const t = document.getElementById('thumb_' + id);
      if (t) { t.innerHTML = ''; const ti = document.createElement('img'); ti.src = url; t.appendChild(ti); }
      const card = document.getElementById('card_' + id);
      if (card) card.classList.add('has');
      resolve();
    };
    img.onerror = () => resolve();
    img.src = url;
  });
}

function removeImage(id) {
  delete imgData[id]; delete pending[id];
  const box = picEls[id];
  box.style.display = 'none';
  box.querySelector('img').removeAttribute('src');
  const inp = document.getElementById('file_' + id); if (inp) inp.value = '';
  const t = document.getElementById('thumb_' + id); if (t) t.textContent = 'No image';
  const card = document.getElementById('card_' + id); if (card) card.classList.remove('has');
  const rm = form && form.elements['remove_' + id]; if (rm) rm.value = '1';
}

/* Keep the real photo. Only very large camera photos are scaled down (to 1600px on the long side),
   which is still far sharper than the photo box on the printed A4 page. */
function loadImage(id, file) {
  if (!file.type.startsWith('image/')) { showMsg('Please choose an image file.', true); return; }
  const reader = new FileReader();
  reader.onload = e => {
    const raw = new Image();
    raw.onload = () => {
      const k = Math.min(1, 1600 / Math.max(raw.naturalWidth, raw.naturalHeight));
      const c = document.createElement('canvas');
      c.width = Math.max(1, Math.round(raw.naturalWidth * k));
      c.height = Math.max(1, Math.round(raw.naturalHeight * k));
      const cx = c.getContext('2d');
      cx.imageSmoothingEnabled = true; cx.imageSmoothingQuality = 'high';
      cx.fillStyle = '#fff'; cx.fillRect(0, 0, c.width, c.height);
      cx.drawImage(raw, 0, 0, c.width, c.height);
      c.toBlob(blob => {
        pending[id] = blob;
        const rm = form && form.elements['remove_' + id]; if (rm) rm.value = '0';
        showImage(id, URL.createObjectURL(blob)).then(() => showMsg('Picture added — "Save Form" dabayein.'));
      }, 'image/jpeg', 0.95);
    };
    raw.onerror = () => showMsg('This picture could not be opened. Try a JPG or PNG.', true);
    raw.src = e.target.result;
  };
  reader.readAsDataURL(file);
}

/* ---------- PDF (same coordinates, identical on every device) ---------- */
function renderPage(p, SCALE) {
  const bg = document.getElementById('bgImg' + p);
  const canvas = document.createElement('canvas');
  canvas.width = FORM_W * SCALE; canvas.height = FORM_H * SCALE;
  const ctx = canvas.getContext('2d');
  ctx.scale(SCALE, SCALE);
  ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high';
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, FORM_W, FORM_H);
  ctx.drawImage(bg, 0, 0, FORM_W, FORM_H);
  IMAGES.filter(m => m.page === p && imgData[m.id]).forEach(m => {
    const im = imgData[m.id];
    const [x0, y0, x1, y1] = m.r, bw = x1 - x0, bh = y1 - y0;
    const iw = im.naturalWidth, ih = im.naturalHeight;
    const s = Math.max(bw / iw, bh / ih), sw = bw / s, sh = bh / s;
    ctx.drawImage(im, (iw - sw) / 2, (ih - sh) / 2, sw, sh, x0, y0, bw, bh);
  });
  ctx.fillStyle = '#111';
  ctx.textAlign = 'center';
  FIELDS.filter(f => (f.page || 1) === p).forEach(f => {
    const text = val(f.id);
    if (!text) return;
    const [x0, y0, x1, y1] = f.r;
    let size = f.fs;
    ctx.font = '700 ' + size + 'px ' + FONT;
    const avail = (x1 - x0) * 0.97, w = ctx.measureText(text).width;
    if (w > avail) { size *= Math.max(0.45, avail / w); ctx.font = '700 ' + size + 'px ' + FONT; }
    const cx = (x0 + x1) / 2;
    ctx.direction = /[؀-ۿ]/.test(text) ? 'rtl' : 'ltr';
    if (f.type === 'line') { ctx.textBaseline = 'alphabetic'; ctx.fillText(text, cx, y1 - LINE_GAP - size * 0.2); }
    else { ctx.textBaseline = 'middle'; ctx.fillText(text, cx, (y0 + y1) / 2); }
  });
  if (p === 1) {
    CNICS.forEach(c => {
      const digits = val(c.id).replace(/\D/g, '').slice(0, 13);
      ctx.font = '700 ' + c.fs + 'px ' + FONT;
      ctx.textBaseline = 'middle';
      ctx.direction = 'ltr';
      [...digits].forEach((dg, i) => {
        const ci = DIGIT_CELLS[i];
        ctx.fillText(dg, (c.edges[ci] + c.edges[ci + 1]) / 2, (c.y[0] + c.y[1]) / 2);
      });
    });
  }
  const dataUrl = canvas.toDataURL('image/jpeg', 0.92);
  canvas.width = canvas.height = 0;
  const bin = atob(dataUrl.split(',')[1]);
  const jpg = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) jpg[i] = bin.charCodeAt(i);
  return jpg;
}

async function downloadPDF() {
  try {
    updatePreview();
    for (let p = 1; p <= PAGE_COUNT; p++) {
      const bg = document.getElementById('bgImg' + p);
      if (!bg.complete) await new Promise(r => { bg.onload = r; bg.onerror = r; });
    }
    if (document.fonts && document.fonts.ready) await document.fonts.ready;
    showMsg('Making PDF…');
    const SCALE = 2, W = FORM_W * SCALE, H = FORM_H * SCALE;
    const pages = [];
    for (let p = 1; p <= PAGE_COUNT; p++) pages.push(renderPage(p, SCALE));
    const enc = new TextEncoder();
    const parts = []; let pos = 0; const off = {};
    const addText = t => { const b = enc.encode(t); parts.push(b); pos += b.length; };
    const addBytes = b => { parts.push(b); pos += b.length; };
    const obj = (n, body) => { off[n] = pos; addText(n + ' 0 obj\n' + body + '\nendobj\n'); };
    const pageObj = i => 3 + i * 3;
    addText('%PDF-1.4\n%\xFF\xFF\xFF\xFF\n');
    obj(1, '<< /Type /Catalog /Pages 2 0 R >>');
    obj(2, '<< /Type /Pages /Kids [' + pages.map((_, i) => pageObj(i) + ' 0 R').join(' ') + '] /Count ' + pages.length + ' >>');
    const stream = 'q\n595.28 0 0 841.89 0 0 cm\n/Im0 Do\nQ\n';
    pages.forEach((jpg, i) => {
      const pn = pageObj(i), cn = pn + 1, inum = pn + 2;
      obj(pn, '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 841.89] /Resources << /XObject << /Im0 ' + inum + ' 0 R >> >> /Contents ' + cn + ' 0 R >>');
      obj(cn, '<< /Length ' + enc.encode(stream).length + ' >>\nstream\n' + stream + 'endstream');
      off[inum] = pos;
      addText(inum + ' 0 obj\n<< /Type /XObject /Subtype /Image /Width ' + W + ' /Height ' + H + ' /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ' + jpg.length + ' >>\nstream\n');
      addBytes(jpg);
      addText('\nendstream\nendobj\n');
    });
    const total = 2 + pages.length * 3, xref = pos;
    addText('xref\n0 ' + (total + 1) + '\n0000000000 65535 f \n');
    for (let i = 1; i <= total; i++) addText(String(off[i]).padStart(10, '0') + ' 00000 n \n');
    addText('trailer\n<< /Size ' + (total + 1) + ' /Root 1 0 R >>\nstartxref\n' + xref + '\n%%EOF');
    const url = URL.createObjectURL(new Blob(parts, { type: 'application/pdf' }));
    const a = document.createElement('a');
    a.href = url;
    const nm = val('name').replace(/[^\w\- ]+/g, '').replace(/\s+/g, '_');
    a.download = 'Tooba_Loan_Form_' + (PAYLOAD.appId || '') + (nm ? '_' + nm : '') + '.pdf';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    showMsg('PDF (3 pages) downloaded.');
  } catch (err) {
    console.error(err);
    showMsg('PDF download failed. Use Print → Save as PDF.', true);
  }
}

/* ---------- save ---------- */
async function save(e) {
  e.preventDefault();
  const btn = form.querySelector('[type=submit]');
  btn.disabled = true; showMsg('Saving…');
  const fd = new FormData(form);
  Object.keys(pending).forEach(id => fd.set('photo_' + id, pending[id], id + '.jpg'));
  try {
    const res = await fetch(form.action || location.href, {
      method: 'POST', body: fd, credentials: 'same-origin', headers: { 'X-Requested-With': 'fetch' } });
    let data = null;
    try { data = await res.json(); } catch (_) { /* not JSON */ }
    if (data && data.ok) { location.href = data.redirect; return; }
    showMsg((data && data.error) || ('Save nahi hua (error ' + res.status + '). Page refresh karke dobara koshish karein.'), true);
  } catch (_) {
    showMsg('Internet connection check karein — save nahi hua.', true);
  }
  btn.disabled = false;
}

function clearAll() {
  if (!window.confirm('Saari fields aur pictures khaali kar dein? (Save dabane tak purana data mehfooz hai)')) return;
  form.querySelectorAll('.grid input:not([type=file])').forEach(i => { i.value = ''; });
  IMAGES.forEach(m => removeImage(m.id));
  updatePreview();
}

/* ---------- wire up ---------- */
buildOverlay();
updatePreview();
const loads = IMAGES.filter(m => PAYLOAD.photos && PAYLOAD.photos[m.id]).map(m => showImage(m.id, PAYLOAD.photos[m.id]));
if (document.fonts && document.fonts.ready) document.fonts.ready.then(updatePreview);
window.addEventListener('resize', updatePreview);

document.addEventListener('click', e => {
  const b = e.target.closest('[data-action]');
  if (!b) return;
  const act = b.getAttribute('data-action');
  if (act === 'pdf') { e.preventDefault(); Promise.all(loads).then(downloadPDF); }
  if (act === 'print') { e.preventDefault(); updatePreview(); Promise.all(loads).then(() => window.print()); }
  if (act === 'clear') { e.preventDefault(); clearAll(); }
  if (act === 'remove') { e.preventDefault(); removeImage(b.getAttribute('data-id')); }
});

if (form) {
  form.addEventListener('submit', save);
  form.querySelectorAll('input[data-photo]').forEach(inp => inp.addEventListener('change', function () {
    if (this.files[0]) loadImage(this.getAttribute('data-photo'), this.files[0]);
  }));
  const ids = [...form.querySelectorAll('.grid input:not([type=file])')];
  ids.forEach((el, i) => {
    el.addEventListener('input', updatePreview);
    el.addEventListener('keydown', e => {      // Enter / Down = next field, Up = previous
      const n = e.key === 'Enter' || e.key === 'ArrowDown' ? ids[i + 1] : e.key === 'ArrowUp' ? ids[i - 1] : null;
      if (e.key === 'Enter' || e.key === 'ArrowDown' || e.key === 'ArrowUp') e.preventDefault();
      if (n) { n.focus(); n.select && n.select(); }
    });
  });
}
})();
