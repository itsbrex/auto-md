from __future__ import annotations

import argparse
import socket
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import uvicorn

from . import __version__
from .web import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch the local Auto-MD web app")
    parser.add_argument(
        "--port", type=int, default=0, help="Loopback port; defaults to an available port"
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="Do not open the browser automatically"
    )
    parser.add_argument(
        "--log-level",
        choices=["critical", "error", "warning", "info", "debug"],
        default="warning",
    )
    parser.add_argument("--version", action="version", version=f"Auto-MD {__version__}")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", args.port))
        sock.listen(2048)
    except OSError as exc:
        sock.close()
        parser.error(f"unable to bind the local server: {exc}")
    port = int(sock.getsockname()[1])
    url = f"http://127.0.0.1:{port}/"

    app = create_app()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level=args.log_level,
        access_log=False,
        server_header=False,
        date_header=True,
    )
    server = uvicorn.Server(config)
    if not args.no_browser:
        threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()

    print(f"Auto-MD {__version__} is ready at {url}")
    print("Press Ctrl-C to stop and clear temporary job data.")
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    return 0


def _open_when_ready(url: str) -> None:
    health_url = f"{url}health"
    for _ in range(60):
        try:
            with urllib.request.urlopen(health_url, timeout=0.5) as response:
                if response.status == 200:
                    webbrowser.open(url, new=2)
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.25)


if __name__ == "__main__":
    raise SystemExit(main())
