# Project Roadmap

## 🧱 Production Infrastructure & Data Safety (COMPLETED)

_Last updated: 2025‑12‑24_

These items are now **implemented, verified, and in active use on the production server**.  
They are considered **baseline guarantees** going forward and should not be regressed.

### ✅ Server runtime & persistence
- [x] Application runs under **Gunicorn + systemd**
- [x] Service auto-starts on boot (`impact-cms.service`)
- [x] App survives SSH disconnects and reboots
- [x] Verified via systemd status, journalctl, and curl checks

### ✅ Database safety & backups
- [x] PostgreSQL production database in use
- [x] Automated nightly logical backups (pg_dump, gzip)
- [x] Backups written to RAID-backed storage
- [x] Backup script verified manually
- [x] Cron job installed and confirmed
- [x] Restore path documented and tested conceptually

### ✅ RAID storage
- [x] 3.6 TB RAID mounted at `/mnt/impact_raid`
- [x] Auto-mounted via `/etc/fstab`
- [x] Permissions corrected (`impact:impact`, 775)
- [x] Dedicated directories created:
  - `/mnt/impact_raid/impact_db_backups`
  - `/mnt/impact_raid/impact_documents`
  - `/mnt/impact_raid/time_machine`

### ✅ Time Machine (Mac backups)
- [x] Samba configured and running
- [x] Avahi advertising Time Machine service
- [x] Both Macs detected existing sparsebundles
- [x] Backups actively running from macOS
- [x] Time Machine data preserved during migration from Raspberry Pi

### ⚠️ Operational rules (IMPORTANT)
- **NO schema changes** on production while Gina is entering live data  
  → Any model change requires Alembic migration
- UI / CSS / JS / route logic fixes are allowed
- Backups must remain enabled at all times
- RAID is the authoritative store for backups and documents

---

## 🔥 Current Stabilization Backlog (Authoritative Checklist)
_This is the single source of truth for active bugs, regressions, and polish items.  
Items here supersede older phase notes until checked off._

### A) Data safety and validation polish
- [x] Global phone/fax input masking across the entire app (auto ( ) -)  
  _Test: type digits in every phone/fax field; confirm formatting + doesn’t fight cursor._
- [x] Phone/fax validation: allow blank; if not blank require correct digit count  
  _Test: blank saves; 9 digits rejects; 10 digits saves._
- [x] Email validation: allow blank; if not blank must be valid  
  _Test: blank saves; x@ rejects; x@y.com saves._
- [x] ZIP validation: allow blank; if not blank must be 5 or 5+4  
  _Test: blank saves; 1234 rejects; 12345 saves; 12345-6789 saves._
- [x] No data loss on validation errors (forms re-render with typed data intact)  
  _Test: intentionally fail validation and confirm fields retain values exactly._
- [ ] Field-level error highlighting (red outline + message near field)  
  _Future enhancement: used later for report DOS/date validation._

### H) Deletes / referential integrity
- [x] Delete Claim succeeds without FK crashes
  _Resolved: invoices, reports, and dependent records now cleanly handled._
- [x] Delete Provider/Employer/Carrier contacts safely clears Claim references
  _Behavior: if a contact is referenced by a Claim, the FK is cleared before delete (no replacement required)._ 

### C) Contacts: CRUD + roles (per parent type)
#### Carrier contacts
- [x] Edit loads existing contact into form
- [x] Edit updates record (doesn’t create new)
- [x] Role/Title dropdown persists + reloads on edit  
  Status: ✅ confirmed working
#### Employer contacts
- [x] Edit loads existing contact into form
- [x] Edit updates record (doesn’t create new)
- [x] Role/Title dropdown persists + reloads on edit  
  Status: ✅ confirmed working
#### Provider contacts
- [x] Edit loads existing contact into form
- [x] Edit updates record (doesn’t create new)
- [x] Role/Title dropdown persists + reloads on edit  
  Status: ✅ confirmed working
_Test script for all 3: Create contact w/ role → save → refresh page → confirm role displays → click Edit → role is selected → change role → save → refresh → confirm changed._

### F) Billables + Billing Activity Codes
- [ ] Billing activity code list exists + populates dropdowns in claim/report billables  
  _Known problems we saw: code length constraint, label NULL, “rate” mismatch._
- [ ] Add new billing activity code works (requires label and code; no per-code rate)
- [ ] Billable item creation persists and appears immediately in the table  
  _Known issue: item “disappears” after add → verify._
- [ ] Billables completeness rules enforced (“NO BILL” special case)
- [ ] Long-format notes field on billables (notes → report; short desc → invoice/report)  
  _Status: likely still pending unless we already implemented._

### E) Claims / Reports workflow
- [ ] “New Report” from Claim Detail works for Initial/Progress/Closure  
  _Test: click each type → new report created → lands on edit page._
- [x] Initial Report: "Primary Care Provider / Family Doctor" saves and persists  
  _Status: column added, model + routes wired, persists correctly._
- [x] Treating Provider checkboxes selectable + persist on save  
  _Status: selectable, persists, and carries forward from previous report._
- [x] Possible Barriers to Recovery checkboxes selectable + persist on save  
  _Status: selectable, persists, and carries forward from previous report._
- [x] Report numbering logic (Initial=0 not printed; Progress starts at 1; Closure not numbered)  
  _Status: verified working; keep counting based on visible/non-deleted reports._
- [ ] Report edit screen not spamming status updates / refresh loops  
  _Test: open report edit and watch top banner behavior._
- [ ] Roll-forward per-field works (shared long text fields)  
  _Test: click roll-forward on a field with a previous report._
- [ ] Roll-forward buttons not functioning on report edit screens  
  _Note: Buttons render but do not populate fields. Likely similar fix to treating providers / barriers carry-forward, but defer until report field mapping is finalized._
- [ ] ICS download works for Next Appointment  
  _Note: previously flagged to fix; verify current behavior._
- [ ] Closure report print/layout tweaks (remove unused fields, update closure reasons, full-width Closure Details)  
  _Priority: NEXT (blocker for shadowing). Do this before server cutover._
- [ ] Report date auto-fill + non-overlap enforcement (DOS start/end logic prevents overlaps across reports)  
  _Add rule: Initial DOS start = referral date; Progress/Closure DOS start = day after prior report DOS end; DOS end = today; validate overlap and warn._
  _Priority: lower (after closure report + server go-live)._
- [ ] Confirm/decide auto-billable creation for reports (Initial/Progress/Closure)  
  _Status: currently unclear; verify with Gina and either implement or explicitly disable._
  _Priority: lower (defer until Gina confirms desired behavior after shadowing starts)._
- [x] Date picker standardized using Flatpickr on Claim New and Billable Item date fields  
  _Status: Flatpickr initialized globally and verified working._

### G) Invoices

### K) Go‑Live / Server Cutover (next week)
- [ ] Bump app revision (minor tick) + commit + tag
  _Rule: bump rev before pushing to git._
- [ ] Push current stabilization build to git (main)
- [x] Server deployment: pull latest on server, install deps, restart service
- [x] Verify documents_root points to server storage (RAID later) and uploads work (claim + report)
- [x] Smoke test on server: create claim → create progress report → print/PDF → upload docs → invoice view  
  _partial; awaiting live data_
- [x] Gunicorn + systemd service installed and verified
- [ ] Start with clean production DB on server (wipe OK) + confirm schema matches current models
  _Gina will enter live data on server; we want a clean starting point._
- [ ] Verify documents_root points to server storage (RAID later) and uploads work (claim + report)
- [ ] Smoke test on server: create claim → create progress report → print/PDF → upload docs → invoice view
- [ ] Invoice “Save” persists date (date doesn’t reset)
- [ ] Invoice numbering format returns to INV-YY-### (e.g., INV-25-001)
- [ ] “Add all uninvoiced complete items” works
- [ ] “Delete draft invoice” works
- [ ] Gather billables by report DOS range works (doesn’t say “no items” when there are)  
  _Known issue: still saying none when they exist._

### B) Consistency: address + state dropdowns
- [ ] All State fields use the shared state list helper (no “random characters”)  
  _Test: Claim/Carrier/Employer/Provider + Settings “Business State”._
- [ ] Carrier: Address 1/2 present on New and Edit and ordered correctly  
  _Test: carrier new/edit show Name, Addr1, Addr2, City, State, Zip, Phone, Fax, Email, Rates._
- [ ] Employer: Address 1/2 present on New/Edit and ordered correctly  
  _Test: same ordering/labels as carrier._
- [ ] Provider: Address 1/2 present on New/Edit and ordered correctly  
  _Test: same ordering/labels as carrier._
- [ ] Detail summary boxes include Address 1/2 (Carrier/Employer/Provider)  
  _Status guess: Provider ✅ (seen), Carrier/Employer = verify._
- [ ] List tables include Address 1/2 columns (Carrier/Employer/Provider) and keep sorting  
  _Test: columns visible; sort still works._
- [ ] Rename labels: “Postal Code” → “ZIP Code” everywhere (if not already)  
  _Test: carrier/employer/provider new/edit/detail/list._

### D) Phone extension fields everywhere
- [x] Phone extension fields exist on Carrier/Employer/Provider (new/edit/detail/list where appropriate)  
  _Status guess: mostly done — verify._
- [ ] Claimant phone extension exists on Claim New/Edit and shows on Claim Detail summary  
  _Known issue: was missing from claim summary at one point → verify._
- [ ] Report print / report headers show extensions where phone numbers appear
- [ ] Invoice print/details show extensions where phone numbers appear

### I) Forms/Templates area
- [x] Fax cover sheet search works across contacts, claimants, employers, providers, carriers, with category + association  
  _Status guess: ✅ working now._
- [x] Fax cover sheet: remove address field  
  _Status guess: ✅ done, verify._
- [ ] Forms.py will get big → plan to split later (note only)

### J) “Tomorrow notes” (explicit parking lot)
- [ ] Finalize canonical Report field list + storage strategy before completing roll-forward logic  
  _Note: Treating providers + barriers roll-forward confirmed working; per-field text roll-forward still pending._
- [ ] Refactor core_data.py into smaller modules once stabilization backlog is clear
- [ ] Standardized delete behavior: clear FK references instead of forcing replacement
  _Applies to Contacts referenced by Claims._
- [ ] Remove email column from carriers view, employers view, providers view (you flagged this)
- [ ] Discuss/confirm extension strategy is “ext field everywhere” (we chose this)
- [ ] Keep refactor plan: split big route files as forms expand
- [ ] Live server update strategy: support wipe-at-will and/or migrate-at-will using Alembic baseline + repeatable seed/test data  
  _Goal: safe updates while Gina shadows; ability to reset DB during rollout without losing docs storage layout._
- [ ] Mobile billables entry page needs full verification and likely UX rework  
  _Deferred until Gina is actively entering billables on the server._
- [ ] Alembic baseline created on production DB (no-op migration)  
  _Rule: future schema changes must generate explicit migrations._
- [ ] Backup retention policy (e.g., prune DB backups >60 days)  
  _Low risk; implement later._

---

## Impact Medical CMS — Development Roadmap  
_Last updated: 2025‑02‑19_

This roadmap summarizes current system status, upcoming work, and long‑term features for the Impact Medical Consulting CMS. It is designed to guide structured development while keeping everything aligned with the project’s architecture and goals.

---

## ✅ Phase 1 — Core System Stabilization (Completed or In‑Progress)

### **1. Project Recovery & File Restoration**
- Reconstructed project structure from the uploaded ZIP.
- Verified key modules: `app/__init__.py`, `models.py`, `routes.py`, templates, static assets.
- Restored SQLAlchemy initialization patterns and ensured one `db` instance.

### **2. Database Model Accuracy**
- Rebuilt **Settings**, **Carrier**, **Employer**, **Provider**, and **Contact** models.
- Ensured all fields used by routes/templates exist on the models.
- Confirmed relationships:  
  - Providers → Contacts  
  - Claim → Employer, Carrier, Providers  
- Added missing columns (address fields, phone, fax, notes, claim metadata).

### **3. Functional CRUD**
- Claims: create, view, edit.  
- Billable Items: add/remove/edit with validation.  
- Invoices: generate from complete billables.  
- Documents: upload structure validated.

### **4. File Organization & Safety**
- Centralized document paths:  
  - `_get_claim_folder()`  
  - `_get_report_folder()`  
- Confirmed safe path joining and directory creation.

---

## 🔧 Phase 2 — Bug Fixes & Validation (Current Priority)

See 🔥 Current Stabilization Backlog for active defect tracking.

---

## 🚀 Phase 3 — UI/UX Improvements

### **1. Consistent Form Patterns**
Use standardized:
- Label/value pairs  
- Flash messages  
- Error handling  
- Required-field indicators (⭐)

### **2. Modern Admin UI Polish**
- Improve table readability.
- Enhance mobile-friendly layout.
- Add navigation breadcrumbs.

### **3. Full Reuse of Templates**
- Ensure `claim_new.html` and `claim_edit.html` use common partials.

---

## 📄 Phase 4 — Report Workflow System

### **1. Full-Screen Report Editor**
- Rich text editor (Quill or TinyMCE).
- Autosave drafts.
- Version history per report.

### **2. Multi-Step Report Generation**
- Draft → Review → Finalize → PDF generation.

### **3. Report Attachments**
- Upload & preview attached report documents.

---

## 📦 Phase 5 — Advanced Document Management

### **1. Encrypted Document Storage (HIPAA-oriented)**
- Optional at-rest encryption of documents.
- Hash-based filenames with a readable index.

### **2. File Retention & Audit Trail**
- Automatically log:
  - upload time  
  - user  
  - originating claim  

### **3. Expiration & Purge Policies**
- Configurable retention settings.

---

## 🧾 Phase 6 — Enterprise Billing & Finance

### **1. Invoice Enhancements**
- Multi-invoice per claim.
- Automatic invoice numbering.
- Apply payments + balance tracking.

### **2. Billable Rule Engine**
- Rate lookup per provider or specialty.
- Automatic travel billing logic.

---

## 🌐 Phase 7 — Cloud & Network Integration

### **1. Cloud Sync with On-Prem Storage**
- Local NAS sync service.
- Optional S3/Backblaze offsite mirror.

### **2. Multi-User Accounts**
- Admin, staff, read-only roles.

### **3. Detailed Access Control**
- Per-claim and per-document permissions.

---

## 🧠 Phase 8 — AI & Automation Features

### **1. Document Extraction**
- Auto-read PDFs and populate fields.
- Summaries for claims and reports.

### **2. Smart Suggestions**
- Suggested billable items.
- Missing data detection.

---

## 🏁 Final Notes
This roadmap stays flexible. As the system matures and Gina's workflow becomes more detailed, new phases may be added. Every major feature will maintain the guiding principles:
- Simplicity  
- Stability  
- Safety  
- Predictable behavior  
- Long-term maintainability  








## **Strategic / Long‑Term Roadmap**

## ✅ Phase 1 — Core System Stabilization (Completed or In‑Progress)

### **1. Project Recovery & File Restoration**
...
---
## 🧪 Testing Protocol
Work proceeds strictly top‑down through the Stabilization Backlog.
No new features are added until all unchecked items above are resolved or consciously deferred.