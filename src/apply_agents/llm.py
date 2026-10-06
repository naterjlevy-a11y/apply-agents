"""One interface over any OpenAI-compatible provider (NVIDIA first, OpenRouter as fallback).

Throttling is honest: a per-provider requests-per-minute bucket, a global cap on
parallel calls, exponential backoff that respects Retry-After, a retry limit, then
the fallback provider. Nothing here tries to dodge a provider's limits.
"""
import asyncio
import json
import os
import random
import re
import time

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from . import config


class LLMUnavailable(Exception):
    pass


class RateBucket:
    """Spaces calls so a provider never sees more than `rpm` per rolling minute."""

    def __init__(self, rpm):
        self.rpm = max(1, rpm)
        self.stamps = []
        self.lock = asyncio.Lock()

    async def take(self):
        async with self.lock:
            while True:
                now = time.monotonic()
                self.stamps = [t for t in self.stamps if now - t < 60]
                if len(self.stamps) < self.rpm:
                    self.stamps.append(now)
                    return
                await asyncio.sleep(60 - (now - self.stamps[0]) + 0.05)


class Provider:
    def __init__(self, name, model):
        spec = config.PROVIDERS.get(name)
        if spec is None:
            raise ValueError(f"unknown LLM provider '{name}' (use nvidia, openrouter or custom)")
        self.name, self.model = name, model
        key = os.getenv(spec["key_env"], "")
        self.ok = bool(key and spec["base_url"] and model)
        self.client = (
            AsyncOpenAI(api_key=key, base_url=spec["base_url"], timeout=config.LLM_TIMEOUT_S, max_retries=0)
            if self.ok else None
        )
        # one bucket per provider, so two models on the same account share its limit
        self.bucket = _buckets.setdefault(name, RateBucket(spec["rpm"]))


_global = None
_providers = None
_buckets = {}


def providers():
    global _providers, _global
    if _providers is None:
        _global = asyncio.Semaphore(config.LLM_MAX_PARALLEL)
        chain = [Provider(config.LLM_PROVIDER, config.LLM_MODEL)]
        if config.LLM_BACKUP_MODEL and config.LLM_BACKUP_MODEL != config.LLM_MODEL:
            chain.append(Provider(config.LLM_PROVIDER, config.LLM_BACKUP_MODEL))
        if config.FALLBACK_PROVIDER:
            chain.append(Provider(config.FALLBACK_PROVIDER, config.FALLBACK_MODEL))
        _providers = [p for p in chain if p.ok]
    return _providers


def available():
    return bool(providers())


def _retry_after(err):
    try:
        return float(err.response.headers.get("retry-after"))
    except (AttributeError, TypeError, ValueError):
        return None


async def chat(messages, log=print, max_tokens=1500):
    """Returns the reply text, or raises LLMUnavailable once every provider is exhausted."""
    chain = providers()
    if not chain:
        raise LLMUnavailable("no LLM provider has an API key set in .env")
    last = None
    for p in chain:
        for attempt in range(config.LLM_MAX_RETRIES + 1):
            await p.bucket.take()
            try:
                async with _global:
                    r = await p.client.chat.completions.create(
                        model=p.model, messages=messages, temperature=0, max_tokens=max_tokens
                    )
                return r.choices[0].message.content or ""
            except APIStatusError as e:
                last = e
                if e.status_code not in (408, 409, 429) and e.status_code < 500:
                    log(f"LLM {p.name} refused the request (HTTP {e.status_code}); trying next provider")
                    break
                wait = _retry_after(e) or min(60, 2 ** attempt * 2) + random.random()
                log(f"LLM {p.name} HTTP {e.status_code}; retry {attempt + 1}/{config.LLM_MAX_RETRIES} in {wait:.0f}s")
            except (APIConnectionError, APITimeoutError) as e:
                last = e
                wait = min(60, 2 ** attempt * 2) + random.random()
                log(f"LLM {p.name} connection problem; retry {attempt + 1}/{config.LLM_MAX_RETRIES} in {wait:.0f}s")
            if attempt < config.LLM_MAX_RETRIES:
                await asyncio.sleep(wait)
        log(f"LLM {p.name} gave up after retries")
    raise LLMUnavailable(f"all providers failed: {type(last).__name__}")


def parse_json(text):
    """Pulls the first JSON object/array out of a reply, tolerating ```json fences and <think> blocks."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("no JSON in reply")
    return json.loads(text[start:text.rfind("}" if text[start] == "{" else "]") + 1])
