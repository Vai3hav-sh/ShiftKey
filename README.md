# ShiftKey

Drop the daily **Daily Performance Report** exported from the Starlink attendance
software (v12.2.1727) into a web page and get back:

* the six‑column formatted sheet you build by hand today (`21 October 2026 Expatriates.xlsx` layout),
* half days (`HLF`) and mispunches (`MIS`) pulled out into their own lists and sheets,
* "needs review" rows – a `P` record spanning 16 h+ is almost always a missed punch that the
  machine paired with the next day's punch, and rows with only one punch,
* a mispunch **resolution workflow** for department heads, with a full audit trail,
* role‑based access: one admin, HOD accounts per department, HR accounts per staff segment,
* every day stored in PostgreSQL so you can look back, and so the same tables can later be
  filled straight from the Starlink database instead of an Excel upload.

## Run it

```bash
cp .env.example .env            # optional – set SHIFTKEY_ADMIN_PASSWORD if you want to pick it
docker compose up -d --build
docker compose logs app | grep -A3 "admin account"   # only if you left the password empty
open http://localhost:8080
```

Two containers start: `shiftkey-app` (FastAPI + web UI) and `shiftkey-db` (PostgreSQL 16, data
in the `pgdata` volume). Change the port with `SHIFTKEY_PORT` in `.env`.
Upgrading from the first release keeps your data: missing columns are added on start‑up.

Stop with `docker compose down`; add `-v` to also wipe the database.

> **Changing `POSTGRES_PASSWORD` later:** Postgres only reads it when the volume is first created.
> If you change it afterwards, either reset it in place
> (`docker compose exec db psql -U shiftkey -c "ALTER USER shiftkey WITH PASSWORD 'new-password';"`)
> or start fresh with `docker compose down -v`. `SHIFTKEY_ADMIN_PASSWORD` likewise only applies
> when no admin account exists yet.

## Roles

| Role | Sees | Can do |
|------|------|--------|
| **Admin** (one account, `admin`) | Everything | Upload / delete daily reports, manage users, maintain the shift master, resolve or edit any punch, download original files |
| **HOD** | Only the departments assigned to them (one or more) | Resolve mispunches and long‑span rows in those departments, undo their corrections, export their slice |
| **HR – Indian** | Employees whose PayCode starts with `1`, all departments | View, export, read the corrections log |
| **HR – Local** | Employees whose PayCode starts with `2`, all departments | View, export, read the corrections log |

The prefix → segment rule is `SHIFTKEY_SEGMENT_PREFIXES` (default `1:indian,2:local`).
Departments are learnt from the imports, so upload at least one report before creating HOD accounts.
New users are asked to change their password at first sign‑in; the admin can reset a password or
disable an account at any time (open sessions are revoked immediately).

## Daily flow

1. **Admin** exports *Daily Performance Report* from Starlink as `.xlsx` (`.xls`/`.csv` also work)
   and drags it onto the page. Re‑uploading a date replaces that day.
2. **HODs** sign in and see *Pending resolution* for their departments. For each row they pick:
   * **Use shift end time** (or start time when the IN punch is missing) – from the shift master,
   * **Enter the actual time** – the HOD confirms the real punch,
   * **Mark as half day** or **Mark as absent**.
   Hours are recalculated, the row turns green, the original values stay visible (struck through)
   and in the *Corrections* log. *Undo* reverts to the machine values; the log keeps both entries.
3. **HR / Admin** export. The Excel keeps your layout and shows corrected times; resolved rows are
   green, open mispunches red, half days amber. Extra sheets: *Half Days*, *Mispunches*,
   *Needs Review*, *Absent*, *Corrections*, *Summary* (toggle in the ▾ menu). The ▾ menu also offers
   pending‑only / flagged‑only workbooks, a flat CSV and filtered exports by department or segment.
   File names follow `21 October 2026 Expatriates [Indian] [dept] [filter].xlsx`.

## SQL integrations (pull from / push to databases)

Admins manage everything under **Integrations**.

**Connections** – SQL Server (`pymssql`, what Starlink Time Office normally uses), MySQL/MariaDB,
PostgreSQL and SQLite. Passwords are encrypted at rest (`SHIFTKEY_SECRET_KEY`). Each connection
has *Test*, a **schema browser** (tables, columns, 50-row preview) and a **read-only SQL console**
(single `SELECT` only, executed inside a rolled-back transaction, row-limited, every query audited).
A connection is read-only until you untick that flag; only then can push jobs target it.
Use `:report_date`, `:date_from`, `:date_to` as parameters; other colons (e.g. `'08:30'`) are safe.

**Pull jobs** import attendance straight from the database and go through exactly the same
classification, storage, correction and export path as an Excel upload (`source_kind = sql`,
the raw rows are kept as the report's source with a SHA-256). Two source shapes:

* *Daily rows* – one row per employee per day, e.g. Starlink's `tblTimeRegister` joined to
  `tblEmployee`/`tblDepartment`. Map the columns once (the preview suggests the mapping:
  `PayCode`, `EmpName`, `DepartmentCode`, `In1`→In, `Out2`→Out, `HoursWorked`, `Status`, …).
  Use `:report_date` for one day per run, or `:date_from`/`:date_to` with a mapped date column.
* *Punch log* – raw punches (`PayCode`, `PunchTime`[, `Direction`]) plus an employee master
  query. ShiftKey pairs the first punch after *day start* with the last punch within *max span*
  (night shifts cross midnight correctly); one punch → `MIS`, none → `A`.

Options: hours format (`H.MM` text, decimal hours or minutes), date parameter format for
databases that store dates as text, replace-existing. Jobs can run manually for a date range,
or daily at a fixed time for *yesterday* (or D-0 / D-2). Every run is logged under *Recent runs*
and in the audit trail.

**Push jobs** export ShiftKey data to any writable connection: *attendance* (effective values
with corrections applied plus the machine values), *corrections*, *audit_events* (with hashes)
and *reports*. ShiftKey creates the table (`shiftkey_attendance`, …) or you map ShiftKey fields
onto the columns of an existing table and choose the key columns; write mode is upsert, update-only
or insert-only. Enable **auto-push** to mirror every import and every correction immediately.

```sql
-- Example daily-rows query for a Starlink Time Office database (adjust table/column names)
SELECT r.DateOffice, r.PayCode, e.EmpName, e.PresentCardNo, e.DepartmentCode, d.DepartmentName,
       r.ShiftAttended, r.In1, r.Out1, r.In2, r.Out2, r.HoursWorked, r.Status, r.LateArrival, r.OverTime
FROM   tblTimeRegister r
JOIN   tblEmployee   e ON e.PayCode = r.PayCode
JOIN   tblDepartment d ON d.DepartmentCode = e.DepartmentCode
WHERE  r.DateOffice = :report_date
```

The SQL Server driver ships in the Docker image; on a Mac dev machine `pymssql` may not load,
the UI then shows "driver not installed" for that dialect. If Starlink runs on the same Windows
host as Docker, connect to `host.docker.internal` (see `docker-compose.yml`).

## Audit trail

Everything that can change data or expose it is recorded in an **append-only, hash-chained**
audit trail (`audit_events`). Each event stores who (user, role, IP, browser), what (a catalogued
action such as `correction.applied`), on what (target, report day, department, staff segment,
PayCode), a **before/after snapshot with a field diff**, the outcome, a request id, and the
SHA-256 of the previous event plus its own content. Editing or deleting an event later breaks the
chain, and **Verify integrity** reports the first broken link.

Recorded: sign-ins (success, failure, lock-out), sign-outs, password changes, session revocations,
every user and shift change, report imports (with file SHA-256), replacements and deletions (with
a snapshot of what was lost, including corrections), report views, every export and original-file
download with its filters and output hash, every correction and revert (including refused ones),
refused API calls (401/403/429), and system events (start-up, migrations, admin bootstrap).

The **Audit** tab shows a day-grouped timeline in plain English with filters by period, category,
actor, outcome and free text, expandable details (diff table, context, hashes), auto-refresh, an
integrity check, active-session management with revoke, and CSV/JSON export (itself audited).
The clock icon on any attendance row opens that record's story: machine values, every correction
and revert, the source file hash and the same employee on other days. **History** on a report
filters the trail to that day. Scoping: admins see everything; HODs and HR see their own actions,
report life-cycle events and the corrections inside their departments / segment.

Operational notes: events are written in the same process that handles the request (single
uvicorn worker); the chain lock is per process, so run one app replica. Keep database access
limited – the trail detects tampering, it cannot prevent it. Archive with
`GET /api/audit/export?format=json&period=all` on a schedule if you need an off-box copy.

## Shift master

Starlink prints only the shift *code* and *start*. The end time drives "use shift end". The
defaults (`M07 07:00–19:00`, `M08 08:00–20:00`, `830 08:30–19:00`, `807 08:00–19:00`,
`G 08:30–17:30`, `N19 19:00–07:00`, `N20 20:00–08:00`, …) were inferred from the sample
data – verify them under **Shifts**. Unknown codes seen in an upload are added automatically with
an empty end time; HODs are told to ask the admin when that happens.

## How rows are classified

| Flag | Rule |
|------|------|
| Present / Absent / Half day / Mispunch | Status column from the machine (`P`, `A`, `HLF`, `MIS`) |
| Mispunch (derived) | Any non‑absent row with only one of IN / OUT, whatever the machine said |
| Needs review | Worked span ≥ `SHIFTKEY_LONG_SPAN_HOURS` (default 16 h), or IN == OUT with no hours |
| Pending | Mispunch or needs‑review row without a correction |
| Night | OUT earlier than IN, or shift code starting with `N` |

## Configuration (`.env`)

| Variable | Default | Meaning |
|----------|---------|---------|
| `SHIFTKEY_PORT` | `8080` | Host port for the web UI |
| `SHIFTKEY_ADMIN_USERNAME` / `_PASSWORD` | `admin` / generated | First admin account (created only when no admin exists) |
| `SHIFTKEY_REPORT_NAME_SUFFIX` | `Expatriates` | Export file name suffix |
| `SHIFTKEY_LONG_SPAN_HOURS` | `16` | Threshold for the "needs review" flag |
| `SHIFTKEY_SEGMENT_PREFIXES` | `1:indian,2:local` | PayCode prefix → staff segment |
| `SHIFTKEY_SECURE_COOKIES` | `false` | Set `true` behind HTTPS |
| `SHIFTKEY_SESSION_HOURS` | `168` | Sign‑in lifetime |
| `POSTGRES_DB/USER/PASSWORD` | `shiftkey` | Database credentials |
| `SHIFTKEY_DATABASE_URL` | set by compose | Any SQLAlchemy URL; `sqlite:///./data/shiftkey.db` for a single container |

## Security notes

Passwords are hashed with scrypt; sessions are server‑side and carried in an HttpOnly, SameSite
cookie. State‑changing requests need the `X-Requested-With: ShiftKey` header (CSRF guard).
Six failed sign‑ins lock a username/IP pair for ten minutes. Put a TLS reverse proxy in front
and set `SHIFTKEY_SECURE_COOKIES=true` if ShiftKey leaves the LAN.

## API

Interactive docs at `/api/docs` (sign in first; the cookie is used).

| Method | Path | Who | Purpose |
|--------|------|-----|---------|
| `POST` | `/api/auth/login` · `/api/auth/logout` | all | Session |
| `GET` | `/api/me` · `POST /api/me/password` | all | Profile, password change |
| `GET/POST/PATCH/DELETE` | `/api/users[/{id}]` | admin | User management |
| `GET` | `/api/departments` | all | Departments learnt from imports |
| `GET/PUT/DELETE` | `/api/shifts[/{code}]` | all / admin | Shift master |
| `POST` | `/api/reports/upload` | admin | multipart `file` (+ `report_date`, `replace`) |
| `GET` | `/api/reports` · `/api/reports/{id}` | all (scoped) | Day list, day detail grouped by department |
| `GET` | `/api/reports/{id}/export?format=xlsx\|csv&only=…&segment=…&dept=…` | all (scoped) | Downloads |
| `GET` | `/api/reports/{id}/source` | admin | Original upload |
| `DELETE` | `/api/reports/{id}` | admin | Remove a day |
| `POST` | `/api/records/{id}/correction` | admin, HOD | `{action: shift_time\|set_time\|halfday\|absent, field, time, remark}` |
| `DELETE` | `/api/records/{id}/correction` | admin, HOD | Revert |
| `GET` | `/api/corrections?report_id=` | all (scoped) | Correction log |
| `GET` | `/api/audit?period=7d&category=&action=&actor_id=&outcome=&q=&report_id=&target=&include_views=&before_id=&limit=` | all (scoped) | Audit timeline (cursor paginated) |
| `GET` | `/api/audit/stats` · `/api/audit/actions` · `/api/audit/actors` | all (scoped) | Counters, catalogue, actor list |
| `POST` | `/api/audit/verify` | admin | Hash-chain integrity check |
| `GET` | `/api/audit/export?format=csv\|json` | admin | Archive the trail |
| `GET` | `/api/records/{id}/history` · `/api/reports/{id}/history` | all (scoped) | Story of one record / one day |
| `GET/DELETE` | `/api/sessions[/{id}]` | admin | Active sessions, revoke |
| `GET/POST/PATCH/DELETE` | `/api/connections[/{id}]` · `POST …/test` · `GET …/schema[/{table}?preview=1]` · `POST …/query` | admin | SQL connections, schema browser, read-only console |
| `GET/POST/PATCH/DELETE` | `/api/sync/jobs[/{id}]` · `POST …/{id}/run` · `POST /api/sync/preview` · `GET /api/sync/runs` · `GET /api/sync/catalog` | admin | Pull/push jobs, preview, run history |

## Development

```bash
uv venv --python 3.12 .venv && uv pip install -r requirements-dev.txt
SHIFTKEY_DATABASE_URL=sqlite:///./data/dev.db SHIFTKEY_ADMIN_PASSWORD=Admin-Pass1 \
  .venv/bin/uvicorn app.main:app --reload --port 8765
.venv/bin/python -m pytest
```

Layout: `app/parser.py` (Starlink sheet → records + flags), `app/services.py` (scope, storage,
corrections), `app/auth.py` (passwords, sessions), `app/exporter.py` (xlsx/csv), `app/main.py`
(API), `app/static/` (UI, no build step), `tests/` (parser, exporter, API/role tests against
the files in `dataset/`).

## Next step: reading Starlink's database directly

`reports` / `attendance_records` are the only tables the UI depends on. A scheduled job that
queries the Starlink SQL database and calls `parser.classify` + `services.save_report` with the
same records will make the daily upload unnecessary; roles, corrections and exports keep working.
