use crate::error::{Error, Result};
use crate::{history_feishu, history_hosts, ingestion, server::lock};
use rusqlite::{Connection, OptionalExtension, Transaction, params};
use serde_json::{Value, json};
use std::collections::HashMap;
use std::sync::Mutex;
use uuid::Uuid;

const LOCAL_PAGES_PER_PASS: usize = 32;
const EVENTS_PER_PAGE: usize = 48;

struct Source {
    id: String,
    kind: String,
    account: String,
}

pub fn migrate(db: &Connection) -> Result<()> {
    db.execute_batch(
        "CREATE TABLE IF NOT EXISTS history_sources (
            source_id TEXT PRIMARY KEY REFERENCES ing_sources(source_id),
            enabled INTEGER NOT NULL,
            next_at INTEGER NOT NULL DEFAULT 0,
            last_scan INTEGER,
            last_error TEXT,
            events INTEGER NOT NULL DEFAULT 0,
            skipped_containers INTEGER NOT NULL DEFAULT 0
         );
         CREATE TABLE IF NOT EXISTS history_files (
            source_id TEXT NOT NULL REFERENCES history_sources(source_id),
            path TEXT NOT NULL,
            offset INTEGER NOT NULL,
            size INTEGER NOT NULL,
            complete INTEGER NOT NULL,
            PRIMARY KEY(source_id,path)
         );
         CREATE TABLE IF NOT EXISTS history_feishu_passes (
            source_id TEXT PRIMARY KEY REFERENCES history_sources(source_id),
            ends_at INTEGER NOT NULL,
            list_token TEXT,
            listed INTEGER NOT NULL DEFAULT 0
         );
         CREATE TABLE IF NOT EXISTS history_feishu_chats (
            source_id TEXT NOT NULL REFERENCES history_sources(source_id),
            chat_id TEXT NOT NULL,
            watermark INTEGER NOT NULL DEFAULT 0,
            page_token TEXT,
            seen_at INTEGER NOT NULL,
            blocked_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(source_id,chat_id)
         );
         CREATE TABLE IF NOT EXISTS history_feishu_threads (
            source_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            watermark INTEGER NOT NULL DEFAULT 0,
            page_token TEXT,
            blocked_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(source_id,thread_id),
            FOREIGN KEY(source_id,chat_id) REFERENCES history_feishu_chats(source_id,chat_id)
         );",
    )?;
    let has_kind: bool = db.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('history_feishu_chats') WHERE name='kind')",
        [],
        |row| row.get(0),
    )?;
    if !has_kind {
        db.execute_batch("ALTER TABLE history_feishu_chats ADD COLUMN kind TEXT")?;
    }
    Ok(())
}

fn supported(kind: &str, account: &str) -> bool {
    match kind {
        "codex" | "claude-code" | "dsh" => account == "local",
        "feishu" => account.starts_with("feishu-"),
        _ => false,
    }
}

pub fn set_enabled(db: &mut Connection, source_id: &str, enabled: bool) -> Result<Value> {
    let tx = db.transaction()?;
    let (kind, account) = tx
        .query_row(
            "SELECT kind,account FROM ing_sources WHERE source_id=?1",
            [source_id],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()?
        .ok_or_else(|| Error::missing("source is unavailable"))?;
    if !supported(&kind, &account) {
        return Err(Error::invalid("这个来源只能从文件导入历史。"));
    }
    if enabled && kind == "feishu" {
        ingestion::bind_actor_in(&tx, source_id, "owner")?;
    }
    tx.execute(
        "INSERT INTO history_sources(source_id,enabled) VALUES(?1,?2)
         ON CONFLICT(source_id) DO UPDATE SET enabled=excluded.enabled,next_at=0,last_error=NULL",
        params![source_id, enabled],
    )?;
    tx.commit()?;
    Ok(json!({"source_id":source_id,"history_enabled":enabled}))
}

pub fn describe(db: &Connection, sources: &mut Value) -> Result<()> {
    let mut stmt = db.prepare(
        "SELECT enabled,last_scan,last_error,events,skipped_containers
         FROM history_sources WHERE source_id=?1",
    )?;
    for source in sources.as_array_mut().into_iter().flatten() {
        let id = source["source_id"].as_str().unwrap_or_default();
        let history = stmt
            .query_row([id], |row| {
                Ok(json!({
                    "enabled":row.get::<_,bool>(0)?,
                    "last_scan":row.get::<_,Option<i64>>(1)?,
                    "last_error":row.get::<_,Option<String>>(2)?,
                    "events":row.get::<_,i64>(3)?,
                    "skipped_containers":row.get::<_,i64>(4)?,
                }))
            })
            .optional()?;
        let mut history = history.unwrap_or_else(|| json!({"enabled":false,"events":0}));
        history["supported"] = json!(supported(
            source["type"].as_str().unwrap_or_default(),
            source["account"].as_str().unwrap_or_default(),
        ));
        source["history"] = history;
    }
    Ok(())
}

fn clock() -> i64 {
    chrono::Utc::now().timestamp()
}

pub fn scan_next(
    database: &Mutex<Connection>,
    profile: &str,
    stop: &impl Fn() -> bool,
) -> Result<bool> {
    let now = clock();
    let source = lock(database)?
        .query_row(
            "SELECT s.source_id,s.kind,s.account FROM history_sources h
             JOIN ing_sources s ON h.source_id=s.source_id
             JOIN ing_grants g ON h.source_id=g.source_id AND g.purpose='capture' AND g.active=1
             WHERE h.enabled=1 AND h.next_at<=?1 ORDER BY h.next_at,s.created_at LIMIT 1",
            [now],
            |row| {
                Ok(Source {
                    id: row.get(0)?,
                    kind: row.get(1)?,
                    account: row.get(2)?,
                })
            },
        )
        .optional()?;
    let Some(source) = source else {
        return Ok(false);
    };
    let result = if source.kind == "feishu" {
        scan_feishu(database, &source, profile, stop)
    } else {
        scan_files(database, &source, profile, stop)
    };
    if stop() {
        return Ok(false);
    }
    let (delay, error) = match result {
        Ok(Some(delay)) => (delay, None),
        Ok(None) => return Ok(false),
        Err(error) => (60, Some(error.code)),
    };
    let now = clock();
    lock(database)?.execute(
        "UPDATE history_sources SET next_at=?2,last_scan=?3,last_error=?4
         WHERE source_id=?1 AND enabled=1",
        params![source.id, now + delay, now, error],
    )?;
    Ok(true)
}

fn active(db: &Transaction<'_>, source_id: &str) -> Result<bool> {
    Ok(db.query_row(
        "SELECT EXISTS(SELECT 1 FROM history_sources h JOIN ing_grants g
         ON h.source_id=g.source_id AND g.purpose='capture'
         WHERE h.source_id=?1 AND h.enabled=1 AND g.active=1)",
        [source_id],
        |row| row.get(0),
    )?)
}

fn accept(
    db: &Transaction<'_>,
    source_id: &str,
    events: Vec<Value>,
    profile: &str,
    feishu_kind: Option<&str>,
) -> Result<()> {
    if events.is_empty() {
        return Ok(());
    }
    let cursor: String = db.query_row(
        "SELECT cursor FROM ing_sources WHERE source_id=?1",
        [source_id],
        |row| row.get(0),
    )?;
    let key = Uuid::new_v4().to_string();
    let page = json!({"source_id":source_id,"cursor":cursor,"next_cursor":key,"events":events});
    let receipt = ingestion::accept_in(db, "owner", &key, page, profile)?;
    if let Some(kind) = feishu_kind {
        crate::retrieval::record_feishu_kind(db, &receipt, kind)?;
    }
    db.execute(
        "UPDATE history_sources SET events=events+?2 WHERE source_id=?1",
        params![
            source_id,
            receipt["jobs_created"].as_i64().unwrap_or_default()
        ],
    )?;
    Ok(())
}

fn scan_files(
    database: &Mutex<Connection>,
    source: &Source,
    profile: &str,
    stop: &impl Fn() -> bool,
) -> Result<Option<i64>> {
    let state: HashMap<String, (u64, u64, bool)> = {
        let db = lock(database)?;
        let mut stmt =
            db.prepare("SELECT path,offset,size,complete FROM history_files WHERE source_id=?1")?;
        stmt.query_map([&source.id], |row| {
            Ok((
                row.get(0)?,
                (
                    row.get::<_, i64>(1)? as u64,
                    row.get::<_, i64>(2)? as u64,
                    row.get(3)?,
                ),
            ))
        })?
        .collect::<rusqlite::Result<_>>()?
    };
    let mut pages = 0;
    for path in history_hosts::files(&source.kind)? {
        if stop() {
            return Ok(None);
        }
        let Some(name) = path.to_str() else {
            continue;
        };
        let Ok(metadata) = path.symlink_metadata() else {
            continue;
        };
        if !metadata.is_file() {
            continue;
        }
        let size = metadata.len();
        let (offset, cached_size, complete) = state.get(name).copied().unwrap_or_default();
        if size == cached_size && complete {
            continue;
        }
        if pages == LOCAL_PAGES_PER_PASS {
            return Ok(Some(1));
        }
        let offset = if size < cached_size { 0 } else { offset };
        let page = match history_hosts::read_page(&source.kind, &path, offset, EVENTS_PER_PAGE) {
            Ok(page) => page,
            Err(error) if error.status == 404 => continue,
            Err(error) => return Err(error),
        };
        let mut db = lock(database)?;
        let tx = db.transaction()?;
        if !active(&tx, &source.id)? {
            return Ok(None);
        }
        accept(&tx, &source.id, page.events, profile, None)?;
        tx.execute(
            "INSERT INTO history_files VALUES(?1,?2,?3,?4,?5)
             ON CONFLICT(source_id,path) DO UPDATE SET
                offset=excluded.offset,size=excluded.size,complete=excluded.complete",
            params![source.id, name, page.offset as i64, size as i64, page.eof],
        )?;
        tx.commit()?;
        pages += 1;
    }
    Ok(Some(if pages == 0 { 45 } else { 2 }))
}

fn scan_feishu(
    database: &Mutex<Connection>,
    source: &Source,
    profile: &str,
    stop: &impl Fn() -> bool,
) -> Result<Option<i64>> {
    let (end, token, listed): (i64, Option<String>, bool) = {
        let db = lock(database)?;
        db.execute(
            "INSERT OR IGNORE INTO history_feishu_passes(source_id,ends_at) VALUES(?1,?2)",
            params![source.id, clock()],
        )?;
        db.query_row(
            "SELECT ends_at,list_token,listed FROM history_feishu_passes WHERE source_id=?1",
            [&source.id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )?
    };
    let account = history_feishu::account(&source.account, stop)?;
    if !listed {
        let page = match account.chat_list("p2p,group", token.as_deref(), stop) {
            Err(error) if error.code == "feishu_cursor_expired" && token.is_some() => {
                lock(database)?.execute(
                    "UPDATE history_feishu_passes SET list_token=NULL WHERE source_id=?1",
                    [&source.id],
                )?;
                return Ok(Some(2));
            }
            result => result?,
        };
        let mut db = lock(database)?;
        let tx = db.transaction()?;
        if !active(&tx, &source.id)? {
            return Ok(None);
        }
        for chat in page.chats {
            let kind = match chat.mode.as_str() {
                "p2p" => Some("dm"),
                "group" | "topic" => Some("group"),
                _ => None,
            };
            tx.execute(
                "INSERT INTO history_feishu_chats(source_id,chat_id,seen_at,kind) VALUES(?1,?2,?3,?4)
                 ON CONFLICT(source_id,chat_id) DO UPDATE SET seen_at=excluded.seen_at, kind=coalesce(excluded.kind,kind)",
                params![source.id, chat.id, end, kind],
            )?;
        }
        tx.execute(
            "UPDATE history_feishu_passes SET list_token=?2,listed=?3 WHERE source_id=?1",
            params![source.id, page.next_token, page.next_token.is_none()],
        )?;
        tx.commit()?;
        return Ok(Some(1));
    }
    let chat: Option<(String, i64, Option<String>, Option<String>)> = lock(database)?
        .query_row(
            "SELECT chat_id,watermark,page_token,kind FROM history_feishu_chats
             WHERE source_id=?1 AND watermark<?2 AND seen_at=?2 AND blocked_at<>?2
             ORDER BY page_token IS NULL,watermark DESC,chat_id LIMIT 1",
            params![source.id, end],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()?;
    let Some((chat, start, token, kind)) = chat else {
        return scan_feishu_thread(database, source, profile, &account, end, stop);
    };
    let result = account.messages(&chat, (start - 60).max(0), end, token.as_deref(), stop);
    let (page, skip) = feishu_page(result, token.as_deref())?;
    let mut db = lock(database)?;
    let tx = db.transaction()?;
    if !active(&tx, &source.id)? {
        return Ok(None);
    }
    let (next, watermark) = if let Some(page) = page {
        accept(&tx, &source.id, page.events, profile, kind.as_deref())?;
        for thread in page.thread_ids {
            tx.execute(
                "INSERT OR IGNORE INTO history_feishu_threads(source_id,chat_id,thread_id)
                 VALUES(?1,?2,?3)",
                params![source.id, chat, thread],
            )?;
        }
        let watermark = if page.next_token.is_none() {
            end
        } else {
            start
        };
        (page.next_token, watermark)
    } else {
        (None, start)
    };
    tx.execute(
        "UPDATE history_feishu_chats SET page_token=?3,watermark=?4,blocked_at=?5
         WHERE source_id=?1 AND chat_id=?2",
        params![source.id, chat, next, watermark, if skip { end } else { 0 }],
    )?;
    record_gap(&tx, &source.id, skip)?;
    tx.commit()?;
    Ok(Some(1))
}

fn scan_feishu_thread(
    database: &Mutex<Connection>,
    source: &Source,
    profile: &str,
    account: &history_feishu::Account,
    end: i64,
    stop: &impl Fn() -> bool,
) -> Result<Option<i64>> {
    let thread: Option<(String, i64, Option<String>, Option<String>)> = lock(database)?
        .query_row(
            "SELECT t.thread_id,t.watermark,t.page_token,c.kind FROM history_feishu_threads t
             JOIN history_feishu_chats c ON t.source_id=c.source_id AND t.chat_id=c.chat_id
             WHERE t.source_id=?1 AND t.watermark<?2 AND c.seen_at=?2 AND t.blocked_at<>?2
             ORDER BY t.page_token IS NULL,t.watermark DESC,t.thread_id LIMIT 1",
            params![source.id, end],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()?;
    let Some((thread, start, token, kind)) = thread else {
        lock(database)?.execute(
            "DELETE FROM history_feishu_passes WHERE source_id=?1",
            [&source.id],
        )?;
        return Ok(Some(300));
    };
    let (page, skip) = feishu_page(
        account.thread_messages(&thread, token.as_deref(), stop),
        token.as_deref(),
    )?;
    let mut db = lock(database)?;
    let tx = db.transaction()?;
    if !active(&tx, &source.id)? {
        return Ok(None);
    }
    let (next, watermark) = if let Some(page) = page {
        let since = (start - 60).max(0);
        let next = page
            .next_token
            .filter(|_| page.oldest_timestamp.is_none_or(|oldest| oldest >= since));
        accept(&tx, &source.id, page.events, profile, kind.as_deref())?;
        let watermark = if next.is_none() { end } else { start };
        (next, watermark)
    } else {
        (None, start)
    };
    tx.execute(
        "UPDATE history_feishu_threads SET page_token=?3,watermark=?4,blocked_at=?5
         WHERE source_id=?1 AND thread_id=?2",
        params![
            source.id,
            thread,
            next,
            watermark,
            if skip { end } else { 0 }
        ],
    )?;
    record_gap(&tx, &source.id, skip)?;
    tx.commit()?;
    Ok(Some(1))
}

fn feishu_page(
    result: Result<history_feishu::MessagePage>,
    token: Option<&str>,
) -> Result<(Option<history_feishu::MessagePage>, bool)> {
    match result {
        Ok(page) => Ok((Some(page), false)),
        Err(error) if error.code == "feishu_cursor_expired" && token.is_some() => Ok((None, false)),
        Err(error) if error.code == "feishu_container_unavailable" => Ok((None, true)),
        Err(error) => Err(error),
    }
}

fn record_gap(db: &Transaction<'_>, source_id: &str, skip: bool) -> Result<()> {
    if skip {
        db.execute(
            "UPDATE history_sources SET skipped_containers=skipped_containers+1 WHERE source_id=?1",
            [source_id],
        )?;
    }
    Ok(())
}
