# Tooba Foundation — Loan Department

An online loan application system. Staff log in with a username and password, fill in the loan form, and upload the applicant's photo and documents. Anyone with a login can later open, edit and print the application.

## Features
- **Login**: each person gets their own username and password. An Admin creates them under *Users*.
- **Form save**: every field is saved in the database (Supabase PostgreSQL), so nothing is lost on refresh or redeploy.
- **Shared access**: any logged-in user can open, edit and update any application. Set `RESTRICT_STAFF_ACCESS=1` if Staff should only see the applications they created or are assigned to.
- **Photo & documents**: upload the applicant photo and several documents (PDF/JPG/PNG/WEBP/DOC/DOCX, 15 MB per request).
  - The photo shows a preview before saving, and the phone camera works.
  - Images show as thumbnails. Files can be downloaded or deleted.
- **Print**: the *Print* button opens an A4 page with the photo, all fields and signature lines. To get a PDF, choose *Save as PDF* in the print dialog.
- **History**: every create, save, upload, print and delete is logged with the user and the time.
- **Admin**:
  - manage users and reset passwords
  - reports
  - CSV export (opens in Excel)
  - JSON backup
  - delete applications

## Changing the form fields
All fields are defined in **`form_fields.py`**. When you add or remove a field, or change its label there, the form page, print page and CSV export all update automatically. Do not change a field's `name` after real data has been saved.

## Where data is stored
| What | Where |
|---|---|
| Form data, users, history | The PostgreSQL database in `DATABASE_URL` (Supabase) |
| Photos / documents | A private Supabase Storage bucket if `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are set; otherwise inside the database |

Without `DATABASE_URL`, the app uses a local SQLite file instead. On Vercel that file is **temporary** and gets deleted, so a red warning appears on every page while this is the case.

## Deploy (Vercel + Supabase)
1. Create a Supabase project. Go to *Connect* (or *Project Settings → Database*), copy the **Transaction pooler** connection string (port 6543) and put your database password into it.
2. In Vercel, go to *Project → Settings → Environment Variables* and add:
   - `DATABASE_URL`: the Supabase connection string
   - `SECRET_KEY`: a long random string (`python -c "import secrets;print(secrets.token_hex(32))"`)
   - `ADMIN_USERNAME` and `ADMIN_PASSWORD`: the first admin login
   - Optional, to store files in Supabase Storage: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` and `SUPABASE_BUCKET`. Create the bucket as **private**.
3. Redeploy. The tables are created automatically on first start. Older databases get the new columns added automatically.
4. Log in as admin, open *Users*, and create a login for each staff member.

## Local run
```bash
python -m pip install -r requirements.txt
export SECRET_KEY='long-random-secret' ADMIN_USERNAME='admin' ADMIN_PASSWORD='replace-with-strong-password'
python app.py
```

## Security notes
Included:
- CSRF protection
- hashed passwords
- HttpOnly and secure session cookies that expire after 8 hours
- file downloads only after login
- an audit log

You still need to use strong passwords, never commit `.env` files or keys, and keep regular backups.
