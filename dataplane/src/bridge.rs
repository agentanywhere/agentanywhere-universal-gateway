//! The JSON-to-gRPC bridge under `/grpc`, as the Python gateway serves it.
//!
//! A JSON body names a service and a method and carries `data`. The Python gateway's bridge
//! knows one service, the sample `greeter.Greeter`: `SayHello` is called and its answer returned
//! as JSON, `SayManyHellos` (with `"stream": true`) is called and each answer written as a line
//! of JSON while the call goes on. Every other pair is answered with `"status": "unsupported"`
//! and the data sent back. Whether `SayHello` is called at all depends on two things about the
//! Python gateway's installation (whether the reflection package is there, and whether the
//! sample's generated code is), which the snapshot says.

use std::collections::VecDeque;
use std::convert::Infallible;
use std::future::Future;
use std::pin::Pin;
use std::task::{Context, Poll};
use std::time::Duration;

use bytes::{Buf, Bytes, BytesMut};
use http_body_util::BodyExt;
use hyper::body::{Frame, Incoming};
use hyper::header::{HeaderMap, HeaderValue, CONTENT_TYPE};
use hyper::{Method, Request, Response, StatusCode, Uri};
use hyper_util::client::legacy::ResponseFuture;
use serde::Deserialize;

use crate::pyjson::{self, Name};

/// What the snapshot says of the Python gateway's bridge.
#[derive(Deserialize, Clone, Debug, PartialEq)]
pub struct Bridge {
    /// The gRPC server, as an `http://` address spoken to in HTTP/2.
    pub target: String,
    /// Whether the reflection package can be loaded there; without it no unary call is made.
    pub reflection: bool,
    /// Whether the sample service's generated code can be loaded there.
    pub stubs: bool,
    /// The address, read.
    #[serde(skip)]
    pub uri: Uri,
}

/// How long the Python gateway gives a unary call, and a stream, in seconds.
pub const UNARY_SECONDS: u64 = 30;
pub const STREAM_SECONDS: u64 = 60;
/// The longest answer to a unary call that is read (gRPC's own default limit).
const UNARY_MAX: usize = 4 << 20;

/// What to do for a body posted to the bridge.
#[derive(Debug, PartialEq)]
pub enum Plan {
    /// Answer with this JSON; nothing is called.
    Answer(Vec<u8>),
    /// Call `SayHello` with this name.
    Hello(String),
    /// Call `SayManyHellos` with this name and pass its answers on as they come.
    Hellos(String),
}

/// What the Python gateway would do for this body. `None` when that is not certain or would not
/// be an answer with status 200.
pub fn plan(body: &[u8], bridge: &Bridge) -> Option<Plan> {
    let asked = pyjson::bridge(body)?;
    let greeter = asked.service == "Greeter";
    let name = || match &asked.name {
        Name::Absent => Some("World".to_owned()),
        Name::Text(name) => Some(name.clone()),
        Name::Other => None,
    };
    if asked.stream && greeter && asked.method == "SayManyHellos" {
        // Without the generated code the Python gateway's stream breaks off at once: its to do.
        return if bridge.stubs { name().map(Plan::Hellos) } else { None };
    }
    let message = if !bridge.reflection {
        format!("gRPC reflection is unavailable; {}.{} could not be bridged to the backend.", asked.service, asked.method)
    } else if greeter && asked.method == "SayHello" && bridge.stubs {
        return name().map(Plan::Hello);
    } else {
        format!("No gRPC stub available to bridge {}.{}; request was not forwarded to the backend.", asked.service, asked.method)
    };
    let mut out = format!(
        "{{\"status\":\"unsupported\",\"message\":{},\"service\":{},\"method\":{},\"request_data\":",
        pyjson::quoted(&message),
        pyjson::quoted(&asked.service),
        pyjson::quoted(&asked.method)
    )
    .into_bytes();
    out.extend_from_slice(&asked.data);
    out.push(b'}');
    Some(Plan::Answer(out))
}

/// The answer to a `SayHello` that succeeded.
pub fn greeted(message: &str) -> String {
    format!("{{\"status\":\"success\",\"message\":{},\"service\":\"Greeter\",\"method\":\"SayHello\"}}", pyjson::quoted(message))
}

/// The call to make: a `HelloRequest` with this name, as one gRPC message.
pub fn call(target: &Uri, method: &str, name: &str, seconds: u64) -> Option<Request<Bytes>> {
    let mut message = Vec::with_capacity(name.len() + 8);
    // A name that is empty is left out, as every default is.
    if !name.is_empty() {
        message.push(0x0a);
        varint(&mut message, name.len() as u64);
        message.extend_from_slice(name.as_bytes());
    }
    let mut framed = Vec::with_capacity(message.len() + 5);
    framed.push(0);
    framed.extend_from_slice(&(message.len() as u32).to_be_bytes());
    framed.extend_from_slice(&message);
    let uri = Uri::builder().scheme(target.scheme()?.clone()).authority(target.authority()?.clone()).path_and_query(format!("/greeter.Greeter/{method}")).build().ok()?;
    Request::builder()
        .method(Method::POST)
        .uri(uri)
        .header(CONTENT_TYPE, "application/grpc")
        .header("te", "trailers")
        .header("grpc-timeout", format!("{seconds}S"))
        .header("grpc-accept-encoding", "identity")
        .header("user-agent", "agentanywhere-dataplane")
        .body(Bytes::from(framed))
        .ok()
}

fn varint(out: &mut Vec<u8>, mut n: u64) {
    while n >= 0x80 {
        out.push(n as u8 | 0x80);
        n >>= 7;
    }
    out.push(n as u8);
}

fn read_varint(buf: &mut &[u8]) -> Option<u64> {
    let mut n = 0u64;
    for shift in (0..64).step_by(7) {
        let (&byte, rest) = buf.split_first()?;
        *buf = rest;
        n |= u64::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            return Some(n);
        }
    }
    None
}

/// The `message` of a `HelloReply`: field 1, the last one given; every other field is passed over.
fn message(mut reply: &[u8]) -> Option<String> {
    let mut found = String::new();
    while !reply.is_empty() {
        let key = read_varint(&mut reply)?;
        let skip = match key & 7 {
            0 => {
                read_varint(&mut reply)?;
                0
            }
            1 => 8,
            5 => 4,
            2 => {
                let len = usize::try_from(read_varint(&mut reply)?).ok()?;
                if key >> 3 == 1 {
                    found = std::str::from_utf8(reply.get(..len)?).ok()?.to_owned();
                }
                len
            }
            _ => return None,
        };
        reply = reply.get(skip..)?;
    }
    Some(found)
}

/// The messages that have arrived whole, taken off the front of what was read.
fn messages(buf: &mut BytesMut) -> Option<Vec<String>> {
    let mut found = Vec::new();
    while buf.len() >= 5 {
        // A compressed message was not asked for.
        if buf[0] != 0 {
            return None;
        }
        let len = u32::from_be_bytes([buf[1], buf[2], buf[3], buf[4]]) as usize;
        if buf.len() < 5 + len {
            break;
        }
        buf.advance(5);
        found.push(message(&buf.split_to(len))?);
    }
    Some(found)
}

/// The status a call ended with, from the headers or trailers that carry it.
fn status(headers: &HeaderMap) -> Option<u8> {
    let code: u8 = headers.get("grpc-status")?.to_str().ok()?.parse().ok()?;
    Some(if code <= 16 { code } else { 2 })
}

/// The status gRPC makes of an answer that is not gRPC's.
fn of_http(status: StatusCode) -> u8 {
    match status.as_u16() {
        400 => 13,
        401 => 16,
        403 => 7,
        404 => 12,
        429 | 502 | 503 | 504 => 14,
        _ => 2,
    }
}

/// A status as the Python gateway writes it in an error.
pub fn code_name(code: u8) -> &'static str {
    const NAMES: [&str; 17] = [
        "OK", "CANCELLED", "UNKNOWN", "INVALID_ARGUMENT", "DEADLINE_EXCEEDED", "NOT_FOUND", "ALREADY_EXISTS", "PERMISSION_DENIED", "RESOURCE_EXHAUSTED",
        "FAILED_PRECONDITION", "ABORTED", "OUT_OF_RANGE", "UNIMPLEMENTED", "INTERNAL", "UNAVAILABLE", "DATA_LOSS", "UNAUTHENTICATED",
    ];
    NAMES.get(usize::from(code)).copied().unwrap_or("UNKNOWN")
}

/// Whether an answer is gRPC's at all; if not, the status that stands for it.
fn grpc_answer<B>(resp: &Response<B>) -> Result<(), u8> {
    if resp.status() != StatusCode::OK {
        return Err(of_http(resp.status()));
    }
    let grpc = resp.headers().get(CONTENT_TYPE).is_some_and(|v| v.as_bytes().starts_with(b"application/grpc"));
    if grpc { Ok(()) } else { Err(2) }
}

/// Make a unary call and read its one answer: the `message` of the reply, or the gRPC status
/// the call failed with.
pub async fn hello(sent: ResponseFuture) -> Result<String, u8> {
    let whole = async {
        let resp = sent.await.map_err(|_| 14u8)?;
        grpc_answer(&resp)?;
        // A call refused outright says so in its headers and sends nothing else.
        if let Some(code) = status(resp.headers()) {
            return Err(if code == 0 { 13 } else { code });
        }
        let read = http_body_util::Limited::new(resp.into_body(), UNARY_MAX).collect().await.map_err(|_| 14u8)?;
        match read.trailers().and_then(status) {
            Some(0) => {}
            Some(code) => return Err(code),
            None => return Err(2),
        }
        let mut buf = BytesMut::from(&read.to_bytes()[..]);
        match messages(&mut buf).as_deref() {
            Some([one]) if buf.is_empty() => Ok(one.clone()),
            _ => Err(13),
        }
    };
    tokio::time::timeout(Duration::from_secs(UNARY_SECONDS), whole).await.unwrap_or(Err(4))
}

enum State {
    Calling(ResponseFuture),
    Reading(Incoming),
    Done,
}

/// The answers of a `SayManyHellos`, each a line of JSON, as they arrive.
pub struct Lines {
    state: State,
    buf: BytesMut,
    ready: VecDeque<Bytes>,
    deadline: Pin<Box<tokio::time::Sleep>>,
}

impl Lines {
    pub fn new(sent: ResponseFuture) -> Lines {
        Lines {
            state: State::Calling(sent),
            buf: BytesMut::new(),
            ready: VecDeque::new(),
            deadline: Box::pin(tokio::time::sleep(Duration::from_secs(STREAM_SECONDS))),
        }
    }

    /// End the stream after a failure. The Python gateway's answer ends there as a finished
    /// one, after the lines that came before it (Starlette's streaming answer since 1.x).
    fn broken(&mut self) -> Poll<Option<Result<Frame<Bytes>, Infallible>>> {
        self.state = State::Done;
        Poll::Ready(None)
    }
}

/// One answer as the Python gateway writes it: `json.dumps` and a newline.
fn line(message: &str) -> Bytes {
    let mut out = b"{\"status\": \"stream_chunk\", \"message\": ".to_vec();
    out.extend_from_slice(pyjson::dumped(message).as_bytes());
    out.extend_from_slice(b"}\n");
    Bytes::from(out)
}

impl hyper::body::Body for Lines {
    type Data = Bytes;
    type Error = Infallible;

    fn poll_frame(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Result<Frame<Bytes>, Infallible>>> {
        let this = self.get_mut();
        loop {
            if let Some(line) = this.ready.pop_front() {
                return Poll::Ready(Some(Ok(Frame::data(line))));
            }
            match &mut this.state {
                State::Done => return Poll::Ready(None),
                _ if this.deadline.as_mut().poll(cx).is_ready() => return this.broken(),
                State::Calling(sent) => match std::task::ready!(Pin::new(sent).poll(cx)) {
                    Ok(resp) => match (grpc_answer(&resp), status(resp.headers())) {
                        (Ok(()), None) => this.state = State::Reading(resp.into_body()),
                        (Ok(()), Some(0)) => this.state = State::Done,
                        _ => return this.broken(),
                    },
                    Err(_) => return this.broken(),
                },
                State::Reading(body) => match std::task::ready!(Pin::new(body).poll_frame(cx)) {
                    Some(Ok(frame)) => match frame.into_data() {
                        Ok(data) => {
                            this.buf.extend_from_slice(&data);
                            match messages(&mut this.buf) {
                                Some(found) => this.ready.extend(found.iter().map(|m| line(m))),
                                None => return this.broken(),
                            }
                        }
                        Err(frame) => match frame.trailers_ref().and_then(status) {
                            Some(0) if this.buf.is_empty() => this.state = State::Done,
                            _ => return this.broken(),
                        },
                    },
                    // Ended without saying how it went.
                    _ => return this.broken(),
                },
            }
        }
    }
}

/// The content type of a stream of answers.
pub fn ndjson() -> HeaderValue {
    HeaderValue::from_static("application/x-ndjson")
}

/// `dataplane --judge-bridges`: for each body on stdin (one per line, in hex), what the bridge
/// would do with it in each of three installations of the Python gateway (no reflection package;
/// everything there; no generated code), or `ask`. The conformance suite compares the lines with
/// what the Python gateway's own code makes of the same bodies.
pub fn judge_stdin() -> Result<(), String> {
    use std::io::{BufRead, Write};
    let hex = |bytes: &[u8]| bytes.iter().map(|b| format!("{b:02x}")).collect::<String>();
    let out = std::io::stdout();
    let mut out = out.lock();
    for line in std::io::stdin().lock().lines() {
        let line = line.map_err(|e| e.to_string())?;
        let line = line.trim();
        let body: Vec<u8> = (0..line.len() / 2)
            .map(|i| line.get(2 * i..2 * i + 2).and_then(|pair| u8::from_str_radix(pair, 16).ok()))
            .collect::<Option<_>>()
            .filter(|_| line.len() % 2 == 0)
            .ok_or("a body is not hex")?;
        let said: Vec<String> = [(false, true), (true, true), (true, false)]
            .into_iter()
            .map(|(reflection, stubs)| match plan(&body, &Bridge { target: String::new(), reflection, stubs, uri: Uri::default() }) {
                Some(Plan::Answer(json)) => format!("answer:{}", hex(&json)),
                Some(Plan::Hello(name)) => format!("hello:{}", hex(name.as_bytes())),
                Some(Plan::Hellos(name)) => format!("hellos:{}", hex(name.as_bytes())),
                None => "ask".to_owned(),
            })
            .collect();
        writeln!(out, "{}", said.join(" ")).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A body of frames given beforehand, all ready at once.
    struct Frames(VecDeque<Frame<Bytes>>);

    impl hyper::body::Body for Frames {
        type Data = Bytes;
        type Error = std::convert::Infallible;

        fn poll_frame(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
            Poll::Ready(self.get_mut().0.pop_front().map(Ok))
        }
    }

    #[test]
    fn a_stream_that_fails_at_once_sends_what_came_before_and_ends() {
        use hyper_util::rt::{TokioExecutor, TokioIo};
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        rt.block_on(async {
            // A gRPC server whose stream sends one answer and its failure in the same breath.
            let backend = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let at = backend.local_addr().unwrap();
            tokio::spawn(async move {
                while let Ok((stream, _)) = backend.accept().await {
                    let svc = hyper::service::service_fn(|_req| async {
                        let reply = b"\x0a\x10Hello nobody #0 ";
                        let mut data = vec![0u8];
                        data.extend_from_slice(&u32::try_from(reply.len()).unwrap().to_be_bytes());
                        data.extend_from_slice(reply);
                        let mut trailers = HeaderMap::new();
                        trailers.insert("grpc-status", HeaderValue::from_static("14"));
                        let body = Frames(VecDeque::from([Frame::data(Bytes::from(data)), Frame::trailers(trailers)]));
                        let mut resp = Response::new(body);
                        resp.headers_mut().insert(CONTENT_TYPE, HeaderValue::from_static("application/grpc"));
                        Ok::<_, std::convert::Infallible>(resp)
                    });
                    tokio::spawn(hyper::server::conn::http2::Builder::new(TokioExecutor::new()).serve_connection(TokioIo::new(stream), svc));
                }
            });
            let client = hyper_util::client::legacy::Client::builder(TokioExecutor::new())
                .http2_only(true)
                .build_http::<http_body_util::Empty<Bytes>>();
            // The bridge in front of it, as the data plane serves it.
            let front = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let front_at = front.local_addr().unwrap();
            tokio::spawn(async move {
                let (stream, _) = front.accept().await.unwrap();
                let svc = hyper::service::service_fn(move |_req| {
                    let sent = client.request(Request::post(format!("http://{at}/greeter.Greeter/SayManyHellos")).body(http_body_util::Empty::new()).unwrap());
                    async move { Ok::<_, std::convert::Infallible>(Response::new(Lines::new(sent))) }
                });
                let _ = hyper::server::conn::http1::Builder::new().serve_connection(TokioIo::new(stream), svc).await;
            });
            let mut caller = tokio::net::TcpStream::connect(front_at).await.unwrap();
            caller.write_all(b"POST /grpc/call HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n").await.unwrap();
            let mut got = Vec::new();
            let _ = tokio::time::timeout(Duration::from_secs(10), caller.read_to_end(&mut got)).await;
            let got = String::from_utf8_lossy(&got);
            assert!(got.contains(r#"{"status": "stream_chunk", "message": "Hello nobody #0 "}"#), "{got}");
            assert!(got.ends_with("0\r\n\r\n"), "the answer was not finished, as the Python gateway finishes it: {got}");
        });
    }

    fn bridge(reflection: bool, stubs: bool) -> Bridge {
        Bridge { target: "http://127.0.0.1:50051".into(), reflection, stubs, uri: Uri::default() }
    }

    fn answer(body: &str, reflection: bool, stubs: bool) -> Option<String> {
        match plan(body.as_bytes(), &bridge(reflection, stubs))? {
            Plan::Answer(json) => Some(String::from_utf8(json).unwrap()),
            other => Some(format!("{other:?}")),
        }
    }

    #[test]
    fn a_body_is_answered_as_the_python_gateway_answers_it() {
        // Without the reflection package nothing is ever called.
        assert_eq!(
            answer("{}", false, true).unwrap(),
            r#"{"status":"unsupported","message":"gRPC reflection is unavailable; Greeter.SayHello could not be bridged to the backend.","service":"Greeter","method":"SayHello","request_data":{}}"#
        );
        assert_eq!(
            answer(r#"{"service":"S","method":"M","data":{"x": [1, 2.50, "caf\u00e9"]}}"#, true, true).unwrap(),
            "{\"status\":\"unsupported\",\"message\":\"No gRPC stub available to bridge S.M; request was not forwarded to the backend.\",\"service\":\"S\",\"method\":\"M\",\"request_data\":{\"x\":[1,2.5,\"caf\u{e9}\"]}}"
        );
        // With it, and the generated code, the one call the bridge knows is made.
        assert_eq!(answer(r#"{"data":{"name":"Ada"}}"#, true, true).unwrap(), "Hello(\"Ada\")");
        assert_eq!(answer("{}", true, true).unwrap(), "Hello(\"World\")");
        assert!(answer("{}", true, false).unwrap().contains("No gRPC stub available to bridge Greeter.SayHello"));
        // A stream needs only the generated code, and is one only when asked for by name.
        let many = r#"{"stream":true,"service":"Greeter","method":"SayManyHellos","data":{"name":"Ada"}}"#;
        assert_eq!(answer(many, false, true).unwrap(), "Hellos(\"Ada\")");
        assert_eq!(answer(many, false, false), None);
        assert!(answer(&many.replace("true", "false"), false, true).unwrap().contains("Greeter.SayManyHellos could not be bridged"));
        // What the Python gateway would make of these is its to say.
        for asked in [
            "", "[]", "{\"service\":null}", "{\"method\":5}", "{\"stream\":1}", "{\"stream\":\"yes\"}", "{\"data\":1e400}", "{\"data\":NaN}",
            "{\"data\":{\"a\":1,\"a\":2}}", "{\"service\":\"S\",\"service\":\"T\"}", "{\"data\":\"\\ud800\"}", "{} x",
        ] {
            assert_eq!(answer(asked, false, true), None, "{asked}");
        }
        // A name that is not text, where a call would be made.
        assert_eq!(answer(r#"{"data":{"name":5}}"#, true, true), None);
        assert_eq!(answer(r#"{"data":[]}"#, true, true), None);
        assert!(answer(r#"{"data":[]}"#, false, true).unwrap().ends_with("\"request_data\":[]}"));
    }

    #[test]
    fn a_call_is_one_message_and_its_answers_are_read_back() {
        let asked = call(&"http://127.0.0.1:50051".parse().unwrap(), "SayHello", "Ada", 30).unwrap();
        assert_eq!(asked.uri(), "http://127.0.0.1:50051/greeter.Greeter/SayHello");
        assert_eq!(asked.headers()["grpc-timeout"], "30S");
        assert_eq!(&asked.body()[..], b"\0\0\0\0\x05\x0a\x03Ada");
        let empty = call(&"http://h:1".parse().unwrap(), "SayHello", "", 30).unwrap();
        assert_eq!(&empty.body()[..], b"\0\0\0\0\0");

        let mut buf = BytesMut::from(&b"\0\0\0\0\x04\x0a\x02hi\0\0\0\0\0\0\0\0\0\x08\x10\x07\x0a\x04yo"[..]);
        assert_eq!(messages(&mut buf).unwrap(), ["hi", ""]);
        // The last is not whole yet.
        assert_eq!(buf.len(), 11);
        buf.extend_from_slice(b"\xc3\xa9");
        assert_eq!(messages(&mut buf).unwrap(), ["yo\u{e9}"]);
        assert_eq!(messages(&mut BytesMut::from(&b"\x01\0\0\0\0"[..])), None);
        assert_eq!(messages(&mut BytesMut::from(&b"\0\0\0\0\x03\x0a\x01\xff"[..])), None);
        assert_eq!(&line("caf\u{e9} \"x\"")[..], b"{\"status\": \"stream_chunk\", \"message\": \"caf\\u00e9 \\\"x\\\"\"}\n");
        assert_eq!(greeted("caf\u{e9}"), "{\"status\":\"success\",\"message\":\"caf\u{e9}\",\"service\":\"Greeter\",\"method\":\"SayHello\"}");
        assert_eq!((code_name(14), code_name(5), code_name(99)), ("UNAVAILABLE", "NOT_FOUND", "UNKNOWN"));
    }
}
