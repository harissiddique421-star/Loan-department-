"""Google Sheets layout of the disbursement sheets, drawn like the printed sheet in the app:
banner with logo, "Disbursement Number" box, the 12-column table with photos, totals, the
"Disbursement Report" (fee-wise and purpose-wise) and the signature lines.

Pure Python: builds a Grid of cells (value + format) that gsheets.GoogleClient.write_layout
sends in one batchUpdate. Applicant text is always written as a plain string (never a
formula), so nothing typed in a form can run inside the spreadsheet.
"""
GREEN = "#0E7A36"
GREEN_DARK = "#07552C"
GREEN_SOFT = "#ECF7EF"
NAVY = "#1B2F6E"
RED = "#C4372F"
GREY = "#6B7280"
URDU_TAGLINE = "سود سے پاک چھوٹے قرضوں کا فلاحی ادارہ"

COLS = ["S.No", "Name", "CNIC No.", "Cell No.", "Amount", "Purpose", "Loan No.", "Cheque No.",
        "Photo", "Signature", "Thumb", "Form Fees"]
# pixels — wide enough for a CNIC, a photo, and a signature / thumb impression
WIDTHS = [52, 170, 135, 115, 90, 110, 95, 110, 110, 150, 120, 90]
NCOLS = len(COLS)
MONEY = "#,##0"


def _rgb(hexcolor):
    h = hexcolor.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


def _border(style="SOLID", color="#000000"):
    return {"style": style, "colorStyle": {"rgbColor": _rgb(color)}}


class Grid:
    def __init__(self, ncols=NCOLS):
        self.ncols = ncols
        self.cells = {}            # (row, col) -> (value, formula, style)
        self.merges = []           # (r1, c1, r2, c2) inclusive
        self.heights = {}          # row -> px
        self.row = 0

    # --- writing
    def put(self, r, c, value=None, formula=None, **style):
        old = self.cells.get((r, c))
        merged = dict(old[2]) if old else {}
        merged.update(style)
        if old and value is None and formula is None:
            value, formula = old[0], old[1]
        self.cells[(r, c)] = (value, formula, merged)

    def style(self, r1, c1, r2, c2, **style):
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                self.put(r, c, **style)

    def merge(self, r1, c1, r2, c2):
        if (r1, c1) != (r2, c2):
            self.merges.append((r1, c1, r2, c2))

    def box(self, r1, c1, r2, c2):
        self.style(r1, c1, r2, c2, border=True)

    @property
    def nrows(self):
        return max([r for r, _ in self.cells] + [r for r in self.heights] + [self.row - 1, 0]) + 1

    def values(self):
        """Plain values row by row (used by tests and as a readable dump)."""
        out = [["" for _ in range(self.ncols)] for _ in range(self.nrows)]
        for (r, c), (v, f, _) in self.cells.items():
            out[r][c] = v if v is not None else (f or "")
        return out

    # --- Google Sheets API
    def _cell(self, value, formula, s):
        cell = {}
        if formula:
            cell["userEnteredValue"] = {"formulaValue": formula}
        elif isinstance(value, bool) or value is None or value == "":
            pass
        elif isinstance(value, (int, float)):
            cell["userEnteredValue"] = {"numberValue": value}
        else:
            cell["userEnteredValue"] = {"stringValue": str(value)}
        fmt, text = {}, {}
        if s.get("bold"):
            text["bold"] = True
        if s.get("italic"):
            text["italic"] = True
        if s.get("underline"):
            text["underline"] = True
        if s.get("size"):
            text["fontSize"] = s["size"]
        if s.get("font"):
            text["fontFamily"] = s["font"]
        if s.get("color"):
            text["foregroundColorStyle"] = {"rgbColor": _rgb(s["color"])}
        if text:
            fmt["textFormat"] = text
        if s.get("bg"):
            fmt["backgroundColorStyle"] = {"rgbColor": _rgb(s["bg"])}
        fmt["horizontalAlignment"] = s.get("align", "CENTER")
        fmt["verticalAlignment"] = s.get("valign", "MIDDLE")
        fmt["wrapStrategy"] = "WRAP" if s.get("wrap", True) else "OVERFLOW_CELL"
        if s.get("num"):
            fmt["numberFormat"] = {"type": "NUMBER", "pattern": s["num"]}
        elif isinstance(value, str):
            fmt["numberFormat"] = {"type": "TEXT"}
        if s.get("border"):
            b = _border()
            fmt["borders"] = {"top": b, "bottom": b, "left": b, "right": b}
        elif s.get("line_below"):
            fmt["borders"] = {"bottom": _border("SOLID_THICK", s["line_below"])}
        cell["userEnteredFormat"] = fmt
        return cell

    def requests(self, sheet_id):
        nrows = max(self.nrows + 5, 60)
        reqs = [
            {"unmergeCells": {"range": {"sheetId": sheet_id}}},
            {"updateCells": {"range": {"sheetId": sheet_id}, "fields": "*"}},          # clear everything
            {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {
                "rowCount": nrows, "columnCount": self.ncols, "hideGridlines": True, "frozenRowCount": 0}},
                "fields": "gridProperties(rowCount,columnCount,hideGridlines,frozenRowCount)"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "ROWS",
                                                     "startIndex": 0, "endIndex": nrows},
                                           "properties": {"pixelSize": 24}, "fields": "pixelSize"}},
        ]
        widths = WIDTHS if self.ncols == NCOLS else [140] * self.ncols
        for i, w in enumerate(widths[:self.ncols]):
            reqs.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                                 "startIndex": i, "endIndex": i + 1},
                                                       "properties": {"pixelSize": w}, "fields": "pixelSize"}})
        rows = []
        for r in range(self.nrows):
            vals = []
            for c in range(self.ncols):
                if (r, c) in self.cells:
                    vals.append(self._cell(*self.cells[(r, c)]))
                else:
                    vals.append({})            # untouched cell: cleared above, nothing to send
            rows.append({"values": vals})
        reqs.append({"updateCells": {"start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 0},
                                     "rows": rows, "fields": "userEnteredValue,userEnteredFormat"}})
        for r1, c1, r2, c2 in self.merges:
            reqs.append({"mergeCells": {"mergeType": "MERGE_ALL", "range": {
                "sheetId": sheet_id, "startRowIndex": r1, "endRowIndex": r2 + 1,
                "startColumnIndex": c1, "endColumnIndex": c2 + 1}}})
        for r, px in sorted(self.heights.items()):
            reqs.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "ROWS",
                                                                 "startIndex": r, "endIndex": r + 1},
                                                       "properties": {"pixelSize": px}, "fields": "pixelSize"}})
        return reqs


def _image(url):
    # Google fetches the picture itself; mode 1 = fit inside the cell, keep proportions
    return f'=IMAGE("{url}", 1)' if url and '"' not in url else None


def banner(g, logo_url=None):
    r = g.row
    g.put(r, 0, "THE TOOBA FOUNDATION", bold=True, size=24, font="Montserrat", color=GREEN)
    g.merge(r, 0, r, NCOLS - 2)
    g.put(r + 1, 0, "& Community Center", bold=True, size=13, color=NAVY)
    g.merge(r + 1, 0, r + 1, NCOLS - 2)
    g.put(r + 2, 0, URDU_TAGLINE, bold=True, size=15, color=RED)
    g.merge(r + 2, 0, r + 2, NCOLS - 2)
    g.put(r, NCOLS - 1, formula=_image(logo_url))
    g.merge(r, NCOLS - 1, r + 2, NCOLS - 1)
    g.heights.update({r: 46, r + 1: 26, r + 2: 34, r + 3: 8})
    g.style(r + 3, 0, r + 3, NCOLS - 1, line_below=GREEN)
    g.row = r + 5


def sheet_block(g, s, link=None, photo=None, logo_url=None, status=None):
    """One disbursement sheet. `s` is app.sheet_data(batch)."""
    banner(g, logo_url)
    r = g.row
    g.put(r, 2, f"Disbursement Number {s['number_text']}", bold=True, size=12)
    g.merge(r, 2, r, 4)
    g.put(r, 5, s["date_text"], bold=True, size=12, underline=True)
    g.merge(r, 5, r, 7)
    g.box(r, 2, r, 7)
    if status:
        g.put(r, NCOLS - 1, status, bold=True, color=GREEN if status == "Open" else RED, size=9)
    g.heights[r] = 34
    r += 2
    for c, name in enumerate(COLS):
        g.put(r, c, name, bold=True, bg=GREEN_SOFT)
    g.box(r, 0, r, NCOLS - 1)
    g.heights[r] = 40
    for row in s["rows"]:
        r += 1
        vals = [row["sno"], row["name"], row["cnic"], row["phone"], row["amount"], row["purpose"],
                row["loan_no"], row["cheque_no"]]
        for c, v in enumerate(vals):
            g.put(r, c, v, bold=True, align="LEFT" if c == 1 else "CENTER",
                  num=MONEY if c == 4 else None)
        img = _image(photo(row)) if photo else None
        if img:
            g.put(r, 8, formula=img)
        elif link:
            url = link(row)
            if url and '"' not in url:
                g.put(r, 8, formula=f'=HYPERLINK("{url}", "View")', color=GREEN, underline=True)
        g.put(r, NCOLS - 1, row["fee"], bold=True, num=MONEY)
        g.box(r, 0, r, NCOLS - 1)
        g.heights[r] = 96 if img else 34
    if not s["rows"]:
        r += 1
        g.put(r, 0, "Abhi is sheet mein koi application nahi.", italic=True, color=GREY)
        g.merge(r, 0, r, NCOLS - 1)
        g.box(r, 0, r, NCOLS - 1)
    # totals
    r += 1
    g.put(r, 1, "Total Amount", bold=True, underline=True)
    g.put(r, 2, f"Applicants: {s['count']}", bold=True)
    g.merge(r, 2, r, 3)
    g.put(r, 4, s["total_amount"], bold=True, underline=True, num=MONEY)
    g.put(r, 7, f"Disbursed: {s['disbursed_amount']:,}", bold=True)
    g.merge(r, 7, r, 8)
    g.put(r, NCOLS - 1, s["total_fees"], bold=True, num=MONEY)
    g.heights[r] = 32
    # Disbursement Report — fee-wise (B..E) and purpose-wise (F..H), like the printed sheet
    r += 3
    g.put(r, 1, "Disbursement Report", bold=True, size=14, underline=True)
    g.merge(r, 1, r, 7)
    g.heights[r] = 32
    r += 1
    heads = ["Form Fee", "Loan Amount", "Fee Collection", "Fee Amount", "Number of Loan", "Purpose", "Loan Amount"]
    for i, h in enumerate(heads):
        g.put(r, 1 + i, h, bold=True, bg=GREEN_SOFT)
    g.box(r, 1, r, 7)
    g.heights[r] = 36
    fees, purposes = s["fee_table"], s["purpose_table"]
    for k in range(max(len(fees), len(purposes))):
        r += 1
        f = fees[k] if k < len(fees) else None
        p = purposes[k] if k < len(purposes) else None
        vals = [f["fee"] if f else None, f["loan"] if f else None, f["count"] if f else None,
                f["sum"] if f else None, p["count"] if p else None, p["name"] if p else None,
                p["sum"] if p else None]
        for i, v in enumerate(vals):
            g.put(r, 1 + i, v, num=MONEY if i in (0, 1, 3, 6) and v is not None else None)
        g.box(r, 1, r, 7)
        g.heights[r] = 28
    r += 1
    for i, v in enumerate([None, "Total", s["count"], s["total_fees"], s["count"], None, s["total_amount"]]):
        g.put(r, 1 + i, v, bold=True, num=MONEY if i in (3, 6) else None)
    g.box(r, 1, r, 7)
    g.heights[r] = 30
    # signatures
    r += 4
    for c, label in ((1, "President"), (4, "General Secretary"), (8, "Enquiry Officer")):
        g.put(r, c, label, bold=True, underline=True)
        g.merge(r, c, r, c + 1)
    g.heights[r] = 30
    g.row = r + 4


def month_grid(sheets, link=None, photo=None, logo_url=None):
    """`sheets`: list of (sheet_data dict, "Open"/"Closed") for one month, in date order."""
    g = Grid()
    for s, status in sheets:
        sheet_block(g, s, link=link, photo=photo, logo_url=logo_url, status=status)
    if len(sheets) > 1:
        r = g.row
        n = sum(s["count"] for s, _ in sheets)
        g.put(r, 1, "Month total", bold=True, size=12, bg=GREEN_SOFT)
        g.put(r, 2, f"Sheets: {len(sheets)}", bold=True, bg=GREEN_SOFT)
        g.put(r, 3, f"Applicants: {n}", bold=True, bg=GREEN_SOFT)
        g.put(r, 4, sum(s["total_amount"] for s, _ in sheets), bold=True, num=MONEY, bg=GREEN_SOFT)
        g.put(r, NCOLS - 1, sum(s["total_fees"] for s, _ in sheets), bold=True, num=MONEY, bg=GREEN_SOFT)
        g.style(r, 1, r, NCOLS - 1, bg=GREEN_SOFT)
        g.box(r, 1, r, NCOLS - 1)
        g.heights[r] = 34
        g.row = r + 2
    if not sheets:
        banner(g, logo_url)
        g.put(g.row, 0, "Is mahine abhi koi disbursement sheet nahi.", italic=True, color=GREY)
        g.merge(g.row, 0, g.row, NCOLS - 1)
    return g


def summary_grid(year, months, purposes, fees, logo_url=None):
    """Yearly Summary tab. months: [(name, sheets, applicants, amount, disbursed, fees)]."""
    g = Grid()
    banner(g, logo_url)
    r = g.row
    g.put(r, 1, f"Yearly Summary {year}", bold=True, size=15, color=GREEN_DARK)
    g.merge(r, 1, r, 6)
    g.heights[r] = 34
    r += 2
    heads = ["Month", "Sheets", "Applicants", "Total Amount", "Disbursed Amount", "Form Fees"]
    for i, h in enumerate(heads):
        g.put(r, 1 + i, h, bold=True, bg=GREEN_SOFT)
    g.box(r, 1, r, 6)
    g.heights[r] = 36
    tot = [0] * 5
    for m in months:
        r += 1
        g.put(r, 1, m[0], bold=True, align="LEFT")
        for i, v in enumerate(m[1:]):
            g.put(r, 2 + i, v, num=MONEY if i >= 2 else None)
            tot[i] += v
        g.box(r, 1, r, 6)
        g.heights[r] = 28
    r += 1
    g.put(r, 1, "Total", bold=True, align="LEFT", bg=GREEN_SOFT)
    for i, v in enumerate(tot):
        g.put(r, 2 + i, v, bold=True, num=MONEY if i >= 2 else None, bg=GREEN_SOFT)
    g.box(r, 1, r, 6)
    g.heights[r] = 32
    for title, heads, rows in (("Purpose-wise", ["Purpose", "Number of Loans", "Loan Amount"], purposes),
                               ("Fee-wise", ["Form Fee", "Fee Collection", "Fee Amount"], fees)):
        r += 3
        g.put(r, 1, title, bold=True, size=13, underline=True)
        g.merge(r, 1, r, 3)
        r += 1
        for i, h in enumerate(heads):
            g.put(r, 1 + i, h, bold=True, bg=GREEN_SOFT)
        g.box(r, 1, r, 3)
        g.heights[r] = 32
        for row in rows:
            r += 1
            for i, v in enumerate(row):
                g.put(r, 1 + i, v, num=MONEY if isinstance(v, (int, float)) and (i == 2 or title == "Fee-wise") else None)
            g.box(r, 1, r, 3)
        if not rows:
            r += 1
            g.put(r, 1, "—", color=GREY)
            g.merge(r, 1, r, 3)
            g.box(r, 1, r, 3)
    g.row = r + 2
    return g
