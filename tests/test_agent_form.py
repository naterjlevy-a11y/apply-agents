"""End-to-end on a local fake application: fills, verifies, stops before Submit."""
import pathlib

import pytest
from playwright.async_api import async_playwright

from apply_agents import db, llm
from apply_agents.agent import Agent, Session
from apply_agents.profile import norm

FORM = (pathlib.Path(__file__).parent / "fixtures" / "form.html").as_uri()


@pytest.fixture
async def page():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        ctx = await b.new_context()
        yield ctx, await ctx.new_page()
        await b.close()


async def test_fills_and_stops_for_human(page, profile, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: False)
    ctx, pg = page
    app_id = db.add_application(FORM)
    app = db.get(app_id)
    s = Session(app_id, ctx, pg)
    agent = Agent("Agent 01", profile)
    status = await agent.prepare(s, app)

    assert app["company"] == "Acme Rockets" and "Mechanical" in app["position"]
    assert status == "WAITING_FOR_INPUT"  # the "why us" essay is required and has no approved answer
    q = {v["label"].rstrip(" *"): v for v in s.questions.values()}
    assert q["First Name"]["value"] == "Alex" and q["First Name"]["status"] == "filled"
    assert q["Last Name"]["value"] == "Sample"
    assert q["Will you now or in the future require sponsorship for employment visa status?"]["value"] == "No"
    assert q["Are you legally authorized to work in the United States?"]["value"] == "Yes"
    assert q["Veteran Status"]["value"] == "I don't wish to answer"
    assert q["Why do you want to work at Acme Rockets?"]["status"] == "needs_input"
    assert q["GPA"]["value"] == "N/A"  # his standing answer until McGill gives him a CGPA
    assert any(v["kind"] == "file" and v["status"] == "filled" for v in s.questions.values())
    assert await pg.title() != "SUBMITTED"

    # The applicant answers; the agent picks it back up and reaches the review checkpoint, still not submitted.
    label = next(v["label"] for v in s.questions.values() if v["label"].startswith("Why"))
    db.save_answer(label, norm(label), "Because I like rockets.", "Acme Rockets")
    status = await agent.prepare(s, app)
    assert status == "READY_FOR_REVIEW"
    assert await pg.title() != "SUBMITTED"
    assert await pg.locator("#fn").input_value() == "Alex"
    assert s.page_no == 1  # a re-check of the same page isn't counted as a new page

    # Only an explicit approval clicks Submit.
    assert await agent.submit(s, app) == "SUBMITTED"
    assert await pg.title() == "SUBMITTED"
