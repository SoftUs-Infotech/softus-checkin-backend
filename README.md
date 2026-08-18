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
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt

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
python scripts/seed.py --email you@acme.com --password "Str0ngPass"
```

Idempotent — it does nothing if an administrator already exists, unless `--force`.

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

**Leave types are data.** Introducing a new kind of leave is a row, never a code
change. A type that no request references is deleted outright; one that is
referenced is deactivated instead, so historical requests keep resolving to the
policy they were made under.

**The audit trail is append-only.** Every administrative mutation records actor,
IP, and only the fields that actually changed. Password hashes and token digests
are stripped before writing. A no-op update writes nothing.

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
- **The last active administrator cannot be suspended or deleted.**

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
