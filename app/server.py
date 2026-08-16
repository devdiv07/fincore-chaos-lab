"""Entry point.

psycopg's async driver cannot run on Windows' ProactorEventLoop, and uvicorn
(>=0.34) hardcodes exactly that loop on win32 -- `uvicorn.run(loop="asyncio")`
does NOT honour `asyncio.set_event_loop_policy`. So the server is driven
directly, under a selector loop we choose ourselves.
"""

from __future__ import annotations

import asyncio
import sys

import uvicorn

from . import config


def _loop_factory():
    """Selector loop on Windows; platform default elsewhere."""
    if sys.platform == "win32":  # pragma: no cover - platform specific
        return asyncio.SelectorEventLoop
    return asyncio.new_event_loop


def main() -> None:
    print(f"\nDemo:\nhttp://{config.HOST}:{config.PORT}/?recording=1\n", flush=True)

    server = uvicorn.Server(
        uvicorn.Config(
            "app.main:app",
            host=config.HOST,
            port=config.PORT,
            log_level="warning",
        )
    )
    try:
        asyncio.run(server.serve(), loop_factory=_loop_factory())
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass


if __name__ == "__main__":
    main()
