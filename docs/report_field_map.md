

# Canonical Report Field Map

This document is the **source of truth** for Report fields and behavior in Impact Medical CMS.

It is intentionally **not** the same as the database schema export:
- **Schema/models** = what is stored.
- **Field map** = what the user sees + how workflows behave (defaults, roll-forward, PDF visibility, etc.).

## Conventions

- **Storage**: where the value should live long-term.
  - `Report` = stored on a specific report instance.
  - `Claim` = stored on the claim (persists across reports).
  - `Derived` = computed/display-only.
- **Applies to**: which report types show the field (`Initial`, `Progress`, `Closure`).
- **Roll-forward**:
  - `Yes` = “Roll Forward” button allowed for this field.
  - `No` = never roll-forward automatically.
  - `N/A` = not a user-entered field.
- **Defaults on New Report**: what happens when creating a new report from Claim Detail.

---

## Report header (read-only on edit screens)

These are displayed in report headers and print/PDF views (pulled from Claim + related entities).

| Field (UI label) | Storage | Applies to | Roll-forward | Defaults on New Report | Notes |
|---|---|---:|---:|---|---|
| Claimant | Claim (derived from Claim record) | All | N/A | N/A | Display only |
| Claim Number | Claim | All | N/A | N/A | Display only |
| DOB | Claim | All | N/A | N/A | Display only |
| DOI | Claim | All | N/A | N/A | Display only |
| Claim State | Claim | All | N/A | N/A | Display only |
| Carrier | Claim (carrier_id) | All | N/A | N/A | Display only |
| Carrier Contact | Claim (carrier_contact_id) | All | N/A | N/A | Display only |
| Employer | Claim (employer_id) | All | N/A | N/A | Display only |
| Referral Date | Claim (referral_date) | All | N/A | N/A | Display only |
| Injured Body Part | Claim | All | N/A | N/A | Display only |

> Note: PCP must **not** appear in headers.

---

## Shared Report fields (appear across report types)

| Field (UI label) | Storage | Applies to | Roll-forward | Defaults on New Report | Notes |
|---|---|---:|---:|---|---|
| Report Type | Report (`report_type`) | All | N/A | Selected from Claim Detail dropdown | initial/progress/closure |
| DOS Start | Report (`dos_start`) | All | No | Initial: Claim Referral Date → Today; Progress/Closure: day after last report DOS End → Today (fallback = today) | Never roll-forward |
| DOS End | Report (`dos_end`) | All | No | Today | Never roll-forward |
| Next Report Due | Report (`next_report_due`) | Initial, Progress | No | (optional) | Closure typically N/A |
| Treating Provider (single) | Report (`treating_provider_id`) | All | No | (optional) | This is the “primary treating provider” dropdown |
| Possible Barriers to Recovery | Report (`barriers_json`) | All | Yes | Copy from most recent prior report on same claim | Stored as JSON list of BarrierOption IDs |
| Status / Treatment Plan | Report (`status_treatment_plan`) | All | Yes | Blank | Long text |
| Work Status | Report (`work_status`) | All | Yes | Blank | Long text |
| Case Management Plan | Report (`case_management_plan`) | Initial, Progress | Yes | Blank | Long text |
| Employment Status | Report (`employment_status`) | Initial | Yes | Blank | Ensure shows/prints |

---

## Initial Report fields

| Field (UI label) | Storage | Applies to | Roll-forward | Defaults on New Report | Notes |
|---|---|---:|---:|---|---|
| Primary Care Provider / Family Doctor | Report (`primary_care_provider`) | Initial | Yes | Blank | **Initial-only**, free-text single line |
| Diagnosis | Report (`initial_diagnosis`) | Initial | Yes | Blank | Long text |
| Mechanism of Injury | Report (`initial_mechanism_of_injury`) | Initial | Yes | Blank | Long text |
| Concurrent Conditions | Report (`initial_coexisting_conditions`) | Initial | Yes | Blank | (renamed from co-existing) |
| Surgical History | Report (`initial_surgical_history`) | Initial | Yes | Blank | Long text |
| Medications | Report (`initial_medications`) | Initial | Yes | Blank | Long text |
| Diagnostics | Report (`initial_diagnostics`) | Initial | Yes | Blank | Long text |
| Next Appointment (datetime) | Report (`initial_next_appt_datetime`) | Initial | No | Blank | Drives ICS generation |
| Next Appointment Provider Name | Report (`initial_next_appt_provider_name`) | Initial | No | Blank | Used in ICS summary |
| Next Appointment Notes | Report (planned) | Initial, Progress | Yes | Blank | Allows “pending scheduling” etc. |

---

## Progress Report fields

| Field (UI label) | Storage | Applies to | Roll-forward | Defaults on New Report | Notes |
|---|---|---:|---:|---|---|
| (Uses only Shared fields) | — | Progress | — | — | Progress screen is shared fields + Next Report Due |

---

## Closure Report fields

| Field (UI label) | Storage | Applies to | Roll-forward | Defaults on New Report | Notes |
|---|---|---:|---:|---|---|
| Reason for Closure | Report (`closure_reason`) | Closure | No | Blank | Enum options |
| Closure Details | Report (`closure_details`) | Closure | Yes | Blank | Long text |
| Case Management Impact | Report (`closure_case_management_impact`) | Closure | Yes | Blank | Long text |

Closure side effects:
- Creating a Closure Report sets Claim status to **closed** (but claim must be reopenable).

---

## Treating Providers (multi-select)

This is the checkbox list of providers associated to the report (separate from the single treating provider dropdown).

| Field (UI label) | Storage | Applies to | Roll-forward | Defaults on New Report | Notes |
|---|---|---:|---:|---|---|
| Treating Providers (checkbox list) | Join table `report_approved_provider` | All | Yes | Copy from most recent prior report on same claim | Preserve sort order |

---

## Billable automation (report creation)

When creating a report, auto-create a BillableItem for report-writing time:
- Initial = **1.0 hours**
- Progress = **0.5 hours**
- Closure = **0.5 hours**

Invoicing behavior note:
- From a Report, “Gather billable items” should include items whose **service_date** falls within the report’s DOS range.

---

## Planned / pending items (do not implement without updating this map)

- Surgery Date: recommended at **Claim** level so it persists and prints on future reports.
- Date overlap checks: DOS ranges should not overlap prior report DOS ranges; new report DOS start should be auto-derived.
- Roll-forward buttons: implement for all `Roll-forward = Yes` long-text fields consistently.
