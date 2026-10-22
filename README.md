# Softus Checkin — Backend

FastAPI + MongoDB API for the employee attendance and management system.

## Layout

```
main.py            dev entrypoint — runs uvicorn, nothing else
app/
  main.py          app assembly: lifespan, CORS, middleware, exception handlers
  api.py           the one place every router is mounted
  core/            config · logging · security · exceptions · datetime
  db/              motor client · index declarations
  models/          what is stored in MongoDB
  schemas/         what the API accepts and returns
  routes/          thin HTTP layer — no try/except
  services/        all business logic; the only layer that touches the DB
  utils/           deps · middleware · response
tests/
```

**Layering rule:** a route validates input, calls a service, and wraps the result.
Services raise `AppError` subclasses; global handlers render them. Routes never
catch exceptions, and only services touch the database.

## Running locally

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements-dev.txt   # the API plus pytest and ruff

cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste into SECRET_KEY

python main.py
```

API docs: <http://localhost:8000/docs>

The app refuses to start without a valid `SECRET_KEY` — that is deliberate.

### With Docker

```bash
docker compose up --build     # API on :8000, MongoDB on :27017
```

### Seeding the first administrator

```bash
python scripts/seed.py                       # generates and prints a password once
python scripts/seed.py --email you@yourcompany.com   # same, for your own address
```

Idempotent — it does nothing if an administrator already exists, unless `--force`.

### Setting administrators on any database (including the live one)

```bash
cp scripts/admins.local.example.json scripts/admins.local.json   # gitignored
# write the admins' email, password, first_name, last_name into it
python scripts/set_admins.py
```

It writes to whatever `MONGO_URI` / `MONGO_DB` in `.env` point at. Put the live
Atlas URI there to set up production from your own machine. Before writing, it
shows the server (password hidden) and the database, and waits for you to type
the database name. For each entry:

- a new email becomes an active administrator;
- an existing account is made an administrator, reinstated if suspended or
  removed, and given the file's password, which signs it out everywhere.

A fresh database also gets its settings, default shift and leave types. Every
change appears in the Audit trail. Nothing is emailed, and `.env` needs no
`SECRET_KEY` for this. `admins.local.json` holds real passwords: it is ignored
by git, but delete it once the admins can sign in.

### Seeding a demo organisation

```bash
python scripts/seed_demo.py                  # add demo data alongside what exists
python scripts/seed_demo.py --reset          # drop the database first
```

Creates an administrator and four employees, then back-fills six weeks of attendance so every calendar colour appears. It also
seeds the things the newer screens need in order to show anything: salaries
(including one employee deliberately left without one, so the warning is
visible), a Saturday swap and a hand-closed day with the comp-offs they
generate, tasks spread across every progress state including one left open long
enough to read as stale, an employee working from home, and enough pending
corrections, leave and tasks to try bulk approval on.

The administrator is seeded unable to punch, which is the default for the role —
that is what makes the admin and employee halves of the application differ.

**Local demos only.** The demo accounts share fixed passwords, so it refuses to
run when `ENVIRONMENT=production`. Never point it at a real database.

## Deploying

To AWS (one EC2 instance, Docker, Caddy for HTTPS, MongoDB Atlas) — see
[`DEPLOY.md`](DEPLOY.md). The web app lives in
[SoftUs-Infotech/softus-checkin](https://github.com/SoftUs-Infotech/softus-checkin)
and deploys to Vercel.

## Tests

```bash
pytest          # database-backed tests skip themselves if MongoDB is not running
ruff check .
```

## API

All paths are prefixed with `API_PREFIX` (default `/api/v1`).

### Auth

| Method | Path | Access | Purpose |
|---|---|---|---|
| POST | `/auth/invite` | admin | Invite an employee; emails a one-time link |
| GET | `/auth/invite/{token}` | public | Preview an invitation before accepting |
| POST | `/auth/accept-invite` | public | Set password, complete profile, sign in |
| GET | `/auth/invites` | admin | List invitations |
| DELETE | `/auth/invites/{id}` | admin | Revoke a pending invitation |
| POST | `/auth/login` | public | Sign in |
| POST | `/auth/refresh` | public | Rotate the token pair |
| POST | `/auth/logout` | employee | Sign out of this or every device |
| GET | `/auth/me` | employee | The signed-in account |
| PATCH | `/auth/me` | employee | Update own profile |
| POST | `/auth/change-password` | employee | Change password |
| POST | `/auth/forgot-password` | public | Request a reset link |
| POST | `/auth/reset-password` | public | Set a new password from a link |
| GET | `/auth/2fa` | admin | Whether two-factor is on, and codes remaining |
| POST | `/auth/2fa/setup` | admin | Generate a secret and recovery codes, enabling nothing yet |
| POST | `/auth/2fa/confirm` | admin | Prove the authenticator works, and switch it on |
| POST | `/auth/2fa/verify` | public | Exchange a login challenge and a code for tokens |
| POST | `/auth/2fa/disable` | admin | Turn it off — requires the account password |

### Users

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/users` | admin | List employees (`search`, `role`, `status`, `department`, paginated) |
| GET | `/users/departments` | employee | Distinct department names |
| GET | `/users/{id}` | admin | One employee |
| PATCH | `/users/{id}` | admin | Update an employee |
| POST | `/users/{id}/suspend` | admin | Suspend and revoke their sessions |
| POST | `/users/{id}/activate` | admin | Reinstate |
| DELETE | `/users/{id}` | admin | Soft delete |

### Configuration

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/shifts` | employee | List shifts with assigned headcount |
| POST | `/shifts` | admin | Create a shift |
| GET · PATCH · DELETE | `/shifts/{id}` | admin (GET: employee) | Manage one shift |
| POST | `/shifts/{id}/assign` | admin | Assign employees to a shift |
| GET | `/holidays` | employee | Calendar, filtered by year or date range |
| GET | `/holidays/upcoming` | employee | Next few holidays, for the dashboard |
| POST | `/holidays` · `/holidays/bulk` | admin | Add one or a whole year |
| PATCH · DELETE | `/holidays/{id}` | admin | Maintain a holiday |
| GET | `/leave-types` | employee | Types available when applying |
| POST | `/leave-types` | admin | Introduce a new kind of leave |
| GET · PATCH · DELETE | `/leave-types/{id}` | admin (GET: employee) | Manage a type |
| GET | `/settings` | employee | Organisation policy |
| PATCH | `/settings` | admin | Update policy |
| GET | `/audit-logs` | admin | Who changed what, when |

**Shifts decide what a full day means.** Required minutes, half-day threshold,
grace, break deduction and working weekdays are all per-shift data, assigned per
employee with one org default. The attendance engine reads whatever is stored —
changing "nine hours" to "eight" is an admin action, not a deployment.

Rules the service enforces:

- Exactly one shift is the default; promoting one demotes the rest.
- The default shift cannot be deleted or deactivated.
- A shift with employees on it cannot be deleted — move them first.
- `half_day_minutes` must be below `required_minutes`, on create and on update.
- A shift whose end time is at or before its start time is a night shift, and is
  reported with `crosses_midnight: true`.

### Work calendar and salary

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/work-calendar` | employee | What each date of a range is: working, off, and why |
| POST · DELETE | `/work-calendar/overrides` | admin | Open or close a single date by hand |
| POST · DELETE | `/work-calendar/swaps` | admin | "The third Saturday off instead of the second" |
| GET | `/work-calendar/comp-offs` | employee (`scope=org`: admin) | The comp-off ledger |
| GET · POST | `/salary` | **admin** | Salary history, and setting a new figure |
| POST | `/salary/adjustments` | **admin** | A one-off correction settled in a named month |
| GET | `/salary/projection` | **admin** | One employee's month, or the whole organisation's |

**A holiday is a closed day, not an annotation on an open one.** Declaring a
holiday closes it in the work calendar, which is the one place that answers "is
the office open". The month grid greys it, nobody is marked absent on it, it
leaves the month's working-day count, and the per-day rate — the month's pay
divided by exactly that count — follows without anything else being told. An
override still beats it, for the year the office decides to work a holiday.

**Saturdays are org policy, not shift policy.** `working_saturdays` says which
Saturdays of the month the office is open; the fifth is tri-state because it is
usually decided late. A per-date override beats the pattern, and a *swap* — the
third Saturday off instead of the second — is stored as two linked rulings and
refused unless the month keeps the same number of working days. Turning a worked
day off credits a comp-off to everyone who worked it.

**Salary is administrators only, and there is no employee-facing route at all.**
Not a hidden field — no endpoint exists, and no schema an employee receives
carries a figure. Salary is a history: a raise adds a record rather than
overwriting one, so what somebody was paid in March still reads correctly in
December. The projection prices the month from the work calendar's working-day
count and the deduction flags in settings.

**Leave types are data.** Introducing a new kind of leave is a row, never a code
change. A type that no request references is deleted outright; one that is
referenced is deactivated instead, so historical requests keep resolving to the
policy they were made under.

**Leave accrues on a policy you set.** Frequency (monthly, quarterly, yearly),
amount per cycle — 0.5 is allowed, which is how "half a day of sick leave per
month" is expressed — credited at the start or the end of the cycle, prorated
for somebody who joined mid-cycle, capped on carry-forward, and lapsing at year
end unless the type says otherwise. Crediting is idempotent: every credit is
recorded against a cycle key, so running the job twice never pays twice. A type
may permit a negative balance; by default it does not.

**A late approval never rewrites a month you have already paid.** Approving an
August leave in September recomputes August's attendance so the calendar tells
the truth, then posts the difference as an adjustment line in the current month,
labelled with the date it came from. Approvals older than
`retro_approval_months` are refused.

**Approvals route to the person who knows.** A manager decides corrections,
leave and tasks for the employees who report to them, and their queues and
badge counts are scoped to exactly those people — asking for somebody else's is
`403 NOT_YOUR_REPORT`, not a silently empty page. An administrator remains the
fallback for everybody, including for a manager's own requests: nobody decides
their own. Every decision records the role that actually made it, so the audit
answers "who approved this, acting as what". A manager sees none of the rest of
the administration product — no people, salary, configuration, settings or audit
trail — and those endpoints refuse them.

**The audit trail is append-only.** Every administrative mutation records actor,
IP, and only the fields that actually changed. Password hashes and token digests
are stripped before writing. A no-op update writes nothing.

### Daily tasks

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET · POST | `/tasks` | employee | List (filter by `status`, `progress`) and log work |
| GET | `/tasks/today` | employee | Today's log, including work carried in |
| GET | `/tasks/day` | employee (admin: anyone) | One day's log for one employee |
| GET | `/tasks/calendar` | employee (admin: anyone) | A month grid plus week summaries |
| PATCH | `/tasks/{id}/progress` | owner | Move a task along |
| POST | `/tasks/{id}/review` | admin | Approve, reject or ask for changes |
| POST | `/tasks/bulk-decision` | admin | One decision and one note across many tasks |

**Progress and status are different things.** *Progress* — `TO_DO`,
`IN_PROGRESS`, `DONE`, `BLOCKED` — is the employee's word on the work, and only
they can change it, on any day. *Status* — `SUBMITTED`, `APPROVED`, `REJECTED`,
`CHANGES_REQUESTED` — is the administrator's word on the record. Merging them
would mean an employee marking their own work approved.

**Carry-forward is a reading rule, not a copy.** A task that is not `DONE`
appears on every following day until it closes, so one task keeps one history and
`days_open` means what it says. Anything open longer than a fortnight is flagged
as stale.

### Security behaviour worth knowing

- **Refresh rotation with reuse detection.** Each refresh issues a new token and
  revokes the old one. Presenting an already-rotated token means it leaked, so
  the whole token family is revoked and the user must sign in again.
- **No account enumeration.** Sign-in and forgot-password answer identically
  whether or not the address exists.
- **Suspension and password changes revoke sessions immediately**, rather than
  waiting for the access token to expire.
- **Tokens are stored as digests.** Invite, reset and refresh tokens exist in
  plaintext only in the email or the client.
- **Rate limiting is per process** (`app/core/rate_limit.py`). Running multiple
  workers behind a load balancer needs a shared store — swap the backend, the
  call signature stays.
- **Two-factor authentication, for administrators.** An admin account with it
  enabled gets no tokens from `/auth/login` — only a short-lived, single-use
  challenge, exchanged with a six-digit code or a recovery code. The challenge
  carries a fingerprint of the password hash, so changing or resetting the
  password voids any outstanding challenge without the password paths needing to
  know that two-factor exists. Recovery codes are stored as digests and spent on
  use; a code already accepted cannot be replayed.
- **The last active administrator cannot be suspended or deleted.**

### Where a punch is allowed to come from

Three independent gates, each switchable, checked in this order:

1. **`can_punch` on the account.** An administrator oversees attendance rather
   than recording it, so admins are created with this off. A working admin gets
   it back with one flag.
2. **The device.** `block_mobile_punch` keeps punching on a laptop or desktop.
3. **The office network.** `enforce_office_network_for_punch` and
   `enforce_office_network_for_corrections` check the caller's public IP against
   `office_ip_ranges` — plain addresses or CIDR, both accepted. An empty
   allowlist can never match, so enforcement cannot pass vacuously. An employee
   with `wfh_enabled` (optionally date-bounded) is exempt.

Refusal is `403 OFFICE_NETWORK_REQUIRED` or `403 PUNCH_NOT_PERMITTED`, and the
message says what to do instead — file a correction.

**The geofence flags, it never blocks.** With `office_latitude`/`office_longitude`
set, every punch stores its distance from the office and an `outside_office`
boolean. A bad GPS fix must not cost somebody their attendance; it costs them a
line on the administrator's exception feed instead.

**Where a punch happened.** Coordinates are reverse-geocoded into two things: a
full street address, kept on the day's detail, and a *city*, which is what the
register column shows — "Surat" answers "where was this punch" far better than a
street line, and immeasurably better than a raw IP address. With
`GOOGLE_MAPS_API_KEY` set Google answers; without it the lookup falls back to
OpenStreetMap's Nominatim, which needs no key. Either answer is cached for 30
days by coordinates rounded to four decimal places, so a whole office costs one
request. On any failure the punch still succeeds and stores coordinates alone —
geocoding is never allowed to fail a punch.

Punches recorded before the city existed can be named retrospectively:

```bash
python scripts/backfill_places.py           # report what it would change
python scripts/backfill_places.py --apply
```

It touches only punches that have coordinates and no city, and writes no
attendance figure of any kind.

**Who the caller is, and why it is not the header.** `X-Forwarded-For` is a
header: it is whatever the sender typed. Reading it from anybody made the
office-network rule decorative — an employee could punch from a beach by naming
the office's address in one. `client_ip` now consults forwarding headers only
when the machine that opened the connection is listed in `TRUSTED_PROXIES`, and
reads the chain **from the right**, because a proxy *appends* the peer it saw
and everything left of what infrastructure added is the client's own text. If
every hop is a trusted proxy, the socket's peer address stands; falling back to
anything the caller writes would reopen the hole.

`TRUSTED_PROXIES` defaults to loopback, which is what a same-host reverse proxy
or tunnel client looks like. **Behind a proxy that does not strip an inbound
`X-Forwarded-For`, this rule cannot be enforced at all** — fix the proxy, or
leave enforcement off.

**Which network counts as the office.** `GET /settings/network` reports the
address the API itself judges the caller by, whether the allowlist already
covers it, and the entry to add if not. The settings screen uses it so an
administrator standing on the office Wi-Fi can allow that network in one press.
Sending them to a "what is my IP" site does not work: behind a proxy or a
tunnel, the address such a site reports is not the one the allowlist compares.
The suggestion for an IPv6 address is its `/64` prefix rather than the address
itself, because the back half rotates and pinning it would stop punching within
a day.

### Email in development

`EMAIL_PROVIDER=console` prints each email — including the full invite and reset
links — to the log instead of sending it. The invite response also returns
`invite_url`, so an admin is never stuck when mail is not configured.

## Response envelope

Every endpoint — success or failure — returns the same shape:

```json
{ "success": true, "message": "OK", "data": {}, "meta": null }
```

```json
{
  "success": false,
  "message": "The requested resource was not found.",
  "data": null,
  "error": { "code": "NOT_FOUND" },
  "meta": { "request_id": "8f2c1ab39d4e5f60" }
}
```

Every response carries `X-Request-ID`; supply your own header to trace a request
end to end through the logs.

## Time handling

All timestamps are stored in UTC. The *attendance date* is derived in
`ORG_TIMEZONE` — see `app/core/datetime.py`. A punch-out at 00:30 local time
belongs to the local calendar day, not the UTC one.
