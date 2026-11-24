use super::*;
use std::net::SocketAddr;
use std::sync::mpsc::{Receiver, Sender, channel};
use std::time::{Duration, Instant};
use tempfile::TempDir;

/// A data root plus an `Instance` that points at it. The `TempDir` must outlive
/// the instance, so tests keep the fixture rather than the instance alone.
struct Fixture {
    _dir: TempDir,
    instance: Instance,
}

fn fixture() -> Fixture {
    let dir = TempDir::new().expect("temporary data root");
    fs::create_dir_all(dir.path().join("run")).expect("run directory");
    let instance = Instance {
        root: dir.path().to_path_buf(),
        id: "inst-lock-test".to_string(),
        owner_token: "owner-token".to_string(),
    };
    Fixture {
        _dir: dir,
        instance,
    }
}

/// A second handle on the same data root. The lock lives on the file path, so
/// this contends exactly like another process would, without needing `Instance`
/// to derive `Clone` in the product code.
fn same_root(instance: &Instance) -> Instance {
    Instance {
        root: instance.root.clone(),
        id: instance.id.clone(),
        owner_token: instance.owner_token.clone(),
    }
}

/// Holds `run/service.lock` in a thread until told to release it, so a test can
/// observe the daemon-side behaviour during a real contention window. `started`
/// fires only after the lock is actually owned, which removes the race between
/// "the holder has the lock" and "the test starts measuring".
struct Holder {
    started: Receiver<()>,
    release: Sender<()>,
    handle: std::thread::JoinHandle<Result<()>>,
}

fn hold_lock(instance: &Instance) -> Holder {
    let owned = same_root(instance);
    let (started_tx, started) = channel();
    let (release_tx, release) = channel();
    let handle = thread::spawn(move || -> Result<()> {
        let file = lock_service(&owned)?;
        started_tx.send(()).expect("signal acquired");
        // Keep the lock until the test releases it, or 10s as a safety net so a
        // crashed assertion cannot leave a stray thread holding it forever.
        let _ = release.recv_timeout(Duration::from_secs(10));
        drop(file);
        Ok(())
    });
    Holder {
        started,
        release: release_tx,
        handle,
    }
}

impl Holder {
    fn wait_until_holding(&self) {
        self.started
            .recv_timeout(Duration::from_secs(10))
            .expect("holder thread never acquired the lock");
    }

    fn finish(self) {
        let _ = self.release.send(());
        self.handle.join().expect("holder thread panicked").unwrap();
    }
}

/// The daemon must survive a CLI liveness probe that owns the lock while it
/// starts: that is the race that killed a starting service about 4% of the time
/// and made the whole Python suite order-dependent.
#[test]
fn daemon_lock_waits_out_a_transient_probe() -> Result<()> {
    let fixture = fixture();
    let instance = same_root(&fixture.instance);
    let (started_tx, started) = channel();
    let (release_tx, release) = channel();
    let handle = thread::spawn(move || -> Result<()> {
        let file = lock_service(&instance)?;
        started_tx.send(()).expect("signal acquired");
        // A real probe releases within a millisecond; 120ms is far beyond it and
        // still inside the daemon budget, so this models a transient holder.
        let _ = release.recv_timeout(Duration::from_millis(120));
        drop(file);
        Ok(())
    });

    started
        .recv_timeout(Duration::from_secs(10))
        .expect("holder thread never acquired the lock");
    let started_at = Instant::now();
    let lock = lock_service_for_daemon(&fixture.instance, Duration::from_secs(2))?;
    let waited = started_at.elapsed();
    assert!(
        waited < Duration::from_secs(2),
        "daemon lock waited {waited:?}, over budget"
    );
    let _ = release_tx.send(());
    handle.join().expect("holder thread panicked")?;
    drop(lock);
    Ok(())
}

/// A real second service must still be refused, and refused with the original
/// error, once the budget expires rather than retried forever.
#[test]
fn daemon_lock_gives_up_on_a_persistent_owner() -> Result<()> {
    let fixture = fixture();
    let holder = hold_lock(&fixture.instance);
    holder.wait_until_holding();

    let started_at = Instant::now();
    let error = lock_service_for_daemon(&fixture.instance, Duration::from_millis(300)).unwrap_err();
    let waited = started_at.elapsed();

    assert_eq!(error.status, 409);
    assert_eq!(error.message, "a service already owns this data root");
    assert!(
        waited >= Duration::from_millis(280),
        "daemon gave up after only {waited:?} instead of exhausting the budget"
    );
    holder.finish();
    Ok(())
}

/// The probe side keeps its one-shot semantics: `lock_service` and `discover`
/// must report "already running" immediately instead of waiting out a budget,
/// otherwise every CLI call would stall for two seconds on a live service.
#[test]
fn probe_semantics_stay_immediate_while_running() -> Result<()> {
    let fixture = fixture();
    let address = SocketAddr::from(([127, 0, 0, 1], 61234));
    publish(&fixture.instance, address)?;
    let holder = hold_lock(&fixture.instance);
    holder.wait_until_holding();

    let started_at = Instant::now();
    let conflict = lock_service(&fixture.instance).unwrap_err();
    let lock_wait = started_at.elapsed();
    assert_eq!(conflict.status, 409);
    assert!(
        lock_wait < Duration::from_millis(150),
        "probe lock waited {lock_wait:?}; it must stay non-blocking"
    );

    let started_at = Instant::now();
    let discovered = discover(&fixture.instance)?;
    let discover_wait = started_at.elapsed();
    assert_eq!(discovered, address);
    assert!(
        discover_wait < Duration::from_millis(150),
        "discover waited {discover_wait:?}; it must stay non-blocking"
    );

    holder.finish();
    Ok(())
}
