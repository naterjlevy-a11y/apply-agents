import pathlib

from apply_agents import claude_writer, config, db
from apply_agents.profile import Profile


def make_app(tmp_path):
    letter = tmp_path / "Letter.pdf"
    letter.write_bytes(b"%PDF")
    app_id = db.add_application("https://x/jobs/1", "Acme", "Intern", cover_letter_path=str(letter))
    db.update(app_id, questions=[
        {"label": "Why Acme? *", "kind": "textarea", "required": True, "options": [], "status": "needs_input"},
        {"label": "Pick a team *", "kind": "select", "required": True, "options": ["Propulsion", "Structures"], "status": "needs_input"},
        {"label": "Cover letter *", "kind": "file", "required": True, "options": [], "status": "needs_input"},
        {"label": "What is your GPA? *", "kind": "text", "required": True, "options": [], "status": "needs_input"},
        {"label": "Anything else?", "kind": "textarea", "required": False, "options": [], "status": "optional_blank"},
        {"label": "First name", "kind": "text", "required": True, "options": [], "status": "filled"},
    ])
    return db.get(app_id), letter


def test_claude_answers_are_checked_and_saved(tmp_path, profile, monkeypatch):
    monkeypatch.setattr(claude_writer, "Profile", lambda: profile)
    monkeypatch.setattr(config, "CLAUDE_FACTS", str(tmp_path / "none.md"))
    app, letter = make_app(tmp_path)
    seen = {}

    def fake_ask(prompt):
        seen["prompt"] = prompt
        return {"answers": [
            {"n": 0, "answer": "Because rockets — and I build things!"},
            {"n": 1, "answer": "Underwater basket weaving"},  # not an option: dropped
            {"n": 2, "answer": "cover_letter"},
            {"n": 3, "answer": "ASK_NATE"},
            {"n": 4, "answer": "ASK_NATE"},
        ]}

    written, left = claude_writer.answer(app, ask=fake_ask)
    assert "First name" not in seen["prompt"]  # only open questions go to Claude
    assert (written, left) == (2, 2)
    saved = {a["question"]: a for a in db.approved_answers()}
    assert saved["Why Acme? *"]["answer"] == "Because rockets, and I build things."
    assert saved["Why Acme? *"]["company"] == "Acme" and saved["Why Acme? *"]["source"] == "claude"
    assert saved["Cover letter *"]["answer"] == str(letter)
    assert "What is your GPA? *" not in saved and "Pick a team *" not in saved

    v, source, _ = Profile.resolve(profile, {"label": "Why Acme? *", "kind": "textarea"}, "Acme")
    assert source.startswith("claude:")
