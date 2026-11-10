//! The working set the control plane gives the data plane, held in memory.
//!
//! Keys are stored only as SHA-256 digests (what the database stores), so the data plane
//! never holds a usable credential. For now the snapshot is a JSON file; the control-plane
//! channel that serves it live, with change events, is the next step (see DESIGN.md).
use crate::breaker;
use crate::limits::Window;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;
use std::hash::{BuildHasherDefault, Hasher};

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
    pub id: String,
    pub is_active: bool,
    pub expires_at: Option<i64>,
    /// False when something this phase does not do natively applies to the key (tenant quota,
    /// IP rules, policies): its requests are handed to the control plane.
    pub native_ok: bool,
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
    Unchanged,
    Full(Snapshot),
}

impl Snapshot {
    pub fn empty() -> Snapshot {
        Snapshot::from_json(r#"{"keys":[]}"#).expect("an empty snapshot is valid")
    }

    pub fn from_json(text: &str) -> Result<Snapshot, String> {
        match Snapshot::parse(text, None)? {
            Parsed::Full(s) => Ok(s),
            Parsed::Unchanged => Err("snapshot says unchanged but none was loaded".into()),
        }
    }

    /// Parse a control-plane answer. Counters of keys already in `prev` are carried over.
    pub fn parse(text: &str, prev: Option<&Snapshot>) -> Result<Parsed, String> {
        let file: SnapshotFile = serde_json::from_str(text).map_err(|e| format!("snapshot is not valid: {e}"))?;
        if file.unchanged {
            return Ok(Parsed::Unchanged);
        }
        let mut keys: HashMap<Digest32, KeyEntry, BuildHasherDefault<DigestHasher>> = HashMap::default();
        for k in file.keys {
            let digest = parse_hex32(&k.hash).ok_or_else(|| format!("key {}: hash must be 64 hex characters", k.id))?;
            let reused = prev.and_then(|p| p.keys.get(&digest));
            keys.insert(
                digest,
                KeyEntry {
                    id: k.id,
                    is_active: k.is_active,
                    expires_at: k.expires_at,
                    native_ok: k.native_ok,
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
        Ok(Parsed::Full(Snapshot {
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
            fallthrough_prefixes: file.fallthrough_prefixes,
        }))
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
        let Parsed::Full(second) = Snapshot::parse(&text, Some(&first)).unwrap() else { panic!("expected a full snapshot") };
        assert!(!second.keys.get(&d).unwrap().minute.allow(2, 60, 1000), "the window was reset by the update");
        assert_eq!(second.revision.as_deref(), Some("r1"));
        assert!(matches!(
            Snapshot::parse(r#"{"unchanged":true,"revision":"r1"}"#, Some(&first)).unwrap(),
            Parsed::Unchanged
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

    #[test]
    fn a_bad_hash_is_rejected() {
        assert!(Snapshot::from_json(r#"{"keys":[{"hash":"abc","id":"k","rate_limit_per_minute":1,"rate_limit_per_hour":1}]}"#).is_err());
    }
}
