use super::files;
use crate::error::{Error, Result};
use serde::Deserialize;
use serde_json::Value;
use std::fs::{self, Metadata};
use std::io;
use std::path::Path;
use uuid::Uuid;

#[derive(Deserialize)]
struct Manifest {
    instance_id: String,
    authority: Value,
    settings: Value,
}

fn real_entry(metadata: &Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        // Windows junctions are also reparse points.
        if metadata.file_attributes() & 0x400 != 0 {
            return false;
        }
    }
    !metadata.file_type().is_symlink()
}

fn verify_parent(path: &Path) -> Result<()> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.is_dir() && real_entry(&metadata) {
        return Ok(());
    }
    Err(Error::denied(
        "update recovery parent must be a real directory",
    ))
}

fn owned_snapshot(directory: &Path, instance_id: &str) -> bool {
    let path = directory.join("snapshot.json");
    if !fs::symlink_metadata(&path).is_ok_and(|meta| meta.is_file() && real_entry(&meta)) {
        return false;
    }
    files::read::<Manifest>(&path).is_ok_and(|record| {
        record.is_some_and(|record| {
            record.instance_id == instance_id
                && [record.authority, record.settings]
                    .into_iter()
                    .all(|digest| {
                        digest.is_null()
                            || digest.as_str().is_some_and(|hash| {
                                hash.len() == 64
                                    && hash.bytes().all(|byte| byte.is_ascii_hexdigit())
                            })
                    })
        })
    })
}

pub(super) fn completed_snapshot(
    recovery: &Path,
    instance_id: &str,
    operation: Uuid,
) -> Result<()> {
    let run = recovery
        .parent()
        .ok_or_else(|| Error::invalid("invalid recovery directory"))?;
    verify_parent(run)?;
    verify_parent(recovery)?;

    let current = recovery.join(operation.to_string());
    match fs::symlink_metadata(&current) {
        Ok(meta) if meta.is_dir() && real_entry(&meta) => {
            if current.join("snapshot.json").symlink_metadata().is_ok()
                && !owned_snapshot(&current, instance_id)
            {
                return Err(Error::denied(
                    "completed recovery point ownership could not be confirmed",
                ));
            }
        }
        Ok(_) => {
            return Err(Error::denied(
                "completed recovery point is not a real directory",
            ));
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return files::sync_directory(recovery);
        }
        Err(error) => return Err(error.into()),
    }
    fs::remove_dir_all(&current)?;
    files::sync_directory(recovery)
}
