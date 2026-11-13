//! The working set the control plane gives the data plane, held in memory.
//!
//! Keys are stored only as SHA-256 digests (what the database stores), so the data plane
//! never holds a usable credential. The control plane compiles everything that decides a
//! request (key scopes, tenants and their quotas, IP rules, policies) into the forms used here;
//! anything it cannot express that way it marks as not native, and the Python gateway keeps it.
use crate::boundary::{Boundary, BoundaryRecord};
use crate::breaker;
use crate::govern::{IpRule, IpRules, Net, Policy, Scope, ScopeRule};
use crate::limits::Window;
use crate::proxy::{Backend, ROUTED};
use crate::tenant::{Tenant, Usage};
use hyper::header::{HeaderName, HeaderValue};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hasher};
use std::sync::atomic::AtomicU64;
use std::sync::Arc;
use std::time::Duration;

/// Keys are already SHA-256 digests, so the first eight bytes are a perfectly good hash.
#[derive(Default)]
pub struct DigestHasher(u64);

impl Hasher for DigestHasher {
    fn finish(&self) -> u64 {
        self.0
    }
    fn write(&mut self, bytes: &[u8]) {
        if bytes.len() >= 8 {
            self.0 = u64::from_le_bytes(bytes[..8].try_into().unwrap());
        }
    }
}

pub type Digest32 = [u8; 32];

pub struct KeyEntry {
    /// The key's id in the control plane's database; reported with each request.
    pub id: Arc<str>,
    pub is_active: bool,
    pub expires_at: Option<i64>,
    /// False when something that applies to the key cannot be decided here exactly as the Python
    /// gateway decides it: its requests are handed to the control plane.
    pub native_ok: bool,
    /// The key's tenant, when it has an active one.
    pub tenant: Option<Arc<Tenant>>,
    /// The key's `allowed_paths`, when it has any.
    pub scope: Option<Scope>,
    /// Epoch seconds of the key's latest native request, zero once reported. Shared across
    /// snapshot updates.
    pub last_used: Arc<AtomicU64>,
    pub per_minute: u32,
    pub per_hour: u32,
    /// Shared across snapshot updates so a sync never resets a key's counters.
    pub minute: Arc<Window>,
    pub hour: Arc<Window>,
}

/// API versions as the data plane needs them: which are served as plain pass-through.
pub struct ApiVersions {
    pub default: Option<String>,
    /// version -> true when it is active, has no deprecation date and no backend of its own.
    /// Anything else (deprecated, sunset, removed, own backend) is handled by the control plane.
    pub native_ok: HashMap<String, bool>,
}

pub struct Snapshot {
    /// The stock request-id plugin is on: forward an `X-Request-ID` the client did not send.
    pub inject_request_id: bool,
    /// The control plane's revision of this content, sent back as `since` to ask "anything new?".
    pub revision: Option<String>,
    pub api_versions: ApiVersions,
    pub rate_limit_enabled: bool,
    pub circuit_breaker: breaker::Config,
    /// How long the backend may take to start answering before the request fails with a 502.
    pub upstream_timeout: Duration,
    pub keys: HashMap<Digest32, KeyEntry, BuildHasherDefault<DigestHasher>>,
    pub tenants: HashMap<String, Arc<Tenant>>,
    /// Peers whose `X-Forwarded-For` is believed.
    pub trusted_proxies: Vec<Net>,
    pub ip_rules: IpRules,
    /// In the order the Python engine evaluates them.
    pub policies: Vec<Policy>,
    /// Path prefixes the control plane must handle (dynamic routes, transformation rules, ...).
    pub fallthrough_prefixes: Vec<String>,
    /// Where each protocol in `ROUTED` is sent, as the control plane has it configured.
    pub protocol_urls: [Option<String>; 4],
    /// The backends at those addresses, filled in when the snapshot is put to use. A protocol
    /// without one is the control plane's to serve.
    pub routes: [Option<Arc<Backend>>; 4],
    /// What the Python gateway checks in a request body before forwarding it.
    pub boundary: Boundary,
    /// The header a key is read from first; `Authorization` is always the second place.
    pub key_header: HeaderName,
    /// How a request without a usable credential is refused. `None` when only the Python gateway
    /// can tell (a plugin may authenticate the request some other way): it then answers them all.
    pub refusals: Option<Refusals>,
    /// What the gateway's health path answers with. `None` when the control plane did not say.
    pub ops: Option<Ops>,
    /// The WebSocket server `/ws/...` connections go to (a `ws://` address), when this side may
    /// relay them itself.
    pub websocket_url: Option<String>,
    /// The backend at that address, filled in when the snapshot is put to use.
    pub websocket: Option<Arc<Backend>>,
    /// The gRPC server calls are relayed to (an `http://` address spoken to in HTTP/2).
    pub grpc_url: Option<String>,
    /// The backend at that address, filled in when the snapshot is put to use.
    pub grpc: Option<Arc<Backend>>,
    /// The agent edge: slug to upstream address (`http://`), for the agents this side forwards to.
    pub agent_urls: HashMap<String, String>,
    /// The backends at those addresses, filled in when the snapshot is put to use.
    pub agents: HashMap<String, Arc<Backend>>,
    /// How a failed agent is answered. Without it, agents are the Python gateway's.
    pub agent_failure: Option<AgentFailure>,
    /// The dynamic routes in force, in the order the Python gateway tries them.
    pub dynamic_routes: Vec<DynamicRoute>,
    /// `None` when the control plane did not say: requests from other origins are then its own.
    pub cross_origin: Option<CrossOrigin>,
    /// The pages a browser is shown for the errors answered here, by `<status>:<hint><countdown>`.
    /// `None` when the control plane sent none: browsers are then its own.
    pub error_pages: Option<HashMap<String, String>>,
    /// The transformation rules in force. A request one applies to is the Python gateway's.
    /// `None`: there are rules this side was not told the reach of, and every request is.
    pub transform_rules: Option<Vec<TransformRule>>,
}

/// The gateway's health endpoint, as the Python gateway serves it.
#[derive(Deserialize)]
pub struct Ops {
    /// Where it lives. `None` when only the Python gateway can answer there.
    pub health_path: Option<String>,
    /// The version it reports, as a JSON string.
    pub version_json: String,
}

fn no_rules() -> Option<Vec<TransformRule>> {
    Some(Vec::new())
}

/// Which requests a transformation rule applies to. The Python gateway applies it; this side
/// only needs to know that a request is one of them.
#[derive(Deserialize, Debug)]
pub struct TransformRule {
    /// A path pattern: plain characters, `*` for any run of characters (slashes included) and
    /// `?` for any one. It must match the whole path.
    glob: String,
    /// Upper-case method names; none means every method.
    #[serde(default)]
    methods: Vec<String>,
}

impl TransformRule {
    fn applies(&self, method: &str, path: &str) -> bool {
        (self.methods.is_empty() || self.methods.iter().any(|m| m == method)) && glob_matches(self.glob.as_bytes(), path.as_bytes())
    }
}

/// Whether `text` is matched in full by `pattern`, in which `*` stands for any run of bytes and
/// `?` for any one: Python's `fnmatch`, without its character classes.
pub fn glob_matches(pattern: &[u8], text: &[u8]) -> bool {
    let (mut p, mut t) = (0, 0);
    // Where to come back to when what follows a `*` does not fit: the `*`, and how much it took.
    let mut star: Option<(usize, usize)> = None;
    while t < text.len() {
        if p < pattern.len() && (pattern[p] == b'?' || (pattern[p] != b'*' && pattern[p] == text[t])) {
            p += 1;
            t += 1;
        } else if p < pattern.len() && pattern[p] == b'*' {
            star = Some((p, t));
            p += 1;
        } else if let Some((at, took)) = star {
            star = Some((at, took + 1));
            p = at + 1;
            t = took + 1;
        } else {
            return false;
        }
    }
    pattern[p..].iter().all(|c| *c == b'*')
}

/// How the Python gateway answers a request from a web page in another origin.
#[derive(Deserialize, Debug, PartialEq)]
pub struct CrossOrigin {
    /// Every origin is welcome (`*`).
    pub any_origin: bool,
    /// The origins named back to their own requests.
    pub origins: std::collections::HashSet<String>,
    /// Whether answers say credentials are allowed.
    pub credentials: bool,
}

/// A route registered through the admin API: where a matching request goes instead.
#[derive(Deserialize)]
pub struct DynamicRoute {
    /// The path it matches: all of it, or with `wildcard` its beginning.
    pub path: String,
    pub wildcard: bool,
    /// Where requests go (`http://`). `None` for a route only the Python gateway can follow.
    #[serde(rename = "backend")]
    pub backend_url: Option<String>,
    /// That backend, filled in when the snapshot is put to use.
    #[serde(skip)]
    pub backend: Option<Arc<Backend>>,
    /// The protocol name the backend is told.
    pub protocol: String,
    timeout_seconds: Option<u64>,
    /// The path the backend is asked for, in place of the caller's.
    pub rewrite: Option<String>,
}

impl DynamicRoute {
    pub fn matches(&self, path: &str) -> bool {
        if self.wildcard {
            path.starts_with(self.path.as_str())
        } else {
            path == self.path
        }
    }

    /// How long the backend may take to start answering.
    pub fn timeout(&self) -> Option<Duration> {
        self.timeout_seconds.map(Duration::from_secs)
    }
}

/// The Python gateway's answer when an agent's connection fails before it answers.
#[derive(Deserialize, Debug, PartialEq)]
pub struct AgentFailure {
    /// The message of its 500, as a JSON string.
    pub message_json: String,
    /// The page a browser is shown, in two parts: the request id goes between them.
    pub page: (String, String),
}

/// The Python gateway's own answer to `/ready`, as of one snapshot answer.
#[derive(Deserialize, Debug, PartialEq)]
pub struct Readiness {
    pub status: u16,
    pub body_json: String,
}

/// What comes with every control-plane answer, changed or not.
#[derive(Default)]
pub struct Extras {
    pub usage: Option<UsageBase>,
    pub ready: Option<Readiness>,
}

/// The Python gateway's words for one refusal, ready to put into its error envelope.
#[derive(Deserialize)]
pub struct Wording {
    pub message_json: String,
    pub details_json: String,
}

#[derive(Deserialize)]
pub struct Refusals {
    /// No credential was sent.
    pub missing_key: Wording,
    /// The credential is not a key that works.
    pub invalid_key: Wording,
}

#[derive(Deserialize)]
struct SnapshotFile {
    /// Protocol name -> backend address. Without it only plain HTTP is served natively.
    #[serde(default)]
    backends: HashMap<String, Option<String>>,
    boundary: Option<BoundaryRecord>,
    key_header: Option<String>,
    refusals: Option<Refusals>,
    ops: Option<Ops>,
    websocket: Option<String>,
    grpc: Option<String>,
    #[serde(default)]
    agents: HashMap<String, String>,
    agent_failure: Option<AgentFailure>,
    #[serde(default)]
    dynamic_routes: Vec<DynamicRoute>,
    cross_origin: Option<CrossOrigin>,
    error_pages: Option<HashMap<String, String>>,
    /// Absent: a control plane that knows of no rules. `null`: rules it could not describe.
    #[serde(default = "no_rules")]
    transform_rules: Option<Vec<TransformRule>>,
    ready: Option<Readiness>,
    #[serde(default)]
    keys: Vec<KeyRecord>,
    #[serde(default)]
    fallthrough_prefixes: Vec<String>,
    #[serde(default = "yes")]
    rate_limit_enabled: bool,
    api_versions: Option<VersionsRecord>,
    revision: Option<String>,
    #[serde(default)]
    unchanged: bool,
    #[serde(default)]
    inject_request_id: bool,
    circuit_breaker: Option<BreakerRecord>,
    upstream_timeout_seconds: Option<f64>,
    #[serde(default)]
    tenants: HashMap<String, TenantRecord>,
    #[serde(default)]
    trusted_proxies: Vec<String>,
    #[serde(default)]
    ip_rules: Vec<IpRuleRecord>,
    #[serde(default)]
    policies: Vec<PolicyRecord>,
    tenant_usage: Option<UsageBase>,
}

#[derive(Deserialize)]
struct TenantRecord {
    minute: Option<u64>,
    hour: Option<u64>,
    details_json: String,
}

#[derive(Deserialize)]
struct IpRuleRecord {
    scope: String,
    scope_id: Option<String>,
    allow: bool,
    net: Option<String>,
    details_json: String,
}

#[derive(Deserialize)]
struct PolicyRecord {
    path: Option<String>,
    #[serde(default)]
    methods: Vec<String>,
    #[serde(default)]
    tenants: Vec<String>,
    cidrs: Option<Vec<String>>,
    time: Option<(u32, u32)>,
    deny: bool,
    message_json: String,
    details_json: String,
}

#[derive(Deserialize)]
struct ScopeRecord {
    rules: Vec<Vec<String>>,
    patterns_json: String,
}

/// What the database holds for each limited tenant in the current minute and hour.
#[derive(Deserialize)]
pub struct UsageBase {
    minute_start: u64,
    hour_start: u64,
    /// tenant id -> `[minute, hour]`; a tenant not listed has used nothing.
    #[serde(default)]
    used: HashMap<String, (u64, u64)>,
}

impl UsageBase {
    pub fn apply(&self, snap: &Snapshot) {
        for (id, t) in &snap.tenants {
            if t.minute.is_some() || t.hour.is_some() {
                let (minute, hour) = self.used.get(id).copied().unwrap_or((0, 0));
                t.usage.set_base(self.minute_start, self.hour_start, minute, hour);
            }
        }
    }
}

#[derive(Deserialize)]
struct BreakerRecord {
    enabled: bool,
    failure_threshold: u32,
    timeout_seconds: u64,
}

#[derive(Deserialize)]
struct VersionsRecord {
    default: Option<String>,
    #[serde(default)]
    versions: Vec<VersionRecord>,
}

#[derive(Deserialize)]
struct VersionRecord {
    version: String,
    #[serde(default = "yes")]
    native_ok: bool,
}

#[derive(Deserialize)]
struct KeyRecord {
    hash: String,
    id: String,
    #[serde(default = "yes")]
    is_active: bool,
    expires_at: Option<i64>,
    rate_limit_per_minute: u32,
    rate_limit_per_hour: u32,
    #[serde(default = "yes")]
    native_ok: bool,
    tenant: Option<String>,
    allowed_paths: Option<ScopeRecord>,
}

fn yes() -> bool {
    true
}

pub fn hash_key(key: &[u8]) -> Digest32 {
    Sha256::digest(key).into()
}

pub fn parse_hex32(s: &str) -> Option<Digest32> {
    let b = s.as_bytes();
    if b.len() != 64 {
        return None;
    }
    let nib = |c: u8| match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    };
    let mut out = [0u8; 32];
    for i in 0..32 {
        out[i] = (nib(b[2 * i])? << 4) | nib(b[2 * i + 1])?;
    }
    Some(out)
}

/// What a control-plane answer means: nothing new, or a complete new snapshot.
pub enum Parsed {
    Unchanged(Extras),
    Full(Snapshot, Extras),
}

fn net(text: &str) -> Result<Net, String> {
    Net::parse(text).ok_or_else(|| format!("{text} is not a network"))
}

fn scope(record: ScopeRecord) -> Result<Scope, String> {
    let mut rules = Vec::with_capacity(record.rules.len());
    for r in record.rules {
        let mut it = r.into_iter();
        rules.push(match (it.next().as_deref(), it.next()) {
            (Some("all"), None) => ScopeRule::All,
            (Some("prefix"), Some(text)) => ScopeRule::Prefix(text),
            (Some("under"), Some(base)) => ScopeRule::Under(base),
            (kind, _) => return Err(format!("unknown allowed-path rule {kind:?}")),
        });
    }
    Ok(Scope { rules, patterns_json: record.patterns_json })
}

impl Snapshot {
    /// Whether a transformation rule applies to a request: to what the backend is asked for
    /// (`asked`), or to its answer, which the Python gateway matches by the request's own path
    /// without the leading slash.
    pub fn transformed(&self, method: &str, asked: &str, path: &str) -> bool {
        let Some(rules) = &self.transform_rules else { return true };
        if rules.is_empty() {
            return false;
        }
        let answered = path.strip_prefix('/').unwrap_or(path);
        rules.iter().any(|r| r.applies(method, asked) || r.applies(method, answered))
    }
}

impl Snapshot {
    pub fn empty() -> Snapshot {
        Snapshot::from_json(r#"{"keys":[]}"#).expect("an empty snapshot is valid")
    }

    pub fn from_json(text: &str) -> Result<Snapshot, String> {
        match Snapshot::parse(text, None)? {
            Parsed::Full(s, extras) => {
                if let Some(u) = extras.usage {
                    u.apply(&s);
                }
                Ok(s)
            }
            Parsed::Unchanged(_) => Err("snapshot says unchanged but none was loaded".into()),
        }
    }

    /// Parse a control-plane answer. Counters of keys and tenants already in `prev` are carried
    /// over. Anything in it this side cannot read is an error: the snapshot held stays in force
    /// and, once it is too old, everything goes to the Python gateway.
    pub fn parse(text: &str, prev: Option<&Snapshot>) -> Result<Parsed, String> {
        let file: SnapshotFile = serde_json::from_str(text).map_err(|e| format!("snapshot is not valid: {e}"))?;
        // Both are put into answers as they are: they must be what they claim to be.
        if file.ready.as_ref().is_some_and(|r| !matches!(r.status, 200 | 503) || !r.body_json.starts_with('{')) {
            return Err("ready: not an answer to /ready".into());
        }
        if file.ops.as_ref().is_some_and(|o| !o.version_json.starts_with('"')) {
            return Err("ops: the version must be a JSON string".into());
        }
        let extras = Extras { usage: file.tenant_usage, ready: file.ready };
        if file.unchanged {
            return Ok(Parsed::Unchanged(extras));
        }
        let mut tenants: HashMap<String, Arc<Tenant>> = HashMap::with_capacity(file.tenants.len());
        for (id, t) in file.tenants {
            if !t.details_json.starts_with('{') {
                return Err(format!("tenant {id}: details must be a JSON object"));
            }
            let usage = prev.and_then(|p| p.tenants.get(&id)).map(|t| t.usage.clone()).unwrap_or_else(|| Arc::new(Usage::default()));
            let tenant = Tenant {
                id: Arc::from(id.as_str()),
                minute: t.minute,
                hour: t.hour,
                details_json: t.details_json,
                header: HeaderValue::from_str(&id).ok(),
                usage,
            };
            tenants.insert(id, Arc::new(tenant));
        }
        let trusted_proxies = file.trusted_proxies.iter().map(|t| net(t)).collect::<Result<Vec<_>, _>>()?;
        let mut ip_rules = IpRules::default();
        for r in file.ip_rules {
            if !r.details_json.starts_with('{') {
                return Err("IP rule: details must be a JSON object".into());
            }
            let rule = IpRule { allow: r.allow, net: r.net.as_deref().map(net).transpose()?, details_json: r.details_json };
            ip_rules.add(&r.scope, r.scope_id, rule)?;
        }
        let mut policies = Vec::with_capacity(file.policies.len());
        for p in file.policies {
            policies.push(Policy {
                path: p.path,
                methods: p.methods,
                tenants: p.tenants,
                cidrs: p.cidrs.map(|nets| nets.iter().map(|t| net(t)).collect::<Result<Vec<_>, _>>()).transpose()?,
                time: p.time,
                deny: p.deny,
                message_json: p.message_json,
                details_json: p.details_json,
            });
        }
        let mut keys: HashMap<Digest32, KeyEntry, BuildHasherDefault<DigestHasher>> = HashMap::default();
        for k in file.keys {
            let digest = parse_hex32(&k.hash).ok_or_else(|| format!("key {}: hash must be 64 hex characters", k.id))?;
            let reused = prev.and_then(|p| p.keys.get(&digest));
            let tenant = match &k.tenant {
                Some(id) => Some(tenants.get(id).cloned().ok_or_else(|| format!("key {}: unknown tenant {id}", k.id))?),
                None => None,
            };
            keys.insert(
                digest,
                KeyEntry {
                    id: Arc::from(k.id.as_str()),
                    is_active: k.is_active,
                    expires_at: k.expires_at,
                    native_ok: k.native_ok,
                    tenant,
                    scope: k.allowed_paths.map(scope).transpose()?,
                    last_used: reused.map(|e| e.last_used.clone()).unwrap_or_default(),
                    per_minute: k.rate_limit_per_minute,
                    per_hour: k.rate_limit_per_hour,
                    minute: reused.map(|e| e.minute.clone()).unwrap_or_else(|| Arc::new(Window::new())),
                    hour: reused.map(|e| e.hour.clone()).unwrap_or_else(|| Arc::new(Window::new())),
                },
            );
        }
        // Without an `api_versions` section, the gateway's shipped defaults apply: versions 1 (default) and 2.
        let api_versions = match file.api_versions {
            Some(v) => ApiVersions {
                default: v.default,
                native_ok: v.versions.into_iter().map(|r| (r.version, r.native_ok)).collect(),
            },
            None => ApiVersions {
                default: Some("1".into()),
                native_ok: [("1".to_string(), true), ("2".to_string(), true)].into_iter().collect(),
            },
        };
        let key_header = file.key_header.as_deref().unwrap_or("x-api-key");
        let key_header = HeaderName::from_bytes(key_header.to_ascii_lowercase().as_bytes())
            .map_err(|_| format!("{key_header} is not a header name"))?;
        if let Some(r) = &file.refusals {
            for w in [&r.missing_key, &r.invalid_key] {
                if !w.message_json.starts_with('"') || !w.details_json.starts_with('{') {
                    return Err("refusals: a message must be a JSON string and details a JSON object".into());
                }
            }
        }
        let snap = Snapshot {
            key_header,
            refusals: file.refusals,
            ops: file.ops,
            websocket_url: file.websocket.filter(|u| u.starts_with("ws://")),
            websocket: None,
            grpc_url: file.grpc.filter(|u| u.starts_with("http://")),
            grpc: None,
            agent_urls: file.agents,
            agents: HashMap::new(),
            agent_failure: file.agent_failure.filter(|f| f.message_json.starts_with('"')),
            dynamic_routes: file.dynamic_routes,
            cross_origin: file.cross_origin,
            error_pages: file.error_pages,
            transform_rules: file.transform_rules,
            inject_request_id: file.inject_request_id,
            revision: file.revision,
            api_versions,
            rate_limit_enabled: file.rate_limit_enabled,
            // Without these sections, the gateway's shipped defaults apply.
            circuit_breaker: file
                .circuit_breaker
                .map(|c| breaker::Config { enabled: c.enabled, threshold: c.failure_threshold, timeout_secs: c.timeout_seconds })
                .unwrap_or_default(),
            upstream_timeout: Duration::from_secs_f64(file.upstream_timeout_seconds.filter(|s| *s > 0.0 && s.is_finite()).unwrap_or(30.0)),
            keys,
            tenants,
            trusted_proxies,
            ip_rules,
            policies,
            fallthrough_prefixes: file.fallthrough_prefixes,
            protocol_urls: ROUTED.map(|p| file.backends.get(p.name()).cloned().flatten().filter(|u| !u.is_empty())),
            routes: Default::default(),
            // Without the section, the gateway's shipped defaults apply.
            boundary: file.boundary.map(Boundary::from_record).unwrap_or_else(Boundary::defaults),
        };
        Ok(Parsed::Full(snap, extras))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hex_round_trips_a_digest() {
        let d = hash_key(b"sk-test");
        let hex: String = d.iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(parse_hex32(&hex), Some(d));
        assert_eq!(parse_hex32("zz"), None);
    }

    #[test]
    fn the_key_header_and_the_refusal_wording_are_read() {
        let snap = Snapshot::from_json(r#"{"keys":[]}"#).unwrap();
        assert!(snap.key_header == "x-api-key" && snap.refusals.is_none());
        let snap = Snapshot::from_json(
            r#"{"keys":[],"key_header":"X-Gateway-Key","refusals":{
                "missing_key":{"message_json":"\"m\"","details_json":"{}"},"invalid_key":{"message_json":"\"i\"","details_json":"{\"a\":1}"}}}"#,
        )
        .unwrap();
        assert_eq!(snap.key_header, "x-gateway-key");
        assert_eq!(snap.refusals.unwrap().invalid_key.details_json, r#"{"a":1}"#);
        assert!(Snapshot::from_json(r#"{"keys":[],"key_header":"not a header"}"#).is_err());
        assert!(Snapshot::from_json(r#"{"keys":[],"refusals":{"missing_key":{"message_json":"m","details_json":"{}"},"invalid_key":{"message_json":"\"i\"","details_json":"{}"}}}"#).is_err());
    }

    #[test]
    fn loads_keys_and_defaults() {
        let d = hash_key(b"sk-test");
        let hex: String = d.iter().map(|b| format!("{b:02x}")).collect();
        let snap = Snapshot::from_json(&format!(
            r#"{{"keys":[{{"hash":"{hex}","id":"k1","rate_limit_per_minute":5,"rate_limit_per_hour":50}}]}}"#
        ))
        .unwrap();
        let e = snap.keys.get(&d).unwrap();
        assert!(e.is_active && e.native_ok && e.per_minute == 5);
    }

    #[test]
    fn shipped_version_defaults_apply_when_the_section_is_absent() {
        let snap = Snapshot::from_json(r#"{"keys":[]}"#).unwrap();
        assert_eq!(snap.api_versions.default.as_deref(), Some("1"));
        assert_eq!(snap.api_versions.native_ok.get("2"), Some(&true));
    }

    #[test]
    fn the_health_path_and_readiness_are_read_and_checked() {
        let snap = Snapshot::from_json(r#"{"keys":[]}"#).unwrap();
        assert!(snap.ops.is_none());
        let text = r#"{"keys":[],"ops":{"health_path":"/health","version_json":"\"1.2\""},"ready":{"status":200,"body_json":"{\"status\":\"ready\"}"}}"#;
        let Parsed::Full(snap, extras) = Snapshot::parse(text, None).unwrap() else { panic!("expected a full snapshot") };
        let ops = snap.ops.unwrap();
        assert_eq!((ops.health_path.as_deref(), ops.version_json.as_str()), (Some("/health"), "\"1.2\""));
        assert_eq!(extras.ready, Some(Readiness { status: 200, body_json: r#"{"status":"ready"}"#.into() }));
        // Readiness comes with an answer that says nothing else changed, too.
        let same = r#"{"unchanged":true,"revision":"r","ready":{"status":503,"body_json":"{}"}}"#;
        let Parsed::Unchanged(extras) = Snapshot::parse(same, Some(&snap_with_nothing())).unwrap() else { panic!("expected unchanged") };
        assert_eq!(extras.ready.unwrap().status, 503);
        // Neither is taken on trust: both are put into answers as they are.
        for bad in [
            r#"{"keys":[],"ops":{"health_path":"/health","version_json":"1.2"}}"#,
            r#"{"keys":[],"ready":{"status":204,"body_json":"{}"}}"#,
            r#"{"keys":[],"ready":{"status":200,"body_json":"ready"}}"#,
        ] {
            assert!(Snapshot::from_json(bad).is_err(), "{bad}");
        }
    }

    fn snap_with_nothing() -> Snapshot {
        Snapshot::from_json(r#"{"keys":[]}"#).unwrap()
    }

    #[test]
    fn counters_survive_an_update_and_unchanged_is_recognised() {
        let d = hash_key(b"sk-test");
        let hex: String = d.iter().map(|b| format!("{b:02x}")).collect();
        let text = format!(
            r#"{{"revision":"r1","keys":[{{"hash":"{hex}","id":"k1","rate_limit_per_minute":2,"rate_limit_per_hour":50}}]}}"#
        );
        let first = Snapshot::from_json(&text).unwrap();
        let e = first.keys.get(&d).unwrap();
        assert!(e.minute.allow(2, 60, 1000) && e.minute.allow(2, 60, 1000));
        let Parsed::Full(second, _) = Snapshot::parse(&text, Some(&first)).unwrap() else { panic!("expected a full snapshot") };
        assert!(!second.keys.get(&d).unwrap().minute.allow(2, 60, 1000), "the window was reset by the update");
        assert_eq!(second.revision.as_deref(), Some("r1"));
        assert!(matches!(
            Snapshot::parse(r#"{"unchanged":true,"revision":"r1"}"#, Some(&first)).unwrap(),
            Parsed::Unchanged(Extras { usage: None, ready: None })
        ));
    }

    #[test]
    fn breaker_and_timeout_come_from_the_control_plane_or_default() {
        let snap = Snapshot::from_json(r#"{"keys":[]}"#).unwrap();
        assert_eq!(snap.circuit_breaker, breaker::Config { enabled: true, threshold: 5, timeout_secs: 60 });
        assert_eq!(snap.upstream_timeout, Duration::from_secs(30));
        let snap = Snapshot::from_json(
            r#"{"keys":[],"upstream_timeout_seconds":2.5,"circuit_breaker":{"enabled":false,"failure_threshold":2,"timeout_seconds":7}}"#,
        )
        .unwrap();
        assert_eq!(snap.circuit_breaker, breaker::Config { enabled: false, threshold: 2, timeout_secs: 7 });
        assert_eq!(snap.upstream_timeout, Duration::from_millis(2500));
    }

    fn hex(key: &[u8]) -> String {
        hash_key(key).iter().map(|b| format!("{b:02x}")).collect()
    }

    fn governed() -> String {
        format!(
            r#"{{"revision":"r1","keys":[
              {{"hash":"{}","id":"k1","rate_limit_per_minute":5,"rate_limit_per_hour":50,"tenant":"acme",
                "allowed_paths":{{"rules":[["under","/api/users"],["prefix","/api/pub"]],"patterns_json":"[\"/api/users\",\"/api/pub*\"]"}}}}],
             "tenants":{{"acme":{{"minute":2,"hour":null,"details_json":"{{\"tenant_id\":\"acme\",\"quota\":{{}}}}"}}}},
             "trusted_proxies":["127.0.0.1/32"],
             "ip_rules":[{{"scope":"tenant","scope_id":"acme","allow":false,"net":"10.0.0.0/8","details_json":"{{\"rule_id\":\"r\",\"scope\":\"tenant\"}}"}}],
             "policies":[{{"path":"/api/admin*","methods":["DELETE"],"tenants":[],"cidrs":null,"time":[60,120],"deny":true,
                          "message_json":"\"no\"","details_json":"{{\"policy_id\":\"p\"}}"}}],
             "tenant_usage":{{"minute_start":600,"hour_start":0,"used":{{"acme":[2,2]}}}}}}"#,
            hex(b"sk-test")
        )
    }

    #[test]
    fn tenants_scopes_rules_and_policies_are_loaded() {
        let snap = Snapshot::from_json(&governed()).unwrap();
        let e = snap.keys.get(&hash_key(b"sk-test")).unwrap();
        let tenant = e.tenant.as_ref().unwrap();
        assert_eq!((&*tenant.id, tenant.minute, tenant.hour), ("acme", Some(2), None));
        let scope = e.scope.as_ref().unwrap();
        assert!(scope.allows("/api/users/7") && scope.allows("/api/public") && !scope.allows("/api/orders"));
        assert!(snap.ip_rules.blocking("10.1.2.3".parse().unwrap(), Some("acme"), "k1").is_some());
        assert!(snap.ip_rules.blocking("10.1.2.3".parse().unwrap(), None, "k1").is_none());
        assert_eq!(snap.policies.len(), 1);
        assert_eq!(snap.trusted_proxies.len(), 1);
        // The database already holds the tenant's two requests for the minute starting at 600.
        assert!(tenant.usage.admit(tenant.minute, tenant.hour, 610).is_err());
        assert!(tenant.usage.admit(tenant.minute, tenant.hour, 660).is_ok());
    }

    #[test]
    fn a_tenants_counts_survive_an_update_and_an_unchanged_answer_carries_usage() {
        let first = Snapshot::from_json(&governed()).unwrap();
        let Parsed::Full(second, Extras { usage, .. }) = Snapshot::parse(&governed(), Some(&first)).unwrap() else { panic!("expected a full snapshot") };
        assert!(Arc::ptr_eq(&first.tenants["acme"].usage, &second.tenants["acme"].usage));
        assert!(usage.is_some());
        let answer = r#"{"unchanged":true,"revision":"r1","tenant_usage":{"minute_start":660,"hour_start":0,"used":{}}}"#;
        let Parsed::Unchanged(Extras { usage: Some(usage), .. }) = Snapshot::parse(answer, Some(&second)).unwrap() else { panic!("expected usage") };
        usage.apply(&second);
        let t = &second.tenants["acme"];
        assert!(t.usage.admit(t.minute, t.hour, 661).is_ok());
    }

    #[test]
    fn what_cannot_be_read_is_refused_whole() {
        let bad_net = r#"{"keys":[],"trusted_proxies":["nonsense"]}"#;
        assert!(Snapshot::from_json(bad_net).is_err());
        let unknown_tenant = format!(r#"{{"keys":[{{"hash":"{}","id":"k","rate_limit_per_minute":1,"rate_limit_per_hour":1,"tenant":"ghost"}}]}}"#, hex(b"k"));
        assert!(Snapshot::from_json(&unknown_tenant).is_err());
        let odd_rule = format!(
            r#"{{"keys":[{{"hash":"{}","id":"k","rate_limit_per_minute":1,"rate_limit_per_hour":1,"allowed_paths":{{"rules":[["regex","x"]],"patterns_json":"[]"}}}}]}}"#,
            hex(b"k")
        );
        assert!(Snapshot::from_json(&odd_rule).is_err());
    }

    #[test]
    fn a_bad_hash_is_rejected() {
        assert!(Snapshot::from_json(r#"{"keys":[{"hash":"abc","id":"k","rate_limit_per_minute":1,"rate_limit_per_hour":1}]}"#).is_err());
    }
}
