# Setup guide

Everything needed to go from a fresh clone to drafts landing in Gmail every morning.
Do the sections in order: each one produces a value that goes into `.env` (and later into Render).

| # | Where | You get | Goes into |
|---|---|---|---|
| 1 | Supabase | Postgres connection string | `DATABASE_URL` |
| 2 | Google account | Gmail app password | `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` |
| 3 | Google AI Studio | Gemini key | `GEMINI_API_KEY` |
| 4 | Groq console | Groq key | `GROQ_API_KEY` |
| 5 | Apollo (skip on free plan) | API key | `APOLLO_API_KEY` |
| 6 | Hunter | API key | `HUNTER_API_KEY` |
| 7 | Your machine | `.env`, first real run | - |
| 8 | Render | Backend URL | GitHub secret `BACKEND_URL`, Vercel `VITE_API_URL` |
| 9 | Vercel | Dashboard URL | Render `FRONTEND_ORIGINS` |
| 10 | GitHub | Daily 09:00 IST cron | - |

Only `DATABASE_URL`, `API_KEY`, `CANDIDATE_NAME`, `RESUME_URL`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` and at least
one LLM key are required. Without Apollo/Hunter keys the pipeline still drafts to emails printed in job posts, and
everything else lands under **Jobs -> Apply manually**.

---

## 1. Supabase (database)

1. Sign in at [supabase.com](https://supabase.com) -> **New project**.
   - Region: **South Asia (Mumbai)** - closest to you; Render is in Singapore, which is also fine.
   - Set a database password and save it somewhere. Avoid `@ : / ? #` in it, or URL-encode them later.
2. Once the project is ready, click **Connect** (top bar) -> **Connection string** -> **URI** -> **Session pooler**.
   Copy it. It looks like:
   ```
   postgresql://postgres.abcdefgh:[YOUR-PASSWORD]@aws-0-ap-south-1.pooler.supabase.com:5432/postgres
   ```
   Replace `[YOUR-PASSWORD]` (including the brackets) with your password -> that's `DATABASE_URL`.
   - Use **Session pooler**, not *Direct connection*: the direct host is IPv6-only and Render can't reach it.
   - The app rewrites the scheme to `postgresql+psycopg://` itself; paste it as-is.
3. You don't create any tables. The backend creates them on startup (`jobs`, `companies`, `contacts`, `drafts`,
   `runs`, `events`, `provider_usage`, `resume_cache`).
4. **After the first run** (step 7), lock the tables away from Supabase's public Data API. The backend connects as
   `postgres`, which bypasses RLS, so this changes nothing for the app - it only stops anyone holding your project's
   anon key from reading your data. Supabase -> **SQL Editor** -> run:
   ```sql
   do $$
   declare t record;
   begin
     for t in select tablename from pg_tables where schemaname = 'public' loop
       execute format('alter table public.%I enable row level security', t.tablename);
     end loop;
   end $$;
   ```

> Free Supabase projects pause after ~7 days without activity. The daily cron keeps it active; if you stop the
> cron for a while, unpause the project from the dashboard.

## 2. Gmail app password (drafts)

Drafts are written over IMAP with an app password. Nothing is ever sent.

1. Use the Gmail account you'll send from. 2-Step Verification must be on:
   Google Account -> **Security** -> **2-Step Verification**.
2. Open [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords), name it `Outreach`, create.
   Copy the 16-character password -> `GMAIL_APP_PASSWORD` (spaces are fine, the app strips them).
3. `GMAIL_ADDRESS` = that same Gmail address.
4. Gmail -> Settings -> **See all settings** -> **Forwarding and POP/IMAP**: if there's an IMAP toggle, make sure
   it's enabled (newer accounts have IMAP always on and no toggle).

If you change your Google password, app passwords are revoked - create a new one and update `.env` and Render.

## 3. Gemini (primary LLM)

1. [aistudio.google.com](https://aistudio.google.com) -> **Get API key** -> **Create API key** (in a new project is fine).
2. Copy -> `GEMINI_API_KEY`.

The free tier is rate limited per minute and per day. The app stays under the per-minute limit (`GEMINI_RPM=8`)
and switches to Groq for the rest of the day once the daily quota is hit.

## 4. Groq (fallback LLM)

1. [console.groq.com](https://console.groq.com) -> **API Keys** -> **Create API Key**.
2. Copy -> `GROQ_API_KEY`.

## 5. Apollo (skip unless you pay for it)

Apollo's free plan doesn't include the two APIs this app needs (people search and people match), and it only
accepts sign-ups from a work email. Leave `APOLLO_API_KEY` empty: the pipeline skips Apollo and goes straight
to Hunter. If you ever get a paid Apollo plan, create a key under **Settings -> Integrations -> API** (as a
master key) and set it - Apollo is then tried before Hunter, with no other change needed.

## 6. Hunter (contacts)

1. Sign up at [hunter.io](https://hunter.io) (free plan). It may also want a non-Gmail address; a college email works.
2. Dashboard -> **API** -> copy the key -> `HUNTER_API_KEY`.

One domain search per company returns people with emails, shared inboxes, and the company's email pattern.
The app picks a recruiter / HR / engineering manager if there is one, otherwise a `careers@` / `hr@` / `jobs@`
inbox, so a search rarely comes back empty-handed.

The free plan has **60 searches a month** (resets monthly). `HUNTER_DAILY_SEARCHES=2` spreads them over the
month: good-fit jobs past today's two lookups stay queued for tomorrow instead of being dropped. Jobs whose
posting includes an email never use a search. The Providers page shows used / available, read from Hunter's free
`/account` endpoint.

## 7. Local `.env` and first run

```powershell
cd Cold-Outreach-Backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env
```

Fill in `.env`:

- `DATABASE_URL`, `GMAIL_*`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `APOLLO_API_KEY`, `HUNTER_API_KEY` from above.
- `API_KEY`: any long random string. It's the password for the dashboard and the cron. Generate one:
  `python -c "import secrets;print(secrets.token_urlsafe(32))"`
- `CANDIDATE_PHONE`, `CANDIDATE_LINKEDIN`: check they're your real ones (they go in every signature).
- `RESUME_URL`: the Drive file must be shared as **Anyone with the link -> Viewer**. The scorer reads the
  resume from this link, so updating the file on Drive is enough (picked up within 12h).

Then:

```powershell
pytest                         # 30 tests, no network, uses a local SQLite file
python -m app.run_once         # one full real run: fetch -> score -> contacts -> Gmail drafts
```

Check:
- Gmail -> **Drafts** has new emails. Read a couple end to end before trusting the rest.
- The run prints its stats at the end; errors are logged in the `events` table.
- Optional: `uvicorn app.main:app --reload`, then in the frontend repo `npm install && npm run dev` with
  `VITE_API_URL=http://localhost:8000` in its `.env` -> open `http://localhost:5173`, log in with `API_KEY`.

Now do step 1.4 (enable RLS), since the tables exist.

## 8. Render (backend)

1. Push the backend repo to GitHub.
2. [dashboard.render.com](https://dashboard.render.com) -> **New** -> **Blueprint** -> connect the
   `Cold-Outreach-Backend` repo. Render reads `render.yaml` (free plan, Singapore, Python 3.12).
3. It asks for every variable marked `sync: false`. Paste the same values as your `.env`. Leave
   `FRONTEND_ORIGINS` as `http://localhost:5173` for now - you'll add the Vercel URL in step 9.
   Variables not listed in `render.yaml` (`CANDIDATE_GITHUB`, `GEMINI_MODEL`, `DAILY_DRAFT_TARGET`, ...) use the
   defaults from `app/config.py`; add them under **Environment** only if you want to change them.
4. Deploy. Copy the service URL, e.g. `https://cold-outreach-backend.onrender.com`.
5. Check `https://<your-url>/health` returns `{"ok": true}`.

The free instance sleeps after ~15 minutes idle and takes up to a minute to wake. The cron handles that; the
dashboard may just be slow on its first request.

## 9. Vercel (dashboard)

1. Push the frontend repo to GitHub.
2. [vercel.com](https://vercel.com) -> **Add New** -> **Project** -> import `Cold-Outreach-Frontend`.
   Framework preset: **Vite** (auto-detected).
3. **Environment Variables**: `VITE_API_URL` = the Render URL from step 8 (no trailing slash).
4. Deploy and copy the URL, e.g. `https://cold-outreach-frontend.vercel.app`.
5. Back on Render -> your service -> **Environment** -> set
   `FRONTEND_ORIGINS=http://localhost:5173,https://cold-outreach-frontend.vercel.app`
   (exact origin, no trailing slash) -> save; Render redeploys.
6. Open the dashboard, log in with `API_KEY`. **Providers -> Test connection** confirms Gmail works from Render.

If the dashboard says "Can't reach backend", it's almost always `FRONTEND_ORIGINS` (CORS) or a wrong `VITE_API_URL`.
`VITE_*` values are baked in at build time - after changing one, redeploy on Vercel.

## 10. GitHub Actions (daily cron)

The workflow `.github/workflows/daily-run.yml` runs at **09:00 IST** daily: it wakes Render, calls `POST /runs`,
and polls until the run finishes (the polling also keeps the free instance awake).

1. GitHub -> `Cold-Outreach-Backend` -> **Settings** -> **Secrets and variables** -> **Actions** ->
   **New repository secret**, twice:
   - `BACKEND_URL` = Render URL from step 8, no trailing slash
   - `API_KEY` = exactly the same value as on Render
2. **Actions** tab -> **Daily outreach run** -> **Run workflow** to test it now. A green run whose last step
   prints `"status": "success"` means everything is wired up.

Notes:
- GitHub can start scheduled runs a few minutes late; that's normal.
- On a public repo, GitHub disables scheduled workflows after 60 days with no commits. If it stops, re-enable it
  from the Actions tab (or push any commit).
- Only one run can happen at a time; a second trigger gets a 409 and the workflow exits cleanly.

---

## Daily routine

1. Gmail -> Drafts. Read each one, edit anything that sounds off, send.
2. Drafts flagged **verify before sending** on the dashboard use a guessed email (name + company pattern).
   Double-check the person on LinkedIn, or skip it.
3. Mark drafts **sent** / **replied** on the dashboard - that's how the reply rate is tracked.
4. **Jobs -> Apply manually**: good-fit jobs where no email was found. Apply on the site, or find someone on
   LinkedIn.

## Changing things later

| Want to | Change |
|---|---|
| Swap resume | Replace the file on Drive (same link), or update `RESUME_URL` in `.env` + Render |
| More / fewer drafts | `DAILY_DRAFT_TARGET` |
| Stricter / looser fit | `MIN_SCORE` (default 70) |
| Different roles | `SEARCH_TERMS`, and `TITLE_INCLUDE` / `TITLE_EXCLUDE` in `app/config.py` |
| Target specific companies | Add their Greenhouse / Lever / Ashby board to `config/companies.json` (see README) |
| Rotate `API_KEY` | Render env + GitHub secret + log in again on the dashboard |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Run fails on start with a DB error | `DATABASE_URL` is the Direct connection, or the password has unencoded special characters |
| `check/gmail` fails with `AUTHENTICATIONFAILED` | App password wrong/revoked, or `GMAIL_ADDRESS` isn't the account that owns it |
| Providers shows Gemini **exhausted** early | Daily free quota used; Groq takes over until tomorrow |
| 0 drafts, many jobs in **Apply manually** | Contact finders out of credits; those jobs wait there for you |
| 0 jobs passed the filter | Search terms too narrow or `MAX_REQUIRED_EXPERIENCE` too strict; check Jobs -> Filtered for the reasons |
