//! Tells the control plane what the data plane served, so the console, metrics, SLOs and logs
//! stay complete.
//!
//! Counts and latencies are aggregated here per (method, path pattern, status) in the same
//! histogram buckets the Python gateway uses and sent exactly once per interval, so their cost
//! does not grow with traffic. Individual request-log rows are sent for every error and for
//! successes up to a rate cap; the rest are counted as skipped (and shown in a metric), never
//! silently lost. If a report cannot be delivered the aggregates are kept for the next one.
//!
//! Each worker counts into its own shard, so serving a request never waits on another core. A
//! key's latest use and a tenant's usage live with the key and the tenant in the snapshot and are
//! collected from there.
//!
//! Events are the things the control plane announces by webhook and alert: the circuit breaker
//! opening or closing, and a key going over its rate limit (one event per key and window per
//! report, carrying how many requests were refused).
use crate::proxy::State;
use crate::snapshot::Snapshot;
use crate::tenant::{Slot, Tenant};
use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::{Method, Request};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::net::IpAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::Duration;

/// Upper bounds of the latency histogram, in seconds: the Python gateway's own buckets.
pub const BOUNDS: [f64; 8] = [0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0];
const MAX_LABELS: usize = 200;
const MAX_SEGMENTS: usize = 4;
const MAX_EVENTS: usize = 1000;
/// Paths whose label each worker remembers, so a repeated path costs one lookup.
const MAX_PATHS: usize = 4096;
/// The most entries of one kind the control plane accepts in a report.
const MAX_PER_REPORT: usize = 5000;

/// One served request, as the aggregates see it.
pub struct Served<'a> {
    pub method: &'static str,
    pub path: &'a str,
    pub status: u16,
    pub seconds: f64,
    pub key_id: &'a str,
    pub tenant: Option<&'a str>,
    /// Refused here before reaching the backend (403, 429).
    pub rejected: bool,
    /// For a 5xx produced here: the error code of its body.
    pub error_code: Option<&'static str>,
    /// For a call to the backend that got no answer: the kind of failure.
    pub error_type: Option<&'static str>,
}

#[derive(Hash, PartialEq, Eq, Clone)]
struct AggKey {
    method: &'static str,
    label: Arc<str>,
    status: u16,
    /// Set only for requests the data plane rejected: the key that was refused.
    rejected_key: Option<Arc<str>>,
    tenant: Option<Arc<str>>,
    error_code: Option<&'static str>,
    error_type: Option<&'static str>,
}

impl AggKey {
    fn is(&self, s: &Served, label: &str) -> bool {
        self.status == s.status
            && self.method == s.method
            && *self.label == *label
            && self.rejected_key.as_deref() == s.rejected.then_some(s.key_id)
            && self.tenant.as_deref() == s.tenant
            && self.error_code == s.error_code
            && self.error_type == s.error_type
    }
}

/// A cheap hash of what tells aggregates apart; equal keys always hash alike, and the few that
/// collide are told apart by comparing.
fn mix(method: &str, label: &str, status: u16, rejected_key: Option<&str>, tenant: Option<&str>) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325 ^ u64::from(status);
    for part in [method, label, rejected_key.unwrap_or(""), tenant.unwrap_or("")] {
        for b in part.bytes() {
            h = (h ^ u64::from(b)).wrapping_mul(0x0000_0100_0000_01b3);
        }
        h = (h ^ 0xff).wrapping_mul(0x0000_0100_0000_01b3);
    }
    h
}

#[derive(Clone, Default)]
struct Agg {
    sample_path: String,
    /// Per bucket (the eight bounds, then +Inf): number of requests and their summed seconds.
    buckets: [(u64, f64); 9],
}

impl Agg {
    fn add(&mut self, other: &Agg) {
        for i in 0..9 {
            self.buckets[i].0 += other.buckets[i].0;
            self.buckets[i].1 += other.buckets[i].1;
        }
    }
}

#[derive(Serialize)]
struct AggOut {
    method: &'static str,
    /// One real path from the group; the control plane derives its own label from it.
    path: String,
    status: u16,
    rejected: bool,
    client_id: Option<Arc<str>>,
    tenant: Option<Arc<str>>,
    error_code: Option<&'static str>,
    error_type: Option<&'static str>,
    /// `[count, seconds]` per bucket.
    buckets: Vec<[f64; 2]>,
}

#[derive(Serialize, Clone)]
pub struct Row {
    pub request_id: String,
    pub method: &'static str,
    pub path: String,
    pub client_ip: String,
    pub api_key_id: Option<String>,
    pub tenant_id: Option<String>,
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
    id: Arc<str>,
    at: u64,
}

#[derive(Serialize)]
struct UsageOut {
    tenant_id: Arc<str>,
    minute: u64,
    requests: u64,
    errors: u64,
    bytes_out: u64,
}

#[derive(Serialize)]
struct Report {
    tenant_usage: Vec<UsageOut>,
    keys_used: Vec<KeyUse>,
    aggregates: Vec<AggOut>,
    rows: Vec<Row>,
    rows_skipped: u64,
    events: Vec<Event>,
}

/// What one worker has served since the last report. Only that worker and, once per report, the
/// control loop take its lock, so it is all but uncontended.
#[derive(Default)]
struct Shard {
    aggs: HashMap<u64, Vec<(AggKey, Agg)>>,
    paths: HashMap<String, Arc<str>>,
    rows: Vec<Row>,
    skipped: u64,
    /// The second the row budget below belongs to, and how many success rows it has kept.
    second: u64,
    kept: u32,
}

pub struct Reporter {
    /// One per worker, then one the control loop puts undelivered counts back into.
    shards: Vec<Mutex<Shard>>,
    labels: RwLock<HashSet<Arc<str>>>,
    /// Success rows each worker keeps per second, and rows of any kind it holds per report.
    rows_per_sec: u32,
    row_cap: usize,
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

/// The keys a report says were used, held so their use can be put back if it is not delivered.
type Used = Vec<(Arc<AtomicU64>, Arc<str>, u64)>;

impl Reporter {
    /// `max_rows_per_sec` and `row_cap` are for the whole process; each of `workers` gets a share.
    pub fn new(max_rows_per_sec: u32, row_cap: usize, workers: usize) -> Reporter {
        let workers = workers.max(1);
        Reporter {
            shards: (0..=workers).map(|_| Mutex::new(Shard::default())).collect(),
            labels: RwLock::new(HashSet::new()),
            rows_per_sec: (max_rows_per_sec / workers as u32).max(1),
            row_cap: (row_cap / workers).max(1),
            events: Mutex::new(Vec::new()),
            limited: Mutex::new(HashMap::new()),
        }
    }

    /// A bounded grouping label for a path: ids collapse, at most four segments, at most 200 labels.
    fn label(&self, path: &str) -> Arc<str> {
        let segs: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
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
        if let Some(known) = self.labels.read().unwrap().get(label.as_str()) {
            return known.clone();
        }
        let mut known = self.labels.write().unwrap();
        if let Some(l) = known.get(label.as_str()) {
            return l.clone();
        }
        if known.len() >= MAX_LABELS {
            return Arc::from("other");
        }
        let label: Arc<str> = Arc::from(label);
        known.insert(label.clone());
        label
    }

    /// Count one served request on worker `shard` and decide whether to keep its log row.
    pub fn record(&self, shard: usize, s: &Served, now: u64, row: impl FnOnce() -> Row) {
        let mut g = self.shards[shard % (self.shards.len() - 1)].lock().unwrap();
        let label = match g.paths.get(s.path) {
            Some(l) => l.clone(),
            None => {
                let l = self.label(s.path);
                if g.paths.len() < MAX_PATHS {
                    g.paths.insert(s.path.to_owned(), l.clone());
                }
                l
            }
        };
        let rejected_key = s.rejected.then_some(s.key_id);
        let slot = g.aggs.entry(mix(s.method, &label, s.status, rejected_key, s.tenant)).or_default();
        let i = bucket_index(s.seconds);
        match slot.iter_mut().find(|(k, _)| k.is(s, &label)) {
            Some((_, agg)) => {
                agg.buckets[i].0 += 1;
                agg.buckets[i].1 += s.seconds;
            }
            None => {
                let key = AggKey {
                    method: s.method,
                    label,
                    status: s.status,
                    rejected_key: rejected_key.map(Arc::from),
                    tenant: s.tenant.map(Arc::from),
                    error_code: s.error_code,
                    error_type: s.error_type,
                };
                let mut agg = Agg { sample_path: s.path.to_owned(), ..Agg::default() };
                agg.buckets[i] = (1, s.seconds);
                slot.push((key, agg));
            }
        }
        let keep = s.status >= 400 || {
            if g.second != now {
                (g.second, g.kept) = (now, 0);
            }
            g.kept < self.rows_per_sec && {
                g.kept += 1;
                true
            }
        };
        if keep && g.rows.len() < self.row_cap {
            g.rows.push(row());
        } else {
            g.skipped += 1;
        }
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

    fn take(&self) -> (Vec<(AggKey, Agg)>, Vec<Row>, u64) {
        let mut merged: HashMap<AggKey, Agg> = HashMap::new();
        let (mut rows, mut skipped) = (Vec::new(), 0);
        for shard in &self.shards {
            let mut g = shard.lock().unwrap();
            for (k, v) in std::mem::take(&mut g.aggs).into_values().flatten() {
                match merged.get_mut(&k) {
                    Some(slot) => slot.add(&v),
                    None => {
                        merged.insert(k, v);
                    }
                }
            }
            rows.append(&mut g.rows);
            skipped += std::mem::take(&mut g.skipped);
        }
        (merged.into_iter().collect(), rows, skipped)
    }

    /// Put undelivered aggregates back so the next report includes them.
    fn restore(&self, aggs: Vec<(AggKey, Agg)>, skipped: u64) {
        let mut g = self.shards[self.shards.len() - 1].lock().unwrap();
        for (k, v) in aggs {
            let slot = g.aggs.entry(mix(k.method, &k.label, k.status, k.rejected_key.as_deref(), k.tenant.as_deref())).or_default();
            match slot.iter_mut().find(|(have, _)| *have == k) {
                Some((_, agg)) => agg.add(&v),
                None => slot.push((k, v)),
            }
        }
        g.skipped += skipped;
    }
}

/// The keys used since the last report, at most as many as one report may carry; the rest stay
/// marked for the next.
fn take_used(snap: &Snapshot) -> Used {
    let mut used = Vec::new();
    for e in snap.keys.values() {
        if e.last_used.load(Ordering::Relaxed) != 0 {
            used.push((e.last_used.clone(), e.id.clone(), e.last_used.swap(0, Ordering::Relaxed)));
            if used.len() == MAX_PER_REPORT {
                break;
            }
        }
    }
    used
}

fn restore_used(used: Used) {
    for (slot, _, at) in used {
        slot.fetch_max(at, Ordering::Relaxed);
    }
}

fn take_usage(snap: &Snapshot) -> Vec<(Arc<Tenant>, Vec<Slot>)> {
    let (mut out, mut n) = (Vec::new(), 0);
    for t in snap.tenants.values() {
        let slots = t.usage.take();
        if slots.is_empty() {
            continue;
        }
        n += slots.len();
        out.push((t.clone(), slots));
        if n >= MAX_PER_REPORT {
            break;
        }
    }
    // The last tenant may have taken the total over what one report carries.
    while n > MAX_PER_REPORT {
        let Some((t, mut slots)) = out.pop() else { break };
        let keep = slots.len().saturating_sub(n - MAX_PER_REPORT);
        n -= slots.len() - keep;
        t.usage.restore(slots.split_off(keep));
        if !slots.is_empty() {
            out.push((t, slots));
        }
    }
    out
}

fn to_report(
    aggs: &[(AggKey, Agg)],
    rows: Vec<Row>,
    skipped: u64,
    used: &Used,
    usage: &[(Arc<Tenant>, Vec<Slot>)],
    events: &[Event],
) -> Report {
    Report {
        tenant_usage: usage
            .iter()
            .flat_map(|(t, slots)| {
                slots.iter().map(|s| UsageOut {
                    tenant_id: t.id.clone(),
                    minute: s.minute,
                    requests: s.requests,
                    errors: s.errors,
                    bytes_out: s.bytes_out,
                })
            })
            .collect(),
        keys_used: used.iter().map(|(_, id, at)| KeyUse { id: id.clone(), at: *at }).collect(),
        aggregates: aggs
            .iter()
            .map(|(k, a)| AggOut {
                method: k.method,
                path: a.sample_path.clone(),
                status: k.status,
                rejected: k.rejected_key.is_some(),
                client_id: k.rejected_key.clone(),
                tenant: k.tenant.clone(),
                error_code: k.error_code,
                error_type: k.error_type,
                buckets: a.buckets.iter().map(|(n, s)| [*n as f64, *s]).collect(),
            })
            .collect(),
        rows,
        rows_skipped: skipped,
        events: events.to_vec(),
    }
}

/// How one attempt to report went.
pub enum Delivery {
    Nothing,
    Delivered,
    Failed,
}

/// Send the control plane everything served since the last report. What it does not acknowledge
/// is kept for the next one, except request-log rows, which are counted as skipped instead.
pub async fn deliver(state: &State, reporter: &Reporter, client: &Client<HttpConnector, Full<Bytes>>, token: &str) -> Delivery {
    let snap = state.snapshot.load_full();
    let (mut aggs, mut rows, mut skipped) = reporter.take();
    if aggs.len() > MAX_PER_REPORT {
        reporter.restore(aggs.split_off(MAX_PER_REPORT), 0);
    }
    if rows.len() > MAX_PER_REPORT {
        skipped += (rows.len() - MAX_PER_REPORT) as u64;
        rows.truncate(MAX_PER_REPORT);
    }
    let used = take_used(&snap);
    let usage = take_usage(&snap);
    let events = reporter.take_events();
    if aggs.is_empty() && rows.is_empty() && skipped == 0 && used.is_empty() && usage.is_empty() && events.is_empty() {
        return Delivery::Nothing;
    }
    let rows_len = rows.len() as u64;
    let request = serde_json::to_vec(&to_report(&aggs, rows, skipped, &used, &usage, &events)).ok().and_then(|body| {
        Request::builder()
            .method(Method::POST)
            .uri(format!("{}/internal/dataplane/report", state.control.text))
            .header("x-dataplane-token", token)
            .header("content-type", "application/json")
            .body(Full::new(Bytes::from(body)))
            .ok()
    });
    let delivered = match request {
        // Shorter than the staleness limit: a control plane too slow to take a report must not
        // by that alone stop the snapshot from being refreshed in time.
        Some(r) => match tokio::time::timeout(Duration::from_secs(3), client.request(r)).await {
            Ok(Ok(resp)) => {
                let ok = resp.status().is_success();
                let _ = resp.into_body().collect().await;
                ok
            }
            _ => false,
        },
        None => false,
    };
    if delivered {
        // The database now holds this usage: it stops counting as ours alone.
        for (t, slots) in &usage {
            t.usage.acked(slots);
        }
        return Delivery::Delivered;
    }
    reporter.restore(aggs, skipped + rows_len);
    restore_used(used);
    for (t, slots) in usage {
        t.usage.restore(slots);
    }
    reporter.restore_events(events);
    Delivery::Failed
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row() -> Row {
        Row {
            request_id: "r".into(),
            method: "GET",
            path: "/api/users/1".into(),
            client_ip: "127.0.0.1".into(),
            api_key_id: Some("k".into()),
            tenant_id: None,
            status: 200,
            duration_ms: 1.0,
            user_agent: None,
            error: None,
        }
    }

    fn served(path: &str, status: u16, seconds: f64) -> Served<'_> {
        Served { method: "GET", path, status, seconds, key_id: "k", tenant: None, rejected: false, error_code: None, error_type: None }
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
        let r = Reporter::new(1000, 100, 1);
        assert_eq!(&*r.label("/api/users/42"), "/api/users/:id");
        assert_eq!(&*r.label("/api/users/43"), "/api/users/:id");
        assert_eq!(&*r.label("/api/users/550e8400-e29b-41d4-a716-446655440000"), "/api/users/:id");
        assert_eq!(&*r.label("/api/orders/a1b2c3d4e5"), "/api/orders/:id");
        assert_eq!(&*r.label("/api/a/b/c/d/e"), "/api/a/b/c/...");
        assert_eq!(&*r.label("/"), "/");
    }

    #[test]
    fn labels_are_capped() {
        let r = Reporter::new(1000, 100, 1);
        for i in 0..250 {
            r.label(&format!("/api/route{}x/y", "a".repeat(i % 20 + 1) + &"b".repeat(i / 20)));
        }
        assert_eq!(&*r.label("/api/never/seen/before/ok"), "other");
    }

    #[test]
    fn counts_are_exact_across_workers_and_sums_add_up() {
        let r = Reporter::new(1000, 100, 4);
        for i in 0..1000 {
            r.record(i % 4, &served("/api/users/7", 200, 0.002), 1, row);
        }
        r.record(2, &served("/api/users/8", 200, 0.3), 1, row);
        let (aggs, _, _) = r.take();
        assert_eq!(aggs.len(), 1);
        let (_, a) = &aggs[0];
        assert_eq!(a.buckets[0].0, 1000);
        assert!((a.buckets[0].1 - 2.0).abs() < 1e-9);
        assert_eq!(a.buckets[3].0, 1);
        assert!(r.take().0.is_empty(), "a take drains the aggregates");
    }

    #[test]
    fn tenants_refusals_and_failures_are_counted_apart() {
        let r = Reporter::new(1000, 100, 1);
        let base = served("/api/x", 502, 0.001);
        r.record(0, &base, 1, row);
        r.record(0, &Served { tenant: Some("acme"), ..served("/api/x", 502, 0.001) }, 1, row);
        r.record(0, &Served { error_code: Some("BACKEND_ERROR"), error_type: Some("ConnectError"), ..served("/api/x", 502, 0.001) }, 1, row);
        r.record(0, &Served { error_code: Some("BACKEND_ERROR"), error_type: Some("ReadTimeout"), ..served("/api/x", 502, 0.001) }, 1, row);
        r.record(0, &Served { rejected: true, ..served("/api/x", 403, 0.001) }, 1, row);
        let (aggs, rows, _) = r.take();
        assert_eq!(aggs.len(), 5);
        let report = to_report(&aggs, rows, 0, &Vec::new(), &[], &[]);
        let wire = serde_json::to_value(&report).unwrap();
        let all = wire["aggregates"].as_array().unwrap();
        assert!(all.iter().any(|a| a["tenant"] == "acme"));
        assert!(all.iter().any(|a| a["error_type"] == "ReadTimeout" && a["error_code"] == "BACKEND_ERROR"));
        assert!(all.iter().any(|a| a["rejected"] == true && a["client_id"] == "k" && a["status"] == 403));
    }

    #[test]
    fn errors_always_get_a_row_and_successes_are_capped() {
        let r = Reporter::new(5, 1000, 1);
        for _ in 0..100 {
            r.record(0, &served("/api/x", 200, 0.001), 7, row);
        }
        for _ in 0..20 {
            r.record(0, &served("/api/x", 500, 0.001), 7, || Row { status: 500, ..row() });
        }
        let (_, rows, skipped) = r.take();
        assert_eq!(rows.iter().filter(|x| x.status == 500).count(), 20);
        assert_eq!(rows.iter().filter(|x| x.status == 200).count(), 5);
        assert_eq!(rows.len() as u64 + skipped, 120, "every request is either kept or counted as skipped");
    }

    fn snapshot_with(keys: usize) -> Snapshot {
        let keys: Vec<String> = (0..keys)
            .map(|i| format!(r#"{{"hash":"{:064x}","id":"k{i}","rate_limit_per_minute":1,"rate_limit_per_hour":1}}"#, i + 1))
            .collect();
        Snapshot::from_json(&format!(
            r#"{{"keys":[{}],"tenants":{{"acme":{{"minute":5,"hour":null,"details_json":"{{}}"}}}}}}"#,
            keys.join(",")
        ))
        .unwrap()
    }

    #[test]
    fn the_latest_use_of_each_key_is_reported_once_and_survives_a_failed_delivery() {
        let snap = snapshot_with(3);
        for e in snap.keys.values().filter(|e| &*e.id != "k2") {
            e.last_used.store(1_700_000_000, Ordering::Relaxed);
        }
        let used = take_used(&snap);
        assert_eq!(used.len(), 2);
        assert!(take_used(&snap).is_empty());
        restore_used(used);
        assert_eq!(take_used(&snap).len(), 2);
    }

    #[test]
    fn a_report_never_carries_more_keys_than_the_control_plane_accepts() {
        let snap = snapshot_with(MAX_PER_REPORT + 10);
        for e in snap.keys.values() {
            e.last_used.store(5, Ordering::Relaxed);
        }
        assert_eq!(take_used(&snap).len(), MAX_PER_REPORT);
        assert_eq!(take_used(&snap).len(), 10);
    }

    #[test]
    fn tenant_usage_goes_out_per_minute_and_comes_back_if_undelivered() {
        let snap = snapshot_with(0);
        let t = &snap.tenants["acme"];
        t.usage.admit(t.minute, t.hour, 120).unwrap().complete(200, 40);
        t.usage.admit(t.minute, t.hour, 121).unwrap().complete(500, 2);
        let usage = take_usage(&snap);
        let wire = serde_json::to_value(to_report(&[], Vec::new(), 0, &Vec::new(), &usage, &[])).unwrap();
        assert_eq!(wire["tenant_usage"][0], serde_json::json!({"tenant_id":"acme","minute":120,"requests":2,"errors":1,"bytes_out":42}));
        assert!(take_usage(&snap).is_empty());
        for (t, slots) in usage {
            t.usage.restore(slots);
        }
        assert_eq!(take_usage(&snap).len(), 1);
    }

    #[test]
    fn refusals_become_one_event_per_key_and_window() {
        let r = Reporter::new(1000, 100, 1);
        let ip: IpAddr = "10.0.0.9".parse().unwrap();
        for _ in 0..50 {
            r.rate_limited("k1", "minute", 3, "/api/users", ip);
        }
        r.rate_limited("k1", "hour", 100, "/api/orders", ip);
        r.rate_limited("k2", "minute", 3, "/api/users", ip);
        let events = r.take_events();
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
        let r = Reporter::new(1000, 100, 1);
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
        let r = Reporter::new(1000, 100, 2);
        r.record(0, &served("/api/x", 200, 0.001), 1, row);
        let (aggs, _, _) = r.take();
        r.restore(aggs, 3);
        r.record(1, &served("/api/x", 200, 0.001), 1, row);
        let (aggs, _, skipped) = r.take();
        assert_eq!(aggs.len(), 1);
        assert_eq!(aggs[0].1.buckets[0].0, 2);
        assert_eq!(skipped, 3);
    }
}
