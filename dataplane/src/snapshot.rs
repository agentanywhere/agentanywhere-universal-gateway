//! The working set the control plane gives the data plane, held in memory.
//!
//! Keys are stored only as SHA-256 digests (what the database stores), so the data plane
//! never holds a usable credential. The control plane compiles everything that decides a
//! request (key scopes, tenants and their quotas, IP rules, policies) into the forms used here;
//! anything it cannot express that way it marks as not native, and the Python gateway keeps it.
use crate::breaker;
use crate::govern::{IpRule, IpRules, Net, Policy, Scope, ScopeRule};
use crate::limits::Window;
use crate::tenant::{Tenant, Usage};
use hyper::header::HeaderValue;
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
}

#[derive(Deserialize)]
struct SnapshotFile {
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
    Unchanged(Option<UsageBase>),
    Full(Snapshot, Option<UsageBase>),
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
    pub fn empty() -> Snapshot {
        Snapshot::from_json(r#"{"keys":[]}"#).expect("an empty snapshot is valid")
    }

    pub fn from_json(text: &str) -> Result<Snapshot, String> {
        match Snapshot::parse(text, None)? {
            Parsed::Full(s, usage) => {
                if let Some(u) = usage {
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
        if file.unchanged {
            return Ok(Parsed::Unchanged(file.tenant_usage));
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
        let snap = Snapshot {
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
        };
        Ok(Parsed::Full(snap, file.tenant_usage))
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
            Parsed::Unchanged(None)
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
        let Parsed::Full(second, usage) = Snapshot::parse(&governed(), Some(&first)).unwrap() else { panic!("expected a full snapshot") };
        assert!(Arc::ptr_eq(&first.tenants["acme"].usage, &second.tenants["acme"].usage));
        assert!(usage.is_some());
        let answer = r#"{"unchanged":true,"revision":"r1","tenant_usage":{"minute_start":660,"hour_start":0,"used":{}}}"#;
        let Parsed::Unchanged(Some(usage)) = Snapshot::parse(answer, Some(&second)).unwrap() else { panic!("expected usage") };
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
