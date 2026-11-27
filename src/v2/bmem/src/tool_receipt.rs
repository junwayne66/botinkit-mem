use crate::error::{Error, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;

const KIND: &str = "artifact_check_v1";

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum Status {
    Passed,
    Failed,
    Timeout,
    Unknown,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ArtifactCheck<'a> {
    kind: &'a str,
    #[serde(rename = "status")]
    _status: Status,
    marker: &'a str,
}

pub fn is_typed(value: &Value) -> bool {
    value["kind"] == KIND
}

pub fn valid_marker_check(value: &Value) -> bool {
    ArtifactCheck::deserialize(value).is_ok_and(|check| {
        let marker = check.marker.as_bytes();
        check.kind == KIND
            && marker.len() == 4
            && marker
                .iter()
                .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit())
            && marker.iter().any(u8::is_ascii_uppercase)
            && marker.iter().any(u8::is_ascii_digit)
    })
}

pub fn valid_field(field: &str) -> bool {
    (1..=64).contains(&field.len())
        && field
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ArtifactCheckRef {
    pub handle: String,
    pub field: String,
}

impl ArtifactCheckRef {
    pub fn parse(value: &Value) -> Result<Self> {
        let reference = Self::deserialize(value)
            .map_err(|_| Error::invalid("artifact_check_ref requires a handle and check field"))?;
        if !valid_field(&reference.field)
            || reference.handle.is_empty()
            || reference.handle.len() > 128
        {
            return Err(Error::invalid("invalid artifact check reference"));
        }
        Ok(reference)
    }
}
