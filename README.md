# Cold Outreach - Backend

Personal job-switch assistant. Every morning it finds fresh jobs in India, keeps only the ones I'm a real fit for,
finds one person to write to, and puts a personalised email in my **Gmail Drafts**. I review and send them by hand.

```
GitHub Actions cron (09:00 IST)
  -> POST /runs on Render
     1. fetch      LinkedIn, Naukri, Indeed (JobSpy) + optional Greenhouse/Lever/Ashby boards
     2. filter     free rules: title, years asked, salary, location, blocklist, dedupe
     3. score      Gemini -> Groq fallback, scored against my resume (read from the Drive link)
     4. contact    email in JD -> Apollo -> Hunter -> Apollo name + Hunter pattern
     5. write      LLM writes the body; links + signature appended in code
     6. draft      IMAP APPEND into Gmail Drafts (never sends)
```

Quality over volume: at most `DAILY_DRAFT_TARGET` (5) drafts a day, only jobs scoring `MIN_SCORE` (70)+,
one job per company per run, no company twice within 30 days, no email address ever twice.
Good-fit jobs without a findable contact show up in the dashboard under **Apply manually**.

## Setup

Step-by-step for every service (Supabase, Gmail, Gemini, Groq, Apollo, Hunter, Render, Vercel, GitHub secrets):
[docs/SETUP.md](docs/SETUP.md). Short version:

1. **Supabase** - create a project, copy *Connect -> Session pooler* URI into `DATABASE_URL`.
   (Render can't reach Supabase's IPv6-only direct host.) Tables are created on startup.
2. **Gmail app password** - Google Account -> Security -> 2-Step Verification -> App passwords.
   IMAP must be enabled in Gmail settings (it is by default for new accounts).
3. **Keys** - Gemini (aistudio.google.com), Groq (console.groq.com), Apollo, Hunter. All optional except one LLM.
4. **Resume** - Drive file shared as *Anyone with the link*. Changing the file on Drive updates the scorer within 12h.

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows; source .venv/bin/activate elsewhere
pip install -r requirements-dev.txt
cp .env.example .env                                 # fill it in
python -m app.run_once                               # one full run from your machine
uvicorn app.main:app --reload                        # API for the dashboard on :8000
pytest
```

Sanity checks once it's running: `GET /check/gmail` (IMAP login + drafts folder), `GET /providers` (usage, Hunter credits left).

## Deploy

- **Render**: New -> Blueprint -> this repo (`render.yaml`). Fill the env vars in the Render dashboard.
- **GitHub Actions**: repo Settings -> Secrets -> `BACKEND_URL` (e.g. `https://cold-outreach-backend.onrender.com`) and `API_KEY`.
  The workflow wakes the free instance, starts a run and polls until it finishes (polling also keeps it awake).
  Trigger it by hand from the Actions tab to test.

## Tuning

Everything lives in env vars (see `.env.example` and `app/config.py`): search terms, title include/exclude words,
max years asked, min score, daily target. To add companies you'd love to work at, list their public ATS boards in
`config/companies.json`:

```json
[
  {"name": "Acme", "ats": "greenhouse", "slug": "acme", "domain": "acme.com"},
  {"name": "Foo",  "ats": "lever",      "slug": "foo",  "domain": "foo.ai"},
  {"name": "Bar",  "ats": "ashby",      "slug": "bar",  "domain": "bar.in"}
]
```

The slug is the last part of their careers URL (`boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`).

## API (all need `X-API-Key`)

| Method | Path | |
|---|---|---|
| POST | `/runs?trigger=manual` | start a run (409 if one is running) |
| GET | `/runs`, `/runs/{id}` | history, stats and event log |
| GET | `/stats` | today's count, reply rate, jobs by status |
| GET | `/drafts?status=` / PATCH `/drafts/{id}` | mark `sent` / `replied` / `skipped` |
| GET | `/jobs?status=no_contact` / PATCH `/jobs/{id}` | apply-manually list; requeue (`scored`) or dismiss |
| GET | `/providers`, `/events?level=error`, `/check/gmail` | health of everything |
