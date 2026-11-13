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
use hyper::header::{HeaderName, HeaderValue, ACCEPT, AUTHORIZATION, CONNECTION, CONTENT_LENGTH, CONTENT_TYPE, HOST, ORIGIN, REFERER, UPGRADE, USER_AGENT};
use hyper::http::uri::{Authority, Scheme};
use hyper::{HeaderMap, Method, Request, Response, StatusCode, Uri, Version};
use hyper_util::client::legacy::{connect::HttpConnector, Client};
use hyper_util::rt::{TokioExecutor, TokioIo};
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
use crate::snapshot::{hash_key, Digest32, KeyEntry, Readiness, Snapshot, Wording};
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
    /// gRPC calls, which arrive over HTTP/2 and are told apart by their content type, not by path.
    Grpc,
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
            Protocol::Grpc => "grpc",
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
        // A WebSocket is opened with an HTTP request to the same address.
        snap.websocket = snap.websocket_url.as_deref().and_then(|u| self.at(&u.replacen("ws://", "http://", 1)));
        snap.grpc = snap.grpc_url.as_deref().and_then(|u| self.at(u));
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
    /// Connections to gRPC backends, which are spoken to in HTTP/2 from the first byte.
    grpc: Client<HttpConnector, Body>,
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
        let mut connector = HttpConnector::new();
        connector.set_nodelay(true);
        connector.set_connect_timeout(Some(GRPC_CONNECT_TIMEOUT));
        let grpc = Client::builder(TokioExecutor::new()).http2_only(true).pool_idle_timeout(Duration::from_secs(90)).build(connector);
        let held = Mutex::new((shared.generation.load(Ordering::Acquire), Arc::new(shared.snapshot.load_full())));
        Worker { shared, client, grpc, held, index }
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
/// puts in error bodies (it leaves the fraction out when it is zero), or with `log`,
/// `YYYY-MM-DDTHH:MM:SS.mmmZ`, the shape of its log lines.
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
    } else if micros == 0 {
        format!("{year:04}-{month:02}-{day:02}T{h:02}:{m:02}:{s:02}")
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
    // A request to change protocol (a WebSocket handshake) is not an ordinary request there, and
    // one from a web page in another origin is answered with the gateway's cross-origin headers.
    if req.headers().contains_key(UPGRADE) || req.headers().contains_key(ORIGIN) {
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

/// A WebSocket handshake this side relays itself.
struct Relay<'a> {
    backend: &'a Backend,
    entry: &'a KeyEntry,
    /// What the backend is asked for: the path after `/ws`, and the query.
    target: String,
}

/// Whether a request is a WebSocket handshake for `/ws/...` that is certain to be let in: the
/// key is in its header, known, working and allowed this path. Anything else, a key in the query
/// included, is the Python gateway's to judge; it relays or refuses in its own way.
fn relay<'a, B>(snap: &'a Snapshot, req: &Request<B>, now: u64) -> Option<Relay<'a>> {
    let backend = snap.websocket.as_deref()?;
    let (uri, headers) = (req.uri(), req.headers());
    let path = uri.path();
    let rest = path.strip_prefix("/ws/")?;
    if req.method() != Method::GET || uri.authority().is_some() || !path.is_ascii() || path.contains('%') {
        return None;
    }
    let one = |name: &str| {
        let mut all = headers.get_all(name).iter();
        all.next().filter(|_| all.next().is_none())
    };
    let handshake = one("upgrade").is_some_and(|v| v.as_bytes().eq_ignore_ascii_case(b"websocket"))
        && one("sec-websocket-version").is_some_and(|v| v == "13")
        && one("sec-websocket-key").is_some();
    let query = uri.query().unwrap_or("");
    if !handshake || query.contains("api_key") {
        return None;
    }
    let key = one("x-api-key").map(|v| v.as_bytes()).filter(|k| !k.is_empty() && k.is_ascii())?;
    let entry = snap.keys.get(&hash_key(key))?;
    let expired = entry.expires_at.is_some_and(|e| e <= now as i64);
    if !entry.is_active || !entry.native_ok || expired || entry.scope.as_ref().is_some_and(|s| !s.allows(path)) {
        return None;
    }
    let prefix = &backend.upstream.prefix;
    let target = if query.is_empty() { format!("{prefix}/{rest}") } else { format!("{prefix}/{rest}?{query}") };
    Some(Relay { backend, entry, target })
}

/// What of the caller's handshake the backend is sent, as the Python gateway's relay sends it:
/// the handshake itself, the subprotocols offered, and the caller's own credentials for the
/// backend. Never the gateway's key.
const RELAYED: [&str; 6] =
    ["sec-websocket-key", "sec-websocket-version", "sec-websocket-protocol", "sec-websocket-extensions", "authorization", "cookie"];

/// How long the backend may take to accept a WebSocket handshake.
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);

/// Open the connection to the backend and, if it accepts, join the two. If it cannot be reached
/// or refuses, the caller's handshake is refused, as the Python gateway refuses it.
async fn relay_websocket(w: &Worker, r: &Relay<'_>, mut req: Request<Incoming>) -> Response<Body> {
    let refused = || {
        let mut resp = Response::new(full(Bytes::new()));
        *resp.status_mut() = StatusCode::FORBIDDEN;
        resp
    };
    let to = &r.backend.upstream;
    let Ok(uri) = Uri::builder().scheme(to.scheme.clone()).authority(to.authority.clone()).path_and_query(r.target.as_str()).build() else {
        return refused();
    };
    let client_end = hyper::upgrade::on(&mut req);
    let mut out = Request::new(full(Bytes::new()));
    *out.uri_mut() = uri;
    for name in RELAYED {
        for value in req.headers().get_all(name) {
            out.headers_mut().append(HeaderName::from_static(name), value.clone());
        }
    }
    out.headers_mut().insert(CONNECTION, HeaderValue::from_static("upgrade"));
    out.headers_mut().insert(UPGRADE, HeaderValue::from_static("websocket"));
    match tokio::time::timeout(HANDSHAKE_TIMEOUT, w.client.request(out)).await {
        Ok(Ok(resp)) if resp.status() == StatusCode::SWITCHING_PROTOCOLS => {
            let mut resp = tunnel(client_end, resp);
            resp.headers_mut().insert("x-served-by", HeaderValue::from_static("dataplane"));
            resp
        }
        _ => refused(),
    }
}

/// How long a gRPC backend may take to accept a connection. Nothing bounds a call after that
/// but the caller's own deadline: a stream may rightly stay open for hours.
const GRPC_CONNECT_TIMEOUT: Duration = Duration::from_secs(10);

/// Whether a request is a gRPC call: HTTP/2, POST, and a gRPC content type (`application/grpc`,
/// or that with `+proto`, `+json`, ...). gRPC-Web is another protocol and is not one.
fn is_grpc<B>(req: &Request<B>) -> bool {
    req.version() == Version::HTTP_2
        && req.method() == Method::POST
        && req.headers().get(CONTENT_TYPE).is_some_and(|v| {
            v.as_bytes().strip_prefix(b"application/grpc").is_some_and(|rest| matches!(rest.first(), None | Some(b'+') | Some(b';')))
        })
}

/// A gRPC call answered here instead of by the backend: the status a gRPC client is told, its
/// message, and the HTTP status the same refusal would have had, which is what is counted and logged.
#[derive(Debug, PartialEq, Clone, Copy)]
struct GrpcRefusal {
    code: u8,
    message: &'static str,
    http: u16,
    /// The gateway's name for the error, in the request log.
    error: &'static str,
}

impl GrpcRefusal {
    const NOT_READY: Self = Self { code: 14, message: "the gateway is not ready", http: 503, error: "SERVICE_UNAVAILABLE" };
    const NOT_CONFIGURED: Self = Self { code: 12, message: "gRPC is not configured on this gateway", http: 501, error: "NOT_IMPLEMENTED" };
    const NO_KEY: Self = Self { code: 16, message: "API key required", http: 401, error: "AUTHENTICATION_ERROR" };
    const BAD_KEY: Self = Self { code: 16, message: "invalid API key", http: 401, error: "AUTHENTICATION_ERROR" };
    const NOT_APPLICABLE: Self =
        Self { code: 7, message: "the gateway's rules for this key cannot be applied to gRPC", http: 403, error: "AUTHORIZATION_ERROR" };
    const DENIED: Self = Self { code: 7, message: "access denied", http: 403, error: "AUTHORIZATION_ERROR" };
    const LIMITED: Self = Self { code: 8, message: "rate limit exceeded", http: 429, error: "RATE_LIMIT_EXCEEDED" };
    const CIRCUIT_OPEN: Self = Self { code: 14, message: "the backend is unavailable (circuit open)", http: 503, error: "SERVICE_UNAVAILABLE" };
    const NO_ANSWER: Self = Self { code: 14, message: "the backend is unavailable", http: 502, error: "BACKEND_ERROR" };

    /// The refusal for an answer the HTTP path made, by its error code.
    fn for_error(error: &str) -> Self {
        match error {
            "AUTHORIZATION_ERROR" => Self::DENIED,
            "RATE_LIMIT_EXCEEDED" => Self::LIMITED,
            "SERVICE_UNAVAILABLE" => Self::CIRCUIT_OPEN,
            _ => Self::NO_ANSWER,
        }
    }

    /// Turn an answer into the form a gRPC client reads: HTTP 200, no body, the status in the
    /// headers. What the answer was stamped with (request id, trace, `Retry-After`) stays.
    fn onto(&self, mut resp: Response<Body>) -> Response<Body> {
        *resp.status_mut() = StatusCode::OK;
        *resp.body_mut() = full(Bytes::new());
        let h = resp.headers_mut();
        h.remove(CONTENT_LENGTH);
        h.insert(CONTENT_TYPE, HeaderValue::from_static("application/grpc"));
        h.insert("grpc-status", HeaderValue::from(u16::from(self.code)));
        h.insert("grpc-message", HeaderValue::from_static(self.message));
        resp
    }
}

/// A gRPC call that goes on to the backend, and which of its headers carried the gateway's key.
struct GrpcCall<'a> {
    native: Native<'a>,
    key_in_authorization: bool,
}

/// Decide a gRPC call. There is no Python gateway to hand one to (it does not speak HTTP/2), so
/// whatever this side cannot be sure of is refused: a stale snapshot, a key it does not know (one
/// made a moment ago included), a key whose rules it cannot apply. The key's allowed paths, the IP
/// rules, the policies, the limits and the quotas are applied afterwards, as for any native request.
fn admit_grpc<'a, B>(snap: &'a Snapshot, peer: SocketAddr, req: &Request<B>, now: u64, trusted: bool) -> Result<GrpcCall<'a>, GrpcRefusal> {
    if !trusted {
        return Err(GrpcRefusal::NOT_READY);
    }
    let backend = snap.grpc.as_deref().ok_or(GrpcRefusal::NOT_CONFIGURED)?;
    let path = req.uri().path();
    // A method is `/package.Service/Method`; nothing a client library sends is escaped.
    if !path.is_ascii() || path.contains('%') {
        return Err(GrpcRefusal::DENIED);
    }
    let ip = govern::client_ip(peer, req.headers(), &snap.trusted_proxies).ok_or(GrpcRefusal::DENIED)?;
    let header = |name: &HeaderName| req.headers().get(name).map(|v| v.as_bytes()).filter(|v| !v.is_empty());
    let (credential, key_in_authorization) = match (header(&snap.key_header), header(&AUTHORIZATION)) {
        (None, None) => return Err(GrpcRefusal::NO_KEY),
        (Some(key), _) => (Cow::Borrowed(key), false),
        (None, Some(value)) => (without_bearer(value), true),
    };
    if credential.is_empty() || !credential.is_ascii() {
        return Err(GrpcRefusal::BAD_KEY);
    }
    let entry = snap.keys.get(&hash_key(&credential)).ok_or(GrpcRefusal::BAD_KEY)?;
    if !entry.is_active || entry.expires_at.is_some_and(|e| e <= now as i64) {
        return Err(GrpcRefusal::BAD_KEY);
    }
    if !entry.native_ok {
        return Err(GrpcRefusal::NOT_APPLICABLE);
    }
    let native = Native { protocol: Protocol::Grpc, backend: Some(backend), entry, version: None, ip, method: "POST" };
    Ok(GrpcCall { native, key_in_authorization })
}

/// Answer a gRPC call that never got as far as a key: counted and logged like any refusal made
/// here, under the HTTP status it stands for.
fn grpc_refused<B>(w: &Worker, refusal: &GrpcRefusal, peer: SocketAddr, req: &Request<B>, started: Instant, at: Duration) -> Response<Body> {
    let t = trace_for(req.headers());
    let mut resp = Response::new(full(Bytes::new()));
    *resp.status_mut() = StatusCode::from_u16(refusal.http).unwrap_or(StatusCode::FORBIDDEN);
    let a = Answered { method: "POST", ip: peer.ip(), version: None, rejected: true, error: Some(refusal.error) };
    refusal.onto(finish(w, resp, &a, req, &t, started, at))
}

/// Send a gRPC call to its backend over HTTP/2, body and trailers streamed both ways. The backend
/// is not sent the gateway's key; an `Authorization` that did not carry it is the caller's own
/// and goes through.
async fn forward_grpc(
    w: &Worker,
    to: &Upstream,
    peer: SocketAddr,
    mut req: Request<Body>,
    trace: &Trace,
    key_header: &HeaderName,
    key_in_authorization: bool,
) -> Result<Response<Body>, Unanswered> {
    let asked = req.uri().path_and_query().map(|p| p.as_str()).unwrap_or("/");
    let uri = Uri::builder().scheme(to.scheme.clone()).authority(to.authority.clone()).path_and_query(format!("{}{asked}", to.prefix));
    *req.uri_mut() = uri.build().map_err(|_| Unanswered::Broken)?;
    let headers = req.headers_mut();
    for h in HOP_BY_HOP {
        headers.remove(h);
    }
    headers.remove(HOST);
    headers.remove(key_header);
    if key_in_authorization {
        headers.remove(AUTHORIZATION);
    }
    // Every gRPC call says it accepts trailers: the status of the call arrives in them.
    headers.insert("te", HeaderValue::from_static("trailers"));
    let xff = match headers.get("x-forwarded-for").and_then(|v| v.to_str().ok()) {
        Some(prior) => format!("{prior}, {}", peer.ip()),
        None => peer.ip().to_string(),
    };
    if let Ok(v) = HeaderValue::from_str(&xff) {
        headers.insert("x-forwarded-for", v);
    }
    if let Ok(v) = HeaderValue::from_str(&trace.traceparent) {
        headers.insert("traceparent", v);
    }
    match w.grpc.request(req).await {
        Ok(resp) => Ok(resp.map(Either::Left)),
        Err(e) if e.is_connect() => Err(Unanswered::Refused),
        Err(_) => Err(Unanswered::Broken),
    }
}

/// One of the gateway's own endpoints, answered here.
#[derive(Debug, PartialEq)]
enum Own<'a> {
    /// The health path: this side is serving and the Python gateway is answering. Holds the
    /// version to report, as JSON.
    Health(&'a str),
    /// `/ready`: the Python gateway's own answer, as of its latest word.
    Ready(Arc<Readiness>),
    /// `/live`: this process is running. Needs nothing from the Python gateway, so a restart of
    /// that one never makes this one look dead.
    Live,
}

/// Which of the gateway's own endpoints a request is for, if this side may answer it. `up` says
/// whether the Python gateway is known to be answering, and `ready` gives its answer to `/ready`.
/// The health path comes first, as its route does in the Python gateway.
fn own<'a, B>(snap: &'a Snapshot, req: &Request<B>, up: impl FnOnce() -> bool, ready: impl FnOnce() -> Option<Arc<Readiness>>) -> Option<Own<'a>> {
    let (uri, headers) = (req.uri(), req.headers());
    let path = uri.path();
    // The path first: almost every request is for none of these, and is done here.
    let health = snap.ops.as_ref().filter(|o| o.health_path.as_deref() == Some(path));
    if health.is_none() && path != "/ready" && path != "/live" {
        return None;
    }
    if req.method() != Method::GET || uri.authority().is_some() || headers.contains_key(UPGRADE) || headers.contains_key(ORIGIN) {
        return None;
    }
    match (health, path) {
        (Some(ops), _) => up().then_some(Own::Health(&ops.version_json)),
        (None, "/ready") => ready().map(Own::Ready),
        (None, _) => Some(Own::Live),
    }
}

/// Answer one of the gateway's own endpoints, counted and logged as the Python gateway does its
/// own. `None` when the request is one only the Python gateway can describe.
fn answer_own<B>(w: &Worker, snap: &Snapshot, own: &Own, peer: SocketAddr, req: &Request<B>, started: Instant, at: Duration) -> Option<Response<Body>> {
    let version = match resolve_version(snap, req.headers()) {
        Some((_, false)) => return None,
        found => found.map(|(v, _)| v),
    };
    let ip = govern::client_ip(peer, req.headers(), &snap.trusted_proxies)?;
    let (status, body) = match own {
        Own::Health(version_json) => (
            StatusCode::OK,
            format!("{{\"status\":\"healthy\",\"version\":{version_json},\"timestamp\":\"{}\",\"services\":{{}}}}", timestamp(at, false)),
        ),
        Own::Ready(r) => (StatusCode::from_u16(r.status).ok()?, r.body_json.clone()),
        Own::Live => (StatusCode::OK, format!("{{\"status\":\"alive\",\"pid\":{}}}", std::process::id())),
    };
    let mut resp = Response::new(full(body));
    *resp.status_mut() = status;
    resp.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    let a = Answered { method: "GET", ip, version, rejected: false, error: None };
    Some(finish(w, resp, &a, req, &trace_for(req.headers()), started, at))
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

/// An answer made here for a request that never reached a backend or a key.
struct Answered<'a> {
    method: &'static str,
    ip: IpAddr,
    version: Option<&'a str>,
    /// A refusal: counted under a bounded path label, as the Python gateway counts its own.
    rejected: bool,
    /// The error code of the body, for the log row.
    error: Option<&'static str>,
}

/// Stamp an answer made here, and report and log it as the Python gateway records its own:
/// counted, and logged without a key.
fn finish<B>(w: &Worker, mut resp: Response<Body>, a: &Answered, req: &Request<B>, t: &Trace, started: Instant, at: Duration) -> Response<Body> {
    let st = &*w.shared;
    let path = req.uri().path();
    let status = resp.status().as_u16();
    let seconds = started.elapsed().as_secs_f64();
    let bytes_out = resp.body().size_hint().exact().unwrap_or(0);
    stamp(&mut resp, t, seconds, a.version);
    let user_agent = req.headers().get(USER_AGENT).and_then(|v| v.to_str().ok());
    if let Some(reporter) = &st.reporter {
        let served = Served {
            method: a.method,
            path,
            status,
            seconds,
            key_id: "",
            tenant: None,
            rejected: a.rejected,
            error_code: None,
            error_type: None,
        };
        reporter.record(w.index, &served, at.as_secs(), || Row {
            request_id: t.request_id.clone(),
            method: a.method,
            path: path.to_owned(),
            client_ip: a.ip.to_string(),
            api_key_id: None,
            tenant_id: None,
            status,
            duration_ms: seconds * 1000.0,
            user_agent: user_agent.map(str::to_owned),
            error: a.error.map(str::to_owned),
        });
    }
    if let Some(log) = &st.access_log {
        log.write(accesslog::line(&accesslog::Entry {
            at,
            request_id: &t.request_id,
            traceparent: &t.traceparent,
            key_id: None,
            tenant: None,
            ip: a.ip,
            method: a.method,
            path,
            query: req.uri().query().unwrap_or(""),
            status,
            bytes: bytes_out,
            seconds,
            user_agent,
            referrer: req.headers().get(REFERER).and_then(|v| v.to_str().ok()),
        }));
    }
    resp
}

/// Answer a request that carries no credential that works.
fn unauthenticated<B>(w: &Worker, u: &Unauthenticated, req: &Request<B>, started: Instant, at: Duration) -> Response<Body> {
    let t = trace_for(req.headers());
    let resp = envelope(StatusCode::UNAUTHORIZED, "AUTHENTICATION_ERROR", &u.wording.message_json, &u.wording.details_json, &t, true);
    let a = Answered { method: u.method, ip: u.ip, version: u.version, rejected: true, error: Some("AUTHENTICATION_ERROR") };
    finish(w, resp, &a, req, &t, started, at)
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

/// An HTTP/2 request names its host in the address; put it where an HTTP/1 request has it, so
/// that everything after this reads the two alike and the Python gateway is told the same host.
fn as_http1_names_it<B>(req: &mut Request<B>) {
    let Some(authority) = req.uri().authority().cloned() else { return };
    if !req.headers().contains_key(HOST) {
        if let Ok(host) = HeaderValue::from_str(authority.as_str()) {
            req.headers_mut().insert(HOST, host);
        }
    }
    let mut parts = std::mem::take(req.uri_mut()).into_parts();
    (parts.scheme, parts.authority) = (None, None);
    *req.uri_mut() = Uri::from_parts(parts).unwrap_or_else(|_| Uri::from_static("/"));
}

pub async fn handle(w: Arc<Worker>, peer: SocketAddr, mut req: Request<Incoming>) -> Result<Response<Body>, Infallible> {
    let started = Instant::now();
    let st = &*w.shared;
    if req.version() == Version::HTTP_2 {
        as_http1_names_it(&mut req);
    }
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
    // The gateway's own health endpoints, where this side can speak for it.
    let link = st.sync.as_deref();
    if let Some(own) = own(&snap, &req, || link.map(Link::up).unwrap_or(true), || link.and_then(Link::readiness)) {
        if let Some(resp) = answer_own(&w, &snap, &own, peer, &req, started, at) {
            return Ok(resp);
        }
    }
    // Fail closed: if the snapshot is not known to be current, the Python gateway decides.
    let trusted = st.sync.as_ref().map(|s| s.fresh()).unwrap_or(true);
    if trusted && req.headers().contains_key(UPGRADE) {
        if let Some(r) = relay(&snap, &req, now) {
            if r.entry.last_used.load(Ordering::Relaxed) != now {
                r.entry.last_used.store(now, Ordering::Relaxed);
            }
            return Ok(relay_websocket(&w, &r, req).await);
        }
    }
    let mut key_in_authorization = false;
    let decision = if is_grpc(&req) {
        match admit_grpc(&snap, peer, &req, now, trusted) {
            Ok(call) => {
                key_in_authorization = call.key_in_authorization;
                Decision::Native(call.native)
            }
            Err(refusal) => return Ok(grpc_refused(&w, &refusal, peer, &req, started, at)),
        }
    } else if trusted {
        decide(&snap, peer, &req, now, |digest| st.denied.lock().unwrap().holds(digest, generation, started))
    } else {
        Decision::Ask(None)
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
    let grpc = n.protocol == Protocol::Grpc;
    if refused.is_none() && !grpc && snap.boundary.reads_body(n.method, path) {
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
                    let answer = match grpc {
                        true => forward_grpc(&w, &backend.upstream, peer, req, &t, &snap.key_header, key_in_authorization).await,
                        false => forward(&w, &backend.upstream, peer, req, Some(native), Some(snap.upstream_timeout)).await,
                    };
                    match answer {
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
    // A gRPC client reads a refusal in gRPC's own form; it was counted and logged above as the
    // HTTP answer it stands for.
    if let Some(error) = error.filter(|_| grpc) {
        resp = GrpcRefusal::for_error(error).onto(resp);
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
    // A request to change protocol (a WebSocket handshake) keeps the headers that ask for it, and
    // this side's end of the connection is held ready for the tunnel.
    let asked_for = req.headers().get(UPGRADE).cloned();
    let client_end = asked_for.as_ref().map(|_| hyper::upgrade::on(&mut req));
    let headers = req.headers_mut();
    for h in HOP_BY_HOP {
        headers.remove(h);
    }
    if let Some(protocol) = asked_for {
        headers.insert(CONNECTION, HeaderValue::from_static("upgrade"));
        headers.insert(UPGRADE, protocol);
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
            if let Some(client_end) = client_end.filter(|_| resp.status() == StatusCode::SWITCHING_PROTOCOLS) {
                return Ok(tunnel(client_end, resp));
            }
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

/// The upstream agreed to change protocol: pass its answer on and, once the client has it, carry
/// bytes both ways until either side closes. What is spoken inside (WebSocket frames, pings,
/// compression) is between the two ends.
fn tunnel(client_end: hyper::upgrade::OnUpgrade, mut resp: Response<Incoming>) -> Response<Body> {
    let upstream_end = hyper::upgrade::on(&mut resp);
    tokio::spawn(async move {
        // The upstream's end is ready already; the client's is once it has been sent the answer.
        if let (Ok(upstream), Ok(client)) = (upstream_end.await, client_end.await) {
            let _ = tokio::io::copy_bidirectional(&mut TokioIo::new(client), &mut TokioIo::new(upstream)).await;
        }
    });
    let (mut parts, _) = resp.into_parts();
    let protocol = parts.headers.remove(UPGRADE);
    for h in HOP_BY_HOP {
        parts.headers.remove(h);
    }
    parts.headers.insert(CONNECTION, HeaderValue::from_static("upgrade"));
    if let Some(protocol) = protocol {
        parts.headers.insert(UPGRADE, protocol);
    }
    Response::from_parts(parts, full(Bytes::new()))
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
        // Python leaves a zero fraction out.
        assert_eq!(timestamp(Duration::new(951_782_400, 0), false), "2000-02-29T00:00:00");
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

    fn probe(snap: &Snapshot, method: Method, uri: &str, headers: &[(&str, &str)], up: bool, ready: Option<u16>) -> Option<String> {
        let mut req = Request::builder().method(method).uri(uri);
        for (name, value) in headers {
            req = req.header(*name, *value);
        }
        let req = req.body(()).unwrap();
        let ready = ready.map(|status| Arc::new(Readiness { status, body_json: "{}".into() }));
        own(snap, &req, || up, || ready).map(|o| match o {
            Own::Health(v) => format!("health {v}"),
            Own::Ready(r) => format!("ready {}", r.status),
            Own::Live => "live".into(),
        })
    }

    #[test]
    fn the_gateways_own_endpoints_are_answered_here_when_this_side_can_speak_for_it() {
        let snap = Snapshot::from_json(r#"{"keys":[],"ops":{"health_path":"/health","version_json":"\"1.2\""}}"#).unwrap();
        let get = |uri: &str, up: bool, ready: Option<u16>| probe(&snap, Method::GET, uri, &[], up, ready);
        assert_eq!(get("/health", true, None).as_deref(), Some("health \"1.2\""));
        assert_eq!(get("/health?verbose=1", true, None).as_deref(), Some("health \"1.2\""));
        assert_eq!(get("/ready", true, Some(200)).as_deref(), Some("ready 200"));
        assert_eq!(get("/ready", true, Some(503)).as_deref(), Some("ready 503"));
        // Health and readiness speak for the Python gateway: not without its word.
        assert_eq!(get("/health", false, None), None);
        assert_eq!(get("/ready", true, None), None);
        // Being alive is this process's own business.
        assert_eq!(get("/live", false, None).as_deref(), Some("live"));
        // Only these paths, exactly.
        for uri in ["/health/", "/health/detailed", "/healthz", "/ready/", "/live/x", "/metrics", "/api/health", "/"] {
            assert_eq!(get(uri, true, Some(200)), None, "{uri}");
        }
        // Anything but a plain GET is the Python gateway's to answer (405, a preflight, a handshake).
        for method in [Method::HEAD, Method::POST, Method::OPTIONS] {
            assert_eq!(probe(&snap, method, "/live", &[], true, None), None);
        }
        assert_eq!(probe(&snap, Method::GET, "/live", &[("Origin", "https://app.example")], true, None), None);
        assert_eq!(probe(&snap, Method::GET, "/health", &[("Upgrade", "websocket")], true, None), None);
        assert_eq!(probe(&snap, Method::GET, "http://other.example/live", &[], true, None), None);
    }

    #[test]
    fn the_health_path_is_wherever_the_gateway_has_it_and_comes_first() {
        let moved = Snapshot::from_json(r#"{"keys":[],"ops":{"health_path":"/api/status","version_json":"\"1\""}}"#).unwrap();
        assert_eq!(probe(&moved, Method::GET, "/api/status", &[], true, None).as_deref(), Some("health \"1\""));
        assert_eq!(probe(&moved, Method::GET, "/health", &[], true, None), None);
        // Configured onto another endpoint's path, the health route wins there, as it does in Python.
        let shadowing = Snapshot::from_json(r#"{"keys":[],"ops":{"health_path":"/live","version_json":"\"1\""}}"#).unwrap();
        assert_eq!(probe(&shadowing, Method::GET, "/live", &[], true, None).as_deref(), Some("health \"1\""));
        assert_eq!(probe(&shadowing, Method::GET, "/live", &[], false, None), None);
        // No path, or no word from the control plane at all: health is Python's, alive is still ours.
        let hidden = Snapshot::from_json(r#"{"keys":[],"ops":{"health_path":null,"version_json":"\"1\""}}"#).unwrap();
        for snap in [&hidden, &Snapshot::empty()] {
            assert_eq!(probe(snap, Method::GET, "/health", &[], true, None), None);
            assert_eq!(probe(snap, Method::GET, "/live", &[], true, None).as_deref(), Some("live"));
        }
    }

    fn handshake(uri: &str, headers: &[(&str, &str)]) -> Request<()> {
        let mut b = Request::builder().uri(uri);
        for (name, value) in [("upgrade", "websocket"), ("connection", "Upgrade"), ("sec-websocket-version", "13"), ("sec-websocket-key", "a2V5")] {
            if !headers.iter().any(|(n, _)| n.eq_ignore_ascii_case(name)) {
                b = b.header(name, value);
            }
        }
        for (name, value) in headers.iter().filter(|(_, v)| !v.is_empty()) {
            b = b.header(*name, *value);
        }
        b.body(()).unwrap()
    }

    const WS: &str = r#","websocket":"ws://127.0.0.1:7000/base/""#;

    #[test]
    fn a_websocket_handshake_with_a_working_key_is_relayed_here() {
        let snap = governed(WS, "");
        let target = |uri: &str, headers: &[(&str, &str)]| relay(&snap, &handshake(uri, headers), 100).map(|r| r.target);
        let key = [("x-api-key", KEY)];
        assert_eq!(target("/ws/chat", &key).as_deref(), Some("/base/chat"));
        assert_eq!(target("/ws/rooms/7?room=blue&x=1", &key).as_deref(), Some("/base/rooms/7?room=blue&x=1"));
        assert_eq!(target("/ws/", &key).as_deref(), Some("/base/"));
        assert_eq!(target("/ws/chat", &[("x-api-key", KEY), ("upgrade", "WebSocket"), ("authorization", "Bearer t")]).as_deref(), Some("/base/chat"));
        // Whatever is not certain to be let in is the Python gateway's to judge.
        for (why, uri, headers) in [
            ("no key", "/ws/chat", vec![]),
            ("unknown key", "/ws/chat", vec![("x-api-key", "sk-nope")]),
            ("key in the query", "/ws/chat?api_key=sk-test", vec![]),
            ("key in both", "/ws/chat?api_key=sk-test", key.to_vec()),
            ("key as a Bearer token", "/ws/chat", vec![("authorization", "Bearer sk-test")]),
            ("not under /ws/", "/ws", key.to_vec()),
            ("not under /ws/", "/wsx/chat", key.to_vec()),
            ("an agent's socket", "/agents/a/ws/chat", key.to_vec()),
            ("an escaped path", "/ws/a%2Fb", key.to_vec()),
            ("not a WebSocket", "/ws/chat", vec![("x-api-key", KEY), ("upgrade", "h2c")]),
            ("another version", "/ws/chat", vec![("x-api-key", KEY), ("sec-websocket-version", "8")]),
            ("no handshake key", "/ws/chat", vec![("x-api-key", KEY), ("sec-websocket-key", "")]),
        ] {
            assert!(target(uri, &headers).is_none(), "{why}");
        }
        let twice = Request::builder().uri("/ws/chat").header("upgrade", "websocket").header("sec-websocket-version", "13")
            .header("sec-websocket-key", "a2V5").header("x-api-key", KEY).header("x-api-key", "sk-other").body(()).unwrap();
        assert!(relay(&snap, &twice, 100).is_none());
        let post = Request::builder().method("POST").uri("/ws/chat").header("upgrade", "websocket").header("sec-websocket-version", "13")
            .header("sec-websocket-key", "a2V5").header("x-api-key", KEY).body(()).unwrap();
        assert!(relay(&snap, &post, 100).is_none());
    }

    #[test]
    fn a_websocket_is_relayed_here_only_with_a_backend_and_a_key_that_may() {
        let req = handshake("/ws/chat", &[("x-api-key", KEY)]);
        // No WebSocket backend this side can reach.
        assert!(relay(&governed("", ""), &req, 100).is_none());
        assert!(relay(&governed(r#","websocket":"wss://secure.example/base""#, ""), &req, 100).is_none());
        assert!(relay(&governed(r#","websocket":"http://127.0.0.1:7000""#, ""), &req, 100).is_none());
        // A key that is off, not reproducible here, expired, or not allowed this path.
        for key_extra in [r#","is_active":false"#, r#","native_ok":false"#, r#","expires_at":100"#, r#","expires_at":99"#] {
            assert!(relay(&governed(WS, key_extra), &req, 100).is_none(), "{key_extra}");
        }
        assert!(relay(&governed(WS, r#","expires_at":101"#), &req, 100).is_some());
        let scoped = governed(WS, r#","allowed_paths":{"rules":[["under","/ws/chat"]],"patterns_json":"[\"/ws/chat\"]"}"#);
        assert!(relay(&scoped, &req, 100).is_some());
        assert!(relay(&scoped, &handshake("/ws/other", &[("x-api-key", KEY)]), 100).is_none());
    }

    const GRPC: &str = r#","grpc":"http://127.0.0.1:50051""#;

    fn rpc(path: &str, headers: &[(&str, &str)]) -> Request<()> {
        let mut b = Request::builder().method("POST").uri(path).version(Version::HTTP_2).header("content-type", "application/grpc");
        for (k, v) in headers {
            b = b.header(*k, *v);
        }
        b.body(()).unwrap()
    }

    #[test]
    fn a_grpc_call_is_told_by_its_content_type_over_http2() {
        assert!(is_grpc(&rpc("/pkg.Service/Method", &[])));
        for kind in ["application/grpc+proto", "application/grpc+json", "application/grpc;charset=utf-8"] {
            let req = Request::builder().method("POST").uri("/a/b").version(Version::HTTP_2).header("content-type", kind).body(()).unwrap();
            assert!(is_grpc(&req), "{kind}");
        }
        for kind in ["application/grpc-web", "application/grpc-web+proto", "application/grpcx", "application/json"] {
            let req = Request::builder().method("POST").uri("/a/b").version(Version::HTTP_2).header("content-type", kind).body(()).unwrap();
            assert!(!is_grpc(&req), "{kind}");
        }
        // Not over HTTP/1, and not with another method.
        assert!(!is_grpc(&Request::builder().method("POST").uri("/a/b").header("content-type", "application/grpc").body(()).unwrap()));
        assert!(!is_grpc(&Request::builder().uri("/a/b").version(Version::HTTP_2).header("content-type", "application/grpc").body(()).unwrap()));
    }

    fn admitted(snap: &Snapshot, req: &Request<()>, now: u64, trusted: bool) -> Result<bool, GrpcRefusal> {
        admit_grpc(snap, peer(), req, now, trusted).map(|call| call.key_in_authorization)
    }

    #[test]
    fn a_grpc_call_with_a_working_key_goes_to_the_grpc_backend() {
        let snap = governed(GRPC, "");
        let call = admit_grpc(&snap, peer(), &rpc("/pkg.Service/Method", &[("x-api-key", KEY)]), 100, true).ok().unwrap();
        assert_eq!(call.native.protocol, Protocol::Grpc);
        assert_eq!(call.native.backend.unwrap().upstream.text, "http://127.0.0.1:50051");
        assert!(!call.key_in_authorization);
        let bearer = format!("Bearer {KEY}");
        assert_eq!(admitted(&snap, &rpc("/pkg.Service/Method", &[("authorization", &bearer)]), 100, true), Ok(true));
        // With both, the key is the one in its own header and the other is the caller's own.
        assert_eq!(admitted(&snap, &rpc("/a/b", &[("x-api-key", KEY), ("authorization", "Bearer theirs")]), 100, true), Ok(false));
    }

    #[test]
    fn a_grpc_call_this_side_cannot_be_sure_of_is_refused() {
        let with_key = rpc("/pkg.Service/Method", &[("x-api-key", KEY)]);
        let snap = governed(GRPC, "");
        assert_eq!(admitted(&snap, &with_key, 100, false), Err(GrpcRefusal::NOT_READY));
        assert_eq!(admitted(&governed("", ""), &with_key, 100, true), Err(GrpcRefusal::NOT_CONFIGURED));
        assert_eq!(admitted(&governed(r#","grpc":"https://secure.example:443""#, ""), &with_key, 100, true), Err(GrpcRefusal::NOT_CONFIGURED));
        assert_eq!(admitted(&snap, &rpc("/pkg.Service/Method", &[]), 100, true), Err(GrpcRefusal::NO_KEY));
        assert_eq!(admitted(&snap, &rpc("/pkg.Service/Method", &[("x-api-key", "sk-nope")]), 100, true), Err(GrpcRefusal::BAD_KEY));
        assert_eq!(admitted(&snap, &rpc("/pkg.Service/Method", &[("authorization", "Bearer sk-nope")]), 100, true), Err(GrpcRefusal::BAD_KEY));
        assert_eq!(admitted(&snap, &rpc("/pkg.Service/M%65thod", &[("x-api-key", KEY)]), 100, true), Err(GrpcRefusal::DENIED));
        // A key that is off or expired (there is no second opinion to ask, so none on the margin).
        for key_extra in [r#","is_active":false"#, r#","expires_at":100"#, r#","expires_at":99"#] {
            assert_eq!(admitted(&governed(GRPC, key_extra), &with_key, 100, true), Err(GrpcRefusal::BAD_KEY), "{key_extra}");
        }
        assert_eq!(admitted(&governed(GRPC, r#","expires_at":101"#), &with_key, 100, true), Ok(false));
        assert_eq!(admitted(&governed(GRPC, r#","native_ok":false"#), &with_key, 100, true), Err(GrpcRefusal::NOT_APPLICABLE));
    }

    #[test]
    fn a_grpc_key_only_reaches_its_allowed_methods() {
        let snap = governed(GRPC, r#","allowed_paths":{"rules":[["under","/pkg.Service"]],"patterns_json":"[\"/pkg.Service\"]"}"#);
        let at = Duration::from_secs(100);
        let allowed = rpc("/pkg.Service/Method", &[("x-api-key", KEY)]);
        let call = admit_grpc(&snap, peer(), &allowed, 100, true).ok().unwrap();
        assert!(refusal(&snap, &call.native, allowed.uri().path(), at).is_none());
        let other = rpc("/pkg.Other/Method", &[("x-api-key", KEY)]);
        let call = admit_grpc(&snap, peer(), &other, 100, true).ok().unwrap();
        assert!(matches!(refusal(&snap, &call.native, other.uri().path(), at), Some(Refusal::Scope(_))));
    }

    #[test]
    fn a_grpc_refusal_is_one_a_grpc_client_reads() {
        let t = trace_for(&HeaderMap::new());
        let mut made = rate_limited("minute", 3, 60, None, &t);
        stamp(&mut made, &t, 0.001, None);
        let resp = GrpcRefusal::for_error("RATE_LIMIT_EXCEEDED").onto(made);
        assert_eq!(resp.status(), StatusCode::OK);
        let h = resp.headers();
        assert_eq!((h["content-type"].as_bytes(), h["grpc-status"].as_bytes()), (&b"application/grpc"[..], &b"8"[..]));
        assert_eq!(h["grpc-message"], "rate limit exceeded");
        assert!(h.contains_key("retry-after") && h.contains_key("x-request-id") && !h.contains_key("content-length"));
        assert_eq!(resp.body().size_hint().exact(), Some(0));
        assert_eq!(GrpcRefusal::for_error("AUTHORIZATION_ERROR").code, 7);
        assert_eq!(GrpcRefusal::for_error("SERVICE_UNAVAILABLE").code, 14);
        assert_eq!(GrpcRefusal::for_error("BACKEND_ERROR").code, 14);
    }

    #[test]
    fn a_request_from_another_origin_is_left_to_python() {
        let snap = Snapshot::from_json(r#"{"keys":[]}"#).unwrap();
        let req = Request::builder().uri("/api/users").header("origin", "https://app.example").body(()).unwrap();
        assert!(matches!(decide(&snap, "127.0.0.1:9".parse().unwrap(), &req, 0, |_| false), Decision::Ask(None)));
    }

    #[test]
    fn an_http2_request_reads_like_an_http1_one() {
        let mut req = Request::builder().uri("http://gw.example:8000/api/users?a=1").version(Version::HTTP_2).body(()).unwrap();
        as_http1_names_it(&mut req);
        assert_eq!(req.uri().to_string(), "/api/users?a=1");
        assert_eq!(req.headers()[HOST], "gw.example:8000");
        // A host the client also sent as a header is kept as sent.
        let mut req = Request::builder().uri("http://gw.example/").header("host", "other").body(()).unwrap();
        as_http1_names_it(&mut req);
        assert_eq!((req.uri().to_string().as_str(), &req.headers()[HOST]), ("/", &HeaderValue::from_static("other")));
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
