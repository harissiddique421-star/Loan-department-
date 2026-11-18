"""Filled Microsoft Word (.docx) copy of one loan application (server side only).

The first three pages are the original scanned Tooba form (static/form/page1-3.png) with every
typed value placed at exactly the same spot as on the website and the printed form (same
coordinates as static/loanform.js), plus the applicant / guarantor photos. Values are real,
editable Word text boxes (Urdu text is shaped by Word itself), not a flattened picture.

Then come the CNIC DOCUMENTS page (same layout as static/cnicpage.js) and a "Complete record"
section: every field with its label, guarantors, loan details, status, review notes,
disbursement / cheques, the list of uploaded documents (with SHA-256) and the change history.

Nothing here touches the database: the caller passes a plain `record` dict (see app.py
`archive_record`), so the generator can be tested and verified on its own.
"""
import io
import os
import re
import zipfile
from xml.sax.saxutils import escape

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_BREAK
from docx.oxml import parse_xml
from docx.shared import Mm, Pt, RGBColor

from form_fields import FORM_SECTIONS, PHOTO_SLOTS

FORM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "form")
FORM_W, FORM_H = 1241, 1755
PAGE_W_EMU, PAGE_H_EMU = 7560000, 10692000           # A4: 210 x 297 mm
EMU_X = PAGE_W_EMU / FORM_W
EMU_Y = PAGE_H_EMU / FORM_H
PT_PER_PX = 297 / 25.4 * 72 / FORM_H                  # one scanned-page pixel in points
LATIN_FONT = "Bookman Old Style"
URDU_FONT = "Jameel Noori Nastaleeq"
MIME_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Same boxes as static/loanform.js (FIELDS / CNICS / IMAGES). Keep the two in step.
FIELDS = [
    ("amount", 1, "box", (758, 350, 1070, 410), 40),
    ("name", 1, "box", (648, 541, 1108, 597), 36),
    ("father", 1, "box", (82, 541, 490, 597), 36),
    ("phone", 1, "line", (795, 712, 1072, 760), 32),
    ("familyPhone", 1, "line", (447, 712, 698, 760), 32),
    ("qist", 1, "line", (227, 718, 363, 760), 28),
    ("address", 1, "line", (92, 788, 962, 835), 32),
    ("business", 1, "line", (786, 855, 1000, 901), 30),
    ("businessAddress", 1, "line", (92, 855, 718, 901), 30),
    ("income", 1, "line", (954, 936, 1064, 980), 28),
    ("installment", 1, "line", (740, 936, 843, 980), 28),
    ("eduExpense", 1, "line", (510, 940, 611, 984), 28),
    ("jobFamily", 1, "line", (299, 934, 356, 978), 28),
    ("expenses", 1, "line", (89, 938, 181, 982), 28),
    ("family", 1, "line", (945, 1014, 1016, 1058), 28),
    ("houseEdu", 1, "line", (726, 1011, 894, 1055), 28),
    ("relative1", 1, "line", (633, 1088, 1013, 1132), 30),
    ("relative2", 1, "line", (89, 1088, 458, 1132), 30),
    ("language", 1, "line", (290, 1011, 554, 1055), 28),
    ("g1name", 1, "line", (657, 1194, 1111, 1238), 30),
    ("g1father", 1, "line", (92, 1194, 567, 1238), 30),
    ("g1phone", 1, "line", (96, 1264, 313, 1308), 28),
    ("g1address", 1, "line", (95, 1327, 533, 1371), 28),
    ("g1business", 1, "line", (811, 1331, 1108, 1375), 28),
    ("g2name", 1, "line", (657, 1424, 1111, 1468), 30),
    ("g2father", 1, "line", (92, 1424, 567, 1468), 30),
    ("g2phone", 1, "line", (96, 1495, 313, 1539), 28),
    ("g2address", 1, "line", (95, 1570, 533, 1614), 28),
    ("g2business", 1, "line", (811, 1574, 1108, 1618), 28),
    ("detail1", 3, "box", (62, 1305, 1186, 1386), 34),
    ("detail2", 3, "box", (62, 1390, 1186, 1477), 34),
    ("detail3", 3, "box", (62, 1482, 1186, 1577), 34),
    ("detail4", 3, "box", (62, 1580, 1186, 1666), 34),
]
DIGIT_CELLS = [0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 14]
_G_EDGES = [394.5, 434.5, 474.5, 512.5, 555.5, 597.5, 642.5, 687.5, 727.5, 773, 814.5, 858.5, 901, 944.5, 987,
            1028.5]
CNICS = [
    ("cnic", (637, 680), 32, [76, 123.5, 171.5, 216.5, 267.5, 318, 371.5, 424.5, 472.5, 526.5, 576.5, 627.5,
                              678.5, 729.5, 781, 830.5]),
    ("g1cnic", (1276, 1319), 30, _G_EDGES),
    ("g2cnic", (1506, 1548), 30, _G_EDGES),
]
IMAGES = [("photo", 1, (539, 314, 724, 519)),
          ("photo", 2, (137, 428, 266, 539)), ("famPhoto", 2, (137, 566, 266, 683)),
          ("g1Photo", 2, (138, 855, 274, 1016)),
          ("g2Photo", 2, (138, 1079, 274, 1240))]
LINE_GAP = 5
# Printed label + underline that the scanned page lacks (same as LABELS / RULES in loanform.js)
LABELS = [(1, "مادری زبان :", (560, 1003, 712, 1055), 25)]
RULES = [(1, (290, 1055, 554, 1058)), (2, (269, 425, 272, 542)), (2, (269, 563, 272, 686))]


def printed_value(values, fid):
    """What goes on the form for a field: Language shows the typed language when "Other" was picked."""
    if fid == "language" and values.get("language") == "Other":
        return values.get("languageOther") or "Other"
    return values.get(fid, "")

NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
      'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
      'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"')


def clean(v):
    return CTRL.sub("", "" if v is None else str(v)).strip()


class _Ids:
    def __init__(self):
        self.n = 100

    def next(self):
        self.n += 1
        return self.n


def _run_props(text, size_pt, bold=True, color="111111"):
    rtl = "<w:rtl/>" if ARABIC.search(text) else ""
    half = max(8, int(round(size_pt * 2)))
    return (f'<w:rPr><w:rFonts w:ascii="{LATIN_FONT}" w:hAnsi="{LATIN_FONT}" w:cs="{URDU_FONT}"/>'
            f'{"<w:b/><w:bCs/>" if bold else ""}<w:color w:val="{color}"/>'
            f'<w:sz w:val="{half}"/><w:szCs w:val="{half}"/>{rtl}</w:rPr>')


def _anchor_xml(ids, x0, y0, x1, y1, graphic, behind=False, name="Shape"):
    """A floating object at an exact position on the page (positions in scanned-page pixels)."""
    sid = ids.next()
    off_x, off_y = int(x0 * EMU_X), int(y0 * EMU_Y)
    cx, cy = max(1, int((x1 - x0) * EMU_X)), max(1, int((y1 - y0) * EMU_Y))
    return (f'<wp:anchor {NS} distT="0" distB="0" distL="0" distR="0" simplePos="0" '
            f'relativeHeight="{sid}" behindDoc="{1 if behind else 0}" locked="1" layoutInCell="1" allowOverlap="1">'
            f'<wp:simplePos x="0" y="0"/>'
            f'<wp:positionH relativeFrom="page"><wp:posOffset>{off_x}</wp:posOffset></wp:positionH>'
            f'<wp:positionV relativeFrom="page"><wp:posOffset>{off_y}</wp:posOffset></wp:positionV>'
            f'<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/>'
            f'<wp:docPr id="{sid}" name="{name} {sid}"/><wp:cNvGraphicFramePr/>'
            f'{graphic(sid, cx, cy)}</wp:anchor>')


def _picture(rid):
    def g(sid, cx, cy):
        return (f'<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
                f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{sid}" name="img{sid}"/><pic:cNvPicPr/></pic:nvPicPr>'
                f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
                f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
                f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic>')
    return g


def _textbox(text, size_pt, align="center", valign="ctr", bold=True, color="111111", border=None):
    def g(sid, cx, cy):
        ln = (f'<a:ln w="9525"><a:solidFill><a:srgbClr val="{border}"/></a:solidFill></a:ln>' if border
              else '<a:ln><a:noFill/></a:ln>')
        bidi = "<w:bidi/>" if ARABIC.search(text) else ""
        return (f'<a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
                f'<wps:wsp><wps:cNvSpPr txBox="1"/><wps:spPr><a:xfrm><a:off x="0" y="0"/>'
                f'<a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
                f'<a:noFill/>{ln}</wps:spPr><wps:txbx><w:txbxContent><w:p><w:pPr>{bidi}'
                f'<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/><w:jc w:val="{align}"/>'
                f'</w:pPr><w:r>{_run_props(text, size_pt, bold, color)}<w:t xml:space="preserve">{escape(text)}</w:t>'
                f'</w:r></w:p></w:txbxContent></wps:txbx><wps:bodyPr rot="0" vert="horz" wrap="none" lIns="0" '
                f'tIns="0" rIns="0" bIns="0" anchor="{valign}" anchorCtr="0"><a:noAutofit/></wps:bodyPr>'
                f'</wps:wsp></a:graphicData></a:graphic>')
    return g


def _filled_rect(color="111111"):
    def g(sid, cx, cy):
        return (f'<a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
                f'<wps:wsp><wps:cNvSpPr/><wps:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/>'
                f'</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
                f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill><a:ln><a:noFill/></a:ln></wps:spPr>'
                f'<wps:bodyPr/></wps:wsp></a:graphicData></a:graphic>')
    return g


def _fit_size(text, fs_px, width_px):
    """Shrink long values so they stay inside their box (like setField() in loanform.js)."""
    est = sum(0.62 if ch.isupper() or ch.isdigit() else 0.52 for ch in text) * fs_px
    k = 1.0 if est <= width_px * 0.97 else max(0.45, width_px * 0.97 / est)
    return fs_px * k * PT_PER_PX


def _cover(data, w, h):
    """Crop a photo to the box's shape (CSS object-fit: cover). Returns JPEG/PNG bytes."""
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.load()
    if im.mode not in ("RGB", "L"):
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.convert("RGBA").split()[-1])
        im = bg
    want = w / h
    iw, ih = im.size
    if iw / ih > want:
        nw = int(ih * want)
        im = im.crop(((iw - nw) // 2, 0, (iw - nw) // 2 + nw, ih))
    else:
        nh = int(iw / want)
        im = im.crop((0, (ih - nh) // 2, iw, (ih - nh) // 2 + nh))
    im.thumbnail((900, 900))
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=88)
    return out.getvalue()


def _contain(data, max_px=1400):
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.load()
    if im.mode not in ("RGB", "L"):
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.convert("RGBA").split()[-1])
        im = bg
    im.thumbnail((max_px, max_px))
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=88)
    return out.getvalue(), im.size


class _Builder:
    def __init__(self):
        self.doc = Document()
        self.ids = _Ids()
        sec = self.doc.sections[0]
        sec.orientation = WD_ORIENT.PORTRAIT
        sec.page_width, sec.page_height = Mm(210), Mm(297)
        for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
            setattr(sec, side, Mm(0))
        sec.header_distance = sec.footer_distance = Mm(0)
        st = self.doc.styles["Normal"]
        st.font.name, st.font.size = LATIN_FONT, Pt(10)

    def rid(self, data):
        """Add an image part; returns its relationship id."""
        part = self.doc.part
        img_part = part.package.get_or_add_image_part(io.BytesIO(data))
        return part.relate_to(img_part, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image")

    def page_paragraph(self, first=False):
        """A new fixed-layout page (a page break is put after the previous one)."""
        if getattr(self, "last_p", None) is not None:
            self.last_p.add_run().add_break(WD_BREAK.PAGE)
        p = self.doc.paragraphs[0] if first and self.doc.paragraphs else self.doc.add_paragraph()
        self.last_p = p
        pf = p.paragraph_format
        pf.space_before = pf.space_after = Pt(0)
        return p

    def anchor(self, p, xml_fn):
        r = p.add_run()
        r._r.append(parse_xml(f'<w:drawing {NS}>{xml_fn}</w:drawing>'))

    def text(self, p, text, rect, fs_px, kind="box"):
        x0, y0, x1, y1 = rect
        if kind == "line":
            y1 -= LINE_GAP
        size = _fit_size(text, fs_px, x1 - x0)
        self.anchor(p, _anchor_xml(self.ids, x0, y0, x1, y1,
                                   _textbox(text, size, valign="b" if kind == "line" else "ctr"), name="Field"))

    def image(self, p, data, rect, behind=False):
        x0, y0, x1, y1 = rect
        self.anchor(p, _anchor_xml(self.ids, x0, y0, x1, y1, _picture(self.rid(data)), behind=behind, name="Picture"))


def build(record):
    """record: dict with keys
         app_id, values {field: text}, photos {kind: bytes}, cnic_rows [{title, name, front, back}],
         info [(label, value)], notes [(when, kind, who, body)], disbursement [(label, value)],
         documents [(id, kind, name, type, size, sha256, uploaded, state)], images [(label, bytes)],
         history [(when, who, action, detail)], generated_at (text), version (int)
       Returns the .docx bytes."""
    b = _Builder()
    values = {k: clean(v) for k, v in (record.get("values") or {}).items()}
    photos = record.get("photos") or {}
    for n in (1, 2, 3):
        p = b.page_paragraph(first=(n == 1))
        with open(os.path.join(FORM_DIR, f"page{n}.png"), "rb") as fh:
            b.image(p, fh.read(), (0, 0, FORM_W, FORM_H), behind=True)
        for page, rect in RULES:
            if page == n:
                b.anchor(p, _anchor_xml(b.ids, *rect, _filled_rect(), name="Rule"))
        for page, text, (x0, y0, x1, y1), fs in LABELS:
            if page == n:
                b.anchor(p, _anchor_xml(b.ids, x0, y0, x1, y1 - LINE_GAP,
                                        _textbox(text, fs * PT_PER_PX, align="right", valign="b", bold=False),
                                        name="Label"))
        for fid, page, kind, rect, fs in FIELDS:
            text = printed_value(values, fid)
            if page == n and text:
                b.text(p, text, rect, fs, kind)
        if n == 1:
            for fid, (y0, y1), fs, edges in CNICS:
                digits = re.sub(r"\D", "", values.get(fid, ""))[:13]
                for i, ch in enumerate(digits):
                    ci = DIGIT_CELLS[i]
                    b.anchor(p, _anchor_xml(b.ids, edges[ci], y0, edges[ci + 1], y1,
                                            _textbox(ch, fs * PT_PER_PX), name="Digit"))
        for kind, page, rect in IMAGES:
            if page == n and photos.get(kind):
                try:
                    b.image(p, _cover(photos[kind], rect[2] - rect[0], rect[3] - rect[1]), rect)
                except Exception:
                    pass                      # unreadable picture: listed in the documents table

    rows = record.get("cnic_rows") or []
    if rows:
        _cnic_page(b, rows)
    _record_section(b, record, values)
    out = io.BytesIO()
    b.doc.core_properties.title = f"Tooba Foundation Loan Application {record.get('app_id', '')}"
    b.doc.core_properties.subject = f"Version {record.get('version', 1)}"
    b.doc.core_properties.author = "Tooba Foundation Loan Department"
    b.doc.save(out)
    return out.getvalue()


def _cnic_page(b, rows):
    """Same geometry as static/cnicpage.js."""
    W, M, TOP, BOTTOM, GAP, TITLE, LABEL, COLGAP = FORM_W, 70, 210, 1700, 26, 54, 40, 30
    p = b.page_paragraph()
    b.anchor(p, _anchor_xml(b.ids, M, 80, W - M, 170, _textbox("CNIC DOCUMENTS", 64 * PT_PER_PX), name="Head"))
    n = len(rows)
    rh = (BOTTOM - TOP - GAP * (n - 1)) / n
    col_w = (W - 2 * M - COLGAP) / 2
    for i, r in enumerate(rows):
        y = TOP + i * (rh + GAP)
        title = f"{i + 1}. {clean(r.get('title'))}" + (f" — {clean(r.get('name'))}" if r.get("name") else "")
        b.anchor(p, _anchor_xml(b.ids, M, y, W - M, y + TITLE,
                                _textbox(title, 30 * PT_PER_PX, align="left"), name="Title"))
        for j, side in enumerate(("front", "back")):
            x = M + j * (col_w + COLGAP)
            b.anchor(p, _anchor_xml(b.ids, x, y + TITLE, x + col_w, y + TITLE + LABEL,
                                    _textbox(f"{side.title()} Side", 24 * PT_PER_PX, bold=False), name="Label"))
            box = (x, y + TITLE + LABEL, x + col_w, y + rh)
            data = r.get(side)
            placed = False
            if data:
                try:
                    jpg, (iw, ih) = _contain(data)
                    bw, bh = box[2] - box[0], box[3] - box[1]
                    k = min(bw / iw, bh / ih)
                    w, h = iw * k, ih * k
                    cx, cy = box[0] + (bw - w) / 2, box[1] + (bh - h) / 2
                    b.image(p, jpg, (cx, cy, cx + w, cy + h))
                    placed = True
                except Exception:
                    pass
            if not placed:
                b.anchor(p, _anchor_xml(b.ids, *box, _textbox("Not uploaded", 22 * PT_PER_PX, bold=False,
                                                                color="888888", border="BBBBBB"), name="Empty"))


def _record_section(b, record, values):
    """Ordinary flowing pages (with normal margins) holding the complete record as tables."""
    doc = b.doc
    sec = doc.add_section()
    for side in ("left_margin", "right_margin"):
        setattr(sec, side, Mm(15))
    sec.top_margin = sec.bottom_margin = Mm(15)
    from docx.enum.section import WD_SECTION
    sec.start_type = WD_SECTION.NEW_PAGE          # the section break itself starts the new page

    def heading(text, level=1):
        h = doc.add_heading(level=level)
        run = h.add_run(text)
        run.font.color.rgb = RGBColor(0x0E, 0x7A, 0x36)
        return h

    def table(rows, header=None, widths=None):
        cols = len(header or rows[0]) if (header or rows) else 2
        t = doc.add_table(rows=0, cols=cols)
        t.style = "Table Grid"
        if header:
            cells = t.add_row().cells
            for c, h in zip(cells, header):
                c.text = ""
                run = c.paragraphs[0].add_run(clean(h))
                run.bold = True
        for r in rows:
            cells = t.add_row().cells
            for c, v in zip(cells, r):
                _cell_text(c, clean(v))
        if widths:
            for row in t.rows:
                for c, w in zip(row.cells, widths):
                    c.width = Mm(w)
        doc.add_paragraph()
        return t

    heading(f"Complete record — {clean(record.get('app_id'))}")
    doc.add_paragraph(f"Generated: {clean(record.get('generated_at'))} (Pakistan time) · Archive version "
                      f"{record.get('version', 1)}. Pages 1–3 above are the original Tooba form filled with "
                      "the saved values; this section lists every saved value and related record.")
    if record.get("info"):
        heading("Application", 2)
        table(record["info"], widths=(60, 120))
    for title, fields in FORM_SECTIONS:
        rows = [(label.rstrip(" *"), values.get(name, "")) for name, label, _ in fields]
        heading(title, 2)
        table(rows, header=("Field", "Value"), widths=(70, 110))
    extra = sorted(k for k in values if k not in {n for _, fs in FORM_SECTIONS for n, _, _ in fs})
    if extra:
        heading("Other saved values", 2)
        table([(k, values[k]) for k in extra], header=("Field", "Value"), widths=(70, 110))
    heading("Photos on the form", 2)
    table([(en, "Uploaded" if (record.get("photos") or {}).get(k) else "Not uploaded")
           for k, en, _, _ in PHOTO_SLOTS], widths=(70, 110))
    if record.get("disbursement"):
        heading("Disbursement and cheques", 2)
        table(record["disbursement"], widths=(60, 120))
    heading("Review notes and remarks", 2)
    if record.get("notes"):
        table(record["notes"], header=("When", "Type", "By", "Note"), widths=(32, 25, 25, 98))
    else:
        doc.add_paragraph("None.")
    heading("Uploaded documents", 2)
    if record.get("documents"):
        table(record["documents"], header=("ID", "Kind", "File", "Type", "Bytes", "SHA-256", "Uploaded", "State"),
              widths=(10, 20, 30, 20, 15, 45, 22, 18))
    else:
        doc.add_paragraph("None.")
    for label, data in record.get("images") or []:
        try:
            jpg, (iw, ih) = _contain(data, 1200)
        except Exception:
            continue
        heading(clean(label), 3)
        if iw >= ih:
            doc.add_picture(io.BytesIO(jpg), width=Mm(170))
        else:
            doc.add_picture(io.BytesIO(jpg), height=Mm(190))
    heading("History", 2)
    if record.get("history"):
        table(record["history"], header=("When", "By", "Action", "Detail"), widths=(32, 25, 45, 78))
    else:
        doc.add_paragraph("None.")


def _cell_text(cell, text):
    cell.text = ""
    p = cell.paragraphs[0]
    run = p.add_run(text)
    if ARABIC.search(text):
        rpr = run._r.get_or_add_rPr()
        rpr.append(parse_xml(f'<w:rtl {NS}/>'))
        fonts = rpr.find("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rFonts")
        if fonts is None:
            fonts = parse_xml(f'<w:rFonts {NS} w:cs="{URDU_FONT}"/>')
            rpr.insert(0, fonts)


def verify(data, app_id=None, values=None):
    """Re-open a generated .docx and check it is a complete, readable Word file that really
    contains the application's values. Returns a list of problems (empty = OK)."""
    problems = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            bad = z.testzip()
            if bad:
                problems.append(f"corrupt part {bad}")
            xml = z.read("word/document.xml").decode("utf-8")
            media = [n for n in z.namelist() if n.startswith("word/media/")]
    except Exception as exc:
        return [f"not a valid .docx: {exc}"]
    try:
        Document(io.BytesIO(data))
    except Exception as exc:
        problems.append(f"Word cannot open it: {exc}")
    if len(media) < 3:
        problems.append("form page images missing")
    if app_id and escape(app_id) not in xml:
        problems.append("application ID missing")
    for k, v in (values or {}).items():
        v = clean(v)
        if v and escape(v) not in xml:
            problems.append(f"value of {k} missing")
    return problems


def build_guarantors(record):
    """Guarantor Records copy: both guarantors' details, photos and witness CNIC pictures."""
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(sec, side, Mm(15))
    values = {k: clean(v) for k, v in (record.get("values") or {}).items()}
    h = doc.add_heading(level=1)
    h.add_run(f"Guarantor records — {clean(record.get('app_id'))}").font.color.rgb = RGBColor(0x0E, 0x7A, 0x36)
    doc.add_paragraph(f"Applicant: {values.get('name', '')}  ·  CNIC: {values.get('cnic', '')}  ·  "
                      f"Generated {clean(record.get('generated_at'))} (Pakistan time), archive version "
                      f"{record.get('version', 1)}")
    photos = record.get("photos") or {}
    witness = {r.get("key"): r for r in record.get("cnic_rows") or []}
    for n, title, wkey in ((1, "Guarantor 1 (ضمانتی نمبر 1)", "w1"), (2, "Guarantor 2 (ضمانتی نمبر 2)", "w2")):
        doc.add_heading(title, level=2)
        fields = next(fs for t, fs in FORM_SECTIONS if t.startswith(f"Guarantor {n}"))
        t = doc.add_table(rows=0, cols=2)
        t.style = "Table Grid"
        for name, label, _ in fields:
            cells = t.add_row().cells
            _cell_text(cells[0], label)
            _cell_text(cells[1], values.get(name, ""))
        pics = [(f"Guarantor {n} photo", photos.get(f"g{n}Photo"))]
        w = witness.get(wkey) or {}
        pics += [(f"CNIC front", w.get("front")), (f"CNIC back", w.get("back"))]
        fam = witness.get(wkey + "fam")
        if fam:
            pics += [("Family member CNIC front", fam.get("front")), ("Family member CNIC back", fam.get("back"))]
        for label, data in pics:
            p = doc.add_paragraph()
            p.add_run(label + ": ").bold = True
            if not data:
                p.add_run("not uploaded")
                continue
            try:
                jpg, (iw, ih) = _contain(data, 900)
                doc.add_picture(io.BytesIO(jpg), width=Mm(70) if iw >= ih else None,
                                height=None if iw >= ih else Mm(70))
            except Exception:
                p.add_run("picture could not be read")
    out = io.BytesIO()
    doc.core_properties.title = f"Guarantor records {record.get('app_id', '')}"
    doc.save(out)
    return out.getvalue()
