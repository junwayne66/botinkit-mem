use crate::error::{Error, Result};
use crate::{
    archive, authority, context, data_management, history, history_tasks, ingestion, integrations,
    platform, providers, retrieval, settings::Settings, vector,
};
use axum::body::{Body, to_bytes};
use axum::extract::State;
use axum::http::{HeaderMap, Response, StatusCode};
use axum::response::IntoResponse;
use rusqlite::{Connection, OpenFlags, OptionalExtension};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::net::{Ipv4Addr, SocketAddr};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};
use subtle::ConstantTimeEq;
use tokio::sync::{Notify, watch};
use uuid::Uuid;

struct App {
    instance: platform::Instance,
    address: SocketAddr,
    database: Mutex<Connection>,
    settings: Mutex<Settings>,
    tickets: Mutex<HashMap<String, Instant>>,
    web_sessions: Mutex<HashSet<String>>,
    stop: watch::Sender<bool>,
    jobs: Notify,
    history: Notify,
    feishu: Mutex<crate::feishu::Flow>,
}

pub(crate) fn lock<T>(mutex: &Mutex<T>) -> Result<MutexGuard<'_, T>> {
    mutex
        .lock()
        .map_err(|_| Error::internal("local state lock poisoned"))
}

pub fn database(instance: &platform::Instance) -> Result<Connection> {
    let mut db = Connection::open(instance.root.join("data/bmem.sqlite3"))?;
    db.busy_timeout(Duration::from_secs(5))?;
    let wal: String = db.query_row("PRAGMA journal_mode=WAL", [], |row| row.get(0))?;
    if !wal.eq_ignore_ascii_case("wal") {
        return Err(Error::conflict(
            "selected data root does not support SQLite WAL",
        ));
    }
    db.execute_batch("PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA secure_delete=ON;")?;
    authority::migrate(&db)?;
    context::migrate(&db)?;
    ingestion::migrate(&db)?;
    history::migrate(&db)?;
    integrations::migrate(&db)?;
    integrations::migrate_legacy_hosts(instance, &db)?;
    data_management::migrate(&db)?;
    history_tasks::migrate(&db)?;
    retrieval::migrate(&mut db)?;
    vector::migrate(&db)?;
    crate::analytics::migrate(&mut db)?;
    Ok(db)
}

fn read_snapshot(app: &App) -> Result<Connection> {
    let db = Connection::open_with_flags(
        app.instance.root.join("data/bmem.sqlite3"),
        OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?;
    db.busy_timeout(Duration::from_millis(500))?;
    // Keep all queries in one response on the same committed WAL snapshot.
    db.execute_batch("BEGIN DEFERRED")?;
    Ok(db)
}

pub async fn serve(instance: platform::Instance) -> Result<()> {
    let _service_lock = platform::lock_service(&instance)?;
    let preferred = platform::registered_endpoint(&instance).map_or(0, |addr| addr.port());
    let listener = match tokio::net::TcpListener::bind((Ipv4Addr::LOCALHOST, preferred)).await {
        Err(err) if preferred != 0 && err.kind() == std::io::ErrorKind::AddrInUse => {
            tokio::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await?
        }
        result => result?,
    };
    let address = listener.local_addr()?;
    platform::publish(&instance, address)?;
    let db = database(&instance)?;
    let settings = Settings::open(&instance.root)?;
    let (stop, mut stop_wait) = watch::channel(false);
    let state = Arc::new(App {
        instance,
        address,
        database: Mutex::new(db),
        settings: Mutex::new(settings),
        tickets: Mutex::new(HashMap::new()),
        web_sessions: Mutex::new(HashSet::new()),
        stop,
        jobs: Notify::new(),
        history: Notify::new(),
        feishu: Mutex::new(crate::feishu::Flow::default()),
    });
    let app = axum::Router::new().fallback(http).with_state(state.clone());
    let workers = [
        tokio::spawn(run_jobs(state.clone(), run_one)),
        tokio::spawn(run_jobs(state.clone(), run_vector)),
        tokio::spawn(run_history(state.clone(), |app, profile| {
            history::scan_next(&app.database, profile, &|| *app.stop.borrow())
        })),
        tokio::spawn(run_history(state.clone(), scan_history_task)),
        tokio::spawn(run_feishu_checks(state.clone())),
    ];
    let result = axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            tokio::select! {
                _ = shutdown_signal() => {},
                _ = stop_wait.wait_for(|stop| *stop) => {}
            }
        })
        .await;
    state.stop.send_replace(true);
    for worker in workers {
        worker
            .await
            .map_err(|_| Error::internal("job worker stopped abnormally"))?;
    }
    result?;
    Ok(())
}

async fn shutdown_signal() {
    #[cfg(unix)]
    if let Ok(mut terminate) =
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
    {
        tokio::select! {
            _ = terminate.recv() => {},
            _ = tokio::signal::ctrl_c() => {},
        }
        return;
    }
    let _ = tokio::signal::ctrl_c().await;
}

async fn http(State(state): State<Arc<App>>, request: axum::extract::Request) -> Response<Body> {
    let address = state.address.to_string();
    if header(request.headers(), "host") != Some(&address) {
        return response(
            &state,
            Err(Error::denied(
                "request host does not match the local service",
            )),
        );
    }
    let (parts, body) = request.into_parts();
    let path = parts.uri.path().to_string();
    let method = parts.method.as_str().to_string();
    if method == "GET" {
        if let Some((mime, content)) = asset(&path) {
            return decorate(
                &state,
                ([(axum::http::header::CONTENT_TYPE, mime)], content).into_response(),
            );
        }
        if path == "/v2/integrations/feishu/qr" {
            let authorized = authenticate(&state, &parts.headers, "GET").and_then(|identity| {
                identity.owner()?;
                lock(&state.feishu)?
                    .qr()
                    .ok_or_else(|| Error::missing("授权二维码尚未生成"))
            });
            return decorate(
                &state,
                match authorized {
                    Ok(bytes) => {
                        ([(axum::http::header::CONTENT_TYPE, "image/png")], bytes).into_response()
                    }
                    Err(error) => error_response(error),
                },
            );
        }
        if path == "/session" {
            let code = query(parts.uri.query(), "code").unwrap_or_default();
            return decorate(
                &state,
                session(&state, &code).unwrap_or_else(error_response),
            );
        }
    }
    let body = match to_bytes(body, 4 * 1024 * 1024).await {
        Ok(body) => body,
        Err(_) => {
            return response(
                &state,
                Err(Error::new(
                    413,
                    "input_too_large",
                    "request exceeds four MiB",
                )),
            );
        }
    };
    let app = state.clone();
    let result = tokio::task::spawn_blocking(move || {
        dispatch(
            &app,
            &method,
            &path,
            parts.uri.query(),
            &parts.headers,
            &body,
        )
    })
    .await
    .unwrap_or_else(|_| Err(Error::internal("request processing failed")));
    response(&state, result)
}

const JS: &str = "text/javascript; charset=utf-8";
const CSS: &str = "text/css; charset=utf-8";
const WEB_ASSETS: &[(&str, &str, &str)] = &[
    (
        "/",
        "text/html; charset=utf-8",
        include_str!("../../web/index.html"),
    ),
    (
        "/brand.svg",
        "image/svg+xml",
        include_str!("../../web/brand.svg"),
    ),
    ("/app.js", JS, include_str!("../../web/app.js")),
    ("/app.css", CSS, include_str!("../../web/app.css")),
    ("/dashboard.js", JS, include_str!("../../web/dashboard.js")),
    ("/atlas.js", JS, include_str!("../../web/atlas.js")),
    ("/atlas.css", CSS, include_str!("../../web/atlas.css")),
    ("/settings.js", JS, include_str!("../../web/settings.js")),
    ("/settings.css", CSS, include_str!("../../web/settings.css")),
    ("/library.css", CSS, include_str!("../../web/library.css")),
    ("/intake.js", JS, include_str!("../../web/intake.js")),
    ("/intake.css", CSS, include_str!("../../web/intake.css")),
    ("/ledger.js", JS, include_str!("../../web/ledger.js")),
    ("/ledger.css", CSS, include_str!("../../web/ledger.css")),
    ("/search.js", JS, include_str!("../../web/search.js")),
    ("/search.css", CSS, include_str!("../../web/search.css")),
];

fn asset(path: &str) -> Option<(&'static str, &'static str)> {
    WEB_ASSETS
        .iter()
        .find(|entry| entry.0 == path)
        .map(|&(_, mime, body)| (mime, body))
}

pub fn web_build() -> &'static str {
    static BUILD: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    BUILD.get_or_init(|| {
        let mut digest = Sha256::new();
        for (path, mime, contents) in WEB_ASSETS {
            for field in [path, mime, contents] {
                digest.update((field.len() as u64).to_be_bytes());
                digest.update(field.as_bytes());
            }
        }
        format!("{:x}", digest.finalize())
    })
}

fn decorate(app: &App, mut response: Response<Body>) -> Response<Body> {
    let headers = response.headers_mut();
    if let Ok(value) = app.instance.id.parse() {
        headers.insert("x-bmem-instance", value);
    }
    headers.insert("cache-control", "no-store".parse().unwrap());
    headers.insert("x-content-type-options", "nosniff".parse().unwrap());
    headers.insert("referrer-policy", "no-referrer".parse().unwrap());
    headers.insert(
        "content-security-policy",
        "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            .parse()
            .unwrap(),
    );
    response
}

fn response(app: &App, result: Result<(u16, Value)>) -> Response<Body> {
    decorate(
        app,
        match result {
            Ok((status, value)) => (
                StatusCode::from_u16(status).unwrap_or(StatusCode::OK),
                axum::Json(value),
            )
                .into_response(),
            Err(err) => error_response(err),
        },
    )
}

fn error_response(err: Error) -> Response<Body> {
    (
        StatusCode::from_u16(err.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        axum::Json(err.value()),
    )
        .into_response()
}

fn session(app: &App, code: &str) -> Result<Response<Body>> {
    if !lock(&app.tickets)?
        .remove(code)
        .is_some_and(|expiry| expiry > Instant::now())
    {
        return Err(Error::denied(
            "web code is expired or already used; run bmem web again",
        ));
    }
    let token = secret();
    lock(&app.web_sessions)?.insert(token.clone());
    let cookie = format!(
        "bmem_session_{port}={token}; Path=/; HttpOnly; SameSite=Strict",
        port = app.address.port()
    );
    Ok((
        StatusCode::SEE_OTHER,
        [("location", "/"), ("set-cookie", &cookie)],
        "",
    )
        .into_response())
}

fn secret() -> String {
    format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple())
}

fn header<'a>(headers: &'a HeaderMap, name: &str) -> Option<&'a str> {
    headers.get(name)?.to_str().ok()
}

fn query(raw: Option<&str>, key: &str) -> Option<String> {
    url::form_urlencoded::parse(raw?.as_bytes())
        .find_map(|(name, value)| (name == key).then(|| value.into_owned()))
}

enum Identity {
    Owner,
    Browser,
    Bound(context::BoundClient),
}

impl Identity {
    fn owner(&self) -> Result<()> {
        match self {
            Self::Owner | Self::Browser => Ok(()),
            Self::Bound(_) => Err(Error::denied(
                "only the local owner can perform this operation",
            )),
        }
    }

    fn bound(&self) -> Result<&context::BoundClient> {
        match self {
            Self::Bound(client) => Ok(client),
            _ => Err(Error::denied("operation requires a bound client")),
        }
    }

    fn optional_bound(&self) -> Option<&context::BoundClient> {
        match self {
            Self::Bound(client) => Some(client),
            _ => None,
        }
    }
}

fn authenticate(app: &App, headers: &HeaderMap, method: &str) -> Result<Identity> {
    if let Some(version) = header(headers, "bmem-command-version")
        && version != "1"
    {
        return Err(Error::new(
            406,
            "unsupported_contract_version",
            "this version supports command contract 1",
        ));
    }
    if let Some(token) = header(headers, "authorization").and_then(|v| v.strip_prefix("Bearer ")) {
        if header(headers, "x-bmem-instance") != Some(&app.instance.id) {
            return Err(Error::conflict(
                "request instance does not match the data root",
            ));
        }
        if bool::from(token.as_bytes().ct_eq(app.instance.owner_token.as_bytes())) {
            return Ok(Identity::Owner);
        }
        return Ok(Identity::Bound(context::bind(&read_snapshot(app)?, token)?));
    }
    let name = format!("bmem_session_{}", app.address.port());
    let cookie = header(headers, "cookie").and_then(|cookies| {
        cookies
            .split(';')
            .filter_map(|part| part.trim().split_once('='))
            .find_map(|(key, value)| (key == name).then_some(value))
    });
    if let Some(cookie) = cookie
        && lock(&app.web_sessions)?.contains(cookie)
    {
        if method != "GET" && header(headers, "origin") != Some(&format!("http://{}", app.address))
        {
            return Err(Error::denied("browser mutation must have the local origin"));
        }
        return Ok(Identity::Browser);
    }
    Err(Error::new(
        401,
        "authentication_required",
        "run bmem web to open an authenticated local session",
    ))
}

fn key(headers: &HeaderMap) -> Result<&str> {
    header(headers, "idempotency-key")
        .filter(|k| !k.trim().is_empty() && k.len() <= 160)
        .ok_or_else(|| Error::invalid("provide an Idempotency-Key between 1 and 160 bytes"))
}

fn required<'a>(body: &'a Value, field: &str) -> Result<&'a str> {
    body[field]
        .as_str()
        .filter(|v| !v.trim().is_empty())
        .ok_or_else(|| Error::invalid(format!("missing {field}")))
}

fn dispatch(
    app: &Arc<App>,
    method: &str,
    path: &str,
    qs: Option<&str>,
    headers: &HeaderMap,
    bytes: &[u8],
) -> Result<(u16, Value)> {
    let identity = authenticate(app, headers, method)?;
    let body = if bytes.is_empty() {
        json!({})
    } else {
        serde_json::from_slice(bytes)?
    };
    let parts: Vec<&str> = path.trim_matches('/').split('/').collect();
    let outcome = match (method, parts.as_slice()) {
        ("GET", ["v2", "overview"]) => {
            identity.owner()?;
            (
                200,
                crate::analytics::overview(&read_snapshot(app)?, query(qs, "days").as_deref())?,
            )
        }
        ("GET", ["v2", "graph"]) => {
            identity.owner()?;
            (
                200,
                crate::analytics::graph(&read_snapshot(app)?, query(qs, "limit").as_deref())?,
            )
        }
        ("GET", ["v2", "health"]) => {
            identity.owner()?;
            let settings = lock(&app.settings)?.get();
            (
                200,
                json!({"state":"running","endpoint":format!("http://{}",app.address),"instance_id":app.instance.id,
                "version":env!("CARGO_PKG_VERSION"),"profile":settings["profile"],"vector_search":settings["vector_search"],
                "activity":settings["activity"],"database":"ready","service":"ready","capabilities":{"lexical":"ready"}}),
            )
        }
        ("POST", ["v2", "diagnostics"]) => {
            identity.owner()?;
            (200, platform::diagnostics(Some(&app.instance.root)))
        }
        ("POST", ["v2", "web", "session"]) => {
            if !matches!(&identity, Identity::Owner) {
                return Err(Error::denied("session codes are created by the owner CLI"));
            }
            let code = secret();
            let mut tickets = lock(&app.tickets)?;
            tickets.retain(|_, expiry| *expiry > Instant::now());
            tickets.insert(code.clone(), Instant::now() + Duration::from_secs(120));
            (
                201,
                json!({"url":format!("http://{}/session?code={code}",app.address),"web_build":web_build()}),
            )
        }
        ("POST", ["v2", "service", "stop"]) => {
            identity.owner()?;
            app.stop.send_replace(true);
            (
                202,
                json!({"state":"stopping","instance_id":app.instance.id}),
            )
        }
        ("GET", ["v2", "config"]) => {
            identity.owner()?;
            (200, lock(&app.settings)?.get())
        }
        ("PATCH", ["v2", "config"]) => {
            identity.owner()?;
            let value = lock(&app.settings)?.patch("owner", key(headers)?, body)?;
            app.jobs.notify_one();
            (200, value)
        }
        ("POST", ["v2", "models", "ollama", "discover"]) => {
            identity.owner()?;
            (
                200,
                providers::discover_ollama(required(&body, "endpoint")?)?,
            )
        }
        ("POST", ["v2", "models", model_id, "test"]) => {
            identity.owner()?;
            let purpose = required(&body, "purpose")?;
            let value = probe(app, model_id, purpose)?;
            (200, value)
        }
        ("POST", ["v2", "statements"]) => {
            identity.owner()?;
            (
                201,
                authority::append(&mut *lock(&app.database)?, "owner", key(headers)?, body)?,
            )
        }
        ("GET", ["v2", "statements"]) => {
            identity.owner()?;
            (
                200,
                authority::current(
                    &read_snapshot(app)?,
                    query(qs, "project").as_deref(),
                    query(qs, "valid_at").as_deref(),
                    query(qs, "known_at").as_deref(),
                    false,
                )?,
            )
        }
        ("GET", ["v2", "facts", id, "history"]) => {
            identity.owner()?;
            (200, authority::history(&read_snapshot(app)?, id)?)
        }
        ("POST", ["v2", "clients"]) => {
            identity.owner()?;
            (
                201,
                context::register(&mut *lock(&app.database)?, key(headers)?, body)?,
            )
        }
        ("GET", ["v2", "clients"]) => {
            identity.owner()?;
            (200, context::clients(&read_snapshot(app)?)?)
        }
        ("GET", ["v2", "deliveries"]) => {
            identity.owner()?;
            (200, context::deliveries(&read_snapshot(app)?)?)
        }
        ("GET", ["v2", "deliveries", "ledger"]) => {
            identity.owner()?;
            let page = query(qs, "page")
                .map(|value| value.parse::<usize>())
                .transpose()
                .map_err(|_| Error::invalid("page must be a positive integer"))?
                .unwrap_or(1);
            (
                200,
                context::delivery_ledger(
                    &read_snapshot(app)?,
                    query(qs, "host").as_deref(),
                    query(qs, "loaded").as_deref(),
                    query(qs, "used").as_deref(),
                    page,
                )?,
            )
        }
        ("POST", ["v2", "context", "load"]) => {
            let mut body = body;
            if body.is_object() && body.get("requested_profile").is_none() {
                body["requested_profile"] = lock(&app.settings)?.get()["profile"].clone();
            }
            (
                200,
                context::load(
                    &mut *lock(&app.database)?,
                    identity.bound()?,
                    header(headers, "idempotency-key"),
                    body,
                )?,
            )
        }
        ("POST", ["v2", "deliveries", id, "receipts"]) => (
            200,
            context::receipt(&mut *lock(&app.database)?, identity.bound()?, id, body)?,
        ),
        ("GET", ["v2", "tasks"]) => {
            identity.owner()?;
            (200, context::tasks(&read_snapshot(app)?)?)
        }
        ("POST", ["v2", "tasks"]) => (
            201,
            context::create_task(
                &mut *lock(&app.database)?,
                identity.optional_bound(),
                key(headers)?,
                body,
            )?,
        ),
        ("POST", ["v2", "tasks", id, "attempts"]) => (
            201,
            context::create_attempt(
                &mut *lock(&app.database)?,
                identity.optional_bound(),
                id,
                key(headers)?,
                body,
            )?,
        ),
        ("POST", ["v2", "attempts", id, "outcomes"]) => (
            200,
            context::outcome(&mut *lock(&app.database)?, identity.bound()?, id, body)?,
        ),
        ("GET", ["v2", "tasks", id, "handoff"]) => {
            let db = read_snapshot(app)?;
            let data = if let Some(client) = identity.optional_bound() {
                if !context::task_access(&db, id, &client.id)? {
                    return Err(Error::missing("task is unavailable"));
                }
                retrieval::for_client(&db, id, &client.id)?
            } else {
                retrieval::handoff(&db, id)?
            };
            (200, data)
        }
        ("POST", ["v2", "search"]) => {
            let query = required(&body, "query")?;
            if let Some(client) = identity.optional_bound() {
                let db = read_snapshot(app)?;
                if body["route_ref"]
                    .as_str()
                    .is_some_and(|route| route != client.route_ref)
                {
                    return Err(Error::denied("requested route is not bound to the client"));
                }
                if !context::shared_enabled(&db, client)? {
                    return Err(Error::denied(
                        "shared statements are not granted to this client",
                    ));
                }
                (200, retrieval::search_shared(&db, query)?)
            } else {
                (
                    200,
                    owner_search(app, query, body["task_id"].as_str(), body["mode"].as_str())?,
                )
            }
        }
        ("POST", ["v2", "evidence", "expand"]) => {
            let db = read_snapshot(app)?;
            let handle = required(&body, "handle")?;
            let data = if let Some(client) = identity.optional_bound() {
                retrieval::expand_for_client(&db, handle, &client.id)?
            } else {
                retrieval::expand(&db, handle)?
            };
            (200, data)
        }
        ("GET", ["v2", "integrations"]) => {
            identity.owner()?;
            let mut flow = lock(&app.feishu)?.status();
            let db = read_snapshot(app)?;
            let mut status = integrations::status(&db)?;
            let durable = integrations::feishu_status(&db)?;
            if let (Some(flow), Some(persisted)) = (flow.as_object_mut(), durable.as_object()) {
                flow.extend(persisted.clone());
            }
            status["feishu"] = flow;
            (200, status)
        }
        ("POST", ["v2", "integrations", "feishu", "start"]) => {
            identity.owner()?;
            if lock(&app.feishu)?.begin() {
                let app = app.clone();
                tokio::task::spawn_blocking(move || {
                    let shutdown = app.stop.subscribe();
                    let result = crate::feishu::authorize(&app.feishu, || *shutdown.borrow());
                    let result = result.and_then(|account| {
                        let mut db = lock(&app.database)?;
                        let source = ingestion::register(
                            &mut db,
                            json!({
                                "type":"feishu","account":account.key,"actors":["owner"]
                            }),
                        )?;
                        let source_id = required(&source, "source_id")?;
                        integrations::record_feishu(&db, source_id, Some(&account))?;
                        Ok(json!({"account_name":account.label,"source_id":source["source_id"]}))
                    });
                    if let Ok(mut flow) = app.feishu.lock() {
                        flow.finish(result);
                    }
                });
            }
            (202, lock(&app.feishu)?.status())
        }
        ("POST", ["v2", "integrations", host, "install"]) => {
            identity.owner()?;
            let configured = crate::integrations::install(
                &app.instance,
                &mut *lock(&app.database)?,
                host,
                &body,
            )?;
            (200, configured)
        }
        ("GET", ["v2", "sources"]) => {
            identity.owner()?;
            let route = match model_route(app, "extract") {
                Ok(route) => route,
                Err(error) if error.code == "temporarily_unavailable" => None,
                Err(error) => return Err(error),
            };
            let fingerprint = route.as_ref().map(|(_, _, id)| id.as_str());
            let db = read_snapshot(app)?;
            let mut sources = ingestion::list(&db, fingerprint)?;
            history::describe(&db, &mut sources)?;
            (200, json!({"sources":sources}))
        }
        ("POST", ["v2", "sources", id, "history"]) => {
            identity.owner()?;
            let enabled = body["enabled"]
                .as_bool()
                .ok_or_else(|| Error::invalid("enabled must be boolean"))?;
            let output = history::set_enabled(&mut *lock(&app.database)?, id, enabled)?;
            app.history.notify_one();
            (202, output)
        }
        ("GET", ["v2", "history", "options"]) => {
            identity.owner()?;
            let source_id =
                query(qs, "source_id").ok_or_else(|| Error::invalid("source_id is required"))?;
            let kind = query(qs, "kind").ok_or_else(|| Error::invalid("kind is required"))?;
            let (source, account) = history_source(&read_snapshot(app)?, &source_id)?;
            let search = query(qs, "query");
            let cursor = query(qs, "cursor");
            let stop = || *app.stop.borrow();
            let options = if source == "feishu" {
                crate::history_feishu::options(
                    &account,
                    &kind,
                    search.as_deref(),
                    cursor.as_deref(),
                    &stop,
                )?
            } else {
                crate::history_hosts::options(
                    &source,
                    &kind,
                    search.as_deref(),
                    cursor.as_deref(),
                    &stop,
                )?
            };
            (200, options)
        }
        ("POST", ["v2", "history", "preview"]) => {
            identity.owner()?;
            (200, history_tasks::preview(&read_snapshot(app)?, &body)?)
        }
        ("GET", ["v2", "history", "jobs"]) => {
            identity.owner()?;
            (200, history_tasks::list(&read_snapshot(app)?)?)
        }
        ("POST", ["v2", "history", "jobs"]) => {
            identity.owner()?;
            let mut db = lock(&app.database)?;
            let key = key(headers)?;
            let job = if body.get("kinds").is_some() {
                history_tasks::create_all(&mut db, &body, key)?
            } else {
                history_tasks::create(&mut db, &body, key)?
            };
            app.history.notify_waiters();
            (202, job)
        }
        ("POST", ["v2", "history", "jobs", id, action @ ("cancel" | "retry")]) => {
            identity.owner()?;
            let job = history_tasks::change(&mut *lock(&app.database)?, id, action)?;
            app.history.notify_waiters();
            (200, job)
        }
        ("GET", ["v2", "data", "records"]) => {
            identity.owner()?;
            (200, data_management::records(&read_snapshot(app)?)?)
        }
        ("POST", ["v2", "data", "export"]) => {
            identity.owner()?;
            (
                200,
                data_management::export(&mut *lock(&app.database)?, &body)?,
            )
        }
        ("POST", ["v2", "sources"]) => {
            identity.owner()?;
            let mut db = lock(&app.database)?;
            let mut body = body;
            let client_id = body
                .get("client_id")
                .and_then(Value::as_str)
                .map(str::to_owned);
            if let Some(client_id) = client_id {
                let registered = db
                    .query_row(
                        "SELECT host FROM context_clients WHERE id=?1",
                        [&client_id],
                        |r| r.get::<_, String>(0),
                    )
                    .optional()?;
                if registered.as_deref() != body["type"].as_str() {
                    return Err(Error::denied("source type must match a registered host"));
                }
                body["actors"] = json!(["owner", client_id]);
            } else {
                body["actors"] = json!(["owner"]);
            }
            let data = ingestion::register(&mut db, body)?;
            (201, data)
        }
        ("POST", ["v2", "events"]) => {
            let source_id = required(&body, "source_id")?.to_owned();
            let key = key(headers)?;
            let mut db = lock(&app.database)?;
            if let Some(client) = identity.optional_bound() {
                for event in body["events"].as_array().into_iter().flatten() {
                    if let Some(task) = event["task_id"].as_str()
                        && !context::task_access(&db, task, &client.id)?
                    {
                        return Err(Error::denied(
                            "source adapter cannot attach evidence to that task",
                        ));
                    }
                }
            }
            let actor = identity
                .optional_bound()
                .map_or("owner", |client| client.id.as_str());
            let mut body = body;
            if body["next_cursor"].is_null() && identity.optional_bound().is_some() {
                let current = db
                    .query_row(
                        "SELECT cursor FROM ing_sources WHERE source_id=?1",
                        [&source_id],
                        |r| r.get::<_, String>(0),
                    )
                    .optional()?
                    .ok_or_else(|| Error::missing("source is unavailable"))?;
                let previous = db.query_row("SELECT cursor_before,cursor_after FROM ing_ingests WHERE source_id=?1 AND actor=?2 AND idem_key=?3",
                    [&source_id,actor,key],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?))).optional()?;
                let (before, after) =
                    previous.unwrap_or_else(|| (current, Uuid::new_v4().to_string()));
                body["cursor"] = json!(before);
                body["next_cursor"] = json!(after);
            }
            let profile = lock(&app.settings)?.get()["profile"]
                .as_str()
                .unwrap_or("balanced")
                .to_owned();
            let manual_import = header(headers, "x-bmem-import") == Some("file")
                || query(qs, "import").as_deref() == Some("true");
            let value = if manual_import {
                identity.owner()?;
                let tx = db.transaction()?;
                let value = ingestion::accept_in(&tx, actor, key, body, &profile)?;
                data_management::record_import(&tx, &source_id, &value)?;
                tx.commit()?;
                value
            } else {
                ingestion::accept(&mut db, actor, key, body, &profile)?
            };
            app.jobs.notify_waiters();
            (201, value)
        }
        ("GET", ["v2", "jobs"]) => {
            identity.owner()?;
            (
                200,
                crate::analytics::job_page(
                    &read_snapshot(app)?,
                    query(qs, "limit").as_deref().unwrap_or("40"),
                    query(qs, "before").as_deref(),
                    query(qs, "status").as_deref(),
                )?,
            )
        }
        ("GET", ["v2", "jobs", id]) => {
            identity.owner()?;
            let mut job = ingestion::fetch_job(&read_snapshot(app)?, id)?;
            job["job_id"] = job["id"].clone();
            (200, job)
        }
        ("GET", ["v2", "jobs", id, "candidates"]) => {
            identity.owner()?;
            (200, ingestion::job_candidates(&read_snapshot(app)?, id)?)
        }
        ("POST", ["v2", "jobs", id, "retry"]) => {
            identity.owner()?;
            let value = ingestion::retry(&*lock(&app.database)?, id)?;
            app.jobs.notify_one();
            (202, value)
        }
        ("POST", ["v2", "operations"] | ["v2", "operations", "preview"]) => {
            identity.owner()?;
            if path == "/v2/operations" {
                match body["kind"].as_str() {
                    Some("backup") => {
                        return Ok((
                            201,
                            archive::backup(
                                &*lock(&app.database)?,
                                &app.instance.root,
                                &app.instance.id,
                            )?,
                        ));
                    }
                    Some("restore_preview") => {
                        return Ok((
                            200,
                            archive::preview(
                                &*lock(&app.database)?,
                                &app.instance.root,
                                &app.instance.id,
                                body,
                            )?,
                        ));
                    }
                    _ => {}
                }
            }
            let route = match (body["purpose"].as_str(), body["allow"].as_bool()) {
                (Some(purpose @ ("extract" | "embed")), Some(true)) => model_route(app, purpose)?,
                _ => None,
            };
            let preview = path.ends_with("/preview");
            // Readiness must be honest before asking the owner to confirm:
            // an extract/embed allow preview without a bound, tested model
            // fails with the same 409 the confirm path returns.
            if preview
                && route.is_none()
                && body["kind"].as_str() == Some("change_grant")
                && body["source_id"].as_str().is_some()
                && body["allow"].as_bool() == Some(true)
                && matches!(body["purpose"].as_str(), Some("extract" | "embed"))
            {
                let message = if body["purpose"].as_str() == Some("embed") {
                    "请先开启向量检索，再测试并绑定本机嵌入模型。"
                } else {
                    "请先在模型设置中测试并绑定提炼模型，可使用服务器 API 或本地 Ollama。"
                };
                return Err(Error::new(409, "model_not_ready", message));
            }
            let fingerprint = route.as_ref().map(|(_, _, id)| id.as_str());
            let mut result = retrieval::operate(
                &mut *lock(&app.database)?,
                preview,
                key(headers)?,
                body,
                fingerprint,
            )?;
            if preview && let Some((model, _, _)) = route {
                result["model_destination"] = json!({
                    "name": model.name,
                    "endpoint": model.endpoint,
                    "model_id": model.model_id,
                    "local_endpoint": model.local_endpoint(),
                });
            }
            (202, result)
        }
        ("GET", ["v2", "operations"]) => {
            identity.owner()?;
            (200, retrieval::operations(&read_snapshot(app)?)?)
        }
        ("GET", ["v2", "operations", id]) => {
            identity.owner()?;
            (
                200,
                find(
                    retrieval::operations(&read_snapshot(app)?)?,
                    "operations",
                    "operation_id",
                    id,
                )?,
            )
        }
        ("GET", ["v2", "capabilities"]) => (
            200,
            json!({"items":capabilities(app,&identity)?,"next_cursor":null}),
        ),
        ("GET", ["v2", "capabilities", id]) => (
            200,
            find(
                json!({"items":capabilities(app,&identity)?}),
                "items",
                "command_id",
                id,
            )?,
        ),
        _ => return Err(Error::missing("endpoint is unavailable on this release")),
    };
    Ok(outcome)
}

fn find(value: Value, list: &str, field: &str, id: &str) -> Result<Value> {
    value[list]
        .as_array()
        .into_iter()
        .flatten()
        .find(|item| item[field].as_str() == Some(id))
        .cloned()
        .ok_or_else(|| Error::missing("record is unavailable"))
}

fn capabilities(app: &App, identity: &Identity) -> Result<Vec<Value>> {
    let own = identity.optional_bound().is_none();
    let search = if let Some(client) = identity.optional_bound() {
        context::shared_enabled(&read_snapshot(app)?, client)?
    } else {
        true
    };
    let mut items = vec![capability("search.query", search, "query", "/v2/search")];
    if own {
        items.extend([
            capability("evidence.expand", true, "query", "/v2/evidence/expand"),
            capability("config.patch", true, "action", "/v2/config"),
            capability("config.read", true, "query", "/v2/config"),
            capability("statement.append", true, "action", "/v2/statements"),
            capability("statement.list", true, "query", "/v2/statements"),
            capability("health.read", true, "query", "/v2/health"),
        ]);
    }
    Ok(items)
}

fn capability(id: &str, available: bool, kind: &str, path: &str) -> Value {
    json!({"command_id":id,"available":available,"kind":kind,"stage":"P0","supported_versions":[1],"http":{"path":path}})
}

fn probe(app: &App, id: &str, purpose: &str) -> Result<Value> {
    let (model, credential) = {
        let settings = lock(&app.settings)?;
        if purpose == "embed" && settings.get()["vector_search"]["enabled"] != true {
            return Err(Error::conflict("vector search is disabled"));
        }
        if !matches!(purpose, "extract" | "embed") {
            return Err(Error::invalid("model purpose must be extract or embed"));
        }
        let model = settings.model(id)?;
        let credential = settings.model_credential(&model)?;
        (model, credential)
    };
    let sample = json!({"event_id":"probe","actor_kind":"tool_result","inputs":[
        {"event_ref":"probe@1","kind":"tool_result","span_id":"probe#/checks/parse","field":"/checks/parse","value":"passed"},
        {"event_ref":"probe@1","kind":"tool_result","span_id":"probe#/checks/write","field":"/checks/write","value":"timeout"}
    ]});
    let dimension = if purpose == "embed" {
        Some(
            vector::embed(
                &model.protocol,
                &model.endpoint,
                &model.model_id,
                credential.as_deref(),
                "A small bmem embedding probe.",
            )?
            .len(),
        )
    } else {
        providers::extract(
            &model.protocol,
            &model.endpoint,
            &model.model_id,
            credential.as_deref(),
            &sample,
        )?;
        None
    };
    let mut settings = lock(&app.settings)?;
    let current = settings.model(id)?;
    if current.protocol != model.protocol
        || current.endpoint != model.endpoint
        || current.model_id != model.model_id
        || current.credential_ref != model.credential_ref
    {
        return Err(Error::conflict("model settings changed while probing"));
    }
    let mut result = settings.mark_tested(id, purpose)?;
    if let Some(dimension) = dimension {
        result["dimension"] = json!(dimension);
    }
    Ok(result)
}

async fn run_jobs(state: Arc<App>, work: fn(&App) -> Result<bool>) {
    let mut stop = state.stop.subscribe();
    loop {
        let run = state.clone();
        let worked = tokio::task::spawn_blocking(move || work(&run))
            .await
            .ok()
            .and_then(std::result::Result::ok)
            .unwrap_or(false);
        if *stop.borrow() {
            return;
        }
        if !worked {
            tokio::select! {
                _ = stop.wait_for(|stop| *stop) => return,
                _ = state.jobs.notified() => {},
                _ = tokio::time::sleep(Duration::from_secs(2)) => {}
            }
        }
    }
}

fn history_source(db: &Connection, id: &str) -> Result<(String, String)> {
    let (kind, account): (String, String) = db
        .query_row(
            "SELECT kind, account FROM ing_sources WHERE source_id=?1",
            [id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?
        .ok_or_else(|| Error::missing("来源不存在"))?;
    let supported = match kind.as_str() {
        "feishu" => account.starts_with("feishu-"),
        "codex" | "claude-code" | "dsh" => account == "local",
        _ => false,
    };
    if !supported {
        return Err(Error::invalid("该来源仅支持文件导入"));
    }
    Ok((kind, account))
}

async fn run_feishu_checks(app: Arc<App>) {
    let mut stop = app.stop.subscribe();
    let mut interval = tokio::time::interval(Duration::from_secs(60));
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    while !*stop.borrow() {
        tokio::select! {
            _ = stop.changed() => break,
            _ = interval.tick() => {
                let app = app.clone();
                let _ = tokio::task::spawn_blocking(move || verify_feishu_registrations(&app)).await;
            }
        }
    }
}

fn verify_feishu_registrations(app: &App) -> Result<()> {
    let state = integrations::feishu_status(&read_snapshot(app)?)?;
    let stop = || *app.stop.borrow();
    for row in state["accounts"].as_array().into_iter().flatten() {
        if stop() {
            break;
        }
        if let (Some(source), Some(account)) = (row["source_id"].as_str(), row["account"].as_str())
        {
            let result = crate::history_feishu::account(account, &stop);
            if !stop() {
                match result {
                    Ok(account) => integrations::record_feishu(
                        &*lock(&app.database)?,
                        source,
                        Some(&account.identity),
                    )?,
                    Err(error) if error.code == "action_denied" => {
                        integrations::record_feishu(&*lock(&app.database)?, source, None)?;
                    }
                    Err(_) => {}
                }
            }
        }
    }
    Ok(())
}

fn scan_history_task(app: &App, profile: &str) -> Result<bool> {
    let stop = || *app.stop.borrow();
    history_tasks::run_next(
        &app.database,
        profile,
        &stop,
        &|source_type, account, kind, target, from, to, content, cursor| {
            if source_type == "feishu" {
                crate::history_feishu::read_import_page(
                    account,
                    kind,
                    target,
                    (from, to),
                    content,
                    cursor,
                    &stop,
                )
            } else {
                crate::history_hosts::read_import_page(
                    source_type,
                    kind,
                    target,
                    (from, to),
                    content,
                    cursor,
                    &stop,
                )
            }
        },
    )
}

async fn run_history(state: Arc<App>, scan: fn(&App, &str) -> Result<bool>) {
    let mut stop = state.stop.subscribe();
    loop {
        let run = state.clone();
        let worked = tokio::task::spawn_blocking(move || -> Result<bool> {
            let profile = lock(&run.settings)?.get()["profile"]
                .as_str()
                .unwrap_or("balanced")
                .to_owned();
            scan(&run, &profile)
        })
        .await
        .ok()
        .and_then(std::result::Result::ok)
        .unwrap_or(false);
        if *stop.borrow() {
            return;
        }
        if worked {
            state.jobs.notify_waiters();
        } else {
            tokio::select! {
                _ = stop.wait_for(|stop| *stop) => return,
                _ = state.history.notified() => {},
                _ = tokio::time::sleep(Duration::from_secs(2)) => {}
            }
        }
    }
}

fn run_one(app: &App) -> Result<bool> {
    let Some((model, credential, route)) = model_route(app, "extract")? else {
        return Ok(false);
    };
    let job = ingestion::claim(&mut *lock(&app.database)?, &route)?;
    let Some(job) = job else { return Ok(false) };
    let result = providers::extract(
        &model.protocol,
        &model.endpoint,
        &model.model_id,
        credential.as_deref(),
        &job.input,
    );
    let current_route = model_route(app, "extract")
        .ok()
        .flatten()
        .map(|(_, _, fingerprint)| fingerprint);
    let mut db = lock(&app.database)?;
    if current_route.as_deref() != Some(&route) {
        ingestion::fail(&db, &job, "model_route_changed")?;
        return Ok(true);
    }
    match result {
        Ok(candidates) => {
            if let Err(error) = ingestion::finish(&mut db, &job, candidates, &route) {
                ingestion::fail(&db, &job, &error.code)?;
            }
        }
        Err(error) => ingestion::fail(&db, &job, &error.code)?,
    }
    Ok(true)
}

type ModelRoute = (crate::settings::Model, Option<String>, String);

fn model_route(app: &App, purpose: &str) -> Result<Option<ModelRoute>> {
    let settings = lock(&app.settings)?;
    let model = settings.bound_model(purpose);
    let Some(model) = model else { return Ok(None) };
    let credential = settings.model_credential(&model)?;
    let credential_fingerprint = credential
        .as_ref()
        .map(|value| format!("{:x}", Sha256::digest(value)));
    let identity = json!([
        purpose,
        model.id,
        model.protocol,
        model.endpoint,
        model.model_id,
        credential_fingerprint
    ]);
    let fingerprint = format!("{:x}", Sha256::digest(identity.to_string()));
    Ok(Some((model, credential, fingerprint)))
}

fn run_vector(app: &App) -> Result<bool> {
    let Some((model, credential, route)) = model_route(app, "embed")? else {
        return Ok(false);
    };
    let job = vector::pending(&read_snapshot(app)?, &route)?;
    let Some(job) = job else { return Ok(false) };
    let value = vector::embed(
        &model.protocol,
        &model.endpoint,
        &model.model_id,
        credential.as_deref(),
        &job.text,
    )?;
    let current = model_route(app, "embed")?.map(|(_, _, id)| id);
    if current.as_deref() == Some(&route) {
        vector::commit(&*lock(&app.database)?, &job, &route, &value)?;
    }
    Ok(true)
}

fn owner_search(app: &App, query: &str, task: Option<&str>, mode: Option<&str>) -> Result<Value> {
    let failure = match mode {
        Some("vector") => match vector_search(app, query, task) {
            Ok(Some(results)) if !results.is_empty() => {
                return Ok(json!({"results":results,"retrieval":"vector"}));
            }
            Ok(_) => Some("vector_not_ready"),
            Err(error) => Some(if error.status == 503 {
                "vector_provider_unavailable"
            } else {
                "vector_unavailable"
            }),
        },
        None | Some("lexical") => None,
        _ => return Err(Error::invalid("search mode must be lexical or vector")),
    };
    let mut output = retrieval::search_owner(&read_snapshot(app)?, query, task)?;
    if let Some(reason) = failure {
        output["fallback_reason"] = json!(reason);
    }
    Ok(output)
}

fn vector_search(app: &App, query: &str, task: Option<&str>) -> Result<Option<Vec<Value>>> {
    let Some((model, credential, route)) = model_route(app, "embed")? else {
        return Ok(None);
    };
    let q = vector::embed(
        &model.protocol,
        &model.endpoint,
        &model.model_id,
        credential.as_deref(),
        query,
    )?;
    if model_route(app, "embed")?
        .as_ref()
        .map(|(_, _, current)| current)
        != Some(&route)
    {
        return Ok(None);
    }
    Ok(Some(vector::search(
        &read_snapshot(app)?,
        &route,
        &q,
        task,
    )?))
}
