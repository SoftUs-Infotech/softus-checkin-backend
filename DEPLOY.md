# Deploying the API to AWS

One EC2 instance runs the API in Docker behind [Caddy](https://caddyserver.com),
which gets and renews the HTTPS certificate by itself. The database is MongoDB
Atlas. The web app is deployed separately to Vercel — see `DEPLOY.md` in
[SoftUs-Infotech/softus-checkin](https://github.com/SoftUs-Infotech/softus-checkin).

```
 browser ──https──▶ Vercel (web app)
    │
    └──https──▶ EC2: Caddy :443 ──▶ API container :8000 ──▶ MongoDB Atlas
```

**Why one instance:** the scheduled jobs (closing forgotten punch-outs, leave
accruals, the weekly digest) and the login rate limiter run inside the API
process. Two copies would run every job twice. Scale by choosing a bigger
instance, not by adding instances or workers.

**Why the browser talks to the API directly** (not through a Vercel rewrite):
the office-network punch rule needs each employee's real IP address. Caddy
records it and overwrites any `X-Forwarded-For` a caller sends, so the rule
cannot be faked with a header.

Do the steps in order: the web app needs the API's address, and the API needs
the web app's.

---

## 1. MongoDB Atlas

1. Create a cluster. Pick the region closest to the EC2 instance (Mumbai,
   `ap-south-1`, for an India-based organisation). The free M0 tier is fine for
   a trial; use M10 or above for production, which adds continuous backups.
2. **Database Access** → add a database user with a generated password.
3. **Connect → Drivers** → copy the `mongodb+srv://…` connection string and
   put the user's password into it.
4. **Network Access** → add the instance's Elastic IP (you get it in step 2).
   Do not allow `0.0.0.0/0`.

## 2. EC2 instance

1. Launch an instance:
   - **AMI:** Ubuntu Server 24.04 LTS
   - **Type:** `t3.small` (2 vCPU, 2 GB) is enough for an office of a few hundred
   - **Storage:** 20 GB gp3
   - **Security group:** SSH (22) from *your* IP only; HTTP (80) and HTTPS
     (443) from anywhere. Port 80 is needed for the certificate and redirects
     to HTTPS.
2. **Elastic IPs** → allocate one and associate it with the instance, so the
   address survives a restart. Add it to Atlas Network Access (step 1.4).

## 3. DNS

Create an **A record**, e.g. `api.yourcompany.com`, pointing at the Elastic IP.
Wait until `nslookup api.yourcompany.com` returns it. Caddy cannot get a
certificate before the name resolves.

## 4. Prepare the server

```bash
ssh -i your-key.pem ubuntu@api.yourcompany.com

git clone https://github.com/SoftUs-Infotech/softus-checkin-backend.git
cd softus-checkin-backend
sudo bash deploy/ec2-setup.sh             # Docker, Compose, swap, security updates
exit                                      # log out and back in for the docker group
```

The repository is private: clone with a GitHub personal access token, or add a
read-only deploy key for the instance under the repository's settings.

## 5. Configure

```bash
cd softus-checkin-backend/deploy
cp .env.production.example .env.production
chmod 600 .env.production
nano .env.production
```

Fill in every value marked **REQUIRED**:

| Setting | Value |
|---|---|
| `API_DOMAIN` | `api.yourcompany.com` |
| `ACME_EMAIL` | an inbox that reads certificate notices |
| `SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `MONGO_URI` | the Atlas string from step 1 |
| `FRONTEND_URL` | the web app's address, e.g. `https://checkin.yourcompany.com` |
| `CORS_ORIGINS` | the web app's address(es), comma-separated |
| `SMTP_*`, `EMAIL_FROM` | the mail account the app sends from |

You won't know the Vercel address until the web app is deployed. Put your
intended custom domain in now, and correct it in step 8 if it changes.

The API **refuses to start** in production if `FRONTEND_URL` is not `https://`,
if `CORS_ORIGINS` is empty or `*`, or if `DEBUG` is on. That is deliberate: such
a deployment would look healthy and then fail for every user.

**Email:** `EMAIL_PROVIDER=smtp` sends real mail. Use it on the production
server only. For a staging copy, set `EMAIL_PROVIDER=console` so test activity
doesn't send mail from the organisation's domain. For mail to reach inboxes,
the sending domain needs SPF and DKIM records for the SMTP provider.

## 6. Start

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps        # api should become "healthy"
curl https://api.yourcompany.com/api/v1/health/ready
```

A `200` with `"database": true` means the API, the certificate and Atlas are
all working. If Caddy's logs show certificate errors, recheck step 3:

```bash
docker compose -f docker-compose.prod.yml logs -f caddy
docker compose -f docker-compose.prod.yml logs -f api
```

## 7. Create the first administrator

```bash
docker compose -f docker-compose.prod.yml exec api \
  python scripts/seed.py --email you@yourcompany.com --first-name Your --last-name Name
```

This creates the default shift, the standard leave types and the organisation
settings, then prints a generated password **once**. Sign in, change it, and
turn on two-factor authentication under Profile. Running it again adds nothing.

`scripts/seed_demo.py` (demo accounts with published passwords) refuses to run
when `ENVIRONMENT=production`, which the Compose file sets.

## 8. Connect the web app

After the web app is on Vercel (its own `DEPLOY.md`), put its final address
into `.env.production`:

```bash
FRONTEND_URL=https://checkin.yourcompany.com
CORS_ORIGINS=https://checkin.yourcompany.com,https://softus-checkin.vercel.app
```

Exact addresses only. `CORS_ORIGIN_REGEX` is refused in production: a pattern
is easy to make wider than intended (`softus-checkin-.*\.vercel\.app` matches
projects anybody can create), and preview builds of unreviewed code shouldn't
reach real records. If you want previews to work, run a second, staging copy of
this stack with `EMAIL_PROVIDER=console`, its own database and a team-pinned
`CORS_ORIGIN_REGEX`, and point Vercel's **Preview** `VITE_API_BASE` at it.

…and recreate the container so it reads them:

```bash
docker compose -f docker-compose.prod.yml up -d
```

## 9. First-day settings, in the app

- **Settings → Where punching is allowed:** open it *from the office Wi-Fi*
  and press **Add this network**. The address it shows is the one this API
  sees for you, which is the one that counts.
- **Shifts, holidays, leave types, Saturday pattern:** check them before
  inviting anyone. Invite links point at `FRONTEND_URL`.
- **Settings → Attendance start → Reset attendance**, on launch day. Without
  it, every earlier day of the month reads as absent and the launch day as a
  short day for everybody. After the reset:
  - days before it are *Not tracked*;
  - the launch day is a grace day: anybody who punched is present, with no
    late or short-day mark;
  - normal rules apply from the next morning.

  That month's salary pays the days up to the launch in full. Nothing is
  deleted, so a wrong date is fixed by resetting again with the right one.

---

## Day-to-day

| Task | Command (from `deploy/`) |
|---|---|
| Deploy a new version | `git pull && docker compose -f docker-compose.prod.yml up -d --build` |
| Follow the logs | `docker compose -f docker-compose.prod.yml logs -f api` |
| Restart | `docker compose -f docker-compose.prod.yml restart api` |
| Change a setting | edit `.env.production`, then `docker compose -f docker-compose.prod.yml up -d` |
| Free disk from old images | `docker image prune -f` |

Deploys cause a few seconds of downtime while the container restarts. Logs
rotate automatically (5 × 10 MB per container).

**Changing `SECRET_KEY` signs everybody out.** Rotate it only when you mean to.

### Backups

Atlas M10+ takes continuous backups; check that they are on. If you run MongoDB
on the instance instead (below), schedule a `mongodump` to S3 yourself.
Without that, losing the instance means losing the records.

### MongoDB on the instance instead of Atlas

For a trial or a small office that accepts the backup trade-off:

1. In `.env.production`, set `MONGO_INITDB_ROOT_PASSWORD` and
   `MONGO_URI=mongodb://softus:<that password>@mongo:27017/?authSource=admin`.
2. Add `--profile local-mongo` to every `docker compose` command.

The database has no published port; only the API container can reach it.
