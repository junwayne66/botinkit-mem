use crate::error::{Error, Result};
use crate::platform::Instance;
use serde::{Deserialize, Serialize};
use std::path::Path;
#[cfg(target_os = "macos")]
use std::path::PathBuf;

#[cfg(target_os = "linux")]
mod systemd;
#[cfg(target_os = "linux")]
use systemd::Systemd;
#[cfg(windows)]
mod windows_task;
#[cfg(windows)]
use windows_task::ScheduledTask;

/// A previously loaded, verified supervisor belonging to the selected instance.
#[derive(Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Supervisor {
    #[cfg(target_os = "macos")]
    Launchd(Launchd),
    #[cfg(target_os = "linux")]
    Systemd(Systemd),
    #[cfg(windows)]
    ScheduledTask(ScheduledTask),
}

#[cfg(target_os = "macos")]
#[derive(Clone, Serialize, Deserialize)]
pub struct Launchd {
    domain: String,
    plist: PathBuf,
    plist_sha256: String,
    binary: PathBuf,
    root: PathBuf,
    #[serde(default = "default_label")]
    label: String,
    #[serde(default)]
    running: bool,
}

#[cfg(target_os = "macos")]
#[derive(Clone, Copy, PartialEq, Eq)]
enum LaunchdState {
    Running,
    Stopped,
    Retiring,
}

impl Supervisor {
    pub fn discover(instance: &Instance, executable: &Path) -> Result<Option<Self>> {
        #[cfg(target_os = "macos")]
        {
            Launchd::discover(instance, executable).map(|item| item.map(Self::Launchd))
        }
        #[cfg(target_os = "linux")]
        {
            Systemd::discover(instance, executable).map(|item| item.map(Self::Systemd))
        }
        #[cfg(windows)]
        {
            ScheduledTask::discover(instance, executable).map(|item| item.map(Self::ScheduledTask))
        }
        #[cfg(not(any(target_os = "macos", target_os = "linux", windows)))]
        {
            let _ = (instance, executable);
            Ok(None)
        }
    }

    pub fn pause(&self) -> Result<()> {
        match self {
            #[cfg(target_os = "macos")]
            Self::Launchd(item) => item.pause(),
            #[cfg(target_os = "linux")]
            Self::Systemd(item) => item.pause(),
            #[cfg(windows)]
            Self::ScheduledTask(item) => item.pause(),
            #[cfg(not(any(target_os = "macos", target_os = "linux", windows)))]
            _ => Err(Error::conflict("unsupported registered updater supervisor")),
        }
    }

    pub fn resume(&self) -> Result<()> {
        match self {
            #[cfg(target_os = "macos")]
            Self::Launchd(item) => item.resume(),
            #[cfg(target_os = "linux")]
            Self::Systemd(item) => item.resume(),
            #[cfg(windows)]
            Self::ScheduledTask(item) => item.resume(),
            #[cfg(not(any(target_os = "macos", target_os = "linux", windows)))]
            _ => Err(Error::conflict("unsupported registered updater supervisor")),
        }
    }

    pub fn resumes_running_job(&self) -> bool {
        match self {
            #[cfg(target_os = "macos")]
            Self::Launchd(item) => item.running,
            #[cfg(target_os = "linux")]
            Self::Systemd(_) => true,
            #[cfg(windows)]
            Self::ScheduledTask(item) => item.resumes_running_job(),
            #[cfg(not(any(target_os = "macos", target_os = "linux", windows)))]
            _ => false,
        }
    }
}

#[cfg(target_os = "macos")]
fn default_label() -> String {
    "com.botinkit.memory.v2".into()
}

#[cfg(target_os = "macos")]
impl Launchd {
    fn discover(instance: &Instance, executable: &Path) -> Result<Option<Self>> {
        use sha2::{Digest, Sha256};
        use std::{fs, io, process::Command};

        let Some(home) = dirs::home_dir() else {
            return Ok(None);
        };
        let label = test_supervisor_id(instance, executable)?
            .map(|id| format!("com.botinkit.memory.v2.test.{id}"))
            .unwrap_or_else(default_label);
        let plist = home
            .join("Library/LaunchAgents")
            .join(format!("{label}.plist"));
        let bytes = match fs::symlink_metadata(&plist) {
            Ok(info) if info.file_type().is_file() => fs::read(&plist)?,
            Ok(_) => {
                return Err(Error::conflict(
                    "the known launchd registration is not a regular file",
                ));
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        let uid = Command::new("/usr/bin/id").arg("-u").output()?;
        let uid_text = String::from_utf8_lossy(&uid.stdout);
        let uid = uid_text
            .trim()
            .parse::<u32>()
            .map_err(|_| Error::internal("cannot determine the user's launchd domain"))?;
        let mut candidate = Self {
            domain: format!("gui/{uid}"),
            plist,
            plist_sha256: format!("{:x}", Sha256::digest(bytes)),
            binary: executable.canonicalize()?,
            root: instance.root.clone(),
            label,
            running: false,
        };
        candidate.verify_plist()?;
        if let Some(state) = candidate.loaded()? {
            candidate.running = state != LaunchdState::Stopped;
            Ok(Some(candidate))
        } else {
            Ok(None)
        }
    }

    fn target(&self) -> String {
        format!("{}/{}", self.domain, self.label)
    }

    fn pause(&self) -> Result<()> {
        self.verify_plist()?;
        match self.loaded()? {
            None => return Ok(()),
            Some(LaunchdState::Retiring) => {}
            Some(_) => self.launchctl(&["bootout".into(), self.target()])?,
        }
        self.wait_for_retirement()
    }

    fn wait_for_retirement(&self) -> Result<()> {
        use std::time::{Duration, Instant};

        // launchd can retain a terminating job beyond the service-lease budget.
        let deadline = Instant::now() + Duration::from_secs(90);
        while self.loaded()?.is_some() {
            if Instant::now() >= deadline {
                return Err(Error::new(
                    503,
                    "launchd_retirement_timeout",
                    "the registered launchd service did not unregister in time",
                ));
            }
            std::thread::sleep(Duration::from_millis(150));
        }
        Ok(())
    }

    fn resume(&self) -> Result<()> {
        self.verify_plist()?;
        let mut state = self.loaded()?;
        if state == Some(LaunchdState::Retiring) {
            self.wait_for_retirement()?;
            state = None;
        }
        if state.is_none() {
            self.launchctl(&[
                "bootstrap".into(),
                self.domain.clone(),
                path_text(&self.plist)?.to_owned(),
            ])?;
            state = self.loaded()?;
            if state.is_none() {
                return Err(Error::conflict(
                    "the registered launchd service was not restored",
                ));
            }
        }
        if self.running && state == Some(LaunchdState::Stopped) {
            self.launchctl(&["kickstart".into(), self.target()])?;
        }
        Ok(())
    }

    fn verify_plist(&self) -> Result<()> {
        use sha2::{Digest, Sha256};
        use std::{fs, process::Command};

        let hash = format!("{:x}", Sha256::digest(fs::read(&self.plist)?));
        if hash != self.plist_sha256 {
            return Err(Error::conflict(
                "the launchd registration changed during the update",
            ));
        }
        let output = Command::new("/usr/bin/plutil")
            .args(["-convert", "json", "-o", "-"])
            .arg(&self.plist)
            .output()?;
        if !output.status.success() {
            return Err(Error::conflict("could not verify the launchd registration"));
        }
        let config: serde_json::Value = serde_json::from_slice(&output.stdout)?;
        let expected = config["ProgramArguments"]
            .as_array()
            .ok_or_else(|| Error::conflict("the launchd registration has no program arguments"))?;
        let arguments = expected
            .iter()
            .map(|entry| entry.as_str().unwrap_or(""))
            .collect::<Vec<_>>();
        if config["Label"] != self.label
            || !self.matches_arguments(&arguments)
            || config
                .get("Program")
                .is_some_and(|program| program.as_str().is_none_or(|p| !self.matches_executable(p)))
        {
            return Err(Error::conflict(
                "the launchd registration belongs to a different executable or data root",
            ));
        }
        Ok(())
    }

    fn matches_executable(&self, value: &str) -> bool {
        let path = Path::new(value);
        path.is_absolute() && path.canonicalize().is_ok_and(|path| path == self.binary)
    }

    fn matches_arguments(&self, args: &[&str]) -> bool {
        match args {
            [executable, "--home", root, "service", "run"] => {
                self.matches_executable(executable)
                    && Path::new(root)
                        .canonicalize()
                        .is_ok_and(|path| path == self.root)
            }
            _ => false,
        }
    }

    fn loaded(&self) -> Result<Option<LaunchdState>> {
        use std::process::Command;
        let output = Command::new("/bin/launchctl")
            .args(["print", self.target().as_str()])
            .env("LC_ALL", "C")
            .output()?;
        if output.status.code() == Some(113) {
            return Ok(None);
        }
        if !output.status.success() {
            return Err(Error::conflict(
                "could not verify the loaded launchd registration",
            ));
        }
        let text = String::from_utf8_lossy(&output.stdout);
        let running = text.lines().any(|line| {
            line.trim()
                .strip_prefix("pid = ")
                .and_then(|pid| pid.parse::<u32>().ok())
                .is_some_and(|pid| pid != 0)
        });
        let mut lines = text.lines().map(str::trim);
        let scan = lines.by_ref();
        let mut imported_path = None;
        let mut program = None;
        let mut argv = None;
        while let Some(line) = scan.next() {
            if let Some(path) = line.strip_prefix("path = ") {
                imported_path = Some(path);
            } else if let Some(executable) = line.strip_prefix("program = ") {
                program = Some(executable);
            } else if line == "arguments = {" {
                argv = Some(
                    scan.by_ref()
                        .take_while(|line| *line != "}")
                        .collect::<Vec<_>>(),
                );
                break;
            }
        }
        if imported_path != Some(path_text(&self.plist)?)
            || program.is_none_or(|path| !self.matches_executable(path))
            || argv.is_none_or(|args| !self.matches_arguments(&args))
        {
            return Err(Error::conflict(
                "the loaded launchd registration does not match the selected instance",
            ));
        }
        let state = text
            .lines()
            .find_map(|line| line.strip_prefix("\tstate = "));
        Ok(Some(match state {
            Some("SIGTERMed" | "SIGKILLed") => LaunchdState::Retiring,
            _ if running => LaunchdState::Running,
            _ => LaunchdState::Stopped,
        }))
    }

    fn launchctl(&self, arguments: &[String]) -> Result<()> {
        let status = std::process::Command::new("/bin/launchctl")
            .args(arguments)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()?;
        if !status.success() {
            return Err(Error::conflict(
                "could not change the registered launchd service state",
            ));
        }
        Ok(())
    }
}

#[cfg(target_os = "macos")]
fn path_text(path: &Path) -> Result<&str> {
    path.to_str()
        .ok_or_else(|| Error::invalid("launchd registration path is not UTF-8"))
}

#[cfg(any(target_os = "macos", target_os = "linux", windows))]
fn test_supervisor_id(instance: &Instance, executable: &Path) -> Result<Option<uuid::Uuid>> {
    #[cfg(feature = "update-test")]
    if let Some(name) = std::env::var_os("BMEM_UPDATE_TEST_SUPERVISOR") {
        use std::path::PathBuf;
        let id = uuid::Uuid::parse_str(&name.to_string_lossy())
            .map_err(|_| Error::invalid("invalid isolated updater supervisor ID"))?;
        let repository = std::env::var_os("BMEM_UPDATE_TEST_REPOSITORY")
            .ok_or_else(|| Error::denied("an isolated supervisor requires a test repository"))?;
        let repository = PathBuf::from(repository).canonicalize()?;
        let fixture = repository
            .parent()
            .ok_or_else(|| Error::denied("invalid isolated updater fixture"))?;
        if !fixture.starts_with(std::env::temp_dir().canonicalize()?)
            || !instance.root.starts_with(fixture)
            || !executable.starts_with(fixture)
        {
            return Err(Error::denied(
                "isolated updater supervisor must use the same temporary fixture",
            ));
        }
        return Ok(Some(id));
    }
    let _ = (instance, executable);
    Ok(None)
}
