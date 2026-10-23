"""Private, test-only observations for the real ``python -m orch`` child."""

import json
import os
from pathlib import Path
import signal
import threading


_BOOTSTRAP = r'''
import faulthandler, json, os, signal, sys, time
from pathlib import Path
from orch import bridge_local_service as service
directory = Path(__file__).resolve().parent
events = os.open(str(directory / "events.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
stack = os.open(str(directory / "stack.txt"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
faulthandler.register(signal.SIGUSR1, file=stack, all_threads=True)
count = 0
def record(event, **values):
    global count
    if count < 512:
        os.write(events, (json.dumps(dict(event=event, at=time.monotonic(), **values)) + "\n").encode())
        count += 1
original_handler = service._DeferredStop.__call__
def handler(self, signum, frame):
    record("handler", signum=signum, depth=self._depth, requested=self._requested,
           function=frame.f_code.co_name if frame else None,
           line=frame.f_lineno if frame else None)
    try:
        result = original_handler(self, signum, frame)
    except BaseException:
        record("handler_raise")
        raise
    record("handler_return", requested=self._requested)
    return result
service._DeferredStop.__call__ = handler
original_hook = sys.unraisablehook
def unraisable(event):
    record("unraisable", exception=type(event.exc_value).__name__)
    original_hook(event)
sys.unraisablehook = unraisable
def observe(owner, name, label):
    original = getattr(owner, name)
    def call(*args, **kwargs):
        record(label + ":enter")
        try:
            result = original(*args, **kwargs)
        except EOFError:
            record(label + ":eof")
            raise
        finally:
            record(label + ":leave")
        return result
    setattr(owner, name, call)
for owner, name, label in (
    (service, "receive_frame", "receive"),
    (service, "send_frame", "send"),
    (service, "_serve_connection", "connection"),
    (service, "serve_local_bridge", "service"),
    (service.LocalFileScope, "close", "scope_close"),
    (service.LocalRunSessionAuthority, "close_session", "session_close"),
    (service.socket.socket, "accept", "accept"),
    (service.socket.socket, "close", "socket_close"),
):
    observe(owner, name, label)
record("observer_ready")
'''


def child_environment(directory):
    """Keep the direct -m invocation and observe only its private test process."""
    directory = Path(directory)
    directory.mkdir(mode=0o700)
    (directory / "sitecustomize.py").write_text(_BOOTSTRAP, encoding="utf-8")
    env = dict(os.environ)
    paths = [str(directory), str(Path(__file__).resolve().parents[1])]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def failure_snapshot(process, timeout, directory, credential, endpoint):
    """Observe the original deadline before the caller kills/reaps the child."""
    directory = Path(directory)
    polled = process.poll()
    result = dict(child_returncode_at_deadline=polled,
                  stdout_bytes_at_deadline=len(timeout.output or b""),
                  stderr_bytes_at_deadline=len(timeout.stderr or b""),
                  credential_present=credential.exists(), endpoint_present=endpoint.exists())
    events_path = directory / "events.jsonl"
    events = events_path.read_text(encoding="utf-8") if events_path.exists() else ""
    ready = False
    for line in events.splitlines():
        try:
            ready = ready or json.loads(line).get("event") == "observer_ready"
        except ValueError:
            pass  # A failure snapshot may race the child's final write.
    result["observer_ready"] = ready
    if polled is None and ready:
        try:
            process.send_signal(signal.SIGUSR1)
        except ProcessLookupError:
            pass
        # Failure-only collection is separate from the unchanged 8s deadline.
        threading.Event().wait(0.2)
    for name, limit in (("events.jsonl", 49152), ("stack.txt", 8192)):
        path = directory / name
        result[name] = path.read_text(encoding="utf-8")[:limit] if path.exists() else ""
    return result
