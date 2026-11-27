mod catalog;
mod cleanup;
mod files;
mod supervisor;

use crate::error::{Error, Result};
use crate::{build_info, client, platform, server};
use clap::{Args, Subcommand};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::fs::{self, File, OpenOptions};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};
use supervisor::Supervisor;
use uuid::Uuid;

const PROBE_WAIT: Duration = Duration::from_secs(12);
const DRAIN_WAIT: Duration = Duration::from_secs(35);

#[derive(Args)]
pub struct Options {
    #[arg(long)]
    check: bool,
    #[command(subcommand)]
    action: Option<Action>,
}

#[derive(Subcommand)]
enum Action {
    Status,
    Resume,
    Rollback,
    #[command(hide = true)]
    Apply {
        operation: Uuid,
    },
    #[command(hide = true)]
    Recover {
        operation: Uuid,
    },
}

#[derive(Clone, Deserialize, Serialize)]
struct Installation {
    instance_id: String,
    alias: PathBuf,
    executable: PathBuf,
    version: String,
    build_id: String,
    sha256: String,
    authority_epoch: i64,
    settings_epoch: i64,
    #[serde(default)]
    web_build_id: Option<String>,
    highest_version: String,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
enum Phase {
    Preflight,
    Drained,
    Snapshotted,
    Swapping,
    Swapped,
    Migrating,
    Migrated,
    Probed,
    Committed,
    Published,
    RolledBack,
}

impl Phase {
    fn terminal(self) -> bool {
        matches!(self, Self::Published | Self::RolledBack)
    }

    fn may_have_migrated(self) -> bool {
        matches!(self, Self::Migrating | Self::Migrated | Self::Probed)
    }
}

#[derive(Clone, Deserialize, Serialize)]
struct Transaction {
    id: Uuid,
    phase: Phase,
    previous: Option<Installation>,
    next: Installation,
    was_running: bool,
    #[serde(default)]
    supervisor: Option<Supervisor>,
    outcome: String,
    error_code: Option<String>,
    #[serde(default)]
    recovery_cleaned: bool,
}

impl Transaction {
    fn operation(&self) -> String {
        self.id.to_string()
    }

    fn status(&self) -> Value {
        let (status, installed, next) = match self.phase {
            Phase::Published => (self.outcome.as_str(), Some(&self.next), None),
            Phase::RolledBack => ("rolled_back", self.previous.as_ref(), None),
            _ => ("in_progress", self.previous.as_ref(), Some(&self.next)),
        };
        json!({
            "operation_id":self.id,"status":status,"phase":self.phase,"instance_id":self.next.instance_id,
            "installed_version":installed.map(|value| &value.version),
            "installed_build":installed.map(|value| &value.build_id),
            "target_version":next.map(|value| &value.version),"target_build":next.map(|value| &value.build_id),
            "error_code":self.error_code,
            "recovery_cleanup": if self.phase.terminal() {
                if self.recovery_cleaned {"complete"} else {"pending"}
            } else {"retained"},
            "next_command": if self.phase.terminal() && self.recovery_cleaned {None} else {Some("bmem update resume")},
        })
    }
}

struct State<'a> {
    instance: &'a platform::Instance,
    ledger: PathBuf,
    registry: PathBuf,
    work: PathBuf,
    recovery: PathBuf,
}

impl<'a> State<'a> {
    fn new(instance: &'a platform::Instance, executable: &Path) -> Result<Self> {
        let parent = executable
            .parent()
            .ok_or_else(|| Error::invalid("binary has no parent"))?;
        let run = instance.root.join("run");
        let work = parent.join(".bmem-updates");
        platform::private_directory(&work)?;
        let recovery = run.join("update-recovery");
        files::directory(&recovery)?;
        Ok(Self {
            instance,
            ledger: run.join("bmem-update.json"),
            registry: run.join("bmem-install.json"),
            work,
            recovery,
        })
    }

    fn transaction(&self) -> Result<Option<Transaction>> {
        files::read(&self.ledger)
    }

    fn save(&self, tx: &Transaction) -> Result<()> {
        files::save(&self.ledger, tx)
    }

    fn phase(&self, tx: &mut Transaction, phase: Phase) -> Result<()> {
        tx.phase = phase;
        self.save(tx)?;
        fault(phase)
    }

    fn finish(&self, tx: &mut Transaction, phase: Phase) -> Result<()> {
        self.phase(tx, phase)?;
        self.cleanup_terminal(tx)
    }

    fn cleanup_terminal(&self, tx: &mut Transaction) -> Result<()> {
        if !tx.phase.terminal()
            || tx.next.instance_id != self.instance.id
            || platform::update_barrier(self.instance)?.is_some()
        {
            return Err(Error::denied(
                "recovery cleanup requires an idle completed operation",
            ));
        }
        if !tx.recovery_cleaned
            && cleanup::completed_snapshot(&self.recovery, &self.instance.id, tx.id).is_ok()
        {
            tx.recovery_cleaned = true;
            self.save(tx)?;
        }
        Ok(())
    }

    fn finish_prior_cleanup(&self) -> Result<()> {
        if let Some(mut tx) = self.transaction()? {
            self.cleanup_terminal(&mut tx)?;
            if !tx.recovery_cleaned {
                return Err(Error::new(
                    503,
                    "update_cleanup_pending",
                    "the completed recovery point could not be removed; retry bmem update resume",
                ));
            }
        }
        Ok(())
    }

    fn stage(&self, tx: &Transaction) -> PathBuf {
        self.work
            .join(tx.operation())
            .join(executable_name("candidate"))
    }

    fn previous(&self, tx: &Transaction) -> PathBuf {
        self.work
            .join(tx.operation())
            .join(executable_name("previous"))
    }

    fn snapshot(&self, tx: &Transaction) -> PathBuf {
        self.recovery.join(tx.operation())
    }
}

fn fault(phase: Phase) -> Result<()> {
    #[cfg(feature = "update-test")]
    {
        let name = format!("{phase:?}").to_ascii_lowercase();
        if std::env::var("BMEM_UPDATE_TEST_CRASH_AT").is_ok_and(|requested| requested == name) {
            std::process::abort();
        }
        if std::env::var("BMEM_UPDATE_TEST_FAIL_AT").is_ok_and(|requested| requested == name) {
            return Err(Error::new(
                500,
                "isolated_update_fault",
                format!("injected failure at {name}"),
            ));
        }
    }
    #[cfg(not(feature = "update-test"))]
    let _ = phase;
    Ok(())
}

fn executable_name(stem: &str) -> String {
    format!("{stem}{}", std::env::consts::EXE_SUFFIX)
}

fn update_error(code: &str, message: &str) -> Error {
    Error::new(409, code, message)
}

pub fn exit_code(status: &Value) -> u8 {
    match status["status"].as_str() {
        Some("handed_off") => 10,
        Some("rolled_back") if !status["error_code"].is_null() => 22,
        Some("repair_required") => 23,
        _ => 0,
    }
}

pub fn error_exit(error: &Error) -> u8 {
    match error.code.as_str() {
        "repair_required" => 23,
        _ if matches!(error.status, 409 | 500 | 503)
            && matches!(
                error.code.as_str(),
                "update_in_progress"
                    | "temporarily_unavailable"
                    | "update_transport_failed"
                    | "update_cleanup_pending"
                    | "executable_in_use"
                    | "internal_error"
            ) =>
        {
            20
        }
        _ => 21,
    }
}

pub fn summary(instance: &platform::Instance) -> Result<Value> {
    let ledger = instance.root.join("run/bmem-update.json");
    if let Some(tx) = files::read::<Transaction>(&ledger)? {
        if tx.next.instance_id != instance.id {
            return Err(Error::denied("update record belongs to another instance"));
        }
        return Ok(tx.status());
    }
    let installation = files::read::<Installation>(&instance.root.join("run/bmem-install.json"))?;
    Ok(json!({
        "status": if installation.is_some() {"installed"} else {"unmanaged"},
        "instance_id":instance.id,"installed_version": installation.as_ref().map(|row| &row.version),
        "installed_build": installation.as_ref().map(|row| &row.build_id),
        "operation_id":null,"phase":null,"target_version":null,"target_build":null,
        "error_code":null,"next_command":null,
    }))
}

fn resolved_target(path: &Path, adopt: bool) -> Result<(PathBuf, PathBuf)> {
    if !path.is_absolute() {
        return Err(Error::invalid("give the absolute binary path"));
    }
    let parent = path
        .parent()
        .ok_or_else(|| Error::invalid("invalid binary path"))?
        .canonicalize()?;
    let name = path
        .file_name()
        .ok_or_else(|| Error::invalid("invalid binary filename"))?;
    let alias = parent.join(name);
    let real = if adopt {
        alias.canonicalize()?
    } else {
        alias.clone()
    };
    if !adopt && fs::symlink_metadata(&real).is_ok() {
        return Err(update_error(
            "installation_conflict",
            "use install --adopt for an existing binary",
        ));
    }
    if adopt && !fs::symlink_metadata(&real)?.is_file() {
        return Err(Error::denied("the chosen bmem is not a regular executable"));
    }
    let home = dirs::home_dir()
        .ok_or_else(|| Error::denied("no current user home"))?
        .canonicalize()?;
    if !real.starts_with(&home) || !alias.starts_with(&home) {
        #[cfg(feature = "update-test")]
        if std::env::var_os("BMEM_UPDATE_TEST_REPOSITORY").is_some()
            && real.starts_with(std::env::temp_dir().canonicalize()?)
            && alias.starts_with(std::env::temp_dir().canonicalize()?)
        {
            return Ok((alias, real));
        }
        return Err(Error::denied(
            "use a user-owned installation inside your home",
        ));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let owner = fs::metadata(&home)?.uid();
        let dir = fs::metadata(real.parent().expect("absolute executable has a parent"))?;
        if dir.uid() != owner
            || dir.mode() & 0o022 != 0
            || (adopt && fs::metadata(&real)?.uid() != owner)
        {
            return Err(Error::denied(
                "the binary and installation directory must belong to this user",
            ));
        }
    }
    Ok((alias, real))
}

fn reported_version(path: &Path) -> Result<String> {
    let output = Command::new(path).arg("--version").output()?;
    let text = std::str::from_utf8(&output.stdout).unwrap_or_default();
    if output.status.success()
        && let Some(version) = text.trim().strip_prefix("bmem ")
    {
        return Ok(version.into());
    }
    Err(Error::invalid(
        "the selected binary did not identify itself as bmem",
    ))
}

fn stored_epoch(instance: &platform::Instance) -> Result<i64> {
    let db = rusqlite::Connection::open_with_flags(
        instance.root.join("data/bmem.sqlite3"),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?;
    Ok(db.pragma_query_value(None, "user_version", |row| row.get(0))?)
}

fn require_digest(path: &Path, expected: &str) -> Result<()> {
    if files::digest(path)? != expected {
        return Err(update_error(
            "binary_changed",
            "binary did not match the update record",
        ));
    }
    Ok(())
}

fn check_installation(instance: &platform::Instance, install: &Installation) -> Result<()> {
    if instance.id != install.instance_id {
        return Err(Error::denied("installation belongs to another instance"));
    }
    let (alias, target) = resolved_target(&install.alias, true)?;
    if alias != install.alias || target != install.executable {
        return Err(Error::denied("the registered executable path changed"));
    }
    require_digest(&target, &install.sha256)
}

fn build_report(path: &Path) -> Result<Value> {
    let result = Command::new(path).args(["--json", "build-info"]).output()?;
    if !result.status.success() {
        return Err(update_error(
            "candidate_invalid",
            "verified candidate did not report its build identity",
        ));
    }
    Ok(serde_json::from_slice(&result.stdout)?)
}

fn verify_candidate(path: &Path, install: &Installation, commit: Option<&str>) -> Result<()> {
    require_digest(path, &install.sha256)?;
    let report = build_report(path)?;
    if report["version"] != install.version
        || report["build_id"] != install.build_id
        || report["target"] != build_info::TARGET
        || report["authority_epoch"] != install.authority_epoch
        || report["settings_epoch"] != install.settings_epoch
        || commit.is_some_and(|commit| report["commit"] != commit)
        || (!cfg!(feature = "update-test") && report["test_build"] != false)
    {
        return Err(update_error(
            "candidate_invalid",
            "verified candidate does not match its declared build",
        ));
    }
    Ok(())
}

fn verify_space(state: &State, len: u64) -> Result<()> {
    #[cfg(feature = "update-test")]
    if std::env::var("BMEM_UPDATE_TEST_FAIL_AT").as_deref() == Ok("no_space") {
        return Err(Error::new(
            507,
            "update_no_space",
            "insufficient space for the update",
        ));
    }
    let data = state.instance.root.join("data");
    let live = [
        "bmem.sqlite3",
        "bmem.sqlite3-wal",
        "settings.sqlite3",
        "settings.sqlite3-wal",
    ]
    .into_iter()
    .map(|name| data.join(name).metadata().map_or(0, |meta| meta.len()))
    .sum::<u64>();
    if fs2::available_space(&state.work)? < len.saturating_mul(3)
        || fs2::available_space(&state.recovery)? < live.saturating_mul(2)
    {
        return Err(Error::new(
            507,
            "update_no_space",
            "insufficient space for the staged program or database recovery point",
        ));
    }
    Ok(())
}

pub fn install(home: Option<&Path>, path: &Path, adopt: bool) -> Result<Value> {
    let (alias, real) = resolved_target(path, adopt)?;
    let instance = if adopt {
        platform::resolve(home)?
    } else {
        platform::init(home)?
    };
    let state = State::new(&instance, &real)?;
    let _root_lock = files::lock(&instance.root.join("run/bmem-update.lock"))?;
    let _lock = files::lock(&state.work.join("update.lock"))?;
    if files::read::<Installation>(&state.registry)?.is_some() {
        return Err(update_error(
            "already_managed",
            "run bmem update for an existing managed installation",
        ));
    }
    ensure_idle(&state)?;
    state.finish_prior_cleanup()?;
    let previous = if adopt {
        let version = reported_version(&real)?;
        if version != "0.2.0" {
            return Err(update_error(
                "legacy_version_unsupported",
                "adoption currently supports verified bmem 0.2.0 installations",
            ));
        }
        let sha256 = files::digest(&real)?;
        Some(Installation {
            instance_id: instance.id.clone(),
            alias: alias.clone(),
            executable: real.clone(),
            version: version.clone(),
            build_id: format!("legacy-{sha256}"),
            sha256,
            authority_epoch: stored_epoch(&instance)?,
            settings_epoch: build_info::SETTINGS_EPOCH,
            web_build_id: None,
            highest_version: version,
        })
    } else {
        None
    };
    let (version, build, epoch) = previous.as_ref().map_or(("0.0.0", "", 0), |source| {
        (
            source.version.as_str(),
            source.build_id.as_str(),
            source.authority_epoch,
        )
    });
    let release = catalog::choose(&state.work.join("trusted"), version, build, epoch, None)?
        .ok_or_else(|| {
            update_error(
                "release_unavailable",
                "no compatible signed bootstrap release is available",
            )
        })?;
    let mut tx = prepare(&state, previous, alias, real, release, "installed")?;
    dispatch(&state, &mut tx)
}

pub fn update(home: Option<&Path>, options: Options) -> Result<Value> {
    if options.check && options.action.is_some() {
        return Err(Error::invalid(
            "--check cannot be combined with an update subcommand",
        ));
    }
    let instance = platform::resolve(home)?;
    if matches!(&options.action, Some(Action::Status)) {
        return summary(&instance);
    }
    let helper = matches!(
        options.action,
        Some(Action::Apply { .. } | Action::Recover { .. })
    );
    let guard = instance.root.join("run/bmem-update.lock");
    let _root_lock = if helper {
        wait_update_lock(&guard)?
    } else {
        files::lock(&guard)?
    };
    let ledger = instance.root.join("run/bmem-update.json");
    let registry = instance.root.join("run/bmem-install.json");
    let saved = files::read::<Installation>(&registry)?;
    let txn = files::read::<Transaction>(&ledger)?;
    let install = if helper || matches!(options.action, Some(Action::Resume)) {
        txn.as_ref().map(|tx| &tx.next).or(saved.as_ref())
    } else {
        saved.as_ref()
    }
    .ok_or_else(|| {
        update_error(
            "installation_unmanaged",
            "use the verified installer to adopt this installation",
        )
    })?;
    if install.instance_id != instance.id {
        return Err(Error::denied("installation belongs to another instance"));
    }
    let state = State::new(&instance, &install.executable)?;
    let _lock = if helper {
        wait_update_lock(&state.work.join("update.lock"))?
    } else {
        files::lock(&state.work.join("update.lock"))?
    };
    match options.action {
        Some(Action::Apply { operation }) => {
            let mut tx = state
                .transaction()?
                .filter(|tx| tx.id == operation && tx.phase == Phase::Preflight)
                .ok_or_else(|| {
                    Error::denied("upgrade helper does not match a pending operation")
                })?;
            let stage = state.stage(&tx);
            verify_candidate(&stage, &tx.next, None)?;
            #[cfg(windows)]
            require_digest(&std::env::current_exe()?, &tx.next.sha256)?;
            apply(&state, &mut tx)
        }
        Some(Action::Recover { operation }) => {
            let mut tx = state
                .transaction()?
                .filter(|tx| {
                    tx.id == operation && !tx.phase.terminal() && tx.phase != Phase::Committed
                })
                .ok_or_else(|| {
                    Error::denied("recovery helper does not match a pending operation")
                })?;
            require_digest(&std::env::current_exe()?, &tx.next.sha256)?;
            restore(&state, &mut tx)?;
            Ok(tx.status())
        }
        Some(Action::Resume) => {
            let mut tx = state
                .transaction()?
                .ok_or_else(|| Error::missing("no update to resume"))?;
            match tx.phase {
                Phase::Committed => {
                    complete(&state, &mut tx).map_err(|_| update_error("repair_required", "the write epoch is committed; retry the verified repair without restoring old data"))?;
                }
                phase if phase.terminal() => {
                    state.cleanup_terminal(&mut tx)?;
                }
                _ => {
                    #[cfg(windows)]
                    if std::env::current_exe()?.canonicalize()? == tx.next.executable {
                        return handoff(&state, &tx, "recover");
                    }
                    restore(&state, &mut tx).map_err(|_| {
                        update_error(
                            "repair_required",
                            "could not verify or restore the pre-commit state",
                        )
                    })?;
                }
            }
            Ok(tx.status())
        }
        Some(Action::Rollback) => {
            check_installation(&instance, install)?;
            let mut tx = prepare_local_rollback(&state, install.clone())?;
            dispatch(&state, &mut tx)
        }
        _ => {
            if std::env::current_exe()?.canonicalize()? != install.executable {
                return Err(update_error(
                    "installation_conflict",
                    "this command is running from a different copy of bmem; use the registered executable path",
                ));
            }
            check_installation(&instance, install)?;
            ensure_idle(&state)?;
            if !options.check {
                state.finish_prior_cleanup()?;
            }
            let release = catalog::choose(
                &state.work.join("trusted"),
                &install.version,
                &install.build_id,
                install.authority_epoch,
                Some(&install.highest_version),
            )?;
            if options.check || release.is_none() {
                return Ok(json!({
                    "status": if release.is_some() { "update_available" } else { "up_to_date" },
                    "instance_id":instance.id,"installed_version":install.version,"installed_build":install.build_id,
                    "target_version":release.as_ref().map(|row| &row.version),
                    "target_build":release.as_ref().map(|row| &row.build_id),
                    "operation_id":null,"phase":null,"error_code":null,"next_command":null,
                }));
            }
            let mut tx = prepare(
                &state,
                Some(install.clone()),
                install.alias.clone(),
                install.executable.clone(),
                release.expect("checked above"),
                "updated",
            )?;
            dispatch(&state, &mut tx)
        }
    }
}

fn ensure_idle(state: &State) -> Result<()> {
    if state.transaction()?.is_some_and(|tx| !tx.phase.terminal())
        || platform::update_barrier(state.instance)?.is_some()
    {
        return Err(update_error(
            "update_in_progress",
            "complete the pending operation with bmem update resume",
        ));
    }
    Ok(())
}

fn prepare(
    state: &State,
    previous: Option<Installation>,
    alias: PathBuf,
    executable: PathBuf,
    release: catalog::Release,
    outcome: &str,
) -> Result<Transaction> {
    verify_space(state, release.len)?;
    let mut tx = Transaction {
        id: Uuid::new_v4(),
        phase: Phase::Preflight,
        outcome: outcome.into(),
        was_running: service_present(state.instance)?,
        supervisor: Supervisor::discover(state.instance, &executable)?,
        previous,
        error_code: None,
        recovery_cleaned: false,
        next: Installation {
            instance_id: state.instance.id.clone(),
            alias,
            executable,
            version: release.version.clone(),
            build_id: release.build_id.clone(),
            sha256: release.sha256.clone(),
            authority_epoch: release.authority_epoch,
            settings_epoch: release.settings_epoch,
            web_build_id: None,
            highest_version: release.version.clone(),
        },
    };
    let stage = state.stage(&tx);
    files::directory(stage.parent().expect("stage has a parent"))?;
    catalog::download(&release, &stage)?;
    files::executable(&stage)?;
    verify_candidate(&stage, &tx.next, Some(&release.commit))?;
    tx.next.web_build_id = build_report(&stage)?["web_build_id"]
        .as_str()
        .map(str::to_owned);
    state.save(&tx)?;
    fault(Phase::Preflight)?;
    Ok(tx)
}

fn prepare_local_rollback(state: &State, current: Installation) -> Result<Transaction> {
    ensure_idle(state)?;
    state.finish_prior_cleanup()?;
    let previous_transaction = state
        .transaction()?
        .ok_or_else(|| Error::missing("no prior installation"))?;
    let mut earlier = previous_transaction
        .previous
        .clone()
        .ok_or_else(|| Error::missing("no prior installation"))?;
    if previous_transaction.phase != Phase::Published
        || earlier.build_id.starts_with("legacy-")
        || earlier.authority_epoch != stored_epoch(state.instance)?
        || earlier.settings_epoch != current.settings_epoch
    {
        return Err(update_error(
            "repair_required",
            "prior program is incompatible with the current write format; use a signed forward repair",
        ));
    }
    earlier.highest_version = current.highest_version.clone();
    let before = state.previous(&previous_transaction);
    verify_candidate(&before, &earlier, None)?;
    let tx = Transaction {
        id: Uuid::new_v4(),
        phase: Phase::Preflight,
        outcome: "rolled_back".into(),
        was_running: service_present(state.instance)?,
        supervisor: Supervisor::discover(state.instance, &current.executable)?,
        previous: Some(current),
        next: earlier,
        error_code: None,
        recovery_cleaned: false,
    };
    let stage = state.stage(&tx);
    files::directory(stage.parent().expect("stage has a parent"))?;
    files::replace(&before, &stage)?;
    state.save(&tx)?;
    Ok(tx)
}

fn service_present(instance: &platform::Instance) -> Result<bool> {
    match platform::lock_service(instance) {
        Ok(_) => Ok(false),
        Err(error) if error.status == 409 => Ok(true),
        Err(error) => Err(error),
    }
}

fn wait_update_lock(path: &Path) -> Result<File> {
    let deadline = Instant::now() + PROBE_WAIT;
    loop {
        match files::lock(path) {
            Err(error) if error.code == "update_in_progress" && Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(50));
            }
            result => return result,
        }
    }
}

fn wait_service_lock(instance: &platform::Instance) -> Result<File> {
    let deadline = Instant::now() + DRAIN_WAIT;
    loop {
        match platform::lock_service(instance) {
            Err(error) if error.status == 409 && Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(60));
            }
            Err(error) if error.status == 409 => {
                return Err(Error::new(
                    503,
                    "update_in_progress",
                    "the service did not drain; stop its registered supervisor or close the busy sessions, then retry",
                ));
            }
            result => return result,
        }
    }
}

fn dispatch(state: &State, tx: &mut Transaction) -> Result<Value> {
    #[cfg(not(windows))]
    {
        apply(state, tx)
    }
    #[cfg(windows)]
    {
        handoff(state, tx, "apply")
    }
}

#[cfg(windows)]
fn handoff(state: &State, tx: &Transaction, action: &str) -> Result<Value> {
    let stage = state.stage(tx);
    require_digest(&stage, &tx.next.sha256)?;
    let helper = stage.with_file_name(format!("{action}.exe"));
    files::replace(&stage, &helper)?;
    require_digest(&helper, &tx.next.sha256)?;
    spawn(
        &helper,
        state.instance,
        &["update", action, &tx.operation()],
    )?;
    let mut status = tx.status();
    status["status"] = json!("handed_off");
    status["next_command"] = json!("bmem update status");
    Ok(status)
}

fn apply(state: &State, tx: &mut Transaction) -> Result<Value> {
    let result = switch(state, tx);
    #[cfg(feature = "update-test")]
    if let Err(error) = &result {
        eprintln!("update transaction failed at {:?} ({}): {error}", tx.phase, error.code);
    }
    match result {
        Ok(()) => Ok(tx.status()),
        Err(error) if matches!(tx.phase, Phase::Committed | Phase::Published) => {
            tx.error_code = Some(error.code);
            state.save(tx)?;
            if tx.phase == Phase::Committed {
                platform::set_update_barrier(state.instance, &tx.operation())?;
            }
            Err(update_error(
                "repair_required",
                "the new write epoch was committed; resume the installation or apply a signed forward repair",
            ))
        }
        Err(error) => {
            tx.error_code = Some(error.code);
            state.save(tx)?;
            restore(state, tx).map_err(|_| update_error("repair_required",
                "automatic recovery could not verify the saved state; keep maintenance active and resume using the verified installer"))?;
            Ok(tx.status())
        }
    }
}

fn switch(state: &State, tx: &mut Transaction) -> Result<()> {
    let operation = tx.operation();
    verify_candidate(&state.stage(tx), &tx.next, None)?;
    if let Some(old) = &tx.previous {
        check_installation(state.instance, old)?;
    }
    tx.was_running = service_present(state.instance)?;
    state.save(tx)?;
    platform::set_update_barrier(state.instance, &operation)?;
    pause(state, tx)?;
    let lock = wait_service_lock(state.instance)?;
    state.phase(tx, Phase::Drained)?;
    let recovery = state.snapshot(tx);
    files::directory(&recovery)?;
    server::snapshot_update(state.instance, &recovery)?;
    if let Some(old) = &tx.previous {
        files::replace(&old.executable, &state.previous(tx))?;
        require_digest(&state.previous(tx), &old.sha256)?;
    }
    state.phase(tx, Phase::Snapshotted)?;
    if let Some(old) = tx
        .previous
        .as_ref()
        .filter(|old| old.build_id.starts_with("legacy-"))
    {
        prove_legacy_fence(state, tx, old)?;
    }
    state.phase(tx, Phase::Swapping)?;
    files::replace(&state.stage(tx), &tx.next.executable)?;
    require_digest(&tx.next.executable, &tx.next.sha256)?;
    state.phase(tx, Phase::Swapped)?;
    state.phase(tx, Phase::Migrating)?;
    migrate(state, tx)?;
    state.phase(tx, Phase::Migrated)?;
    drop(lock);
    probation(state, tx)?;
    state.phase(tx, Phase::Probed)?;
    tx.phase = Phase::Committed;
    state.save(tx)?;
    fault(Phase::Committed)?;
    complete(state, tx)
}

fn pause(state: &State, tx: &Transaction) -> Result<()> {
    if let Some(supervisor) = &tx.supervisor {
        supervisor.pause()?;
        if supervisor.stops_running_job() && !tx.phase.may_have_migrated() {
            return Ok(());
        }
    }
    if client::Client::owner(state.instance).is_ok() {
        match client::drain_for_update(state.instance) {
            Err(error) if tx.supervisor.is_some() && error.status == 503 => Ok(()),
            result => result.map(|_| ()),
        }
    } else {
        Ok(())
    }
}

fn prove_legacy_fence(state: &State, tx: &Transaction, old: &Installation) -> Result<()> {
    let private = tempfile::tempdir_in(state.snapshot(tx))?;
    let root = private.path().join("data-root");
    let locator = private.path().join("locator");
    let mut args = Command::new(&state.stage(tx));
    args.arg("--home")
        .arg(&root)
        .args(["--json", "init", "--non-interactive"])
        .env("BMEM_LOCATOR_DIR", &locator);
    let output = args.output()?;
    if !output.status.success() {
        return Err(update_error(
            "migration_probe_failed",
            "could not initialize an isolated migration preview",
        ));
    }
    let identity: Value = serde_json::from_slice(&output.stdout)?;
    let instance = platform::Instance {
        root: root.clone(),
        id: identity["instance_id"]
            .as_str()
            .ok_or_else(|| Error::internal("preview has no instance ID"))?
            .into(),
        owner_token: String::new(),
    };
    for name in ["bmem.sqlite3", "settings.sqlite3"] {
        let source = state.snapshot(tx).join(name);
        if source.try_exists()? {
            fs::copy(source, root.join("data").join(name))?;
        }
    }
    let mut preview = tx.clone();
    preview.next.instance_id = instance.id.clone();
    preview.phase = Phase::Migrating;
    files::save(&root.join("run/bmem-update.json"), &preview)?;
    platform::set_update_barrier(&instance, &tx.operation())?;
    let _lease = wait_service_lock(&instance)?;
    let migrated = Command::new(state.stage(tx))
        .arg("--home")
        .arg(&root)
        .args(["--json", "service", "update-migrate", &tx.operation()])
        .env("BMEM_LOCATOR_DIR", &locator)
        .output()?;
    if !migrated.status.success() {
        return Err(update_error(
            "migration_probe_failed",
            "isolated database migration failed",
        ));
    }
    drop(_lease);
    let mut log = tempfile::tempfile_in(private.path())?;
    let mut legacy = Command::new(&old.executable)
        .arg("--home")
        .arg(&root)
        .args(["service", "run"])
        .env("BMEM_LOCATOR_DIR", &locator)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(log.try_clone()?)
        .spawn()?;
    let limit = Instant::now() + Duration::from_secs(4);
    let legacy_status = loop {
        if let Some(status) = legacy.try_wait()? {
            break status;
        }
        if Instant::now() >= limit {
            let _ = legacy.kill();
            let _ = legacy.wait();
            return Err(update_error(
                "legacy_fence_failed",
                "the chosen legacy executable could start against the migrated database",
            ));
        }
        std::thread::sleep(Duration::from_millis(50));
    };
    use std::io::{Read, Seek};
    log.rewind()?;
    let mut diagnostic = String::new();
    log.take(32 * 1024).read_to_string(&mut diagnostic)?;
    if legacy_status.success()
        || !(diagnostic.contains("bmem_writer_epoch")
            || diagnostic.contains("authority writer upgrade required")
            || diagnostic.contains("authority write epoch"))
    {
        return Err(update_error(
            "legacy_fence_failed",
            "could not verify the legacy write fence on the migrated copy",
        ));
    }
    Ok(())
}

fn migrate(state: &State, tx: &Transaction) -> Result<()> {
    let output = Command::new(&tx.next.executable)
        .arg("--home")
        .arg(&state.instance.root)
        .args(["--json", "service", "update-migrate", &tx.operation()])
        .output()?;
    if !output.status.success() {
        return Err(update_error(
            "migration_failed",
            "the new executable could not migrate and validate the isolated service",
        ));
    }
    Ok(())
}

fn spawn(binary: &Path, instance: &platform::Instance, args: &[&str]) -> Result<Child> {
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(instance.root.join("run/service.stderr.log"))?;
    let mut command = Command::new(binary);
    command
        .arg("--home")
        .arg(&instance.root)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(log);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    Ok(command.spawn()?)
}

fn healthy(
    instance: &platform::Instance,
    install: &Installation,
    probation: bool,
    mut child: Option<&mut Child>,
) -> Result<()> {
    let route = if probation {
        "/v2/update/health"
    } else {
        "/v2/health"
    };
    let deadline = Instant::now() + PROBE_WAIT;
    loop {
        if let Ok(result) =
            client::Client::owner(instance).and_then(|app| app.request("GET", route, None, None))
        {
            if result["version"] != install.version
                || result["instance_id"] != install.instance_id
                || install
                    .web_build_id
                    .as_deref()
                    .is_some_and(|expected| result["web_build_id"] != expected)
                || (probation
                    && (result["settings_epoch"] != install.settings_epoch
                        || result["database"] != "ready"
                        || result["lexical"] != "ready"))
                || (!install.build_id.starts_with("legacy-")
                    && result["build_id"] != install.build_id)
                || (probation && result["authority_epoch"] != install.authority_epoch)
            {
                return Err(update_error(
                    "candidate_health_mismatch",
                    "service response did not match the expected build and database format",
                ));
            }
            return Ok(());
        }
        if child
            .as_mut()
            .map(|child| child.try_wait())
            .transpose()?
            .flatten()
            .is_some()
            || Instant::now() >= deadline
        {
            return Err(update_error(
                "candidate_health_failed",
                "expected bmem service did not become healthy",
            ));
        }
        std::thread::sleep(Duration::from_millis(65));
    }
}

fn probation(state: &State, tx: &Transaction) -> Result<()> {
    let mut child = spawn(
        &tx.next.executable,
        state.instance,
        &["service", "update-probe", &tx.operation()],
    )?;
    let result = healthy(state.instance, &tx.next, true, Some(&mut child));
    if let Ok(app) = client::Client::owner(state.instance) {
        let _ = app.request("POST", "/v2/service/stop", Some(&json!({})), None);
    }
    if result.is_err() && child.try_wait()?.is_none() {
        let _ = child.kill();
        let _ = child.wait();
    }
    let _lock = wait_service_lock(state.instance)?;
    result
}

fn complete(state: &State, tx: &mut Transaction) -> Result<()> {
    require_digest(&tx.next.executable, &tx.next.sha256)?;
    let barrier = platform::update_barrier(state.instance)?;
    if barrier
        .as_deref()
        .is_some_and(|owner| owner != tx.operation())
    {
        return Err(Error::denied(
            "the write barrier belongs to another operation",
        ));
    }
    files::save(&state.registry, &tx.next)?;
    if barrier.is_some() {
        platform::clear_update_barrier(state.instance, &tx.operation())?;
    }
    if let Some(supervisor) = &tx.supervisor {
        supervisor.resume()?;
    }
    if tx.was_running {
        let online = client::running(state.instance)
            .and_then(|app| app.request("GET", "/v2/health", None, None))
            .is_ok_and(|value| value["build_id"] == tx.next.build_id);
        if !online {
            wait_for_restart(state, &tx.next, tx.supervisor.as_ref())?;
        }
    }
    tx.error_code = None;
    state.finish(tx, Phase::Published)
}

fn restore(state: &State, tx: &mut Transaction) -> Result<()> {
    let operation = tx.operation();
    if platform::update_barrier(state.instance)?.as_deref() == Some(&operation) {
        pause(state, tx)?;
        let lock = wait_service_lock(state.instance)?;
        if tx.phase.may_have_migrated() {
            server::restore_update(state.instance, &state.snapshot(tx))?;
        }
        if let Some(old) = &tx.previous {
            if files::digest(&old.executable).is_ok_and(|hash| hash == old.sha256) {
                // A sharing violation can leave the original file in place.
            } else {
                let previous = state.previous(tx);
                require_digest(&previous, &old.sha256)?;
                files::replace(&previous, &old.executable)?;
            }
            require_digest(&old.executable, &old.sha256)?;
        } else if tx.next.executable.exists() {
            require_digest(&tx.next.executable, &tx.next.sha256)?;
            fs::remove_file(&tx.next.executable)?;
            files::sync_directory(
                tx.next
                    .executable
                    .parent()
                    .expect("registered binary has a parent"),
            )?;
        }
        platform::clear_update_barrier(state.instance, &operation)?;
        drop(lock);
    }
    if let Some(supervisor) = &tx.supervisor {
        supervisor.resume()?;
    }
    if tx.was_running
        && let Some(old) = &tx.previous
    {
        let alive = client::running(state.instance)
            .and_then(|app| app.request("GET", "/v2/health", None, None))
            .is_ok_and(|value| {
                value["version"] == old.version
                    && (old.build_id.starts_with("legacy-") || value["build_id"] == old.build_id)
            });
        if !alive {
            wait_for_restart(state, old, tx.supervisor.as_ref())?;
        }
    }
    state.finish(tx, Phase::RolledBack)
}

fn wait_for_restart(
    state: &State,
    install: &Installation,
    supervisor: Option<&Supervisor>,
) -> Result<()> {
    let mut child = if !supervisor.is_some_and(Supervisor::resumes_running_job) {
        Some(spawn(
            &install.executable,
            state.instance,
            &["service", "run"],
        )?)
    } else {
        None
    };
    healthy(state.instance, install, false, child.as_mut())
}

pub fn verify_operation(instance: &platform::Instance, operation: &str) -> Result<()> {
    let record: Transaction = files::read(&instance.root.join("run/bmem-update.json"))?
        .ok_or_else(|| Error::denied("no pending update operation"))?;
    if record.id.to_string() != operation
        || record.next.instance_id != instance.id
        || platform::update_barrier(instance)?.as_deref() != Some(operation)
        || !matches!(record.phase, Phase::Migrating | Phase::Migrated)
    {
        return Err(Error::denied(
            "the update operation has no access to this phase",
        ));
    }
    require_digest(&std::env::current_exe()?, &record.next.sha256)
}
