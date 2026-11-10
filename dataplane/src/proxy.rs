//! Request handling: serve what the data plane owns, hand the rest to the control plane.
//!
//! A request is handled natively only when everything that decides its outcome is something
//! the data plane reproduces exactly: a plain HTTP path, a key present in the snapshot that is
//! active and unexpired, and a client that is not a browser. For such a request the key's
//! allowed paths, the IP rules, the policies, the key's rate limit and its tenant's quota are
//! all decided here, in the Python gateway's order and with its answers. Every other request
//! is forwarded unchanged to the control plane, so behaviour never differs; only speed does.
//! See DESIGN.md.
use bytes::Bytes;
use http_body_util::{Either, Full};
use hyper::body::{Body as _, Incoming};
use hyper::header::{HeaderName, HeaderValue, ACCEPT, CONTENT_LENGTH, CONTENT_TYPE, HOST, REFERER, USER_AGENT};
use hyper::http::uri::{Authority, Scheme};
use hyper::{HeaderMap, Method, Request, Response, StatusCode, Uri, Version};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::TokioExecutor;
use std::convert::Infallible;
use std::net::{IpAddr, SocketAddr};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::accesslog::{self, AccessLog};
use crate::breaker::{self, Breaker};
use crate::govern::{self, IpRule, Policy, Scope, Subject};
use crate::report::{Event, Reporter, Row, Served};
use crate::snapshot::{hash_key, KeyEntry, Snapshot};
use crate::sync::Link;

/// A response is either streamed from upstream or a small body made here; neither is boxed.
pub type Body = Either<Incoming, Full<Bytes>>;

/// Where requests are sent, parsed once so that no request pays for it.
pub struct Upstream {
    pub text: String,
    scheme: Scheme,
    authority: Authority,
    /// A path the upstream's own URL carries, put before every request's path.
    prefix: String,
}

impl Upstream {
    pub fn parse(text: &str) -> Result<Upstream, String> {
        let text = text.trim_end_matches('/');
        let uri: Uri = text.parse().map_err(|e| format!("{text}: {e}"))?;
        match (uri.scheme(), uri.authority()) {
            (Some(scheme), Some(authority)) if *scheme == Scheme::HTTP => Ok(Upstream {
                text: text.to_owned(),
                scheme: scheme.clone(),
                authority: authority.clone(),
                prefix: uri.path().trim_end_matches('/').to_owned(),
            }),
            _ => Err(format!("{text}: must be an http:// address")),
        }
    }
}

pub struct State {
    pub snapshot: arc_swap::ArcSwap<Snapshot>,
    /// Counts snapshot replacements, so a worker can tell cheaply that the one it holds is current.
    pub generation: AtomicU64,
    pub backend: Upstream,
    pub control: Upstream,
    /// For the backend native requests go to. The control plane keeps its own for what it serves.
    pub breaker: Breaker,
    /// Present when the snapshot comes from the control plane; native serving needs it fresh.
    pub sync: Option<Arc<Link>>,
    /// Present with a control-plane link: what was served is reported back.
    pub reporter: Option<Arc<Reporter>>,
    /// Present when a line per native request was asked for.
    pub access_log: Option<AccessLog>,
}

impl State {
    pub fn replace(&self, snap: Snapshot) {
        self.snapshot.store(Arc::new(snap));
        self.generation.fetch_add(1, Ordering::Release);
    }
}

/// One core's share of the data plane: its own connections to the upstreams, its own counters,
/// and its own handle on the snapshot, so that serving a request touches nothing another core
/// is writing to.
pub struct Worker {
    pub shared: Arc<State>,
    client: Client<HttpConnector, Incoming>,
    /// The snapshot as of `generation`, behind a reference count only this worker touches.
    held: Mutex<(u64, Arc<Arc<Snapshot>>)>,
    index: usize,
}

impl Worker {
    pub fn new(shared: Arc<State>, index: usize) -> Worker {
        let mut connector = HttpConnector::new();
        connector.set_nodelay(true);
        let client = Client::builder(TokioExecutor::new())
            .pool_idle_timeout(Duration::from_secs(30))
            .pool_max_idle_per_host(512)
            .build(connector);
        let held = Mutex::new((shared.generation.load(Ordering::Acquire), Arc::new(shared.snapshot.load_full())));
        Worker { shared, client, held, index }
    }

    fn snapshot(&self) -> Arc<Arc<Snapshot>> {
        let generation = self.shared.generation.load(Ordering::Acquire);
        let mut held = self.held.lock().unwrap();
        if held.0 != generation {
            *held = (generation, Arc::new(self.shared.snapshot.load_full()));
        }
        held.1.clone()
    }
}

const HOP_BY_HOP: [&str; 8] = [
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
];

/// First path segments after `/api/` that belong to other protocols (and so to the control plane).
const OTHER_PROTOCOLS: [&str; 9] = ["mcp", "a2a", "agui", "acp", "mqtt", "ws", "grpc", "graphql", "agents"];

fn is_native_path(path: &str, fallthrough: &[String]) -> bool {
    let Some(rest) = path.strip_prefix("/api/") else { return false };
    let first = rest.split('/').next().unwrap_or("");
    if OTHER_PROTOCOLS.contains(&first) {
        return false;
    }
    !fallthrough.iter().any(|p| path.starts_with(p.as_str()))
}

fn wants_html(headers: &HeaderMap) -> bool {
    headers
        .get(ACCEPT)
        .and_then(|v| v.to_str().ok())
        .map(|v| v.contains("text/html"))
        .unwrap_or(false)
}

fn full(bytes: impl Into<Bytes>) -> Body {
    Either::Right(Full::new(bytes.into()))
}

fn json(text: &str) -> String {
    serde_json::to_string(text).unwrap_or_else(|_| "\"\"".into())
}

/// A time since the epoch as UTC text: `YYYY-MM-DDTHH:MM:SS.ffffff`, the shape the Python gateway
/// puts in error bodies, or with `log`, `YYYY-MM-DDTHH:MM:SS.mmmZ`, the shape of its log lines.
pub fn timestamp(d: Duration, log: bool) -> String {
    let (secs, micros) = (d.as_secs() as i64, d.subsec_micros());
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // Days since 1970-01-01 to a civil date (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    let (h, m, s) = (rem / 3600, (rem % 3600) / 60, rem % 60);
    if log {
        format!("{year:04}-{month:02}-{day:02}T{h:02}:{m:02}:{s:02}.{:03}Z", micros / 1000)
    } else {
        format!("{year:04}-{month:02}-{day:02}T{h:02}:{m:02}:{s:02}.{micros:06}")
    }
}

fn wall() -> Duration {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default()
}

/// The API version a request resolves to, as the Python gateway does for a path that does not
/// start with `/v<N>`: the `Accept-Version` or `X-API-Version` header (a leading v is ignored),
/// else the default. `None` when there is none; `Some((version, native_ok))` otherwise.
fn resolve_version<'a>(snap: &'a Snapshot, headers: &HeaderMap) -> Option<(&'a str, bool)> {
    let wanted = headers
        .get("accept-version")
        .or_else(|| headers.get("x-api-version"))
        .and_then(|v| v.to_str().ok())
        .map(|v| v.trim().trim_start_matches(['v', 'V']))
        .filter(|v| !v.is_empty())
        .and_then(|v| snap.api_versions.native_ok.get_key_value(v));
    let found = wanted.or_else(|| {
        snap.api_versions.default.as_deref().and_then(|d| snap.api_versions.native_ok.get_key_value(d))
    });
    found.map(|(k, ok)| (k.as_str(), *ok))
}

struct Trace {
    request_id: String,
    traceparent: String,
}

/// An incoming `traceparent` is honoured (same trace, new span for this hop); otherwise a new trace starts.
fn trace_for(headers: &HeaderMap) -> Trace {
    let request_id = headers
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .filter(|v| !v.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    let trace_id = headers
        .get("traceparent")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| {
            let mut it = v.split('-');
            let (_, t, _) = (it.next()?, it.next()?, it.next()?);
            (t.len() == 32 && t.bytes().all(|c| c.is_ascii_hexdigit())).then_some(t)
        });
    let traceparent = match trace_id {
        Some(t) => format!("00-{t}-{:016x}-01", fastrand::u64(..)),
        None => format!("00-{:016x}{:016x}-{:016x}-01", fastrand::u64(..), fastrand::u64(..), fastrand::u64(..)),
    };
    Trace { request_id, traceparent }
}

/// A request the data plane serves itself.
struct Native<'a> {
    entry: &'a KeyEntry,
    version: Option<&'a str>,
    /// The client's address, as the Python gateway would resolve it.
    ip: IpAddr,
    method: &'static str,
}

/// Decide whether a request is served here. `None` hands it to the Python gateway, which is
/// always right: it is the answer whenever this side cannot be sure of deciding the same.
fn route<'a, B>(snap: &'a Snapshot, peer: SocketAddr, req: &Request<B>, now: u64) -> Option<Native<'a>> {
    let uri = req.uri();
    let path = uri.path();
    // An escaped or non-ASCII path reads differently once decoded, and what the key may reach
    // and what a policy matches are decided on the decoded one.
    if uri.authority().is_some() || !path.is_ascii() || path.contains('%') {
        return None;
    }
    if !is_native_path(path, &snap.fallthrough_prefixes) || wants_html(req.headers()) {
        return None;
    }
    let m = req.method();
    let method = if m == Method::GET {
        "GET"
    } else if m == Method::POST {
        "POST"
    } else if m == Method::PUT {
        "PUT"
    } else if m == Method::DELETE {
        "DELETE"
    } else if m == Method::PATCH {
        "PATCH"
    } else if m == Method::HEAD {
        "HEAD"
    } else {
        return None;
    };
    let entry = snap.keys.get(&hash_key(req.headers().get("x-api-key")?.as_bytes()))?;
    let expired = entry.expires_at.map(|e| e <= now as i64).unwrap_or(false);
    let version = resolve_version(snap, req.headers());
    if !entry.is_active || expired || !entry.native_ok || !version.map(|(_, ok)| ok).unwrap_or(true) {
        return None;
    }
    let ip = govern::client_ip(peer, req.headers(), &snap.trusted_proxies)?;
    Some(Native { entry, version: version.map(|(v, _)| v), ip, method })
}

/// Why a request with a valid key is refused with a 403.
enum Refusal<'a> {
    Scope(&'a Scope),
    Ip(&'a IpRule),
    Policy(&'a Policy),
}

/// The Python gateway's checks after the key is known, in its order: the key's allowed paths,
/// the IP rules, then the policies.
fn refusal<'a>(snap: &'a Snapshot, n: &Native<'a>, path: &str, at: Duration) -> Option<Refusal<'a>> {
    if let Some(scope) = n.entry.scope.as_ref().filter(|s| !s.allows(path)) {
        return Some(Refusal::Scope(scope));
    }
    let tenant = n.entry.tenant.as_ref().map(|t| &*t.id);
    if let Some(rule) = snap.ip_rules.blocking(n.ip, tenant, &n.entry.id) {
        return Some(Refusal::Ip(rule));
    }
    if snap.policies.is_empty() {
        return None;
    }
    let subject = Subject {
        path,
        method: n.method,
        tenant: tenant.unwrap_or(""),
        ip: n.ip,
        micros_of_day: (at.as_secs() % 86_400) * 1_000_000 + u64::from(at.subsec_micros()),
    };
    govern::refusing(&snap.policies, &subject).map(Refusal::Policy)
}

/// The Python gateway's error envelope. Errors it raises put the timestamp before the details;
/// the upstream failures it answers put it after.
fn envelope(status: StatusCode, code: &str, message_json: &str, details: &str, t: &Trace, details_last: bool) -> Response<Body> {
    let (id, at) = (json(&t.request_id), timestamp(wall(), false));
    let body = if details_last {
        format!("{{\"error\":\"{code}\",\"message\":{message_json},\"request_id\":{id},\"timestamp\":\"{at}\",\"details\":{details}}}")
    } else {
        format!("{{\"error\":\"{code}\",\"message\":{message_json},\"request_id\":{id},\"details\":{details},\"timestamp\":\"{at}\"}}")
    };
    let mut resp = Response::new(full(body));
    *resp.status_mut() = status;
    let h = resp.headers_mut();
    h.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    h.insert("x-content-type-options", HeaderValue::from_static("nosniff"));
    resp
}

/// `{first, ...rest}`: a JSON object with one member put before those of another.
fn led_by(first: &str, rest: &str) -> String {
    match rest.get(1..).map(str::trim_start) {
        Some(tail) if tail != "}" && !tail.is_empty() => format!("{{{first},{tail}"),
        _ => format!("{{{first}}}"),
    }
}

fn forbidden(r: &Refusal, path: &str, ip: IpAddr, t: &Trace) -> Response<Body> {
    let (message, details) = match r {
        Refusal::Scope(scope) => (
            "\"This API key is not allowed to access this path\"",
            format!(
                "{{\"path\":{},\"allowed_paths\":{},\"hint\":\"The key was created with allowed_paths. Use a key whose scope covers this path.\"}}",
                json(path),
                scope.patterns_json
            ),
        ),
        Refusal::Ip(rule) => ("\"Request blocked by IP rule\"", led_by(&format!("\"ip\":\"{ip}\""), &rule.details_json)),
        Refusal::Policy(p) => (p.message_json.as_str(), p.details_json.clone()),
    };
    envelope(StatusCode::FORBIDDEN, "AUTHORIZATION_ERROR", message, &details, t, true)
}

/// A 429, for a key's own limit or, with `tenant_details`, its tenant's quota.
fn rate_limited(window: &str, limit: u64, retry_after: u32, tenant_details: Option<&str>, t: &Trace) -> Response<Body> {
    let limits = format!("\"limit\":{limit},\"window\":\"{window}\",\"retry_after\":{retry_after}");
    let details = led_by(&limits, tenant_details.unwrap_or("{}"));
    let message = format!("\"Rate limit exceeded: {limit} requests per {window}\"");
    let mut resp = envelope(StatusCode::TOO_MANY_REQUESTS, "RATE_LIMIT_EXCEEDED", &message, &details, t, true);
    resp.headers_mut().insert("retry-after", HeaderValue::from(retry_after));
    resp
}

/// The backend did not answer (refused, reset, or slower than the timeout).
fn backend_unavailable(t: &Trace) -> Response<Body> {
    envelope(
        StatusCode::BAD_GATEWAY,
        "BACKEND_ERROR",
        "\"Backend: Backend service unavailable\"",
        "{\"hint\":\"The upstream backend could not be reached. Check that the backend is running and reachable from the gateway.\",\"service\":\"Backend\"}",
        t,
        false,
    )
}

/// The circuit breaker is open: the request was not sent.
fn circuit_open(t: &Trace) -> Response<Body> {
    envelope(
        StatusCode::SERVICE_UNAVAILABLE,
        "SERVICE_UNAVAILABLE",
        "\"Backend: Upstream service is temporarily unavailable (circuit open)\"",
        "{\"service\":\"Backend\"}",
        t,
        false,
    )
}

/// The control plane itself could not be reached; there is no gateway behind us to shape the answer.
fn control_unreachable() -> Response<Body> {
    let t = Trace { request_id: uuid::Uuid::new_v4().to_string(), traceparent: String::new() };
    let mut resp =
        envelope(StatusCode::BAD_GATEWAY, "BACKEND_ERROR", "\"Gateway: control plane unreachable\"", "{\"service\":\"Gateway\"}", &t, false);
    if let Ok(v) = HeaderValue::from_str(&t.request_id) {
        resp.headers_mut().insert("x-request-id", v);
    }
    resp
}

fn announce(st: &State, cfg: &breaker::Config, tr: breaker::Transition) {
    eprintln!("dataplane: circuit breaker for {} is {} (was {})", st.backend.text, tr.state.name(), tr.previous.name());
    if let Some(reporter) = &st.reporter {
        reporter.event(Event::CircuitBreaker {
            state: tr.state.name(),
            previous_state: tr.previous.name(),
            backend_url: st.backend.text.clone(),
            failure_count: tr.failures,
            failure_threshold: cfg.threshold,
            opened_at: wall().as_secs_f64(),
            timeout_seconds: cfg.timeout_secs,
        });
    }
}

/// The key's own limit that refuses this request, if one does: window, limit, seconds to wait.
fn key_limit(snap: &Snapshot, entry: &KeyEntry, now: u64) -> Option<(&'static str, u32, u32)> {
    if !snap.rate_limit_enabled {
        None
    } else if !entry.minute.allow(entry.per_minute, 60, now as u32) {
        Some(("minute", entry.per_minute, 60))
    } else if !entry.hour.allow(entry.per_hour, 3600, now as u32) {
        Some(("hour", entry.per_hour, 3600))
    } else {
        None
    }
}

pub async fn handle(w: Arc<Worker>, peer: SocketAddr, req: Request<Incoming>) -> Result<Response<Body>, Infallible> {
    let started = Instant::now();
    let st = &*w.shared;
    // The control plane's internal endpoints are never reachable from clients.
    if req.uri().path().starts_with("/internal/") {
        let mut resp = Response::new(full("{\"detail\":\"Not Found\"}"));
        *resp.status_mut() = StatusCode::NOT_FOUND;
        resp.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        return Ok(resp);
    }
    let snap = w.snapshot();
    let at = wall();
    let now = at.as_secs();
    // Fail closed: if the snapshot is not known to be current, the Python gateway decides.
    let trusted = st.sync.as_ref().map(|s| s.fresh()).unwrap_or(true);
    let Some(n) = trusted.then(|| route(&snap, peer, &req, now)).flatten() else {
        return Ok(forward(&w, &st.control, peer, req, None, None).await.unwrap_or_else(|_| control_unreachable()));
    };
    let entry = n.entry;
    if entry.last_used.load(Ordering::Relaxed) != now {
        entry.last_used.store(now, Ordering::Relaxed);
    }
    let t = trace_for(req.headers());
    let uri = req.uri().clone();
    let path = uri.path();
    let tenant = entry.tenant.as_deref();
    let user_agent = (st.reporter.is_some() || st.access_log.is_some()).then(|| req.headers().get(USER_AGENT).cloned()).flatten();
    let referrer = st.access_log.as_ref().and_then(|_| req.headers().get(REFERER).cloned());
    let cb = snap.circuit_breaker;

    let (mut ticket, mut rejected, mut error_type) = (None, false, None);
    let (mut resp, error) = if let Some(r) = refusal(&snap, &n, path, at) {
        rejected = true;
        (forbidden(&r, path, n.ip, &t), Some("AUTHORIZATION_ERROR"))
    } else if let Some((window, limit, retry_after)) = key_limit(&snap, entry, now) {
        rejected = true;
        if let Some(reporter) = &st.reporter {
            reporter.rate_limited(&entry.id, window, limit, path, n.ip);
        }
        (rate_limited(window, u64::from(limit), retry_after, None, &t), Some("RATE_LIMIT_EXCEEDED"))
    } else {
        match tenant.map(|tn| tn.usage.admit(tn.minute, tn.hour, now)).transpose() {
            Err(over) => {
                rejected = true;
                let details = tenant.map(|tn| tn.details_json.as_str());
                (rate_limited(over.window, over.limit, over.retry_after, details, &t), Some("RATE_LIMIT_EXCEEDED"))
            }
            Ok(admitted) => {
                ticket = admitted;
                if !st.breaker.allow(&cb, Instant::now()) {
                    (circuit_open(&t), Some("SERVICE_UNAVAILABLE"))
                } else {
                    let native = Sent { trace: &t, version: n.version, inject_request_id: snap.inject_request_id };
                    match forward(&w, &st.backend, peer, req, Some(native), Some(snap.upstream_timeout)).await {
                        Ok(resp) => {
                            if let Some(tr) = st.breaker.success(&cb) {
                                announce(st, &cb, tr);
                            }
                            (resp, None)
                        }
                        Err(unanswered) => {
                            if let Some(tr) = st.breaker.failure(&cb, Instant::now()) {
                                announce(st, &cb, tr);
                            }
                            error_type = Some(unanswered.name());
                            (backend_unavailable(&t), Some("BACKEND_ERROR"))
                        }
                    }
                }
            }
        }
    };

    let status = resp.status().as_u16();
    let seconds = started.elapsed().as_secs_f64();
    let bytes_out = resp
        .headers()
        .get(CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse().ok())
        .or_else(|| resp.body().size_hint().exact())
        .unwrap_or(0);
    let h = resp.headers_mut();
    // Tells operators (and the conformance suite) which plane answered.
    h.insert("x-served-by", HeaderValue::from_static("dataplane"));
    if let Ok(v) = HeaderValue::from_str(&format!("{seconds:.3}")) {
        h.insert("x-response-time", v);
    }
    if let Some(v) = n.version.and_then(|v| HeaderValue::from_str(v).ok()) {
        h.insert("x-api-version", v);
    }
    if let Some(v) = tenant.and_then(|tn| tn.header.clone()) {
        h.insert("x-tenant-id", v);
    }
    if let Ok(v) = HeaderValue::from_str(&t.request_id) {
        h.insert("x-request-id", v);
    }
    if let Ok(v) = HeaderValue::from_str(&t.traceparent) {
        h.insert("traceparent", v);
    }
    // An answered request is usage, whatever the answer; a refused one never is.
    if let Some(ticket) = ticket {
        ticket.complete(status, bytes_out);
    }
    let user_agent = user_agent.as_ref().and_then(|v| v.to_str().ok());
    if let Some(reporter) = &st.reporter {
        let served = Served {
            method: n.method,
            path,
            status,
            seconds,
            key_id: &entry.id,
            tenant: tenant.map(|tn| &*tn.id),
            rejected,
            error_code: error.filter(|_| status >= 500),
            error_type,
        };
        reporter.record(w.index, &served, now, || Row {
            request_id: t.request_id.clone(),
            method: n.method,
            path: path.to_owned(),
            client_ip: n.ip.to_string(),
            api_key_id: Some(entry.id.to_string()),
            tenant_id: tenant.map(|tn| tn.id.to_string()),
            status,
            duration_ms: seconds * 1000.0,
            user_agent: user_agent.map(str::to_owned),
            error: error.map(str::to_owned),
        });
    }
    if let Some(log) = &st.access_log {
        log.write(accesslog::line(&accesslog::Entry {
            at,
            request_id: &t.request_id,
            traceparent: &t.traceparent,
            key_id: &entry.id,
            tenant: tenant.map(|tn| &*tn.id),
            ip: n.ip,
            method: n.method,
            path,
            query: uri.query().unwrap_or(""),
            status,
            bytes: bytes_out,
            seconds,
            user_agent,
            referrer: referrer.as_ref().and_then(|v| v.to_str().ok()),
        }));
    }
    Ok(resp)
}

/// The upstream gave no response.
#[derive(Debug, PartialEq)]
enum Unanswered {
    /// No connection could be made.
    Refused,
    /// Slower than the timeout to start answering.
    TimedOut,
    /// Connected, then failed before a response arrived.
    Broken,
}

impl Unanswered {
    /// The name the Python gateway's metrics give this failure (its HTTP client's error class).
    fn name(&self) -> &'static str {
        match self {
            Unanswered::Refused => "ConnectError",
            Unanswered::TimedOut => "ReadTimeout",
            Unanswered::Broken => "RemoteProtocolError",
        }
    }
}

/// What a native request carries to the backend.
struct Sent<'a> {
    trace: &'a Trace,
    version: Option<&'a str>,
    inject_request_id: bool,
}

/// Forward to `to`. With `native`, the Python gateway's upstream headers are reproduced.
/// Without, the request is handed to the control plane as it came, plus the client's address.
/// `timeout` bounds the wait for the response to start.
async fn forward(
    w: &Worker,
    to: &Upstream,
    peer: SocketAddr,
    mut req: Request<Incoming>,
    native: Option<Sent<'_>>,
    timeout: Option<Duration>,
) -> Result<Response<Body>, Unanswered> {
    let mut parts = std::mem::take(req.uri_mut()).into_parts();
    parts.scheme = Some(to.scheme.clone());
    parts.authority = Some(to.authority.clone());
    if parts.path_and_query.is_none() || !to.prefix.is_empty() {
        let pq = parts.path_and_query.as_ref().map(|p| p.as_str()).unwrap_or("/");
        parts.path_and_query = Some(format!("{}{pq}", to.prefix).parse().map_err(|_| Unanswered::Broken)?);
    }
    *req.uri_mut() = Uri::from_parts(parts).map_err(|_| Unanswered::Broken)?;
    *req.version_mut() = Version::HTTP_11;
    let headers = req.headers_mut();
    for h in HOP_BY_HOP {
        headers.remove(h);
    }
    match native {
        Some(sent) => {
            headers.remove(HOST);
            let internal: Vec<HeaderName> = headers.keys().filter(|k| k.as_str().starts_with("x-gateway-")).cloned().collect();
            for name in internal {
                headers.remove(name);
            }
            headers.insert("x-protocol", HeaderValue::from_static("http"));
            if let Some(v) = sent.version.and_then(|v| HeaderValue::from_str(v).ok()) {
                headers.insert("x-api-version", v);
            }
            if !headers.contains_key("x-forwarded-for") {
                headers.insert("x-forwarded-for", HeaderValue::from_static(""));
            }
            if let Ok(v) = HeaderValue::from_str(&sent.trace.traceparent) {
                headers.insert("traceparent", v);
            }
            if sent.inject_request_id && !headers.contains_key("x-request-id") {
                if let Ok(v) = HeaderValue::from_str(&sent.trace.request_id) {
                    headers.insert("x-request-id", v);
                }
            }
        }
        None => {
            let xff = match headers.get("x-forwarded-for").and_then(|v| v.to_str().ok()) {
                Some(prior) => format!("{prior}, {}", peer.ip()),
                None => peer.ip().to_string(),
            };
            if let Ok(v) = HeaderValue::from_str(&xff) {
                headers.insert("x-forwarded-for", v);
            }
        }
    }
    let sent = w.client.request(req);
    let answer = match timeout {
        Some(limit) => tokio::time::timeout(limit, sent).await.map_err(|_| Unanswered::TimedOut)?,
        None => sent.await,
    };
    match answer {
        Ok(resp) => {
            let (mut rp, rb) = resp.into_parts();
            for h in HOP_BY_HOP {
                rp.headers.remove(h);
            }
            Ok(Response::from_parts(rp, Either::Left(rb)))
        }
        Err(e) if e.is_connect() => Err(Unanswered::Refused),
        Err(_) => Err(Unanswered::Broken),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_plain_http_api_paths_are_native() {
        let none: Vec<String> = vec![];
        assert!(is_native_path("/api/users", &none));
        assert!(is_native_path("/api/v1/orders/7", &none));
        for p in ["/admin/keys", "/oauth2/token", "/health", "/mcp/x", "/api/mcp/x", "/api/a2a", "/api/graphql", "/docs"] {
            assert!(!is_native_path(p, &none), "{p}");
        }
    }

    #[test]
    fn control_plane_prefixes_win() {
        assert!(!is_native_path("/api/custom/x", &["/api/custom".to_string()]));
    }

    #[test]
    fn timestamps_have_the_python_shapes() {
        let at = Duration::new(1_700_000_000, 123_456_000);
        assert_eq!(timestamp(at, false), "2023-11-14T22:13:20.123456");
        assert_eq!(timestamp(at, true), "2023-11-14T22:13:20.123Z");
        assert_eq!(timestamp(Duration::new(951_782_400, 0), true), "2000-02-29T00:00:00.000Z");
    }

    #[test]
    fn an_incoming_trace_is_kept_with_a_new_span() {
        let mut h = HeaderMap::new();
        h.insert("traceparent", HeaderValue::from_static("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"));
        h.insert("x-request-id", HeaderValue::from_static("req-1"));
        let t = trace_for(&h);
        assert_eq!(t.request_id, "req-1");
        assert!(t.traceparent.starts_with("00-0af7651916cd43dd8448eb211c80319c-"));
        assert!(!t.traceparent.contains("b7ad6b7169203331"));
    }

    #[test]
    fn a_missing_trace_starts_a_new_one() {
        let t = trace_for(&HeaderMap::new());
        assert_eq!(t.request_id.len(), 36);
        assert_eq!(t.traceparent.len(), 55);
    }

    fn snap() -> Snapshot {
        Snapshot::from_json(r#"{"keys":[],"api_versions":{"default":"1","versions":[
            {"version":"1"},{"version":"2"},{"version":"3","native_ok":false}]}}"#).unwrap()
    }

    #[test]
    fn the_default_version_applies_without_a_hint() {
        assert_eq!(resolve_version(&snap(), &HeaderMap::new()), Some(("1", true)));
    }

    #[test]
    fn a_header_picks_the_version_and_ignores_a_leading_v() {
        let mut h = HeaderMap::new();
        h.insert("x-api-version", HeaderValue::from_static("V2"));
        assert_eq!(resolve_version(&snap(), &h), Some(("2", true)));
        let mut h = HeaderMap::new();
        h.insert("accept-version", HeaderValue::from_static("3"));
        assert_eq!(resolve_version(&snap(), &h), Some(("3", false)));
    }

    #[test]
    fn an_unknown_version_hint_falls_back_to_the_default() {
        let mut h = HeaderMap::new();
        h.insert("x-api-version", HeaderValue::from_static("9"));
        assert_eq!(resolve_version(&snap(), &h), Some(("1", true)));
    }

    #[test]
    fn browsers_are_not_native() {
        let mut h = HeaderMap::new();
        h.insert(ACCEPT, HeaderValue::from_static("text/html,application/xhtml+xml"));
        assert!(wants_html(&h));
        assert!(!wants_html(&HeaderMap::new()));
    }

    const KEY: &str = "sk-test";

    fn governed(extra: &str, key_extra: &str) -> Snapshot {
        let hex: String = hash_key(KEY.as_bytes()).iter().map(|b| format!("{b:02x}")).collect();
        Snapshot::from_json(&format!(
            r#"{{"keys":[{{"hash":"{hex}","id":"k1","rate_limit_per_minute":100,"rate_limit_per_hour":1000{key_extra}}}]{extra}}}"#
        ))
        .unwrap()
    }

    fn request(method: &str, uri: &str) -> Request<()> {
        Request::builder().method(method).uri(uri).header("x-api-key", KEY).body(()).unwrap()
    }

    fn peer() -> SocketAddr {
        "203.0.113.7:4000".parse().unwrap()
    }

    #[test]
    fn a_known_key_on_a_plain_path_is_served_here() {
        let snap = governed("", "");
        let n = route(&snap, peer(), &request("GET", "/api/users?x=1"), 0).unwrap();
        assert_eq!((&*n.entry.id, n.method, n.version, n.ip.to_string().as_str()), ("k1", "GET", Some("1"), "203.0.113.7"));
    }

    #[test]
    fn what_this_side_cannot_be_sure_of_goes_to_python() {
        let snap = governed("", "");
        for (method, uri) in [
            ("GET", "/api/a%2Fb"),
            ("GET", "/api/caf\u{e9}"),
            ("OPTIONS", "/api/users"),
            ("PROPFIND", "/api/users"),
            ("GET", "http://other.example/api/users"),
            ("GET", "/admin/api-keys"),
        ] {
            assert!(route(&snap, peer(), &request(method, uri), 0).is_none(), "{method} {uri}");
        }
        let unknown = Request::builder().uri("/api/users").header("x-api-key", "sk-other").body(()).unwrap();
        assert!(route(&snap, peer(), &unknown, 0).is_none());
        let keyless = Request::builder().uri("/api/users").body(()).unwrap();
        assert!(route(&snap, peer(), &keyless, 0).is_none());
    }

    #[test]
    fn the_client_address_comes_from_a_trusted_proxy() {
        let snap = governed(r#","trusted_proxies":["203.0.113.0/24"]"#, "");
        let mut req = request("GET", "/api/users");
        req.headers_mut().insert("x-forwarded-for", HeaderValue::from_static("198.51.100.9, 203.0.113.1"));
        assert_eq!(route(&snap, peer(), &req, 0).unwrap().ip.to_string(), "198.51.100.9");
        req.headers_mut().append("x-forwarded-for", HeaderValue::from_static("10.0.0.1"));
        assert!(route(&snap, peer(), &req, 0).is_none(), "two headers: Python decides");
    }

    fn refused(snap: &Snapshot, req: &Request<()>, at: Duration) -> Option<String> {
        let n = route(snap, peer(), req, at.as_secs()).expect("native");
        let t = Trace { request_id: "r\"1".into(), traceparent: String::new() };
        let r = refusal(snap, &n, req.uri().path(), at)?;
        let resp = forbidden(&r, req.uri().path(), n.ip, &t);
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
        Some(match &r {
            Refusal::Scope(_) => "scope".into(),
            Refusal::Ip(rule) => format!("ip {}", rule.details_json),
            Refusal::Policy(p) => format!("policy {}", p.details_json),
        })
    }

    #[test]
    fn a_key_only_reaches_its_allowed_paths() {
        let snap = governed("", r#","allowed_paths":{"rules":[["under","/api/users"]],"patterns_json":"[\"/api/users\"]"}"#);
        let at = Duration::from_secs(0);
        assert_eq!(refused(&snap, &request("GET", "/api/users/7"), at), None);
        assert_eq!(refused(&snap, &request("GET", "/api//users"), at), None);
        assert_eq!(refused(&snap, &request("GET", "/api/usersx"), at).as_deref(), Some("scope"));
        assert_eq!(refused(&snap, &request("GET", "/api/orders"), at).as_deref(), Some("scope"));
    }

    #[test]
    fn ip_rules_come_before_policies_and_the_first_matching_policy_decides() {
        let rules = r#","ip_rules":[{"scope":"global","scope_id":null,"allow":false,"net":"203.0.113.0/24","details_json":"{\"rule_id\":\"r1\",\"scope\":\"global\"}"}]"#;
        let policies = r#","policies":[
            {"path":"/api/open/*","methods":[],"tenants":[],"cidrs":null,"time":null,"deny":false,"message_json":"\"\"","details_json":"{}"},
            {"path":"/api/*","methods":["DELETE"],"tenants":[],"cidrs":null,"time":null,"deny":true,"message_json":"\"no deletes\"","details_json":"{\"policy_id\":\"p1\"}"}]"#;
        let at = Duration::from_secs(0);
        let both = governed(&format!("{rules}{policies}"), "");
        assert!(refused(&both, &request("DELETE", "/api/users"), at).unwrap().starts_with("ip "));
        let snap = governed(policies, "");
        assert_eq!(refused(&snap, &request("DELETE", "/api/users"), at).as_deref(), Some(r#"policy {"policy_id":"p1"}"#));
        assert_eq!(refused(&snap, &request("GET", "/api/users"), at), None);
        assert_eq!(refused(&snap, &request("DELETE", "/api/open/x"), at), None, "an earlier allow wins");
    }

    #[test]
    fn a_policy_window_is_read_in_utc() {
        let policies = r#","policies":[{"path":null,"methods":[],"tenants":[],"cidrs":null,"time":[60,120],"deny":true,"message_json":"\"closed\"","details_json":"{\"policy_id\":\"p2\"}"}]"#;
        let snap = governed(policies, "");
        let day = 1_700_006_400 - 1_700_006_400 % 86_400;
        assert!(refused(&snap, &request("GET", "/api/x"), Duration::from_secs(day + 90 * 60)).is_some());
        assert!(refused(&snap, &request("GET", "/api/x"), Duration::from_secs(day + 121 * 60)).is_none());
    }

    fn body_of(resp: Response<Body>) -> serde_json::Value {
        let Either::Right(body) = resp.into_body() else { panic!("made here") };
        let mut body = std::pin::pin!(body);
        let waker = std::task::Waker::noop();
        let mut cx = std::task::Context::from_waker(waker);
        let std::task::Poll::Ready(Some(Ok(frame))) = body.as_mut().poll_frame(&mut cx) else { panic!("a full body is ready") };
        serde_json::from_slice(&frame.into_data().unwrap()).unwrap()
    }

    #[test]
    fn refusals_are_valid_json_in_the_python_order() {
        let t = Trace { request_id: "r\"1".into(), traceparent: String::new() };
        let rule = IpRule { allow: false, net: None, details_json: r#"{"rule_id":"r1","scope":"tenant"}"#.into() };
        let resp = forbidden(&Refusal::Ip(&rule), "/api/x", "10.1.2.3".parse().unwrap(), &t);
        let v = body_of(resp);
        assert_eq!(v["request_id"], "r\"1");
        assert_eq!(v["details"], serde_json::json!({"ip":"10.1.2.3","rule_id":"r1","scope":"tenant"}));
        assert_eq!(v["message"], "Request blocked by IP rule");

        let resp = rate_limited("minute", 5, 60, Some(r#"{"tenant_id":"acme","quota":{"requests_per_minute":5}}"#), &t);
        assert_eq!(resp.headers()["retry-after"], "60");
        let v = body_of(resp);
        assert_eq!(v["message"], "Rate limit exceeded: 5 requests per minute");
        assert_eq!(
            serde_json::to_string(&v["details"]).unwrap().len(),
            r#"{"limit":5,"window":"minute","retry_after":60,"tenant_id":"acme","quota":{"requests_per_minute":5}}"#.len()
        );
        assert_eq!(body_of(rate_limited("hour", 9, 3600, None, &t))["details"], serde_json::json!({"limit":9,"window":"hour","retry_after":3600}));
        assert_eq!(led_by("\"a\":1", "{}"), "{\"a\":1}");
    }

    #[test]
    fn an_upstream_is_parsed_once() {
        let u = Upstream::parse("http://127.0.0.1:9100/").unwrap();
        assert_eq!((u.text.as_str(), u.prefix.as_str(), u.authority.as_str()), ("http://127.0.0.1:9100", "", "127.0.0.1:9100"));
        assert_eq!(Upstream::parse("http://backend:80/base/").unwrap().prefix, "/base");
        assert!(Upstream::parse("https://backend").is_err());
        assert!(Upstream::parse("backend:80").is_err());
    }
}
