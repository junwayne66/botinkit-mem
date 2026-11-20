//! bmem v2 settings service — the single `config.patch` / `config.read`
//! handler shared by the CLI (`bmem config set`, `bmem model …`,
//! `bmem connector configure`) and the Web console (`PATCH /v2/config`).
//!
//! Contract sources: `docs/v2/technical/interfaces/config-patch.md`,
//! `config-patch.example.json` (paired input/output JSON Schemas),
//! `config-patch-exchange.schema.json` and
//! `docs/v2/technical/cli-and-platform.md`.
//!
//! Guarantees implemented here:
//! * One process-wide mutex serializes every settings read/write; both
//!   entry points run through the exact same `patch` handler.
//! * A single SQLite transaction commits the settings snapshot, model keys,
//!   and idempotency receipt. The in-memory snapshot advances after commit.
//! * The latest 64 writes per actor retain a request digest and safe receipt.
//!   Model API keys live in a dedicated table and never appear in readbacks.
//! * Bindings are only accepted for models that passed an *actual* probe via
//!   [`Settings::mark_tested`]; this module performs no provider I/O and
//!   cannot fabricate a probe result. While `vector_search.enabled` is false,
//!   `bound_model("embed")` yields no model and embed probes are refused, so
//!   disabled vectors imply no implicit embedding calls.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard};

use rusqlite::{Connection, OptionalExtension, Transaction, params};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::error::{Error, Result};

/// Legacy settings file imported when opening an existing workspace.
const CONFIG_FILE: &str = "config.toml";
/// Number of recent committed writes remembered per actor for idempotent
/// replay (`Idempotency-Key` window).
const IDEMPOTENCY_WINDOW: usize = 64;
const PROFILES: [&str; 3] = ["data", "balanced", "high"];
pub(crate) const PURPOSES: [&str; 2] = ["extract", "embed"];
const GENERATION_PROTOCOLS: [&str; 3] = ["openai_chat", "anthropic_messages", "ollama"];
const EMBEDDING_PROTOCOLS: [&str; 2] = ["openai_embeddings", "ollama"];
const PROTOCOLS: [&str; 4] = [
    "openai_chat",
    "anthropic_messages",
    "openai_embeddings",
    "ollama",
];

/// A configured model as seen by the rest of the service.
///
/// `credential_ref` identifies an existing reference or a versioned database key.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Model {
    pub id: String,
    pub name: String,
    pub protocol: String,
    pub endpoint: String,
    pub model_id: String,
    pub credential_ref: Option<Value>,
}

impl Model {
    pub fn local_endpoint(&self) -> bool {
        let Ok(endpoint) = url::Url::parse(&self.endpoint) else {
            return false;
        };
        match endpoint.host() {
            Some(url::Host::Ipv4(ip)) => ip.is_loopback(),
            Some(url::Host::Ipv6(ip)) => ip.is_loopback(),
            _ => false,
        }
    }

    fn supports_background(&self, purpose: &str) -> bool {
        purpose != "embed" || self.local_endpoint()
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct StoredModel {
    #[serde(flatten)]
    model: Model,
    /// Purposes for which an actual probe succeeded (see `mark_tested`).
    /// Invalidated whenever protocol, endpoint, provider model id or the
    /// credential reference changes.
    #[serde(default)]
    tested: BTreeSet<String>,
}

impl StoredModel {
    fn key_fingerprint(&self) -> (String, String, String, Value) {
        (
            self.model.protocol.clone(),
            self.model.endpoint.clone(),
            self.model.model_id.clone(),
            self.model.credential_ref.clone().unwrap_or(Value::Null),
        )
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct Bindings {
    #[serde(default)]
    extract: Option<String>,
    #[serde(default)]
    embed: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ConnectorSettings {
    display_name: String,
    #[serde(default)]
    page_size: Option<i64>,
    #[serde(default)]
    credential_ref: Option<Value>,
}

/// One remembered committed write: `(actor, "config.patch", key)` scoped by
/// the caller; `request` is the accepted body digest (or legacy canonical JSON), and
/// `response` the receipt replayed on retry.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct IdemEntry {
    key: String,
    request: String,
    response: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct IdempotencyWindow {
    actors: BTreeMap<String, VecDeque<IdemEntry>>,
}

impl IdempotencyWindow {
    fn find(&self, actor: &str, key: &str) -> Option<&IdemEntry> {
        self.actors
            .get(actor)
            .and_then(|q| q.iter().find(|e| e.key == key))
    }

    fn record(&mut self, actor: String, key: String, request: String, response: String) {
        let q = self.actors.entry(actor).or_default();
        if let Some(pos) = q.iter().position(|e| e.key == key) {
            q.remove(pos);
        }
        q.push_back(IdemEntry {
            key,
            request,
            response,
        });
        while q.len() > IDEMPOTENCY_WINDOW {
            q.pop_front();
        }
    }
}

/// The full durable snapshot of the settings service.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
struct ConfigFile {
    config_revision: u64,
    /// Monotonic local counter feeding generated model ids together with
    /// (instance root hash, revision); never reused across removals.
    instance_seed: u64,
    profile: String,
    vector_search_enabled: bool,
    models: Vec<StoredModel>,
    bindings: Bindings,
    connectors: BTreeMap<String, ConnectorSettings>,
    idempotency: IdempotencyWindow,
}

impl Default for ConfigFile {
    fn default() -> Self {
        // `bmem init` ships balanced profile and vector search off
        // ("向量相似检索默认关闭"); extraction stays "pending_model".
        ConfigFile {
            config_revision: 0,
            instance_seed: 0,
            profile: "balanced".into(),
            vector_search_enabled: false,
            models: Vec::new(),
            bindings: Bindings::default(),
            connectors: BTreeMap::new(),
            idempotency: IdempotencyWindow::default(),
        }
    }
}

/// Owner settings database and its in-memory read snapshot.
pub struct Settings {
    root: PathBuf,
    database: Connection,
    inner: Mutex<ConfigFile>,
}

impl Settings {
    pub fn open(root: &Path) -> Result<Self> {
        let path = root.join("data/settings.sqlite3");
        if !path.exists() {
            let mut options = fs::OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.mode(0o600);
            }
            options.open(&path)?;
        }
        let database = Connection::open(path)?;
        database.execute_batch(
            "PRAGMA synchronous=FULL;
             PRAGMA secure_delete=ON;
             CREATE TABLE IF NOT EXISTS user_settings (
                 id INTEGER PRIMARY KEY CHECK(id=1), snapshot TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS model_api_keys (
                 id TEXT PRIMARY KEY, api_key TEXT NOT NULL
             );",
        )?;
        let stored = database
            .query_row("SELECT snapshot FROM user_settings WHERE id=1", [], |row| {
                row.get::<_, String>(0)
            })
            .optional()?;
        let config = if let Some(snapshot) = stored {
            serde_json::from_str::<ConfigFile>(&snapshot)
                .map_err(|_| Error::internal("cannot read the workspace settings snapshot"))?
        } else {
            let path = root.join(CONFIG_FILE);
            let initial = if path.exists() {
                let text = fs::read_to_string(&path)?;
                toml::from_str::<ConfigFile>(&text)
                    .map_err(|_| Error::internal("cannot read the previous config.toml"))?
            } else {
                ConfigFile::default()
            };
            write_snapshot(&database, &initial)?;
            initial
        };
        Ok(Settings {
            root: root.to_path_buf(),
            database,
            inner: Mutex::new(config),
        })
    }

    pub fn model_credential(&self, model: &Model) -> Result<Option<String>> {
        match &model.credential_ref {
            None => Ok(None),
            Some(reference) if reference["backend"] == "database" => self
                .database
                .query_row(
                    "SELECT api_key FROM model_api_keys WHERE id=?",
                    [reference["id"].as_str().unwrap_or_default()],
                    |row| row.get(0),
                )
                .optional()?
                .map(Some)
                .ok_or_else(|| Error::unavailable("model API key is unavailable")),
            Some(reference) if reference["backend"] == "env" => {
                let name = reference["name"].as_str().unwrap_or_default();
                std::env::var(name).map(Some).map_err(|_| {
                    Error::unavailable("model credential environment reference is unavailable")
                })
            }
            _ => Err(Error::unavailable(
                "model credential backend is unavailable",
            )),
        }
    }

    /// Locking never fails the operation; the state is only ever swapped in
    /// whole, so a poisoned guard still holds a consistent snapshot.
    fn lock(&self) -> MutexGuard<'_, ConfigFile> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// `config.read` / `GET /v2/config`. Owner-safe: exposes revision,
    /// profile, vector flag, model catalog (with `credential_configured`
    /// and tested purposes), both bindings, connector settings and the
    /// readiness derived from configuration. Raw credential values — and the
    /// references themselves — never appear; only their presence.
    pub fn get(&self) -> Value {
        let c = self.lock();
        let models: Vec<Value> = c
            .models
            .iter()
            .map(|m| {
                json!({
                    "id": m.model.id,
                    "name": m.model.name,
                    "protocol": m.model.protocol,
                    "endpoint": m.model.endpoint,
                    "model_id": m.model.model_id,
                    "credential_configured": m.model.credential_ref.is_some(),
                    "local_endpoint": m.model.local_endpoint(),
                    "tested": m.tested.iter().collect::<Vec<&String>>(),
                })
            })
            .collect();
        let connectors: Vec<Value> = c
            .connectors
            .iter()
            .map(|(source_id, s)| {
                json!({
                    "source_id": source_id,
                    "display_name": s.display_name,
                    "page_size": s.page_size,
                    "credential_configured": s.credential_ref.is_some(),
                })
            })
            .collect();
        let mut out = json!({
            "config_revision": c.config_revision,
            "profile": c.profile,
            "vector_search": { "enabled": c.vector_search_enabled },
            "models": models,
            "bindings": { "extract": c.bindings.extract, "embed": c.bindings.embed },
            "connectors": connectors,
        });
        if let Some(activity) = build_activity(&c, true, true, None) {
            out["activity"] = activity;
        }
        out
    }

    /// Full record of one model for callers that need the credential
    /// reference (e.g. the `model.test` handler resolving a key from the OS
    /// store). Never expose this value on a CLI/Web response.
    pub fn model(&self, id: &str) -> Result<Model> {
        let c = self.lock();
        c.models
            .iter()
            .find(|m| m.model.id == id)
            .map(|m| m.model.clone())
            .ok_or_else(|| Error::missing(format!("model '{id}' is not configured")))
    }

    /// The eligible tested model currently bound for `purpose`, or `None`. While vector
    /// search is disabled, `embed` never resolves — callers therefore cannot
    /// issue an embedding request implicitly. Bindings intentionally survive
    /// a vector disable ("关闭停止新嵌入作业，保留原绑定").
    pub fn bound_model(&self, purpose: &str) -> Option<Model> {
        let c = self.lock();
        let id = match purpose {
            "extract" => c.bindings.extract.as_deref()?,
            "embed" if c.vector_search_enabled => c.bindings.embed.as_deref()?,
            _ => return None,
        };
        c.models
            .iter()
            .find(|m| {
                m.model.id == id
                    && m.model.supports_background(purpose)
                    && m.tested.contains(purpose)
            })
            .map(|m| m.model.clone())
    }

    /// True when an actual probe for (`id`, `purpose`) is on record and has
    /// not been invalidated by a route/credential change.
    /// Record that the *model.test* handler ran a real probe for this model
    /// and purpose and it succeeded. Settings never synthesizes this fact and
    /// no patch input can set it; it persists across restarts without
    /// advancing `config_revision` (revision tracks owner config edits, and
    /// `POST /v2/models/{id}/test` is not a config write).
    pub fn mark_tested(&mut self, id: &str, purpose: &str) -> Result<Value> {
        validate_purpose(purpose, "purpose")?;
        let mut c = self.lock();
        let vector_enabled = c.vector_search_enabled;
        let protocol = match c.models.iter().find(|m| m.model.id == id) {
            Some(m) => m.model.protocol.clone(),
            None => {
                return Err(Error::missing(format!("model '{id}' is not configured")));
            }
        };
        if purpose == "embed" && !vector_enabled {
            // "向量未启用时服务对 --for embed 的测试请求统一返回功能已关闭":
            // no probe is attempted and no vector work is implied.
            return Err(Error::denied(
                "vector search is disabled; embed probes are refused",
            ));
        }
        let compatible = match purpose {
            "extract" => GENERATION_PROTOCOLS.contains(&protocol.as_str()),
            _ => EMBEDDING_PROTOCOLS.contains(&protocol.as_str()),
        };
        if !compatible {
            return Err(Error::invalid(format!(
                "models.protocol '{protocol}' cannot serve purpose '{purpose}'",
            )));
        }
        let credential_configured = c
            .models
            .iter()
            .find(|m| m.model.id == id)
            .is_some_and(|m| m.model.credential_ref.is_some());
        let mut target = c.clone();
        if let Some(model) = target.models.iter_mut().find(|m| m.model.id == id) {
            model.tested.insert(purpose.to_string());
        }
        write_snapshot(&self.database, &target)?;
        *c = target;
        Ok(json!({
            "id": id,
            "purpose": purpose,
            "tested": true,
            "credential_configured": credential_configured,
        }))
    }
}

fn write_snapshot(db: &Connection, config: &ConfigFile) -> Result<()> {
    db.execute(
        "INSERT INTO user_settings(id, snapshot) VALUES(1, ?)
         ON CONFLICT(id) DO UPDATE SET snapshot=excluded.snapshot",
        [serde_json::to_string(config)?],
    )?;
    Ok(())
}

fn database_key(reference: Option<&Value>) -> Option<&str> {
    let reference = reference?;
    if reference["backend"] != "database" {
        return None;
    }
    reference["id"].as_str()
}

/// Persist the new key and retire the old version in the same transaction as the snapshot.
fn write_model_key(
    tx: &Transaction<'_>,
    previous: &ConfigFile,
    target: &ConfigFile,
    touched: &Touched,
) -> Result<()> {
    if let Some((id, key)) = &touched.new_api_key {
        tx.execute(
            "INSERT INTO model_api_keys(id, api_key) VALUES(?, ?)",
            params![id, key],
        )?;
    }
    if let Some(result) = &touched.model_op {
        let id = result["id"].as_str().unwrap_or_default();
        let reference = |config: &ConfigFile| {
            config
                .models
                .iter()
                .find(|stored| stored.model.id == id)
                .and_then(|stored| database_key(stored.model.credential_ref.as_ref()))
                .map(str::to_owned)
        };
        let old = reference(previous);
        if old != reference(target)
            && let Some(old) = old
        {
            tx.execute("DELETE FROM model_api_keys WHERE id=?", [old])?;
        }
    }
    Ok(())
}

/// Which paired-schema result groups the receipt must carry.
#[derive(Default)]
struct Touched {
    new_api_key: Option<(String, String)>,
    model_op: Option<Value>,
    bindings: bool,
    connector_source: Option<String>,
    profile_changed: bool,
    vector_changed: bool,
}

impl Settings {
    /// `config.patch` / `PATCH /v2/config` — the one handler behind
    /// `bmem config set`, `bmem model add|configure|remove|use|unbind`, and
    /// the Web settings forms. `actor` is the authenticated owner principal
    /// resolved by the session layer, `key` the `Idempotency-Key` header.
    ///
    /// Order inside the single settings lock:
    /// 1. idempotent replay (same actor+key+normalized body) — *before* any
    ///    revision comparison, so a retry after a disconnect succeeds even
    ///    when the revision already moved;
    /// 2. strict schema validation of the envelope and patch;
    /// 3. `expected_revision` comparison (`revision_changed` conflict);
    /// 4. build the complete target config on a clone, validate all
    ///    cross-references against that target, then persist atomically and
    ///    only then swap the in-memory snapshot and advance the revision once.
    ///
    /// Any rejected field leaves the previous revision intact.
    pub fn patch(&mut self, actor: &str, key: &str, request: Value) -> Result<Value> {
        let mut c = self.lock();
        if actor.trim().is_empty() {
            return Err(Error::denied(
                "settings writes require an identified owner actor",
            ));
        }
        if key.trim().is_empty() || key.chars().count() > 256 {
            return Err(Error::invalid(
                "an Idempotency-Key header (non-empty, bounded) is required for config.patch",
            ));
        }
        // Canonical form: serde_json maps are key-ordered, so the compact
        // serialization is stable across entries; numbers keep their token.
        let normalized = serde_json::to_string(&request)?;
        let request_digest = format!("sha256:{:x}", Sha256::digest(normalized.as_bytes()));

        // --- 1. idempotency replay / conflict (before revision check) ---
        if let Some(entry) = c.idempotency.find(actor, key) {
            if entry.request == request_digest || entry.request == normalized {
                return serde_json::from_str(&entry.response)
                    .map_err(|e| Error::internal(format!("cannot replay stored receipt: {e}")));
            }
            return Err(Error::conflict(
                "key_conflict: this idempotency key was already committed with a different request body",
            ));
        }

        // --- 2. envelope validation (exact paired-schema fields only) ---
        let envelope = object_of(&request, "request")?;
        only_fields(envelope, &["expected_revision", "patch"], "request")?;
        let expected_revision = match envelope.get("expected_revision") {
            Some(Value::Number(n)) => n
                .as_u64()
                .ok_or_else(|| Error::invalid("expected_revision must be an integer >= 0"))?,
            Some(other) => {
                return Err(Error::invalid(format!(
                    "expected_revision must be an integer, got {}",
                    type_name(other)
                )));
            }
            None => return Err(Error::invalid("request is missing expected_revision")),
        };
        let patch_value = envelope
            .get("patch")
            .ok_or_else(|| Error::invalid("request is missing patch"))?;
        let patch = object_of(patch_value, "patch")?;
        only_fields(
            patch,
            &[
                "profile",
                "vector_search",
                "models",
                "bindings",
                "connectors",
            ],
            "patch",
        )?;
        if patch.is_empty() {
            return Err(Error::invalid("patch must change at least one field"));
        }

        // --- 3. compare-and-swap on the revision ---
        if expected_revision != c.config_revision {
            return Err(Error::conflict(format!(
                "revision_changed: expected_revision {expected_revision} is stale, current config_revision is {}",
                c.config_revision
            )));
        }

        // --- 4. build the whole target config on a clone first ---
        let mut target = c.clone();
        let mut touched = Touched::default();

        if let Some(profile) = patch.get("profile") {
            let profile = text_of(profile, "patch.profile")?;
            if !PROFILES.contains(&profile) {
                return Err(Error::invalid(format!(
                    "patch.profile must be one of data|balanced|high, got '{profile}'"
                )));
            }
            touched.profile_changed = target.profile != profile;
            target.profile = profile.to_owned();
        }

        if let Some(vs) = patch.get("vector_search") {
            let vs = object_of(vs, "patch.vector_search")?;
            only_fields(vs, &["enabled"], "patch.vector_search")?;
            target.vector_search_enabled = match vs.get("enabled") {
                Some(Value::Bool(b)) => *b,
                Some(other) => {
                    return Err(Error::invalid(format!(
                        "patch.vector_search.enabled must be a boolean (not a {})",
                        type_name(other)
                    )));
                }
                None => return Err(Error::invalid("patch.vector_search requires enabled")),
            };
            touched.vector_changed = true;
        }

        if let Some(models) = patch.get("models") {
            apply_model_op(&mut target, &mut touched, models, &instance_tag(&self.root))?;
        }

        if let Some(bindings) = patch.get("bindings") {
            let bindings = object_of(bindings, "patch.bindings")?;
            only_fields(bindings, &["extract", "embed"], "patch.bindings")?;
            if bindings.is_empty() {
                return Err(Error::invalid(
                    "patch.bindings must name at least one purpose",
                ));
            }
            for purpose in bindings.keys() {
                validate_purpose(purpose, "patch.bindings")?;
            }
            if let Some(v) = bindings.get("extract") {
                target.bindings.extract = optional_binding_id(v, "patch.bindings.extract")?;
            }
            if let Some(v) = bindings.get("embed") {
                target.bindings.embed = optional_binding_id(v, "patch.bindings.embed")?;
            }
            touched.bindings = true;
        }

        if let Some(conn) = patch.get("connectors") {
            let conn = object_of(conn, "patch.connectors")?;
            only_fields(conn, &["source_id", "settings"], "patch.connectors")?;
            let source_id = match conn.get("source_id") {
                Some(v) => id_of(v, "patch.connectors.source_id")?.to_string(),
                None => return Err(Error::invalid("patch.connectors requires source_id")),
            };
            let settings_value = conn
                .get("settings")
                .ok_or_else(|| Error::invalid("patch.connectors requires settings"))?;
            let settings = object_of(settings_value, "patch.connectors.settings")?;
            only_fields(
                settings,
                &["display_name", "page_size", "credential_ref"],
                "patch.connectors.settings",
            )?;
            if settings.is_empty() {
                return Err(Error::invalid(
                    "patch.connectors.settings must change at least one field",
                ));
            }
            let entry = target
                .connectors
                .entry(source_id.clone())
                .or_insert_with(|| ConnectorSettings {
                    // Connection-params step of `connector add` may create
                    // the entry; the display name falls back to the source id.
                    display_name: source_id.clone(),
                    page_size: None,
                    credential_ref: None,
                });
            if let Some(v) = settings.get("display_name") {
                entry.display_name =
                    text_of(v, "patch.connectors.settings.display_name")?.to_string();
            }
            if let Some(v) = settings.get("page_size") {
                match v {
                    Value::Null => entry.page_size = None,
                    Value::Number(n) => {
                        let n = n.as_i64().ok_or_else(|| {
                            Error::invalid("patch.connectors.settings.page_size must be an integer")
                        })?;
                        if n < 1 {
                            return Err(Error::invalid(
                                "patch.connectors.settings.page_size must be >= 1",
                            ));
                        }
                        entry.page_size = Some(n);
                    }
                    other => {
                        return Err(Error::invalid(format!(
                            "patch.connectors.settings.page_size must be an integer or null (got {})",
                            type_name(other)
                        )));
                    }
                }
            }
            if let Some(v) = settings.get("credential_ref") {
                validate_credential_ref(v, "patch.connectors.settings.credential_ref")?;
                entry.credential_ref = if v.is_null() { None } else { Some(v.clone()) };
            }
            touched.connector_source = Some(source_id);
        }

        validate_target(&target, &touched)?;

        // One successful patch advances the revision exactly once.
        let new_revision = target
            .config_revision
            .checked_add(1)
            .ok_or_else(|| Error::internal("config revision overflow"))?;
        target.config_revision = new_revision;

        let response = build_response(&target, &touched);
        let response_json = serde_json::to_string(&response)?;

        // The receipt must survive a crash between committing and replying.
        target.idempotency.record(
            actor.to_string(),
            key.to_string(),
            request_digest,
            response_json,
        );
        let tx = self.database.unchecked_transaction()?;
        write_model_key(&tx, &c, &target, &touched)?;
        write_snapshot(&tx, &target)?;
        tx.commit()?;
        *c = target;
        Ok(response)
    }
}

fn model_credential_change(
    fields: &Map<String, Value>,
    touched: &mut Touched,
    field: &str,
) -> Result<Option<Option<Value>>> {
    if let Some(key) = fields.get("api_key") {
        if fields.contains_key("credential_ref") {
            return Err(Error::invalid("provide either API_KEY or credential_ref"));
        }
        if key.is_null() {
            return Ok(Some(None));
        }
        let key = key
            .as_str()
            .filter(|value| {
                !value.is_empty()
                    && value.len() <= 8192
                    && value.bytes().all(|byte| byte.is_ascii_graphic())
            })
            .ok_or_else(|| {
                Error::invalid("API_KEY must be a non-empty token of at most 8192 characters")
            })?;
        let id = uuid::Uuid::new_v4().to_string();
        let reference = json!({"backend":"database","id":id});
        touched.new_api_key = Some((id, key.to_owned()));
        return Ok(Some(Some(reference)));
    }
    fields
        .get("credential_ref")
        .map(|reference| {
            validate_credential_ref(reference, &format!("{field}.credential_ref"))?;
            Ok((!reference.is_null()).then(|| reference.clone()))
        })
        .transpose()
}

/// Apply one `patch.models` operation (at most one model change per patch)
/// to the target config. `inst` is the stable short tag of the instance root
/// used when minting ids for new models.
fn apply_model_op(
    target: &mut ConfigFile,
    touched: &mut Touched,
    models: &Value,
    inst: &str,
) -> Result<()> {
    let obj = object_of(models, "patch.models")?;
    let op = match obj.get("op") {
        Some(Value::String(s)) => s.as_str(),
        Some(other) => {
            return Err(Error::invalid(format!(
                "patch.models.op must be the string add, update or remove (got {})",
                type_name(other)
            )));
        }
        None => return Err(Error::invalid("patch.models requires op")),
    };
    match op {
        "add" => {
            only_fields(
                obj,
                &[
                    "op",
                    "name",
                    "protocol",
                    "endpoint",
                    "model_id",
                    "credential_ref",
                    "api_key",
                ],
                "patch.models",
            )?;
            let name = match obj.get("name") {
                Some(v) => text_of(v, "patch.models.name")?.to_string(),
                None => return Err(Error::invalid("patch.models add requires name")),
            };
            let protocol = match obj.get("protocol") {
                Some(v) => validate_protocol(v, "patch.models.protocol")?.to_string(),
                None => return Err(Error::invalid("patch.models add requires protocol")),
            };
            let endpoint = match obj.get("endpoint") {
                Some(v) => validate_endpoint(v, "patch.models.endpoint")?.to_string(),
                None => return Err(Error::invalid("patch.models add requires endpoint")),
            };
            let model_id = match obj.get("model_id") {
                Some(v) => text_of(v, "patch.models.model_id")?.to_string(),
                None => return Err(Error::invalid("patch.models add requires model_id")),
            };
            let credential_ref = model_credential_change(obj, touched, "patch.models")?.flatten();
            if target.models.iter().any(|m| m.model.name == name) {
                return Err(Error::invalid(format!(
                    "patch.models.name '{name}' is already used; model names must be unique (remove or rename the existing model first)"
                )));
            }
            // Service-assigned stable id: instance tag + new revision + a
            // monotonic local seed, so ids are never reused after removal.
            target.instance_seed = target.instance_seed.saturating_add(1);
            let id = format!(
                "m_{}_{}_{}",
                target.config_revision + 1,
                target.instance_seed,
                inst
            );
            let model = Model {
                id: id.clone(),
                name,
                protocol,
                endpoint,
                model_id,
                credential_ref,
            };
            touched.model_op = Some(json!({
                "op": "add",
                "id": model.id,
                "name": model.name,
                "protocol": model.protocol,
                "endpoint": model.endpoint,
                "model_id": model.model_id,
                "credential_configured": model.credential_ref.is_some(),
            }));
            target.models.push(StoredModel {
                model,
                tested: BTreeSet::new(),
            });
            Ok(())
        }
        "update" => {
            only_fields(obj, &["op", "id", "changes"], "patch.models")?;
            let id = match obj.get("id") {
                Some(v) => id_of(v, "patch.models.id")?.to_string(),
                None => return Err(Error::invalid("patch.models update requires id")),
            };
            let changes = object_of(
                match obj.get("changes") {
                    Some(v) => v,
                    None => return Err(Error::invalid("patch.models update requires changes")),
                },
                "patch.models.changes",
            )?;
            only_fields(
                changes,
                &[
                    "name",
                    "protocol",
                    "endpoint",
                    "model_id",
                    "credential_ref",
                    "api_key",
                ],
                "patch.models.changes",
            )?;
            if changes.is_empty() {
                return Err(Error::invalid(
                    "patch.models.changes must modify at least one field",
                ));
            }
            let index = match target.models.iter().position(|m| m.model.id == id) {
                Some(i) => i,
                None => {
                    return Err(Error::invalid(format!(
                        "patch.models.id '{id}' does not reference a configured model"
                    )));
                }
            };
            // Validate the complete change set before touching anything.
            let new_name = match changes.get("name") {
                Some(v) => Some(text_of(v, "patch.models.changes.name")?.to_string()),
                None => None,
            };
            let new_protocol = match changes.get("protocol") {
                Some(v) => Some(validate_protocol(v, "patch.models.changes.protocol")?.to_string()),
                None => None,
            };
            let new_endpoint = match changes.get("endpoint") {
                Some(v) => Some(validate_endpoint(v, "patch.models.changes.endpoint")?.to_string()),
                None => None,
            };
            let new_model_id = match changes.get("model_id") {
                Some(v) => Some(text_of(v, "patch.models.changes.model_id")?.to_string()),
                None => None,
            };
            let new_credential = model_credential_change(changes, touched, "patch.models.changes")?;
            if let Some(candidate) = &new_name
                && target
                    .models
                    .iter()
                    .enumerate()
                    .any(|(i, m)| i != index && m.model.name == *candidate)
            {
                return Err(Error::invalid(format!(
                    "patch.models.changes.name '{candidate}' is already used by another model"
                )));
            }
            let before = target.models[index].key_fingerprint();
            let model = &mut target.models[index].model;
            if let Some(v) = new_name {
                model.name = v;
            }
            if let Some(v) = new_protocol {
                model.protocol = v;
            }
            if let Some(v) = new_endpoint {
                model.endpoint = v;
            }
            if let Some(v) = new_model_id {
                model.model_id = v;
            }
            if let Some(v) = new_credential {
                model.credential_ref = v;
            }
            // "Changing protocol, endpoint, provider model, or credentials invalidates old connection tests."
            // A display-name-only change keeps prior tests.
            if before != target.models[index].key_fingerprint() {
                target.models[index].tested.clear();
            }
            let model = target.models[index].model.clone();
            touched.model_op = Some(json!({
                "op": "update",
                "id": model.id,
                "name": model.name,
                "protocol": model.protocol,
                "endpoint": model.endpoint,
                "model_id": model.model_id,
                "credential_configured": model.credential_ref.is_some(),
            }));
            Ok(())
        }
        "remove" => {
            only_fields(obj, &["op", "id"], "patch.models")?;
            let id = match obj.get("id") {
                Some(v) => id_of(v, "patch.models.id")?.to_string(),
                None => return Err(Error::invalid("patch.models remove requires id")),
            };
            // Whether the id survives in the target is checked by
            // `validate_target`, so "unbind both purposes + remove the model"
            // in one patch succeeds while a still-bound removal fails.
            if !target.models.iter().any(|m| m.model.id == id) {
                return Err(Error::invalid(format!(
                    "patch.models.id '{id}' does not reference a configured model"
                )));
            }
            target.models.retain(|m| m.model.id != id);
            touched.model_op = Some(json!({ "op": "remove", "id": id }));
            Ok(())
        }
        other => Err(Error::invalid(format!(
            "patch.models.op '{other}' must be add, update or remove"
        ))),
    }
}

/// Reference validation against the *complete* target config (the service
/// "first builds the entire target configuration, then validates
/// references"). Existence of bound ids is an unconditional invariant; the
/// stricter "actually tested / protocol fits / vector on" rules apply when
/// the binding itself is (re)set in this patch, so an unrelated update that
/// invalidates a probe leaves the binding pending verification instead of
/// rewriting history.
fn validate_target(target: &ConfigFile, touched: &Touched) -> Result<()> {
    for purpose in PURPOSES {
        let bound = match purpose {
            "extract" => &target.bindings.extract,
            _ => &target.bindings.embed,
        };
        let Some(id) = bound else { continue };
        let Some(model) = target.models.iter().find(|m| m.model.id == *id) else {
            return Err(Error::invalid(format!(
                "patch.bindings.{purpose} keeps a reference to model '{id}' that no longer exists; clear the binding first or keep the model"
            )));
        };
        if !touched.bindings {
            continue;
        }
        let compatible = match purpose {
            "extract" => GENERATION_PROTOCOLS.contains(&model.model.protocol.as_str()),
            _ => EMBEDDING_PROTOCOLS.contains(&model.model.protocol.as_str()),
        };
        if !compatible {
            return Err(Error::invalid(format!(
                "patch.bindings.{purpose}: model '{id}' uses protocol '{}', which cannot serve {purpose}",
                model.model.protocol
            )));
        }
        if purpose == "embed" && !target.vector_search_enabled {
            return Err(Error::invalid(
                "patch.bindings.embed requires vector_search.enabled true (already enabled, or enabled in this same patch)",
            ));
        }
        if !model.tested.contains(purpose) {
            return Err(Error::invalid(format!(
                "patch.bindings.{purpose}: model '{id}' has not passed an actual {purpose} test for its current endpoint; run the {purpose} probe first"
            )));
        }
    }
    Ok(())
}

/// Paired output-schema receipt: required core fields always; a result group
/// appears if and only if the request touched it.
fn build_response(target: &ConfigFile, touched: &Touched) -> Value {
    let mut out = Map::new();
    out.insert("config_revision".into(), json!(target.config_revision));
    out.insert("profile".into(), json!(target.profile));
    out.insert(
        "vector_search".into(),
        json!({ "enabled": target.vector_search_enabled }),
    );
    if let Some(model_result) = &touched.model_op {
        out.insert("model_result".into(), model_result.clone());
    }
    if touched.bindings {
        out.insert(
            "bindings".into(),
            json!({ "extract": target.bindings.extract, "embed": target.bindings.embed }),
        );
    }
    if let Some(source_id) = &touched.connector_source
        && let Some(s) = target.connectors.get(source_id)
    {
        out.insert(
            "connector_result".into(),
            json!({
                "source_id": source_id,
                "display_name": s.display_name,
                "page_size": s.page_size,
                "credential_configured": s.credential_ref.is_some(),
            }),
        );
    }
    let touch_model_or_binding = touched.model_op.is_some() || touched.bindings;
    let activity = build_activity(
        target,
        touch_model_or_binding,
        touch_model_or_binding || touched.vector_changed,
        if touched.profile_changed {
            // The change queues historical repacking; completion is a jobs
            // domain observation, so the receipt reports it as pending.
            Some("pending")
        } else {
            None
        },
    );
    if let Some(activity) = activity {
        out.insert("activity".into(), activity);
    }
    Value::Object(out)
}

/// Config-derived readiness per capability. `building`/`degraded` and the
/// live repack progress belong to the jobs/index domains and are never
/// fabricated here.
fn build_activity(
    c: &ConfigFile,
    include_extract: bool,
    include_embed: bool,
    repack: Option<&'static str>,
) -> Option<Value> {
    let mut m = Map::new();
    if include_extract {
        m.insert("extract".into(), json!(binding_readiness(c, "extract")));
    }
    if include_embed {
        m.insert("embed".into(), json!(binding_readiness(c, "embed")));
    }
    if let Some(r) = repack {
        m.insert("repack".into(), json!(r));
    }
    if m.is_empty() {
        None
    } else {
        Some(Value::Object(m))
    }
}

fn binding_readiness(c: &ConfigFile, purpose: &str) -> &'static str {
    if purpose == "embed" && !c.vector_search_enabled {
        return "disabled";
    }
    let bound = match purpose {
        "extract" => c.bindings.extract.as_deref(),
        _ => c.bindings.embed.as_deref(),
    };
    match bound {
        None => "pending_model",
        Some(id) => match c.models.iter().find(|m| m.model.id == id) {
            None => "pending_model",
            Some(m) if !m.model.supports_background(purpose) => "local_model_required",
            Some(m) if m.tested.contains(purpose) => "ready",
            Some(_) => "pending_test",
        },
    }
}

// ---------------------------------------------------------------------------
// Strict request validation mirroring the paired JSON Schemas
// (`urn:bmem:v2:config-patch:input`). Every container uses
// `additionalProperties: false`: unknown fields — including a raw `api_key`
// smuggled anywhere — are rejected, never dropped, and never persisted.
// ---------------------------------------------------------------------------

fn object_of<'a>(v: &'a Value, field: &str) -> Result<&'a Map<String, Value>> {
    match v {
        Value::Object(m) => Ok(m),
        other => Err(Error::invalid(format!(
            "{field} must be an object (got {})",
            type_name(other)
        ))),
    }
}

fn only_fields(obj: &Map<String, Value>, allowed: &[&str], field: &str) -> Result<()> {
    for k in obj.keys() {
        if !allowed.contains(&k.as_str()) {
            return Err(Error::invalid(format!(
                "{field} does not accept field '{k}'; declared fields are [{}]",
                allowed.join(", ")
            )));
        }
    }
    Ok(())
}

fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "boolean",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

/// Schema `text`: `minLength 1` and contains at least one non-space.
fn has_visible(s: &str) -> bool {
    !s.is_empty() && s.chars().any(|c| !c.is_whitespace())
}

fn text_of<'a>(v: &'a Value, field: &str) -> Result<&'a str> {
    match v {
        Value::String(s) if has_visible(s) => Ok(s.as_str()),
        Value::String(_) => Err(Error::invalid(format!(
            "{field} must be a non-empty text value, not whitespace only"
        ))),
        other => Err(Error::invalid(format!(
            "{field} must be a string (got {})",
            type_name(other)
        ))),
    }
}

/// Schema `id`: `^[A-Za-z0-9_-]+$`.
fn is_valid_id(s: &str) -> bool {
    !s.is_empty()
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

fn id_of<'a>(v: &'a Value, field: &str) -> Result<&'a str> {
    match v {
        Value::String(s) if is_valid_id(s) => Ok(s.as_str()),
        other => Err(Error::invalid(format!(
            "{field} must be a string matching [A-Za-z0-9_-]+ (got {})",
            type_name(other)
        ))),
    }
}

/// Schema `idOrNull`: a bare id sets the binding, `null` clears it.
fn optional_binding_id(v: &Value, field: &str) -> Result<Option<String>> {
    if v.is_null() {
        Ok(None)
    } else {
        Ok(Some(id_of(v, field)?.to_string()))
    }
}

fn validate_purpose(s: &str, field: &str) -> Result<()> {
    if PURPOSES.contains(&s) {
        Ok(())
    } else {
        Err(Error::invalid(format!(
            "{field} must be extract or embed, got '{s}'"
        )))
    }
}

fn validate_protocol<'a>(v: &'a Value, field: &str) -> Result<&'a str> {
    match v {
        Value::String(s) if PROTOCOLS.contains(&s.as_str()) => Ok(s.as_str()),
        other => Err(Error::invalid(format!(
            "{field} must be one of openai_chat|anthropic_messages|openai_embeddings|ollama (got {})",
            type_name(other)
        ))),
    }
}

/// Schema `endpoint`: `^https?://[^/?#@\s]+(?:/[^?#\s]*)?$` — scheme, host
/// (optionally with port), and an optional API root path only. Query strings
/// and fragments are impossible, which also keeps secrets out of URLs.
fn validate_endpoint<'a>(v: &'a Value, field: &str) -> Result<&'a str> {
    let s = text_of(v, field)?;
    if endpoint_is_valid(s) {
        Ok(s)
    } else {
        Err(Error::invalid(format!(
            "{field} must be an http(s) endpoint URL containing only scheme, host and an optional path (no credentials, query or fragment)"
        )))
    }
}

fn endpoint_is_valid(s: &str) -> bool {
    let Some(rest) = s
        .strip_prefix("http://")
        .or_else(|| s.strip_prefix("https://"))
    else {
        return false;
    };
    if s.is_empty()
        || s.chars().any(|c| c.is_whitespace())
        || s.contains('?')
        || s.contains('#')
        || rest.is_empty()
    {
        return false;
    }
    let authority = match rest.find('/') {
        Some(i) => &rest[..i],
        None => rest,
    };
    !authority.is_empty() && !authority.contains('@')
}

/// Schema `credential`: `null`, a system-store reference `{"backend":
/// "system","id":"cred_…"}`, or an environment-variable name reference
/// `{"backend":"env","name":"VAR"}`. Raw key material is never a valid shape
/// here — model keys use the separate `api_key` field.
fn validate_credential_ref(v: &Value, field: &str) -> Result<()> {
    let obj = match v {
        Value::Null => return Ok(()),
        Value::Object(o) => o,
        other => {
            return Err(Error::invalid(format!(
                "{field} must be null or a system/env credential reference (got {}); for model keys use the api_key field",
                type_name(other)
            )));
        }
    };
    let backend = match obj.get("backend") {
        Some(Value::String(s)) => s.as_str(),
        _ => {
            return Err(Error::invalid(format!(
                "{field} requires backend system or env"
            )));
        }
    };
    match backend {
        "system" => {
            only_fields(obj, &["backend", "id"], field)?;
            match obj.get("id") {
                Some(Value::String(s)) if s.strip_prefix("cred_").is_some_and(is_valid_id) => {
                    Ok(())
                }
                _ => Err(Error::invalid(format!(
                    "{field}.id must match cred_[A-Za-z0-9_-]+"
                ))),
            }
        }
        "env" => {
            only_fields(obj, &["backend", "name"], field)?;
            match obj.get("name") {
                Some(Value::String(s))
                    if !s.is_empty()
                        && s.chars()
                            .next()
                            .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
                        && s.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') =>
                {
                    Ok(())
                }
                _ => Err(Error::invalid(format!(
                    "{field}.name must be an environment variable name"
                ))),
            }
        }
        other => Err(Error::invalid(format!(
            "{field}.backend '{other}' is not supported; only system and env references are storable (for model keys use the api_key field)"
        ))),
    }
}

/// Short stable tag of the instance root (FNV-1a over the path bytes), mixed
/// into generated model ids so ids from different data roots never collide.
fn instance_tag(root: &Path) -> String {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in root.as_os_str().as_encoded_bytes() {
        hash ^= *byte as u64;
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("{:04x}", (hash & 0xffff) as u16)
}
