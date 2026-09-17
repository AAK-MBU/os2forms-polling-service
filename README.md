# OS2forms Polling Service

A generic, **standalone** polling service. Consumers (journalizing, other apps) **register
jobs** via its JWT-authed API; it polls **OS2forms** webform submissions for each active job
and delivers each submission (case data + document references) to a configurable **destination
system** — Automation Server first, others via pluggable adapters.

It owns its own database and config (`[polling].[PollJob]`) and never reads a consumer's
schema. The same form can feed **multiple** destinations (one job each).

## How it works

```
consumers ── POST /polling/jobs (JWT) ──►  [polling].PollJob
                                                  │
poll loop (every POLL_INTERVAL_SECONDS):          ▼  active jobs
  group active jobs by (source, webformId)   → list each form once (OS2forms webform_rest)
    for each job in the group:
      for each submission NOT delivered for THIS job (dedup on job_id+uuid):
        fetch submission → build payload → destination adapter (e.g. workqueue add, ref=uuid)
        → record in [polling].SubmissionPollStatus (uuid + serial, for gap detection)
erase_after sweep → expire old state rows
```

- **Standalone:** own DB/schema, own JWT auth. No shared tables with any consumer.
- **Idempotency** is owned by the service via `SubmissionPollStatus` keyed `(job_id, uuid)`;
  the submission UUID is also stamped as the destination work-item `reference` as a 2nd guard.
- **Documents** are passed through as OS2forms authenticated attachment URLs; the consuming
  robot fetches them with its own key. The poller never downloads binaries.

## Layout

```
app/
  config.py            # env-driven settings (DB, OS2forms, destination, JWT)
  logging.py           # structured JSON logging with secret scrubbing
  exceptions.py        # source/destination error hierarchies
  db.py                # sync SQLAlchemy engine + migrations bootstrap
  models.py            # PollJob (config) + ApiKey + SubmissionPollStatus
  jobs.py              # PollJob repository (CRUD + active-job loading)
  state.py             # SubmissionPollStatus repository (dedup/retry/retention/gaps)
  payload.py           # build a work-item payload from a submission + destination_config
  auth.py              # JWT mint/verify, API-key exchange, scopes/ownership
  poller.py            # the poll loop (group by source+form, fan out to jobs)
  main.py              # FastAPI app + background poll thread (entrypoint)
  api/                 # FastAPI routers: health, auth (/auth/token), jobs (/polling/jobs)
  sources/             # source adapters, keyed by PollJob.source (os2forms)
  destinations/        # destination adapters, keyed by destination_system prefix
migrations/            # idempotent T-SQL, applied in order at startup
  001_create_poll_job.sql  002_create_api_key.sql  003_create_submission_poll_status.sql
  004_add_payload_mapping.sql  005_add_submission_serial.sql
```

## Using the API

```bash
# 1. Get a JWT from a registered API key (admin key can be seeded via BOOTSTRAP_ADMIN_KEY)
TOKEN=$(curl -s localhost:8080/auth/token -d '{"api_key":"<your-key>"}' \
  -H 'content-type: application/json' | jq -r .access_token)

# 2. Register a poll job (needs polling:write scope)
curl localhost:8080/polling/jobs -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{
    "name": "My webform",
    "source": "os2forms",
    "webformId": "_my_webform",
    "destination_system": "automation_server:IntakeQueue",
    "destination_config": {"caseType": "MyCaseType", "caseData": {}, "documentData": {}},
    "erase_after": 30
  }'

# 3. Watch a job's poll health
curl localhost:8080/polling/jobs/1/status -H "Authorization: Bearer $TOKEN"

# 4. Reconcile backwards: which submissions were never listed to us at all?
curl localhost:8080/polling/jobs/1/gaps -H "Authorization: Bearer $TOKEN"
```

OS2forms serials are consecutive per webform, so a hole in the serials a job holds rows for
means a submission never reached the poller — the one loss that leaves no state row behind.
Holes are a prompt to check, not proof: a job registered after the form went live starts
mid-sequence, and submissions deleted in OS2forms leave permanent, legitimate holes.

Interactive docs at `http://localhost:8080/docs`.

## Run

The project uses [`uv`](https://docs.astral.sh/uv/) for dependency management (a committed
`uv.lock` pins exact versions).

```bash
cp .env.example .env      # fill in DB + OS2forms + Automation Server credentials
docker build -t os2forms-polling-service .
docker run --env-file .env os2forms-polling-service
```

Locally (needs the SQL Server ODBC driver installed on the host for real DB access):

```bash
uv sync                   # create .venv and install from the lockfile
uv run python -m app.main
```

## Testing

There are two levels: **lint**, and a **live end-to-end run** against the real systems.

### 1. Lint

```bash
uv sync --extra dev       # create .venv and install deps (incl. ruff) from the lockfile
uv run ruff check app
```

Expected: `All checks passed!`.

### 2. Live end-to-end run

Requires real credentials and the SQL Server ODBC driver (bundled in the Docker image; see
`Dockerfile`). Point at a **non-production queue** first.

```bash
cp .env.example .env      # fill DATABASE_URL, OS2FORMS_API_KEY, AUTOMATION_SERVER_* 
docker build -t os2forms-polling-service .
docker run --env-file .env os2forms-polling-service
```

Register a job (see **Using the API** above), then watch the structured JSON logs
(secrets are auto-redacted):

- `migration.applied` / `state.bootstrapped` — the `[polling]` schema + tables were created.
- `os2forms.listed count=N` — the webform_rest list call succeeded and found submissions.
- `submission.delivered` — a submission was pushed to the destination work queue.
- `poll.completed jobs=… delivered=… failed=… skipped=…` — end-of-cycle summary. It also
  carries `dead_letter=`, `abort_job=` and `abort_group=` when those occur.
- `job.serial_gap count=N` — the job's serial sequence has holes; enumerate them with
  `GET /polling/jobs/{id}/gaps`.

Then check the target queue for the new work items, `GET /polling/jobs/{id}/status`, and the
state table:

```sql
SELECT status, attempts, delivered_at, last_error
FROM polling.SubmissionPollStatus ORDER BY first_seen_at DESC;
```

A second poll cycle should report the same submissions as `skipped` (idempotency working),
not re-deliver them.

**Tips for a safe first run:**

- Set `MAX_ATTEMPTS` low and watch a submission the destination keeps 5xx-ing move
  `failed → dead_letter` (retry it via `POST /polling/jobs/{id}/submissions/{uuid}/retry`).
- Point a job at a queue that does not exist and confirm the log shows `job.fatal` with
  `scope=abort_job` and **no** growth in `attempts`: a broken job is not the submissions' fault,
  so the poller abandons the job for that cycle instead of parking its whole backlog.
- Set a job's `isActive` false (`PATCH /polling/jobs/{id}`) to confirm it is ignored.
- To reduce blast radius, point `destination_system` at a throwaway queue
  (`automation_server:TestQueue`).

> The DDL in `migrations/` is verified to apply on real SQL Server (tables, defaults, FK
> cascade, unique constraint, idempotent re-run).

## Open items (see SPEC §12)

Non-blocking operational confirmations only: `erase_after` unit (code assumes days, override
via `ERASE_AFTER_UNIT`), `MAX_ATTEMPTS` (default 5), the poller's own DB vs a `[polling]` schema
on a shared server, and API-key management (built-in `ApiKey`/`/auth/token` vs an external IdP).
OS2forms parsing is verified against real responses — no field-shape guesses remain.
