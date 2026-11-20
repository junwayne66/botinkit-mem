use crate::error::{Error, Result};
use rusqlite::Connection;
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::net::SocketAddr;
use std::path::{Component, Path, PathBuf};
use uuid::Uuid;

pub struct Instance {
    pub root: PathBuf,
    pub id: String,
    pub owner_token: String,
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
struct Binding {
    root: PathBuf,
    instance_id: String,
}

#[derive(Serialize, Deserialize)]
struct Locator {
    version: u32,
    state: String,
    #[serde(flatten)]
    binding: Binding,
}

#[derive(Serialize, Deserialize)]
struct Registration {
    active: bool,
    #[serde(flatten)]
    binding: Binding,
}

#[derive(Serialize, Deserialize)]
struct Owner {
    instance_id: String,
    owner_token: String,
}

#[derive(Serialize, Deserialize)]
struct Endpoint {
    instance_id: String,
    address: SocketAddr,
}

fn machine_key() -> Result<String> {
    #[cfg(target_os = "linux")]
    let identifier = fs::read_to_string("/etc/machine-id")
        .or_else(|_| fs::read_to_string("/var/lib/dbus/machine-id"))?;
    #[cfg(target_os = "macos")]
    let identifier = {
        let output = std::process::Command::new("/usr/sbin/ioreg")
            .args(["-rd1", "-c", "IOPlatformExpertDevice"])
            .output()?;
        String::from_utf8_lossy(&output.stdout)
            .lines()
            .find(|line| line.contains("\"IOPlatformUUID\""))
            .and_then(|line| line.split_once('='))
            .map(|(_, value)| value.trim().to_owned())
            .ok_or_else(|| Error::unavailable("unable to read the macOS machine identifier"))?
    };
    #[cfg(windows)]
    let identifier = {
        let output = std::process::Command::new("reg.exe")
            .args([
                "query",
                "HKLM\\SOFTWARE\\Microsoft\\Cryptography",
                "/v",
                "MachineGuid",
            ])
            .output()?;
        String::from_utf8_lossy(&output.stdout)
            .lines()
            .find(|line| line.contains("MachineGuid"))
            .and_then(|line| line.split_whitespace().last())
            .map(str::to_owned)
            .ok_or_else(|| Error::unavailable("unable to read the Windows machine identifier"))?
    };
    #[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
    let identifier = std::env::var("HOSTNAME")
        .map_err(|_| Error::unavailable("unable to read the machine identifier"))?;
    if identifier.trim().is_empty() {
        return Err(Error::unavailable("machine identifier is empty"));
    }
    Ok(format!(
        "{:x}",
        Sha256::digest(identifier.trim().as_bytes())
    ))
}

fn locator_dir() -> Result<PathBuf> {
    if let Some(path) = std::env::var_os("BMEM_LOCATOR_DIR") {
        return canonical_candidate(Path::new(&path));
    }
    #[cfg(windows)]
    let base = dirs::data_local_dir();
    #[cfg(not(windows))]
    let base = dirs::config_dir();
    Ok(base
        .ok_or_else(|| Error::unavailable("no user settings directory"))?
        .join("bmem")
        .join(machine_key()?))
}

fn default_root() -> Result<PathBuf> {
    if let Some(path) = std::env::var_os("BMEM_DEFAULT_HOME") {
        return canonical_candidate(Path::new(&path));
    }
    Ok(dirs::home_dir()
        .ok_or_else(|| Error::unavailable("no user home directory"))?
        .join(".bmem"))
}

fn canonical_candidate(path: &Path) -> Result<PathBuf> {
    if !path.is_absolute() || path.components().any(|part| part == Component::ParentDir) {
        return Err(Error::invalid(
            "supply an absolute path without parent traversal",
        ));
    }
    match path.canonicalize() {
        Ok(path) => Ok(path),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            let parent = path.parent().ok_or(error)?;
            let name = path
                .file_name()
                .ok_or_else(|| Error::invalid("invalid data root"))?;
            Ok(canonical_candidate(parent)?.join(name))
        }
        Err(error) => Err(error.into()),
    }
}

fn private_directory(path: &Path) -> Result<()> {
    if path.exists() {
        return verify_private_directory(path);
    }
    let mut create = fs::DirBuilder::new();
    create.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        create.mode(0o700);
    }
    create.create(path)?;
    #[cfg(windows)]
    secure_windows_directory(path)?;
    verify_private_directory(path)
}

fn verify_private_directory(path: &Path) -> Result<()> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_dir() {
        return Err(Error::denied(
            "data or locator path must be a real directory",
        ));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if meta.permissions().mode() & 0o077 != 0 {
            return Err(Error::denied(format!(
                "directory must be accessible only to its owner: {}",
                path.display()
            )));
        }
    }
    Ok(())
}

#[cfg(windows)]
fn secure_windows_directory(path: &Path) -> Result<()> {
    use std::process::Command;
    let output = Command::new("whoami.exe")
        .args(["/user", "/fo", "csv", "/nh"])
        .output()?;
    let csv = String::from_utf8_lossy(&output.stdout);
    let sid = csv
        .split('"')
        .find(|part| part.starts_with("S-1-"))
        .ok_or_else(|| Error::unavailable("cannot determine current Windows user SID"))?;
    let grant = format!("*{sid}:(OI)(CI)F");
    let result = Command::new("icacls.exe")
        .arg(path)
        .args(["/inheritance:r", "/grant:r", &grant])
        .output()?;
    if !result.status.success() {
        return Err(Error::denied(
            "cannot apply user-only permissions to the instance directory",
        ));
    }
    Ok(())
}

fn open_lock(path: &Path) -> Result<File> {
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true).truncate(false);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    Ok(options.open(path)?)
}

fn read<T: DeserializeOwned>(path: &Path) -> Result<Option<T>> {
    let bytes = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    serde_json::from_slice(&bytes).map(Some).map_err(|_| {
        Error::conflict(format!(
            "cannot read instance metadata at {}",
            path.display()
        ))
    })
}

fn save<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    let dir = path
        .parent()
        .ok_or_else(|| Error::internal("metadata has no parent directory"))?;
    let mut temporary = tempfile::NamedTempFile::new_in(dir)?;
    serde_json::to_writer(&mut temporary, value)?;
    temporary.write_all(b"\n")?;
    temporary.as_file().sync_all()?;
    temporary.persist(path).map_err(|error| error.error)?;
    #[cfg(unix)]
    File::open(dir)?.sync_all()?;
    Ok(())
}

fn root_binding(root: &Path) -> Result<Option<Binding>> {
    read(&root.join("instance.json"))
}

fn match_path(explicit: Option<&Path>, binding: &Binding) -> Result<()> {
    if let Some(explicit) = explicit {
        let candidate = canonical_candidate(explicit)?;
        if candidate != binding.root {
            return Err(Error::conflict(format!(
                "requested path {} differs from registered root {}",
                candidate.display(),
                binding.root.display()
            )));
        }
    }
    Ok(())
}

fn load_instance(binding: Binding) -> Result<Instance> {
    verify_private_directory(&binding.root)?;
    if binding.root.canonicalize()? != binding.root
        || root_binding(&binding.root)?.as_ref() != Some(&binding)
    {
        return Err(Error::conflict(
            "registered path and instance marker do not match",
        ));
    }
    let owner: Owner = read(&binding.root.join("owner.json"))?
        .ok_or_else(|| Error::conflict("instance owner credential is unavailable"))?;
    if owner.instance_id != binding.instance_id || owner.owner_token.len() != 64 {
        return Err(Error::conflict(
            "owner credential belongs to a different instance",
        ));
    }
    if !binding.root.join("data/bmem.sqlite3").is_file() {
        return Err(Error::conflict(
            "registered authority database is unavailable",
        ));
    }
    Ok(Instance {
        root: binding.root,
        id: binding.instance_id,
        owner_token: owner.owner_token,
    })
}

pub fn resolve(explicit: Option<&Path>) -> Result<Instance> {
    let dir = locator_dir()?;
    let locator: Locator = read(&dir.join("active.json"))?.ok_or_else(|| {
        Error::missing("active bmem instance not found; run bmem init or recover its locator")
    })?;
    verify_private_directory(&dir)?;
    if locator.version != 1 || locator.state != "active" {
        return Err(Error::conflict(
            "instance initialization is incomplete; rerun bmem init",
        ));
    }
    match_path(explicit, &locator.binding)?;
    let registration = read::<Registration>(&dir.join("registration.json"))?;
    if !registration.is_some_and(|record| record.active && record.binding == locator.binding) {
        return Err(Error::conflict(
            "locator and installation registration do not match",
        ));
    }
    load_instance(locator.binding)
}

fn known_remote_volume(path: &Path) -> bool {
    #[cfg(windows)]
    {
        return path.to_string_lossy().starts_with("\\\\?\\UNC\\")
            || (path.to_string_lossy().starts_with("\\\\")
                && !path.to_string_lossy().starts_with("\\\\?\\"));
    }
    #[cfg(unix)]
    {
        std::process::Command::new("df")
            .arg("-P")
            .arg(path)
            .output()
            .ok()
            .and_then(|out| String::from_utf8(out.stdout).ok())
            .and_then(|text| {
                text.lines()
                    .nth(1)?
                    .split_whitespace()
                    .next()
                    .map(str::to_owned)
            })
            .is_some_and(|device| device.starts_with("//") || device.contains(":/"))
    }
    #[cfg(not(any(unix, windows)))]
    false
}

fn new_root(binding: &Binding) -> Result<()> {
    if binding.root.exists()
        && root_binding(&binding.root)?.is_none()
        && fs::read_dir(&binding.root)?.next().is_some()
    {
        return Err(Error::conflict(
            "data root must be a new or empty directory",
        ));
    }
    private_directory(&binding.root)?;
    #[cfg(windows)]
    secure_windows_directory(&binding.root)?;
    if known_remote_volume(&binding.root) {
        return Err(Error::denied(
            "selected data directory is on a network volume; choose a local volume",
        ));
    }
    let marker = binding.root.join("instance.json");
    match root_binding(&binding.root)? {
        Some(existing) if &existing != binding => {
            return Err(Error::conflict("data root belongs to another instance"));
        }
        None => save(&marker, binding)?,
        _ => {}
    }
    let owner_path = binding.root.join("owner.json");
    if let Some(owner) = read::<Owner>(&owner_path)? {
        if owner.instance_id != binding.instance_id {
            return Err(Error::conflict(
                "data root has an unrelated owner credential",
            ));
        }
    } else {
        save(
            &owner_path,
            &Owner {
                instance_id: binding.instance_id.clone(),
                owner_token: format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple()),
            },
        )?;
    }
    for directory in [
        "data", "objects", "indexes", "run", "logs", "models", "backups", "tmp",
    ] {
        private_directory(&binding.root.join(directory))?;
    }
    let db = Connection::open(binding.root.join("data/bmem.sqlite3"))?;
    let journal: String = db.query_row("PRAGMA journal_mode=WAL", [], |row| row.get(0))?;
    if !journal.eq_ignore_ascii_case("wal") {
        return Err(Error::conflict(
            "selected data filesystem does not support SQLite WAL",
        ));
    }
    Ok(())
}

fn initialize_volume(path: &Path) -> Result<()> {
    #[cfg(unix)]
    if path.is_dir() {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    private_directory(path)?;
    let probe = tempfile::NamedTempFile::new_in(path)?;
    let db = Connection::open(probe.path())?;
    let journal: String = db.query_row("PRAGMA journal_mode=WAL", [], |row| row.get(0))?;
    if !journal.eq_ignore_ascii_case("wal") {
        return Err(Error::conflict(
            "selected data filesystem does not support SQLite WAL",
        ));
    }
    Ok(())
}

pub fn init(explicit: Option<&Path>) -> Result<Instance> {
    let dir = locator_dir()?;
    private_directory(&dir)?;
    let _lock = open_lock(&dir.join("init.lock"))?;
    fs2::FileExt::lock_exclusive(&_lock)?;
    let locator_path = dir.join("active.json");
    let registration_path = dir.join("registration.json");
    let locator = read::<Locator>(&locator_path)?;
    let registration = read::<Registration>(&registration_path)?;
    if let Some(locator) = &locator {
        if locator.version != 1
            || registration.as_ref().map(|record| &record.binding) != Some(&locator.binding)
        {
            return Err(Error::conflict(
                "locator and installation registration do not match",
            ));
        }
        match_path(explicit, &locator.binding)?;
        if locator.state == "active" {
            if !registration.as_ref().is_some_and(|record| record.active) {
                return Err(Error::conflict("active locator has no active registration"));
            }
            return load_instance(locator.binding.clone());
        }
        if locator.state != "initializing" {
            return Err(Error::conflict("unknown locator state"));
        }
    }
    let binding = if let Some(record) = registration {
        let binding = record.binding;
        match_path(explicit, &binding)?;
        if locator.is_none() && explicit.is_none() {
            return Err(Error::conflict(
                "missing locator; pass --home with the known registered data root",
            ));
        }
        if record.active {
            let instance = load_instance(binding.clone())?;
            save(
                &locator_path,
                &Locator {
                    version: 1,
                    state: "active".into(),
                    binding,
                },
            )?;
            return Ok(instance);
        }
        binding
    } else {
        let default = default_root()?;
        let selected = canonical_candidate(explicit.unwrap_or(&default))?;
        if selected.starts_with(&dir) || dir.starts_with(&selected) {
            return Err(Error::conflict(
                "the instance must use a directory separate from its locator",
            ));
        }
        if root_binding(&default)?.is_some()
            || (selected.exists() && fs::read_dir(&selected)?.next().is_some())
        {
            return Err(Error::conflict(
                "cannot claim an existing data root without its original registration",
            ));
        }
        if known_remote_volume(
            selected
                .ancestors()
                .find(|part| part.exists())
                .unwrap_or(&selected),
        ) {
            return Err(Error::denied(
                "selected data directory is on a network volume; choose a local volume",
            ));
        }
        initialize_volume(&selected)?;
        let binding = Binding {
            root: selected,
            instance_id: Uuid::new_v4().to_string(),
        };
        save(
            &registration_path,
            &Registration {
                active: false,
                binding: binding.clone(),
            },
        )?;
        binding
    };
    save(
        &locator_path,
        &Locator {
            version: 1,
            state: "initializing".into(),
            binding: binding.clone(),
        },
    )?;
    new_root(&binding)?;
    let instance = load_instance(binding.clone())?;
    save(
        &registration_path,
        &Registration {
            active: true,
            binding: binding.clone(),
        },
    )?;
    save(
        &locator_path,
        &Locator {
            version: 1,
            state: "active".into(),
            binding,
        },
    )?;
    Ok(instance)
}

pub fn lock_service(instance: &Instance) -> Result<File> {
    let file = open_lock(&instance.root.join("run/service.lock"))?;
    match fs2::FileExt::try_lock_exclusive(&file) {
        Ok(()) => Ok(file),
        Err(error) if error.raw_os_error() == fs2::lock_contended_error().raw_os_error() => {
            Err(Error::conflict("a service already owns this data root"))
        }
        Err(error) => Err(error.into()),
    }
}

pub fn publish(instance: &Instance, address: SocketAddr) -> Result<()> {
    if !address.ip().is_loopback() || address.port() == 0 {
        return Err(Error::denied(
            "service address must be a bound loopback port",
        ));
    }
    save(
        &instance.root.join("run/service.json"),
        &Endpoint {
            instance_id: instance.id.clone(),
            address,
        },
    )
}

pub fn discover(instance: &Instance) -> Result<SocketAddr> {
    match lock_service(instance) {
        Ok(_) => return Err(Error::missing("service is stopped")),
        Err(error) if error.status == 409 => {}
        Err(error) => return Err(error),
    }
    registered_endpoint(instance)
}

pub fn registered_endpoint(instance: &Instance) -> Result<SocketAddr> {
    let discovery: Endpoint = read(&instance.root.join("run/service.json"))?
        .ok_or_else(|| Error::missing("local service endpoint is unavailable"))?;
    if discovery.instance_id != instance.id
        || !discovery.address.ip().is_loopback()
        || discovery.address.port() == 0
    {
        return Err(Error::conflict(
            "local service endpoint does not belong to this instance",
        ));
    }
    Ok(discovery.address)
}

pub fn diagnostics(explicit: Option<&Path>) -> Value {
    let locator = locator_dir();
    let resolved = resolve(explicit);
    let locator_info = match locator {
        Ok(path) => match test_atomic_replace(&path) {
            Ok(_) => json!({"path":path,"atomic_replace":true}),
            Err(error) => json!({"path":path,"atomic_replace":false,"reason":error.to_string()}),
        },
        Err(error) => json!({"status":"unavailable","reason":error.message}),
    };
    let data_info = match resolved {
        Ok(instance) => {
            let network = known_remote_volume(&instance.root);
            let journal = Connection::open_with_flags(
                instance.root.join("data/bmem.sqlite3"),
                rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
            )
            .and_then(|db| db.query_row("PRAGMA journal_mode", [], |row| row.get::<_, String>(0)));
            json!({
                "path": instance.root,
                "network_volume_detected": network,
                "journal_mode":journal.as_ref().ok(),
                "wal_ready":!network && journal.as_ref().is_ok_and(|mode| mode == "wal"),
                "reason":journal.as_ref().err().map(ToString::to_string),
                "status": "registered",
                "instance_id": instance.id
            })
        }
        Err(error) => json!({"status":"unavailable","reason":error.message}),
    };
    json!({"locator_volume": locator_info, "data_volume":data_info})
}

fn test_atomic_replace(path: &Path) -> io::Result<()> {
    let previous = tempfile::NamedTempFile::new_in(path)?.into_temp_path();
    let replacement = tempfile::NamedTempFile::new_in(path)?;
    replacement
        .persist(&previous)
        .map_err(|error| error.error)?;
    Ok(())
}
