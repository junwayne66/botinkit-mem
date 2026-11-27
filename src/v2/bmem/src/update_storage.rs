use crate::error::{Error, Result};
use crate::{platform, settings::Settings, storage_epoch};
use rusqlite::{Connection, OpenFlags, OptionalExtension, backup::Backup};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io;
use std::path::Path;
use std::time::Duration;

const AUTHORITY: &str = "bmem.sqlite3";
const SETTINGS: &str = "settings.sqlite3";
const MANIFEST: &str = "snapshot.json";

#[derive(Serialize, Deserialize)]
struct Snapshot {
    instance_id: String,
    authority: Option<String>,
    settings: Option<String>,
}

fn require_barrier(instance: &platform::Instance) -> Result<()> {
    if platform::update_barrier(instance)?.is_none() {
        return Err(Error::conflict("no update has reserved this instance"));
    }
    Ok(())
}

fn readonly(path: &Path) -> Result<Connection> {
    Ok(Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NOFOLLOW,
    )?)
}

fn integrity(db: &Connection) -> Result<()> {
    let status: String = db.query_row("PRAGMA quick_check(1)", [], |row| row.get(0))?;
    if status != "ok" {
        return Err(Error::conflict("update SQLite integrity check failed"));
    }
    Ok(())
}

fn checksum(path: &Path) -> Result<String> {
    let mut hash = Sha256::new();
    io::copy(&mut File::open(path)?, &mut hash)?;
    Ok(format!("{:x}", hash.finalize()))
}

fn transfer(source: &Connection, destination: &Path) -> Result<()> {
    let mut copy = Connection::open_with_flags(
        destination,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NOFOLLOW,
    )?;
    copy.execute_batch("PRAGMA synchronous=FULL")?;
    {
        let backup = Backup::new(source, &mut copy)?;
        backup.run_to_completion(128, Duration::from_millis(5), None)?;
    }
    let journal: String = copy.query_row("PRAGMA journal_mode=DELETE", [], |row| row.get(0))?;
    if journal != "delete" {
        return Err(Error::internal(
            "could not finish a self-contained update database",
        ));
    }
    integrity(&copy)?;
    drop(copy);
    // FlushFileBuffers on Windows needs a handle opened for writing.
    OpenOptions::new()
        .write(true)
        .open(destination)?
        .sync_all()?;
    Ok(())
}

fn copy_to_snapshot(source: &Path, destination: &Path) -> Result<String> {
    let directory = destination
        .parent()
        .ok_or_else(|| Error::invalid("snapshot has no parent"))?;
    let staged = tempfile::NamedTempFile::new_in(directory)?;
    transfer(&readonly(source)?, staged.path())?;
    let digest = checksum(staged.path())?;
    staged.persist(destination).map_err(|error| error.error)?;
    Ok(digest)
}

fn snapshot_member(data: &Path, directory: &Path, name: &str) -> Result<Option<String>> {
    let original = data.join(name);
    if original.try_exists()? {
        Ok(Some(copy_to_snapshot(&original, &directory.join(name))?))
    } else {
        Ok(None)
    }
}

/// The coordinator must own the instance's service lock while creating or restoring this local snapshot.
pub fn snapshot_update(instance: &platform::Instance, directory: &Path) -> Result<()> {
    require_barrier(instance)?;
    platform::private_directory(directory)?;
    let manifest_path = directory.join(MANIFEST);
    if manifest_path.try_exists()? {
        checked_snapshot(instance, directory)?;
        return Ok(());
    }

    let data = instance.root.join("data");
    let authority = snapshot_member(&data, directory, AUTHORITY)?;
    let settings = snapshot_member(&data, directory, SETTINGS)?;
    // The only persistent paths changed by the current database migrations are these two SQLite files.
    platform::save(
        &manifest_path,
        &Snapshot {
            instance_id: instance.id.clone(),
            authority,
            settings,
        },
    )
}

fn checked_snapshot(instance: &platform::Instance, directory: &Path) -> Result<Snapshot> {
    let snapshot: Snapshot = platform::read(&directory.join(MANIFEST))?
        .ok_or_else(|| Error::missing("the update database snapshot is incomplete"))?;
    if snapshot.instance_id != instance.id {
        return Err(Error::conflict(
            "the update snapshot belongs to another instance",
        ));
    }
    let files = [
        (AUTHORITY, snapshot.authority.as_ref()),
        (SETTINGS, snapshot.settings.as_ref()),
    ];
    for (name, digest) in files {
        if let Some(digest) = digest {
            let path = directory.join(name);
            if !fs::symlink_metadata(&path)?.file_type().is_file() || checksum(&path)? != *digest {
                return Err(Error::conflict(
                    "the update snapshot does not match its recorded checksum",
                ));
            }
            integrity(&readonly(&path)?)?;
        }
    }
    Ok(snapshot)
}

fn restore_db(source: &Path, destination: &Path) -> Result<()> {
    if !destination.try_exists()? {
        tempfile::NamedTempFile::new_in(
            destination
                .parent()
                .ok_or_else(|| Error::invalid("database has no parent"))?,
        )?
        .persist_noclobber(destination)
        .map_err(|error| error.error)?;
    }
    transfer(&readonly(source)?, destination)
}

fn restore_member(data: &Path, directory: &Path, name: &str, existed: bool) -> Result<()> {
    if existed {
        return restore_db(&directory.join(name), &data.join(name));
    }
    for suffix in ["", "-journal", "-wal", "-shm"] {
        match fs::remove_file(data.join(format!("{name}{suffix}"))) {
            Ok(()) => {}
            Err(err) if err.kind() == io::ErrorKind::NotFound => {}
            Err(err) => return Err(err.into()),
        }
    }
    Ok(())
}

pub fn restore_update(instance: &platform::Instance, directory: &Path) -> Result<()> {
    require_barrier(instance)?;
    let snapshot = checked_snapshot(instance, directory)?;
    let data = instance.root.join("data");
    restore_member(&data, directory, AUTHORITY, snapshot.authority.is_some())?;
    restore_member(&data, directory, SETTINGS, snapshot.settings.is_some())?;
    #[cfg(unix)]
    File::open(data)?.sync_all()?;
    Ok(())
}

/// Called by the staged build before the update coordinator hands off the service lock.
pub fn migrate_update(instance: &platform::Instance) -> Result<Value> {
    require_barrier(instance)?;
    drop(Settings::open(&instance.root)?);
    drop(super::initialize_database(instance)?);
    validate_update(instance)
}

pub(super) fn validate_update(instance: &platform::Instance) -> Result<Value> {
    let data = instance.root.join("data");
    let authority = readonly(&data.join(AUTHORITY))?;
    let settings = readonly(&data.join(SETTINGS))?;
    integrity(&authority)?;
    integrity(&settings)?;
    let authority_epoch = storage_epoch::read(&authority)?;
    if authority_epoch != storage_epoch::WRITE_EPOCH {
        return Err(Error::conflict(
            "the update database has not reached the target writer epoch",
        ));
    }
    let sequence: i64 =
        authority.query_row("SELECT seq FROM authority_changes WHERE id=1", [], |row| {
            row.get(0)
        })?;
    let tombstones: i64 =
        authority.query_row("SELECT count(*) FROM ing_root_revocations", [], |row| {
            row.get(0)
        })?;
    let withdrawn_event = authority
        .query_row(
            "SELECT e.event_id FROM ing_root_revocations w
             JOIN ing_event_versions e ON e.root_id=w.root_id LIMIT 1",
            [],
            |row| row.get::<_, String>(0),
        )
        .optional()?;
    if let Some(event) = withdrawn_event.as_ref() {
        match crate::retrieval::expand_for_owner(&authority, &format!("event_{event}")) {
            Err(error) if error.status == 404 => {}
            Err(error) => return Err(error),
            Ok(_) => {
                return Err(Error::conflict(
                    "withdrawn evidence was accessible after migration",
                ));
            }
        }
    }
    let settings_rows: i64 =
        settings.query_row("SELECT count(*) FROM user_settings WHERE id=1", [], |row| {
            row.get(0)
        })?;
    if settings_rows != 1 {
        return Err(Error::conflict("the update settings snapshot is missing"));
    }
    let terms = authority
        .query_row(
            "SELECT terms FROM recall_fts WHERE length(terms)>0 LIMIT 1",
            [],
            |row| row.get::<_, String>(0),
        )
        .optional()?;
    let term = terms.as_deref().and_then(|text| {
        text.split_whitespace()
            .find(|part| part.chars().any(char::is_alphanumeric))
    });
    let query = term.unwrap_or("bmemupdatehealth").replace('"', "\"\"");
    let matches: i64 = authority.query_row(
        "SELECT count(*) FROM recall_fts WHERE recall_fts MATCH ?1",
        [format!("\"{query}\"")],
        |row| row.get(0),
    )?;
    if term.is_some() && matches == 0 {
        return Err(Error::conflict(
            "the update lexical probe could not find an indexed term",
        ));
    }
    Ok(json!({
        "instance_id": instance.id,
        "version": crate::build_info::VERSION,
        "build_id": crate::build_info::ID,
        "build_target": crate::build_info::TARGET,
        "web_build": crate::build_info::WEB_ID,
        "web_build_id": crate::build_info::WEB_ID,
        "web_assets_sha256": super::web_build(),
        "authority_epoch": authority_epoch,
        "settings_epoch": storage_epoch::read(&settings)?,
        "authority_sequence": sequence,
        "withdrawal_count": tombstones,
        "withdrawal_probe": withdrawn_event.is_some(),
        "database": "ready", "lexical": "ready",
        "fts_query_matched": matches > 0,
    }))
}
