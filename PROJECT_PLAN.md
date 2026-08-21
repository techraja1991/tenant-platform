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
| 2026-08-12 | **Postpaid billing model — torrent engine**: `torrent_automation_engine.py` now stores fetched bills under the CONSUMPTION month (fetch month − 1), and parses/stores torrent `LastBill`/`LastBillAmt`/`LastBilledUnits` (display-only, never used in calcs). Migration `003` adds the three display columns. Pure-function unit tests added (`test_engine_postpaid.py`, 21 passing) that never hit the live API | Phase 6 |
| 2026-08-12 | **Re-attributed existing fetched bills**: exec `2026-07`→`2026-06`, `2026-08`→`2026-07` (July-fetched = June consumption, Aug-fetched = July consumption); June/July ledgers recompiled in target order; August ledgers removed (no August bill until September's run). Live data snapshots in `backups/` | Phase 6 |
| 2026-08-12 | **Ledger generation + GUI shifted to billing period**: `allocate_tenant_bills.py` and `import_gas_metrics.py` default to the consumption month; `gui_api.py` `current_billing_period()` drives ledgers/payments/expenses/dashboard defaults; months without a ledger show `Not Billed` | Phase 6 |
| 2026-08-12 | **Customer View payment dates show month**: `year_ledger` returns payment dates grouped by actual payment month, e.g. `Aug-26(04,11)` | Phase 6 |
| 2026-08-18 | **WhatsApp reminder templates + manual send + auto sender**: `message_templates` master (active/inactive/default), per-tenant template mapping (`tenant_message_templates`), `whatsapp_send_log` audit; manual wa.me buttons on Customer View + Monthly Ledger view; `send_whatsapp_reminders.py` Playwright auto-sender with persistent session, anti-detection fingerprint, human typing + randomised 60–180s inter-message delays; dashboard trigger + send-log viewer | Phase 7 |
| 2026-08-19 | **Responsive GUI overhaul**: rebuilt as a single-file Alpine.js + Tailwind app (`static/index.html`); old GUI backed up to `static/index_v1.html`; removed all `sudo` calls from app code; all features re-verified with Playwright at 390px/1440px (no JS errors, no horizontal overflow) | `static/index.html` |
| 2026-08-19 | **Ledger view enhancements**: `property_id` + `apartment_name` added to `get_ledgers()` (join `property_masters`); search / property filter / sort (outstanding ↑↓, paid ↑↓, address natural sort — G1/G2 before 101/201) applied to mobile cards + desktop table; mobile filter bar stacked full-width; full hisab detail on mobile cards; "Customer" button per row opens that tenant's Customer View; Detailed/Summary toggle | `static/index.html`, `gui_api.py` |
| 2026-08-19 | **Customer year-ledger header frozen on mobile**: scroll container `max-h-[60vh]` + sticky `thead`/`th` (`border-collapse: separate`), so the month columns stay visible while scrolling on phones | `static/index.html` |
| 2026-08-20 | **Dashboard vacancy card detail + one-click occupy**: `GET /api/dashboard/vacancy?upcoming_days=120` returns vacant rooms grouped by property (with unit list) + leases expiring in the window (NOT counted as vacant); modal lists vacant units; each unit chip opens the Add-Tenant modal pre-filled with `room_id`, property defaults and lease start = today | `static/index.html`, `gui_api.py` |
| 2026-08-20 | **Clickable KPI cards + collection progress**: every KPI card opens a detail modal backed by `GET /api/dashboard/kpi` (per-property occupancy/billed/collected/outstanding, tenant directory with active status, per-tenant ledger breakdown); dashboard gains a Collection Progress section (progress bar collected vs total fixed due + Total Fixed Due / Collected / Current Due cards); `stats` now returns `fixed_due_total` (Σ `gross_baki`) and `current_due_total` (Σ positive `new_baki`) | `static/index.html`, `gui_api.py` |
| 2026-08-20 | **KPI modal semantics split**: Outstanding Dues shows `new_baki` + "Carried fwd vs Current month" split (`prev_baki` vs `new_baki−prev_baki`) + `payment_status` badge; Monthly Rent Billed shows bill composition (Rent/Light/Gas/Maint/Misc → Month Total, sorted by month total desc) — the two cards now show distinct, meaningful detail | `static/index.html`, `gui_api.py` |

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

## Phase 6 — Postpaid Billing Model

Goal: correctly model torrent/gas bills as POSTPAID — a bill fetched this month represents the PREVIOUS month's consumption. All ingestion, ledger generation, and UI defaults must target the consumption month.

| # | Sub-task | Details | Status |
|---|---|---|---|
| 6.1 | Engine stores bill under consumption month | `torrent_automation_engine.py` attributes fetched bills to `execution_month = fetch_month − 1`; stores `LastBill`/`LastBillAmt`/`LastBilledUnits` (display-only) via migration `003_postpaid_bill_metadata.sql`; pure-function units in `test_engine_postpaid.py` (21 passing, no live API) | ✅ Done |
| 6.2 | Re-attribute existing fetched bills | `2026-07`→`2026-06`, `2026-08`→`2026-07`; June/July ledgers recompiled; live-data snapshots in `backups/` | ✅ Done |
| 6.3 | Ledger generation defaults to consumption month | `allocate_tenant_bills.py` defaults to previous month (billing period) unless `--month` given; connect/disconnect/package charges still booked on current ledger | ✅ Done |
| 6.4 | Remove bogus August ledgers | No August bill exists until September's run; August ledgers deleted so they don't show as Unpaid | ✅ Done |
| 6.5 | Gas import uses consumption month | `import_gas_metrics.py` future imports default to previous month; existing 4 gas bills left at `2026-07` | ✅ Done |
| 6.6 | GUI + customer view follow billing period | `gui_api.py` `current_billing_period()` drives ledgers/payments/expenses defaults; months w/o ledger show `Not Billed` instead of Unpaid; payment dates grouped per month (e.g. `Aug-26(04,11)`; multiple months joined `Jul-26(12), Aug-26(04)`) | ✅ Done |

---

## Phase 7 — WhatsApp Reminders

Goal: send tenants their hisab as a WhatsApp reminder — manually (zero ban risk, wa.me deep link) and automated (Playwright WhatsApp Web with human-like behaviour). Templates are master-managed with per-tenant overrides and a default-for-all fallback.

| # | Sub-task | Details | Status |
|---|---|---|---|
| 7.1 | Template master module | `message_templates` table (name/type/body/note/signature/active/default); single-default enforced via trigger; CRUD API `GET/POST/PUT/DELETE /api/templates`; frontend Templates tab | ✅ Done |
| 7.2 | Per-tenant template mapping | `tenant_message_templates` mapping table; `PUT /api/templates/mappings/{tenant_id}` (template_id clears); mapping modal in UI; default template used when no mapping | ✅ Done |
| 7.3 | Hisab message renderer | `whatsapp_message.py` fills placeholders (`{name} {address} {month} {prev_month} {prev_baki} {prev_jama} {bhadu} {light_bill} {gas_bill} {maintenance} {misc} {month_total} {gross_baki} {jama} {jama_date} {new_baki}`) from the ledger; `GET /api/tenants/{id}/whatsapp-message?billing_month=` returns phone + `wa.me` link | ✅ Done |
| 7.4 | Manual WhatsApp buttons | Customer View header + each Monthly Ledger row opens `https://wa.me/91…?text=<hisab>` prefilled, ready to send — safe, manual | ✅ Done |
| 7.5 | Automated reminder sender | `send_whatsapp_reminders.py`: persistent Chrome profile (`--login` scans QR once, image served at `/static/whatsapp_qr.png`), anti-detection fingerprint (real UA, no `webdriver`, locale/timezone), character-by-character typing, randomised 60–180s delays between messages, dry-run mode, logs every send to `whatsapp_send_log` | ✅ Done |
| 7.6 | Dashboard trigger + audit | `POST /api/operations/send-whatsapp-reminders` runs sender in background; `GET /api/whatsapp/send-log` + `/last-batch`; dashboard "Send WhatsApp Reminders" button + Send Log modal; targets active tenants of the billing period with `new_baki > 0` | ✅ Done |
| 7.7 | One-time WhatsApp Web login | On first auto-send, run `python send_whatsapp_reminders.py --login` under `xvfb-run`, scan QR from `/static/whatsapp_qr.png`; session persists in `whatsapp_profile/` | ✅ Done |
| 7.8 | Paste-based sending (single message) | `--mode paste` (default) inserts the whole multi-line hisab via real clipboard Ctrl+V — never triggers WhatsApp's Enter auto-send, so one message not per-line; `--mode type` kept as fallback; updated send-button selector for new WhatsApp DOM (`button[aria-label="Send"]`); clears leftover drafts before typing | ✅ Done |
| 7.9 | Test-number verification mode | `--test-number 919033127964` routes rendered messages to a dummy number; verified NOTIFY/D1/DUE/HISAB delivery, time gaps between messages, and single-message multi-line formatting | ✅ Done |

---

## Phase 8 — Next-Due-Date Reminders (per billing month)

Goal: track the date a tenant promises to clear their due for a **specific billing month**, view it month-wise, and auto-send WhatsApp reminders (set-confirmation, day-before, on-due-date) at an admin-configured master send time.

| # | Sub-task | Details | Status |
|---|---|---|---|
| 8.1 | Reminder schema | `tenant_reminders` (tenant_id, billing_month, reminder_date, amount_due, note, status ACTIVE/DONE/CANCELLED/SUPERSEDED, notify/d1/due sent_at stamps) + `reminder_send_log` audit; multiple dates per tenant+month allowed — setting a new date flips older ACTIVE rows to SUPERSEDED; date is scoped to one billing_month only | ✅ Done |
| 8.2 | Set / reschedule API | `POST /api/reminders` (set, auto-fills amount from month's `new_baki`), `PUT /api/reminders/{id}` (reschedule, keeps one row), `DELETE /api/reminders/{id}`; each set/reschedule queues a NOTIFY message "Your New Next Due Date is : DD-MM-YYYY" + amount | ✅ Done |
| 8.3 | Month-wise view | `GET /api/reminders?billing_month=` (all rows, ACTIVE first) + `GET /api/reminders/tenants?billing_month=` (eligible tenants with outstanding + current active date); frontend "Due Dates" tab with month selector, status badges, sent-at columns, edit/delete, Send Log modal | ✅ Done |
| 8.4 | Auto-send sender | `send_due_reminders.py`: sends queued NOTIFY (set confirmation), D1 (day before), DUE (on date short reminder **+ full template hisab message**); stamps sent_at so nothing sends twice; skips a tenant when the month's outstanding is already 0 (paid before due date) | ✅ Done |
| 8.5 | Master send time (admin-configurable) | `app_settings` table stores `reminder_send_time` (default `10:00`) + `reminder_send_enabled`; `GET/PUT /api/reminders/schedule`; Due Dates tab has an inline time picker + enable toggle + Save | ✅ Done |
| 8.6 | Daily scheduling | `due_reminder_trigger.py` dispatcher reads the master time from DB and only invokes the Playwright sender inside the 5-minute window; systemd timer `tenant-due-reminders.timer` runs every 5 min (service `tenant-due-reminders.service`); admin changes time/enabled purely from GUI | ✅ Done |

---

## Phase 9 — Executive Dashboard & Ledger UX (GUI overhaul)

Goal: a fast, mobile-first operational UI with an at-a-glance executive dashboard whose KPIs are all drill-down-able, and a fully searchable/filterable/sortable ledger view.

| # | Sub-task | Details | Status |
|---|---|---|---|
| 9.1 | Responsive single-file GUI | Rebuilt `static/index.html` as Alpine.js + Tailwind single file; old GUI kept at `static/index_v1.html`; no `sudo` in app code; verified mobile (390px) + desktop (1440px) with Playwright — no JS errors, no horizontal overflow | ✅ Done |
| 9.2 | Ledger search / filter / sort | `get_ledgers()` returns `property_id`+`apartment_name`; frontend computed search + property filter + sort (outstanding ↑↓, paid ↑↓, address natural sort — `G1/G2` before `101/201`); applied to both mobile cards and desktop table | ✅ Done |
| 9.3 | Mobile ledger cards | Full hisab shown directly on cards (Prev Net, Rent/Bhadu, Light, Gas, Maint, Misc, Fixed Total Baki, Paid, Current Baki, status, actions); Detailed/Summary toggle; filter bar stacked full-width | ✅ Done |
| 9.4 | Ledger → Customer View | "Customer" button per ledger row opens that tenant's Customer View; customer year-ledger header frozen on mobile (sticky thead) | ✅ Done |
| 9.5 | Vacancy card drill-down + occupy | `GET /api/dashboard/vacancy?upcoming_days=120`: vacant grouped by property + lease-expiring upcoming (excluded from vacant count); modal lists units; one-click Occupy pre-fills Add-Tenant modal (room_id, property defaults, lease start today) | ✅ Done |
| 9.6 | Clickable KPI cards | All 4 stat cards + 3 money-metric cards open detail modals from `GET /api/dashboard/kpi`: properties overview, tenant directory, outstanding dues, rent billed breakdown, collections breakdown, recovery by property | ✅ Done |
| 9.7 | Collection progress on dashboard | Progress bar + % (collected vs `fixed_due_total`); Total Fixed Due / Collected / Current Due cards; `stats` returns `fixed_due_total` (Σ `gross_baki`) + `current_due_total` (Σ positive `new_baki`) | ✅ Done |
| 9.8 | Distinct KPI semantics | Outstanding Dues = `new_baki` + carried-fwd/current split + status badge; Monthly Rent Billed = bill composition (Rent/Light/Gas/Maint/Misc → Month Total) — cards no longer show identical data | ✅ Done |

---

## Current Focus

**Phase 0 (finishing):** item 0.5 only — pipeline scheduling automation (systemd timer). Schema hardening (0.6–0.9) is done.
**Phase 1 (complete):** all of items 1.1–1.8 are done. Phase 1 is fully delivered.
**Phase 2 (next):** tenant lifecycle — move-in/move-out workflows (2.1, 2.2), deposit ledger (2.3), vacancy report (2.4), lease expiry alerts (2.5).
**Phase 6 (complete):** postpaid billing model delivered — engine + gas import both attribute bills to the consumption month, GUI/customer-view follow the billing period, bogus August ledgers removed. Verify at next run (September 2026) that `2026-08` bills arrive and ledger correctly.
**Phase 7 (delivered, one manual step):** WhatsApp reminders built — templates, mappings, manual buttons and the Playwright auto-sender are live. Before first automated run, complete one-time login: `xvfb-run python send_whatsapp_reminders.py --login`, scan `/static/whatsapp_qr.png`, then the dashboard button sends reminders automatically.
**Phase 8 (delivered):** per-month next-due-date reminders live — set dates from the Due Dates tab, history per month with supersede, auto-send (NOTIFY / D-1 / DUE+hisab) at the admin-set master time via the 5-minute systemd dispatcher, and no send when the month's balance is already paid. End-to-end verified to the dummy number 919033127964.
**Phase 9 (delivered):** executive dashboard + ledger UX overhaul complete — all KPIs are drill-down-able, ledger is search/filter/sort-able, vacancy has one-click occupy, and the dashboard shows live collection progress. Verify with real payment data as August 2026 progresses.
