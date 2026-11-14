//! A `POST /mqtt/publish` body, read as the Python gateway reads it and written as it writes it.
//!
//! The Python gateway parses the body with `json.loads`, takes `topic`, `payload` and `qos` out of
//! it, and publishes `json.dumps(payload)`. What reaches the broker is therefore Python's own
//! spelling of the payload: `, ` and `: ` between members, everything outside ASCII escaped,
//! numbers as Python prints them. This module produces those bytes for the bodies it is certain
//! about and says nothing (`None`) for every other, which the Python gateway then handles. It is
//! certain about less than Python accepts: a body with a byte-order mark or in UTF-16, `NaN`, a
//! member named twice, a topic that is not a non-empty string, a `qos` that is not written `0`,
//! `1` or `2`: all of those are left to it.

/// What to publish.
#[derive(Debug, PartialEq)]
pub struct Publish {
    pub topic: String,
    pub qos: u8,
    /// `json.dumps(payload)`.
    pub payload: Vec<u8>,
}

/// Deeper than this, a body is the Python gateway's (which gives up at some depth of its own).
const MAX_DEPTH: usize = 64;
/// Python refuses to read an integer of more than 4300 digits; long before that, it is asked.
const MAX_INT_DIGITS: usize = 1000;
/// The longest topic MQTT can carry, in bytes.
const MAX_TOPIC: usize = 65_535;

struct Reader<'a> {
    text: &'a [u8],
    at: usize,
}

/// Where a string that is read goes.
enum Sink<'a> {
    /// Nowhere: it only has to be valid.
    Nothing,
    /// Out again as `json.dumps` writes a string.
    Dumped(&'a mut Vec<u8>),
    /// A string of its own. One that Python could hold but UTF-8 cannot (half a surrogate pair) fails.
    Text(&'a mut String),
}

impl Reader<'_> {
    fn peek(&self) -> Option<u8> {
        self.text.get(self.at).copied()
    }

    fn blank(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.at += 1;
        }
    }

    fn take(&mut self, byte: u8) -> Option<()> {
        (self.peek()? == byte).then(|| self.at += 1)
    }

    fn word(&mut self, word: &str) -> Option<()> {
        self.text[self.at..].starts_with(word.as_bytes()).then(|| self.at += word.len())
    }

    fn hex4(&mut self) -> Option<u32> {
        let digits = self.text.get(self.at..self.at + 4)?;
        let mut unit = 0;
        for d in digits {
            unit = unit * 16 + (*d as char).to_digit(16)?;
        }
        self.at += 4;
        Some(unit)
    }

    /// A string, from its opening quote to its closing one.
    fn string(&mut self, mut into: Sink) -> Option<()> {
        self.take(b'"')?;
        // The first half of a surrogate pair written as an escape, waiting for its second.
        let mut high: Option<u32> = None;
        loop {
            let start = self.at;
            while !matches!(self.peek()?, b'"' | b'\\' | 0..=0x1f) {
                self.at += 1;
            }
            if self.at > start {
                // The body is valid UTF-8 as a whole, and a run ends only at an ASCII byte.
                let run = std::str::from_utf8(&self.text[start..self.at]).ok()?;
                match &mut into {
                    Sink::Nothing => {}
                    Sink::Dumped(out) => run.chars().for_each(|c| dump_char(out, c as u32)),
                    Sink::Text(text) => {
                        if high.take().is_some() {
                            return None;
                        }
                        text.push_str(run);
                    }
                }
            }
            match self.peek()? {
                b'"' => {
                    self.at += 1;
                    return high.is_none().then_some(());
                }
                b'\\' => {
                    self.at += 1;
                    let escape = self.peek()?;
                    self.at += 1;
                    let unit = match escape {
                        b'"' | b'\\' | b'/' => u32::from(escape),
                        b'b' => 8,
                        b'f' => 12,
                        b'n' => 10,
                        b'r' => 13,
                        b't' => 9,
                        b'u' => self.hex4()?,
                        _ => return None,
                    };
                    match &mut into {
                        Sink::Nothing => {}
                        // Each half of a pair is written out as it came, which is how Python
                        // writes the character the two make.
                        Sink::Dumped(out) => dump_char(out, unit),
                        Sink::Text(text) => match (high.take(), unit) {
                            (None, 0xd800..=0xdbff) => high = Some(unit),
                            (Some(h), 0xdc00..=0xdfff) => text.push(char::from_u32(0x10000 + ((h - 0xd800) << 10) + (unit - 0xdc00))?),
                            (None, _) => text.push(char::from_u32(unit)?),
                            (Some(_), _) => return None,
                        },
                    }
                }
                // A control character as itself is not JSON.
                _ => return None,
            }
        }
    }

    /// A number, written out as Python writes the number it reads.
    fn number(&mut self, out: Option<&mut Vec<u8>>) -> Option<()> {
        let start = self.at;
        let digits = |r: &mut Self| {
            let from = r.at;
            while matches!(r.peek(), Some(b'0'..=b'9')) {
                r.at += 1;
            }
            r.at - from
        };
        let negative = self.take(b'-').is_some();
        let whole = match self.peek()? {
            b'0' => {
                self.at += 1;
                1
            }
            b'1'..=b'9' => digits(self),
            _ => return None,
        };
        let mut float = false;
        if self.peek() == Some(b'.') {
            self.at += 1;
            if digits(self) == 0 {
                return None;
            }
            float = true;
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            self.at += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.at += 1;
            }
            if digits(self) == 0 {
                return None;
            }
            float = true;
        }
        let text = std::str::from_utf8(&self.text[start..self.at]).ok()?;
        if !float {
            if whole > MAX_INT_DIGITS {
                return None;
            }
            // An integer of any size is kept digit for digit; minus nothing is nothing.
            if let Some(out) = out {
                out.extend_from_slice(if negative && whole == 1 && text == "-0" { b"0" } else { text.as_bytes() });
            }
            return Some(());
        }
        let value: f64 = text.parse().ok()?;
        // Too large to hold: Python writes `Infinity`, and is left to.
        if !value.is_finite() {
            return None;
        }
        if let Some(out) = out {
            out.extend_from_slice(float_repr(value)?.as_bytes());
        }
        Some(())
    }

    /// Any value. With `out`, it is written there as `json.dumps` writes it.
    fn value(&mut self, mut out: Option<&mut Vec<u8>>, depth: usize) -> Option<()> {
        if depth > MAX_DEPTH {
            return None;
        }
        match self.peek()? {
            b'"' => match out {
                Some(out) => {
                    out.push(b'"');
                    self.string(Sink::Dumped(out))?;
                    out.push(b'"');
                    Some(())
                }
                None => self.string(Sink::Nothing),
            },
            b'{' => {
                self.at += 1;
                self.blank();
                if let Some(out) = out.as_deref_mut() {
                    out.push(b'{');
                }
                let mut names: Vec<Vec<u8>> = Vec::new();
                if self.take(b'}').is_none() {
                    loop {
                        let mut name = Vec::new();
                        self.string(Sink::Dumped(&mut name))?;
                        // Named twice, the member keeps its first place and its last value in
                        // Python: its to write.
                        if names.contains(&name) {
                            return None;
                        }
                        self.blank();
                        self.take(b':')?;
                        self.blank();
                        if let Some(out) = out.as_deref_mut() {
                            if !names.is_empty() {
                                out.extend_from_slice(b", ");
                            }
                            out.push(b'"');
                            out.extend_from_slice(&name);
                            out.extend_from_slice(b"\": ");
                        }
                        names.push(name);
                        self.value(out.as_deref_mut(), depth + 1)?;
                        self.blank();
                        if self.take(b',').is_none() {
                            self.take(b'}')?;
                            break;
                        }
                        self.blank();
                    }
                }
                if let Some(out) = out {
                    out.push(b'}');
                }
                Some(())
            }
            b'[' => {
                self.at += 1;
                self.blank();
                if let Some(out) = out.as_deref_mut() {
                    out.push(b'[');
                }
                if self.take(b']').is_none() {
                    let mut first = true;
                    loop {
                        if let Some(out) = out.as_deref_mut().filter(|_| !first) {
                            out.extend_from_slice(b", ");
                        }
                        first = false;
                        self.value(out.as_deref_mut(), depth + 1)?;
                        self.blank();
                        if self.take(b',').is_none() {
                            self.take(b']')?;
                            break;
                        }
                        self.blank();
                    }
                }
                if let Some(out) = out {
                    out.push(b']');
                }
                Some(())
            }
            b't' | b'f' | b'n' => {
                let word = ["true", "false", "null"].into_iter().find(|w| self.word(w).is_some())?;
                if let Some(out) = out {
                    out.extend_from_slice(word.as_bytes());
                }
                Some(())
            }
            _ => self.number(out),
        }
    }

    /// Any value, held. A member named twice keeps its first place and its last value, as in
    /// a Python dictionary.
    fn tree(&mut self, depth: usize) -> Option<Value> {
        if depth > MAX_DEPTH {
            return None;
        }
        match self.peek()? {
            b'{' => {
                self.at += 1;
                self.blank();
                let mut members: Vec<(Vec<u8>, Value)> = Vec::new();
                if self.take(b'}').is_none() {
                    loop {
                        let mut name = Vec::new();
                        self.string(Sink::Dumped(&mut name))?;
                        self.blank();
                        self.take(b':')?;
                        self.blank();
                        let value = self.tree(depth + 1)?;
                        match members.iter_mut().find(|(n, _)| *n == name) {
                            Some(member) => member.1 = value,
                            None => members.push((name, value)),
                        }
                        self.blank();
                        if self.take(b',').is_none() {
                            self.take(b'}')?;
                            break;
                        }
                        self.blank();
                    }
                }
                Some(Value::Object(members))
            }
            b'[' => {
                self.at += 1;
                self.blank();
                let mut items = Vec::new();
                if self.take(b']').is_none() {
                    loop {
                        items.push(self.tree(depth + 1)?);
                        self.blank();
                        if self.take(b',').is_none() {
                            self.take(b']')?;
                            break;
                        }
                        self.blank();
                    }
                }
                Some(Value::List(items))
            }
            _ => {
                let mut out = Vec::new();
                self.value(Some(&mut out), depth)?;
                Some(Value::Atom(out))
            }
        }
    }
}

/// One character of a string as `json.dumps` writes it: ASCII that prints as itself, the rest escaped.
fn dump_char(out: &mut Vec<u8>, c: u32) {
    match c {
        0x22 => out.extend_from_slice(b"\\\""),
        0x5c => out.extend_from_slice(b"\\\\"),
        0x20..=0x7e => out.push(c as u8),
        8 => out.extend_from_slice(b"\\b"),
        9 => out.extend_from_slice(b"\\t"),
        10 => out.extend_from_slice(b"\\n"),
        12 => out.extend_from_slice(b"\\f"),
        13 => out.extend_from_slice(b"\\r"),
        0x10000.. => {
            let v = c - 0x10000;
            out.extend_from_slice(format!("\\u{:04x}\\u{:04x}", 0xd800 + (v >> 10), 0xdc00 + (v & 0x3ff)).as_bytes());
        }
        _ => out.extend_from_slice(format!("\\u{c:04x}").as_bytes()),
    }
}

/// A finite number as Python's `repr` writes it: the shortest digits that read back as the same
/// number, in plain notation between 1e-4 and 1e16 and with an exponent outside. `None` for the
/// few numbers whose digits this side cannot be sure Python would choose.
pub fn float_repr(value: f64) -> Option<String> {
    // Rust's shortest form, `d[.ddd]e<exponent>`. Python chooses, of the shortest forms that read
    // back as the number, the one nearest to it, and of two equally near the even one. Rust
    // chooses otherwise for a number exactly halfway between two forms. So Rust's digits are
    // taken only when they are also the number rounded to that many digits, and one digit fewer
    // would not have done.
    let shortest = format!("{value:e}");
    let (mantissa, exponent) = shortest.split_once('e')?;
    let count = mantissa.bytes().filter(u8::is_ascii_digit).count();
    if format!("{value:.*e}", count - 1) != shortest || (count > 1 && format!("{value:.*e}", count - 2).parse() == Ok(value)) {
        return None;
    }
    let exponent: i32 = exponent.parse().ok()?;
    let (sign, mantissa) = match mantissa.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", mantissa),
    };
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    if !(-4..16).contains(&exponent) {
        let (first, rest) = digits.split_at(1);
        let point = if rest.is_empty() { "" } else { "." };
        let mark = if exponent < 0 { '-' } else { '+' };
        return Some(format!("{sign}{first}{point}{rest}e{mark}{:02}", exponent.abs()));
    }
    if exponent < 0 {
        return Some(format!("{sign}0.{}{digits}", "0".repeat((-exponent - 1) as usize)));
    }
    let whole = exponent as usize + 1;
    if digits.len() <= whole {
        return Some(format!("{sign}{digits}{}.0", "0".repeat(whole - digits.len())));
    }
    Some(format!("{sign}{}.{}", &digits[..whole], &digits[whole..]))
}

/// A string as the Python gateway's JSON answers write it: quoted, with only what must be escaped.
pub fn quoted(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// A JSON value as Python holds it after `json.loads`, kept in the spelling `json.dumps` gives it.
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    /// `null`, `true`, `false`, a number or a string, as `json.dumps` writes it.
    Atom(Vec<u8>),
    List(Vec<Value>),
    /// Members in order, each under its name as `json.dumps` writes it (without the quotes).
    Object(Vec<(Vec<u8>, Value)>),
}

/// A dotted path into an object, as a transformation rule gives one: the names to follow.
pub type Path = Vec<Vec<u8>>;

/// A rule's dotted path, split as the Python gateway splits it.
pub fn path(dotted: &str) -> Path {
    dotted
        .split('.')
        .map(|part| {
            let mut name = Vec::new();
            part.chars().for_each(|c| dump_char(&mut name, c as u32));
            name
        })
        .collect()
}

/// The value `text` is to Python, when this side is certain of how Python would write it again.
/// `None` both for text that is not JSON and for JSON this side is not certain about (deeper than
/// 64 levels, `NaN`, a number too large to hold, a byte-order mark).
pub fn parse(text: &[u8]) -> Option<Value> {
    std::str::from_utf8(text).ok()?;
    let mut r = Reader { text, at: 0 };
    r.blank();
    let value = r.tree(0)?;
    r.blank();
    (r.at == text.len()).then_some(value)
}

impl Value {
    /// As `json.dumps` writes it.
    pub fn dump(&self, out: &mut Vec<u8>) {
        match self {
            Value::Atom(text) => out.extend_from_slice(text),
            Value::List(items) => {
                out.push(b'[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.extend_from_slice(b", ");
                    }
                    item.dump(out);
                }
                out.push(b']');
            }
            Value::Object(members) => {
                out.push(b'{');
                for (i, (name, value)) in members.iter().enumerate() {
                    if i > 0 {
                        out.extend_from_slice(b", ");
                    }
                    out.push(b'"');
                    out.extend_from_slice(name);
                    out.extend_from_slice(b"\": ");
                    value.dump(out);
                }
                out.push(b'}');
            }
        }
    }

    fn members(&self) -> Option<&Vec<(Vec<u8>, Value)>> {
        match self {
            Value::Object(members) => Some(members),
            _ => None,
        }
    }

    /// The value at a path (`_get_nested`): nothing where the path leads nowhere, and nothing
    /// for a `null` found there, which Python does not tell apart.
    pub fn get(&self, path: &[Vec<u8>]) -> Option<&Value> {
        let mut at = self;
        for name in path {
            at = &at.members()?.iter().find(|(n, _)| n == name)?.1;
        }
        (*at != Value::Atom(b"null".to_vec())).then_some(at)
    }

    /// Take away what is at a path (`_remove_nested`); nothing happens where it leads nowhere.
    pub fn remove(&mut self, path: &[Vec<u8>]) {
        let Some((last, way)) = path.split_last() else { return };
        let mut at = self;
        for name in way {
            let Value::Object(members) = at else { return };
            let Some(member) = members.iter_mut().find(|(n, _)| n == name) else { return };
            at = &mut member.1;
        }
        if let Value::Object(members) = at {
            members.retain(|(n, _)| n != last);
        }
    }

    /// Put a value at a path (`_set_nested`). Whatever stands in the way that is not an object
    /// is replaced by one. A member that is there keeps its place; a new one goes last.
    pub fn set(&mut self, path: &[Vec<u8>], value: Value) {
        let Some((last, way)) = path.split_last() else { return };
        let mut at = self;
        for name in way {
            let Value::Object(members) = at else { return };
            let i = match members.iter().position(|(n, _)| n == name) {
                Some(i) => i,
                None => {
                    members.push((name.clone(), Value::Object(Vec::new())));
                    members.len() - 1
                }
            };
            if !matches!(members[i].1, Value::Object(_)) {
                members[i].1 = Value::Object(Vec::new());
            }
            at = &mut members[i].1;
        }
        let Value::Object(members) = at else { return };
        match members.iter_mut().find(|(n, _)| n == last) {
            Some(member) => member.1 = value,
            None => members.push((last.clone(), value)),
        }
    }
}

/// What the Python gateway would publish for this body, when that is certain and it would answer
/// 200. `None` for a body it would refuse and for one this side is not sure of.
pub fn publish(body: &[u8]) -> Option<Publish> {
    // UTF-8 without a byte-order mark; Python also reads UTF-16 and UTF-32.
    std::str::from_utf8(body).ok()?;
    let mut r = Reader { text: body, at: 0 };
    r.blank();
    r.take(b'{')?;
    r.blank();
    let (mut topic, mut qos, mut payload) = (None, None, None);
    let mut names: Vec<String> = Vec::new();
    if r.take(b'}').is_none() {
        loop {
            let mut name = String::new();
            r.string(Sink::Text(&mut name))?;
            if names.contains(&name) {
                return None;
            }
            r.blank();
            r.take(b':')?;
            r.blank();
            match name.as_str() {
                "topic" => {
                    let mut text = String::new();
                    r.string(Sink::Text(&mut text))?;
                    topic = Some(text);
                }
                "qos" => {
                    let level = r.peek()?;
                    r.at += 1;
                    qos = Some(match level {
                        b'0'..=b'2' => level - b'0',
                        _ => return None,
                    });
                }
                "payload" => {
                    let mut out = Vec::new();
                    r.value(Some(&mut out), 1)?;
                    payload = Some(out);
                }
                _ => r.value(None, 1)?,
            }
            names.push(name);
            r.blank();
            if r.take(b',').is_none() {
                r.take(b'}')?;
                break;
            }
            r.blank();
        }
    }
    r.blank();
    if r.at != body.len() {
        return None;
    }
    // Without a topic the answer is a 400; with wildcards or too long, a 500.
    let topic = topic.filter(|t| !t.is_empty() && t.len() <= MAX_TOPIC && !t.contains(['+', '#']))?;
    Some(Publish { topic, qos: qos.unwrap_or(1), payload: payload.unwrap_or_else(|| b"{}".to_vec()) })
}

/// `dataplane --judge-publishes`: for each body on stdin (one per line, in hex), what would be
/// published, as a line of JSON, or `ask`. The conformance suite compares the lines with what the
/// Python gateway's own code makes of the same bodies.
pub fn judge_stdin() -> Result<(), String> {
    use std::io::{BufRead, Write};
    let hex = |bytes: &[u8]| bytes.iter().map(|b| format!("{b:02x}")).collect::<String>();
    let out = std::io::stdout();
    let mut out = out.lock();
    for line in std::io::stdin().lock().lines() {
        let line = line.map_err(|e| e.to_string())?;
        let line = line.trim();
        if line.len() % 2 != 0 {
            return Err("a body has an odd number of hex digits".into());
        }
        let body: Vec<u8> = (0..line.len() / 2)
            .map(|i| line.get(2 * i..2 * i + 2).and_then(|pair| u8::from_str_radix(pair, 16).ok()))
            .collect::<Option<_>>()
            .ok_or("a body is not hex")?;
        let said = match publish(&body) {
            Some(p) => format!("{{\"topic\":\"{}\",\"qos\":{},\"payload\":\"{}\"}}", hex(p.topic.as_bytes()), p.qos, hex(&p.payload)),
            None => "ask".to_owned(),
        };
        writeln!(out, "{said}").map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn published(body: &str) -> Option<(String, u8, String)> {
        publish(body.as_bytes()).map(|p| (p.topic, p.qos, String::from_utf8(p.payload).unwrap()))
    }

    #[test]
    fn a_payload_is_written_as_python_writes_it() {
        let said = published(r#" {"topic":"a/b","payload":{"a":1,"b":[1,2.5,null,true],"c":"é☃😀\"\\\n\u007f/"} } "#).unwrap();
        assert_eq!(said.0, "a/b");
        assert_eq!(said.1, 1);
        assert_eq!(said.2, r#"{"a": 1, "b": [1, 2.5, null, true], "c": "\u00e9\u2603\ud83d\ude00\"\\\n\u007f/"}"#);
        assert_eq!(published(r#"{"topic":"t"}"#).unwrap().2, "{}");
        assert_eq!(published(r#"{"topic":"t","payload":null,"qos":2}"#).unwrap(), ("t".into(), 2, "null".into()));
        assert_eq!(published(r#"{"topic":"t","payload":[[],{},"\ud800","\ud83d\ude00"],"qos":0}"#).unwrap().2, r#"[[], {}, "\ud800", "\ud83d\ude00"]"#);
    }

    #[test]
    fn numbers_are_written_as_python_prints_them() {
        for (read, written) in [
            ("1e22", "1e+22"), ("1E16", "1e+16"), ("1e15", "1000000000000000.0"), ("0.1", "0.1"), ("-0.0", "-0.0"), ("1e-7", "1e-07"),
            ("0.0001", "0.0001"), ("0.00001", "1e-05"), ("1.0E+2", "100.0"), ("0.30000000000000004", "0.30000000000000004"),
            ("5e-324", "5e-324"), ("123456789012345678.0", "1.2345678901234568e+17"), ("1.5", "1.5"), ("-12.25e1", "-122.5"),
            ("123456789012345678901234567890", "123456789012345678901234567890"), ("-0", "0"), ("-7", "-7"), ("0", "0"),
        ] {
            assert_eq!(published(&format!(r#"{{"topic":"t","payload":{read}}}"#)).map(|p| p.2).as_deref(), Some(written), "{read}");
        }
    }

    #[test]
    fn what_python_would_refuse_or_write_its_own_way_is_left_to_it() {
        for body in [
            "", "nope", "[1]", "\"x\"", "{}", r#"{"topic":""}"#, r#"{"topic":null}"#, r#"{"topic":5}"#, r#"{"topic":"a/+/b"}"#, r#"{"topic":"a/#"}"#,
            r#"{"topic":"a\ud800"}"#, r#"{"topic":"t","qos":3}"#, r#"{"topic":"t","qos":1.0}"#, r#"{"topic":"t","qos":"1"}"#, r#"{"topic":"t","qos":true}"#,
            r#"{"topic":"t","qos":null}"#, r#"{"topic":"t","qos":10}"#, r#"{"topic":"t","qos":-1}"#, r#"{"topic":"t","payload":NaN}"#,
            r#"{"topic":"t","payload":1e400}"#, r#"{"topic":"t","payload":600654557994792.25}"#, r#"{"topic":"t","payload":1234567890123456.25}"#, r#"{"topic":"t","payload":{"a":1,"a":2}}"#, r#"{"topic":"x","topic":"t"}"#, r#"{"topic":"t","payload":01}"#,
            r#"{"topic":"t","payload":1.}"#, r#"{"topic":"t",}"#, r#"{"topic":"t"} x"#, "{\"topic\":\"t\",\"payload\":\"a\u{1}b\"}", r#"{"topic":"t","payload":"\x"}"#,
            "\u{feff}{\"topic\":\"t\"}", r#"{"topic":"t","other":{"a":1,"a":2}}"#,
        ] {
            assert_eq!(publish(body.as_bytes()), None, "{body}");
        }
        assert_eq!(publish(&[b'{', 0xff, b'}']), None);
        assert_eq!(publish(format!(r#"{{"topic":"{}"}}"#, "x".repeat(65_536)).as_bytes()), None);
        assert!(publish(format!(r#"{{"topic":"{}"}}"#, "x".repeat(65_535)).as_bytes()).is_some());
        let deep = format!(r#"{{"topic":"t","payload":{}{}}}"#, "[".repeat(80), "]".repeat(80));
        assert_eq!(publish(deep.as_bytes()), None);
    }

    #[test]
    fn a_topic_is_echoed_as_the_python_gateway_echoes_it() {
        assert_eq!(quoted("a/b"), "\"a/b\"");
        assert_eq!(quoted("é/☃ \"q\" \\ \n\u{0}\u{7f}"), "\"é/☃ \\\"q\\\" \\\\ \\n\\u0000\u{7f}\"");
        assert_eq!(published(r#"{"topic":"\u00e9\/\ud83d\ude00","extra":[1,{"k":null}]}"#).unwrap().0, "é/😀");
    }
}
