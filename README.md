# Apply Agents

A local tool that fills out internship applications in parallel. Several browser agents work
through a queue of job postings at the same time, fill every field they can back with a saved
fact, hand essays to Claude, and then **stop at a review checkpoint**. Nothing is ever submitted
until you read it and press **APPROVE SUBMISSION**.

```
URLs ─► queue ─► Agent 01 ┐
                 Agent 02 ├─► fill form ─► read every value back ─► READY_FOR_REVIEW ─► you approve ─► Submit
                 Agent 03 ┘          └─► question it can't answer ─► WAITING_FOR_INPUT ─► you answer ─► back to agent
```

Built with Python, Playwright, FastAPI and SQLite. It runs entirely on your own machine; the
dashboard listens on `127.0.0.1` only.

## How it works, step by step

1. **You queue jobs.** Paste posting URLs into the dashboard (one per line, or
   `Company | Role | URL`). Or import a folder of jobs that already have a tailored resume and
   cover letter.
2. **The manager hands each job to a free agent.** `MAX_CONCURRENT_AGENTS` sets how many run at
   once. Each agent gets its own Chrome window, so you can watch it type.
3. **The agent opens the posting and finds the form.** It clicks through "Apply" buttons and
   iframes (Greenhouse, Lever and Ashby embed forms differently).
4. **It reads every question on the page:** the label, the type (text, dropdown, checkbox,
   combobox, file upload), the options, whether it's required, and any character limit.
5. **It decides an answer for each question, in this order:**
   1. **Approved answers.** Anything you've answered in the dashboard before. Two questions count
      as the same if only filler words differ ("Do you have experience with X" vs "Have you any
      experience with X"). Company-specific answers ("Why Acme?") stay with that company.
   2. **Rules in `profile.yaml`.** A regex on the question label, for things like work
      authorization, relocation or GPA. `prefer` patterns pick the right option on a dropdown.
   3. **Facts in `profile.yaml`.** Name, email, school and so on, matched by label.
   4. **The LLM, once per page, for whatever's left.** It can only *point* at one of your saved
      facts or answers, or pick an option that's already on the form. It never writes free text,
      so it can't make anything up. Anything it matched is flagged for you to double-check.
   5. **Claude, for essays and file uploads.** Headless Claude Code writes answers to "Why us?",
      "Tell us about a project" and similar questions using only your facts file and profile. It
      also picks which PDF goes in each upload box (resume, cover letter or portfolio). Every
      Claude answer is flagged "written by Claude" so you read it before approving.
6. **It types the answers in,** including custom dropdowns that open up or down, comboboxes that
   filter as you type, and file uploads.
7. **It reads every field back off the page.** Anything that didn't stick, or that the site
   changed, goes to you.
8. **It stops.** If anything is missing it's `WAITING_FOR_INPUT`: answer in the dashboard, press
   RETURN TO AGENT, and the agent picks the form back up. Otherwise it's `READY_FOR_REVIEW`, with
   a screenshot and a list of where every value came from.
9. **You review and approve.** APPROVE SUBMISSION makes the agent click the site's real Submit
   and look for the confirmation page. EDIT brings the window to the front so you can change
   anything by hand. Afterwards it logs the application to a CSV.

It also stops and hands the application to you on login pages, bot checks, CAPTCHAs, Next buttons
that don't advance, and any website error. It never tries to get around any of those.

### Tricky questions it handles

Some questions look like one thing and mean another, so the matcher has
guards for them:
- A yes/no rule never fills a free-text box (an address field won't get "Yes").
- "Are you available to work N weeks full-time" doesn't trigger the work-authorization rule.
- "If other, please explain" boxes stay empty unless "Other" was chosen.
- Answers longer than a field's max length go to you instead of getting cut off.

## Setup

```powershell
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env
copy profile.example.yaml profile.yaml   # then fill it in
copy facts.example.md facts.md           # then fill it in
```

**LLM key (optional, free):** sign in at <https://build.nvidia.com>, open any model, click
**Get API Key**, and paste it into `.env` after `NVIDIA_API_KEY=`. Without a key, leftover
questions just come to you instead.

**Claude (optional):** install [Claude Code](https://claude.com/claude-code). Set `CLAUDE_PATH`
in `.env` if it isn't at the default location. Turn it off with `CLAUDE_AUTO_ANSWER=false`, or
press **Ask Claude** on one application by hand.

## Run it

Double-click **`run.cmd`** (or run `python -m apply_agents` with `src` on `PYTHONPATH`). The
dashboard opens at <http://127.0.0.1:8765>, and Chrome windows appear as agents start.

- **Add internships:** paste URLs into the box, or click **Import my Ready folders** to queue every
  job in `<APPLICATIONS_DIR>/Ready/`. Each folder's tailored resume and cover letter are uploaded
  automatically.
- **Review:** click a row to see what was filled and where each value came from, the questions
  that need you, anything uncertain, a screenshot, and the log.
  - **APPROVE SUBMISSION**: the agent clicks Submit and checks for the confirmation page.
  - **EDIT**: brings that application's window to the front.
  - **RETURN TO AGENT**: saves your answers for reuse and re-checks the page.
  - **SKIP**: closes it.
  - **Mark submitted**: if you finished it yourself in the window.

## Settings (`.env`)

| Setting | What it does |
|---|---|
| `MAX_CONCURRENT_AGENTS=3` | How many applications run at once |
| `LLM_PROVIDER=nvidia` / `LLM_MODEL=...` | Main model. `nvidia`, `openrouter`, or `custom` + `CUSTOM_BASE_URL` (any OpenAI-compatible endpoint) |
| `LLM_BACKUP_MODEL` | Second model on the same provider, tried if the main one fails |
| `LLM_FALLBACK_PROVIDER` | Used when the main provider is rate-limited or down |
| `NVIDIA_RPM=35` | Requests per minute it allows itself (free tier is 40) |
| `HEADLESS=false` | `true` hides the browser windows (you then can't use EDIT) |
| `APPLICATIONS_DIR`, `APPLIED_LOG`, `CLAUDE_FACTS` | Where job folders, the submission log and the facts file live |

Rate limits: one requests-per-minute bucket per provider plus a global cap on parallel calls. It
backs off exponentially, respects `Retry-After`, and moves to the fallback provider after
`LLM_MAX_RETRIES`. Expect about one LLM call per form page.

## What's where

```
src/apply_agents/
  manager.py        queue, N workers, paused windows, approve / skip / return
  agent.py          one application: open, find the form, fill, verify, stop; submit only on approval
  page_js.py        in-page scripts that read questions, read values back, find buttons and blockers
  profile.py        answer matching (approved → rules → facts → LLM pointer)
  claude_writer.py  headless Claude for essays and file choices, facts-only
  llm.py            OpenAI-compatible providers, throttling, backoff, fallback
  db.py             SQLite: applications, logs, approved answers
  server.py         dashboard + API on 127.0.0.1 only
  static/index.html the dashboard
tests/              python -m pytest   (includes a fake form proving it never submits on its own)
```

## Privacy

Your data stays on your machine. It only goes to the application sites themselves, to the LLM
(which sees question labels plus your saved facts), and to Claude if enabled. API keys are masked
in every log line and never stored in SQLite. `profile.yaml`, `facts.md`, `.env` and `data/` are
git-ignored.

## Limits

- Works best on single-page forms: Greenhouse, Lever, Ashby.
- Workday and other account-based portals stop at the login page. Sign in in the window, then
  RETURN TO AGENT.
- Closing the app closes any paused windows. RETURN TO AGENT rebuilds them from scratch.
