"""Start the backend on loopback only:  cd backend && .venv/Scripts/python -m app.run

SEALED_HOST (default 127.0.0.1) must be a loopback address; SEALED_PORT defaults to 8000.
"""

import os

import uvicorn

from app.security import bind_host
from sealed.env import load_env


def main() -> None:
    load_env()
    host = bind_host()                       # raises ConfigError for anything but loopback
    uvicorn.run("app.main:app", host=host, port=int(os.getenv("SEALED_PORT", "8000")))


if __name__ == "__main__":
    main()
