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

## Tests

```bash
pytest          # no live MongoDB required
ruff check .
```

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
