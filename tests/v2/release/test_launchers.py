"""Exercise updater restoration through a real, isolated platform launcher."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import subprocess
import sys
from threading import Event
import time
import unittest
import uuid

import test_update


class LauncherAcceptance(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("BMEM_UPDATE_TEST_BIN"):
            self.skipTest("requires the signed isolated update fixture")
        self.update = test_update.UpdateAcceptance("runTest")
        self.update.setUpClass()
        self.addCleanup(self.update.doCleanups)
        self.update.setUp()
        self.fixture_id = uuid.uuid4()
        self.update.env["BMEM_UPDATE_TEST_SUPERVISOR"] = str(self.fixture_id)

    def run_checked(self, *args, optional=False):
        result = subprocess.run(args, env=self.update.env, cwd=self.update.work,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=False, text=True, timeout=15)
        if not optional:
            self.assertEqual(result.returncode, 0, (args[0], result.stdout, result.stderr))
        return result

    def wait_for(self, read):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if value := read():
                return value
            time.sleep(0.10)
        self.fail("the registered fixture launcher did not start the bmem service")

    @contextmanager
    def launchd(self, *, keep_alive, exit_timeout=None):
        label = f"com.botinkit.memory.v2.test.{self.fixture_id}"
        domain = f"gui/{os.getuid()}"
        target = f"{domain}/{label}"
        agents = self.update.work / "account/Library/LaunchAgents"
        agents.mkdir(parents=True)
        plist = agents / f"{label}.plist"
        config = {
            "Label": label,
            "ProgramArguments": [str(self.update.launcher), "--home", str(self.update.root), "service", "run"],
            "EnvironmentVariables": {
                "BMEM_LOCATOR_DIR": self.update.env["BMEM_LOCATOR_DIR"],
                "HOME": self.update.env["HOME"],
            },
            "RunAtLoad": keep_alive,
            "KeepAlive": keep_alive,
            "StandardOutPath": str(self.update.work / "launchd.out.log"),
            "StandardErrorPath": str(self.update.work / "launchd.err.log"),
        }
        if exit_timeout is not None:
            config["ExitTimeOut"] = exit_timeout
        definition = plistlib.dumps(config)
        plist.write_bytes(definition)

        def pid():
            result = self.run_checked("/bin/launchctl", "print", target, optional=True)
            match = re.search(r"^\s*pid = (\d+)$", result.stdout, re.MULTILINE)
            return int(match[1]) if result.returncode == 0 and match else None

        self.run_checked("/bin/launchctl", "bootstrap", domain, str(plist))
        try:
            if not keep_alive:
                self.run_checked("/bin/launchctl", "kickstart", target)
            yield pid, lambda: self.assertEqual(plist.read_bytes(), definition)
        finally:
            self.run_checked("/bin/launchctl", "bootout", target, optional=True)

    @contextmanager
    def systemd(self):
        if os.environ.get("BMEM_UPDATE_ISOLATED_SYSTEMD_MANAGER") != "disposable-profile":
            self.skipTest("requires a disposable Linux profile with native systemd user D-Bus")
        config = Path(os.environ["XDG_CONFIG_HOME"]).resolve(strict=True)
        if os.getuid() == 0 or config.stat().st_uid != os.getuid():
            self.skipTest("disposable systemd profile must belong to the ordinary test user")
        self.update.env["XDG_CONFIG_HOME"] = str(config)
        self.run_checked("/usr/bin/busctl", "--user", "call", "org.freedesktop.DBus",
                         "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetNameOwner",
                         "s", "org.freedesktop.systemd1")
        user_units = config / "systemd/user"
        user_units.mkdir(parents=True, exist_ok=True)
        unit = f"bmem-upgrade-test-{self.fixture_id}.service"
        filename = user_units / unit
        start = " ".join(f'"{part}"' for part in
                         [self.update.launcher, "--home", self.update.root, "service", "run"])
        definition = ("\n".join(["[Unit]", "Description=Isolated bmem 007 upgrade fixture", "[Service]",
                                "Type=simple", f"ExecStart={start}", "Restart=always", "RestartSec=0.2",
                                f'Environment="BMEM_LOCATOR_DIR={self.update.env["BMEM_LOCATOR_DIR"]}"', ""]))
        filename.write_text(definition, encoding="utf-8")

        def ctl(*args, optional=False):
            return self.run_checked("/usr/bin/systemctl", "--user", *args, optional=optional)

        def pid():
            reported = ctl("show", "--property=MainPID", "--value", unit).stdout.strip()
            return int(reported) or None

        ctl("daemon-reload")
        self.assertEqual(ctl("show", "--property=FragmentPath", "--value", unit).stdout.strip(), str(filename))
        ctl("start", unit)
        try:
            yield pid, lambda: self.assertEqual(filename.read_text(), definition)
        finally:
            ctl("stop", unit, optional=True)
            filename.unlink(missing_ok=True)
            ctl("daemon-reload", optional=True)

    def check_upgrade(self, manager):
        accepted = self.update
        instance = accepted.legacy_installation()
        with manager as (launcher_pid, check_configuration):
            old_pid = self.wait_for(launcher_pid)
            self.wait_for(lambda: accepted.status()["state"] == "running")
            result = accepted.completed(accepted.install(adopt=True), "installed")
            new_pid = self.wait_for(launcher_pid)
            self.assertNotEqual(old_pid, new_pid)
            self.assertEqual(result["instance_id"], instance)
            accepted.active_health(instance, accepted.info)
            check_configuration()
            print(json.dumps({"target": accepted.info["target"], "before_launcher_pid": old_pid,
                              "after_launcher_pid": new_pid, "matching_instance": True,
                              "build_id": accepted.info["build_id"], "configuration_preserved": True}))

    def test_registered_launcher_upgrade(self):
        if sys.platform == "darwin":
            self.check_upgrade(self.launchd(keep_alive=True))
        elif sys.platform == "linux":
            self.check_upgrade(self.systemd())
        else:
            self.skipTest("Windows Task Scheduler requires a dedicated interactive test profile")

    @unittest.skipUnless(sys.platform == "darwin", "requires native launchd")
    def test_registered_demand_launcher_resumes_previously_running_job(self):
        self.check_upgrade(self.launchd(keep_alive=False))

    @unittest.skipUnless(sys.platform == "darwin", "requires native launchd")
    def test_registered_launchd_retires_after_legacy_budget(self):
        accepted = self.update
        instance_id = accepted.legacy_installation()
        target = f"gui/{os.getuid()}/com.botinkit.memory.v2.test.{self.fixture_id}"
        # The old updater allowed 35 seconds, whereas launchd can keep its
        # registration while the old daemon is still shutting down.
        hold_seconds = 36
        with self.launchd(keep_alive=True, exit_timeout=90) as (launcher_pid, check_configuration):
            old_pid = self.wait_for(launcher_pid)
            self.wait_for(lambda: accepted.status()["state"] == "running")
            with ThreadPoolExecutor(max_workers=1) as pool:
                os.kill(old_pid, signal.SIGSTOP)
                try:
                    updating = pool.submit(accepted.install, adopt=True)

                    def job_is_terminating():
                        state = self.run_checked("/bin/launchctl", "print", target, optional=True)
                        return (state.returncode == 0 and launcher_pid() == old_pid
                                and "SIGTERMed" in state.stdout)

                    self.wait_for(job_is_terminating)
                    time.sleep(hold_seconds)
                    self.assertTrue(job_is_terminating())
                finally:
                    observed = self.run_checked("/bin/ps", "-p", str(old_pid), "-o", "command=", optional=True)
                    if str(accepted.root) in observed.stdout and " service run" in observed.stdout:
                        try:
                            os.kill(old_pid, signal.SIGCONT)
                        except ProcessLookupError:
                            pass
                result = updating.result()
            receipt = accepted.completed(result, "installed")
            self.assertNotEqual(old_pid, self.wait_for(launcher_pid))
            accepted.active_health(instance_id, accepted.info)
            self.assertEqual(receipt["instance_id"], instance_id)
            self.assertEqual(receipt["phase"], "published")
            self.assertEqual(receipt["recovery_cleanup"], "complete")
            self.assertFalse((accepted.root / "run/update-barrier.json").exists())
            check_configuration()
            print(json.dumps({"held_terminating_seconds": hold_seconds, "status": receipt["status"],
                              "matching_instance": True, "configuration_preserved": True}))

    @unittest.skipUnless(sys.platform == "darwin", "requires native launchd")
    def test_registered_launchd_drains_late_legacy_owner(self):
        import fcntl

        accepted = self.update
        instance_id = accepted.legacy_installation()
        run = accepted.root / "run"
        launchd_budget = 60
        release_lease = Event()
        received_stop = Event()
        replacement_ready = Event()

        class RetiredEndpoint(BaseHTTPRequestHandler):
            def do_POST(self):
                if (self.path == "/v2/service/stop"
                        and self.headers.get("X-Bmem-Instance") == instance_id
                        and self.headers.get("Authorization", "").startswith("Bearer ")):
                    received_stop.set()
                    replacement_ready.wait(timeout=5)
                body = b'{"code":"unavailable","message":"previous listening daemon exited"}'
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Bmem-Instance", instance_id)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        def retain_lease_during_rebind():
            deadline = time.monotonic() + launchd_budget
            with (run / "service.lock").open("rb") as lease:
                while not release_lease.is_set() and time.monotonic() < deadline:
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        time.sleep(0.002)
                    else:
                        release_lease.wait(timeout=launchd_budget)
                        return

        with self.launchd(keep_alive=True) as (launcher_pid, check_configuration):
            old_pid = self.wait_for(launcher_pid)
            self.wait_for(lambda: accepted.status()["state"] == "running")
            with HTTPServer(("127.0.0.1", 0), RetiredEndpoint) as retired, ThreadPoolExecutor(max_workers=3) as pool:
                retired.timeout = launchd_budget
                # Preserve only the retiring service's stale discovery until
                # a real standalone 0.2.0 process publishes its own endpoint.
                discovery = run / "service.json"
                address = f"127.0.0.1:{retired.server_port}"
                replacement = discovery.with_suffix(".fixture.json")
                replacement.write_text(json.dumps({"instance_id": instance_id, "address": address}))
                replacement.replace(discovery)
                retiring = pool.submit(retired.handle_request)
                lease = pool.submit(retain_lease_during_rebind)
                updating = pool.submit(accepted.install, adopt=True)
                late_service = None
                try:
                    self.assertTrue(received_stop.wait(timeout=launchd_budget),
                                    "initial authenticated drain was not observed")
                    release_lease.set()
                    lease.result(timeout=4)
                    with (accepted.work / "late-service.log").open("w") as log:
                        late_service = subprocess.Popen(accepted.argv("service", "run", binary=accepted.canonical),
                                                        env=accepted.env, cwd=accepted.work,
                                                        stdin=subprocess.DEVNULL, stdout=log, stderr=log)

                    def new_endpoint_is_running():
                        value = json.loads(discovery.read_text())
                        return value["address"] != address and accepted.status()["state"] == "running"

                    self.wait_for(new_endpoint_is_running)
                    self.assertEqual(accepted.status()["version"], "0.2.0")
                    self.assertIsNone(late_service.poll(), "real standalone legacy writer has already exited")
                    replacement_ready.set()
                    retiring.result(timeout=8)
                    result = updating.result(timeout=120)
                    late_service.wait(timeout=5)
                finally:
                    release_lease.set()
                    replacement_ready.set()
                    if late_service is not None and late_service.poll() is None:
                        late_service.terminate()
                        late_service.wait(timeout=10)
            receipt = accepted.completed(result, "installed")
            self.assertNotEqual(old_pid, self.wait_for(launcher_pid))
            self.assertEqual(receipt["instance_id"], instance_id)
            self.assertEqual(receipt["phase"], "published")
            self.assertEqual(receipt["recovery_cleanup"], "complete")
            accepted.active_health(instance_id, accepted.info)
            self.assertFalse((run / "update-barrier.json").exists())
            check_configuration()
            print(json.dumps({"real_standalone_writer_observed": True, "status": receipt["status"],
                              "matching_instance": True, "configuration_preserved": True}))
