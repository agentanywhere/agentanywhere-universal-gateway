//! The working set the control plane gives the data plane, held in memory.
//!
//! Keys are stored only as SHA-256 digests (what the database stores), so the data plane
//! never holds a usable credential. For now the snapshot is a JSON file; the control-plane
//! channel that serves it live, with change events, is the next step (see DESIGN.md).
use crate::limits::Window;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
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
    /// Goes into the request-log row once the data plane emits logs (next step).
    #[allow(dead_code)]
    pub id: String,
    pub is_active: bool,
    pub expires_at: Option<i64>,
    /// False when something this phase does not do natively applies to the key (tenant quota,
    /// IP rules, policies): its requests are handed to the control plane.
    pub native_ok: bool,
    pub per_minute: u32,
    pub per_hour: u32,
    pub minute: Window,
    pub hour: Window,
}

/// API versions as the data plane needs them: which are served as plain pass-through.
pub struct ApiVersions {
    pub default: Option<String>,
    /// version -> true when it is active, has no deprecation date and no backend of its own.
    /// Anything else (deprecated, sunset, removed, own backend) is handled by the control plane.
    pub native_ok: HashMap<String, bool>,
}

pub struct Snapshot {
    pub api_versions: ApiVersions,
    pub rate_limit_enabled: bool,
    pub keys: HashMap<Digest32, KeyEntry, BuildHasherDefault<DigestHasher>>,
    /// Path prefixes the control plane must handle (dynamic routes, transformation rules, ...).
    pub fallthrough_prefixes: Vec<String>,
}

#[derive(Deserialize)]
struct SnapshotFile {
    keys: Vec<KeyRecord>,
    #[serde(default)]
    fallthrough_prefixes: Vec<String>,
    #[serde(default = "yes")]
    rate_limit_enabled: bool,
    api_versions: Option<VersionsRecord>,
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

impl Snapshot {
    pub fn from_json(text: &str) -> Result<Snapshot, String> {
        let file: SnapshotFile = serde_json::from_str(text).map_err(|e| format!("snapshot is not valid: {e}"))?;
        let mut keys: HashMap<Digest32, KeyEntry, BuildHasherDefault<DigestHasher>> = HashMap::default();
        for k in file.keys {
            let digest = parse_hex32(&k.hash).ok_or_else(|| format!("key {}: hash must be 64 hex characters", k.id))?;
            keys.insert(
                digest,
                KeyEntry {
                    id: k.id,
                    is_active: k.is_active,
                    expires_at: k.expires_at,
                    native_ok: k.native_ok,
                    per_minute: k.rate_limit_per_minute,
                    per_hour: k.rate_limit_per_hour,
                    minute: Window::new(),
                    hour: Window::new(),
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
        Ok(Snapshot { api_versions, rate_limit_enabled: file.rate_limit_enabled, keys, fallthrough_prefixes: file.fallthrough_prefixes })
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
    fn a_bad_hash_is_rejected() {
        assert!(Snapshot::from_json(r#"{"keys":[{"hash":"abc","id":"k","rate_limit_per_minute":1,"rate_limit_per_hour":1}]}"#).is_err());
    }
}
