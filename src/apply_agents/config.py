"""Settings, read once from .env. Nothing secret is ever printed or stored in SQLite."""
import os
import pathlib

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _bool(name, default):
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "applications.db"
SHOTS_DIR = DATA_DIR / "screenshots"
LOG_PATH = DATA_DIR / "agents.log"
PROFILE_PATH = pathlib.Path(os.getenv("PROFILE_PATH", ROOT / "profile.yaml"))

MAX_CONCURRENT_AGENTS = max(1, _int("MAX_CONCURRENT_AGENTS", 3))
HEADLESS = _bool("HEADLESS", False)
PORT = _int("PORT", 8765)
PAGE_TIMEOUT_MS = _int("PAGE_TIMEOUT_MS", 45000)
MAX_FORM_PAGES = _int("MAX_FORM_PAGES", 8)

# Where the career pipeline keeps per-job folders (resume + cover letter).
APPLICATIONS_DIR = pathlib.Path(os.getenv("APPLICATIONS_DIR") or ROOT / "applications")
APPLIED_LOG = os.getenv("APPLIED_LOG") or str(ROOT / "data" / "applied-log.csv")

# LLM providers. Every one speaks the OpenAI chat-completions format.
PROVIDERS = {
    "nvidia": {
        "base_url": os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        "key_env": "NVIDIA_API_KEY",
        "rpm": _int("NVIDIA_RPM", 35),  # free tier is 40/min; stay under it
    },
    "openrouter": {
        "base_url": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        "key_env": "OPENROUTER_API_KEY",
        "rpm": _int("OPENROUTER_RPM", 18),  # free models are ~20/min
    },
    "custom": {
        "base_url": os.getenv("CUSTOM_BASE_URL", ""),
        "key_env": "CUSTOM_API_KEY",
        "rpm": _int("CUSTOM_RPM", 30),
    },
}

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "nvidia")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-ai/deepseek-v4.1-flash")
LLM_BACKUP_MODEL = os.getenv("LLM_BACKUP_MODEL", "z-ai/glm-5.3")  # same provider, tried if the main model fails
FALLBACK_PROVIDER = os.getenv("LLM_FALLBACK_PROVIDER", "")
FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
LLM_MAX_RETRIES = _int("LLM_MAX_RETRIES", 4)
LLM_MAX_PARALLEL = _int("LLM_MAX_PARALLEL", 2)
LLM_TIMEOUT_S = _int("LLM_TIMEOUT_S", 60)


# Claude (headless Claude Code) answers essays and picks files when an application gets stuck.
CLAUDE_AUTO_ANSWER = _bool("CLAUDE_AUTO_ANSWER", True)
CLAUDE_PATH = os.getenv("CLAUDE_PATH", str(pathlib.Path.home() / ".local" / "bin" / "claude.exe"))
CLAUDE_FACTS = os.getenv("CLAUDE_FACTS") or str(ROOT / "facts.md")


def secrets():
    """Every configured secret value, so the log filter can mask them."""
    out = []
    for p in PROVIDERS.values():
        v = os.getenv(p["key_env"])
        if v:
            out.append(v)
    return out
