# Project Roadmap

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

### **1. Model/Route Alignment Audit**
- Verify **all** create/edit forms submit fields matching SQLAlchemy models.
- Ensure no routes pass invalid keyword args (e.g., `"notes"` before field existed).

### **2. SQLAlchemy App Registration Stability**
- Finalize and lock down:
  - Single `db = SQLAlchemy()` in `app/__init__.py`
  - `db.init_app(app)` inside `create_app()`
  - Remove all circular imports

### **3. Contact Management**
- Ensure Contact model includes:
  - `first_name`, `last_name`, `phone`, `email`, `notes`
- Fix routing for provider/employer/carrier contacts.

### **4. Claim Summary View**
- Restore missing fields to summary box:
  - Address  
  - Phone  
  - Email  
  - Primary care  
  - Claim state  

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
