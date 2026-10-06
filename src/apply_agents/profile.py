"""Decides what goes in each form field, using only what Nate has written down.

Order for every field:
  1. answers he approved in the dashboard (same question, or the same question
     differing only in filler words)
  2. rules in profile.yaml (regex on the question label)
  3. built-in label rules that point at a fact (first name -> facts.first_name)
  4. one batched LLM call for whatever is left. The model may only POINT at a
     fact/answer id and, for choice questions, pick one of the listed options.
     It never writes text, so it cannot invent anything.
Anything still unresolved is a question for Nate.
"""
import re

import yaml

from . import config, db, llm

STOP = set(
    "a an the to of in on for and or is are be you your do does did have has will would "
    "please this that with at by as from any if our we us i me my currently".split()
)

# Generic label -> fact key. First match wins, so specific patterns come first.
LABEL_RULES = [
    (r"\blegal\b.*\bfirst\b|\bfirst\b.*\blegal\b", "legal_first_name"),
    (r"\blegal\b.*\b(last|family|sur)\b|\b(last|family)\b.*\blegal\b", "legal_last_name"),
    (r"\blegal\b.*\bname\b", "legal_full_name"),
    (r"\bpreferred\b.*\bname\b|\bnickname\b", "preferred_name"),
    (r"\bfirst\b.*\bname\b|\bgiven name\b|^first$", "first_name"),
    (r"\blast\b.*\bname\b|\bfamily name\b|\bsurname\b|^last$", "last_name"),
    (r"^(full )?name$|\bfull name\b|^your name$", "full_name"),
    (r"\be-?mail\b", "email"),
    (r"\bphone\b|\bmobile\b|\bcell\b", "phone"),
    (r"\blinkedin\b", "linkedin"),
    (r"\bgithub\b", "github"),
    (r"\bportfolio\b|\bpersonal (web)?site\b|^website$|\bwebsite url\b", "portfolio"),
    (r"\b(school|university|college|institution)\b(?!.*\b(year|gpa|grade)\b)", "school"),
    (r"\b(major|discipline|field of study|area of study|program of study)\b", "major"),
    (r"\bdegree\b(?!.*\b(year|date)\b)", "degree"),
    (r"\bgraduation year\b|\bgrad(uation)? year\b|\byear of graduation\b", "grad_year"),
    (r"\b(expected |anticipated )?graduation (date|month|term)\b|^(expected )?graduation$|\bwhen (do|will) you graduate\b", "grad_date"),
    (r"\bcurrent (city|location)\b|\bwhere are you (currently )?(located|based)\b|^location( city)?$", "location"),
    (r"^city$", "city"),
    (r"^(state|province)( or (province|state))?$", "province"),
    (r"^country$|\bcountry of residence\b", "country"),
]


def norm(text):
    t = (text or "").lower().replace("*", " ")
    t = re.sub(r"\(required\)|\brequired\b|\(optional\)", " ", t)
    t = re.sub(r"[^a-z0-9+#.]+", " ", t)
    t = re.sub(r"(?<![a-z0-9])\.|\.(?![a-z0-9])", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def content_words(text):
    return frozenset(w for w in norm(text).split() if w not in STOP)


class Profile:
    def __init__(self, path=None):
        path = path or config.PROFILE_PATH
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        self.facts = {k: str(v).strip() for k, v in (raw.get("facts") or {}).items() if v not in (None, "")}
        self.resume_path = str(raw.get("resume_path") or "")
        self.portfolio_pdf = str(raw.get("portfolio_pdf_path") or "")
        self.rules = []
        for r in raw.get("answers") or []:
            value = self.facts.get(r["fact"]) if r.get("fact") else r.get("answer")
            if value in (None, ""):
                continue
            self.rules.append({
                "id": r["id"],
                "match": [re.compile(p, re.I) for p in r.get("match", [])],
                "value": str(value),
                "prefer": [re.compile(p, re.I) for p in r.get("prefer", [])],
                "check": r.get("check"),  # for a lone checkbox: true = tick it
            })

    # ---------- lookup ----------
    def approved_for(self, label, company=""):
        n, words = norm(label), content_words(label)
        best = None
        for a in db.approved_answers():
            if a["company"] and a["company"].lower() != (company or "").lower():
                continue
            same = a["norm"] == n or (len(words) >= 2 and content_words(a["question"]) == words)
            if same and (best is None or a["company"]):  # company-specific beats general
                best = a
        return best

    def resolve(self, field, company=""):
        """Returns (value, source, prefer_patterns) or None."""
        label = field["label"]
        nl = norm(label)
        if re.match(r"(if (yes|no|so|other|you (selected|answered|chose|checked))\b|if [a-z ]{0,40} other\b|please specify|other\b)", nl):
            return None  # follow-up boxes ("If other, explain") only matter for answers he didn't give
        a = self.approved_for(label, company)
        if a:
            kind = "claude" if a.get("source") == "claude" else "approved"
            return a["answer"], f"{kind}:{a['id']}", []
        free_text = field.get("kind") in ("text", "textarea", "email", "tel", "url", "number")
        for r in self.rules:
            if any(p.search(label) for p in r["match"]):
                if free_text and r["value"] in ("Yes", "No") and "?" not in label:
                    continue  # "Please provide your address (... relocation ...)" is not a yes/no question
                return r["value"], f"rule:{r['id']}", r["prefer"]
        if len(nl) > 80:  # a long question is never just "first name"
            return None
        for pattern, key in LABEL_RULES:
            if re.search(pattern, nl):
                if key in self.facts:
                    return self.facts[key], f"fact:{key}", []
                return None  # we know what it asks, but Nate hasn't given the fact: ask him
        return None

    def sources(self):
        """Everything the LLM may point at, keyed by id."""
        out = {f"fact:{k}": v for k, v in self.facts.items()}
        out.update({f"rule:{r['id']}": r["value"] for r in self.rules})
        out.update({f"approved:{a['id']}": a["answer"] for a in db.approved_answers() if not a["company"]})
        return out


# ---------- choosing an option ----------
def pick_option(value, options, prefer=()):
    """Maps an approved answer onto one of a question's options, or None if unsure."""
    if not options:
        return None
    for p in prefer:
        for o in options:
            if p.search(o):
                return o
    nv = norm(value)
    for o in options:
        if norm(o) == nv:
            return o
    def within(a, b):  # whole-word containment, so "no" doesn't match "not"
        return bool(a) and re.search(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", b) is not None

    hits = [o for o in options if within(nv, norm(o)) or within(norm(o), nv)]
    if len(hits) == 1:
        return hits[0]
    if nv in ("yes", "no"):
        starts = [o for o in options if norm(o).split()[:1] == [nv]]
        if len(starts) == 1:
            return starts[0]
    return None


LLM_SYSTEM = """You map job-application form questions to an applicant's existing, pre-approved data.
You NEVER write new text. For each question you either point at ONE source id from the SOURCES
list whose value genuinely answers that question, or you answer UNKNOWN.
Rules:
- Only use a source if it answers exactly what is asked. When in doubt, UNKNOWN.
- Never answer essay, motivation, "why us", behavioural, salary, references, or company-specific
  history questions unless a source answers that exact question.
- For questions with OPTIONS, also return "option": the exact text of the one option that matches
  the source's value. If none clearly matches, UNKNOWN.
Reply with JSON only: {"answers": [{"n": <question number>, "source": "<id>" | "UNKNOWN", "option": "<exact option text or omit>"}]}"""


async def llm_map(fields, profile, log=print):
    """One call for every leftover question on the page. Returns {field_index: (value, source, option)}."""
    if not fields or not llm.available():
        return {}
    sources = profile.sources()
    src_lines = "\n".join(f"- {k}: {v[:160]}" for k, v in sources.items())
    q_lines = []
    for i, f in enumerate(fields):
        opts = f" OPTIONS: {f['options'][:40]}" if f.get("options") else ""
        q_lines.append(f"{i}. [{f['kind']}] {f['label'][:300]}{opts}")
    msg = [
        {"role": "system", "content": LLM_SYSTEM},
        {"role": "user", "content": "SOURCES:\n" + src_lines + "\n\nQUESTIONS:\n" + "\n".join(q_lines)},
    ]
    try:
        reply = await llm.chat(msg, log=log)
        data = llm.parse_json(reply)
    except llm.LLMUnavailable as e:
        log(f"LLM unavailable ({e}); leftover questions go to you")
        return {}
    except (ValueError, KeyError) as e:
        log(f"LLM reply wasn't usable JSON ({type(e).__name__}); leftover questions go to you")
        return {}
    out = {}
    for a in data.get("answers", []) if isinstance(data, dict) else []:
        try:
            i = int(a.get("n"))
        except (TypeError, ValueError):
            continue
        src = a.get("source")
        if not (0 <= i < len(fields)) or src not in sources:
            continue  # unknown, or the model pointed at something that doesn't exist
        f, value, option = fields[i], sources[src], a.get("option")
        if f.get("options"):
            if option not in f["options"]:
                continue  # must be one of the real options, verbatim
            out[i] = (value, f"llm:{src}", option)
        elif f["kind"] in ("text", "textarea", "email", "tel", "url", "number", "combobox"):
            out[i] = (value, f"llm:{src}", None)
    return out
