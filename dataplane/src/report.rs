//! Tells the control plane what the data plane served, so the console, metrics, SLOs and logs
//! stay complete.
//!
//! Counts and latencies are aggregated here per (method, path pattern, status) in the same
//! histogram buckets the Python gateway uses and sent exactly once per interval, so their cost
//! does not grow with traffic. Individual request-log rows are sent for every error and for
//! successes up to a rate cap; the rest are counted as skipped (and shown in a metric), never
//! silently lost. If a report cannot be delivered the aggregates are kept for the next one.
//!
//! Events are the things the control plane announces by webhook and alert: the circuit breaker
//! opening or closing, and a key going over its rate limit (one event per key and window per
//! report, carrying how many requests were refused).
use crate::limits::Window;
use crate::proxy::State;
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::{Method, Request};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::TokioExecutor;
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::net::IpAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// Upper bounds of the latency histogram, in seconds: the Python gateway's own buckets.
pub const BOUNDS: [f64; 8] = [0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0];
const SHARDS: usize = 32;
const MAX_LABELS: usize = 200;
const MAX_SEGMENTS: usize = 4;
const MAX_EVENTS: usize = 1000;

#[derive(Hash, PartialEq, Eq, Clone)]
struct AggKey {
    method: String,
    label: String,
    status: u16,
    /// Set only for requests the data plane rejected (429): the key the limit hit.
    rejected_key: Option<String>,
}

#[derive(Clone, Default)]
struct Agg {
    sample_path: String,
    /// Per bucket (the eight bounds, then +Inf): number of requests and their summed seconds.
    buckets: [(u64, f64); 9],
}

#[derive(Serialize)]
struct AggOut {
    method: String,
    /// One real path from the group; the control plane derives its own label from it.
    path: String,
    status: u16,
    rejected: bool,
    client_id: Option<String>,
    /// `[count, seconds]` per bucket.
    buckets: Vec<[f64; 2]>,
}

#[derive(Serialize, Clone)]
pub struct Row {
    pub request_id: String,
    pub method: String,
    pub path: String,
    pub client_ip: String,
    pub api_key_id: Option<String>,
    pub status: u16,
    pub duration_ms: f64,
    pub user_agent: Option<String>,
    pub error: Option<String>,
}

#[derive(Serialize, Clone, Debug, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Event {
    CircuitBreaker {
        state: &'static str,
        previous_state: &'static str,
        backend_url: String,
        failure_count: u32,
        failure_threshold: u32,
        opened_at: f64,
        timeout_seconds: u64,
    },
    RateLimitExceeded {
        api_key_id: String,
        client_ip: String,
        limit: u32,
        window: &'static str,
        /// The first refused path; `count` says how many requests the event stands for.
        path: String,
        count: u64,
    },
}

struct Limited {
    client_ip: String,
    limit: u32,
    path: String,
    count: u64,
}

#[derive(Serialize)]
struct KeyUse {
    id: String,
    at: u64,
}

#[derive(Serialize)]
struct Report {
    keys_used: Vec<KeyUse>,
    aggregates: Vec<AggOut>,
    rows: Vec<Row>,
    rows_skipped: u64,
    events: Vec<Event>,
}

pub struct Reporter {
    shards: Vec<Mutex<HashMap<AggKey, Agg>>>,
    /// Latest use (epoch seconds) per key id, sharded like the aggregates.
    used: Vec<Mutex<HashMap<String, u64>>>,
    labels: RwLock<HashSet<String>>,
    rows: Mutex<Vec<Row>>,
    row_window: Window,
    max_rows_per_sec: u32,
    row_cap: usize,
    skipped_rows: AtomicU64,
    events: Mutex<Vec<Event>>,
    /// Refusals since the last report, per (key id, window).
    limited: Mutex<HashMap<(String, &'static str), Limited>>,
}

pub fn bucket_index(seconds: f64) -> usize {
    BOUNDS.iter().position(|b| seconds <= *b).unwrap_or(BOUNDS.len())
}

/// True for a path segment that is probably an identifier, so `/api/users/42` and `/api/users/43` group together.
fn looks_like_id(s: &str) -> bool {
    let b = s.as_bytes();
    if b.is_empty() {
        return false;
    }
    if b.iter().all(u8::is_ascii_digit) {
        return true;
    }
    let is_uuid = b.len() == 36
        && b.iter().enumerate().all(|(i, c)| if matches!(i, 8 | 13 | 18 | 23) { *c == b'-' } else { c.is_ascii_hexdigit() });
    if is_uuid || (b.len() >= 8 && b.iter().all(u8::is_ascii_hexdigit)) {
        return true;
    }
    let simple = b.iter().all(|c| c.is_ascii_alphanumeric() || *c == b'_' || *c == b'-');
    simple && b.iter().filter(|c| c.is_ascii_digit()).count() >= 2
}

impl Reporter {
    pub fn new(max_rows_per_sec: u32, row_cap: usize) -> Reporter {
        Reporter {
            shards: (0..SHARDS).map(|_| Mutex::new(HashMap::new())).collect(),
            used: (0..SHARDS).map(|_| Mutex::new(HashMap::new())).collect(),
            labels: RwLock::new(HashSet::new()),
            rows: Mutex::new(Vec::new()),
            row_window: Window::new(),
            max_rows_per_sec,
            row_cap,
            skipped_rows: AtomicU64::new(0),
            events: Mutex::new(Vec::new()),
            limited: Mutex::new(HashMap::new()),
        }
    }

    /// A bounded grouping label for a path: ids collapse, at most four segments, at most 200 labels.
    fn label(&self, path: &str) -> String {
        let segs: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
        if segs.is_empty() {
            return "/".into();
        }
        let mut label = String::from("/");
        let kept: Vec<String> = segs
            .iter()
            .take(MAX_SEGMENTS)
            .map(|s| if looks_like_id(s) { ":id".to_string() } else { s.chars().take(32).collect() })
            .collect();
        label.push_str(&kept.join("/"));
        if segs.len() > MAX_SEGMENTS {
            label.push_str("/...");
        }
        if self.labels.read().unwrap().contains(&label) {
            return label;
        }
        let mut known = self.labels.write().unwrap();
        if known.contains(&label) {
            return label;
        }
        if known.len() >= MAX_LABELS {
            return "other".into();
        }
        known.insert(label.clone());
        label
    }

    /// Count one served request and decide whether to keep its log row.
    pub fn record(
        &self,
        method: &str,
        path: &str,
        status: u16,
        seconds: f64,
        key_id: &str,
        rejected: bool,
        row: impl FnOnce() -> Row,
    ) {
        let rejected_key = rejected.then_some(key_id);
        let key = AggKey {
            method: method.to_owned(),
            label: self.label(path),
            status,
            rejected_key: rejected_key.map(str::to_owned),
        };
        {
            let mut shard = self.shards[fastrand::usize(..SHARDS)].lock().unwrap();
            let agg = shard.entry(key).or_insert_with(|| Agg { sample_path: path.to_owned(), ..Agg::default() });
            let i = bucket_index(seconds);
            agg.buckets[i].0 += 1;
            agg.buckets[i].1 += seconds;
        }
        let now64 = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
        {
            let mut used = self.used[fastrand::usize(..SHARDS)].lock().unwrap();
            match used.get_mut(key_id) {
                Some(at) => *at = now64,
                None => {
                    used.insert(key_id.to_owned(), now64);
                }
            }
        }
        let now = now64 as u32;
        let keep = status >= 400 || self.row_window.allow(self.max_rows_per_sec, 1, now);
        if keep {
            let mut rows = self.rows.lock().unwrap();
            if rows.len() < self.row_cap {
                rows.push(row());
                return;
            }
        }
        self.skipped_rows.fetch_add(1, Ordering::Relaxed);
    }

    /// Queue an event for the next report. Beyond the cap the oldest are kept and new ones dropped.
    pub fn event(&self, event: Event) {
        let mut events = self.events.lock().unwrap();
        if events.len() < MAX_EVENTS {
            events.push(event);
        }
    }

    /// A request was refused with a 429. Only reached on refusals, so the lock is off the hot path.
    pub fn rate_limited(&self, key_id: &str, window: &'static str, limit: u32, path: &str, ip: IpAddr) {
        let mut limited = self.limited.lock().unwrap();
        if let Some(l) = limited.get_mut(&(key_id.to_owned(), window)) {
            l.count += 1;
        } else if limited.len() < MAX_EVENTS {
            limited.insert(
                (key_id.to_owned(), window),
                Limited { client_ip: ip.to_string(), limit, path: path.to_owned(), count: 1 },
            );
        }
    }

    fn take_events(&self) -> Vec<Event> {
        let mut events = std::mem::take(&mut *self.events.lock().unwrap());
        for ((api_key_id, window), l) in std::mem::take(&mut *self.limited.lock().unwrap()) {
            events.push(Event::RateLimitExceeded {
                api_key_id,
                client_ip: l.client_ip,
                limit: l.limit,
                window,
                path: l.path,
                count: l.count,
            });
        }
        events
    }

    /// Put undelivered events back, ahead of anything queued since.
    fn restore_events(&self, mut events: Vec<Event>) {
        let mut queued = self.events.lock().unwrap();
        events.append(&mut queued);
        events.truncate(MAX_EVENTS);
        *queued = events;
    }

    fn take_used(&self) -> HashMap<String, u64> {
        let mut merged: HashMap<String, u64> = HashMap::new();
        for shard in &self.used {
            for (k, v) in std::mem::take(&mut *shard.lock().unwrap()) {
                let slot = merged.entry(k).or_insert(0);
                *slot = (*slot).max(v);
            }
        }
        merged
    }

    fn restore_used(&self, used: HashMap<String, u64>) {
        let mut shard = self.used[0].lock().unwrap();
        for (k, v) in used {
            let slot = shard.entry(k).or_insert(0);
            *slot = (*slot).max(v);
        }
    }

    fn take(&self) -> (Vec<(AggKey, Agg)>, Vec<Row>, u64) {
        let mut merged: HashMap<AggKey, Agg> = HashMap::new();
        for shard in &self.shards {
            for (k, v) in std::mem::take(&mut *shard.lock().unwrap()) {
                let slot = merged.entry(k).or_insert_with(|| Agg { sample_path: v.sample_path.clone(), ..Agg::default() });
                for i in 0..9 {
                    slot.buckets[i].0 += v.buckets[i].0;
                    slot.buckets[i].1 += v.buckets[i].1;
                }
            }
        }
        let rows = std::mem::take(&mut *self.rows.lock().unwrap());
        (merged.into_iter().collect(), rows, self.skipped_rows.swap(0, Ordering::Relaxed))
    }

    /// Put undelivered aggregates back so the next report includes them.
    fn restore(&self, aggs: Vec<(AggKey, Agg)>, skipped: u64) {
        let mut shard = self.shards[0].lock().unwrap();
        for (k, v) in aggs {
            let slot = shard.entry(k).or_insert_with(|| Agg { sample_path: v.sample_path.clone(), ..Agg::default() });
            for i in 0..9 {
                slot.buckets[i].0 += v.buckets[i].0;
                slot.buckets[i].1 += v.buckets[i].1;
            }
        }
        self.skipped_rows.fetch_add(skipped, Ordering::Relaxed);
    }
}

fn to_report(
    aggs: &[(AggKey, Agg)],
    rows: Vec<Row>,
    skipped: u64,
    used: &HashMap<String, u64>,
    events: &[Event],
) -> Report {
    Report {
        keys_used: used.iter().map(|(id, at)| KeyUse { id: id.clone(), at: *at }).collect(),
        aggregates: aggs
            .iter()
            .map(|(k, a)| AggOut {
                method: k.method.clone(),
                path: a.sample_path.clone(),
                status: k.status,
                rejected: k.rejected_key.is_some(),
                client_id: k.rejected_key.clone(),
                buckets: a.buckets.iter().map(|(n, s)| [*n as f64, *s]).collect(),
            })
            .collect(),
        rows,
        rows_skipped: skipped,
        events: events.to_vec(),
    }
}

pub async fn run(state: Arc<State>, reporter: Arc<Reporter>, control: String, token: String, interval: Duration) {
    let mut connector = HttpConnector::new();
    connector.set_nodelay(true);
    let client: Client<HttpConnector, Full<Bytes>> = Client::builder(TokioExecutor::new()).build(connector);
    let _ = &state; // the loop only needs the reporter; the state keeps the task tied to the process
    let mut failing = false;
    loop {
        tokio::time::sleep(interval).await;
        let (aggs, rows, skipped) = reporter.take();
        let used = reporter.take_used();
        let events = reporter.take_events();
        if aggs.is_empty() && rows.is_empty() && skipped == 0 && used.is_empty() && events.is_empty() {
            continue;
        }
        let rows_len = rows.len() as u64;
        let body = match serde_json::to_vec(&to_report(&aggs, rows, skipped, &used, &events)) {
            Ok(b) => b,
            Err(_) => continue,
        };
        let req = Request::builder()
            .method(Method::POST)
            .uri(format!("{control}/internal/dataplane/report"))
            .header("x-dataplane-token", &token)
            .header("content-type", "application/json")
            .body(Full::new(Bytes::from(body)));
        let delivered = match req {
            Ok(r) => match tokio::time::timeout(Duration::from_secs(10), client.request(r)).await {
                Ok(Ok(resp)) => {
                    let ok = resp.status().is_success();
                    let _ = resp.into_body().collect().await;
                    ok
                }
                _ => false,
            },
            Err(_) => false,
        };
        if delivered {
            if failing {
                eprintln!("dataplane: reports are being delivered again");
                failing = false;
            }
        } else {
            // Counts are kept for the next report; rows are not (they would grow without bound).
            reporter.restore(aggs, skipped + rows_len);
            reporter.restore_used(used);
            reporter.restore_events(events);
            if !failing {
                eprintln!("dataplane: could not deliver a report to the control plane; counts are kept, rows are skipped");
                failing = true;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row() -> Row {
        Row {
            request_id: "r".into(),
            method: "GET".into(),
            path: "/api/users/1".into(),
            client_ip: "127.0.0.1".into(),
            api_key_id: Some("k".into()),
            status: 200,
            duration_ms: 1.0,
            user_agent: None,
            error: None,
        }
    }

    #[test]
    fn buckets_match_the_python_histogram() {
        assert_eq!(bucket_index(0.005), 0);
        assert_eq!(bucket_index(0.01), 0);
        assert_eq!(bucket_index(0.011), 1);
        assert_eq!(bucket_index(10.0), 7);
        assert_eq!(bucket_index(10.5), 8);
    }

    #[test]
    fn ids_collapse_into_one_label() {
        let r = Reporter::new(1000, 100);
        assert_eq!(r.label("/api/users/42"), "/api/users/:id");
        assert_eq!(r.label("/api/users/43"), "/api/users/:id");
        assert_eq!(r.label("/api/users/550e8400-e29b-41d4-a716-446655440000"), "/api/users/:id");
        assert_eq!(r.label("/api/orders/a1b2c3d4e5"), "/api/orders/:id");
        assert_eq!(r.label("/api/a/b/c/d/e"), "/api/a/b/c/...");
        assert_eq!(r.label("/"), "/");
    }

    #[test]
    fn labels_are_capped() {
        let r = Reporter::new(1000, 100);
        for i in 0..250 {
            r.label(&format!("/api/route{}x/y", "a".repeat(i % 20 + 1) + &"b".repeat(i / 20)));
        }
        assert_eq!(r.label("/api/never/seen/before/ok"), "other");
    }

    #[test]
    fn counts_are_exact_and_sums_add_up() {
        let r = Reporter::new(1000, 100);
        for _ in 0..1000 {
            r.record("GET", "/api/users/7", 200, 0.002, "k", false, row);
        }
        r.record("GET", "/api/users/7", 200, 0.3, "k", false, row);
        let (aggs, _, _) = r.take();
        assert_eq!(aggs.len(), 1);
        let (_, a) = &aggs[0];
        assert_eq!(a.buckets[0].0, 1000);
        assert!((a.buckets[0].1 - 2.0).abs() < 1e-9);
        assert_eq!(a.buckets[3].0, 1);
        assert!(r.take().0.is_empty(), "a take drains the aggregates");
    }

    #[test]
    fn errors_always_get_a_row_and_successes_are_capped() {
        let r = Reporter::new(5, 1000);
        for _ in 0..100 {
            r.record("GET", "/api/x", 200, 0.001, "k", false, row);
        }
        for _ in 0..20 {
            r.record("GET", "/api/x", 500, 0.001, "k", false, || Row { status: 500, ..row() });
        }
        let (_, rows, skipped) = r.take();
        assert_eq!(rows.iter().filter(|x| x.status == 500).count(), 20);
        assert!(rows.iter().filter(|x| x.status == 200).count() <= 5);
        assert_eq!(rows.len() as u64 + skipped, 120, "every request is either kept or counted as skipped");
    }

    #[test]
    fn the_latest_use_of_each_key_is_tracked_and_survives_a_failed_delivery() {
        let r = Reporter::new(1000, 100);
        r.record("GET", "/api/x", 200, 0.001, "k1", false, row);
        r.record("GET", "/api/x", 200, 0.001, "k2", false, row);
        let used = r.take_used();
        assert_eq!(used.len(), 2);
        assert!(r.take_used().is_empty());
        r.restore_used(used);
        assert_eq!(r.take_used().len(), 2);
    }

    #[test]
    fn refusals_become_one_event_per_key_and_window() {
        let r = Reporter::new(1000, 100);
        let ip: IpAddr = "10.0.0.9".parse().unwrap();
        for _ in 0..50 {
            r.rate_limited("k1", "minute", 3, "/api/users", ip);
        }
        r.rate_limited("k1", "hour", 100, "/api/orders", ip);
        r.rate_limited("k2", "minute", 3, "/api/users", ip);
        let mut events = r.take_events();
        events.sort_by_key(|e| serde_json::to_string(e).unwrap());
        assert_eq!(events.len(), 3);
        assert!(events.contains(&Event::RateLimitExceeded {
            api_key_id: "k1".into(),
            client_ip: "10.0.0.9".into(),
            limit: 3,
            window: "minute",
            path: "/api/users".into(),
            count: 50,
        }));
        assert!(r.take_events().is_empty());
    }

    #[test]
    fn events_survive_a_failed_delivery_in_order_and_have_the_wire_shape() {
        let r = Reporter::new(1000, 100);
        let open = Event::CircuitBreaker {
            state: "open",
            previous_state: "closed",
            backend_url: "http://b".into(),
            failure_count: 5,
            failure_threshold: 5,
            opened_at: 1.5,
            timeout_seconds: 60,
        };
        r.event(open.clone());
        let taken = r.take_events();
        r.event(Event::CircuitBreaker {
            state: "closed",
            previous_state: "half-open",
            backend_url: "http://b".into(),
            failure_count: 0,
            failure_threshold: 5,
            opened_at: 2.0,
            timeout_seconds: 60,
        });
        r.restore_events(taken);
        let events = r.take_events();
        assert_eq!(events.len(), 2);
        assert_eq!(events[0], open);
        let wire = serde_json::to_value(&events[0]).unwrap();
        assert_eq!(wire["type"], "circuit_breaker");
        assert_eq!(wire["previous_state"], "closed");
    }

    #[test]
    fn undelivered_counts_come_back() {
        let r = Reporter::new(1000, 100);
        r.record("GET", "/api/x", 200, 0.001, "k", false, row);
        let (aggs, _, _) = r.take();
        r.restore(aggs, 3);
        let (aggs, _, skipped) = r.take();
        assert_eq!(aggs.len(), 1);
        assert_eq!(skipped, 3);
    }
}
