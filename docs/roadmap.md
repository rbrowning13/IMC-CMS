# Project Roadmap

## 🔥 Current Stabilization Backlog (Authoritative Checklist)
_This is the single source of truth for active bugs, regressions, and polish items.  
Items here supersede older phase notes until checked off._

### A) Data safety and validation polish
- [ ] Global phone/fax input masking across the entire app (auto ( ) -)  
  _Test: type digits in every phone/fax field; confirm formatting + doesn’t fight cursor._
- [ ] Phone/fax validation: allow blank; if not blank require correct digit count  
  _Test: blank saves; 9 digits rejects; 10 digits saves._
- [ ] Email validation: allow blank; if not blank must be valid  
  _Test: blank saves; x@ rejects; x@y.com saves._
- [ ] ZIP validation: allow blank; if not blank must be 5 or 5+4  
  _Test: blank saves; 1234 rejects; 12345 saves; 12345-6789 saves._
- [ ] No data loss on validation errors (forms re-render with typed data intact)  
  _Test: intentionally fail validation and confirm fields retain values exactly._

### H) Deletes / referential integrity
- [ ] Delete Claim succeeds without FK crashes (report_approved_provider, etc.)  
  _Known issue: FK violation from report_approved_provider when deleting reports via bulk delete._
- [ ] Delete Provider/Employer/Carrier deletes related contacts safely (or blocks with clear message)

### C) Contacts: CRUD + roles (per parent type)
#### Carrier contacts
- [ ] Edit loads existing contact into form
- [ ] Edit updates record (doesn’t create new)
- [ ] Role/Title dropdown persists + reloads on edit  
  _Status guess: Carrier ✅ working now, but verify._
#### Employer contacts
- [ ] Edit loads existing contact into form
- [ ] Edit updates record (doesn’t create new)
- [ ] Role/Title dropdown persists + reloads on edit  
  _Status guess: fixed “twice” — verify._
#### Provider contacts
- [ ] Edit loads existing contact into form
- [ ] Edit updates record (doesn’t create new)
- [ ] Role/Title dropdown persists + reloads on edit  
  _Status guess: ✅ working now, verify._
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
- [ ] Report fields save reliably (esp. Initial “Primary Care Provider / Family Doctor”)  
  _Known issue: PCP wasn’t saving at one point → verify._
- [ ] Report edit screen not spamming status updates / refresh loops  
  _Test: open report edit and watch top banner behavior._
- [ ] Roll-forward per-field works (shared long text fields)  
  _Test: click roll-forward on a field with a previous report._
- [ ] ICS download works for Next Appointment  
  _Note: previously flagged to fix; verify current behavior._

### G) Invoices
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
- [ ] Phone extension fields exist on Carrier/Employer/Provider (new/edit/detail/list where appropriate)  
  _Status guess: mostly done — verify._
- [ ] Claimant phone extension exists on Claim New/Edit and shows on Claim Detail summary  
  _Known issue: was missing from claim summary at one point → verify._
- [ ] Report print / report headers show extensions where phone numbers appear
- [ ] Invoice print/details show extensions where phone numbers appear

### I) Forms/Templates area
- [ ] Fax cover sheet search works across contacts, claimants, employers, providers, carriers, with category + association  
  _Status guess: ✅ working now._
- [ ] Fax cover sheet: remove address field  
  _Status guess: ✅ done, verify._
- [ ] Forms.py will get big → plan to split later (note only)

### J) “Tomorrow notes” (explicit parking lot)
- [ ] Remove email column from carriers view, employers view, providers view (you flagged this)
- [ ] Discuss/confirm extension strategy is “ext field everywhere” (we chose this)
- [ ] Keep refactor plan: split big route files as forms expand

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