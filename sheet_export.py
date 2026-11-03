"""Excel (.xlsx) version of a disbursement sheet, laid out like the foundation's own sheet:
logo banner, Disbursement Number + date, 12 columns with photos, totals, the
"Disbursement Report" block (fee-wise and purpose-wise) and signature lines."""
import io

COLS = ["S.No", "Name", "CNIC No.", "Cell No.", "Amount", "Purpose", "Loan No.", "Cheque No.",
        "Photo", "Signature", "Thumb", "Form Fees"]
WIDTHS = [6, 24, 17, 14, 12, 14, 10, 15, 16, 22, 18, 11]
ROW_H = 80          # points; tall enough for a photo, a signature and a thumb impression


def build_xlsx(sheet, logo_bytes=None):
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.page import PageMargins

    wb = Workbook()
    ws = wb.active
    ws.title = f"Disbursement {sheet['number']}"[:31]
    thin = Side(style="thin", color="000000")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left = Alignment(horizontal="left", vertical="center", wrap_text=True)
    green = "0E7A36"
    for i, w in enumerate(WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # banner
    ws.merge_cells("A1:L1")
    ws["A1"] = "THE TOOBA FOUNDATION"
    ws["A1"].font = Font(name="Arial Black", size=26, bold=True, color=green)
    ws["A1"].alignment = center
    ws.row_dimensions[1].height = 40
    ws.merge_cells("A2:L2")
    ws["A2"] = "& Community Center"
    ws["A2"].font = Font(size=14, bold=True, color="1B2F6E")
    ws["A2"].alignment = center
    ws.merge_cells("A3:L3")
    ws["A3"] = "سود سے پاک چھوٹے قرضوں کا فلاحی ادارہ"
    ws["A3"].font = Font(size=16, bold=True, color="C4372F")
    ws["A3"].alignment = center
    ws.row_dimensions[3].height = 26
    if logo_bytes:
        try:
            img = XLImage(io.BytesIO(logo_bytes))
            img.height, img.width = 70, 70
            ws.add_image(img, "L1")
        except Exception:
            pass

    # number + date
    ws.merge_cells("C5:E5")
    ws["C5"] = f"Disbursement Number {sheet['number_text']}"
    ws.merge_cells("F5:H5")
    ws["F5"] = sheet["date_text"]
    for ref in ("C5", "F5"):
        ws[ref].font = Font(bold=True, size=12, underline="single" if ref == "F5" else None)
        ws[ref].alignment = center
    for col in range(3, 9):
        ws.cell(row=5, column=col).border = box

    # header
    hr = 6
    for i, name in enumerate(COLS, 1):
        c = ws.cell(row=hr, column=i, value=name)
        c.font = Font(bold=True)
        c.alignment = center
        c.border = box
        c.fill = PatternFill("solid", fgColor="ECF7EF")
    ws.print_title_rows = f"{hr}:{hr}"

    r = hr
    for row in sheet["rows"]:
        r += 1
        vals = [row["sno"], row["name"], row["cnic"], row["phone"], row["amount"], row["purpose"],
                row["loan_no"], row["cheque_no"], None, None, None, row["fee"]]
        for i, v in enumerate(vals, 1):
            c = ws.cell(row=r, column=i, value=v)
            c.border = box
            c.alignment = left if i == 2 else center
            c.font = Font(bold=True)
            if i in (5, 12) and isinstance(v, (int, float)):
                c.number_format = "#,##0"
        ws.row_dimensions[r].height = ROW_H
        if row.get("photo"):
            try:
                img = XLImage(io.BytesIO(row["photo"]))
                ratio = img.width / img.height if img.height else 1
                img.height = ROW_H * 1.25 - 10
                img.width = img.height * ratio
                if img.width > WIDTHS[8] * 7:
                    img.width = WIDTHS[8] * 7
                    img.height = img.width / ratio
                ws.add_image(img, f"I{r}")
            except Exception:
                ws.cell(row=r, column=9, value="(photo)")

    # totals
    r += 1
    ws.cell(row=r, column=2, value="Total Amount").font = Font(bold=True, underline="single")
    t = ws.cell(row=r, column=5, value=sheet["total_amount"])
    t.font, t.number_format = Font(bold=True, underline="single"), "#,##0"
    f = ws.cell(row=r, column=12, value=sheet["total_fees"])
    f.font, f.number_format = Font(bold=True), "#,##0"
    ws.cell(row=r, column=7, value=f"Applicants: {sheet['count']}").font = Font(bold=True)

    # report block
    r += 3
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)
    ws.cell(row=r, column=1, value="Disbursement Report").font = Font(bold=True, size=14, underline="single")
    ws.cell(row=r, column=1).alignment = center
    r += 1
    heads = ["Form Fee", "Loan Amount", "Fee Collection", "Fee Amount", "Number of Loan", "Purpose", "Loan Amount"]
    for i, h in enumerate(heads, 1):
        c = ws.cell(row=r, column=i, value=h)
        c.font, c.alignment, c.border = Font(bold=True), center, box
    fee_rows, purpose_rows = sheet["fee_table"], sheet["purpose_table"]
    n = max(len(fee_rows), len(purpose_rows))
    for k in range(n):
        r += 1
        fr = fee_rows[k] if k < len(fee_rows) else None
        pr = purpose_rows[k] if k < len(purpose_rows) else None
        vals = [fr["fee"] if fr else None, fr["loan"] if fr else None, fr["count"] if fr else None,
                fr["sum"] if fr else None, pr["count"] if pr else None, pr["name"] if pr else None,
                pr["sum"] if pr else None]
        for i, v in enumerate(vals, 1):
            c = ws.cell(row=r, column=i, value=v)
            c.alignment, c.border = center, box
            if isinstance(v, (int, float)) and i in (1, 2, 4, 7):
                c.number_format = "#,##0"
    r += 1
    totals = [None, "Total", sheet["count"], sheet["total_fees"], sheet["count"], None, sheet["total_amount"]]
    for i, v in enumerate(totals, 1):
        c = ws.cell(row=r, column=i, value=v)
        c.font, c.alignment, c.border = Font(bold=True), center, box
        if isinstance(v, (int, float)) and i in (4, 7):
            c.number_format = "#,##0"
    r += 3
    for col, label in ((2, "President"), (4, "General Secretary"), (7, "Enquiry Officer")):
        c = ws.cell(row=r, column=col, value=label)
        c.font, c.alignment = Font(bold=True, underline="single"), center

    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins = PageMargins(left=0.3, right=0.3, top=0.4, bottom=0.5)
    ws.oddFooter.center.text = "Page &P of &N"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
