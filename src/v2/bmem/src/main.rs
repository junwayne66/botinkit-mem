mod analytics;
mod archive;
mod authority;
mod client;
mod context;
mod error;
mod feishu;
mod history;
mod history_feishu;
mod history_hosts;
mod host;
mod ingestion;
mod integrations;
mod platform;
mod providers;
mod retrieval;
mod server;
mod settings;
mod vector;

use clap::{Parser, Subcommand};
use client::Client;
use error::{Error, Result};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use uuid::Uuid;

#[derive(Parser)]
#[command(
    name = "bmem",
    version,
    about = "Botinkit Memory · Local personal memory service"
)]
struct Cli {
    #[arg(long, global = true)]
    home: Option<PathBuf>,
    #[arg(long, global = true)]
    json: bool,
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    Init {
        #[arg(long)]
        non_interactive: bool,
    },
    Status,
    Doctor,
    Web {
        #[arg(long)]
        no_open: bool,
    },
    Service {
        #[command(subcommand)]
        command: Service,
    },
    Config {
        #[command(subcommand)]
        command: Config,
    },
    Model {
        #[command(subcommand)]
        command: Model,
    },
    Connector {
        #[command(subcommand)]
        command: Connector,
    },
    Client {
        #[command(subcommand)]
        command: Clients,
    },
    Grant {
        #[command(subcommand)]
        command: Grant,
    },
    Declare {
        original: String,
        #[arg(long, default_value = "global")]
        scope: String,
        #[arg(long)]
        project: Option<String>,
        #[arg(long, default_value = "owner")]
        share: String,
        #[arg(long)]
        corrects: Option<String>,
        #[arg(long)]
        corrects_attempt: Option<String>,
        #[arg(long)]
        idempotency_key: Option<String>,
    },
    Statement {
        #[command(subcommand)]
        command: Statement,
    },
    Search {
        query: String,
        #[arg(long)]
        task: Option<String>,
        #[arg(long, value_parser = ["lexical","vector"], default_value = "lexical")]
        mode: String,
    },
    Evidence {
        #[command(subcommand)]
        command: Evidence,
    },
    Task {
        #[command(subcommand)]
        command: Task,
    },
    Delivery {
        #[command(subcommand)]
        command: Inspection,
    },
    Job {
        #[command(subcommand)]
        command: Jobs,
    },
    Operation {
        #[command(subcommand)]
        command: Inspection,
    },
    Backup {
        #[command(subcommand)]
        command: Backup,
    },
    Restore {
        #[command(subcommand)]
        command: Restore,
    },
    Forget {
        #[arg(long, conflicts_with = "statement")]
        source: Option<String>,
        #[arg(long, conflicts_with = "source")]
        statement: Option<String>,
        #[arg(long, conflicts_with = "confirm")]
        preview: bool,
        #[arg(long)]
        confirm: Option<String>,
    },
    Capabilities {
        #[command(subcommand)]
        command: Inspection,
    },
    Mcp {
        #[command(subcommand)]
        command: Mcp,
    },
    Hook {
        host: String,
        #[arg(long)]
        client: String,
        #[arg(long)]
        source: Option<String>,
        #[arg(long)]
        task: Option<String>,
    },
}

#[derive(Subcommand)]
enum Service {
    Run,
    Start,
    Stop,
    Status,
}
#[derive(Subcommand)]
enum Inspection {
    List,
    Show { id: String },
}
#[derive(Subcommand)]
enum Jobs {
    List {
        #[arg(long, default_value_t = 50)]
        limit: u16,
        #[arg(long)]
        before: Option<String>,
        #[arg(long, value_parser = ["pending", "running", "done", "failed", "cancelled"])]
        status: Option<String>,
    },
    Show {
        id: String,
    },
    Candidates {
        id: String,
    },
    Retry {
        id: String,
    },
}
#[derive(Subcommand)]
enum Config {
    Get {
        key: Option<String>,
    },
    Set {
        key: String,
        value: String,
        #[arg(long)]
        expected_revision: Option<u64>,
        #[arg(long)]
        idempotency_key: Option<String>,
    },
}
#[derive(Subcommand)]
enum Model {
    List,
    Add {
        name: String,
        #[arg(long)]
        protocol: String,
        #[arg(long)]
        endpoint: String,
        #[arg(long)]
        model_id: String,
        #[arg(long)]
        credential_env: Option<String>,
    },
    Configure {
        name: String,
        #[arg(long)]
        endpoint: Option<String>,
        #[arg(long)]
        protocol: Option<String>,
        #[arg(long)]
        model_id: Option<String>,
        #[arg(long)]
        credential_env: Option<String>,
    },
    Test {
        name: String,
        #[arg(long = "for", default_value = "extract")]
        purpose: String,
    },
    Use {
        name: String,
        #[arg(long = "for")]
        purpose: String,
    },
    Unbind {
        #[arg(long = "for")]
        purpose: String,
    },
    Remove {
        name: String,
    },
}
#[derive(Subcommand)]
enum Connector {
    List,
    Add {
        connector_type: String,
        #[arg(long)]
        account: String,
        #[arg(long)]
        client: Option<String>,
    },
    Pause {
        source_id: String,
    },
    Resume {
        source_id: String,
    },
    /// Enable periodic historical collection for a registered source
    Sync {
        source_id: String,
        #[arg(long)]
        pause: bool,
    },
}
#[derive(Subcommand)]
enum Clients {
    List,
    Register {
        host: String,
        #[arg(long)]
        destination: String,
    },
}
#[derive(Subcommand)]
enum Grant {
    Set {
        #[arg(long)]
        client: String,
        #[arg(long,conflicts_with="allow", action = clap::ArgAction::Set)]
        share_statements: Option<bool>,
        #[arg(long, requires = "allow")]
        task: Option<String>,
        #[arg(long,requires="task", action = clap::ArgAction::Set)]
        allow: Option<bool>,
        #[arg(long, conflicts_with = "confirm")]
        preview: bool,
        #[arg(long)]
        confirm: Option<String>,
    },
    Source {
        source_id: String,
        #[arg(long)]
        purpose: String,
        #[arg(long)]
        client: Option<String>,
        #[arg(long, action = clap::ArgAction::Set)]
        allow: bool,
        #[arg(long, conflicts_with = "confirm")]
        preview: bool,
        #[arg(long)]
        confirm: Option<String>,
    },
}
#[derive(Subcommand)]
enum Statement {
    List {
        #[arg(long)]
        project: Option<String>,
        #[arg(long)]
        valid_at: Option<String>,
        #[arg(long)]
        known_at: Option<String>,
    },
    History {
        fact_id: String,
    },
}
#[derive(Subcommand)]
enum Evidence {
    Expand { handle: String },
}
#[derive(Subcommand)]
enum Task {
    List,
    Create {
        anchor: String,
        #[arg(long)]
        project: Option<String>,
    },
    Handoff {
        id: String,
    },
}
#[derive(Subcommand)]
enum Mcp {
    Serve {
        #[arg(long)]
        client: String,
    },
}
#[derive(Subcommand)]
enum Backup {
    Create,
}
#[derive(Subcommand)]
enum Restore {
    Preview { backup_id: String },
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let machine = cli.json;
    match run(cli) {
        Ok(Some(value)) => {
            let output = if machine {
                serde_json::to_string(&value)
            } else {
                serde_json::to_string_pretty(&value)
            };
            match output {
                Ok(text) => {
                    if writeln!(io::stdout().lock(), "{text}").is_err() {
                        ExitCode::FAILURE
                    } else {
                        ExitCode::SUCCESS
                    }
                }
                Err(_) => ExitCode::FAILURE,
            }
        }
        Ok(None) => ExitCode::SUCCESS,
        Err(error) => {
            if machine {
                eprintln!("{}", error.value());
            } else {
                eprintln!("bmem: {error}");
            }
            ExitCode::FAILURE
        }
    }
}

fn run(cli: Cli) -> Result<Option<Value>> {
    match cli.command {
        None
        | Some(Command::Status)
        | Some(Command::Service {
            command: Service::Status,
        }) => Ok(Some(status(cli.home.as_deref()))),
        Some(Command::Doctor) => {
            let mut value = platform::diagnostics(cli.home.as_deref());
            value["service"] = status(cli.home.as_deref());
            Ok(Some(value))
        }
        Some(Command::Init { non_interactive }) => {
            let instance = platform::init(cli.home.as_deref())?;
            let lease = match platform::lock_service(&instance) {
                Ok(lease) => Some(lease),
                Err(error) => {
                    client::running(&instance).map_err(|_| error)?;
                    None
                }
            };
            if lease.is_some() {
                server::database(&instance)?;
                settings::Settings::open(&instance.root)?;
            }
            Ok(Some(
                json!({"state":"ready","root":instance.root,"instance_id":instance.id,
                "non_interactive":non_interactive,"next":"bmem web or bmem model add --help"}),
            ))
        }
        Some(Command::Service {
            command: Service::Run,
        }) => {
            let instance = platform::resolve(cli.home.as_deref())?;
            tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()?
                .block_on(server::serve(instance))?;
            Ok(None)
        }
        Some(Command::Service {
            command: Service::Stop,
        }) => {
            let instance = platform::resolve(cli.home.as_deref())?;
            let output = match client::running(&instance) {
                Ok(client) => post(&client, "/v2/service/stop", &json!({}), None)?,
                Err(_) => json!({"state":"stopped","instance_id":instance.id}),
            };
            Ok(Some(output))
        }
        Some(command) => {
            let instance = platform::resolve(cli.home.as_deref())?;
            let client = client::start(&instance)?;
            if matches!(&command, Command::Mcp { .. } | Command::Hook { .. }) {
                return host_command(command, &instance);
            }
            let output = match command {
                Command::Service {
                    command: Service::Start,
                } => get(&client, "/v2/health")?,
                Command::Web { no_open } => {
                    let output = post(&client, "/v2/web/session", &json!({}), None)?;
                    if output["web_build"].as_str() != Some(server::web_build()) {
                        return Err(Error::conflict(
                            "运行中的服务与当前 V2 程序内嵌的工作台版本不同。请使用当前 V2 程序执行 service stop，随后执行 web。",
                        ));
                    }
                    if !no_open {
                        open_browser(
                            output["url"]
                                .as_str()
                                .ok_or_else(|| Error::internal("service did not return web URL"))?,
                        )?;
                    }
                    output
                }
                Command::Config { command } => config(&client, command)?,
                Command::Model { command } => model(&client, command)?,
                Command::Connector { command } => connector(&client, command)?,
                Command::Client { command } => match command {
                    Clients::List => get(&client, "/v2/clients")?,
                    Clients::Register { host, destination } => {
                        register(&instance, &client, &host, &destination)?
                    }
                },
                Command::Grant {
                    command:
                        Grant::Set {
                            client: client_id,
                            share_statements,
                            task,
                            allow,
                            preview,
                            confirm,
                        },
                } => {
                    let payload = if let Some(allowed) = share_statements {
                        json!({"kind":"change_grant","client_id":client_id,"share_statements":allowed})
                    } else if let (Some(task_id), Some(allowed)) = (task, allow) {
                        json!({"kind":"change_grant","client_id":client_id,"task_id":task_id,"allow":allowed})
                    } else {
                        return Err(Error::invalid(
                            "provide --share-statements or --task with --allow",
                        ));
                    };
                    operation(&client, payload, preview, confirm)?
                }
                Command::Grant {
                    command:
                        Grant::Source {
                            source_id,
                            purpose,
                            client: client_id,
                            allow,
                            preview,
                            confirm,
                        },
                } => {
                    let mut payload = json!({"kind":"change_grant","source_id":source_id,"purpose":purpose,"allow":allow});
                    if let Some(client_id) = client_id {
                        payload["client_id"] = json!(client_id);
                    }
                    operation(&client, payload, preview, confirm)?
                }
                Command::Declare {
                    original,
                    scope,
                    project,
                    share,
                    corrects,
                    corrects_attempt,
                    idempotency_key,
                } => {
                    let mut payload =
                        json!({"original":original,"scope":{"kind":scope},"share":share});
                    if let Some(project) = project {
                        payload["scope"]["project"] = json!(project);
                    }
                    if let Some(rev) = corrects {
                        payload["corrects_revision_id"] = json!(rev);
                    }
                    if let Some(attempt) = corrects_attempt {
                        payload["corrects_attempt_id"] = json!(attempt);
                    }
                    post(
                        &client,
                        "/v2/statements",
                        &payload,
                        idempotency_key.as_deref(),
                    )?
                }
                Command::Statement { command } => match command {
                    Statement::List {
                        project,
                        valid_at,
                        known_at,
                    } => {
                        let mut query = url::form_urlencoded::Serializer::new(String::new());
                        for (key, val) in [
                            ("project", project),
                            ("valid_at", valid_at),
                            ("known_at", known_at),
                        ] {
                            if let Some(val) = val {
                                query.append_pair(key, &val);
                            }
                        }
                        get(&client, &format!("/v2/statements?{}", query.finish()))?
                    }
                    Statement::History { fact_id } => {
                        get(&client, &format!("/v2/facts/{}/history", segment(&fact_id)))?
                    }
                },
                Command::Search { query, task, mode } => post(
                    &client,
                    "/v2/search",
                    &json!({"query":query,"task_id":task,"mode":mode}),
                    None,
                )?,
                Command::Evidence {
                    command: Evidence::Expand { handle },
                } => post(
                    &client,
                    "/v2/evidence/expand",
                    &json!({"handle":handle}),
                    None,
                )?,
                Command::Task { command } => match command {
                    Task::List => get(&client, "/v2/tasks")?,
                    Task::Create { anchor, project } => {
                        let mut body = json!({"anchor":anchor});
                        if let Some(project) = project {
                            body["project"] = json!(project);
                        }
                        post(&client, "/v2/tasks", &body, None)?
                    }
                    Task::Handoff { id } => {
                        get(&client, &format!("/v2/tasks/{}/handoff", segment(&id)))?
                    }
                },
                Command::Delivery { command } => {
                    inspect(&client, "deliveries", "deliveries", "delivery_id", command)?
                }
                Command::Operation { command } => {
                    inspect(&client, "operations", "operations", "operation_id", command)?
                }
                Command::Backup {
                    command: Backup::Create,
                } => post(&client, "/v2/operations", &json!({"kind":"backup"}), None)?,
                Command::Restore {
                    command: Restore::Preview { backup_id },
                } => post(
                    &client,
                    "/v2/operations",
                    &json!({"kind":"restore_preview","backup_id":backup_id}),
                    None,
                )?,
                Command::Capabilities { command } => match command {
                    Inspection::List => get(&client, "/v2/capabilities")?,
                    Inspection::Show { id } => {
                        get(&client, &format!("/v2/capabilities/{}", segment(&id)))?
                    }
                },
                Command::Job { command } => match command {
                    Jobs::List {
                        limit,
                        before,
                        status,
                    } => {
                        let mut query =
                            url::form_urlencoded::Serializer::new(format!("limit={limit}"));
                        if let Some(before) = before {
                            query.append_pair("before", &before);
                        }
                        if let Some(status) = status {
                            query.append_pair("status", &status);
                        }
                        get(&client, &format!("/v2/jobs?{}", query.finish()))?
                    }
                    Jobs::Show { id } => get(&client, &format!("/v2/jobs/{}", segment(&id)))?,
                    Jobs::Candidates { id } => {
                        get(&client, &format!("/v2/jobs/{}/candidates", segment(&id)))?
                    }
                    Jobs::Retry { id } => post(
                        &client,
                        &format!("/v2/jobs/{}/retry", segment(&id)),
                        &json!({}),
                        None,
                    )?,
                },
                Command::Forget {
                    source,
                    statement,
                    preview,
                    confirm,
                } => {
                    let payload = if let Some(source_id) = source {
                        json!({"kind":"forget","target":{"source_id":source_id}})
                    } else if let Some(statement_id) = statement {
                        json!({"kind":"forget","target":{"statement_id":statement_id}})
                    } else {
                        return Err(Error::invalid("provide --source or --statement"));
                    };
                    operation(&client, payload, preview, confirm)?
                }
                _ => unreachable!("platform commands handled before dispatch"),
            };
            Ok(Some(output))
        }
    }
}

fn status(root: Option<&Path>) -> Value {
    match platform::resolve(root) {
        Ok(instance) => match client::running(&instance).and_then(|c| get(&c, "/v2/health")) {
            Ok(health) => health,
            Err(_) => json!({"state":"stopped","instance_id":instance.id,"root":instance.root}),
        },
        Err(error) => {
            json!({"state":if error.status == 404 { "uninitialized" } else { "unavailable" },
            "reason":error.message,"next":"bmem doctor or bmem init"})
        }
    }
}

fn get(client: &Client, path: &str) -> Result<Value> {
    client.request("GET", path, None, None)
}

fn post(client: &Client, path: &str, value: &Value, supplied: Option<&str>) -> Result<Value> {
    let generated = Uuid::new_v4().to_string();
    client.request(
        "POST",
        path,
        Some(value),
        Some(supplied.unwrap_or(&generated)),
    )
}

fn segment(id: &str) -> String {
    url::form_urlencoded::byte_serialize(id.as_bytes()).collect()
}

fn patch(
    client: &Client,
    config: &Value,
    revision: Option<u64>,
    patch: Value,
    key: Option<&str>,
) -> Result<Value> {
    let generated = Uuid::new_v4().to_string();
    let expected = revision
        .or_else(|| config["config_revision"].as_u64())
        .ok_or_else(|| Error::internal("config revision unavailable"))?;
    client.request(
        "PATCH",
        "/v2/config",
        Some(&json!({"expected_revision":expected,"patch":patch})),
        Some(key.unwrap_or(&generated)),
    )
}

fn config(client: &Client, command: Config) -> Result<Value> {
    let settings = get(client, "/v2/config")?;
    match command {
        Config::Get { key: None } => Ok(settings),
        Config::Get { key: Some(key) } => {
            let value = key
                .split('.')
                .try_fold(&settings, |value, part| value.get(part))
                .ok_or_else(|| Error::missing("unknown configuration key"))?;
            Ok(json!({"key":key,"value":value,"config_revision":settings["config_revision"]}))
        }
        Config::Set {
            key,
            value,
            expected_revision,
            idempotency_key,
        } => {
            let change = match key.as_str() {
                "profile" => json!({"profile":value}),
                "vector_search.enabled" => {
                    json!({"vector_search":{"enabled":value.parse::<bool>().map_err(|_|Error::invalid("enabled must be true or false"))?}})
                }
                _ => {
                    return Err(Error::invalid(
                        "use a supported settings key or the dedicated model command",
                    ));
                }
            };
            patch(
                client,
                &settings,
                expected_revision,
                change,
                idempotency_key.as_deref(),
            )
        }
    }
}

fn model(client: &Client, command: Model) -> Result<Value> {
    let config = get(client, "/v2/config")?;
    let lookup = |name: &str| -> Result<String> {
        config["models"]
            .as_array()
            .into_iter()
            .flatten()
            .find(|model| model["name"] == name || model["id"] == name)
            .and_then(|model| model["id"].as_str())
            .map(str::to_owned)
            .ok_or_else(|| Error::missing("model not configured"))
    };
    let binding = |purpose: &str, id: Value| {
        if !["extract", "embed"].contains(&purpose) {
            return Err(Error::invalid("purpose must be extract or embed"));
        }
        patch(
            client,
            &config,
            None,
            json!({"bindings":{(purpose):id}}),
            None,
        )
    };
    match command {
        Model::List => Ok(json!({"models":config["models"],"bindings":config["bindings"]})),
        Model::Add {
            name,
            protocol,
            endpoint,
            model_id,
            credential_env,
        } => {
            let mut body = json!({"op":"add","name":name,"protocol":protocol,"endpoint":endpoint,"model_id":model_id});
            if let Some(name) = credential_env {
                body["credential_ref"] = json!({"backend":"env","name":name});
            }
            patch(client, &config, None, json!({"models":body}), None)
        }
        Model::Configure {
            name,
            endpoint,
            protocol,
            model_id,
            credential_env,
        } => {
            let mut changes = json!({});
            for (key, value) in [
                ("endpoint", endpoint),
                ("protocol", protocol),
                ("model_id", model_id),
            ] {
                if let Some(value) = value {
                    changes[key] = json!(value);
                }
            }
            if let Some(name) = credential_env {
                changes["credential_ref"] = json!({"backend":"env","name":name});
            }
            patch(
                client,
                &config,
                None,
                json!({"models":{"op":"update","id":lookup(&name)?,"changes":changes}}),
                None,
            )
        }
        Model::Test { name, purpose } => post(
            client,
            &format!("/v2/models/{}/test", segment(&lookup(&name)?)),
            &json!({"purpose":purpose}),
            None,
        ),
        Model::Use { name, purpose } => binding(&purpose, json!(lookup(&name)?)),
        Model::Unbind { purpose } => binding(&purpose, Value::Null),
        Model::Remove { name } => patch(
            client,
            &config,
            None,
            json!({"models":{"op":"remove","id":lookup(&name)?}}),
            None,
        ),
    }
}

fn connector(client: &Client, command: Connector) -> Result<Value> {
    match command {
        Connector::List => get(client, "/v2/sources"),
        Connector::Add {
            connector_type,
            account,
            client: client_id,
        } => {
            let mut body = json!({"type":connector_type,"account":account});
            if let Some(client_id) = client_id {
                body["client_id"] = json!(client_id);
            }
            post(client, "/v2/sources", &body, None)
        }
        Connector::Pause { source_id } => post(
            client,
            "/v2/operations",
            &json!({"kind":"stop_capture","source_id":source_id}),
            None,
        ),
        Connector::Resume { source_id } => post(
            client,
            "/v2/operations",
            &json!({"kind":"resume_capture","source_id":source_id}),
            None,
        ),
        Connector::Sync { source_id, pause } => post(
            client,
            &format!("/v2/sources/{}/history", segment(&source_id)),
            &json!({"enabled":!pause}),
            None,
        ),
    }
}

fn inspect(
    client: &Client,
    route: &str,
    list: &str,
    field: &str,
    command: Inspection,
) -> Result<Value> {
    let value = get(client, &format!("/v2/{route}"))?;
    match command {
        Inspection::List => Ok(value),
        Inspection::Show { id } => value[list]
            .as_array()
            .into_iter()
            .flatten()
            .find(|item| item[field] == id)
            .cloned()
            .ok_or_else(|| Error::missing("record is unavailable")),
    }
}

fn operation(
    client: &Client,
    mut payload: Value,
    preview: bool,
    confirm: Option<String>,
) -> Result<Value> {
    if preview {
        return post(client, "/v2/operations/preview", &payload, None);
    }
    if let Some(preview_id) = confirm {
        payload["preview_id"] = json!(preview_id);
        post(client, "/v2/operations", &payload, None)
    } else {
        Err(Error::invalid(
            "first use --preview, then --confirm with the returned preview ID",
        ))
    }
}

fn register(
    instance: &platform::Instance,
    client: &Client,
    host: &str,
    destination: &str,
) -> Result<Value> {
    let payload =
        json!({"host":host,"destination":destination,"capabilities":{"load_context":true}});
    let mut record = post(client, "/v2/clients", &payload, None)?;
    record["host"] = json!(host);
    integrations::store_client(instance, &record)?;
    let mut output = record;
    for field in ["token", "client_token", "credential"] {
        if let Some(object) = output.as_object_mut() {
            object.remove(field);
        }
    }
    Ok(output)
}

fn host_command(command: Command, instance: &platform::Instance) -> Result<Option<Value>> {
    let (id, hook) = match command {
        Command::Mcp {
            command: Mcp::Serve { client },
        } => (client, None),
        Command::Hook {
            host,
            client,
            source,
            task,
        } => (client, Some((host, source, task))),
        _ => unreachable!(),
    };
    if !id
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
        || id.is_empty()
    {
        return Err(Error::invalid("invalid client ID"));
    }
    let record: Value = serde_json::from_slice(&std::fs::read(
        instance.root.join("run/clients").join(format!("{id}.json")),
    )?)?;
    let token = record["token"]
        .as_str()
        .or_else(|| record["client_token"].as_str())
        .ok_or_else(|| Error::denied("local client credential not registered"))?;
    let client = Client::bound(instance, token.to_owned())?;
    let Some((host_name, source, task)) = hook else {
        host::serve(&client)?;
        return Ok(None);
    };
    let registered = record["host"].as_str().unwrap_or("");
    if host_name != registered
        && !([host_name.as_str(), registered]
            .iter()
            .all(|name| matches!(*name, "claude" | "claude-code")))
    {
        return Err(Error::denied("hook host does not match the bound client"));
    }
    let mut raw = String::new();
    io::stdin().take(4 * 1024 * 1024).read_to_string(&mut raw)?;
    let normalized = host::normalize(&host_name, serde_json::from_str(&raw)?)?;
    let event = normalized["event"]
        .as_str()
        .ok_or_else(|| Error::invalid("invalid native hook event"))?;
    let session_id = normalized["session_id"]
        .as_str()
        .ok_or_else(|| Error::invalid("missing native session ID"))?;
    if let Some(source_id) = source {
        let mut events = normalized["events"].as_array().cloned().unwrap_or_default();
        if !events.is_empty() {
            if let Some(task_id) = &task {
                for event in &mut events {
                    event["task_id"] = json!(task_id);
                }
            }
            for event in &mut events {
                if event["actor_kind"] == "tool_result" && event.get("checks").is_none() {
                    let text = event
                        .as_object_mut()
                        .and_then(|obj| obj.remove("text"))
                        .unwrap_or(Value::Null);
                    event["checks"] = json!({"output":text});
                }
            }
            let key = format!(
                "hook-{}-{}",
                events[0]["native_object"].as_str().unwrap_or("event"),
                events[0]["native_version"].as_str().unwrap_or("1")
            );
            let accepted = post(
                &client,
                "/v2/events",
                &json!({"source_id":source_id,"events":events}),
                Some(&key),
            );
            if let Err(error) = accepted {
                if event == "UserPromptSubmit"
                    && task.is_some()
                    && matches!(error.status, 403 | 404)
                {
                    return Ok(Some(
                        json!({"decision":"block","reason":"bmem task capture is no longer authorized; start a clean session"}),
                    ));
                }
                if task.is_some()
                    || !error
                        .message
                        .starts_with("action_denied: capture is not enabled")
                {
                    return Err(error);
                }
            }
        }
    }
    let epoch = normalized["turn_id"].as_str().unwrap_or(session_id);
    if host_name != "dsh" && matches!(event, "SessionStart" | "UserPromptSubmit") {
        let mut context = json!({"session_id":session_id,"context_epoch":epoch,"route_ref":record["route_ref"],
            "append_only":true,"budget":{"statement_tokens":512,"evidence_tokens":0}});
        if let Some(task) = &task {
            let key = format!(
                "native-attempt-{:x}",
                Sha256::digest(json!([task, session_id, epoch]).to_string())
            );
            let attempt = match post(
                &client,
                &format!("/v2/tasks/{}/attempts", segment(task)),
                &context,
                Some(&key),
            ) {
                Ok(value) => value,
                Err(error) if matches!(error.status, 403 | 404) && event == "UserPromptSubmit" => {
                    return Ok(Some(
                        json!({"decision":"block","reason":"bmem task access is no longer authorized; start a clean session"}),
                    ));
                }
                Err(error) => return Err(error),
            };
            context["task_id"] = json!(task);
            context["attempt_id"] = attempt["attempt_id"].clone();
            context["budget"]["evidence_tokens"] = json!(4096);
        }
        let delivery = match post(&client, "/v2/context/load", &context, None) {
            Ok(delivery) => delivery,
            Err(error) if error.code == "fresh_session_required" && event == "UserPromptSubmit" => {
                return Ok(Some(json!({"decision":"block","reason":error.message})));
            }
            Err(error) if error.code == "fresh_session_required" => return Ok(None),
            Err(error) => return Err(error),
        };
        return Ok(Some(host::hook_output(event, &delivery)));
    }
    Ok(None)
}

fn open_browser(url: &str) -> Result<()> {
    #[cfg(target_os = "macos")]
    let mut command = std::process::Command::new("open");
    #[cfg(target_os = "linux")]
    let mut command = std::process::Command::new("xdg-open");
    #[cfg(target_os = "windows")]
    let mut command = std::process::Command::new("explorer.exe");
    let code = command
        .arg(url)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()?;
    if code.success() {
        Ok(())
    } else {
        Err(Error::unavailable(
            "could not open browser; run bmem web --no-open",
        ))
    }
}
