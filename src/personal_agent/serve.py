"""Server process entry point (started by the supervisor in personal_agent.main)."""

import uvicorn

from .config import load_config


def run() -> None:
    config = load_config()
    # Local only unless remote access is explicitly enabled; the server can edit your repos.
    host = config.remote.host if config.remote.enabled else "127.0.0.1"
    uvicorn.run("personal_agent.server:app", host=host, port=config.port)


if __name__ == "__main__":
    run()
