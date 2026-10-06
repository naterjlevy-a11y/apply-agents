"""Local dashboard + JSON API. Binds to 127.0.0.1 only."""
import pathlib
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import config, db, llm
from .manager import Manager

STATIC = pathlib.Path(__file__).parent / "static"
manager = Manager()


@asynccontextmanager
async def lifespan(app):
    await manager.start()
    yield
    await manager.stop()


app = FastAPI(lifespan=lifespan)


class AddBody(BaseModel):
    text: str  # one per line: "url" or "Company | Role | url"


class AnswerBody(BaseModel):
    answers: dict[str, str] = {}
    remember: bool = True


class ResumeBody(BaseModel):
    path: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state():
    apps = db.all_applications()
    for a in apps:
        a["live_window"] = a["application_id"] in manager.sessions
    return {
        "counts": db.counts(),
        "applications": apps,
        "agents": manager.agents(),
        "config": {
            "max_agents": config.MAX_CONCURRENT_AGENTS,
            "provider": config.LLM_PROVIDER,
            "model": config.LLM_MODEL,
            "fallback": config.FALLBACK_PROVIDER or None,
            "llm_ready": llm.available(),
            "claude_auto": config.CLAUDE_AUTO_ANSWER,
        },
    }


@app.get("/api/logs")
def get_logs(app_id: int | None = None):
    return db.logs(app_id, limit=300 if app_id else 120)


@app.post("/api/add")
def add(body: AddBody):
    added, dupes = 0, 0
    for line in body.text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.search(r"https?://\S+", line)
        if not m:
            continue
        parts = [p.strip() for p in line[: m.start()].split("|") if p.strip()]
        company = parts[0] if parts else ""
        position = parts[1] if len(parts) > 1 else ""
        if manager.add(m.group(0), company, position):
            added += 1
        else:
            dupes += 1
    return {"added": added, "duplicates": dupes}


@app.post("/api/import-ready")
def import_ready():
    """Queue every job folder the career pipeline already prepared (Resumes\\Applications\\Ready)."""
    ready = config.APPLICATIONS_DIR / "Ready"
    added = 0
    if ready.exists():
        for d in sorted(ready.iterdir()):
            brief = d / "brief.md"
            if not brief.exists():
                continue
            text = brief.read_text(encoding="utf-8", errors="ignore")
            url = re.search(r"\*\*Posting:\*\*\s*(https?://\S+)", text)
            head = re.search(r"^#\s*(.+?)\s+[—-]\s+(.+)$", text, re.M)
            if not url:
                continue
            if head:
                company, position = head.group(1), head.group(2)
            else:
                company, _, position = d.name.partition(" - ")
            resume = next(iter(sorted(d.glob("*Resume*.pdf"))), "")
            letter = next(iter(sorted(d.glob("*Cover_Letter*.pdf"))), "")
            if db.add_application(url.group(1), company, position, str(resume), str(letter)):
                added += 1
    manager.wake.set()
    return {"added": added}


def _app(app_id):
    a = db.get(app_id)
    if not a:
        raise HTTPException(404, "no such application")
    return a


@app.post("/api/{app_id}/approve")
async def approve(app_id: int):
    _app(app_id)
    return {"message": await manager.approve(app_id)}


@app.post("/api/{app_id}/answer")
async def answer(app_id: int, body: AnswerBody):
    _app(app_id)
    await manager.answer_and_return(app_id, body.answers, body.remember)
    return {"message": "Back with the agent."}


@app.post("/api/{app_id}/edit")
async def edit(app_id: int):
    _app(app_id)
    ok = await manager.show(app_id)
    return {"message": "Brought its window to the front. Edit there, then RETURN TO AGENT or APPROVE." if ok
            else "No live window for this one. RETURN TO AGENT rebuilds it."}


@app.post("/api/{app_id}/skip")
async def skip(app_id: int):
    _app(app_id)
    await manager.skip(app_id)
    return {"message": "Skipped."}


@app.post("/api/{app_id}/retry")
async def retry(app_id: int):
    _app(app_id)
    await manager.retry(app_id)
    return {"message": "Queued again from scratch."}


@app.post("/api/{app_id}/ask-claude")
def ask_claude(app_id: int):
    a = _app(app_id)
    if a["status"] != "WAITING_FOR_INPUT":
        return {"message": "Claude only answers applications that are waiting on questions."}
    manager.ask_claude(app_id)
    return {"message": "Claude is on it."}


@app.post("/api/{app_id}/mark-submitted")
async def mark_submitted(app_id: int):
    _app(app_id)
    await manager.mark_submitted(app_id)
    return {"message": "Marked submitted."}


@app.post("/api/{app_id}/resume")
def set_resume(app_id: int, body: ResumeBody):
    _app(app_id)
    p = pathlib.Path(body.path.strip().strip('"'))
    if not p.exists():
        raise HTTPException(400, "That file doesn't exist.")
    db.update(app_id, resume_path=str(p))
    return {"message": "Résumé set. RETURN TO AGENT to use it."}


@app.get("/shots/{name}")
def shot(name: str):
    p = config.SHOTS_DIR / pathlib.Path(name).name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "no-store"})


@app.get("/api/answers")
def answers():
    return db.approved_answers()


@app.delete("/api/answers/{answer_id}")
def delete_answer(answer_id: int):
    db.delete_answer(answer_id)
    return {"ok": True}
