"""One browser agent working one application, start to review checkpoint.

It never clicks a final Submit on its own: `prepare` stops at READY_FOR_REVIEW or
WAITING_FOR_INPUT, and only `submit` (called after Nate presses Approve) clicks it.
"""
import asyncio
import csv
import datetime as dt
import pathlib
import re
import shutil
from urllib.parse import urlparse

from playwright.async_api import Error as PWError
from playwright.async_api import TimeoutError as PWTimeout

from . import config, db, llm, logs
from .page_js import COMBO_OPTIONS, PAGE_STATE, READ, SCAN
from .profile import llm_map, norm, pick_option

TEXT_KINDS = {"text", "email", "tel", "url", "number", "textarea", "date", "month"}
YES = {"yes", "y", "true", "checked", "check", "agree", "i agree", "accept"}


class NeedsHuman(Exception):
    """Stop here and hand the application to Nate, with a reason."""


class Session:
    """Live browser state for one application. Kept open while it waits for Nate."""

    def __init__(self, app_id, context, page):
        self.app_id, self.context, self.page = app_id, context, page
        self.questions = {}  # label-key -> question dict, across every form page
        self.notes = []  # page-level warnings (CAPTCHA, missing buttons); reset on every pass
        self.page_no = 0
        self.started = False

    def uncertainties(self):
        out = [f"“{q['label'][:90]}”: {q['note']}" for q in self.questions.values() if q.get("note")]
        return out + self.notes

    async def close(self):
        try:
            await self.context.close()
        except PWError:
            pass


# ---------------------------------------------------------------- helpers
def find_job_files(company, position):
    """Ready\\<Company> - <Role>\\ from the career pipeline, if it exists."""
    ready = config.APPLICATIONS_DIR / "Ready"
    if not company or not ready.exists():
        return "", ""
    key = norm(company)
    best = None
    for d in ready.iterdir():
        if not d.is_dir():
            continue
        c, _, role = d.name.partition(" - ")
        if norm(c) != key:
            continue
        if best is None or (position and norm(role) and norm(role) in norm(position)):
            best = d
    if not best:
        return "", ""
    resume = next(iter(sorted(best.glob("*Resume*.pdf"))), "")
    letter = next(iter(sorted(best.glob("*Cover_Letter*.pdf"))), "")
    return str(resume), str(letter)


def guess_title(state, url):
    """(company, position) from the page title or the URL slug."""
    host = urlparse(url).netloc
    path = [p for p in urlparse(url).path.split("/") if p]
    slug = path[0] if path and any(h in host for h in ("greenhouse", "lever.co", "ashbyhq", "workable", "rippling")) else ""
    for t in (state.get("og_title"), state.get("title")):
        t = (t or "").strip()
        m = re.match(r"(?:job application for )?(.+?) (?:at|@) (.+?)(?: [|-].*)?$", t, re.I)
        if m:
            return m.group(2).strip(), m.group(1).strip()
        m = re.match(r"(.+?) [-|–] (.+)$", t)
        if m and slug and norm(m.group(1)).replace(" ", "") == norm(slug).replace(" ", "").replace("-", ""):
            return m.group(1).strip(), m.group(2).strip()
    company = state.get("og_site") or slug.replace("-", " ").title() or host.split(".")[-2].title()
    return company, state.get("h1") or (state.get("title") or "")[:120]


def qkey(field):
    return f"{field['kind']}|{norm(field['label'])}"


# ---------------------------------------------------------------- the agent
class Agent:
    def __init__(self, agent_id, profile):
        self.agent_id, self.profile = agent_id, profile

    def log(self, app_id, msg):
        logs.agent(self.agent_id, app_id, msg)

    # ---- page reading
    async def frames_state(self, page):
        states = []
        for fr in page.frames:
            try:
                s = await fr.evaluate(PAGE_STATE)
                s["frame"] = fr
                states.append(s)
            except PWError:
                continue
        return states

    async def scan(self, page):
        fields = []
        for i, fr in enumerate(page.frames):
            try:
                found = await fr.evaluate(SCAN, f"fr{i}_")
            except PWError:
                continue
            for f in found:
                f["frame"] = fr
                fields.append(f)
        return fields

    async def combobox_options(self, f):
        loc = f["frame"].locator(f["sel"])
        try:
            await loc.evaluate("el => el.scrollIntoView({block: 'center'})")
            await loc.click(timeout=3000)
            await asyncio.sleep(0.6)
            opts = await f["frame"].evaluate(COMBO_OPTIONS, f["sel"])
            await loc.press("Escape")
        except PWError:
            return []
        return [o for o in opts if o][:200]

    # ---- the main flow
    async def prepare(self, s: Session, app):
        page, app_id = s.page, s.app_id
        s.notes = []
        if "linkedin.com" in urlparse(app["url"]).netloc:
            raise NeedsHuman("LinkedIn link. The agents stay off LinkedIn: paste the company's own application URL instead.")
        if not s.started:
            self.log(app_id, f"Opening {urlparse(app['url']).netloc}...")
            await self.goto(page, app["url"])
            s.started = True
        states = await self.frames_state(page)
        top = states[0] if states else {}
        if top.get("interstitial"):
            raise NeedsHuman("The site is showing a bot-check page. Get past it in the window, then RETURN TO AGENT.")
        if top.get("closed") and not any(st.get("submit") for st in states):
            db.update(app_id, error="Posting looks closed")
            return "FAILED"

        if not app["company"] or not app["position"]:
            company, position = guess_title(top, app["url"])
            app["company"] = app["company"] or company
            app["position"] = app["position"] or position
            db.update(app_id, company=app["company"], position=app["position"])
        if not app["resume_path"]:
            resume, letter = find_job_files(app["company"], app["position"])
            app["resume_path"] = resume or self.profile.resume_path
            app["cover_letter_path"] = app["cover_letter_path"] or letter
            db.update(app_id, resume_path=app["resume_path"], cover_letter_path=app["cover_letter_path"])

        tried_apply = 0
        while s.page_no < config.MAX_FORM_PAGES:
            fields = await self.scan(page)
            states = await self.frames_state(page)
            if any(st.get("password") for st in states):
                raise NeedsHuman("This site wants a login or account. Sign in yourself in the window, then RETURN TO AGENT.")
            real = [f for f in fields if f["kind"] not in ("checkbox",)]
            if len(real) < 2:
                if tried_apply < 2 and await self.click_apply(page, states):
                    tried_apply += 1
                    continue
                raise NeedsHuman("Couldn't find the application form. Open it in the window, then RETURN TO AGENT.")

            s.page_no = max(s.page_no, 1)  # only a Next click moves to a new page; re-checks stay put
            self.log(app_id, f"Page {s.page_no}: found {len(fields)} fields")
            await self.fill_page(s, app, fields)
            states = await self.frames_state(page)
            pending = [q for q in s.questions.values() if q["status"] == "needs_input" and q["page"] == s.page_no]
            broken = [q for q in s.questions.values() if q["status"] == "mismatch" and q["page"] == s.page_no]
            if any(st.get("captcha_challenge") for st in states):
                s.notes.append("A CAPTCHA is on the page. Tick it yourself in the window before approving.")
            if pending or broken:
                n = len(pending) + len(broken)
                self.log(app_id, f"Need your answer for {n} question{'s' * (n != 1)}")
                return "WAITING_FOR_INPUT"
            if any(st.get("submit") for st in states):
                return "READY_FOR_REVIEW"
            nxt = next((st for st in states if st.get("next")), None)
            if not nxt:
                s.notes.append("No Submit or Next button found. Check the window.")
                return "WAITING_FOR_INPUT"
            before = page.url, len(fields)
            self.log(app_id, f"Clicking '{nxt['next']}' (not a submit button)")
            await nxt["frame"].locator('[data-aa-btn="next"]').click(timeout=8000)
            await self.settle(page)
            after = await self.scan(page)
            if page.url == before[0] and {qkey(f) for f in after} == {qkey(f) for f in fields}:
                raise NeedsHuman("Clicked Next but the page didn't move, probably a validation error. Check the window.")
            s.page_no += 1
        raise NeedsHuman(f"More than {config.MAX_FORM_PAGES} form pages. Finish it in the window.")

    async def goto(self, page, url):
        for attempt in range(3):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT_MS)
                await self.settle(page)
                return
            except PWTimeout:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt * 3)

    async def settle(self, page):
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass
        await asyncio.sleep(1.0)

    async def click_apply(self, page, states):
        st = next((x for x in states if x.get("apply")), None)
        if not st:
            return False
        if st.get("apply_href", "").startswith("http"):
            await self.goto(page, st["apply_href"])
            return True
        before = len(page.context.pages)
        await st["frame"].locator('[data-aa-btn="apply"]').click(timeout=8000)
        await self.settle(page)
        if len(page.context.pages) > before:  # opened in a new tab: follow it
            new = page.context.pages[-1]
            url = new.url
            await new.close()
            await self.goto(page, url)
        return True

    # ---- filling
    async def fill_page(self, s, app, fields):
        app_id, company = s.app_id, app["company"]
        plans, leftovers = {}, []
        for f in fields:
            f["page"] = s.page_no
            if f["kind"] == "combobox" and not f["options"]:
                f["options"] = await self.combobox_options(f)
            if f["kind"] == "file":
                plans[f["id"]] = self.plan_file(f, app)
                continue
            if f["value"] and f["kind"] not in ("checkbox",):
                plans[f["id"]] = (f["value"], "already filled", f["value"] if f["options"] else None)
                continue
            r = self.profile.resolve(f, company)
            if r:
                value, source, prefer = r
                option = None
                if f["kind"] == "combobox" and pick_option(value, f["options"], prefer) is None:
                    plans[f["id"]] = (value, source, None)  # search-as-you-type list: type it and pick
                    continue
                if f["options"] and f["kind"] != "checkboxes":
                    option = pick_option(value, f["options"], prefer)
                    if option is None:
                        leftovers.append(f)
                        continue
                plans[f["id"]] = (value, source, option)
            else:
                leftovers.append(f)

        reused = sum(1 for p in plans.values() if p and p[1].startswith("approved"))
        if reused:
            self.log(app_id, f"Found {reused} previously approved answer{'s' * (reused != 1)}")
        if leftovers and llm.available():
            self.log(app_id, f"Asking the LLM to match {len(leftovers)} leftover question(s) to your saved data")
            mapped = await llm_map(leftovers, self.profile, log=lambda m: self.log(app_id, m))
            for i, (value, source, option) in mapped.items():
                plans[leftovers[i]["id"]] = (value, source, option)

        filled = 0
        for f in fields:
            plan = plans.get(f["id"])
            q = {"label": f["label"], "kind": f["kind"], "required": f["required"], "options": f["options"][:60],
                 "maxlength": f.get("maxlength"),
                 "page": s.page_no, "value": "", "source": "", "status": ""}
            if not plan:
                q["status"] = "needs_input" if f["required"] else "optional_blank"
            else:
                value, source, option = plan
                q["source"] = source
                if source == "already filled":
                    q.update(value=value, status="filled")
                else:
                    ok = await self.fill(f, value, option, source)
                    q["value"] = option or value
                    q["status"] = "filled" if ok else ("needs_input" if f["required"] else "optional_blank")
                    filled += ok
                if source.startswith("llm:") and q["status"] == "filled":
                    q["status"] = "uncertain"
                    q["note"] = f"matched by the LLM from {source[4:]}; double-check"
                elif source.startswith("claude") and q["status"] == "filled":
                    q["status"] = "uncertain"
                    q["note"] = "written by Claude from your facts; read it before approving"
            q["id"] = f["id"]
            q["frame_url"] = f["frame"].url
            s.questions[qkey(f)] = q
        self.log(app_id, f"Filled {filled} fields")
        await self.verify(s, fields)

    def plan_file(self, f, app):
        label = f["label"].lower()
        if f["value"]:
            return (f["value"], "already filled", None)
        a = self.profile.approved_for(f["label"], app["company"])
        if a and pathlib.Path(a["answer"]).is_file():  # a file you (or Claude) picked for this question
            return (a["answer"], "claude:file" if a.get("source") == "claude" else f"approved:{a['id']}", None)
        if re.search(r"portfolio|work sample", label) and pathlib.Path(self.profile.portfolio_pdf).is_file():
            return (self.profile.portfolio_pdf, "portfolio", None)
        if re.search(r"cover", label):
            return (app["cover_letter_path"], "cover letter", None) if app["cover_letter_path"] else None
        if re.search(r"resume|résumé|\bcv\b|curriculum", label) or not re.search(r"transcript|portfolio|other|additional", label):
            return (app["resume_path"], "resume", None) if app["resume_path"] else None
        return None

    async def fill(self, f, value, option, source):
        fr, fid, kind = f["frame"], f["id"], f["kind"]
        loc = fr.locator(f["sel"])
        try:
            if kind == "file":
                if not pathlib.Path(value).exists():
                    return False
                await loc.set_input_files(value, timeout=8000)
            elif kind == "select":
                await loc.select_option(label=option, timeout=8000)
            elif kind in ("radio", "buttons"):
                k = f["options"].index(option)
                await self.press_option(fr, f"{fid}o{k}", kind)
            elif kind == "checkboxes":
                parts = [p.strip() for p in str(value).split(";") if p.strip()]
                picks = [pick_option(p, f["options"]) for p in parts]
                if not picks or None in picks:
                    return False
                for p in picks:
                    await self.press_option(fr, f"{fid}o{f['options'].index(p)}", kind)
            elif kind == "checkbox":
                if norm(value) not in YES or source.startswith("fact:"):
                    return False
                await loc.check(force=True, timeout=5000)
            elif kind == "combobox":
                return await self.fill_combobox(fr, f["sel"], option or value)
            else:
                if f.get("maxlength") and len(str(value)) > f["maxlength"]:
                    return False  # would be cut off mid-sentence; goes back for a shorter answer
                await loc.fill(str(value), timeout=8000)
            return True
        except (PWError, ValueError):
            return False

    async def press_option(self, fr, aid, kind):
        loc = fr.locator(f'[data-aa="{aid}"]')
        if kind == "buttons":
            await loc.click(timeout=5000)
            return
        try:
            await loc.check(timeout=4000)
        except PWError:
            await loc.evaluate("el => el.click()")

    async def fill_combobox(self, fr, sel, text):
        loc = fr.locator(sel)
        await loc.evaluate("el => el.scrollIntoView({block: 'center'})")
        full = text.split(",")[0].split("(")[0].strip()
        # Search boxes often only match a prefix, so try the full phrase, then its first word.
        for typed in dict.fromkeys([full, full.split()[0] if full else full]):
            await loc.click(timeout=5000)
            await loc.fill("")
            await loc.press_sequentially(typed, delay=30, timeout=10000)
            texts = []
            for _ in range(10):  # search-as-you-type lists load from the network; wait up to ~5s
                await asyncio.sleep(0.5)
                texts = await fr.evaluate(COMBO_OPTIONS, sel)
                if texts and not any(re.search(r"loading|searching", t, re.I) for t in texts):
                    break
            choice = pick_option(text, texts) or pick_option(full, texts)
            if not choice:
                starts = [t for t in texts if norm(t).startswith(norm(full))]
                choice = starts[0] if len(starts) == 1 or (starts and "," in text) else None
            if choice:
                await fr.locator(f'[data-aa-opt="{texts.index(choice)}"]').click(timeout=5000)
                await asyncio.sleep(0.3)
                return True
            await loc.press("Escape")
        await loc.fill("")
        return False

    async def verify(self, s, fields):
        """Read every value back off the page; anything that didn't stick goes to Nate."""
        by_frame = {}
        for f in fields:
            by_frame.setdefault(f["frame"], []).append(f)
        for fr, fs in by_frame.items():
            try:
                pairs = [[f["id"], f["sel"], pathlib.Path(str(s.questions[qkey(f)]["value"])).name if f["kind"] == "file" else ""]
                         for f in fs]
                actual = await fr.evaluate(READ, pairs)
            except PWError:
                continue
            for f in fs:
                q = s.questions[qkey(f)]
                if q["status"] not in ("filled", "uncertain"):
                    continue
                got = (actual.get(f["id"]) or "").strip()
                want = str(q["value"]).strip()
                if f["kind"] == "file":
                    ok = bool(got)
                elif f["kind"] == "checkbox":
                    ok = got == "checked"
                elif f["kind"] in ("combobox", "checkboxes", "buttons", "radio"):
                    ok = bool(got) and (norm(want) in norm(got) or norm(got) in norm(want) or f["kind"] == "combobox")
                else:
                    ok = got == want
                if not ok:
                    q["status"] = "mismatch"
                    q["note"] = f"didn't stick; page shows “{got[:80]}”"

    # ---- after Nate approves
    async def submit(self, s: Session, app):
        page = s.page
        states = await self.frames_state(page)
        st = next((x for x in states if x.get("submit")), None)
        if not st:
            raise NeedsHuman("Couldn't find the Submit button anymore. Check the window.")
        self.log(s.app_id, f"Approved by you. Clicking '{st['submit']}'")
        await st["frame"].locator('[data-aa-btn="submit"]').click(timeout=10000)
        await self.settle(page)
        await asyncio.sleep(3)
        states = await self.frames_state(page)
        if any(x.get("confirmation") for x in states):
            self.log(s.app_id, "Confirmation page seen. Submitted.")
            return "SUBMITTED"
        if any(x.get("captcha_challenge") for x in states):
            raise NeedsHuman("A CAPTCHA appeared after Submit. Solve it and finish in the window, then press Mark submitted.")
        raise NeedsHuman("Clicked Submit but saw no confirmation. Check the window; press Mark submitted if it went through.")


# ---------------------------------------------------------------- bookkeeping after a submit
def record_submission(app):
    """Same bookkeeping the chat pipeline does: applied-log row + move the job folder to Submitted."""
    try:
        path = pathlib.Path(config.APPLIED_LOG)
        if path.parent.exists():
            new = not path.exists()
            with open(path, "a", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(["company", "role", "location", "date_applied", "portal", "queue_id", "notes"])
                w.writerow([app["company"], app["position"], "", dt.date.today().isoformat(),
                            portal_name(app["url"]), f"apply-agents-{app['application_id']}",
                            "Submitted through apply-agents after review"])
    except OSError:
        pass
    resume = pathlib.Path(app.get("resume_path") or "")
    ready = config.APPLICATIONS_DIR / "Ready"
    if resume.parent.parent == ready and resume.parent.exists():
        dest = config.APPLICATIONS_DIR / "Submitted" / resume.parent.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.move(str(resume.parent), str(dest))


def portal_name(url):
    host = urlparse(url).netloc
    for k, v in (("greenhouse", "Greenhouse"), ("lever", "Lever"), ("ashby", "Ashby"), ("workday", "Workday"),
                 ("smartrecruiters", "SmartRecruiters"), ("workable", "Workable"), ("rippling", "Rippling")):
        if k in host:
            return v
    return host
