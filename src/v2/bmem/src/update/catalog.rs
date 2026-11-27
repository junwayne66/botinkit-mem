use crate::build_info;
use crate::error::{Error, Result};
use semver::Version;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::Path;
use std::time::Duration;
use tough::{IntoVec, RepositoryLoader, TargetName, TransportErrorKind};
use url::Url;

#[cfg(not(feature = "internal-release"))]
const CHANNEL_BASE: &str = "https://junwayne66.github.io/botinkit-mem/update/";
const CHANNEL_HOST: &str = "junwayne66.github.io";
const CHANNEL_PATH: &str = "/botinkit-mem/update/";
#[cfg(not(feature = "internal-release"))]
const RELEASE_PREFIX: &str = "/junwayne66/botinkit-mem/releases/download/";
#[cfg(not(feature = "internal-release"))]
const OFFICIAL_ROOT: Option<&str> = option_env!("BMEM_OFFICIAL_TUF_ROOT");
#[cfg(feature = "internal-release")]
const INTERNAL_ROOT: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/internal-tuf-root.json"));
const MAX_METADATA: u64 = 1024 * 1024;
const MAX_BINARY: u64 = 256 * 1024 * 1024;
const UPDATER_PROTOCOL: u32 = 1;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Release {
    pub version: String,
    pub build_id: String,
    pub commit: String,
    pub sha256: String,
    pub len: u64,
    pub url: String,
    pub target: String,
    pub min_os: String,
    pub authority_epoch: i64,
    pub settings_epoch: i64,
    pub from_versions: Vec<String>,
    pub from_authority_epochs: Vec<i64>,
    pub min_updater: u32,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Catalog {
    schema_version: u32,
    channel: String,
    releases: Vec<Release>,
}

pub fn choose(
    cache: &Path,
    current_version: &str,
    current_build: &str,
    epoch: i64,
    highest: Option<&str>,
) -> Result<Option<Release>> {
    let (root, metadata, targets) = repository_location()?;
    let cache = if cfg!(feature = "internal-release") {
        cache.join(format!("internal-{:x}", Sha256::digest(&root)))
    } else if metadata.scheme() == "file" {
        cache.join(format!("fixture-{:x}", Sha256::digest(&root)))
    } else {
        cache.join("official")
    };
    fs::create_dir_all(&cache)?;
    let retained_root = cache.join("trusted-root.json");
    let trusted = match bounded_file(&retained_root) {
        Ok(saved) => saved,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => root,
        Err(_) => {
            return Err(invalid(
                "update_trust_store_invalid",
                "trusted root cache cannot be read",
            ));
        }
    };
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let catalog: Catalog = serde_json::from_slice(
        &runtime.block_on(load_catalog(&trusted, metadata, targets, &cache))?,
    )
    .map_err(|_| {
        invalid(
            "update_catalog_invalid",
            "signed release catalogue has an invalid structure",
        )
    })?;
    choose_release(catalog, current_version, current_build, epoch, highest)
}

#[cfg(feature = "internal-release")]
fn internal_repository() -> Result<Url> {
    let repository = std::env::var_os("BMEM_INTERNAL_TUF_REPOSITORY")
        .map(std::path::PathBuf::from)
        .filter(|path| path.is_absolute())
        .ok_or_else(|| {
            invalid(
                "update_internal_channel_missing",
                "set BMEM_INTERNAL_TUF_REPOSITORY to the absolute local signed channel directory",
            )
        })?;
    let canonical = fs::canonicalize(repository)
        .map_err(|_| network("cannot open the internal signed channel directory"))?;
    Url::from_directory_path(canonical)
        .ok()
        .filter(|url| !url.has_host())
        .ok_or_else(|| {
            invalid(
                "update_catalog_invalid",
                "use a local internal channel directory",
            )
        })
}

#[cfg(feature = "internal-release")]
fn repository_location() -> Result<(Vec<u8>, Url, Url)> {
    let base = internal_repository()?;
    Ok((
        INTERNAL_ROOT.to_vec(),
        base.join("metadata/")
            .expect("valid internal metadata directory"),
        base.join("targets/")
            .expect("valid internal targets directory"),
    ))
}

#[cfg(not(feature = "internal-release"))]
fn repository_location() -> Result<(Vec<u8>, Url, Url)> {
    #[cfg(feature = "update-test")]
    if let Some(repository) = test_repository()? {
        let url = Url::from_directory_path(&repository).map_err(|_| {
            invalid(
                "update_catalog_invalid",
                "test repository is not an absolute directory",
            )
        })?;
        let root = bounded_file(&repository.join("root.json")).map_err(|_| {
            invalid(
                "update_trust_root_missing",
                "isolated trust root cannot be read",
            )
        })?;
        return Ok((
            root,
            url.join("metadata/").unwrap(),
            url.join("targets/").unwrap(),
        ));
    }
    let root = OFFICIAL_ROOT.ok_or_else(|| {
        invalid(
            "update_trust_root_missing",
            "this build does not include an approved official update root",
        )
    })?;
    let base = Url::parse(CHANNEL_BASE).expect("valid official update URL");
    Ok((
        root.as_bytes().to_vec(),
        base.join("metadata/").unwrap(),
        base.join("targets/").unwrap(),
    ))
}

#[cfg(feature = "update-test")]
fn test_repository() -> Result<Option<std::path::PathBuf>> {
    std::env::var_os("BMEM_UPDATE_TEST_REPOSITORY")
        .map(|path| fs::canonicalize(path).map_err(Error::from))
        .transpose()
}

fn bounded_file(path: &Path) -> std::io::Result<Vec<u8>> {
    let mut bytes = Vec::new();
    File::open(path)?
        .take(MAX_METADATA + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_METADATA {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "oversize trust root",
        ));
    }
    Ok(bytes)
}

async fn load_catalog(root: &[u8], metadata: Url, targets: Url, cache: &Path) -> Result<Vec<u8>> {
    let repository = if metadata.scheme() == "file" {
        RepositoryLoader::new(&root, metadata, targets)
            .datastore(cache)
            .load()
            .await
    } else {
        RepositoryLoader::new(&root, metadata, targets)
            .transport(MetadataHttp(
                reqwest::Client::builder()
                    .redirect(reqwest::redirect::Policy::none())
                    .connect_timeout(Duration::from_secs(8))
                    .timeout(Duration::from_secs(30))
                    .build()
                    .map_err(|_| network("cannot initialize update transport"))?,
            ))
            .datastore(cache)
            .load()
            .await
    }
    .map_err(tuf_error)?;
    let target = TargetName::new("release-catalog.json").expect("valid catalogue target name");
    let entry = repository
        .all_targets()
        .find(|(name, _)| *name == &target)
        .map(|(_, entry)| entry)
        .ok_or_else(|| {
            invalid(
                "update_catalog_missing",
                "signed release catalogue is missing",
            )
        })?;
    if entry.length > MAX_METADATA {
        return Err(invalid(
            "update_catalog_invalid",
            "signed release catalogue exceeds its size budget",
        ));
    }
    let catalogue = repository
        .read_target(&target)
        .await
        .map_err(tuf_error)?
        .ok_or_else(|| {
            invalid(
                "update_catalog_missing",
                "signed release catalogue is missing",
            )
        })?
        .into_vec()
        .await
        .map_err(tuf_error)?;
    if catalogue.len() as u64 != entry.length {
        return Err(invalid(
            "update_catalog_invalid",
            "signed release catalogue length does not match",
        ));
    }
    let mut replacement = tempfile::NamedTempFile::new_in(cache)?;
    serde_json::to_writer(&mut replacement, repository.root())?;
    replacement.as_file().sync_all()?;
    replacement
        .persist(cache.join("trusted-root.json"))
        .map_err(|error| error.error)?;
    #[cfg(unix)]
    File::open(cache)?.sync_all()?;
    Ok(catalogue)
}

fn tuf_error(error: tough::error::Error) -> Error {
    use tough::error::Error as TufError;
    match error {
        TufError::ExpiredMetadata { .. } => invalid(
            "update_metadata_expired",
            "signed update metadata has expired",
        ),
        TufError::OlderMetadata { .. } | TufError::OlderSnapshotInTimestamp { .. } => invalid(
            "update_metadata_rollback",
            "channel metadata is older than the last trusted version",
        ),
        TufError::Transport { .. } => network("cannot retrieve update channel data"),
        _ => invalid(
            "update_signature_invalid",
            "signed update metadata could not be verified",
        ),
    }
}

#[derive(Clone, Debug)]
struct MetadataHttp(reqwest::Client);

#[tough::async_trait]
impl tough::Transport for MetadataHttp {
    async fn fetch(
        &self,
        url: Url,
    ) -> std::result::Result<tough::TransportStream, tough::TransportError> {
        let error = |kind| tough::TransportError::new(kind, "official update channel");
        if !https(&url)
            || url.host_str() != Some(CHANNEL_HOST)
            || !url.path().starts_with(CHANNEL_PATH)
            || url.query().is_some()
        {
            return Err(error(TransportErrorKind::UnsupportedUrlScheme));
        }
        let mut response = self
            .0
            .get(url)
            .send()
            .await
            .map_err(|_| error(TransportErrorKind::Other))?;
        if response.status() == reqwest::StatusCode::NOT_FOUND {
            return Err(error(TransportErrorKind::FileNotFound));
        }
        if !response.status().is_success() {
            return Err(error(TransportErrorKind::Other));
        }
        let mut bytes = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| error(TransportErrorKind::Other))?
        {
            if (bytes.len() + chunk.len()) as u64 > MAX_METADATA {
                return Err(error(TransportErrorKind::Other));
            }
            bytes.extend_from_slice(&chunk);
        }
        Ok(Box::pin(futures_util::stream::once(async move {
            Ok(tough::Bytes::from(bytes))
        })))
    }
}

fn choose_release(
    catalog: Catalog,
    current_version: &str,
    current_build: &str,
    epoch: i64,
    highest: Option<&str>,
) -> Result<Option<Release>> {
    let channel = if cfg!(feature = "internal-release") {
        "internal"
    } else {
        "stable"
    };
    if catalog.schema_version != 1 || catalog.channel != channel {
        return Err(invalid(
            "update_catalog_invalid",
            "unsupported release catalogue schema or channel",
        ));
    }
    let installed = stable_version(current_version)?;
    let floor = highest
        .map(stable_version)
        .transpose()?
        .unwrap_or_else(|| installed.clone());
    let platform = host_version()?;
    let mut seen = BTreeSet::new();
    let mut latest = None;
    let mut installed_listed = false;
    let mut incompatible = None;

    for release in catalog.releases {
        let version = stable_version(&release.version)?;
        if !seen.insert((release.target.clone(), version.clone())) {
            return Err(invalid(
                "update_catalog_invalid",
                "duplicate release version for the same target",
            ));
        }
        if release.target != build_info::TARGET {
            continue;
        }
        if !valid_metadata(&release) {
            return Err(invalid(
                "update_catalog_invalid",
                "invalid signed build identity or migration metadata",
            ));
        }
        if version == installed {
            if release.build_id != current_build {
                return Err(invalid(
                    "update_release_identity_conflict",
                    "the installed version and signed release have different build identities",
                ));
            }
            installed_listed = installed >= floor;
            continue;
        }
        if version < installed || version < floor {
            continue;
        }
        let reason = if numeric_version(&release.min_os)? > platform {
            Some("requires a newer operating system or ABI")
        } else if release.min_updater > UPDATER_PROTOCOL {
            Some("requires a newer updater")
        } else if release.settings_epoch != build_info::SETTINGS_EPOCH
            || release.authority_epoch < epoch
            || !release.from_authority_epochs.contains(&epoch)
            || !release
                .from_versions
                .iter()
                .any(|from| from == current_version)
        {
            Some("has no verified direct migration from the installed version and write epoch")
        } else {
            None
        };
        if let Some(reason) = reason {
            if incompatible
                .as_ref()
                .is_none_or(|(previous, _)| &version > previous)
            {
                incompatible = Some((version, reason));
            }
            continue;
        }
        if latest
            .as_ref()
            .is_none_or(|(previous, _)| &version > previous)
        {
            latest = Some((version, release));
        }
    }
    match latest {
        Some((_, release)) => {
            asset_url(&release)?;
            Ok(Some(release))
        }
        None if let Some((version, reason)) = incompatible => Err(Error::new(
            422,
            "update_target_incompatible",
            format!("{channel} release {version} {reason}"),
        )),
        None if installed_listed => Ok(None),
        None => Err(invalid(
            "update_target_unavailable",
            "the channel has no compatible installed or upgrade target",
        )),
    }
}

fn stable_version(raw: &str) -> Result<Version> {
    let version = Version::parse(raw).map_err(|_| {
        invalid(
            "update_catalog_invalid",
            "release version is not a valid SemVer",
        )
    })?;
    if !version.pre.is_empty() || !version.build.is_empty() {
        return Err(invalid(
            "update_catalog_invalid",
            "stable release version must not contain pre-release or build metadata",
        ));
    }
    Ok(version)
}

fn valid_metadata(release: &Release) -> bool {
    (1..=128).contains(&release.build_id.len())
        && release
            .build_id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
        && lowercase_hex(&release.commit, 40)
        && lowercase_hex(&release.sha256, 64)
        && (1..=MAX_BINARY).contains(&release.len)
        && release.authority_epoch >= 0
        && release.settings_epoch >= 0
        && release.min_updater >= 1
        && release
            .from_authority_epochs
            .iter()
            .all(|epoch| *epoch >= 0)
        && release
            .from_versions
            .iter()
            .all(|version| stable_version(version).is_ok())
}

fn lowercase_hex(raw: &str, len: usize) -> bool {
    raw.len() == len
        && raw
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn numeric_version(raw: &str) -> Result<[u32; 4]> {
    let parts = raw.trim().split('.').collect::<Vec<_>>();
    if !(1..=4).contains(&parts.len()) {
        return Err(invalid(
            "update_platform_unsupported",
            "an operating system or ABI version cannot be determined",
        ));
    }
    let mut version = [0; 4];
    for (number, part) in version.iter_mut().zip(parts) {
        *number = part.parse().map_err(|_| {
            invalid(
                "update_platform_unsupported",
                "an operating system or ABI version cannot be determined",
            )
        })?;
    }
    Ok(version)
}

#[cfg(target_os = "macos")]
fn host_version() -> Result<[u32; 4]> {
    let version = std::process::Command::new("/usr/bin/sw_vers")
        .arg("-productVersion")
        .output()?;
    numeric_version(&String::from_utf8_lossy(&version.stdout))
}

#[cfg(all(target_os = "linux", target_env = "gnu"))]
fn host_version() -> Result<[u32; 4]> {
    unsafe extern "C" {
        fn gnu_get_libc_version() -> *const std::ffi::c_char;
    }
    let version = unsafe { std::ffi::CStr::from_ptr(gnu_get_libc_version()) };
    numeric_version(&version.to_string_lossy())
}

#[cfg(windows)]
fn host_version() -> Result<[u32; 4]> {
    #[repr(C)]
    struct WindowsVersion {
        size: u32,
        major: u32,
        minor: u32,
        build: u32,
        platform: u32,
        service_pack: [u16; 128],
    }
    #[link(name = "ntdll")]
    unsafe extern "system" {
        fn RtlGetVersion(version: *mut WindowsVersion) -> i32;
    }
    let mut version = WindowsVersion {
        size: std::mem::size_of::<WindowsVersion>() as u32,
        major: 0,
        minor: 0,
        build: 0,
        platform: 0,
        service_pack: [0; 128],
    };
    if unsafe { RtlGetVersion(&mut version) } != 0 {
        return Err(invalid(
            "update_platform_unsupported",
            "Windows version cannot be determined",
        ));
    }
    Ok([version.major, version.minor, version.build, 0])
}

#[cfg(not(any(
    target_os = "macos",
    all(target_os = "linux", target_env = "gnu"),
    windows
)))]
fn host_version() -> Result<[u32; 4]> {
    Err(invalid(
        "update_platform_unsupported",
        "there is no release channel for this platform",
    ))
}

fn network(message: &'static str) -> Error {
    Error::new(503, "update_transport_failed", message)
}

fn invalid(code: &'static str, message: &'static str) -> Error {
    Error::new(422, code, message)
}

fn https(url: &Url) -> bool {
    url.scheme() == "https"
        && url.username().is_empty()
        && url.password().is_none()
        && url.port().is_none()
        && url.fragment().is_none()
}

#[cfg(feature = "internal-release")]
fn asset_url(release: &Release) -> Result<Url> {
    let denied = || {
        invalid(
            "update_asset_location_invalid",
            "binary is outside the internal assets directory",
        )
    };
    let suffix = if cfg!(windows) { ".exe" } else { "" };
    let name = format!("bmem-{}-{}{suffix}", release.version, build_info::TARGET);
    if release.url != format!("assets/{name}") {
        return Err(denied());
    }
    let base = internal_repository()?;
    let url = base.join(&release.url).map_err(|_| denied())?;
    let file = url.to_file_path().map_err(|_| denied())?;
    let canonical =
        fs::canonicalize(&file).map_err(|_| network("cannot open the signed internal binary"))?;
    let root = fs::canonicalize(base.to_file_path().map_err(|_| denied())?)?;
    if canonical != root.join("assets").join(name) {
        return Err(denied());
    }
    Ok(url)
}

#[cfg(not(feature = "internal-release"))]
fn asset_url(release: &Release) -> Result<Url> {
    let denied = || {
        invalid(
            "update_asset_location_invalid",
            "signed binary is outside the official release location",
        )
    };
    let url = Url::parse(&release.url).map_err(|_| denied())?;
    #[cfg(feature = "update-test")]
    if url.scheme() == "file" {
        let repo = test_repository()?.ok_or_else(denied)?;
        let file = url.to_file_path().map_err(|_| denied())?;
        if fs::canonicalize(file)?.starts_with(repo.join("targets")) {
            return Ok(url);
        }
        return Err(denied());
    }
    let prefix = format!("{RELEASE_PREFIX}v{}/", release.version);
    if https(&url)
        && url.host_str() == Some("github.com")
        && url.query().is_none()
        && url.path().strip_prefix(&prefix).is_some_and(|name| {
            !name.is_empty()
                && name
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-' | b'_'))
        })
    {
        return Ok(url);
    }
    Err(denied())
}

pub fn download(release: &Release, destination: &Path) -> Result<()> {
    if !valid_metadata(release) {
        return Err(invalid(
            "update_catalog_invalid",
            "invalid signed binary length or digest",
        ));
    }
    let url = asset_url(release)?;
    let mut file = tempfile::NamedTempFile::new_in(
        destination
            .parent()
            .ok_or_else(|| Error::invalid("invalid update destination"))?,
    )?;

    if url.scheme() == "file" {
        #[cfg(any(feature = "update-test", feature = "internal-release"))]
        copy_binary(
            File::open(url.to_file_path().expect("validated local asset URL"))?,
            &mut file,
            release,
        )?;
        #[cfg(not(any(feature = "update-test", feature = "internal-release")))]
        return Err(invalid(
            "update_asset_location_invalid",
            "local release assets are disabled in production",
        ));
    } else {
        let response = reqwest::blocking::Client::builder()
            .redirect(reqwest::redirect::Policy::custom(|attempt| {
                let url = attempt.url();
                if attempt.previous().len() <= 4
                    && https(url)
                    && matches!(
                        url.host_str(),
                        Some(
                            "release-assets.githubusercontent.com"
                                | "objects.githubusercontent.com"
                        )
                    )
                {
                    attempt.follow()
                } else {
                    attempt.error("release redirect is not approved")
                }
            }))
            .connect_timeout(Duration::from_secs(8))
            .timeout(Duration::from_secs(120))
            .build()
            .map_err(|_| network("cannot initialize asset transport"))?
            .get(url)
            .send()
            .and_then(reqwest::blocking::Response::error_for_status)
            .map_err(|_| network("cannot download the selected release binary"))?;
        copy_binary(response, &mut file, release)?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.as_file()
            .set_permissions(fs::Permissions::from_mode(0o700))?;
    }
    file.as_file().sync_all()?;
    file.persist_noclobber(destination)
        .map_err(|error| error.error)?;
    Ok(())
}

fn copy_binary(
    mut source: impl Read,
    mut destination: impl Write,
    release: &Release,
) -> Result<()> {
    let mismatch = || {
        invalid(
            "update_asset_digest_mismatch",
            "downloaded binary does not match the signed length and digest",
        )
    };
    let mut hash = Sha256::new();
    let mut remaining = release.len;
    let mut chunk = [0; 64 * 1024];
    loop {
        let size = source
            .read(&mut chunk)
            .map_err(|_| network("release binary download was interrupted"))?;
        if size == 0 {
            break;
        }
        remaining = remaining.checked_sub(size as u64).ok_or_else(mismatch)?;
        hash.update(&chunk[..size]);
        destination.write_all(&chunk[..size])?;
    }
    if remaining != 0 || format!("{:x}", hash.finalize()) != release.sha256 {
        return Err(mismatch());
    }
    Ok(())
}
