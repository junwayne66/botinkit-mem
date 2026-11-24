use super::*;

#[test]
fn ledger_filters_receipts_and_counts_beyond_the_first_page() -> Result<()> {
    let mut db = Connection::open_in_memory()?;
    db.execute_batch("PRAGMA foreign_keys=ON")?;
    migrate(&db)?;
    let registration = register(
        &mut db,
        "ledger-fixture",
        json!({
            "host":"fixture-host","destination":"local-test"
        }),
    )?;
    let client = registration["client_id"].as_str().unwrap();
    let now = chrono::Utc::now().to_rfc3339();
    for index in 0..(LEDGER_PAGE_SIZE + 3) {
        db.execute(
            "INSERT INTO context_deliveries(id,client_id,session,epoch,route,policy,snapshot,package,
                mode,profile,package_state,required_budget,provided,created_at)
             VALUES(?1,?2,?1,'epoch','local',1,'s','p','replace','balanced','provided',0,'[]',?3)",
            params![format!("delivery-{index}"), client, now],
        )?;
    }
    for (event, delivery, kind, complete) in [
        ("loaded-a", "delivery-0", "loaded", 0),
        ("loaded-b", "delivery-1", "loaded", 1),
        ("used-b", "delivery-1", "used", 1),
    ] {
        db.execute(
            "INSERT INTO context_receipts(client_id,event_id,delivery_id,request_hash,kind,result,complete)
             VALUES(?1,?2,?3,'fixture',?4,'{}',?5)",
            params![client, event, delivery, kind, complete],
        )?;
    }
    let first = delivery_ledger(&db, Some("fixture-host"), None, None, 1)?;
    assert_eq!(first["total"], LEDGER_PAGE_SIZE + 3);
    assert_eq!(first["summary"]["loaded"], 2);
    assert_eq!(first["summary"]["used"], 1);
    assert_eq!(
        first["deliveries"].as_array().unwrap().len(),
        LEDGER_PAGE_SIZE
    );
    let second = delivery_ledger(&db, None, None, None, 2)?;
    assert_eq!(second["deliveries"].as_array().unwrap().len(), 3);
    let partial = delivery_ledger(&db, None, Some("partial"), Some("pending"), 1)?;
    assert_eq!(partial["deliveries"][0]["delivery_id"], "delivery-0");
    let adopted = delivery_ledger(&db, None, Some("observed"), Some("observed"), 1)?;
    assert_eq!(adopted["deliveries"][0]["delivery_id"], "delivery-1");
    assert!(delivery_ledger(&db, None, None, None, 0).is_err());
    Ok(())
}
