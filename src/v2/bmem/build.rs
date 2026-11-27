use sha2::{Digest, Sha256};
use std::path::Path;

fn add_tree(root: &Path, hash: &mut Sha256) {
    let mut files: Vec<_> = std::fs::read_dir(root)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    files.sort();
    for file in files {
        if file.is_dir() {
            add_tree(&file, hash);
        } else {
            hash.update(file.to_string_lossy().as_bytes());
            hash.update([0]);
            hash.update(std::fs::read(&file).unwrap());
            hash.update([0]);
        }
    }
}

fn main() {
    for path in [
        "src",
        "../web",
        "Cargo.toml",
        "Cargo.lock",
        "release-root.json",
    ] {
        println!("cargo:rerun-if-changed={path}");
    }
    for variable in [
        "BMEM_BUILD_COMMIT",
        "BMEM_BUILD_VERSION",
        "BMEM_OFFICIAL_TUF_ROOT",
        "BMEM_INTERNAL_TUF_ROOT",
    ] {
        println!("cargo:rerun-if-env-changed={variable}");
    }
    let update_test = std::env::var_os("CARGO_FEATURE_UPDATE_TEST").is_some();
    let internal = std::env::var_os("CARGO_FEATURE_INTERNAL_RELEASE").is_some();
    assert!(
        !internal || (!update_test && std::env::var_os("CARGO_FEATURE_EVALUATION_CLOCK").is_none()),
        "internal-release cannot be combined with fault injection or a frozen clock"
    );
    let version = if update_test || internal {
        std::env::var("BMEM_BUILD_VERSION")
            .unwrap_or_else(|_| std::env::var("CARGO_PKG_VERSION").unwrap())
    } else {
        std::env::var("CARGO_PKG_VERSION").unwrap()
    };
    assert!(
        version
            .bytes()
            .all(|byte| byte.is_ascii_digit() || byte == b'.')
    );
    let target = std::env::var("TARGET").unwrap();
    let commit = std::env::var("BMEM_BUILD_COMMIT").unwrap_or_else(|_| "local".into());
    assert!(
        commit == "local"
            || (commit.len() == 40 && commit.bytes().all(|byte| byte.is_ascii_hexdigit()))
    );
    let mut web = Sha256::new();
    add_tree(Path::new("../web"), &mut web);
    let web_id = format!("{:x}", web.finalize());
    let mut build = Sha256::new();
    for item in [
        &version,
        &target,
        &commit,
        &web_id,
        &std::env::var("PROFILE").unwrap(),
    ] {
        build.update(item.as_bytes());
        build.update([0]);
    }
    for file in ["Cargo.toml", "Cargo.lock", "build.rs", "release-root.json"] {
        if let Ok(bytes) = std::fs::read(file) {
            build.update(bytes);
        }
        build.update([0]);
    }
    let trusted_root = std::env::var("BMEM_OFFICIAL_TUF_ROOT").unwrap_or_default();
    assert!(
        !internal || trusted_root.trim().is_empty(),
        "an internal build cannot embed the public-channel trust root"
    );
    build.update(trusted_root.as_bytes());
    let root_fingerprint = if trusted_root.trim().is_empty() {
        String::new()
    } else {
        format!("{:x}", Sha256::digest(trusted_root.as_bytes()))
    };
    println!("cargo:rustc-env=BMEM_OFFICIAL_ROOT_SHA256={root_fingerprint}");
    build.update([0]);
    let internal_fingerprint = if internal {
        let path = std::path::PathBuf::from(
            std::env::var_os("BMEM_INTERNAL_TUF_ROOT")
                .expect("set BMEM_INTERNAL_TUF_ROOT to the public internal root file"),
        );
        println!("cargo:rerun-if-changed={}", path.display());
        let root = std::fs::read(path).expect("read the internal TUF public root file");
        assert!(
            !root.is_empty() && root.len() <= 1024 * 1024,
            "invalid internal TUF root size"
        );
        let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
        std::fs::write(out.join("internal-tuf-root.json"), &root)
            .expect("stage the internal public root");
        build.update(&root);
        build.update([0]);
        format!("{:x}", Sha256::digest(root))
    } else {
        String::new()
    };
    println!("cargo:rustc-env=BMEM_INTERNAL_ROOT_SHA256={internal_fingerprint}");
    let mut features: Vec<_> = std::env::vars()
        .filter(|(key, _)| key.starts_with("CARGO_FEATURE_"))
        .collect();
    features.sort();
    for (name, value) in features {
        build.update(name.as_bytes());
        build.update(value.as_bytes());
        build.update([0]);
    }
    add_tree(Path::new("src"), &mut build);
    println!("cargo:rustc-env=BMEM_BUILD_ID={:x}", build.finalize());
    println!("cargo:rustc-env=BMEM_WEB_BUILD_ID={web_id}");
    println!("cargo:rustc-env=BMEM_VERSION={version}");
    println!("cargo:rustc-env=BMEM_BUILD_TARGET={target}");
    println!("cargo:rustc-env=BMEM_COMMIT={commit}");
}
