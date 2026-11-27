use serde_json::{Value, json};

pub const ID: &str = env!("BMEM_BUILD_ID");
pub const VERSION: &str = env!("BMEM_VERSION");
pub const TARGET: &str = env!("BMEM_BUILD_TARGET");
pub const WEB_ID: &str = env!("BMEM_WEB_BUILD_ID");
pub const COMMIT: &str = env!("BMEM_COMMIT");
pub const SETTINGS_EPOCH: i64 = 0;
const OFFICIAL_ROOT_SHA256: &str = env!("BMEM_OFFICIAL_ROOT_SHA256");
pub const INTERNAL_ROOT_SHA256: &str = env!("BMEM_INTERNAL_ROOT_SHA256");

pub fn report() -> Value {
    json!({
        "version": VERSION, "target": TARGET, "build_id": ID, "web_build_id": WEB_ID,
        "authority_epoch": crate::storage_epoch::WRITE_EPOCH, "settings_epoch": SETTINGS_EPOCH,
        "commit": COMMIT,
        "test_build": cfg!(any(feature = "evaluation-clock", feature = "update-test", feature = "internal-release")),
        "official_update_root_sha256": (!OFFICIAL_ROOT_SHA256.is_empty()).then_some(OFFICIAL_ROOT_SHA256),
        "internal_update_root_sha256": (!INTERNAL_ROOT_SHA256.is_empty()).then_some(INTERNAL_ROOT_SHA256),
    })
}
