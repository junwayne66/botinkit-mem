use crate::error::{Error, Result};
use rusqlite::backup::Backup;
use rusqlite::types::ValueRef;
use rusqlite::{Connection, OpenFlags};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::fs::{self, File};
use std::io::{BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;
use uuid::Uuid;

const FORMAT: u32 = 1;
const DATABASE: &str = "data.sqlite3";
const MANIFEST: &str = "manifest.json";

#[derive(Deserialize, Serialize)]
struct Manifest {
    format: u32,
    backup_id: String,
    instance_id: String,
    created_at: String,
    db_sha256: String,
    db_size: u64,
    revocation_cut: String,
    rebind_clients: Vec<String>,
    change_seq: i64,
}

pub fn backup(db: &Connection, root: &Path, instance_id: &str) -> Result<Value> {
    let folder = backups(root, true)?;
    check_objects(root)?;
    let staging = tempfile::Builder::new()
        .prefix(".staging-")
        .tempdir_in(&folder)?;
    let data = staging.path().join(DATABASE);
    create_private_file(&data)?;
    let mut copy = Connection::open_with_flags(
        &data,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NOFOLLOW,
    )?;
    {
        let transfer = Backup::new(db, &mut copy)?;
        transfer.run_to_completion(128, Duration::from_millis(5), None)?;
    }
    let journal: String = copy.query_row("PRAGMA journal_mode=DELETE", [], |row| row.get(0))?;
    if !journal.eq_ignore_ascii_case("delete") {
        return Err(Error::internal("backup could not be made self-contained"));
    }
    let rebind_clients = unbind(&copy)?;
    verify_db(&copy)?;
    let revocation_cut = cut(&copy)?;
    let change_seq = change_seq(&copy)?;
    drop(copy);
    File::open(&data)?.sync_all()?;
    let (db_sha256, db_size) = checksum(&data)?;
    let backup_id = format!("backup_{}", Uuid::new_v4().simple());
    let manifest = Manifest {
        format: FORMAT,
        backup_id: backup_id.clone(),
        instance_id: instance_id.to_owned(),
        created_at: chrono::Utc::now().to_rfc3339(),
        db_sha256,
        db_size,
        revocation_cut,
        rebind_clients,
        change_seq,
    };
    let mut metadata = create_private_file(&staging.path().join(MANIFEST))?;
    metadata.write_all(&serde_json::to_vec(&manifest)?)?;
    metadata.sync_all()?;
    drop(metadata);
    sync_directory(staging.path())?;
    fs::rename(staging.path(), folder.join(&backup_id))?;
    sync_directory(&folder)?;
    Ok(json!({
        "backup_id": manifest.backup_id, "state": "complete", "complete": true,
        "sha256": manifest.db_sha256, "database_bytes": manifest.db_size, "created_at": manifest.created_at,
        "revocation_cut": manifest.revocation_cut, "revocation_cut_kind": "logical_state_fingerprint",
        "rebind_clients": manifest.rebind_clients, "objects": 0, "change_seq": manifest.change_seq,
    }))
}

pub fn preview(db: &Connection, root: &Path, instance_id: &str, request: Value) -> Result<Value> {
    let fields = request
        .as_object()
        .ok_or_else(|| Error::invalid("archive preview requires an object"))?;
    if fields
        .keys()
        .any(|key| !matches!(key.as_str(), "kind" | "backup_id"))
    {
        return Err(Error::invalid(
            "archive preview accepts a managed backup_id only",
        ));
    }
    let id = request
        .get("backup_id")
        .and_then(Value::as_str)
        .ok_or_else(|| Error::invalid("backup_id is required"))?;
    if !valid_id(id) {
        return Err(Error::invalid("invalid backup_id"));
    }
    let folder = backups(root, false)?.join(id);
    ensure_dir(&folder)?;
    let metadata = folder.join(MANIFEST);
    ensure_regular_file(&metadata)?;
    let mut encoded = Vec::new();
    File::open(&metadata)?
        .take(1024 * 1024 + 1)
        .read_to_end(&mut encoded)?;
    if encoded.len() > 1024 * 1024 {
        return Err(integrity_error(
            "manifest exceeds the managed format size bound",
        ));
    }
    let manifest: Manifest =
        serde_json::from_slice(&encoded).map_err(|_| integrity_error("manifest invalid"))?;
    if manifest.format != FORMAT || manifest.backup_id != id {
        return Err(integrity_error(
            "managed archive identity or format mismatch",
        ));
    }
    let data = folder.join(DATABASE);
    ensure_regular_file(&data)?;
    for tail in ["-wal", "-journal", "-shm"] {
        if folder
            .join(format!("{DATABASE}{tail}"))
            .symlink_metadata()
            .is_ok()
        {
            return Err(integrity_error(
                "archive is not a self-contained SQLite file",
            ));
        }
    }
    let (sha256, size) = checksum(&data)?;
    if manifest.db_sha256 != sha256 || manifest.db_size != size {
        return Err(integrity_error(
            "SQLite file does not match its checksum or size",
        ));
    }
    let snapshot = Connection::open_with_flags(
        &data,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NOFOLLOW,
    )
    .map_err(|_| integrity_error("archive SQLite file could not be opened"))?;
    verify_db(&snapshot)?;
    if cut(&snapshot)? != manifest.revocation_cut || change_seq(&snapshot)? != manifest.change_seq {
        return Err(integrity_error("archive state does not match its manifest"));
    }
    verify_unbound(&snapshot, &manifest.rebind_clients)?;
    check_objects(root)?;
    // Use the same conservative comparison after any mutation affecting the authoritative state.
    let current = db.unchecked_transaction()?;
    let current_cut = cut(&current)?;
    let current_seq = change_seq(&current)?;
    current.commit()?;
    let blocked = if manifest.instance_id != instance_id {
        Some("independent_revocation_watermark_required")
    } else if current_cut != manifest.revocation_cut {
        Some("authority_state_changed_requires_revocation_replay")
    } else {
        None
    };
    Ok(json!({
        "backup_id": id, "state": if blocked.is_none() { "verified" } else { "blocked" },
        "reason": blocked, "integrity": "verified", "can_apply": false,
        "restore_mode": "preview_only", "sha256": sha256, "revocation_cut": manifest.revocation_cut,
        "active_revocation_cut": current_cut, "active_change_seq": current_seq, "provable_until": manifest.created_at,
        "rebind_clients": manifest.rebind_clients, "objects": 0, "change_seq": manifest.change_seq,
    }))
}

fn valid_id(id: &str) -> bool {
    id.strip_prefix("backup_").is_some_and(|s| {
        s.len() == 32
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}

fn integrity_error(message: &str) -> Error {
    Error::new(409, "archive_integrity_error", message)
}

fn backups(root: &Path, create: bool) -> Result<PathBuf> {
    ensure_dir(root)?;
    let folder = root.canonicalize()?.join("backups");
    if create {
        match fs::create_dir(&folder) {
            Ok(()) => restrict_directory(&folder)?,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
            Err(error) => return Err(error.into()),
        }
    }
    ensure_dir(&folder)?;
    if create {
        restrict_directory(&folder)?;
    }
    Ok(folder)
}

fn ensure_dir(path: &Path) -> Result<()> {
    match path.symlink_metadata() {
        Ok(meta) if meta.is_dir() && !meta.file_type().is_symlink() => Ok(()),
        Ok(_) => Err(integrity_error("managed archive path is not a directory")),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            Err(Error::missing("managed archive unavailable"))
        }
        Err(error) => Err(error.into()),
    }
}

fn ensure_regular_file(path: &Path) -> Result<()> {
    match path.symlink_metadata() {
        Ok(meta) if meta.is_file() && !meta.file_type().is_symlink() => Ok(()),
        Ok(_) => Err(integrity_error(
            "managed archive path is not a regular file",
        )),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            Err(Error::missing("managed archive unavailable"))
        }
        Err(error) => Err(error.into()),
    }
}

fn create_private_file(path: &Path) -> Result<File> {
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    Ok(options.open(path)?)
}

fn restrict_directory(path: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}

fn sync_directory(path: &Path) -> Result<()> {
    #[cfg(unix)]
    File::open(path)?.sync_all()?;
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}

fn check_objects(root: &Path) -> Result<()> {
    let path = root.join("objects");
    match path.symlink_metadata() {
        Ok(meta) if meta.is_dir() && !meta.file_type().is_symlink() => {
            if path.read_dir()?.next().transpose()?.is_some() {
                return Err(Error::unavailable(
                    "external objects cannot be verified by this archive format",
                ));
            }
        }
        Ok(_) => return Err(integrity_error("object directory cannot be verified")),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
        Err(error) => return Err(error.into()),
    }
    Ok(())
}

fn checksum(path: &Path) -> Result<(String, u64)> {
    let mut reader = BufReader::new(File::open(path)?);
    let mut buffer = [0_u8; 64 * 1024];
    let mut sha = Sha256::new();
    let mut length = 0;
    loop {
        let read = reader.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        sha.update(&buffer[..read]);
        length += read as u64;
    }
    Ok((format!("{:x}", sha.finalize()), length))
}

fn verify_db(db: &Connection) -> Result<()> {
    db.execute_batch("PRAGMA trusted_schema=OFF")?;
    let check = db
        .query_row("PRAGMA quick_check", [], |row| row.get::<_, String>(0))
        .map_err(|_| integrity_error("SQLite integrity could not be verified"))?;
    if check != "ok" {
        return Err(integrity_error("SQLite integrity check failed"));
    }
    if db
        .prepare("PRAGMA foreign_key_check")?
        .query([])?
        .next()?
        .is_some()
    {
        return Err(integrity_error("archive has dangling database references"));
    }
    Ok(())
}

fn change_seq(db: &Connection) -> Result<i64> {
    Ok(
        db.query_row("SELECT seq FROM authority_changes WHERE id=1", [], |row| {
            row.get(0)
        })?,
    )
}

fn client_tokens(db: &Connection) -> Result<Vec<(String, String)>> {
    let mut statement = db.prepare("SELECT id, token_hash FROM context_clients ORDER BY id")?;
    Ok(statement
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<rusqlite::Result<Vec<_>>>()?)
}

fn unbind(db: &Connection) -> Result<Vec<String>> {
    let tokens = client_tokens(db)?;
    db.execute_batch(
        "PRAGMA secure_delete=ON;
         UPDATE context_clients SET token_hash='unbound:' || id;
         VACUUM;",
    )?;
    let ids: Vec<String> = tokens.into_iter().map(|(id, _)| id).collect();
    verify_unbound(db, &ids)?;
    Ok(ids)
}

fn verify_unbound(db: &Connection, expected: &[String]) -> Result<()> {
    let tokens = client_tokens(db)?;
    if tokens.len() != expected.len()
        || tokens
            .iter()
            .zip(expected)
            .any(|((id, token), expected)| id != expected || token != &format!("unbound:{id}"))
    {
        return Err(integrity_error(
            "archive client credentials are present or mismatched",
        ));
    }
    Ok(())
}

fn part(digest: &mut Sha256, value: &[u8]) {
    digest.update((value.len() as u64).to_le_bytes());
    digest.update(value);
}

fn cut(db: &Connection) -> Result<String> {
    let mut list = db.prepare(
        "SELECT name, wr FROM pragma_table_list
         WHERE schema='main' AND type='table' AND name NOT GLOB 'sqlite_*' ORDER BY name",
    )?;
    let tables = list
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, bool>(1)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut digest = Sha256::new();
    for (name, without_rowid) in tables {
        // Administrative readbacks do not advance this cutoff. Grants and source tombstones do.
        if matches!(
            name.as_str(),
            "governance_operations"
                | "governance_previews"
                | "context_intents"
                | "context_deliveries"
                | "context_delivery_evidence"
                | "context_receipts"
                | "context_outcomes"
                | "vector_projection"
        ) {
            continue;
        }
        part(&mut digest, name.as_bytes());
        let quoted = name.replace('"', "\"\"");
        let sql = if without_rowid {
            format!("SELECT * FROM main.\"{quoted}\"")
        } else {
            format!("SELECT * FROM main.\"{quoted}\" ORDER BY rowid")
        };
        let mut table = db.prepare(&sql)?;
        let indices: Vec<usize> = table
            .column_names()
            .iter()
            .enumerate()
            .filter_map(|(index, column)| {
                if name == "context_clients" && *column == "token_hash" {
                    return None;
                }
                part(&mut digest, column.as_bytes());
                Some(index)
            })
            .collect();
        digest.update(b"columns_end");
        let mut rows = table.query([])?;
        while let Some(row) = rows.next()? {
            digest.update(b"row");
            for &index in &indices {
                match row.get_ref(index)? {
                    ValueRef::Null => digest.update(b"n"),
                    ValueRef::Integer(value) => {
                        digest.update(b"i");
                        digest.update(value.to_le_bytes());
                    }
                    ValueRef::Real(value) => {
                        digest.update(b"r");
                        digest.update(value.to_le_bytes());
                    }
                    ValueRef::Text(value) => {
                        digest.update(b"t");
                        part(&mut digest, value);
                    }
                    ValueRef::Blob(value) => {
                        digest.update(b"b");
                        part(&mut digest, value);
                    }
                }
            }
        }
        digest.update(b"end");
    }
    Ok(format!("{:x}", digest.finalize()))
}
