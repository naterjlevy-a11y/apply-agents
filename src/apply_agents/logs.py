"""Agent logs: to the console, data/agents.log and SQLite, with secrets masked."""
import logging
import re

from . import config, db

_SECRET_PATTERNS = [
    re.compile(r"nvapi-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-or-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)bearer\s+\S+"),
    re.compile(r"(?i)(token|password|cookie|api[_-]?key)\s*[:=]\s*\S+"),
]


def redact(text):
    text = str(text)
    for s in config.secrets():
        text = text.replace(s, "[secret]")
    for p in _SECRET_PATTERNS:
        text = p.sub("[secret]", text)
    return text


_logger = None


def logger():
    global _logger
    if _logger is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _logger = logging.getLogger("apply_agents")
        _logger.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S")
        for h in (logging.StreamHandler(), logging.FileHandler(config.LOG_PATH, encoding="utf-8")):
            h.setFormatter(fmt)
            _logger.addHandler(h)
    return _logger


def agent(agent_id, app_id, msg):
    msg = redact(msg)
    logger().info(f"[{agent_id or 'Manager'}] #{app_id} {msg}")
    db.log(app_id, agent_id, msg)
