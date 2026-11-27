use crate::error::{Error, Result};
use rusqlite::functions::FunctionFlags;
use rusqlite::{Connection, TransactionBehavior};

pub const WRITE_EPOCH: i64 = 2;

pub fn read(db: &Connection) -> Result<i64> {
    Ok(db.pragma_query_value(None, "user_version", |row| row.get(0))?)
}

pub fn prepare_writer(db: &Connection) -> Result<()> {
    let stored = read(db)?;
    if !(0..=WRITE_EPOCH).contains(&stored) {
        return Err(Error::new(
            409,
            "unsupported_authority_epoch",
            format!("authority write epoch {stored} requires a compatible bmem binary"),
        ));
    }
    db.create_scalar_function(
        "bmem_writer_epoch",
        0,
        FunctionFlags::SQLITE_UTF8
            | FunctionFlags::SQLITE_DETERMINISTIC
            | FunctionFlags::SQLITE_INNOCUOUS,
        |_| Ok(WRITE_EPOCH),
    )?;
    Ok(())
}

pub fn install(db: &mut Connection) -> Result<()> {
    if read(db)? == WRITE_EPOCH {
        return Ok(());
    }
    // The authority sequence is initialized by every existing daemon before
    // serving requests. Resolving the function during that INSERT prevents an
    // older daemon from opening this authority as a writer after migration.
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    for (name, action) in [
        ("insert", "INSERT"),
        ("update", "UPDATE"),
        ("delete", "DELETE"),
    ] {
        tx.execute_batch(&format!(
            "CREATE TRIGGER IF NOT EXISTS bmem_epoch_{WRITE_EPOCH}_{name}
             BEFORE {action} ON authority_changes
             WHEN bmem_writer_epoch() < {WRITE_EPOCH}
             BEGIN SELECT RAISE(ABORT, 'bmem authority writer upgrade required'); END;"
        ))?;
    }
    tx.pragma_update(None, "user_version", WRITE_EPOCH)?;
    tx.commit()?;
    Ok(())
}
