import asyncio

import pytest
from openai import APIStatusError

from apply_agents import config, llm, logs


class FakeCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def create(self, **kw):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, int):
            e = APIStatusError.__new__(APIStatusError)
            Exception.__init__(e, "err")
            e.status_code = step
            e.response = type("Resp", (), {"headers": {"retry-after": "0"}})()
            raise e
        msg = type("M", (), {"content": step})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()


def provider(name, script):
    p = llm.Provider.__new__(llm.Provider)
    p.name, p.model, p.ok = name, "m", True
    p.bucket = llm.RateBucket(1000)
    comp = FakeCompletions(script)
    p.client = type("Cl", (), {"chat": type("Ch", (), {"completions": comp})()})()
    return p, comp


@pytest.fixture
def chain(monkeypatch):
    def set_(*ps):
        monkeypatch.setattr(llm, "_providers", list(ps))
        monkeypatch.setattr(llm, "_global", asyncio.Semaphore(2))
    return set_


async def test_backoff_then_success(chain):
    p, comp = provider("nvidia", [429, 503, "ok"])
    chain(p)
    assert await llm.chat([], log=lambda m: None) == "ok"
    assert comp.calls == 3


async def test_fallback_after_retries(chain, monkeypatch):
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 1)
    a, _ = provider("nvidia", [429, 429])
    b, _ = provider("openrouter", ["from fallback"])
    chain(a, b)
    assert await llm.chat([], log=lambda m: None) == "from fallback"


async def test_all_down_raises(chain, monkeypatch):
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 0)
    a, _ = provider("nvidia", [401])
    chain(a)
    with pytest.raises(llm.LLMUnavailable):
        await llm.chat([], log=lambda m: None)


async def test_rate_bucket_spaces_calls(monkeypatch):
    b = llm.RateBucket(2)
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        b.stamps = []  # pretend a minute passed

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    for _ in range(3):
        await b.take()
    assert len(slept) == 1 and slept[0] > 50


def test_parse_json():
    assert llm.parse_json('sure! ```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.parse_json('{"answers": []} trailing') == {"answers": []}


def test_secrets_are_redacted(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-SUPERSECRET123456")
    assert "SUPERSECRET" not in logs.redact("key nvapi-SUPERSECRET123456 used")
    assert "abc" not in logs.redact("Authorization: Bearer abc")
