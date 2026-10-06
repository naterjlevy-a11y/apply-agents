"""Runs N agents at once over the queue and holds paused applications open for review.

A worker slot frees as soon as its application reaches review or needs input, so
the next one starts right away. The paused browser window stays open until Nate
approves, skips, or sends it back.
"""
import asyncio
import re
import traceback

from playwright.async_api import Error as PWError
from playwright.async_api import async_playwright

from . import config, db, logs
from . import claude_writer
from .agent import Agent, NeedsHuman, Session, record_submission
from .profile import Profile, norm


class Manager:
    def __init__(self):
        self.sessions = {}  # application_id -> Session kept open while paused
        self.claimed = set()
        self.wake = asyncio.Event()
        self.browser = None
        self.pw = None
        self.workers = []
        self.busy = {}  # agent_id -> application_id
        self.browser_lock = asyncio.Lock()

    # ---------------------------------------------------------------- lifecycle
    async def start(self):
        db.reset_interrupted()
        self.pw = await async_playwright().start()
        await self.ensure_browser()
        for i in range(config.MAX_CONCURRENT_AGENTS):
            self.workers.append(asyncio.create_task(self.worker(f"Agent {i + 1:02d}")))
        if config.CLAUDE_AUTO_ANSWER:
            self.workers.append(asyncio.create_task(self.claude_loop()))
        logs.agent("", 0, f"Started {config.MAX_CONCURRENT_AGENTS} agents")
        self.wake.set()

    async def stop(self):
        for t in self.workers:
            t.cancel()
        for s in list(self.sessions.values()):
            await s.close()
        if self.browser:
            try:
                await self.browser.close()
            except PWError:
                pass
        if self.pw:
            await self.pw.stop()

    async def ensure_browser(self):
        async with self.browser_lock:
            if self.browser and self.browser.is_connected():
                return
            if self.browser:
                logs.agent("", 0, "Browser crashed; relaunching. Paused windows were lost; RETURN TO AGENT restarts them.")
                for app_id in list(self.sessions):
                    self.sessions.pop(app_id, None)
            self.browser = await self.pw.chromium.launch(headless=config.HEADLESS)

    async def new_session(self, app_id):
        await self.ensure_browser()
        # each application gets its own context: separate cookies, storage and window
        ctx = await self.browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=False)
        page = await ctx.new_page()
        page.set_default_timeout(config.PAGE_TIMEOUT_MS)
        return Session(app_id, ctx, page)

    # ---------------------------------------------------------------- the worker loop
    async def worker(self, agent_id):
        while True:
            app_id = db.next_queued(exclude=self.claimed)
            if app_id is None:
                self.wake.clear()
                await self.wake.wait()
                continue
            self.claimed.add(app_id)  # claimed before any await, so no two workers take it
            self.busy[agent_id] = app_id
            try:
                await self.run_one(agent_id, app_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # one broken application never stops the others
                logs.agent(agent_id, app_id, f"Unexpected error: {type(e).__name__}: {str(e)[:200]}")
                logs.logger().debug(traceback.format_exc())
                db.update(app_id, status="FAILED", error=f"{type(e).__name__}: {str(e)[:300]}")
                await self.drop_session(app_id)
            finally:
                self.claimed.discard(app_id)
                self.busy.pop(agent_id, None)
                self.wake.set()

    async def run_one(self, agent_id, app_id):
        app = db.get(app_id)
        if not app or app["status"] != "QUEUED":
            return
        profile = Profile()  # re-read each time so profile.yaml edits apply without a restart
        agent = Agent(agent_id, profile)
        db.update(app_id, status="RUNNING", agent_id=agent_id, error="", started_at=app["started_at"] or db.now())
        s = self.sessions.get(app_id)
        if s is None or s.page.is_closed():
            s = await self.new_session(app_id)
            self.sessions[app_id] = s
        try:
            status = await asyncio.wait_for(agent.prepare(s, app), timeout=600)
            error = db.get(app_id)["error"]
        except NeedsHuman as e:
            status, error = "WAITING_FOR_INPUT", str(e)
            s.notes.append(str(e))
        except asyncio.TimeoutError:
            status, error = "WAITING_FOR_INPUT", "Took more than 10 minutes. Check the window."
            s.notes.append(error)
        except PWError as e:
            msg = str(e).split("\n")[0][:200]
            if not self.browser.is_connected() or "closed" in msg.lower():
                status, error = "FAILED", f"Browser closed or crashed: {msg}"
            else:
                status, error = "WAITING_FOR_INPUT", f"Website problem: {msg}"
                s.notes.append(error)
        await self.snapshot(s, app_id)
        db.update(app_id, status=status, error=error, questions=list(s.questions.values()),
                  answers={q["label"]: q["value"] for q in s.questions.values() if q["value"]},
                  uncertainties=s.uncertainties(), review_status="pending" if status == "READY_FOR_REVIEW" else "")
        logs.agent(agent_id, app_id, f"Status: {status}" + (f" ({error})" if error else ""))
        if status in ("FAILED", "SKIPPED") or "linkedin.com" in app["url"]:
            await self.drop_session(app_id)

    async def snapshot(self, s, app_id):
        try:
            path = config.SHOTS_DIR / f"{app_id}.png"
            config.SHOTS_DIR.mkdir(parents=True, exist_ok=True)
            await s.page.screenshot(path=str(path), full_page=True, timeout=15000)
            db.update(app_id, screenshot=path.name, current_url=s.page.url)
        except PWError:
            pass

    async def drop_session(self, app_id):
        s = self.sessions.pop(app_id, None)
        if s:
            await s.close()

    # ---------------------------------------------------------------- Claude answers the hard questions
    async def claude_loop(self):
        """One at a time: stuck applications with open questions go to Claude, then back to an agent."""
        while True:
            app = next((a for a in db.all_applications()[::-1]
                        if a["status"] == "WAITING_FOR_INPUT" and a["claude_status"] in ("", "requested")
                        and not a["error"] and claude_writer.pending_questions(a)), None)
            if app is None:
                await asyncio.sleep(3)
                continue
            app_id = app["application_id"]
            n = len(claude_writer.pending_questions(app))
            db.update(app_id, claude_status="running")
            logs.agent("Claude", app_id, f"Writing answers for {n} question(s)")
            try:
                written, left = await asyncio.to_thread(claude_writer.answer, app)
            except Exception as e:  # Claude being down never blocks the agents
                db.update(app_id, claude_status="failed")
                logs.agent("Claude", app_id, f"Couldn't answer ({type(e).__name__}: {str(e)[:150]}); they stay with you")
                continue
            db.update(app_id, claude_status="done")
            logs.agent("Claude", app_id, f"Wrote {written}; {left} required question(s) left for you")
            if written and db.get(app_id)["status"] == "WAITING_FOR_INPUT":
                db.update(app_id, status="QUEUED")
                self.wake.set()

    def ask_claude(self, app_id):
        db.update(app_id, claude_status="requested", error="")

    # ---------------------------------------------------------------- dashboard actions
    def add(self, url, company="", position="", resume_path=""):
        new = db.add_application(url.strip(), company.strip(), position.strip(), resume_path.strip())
        self.wake.set()
        return new

    async def approve(self, app_id):
        app = db.get(app_id)
        s = self.sessions.get(app_id)
        if not app or app["status"] != "READY_FOR_REVIEW":
            return "Only applications in READY_FOR_REVIEW can be approved."
        if s is None or s.page.is_closed():
            return "Its browser window is gone (restart or crash). Press RETURN TO AGENT to rebuild it first."
        db.update(app_id, review_status="approved")
        agent = Agent(app["agent_id"] or "Agent", Profile())
        try:
            status = await agent.submit(s, app)
        except NeedsHuman as e:
            s.notes.append(str(e))
            await self.snapshot(s, app_id)
            db.update(app_id, status="WAITING_FOR_INPUT", error=str(e), submission_status="unconfirmed",
                      uncertainties=s.uncertainties())
            return str(e)
        except PWError as e:
            db.update(app_id, status="WAITING_FOR_INPUT", error=f"Submit click failed: {str(e)[:150]}")
            return "Submit click failed; check the window."
        await self.snapshot(s, app_id)
        db.update(app_id, status=status, submission_status="confirmed")
        record_submission(db.get(app_id))
        await self.drop_session(app_id)
        return "Submitted."

    async def mark_submitted(self, app_id):
        db.update(app_id, status="SUBMITTED", submission_status="marked by you", review_status="approved")
        logs.agent("", app_id, "Marked submitted by you")
        record_submission(db.get(app_id))
        await self.drop_session(app_id)

    async def answer_and_return(self, app_id, answers, remember=True):
        """Nate's answers become approved answers, then the agent picks the application back up."""
        app = db.get(app_id)
        for label, value in answers.items():
            value = (value or "").strip()
            if not value:
                continue
            company = app["company"] if looks_company_specific(label, app["company"]) else ""
            if remember:
                db.save_answer(label, norm(label), value, company)
        db.update(app_id, status="QUEUED", error="", review_status="returned")
        logs.agent("", app_id, f"Returned to agent with {len([v for v in answers.values() if v])} new answer(s)")
        self.wake.set()

    async def skip(self, app_id):
        db.update(app_id, status="SKIPPED", review_status="skipped")
        logs.agent("", app_id, "Skipped by you")
        await self.drop_session(app_id)

    async def retry(self, app_id):
        await self.drop_session(app_id)
        db.update(app_id, status="QUEUED", error="", questions=[], answers={}, uncertainties=[], claude_status="")
        self.wake.set()

    async def show(self, app_id):
        s = self.sessions.get(app_id)
        if s and not s.page.is_closed():
            await s.page.bring_to_front()
            return True
        return False

    def agents(self):
        return [{"agent": f"Agent {i + 1:02d}", "application_id": self.busy.get(f"Agent {i + 1:02d}")}
                for i in range(config.MAX_CONCURRENT_AGENTS)]


def looks_company_specific(label, company):
    l = label.lower()
    return bool(company and (company.lower() in l or re.search(r"\b(why|this (role|position|company)|our (team|company))\b", l)))
