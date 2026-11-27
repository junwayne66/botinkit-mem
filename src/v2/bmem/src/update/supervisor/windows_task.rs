use super::test_supervisor_id;
use crate::error::{Error, Result};
use crate::platform::Instance;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Clone, Serialize, Deserialize)]
pub struct ScheduledTask {
    name: String,
    definition_sha256: String,
    enabled: bool,
    running: bool,
}

#[derive(Deserialize)]
struct Task {
    program: String,
    arguments: String,
    definition_sha256: String,
    enabled: bool,
    running: bool,
}

impl ScheduledTask {
    pub(super) fn discover(instance: &Instance, executable: &Path) -> Result<Option<Self>> {
        let name = test_supervisor_id(instance, executable)?.map_or_else(
            || "Botinkit Memory V2".into(),
            |id| format!("Botinkit Memory V2 test {id}"),
        );
        let Some(task) = run(&name, "read", "", false, false)? else {
            return Ok(None);
        };
        let path_matches = |candidate: &Path, expected: &Path| {
            candidate.is_absolute() && candidate.canonicalize().is_ok_and(|path| path == expected)
        };
        let arguments = split_arguments(&task.arguments)?;
        let binary = executable.canonicalize()?;
        let program = task.program.trim_matches('"');
        if !path_matches(Path::new(program), &binary)
            || !matches!(arguments.as_slice(), [home, root, service, run]
                if home == "--home" && path_matches(Path::new(root), &instance.root)
                    && service == "service" && run == "run")
        {
            return Err(Error::conflict(
                "the scheduled task has a different executable or data root",
            ));
        }
        if task.running && !task.enabled {
            return Err(Error::conflict(
                "stop the already disabled scheduled task before updating",
            ));
        }
        Ok(Some(Self {
            name,
            definition_sha256: task.definition_sha256,
            enabled: task.enabled,
            running: task.running,
        }))
    }

    pub(super) fn pause(&self) -> Result<()> {
        let task = self.operate("pause")?;
        if task.enabled {
            return Err(Error::conflict(
                "the registered scheduled task was not paused",
            ));
        }
        Ok(())
    }

    pub(super) fn resume(&self) -> Result<()> {
        let task = self.operate("resume")?;
        if task.enabled != self.enabled {
            return Err(Error::conflict(
                "the registered scheduled task was not restored",
            ));
        }
        Ok(())
    }

    pub(super) fn resumes_running_job(&self) -> bool {
        self.running
    }

    fn operate(&self, stage: &str) -> Result<Task> {
        run(
            &self.name,
            stage,
            &self.definition_sha256,
            self.enabled,
            self.running,
        )?
        .ok_or_else(|| Error::conflict("the registered scheduled task is unavailable"))
    }
}

fn run(
    name: &str,
    stage: &str,
    expected: &str,
    enabled: bool,
    running: bool,
) -> Result<Option<Task>> {
    let root = std::env::var_os("SystemRoot")
        .ok_or_else(|| Error::conflict("Windows system root is unavailable"))?;
    let powershell = PathBuf::from(root).join("System32/WindowsPowerShell/v1.0/powershell.exe");
    let output = Command::new(powershell)
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            TASK_SCRIPT,
        ])
        .env("BMEM_UPDATER_TASK_NAME", name)
        .env("BMEM_UPDATER_TASK_STAGE", stage)
        .env("BMEM_UPDATER_TASK_DEFINITION", expected)
        .env("BMEM_UPDATER_TASK_ENABLED", if enabled { "1" } else { "0" })
        .env("BMEM_UPDATER_TASK_RUNNING", if running { "1" } else { "0" })
        .output()?;
    if !output.status.success() {
        return Err(Error::conflict(
            "could not verify or operate on the registered Windows scheduled task",
        ));
    }
    Ok(serde_json::from_slice(&output.stdout)?)
}

const TASK_SCRIPT: &str = r#"
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$folder = $service.GetFolder('\')
$tasks = @($folder.GetTasks(1) | Where-Object { $_.Name -ieq $env:BMEM_UPDATER_TASK_NAME })
if ($tasks.Count -eq 0) { [Console]::WriteLine('null'); exit }
if ($tasks.Count -ne 1) { throw 'The scheduler task is ambiguous' }
$task = $tasks[0]
$def = $task.Definition
$user = $def.Principal.UserId
$owner = if ($user -match '^S-1-') { $user } else {
    ([Security.Principal.NTAccount]::new($user)).Translate([Security.Principal.SecurityIdentifier]).Value
}
if ($owner -ne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value -or
    $def.Principal.LogonType -ne 3 -or $def.Principal.RunLevel -ne 0 -or $def.Actions.Count -ne 1) {
    throw 'The scheduler task is not a limited current-user executable registration'
}
$action = $def.Actions.Item(1)
if ($action.Type -ne 0) { throw 'The scheduler task action is not an executable' }
[xml]$xml = $task.Xml
$node = $xml.SelectSingleNode("/*[local-name()='Task']/*[local-name()='Settings']/*[local-name()='Enabled']")
if ($null -ne $node) { $null = $node.ParentNode.RemoveChild($node) }
$sha = [Security.Cryptography.SHA256]::Create()
try { $hash = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($xml.OuterXml)))).Replace('-', '').ToLowerInvariant() }
finally { $sha.Dispose() }
switch ($env:BMEM_UPDATER_TASK_STAGE) {
    'read' {}
    'pause' {
        if ($hash -ne $env:BMEM_UPDATER_TASK_DEFINITION) { throw 'The scheduler task definition changed' }
        if ($task.Enabled) { $task.Enabled = $false }
    }
    'resume' {
        if ($hash -ne $env:BMEM_UPDATER_TASK_DEFINITION) { throw 'The scheduler task definition changed' }
        $enabled = $env:BMEM_UPDATER_TASK_ENABLED -eq '1'
        if ($task.Enabled -ne $enabled) { $task.Enabled = $enabled }
        if ($enabled -and $env:BMEM_UPDATER_TASK_RUNNING -eq '1' -and $task.State -notin 2,4) {
            $null = $task.Run($null)
        }
    }
    default { throw 'Invalid scheduler stage' }
}
[Console]::WriteLine((ConvertTo-Json -Compress -InputObject @{
    program = [string]$action.Path
    arguments = [string]$action.Arguments
    definition_sha256 = $hash
    enabled = [bool]$task.Enabled
    running = ($task.State -in 2,4)
}))
"#;

fn split_arguments(arguments: &str) -> Result<Vec<std::ffi::OsString>> {
    use std::ffi::{OsString, c_void};
    use std::os::windows::ffi::OsStringExt;

    #[link(name = "shell32")]
    unsafe extern "system" {
        fn CommandLineToArgvW(command: *const u16, count: *mut i32) -> *mut *mut u16;
    }
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn LocalFree(memory: *mut c_void) -> *mut c_void;
    }
    let command: Vec<u16> = format!("bmem.exe {arguments}")
        .encode_utf16()
        .chain([0])
        .collect();
    let mut count = 0;
    let raw = unsafe { CommandLineToArgvW(command.as_ptr(), &mut count) };
    if raw.is_null() {
        return Err(Error::invalid(
            "the scheduled task arguments could not be parsed",
        ));
    }
    // The scheduler stores a Windows command line; argv[0] is supplied by the executable action.
    unsafe {
        let result = std::slice::from_raw_parts(raw, count as usize)
            .iter()
            .skip(1)
            .map(|arg| {
                let len = (0..).take_while(|offset| *arg.add(*offset) != 0).count();
                OsString::from_wide(std::slice::from_raw_parts(*arg, len))
            })
            .collect();
        LocalFree(raw.cast());
        Ok(result)
    }
}
