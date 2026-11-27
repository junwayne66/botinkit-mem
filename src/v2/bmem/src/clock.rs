use crate::error::Result;
use chrono::{SecondsFormat, Utc};

pub fn now() -> Result<String> {
    #[cfg(feature = "evaluation-clock")]
    if let Some(path) = std::env::var_os("BMEM_EVALUATION_CLOCK_FILE") {
        let timestamp = std::fs::read_to_string(path)?;
        let instant = chrono::DateTime::parse_from_rfc3339(timestamp.trim())
            .map_err(|_| crate::error::Error::internal("invalid evaluation clock file"))?;
        return Ok(instant
            .with_timezone(&Utc)
            .to_rfc3339_opts(SecondsFormat::Nanos, true));
    }
    Ok(Utc::now().to_rfc3339_opts(SecondsFormat::Nanos, true))
}
