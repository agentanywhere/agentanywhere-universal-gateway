//! Keeps the snapshot current by polling the control plane, and fails closed when it cannot.
//!
//! Every `interval` the data plane asks the Python gateway for anything newer than the revision
//! it holds. A change, such as a revoked key, arrives within about one interval. If no answer
//! has arrived for `max_staleness`, the data plane stops serving anything natively and hands
//! every request to the Python gateway until the link recovers, so a revocation it could not
//! hear about is never honoured for longer than that.
use crate::proxy::State;
use crate::report::{self, Delivery};
use crate::snapshot::{Parsed, Readiness, Snapshot};
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::{Request, StatusCode};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::TokioExecutor;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub struct Link {
    last_ok_ms: AtomicU64,
    max_staleness_ms: u64,
    /// The latest attempt to reach the control plane failed.
    down: AtomicBool,
    /// The control plane's answer to `/ready`, as of its latest answer. Forgotten when an attempt fails.
    ready: Mutex<Option<Arc<Readiness>>>,
}

fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

impl Link {
    pub fn new(max_staleness: Duration) -> Self {
        Link {
            last_ok_ms: AtomicU64::new(0),
            max_staleness_ms: max_staleness.as_millis() as u64,
            down: AtomicBool::new(false),
            ready: Mutex::new(None),
        }
    }

    /// True while the last successful answer is recent enough to trust. False before the first one.
    pub fn fresh(&self) -> bool {
        let last = self.last_ok_ms.load(Ordering::Relaxed);
        last != 0 && now_ms().saturating_sub(last) <= self.max_staleness_ms
    }

    /// True while the control plane is known to be answering: the latest attempt succeeded, and
    /// recently. Stricter than `fresh`, which rides out a failed attempt or two; this is what
    /// speaking for the control plane's health takes.
    pub fn up(&self) -> bool {
        !self.down.load(Ordering::Relaxed) && self.fresh()
    }

    /// Milliseconds since the control plane last answered, if it ever has.
    pub fn age_ms(&self, now_ms: u64) -> Option<u64> {
        let last = self.last_ok_ms.load(Ordering::Relaxed);
        (last != 0).then(|| now_ms.saturating_sub(last))
    }

    /// The control plane's answer to `/ready`, while it is known to be answering.
    pub fn readiness(&self) -> Option<Arc<Readiness>> {
        self.up().then(|| self.ready.lock().unwrap().clone()).flatten()
    }

    fn mark_ok(&self, ready: Option<Readiness>) {
        *self.ready.lock().unwrap() = ready.map(Arc::new);
        self.last_ok_ms.store(now_ms(), Ordering::Relaxed);
        self.down.store(false, Ordering::Relaxed);
    }

    fn mark_failed(&self) {
        self.down.store(true, Ordering::Relaxed);
        *self.ready.lock().unwrap() = None;
    }
}

async fn fetch(client: &Client<HttpConnector, Full<Bytes>>, control: &str, token: &str, since: Option<&str>) -> Result<String, String> {
    let url = match since {
        Some(rev) => format!("{control}/internal/dataplane/snapshot?since={rev}"),
        None => format!("{control}/internal/dataplane/snapshot"),
    };
    let req = Request::builder()
        .uri(url)
        .header("x-dataplane-token", token)
        .body(Full::<Bytes>::default())
        .map_err(|e| e.to_string())?;
    let resp = tokio::time::timeout(Duration::from_secs(5), client.request(req))
        .await
        .map_err(|_| "timed out".to_string())?
        .map_err(|e| e.to_string())?;
    let status = resp.status();
    let body = resp.into_body().collect().await.map_err(|e| e.to_string())?.to_bytes();
    if status != StatusCode::OK {
        return Err(format!("control plane answered {status}"));
    }
    String::from_utf8(body.to_vec()).map_err(|e| e.to_string())
}

/// The one conversation with the control plane: ask for the snapshot, then, when one is due,
/// report what was served. One after the other and in that order, so that a tenant's usage as
/// the database had it before a report can never arrive after that report was acknowledged and
/// make the usage look unused.
pub async fn run(state: Arc<State>, token: String, interval: Duration, report_interval: Duration) {
    let mut connector = HttpConnector::new();
    connector.set_nodelay(true);
    let client: Client<HttpConnector, Full<Bytes>> = Client::builder(TokioExecutor::new()).build(connector);
    let sync = state.sync.as_ref().expect("sync loop needs sync state").clone();
    let (mut healthy, mut reporting) = (false, true);
    let mut reported = Instant::now();
    loop {
        let current = state.snapshot.load_full();
        let outcome = fetch(&client, &state.control.text, &token, current.revision.as_deref())
            .await
            .and_then(|text| Snapshot::parse(&text, Some(&current)));
        match outcome {
            Ok(parsed) => {
                let ready = match parsed {
                    Parsed::Full(new, extras) => {
                        eprintln!(
                            "dataplane: snapshot updated ({} keys, revision {})",
                            new.keys.len(),
                            new.revision.as_deref().unwrap_or("-")
                        );
                        if let Some(usage) = extras.usage {
                            usage.apply(&new);
                        }
                        state.replace(new);
                        extras.ready
                    }
                    Parsed::Unchanged(extras) => {
                        if let Some(usage) = extras.usage {
                            usage.apply(&current);
                        }
                        extras.ready
                    }
                };
                sync.mark_ok(ready);
                if !healthy {
                    eprintln!("dataplane: control plane link is up");
                    healthy = true;
                }
            }
            Err(e) => {
                // What workers learned from the control plane's answers may no longer hold once it
                // has been away (it may have been restarted with other settings): make them forget.
                state.generation.fetch_add(1, Ordering::Release);
                sync.mark_failed();
                if healthy {
                    eprintln!("dataplane: control plane link lost ({e}); native serving stops after the staleness limit");
                    healthy = false;
                }
            }
        }
        drop(current);
        if let Some(reporter) = state.reporter.as_ref().filter(|_| reported.elapsed() >= report_interval) {
            reported = Instant::now();
            match report::deliver(&state, reporter, &client, &token).await {
                Delivery::Nothing => {}
                Delivery::Delivered => reporting = true,
                Delivery::Failed => {
                    state.counts.reports_failed.fetch_add(1, Ordering::Relaxed);
                    if reporting {
                        eprintln!("dataplane: report not delivered; counts are kept for the next one");
                        reporting = false;
                    }
                }
            }
        }
        tokio::time::sleep(interval).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stale_before_the_first_answer_and_fresh_right_after() {
        let s = Link::new(Duration::from_secs(5));
        assert!(!s.fresh() && !s.up());
        s.mark_ok(None);
        assert!(s.fresh() && s.up());
    }

    #[test]
    fn a_failed_attempt_ends_speaking_for_the_control_plane_at_once() {
        let s = Link::new(Duration::from_secs(5));
        s.mark_ok(Some(Readiness { status: 200, body_json: "{}".into() }));
        assert_eq!(s.readiness().map(|r| r.status), Some(200));
        s.mark_failed();
        // Still fresh enough to serve keys from the snapshot, but not to vouch for the other side.
        assert!(s.fresh() && !s.up() && s.readiness().is_none());
        s.mark_ok(None);
        assert!(s.up() && s.readiness().is_none());
    }

    #[test]
    fn goes_stale_after_the_limit() {
        let s = Link::new(Duration::from_millis(0));
        s.last_ok_ms.store(now_ms() - 10, Ordering::Relaxed);
        assert!(!s.fresh());
    }
}
