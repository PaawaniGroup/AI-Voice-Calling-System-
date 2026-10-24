import socket

def test_headers(headers_to_send, label):
    s = socket.socket()
    s.connect(("127.0.0.1", 8800))
    raw = "GET /v1/providers/plivo/stream HTTP/1.1\r\n" + "\r\n".join(headers_to_send) + "\r\n\r\n"
    s.sendall(raw.encode())
    resp = s.recv(4096).decode("latin1", errors="replace")
    status_line = resp.split("\r\n")[0] if resp else "NO RESPONSE"
    print(f"[{label}] -> {status_line}")

def main():
    base = [
        "Host: 127.0.0.1:8800",
        "Connection: Upgrade",
        "Upgrade: websocket",
        "Sec-Websocket-Version: 13",
        "Sec-Websocket-Key: dGhlIHNhbXBsZSBub25jZQ=="
    ]
    test_headers(base, "Minimal localhost")
    test_headers(base + ["Origin: http://overlabor-arrival-specimen.ngrok-free.dev"], "With ngrok Origin")
    test_headers(base + ["Origin: http://127.0.0.1:8800"], "With local Origin")
    test_headers(base + ["Host: overlabor-arrival-specimen.ngrok-free.dev"], "With ngrok Host")
    test_headers(base + ["Sec-Websocket-Protocol: audio.drachtio.org"], "With Drachtio Protocol")

if __name__ == "__main__":
    main()
