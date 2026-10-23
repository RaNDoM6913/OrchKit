import contextlib
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from bridge_cli_diagnostics import child_environment, failure_snapshot

from orch.cli import build_parser, main as cli_main
from orch.core import Orchestrator
from orch.plan import build_single_task_plan, write_plan
from orch.project import ProjectRegistry


class CliLedgerAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = cli_main(list(args))
        return rc, json.loads(output.getvalue())

    def make_repo(self, name="repo"):
        repo = self.base / name
        remote = self.base / f"{name}-remote.git"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main", str(repo)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config",
             "user.email", "fixture@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config",
             "user.name", "CLI Authority Fixture"],
            check=True,
        )
        (repo / "README.md").write_text("authority fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "initial"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "init", "--bare", str(remote)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
            check=True,
        )
        return repo

    def register_via_cli(self, home, repo):
        rc, result = self.invoke(
            "--root", str(home),
            "project", "add", str(repo),
            "--profile", "standard",
            "--review-mode", "off",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "REGISTERED")
        return result["project"]

    def test_claim_missing_ledger_fails_without_creating_home(self):
        home = self.base / "missing-claim-home"
        rc, result = self.invoke(
            "--root", str(home), "claim", "--worker", "fixture"
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "state_ledger_missing_or_unsafe")
        self.assertFalse(home.exists())

    def test_bridge_parser_exposes_only_readonly_calls_and_operator_binding(self):
        parser = build_parser()
        serve = parser.parse_args([
            "bridge", "serve", "--run-id", "fixture-run",
            "--cap", "/private/native-capability.json",
            "--socket", "/private/bridge.sock",
            "--client-credential-file", "/private/client.json",
        ])
        self.assertEqual(serve.run_id, "fixture-run")
        self.assertEqual(serve.cap, "/private/native-capability.json")
        self.assertEqual(serve.socket, "/private/bridge.sock")
        for operation in ("session.status", "file.read", "file.list"):
            with self.subTest(operation=operation):
                call = parser.parse_args([
                    "bridge", "call", "--client-credential-file",
                    "/private/client.json", "--operation", operation,
                ])
                self.assertEqual(call.operation, operation)
                self.assertEqual(call.max_response_bytes, 4096)
                self.assertIsNone(call.target)
        for arguments in (
            ["--operation", "file.patch"],
            ["--operation", "process.start"],
            ["--operation", "session.status", "--run-id", "other-run"],
            ["--operation", "session.status", "--cap", "/private/native.json"],
            ["--operation", "session.status", "--root", "/other-home"],
        ):
            with self.subTest(arguments=arguments):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as rejected:
                        parser.parse_args([
                            "bridge", "call", "--client-credential-file",
                            "/private/client.json", *arguments,
                        ])
                self.assertEqual(rejected.exception.code, 2)

    def test_bridge_serve_missing_home_is_sanitized_and_never_initializes(self):
        home = self.base / "missing-bridge-home"
        rc, result = self.invoke(
            "--root", str(home), "bridge", "serve",
            "--run-id", "fixture-run", "--cap", str(self.base / "cap.json"),
            "--socket", str(self.base / "bridge.sock"),
            "--client-credential-file", str(self.base / "client.json"),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result, {
            "status": "ERROR", "error_code": "bridge_serve_failed",
        })
        self.assertFalse(home.exists())
        self.assertFalse((self.base / "bridge.sock").exists())
        self.assertFalse((self.base / "client.json").exists())

    def test_bridge_serve_unsafe_home_is_not_repaired(self):
        home = self.base / "unsafe-bridge-home"
        Orchestrator(home)
        db = home / ".runtime" / "orch.sqlite3"
        # Stabilize fixture bytes before exercising the unsafe-home refusal.
        with contextlib.closing(sqlite3.connect(str(db))) as conn:
            self.assertEqual(
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone(),
                (0, 0, 0),
            )
        original = db.read_bytes()
        home.chmod(0o755)
        rc, result = self.invoke(
            "--root", str(home), "bridge", "serve",
            "--run-id", "fixture-run", "--cap", str(self.base / "cap.json"),
            "--socket", str(self.base / "bridge.sock"),
            "--client-credential-file", str(self.base / "client.json"),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error_code"], "bridge_serve_failed")
        self.assertEqual(home.stat().st_mode & 0o777, 0o755)
        self.assertEqual(db.read_bytes(), original)

    def test_bridge_call_missing_credentials_is_sanitized_without_ledger(self):
        home = self.base / "unused-bridge-home"
        credential = self.base / "missing-private-transport.json"
        rc, result = self.invoke(
            "--root", str(home), "bridge", "call",
            "--client-credential-file", str(credential),
            "--operation", "session.status",
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result, {
            "status": "ERROR", "error_code": "bridge_call_failed",
        })
        self.assertFalse(home.exists())

    def make_bridge_run(self, home, name):
        if not home.exists():
            home.mkdir(mode=0o700)
        repo = self.make_repo(name)
        (repo / "data").mkdir()
        (repo / "data" / "sample.bin").write_bytes(b"\x00\xfffixture\n")
        subprocess.run(["git", "-C", str(repo), "add", "data"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "binary fixture"],
            check=True, capture_output=True,
        )
        project = self.register_via_cli(home, repo)
        task_id = name.upper()
        plan = build_single_task_plan(
            project, task_id=task_id, goal="Test local read-only CLI",
            allowed_paths=["README.md", "data/"], plan_revision=name,
        )
        plan_file = self.base / (name + "-plan.json")
        self.assertEqual(write_plan(plan_file, plan)["status"], "CREATED")
        orch = Orchestrator(home)
        self.assertEqual(orch.load_plan(plan_file)["queued_count"], 1)
        claim = orch.claim("cli-fixture", project_id=project["project_id"])
        self.assertEqual(claim["status"], "CLAIMED")
        return orch, project, claim

    def bridge_serve_args(self, home, claim, transport):
        return [
            "--root", str(home), "bridge", "serve",
            "--run-id", claim["run_id"], "--cap", claim["capability_file"],
            "--socket", str(transport / "bridge.sock"),
            "--client-credential-file", str(transport / "client.json"),
        ]

    def test_bridge_serve_uses_selected_rows_with_independent_native_authority(self):
        from orch.bridge_authorization import LocalRunSessionAuthority
        from orch.bridge_readonly_authority import ReadOnlyAuthorityLedger
        from orch.worker_operations import WorkerRunBinding

        home = self.base / "binding-home"
        orch, project, claim = self.make_bridge_run(home, "cli-binding-a")
        self.make_bridge_run(home, "cli-binding-b")
        expected = WorkerRunBinding(
            project_id=project["project_id"], writer_key=project["writer_key"],
            task_id="CLI-BINDING-A", run_id=claim["run_id"], attempt=1,
        )
        transport = self.base / "binding-transport"
        transport.mkdir(mode=0o700)
        with orch.connect() as conn:
            before = hashlib.sha256(json.dumps([
                tuple(row) for row in conn.execute("SELECT * FROM runs")
            ]).encode("utf-8")).hexdigest()

        # A real foreground listener is unbounded here; retain actual native
        # authority in the probe and substitute only that blocking endpoint.
        def serve_probe(ledger, binding, capability_file, socket_path,
                        credential_file, limits):
            self.assertIsInstance(ledger, ReadOnlyAuthorityLedger)
            self.assertEqual(binding, expected)
            self.assertEqual(socket_path, transport / "bridge.sock")
            self.assertEqual(credential_file, transport / "client.json")
            authority = LocalRunSessionAuthority(ledger)
            session_id = authority.open_session(binding, capability_file)
            try:
                authority.require_session(session_id, binding, capability_file)
            finally:
                authority.close_session(session_id)
            return 0

        with mock.patch("orch.bridge_local_service.serve_local_bridge", serve_probe):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = cli_main(self.bridge_serve_args(home, claim, transport))
        self.assertEqual(rc, 0)
        self.assertEqual(output.getvalue(), "")
        with orch.connect() as conn:
            after = hashlib.sha256(json.dumps([
                tuple(row) for row in conn.execute("SELECT * FROM runs")
            ]).encode("utf-8")).hexdigest()
        self.assertEqual(after, before)

    def test_bridge_serve_explicit_and_default_symlink_home_are_refused(self):
        home = self.base / "symlink-bridge-home"
        _, _, claim = self.make_bridge_run(home, "cli-symlink")
        link = self.base / "linked-home"
        link.symlink_to(home, target_is_directory=True)
        transport = self.base / "symlink-transport"
        transport.mkdir(mode=0o700)
        for explicit_root in (True, False):
            with self.subTest(explicit_root=explicit_root):
                arguments = self.bridge_serve_args(link, claim, transport)
                if not explicit_root:
                    arguments = arguments[2:]
                # Returning success makes a bypass of the no-follow root
                # check observable without starting a long-running listener.
                with mock.patch("orch.bridge_local_service.serve_local_bridge", return_value=0):
                    with mock.patch.dict(os.environ, {"ORCH_HOME": str(link)}):
                        output = io.StringIO()
                        with contextlib.redirect_stdout(output):
                            rc = cli_main(arguments)
                self.assertEqual(rc, 1)
                self.assertEqual(json.loads(output.getvalue()), {
                    "status": "ERROR", "error_code": "bridge_serve_failed",
                })

    def test_bridge_serve_existing_endpoint_refusal_is_sanitized_and_preserved(self):
        home = self.base / "occupied-endpoint-home"
        _, _, claim = self.make_bridge_run(home, "cli-occupied")
        transport = self.base / "occupied-transport"
        transport.mkdir(mode=0o700)
        endpoint = transport / "bridge.sock"
        endpoint.write_bytes(b"existing-owner-file")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = cli_main(self.bridge_serve_args(home, claim, transport))
        self.assertEqual(rc, 1)
        self.assertTrue(output.getvalue(), "service refusal needs a safe CLI code")
        result = json.loads(output.getvalue())
        self.assertEqual(result, {
            "status": "ERROR", "error_code": "bridge_serve_failed",
        })
        self.assertEqual(endpoint.read_bytes(), b"existing-owner-file")
        self.assertFalse((transport / "client.json").exists())

    def test_bridge_cli_real_status_binary_read_list_and_refusal_preserve_native_rows(self):
        home = self.base / "wire-home"
        orch, project, claim = self.make_bridge_run(home, "cli-wire-a")
        _, unrelated, _ = self.make_bridge_run(home, "cli-wire-b")
        unrelated_file = Path(unrelated["root"]) / "data" / "sample.bin"
        unrelated_bytes = unrelated_file.read_bytes()
        capability = Path(claim["capability_file"])
        capability_digest = hashlib.sha256(capability.read_bytes()).hexdigest()
        transport = self.base / "wire"
        transport.mkdir(mode=0o700)
        credential = transport / "client.json"
        unused_home = self.base / "client-unused-home"
        with orch.connect() as conn:
            before = hashlib.sha256(json.dumps({
                table: [tuple(row) for row in conn.execute("SELECT * FROM " + table)]
                for table in ("runs", "tasks", "events")
            }, sort_keys=True).encode("utf-8")).hexdigest()
        diagnostics = self.base / "shutdown-diagnostics"
        process = subprocess.Popen(
            [sys.executable, "-m", "orch", *self.bridge_serve_args(home, claim, transport)],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=child_environment(diagnostics),
        )
        try:
            deadline = time.monotonic() + 8
            ready_wait = threading.Event()
            # Publication is the real readiness condition, not elapsed sleep.
            while not credential.exists() and process.poll() is None:
                if time.monotonic() >= deadline:
                    self.fail("local bridge did not publish its credential")
                ready_wait.wait(0.01)
            self.assertIsNone(process.poll(), "local bridge exited before readiness")
            transport_credential = json.loads(credential.read_text(encoding="utf-8"))
            for operation, target, expected in (
                ("session.status", None, None),
                ("file.read", "data/sample.bin", b"\x00\xfffixture\n"),
                ("file.list", "data", b'{"entries":["sample.bin"]}'),
                ("file.read", "outside.txt", None),
            ):
                with self.subTest(operation=operation, target=target):
                    arguments = [
                        "--root", str(unused_home), "bridge", "call",
                        "--client-credential-file", str(credential),
                        "--operation", operation,
                    ]
                    if target is not None:
                        arguments.extend(["--target", target])
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        rc = cli_main(arguments)
                    encoded = output.getvalue()
                    result = json.loads(encoded)
                    self.assertLessEqual(len(encoded.encode("ascii")), 4096)
                    self.assertEqual(encoded, json.dumps(
                        result, sort_keys=True, ensure_ascii=True,
                        separators=(",", ":"), allow_nan=False,
                    ))
                    if target == "outside.txt":
                        self.assertEqual(rc, 1)
                        self.assertEqual(result["state"], "REFUSED")
                        self.assertEqual(result["output_base64"], "")
                    else:
                        self.assertEqual(rc, 0)
                        self.assertEqual(result["state"], "SUCCEEDED")
                        if expected is not None:
                            self.assertEqual(base64.b64decode(result["output_base64"]), expected)
                        else:
                            self.assertEqual(result["status"]["binding"]["project_id"], project["project_id"])
                            self.assertEqual(result["status"]["binding"]["run_id"], claim["run_id"])
                    for private in (str(home), claim["capability_file"], str(transport)):
                        self.assertNotIn(private, encoded)
                    self.assertFalse(transport_credential["transport_token"] in encoded)
            wrong_credential = transport / "wrong-client.json"
            altered = dict(transport_credential)
            altered["transport_token"] = "wrong-private-token-must-never-appear"
            wrong_credential.write_text(json.dumps(altered), encoding="utf-8")
            wrong_credential.chmod(0o600)
            rc, result = self.invoke(
                "--root", str(unused_home), "bridge", "call",
                "--client-credential-file", str(wrong_credential),
                "--operation", "session.status",
            )
            self.assertEqual(rc, 1)
            self.assertEqual(result, {
                "status": "ERROR", "error_code": "bridge_call_failed",
            })
            self.assertTrue(wrong_credential.exists())
            rc, result = self.invoke(
                "bridge", "call", "--client-credential-file", str(credential),
                "--operation", "session.status",
            )
            self.assertEqual(rc, 0)
            self.assertEqual(result["state"], "SUCCEEDED")
            self.assertFalse(unused_home.exists())
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                server_output, server_error = process.communicate(timeout=8)
            except subprocess.TimeoutExpired as timeout:
                try:
                    observed = failure_snapshot(
                        process, timeout, diagnostics, credential, transport / "bridge.sock",
                    )
                except Exception as error:
                    observed = {"diagnostic_error": type(error).__name__}
                finally:
                    process.kill()
                    try:
                        process.communicate(timeout=8)
                    except subprocess.TimeoutExpired:
                        observed["pipe_drain_timed_out"] = True
                        for stream in (process.stdout, process.stderr):
                            if stream is not None:
                                stream.close()
                self.fail("owned local bridge did not stop: " + json.dumps(observed, sort_keys=True))
        self.assertEqual(process.returncode, 0)
        self.assertEqual(server_output, b"")
        self.assertEqual(server_error, b"")
        self.assertFalse(credential.exists())
        self.assertFalse((transport / "bridge.sock").exists())
        self.assertEqual(unrelated_file.read_bytes(), unrelated_bytes)
        self.assertEqual(hashlib.sha256(capability.read_bytes()).hexdigest(), capability_digest)
        with orch.connect() as conn:
            after = hashlib.sha256(json.dumps({
                table: [tuple(row) for row in conn.execute("SELECT * FROM " + table)]
                for table in ("runs", "tasks", "events")
            }, sort_keys=True).encode("utf-8")).hexdigest()
        self.assertEqual(after, before)

    def test_bridge_cli_shutdown_handles_pending_stop_from_non_main_thread(self):
        original, children, command_writers = subprocess.Popen, [], []

        def spawn(command, *args, **kwargs):
            if not (isinstance(command, list) and command[1:3] == ["-m", "orch"]
                    and "serve" in command):
                return original(command, *args, **kwargs)
            read_fd, write_fd = os.pipe()
            command_writers.append(write_fd)
            bootstrap = self.base / "shutdown-diagnostics" / "sitecustomize.py"
            with bootstrap.open("a", encoding="utf-8") as stream:
                stream.write(r'''
import threading
main_ident = threading.get_ident()
command_fd = int(os.environ["ORCH_TEST_STOP_FD"])
def direct_stop_to_worker():
    try:
        if os.read(command_fd, 1) != b"x":
            return
        deadline = time.monotonic() + 2
        while True:
            frame = sys._current_frames().get(main_ident)
            while frame is not None:
                if frame.f_code.co_name == "accept" and frame.f_code.co_filename.endswith("/socket.py"):
                    break
                frame = frame.f_back
            if frame is not None:
                break
            if time.monotonic() >= deadline:
                return
            threading.Event().wait(0.001)
        record("directed_stop", target="worker")
        signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
    finally:
        os.close(command_fd)
threading.Thread(target=direct_stop_to_worker, daemon=True).start()
''')
            kwargs["env"] = dict(kwargs["env"], ORCH_TEST_STOP_FD=str(read_fd))
            kwargs["pass_fds"] = (read_fd,)
            try:
                process = original(command, *args, **kwargs)
            finally:
                os.close(read_fd)
            children.append(process)
            process.terminate = lambda: os.write(write_fd, b"x")
            return process

        try:
            with mock.patch.object(subprocess, "Popen", side_effect=spawn):
                self.test_bridge_cli_real_status_binary_read_list_and_refusal_preserve_native_rows()
            events = [json.loads(line) for line in
                      (self.base / "shutdown-diagnostics" / "events.jsonl").read_text().splitlines()]
            self.assertEqual(len(children), 1)
            self.assertTrue(any(event["event"] == "directed_stop" for event in events))
            self.assertEqual(children[0].returncode, 0)
        finally:
            for write_fd in command_writers:
                os.close(write_fd)
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=8)

    def test_bridge_cli_shutdown_reports_snapshot_when_pipe_drain_times_out(self):
        original, children = subprocess.Popen, []

        def spawn(command, *args, **kwargs):
            process = original(command, *args, **kwargs)
            if (isinstance(command, list) and command[1:3] == ["-m", "orch"]
                    and "serve" in command):
                children.append(process)

                def communicate(*args, **kwargs):
                    # The real CLI has handled all requests and its SIGTERM.
                    # Model only an inherited pipe that never reaches EOF.
                    process.wait(timeout=8)
                    raise subprocess.TimeoutExpired("fixture-pipe-eof", kwargs.get("timeout", 8))

                process.communicate = communicate
            return process

        caught = None
        try:
            with mock.patch.object(subprocess, "Popen", side_effect=spawn):
                try:
                    self.test_bridge_cli_real_status_binary_read_list_and_refusal_preserve_native_rows()
                except BaseException as error:
                    caught = error
            self.assertIsInstance(caught, self.failureException)
            self.assertIn('"child_returncode_at_deadline": 0', str(caught))
            self.assertIn('"pipe_drain_timed_out": true', str(caught))
            self.assertEqual(len(children), 1)
            self.assertEqual(children[0].poll(), 0)
            self.assertTrue(children[0].stdout.closed)
            self.assertTrue(children[0].stderr.closed)
            self.assertFalse((self.base / "wire" / "client.json").exists())
            self.assertFalse((self.base / "wire" / "bridge.sock").exists())
        finally:
            for process in children:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=8)
                process.stdout.close()
                process.stderr.close()

    def test_state_check_missing_ledger_fails_without_initializing(self):
        home = self.base / "missing-state-home"
        rc, result = self.invoke("--root", str(home), "state", "check")
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "state_ledger_missing_or_unsafe")
        self.assertFalse(home.exists())

    def test_state_check_reports_missing_claims_without_recreating_directory(self):
        home = self.base / "missing-claims-state-home"
        Orchestrator(home)
        claims = home / ".runtime" / "claims"
        claims.rmdir()

        rc, result = self.invoke("--root", str(home), "state", "check")

        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(claims.exists())
        claim_path = next(
            item for item in result["permissions"]["paths"]
            if item["path"] == str(claims.resolve())
        )
        self.assertEqual(claim_path["status"], "MISSING")

    def test_state_check_migrates_legacy_schema_without_recreating_claims(self):
        home = self.base / "legacy-state-check-home"
        Orchestrator(home)
        claims = home / ".runtime" / "claims"
        claims.rmdir()
        db_path = home / ".runtime" / "orch.sqlite3"
        with sqlite3.connect(str(db_path)) as db:
            db.execute("DROP TABLE runs")
            db.execute(
                "CREATE TABLE runs ("
                "run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
                "attempt INTEGER NOT NULL, worker_id TEXT NOT NULL, "
                "state TEXT NOT NULL, lease_token TEXT NOT NULL, "
                "started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, "
                "submitted_at TEXT, snapshot_id TEXT, receipt_json TEXT, "
                "verify_status TEXT, review_status TEXT, feedback_json TEXT, "
                "completed_at TEXT, error TEXT)"
            )
            db.execute("PRAGMA user_version=3")

        rc, result = self.invoke("--root", str(home), "state", "check")

        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["schema_version"], 4)
        self.assertFalse(claims.exists())
        with sqlite3.connect(str(db_path)) as db:
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(runs)")
            }
        self.assertIn("claim_git_head", columns)

    def test_init_refuses_existing_registry_without_ledger(self):
        home = self.base / "registry-init-refusal"
        repo = self.make_repo("registry-init-refusal-repo")
        ProjectRegistry(home).add(
            repo, profile="standard", review_mode="off"
        )
        self.assertFalse((home / ".runtime").exists())

        rc, result = self.invoke("--root", str(home), "init")
        self.assertEqual(rc, 1)
        self.assertEqual(
            result["error"], "registered_project_authority_without_ledger"
        )
        self.assertFalse((home / ".runtime").exists())

    def test_project_add_refuses_registry_authority_without_ledger(self):
        home = self.base / "registry-add-refusal"
        repo_one = self.make_repo("registry-add-existing")
        repo_two = self.make_repo("registry-add-new")
        existing = ProjectRegistry(home).add(
            repo_one, profile="standard", review_mode="off"
        )["project"]
        self.assertFalse((home / ".runtime").exists())

        rc, result = self.invoke(
            "--root", str(home),
            "project", "add", str(repo_two),
            "--profile", "standard",
            "--review-mode", "off",
        )
        self.assertEqual(rc, 1)
        self.assertEqual(
            result["error"], "registered_project_authority_without_ledger"
        )
        self.assertFalse((home / ".runtime").exists())
        projects = ProjectRegistry(home).list()
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["project_id"], existing["project_id"])

    def test_project_list_marks_mismatched_config_identity_unsafe(self):
        home = self.base / "registry-list-identity"
        repo = self.make_repo("registry-list-repo")
        registered = ProjectRegistry(home).add(
            repo, profile="standard", review_mode="off"
        )
        project_id = registered["project"]["project_id"]
        config_path = Path(registered["config_path"])
        altered = json.loads(config_path.read_text(encoding="utf-8"))
        altered["project_id"] = "spoofed-project"
        config_path.write_text(json.dumps(altered) + "\n", encoding="utf-8")

        rc, result = self.invoke("--root", str(home), "project", "list")

        self.assertEqual(rc, 0)
        self.assertEqual(result["projects"], [{
            "project_id": project_id,
            "status": "UNSAFE",
        }])

    def test_project_add_refuses_mismatched_existing_registry_identity(self):
        home = self.base / "registry-add-identity"
        registry = ProjectRegistry(home)
        first_repo = self.make_repo("registry-add-first")
        second_repo = self.make_repo("registry-add-second")
        registered = registry.add(
            first_repo, profile="standard", review_mode="off"
        )
        project_id = registered["project"]["project_id"]
        config_path = Path(registered["config_path"])
        altered = json.loads(config_path.read_text(encoding="utf-8"))
        altered["project_id"] = "spoofed-project"
        config_path.write_text(json.dumps(altered) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(
            ValueError, "project_registry_invalid:" + project_id
        ):
            registry.add(
                second_repo, profile="standard", review_mode="off"
            )
        self.assertEqual(
            sorted(path.name for path in (home / "projects").iterdir()),
            [f"{project_id}.json"],
        )

    def test_queue_enqueue_registry_without_ledger_does_not_create_runtime(self):
        home = self.base / "registry-only-home"
        repo = self.make_repo("registry-only-repo")
        config = ProjectRegistry(home).add(
            repo, profile="standard", review_mode="off"
        )["project"]
        self.assertFalse((home / ".runtime").exists())
        rc, result = self.invoke(
            "--root", str(home),
            "queue", "enqueue", config["project_id"],
            "--task-id", "NO-LEDGER-1",
            "--goal", "must not create ledger",
            "--allowed-path", "result.json",
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "state_ledger_missing_or_unsafe")
        self.assertFalse((home / ".runtime").exists())
        self.assertFalse((home / "plans").exists())

    def test_explicit_init_creates_ledger_and_operational_status_works(self):
        home = self.base / "explicit-init-home"
        rc, initialized = self.invoke("--root", str(home), "init")
        self.assertEqual(rc, 0)
        self.assertEqual(initialized["status"], "OK")
        self.assertTrue((home / ".runtime" / "orch.sqlite3").is_file())

        rc, status = self.invoke("--root", str(home), "status")
        self.assertEqual(rc, 0)
        self.assertEqual(status["variant"], "B_NEW_CHAT_PER_ATTEMPT")

    def test_state_backup_cli_preserves_symlink_boundary(self):
        home = self.base / "backup-symlink-home"
        Orchestrator(home)
        victim = self.base / "backup-cli-victim.zip"
        victim.write_bytes(b"owner preserve")
        link = self.base / "backup-cli-link.zip"
        link.symlink_to(victim)

        rc, result = self.invoke(
            "--root", str(home),
            "state", "backup",
            "--output", str(link),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "backup_output_unsafe")
        self.assertEqual(victim.read_bytes(), b"owner preserve")
        self.assertTrue(link.is_symlink())

    def test_load_plan_cli_preserves_no_follow_symlink_boundary(self):
        home = self.base / "load-plan-symlink-home"
        workspace = self.base / "load-plan-workspace"
        workspace.mkdir()
        Orchestrator(home)
        target = self.base / "load-plan-real.json"
        target.write_text(json.dumps({
            "schema_version": 1,
            "plan_revision": "cli-symlink-v1",
            "tasks": [{
                "id": "CLI-SYMLINK",
                "goal": "must not follow cli symlink",
                "workspace": str(workspace),
                "dependencies": [],
                "allowed_paths": ["result.json"],
                "protected_paths": {},
                "checks": [],
                "required_review": False,
                "owner_acceptance": False,
                "publication": {"kind": "none"},
                "max_attempts": 2,
            }],
        }) + "\n", encoding="utf-8")
        link = self.base / "load-plan-link.json"
        link.symlink_to(target)

        rc, result = self.invoke(
            "--root", str(home), "load-plan", str(link)
        )
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "plan_file_missing_or_unsafe")
        self.assertEqual(Orchestrator(home).status()["tasks"], [])
        self.assertTrue(link.is_symlink())

    def test_symlinked_ledger_is_refused_without_touching_target(self):
        home = self.base / "symlink-home"
        runtime = home / ".runtime"
        runtime.mkdir(parents=True)
        external = self.base / "external.sqlite3"
        db = sqlite3.connect(str(external))
        db.execute("CREATE TABLE sentinel(value TEXT)")
        db.execute("INSERT INTO sentinel(value) VALUES('keep')")
        db.commit()
        db.close()
        before = external.read_bytes()
        (runtime / "orch.sqlite3").symlink_to(external)

        rc, result = self.invoke("--root", str(home), "state", "check")
        self.assertEqual(rc, 1)
        self.assertEqual(result["error"], "state_ledger_missing_or_unsafe")
        self.assertEqual(external.read_bytes(), before)
        self.assertTrue((runtime / "orch.sqlite3").is_symlink())

    def test_queue_enqueue_blocks_foreign_workspace_before_plan_write(self):
        home = self.base / "foreign-enqueue-home"
        repo = self.make_repo("foreign-enqueue-repo")
        config = self.register_via_cli(home, repo)
        (repo / "foreign.txt").write_text("owner concurrent bytes\n", encoding="utf-8")

        rc, result = self.invoke(
            "--root", str(home),
            "queue", "enqueue", config["project_id"],
            "--task-id", "FOREIGN-QUEUE",
            "--goal", "must be blocked by readiness",
            "--allowed-path", "result.json",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "project_readiness_blocked")
        self.assertIn(
            "workspace_baseline",
            " ".join(result["audit"]["blockers"]),
        )
        plans = home / "plans"
        self.assertFalse(plans.exists() and any(plans.iterdir()))

    def _finish_git_task(self, orch, repo, claim, task_id, relative):
        (repo / relative).write_text(
            json.dumps({"task": task_id}) + "\n", encoding="utf-8"
        )
        receipt = Path(claim["receipt_file"])
        receipt.write_text(json.dumps({
            "run_id": claim["run_id"],
            "task_id": task_id,
            "changed_paths": [relative],
        }) + "\n", encoding="utf-8")
        lease = orch.lease_from_capability(
            claim["run_id"], Path(claim["capability_file"])
        )
        orch.submit(claim["run_id"], lease, receipt)
        orch.quiesce(claim["run_id"], lease)
        verified = orch.verify(claim["run_id"])
        self.assertEqual(verified["status"], "VERIFIED")
        return orch.publish(claim["run_id"])

    def test_independent_prequeued_git_tasks_bind_base_at_execution_boundary(self):
        home = self.base / "dynamic-base-home"
        repo = self.make_repo("dynamic-base-repo")
        config = self.register_via_cli(home, repo)

        rc, first = self.invoke(
            "--root", str(home),
            "queue", "enqueue", config["project_id"],
            "--task-id", "QUEUE-BASE-1",
            "--goal", "first publication",
            "--allowed-path", "one.json",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(first["base_binding"], "admission_head")

        rc, second = self.invoke(
            "--root", str(home),
            "queue", "enqueue", config["project_id"],
            "--task-id", "QUEUE-BASE-2",
            "--goal", "second publication",
            "--allowed-path", "two.json",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(second["base_binding"], "dynamic_at_verify")
        second_plan = json.loads(
            Path(second["plan_path"]).read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "expected_base", second_plan["tasks"][0]["publication"]
        )

        orch = Orchestrator(home)
        claim_one = orch.claim("worker-one", project_id=config["project_id"])
        published_one = self._finish_git_task(
            orch, repo, claim_one, "QUEUE-BASE-1", "one.json"
        )
        self.assertEqual(published_one["status"], "COMPLETE")

        claim_two = orch.claim("worker-two", project_id=config["project_id"])
        published_two = self._finish_git_task(
            orch, repo, claim_two, "QUEUE-BASE-2", "two.json"
        )
        self.assertEqual(published_two["status"], "COMPLETE")
        self.assertNotEqual(published_one["commit"], published_two["commit"])
        remote = self.base / "dynamic-base-repo-remote.git"
        remote_head = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(remote_head, published_two["commit"])

    def test_batch_independent_git_tasks_only_bind_first_admission_base(self):
        home = self.base / "batch-base-home"
        repo = self.make_repo("batch-base-repo")
        config = self.register_via_cli(home, repo)
        manifest = self.base / "batch-base.json"
        manifest.write_text(json.dumps({
            "schema_version": 1,
            "plan_revision": "batch-base-v1",
            "tasks": [
                {
                    "id": "BATCH-BASE-1",
                    "goal": "first",
                    "allowed_paths": ["first.json"],
                },
                {
                    "id": "BATCH-BASE-2",
                    "goal": "second",
                    "allowed_paths": ["second.json"],
                },
            ],
        }) + "\n", encoding="utf-8")
        rc, result = self.invoke(
            "--root", str(home),
            "queue", "enqueue-batch", config["project_id"], str(manifest),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(result["base_binding"], "first_task_admission_head")
        plan = json.loads(Path(result["plan_path"]).read_text(encoding="utf-8"))
        self.assertIn("expected_base", plan["tasks"][0]["publication"])
        self.assertNotIn("expected_base", plan["tasks"][1]["publication"])

    def test_project_make_plan_blocks_foreign_workspace_before_output(self):
        home = self.base / "foreign-plan-home"
        repo = self.make_repo("foreign-plan-repo")
        config = self.register_via_cli(home, repo)
        (repo / "rogue.txt").write_text("rogue\n", encoding="utf-8")
        output = self.base / "blocked-plan.json"

        rc, result = self.invoke(
            "--root", str(home),
            "project", "make-plan", config["project_id"],
            "--task-id", "FOREIGN-PLAN",
            "--goal", "must be blocked",
            "--allowed-path", "result.json",
            "--output", str(output),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "project_readiness_blocked")
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
