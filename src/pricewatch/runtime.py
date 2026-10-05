"""Keep the browser display and web worker alive as one container lifecycle."""

import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from types import FrameType

X11_DIR = Path("/tmp/.X11-unix")
STARTUP_TIMEOUT = 10.0
MONITOR_INTERVAL = 2.0
STOP_TIMEOUT = 8.0


def display_available(display: str) -> bool:
    """Verify an actual local X11 setup response, not a stale socket file."""
    match = re.fullmatch(r":([0-9]{1,5})(?:\.[0-9]+)?", display)
    if match is None:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.3)
            client.connect(str(X11_DIR / f"X{int(match[1])}"))
            client.sendall(struct.pack("<BBHHHHH", ord("l"), 0, 11, 0, 0, 0, 0))
            reply = b""
            while len(reply) < 8:
                chunk = client.recv(8 - len(reply))
                if not chunk:
                    return False
                reply += chunk
            return reply[0] == 1 and struct.unpack("<H", reply[2:4])[0] == 11
    except (OSError, ValueError):
        return False


def _stop(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        process.wait(timeout=STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        # The group is private to this child; never signal unrelated host processes.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def _wait_display(process: subprocess.Popen[bytes], fd: int, stopping: Callable[[], bool]) -> str:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    number = b""
    while not stopping() and time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Browser display exited during startup")
        if select.select([fd], [], [], 0.1)[0]:
            chunk = os.read(fd, 16)
            if not chunk or len(number) + len(chunk) > 16:
                raise RuntimeError("Browser display returned no valid display number")
            number += chunk
            if number.endswith(b"\n"):
                if re.fullmatch(rb"[0-9]{1,5}\n", number) is None:
                    raise RuntimeError("Browser display returned an invalid display number")
                display = ":" + number[:-1].decode("ascii")
                while not stopping() and time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Browser display exited during startup")
                    if display_available(display):
                        return display
                    time.sleep(0.1)
                break
    raise RuntimeError("Browser display did not become ready")


def main() -> int:
    """Exit on display failure so the existing Docker restart policy can recover."""
    stopping = False

    def stop_requested(_signum: int, _frame: FrameType | None) -> None:
        # Signal handlers must not acquire locks held by an interrupted wait.
        nonlocal stopping
        stopping = True

    previous = {
        signum: signal.signal(signum, stop_requested) for signum in (signal.SIGTERM, signal.SIGINT)
    }
    display_process: subprocess.Popen[bytes] | None = None
    web_process: subprocess.Popen[bytes] | None = None
    try:
        read_fd, write_fd = os.pipe()
        try:
            # Xvfb chooses a free display itself, avoiding reboot leftovers and PID reuse.
            display_process = subprocess.Popen(
                [
                    "Xvfb",
                    "-displayfd",
                    str(write_fd),
                    "-screen",
                    "0",
                    "1280x1024x24",
                    "-nolisten",
                    "tcp",
                    "-noreset",
                ],
                pass_fds=(write_fd,),
                start_new_session=True,
            )
            os.close(write_fd)
            write_fd = -1
            display = _wait_display(display_process, read_fd, lambda: stopping)
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
        if stopping:
            return 0
        environment = {**os.environ, "DISPLAY": display}
        web_process = subprocess.Popen(
            [
                "uvicorn",
                "pricewatch.app:create_app",
                "--factory",
                "--host",
                "0.0.0.0",
                "--port",
                "8080",
                "--workers",
                "1",
                "--proxy-headers",
                "--forwarded-allow-ips",
                "127.0.0.1",
                "--timeout-graceful-shutdown",
                "5",
            ],
            env=environment,
            start_new_session=True,
        )
        while not stopping:
            time.sleep(MONITOR_INTERVAL)
            if stopping:
                break
            if display_process.poll() is not None or not display_available(display):
                print("Browser display unavailable; restarting the container", file=sys.stderr)
                return 1
            status = web_process.poll()
            if status is not None:
                return status if status >= 0 else 128 - status
        return 0
    except (OSError, RuntimeError):
        if stopping:
            return 0
        print(
            "Container runtime could not start; retrying via Docker restart policy", file=sys.stderr
        )
        return 1
    finally:
        _stop(web_process)
        _stop(display_process)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
