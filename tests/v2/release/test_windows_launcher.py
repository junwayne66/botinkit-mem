"""Native Task Scheduler acceptance for an explicitly isolated Windows profile."""

import json
import os
from pathlib import Path
import stat
import subprocess
import time
import unittest
import uuid

import test_update


TASK_FIXTURE = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$folder = $scheduler.GetFolder('\')
$name = $env:BMEM_FIXTURE_TASK_NAME
$tasks = @($folder.GetTasks(1) | Where-Object { $_.Name -ieq $name })
$stage = $env:BMEM_FIXTURE_TASK_STAGE
if ($stage -eq 'preflight') {
    if ($tasks.Count -ne 0) { throw 'Fixture task name is already registered' }
    if ([Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0) { throw 'An interactive profile is required' }
    foreach ($scope in 'User', 'Machine') {
        if ([Environment]::GetEnvironmentVariable('BMEM_LOCATOR_DIR', $scope)) { throw 'The profile overrides the default bmem locator' }
    }
    [Console]::WriteLine((ConvertTo-Json -Compress -InputObject @{
        local_app_data = [Environment]::GetFolderPath('LocalApplicationData')
        profile = [Environment]::GetFolderPath('UserProfile')
    }))
    exit
}
if ($stage -eq 'create') {
    if ($tasks.Count -ne 0) { throw 'Refusing to replace an existing scheduled task' }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $definition = $scheduler.NewTask(0)
    $definition.RegistrationInfo.Description = $name
    $definition.Principal.UserId = $sid
    $definition.Principal.LogonType = 3
    $definition.Principal.RunLevel = 0
    $definition.Settings.Enabled = $true
    $definition.Settings.ExecutionTimeLimit = 'PT0S'
    $definition.Settings.DisallowStartIfOnBatteries = $false
    $definition.Settings.StopIfGoingOnBatteries = $false
    $logon = $definition.Triggers.Create(9)
    $logon.UserId = $sid
    $action = $definition.Actions.Create(0)
    $action.Path = $env:BMEM_FIXTURE_TASK_EXE
    $action.Arguments = $env:BMEM_FIXTURE_TASK_ARGS
    $task = $folder.RegisterTaskDefinition($name, $definition, 2, $sid, $null, 3, $null)
} else {
    if ($tasks.Count -eq 0 -and $stage -eq 'delete') { [Console]::WriteLine('null'); exit }
    if ($tasks.Count -ne 1) { throw 'Fixture task identity is ambiguous' }
    $task = $tasks[0]
}
$definition = $task.Definition
if ($definition.RegistrationInfo.Description -ne $name -or $definition.Actions.Count -ne 1 -or
    $definition.Actions.Item(1).Path -ne $env:BMEM_FIXTURE_TASK_EXE -or
    $definition.Actions.Item(1).Arguments -cne $env:BMEM_FIXTURE_TASK_ARGS) { throw 'Fixture task does not match the expected action' }
switch ($stage) {
    'start' { $null = $task.Run($null) }
    'delete' {
        if ($task.GetInstances(0).Count -gt 0) { $task.Stop(0) }
        $folder.DeleteTask($name, 0)
        [Console]::WriteLine('null')
        exit
    }
    'create' {}
    'read' {}
    default { throw 'Unknown fixture stage' }
}
$instances = $task.GetInstances(0)
[Console]::WriteLine((ConvertTo-Json -Compress -InputObject @{
    instance = if ($instances.Count -eq 1) { [string]$instances.Item(1).InstanceGuid } else { $null }
    count = $instances.Count
    enabled = [bool]$task.Enabled
    state = [int]$task.State
    last_task_result = '0x{0:X8}' -f ([int64]$task.LastTaskResult -band 4294967295L)
    xml = [string]$task.Xml
}))
"""


@unittest.skipUnless(os.name == "nt", "requires native Windows Task Scheduler")
class WindowsTaskSchedulerAcceptance(unittest.TestCase):
    def setUp(self):
        if os.environ.get("BMEM_UPDATE_ISOLATED_TASK_SCHEDULER") != "1":
            self.skipTest("requires an explicitly isolated, fresh interactive Windows profile")
        if not os.environ.get("BMEM_UPDATE_TEST_BIN"):
            self.skipTest("requires the signed isolated update fixture")
        self.update = test_update.UpdateAcceptance("runTest")
        self.update.setUpClass()
        self.addCleanup(self.update.doCleanups)
        self.update.setUp()
        fixture_id = uuid.uuid4()
        self.update.env["BMEM_UPDATE_TEST_SUPERVISOR"] = str(fixture_id)
        self.arguments = subprocess.list2cmdline(["--home", str(self.update.root), "service", "run"])
        self.task_name = f"Botinkit Memory V2 test {fixture_id}"
        profile = self.scheduler("preflight")
        local = Path(profile["local_app_data"])
        user = Path(profile["profile"])
        self.assertTrue(local.is_dir() and user.is_dir(), "Windows default profile paths are unavailable")
        self.locator_parent = local / "bmem"
        for path in (self.locator_parent, user / ".bmem"):
            if os.path.lexists(path):
                self.skipTest("the Windows profile already has bmem state; use a fresh profile")
        self.update.env.pop("BMEM_LOCATOR_DIR")
        self.update.env["LOCALAPPDATA"] = str(local)
        self.update.assert_windows_native_images(self.update.legacy, self.update.test_binary)
        self.addCleanup(self.remove_fixture_locator)

    def scheduler(self, stage):
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", TASK_FIXTURE],
            env={**self.update.env, "BMEM_FIXTURE_TASK_NAME": self.task_name,
                 "BMEM_FIXTURE_TASK_STAGE": stage, "BMEM_FIXTURE_TASK_EXE": str(self.update.launcher),
                 "BMEM_FIXTURE_TASK_ARGS": self.arguments}, cwd=self.update.work,
            capture_output=True, text=True, encoding="utf-8", timeout=25,
        )
        self.assertEqual(result.returncode, 0, (stage, result.stderr))
        return json.loads(result.stdout)

    def remove_fixture_locator(self):
        self.update.stop()
        if not os.path.lexists(self.locator_parent):
            return
        self.assertTrue(hasattr(self, "instance_id"), "preserving locator without a confirmed fixture instance")
        self.assert_real_path(self.locator_parent, directory=True)
        paths = list(self.locator_parent.iterdir())
        self.assertEqual(len(paths), 1, "preserving unexpected contents in the local bmem configuration path")
        folder = paths[0]
        self.assertRegex(folder.name, r"^[0-9a-f]{64}$")
        self.assert_real_path(folder, directory=True)
        owned = {"active.json", "registration.json", "init.lock"}
        self.assertEqual({item.name for item in folder.iterdir()}, owned, "preserving unowned locator files")
        for name in owned:
            self.assert_real_path(folder / name, directory=False)
        for name in ("active.json", "registration.json"):
            document = json.loads((folder / name).read_text(encoding="utf-8"))
            self.assertTrue(Path(document["root"]).samefile(self.update.root),
                            "preserving locator for a different data root")
            self.assertEqual(document["instance_id"], self.instance_id)
        for name in owned:
            (folder / name).unlink()
        folder.rmdir()
        self.locator_parent.rmdir()

    def assert_real_path(self, path, *, directory):
        metadata = path.lstat()
        self.assertFalse(metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        self.assertTrue((stat.S_ISDIR if directory else stat.S_ISREG)(metadata.st_mode))

    def wait_for_task_state(self, running):
        deadline = time.monotonic() + 30
        last = None
        while time.monotonic() < deadline:
            task = self.scheduler("read")
            self.assertLessEqual(task["count"], 1)
            result = self.update.call("status", timeout=10)
            try:
                status = json.loads(result.stdout or result.stderr)
            except json.JSONDecodeError:
                status = {}
            state = status.get("state")
            last = {"task_state": task["state"], "task_result": task["last_task_result"],
                    "task_instances": task["count"], "bmem_exit": result.returncode,
                    "bmem_state": state, "bmem_code": status.get("code")}
            if (result.returncode == 0 and bool(task["instance"]) == running
                    and state == ("running" if running else "stopped")):
                return task
            time.sleep(0.2)
        self.fail(f"Task Scheduler did not reach the expected running state ({running}): {last}")

    def test_registered_task_restores_running_or_stopped_state(self):
        self.instance_id = self.update.legacy_installation()
        self.addCleanup(self.scheduler, "delete")
        baseline = self.scheduler("create")
        self.scheduler("start")
        old = self.wait_for_task_state(running=True)
        self.update.completed(self.update.install(adopt=True), "installed")
        current = self.wait_for_task_state(running=True)
        self.assertNotEqual(old["instance"], current["instance"])
        self.assertTrue(current["enabled"])
        self.assertEqual(current["xml"], baseline["xml"])
        self.update.active_health(self.instance_id, self.update.info)

        self.update.stop()
        self.wait_for_task_state(running=False)
        next_info, next_image = self.update.add_next_release()
        self.update.assert_windows_native_images(next_image)
        self.update.completed(self.update.call("update"), "updated")
        stopped = self.scheduler("read")
        self.assertTrue(stopped["enabled"])
        self.assertEqual(stopped["count"], 0)
        self.assertEqual(stopped["xml"], baseline["xml"])
        self.assertEqual(self.update.status()["state"], "stopped")
        self.scheduler("start")
        self.wait_for_task_state(running=True)
        self.update.active_health(self.instance_id, next_info)
        print(json.dumps({"target": next_info["target"], "build_id": next_info["build_id"],
                          "scheduler_restart_verified": True, "configuration_preserved": True,
                          "stopped_state_preserved": True}))


if __name__ == "__main__":
    unittest.main()
