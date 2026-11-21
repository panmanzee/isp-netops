# lab stand-in, not real FortiOS
"""Tiny HTTPS server that serves a FortiOS-style config backup, for testing without a FortiGate."""
from __future__ import annotations

import argparse
import datetime
import json
import os
import random
import re
import ssl
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

START = Path(__file__).with_name("fortios_start.conf")
_ENC = re.compile(r"\bENC \S+")


def self_signed(directory: str) -> tuple[str, str]:
    """Write a throwaway cert and key into directory."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fw-edge.lab")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30)).sign(key, hashes.SHA256()))
    cert_file, key_file = os.path.join(directory, "cert.pem"), os.path.join(directory, "key.pem")
    Path(cert_file).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    Path(key_file).write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption()))
    return cert_file, key_file


def add_policy(text: str, num: str, name: str) -> str:
    block = (f"    edit {num}\n        set name \"{name}\"\n        set srcintf \"port2\"\n        set dstintf \"port1\"\n"
             "        set action accept\n        set srcaddr \"all\"\n        set dstaddr \"all\"\n"
             "        set schedule \"always\"\n        set service \"ALL\"\n    next\n")
    lines = text.splitlines(keepends=True)
    start = lines.index("config firewall policy\n")
    end = lines.index("end\n", start)
    return "".join(lines[:end]) + block + "".join(lines[end:])


def del_policy(text: str, num: str) -> str:
    return re.sub(rf"    edit {num}\n(?:        .*\n)*?    next\n", "", text, count=1)


class State:
    def __init__(self, text: str, token: str):
        self.text, self.token, self.lock = text, token, threading.Lock()

    def dump(self) -> str:
        with self.lock:
            text = _ENC.sub(lambda _: f"ENC SH2{random.randbytes(24).hex()}", self.text)    # real boxes re-salt secrets
        head = ("#config-version=FGT60F-7.2.5-FW-build1517-230307:opmode=0:vdom=0:user=admin\n"
                f"#conf_file_ver={random.randint(10**15, 10**16)}\n#buildno=1517\n#global_vdom=1:vd_name=root\n")
        return head + text

    def apply(self, cmd: str) -> bool:
        words = cmd.split()
        with self.lock:
            if len(words) == 3 and words[0] == "add-policy":
                self.text = add_policy(self.text, words[1], words[2])
            elif len(words) == 2 and words[0] == "del-policy":
                self.text = del_policy(self.text, words[1])
            else:
                return False
        return True


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code: int, body: str, kind: str = "text/plain"):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self) -> bool:
            if self.headers.get("Authorization") == f"Bearer {state.token}":
                return True
            self._send(401, json.dumps({"http_status": 401, "status": "error"}), "application/json")
            return False

        def do_GET(self):
            url = urlparse(self.path)
            if not self._authed():
                return
            if url.path == "/api/v2/monitor/system/config/backup" and "scope=global" in url.query:
                self._send(200, state.dump())
            else:
                self._send(404, json.dumps({"http_status": 404, "status": "error"}), "application/json")

        def do_POST(self):                                  # test only: change the config by hand
            if not self._authed():
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            ok = self.path == "/sim/cli" and state.apply(json.loads(body or b"{}").get("cmd", ""))
            self._send(200 if ok else 400, json.dumps({"status": "success" if ok else "error"}), "application/json")

    return Handler


class FortiosSim:
    def __init__(self, token: str, port: int = 0, tls: bool = True, start: Path = START):
        self.state = State(Path(start).read_text(), token)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(self.state))
        self.tmp = tempfile.TemporaryDirectory()
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*self_signed(self.tmp.name))
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]

    def start(self) -> "FortiosSim":
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="FortiOS-like REST stand-in for the lab")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--http", action="store_true", help="plain http instead of a self-signed https")
    args = ap.parse_args()
    token = os.environ.get("FORTIOS_TOKEN")
    if not token:
        raise SystemExit("set FORTIOS_TOKEN")
    sim = FortiosSim(token, args.port, tls=not args.http)
    print(f"fortios stand-in on 127.0.0.1:{sim.port} ({'http' if args.http else 'https'})", flush=True)
    sim.httpd.serve_forever()
