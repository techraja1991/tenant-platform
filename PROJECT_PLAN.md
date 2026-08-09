# Tenant Management Platform — Phase-Wise Development Plan & Tracker

> Central tracking document for all planned development work on the Tenant & Property Management Platform.
> **Legend:** ✅ Done · 🔄 In Progress · ⏳ Planned / Not started
> Each task is ticked off here as it is completed. Work log entries reference commits where applicable.

---

## Completed Work Log (historical)

| Date | Work done | Reference |
|---|---|---|
| 2026-08-09 | Created `tenant_payments` table (rent/deposit records) in DB + schema files | — |
| 2026-08-09 | Payments + deposits API (`GET/POST/DELETE /api/payments`, `GET /api/deposits`) with auto ledger `jama`/`payment_status` sync | `gui_api.py` |
| 2026-08-09 | Frontend "Tenant Payments" tab: payments table w/ month filter + delete, deposit status table, record-payment modal | `static/index.html` |
| 2026-08-09 | HTTPS + Basic Auth on port 443 via systemd service `tenant-gui.service` (self-signed cert, auto-start, auto-restart) | `/etc/systemd/system/tenant-gui.service` |
| 2026-08-09 | Centralized all DB config into `.env` + `db_config.py`; removed hardcoded credentials from every script; `db_config.get_db_params()` now requires all vars from `.env` | commit `f10128c` |
| 2026-08-09 | Archived unused/experimental scripts + seed data files to `archive/`; kept only production files in root | commit `f10128c` |
| 2026-08-09 | Added `.gitignore` (`.env`, `venv/`, `certs/`, `*.log`, `*.csv`, `__pycache__/`); untracked `venv/` (5868 files), CSVs and logs accidentally committed before | commit `f10128c` |
| 2026-08-09 | **Phase 0.6–0.9 schema hardening**: 17 new indexes, dropped duplicate unique on `utility_metrics.service_number`, added `created_at` to `monthly_ledgers`, added 14 CHECK constraints (non-negative money, `YYYY-MM` month format, `is_active` Y/N); normalized 51 legacy `Jun-26` ledgers → `2026-06`; added input validation to all API create models; schema files regenerated from live DB (identical) | `migrations/001_phase0_schema_hardening.sql` |

---

## Phase 0 — Foundation & Hygiene

Goal: stable, clean, reproducible base. Mostly done; remaining items are scheduling + schema hardening.

| # | Sub-task | Details | Status |
|---|---|---|---|
| 0.1 | `.env` for all DB config | All DB connection params live in `.env` only; scripts read via `db_config.py` | ✅ Done |
| 0.2 | Remove static/hardcoded credentials from all scripts | Every script uses `db_config.get_db_connection()`; no passwords in code | ✅ Done |
| 0.3 | Archive unused scripts & data | Moved experimental/one-off scripts + token CSVs + logs to `archive/` | ✅ Done |
| 0.4 | Git hygiene | `.gitignore`, untracked `venv`/CSVs/logs, clean baseline commit | ✅ Done |
| 0.5 | Automate monthly pipeline via systemd timers | Create `tenant-pipeline.timer` to run: electricity scraper → gas import → allocate bills on the 1st of each month (configurable day/time), with combined logging and failure notification | ⏳ Planned |
| 0.6 | Add DB indexes on hot FK columns | Index `tenants.room_id`, `tenants.is_active`, `monthly_ledgers.billing_month` + `.room_id`, `tenant_payments.billing_month`/`tenant_id`/`room_id`, `property_maintenance_expenses.execution_month`/`room_id`, `gas_bill_master.execution_month`, `torrent_bill_master.execution_month`, `gas_metrics.room_id` (+ `rooms.master_id`, `tenant_ledgers`, `utility_metrics`) | ✅ Done |
| 0.7 | Remove duplicate unique index on `utility_metrics.service_number` | Drop redundant `utility_metrics_service_number_key`, keep one unique index | ✅ Done |
| 0.8 | Add `created_at` to `monthly_ledgers` | Audit trail for when a month's ledger was generated/updated | ✅ Done |
| 0.9 | Add CHECK constraints for money/date/month formats | Non-negative amounts, `billing_month`/`execution_month` format `YYYY-MM`, `is_active IN ('Y','N')`; normalized 51 legacy `Jun-26` ledgers → `2026-06` | ✅ Done |

---

## Phase 1 — Core Operational UI

Goal: full day-to-day operations from the browser; correct, reconcilable records.

| # | Sub-task | Details | Status |
|---|---|---|---|
| 1.1 | Ledger month selector | `GET /api/ledgers?billing_month={YYYY-MM}` (query param, defaults current), `GET /api/ledgers/months` (distinct months), month dropdown in the Monthly Ledgers tab; kept `/api/ledgers/current` alias | ✅ Done |
| 1.2 | Ledger editing | `PUT /api/ledgers/{id}` to edit `jama`, `jama_date`, `payment_status`; validated against `tenant_payments` — if RENT payments exist for the tenant+month, `jama` must equal their total (auto-corrects to it, rejects mismatches), so ledger and payments never diverge; `payment_status` auto-derived from `month_total` vs `jama` unless explicitly set | ✅ Done |
| 1.3 | Payment reconciliation refinements | `POST /api/payments` now accepts an `allocations: [{billing_month, amount}]` list — one payment splits across multiple months (shared `batch_id`, one row per month); advance payments create a placeholder ledger (carrying `prev_baki` forward) that is auto-removed when the last payment in the batch is deleted; `Paid/Partial/Unpaid` auto-recomputed from payment sums via `_sync_ledger_jama`; deleting any row of a batch deletes the whole batch. Migration `002_phase1_payment_batch.sql` adds `tenant_payments.batch_id` | ✅ Done |
| 1.4 | Enriched dashboard | Cards/trends: monthly rent income (sum `bhadu_applied`), collections (sum `jama`), recovery %, vacancy rate (empty rooms / total), outstanding by property, carried-forward + older-than-current outstanding aging | ✅ Done |
| 1.5 | Properties & rooms management UI | Properties tab added: list properties with `room_count`/`occupied_rooms`, add property, `PUT /api/properties/{id}` to edit defaults (rent, maintenance, deposit, common utility); rooms list with occupant + add/edit via `PUT /api/rooms/{id}` | ✅ Done |
| 1.6 | Tenants full CRUD | `PUT /api/tenants/{id}` now optional-fields with partial update: name, room, phone, rent, maintenance, deposit, `is_active`, `lease_start_date`/`lease_end_date` ('' clears); edit modal wired into Tenants table | ✅ Done |
| 1.7 | Expense lifecycle | `GET /api/expenses?execution_month=`, `GET /api/expenses/months`, `GET /api/expenses/totals` (month-over-month sums), `PUT /api/expenses/{id}`, `DELETE /api/expenses/{id}`; expenses tab has month selector, edit/delete buttons and monthly totals table | ✅ Done |
| 1.8 | Bill allocation from UI | `allocate_tenant_bills.py` wraps each tenant in a SAVEPOINT, tracks per-tenant success/failure, and prints `ALLOC_REPORT <json>`; `POST /api/operations/allocate-bills` captures it and returns `{total, success, failures[]}`; UI shows an allocation report modal with per-tenant failure reasons. Subprocesses now use `sys.executable` (venv) instead of bare `python3` | ✅ Done |

---

## Phase 2 — Tenant Lifecycle

Goal: manage move-in / move-out, deposits and leases properly.

| # | Sub-task | Details | Status |
|---|---|---|---|
| 2.1 | Move-in workflow | On tenant creation, generate prorated first-month ledger; auto-assign room; set lease dates | ⏳ Planned |
| 2.2 | Move-out workflow | Checkout wizard: settle outstanding, compute deposit refund (deposit received − deductions), mark room vacant, set `is_active='N'`, finalize last ledger | ⏳ Planned |
| 2.3 | Deposit ledger | Track deposit received (already partially via `/api/deposits`), refunds, and deductions per tenant with history | ⏳ Planned |
| 2.4 | Vacancy report | List vacant rooms per property with days-vacant and lost-rent estimate | ⏳ Planned |
| 2.5 | Lease expiry alerts | Dashboard alert for leases ending within 30/60/90 days | ⏳ Planned |

---

## Phase 3 — Reporting & Communication

Goal: shareable outputs and automatic reminders.

| # | Sub-task | Details | Status |
|---|---|---|---|
| 3.1 | PDF invoice/receipt per tenant | Generate monthly bill (rent + utilities breakdown) and payment receipt as PDF, downloadable from UI | ⏳ Planned |
| 3.2 | Excel export | Export ledgers, payments, and outstanding per property/month to `.xlsx` | ⏳ Planned |
| 3.3 | WhatsApp payment reminders | Automate reminder messages to `tenants.phone_number` with bill summary; track sent status | ⏳ Planned |
| 3.4 | Collection reports | Monthly cash-flow report: billed vs collected vs outstanding per month and property | ⏳ Planned |
| 3.5 | Dashboard charts | Collections trend (6–12 months), outstanding aging pie/bar, occupancy over time | ⏳ Planned |

---

## Phase 4 — Security & Multi-User

Goal: proper access control and auditability.

| # | Sub-task | Details | Status |
|---|---|---|---|
| 4.1 | Real user accounts + roles | Users table (admin / read-only), session-based login page (replaces basic-auth), password hashing | ⏳ Planned |
| 4.2 | Role-based API protection | Read-only role cannot POST/PUT/DELETE; enforce per-endpoint | ⏳ Planned |
| 4.3 | Audit log | `audit_log` table recording who changed what (tenant/payment/ledger) and when | ⏳ Planned |
| 4.4 | Trusted TLS certificate | Replace self-signed cert with Let's Encrypt via nginx reverse proxy (no browser warning) | ⏳ Planned |
| 4.5 | Rate limiting & CSRF | Basic rate limiting on auth endpoints; CSRF protection for state-changing requests | ⏳ Planned |
| 4.6 | Secret handling review | Move Torrent API tokens/keys out of plaintext DB columns (`torrent_accounts.password`, `access_token`) into encrypted storage | ⏳ Planned |

---

## Phase 5 — Reliability & Ops

Goal: safe, monitored, recoverable system.

| # | Sub-task | Details | Status |
|---|---|---|---|
| 5.1 | Automated end-to-end pipeline test | Script that runs scraper → gas → allocate against a staging DB and asserts idempotency + expected totals | ⏳ Planned |
| 5.2 | Database backups | `pg_dump` cron/systemd timer (daily) with retention (keep last 14), plus a tested restore procedure | ⏳ Planned |
| 5.3 | Healthcheck endpoint + monitoring | `/api/health` (DB ping, last pipeline run time); uptime alerting (e.g. cron + email/webhook) | ⏳ Planned |
| 5.4 | Log rotation | Rotate `automation_engine.log`, `gui.log` (logrotate config) to prevent disk fill | ⏳ Planned |
| 5.5 | Documentation | `README.md` covering setup (`.env`, venv, systemd), pipeline run order, and archive/ contents | ⏳ Planned |

---

## Current Focus

**Phase 0 (finishing):** item 0.5 only — pipeline scheduling automation (systemd timer). Schema hardening (0.6–0.9) is done.
**Phase 1 (complete):** all of items 1.1–1.8 are done. Phase 1 is fully delivered.
**Phase 2 (next):** tenant lifecycle — move-in/move-out workflows (2.1, 2.2), deposit ledger (2.3), vacancy report (2.4), lease expiry alerts (2.5).
