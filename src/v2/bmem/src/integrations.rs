use crate::error::{Error, Result};
use crate::{context, ingestion, platform::Instance};
use rusqlite::Connection;
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

pub fn status(instance: &Instance) -> Value {
    let hosts = HOSTS
        .into_iter()
        .map(|(host, bin)| {
            let saved = read_json(&manifest(instance, host)).unwrap_or_default();
            let installed = saved["state"] == "configured";
            json!({"host":host,"available":executable(bin).is_some(),"configured":installed,
            "source_id":saved["source_id"],"client_id":saved["client_id"]})
        })
        .collect::<Vec<_>>();
    json!({"hosts":hosts})
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
    let (root, dir) = host_root(host)?;
    let location = manifest(instance, host);
    let mut saved = read_json(&location)?;
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
    if body["capture"] == true {
        ingestion::set_capture(db, source_id, true)?;
    }
    if body["share_statements"] == true {
        context::set_shared(db, &id, true)?;
    }
    saved["state"] = json!("configured");
    saved["host"] = json!(host);
    saved["source_id"] = json!(source_id);
    atomic(&location, &serde_json::to_vec(&saved)?, false)?;
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
