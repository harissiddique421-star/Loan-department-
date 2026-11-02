"""Tooba Foundation 3-page loan form — field list.

Each field is (name, label, kind). `name` must match the id used in
static/loanform.js (FIELDS / CNICS), which says where on the scanned page the
text is printed. Do not rename a `name` after real data has been saved.

kind: text | num (digits keyboard) | phone (11 digits) | cnic (13 digits) | row (full width)
"""

FORM_SECTIONS = [
    ("Applicant — Page 1", [
        ("amount", "Loan Amount (قرضہ)", "num"),
        ("name", "Name (نام) *", "text"),
        ("father", "Father / Husband Name (ولدیت/شوہر کا نام)", "text"),
        ("cnic", "CNIC (13 digits)", "cnic"),
        ("phone", "Phone No. (فون نمبر)", "phone"),
        ("familyPhone", "Family Phone No. (فیملی فون نمبر)", "phone"),
        ("qist", "Monthly Installment (ہر ماہ قسط)", "num"),
        ("address", "Permanent Address (پتہ)", "text"),
        ("business", "Business / Job (کاروبار/ملازمت)", "text"),
        ("businessAddress", "Business Address (کاروبار کا پتہ)", "text"),
        ("income", "Monthly Income (کل آمدن)", "num"),
        ("installment", "Mutawaka Amdan (متوقع آمدن)", "num"),
        ("eduExpense", "Educational Expenses (تعلیمی اخراجات)", "num"),
        ("jobFamily", "Earning Persons in Family (کمانے والے افراد)", "num"),
        ("expenses", "Total Expenses (کل اخراجات)", "num"),
        ("family", "Family Members (گھر کے افراد)", "num"),
        ("houseEdu", "Education (تعلیم)", "text"),
        ("relative1", "Nearest Masjid (قریب ترین مسجد)", "text"),
        ("relative2", "Nearest Shop (قریب ترین دکان)", "text"),
    ]),
    ("Guarantor 1 (ضمانتی نمبر 1) — Page 1", [
        ("g1name", "Name", "text"),
        ("g1father", "Father Name", "text"),
        ("g1cnic", "CNIC (13 digits)", "cnic"),
        ("g1phone", "Phone", "phone"),
        ("g1address", "Address", "text"),
        ("g1business", "Business / Job (پیشہ)", "text"),
    ]),
    ("Guarantor 2 (ضمانتی نمبر 2) — Page 1", [
        ("g2name", "Name", "text"),
        ("g2father", "Father Name", "text"),
        ("g2cnic", "CNIC (13 digits)", "cnic"),
        ("g2phone", "Phone", "phone"),
        ("g2address", "Address", "text"),
        ("g2business", "Business / Job (پیشہ)", "text"),
    ]),
    ("Loan Details (قرضے کی تفصیل) — Page 3", [
        ("detail1", "Row 1", "row"),
        ("detail2", "Row 2", "row"),
        ("detail3", "Row 3", "row"),
        ("detail4", "Row 4", "row"),
    ]),
]

# Pictures printed on the form: (kind, English label, Urdu label, page)
PHOTO_SLOTS = [
    ("photo", "Applicant Photo", "درخواست گزار کی تصویر", 1),
    ("g1Photo", "Guarantor 1 Photo", "تصویر ضمانتی 1", 2),
    ("g2Photo", "Guarantor 2 Photo", "تصویر ضمانتی 2", 2),
]

FIELD_NAMES = [n for _, fields in FORM_SECTIONS for n, _, _ in fields]
FIELD_LABELS = {n: label.rstrip(" *") for _, fields in FORM_SECTIONS for n, label, _ in fields}
PHOTO_KINDS = [k for k, *_ in PHOTO_SLOTS]
PHOTO_LABELS = {k: en for k, en, _, _ in PHOTO_SLOTS}
