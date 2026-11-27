use serde_json::{Value, json};
use uuid::Uuid;

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Debug, thiserror::Error)]
#[error("{message}")]
pub struct Error {
    pub status: u16,
    pub code: String,
    pub message: String,
}

impl Error {
    pub fn new(status: u16, code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            status,
            code: code.into(),
            message: message.into(),
        }
    }

    pub fn invalid(message: impl Into<String>) -> Self {
        Self::new(422, "invalid_input", message)
    }

    pub fn conflict(message: impl Into<String>) -> Self {
        let message = message.into();
        let code = if message.starts_with("key_conflict") {
            "key_conflict"
        } else if message.starts_with("cursor_mismatch") {
            "cursor_mismatch"
        } else {
            "revision_changed"
        };
        Self::new(409, code, message)
    }

    pub fn missing(message: impl Into<String>) -> Self {
        Self::new(404, "not_available", message)
    }

    pub fn denied(message: impl Into<String>) -> Self {
        Self::new(403, "action_denied", message)
    }

    pub fn internal(message: impl Into<String>) -> Self {
        Self::new(500, "internal_error", message)
    }

    pub fn unavailable(message: impl Into<String>) -> Self {
        Self::new(503, "temporarily_unavailable", message)
    }

    pub fn value(&self) -> Value {
        json!({
            "code": self.code,
            "message": self.message,
            "request_id": Uuid::new_v4(),
            "retryable": self.status == 503
        })
    }
}

impl From<std::io::Error> for Error {
    fn from(value: std::io::Error) -> Self {
        Self::internal(value.to_string())
    }
}

impl From<rusqlite::Error> for Error {
    fn from(value: rusqlite::Error) -> Self {
        Self::internal(value.to_string())
    }
}

impl From<serde_json::Error> for Error {
    fn from(value: serde_json::Error) -> Self {
        Self::invalid(value.to_string())
    }
}
