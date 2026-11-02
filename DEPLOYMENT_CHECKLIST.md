# Tooba Foundation — Deployment Readiness

> **Do not enter real borrower/CNIC data until this checklist is completed and reviewed.**

## Current status

- Flask starter app is in the repository.
- Vercel config exists, but deployment has not yet been confirmed.
- The current app uses SQLAlchemy `DATABASE_URL`; Supabase environment variables alone do **not** connect the app to Supabase.
- The original loan application form is a large legacy HTML form and still needs to be wired to the application's save/update flow.
- Uploaded files are stored in the configured local upload folder. Vercel's filesystem is ephemeral; this is not durable document storage.
- A default admin bootstrap exists in the starter code. Before any deployment, replace default credentials and require a strong secret.

## Before deployment

- [ ] Set a random `SECRET_KEY` in Vercel environment variables.
- [ ] Set a unique `ADMIN_USERNAME` and a strong `ADMIN_PASSWORD` (at least 12 characters).
- [ ] Set `DATABASE_URL` to the Supabase PostgreSQL connection string (use the appropriate Supabase pooler/direct connection settings).
- [ ] Confirm migrations/schema match the Flask SQLAlchemy models.
- [ ] Verify login, logout, admin/staff permissions, and application access restrictions.
- [ ] Add CSRF protection to all forms that mutate data.
- [ ] Add rate limiting and secure cookie settings.
- [ ] Use Supabase Storage (private bucket) or another persistent private object store for documents.
- [ ] Ensure document download routes verify authorization and storage access.
- [ ] Configure database backups and test restoring one.
- [ ] Confirm secrets are only in Vercel environment settings and are never committed to Git.
- [ ] Test using synthetic data only.

## Recommended deployment sequence

1. Configure environment variables in Vercel (Preview first).
2. Deploy a Preview build.
3. Test with dummy borrower records and dummy files.
4. Review security and database schema.
5. Only after approval, deploy Production.
6. Keep real borrower/CNIC information out of logs, demo data, and public GitHub.

## Supabase notes

The Flask app connects to PostgreSQL through `DATABASE_URL`. `SUPABASE_URL` and `SUPABASE_ANON_KEY` do not automatically configure SQLAlchemy. Never expose the Supabase service-role key in browser-side code or commit it to this repository.
