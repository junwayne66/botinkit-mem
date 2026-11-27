use super::test_supervisor_id;
use crate::error::{Error, Result};
use crate::platform::Instance;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const SERVICE: &str = "org.freedesktop.systemd1";

#[derive(Clone, Serialize, Deserialize)]
pub struct Systemd {
    unit: String,
    fragment: PathBuf,
    fragment_sha256: String,
    binary: PathBuf,
    root: PathBuf,
}

impl Systemd {
    pub(super) fn discover(instance: &Instance, executable: &Path) -> Result<Option<Self>> {
        let unit = test_supervisor_id(instance, executable)?.map_or_else(
            || "bmem.service".into(),
            |id| format!("bmem-upgrade-test-{id}.service"),
        );
        let Some(config) = dirs::config_dir() else {
            return Ok(None);
        };
        let fragment = config.join("systemd/user").join(&unit);
        let Some(properties) = properties(&unit)? else {
            return Ok(None);
        };
        if matches!(properties["ActiveState"].as_str(), "inactive" | "failed") {
            return Ok(None);
        }
        let candidate = Self {
            unit,
            fragment_sha256: regular_file_sha256(&fragment)?,
            fragment,
            binary: executable.canonicalize()?,
            root: instance.root.clone(),
        };
        candidate.verify(&properties)?;
        Ok(Some(candidate))
    }

    fn verify(&self, live: &BTreeMap<String, String>) -> Result<()> {
        if live["Id"] != self.unit
            || live["LoadState"] != "loaded"
            || Path::new(&live["FragmentPath"]) != self.fragment
            || live["NeedDaemonReload"] != "no"
            || !live["DropInPaths"].is_empty()
            || !live["TriggeredBy"].is_empty()
            || ["ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost"]
                .iter()
                .any(|key| live.get(*key).is_some_and(|entry| !entry.is_empty()))
            || regular_file_sha256(&self.fragment)? != self.fragment_sha256
        {
            return Err(Error::conflict(
                "the systemd user registration differs from the selected installation",
            ));
        }
        // A stopped unit may unload as soon as systemctl releases its message.
        // Its already-verified fragment remains the source of its next launch.
        if matches!(live["ActiveState"].as_str(), "inactive" | "failed") {
            return Ok(());
        }
        let unit = bus(&[
            "call",
            SERVICE,
            "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager",
            "GetUnit",
            "s",
            &self.unit,
        ])?;
        let object = unit["data"][0]
            .as_str()
            .ok_or_else(|| Error::conflict("systemd did not identify the loaded user unit"))?;
        let start = bus(&[
            "get-property",
            SERVICE,
            object,
            "org.freedesktop.systemd1.Service",
            "ExecStart",
        ])?;
        if !self.matches_start(&start["data"]) {
            return Err(Error::conflict(
                "the running systemd user unit has a different executable or data root",
            ));
        }
        Ok(())
    }

    fn matches_start(&self, data: &Value) -> bool {
        let Some([entry]) = data.as_array().map(Vec::as_slice) else {
            return false;
        };
        let path = |value: &Value, expected: &Path| {
            value.as_str().map(Path::new).is_some_and(|path| {
                path.is_absolute() && path.canonicalize().is_ok_and(|value| value == expected)
            })
        };
        let Some([argv0, home, root, service, run]) = entry[1].as_array().map(Vec::as_slice) else {
            return false;
        };
        path(&entry[0], &self.binary)
            && path(argv0, &self.binary)
            && home == "--home"
            && path(root, &self.root)
            && service == "service"
            && run == "run"
    }

    pub(super) fn pause(&self) -> Result<()> {
        let live = self.properties()?;
        self.verify(&live)?;
        if !matches!(
            live["ActiveState"].as_str(),
            "inactive" | "failed" | "deactivating"
        ) {
            change("stop", &self.unit)?;
        }
        Ok(())
    }

    pub(super) fn resume(&self) -> Result<()> {
        let live = self.properties()?;
        self.verify(&live)?;
        if !matches!(live["ActiveState"].as_str(), "active" | "activating") {
            change("start", &self.unit)?;
        }
        Ok(())
    }

    fn properties(&self) -> Result<BTreeMap<String, String>> {
        properties(&self.unit)?
            .ok_or_else(|| Error::conflict("the registered systemd user unit is unavailable"))
    }
}

fn regular_file_sha256(path: &Path) -> Result<String> {
    if !fs::symlink_metadata(path)?.file_type().is_file() {
        return Err(Error::conflict(
            "the systemd user registration is not a regular file",
        ));
    }
    Ok(format!("{:x}", Sha256::digest(fs::read(path)?)))
}

fn properties(unit: &str) -> Result<Option<BTreeMap<String, String>>> {
    let properties = [
        "Id",
        "LoadState",
        "ActiveState",
        "FragmentPath",
        "NeedDaemonReload",
        "DropInPaths",
        "TriggeredBy",
        "ExecStartPre",
        "ExecStartPost",
        "ExecStop",
        "ExecStopPost",
    ];
    let output = systemctl()
        .args(["show", "--all"])
        .args(
            properties
                .iter()
                .flat_map(|property| ["--property", property]),
        )
        .arg(unit)
        .output();
    let output = match output {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        result => result?,
    };
    let stderr = String::from_utf8_lossy(&output.stderr);
    let parsed: BTreeMap<_, _> = String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| line.split_once('='))
        .map(|(key, value)| (key.to_owned(), value.to_owned()))
        .collect();
    if parsed
        .get("LoadState")
        .is_some_and(|value| value == "not-found")
        || (!output.status.success() && stderr.starts_with("Failed to connect to bus: "))
    {
        return Ok(None);
    }
    // systemctl does not render empty command arrays, even with --all.
    if !output.status.success() || !properties[..7].iter().all(|key| parsed.contains_key(*key)) {
        return Err(Error::conflict(
            "could not verify the systemd user registration",
        ));
    }
    Ok(Some(parsed))
}

fn systemctl() -> Command {
    let mut command = Command::new("/usr/bin/systemctl");
    command.args(["--user", "--no-pager"]).env("LC_ALL", "C");
    command
}

fn bus(arguments: &[&str]) -> Result<Value> {
    let output = Command::new("/usr/bin/busctl")
        .args(["--user", "--json=short", "--timeout=4s"])
        .args(arguments)
        .output()?;
    ensure_success(&output)?;
    Ok(serde_json::from_slice(&output.stdout)?)
}

fn change(action: &str, unit: &str) -> Result<()> {
    ensure_success(&systemctl().args(["--no-block", action, unit]).output()?)
}

fn ensure_success(output: &Output) -> Result<()> {
    if output.status.success() {
        Ok(())
    } else {
        Err(Error::conflict(
            "could not operate on the registered systemd user unit",
        ))
    }
}
