//! Keeps the snapshot current by polling the control plane, and fails closed when it cannot.
//!
//! Every `interval` the data plane asks the Python gateway for anything newer than the revision
//! it holds. A change, such as a revoked key, arrives within about one interval. If no answer
//! has arrived for `max_staleness`, the data plane stops serving anything natively and hands
//! every request to the Python gateway until the link recovers, so a revocation it could not
//! hear about is never honoured for longer than that.
use crate::proxy::State;
use crate::report::{self, Delivery};
use crate::snapshot::{Parsed, Snapshot};
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::{Request, StatusCode};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::TokioExecutor;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub struct Link {
    last_ok_ms: AtomicU64,
    max_staleness_ms: u64,
}

fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

impl Link {
    pub fn new(max_staleness: Duration) -> Self {
        Link { last_ok_ms: AtomicU64::new(0), max_staleness_ms: max_staleness.as_millis() as u64 }
    }

    /// True while the last successful answer is recent enough to trust. False before the first one.
    pub fn fresh(&self) -> bool {
        let last = self.last_ok_ms.load(Ordering::Relaxed);
        last != 0 && now_ms().saturating_sub(last) <= self.max_staleness_ms
    }

    fn mark_ok(&self) {
        self.last_ok_ms.store(now_ms(), Ordering::Relaxed);
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
                match parsed {
                    Parsed::Full(new, usage) => {
                        eprintln!(
                            "dataplane: snapshot updated ({} keys, revision {})",
                            new.keys.len(),
                            new.revision.as_deref().unwrap_or("-")
                        );
                        if let Some(usage) = usage {
                            usage.apply(&new);
                        }
                        state.replace(new);
                    }
                    Parsed::Unchanged(Some(usage)) => usage.apply(&current),
                    Parsed::Unchanged(None) => {}
                }
                sync.mark_ok();
                if !healthy {
                    eprintln!("dataplane: control plane link is up");
                    healthy = true;
                }
            }
            Err(e) => {
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
        assert!(!s.fresh());
        s.mark_ok();
        assert!(s.fresh());
    }

    #[test]
    fn goes_stale_after_the_limit() {
        let s = Link::new(Duration::from_millis(0));
        s.last_ok_ms.store(now_ms() - 10, Ordering::Relaxed);
        assert!(!s.fresh());
    }
}
