"""Claude answers the questions the agents can't: essays, "why us", which PDF to upload.

Runs headless Claude Code (`claude -p`) the same way the cover-letter script does: it may
only search/fetch the web, and it may only state facts from cover_facts.md and profile.yaml.
Its answers are saved as company-specific answers marked source='claude'. The agent then types
them in and flags each one "written by Claude", so Nate reads them before approving.
Anything Claude can't back with a fact (GPA, referrals, consent boxes...) stays with Nate.
"""
import json
import pathlib
import re
import subprocess
import tempfile

from . import config, db
from .profile import Profile, norm, pick_option

ASK = "ASK_NATE"

RULES = """Rules, all of them strict:
- State only facts from FACTS and PROFILE below. Never invent experience, numbers, grades, dates, skills or people.
- Only the good parts: no bugs, failures or "what broke" stories about the applicant's own work. For work under NDA, stay at the detail written.
- Write as the applicant, first person, plain and specific, like a sharp engineering student. No buzzwords.
- No em dashes or en dashes. No "not just X but Y". No exclamation marks.
- Length: follow the question ("a few sentences" = 2 to 4 sentences). Otherwise 60 to 150 words.
- For "why this company" questions you may use WebFetch/WebSearch on the posting or company site for ONE concrete detail.
- Questions with OPTIONS: answer with one option copied exactly, and only if the facts support it.
- FILE questions: answer with one key from FILES, or ASK_NATE.
- If a question shows MAX N CHARACTERS, the answer must be shorter than N characters, spaces included.
- Answer ASK_NATE when the facts don't cover it: GPA, referrals or names, salary, legal name, dates not in the facts,
  consent or legal acknowledgements (SMS, privacy, certifications, signatures), whether he has interviewed there before,
  his preferences between offices, locations, teams or start dates, and anything you'd have to guess."""


def pending_questions(app):
    out = []
    for q in app["questions"]:
        if q["status"] == "needs_input" or (q["status"] == "optional_blank" and q["kind"] == "textarea"):
            out.append(q)
    return out


def files_for(app, profile):
    files = {
        "resume": app.get("resume_path") or profile.resume_path,
        "cover_letter": app.get("cover_letter_path") or "",
        "portfolio": profile.portfolio_pdf,
    }
    return {k: v for k, v in files.items() if v and pathlib.Path(v).is_file()}


def build_prompt(app, questions, profile, files):
    facts = pathlib.Path(config.CLAUDE_FACTS).read_text(encoding="utf-8") if pathlib.Path(config.CLAUDE_FACTS).exists() else ""
    brief = ""
    resume = pathlib.Path(app.get("resume_path") or "")
    if (resume.parent / "brief.md").exists():
        brief = (resume.parent / "brief.md").read_text(encoding="utf-8", errors="ignore")[:1500]
    profile_lines = [f"- {k}: {v}" for k, v in profile.facts.items()]
    profile_lines += [f"- {r['id']}: {r['value']}" for r in profile.rules]
    q_lines = []
    for i, q in enumerate(questions):
        opts = f"\n   OPTIONS: {json.dumps(q['options'][:60])}" if q.get("options") else ""
        req = "required" if q["required"] else "optional (answer only if it helps him)"
        limit = f" MAX {q['maxlength']} CHARACTERS" if q.get("maxlength") else ""
        q_lines.append(f"{i}. [{q['kind']}, {req}{limit}] {q['label']}{opts}")
    return "\n".join([
        f"You are filling in the applicant's internship application: {app['company']}, {app['position']}.",
        f"Posting: {app['url']}",
        "", RULES, "",
        "FACTS (the only source for claims about him):", facts, "",
        "PROFILE:", *profile_lines, "",
        *(["JOB BRIEF (how he found it, notes):", brief, ""] if brief else []),
        "FILES you can choose for upload questions: " + ", ".join(files) if files else "FILES: none",
        "", "QUESTIONS:", *q_lines, "",
        'Reply with ONLY JSON, no code fence: {"answers": [{"n": <number>, "answer": "<text, exact option, file key, or ASK_NATE>"}]}',
    ])


def ask_claude(prompt):
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(
            [config.CLAUDE_PATH, "-p", "--output-format", "json",
             "--allowedTools", "WebSearch", "WebFetch",
             "--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit", "Agent"],
            input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=tmp, timeout=420)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[-400:])
    result = json.loads(r.stdout).get("result", "")
    m = re.search(r"\{.*\}", result, re.S)
    if not m:
        raise RuntimeError("Claude didn't return JSON")
    return json.loads(m.group(0))


def clean(text):
    text = re.sub(r"\s*[—–]\s*", ", ", text.strip())
    return text.replace("!", ".")


def answer(app, ask=ask_claude):
    """Returns (number written, number left for Nate). Saves answers; doesn't touch the browser."""
    profile = Profile()
    questions = pending_questions(app)
    if not questions:
        return 0, 0
    files = files_for(app, profile)
    data = ask(build_prompt(app, questions, profile, files))
    written, req_done = 0, 0
    for a in data.get("answers", []):
        try:
            q = questions[int(a.get("n"))]
        except (TypeError, ValueError, IndexError):
            continue
        text = str(a.get("answer") or "").strip()
        if not text or text == ASK:
            continue
        if q["kind"] == "file":
            if text not in files:
                continue
            text = files[text]
        elif q.get("options"):
            text = pick_option(text, q["options"])
            if text is None:
                continue  # not one of the real options
        else:
            text = clean(text)
            if q.get("maxlength") and len(text) > q["maxlength"]:
                continue  # too long for the box; Nate trims it instead of the site cutting it off
        db.save_answer(q["label"], norm(q["label"]), text, app["company"], source="claude")
        written += 1
        req_done += q["required"]
    return written, sum(1 for q in questions if q["required"]) - req_done
