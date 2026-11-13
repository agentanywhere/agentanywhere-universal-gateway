//! Enough of JSON Schema to say that a value is certain to pass.
//!
//! The Python gateway validates the arguments of an MCP `tools/call` against the tool's registered
//! schema with the `jsonschema` library (draft 2020-12 unless the schema says otherwise) and words
//! the refusal itself. This side never refuses: it only needs to know when a value certainly
//! passes, so that the request can be forwarded without asking. A schema is understood here only
//! if every keyword in it, at every depth, is one of the few below and is written the way the
//! draft requires; any other schema is not compiled, and its tool's calls stay the Python
//! gateway's. Where the two could disagree on a value (a whole number written as `1.0`, a number
//! too large to compare exactly, a value compared with an `enum` of anything but strings), the
//! answer here is "not certain".

use serde_json::{Map, Value};

/// How deep a schema, or a value checked against one, may nest before this side stops looking.
const MAX_DEPTH: usize = 32;

/// Whole numbers up to here are held exactly by the floating-point numbers both sides compare with.
const EXACT: f64 = 9_007_199_254_740_992.0;

#[derive(Debug, Clone, Copy, PartialEq)]
enum Type {
    Object,
    Array,
    String,
    Number,
    Integer,
    Boolean,
    Null,
}

#[derive(Debug, Default)]
pub struct Schema {
    /// Empty: any type.
    types: Vec<Type>,
    properties: Vec<(String, Schema)>,
    required: Vec<String>,
    /// `None`: anything more is welcome. `Some(None)`: nothing more is. `Some(Some(s))`: what more there is must pass `s`.
    additional: Option<Option<Box<Schema>>>,
    items: Option<Box<Schema>>,
    /// An `enum` of strings.
    one_of: Option<Vec<String>>,
    minimum: Option<f64>,
    maximum: Option<f64>,
    min_length: Option<u64>,
    max_length: Option<u64>,
    min_items: Option<u64>,
    max_items: Option<u64>,
}

fn type_named(name: &str) -> Option<Type> {
    Some(match name {
        "object" => Type::Object,
        "array" => Type::Array,
        "string" => Type::String,
        "number" => Type::Number,
        "integer" => Type::Integer,
        "boolean" => Type::Boolean,
        "null" => Type::Null,
        _ => return None,
    })
}

fn count(v: &Value) -> Option<u64> {
    v.as_u64().filter(|_| v.is_u64())
}

fn bound(v: &Value) -> Option<f64> {
    v.as_f64().filter(|n| n.is_finite() && n.abs() <= EXACT)
}

impl Schema {
    /// The schema, if this side understands all of it. `None` for anything else.
    pub fn compile(schema: &Value) -> Option<Schema> {
        Schema::at(schema, 0)
    }

    fn at(schema: &Value, depth: usize) -> Option<Schema> {
        if depth > MAX_DEPTH {
            return None;
        }
        let members = match schema {
            Value::Bool(true) => return Some(Schema::default()),
            Value::Object(members) => members,
            _ => return None,
        };
        let mut s = Schema::default();
        for (keyword, value) in members {
            match keyword.as_str() {
                "type" => {
                    let names: Vec<&Value> = match value {
                        Value::String(_) => vec![value],
                        Value::Array(all) if !all.is_empty() => all.iter().collect(),
                        _ => return None,
                    };
                    for name in names {
                        let t = type_named(name.as_str()?)?;
                        if s.types.contains(&t) {
                            return None;
                        }
                        s.types.push(t);
                    }
                }
                "properties" => {
                    for (name, sub) in value.as_object()? {
                        s.properties.push((name.clone(), Schema::at(sub, depth + 1)?));
                    }
                }
                "required" => {
                    for name in value.as_array()? {
                        let name = name.as_str()?.to_owned();
                        if s.required.contains(&name) {
                            return None;
                        }
                        s.required.push(name);
                    }
                }
                "additionalProperties" => {
                    s.additional = Some(match value {
                        Value::Bool(false) => None,
                        other => Some(Box::new(Schema::at(other, depth + 1)?)),
                    })
                }
                "items" => s.items = Some(Box::new(Schema::at(value, depth + 1)?)),
                "enum" => s.one_of = Some(value.as_array()?.iter().map(|v| v.as_str().map(str::to_owned)).collect::<Option<_>>()?),
                "minimum" => s.minimum = Some(bound(value)?),
                "maximum" => s.maximum = Some(bound(value)?),
                "minLength" => s.min_length = Some(count(value)?),
                "maxLength" => s.max_length = Some(count(value)?),
                "minItems" => s.min_items = Some(count(value)?),
                "maxItems" => s.max_items = Some(count(value)?),
                // Said about a value, never required of it. `format` is among them: the Python
                // gateway validates without a format checker.
                "title" | "description" | "$comment" | "format" => {
                    value.as_str()?;
                }
                "deprecated" | "readOnly" | "writeOnly" => {
                    value.as_bool()?;
                }
                "examples" => {
                    value.as_array()?;
                }
                "default" => {}
                // What an OpenAPI document adds to a schema. None of it is a JSON Schema keyword,
                // so the Python library passes over it, `nullable` included: null is accepted
                // only where `type` says so.
                "example" | "nullable" | "xml" | "externalDocs" | "discriminator" => {}
                extension if extension.starts_with("x-") => {}
                _ => return None,
            }
        }
        Some(s)
    }

    /// Whether an object with these members is certain to pass.
    pub fn accepts_object(&self, members: &Map<String, Value>) -> bool {
        self.object_passes(members, 0) && self.typed(Type::Object)
    }

    fn typed(&self, t: Type) -> bool {
        self.types.is_empty() || self.types.contains(&t)
    }

    fn accepts(&self, v: &Value, depth: usize) -> bool {
        if depth > MAX_DEPTH {
            return false;
        }
        // An `enum` is of strings here; how another kind of value compares is not this side's to say.
        if let Some(allowed) = &self.one_of {
            if !v.as_str().is_some_and(|text| allowed.iter().any(|a| a == text)) {
                return false;
            }
        }
        match v {
            Value::Null => self.typed(Type::Null),
            Value::Bool(_) => self.typed(Type::Boolean),
            Value::String(text) => {
                if !self.typed(Type::String) {
                    return false;
                }
                if self.min_length.is_none() && self.max_length.is_none() {
                    return true;
                }
                // Counted in characters, as Python counts them.
                let length = text.chars().count() as u64;
                self.min_length.is_none_or(|least| length >= least) && self.max_length.is_none_or(|most| length <= most)
            }
            Value::Number(n) => {
                let whole = n.is_i64() || n.is_u64();
                // A whole number written with a fraction (`1.0`) is an integer to the library;
                // rather than follow it there, such a value is not certain.
                let fits = self.types.is_empty() || self.types.contains(&Type::Number) || (whole && self.types.contains(&Type::Integer));
                if !fits {
                    return false;
                }
                if self.minimum.is_none() && self.maximum.is_none() {
                    return true;
                }
                let Some(value) = n.as_f64().filter(|x| x.is_finite() && x.abs() <= EXACT) else { return false };
                self.minimum.is_none_or(|least| value >= least) && self.maximum.is_none_or(|most| value <= most)
            }
            Value::Array(all) => {
                let length = all.len() as u64;
                self.typed(Type::Array)
                    && self.min_items.is_none_or(|least| length >= least)
                    && self.max_items.is_none_or(|most| length <= most)
                    && self.items.as_ref().is_none_or(|each| all.iter().all(|v| each.accepts(v, depth + 1)))
            }
            Value::Object(members) => self.typed(Type::Object) && self.object_passes(members, depth),
        }
    }

    fn object_passes(&self, members: &Map<String, Value>, depth: usize) -> bool {
        if depth > MAX_DEPTH || self.one_of.is_some() {
            return false;
        }
        if !self.required.iter().all(|name| members.contains_key(name)) {
            return false;
        }
        members.iter().all(|(name, value)| match self.properties.iter().find(|(known, _)| known == name) {
            Some((_, schema)) => schema.accepts(value, depth + 1),
            None => match &self.additional {
                None => true,
                Some(None) => false,
                Some(Some(schema)) => schema.accepts(value, depth + 1),
            },
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn passes(schema: Value, arguments: Value) -> bool {
        Schema::compile(&schema).expect("a schema this side understands").accepts_object(arguments.as_object().unwrap())
    }

    #[test]
    fn a_plain_schema_is_followed() {
        let schema = json!({
            "type": "object", "title": "Search", "description": "Find things",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 5, "description": "what to find"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                "ratio": {"type": "number"},
                "mode": {"enum": ["fast", "full"]},
                "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 2},
                "where": {"type": "object", "properties": {"city": {"type": ["string", "null"]}}, "required": ["city"], "additionalProperties": false},
                "url": {"type": "string", "format": "uri"},
                "any": true
            },
            "required": ["query"],
            "additionalProperties": false
        });
        let ok = |arguments: Value| passes(schema.clone(), arguments);
        assert!(ok(json!({"query": "abc"})));
        assert!(ok(json!({"query": "héllo", "limit": 100, "ratio": 0.5, "mode": "full", "tags": ["a", "b"], "where": {"city": null}, "url": "not a uri", "any": [1, {}]})));
        for refused in [
            json!({}),
            json!({"query": ""}),
            json!({"query": "abcdef"}),
            json!({"query": 7}),
            json!({"query": null}),
            json!({"query": "a", "limit": 0}),
            json!({"query": "a", "limit": 101}),
            json!({"query": "a", "limit": "3"}),
            json!({"query": "a", "limit": true}),
            json!({"query": "a", "ratio": "x"}),
            json!({"query": "a", "mode": "slow"}),
            json!({"query": "a", "tags": []}),
            json!({"query": "a", "tags": ["a", "b", "c"]}),
            json!({"query": "a", "tags": ["a", 1]}),
            json!({"query": "a", "where": {}}),
            json!({"query": "a", "where": {"city": "x", "street": "y"}}),
            json!({"query": "a", "where": []}),
            json!({"query": "a", "extra": 1}),
        ] {
            assert!(!ok(refused.clone()), "{refused}");
        }
    }

    #[test]
    fn where_the_two_sides_could_disagree_the_value_is_not_certain() {
        // A whole number written with a fraction is an integer to the Python library.
        assert!(!passes(json!({"properties": {"n": {"type": "integer"}}}), json!({"n": 1.0})));
        assert!(passes(json!({"properties": {"n": {"type": "number"}}}), json!({"n": 1.0})));
        // A number too large to compare exactly.
        assert!(!passes(json!({"properties": {"n": {"minimum": 0}}}), json!({"n": 18446744073709551615u64})));
        assert!(passes(json!({"properties": {"n": {"type": "integer"}}}), json!({"n": 18446744073709551615u64})));
        // A bound applies to numbers only; a string passes it.
        assert!(passes(json!({"properties": {"n": {"minimum": 5, "minLength": 2}}}), json!({"n": "ab"})));
        assert!(passes(json!({"properties": {"n": {"minimum": 5, "minLength": 2}}}), json!({"n": 5})));
        // A member that is present counts as given, null included.
        assert!(passes(json!({"required": ["a"]}), json!({"a": null})));
        // Anything more than the schema names passes one given for the rest, or fails it.
        let rest = json!({"properties": {"a": {"type": "string"}}, "additionalProperties": {"type": "integer"}});
        assert!(passes(rest.clone(), json!({"a": "x", "b": 1, "c": 2})));
        assert!(!passes(rest, json!({"a": "x", "b": "1"})));
        // A schema too deep to follow is not understood at all.
        let mut deep = json!({"type": "string"});
        for _ in 0..40 {
            deep = json!({"items": deep});
        }
        assert!(Schema::compile(&deep).is_none());
    }

    #[test]
    fn a_schema_with_anything_else_in_it_is_not_understood() {
        for schema in [
            json!({"type": "object", "properties": {"a": {"pattern": "^a"}}}),
            json!({"$schema": "http://json-schema.org/draft-04/schema#", "type": "object"}),
            json!({"$ref": "#/$defs/a"}),
            json!({"oneOf": [{"type": "string"}]}),
            json!({"properties": {"a": {"const": 1}}}),
            json!({"properties": {"a": {"enum": ["a", 1]}}}),
            json!({"properties": {"a": {"enum": [1, 2]}}}),
            json!({"properties": {"a": false}}),
            json!({"properties": {"a": {"type": "str"}}}),
            json!({"properties": {"a": {"type": []}}}),
            json!({"properties": {"a": {"type": ["string", "string"]}}}),
            json!({"required": ["a", "a"]}),
            json!({"required": "a"}),
            json!({"properties": {"a": {"minLength": -1}}}),
            json!({"properties": {"a": {"minLength": 1.5}}}),
            json!({"properties": {"a": {"minimum": "1"}}}),
            json!({"properties": {"a": {"items": [{"type": "string"}]}}}),
            json!({"properties": {"a": {"title": 7}}}),
            json!({"vendor": true}),
            json!(false),
            json!([]),
            json!("object"),
        ] {
            assert!(Schema::compile(&schema).is_none(), "{schema}");
        }
        // What an OpenAPI document adds is passed over, as the Python library passes over it.
        let openapi = json!({"properties": {"a": {"type": "string", "nullable": true, "example": "x", "x-order": 1}}});
        assert!(passes(openapi.clone(), json!({"a": "x"})));
        assert!(!passes(openapi, json!({"a": null})));
        // A schema of the wrong type for arguments passes nothing, and that is certain to no one.
        assert!(!passes(json!({"type": "array"}), json!({})));
        assert!(!passes(json!({"enum": ["a"]}), json!({})));
    }
}
