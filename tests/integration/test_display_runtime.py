"""Exercise the real supervisor with isolated X11 and web-process stand-ins."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

X_SERVER = r"""
import json,os,signal,socket,sys
from pathlib import Path
root=Path(os.environ['PW_RUNTIME_TEST'])
def event(kind):
    with (root/'events').open('a') as out:
        out.write(json.dumps({'event':kind,'pid':os.getpid()})+'\n')
def stop(*args):
    event('display_stopped')
    raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
event('display_started')
if os.environ.get('PW_X_MODE')=='exit': raise SystemExit(17)
assert '-displayfd' in sys.argv and '-nolisten' in sys.argv and '-noreset' in sys.argv
fd=int(sys.argv[sys.argv.index('-displayfd')+1])
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as server:
    server.bind(str(root/'X67'))
    server.listen()
    server.settimeout(0.05)
    os.write(fd,b'67\n')
    os.close(fd)
    event('display_ready')
    while True:
        try: client,_=server.accept()
        except TimeoutError: continue
        with client:
            packet=client.recv(12)
            assert len(packet)==12 and packet[:1]==b'l'
            if os.environ.get('PW_X_MODE')!='unresponsive' and not (root/'unresponsive').exists():
                client.sendall(b'\x01\x00\x0b\x00\x00\x00\x00\x00')
"""

WEB_SERVER = r"""
import json,os,signal,time
from pathlib import Path
root=Path(os.environ['PW_RUNTIME_TEST'])
def event(kind):
    with (root/'events').open('a') as out:
        out.write(json.dumps({'event':kind,'pid':os.getpid(),'display':os.environ.get('DISPLAY')})+'\n')
def stop(*args):
    event('web_stopped')
    raise SystemExit(0)
signal.signal(signal.SIGTERM,signal.SIG_IGN if os.environ.get('PW_WEB_STUCK') else stop)
event('web_started')
if os.environ.get('PW_WEB_EXIT'): raise SystemExit(7)
while True: time.sleep(0.05)
"""


def events(root: Path) -> list[dict]:
    path = root / "events"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def wait_for_event(root: Path, name: str, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(item["event"] == name for item in events(root)):
            return
        if process.poll() is not None:
            output = process.communicate()[1]
            pytest.fail(f"Supervisor exited before {name}: {output}")
        time.sleep(0.02)
    pytest.fail(f"Supervisor did not reach {name}")


@pytest.fixture
def launch_runtime(tmp_path):
    processes = []
    for executable, source in (("Xvfb", X_SERVER), ("uvicorn", WEB_SERVER)):
        path = tmp_path / executable
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o755)

    def launch(**extra):
        env = {**os.environ, "PW_RUNTIME_TEST": str(tmp_path), **extra}
        env["PATH"] = f"{tmp_path}:{env['PATH']}"
        code = (
            "import os,sys,signal,threading,time\n"
            "from pathlib import Path\nfrom pricewatch import runtime\n"
            "runtime.X11_DIR=Path(sys.argv[1]); runtime.STARTUP_TIMEOUT=0.6\n"
            "runtime.MONITOR_INTERVAL=0.05; runtime.STOP_TIMEOUT=0.3\n"
            "if os.environ.get('PW_SIGNAL_BOUNDARY'):\n"
            "    def interrupt(frame,event,arg):\n"
            "        condition = (event=='call' and frame.f_code.co_name=='wait' "
            "and isinstance(frame.f_locals.get('self'),threading.Condition))\n"
            "        sleep = (event=='c_call' and arg is time.sleep "
            "and frame.f_globals.get('__name__')=='pricewatch.runtime')\n"
            "        if condition or sleep:\n"
            "            sys.setprofile(None)\n"
            "            os.kill(os.getpid(),signal.SIGTERM)\n"
            "    sys.setprofile(interrupt)\n"
            "sys.exit(runtime.main())"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(process)
        return process

    yield launch
    for process in processes:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
                # The interrupted supervisor may never reach its cleanup.
                for item in events(tmp_path):
                    if item["event"] in ("web_started", "display_started"):
                        with suppress(ProcessLookupError):
                            os.kill(item["pid"], signal.SIGTERM)


def assert_children_reaped(root: Path) -> None:
    for event in events(root):
        if event["event"] in ("display_started", "web_started"):
            with pytest.raises(ProcessLookupError):
                os.kill(event["pid"], 0)


def test_reboot_stale_socket_does_not_select_a_dead_display(tmp_path, launch_runtime):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(tmp_path / "X99"))
    process = launch_runtime()
    wait_for_event(tmp_path, "web_started", process)
    web = next(item for item in events(tmp_path) if item["event"] == "web_started")
    assert web["display"] == ":67"
    assert (tmp_path / "X99").exists()  # No broad cleanup of pre-existing files.
    process.terminate()
    assert process.wait(timeout=5) == 0
    assert_children_reaped(tmp_path)


@pytest.mark.parametrize("mode", ["exit", "unresponsive"])
def test_unusable_display_prevents_a_false_healthy_web_start(tmp_path, launch_runtime, mode):
    process = launch_runtime(PW_X_MODE=mode)
    wait_for_event(tmp_path, "display_started", process)
    assert process.wait(timeout=5) != 0
    assert not any(item["event"] == "web_started" for item in events(tmp_path))
    assert_children_reaped(tmp_path)


def test_display_exit_stops_web_and_exits_for_container_restart(tmp_path, launch_runtime):
    process = launch_runtime()
    wait_for_event(tmp_path, "web_started", process)
    display_pid = next(
        item["pid"] for item in events(tmp_path) if item["event"] == "display_started"
    )
    os.kill(display_pid, signal.SIGTERM)
    assert process.wait(timeout=5) != 0
    assert any(item["event"] == "web_stopped" for item in events(tmp_path))
    assert_children_reaped(tmp_path)


def test_shutdown_kills_a_stuck_web_process_with_a_bound(tmp_path, launch_runtime):
    process = launch_runtime(PW_WEB_STUCK="1")
    wait_for_event(tmp_path, "web_started", process)
    process.terminate()
    assert process.wait(timeout=5) == 0
    assert_children_reaped(tmp_path)


def test_live_but_unresponsive_display_is_recovered(tmp_path, launch_runtime):
    process = launch_runtime()
    wait_for_event(tmp_path, "web_started", process)
    (tmp_path / "unresponsive").touch()
    assert process.wait(timeout=5) != 0
    assert any(item["event"] == "web_stopped" for item in events(tmp_path))
    assert_children_reaped(tmp_path)


def test_web_exit_reaps_display_and_preserves_failure_status(tmp_path, launch_runtime):
    process = launch_runtime(PW_WEB_EXIT="1")
    wait_for_event(tmp_path, "web_started", process)
    assert process.wait(timeout=5) == 7
    assert_children_reaped(tmp_path)


def test_local_display_probe_rejects_stale_and_remote_displays(tmp_path, monkeypatch):
    from pricewatch import runtime

    monkeypatch.setattr(runtime, "X11_DIR", tmp_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(tmp_path / "X99"))
    assert not runtime.display_available(":99")
    assert not runtime.display_available("192.168.50.99:0")
    assert not runtime.display_available(":../../data")


def test_signal_at_wait_boundary_does_not_deadlock(tmp_path, launch_runtime):
    process = launch_runtime(PW_SIGNAL_BOUNDARY="1")
    assert process.wait(timeout=2) == 0
    assert_children_reaped(tmp_path)
