//! Request handling: serve what the data plane owns, hand the rest to the control plane.
//!
//! A request is handled natively only when everything that decides its outcome is something
//! the data plane reproduces exactly: a path that belongs to plain HTTP, MCP, A2A, AG-UI or ACP,
//! a key present in the snapshot that is active and unexpired, and a client that is not a
//! browser. For such a request the key's allowed paths, the IP rules, the policies, the checks on
//! the body at the protocol boundary, the key's rate limit and its tenant's quota are all
//! decided here, in the Python gateway's order and with its answers. Every other request is
//! forwarded unchanged to the control plane, so behaviour never differs; only speed does.
//! See DESIGN.md.
use bytes::Bytes;
use http_body_util::{BodyExt, Either, Full};
use hyper::body::{Body as _, Incoming};
use hyper::header::{HeaderName, HeaderValue, ACCEPT, AUTHORIZATION, CONTENT_LENGTH, CONTENT_TYPE, HOST, REFERER, UPGRADE, USER_AGENT};
use hyper::http::uri::{Authority, Scheme};
use hyper::{HeaderMap, Method, Request, Response, StatusCode, Uri, Version};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::TokioExecutor;
use std::borrow::Cow;
use std::collections::HashMap;
use std::convert::Infallible;
use std::net::{IpAddr, SocketAddr};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::accesslog::{self, AccessLog};
use crate::boundary::Verdict;
use crate::breaker::{self, Breaker};
use crate::govern::{self, IpRule, Policy, Scope, Subject};
use crate::report::{Event, Reporter, Row, Served};
use crate::snapshot::{hash_key, Digest32, KeyEntry, Snapshot, Wording};
use crate::sync::Link;

/// A body is either streamed from the other side or held here whole; neither is boxed. Responses
/// are streamed from upstream or made here; requests are streamed from the client, or read whole
/// when the gateway looks inside them.
pub type Body = Either<Incoming, Full<Bytes>>;

/// The protocols served natively. Each has its own backend; which one a path belongs to is
/// decided as the Python gateway's router decides it.
#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Protocol {
    Http,
    Mcp,
    A2a,
    AgUi,
    Acp,
}

/// The protocols whose backends come from the snapshot, in the order `Snapshot::routes` holds them.
pub const ROUTED: [Protocol; 4] = [Protocol::Mcp, Protocol::A2a, Protocol::AgUi, Protocol::Acp];

impl Protocol {
    /// The name the Python gateway gives the protocol: the `X-Protocol` header sent upstream and
    /// the key of its backend in the snapshot.
    pub fn name(self) -> &'static str {
        match self {
            Protocol::Http => "http",
            Protocol::Mcp => "mcp",
            Protocol::A2a => "a2a",
            Protocol::AgUi => "ag_ui",
            Protocol::Acp => "acp",
        }
    }

    fn route_index(self) -> Option<usize> {
        ROUTED.iter().position(|p| *p == self)
    }
}

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

/// Somewhere native requests are sent, with the circuit breaker that protects it. The control
/// plane keeps breakers of its own for what it serves.
pub struct Backend {
    pub upstream: Upstream,
    pub breaker: Breaker,
}

impl Backend {
    pub fn parse(text: &str) -> Result<Arc<Backend>, String> {
        Ok(Arc::new(Backend { upstream: Upstream::parse(text)?, breaker: Breaker::new() }))
    }
}

/// Every backend seen so far, by address. Two protocols sent to one address share its breaker,
/// as they do in the Python gateway, and a backend keeps its breaker across snapshots.
#[derive(Default)]
pub struct Backends(Mutex<HashMap<String, Arc<Backend>>>);

impl Backends {
    pub fn with(first: &Arc<Backend>) -> Backends {
        let known = Backends::default();
        known.0.lock().unwrap().insert(first.upstream.text.clone(), first.clone());
        known
    }

    /// The backend at this address, or `None` if it is not one this side can reach (not http://).
    fn at(&self, url: &str) -> Option<Arc<Backend>> {
        let key = url.trim_end_matches('/');
        let mut known = self.0.lock().unwrap();
        if let Some(found) = known.get(key) {
            return Some(found.clone());
        }
        let made = Backend::parse(key).ok()?;
        known.insert(key.to_owned(), made.clone());
        Some(made)
    }

    /// Give each protocol in the snapshot its backend.
    pub fn resolve(&self, snap: &mut Snapshot) {
        for (route, url) in snap.routes.iter_mut().zip(&snap.protocol_urls) {
            *route = url.as_deref().and_then(|u| self.at(u));
        }
    }
}

pub struct State {
    pub snapshot: arc_swap::ArcSwap<Snapshot>,
    /// Counts snapshot replacements, so a worker can tell cheaply that the one it holds is current.
    pub generation: AtomicU64,
    /// Credentials the Python gateway has just refused.
    pub denied: Mutex<Denied>,
    /// Where plain HTTP requests go.
    pub backend: Arc<Backend>,
    pub backends: Backends,
    pub control: Upstream,
    /// Present when the snapshot comes from the control plane; native serving needs it fresh.
    pub sync: Option<Arc<Link>>,
    /// Present with a control-plane link: what was served is reported back.
    pub reporter: Option<Arc<Reporter>>,
    /// Present when a line per native request was asked for.
    pub access_log: Option<AccessLog>,
}

impl State {
    pub fn replace(&self, mut snap: Snapshot) {
        self.backends.resolve(&mut snap);
        self.snapshot.store(Arc::new(snap));
        self.generation.fetch_add(1, Ordering::Release);
    }
}

/// One core's share of the data plane: its own connections to the upstreams, its own counters,
/// and its own handle on the snapshot, so that serving a request touches nothing another core
/// is writing to.
pub struct Worker {
    pub shared: Arc<State>,
    client: Client<HttpConnector, Body>,
    /// The snapshot as of `generation`, behind a reference count only this worker touches.
    held: Mutex<(u64, Arc<Arc<Snapshot>>)>,
    index: usize,
}

/// How long a credential the Python gateway refused is refused here without asking again.
const DENIED_FOR: Duration = Duration::from_secs(10);
/// The most refused credentials remembered. Past it, they are all forgotten and asked about again.
const DENIED_MAX: usize = 65_536;

/// Credentials (as digests) the Python gateway answered with its "invalid key" 401, each with the
/// generation it was learned under and when. An entry counts only while that generation is still
/// the current one, so any change to the snapshot (a key re-enabled, an expiry moved) and any
/// break in the link to the control plane makes every worker ask again. A key that does not exist
/// yet cannot be in here: nobody can present a key before the gateway has made it up.
///
/// Shared by all workers, so one answer from the Python gateway serves them all. Only requests
/// with a credential this side does not know come here; requests with a working key never do.
#[derive(Default)]
pub struct Denied(HashMap<Digest32, (u64, Instant)>);

impl Denied {
    fn holds(&self, digest: &Digest32, generation: u64, now: Instant) -> bool {
        matches!(self.0.get(digest), Some((g, at)) if *g == generation && now.duration_since(*at) < DENIED_FOR)
    }

    fn learn(&mut self, digest: Digest32, generation: u64, now: Instant) {
        if self.0.len() >= DENIED_MAX {
            self.0.clear();
        }
        self.0.insert(digest, (generation, now));
    }
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

    /// The current snapshot and the generation it belongs to.
    fn snapshot(&self) -> (u64, Arc<Arc<Snapshot>>) {
        let generation = self.shared.generation.load(Ordering::Acquire);
        let mut held = self.held.lock().unwrap();
        if held.0 != generation {
            *held = (generation, Arc::new(self.shared.snapshot.load_full()));
        }
        (generation, held.1.clone())
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

/// First path segments after `/api/` that the control plane keeps although its router would call
/// them plain HTTP.
const KEPT_BY_CONTROL: [&str; 2] = ["ws", "agents"];

/// The protocol a path belongs to, decided in the order the Python gateway's router decides it
/// (prefixes are compared as text, so `/mcpx` is MCP there and here). `None` for what the control
/// plane keeps: MQTT, WebSocket, gRPC, GraphQL, its own endpoints and anything without a prefix.
fn classify(path: &str) -> Option<Protocol> {
    let under = |name: &str| path.starts_with(name) || path.strip_prefix("/api").map(|rest| rest.starts_with(name)).unwrap_or(false);
    if under("/mcp") {
        Some(Protocol::Mcp)
    } else if under("/a2a") {
        Some(Protocol::A2a)
    } else if under("/agui") {
        Some(Protocol::AgUi)
    } else if under("/acp") {
        Some(Protocol::Acp)
    } else if under("/mqtt") || path.starts_with("/ws") || path.starts_with("/websocket") || under("/grpc") || under("/graphql") {
        None
    } else {
        let first = path.strip_prefix("/api/")?.split('/').next().unwrap_or("");
        (!KEPT_BY_CONTROL.contains(&first)).then_some(Protocol::Http)
    }
}

/// The path an ACP or AG-UI backend is sent: the gateway's prefix (`/acp`, `/api/acp`, `/agui`,
/// `/api/agui`) is taken off, so the server sees the paths its specification names.
fn upstream_path(path: &str, protocol: Protocol) -> &str {
    let segment = match protocol {
        Protocol::Acp => "/acp",
        Protocol::AgUi => "/agui",
        _ => return path,
    };
    for base in [path, path.strip_prefix("/api").unwrap_or("")] {
        match base.strip_prefix(segment) {
            Some("") => return "/",
            Some(rest) if rest.starts_with('/') => return rest,
            _ => {}
        }
    }
    path
}

/// `/v<digits>[.<digits>]` followed by `/` or nothing: a path that names its API version, which
/// the Python gateway resolves differently from one that does not.
fn names_a_version(path: &str) -> bool {
    let Some(rest) = path.strip_prefix("/v") else { return false };
    let digits = |s: &str| s.bytes().take_while(u8::is_ascii_digit).count();
    let whole = digits(rest);
    if whole == 0 {
        return false;
    }
    let mut rest = &rest[whole..];
    if let Some(fraction) = rest.strip_prefix('.') {
        let n = digits(fraction);
        if n > 0 {
            rest = &fraction[n..];
        }
    }
    rest.is_empty() || rest.starts_with('/')
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
    protocol: Protocol,
    /// The protocol's backend, when it is not the plain HTTP one.
    backend: Option<&'a Backend>,
    entry: &'a KeyEntry,
    version: Option<&'a str>,
    /// The client's address, as the Python gateway would resolve it.
    ip: IpAddr,
    method: &'static str,
}

/// A request refused here with a 401: it carries no credential that works.
struct Unauthenticated<'a> {
    wording: &'a Wording,
    version: Option<&'a str>,
    ip: IpAddr,
    method: &'static str,
}

/// What to do with a request.
enum Decision<'a> {
    Native(Native<'a>),
    Refuse(Unauthenticated<'a>),
    /// The Python gateway answers, which is always right: it is the answer whenever this side
    /// cannot be sure of deciding the same. With a digest: the request carries one credential this
    /// side does not know, and if the Python gateway refuses it, that is worth remembering.
    Ask(Option<Digest32>),
}

/// How long past its expiry a key must be before it is refused here; nearer than that, the two
/// clocks might disagree, and the Python gateway decides.
const EXPIRY_MARGIN_SECS: i64 = 2;

/// An `Authorization` value as the Python gateway turns it into a key: every `Bearer ` taken out.
fn without_bearer(value: &[u8]) -> Cow<'_, [u8]> {
    const WORD: &[u8] = b"Bearer ";
    if !value.windows(WORD.len()).any(|w| w == WORD) {
        return Cow::Borrowed(value);
    }
    let (mut out, mut i) = (Vec::with_capacity(value.len()), 0);
    while i < value.len() {
        if value[i..].starts_with(WORD) {
            i += WORD.len();
        } else {
            out.push(value[i]);
            i += 1;
        }
    }
    Cow::Owned(out)
}

/// Decide what happens to a request. `denied` says whether the Python gateway has just refused
/// the credential with this digest.
fn decide<'a, B>(snap: &'a Snapshot, peer: SocketAddr, req: &Request<B>, now: u64, denied: impl Fn(&Digest32) -> bool) -> Decision<'a> {
    const PYTHON: Decision<'static> = Decision::Ask(None);
    let uri = req.uri();
    let path = uri.path();
    // An escaped or non-ASCII path reads differently once decoded, and what the key may reach
    // and what a policy matches are decided on the decoded one.
    if uri.authority().is_some() || !path.is_ascii() || path.contains('%') {
        return PYTHON;
    }
    let Some(protocol) = classify(path) else { return PYTHON };
    if snap.fallthrough_prefixes.iter().any(|p| path.starts_with(p.as_str())) || wants_html(req.headers()) {
        return PYTHON;
    }
    // A request to change protocol (a WebSocket handshake) is not an ordinary request there.
    if req.headers().contains_key(UPGRADE) {
        return PYTHON;
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
        return PYTHON;
    };
    // The version is echoed on every answer, refusals included; one that adds more than its
    // number (a deprecation, a sunset) is the Python gateway's to announce.
    let version = match resolve_version(snap, req.headers()) {
        Some((_, false)) => return PYTHON,
        found => found.map(|(v, _)| v),
    };
    let Some(ip) = govern::client_ip(peer, req.headers(), &snap.trusted_proxies) else { return PYTHON };
    let refuse = |wording: &'a Wording| Decision::Refuse(Unauthenticated { wording, version, ip, method });

    // The key: the gateway's own header first, `Authorization` when that one is absent or empty.
    let header = |name: &HeaderName| req.headers().get(name).map(|v| v.as_bytes()).filter(|v| !v.is_empty());
    let (own, authorization) = (header(&snap.key_header), header(&AUTHORIZATION));
    let (credential, source) = match (own, authorization) {
        (None, None) => return snap.refusals.as_ref().map(|r| refuse(&r.missing_key)).unwrap_or(PYTHON),
        (Some(key), _) => (Cow::Borrowed(key), b'K'),
        (None, Some(value)) => (without_bearer(value), b'A'),
    };
    // Beyond ASCII the two sides would not hash the same bytes.
    if credential.is_empty() || !credential.is_ascii() {
        return PYTHON;
    }
    // With a key in its own header that does not work, an `Authorization` header may still let
    // the request in (an OAuth2 token): only the Python gateway can tell.
    let only_credential = own.is_none() || authorization.is_none();
    let invalid = snap.refusals.as_ref().filter(|_| only_credential).map(|r| &r.invalid_key);
    let Some(entry) = snap.keys.get(&hash_key(&credential)) else {
        let Some(wording) = invalid else { return PYTHON };
        // Not a key this side knows. That alone proves nothing: it may have been created a
        // moment ago. It is refused here only once the Python gateway has refused it.
        let mut text = Vec::with_capacity(credential.len() + 1);
        text.push(source);
        text.extend_from_slice(own.or(authorization).unwrap_or_default());
        let digest = hash_key(&text);
        return if denied(&digest) { refuse(wording) } else { Decision::Ask(Some(digest)) };
    };
    if !entry.is_active || !entry.native_ok {
        return PYTHON;
    }
    match entry.expires_at {
        Some(e) if e + EXPIRY_MARGIN_SECS < now as i64 => return invalid.map(refuse).unwrap_or(PYTHON),
        Some(e) if e <= now as i64 => return PYTHON,
        _ => {}
    }
    // A protocol without a backend this side can reach is the Python gateway's to answer.
    let backend = match protocol.route_index() {
        Some(i) => match snap.routes[i].as_deref() {
            Some(backend) => Some(backend),
            None => return PYTHON,
        },
        None => None,
    };
    if names_a_version(upstream_path(path, protocol)) {
        return PYTHON;
    }
    Decision::Native(Native { protocol, backend, entry, version, ip, method })
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

/// The client stopped sending its body part-way; there is no one left to answer properly.
fn body_unreadable() -> Response<Body> {
    let mut resp = Response::new(full("{\"detail\":\"There was an error parsing the body\"}"));
    *resp.status_mut() = StatusCode::BAD_REQUEST;
    resp.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    resp
}

/// The headers every answer made or passed on here carries.
fn stamp(resp: &mut Response<Body>, t: &Trace, seconds: f64, version: Option<&str>) {
    let h = resp.headers_mut();
    // Tells operators (and the conformance suite) which plane answered.
    h.insert("x-served-by", HeaderValue::from_static("dataplane"));
    if let Ok(v) = HeaderValue::from_str(&format!("{seconds:.3}")) {
        h.insert("x-response-time", v);
    }
    if let Some(v) = version.and_then(|v| HeaderValue::from_str(v).ok()) {
        h.insert("x-api-version", v);
    }
    if let Ok(v) = HeaderValue::from_str(&t.request_id) {
        h.insert("x-request-id", v);
    }
    if let Ok(v) = HeaderValue::from_str(&t.traceparent) {
        h.insert("traceparent", v);
    }
}

/// Answer a request that carries no credential that works, and report it as the Python gateway
/// records the ones it refuses: counted under a bounded path label, logged without a key.
fn unauthenticated<B>(w: &Worker, u: &Unauthenticated, req: &Request<B>, started: Instant, at: Duration) -> Response<Body> {
    let st = &*w.shared;
    let t = trace_for(req.headers());
    let path = req.uri().path();
    let mut resp = envelope(StatusCode::UNAUTHORIZED, "AUTHENTICATION_ERROR", &u.wording.message_json, &u.wording.details_json, &t, true);
    let seconds = started.elapsed().as_secs_f64();
    let bytes_out = resp.body().size_hint().exact().unwrap_or(0);
    stamp(&mut resp, &t, seconds, u.version);
    let user_agent = req.headers().get(USER_AGENT).and_then(|v| v.to_str().ok());
    if let Some(reporter) = &st.reporter {
        let served = Served {
            method: u.method,
            path,
            status: 401,
            seconds,
            key_id: "",
            tenant: None,
            rejected: true,
            error_code: None,
            error_type: None,
        };
        reporter.record(w.index, &served, at.as_secs(), || Row {
            request_id: t.request_id.clone(),
            method: u.method,
            path: path.to_owned(),
            client_ip: u.ip.to_string(),
            api_key_id: None,
            tenant_id: None,
            status: 401,
            duration_ms: seconds * 1000.0,
            user_agent: user_agent.map(str::to_owned),
            error: Some("AUTHENTICATION_ERROR".to_owned()),
        });
    }
    if let Some(log) = &st.access_log {
        log.write(accesslog::line(&accesslog::Entry {
            at,
            request_id: &t.request_id,
            traceparent: &t.traceparent,
            key_id: None,
            tenant: None,
            ip: u.ip,
            method: u.method,
            path,
            query: req.uri().query().unwrap_or(""),
            status: 401,
            bytes: bytes_out,
            seconds,
            user_agent,
            referrer: req.headers().get(REFERER).and_then(|v| v.to_str().ok()),
        }));
    }
    resp
}

fn announce(st: &State, backend: &Backend, cfg: &breaker::Config, tr: breaker::Transition) {
    eprintln!("dataplane: circuit breaker for {} is {} (was {})", backend.upstream.text, tr.state.name(), tr.previous.name());
    if let Some(reporter) = &st.reporter {
        reporter.event(Event::CircuitBreaker {
            state: tr.state.name(),
            previous_state: tr.previous.name(),
            backend_url: backend.upstream.text.clone(),
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
    let (generation, snap) = w.snapshot();
    let at = wall();
    let now = at.as_secs();
    // Fail closed: if the snapshot is not known to be current, the Python gateway decides.
    let trusted = st.sync.as_ref().map(|s| s.fresh()).unwrap_or(true);
    let decision = match trusted {
        true => decide(&snap, peer, &req, now, |digest| st.denied.lock().unwrap().holds(digest, generation, started)),
        false => Decision::Ask(None),
    };
    let n = match decision {
        Decision::Native(n) => n,
        Decision::Refuse(u) => return Ok(unauthenticated(&w, &u, &req, started, at)),
        Decision::Ask(credential) => {
            let (resp, refused) = ask_control(&w, peer, req.map(Either::Left)).await;
            if let Some(digest) = credential.filter(|_| refused && resp.status() == StatusCode::UNAUTHORIZED) {
                st.denied.lock().unwrap().learn(digest, generation, Instant::now());
            }
            return Ok(resp);
        }
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
    let backend = n.backend.unwrap_or(&st.backend);

    let refused = refusal(&snap, &n, path, at);
    let mut req = req.map(Either::Left);
    // Where the Python gateway looks inside the body, so does this side, before anything is
    // counted. A body that is certain to pass goes on; anything else is the Python gateway's to
    // answer, in its own words, so the request is handed over whole.
    if refused.is_none() && snap.boundary.reads_body(n.method, path) {
        match req.body().size_hint().exact() {
            Some(0) => {}
            Some(len) if len <= snap.boundary.max_body(n.method, path) => {
                let (parts, body) = req.into_parts();
                let Ok(read) = body.collect().await else { return Ok(body_unreadable()) };
                let bytes = read.to_bytes();
                let verdict = snap.boundary.check(n.method, path, &bytes);
                req = Request::from_parts(parts, full(bytes));
                if verdict == Verdict::AskPython {
                    return Ok(to_control(&w, peer, req).await);
                }
            }
            // No length given, or too long to hold here.
            _ => return Ok(to_control(&w, peer, req).await),
        }
    }

    let (mut ticket, mut rejected, mut error_type) = (None, false, None);
    let (mut resp, error) = if let Some(r) = refused {
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
                if !backend.breaker.allow(&cb, Instant::now()) {
                    (circuit_open(&t), Some("SERVICE_UNAVAILABLE"))
                } else {
                    let native = Sent {
                        trace: &t,
                        version: n.version,
                        inject_request_id: snap.inject_request_id,
                        protocol: n.protocol,
                        path: Some(upstream_path(path, n.protocol)).filter(|p| *p != path),
                    };
                    match forward(&w, &backend.upstream, peer, req, Some(native), Some(snap.upstream_timeout)).await {
                        Ok(resp) => {
                            if let Some(tr) = backend.breaker.success(&cb) {
                                announce(st, backend, &cb, tr);
                            }
                            (resp, None)
                        }
                        Err(unanswered) => {
                            if let Some(tr) = backend.breaker.failure(&cb, Instant::now()) {
                                announce(st, backend, &cb, tr);
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
    stamp(&mut resp, &t, seconds, n.version);
    if let Some(v) = tenant.and_then(|tn| tn.header.clone()) {
        resp.headers_mut().insert("x-tenant-id", v);
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
            key_id: Some(&entry.id),
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
    protocol: Protocol,
    /// The path to ask the backend for, when it is not the one the client asked for.
    path: Option<&'a str>,
}

/// Hand a request to the Python gateway as it came; its answer is the answer. Also says whether
/// the Python gateway marked the answer as its refusal of the credential itself (a note for this
/// side only, taken off before the answer goes to the client).
async fn ask_control(w: &Worker, peer: SocketAddr, req: Request<Body>) -> (Response<Body>, bool) {
    match forward(w, &w.shared.control, peer, req, None, None).await {
        Ok(mut resp) => {
            let refused = resp.headers_mut().remove("x-dataplane-auth").is_some();
            (resp, refused)
        }
        Err(_) => (control_unreachable(), false),
    }
}

async fn to_control(w: &Worker, peer: SocketAddr, req: Request<Body>) -> Response<Body> {
    ask_control(w, peer, req).await.0
}

/// Forward to `to`. With `native`, the Python gateway's upstream headers are reproduced.
/// Without, the request is handed to the control plane as it came, plus the client's address.
/// `timeout` bounds the wait for the response to start.
async fn forward(
    w: &Worker,
    to: &Upstream,
    peer: SocketAddr,
    mut req: Request<Body>,
    native: Option<Sent<'_>>,
    timeout: Option<Duration>,
) -> Result<Response<Body>, Unanswered> {
    let mut parts = std::mem::take(req.uri_mut()).into_parts();
    parts.scheme = Some(to.scheme.clone());
    parts.authority = Some(to.authority.clone());
    let asked = parts.path_and_query.as_ref();
    let target = match native.as_ref().and_then(|sent| sent.path) {
        Some(path) => Some(match asked.and_then(|p| p.query()) {
            Some(query) => format!("{}{path}?{query}", to.prefix),
            None => format!("{}{path}", to.prefix),
        }),
        None if asked.is_none() || !to.prefix.is_empty() => Some(format!("{}{}", to.prefix, asked.map(|p| p.as_str()).unwrap_or("/"))),
        None => None,
    };
    if let Some(target) = target {
        parts.path_and_query = Some(target.parse().map_err(|_| Unanswered::Broken)?);
    }
    *req.uri_mut() = Uri::from_parts(parts).map_err(|_| Unanswered::Broken)?;
    *req.version_mut() = Version::HTTP_11;
    let native_answer = native.is_some();
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
            headers.insert("x-protocol", HeaderValue::from_static(sent.protocol.name()));
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
            // A backend cannot leave notes for this side; the Python gateway drops this one too.
            if native_answer {
                rp.headers.remove("x-dataplane-auth");
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
    fn a_path_belongs_to_the_protocol_the_python_router_gives_it() {
        for (path, protocol) in [
            ("/api/users", Some(Protocol::Http)),
            ("/api/v1/orders/7", Some(Protocol::Http)),
            ("/mcp", Some(Protocol::Mcp)),
            ("/mcp/tools", Some(Protocol::Mcp)),
            ("/api/mcp/x", Some(Protocol::Mcp)),
            ("/api/mcpx/y", Some(Protocol::Mcp)),
            ("/a2a/v1/message:send", Some(Protocol::A2a)),
            ("/api/a2a", Some(Protocol::A2a)),
            ("/agui", Some(Protocol::AgUi)),
            ("/api/agui/run", Some(Protocol::AgUi)),
            ("/acp/runs", Some(Protocol::Acp)),
            ("/api/acp/agents", Some(Protocol::Acp)),
        ] {
            assert_eq!(classify(path), protocol, "{path}");
        }
        for path in [
            "/admin/keys", "/oauth2/token", "/health", "/docs", "/", "/api", "/apix/y", "/rest/x", "/users",
            "/mqtt/publish", "/api/mqtt/x", "/ws/x", "/websocket", "/wss", "/grpc/x", "/api/grpc", "/graphql", "/api/graphql",
            "/api/ws/x", "/api/agents/a", "/agents/a/mcp",
        ] {
            assert_eq!(classify(path), None, "{path}");
        }
    }

    #[test]
    fn acp_and_ag_ui_backends_see_their_own_paths() {
        for (path, protocol, sent) in [
            ("/acp", Protocol::Acp, "/"),
            ("/acp/runs/7", Protocol::Acp, "/runs/7"),
            ("/api/acp/runs", Protocol::Acp, "/runs"),
            ("/acpx/runs", Protocol::Acp, "/acpx/runs"),
            ("/agui/", Protocol::AgUi, "/"),
            ("/api/agui", Protocol::AgUi, "/"),
            ("/agui/run", Protocol::AgUi, "/run"),
            ("/mcp/tools", Protocol::Mcp, "/mcp/tools"),
            ("/a2a/x", Protocol::A2a, "/a2a/x"),
            ("/api/users", Protocol::Http, "/api/users"),
        ] {
            assert_eq!(upstream_path(path, protocol), sent, "{path}");
        }
    }

    #[test]
    fn a_version_in_the_path_is_recognised_as_python_recognises_it() {
        for path in ["/v1", "/v1/", "/v2/runs", "/v10.5/x", "/v1./x"] {
            assert_eq!(names_a_version(path), path != "/v1./x", "{path}");
        }
        for path in ["/", "/runs", "/v", "/vx/1", "/v1x", "/v1.2.3", "/version"] {
            assert!(!names_a_version(path), "{path}");
        }
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
        let mut snap = Snapshot::from_json(&format!(
            r#"{{"keys":[{{"hash":"{hex}","id":"k1","rate_limit_per_minute":100,"rate_limit_per_hour":1000{key_extra}}}]{extra}}}"#
        ))
        .unwrap();
        Backends::default().resolve(&mut snap);
        snap
    }

    /// The request as served here, or `None` for anything else.
    fn route<'a>(snap: &'a Snapshot, peer: SocketAddr, req: &Request<()>, now: u64) -> Option<Native<'a>> {
        match decide(snap, peer, req, now, |_| false) {
            Decision::Native(n) => Some(n),
            _ => None,
        }
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
    fn a_protocol_is_served_here_only_with_a_backend_this_side_can_reach() {
        let snap = governed(r#","backends":{"mcp":"http://127.0.0.1:9201/","a2a":"http://127.0.0.1:9201","ag_ui":"https://agui.example","acp":null}"#, "");
        let n = route(&snap, peer(), &request("POST", "/mcp/rpc"), 0).unwrap();
        assert_eq!((n.protocol, n.backend.unwrap().upstream.text.as_str()), (Protocol::Mcp, "http://127.0.0.1:9201"));
        let a2a = route(&snap, peer(), &request("GET", "/api/a2a/tasks"), 0).unwrap();
        assert!(std::ptr::eq(n.backend.unwrap(), a2a.backend.unwrap()), "one address, one breaker");
        assert!(route(&snap, peer(), &request("POST", "/agui/run"), 0).is_none(), "https: Python's");
        assert!(route(&snap, peer(), &request("POST", "/acp/runs"), 0).is_none(), "not configured: Python's");
        let http = route(&snap, peer(), &request("GET", "/api/users"), 0).unwrap();
        assert!(http.protocol == Protocol::Http && http.backend.is_none());
        assert!(route(&governed("", ""), peer(), &request("POST", "/mcp/rpc"), 0).is_none(), "no backends in the snapshot");
    }

    #[test]
    fn a_stripped_path_that_names_a_version_goes_to_python() {
        let snap = governed(r#","backends":{"acp":"http://127.0.0.1:9201"}"#, "");
        assert!(route(&snap, peer(), &request("GET", "/acp/runs"), 0).is_some());
        assert!(route(&snap, peer(), &request("GET", "/acp/v2/runs"), 0).is_none());
        assert!(route(&snap, peer(), &request("GET", "/api/acp/v1"), 0).is_none());
    }

    #[test]
    fn control_plane_prefixes_win() {
        let snap = governed(r#","fallthrough_prefixes":["/api/custom"]"#, "");
        assert!(route(&snap, peer(), &request("GET", "/api/custom/x"), 0).is_none());
        assert!(route(&snap, peer(), &request("GET", "/api/other"), 0).is_some());
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

    const REFUSALS: &str = r#","refusals":{"missing_key":{"message_json":"\"API key required\"","details_json":"{\"hint\":\"h1\"}"},
        "invalid_key":{"message_json":"\"Invalid or inactive API key.\"","details_json":"{\"hint\":\"h2\"}"}}"#;

    fn with_headers(headers: &[(&str, &str)]) -> Request<()> {
        let mut b = Request::builder().uri("/api/users");
        for (name, value) in headers {
            b = b.header(*name, *value);
        }
        b.body(()).unwrap()
    }

    /// "native", "ask", "learn" (ask, and remember a refusal) or the message of the 401 made here.
    fn outcome(snap: &Snapshot, req: &Request<()>, now: u64, denied: bool) -> String {
        match decide(snap, peer(), req, now, |_| denied) {
            Decision::Native(_) => "native".into(),
            Decision::Ask(None) => "ask".into(),
            Decision::Ask(Some(_)) => "learn".into(),
            Decision::Refuse(u) => u.wording.message_json.clone(),
        }
    }

    #[test]
    fn a_key_is_read_from_its_header_or_from_authorization() {
        let snap = governed(REFUSALS, "");
        for headers in [
            vec![("x-api-key", KEY)],
            vec![("authorization", "Bearer sk-test")],
            vec![("authorization", "sk-test")],
            vec![("x-api-key", ""), ("authorization", "Bearer sk-test")],
            vec![("x-api-key", KEY), ("authorization", "Bearer something-else")],
            vec![("authorization", "Bearer Bearer sk-test")],
            vec![("authorization", "sk-Bearer test")],
        ] {
            assert_eq!(outcome(&snap, &with_headers(&headers), 0, false), "native", "{headers:?}");
        }
        // `bearer` in another case is not taken out, as in Python: this is not the key.
        assert_eq!(outcome(&snap, &with_headers(&[("authorization", "bearer sk-test")]), 0, false), "learn");
    }

    #[test]
    fn the_key_header_is_the_one_the_gateway_is_configured_with() {
        let snap = governed(&format!(r#","key_header":"X-Gateway-Key"{REFUSALS}"#), "");
        assert_eq!(outcome(&snap, &with_headers(&[("x-gateway-key", KEY)]), 0, false), "native");
        assert_eq!(outcome(&snap, &with_headers(&[("x-api-key", KEY)]), 0, false), "\"API key required\"");
    }

    #[test]
    fn a_request_without_a_credential_is_refused_here() {
        let snap = governed(REFUSALS, "");
        assert_eq!(outcome(&snap, &with_headers(&[]), 0, false), "\"API key required\"");
        assert_eq!(outcome(&snap, &with_headers(&[("x-api-key", "")]), 0, false), "\"API key required\"");
        // Without the wording (a plugin may authenticate some other way), the Python gateway answers.
        assert_eq!(outcome(&governed("", ""), &with_headers(&[]), 0, false), "ask");
        // What the Python gateway does not treat as a gateway request is not refused here either.
        for (method, uri) in [("GET", "/health"), ("GET", "/admin/api-keys"), ("OPTIONS", "/api/users"), ("GET", "/agents/a"), ("GET", "/api/a%20b")] {
            let req = Request::builder().method(method).uri(uri).body(()).unwrap();
            assert_eq!(outcome(&snap, &req, 0, false), "ask", "{method} {uri}");
        }
        let browser = Request::builder().uri("/api/users").header("accept", "text/html").body(()).unwrap();
        assert_eq!(outcome(&snap, &browser, 0, false), "ask");
        let handshake = Request::builder().uri("/api/users").header("upgrade", "websocket").header("x-api-key", KEY).body(()).unwrap();
        assert_eq!(outcome(&snap, &handshake, 0, false), "ask");
        // A path the gateway answers itself without a key (its health check, moved under /api).
        let moved = governed(&format!(r#","fallthrough_prefixes":["/api/health"]{REFUSALS}"#), "");
        assert_eq!(outcome(&moved, &Request::builder().uri("/api/health").body(()).unwrap(), 0, false), "ask");
    }

    #[test]
    fn an_unknown_credential_is_refused_here_only_after_python_refused_it() {
        let snap = governed(REFUSALS, "");
        let unknown = with_headers(&[("x-api-key", "sk-other")]);
        assert_eq!(outcome(&snap, &unknown, 0, false), "learn");
        assert_eq!(outcome(&snap, &unknown, 0, true), "\"Invalid or inactive API key.\"");
        assert_eq!(outcome(&snap, &with_headers(&[("authorization", "Bearer sk-other")]), 0, true), "\"Invalid or inactive API key.\"");
        // With a second credential the Python gateway may still let the request in.
        let two = with_headers(&[("x-api-key", "sk-other"), ("authorization", "Bearer token")]);
        assert_eq!(outcome(&snap, &two, 0, true), "ask");
        // Beyond ASCII, and without the wording, it is not this side's call.
        assert_eq!(outcome(&snap, &with_headers(&[("x-api-key", "sk-caf\u{e9}")]), 0, true), "ask");
        assert_eq!(outcome(&governed("", ""), &unknown, 0, true), "ask");
    }

    #[test]
    fn the_same_text_in_another_header_is_another_credential() {
        let snap = governed(REFUSALS, "");
        let digest = |req: &Request<()>| match decide(&snap, peer(), req, 0, |_| false) {
            Decision::Ask(Some(d)) => d,
            _ => panic!("an unknown credential"),
        };
        let own = digest(&with_headers(&[("x-api-key", "token-1")]));
        assert_ne!(own, digest(&with_headers(&[("authorization", "token-1")])));
        assert_ne!(digest(&with_headers(&[("authorization", "Bearer token-1")])), digest(&with_headers(&[("authorization", "token-1")])));
        assert_eq!(own, digest(&with_headers(&[("x-api-key", "token-1")])));
    }

    #[test]
    fn an_expired_key_is_refused_here_once_it_is_clearly_past() {
        let snap = governed(REFUSALS, r#","expires_at":1000"#);
        let req = with_headers(&[("x-api-key", KEY)]);
        assert_eq!(outcome(&snap, &req, 999, false), "native");
        assert_eq!(outcome(&snap, &req, 1000, false), "ask", "at the boundary the clocks may differ");
        assert_eq!(outcome(&snap, &req, 1002, false), "ask");
        assert_eq!(outcome(&snap, &req, 1003, false), "\"Invalid or inactive API key.\"");
        let two = with_headers(&[("x-api-key", KEY), ("authorization", "Bearer token")]);
        assert_eq!(outcome(&snap, &two, 1003, false), "ask");
    }

    #[test]
    fn a_refusal_is_remembered_for_a_while_and_only_under_its_generation() {
        let (mut denied, digest, t0) = (Denied::default(), hash_key(b"Ksk-other"), Instant::now());
        assert!(!denied.holds(&digest, 7, t0));
        denied.learn(digest, 7, t0);
        assert!(denied.holds(&digest, 7, t0 + Duration::from_secs(9)));
        assert!(!denied.holds(&digest, 7, t0 + DENIED_FOR));
        assert!(!denied.holds(&digest, 8, t0), "the snapshot changed, or the link broke: ask again");
        assert!(!denied.holds(&hash_key(b"Ask-other"), 7, t0));
        for i in 0..DENIED_MAX as u32 {
            denied.learn(hash_key(&i.to_le_bytes()), 7, t0);
        }
        assert!(denied.0.len() <= DENIED_MAX && !denied.holds(&digest, 7, t0), "full: everything is forgotten");
    }

    #[test]
    fn the_401_is_the_python_envelope() {
        let snap = governed(REFUSALS, "");
        let req = with_headers(&[]);
        let Decision::Refuse(u) = decide(&snap, peer(), &req, 0, |_| false) else { panic!("refused") };
        let t = Trace { request_id: "r1".into(), traceparent: String::new() };
        let resp = envelope(StatusCode::UNAUTHORIZED, "AUTHENTICATION_ERROR", &u.wording.message_json, &u.wording.details_json, &t, true);
        assert_eq!(resp.status(), 401);
        let v = body_of(resp);
        assert_eq!((v["error"].as_str(), v["message"].as_str(), &v["details"]), (Some("AUTHENTICATION_ERROR"), Some("API key required"), &serde_json::json!({"hint":"h1"})));
        assert_eq!(u.version, Some("1"));
    }

    #[test]
    fn bearer_is_taken_out_wherever_it_stands() {
        for (value, key) in [("Bearer abc", "abc"), ("abc", "abc"), ("Bearer Bearer abc", "abc"), ("aBearer bc", "abc"), ("Bearer", "Bearer"), ("BearerBearer  x", "Bearer x"), ("bearer abc", "bearer abc")] {
            assert_eq!(&*without_bearer(value.as_bytes()), key.as_bytes(), "{value}");
        }
    }

    #[test]
    fn a_backend_keeps_its_breaker_across_snapshots() {
        let known = Backends::default();
        let (mut one, mut two) = (governed(r#","backends":{"mcp":"http://b:1"}"#, ""), governed(r#","backends":{"mcp":"http://b:1/","acp":"http://b:2"}"#, ""));
        known.resolve(&mut one);
        known.resolve(&mut two);
        assert!(Arc::ptr_eq(one.routes[0].as_ref().unwrap(), two.routes[0].as_ref().unwrap()));
        assert!(!Arc::ptr_eq(two.routes[0].as_ref().unwrap(), two.routes[3].as_ref().unwrap()));
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
