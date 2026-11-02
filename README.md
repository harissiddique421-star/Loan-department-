# Tooba Foundation Loan Department — starter application

Flask starter for a loan-management workflow, intended for deployment with Vercel + Supabase.

## Features included
- Flask login and Admin/Staff starter roles
- Application create/edit, search, status, staff assignment
- CSV export, basic reports, JSON backup
- Document upload/download routes
- Supabase PostgreSQL starter schema
- Vercel Python configuration

## Deploy
1. Configure Vercel project from this repository, framework preset Other / Python.
2. Add environment variables from `.env.example` in Vercel project settings.
3. Set `DATABASE_URL` to the Supabase PostgreSQL connection string.
4. Run `supabase/schema.sql` in Supabase SQL Editor.
5. Deploy only after testing.

## Important limitations
- This is a starter/demo scaffold, **not production-ready**.
- The original legacy form is not fully restored. `templates/original_form.html` is a placeholder and does not save its fields.
- File uploads use local `UPLOAD_FOLDER`; Vercel storage is ephemeral. Replace with private Supabase Storage and signed URLs before use.
- Schema compatibility/migrations must be confirmed.
- Configure RLS, CSRF protection, secure sessions, access policies, backups, and audit logging before real borrower/CNIC data.
- Set strong admin credentials in Vercel env before deployment. Never commit secrets/service-role key.
- Use synthetic data for testing until reviewed.

## Local run
```bash
python -m pip install -r requirements.txt
export SECRET_KEY='long-random-secret'
export ADMIN_USERNAME='admin'
export ADMIN_PASSWORD='replace-with-strong-password'
python app.py
```
