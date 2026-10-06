"""python -m apply_agents  ->  starts the agents and the dashboard at http://127.0.0.1:8765"""
import sys
import threading
import webbrowser

import uvicorn

from . import config


def main():
    if not config.PROFILE_PATH.exists():
        sys.exit(f"No profile at {config.PROFILE_PATH}. Copy profile.example.yaml to profile.yaml and fill it in.")
    url = f"http://127.0.0.1:{config.PORT}"
    print(f"Dashboard: {url}  ({config.MAX_CONCURRENT_AGENTS} agents, LLM {config.LLM_PROVIDER}/{config.LLM_MODEL})")
    if "--no-open" not in sys.argv:
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    uvicorn.run("apply_agents.server:app", host="127.0.0.1", port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
