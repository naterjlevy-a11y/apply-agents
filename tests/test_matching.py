import re

from apply_agents import db, llm
from apply_agents.profile import llm_map, norm, pick_option


def field(label, kind="text", options=(), required=True):
    return {"label": label, "kind": kind, "options": list(options), "required": required}


def test_facts_and_rules(profile):
    assert profile.resolve(field("First Name *"))[0] == "Alex"
    assert profile.resolve(field("Email"))[0] == "alex.sample@example.com"
    assert profile.resolve(field("LinkedIn Profile"))[0].endswith("/alexsample")
    q = "Will you now or in the future require sponsorship for employment visa status?"
    assert profile.resolve(field(q))[0] == "No"
    q = "Are you legally authorized to work in the United States without sponsorship?"
    assert profile.resolve(field(q))[0] == "Yes"


def test_unknown_or_missing_fact_goes_to_human(profile):
    assert profile.resolve(field("Why do you want to work here?", "textarea")) is None
    assert profile.resolve(field("Preferred pronouns")) is None
    assert profile.resolve(field("What is your favourite food?")) is None
    assert profile.resolve(field("Legal full name"))[0] == "Alex Jordan Sample"
    assert profile.resolve(field("GPA"))[0] == "N/A"


def test_pick_option():
    opts = ["I am a protected veteran", "I am not a protected veteran", "I don't wish to answer"]
    assert pick_option("x", opts, [re.compile("not a (protected )?veteran", re.I)]) == opts[1]
    assert pick_option("Yes", ["Yes", "No"]) == "Yes"
    assert pick_option("No", ["No, I don't have a disability", "Yes, I have one"]) == "No, I don't have a disability"
    assert pick_option("Purple", ["Yes", "No"]) is None


def test_approved_answers_need_the_same_question(profile):
    q = "Are you legally authorized to work in Canada?"
    db.save_answer(q, norm(q), "Yes")
    assert profile.approved_for("Are you legally authorized to work in Canada? *")
    assert profile.approved_for("Are you legally authorised to work in the US?") is None
    assert profile.approved_for("Are you authorized to work in Canada") is None  # "legally" dropped: not identical


def test_filler_words_dont_matter(profile):
    q = "Do you have experience with SolidWorks?"
    db.save_answer(q, norm(q), "Yes")
    assert profile.approved_for("Have you any experience with SolidWorks") is not None
    assert profile.approved_for("Do you have experience with CATIA?") is None


def test_company_specific_answer_stays_with_company(profile):
    q = "Why Acme?"
    db.save_answer(q, norm(q), "Because rockets", "Acme")
    assert profile.approved_for(q, "Acme")
    assert profile.approved_for(q, "Other Co") is None


async def test_llm_can_only_point_at_real_sources(profile, monkeypatch):
    fields = [field("Your e-mail address for contact"), field("Favourite colour"),
              field("Willing to move?", "select", ["Yes", "No"]), field("Made up", "select", ["A", "B"])]
    reply = ('```json\n{"answers": [{"n": 0, "source": "fact:email"}, {"n": 1, "source": "fact:invented"},'
             ' {"n": 2, "source": "rule:relocate", "option": "Yes"}, {"n": 3, "source": "rule:relocate", "option": "C"}]}\n```')

    async def fake_chat(messages, log=print, max_tokens=0):
        return reply

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "chat", fake_chat)
    out = await llm_map(fields, profile)
    assert out[0][0] == "alex.sample@example.com"
    assert 1 not in out  # pointed at a source that doesn't exist
    assert out[2][2] == "Yes"
    assert 3 not in out  # option not on the form


async def test_no_llm_means_ask_human(profile, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: False)
    assert await llm_map([field("Anything")], profile) == {}


def test_mistakes_from_live_run_stay_fixed(profile):
    """Each label below was answered wrong on a real form on 2026-10-04."""
    muon = "Please provide your permanent address (city, state, zip code). We use this information to determine eligibility for relocation assistance"
    assert profile.resolve(field(muon, "text"))[0].startswith("123 Example Street")  # not "Yes"
    rocket = "Preferred Internship/Co-Op Duration: Confirm the number of consecutive weeks you are available to work full-time and in-person.*"
    assert profile.resolve(field(rocket, "combobox", ["10 weeks", "12 weeks"])) is None
    assert profile.resolve(field("If Citizenship Status Other, please explain:", "text")) is None
    assert profile.resolve(field("Please specify", "text")) is None
    # the real questions still work
    assert profile.resolve(field("Are you authorized to work in the United States?*", "combobox"))[0] == "Yes"
    assert profile.resolve(field("If hired, are you able to relocate on your own?", "combobox"))[0] == "Yes"
