# Tooba Foundation — Deployment Gate

**Do not use real applicant/CNIC information until every item below is complete and verified.**

## Required before production
- [ ] Restore the full original loan form and connect every field to persisted application data.
- [ ] Configure a strong, unique `SECRET_KEY`; remove development/default secrets and rotate any credentials that were exposed.
- [ ] Set a unique initial administrator password through environment variables; change it immediately after first login.
- [ ] Add CSRF protection to every state-changing form (login, create/edit, upload, user management, assignment, toggle).
- [ ] Enforce least-privilege roles and record-level access on every application/document/export route.
- [ ] Configure Supabase PostgreSQL connection securely; do not commit connection strings, service-role keys, or passwords.
- [ ] Enable and test Row Level Security where Supabase client access is used. Keep the service-role key server-side only.
- [ ] Replace local uploads with a private storage bucket and short-lived signed URLs. Vercel filesystem is ephemeral.
- [ ] Validate uploaded file type/content, size, and access; keep documents private.
- [ ] Configure HTTPS-only secure cookies, HttpOnly, SameSite, and session expiry.
- [ ] Add audit logging for view/download/update/export and admin actions without logging secrets or full CNICs.
- [ ] Add backups, restore test, retention policy, and operational owner.
- [ ] Run tests for staff/admin permissions, duplicate CNIC handling, form persistence, uploads/downloads, exports, and reports.
- [ ] Verify production and preview environments separately; do not use real data in previews.

## Known starter limitations
- The current repository contains a starter implementation.
- The legacy form page is a placeholder and is not connected to save/load behavior.
- Local document uploads are not durable on Vercel.
- Supabase schema and app model compatibility need migration/testing.
- This checklist is not a certification that the application is production-safe.
