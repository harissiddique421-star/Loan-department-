"""Loan form fields. Edit this list to add/remove/rename fields.

The form page, the print page and the CSV export all read from here, so a
field added here appears everywhere automatically. Each field is:
    (name, label, type)          type = text | number | date | textarea | select:A,B,C
`name` is the key stored in the database — do not rename it after real data is saved.
"""

FORM_SECTIONS = [
    ("Applicant Information", [
        ("full_name", "Full Name *", "text"),
        ("father_name", "Father / Husband Name", "text"),
        ("cnic", "CNIC", "text"),
        ("dob", "Date of Birth", "date"),
        ("gender", "Gender", "select:,Male,Female"),
        ("marital_status", "Marital Status", "select:,Single,Married,Widowed,Divorced"),
        ("phone", "Contact Number", "text"),
        ("alternate_phone", "Alternate Contact", "text"),
        ("address", "Address", "textarea"),
        ("city", "City / Area", "text"),
    ]),
    ("Loan Information", [
        ("amount", "Requested Amount (PKR)", "number"),
        ("duration_months", "Loan Duration (months)", "number"),
        ("purpose", "Loan Purpose", "textarea"),
    ]),
    ("Income / Employment", [
        ("occupation", "Occupation", "text"),
        ("monthly_income", "Monthly Income (PKR)", "number"),
        ("employer", "Employer / Business", "text"),
        ("employment_duration", "Employment Duration", "text"),
        ("family_members", "Family Members", "number"),
        ("monthly_expenses", "Monthly Expenses (PKR)", "number"),
        ("income_details", "Income / Repayment Details", "textarea"),
    ]),
    ("Guarantor", [
        ("guarantor_name", "Guarantor Name", "text"),
        ("guarantor_cnic", "Guarantor CNIC", "text"),
        ("guarantor_phone", "Guarantor Contact", "text"),
        ("guarantor_relationship", "Relationship", "text"),
        ("guarantor_address", "Guarantor Address", "textarea"),
    ]),
    ("Office Use", [
        ("verified_by", "Verified By", "text"),
        ("verification_date", "Verification Date", "date"),
        ("approved_amount", "Approved Amount (PKR)", "number"),
        ("notes", "Additional Notes / Remarks", "textarea"),
    ]),
]

FIELD_LABELS = {name: label.rstrip(" *") for _, fields in FORM_SECTIONS for name, label, _ in fields}
