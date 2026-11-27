use crate::error::{Error, Result};
use crate::{context, ingestion, platform::Instance};
use rusqlite::{Connection, OptionalExtension};
use serde_json::{Map, Value, json};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::{env, fs};

const HOSTS: [(&str, &str); 3] = [
    ("codex", "codex"),
    ("claude-code", "claude"),
    ("dsh", "dsh"),
];

const MCP_NAME: &str = "bmem-v2";
const HOOKS: [&str; 3] = ["SessionStart", "UserPromptSubmit", "PostToolUse"];
const DSH_BEGIN: &str = "# BEGIN bmem-v2";
const DSH_END: &str = "# END bmem-v2";

pub fn migrate(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS integration_hosts (
            host TEXT PRIMARY KEY,
            client_id TEXT NOT NULL REFERENCES context_clients(id),
            source_id TEXT NOT NULL REFERENCES ing_sources(source_id)
         );
         CREATE TABLE IF NOT EXISTS integration_feishu_accounts (
            source_id TEXT PRIMARY KEY REFERENCES ing_sources(source_id),
            account_name TEXT NOT NULL,
            authorized_at TEXT NOT NULL,
            credential_state TEXT NOT NULL DEFAULT 'ready'
         );",
    )?;
    let has_state: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('integration_feishu_accounts') WHERE name='credential_state')",
        [],
        |row| row.get(0),
    )?;
    if !has_state {
        conn.execute_batch(
            "ALTER TABLE integration_feishu_accounts ADD COLUMN credential_state TEXT NOT NULL DEFAULT 'ready';",
        )?;
    }
    Ok(())
}

pub fn migrate_legacy_hosts(instance: &Instance, db: &Connection) -> Result<()> {
    for (host, _) in HOSTS {
        if binding(db, host)?.is_some() {
            continue;
        }
        let Ok(saved) = read_json(&manifest(instance, host)) else {
            continue;
        };
        if saved["state"] == "configured"
            && let (Some(client), Some(source)) =
                (saved["client_id"].as_str(), saved["source_id"].as_str())
        {
            db.execute(
                "INSERT OR IGNORE INTO integration_hosts(host,client_id,source_id)
                 SELECT c.host,c.id,s.source_id FROM context_clients c, ing_sources s
                 WHERE c.id=?1 AND s.source_id=?2 AND c.host=?3
                   AND s.kind=c.host AND s.account='local'",
                rusqlite::params![client, source, host],
            )?;
        }
    }
    Ok(())
}

pub fn executable(name: &str) -> Option<PathBuf> {
    let mut names = vec![name.to_owned()];
    if cfg!(windows) {
        names.push(format!("{name}.exe"));
    }
    env::var_os("PATH")
        .into_iter()
        .flat_map(|path| env::split_paths(&path).collect::<Vec<_>>())
        .flat_map(|dir| names.iter().map(move |name| dir.join(name)))
        .find(|path| path.is_file())
}

fn host_root(host: &str) -> Result<(PathBuf, PathBuf)> {
    if let Some(root) = env::var_os("BMEM_CONFIG_ROOT") {
        let root = PathBuf::from(root);
        let dir = root.join(format!(
            ".{}",
            if host == "claude-code" {
                "claude"
            } else {
                host
            }
        ));
        return Ok((root, dir));
    }
    let root = dirs::home_dir().ok_or_else(|| Error::unavailable("user home is unavailable"))?;
    let (variable, fallback) = match host {
        "codex" => ("CODEX_HOME", ".codex"),
        "claude-code" => ("CLAUDE_CONFIG_DIR", ".claude"),
        "dsh" => ("DSH_HOME", ".dsh"),
        _ => return Err(Error::invalid("unknown local integration")),
    };
    if let Some(value) = env::var_os(variable) {
        let dir = PathBuf::from(value);
        let json_root = if host == "claude-code" {
            dir.clone()
        } else {
            root
        };
        return Ok((json_root, dir));
    }
    let dir = root.join(fallback);
    Ok((root, dir))
}

fn manifest(instance: &Instance, host: &str) -> PathBuf {
    instance
        .root
        .join("run/integrations")
        .join(format!("{host}.json"))
}

fn binding(db: &Connection, host: &str) -> Result<Option<(String, String)>> {
    Ok(db
        .query_row(
            "SELECT client_id, source_id FROM integration_hosts WHERE host=?1",
            [host],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?)
}

fn grants(db: &Connection, source: &str, client: &str) -> Result<(bool, bool)> {
    Ok(db.query_row(
        "SELECT EXISTS(SELECT 1 FROM ing_grants
                       WHERE source_id=?1 AND purpose='capture' AND active=1),
                EXISTS(SELECT 1 FROM context_clients WHERE id=?2 AND shared=1)",
        [source, client],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )?)
}

pub fn status(conn: &Connection) -> Result<Value> {
    let mut hosts = Vec::new();
    for (host, bin) in HOSTS {
        let installed = binding(conn, host)?;
        let (client, source, capture, shared) = if let Some((client, source)) = installed.as_ref() {
            let (capture, shared) = grants(conn, source, client)?;
            (Some(client), Some(source), capture, shared)
        } else {
            (None, None, false, false)
        };
        hosts.push(json!({"host":host,"available":executable(bin).is_some(),
            "configured":installed.is_some(),"source_id":source,"client_id":client,
            "capture_enabled":capture,"share_statements":shared,"shared_statements":shared}));
    }
    Ok(json!({"hosts":hosts,"source_activity":compute_source_activity(conn)?}))
}

fn compute_source_activity(conn: &Connection) -> Result<Vec<Value>> {
    let cutoff = (chrono::Utc::now() - chrono::Duration::days(7)).to_rfc3339();
    let mut stmt = conn.prepare(
        "SELECT s.source_id,
                (SELECT count(*) FROM ing_event_versions e
                 WHERE e.source_id=s.source_id AND e.received_at>=?1),
                (SELECT max(e.received_at) FROM ing_event_versions e WHERE e.source_id=s.source_id),
                s.kind
         FROM ing_sources s ORDER BY s.source_id",
    )?;
    let rows = stmt.query_map([cutoff], |r| {
        Ok(json!({
            "source_id": r.get::<_, String>(0)?,
            "event_count": r.get::<_, i64>(1)?,
            "last_received_at": r.get::<_, Option<String>>(2)?,
            "type": r.get::<_, String>(3)?,
        }))
    })?;
    Ok(rows.collect::<std::result::Result<_, _>>()?)
}

fn install_flag(body: &Value, key: &str) -> Result<Option<bool>> {
    body.get(key)
        .map(|value| {
            value
                .as_bool()
                .ok_or_else(|| Error::invalid(format!("{key} must be boolean")))
        })
        .transpose()
}

pub fn install(
    instance: &Instance,
    db: &mut Connection,
    host: &str,
    body: &Value,
) -> Result<Value> {
    let bin = HOSTS
        .iter()
        .find(|(name, _)| *name == host)
        .map(|(_, bin)| *bin)
        .ok_or_else(|| Error::invalid("unsupported local integration"))?;
    if executable(bin).is_none() {
        return Err(Error::missing(format!(
            "未检测到 {bin}。安装后可重新添加集成。"
        )));
    }
    let capture = install_flag(body, "capture")?;
    let share = install_flag(body, "share_statements")?;
    let (root, dir) = host_root(host)?;
    let location = manifest(instance, host);
    let mut saved = match binding(db, host)? {
        Some((id, source_id)) => json!({"state":"configured","client_id":id,"source_id":source_id}),
        None => read_json(&location)?,
    };
    let id = if let Some(id) = saved["client_id"].as_str() {
        id.to_owned()
    } else {
        let mut record = context::register(
            db,
            &uuid::Uuid::new_v4().to_string(),
            json!({
                "host":host,"destination":format!("local-web-{host}"),"capabilities":{"load_context":true}
            }),
        )?;
        record["host"] = json!(host);
        store_client(instance, &record)?;
        let id = record["client_id"]
            .as_str()
            .ok_or_else(|| Error::internal("missing client ID"))?
            .to_owned();
        saved = json!({"state":"adding","client_id":id});
        atomic(&location, &serde_json::to_vec(&saved)?, false)?;
        id
    };
    let source = ingestion::register(
        db,
        json!({"type":host,"account":"local","actors":["owner",id]}),
    )?;
    let source_id = source["source_id"]
        .as_str()
        .ok_or_else(|| Error::internal("missing source ID"))?;
    let bin = env::current_exe()?;
    let binary = path_text(&bin)?;
    let home = path_text(&instance.root)?;
    let mcp = ["--home", home, "mcp", "serve", "--client", &id];
    let hook = [
        "--json", "--home", home, "hook", host, "--client", &id, "--source", source_id,
    ];

    match host {
        "codex" => install_codex(&dir, binary, &mcp, &hook)?,
        "claude-code" => install_claude(&root, &dir, binary, &mcp, &hook)?,
        "dsh" => install_dsh(instance, &dir, binary, &mcp, &hook)?,
        _ => unreachable!(),
    }

    let tx = db.transaction()?;
    if let Some(capture) = capture {
        ingestion::set_capture(&tx, source_id, capture)?;
    }
    if let Some(share) = share {
        context::set_shared(&tx, &id, share)?;
    }
    let (capture, share) = grants(&tx, source_id, &id)?;
    saved = json!({"state":"configured","host":host,"client_id":id,"source_id":source_id,
        "capture_enabled":capture,"share_statements":share,"shared_statements":share});
    tx.execute(
        "INSERT INTO integration_hosts(host,client_id,source_id) VALUES(?1,?2,?3)
         ON CONFLICT(host) DO UPDATE SET client_id=excluded.client_id,source_id=excluded.source_id",
        rusqlite::params![host, id, source_id],
    )?;
    atomic(&location, &serde_json::to_vec(&saved)?, false)?;
    tx.commit()?;
    Ok(saved)
}

pub fn store_client(instance: &Instance, record: &Value) -> Result<()> {
    let id = record["client_id"]
        .as_str()
        .ok_or_else(|| Error::internal("missing client ID"))?;
    let path = instance.root.join("run/clients").join(format!("{id}.json"));
    atomic(&path, &serde_json::to_vec(record)?, false)
}

fn path_text(path: &Path) -> Result<&str> {
    path.to_str()
        .ok_or_else(|| Error::invalid("integration path is not UTF-8"))
}

fn read_json(path: &Path) -> Result<Value> {
    match fs::read(path) {
        Ok(data) => {
            let value: Value = serde_json::from_slice(&data)
                .map_err(|_| Error::conflict(format!("invalid JSON config: {}", path.display())))?;
            if !value.is_object() {
                return Err(Error::conflict(format!(
                    "expected a JSON object: {}",
                    path.display()
                )));
            }
            Ok(value)
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(json!({})),
        Err(e) => Err(e.into()),
    }
}

fn dictionary<'a>(
    parent: &'a mut Map<String, Value>,
    name: &str,
) -> Result<&'a mut Map<String, Value>> {
    parent
        .entry(name)
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .ok_or_else(|| Error::conflict(format!("{name} 必须是配置对象")))
}

fn atomic(path: &Path, data: &[u8], backup: bool) -> Result<()> {
    let dir = path
        .parent()
        .ok_or_else(|| Error::internal("config path has no parent"))?;
    fs::create_dir_all(dir)?;
    if let Ok(old) = fs::read(path) {
        if old == data {
            return Ok(());
        }
        if backup {
            let mut file = path.as_os_str().to_owned();
            file.push(".bmem-v2.previous");
            let previous = PathBuf::from(file);
            if !previous.exists() {
                atomic(&previous, &old, false)?;
            }
        }
    }
    let mut draft = tempfile::NamedTempFile::new_in(dir)?;
    draft.write_all(data)?;
    draft.as_file().sync_all()?;
    draft.persist(path).map_err(|e| e.error)?;
    Ok(())
}

#[cfg(unix)]
fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

#[cfg(windows)]
fn shell_quote(value: &str) -> String {
    format!("\"{}\"", value.replace('"', "\\\""))
}

fn hook_command(bin: &str, args: &[&str]) -> String {
    std::iter::once(bin)
        .chain(args.iter().copied())
        .map(shell_quote)
        .collect::<Vec<_>>()
        .join(" ")
}

fn write_json(path: &Path, value: &Value) -> Result<()> {
    let mut bytes = serde_json::to_vec_pretty(value)?;
    bytes.push(b'\n');
    atomic(path, &bytes, true)
}

fn install_claude(root: &Path, dir: &Path, bin: &str, mcp: &[&str], hook: &[&str]) -> Result<()> {
    let hooks_path = dir.join("settings.json");
    let mcp_path = root.join(".claude.json");
    let mut config = read_json(&hooks_path)?;
    let mut integrations = read_json(&mcp_path)?;
    let hooks = dictionary(config.as_object_mut().unwrap(), "hooks")?;
    let command = hook_command(bin, hook);
    for name in HOOKS {
        let groups = hooks
            .entry(name)
            .or_insert_with(|| json!([]))
            .as_array_mut()
            .ok_or_else(|| Error::conflict(format!("{name} 必须是 hook 列表")))?;
        let entry = json!({"hooks":[{"type":"command","command":command,"timeout":12}]});
        if !groups.contains(&entry) {
            groups.push(entry);
        }
    }
    dictionary(integrations.as_object_mut().unwrap(), "mcpServers")?.insert(
        MCP_NAME.into(),
        json!({"type":"stdio","command":bin,"args":mcp}),
    );
    write_json(&hooks_path, &config)?;
    write_json(&mcp_path, &integrations)
}

fn install_codex(dir: &Path, bin: &str, mcp: &[&str], hook: &[&str]) -> Result<()> {
    use toml_edit::{Array, DocumentMut, Item, Table, value};
    let path = dir.join("config.toml");
    let mut cfg = match fs::read_to_string(&path) {
        Ok(text) => text
            .parse::<DocumentMut>()
            .map_err(|_| Error::conflict("Codex config.toml 无法解析"))?,
        Err(e) if e.kind() == io::ErrorKind::NotFound => DocumentMut::new(),
        Err(e) => return Err(e.into()),
    };
    let servers = cfg
        .entry("mcp_servers")
        .or_insert(Item::Table(Table::new()))
        .as_table_like_mut()
        .ok_or_else(|| Error::conflict("Codex mcp_servers 必须是配置表"))?;
    let mut server = Table::new();
    server.insert("command", value(bin));
    server.insert("args", value(mcp.iter().copied().collect::<Array>()));
    servers.insert(MCP_NAME, Item::Table(server));

    // Codex merges the standalone hooks.json with hooks in config.toml.
    let hooks_path = dir.join("hooks.json");
    let mut config = read_json(&hooks_path)?;
    let hooks = dictionary(config.as_object_mut().unwrap(), "hooks")?;
    let command = hook_command(bin, hook);
    for name in HOOKS {
        let groups = hooks
            .entry(name)
            .or_insert_with(|| json!([]))
            .as_array_mut()
            .ok_or_else(|| Error::conflict(format!("{name} 必须是 hook 列表")))?;
        let entry = json!({"hooks":[{"type":"command","command":command,"timeout":12}]});
        if !groups.contains(&entry) {
            groups.push(entry);
        }
    }
    atomic(&path, cfg.to_string().as_bytes(), true)?;
    write_json(&hooks_path, &config)
}

fn install_dsh(
    instance: &Instance,
    dir: &Path,
    bin: &str,
    mcp: &[&str],
    hook: &[&str],
) -> Result<()> {
    let plugin = instance.root.join("run/integrations/dsh-capture.mjs");
    atomic(&plugin, include_bytes!("dsh_capture.mjs"), false)?;
    let plugin_url = url::Url::from_file_path(&plugin)
        .map_err(|_| Error::invalid("cannot format DSH plugin URL"))?;
    let path = dir.join("cordis.patch.yml");
    let current = match fs::read_to_string(&path) {
        Ok(text) => text,
        Err(e) if e.kind() == io::ErrorKind::NotFound => String::new(),
        Err(e) => return Err(e.into()),
    };
    let block = format!(
        "{DSH_BEGIN}\n- insert:\n    - id: bmem-v2-capture\n      name: {}\n      config:\n        command: {}\n        args: {}\n    - id: bmem-v2-mcp\n      name: '@deepseek-ai/dsh-mcp-client'\n      config:\n        serverName: bmem_v2\n        transport: stdio\n        command: {}\n        args: {}\n        failOnStartupError: false\n{DSH_END}",
        serde_json::to_string(plugin_url.as_str())?,
        serde_json::to_string(bin)?,
        serde_json::to_string(hook)?,
        serde_json::to_string(bin)?,
        serde_json::to_string(mcp)?
    );
    let next = if let Some(begin) = current.find(DSH_BEGIN) {
        let end = current[begin..]
            .find(DSH_END)
            .map(|offset| begin + offset + DSH_END.len())
            .ok_or_else(|| Error::conflict("DSH 的 bmem-v2 配置块不完整"))?;
        format!("{}{}{}", &current[..begin], block, &current[end..])
    } else {
        if current.lines().any(|line| {
            let line = line.trim();
            !line.is_empty() && !line.starts_with('#')
        }) && !current
            .lines()
            .map(str::trim)
            .find(|line| !line.is_empty() && !line.starts_with('#'))
            .is_some_and(|line| line.starts_with("- "))
        {
            return Err(Error::conflict("DSH cordis.patch.yml 必须是 YAML 补丁列表"));
        }
        format!(
            "{}{block}\n",
            if current.is_empty() || current.ends_with('\n') {
                current
            } else {
                current + "\n"
            }
        )
    };
    atomic(&path, next.as_bytes(), true)
}

pub fn record_feishu(
    conn: &Connection,
    source_id: &str,
    account: Option<&crate::feishu::Account>,
) -> Result<()> {
    let label = account.map(|account| account.label.as_str());
    let credential_state = account.map_or("reauthorization_required", |a| a.credential_state);
    conn.execute(
        "INSERT INTO integration_feishu_accounts(source_id,account_name,authorized_at,credential_state)
         SELECT source_id,coalesce(?2,'飞书账号'),datetime('now'),?3 FROM ing_sources
         WHERE source_id=?1 AND kind='feishu' AND account LIKE 'feishu-%'
         ON CONFLICT(source_id) DO UPDATE SET
             account_name=coalesce(?2,account_name),credential_state=excluded.credential_state
         WHERE credential_state<>excluded.credential_state OR account_name<>coalesce(?2,account_name)",
        rusqlite::params![source_id, label, credential_state],
    )?;
    Ok(())
}

pub fn feishu_status(conn: &Connection) -> Result<Value> {
    let mut stmt = conn.prepare(
        "SELECT s.source_id,s.account,f.account_name,f.credential_state
         FROM ing_sources s LEFT JOIN integration_feishu_accounts f ON s.source_id=f.source_id
         WHERE s.kind='feishu' AND s.account LIKE 'feishu-%'
         ORDER BY f.authorized_at DESC,s.created_at DESC,s.source_id",
    )?;
    let accounts = stmt
        .query_map([], |row| {
            let credential_state: Option<String> = row.get(3)?;
            let authorization = match credential_state.as_deref() {
                Some("ready" | "needs_refresh") => "connected",
                Some("reauthorization_required") => "reauthorization_required",
                _ => "registered_unverified",
            };
            Ok(json!({
                "source_id":row.get::<_, String>(0)?,"account":row.get::<_, String>(1)?,
                "account_name":row.get::<_, Option<String>>(2)?,
                "authorization":authorization,
                "credential_state":credential_state,
            }))
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let registered = !accounts.is_empty();
    let authorization = if accounts
        .iter()
        .any(|account| account["authorization"] == "connected")
    {
        "connected"
    } else if accounts
        .iter()
        .any(|account| account["authorization"] == "reauthorization_required")
    {
        "reauthorization_required"
    } else if registered {
        "registered_unverified"
    } else {
        "unregistered"
    };
    Ok(json!({"registered":registered,"accounts":accounts,"authorization":authorization}))
}
