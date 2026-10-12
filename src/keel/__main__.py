"""python -m keel: serve the API and run the workers in one process."""

import os

import uvicorn


def main() -> None:
    uvicorn.run("keel.service:create_service", factory=True, host=os.environ.get("KEEL_HOST", "0.0.0.0"),
                port=int(os.environ.get("KEEL_PORT", "8700")), proxy_headers=True,
                # Open SSE streams would otherwise hold lifespan shutdown until Docker's SIGKILL.
                timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
