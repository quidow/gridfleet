//! Black-box test: launches the compiled binary against two stub upstreams
//! and asserts the routing/classification/activity contract the agent
//! depends on. No hub, no devices, CI-friendly.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::{Child, Command};
use std::thread;
use std::time::{Duration, Instant};

/// Minimal HTTP/1.1 stub: replies with a JSON body identifying itself and
/// echoing the request path+method; answers WS upgrades with a one-frame echo.
fn spawn_stub(name: &'static str) -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(stream) = stream else { continue };
            thread::spawn(move || handle_conn(name, stream));
        }
    });
    port
}

fn handle_conn(name: &str, mut stream: TcpStream) {
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    loop {
        let mut request_line = String::new();
        if reader.read_line(&mut request_line).unwrap_or(0) == 0 {
            return;
        }
        let mut parts = request_line.split_whitespace();
        let method = parts.next().unwrap_or("").to_string();
        let path = parts.next().unwrap_or("").to_string();
        let mut headers = Vec::new();
        let mut content_length = 0usize;
        let mut ws_key = None;
        loop {
            let mut line = String::new();
            if reader.read_line(&mut line).unwrap_or(0) == 0 {
                return;
            }
            let trimmed = line.trim_end().to_string();
            if trimmed.is_empty() {
                break;
            }
            let lower = trimmed.to_lowercase();
            if let Some(v) = lower.strip_prefix("content-length:") {
                content_length = v.trim().parse().unwrap_or(0);
            }
            if lower.starts_with("sec-websocket-key:") {
                ws_key = Some(trimmed["sec-websocket-key:".len()..].trim().to_string());
            }
            headers.push(trimmed);
        }
        if content_length > 0 {
            let mut body = vec![0u8; content_length];
            reader.read_exact(&mut body).unwrap();
        }
        if let Some(key) = ws_key {
            ws_echo(name, &key, &mut reader, &mut stream);
            return;
        }
        let body = if path.ends_with("/big") {
            // ~2 MB body to prove streaming survives large payloads.
            format!(
                "{{\"upstream\":\"{name}\",\"blob\":\"{}\"}}",
                "x".repeat(2_000_000)
            )
        } else {
            format!("{{\"upstream\":\"{name}\",\"method\":\"{method}\",\"path\":\"{path}\"}}")
        };
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
            body.len(),
            body
        );
        stream.write_all(response.as_bytes()).unwrap();
    }
}

fn ws_echo(name: &str, key: &str, reader: &mut BufReader<TcpStream>, stream: &mut TcpStream) {
    // RFC 6455 accept: base64(sha1(key + GUID)). Tiny local impls keep the
    // test dependency-free.
    let accept = base64(&sha1(
        format!("{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11").as_bytes(),
    ));
    let response = format!(
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
    );
    stream.write_all(response.as_bytes()).unwrap();
    // Read one masked client frame (small payload assumed), echo it prefixed.
    let mut hdr = [0u8; 2];
    reader.read_exact(&mut hdr).unwrap();
    let len = (hdr[1] & 0x7F) as usize;
    let mut mask = [0u8; 4];
    reader.read_exact(&mut mask).unwrap();
    let mut payload = vec![0u8; len];
    reader.read_exact(&mut payload).unwrap();
    for (i, byte) in payload.iter_mut().enumerate() {
        *byte ^= mask[i % 4];
    }
    let reply = format!("{name}:{}", String::from_utf8_lossy(&payload));
    let mut frame = vec![0x81u8, reply.len() as u8];
    frame.extend_from_slice(reply.as_bytes());
    stream.write_all(&frame).unwrap();
}

fn sha1(data: &[u8]) -> [u8; 20] {
    // Minimal SHA-1 (test-only; not security-sensitive here).
    let mut h: [u32; 5] = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0];
    let ml = (data.len() as u64) * 8;
    let mut msg = data.to_vec();
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&ml.to_be_bytes());
    for chunk in msg.chunks(64) {
        let mut w = [0u32; 80];
        for (i, word) in chunk.chunks(4).enumerate() {
            w[i] = u32::from_be_bytes(word.try_into().unwrap());
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let (mut a, mut b, mut c, mut d, mut e) = (h[0], h[1], h[2], h[3], h[4]);
        for (i, wi) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | ((!b) & d), 0x5A827999u32),
                20..=39 => (b ^ c ^ d, 0x6ED9EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1BBCDC),
                _ => (b ^ c ^ d, 0xCA62C1D6),
            };
            let temp = a
                .rotate_left(5)
                .wrapping_add(f)
                .wrapping_add(e)
                .wrapping_add(k)
                .wrapping_add(*wi);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = temp;
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
    }
    let mut out = [0u8; 20];
    for (i, word) in h.iter().enumerate() {
        out[i * 4..i * 4 + 4].copy_from_slice(&word.to_be_bytes());
    }
    out
}

fn base64(data: &[u8]) -> String {
    const TABLE: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in data.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = u32::from_be_bytes([0, b[0], b[1], b[2]]);
        out.push(TABLE[(n >> 18) as usize & 63] as char);
        out.push(TABLE[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            TABLE[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            TABLE[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

struct Proxy {
    child: Child,
    port: u16,
}

impl Drop for Proxy {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn spawn_proxy(appium_port: u16, control_port: u16) -> Proxy {
    let listen_port = TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port();
    let child = Command::new(env!("CARGO_BIN_EXE_gridfleet-relay-proxy"))
        .args([
            "--listen",
            &format!("127.0.0.1:{listen_port}"),
            "--appium",
            &format!("http://127.0.0.1:{appium_port}"),
            "--control",
            &format!("http://127.0.0.1:{control_port}"),
            "--proxy-timeout",
            "5",
        ])
        .spawn()
        .unwrap();
    let proxy = Proxy {
        child,
        port: listen_port,
    };
    // Wait for readiness via healthz.
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Ok(resp) = ureq::get(&format!(
            "http://127.0.0.1:{listen_port}/__gridfleet/healthz"
        ))
        .call()
        {
            if resp.status() == 200 {
                return proxy;
            }
        }
        assert!(Instant::now() < deadline, "proxy did not become healthy");
        thread::sleep(Duration::from_millis(50));
    }
}

#[test]
fn routes_activity_websocket_and_errors() {
    let appium_port = spawn_stub("appium");
    let control_port = spawn_stub("control");
    let proxy = spawn_proxy(appium_port, control_port);
    let base = format!("http://127.0.0.1:{}", proxy.port);

    // 1. Fast lane: session command -> appium.
    let body = ureq::get(&format!("{base}/session/abc123/element"))
        .call()
        .unwrap()
        .into_string()
        .unwrap();
    assert!(body.contains("\"upstream\":\"appium\""), "got: {body}");

    // 2. Control: /status, POST /session -> control.
    let body = ureq::get(&format!("{base}/status"))
        .call()
        .unwrap()
        .into_string()
        .unwrap();
    assert!(body.contains("\"upstream\":\"control\""), "got: {body}");
    let body = ureq::post(&format!("{base}/session"))
        .send_string("{}")
        .unwrap()
        .into_string()
        .unwrap();
    assert!(body.contains("\"upstream\":\"control\""), "got: {body}");

    // 3. Activity endpoint lists the touched session with a token.
    let body = ureq::get(&format!("{base}/__gridfleet/activity"))
        .call()
        .unwrap()
        .into_string()
        .unwrap();
    assert!(body.contains("abc123"), "activity missing session: {body}");
    assert!(
        body.contains("start_token"),
        "activity missing token: {body}"
    );

    // 4. DELETE /session/{id} routes to control AND evicts activity.
    let body = ureq::delete(&format!("{base}/session/abc123"))
        .call()
        .unwrap()
        .into_string()
        .unwrap();
    assert!(body.contains("\"upstream\":\"control\""), "got: {body}");
    let body = ureq::get(&format!("{base}/__gridfleet/activity"))
        .call()
        .unwrap()
        .into_string()
        .unwrap();
    assert!(!body.contains("abc123"), "activity not evicted: {body}");

    // 5. Large body streams through intact (~2 MB).
    let body = ureq::get(&format!("{base}/session/abc123/big"))
        .call()
        .unwrap()
        .into_string()
        .unwrap();
    assert!(
        body.len() > 2_000_000,
        "large body truncated: {} bytes",
        body.len()
    );

    // 6. WebSocket upgrade splices to the fast-lane upstream and echoes.
    let reply = ws_roundtrip(proxy.port, "/session/abc123/se/cdp", "hello-ws");
    assert_eq!(reply, "appium:hello-ws");

    // 7. Dead upstream -> 5xx error (502-family), not a hang or empty reply.
    let dead_proxy = spawn_proxy(get_free_port(), control_port);
    let result = ureq::get(&format!(
        "http://127.0.0.1:{}/session/abc123/element",
        dead_proxy.port
    ))
    .call();
    let status = match result {
        Err(ureq::Error::Status(code, _)) => code,
        other => panic!("expected 5xx error, got {other:?}"),
    };
    assert!((500..600).contains(&status), "expected 5xx, got {status}");
}

fn get_free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

fn ws_roundtrip(proxy_port: u16, path: &str, message: &str) -> String {
    let mut stream = TcpStream::connect(("127.0.0.1", proxy_port)).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{proxy_port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
    );
    stream.write_all(request.as_bytes()).unwrap();
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    // Consume the 101 response headers.
    loop {
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        if line == "\r\n" || line == "\n" {
            break;
        }
        if line.starts_with("HTTP/") {
            assert!(line.contains("101"), "expected 101, got: {line}");
        }
    }
    // Send one masked text frame.
    let mask = [0x11u8, 0x22, 0x33, 0x44];
    let mut frame = vec![0x81u8, 0x80 | message.len() as u8];
    frame.extend_from_slice(&mask);
    frame.extend(message.bytes().enumerate().map(|(i, b)| b ^ mask[i % 4]));
    stream.write_all(&frame).unwrap();
    // Read the unmasked server echo frame.
    let mut hdr = [0u8; 2];
    reader.read_exact(&mut hdr).unwrap();
    let len = (hdr[1] & 0x7F) as usize;
    let mut payload = vec![0u8; len];
    reader.read_exact(&mut payload).unwrap();
    String::from_utf8(payload).unwrap()
}
