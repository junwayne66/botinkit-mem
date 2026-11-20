use crate::error::{Error, Result};
use crate::platform::{self, Instance};
use reqwest::Method;
use serde_json::Value;
use std::io::Read;
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Clone)]
pub struct Client {
    http: reqwest::blocking::Client,
    base: String,
    instance_id: String,
    token: String,
}

impl Client {
    pub fn bound(instance: &Instance, token: String) -> Result<Self> {
        let address = platform::discover(instance)?;
        if !address.ip().is_loopback() {
            return Err(Error::denied("service address must be loopback"));
        }
        Ok(Self {
            http: reqwest::blocking::Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .connect_timeout(Duration::from_millis(700))
                .timeout(Duration::from_secs(90))
                .build()
                .map_err(|_| Error::internal("could not create HTTP client"))?,
            base: format!("http://{address}"),
            instance_id: instance.id.clone(),
            token,
        })
    }

    pub fn owner(instance: &Instance) -> Result<Self> {
        Self::bound(instance, instance.owner_token.clone())
    }

    pub fn request(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
        key: Option<&str>,
    ) -> Result<Value> {
        let method =
            Method::from_bytes(method.as_bytes()).map_err(|_| Error::invalid("invalid method"))?;
        let mut request = self
            .http
            .request(method, format!("{}{path}", self.base))
            .bearer_auth(&self.token)
            .header("X-Bmem-Instance", &self.instance_id);
        if let Some(body) = body {
            request = request.json(body);
        }
        if let Some(key) = key {
            request = request.header("Idempotency-Key", key);
        }
        let response = request.send().map_err(|error| {
            if error.is_connect() || error.is_timeout() {
                Error::unavailable("local service is unreachable or timed out")
            } else {
                Error::internal("local service request failed")
            }
        })?;
        let status = response.status();
        if response
            .headers()
            .get("x-bmem-instance")
            .and_then(|v| v.to_str().ok())
            != Some(&self.instance_id)
        {
            return Err(Error::conflict(
                "service instance does not match the selected data root",
            ));
        }
        let mut output = Vec::new();
        response.take(8 * 1024 * 1024).read_to_end(&mut output)?;
        let value: Value = serde_json::from_slice(&output).map_err(|_| {
            Error::internal(format!("invalid response from local service ({status})"))
        })?;
        if status.is_success() {
            Ok(value)
        } else {
            Err(Error::new(
                status.as_u16(),
                value["code"].as_str().unwrap_or("internal_error"),
                value["message"]
                    .as_str()
                    .unwrap_or("local service rejected the request"),
            ))
        }
    }
}

pub fn running(instance: &Instance) -> Result<Client> {
    let client = Client::owner(instance)?;
    client.request("GET", "/v2/health", None, None)?;
    Ok(client)
}

pub fn start(instance: &Instance) -> Result<Client> {
    if let Ok(client) = running(instance) {
        return Ok(client);
    }
    let exe = std::env::current_exe()?;
    let run_dir = instance.root.join("run");
    std::fs::create_dir_all(&run_dir)?;
    let log = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(run_dir.join("service.stderr.log"))?;
    let mut command = Command::new(exe);
    command
        .arg("--home")
        .arg(&instance.root)
        .args(["service", "run"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(log);
    #[cfg(unix)]
    {
        // The service outlives the terminal command that opened the workbench.
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let mut child = command.spawn()?;
    let deadline = Instant::now() + Duration::from_secs(7);
    loop {
        if let Ok(client) = running(instance) {
            return Ok(client);
        }
        if let Some(status) = child.try_wait()? {
            // A concurrent starter may hold the lock and still be opening the database.
            if !status.success() && Instant::now() + Duration::from_secs(5) < deadline {
                thread::sleep(Duration::from_millis(90));
            }
        }
        if Instant::now() >= deadline {
            return Err(Error::unavailable(
                "local service did not start; run bmem doctor and check run/service.stderr.log",
            ));
        }
        thread::sleep(Duration::from_millis(60));
    }
}
