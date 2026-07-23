from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn


ROOT_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
RUNTIME_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT_DIR
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def find_free_port(preferred: int = 8010) -> int:
    if is_port_free(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def open_browser_later(url: str) -> None:
    def worker() -> None:
        time.sleep(1.2)
        webbrowser.open(url)

    threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    # Keep data/lite_index beside the executable so user uploads persist.
    import os

    os.chdir(RUNTIME_DIR)
    port = find_free_port(8010)
    url = f"http://127.0.0.1:{port}/"
    print(f"Local Knowledge Tool is running: {url}")
    print("Close this window to stop the tool.")
    open_browser_later(url)
    uvicorn.run("app.lite.main:app", host="127.0.0.1", port=port, reload=False)
