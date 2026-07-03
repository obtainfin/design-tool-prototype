"""Starts the Loan Assistant server and opens it in the default browser."""
from __future__ import annotations

import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import HOST, PORT, run_server  # noqa: E402


def main() -> None:
    server = run_server()
    url = f"http://{HOST}:{PORT}"
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    print(f"Loan Assistant running at {url} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Loan Assistant.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
