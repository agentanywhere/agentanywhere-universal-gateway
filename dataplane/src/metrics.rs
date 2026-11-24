//! The data plane's own metrics, in the Prometheus text format, on an address of its own
//! (`--metrics-listen`). `/metrics` on the address callers use stays the Python gateway's: that
//! is the gateway's one registry, which this side's reports already feed. These are the data
//! plane's own health: how much it serves itself and how much it hands over, and whether its
//! snapshot is current enough to serve from.
//!
//! Each worker counts into a shard of its own, on a cache line of its own, so counting a
//! request never waits on another core; the shards are summed when the metrics are read.

use crate::proxy::State;
use bytes::Bytes;
use http_body_util::Full;
use hyper::header::{HeaderValue, CONTENT_TYPE};
use hyper::{Method, Request, Response, StatusCode};
use std::fmt::Write as _;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

/// One worker's counts.
#[derive(Default)]
#[repr(align(128))]
struct Shard {
    requests: AtomicU64,
    handed_over: AtomicU64,
    control_unreachable: AtomicU64,
}

/// What the workers have counted, one shard each.
pub struct Counts {
    shards: Box<[Shard]>,
    /// Reports of what was served that the control plane did not take.
    pub reports_failed: AtomicU64,
}

impl Counts {
    pub fn new(workers: usize) -> Counts {
        Counts { shards: (0..workers.max(1)).map(|_| Shard::default()).collect(), reports_failed: AtomicU64::new(0) }
    }

    fn shard(&self, worker: usize) -> &Shard {
        &self.shards[worker % self.shards.len()]
    }

    /// A request reached this side.
    pub fn request(&self, worker: usize) {
        self.shard(worker).requests.fetch_add(1, Ordering::Relaxed);
    }

    /// A request was handed to the Python gateway; `answered` says whether it answered.
    pub fn handed_over(&self, worker: usize, answered: bool) {
        let shard = self.shard(worker);
        shard.handed_over.fetch_add(1, Ordering::Relaxed);
        if !answered {
            shard.control_unreachable.fetch_add(1, Ordering::Relaxed);
        }
    }

    fn sum(&self, of: impl Fn(&Shard) -> &AtomicU64) -> u64 {
        self.shards.iter().map(|s| of(s).load(Ordering::Relaxed)).sum()
    }
}

/// The metrics as text, read from the state as it stands. `now_ms` is the wall clock.
pub fn render(state: &State, now_ms: u64) -> String {
    let counts = &state.counts;
    let mut out = String::with_capacity(1536);
    let mut metric = |name: &str, kind: &str, help: &str, value: String| {
        let _ = writeln!(out, "# HELP {name} {help}\n# TYPE {name} {kind}\n{name} {value}");
    };
    let requests = counts.sum(|s| &s.requests);
    let handed = counts.sum(|s| &s.handed_over);
    metric("dataplane_requests_total", "counter", "Requests that reached the data plane.", requests.to_string());
    metric("dataplane_requests_handed_over_total", "counter", "Requests handed to the Python gateway as they came.", handed.to_string());
    metric(
        "dataplane_requests_served_total",
        "counter",
        "Requests the data plane answered itself (served, refused or relayed).",
        requests.saturating_sub(handed).to_string(),
    );
    metric(
        "dataplane_control_unreachable_total",
        "counter",
        "Requests handed over that the Python gateway did not answer.",
        counts.sum(|s| &s.control_unreachable).to_string(),
    );
    metric(
        "dataplane_snapshot_updates_total",
        "counter",
        "Times the snapshot was replaced.",
        state.generation.load(Ordering::Relaxed).to_string(),
    );
    metric("dataplane_reports_failed_total", "counter", "Reports the control plane did not take.", counts.reports_failed.load(Ordering::Relaxed).to_string());
    let link = state.sync.as_deref();
    metric(
        "dataplane_control_link_up",
        "gauge",
        "1 while the control plane answers the snapshot poll.",
        u8::from(link.map(|l| l.up()).unwrap_or(false)).to_string(),
    );
    metric(
        "dataplane_snapshot_fresh",
        "gauge",
        "1 while the snapshot is current enough to serve natively.",
        u8::from(link.map(|l| l.fresh()).unwrap_or(true)).to_string(),
    );
    if let Some(age) = link.and_then(|l| l.age_ms(now_ms)) {
        metric("dataplane_snapshot_age_seconds", "gauge", "Seconds since the control plane last answered.", format!("{:.3}", age as f64 / 1000.0));
    }
    metric("dataplane_workers", "gauge", "Worker threads serving requests.", counts.shards.len().to_string());
    let _ = writeln!(
        out,
        "# HELP dataplane_build_info The data plane's version.\n# TYPE dataplane_build_info gauge\ndataplane_build_info{{version=\"{}\"}} 1",
        env!("CARGO_PKG_VERSION")
    );
    out
}

fn answer(state: &State, req: &Request<hyper::body::Incoming>) -> Response<Full<Bytes>> {
    let now_ms = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0);
    let (status, body, kind) = match (req.method(), req.uri().path()) {
        (&Method::GET, "/metrics") => (StatusCode::OK, render(state, now_ms), "text/plain; version=0.0.4; charset=utf-8"),
        (_, "/metrics") => (StatusCode::METHOD_NOT_ALLOWED, String::new(), "text/plain; charset=utf-8"),
        _ => (StatusCode::NOT_FOUND, String::new(), "text/plain; charset=utf-8"),
    };
    let mut resp = Response::new(Full::new(Bytes::from(body)));
    *resp.status_mut() = status;
    resp.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static(kind));
    resp
}

/// Serve the metrics on `addr`, on a thread of their own.
pub fn start(addr: SocketAddr, state: Arc<State>) -> std::io::Result<SocketAddr> {
    let listener = std::net::TcpListener::bind(addr)?;
    listener.set_nonblocking(true)?;
    let bound = listener.local_addr()?;
    std::thread::Builder::new().name("metrics".into()).spawn(move || {
        let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().expect("metrics runtime");
        rt.block_on(async move {
            let listener = tokio::net::TcpListener::from_std(listener).expect("metrics listener");
            loop {
                let Ok((stream, _)) = listener.accept().await else { continue };
                let state = state.clone();
                tokio::spawn(async move {
                    let svc = hyper::service::service_fn(move |req| {
                        let resp = answer(&state, &req);
                        async move { Ok::<_, std::convert::Infallible>(resp) }
                    });
                    let _ = hyper::server::conn::http1::Builder::new().serve_connection(hyper_util::rt::TokioIo::new(stream), svc).await;
                });
            }
        });
    })?;
    Ok(bound)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_shards_are_summed_and_each_worker_counts_into_its_own() {
        let counts = Counts::new(3);
        for worker in [0, 1, 2, 2, 7] {
            counts.request(worker);
        }
        counts.handed_over(1, true);
        counts.handed_over(2, false);
        assert_eq!(counts.sum(|s| &s.requests), 5);
        assert_eq!(counts.sum(|s| &s.handed_over), 2);
        assert_eq!(counts.sum(|s| &s.control_unreachable), 1);
        // Worker 7 has no shard of its own: it counts into one of the three.
        assert_eq!(counts.shard(7).requests.load(Ordering::Relaxed), 2);
        assert_eq!(std::mem::align_of::<Shard>(), 128);
    }
}
