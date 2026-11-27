use crate::error::{Error, Result};
use serde::{Serialize, de::DeserializeOwned};
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::Path;

pub fn directory(path: &Path) -> Result<()> {
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(path)?;
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.is_dir() {
        return Err(Error::denied("update state must be a real directory"));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o077 != 0 {
            return Err(Error::denied("update state must be private"));
        }
    }
    Ok(())
}

pub fn read<T: DeserializeOwned>(path: &Path) -> Result<Option<T>> {
    match fs::read(path) {
        Ok(bytes) => Ok(Some(serde_json::from_slice(&bytes)?)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

pub fn save<T: Serialize>(path: &Path, data: &T) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| Error::invalid("invalid update path"))?;
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    serde_json::to_writer(&mut file, data)?;
    file.write_all(b"\n")?;
    file.as_file().sync_all()?;
    file.persist(path).map_err(|failure| failure.error)?;
    sync_directory(parent)
}

pub fn digest(path: &Path) -> Result<String> {
    if !fs::symlink_metadata(path)?.is_file() {
        return Err(Error::denied("update executable must be a regular file"));
    }
    let mut data = File::open(path)?;
    let mut hash = Sha256::new();
    io::copy(&mut data, &mut hash)?;
    Ok(format!("{:x}", hash.finalize()))
}

pub fn executable(path: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)?
        .sync_all()?;
    sync_directory(
        path.parent()
            .ok_or_else(|| Error::invalid("invalid executable path"))?,
    )
}

pub fn sync_directory(path: &Path) -> Result<()> {
    #[cfg(unix)]
    File::open(path)?.sync_all()?;
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}

pub fn lock(path: &Path) -> Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path)?;
    fs2::FileExt::try_lock_exclusive(&file).map_err(|error| {
        if error.raw_os_error() == fs2::lock_contended_error().raw_os_error() {
            Error::new(
                409,
                "update_in_progress",
                "an updater already owns this installation",
            )
        } else {
            error.into()
        }
    })?;
    Ok(file)
}

pub fn replace(source: &Path, destination: &Path) -> Result<()> {
    let parent = destination
        .parent()
        .ok_or_else(|| Error::invalid("invalid executable path"))?;
    let mut staged = tempfile::NamedTempFile::new_in(parent)?;
    io::copy(&mut File::open(source)?, &mut staged)?;
    executable(staged.path())?;
    #[cfg(not(windows))]
    staged
        .persist(destination)
        .map_err(|failure| failure.error)?;
    #[cfg(windows)]
    if destination.is_file() {
        let path = staged.into_temp_path();
        replace_windows(&path, destination)?;
    } else {
        staged
            .persist_noclobber(destination)
            .map_err(|failure| failure.error)?;
    }
    sync_directory(parent)
}

#[cfg(windows)]
pub fn wait_for_legacy_processes(path: &Path) -> Result<()> {
    use std::path::PathBuf;
    use std::process::{Command, Stdio};

    let system = std::env::var_os("SystemRoot")
        .ok_or_else(|| Error::conflict("Windows system root is unavailable"))?;
    let shell = PathBuf::from(system).join("System32/WindowsPowerShell/v1.0/powershell.exe");
    let command = r#"
$ErrorActionPreference = 'Stop'
$binary = $env:BMEM_UPDATER_LEGACY_EXE
if ($binary.StartsWith('\\?\')) { $binary = $binary.Substring(4) }
$name = [Management.Automation.WildcardPattern]::Escape([IO.Path]::GetFileNameWithoutExtension($binary))
$timer = [Diagnostics.Stopwatch]::StartNew()
do {
    $holders = @(Get-Process -Name $name -ErrorAction SilentlyContinue | Where-Object { $_.Path -ieq $binary })
    if ($holders.Count -eq 0) { exit 0 }
    Start-Sleep -Milliseconds 125
} while ($timer.Elapsed.TotalSeconds -lt 12)
exit 10
"#;
    let status = Command::new(shell)
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ])
        .env("BMEM_UPDATER_LEGACY_EXE", path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()?;
    match status.code() {
        Some(0) => Ok(()),
        Some(10) => Err(executable_in_use()),
        _ => Err(Error::unavailable(
            "could not check for Windows processes using the old bmem program",
        )),
    }
}

#[cfg(windows)]
fn executable_in_use() -> Error {
    Error::new(
        503,
        "executable_in_use",
        "close the bmem CLI and bmem stdio sessions that still hold this installation, then retry",
    )
}

#[cfg(windows)]
fn replace_windows(source: &Path, destination: &Path) -> Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use std::ptr::null_mut;
    use std::time::{Duration, Instant};

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn SetFileAttributesW(path: *const u16, attributes: u32) -> i32;
        fn ReplaceFileW(
            old: *const u16,
            new: *const u16,
            backup: *const u16,
            flags: u32,
            exclude: *mut core::ffi::c_void,
            reserved: *mut core::ffi::c_void,
        ) -> i32;
    }
    let from: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
    let to: Vec<u16> = destination
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect();
    // This direct replacement bypasses NamedTempFile::persist, which clears
    // FILE_ATTRIBUTE_TEMPORARY before promoting the staged file.
    const FILE_ATTRIBUTE_NORMAL: u32 = 0x80;
    if unsafe { SetFileAttributesW(from.as_ptr(), FILE_ATTRIBUTE_NORMAL) } == 0 {
        return Err(io::Error::last_os_error().into());
    }
    let end = Instant::now() + Duration::from_secs(12);
    loop {
        let result = unsafe {
            ReplaceFileW(
                to.as_ptr(),
                from.as_ptr(),
                std::ptr::null(),
                0,
                null_mut(),
                null_mut(),
            )
        };
        if result != 0 {
            return Ok(());
        }
        let error = io::Error::last_os_error();
        match error.raw_os_error() {
            // 1175 leaves both paths intact. 1176 and 1177 can move the old
            // path, so propagate either for the journal's verified restore.
            Some(5 | 32 | 33 | 1175) if Instant::now() < end => {
                std::thread::sleep(Duration::from_millis(125))
            }
            Some(5 | 32 | 33 | 1175) => return Err(executable_in_use()),
            _ => return Err(error.into()),
        }
    }
}
