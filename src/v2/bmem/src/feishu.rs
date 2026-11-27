use crate::error::{Error, Result};
use crate::integrations::executable;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};
use url::Url;

const MAX_OUTPUT: u64 = 512 * 1024;

#[derive(Default)]
pub struct Flow {
    stage: &'static str,
    busy: bool,
    url: Option<String>,
    qr: Option<Vec<u8>>,
    result: Value,
}

impl Flow {
    pub fn begin(&mut self) -> bool {
        if self.busy {
            return false;
        }
        *self = Self {
            stage: "checking",
            busy: true,
            ..Self::default()
        };
        true
    }

    pub fn status(&self) -> Value {
        json!({"available":cli().is_some(),"busy":self.busy,"stage":self.stage,
            "url":self.url,"qr_ready":self.qr.is_some(),"result":self.result})
    }

    pub fn qr(&self) -> Option<Vec<u8>> {
        self.qr.clone()
    }

    fn progress(&mut self, stage: &'static str) {
        self.stage = stage;
        self.url = None;
        self.qr = None;
    }

    pub fn finish(&mut self, result: Result<Value>) {
        self.busy = false;
        match result {
            Ok(value) => {
                self.progress("connected");
                self.result = value;
            }
            Err(error) => {
                self.progress("failed");
                self.result = json!({"message":error.message});
            }
        }
    }
}

pub fn cli() -> Option<PathBuf> {
    executable("lark-cli").or_else(|| executable("feishu-cli"))
}

pub struct Account {
    pub label: String,
    pub key: String,
    pub credential_state: &'static str,
}

pub fn authorize(flow: &Mutex<Flow>, stop: impl Fn() -> bool) -> Result<Account> {
    let bin =
        cli().ok_or_else(|| Error::missing("请先安装官方 CLI：npm install -g @larksuite/cli"))?;
    let mut profile = profile(&bin, &stop)?;
    if profile.is_none() {
        state(flow)?.progress("configuring");
        let name = format!(
            "bmem-feishu-{}",
            &uuid::Uuid::new_v4().simple().to_string()[..8]
        );
        run(
            &bin,
            &[
                "config", "init", "--new", "--brand", "feishu", "--name", &name,
            ],
            None,
            Duration::from_secs(600),
            &stop,
            |output| {
                if let Some(url) = output.split_whitespace().find_map(verification_url) {
                    expose(flow, &bin, url, &stop);
                }
            },
        )?;
        profile = Some(name);
    }
    let profile = profile.unwrap_or_default();
    state(flow)?.progress("authorizing");
    let reply = json_output(run(
        &bin,
        &[
            "auth",
            "login",
            "--scope",
            "offline_access",
            "--no-wait",
            "--json",
            "--profile",
            &profile,
        ],
        None,
        Duration::from_secs(30),
        &stop,
        |_| {},
    )?)?;
    let url = reply["verification_url"]
        .as_str()
        .and_then(verification_url)
        .ok_or_else(|| Error::unavailable("飞书 CLI 未返回可用的网页授权链接"))?;
    let code = reply["device_code"]
        .as_str()
        .ok_or_else(|| Error::unavailable("飞书 CLI 未返回设备授权码"))?;
    expose(flow, &bin, url, &stop);
    let seconds = reply["expires_in"].as_u64().unwrap_or(600).clamp(1, 900);
    run(
        &bin,
        &[
            "auth",
            "login",
            "--device-code",
            code,
            "--json",
            "--profile",
            &profile,
        ],
        None,
        Duration::from_secs(seconds),
        &stop,
        |_| {},
    )?;

    state(flow)?.progress("checking");
    let reply = json_output(run(
        &bin,
        &["auth", "status", "--json", "--profile", &profile],
        None,
        Duration::from_secs(30),
        &stop,
        |_| {},
    )?)?;
    authorized_account(&reply)
        .ok_or_else(|| Error::unavailable("飞书授权完成后未发现可用的用户身份，请重试"))
}

pub fn authorized_account(reply: &Value) -> Option<Account> {
    let user = &reply["identities"]["user"];
    if reply["brand"] != "feishu" || user["available"] != true {
        return None;
    }
    let credential_state = match user["status"].as_str()? {
        "ready" => "ready",
        "needs_refresh" => "needs_refresh",
        _ => return None,
    };
    let open_id = user["openId"].as_str().filter(|id| !id.is_empty())?;
    let app_id = reply["appId"].as_str().filter(|id| !id.is_empty())?;
    let fingerprint = format!(
        "{:x}",
        Sha256::digest(json!(["feishu", app_id, open_id]).to_string())
    );
    Some(Account {
        label: user["userName"].as_str().unwrap_or("飞书用户").to_owned(),
        key: format!("feishu-{}", &fingerprint[..20]),
        credential_state,
    })
}

fn profile(bin: &Path, stop: &impl Fn() -> bool) -> Result<Option<String>> {
    let data = json_output(run(
        bin,
        &["profile", "list"],
        None,
        Duration::from_secs(15),
        stop,
        |_| {},
    )?)?;
    let profiles = data
        .as_array()
        .ok_or_else(|| Error::unavailable("飞书 CLI 配置列表无效"))?;
    let mut feishu = profiles.iter().filter(|entry| entry["brand"] == "feishu");
    let preferred = feishu
        .clone()
        .find(|entry| entry["active"] == true || entry["current"] == true)
        .or_else(|| feishu.next());
    Ok(preferred
        .and_then(|entry| entry["name"].as_str())
        .map(str::to_owned))
}

fn state(flow: &Mutex<Flow>) -> Result<std::sync::MutexGuard<'_, Flow>> {
    flow.lock()
        .map_err(|_| Error::internal("Feishu flow lock poisoned"))
}

fn verification_url(text: &str) -> Option<&str> {
    let url = Url::parse(text).ok()?;
    let host = url.host_str()?;
    (url.scheme() == "https"
        && ["feishu.cn", "larksuite.com"].iter().any(|domain| {
            host == *domain
                || host
                    .strip_suffix(domain)
                    .is_some_and(|prefix| prefix.ends_with('.'))
        })
        && url.username().is_empty()
        && url.password().is_none())
    .then_some(text)
}

fn expose(flow: &Mutex<Flow>, bin: &Path, url: &str, stop: &impl Fn() -> bool) {
    let Ok(mut current) = state(flow) else { return };
    if current.url.as_deref() == Some(url) {
        return;
    }
    current.url = Some(url.to_owned());
    current.qr = None;
    drop(current);

    if let Ok(tmp) = tempfile::tempdir()
        && run(
            bin,
            &["auth", "qrcode", url, "--output", "qr.png"],
            Some(tmp.path()),
            Duration::from_secs(10),
            stop,
            |_| {},
        )
        .is_ok()
        && let Ok(bytes) = fs::read(tmp.path().join("qr.png"))
        && bytes.starts_with(b"\x89PNG\r\n\x1a\n")
        && bytes.len() < MAX_OUTPUT as usize
        && let Ok(mut current) = state(flow)
    {
        current.qr = Some(bytes);
    }
}

pub(crate) fn json_output(bytes: Vec<u8>) -> Result<Value> {
    serde_json::from_slice(&bytes).map_err(|_| Error::unavailable("飞书 CLI 返回了无法读取的结果"))
}

// The device-code process may block while the user authorizes. Keep it outside the Web request.
pub(crate) fn run(
    bin: &Path,
    args: &[&str],
    cwd: Option<&Path>,
    timeout: Duration,
    stop: &impl Fn() -> bool,
    observe: impl Fn(&str),
) -> Result<Vec<u8>> {
    let tmp = tempfile::tempdir()?;
    let out = tmp.path().join("stdout");
    let err = tmp.path().join("stderr");
    let mut command = Command::new(bin);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(fs::File::create(&out)?)
        .stderr(fs::File::create(&err)?);
    command
        .env_remove("OPENCLAW_HOME")
        .env_remove("HERMES_HOME")
        .env("LARKSUITE_CLI_NO_UPDATE_NOTIFIER", "1")
        .env("LARKSUITE_CLI_NO_SKILLS_NOTIFIER", "1");
    if let Some(dir) = cwd {
        command.current_dir(dir);
    }
    let mut child = command
        .spawn()
        .map_err(|_| Error::unavailable("无法启动飞书 CLI"))?;
    let deadline = Instant::now() + timeout;
    loop {
        if let Ok(text) = fs::read_to_string(&err) {
            observe(&text);
        }
        if let Some(status) = child.try_wait()? {
            if !status.success() {
                if let Ok(text) = fs::read_to_string(&err) {
                    observe(&text);
                }
                return Err(Error::unavailable(
                    "飞书 CLI 未完成操作，请确认客户端配置及授权状态后重试",
                ));
            }
            let mut data = Vec::new();
            fs::File::open(&out)?
                .take(MAX_OUTPUT)
                .read_to_end(&mut data)?;
            return Ok(data);
        }
        if stop() || Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(Error::unavailable("飞书授权已结束或超时，请重新发起"));
        }
        thread::sleep(Duration::from_millis(180));
    }
}
