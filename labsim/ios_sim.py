# lab stand-in, not real IOS
"""Tiny SSH server that answers like a Cisco IOS switch, for testing the vendor backup without hardware."""
from __future__ import annotations

import argparse
import os
import socket
import threading
import time
from pathlib import Path

import paramiko

START = Path(__file__).with_name("ios_start.cfg")
_LONG = {"gi": "GigabitEthernet", "gig": "GigabitEthernet", "fa": "FastEthernet", "vl": "Vlan"}


def _long_name(text: str) -> str:
    head = text.rstrip("0123456789/. ")
    return _LONG.get(head.lower(), head) + text[len(head):]


class Config:
    """The running config as [header, [body lines]] blocks, plus the CLI mode."""

    def __init__(self, text: str):
        self.blocks: list[list] = []
        for line in text.replace("\r", "").split("\n"):
            if not line.strip() or line.strip() == "end":
                continue
            if line.startswith(" ") and self.blocks:
                self.blocks[-1][1].append(line)
            else:
                self.blocks.append([line, []])
        self.lock = threading.Lock()

    def hostname(self) -> str:
        return next((b[0].split()[1] for b in self.blocks if b[0].startswith("hostname ")), "switch")

    def render(self) -> str:
        lines = ["Building configuration...", "", "Current configuration : 1432 bytes",
                 f"! Last configuration change at {time.strftime('%H:%M:%S UTC %a %b %d %Y')}", "ntp clock-period 36028797"]
        for head, body in self.blocks:
            lines.append(head)
            lines += body
        return "\n".join(lines + ["end", ""])

    def block(self, head: str) -> list:
        for b in self.blocks:
            if b[0] == head:
                return b
        new = [head, []]
        last = max((i for i, b in enumerate(self.blocks) if b[0].startswith(head.split()[0] + " ")), default=None)
        at = len(self.blocks) if last is None else last + 2        # after the block and its "!"
        self.blocks[at:at] = [new, ["!", []]]
        return new

    def set_line(self, blk: list, key: str, value: str | None):
        blk[1][:] = [l for l in blk[1] if not l.strip().startswith(key)]
        if value is not None:
            blk[1].append(f" {key} {value}".rstrip())


class Session:
    def __init__(self, cfg: Config):
        self.cfg, self.mode, self.blk = cfg, "exec", None

    def prompt(self) -> str:
        suffix = {"exec": "", "config": "(config)", "if": "(config-if)", "vlan": "(config-vlan)"}[self.mode]
        return f"{self.cfg.hostname()}{suffix}#"

    def handle(self, line: str) -> str | None:
        """Return the output text, or None to close the session."""
        words = line.split()
        if not words:
            return ""
        cmd = words[0].lower()
        with self.cfg.lock:
            if self.mode == "exec":
                return self._exec(line, cmd, words)
            return self._config(line, cmd, words)

    def _exec(self, line, cmd, words):
        if line.lower().startswith("show run"):
            return self.cfg.render()
        if line.lower() == "terminal length 0":
            return ""
        if line.lower().startswith("conf") and len(words) > 1:
            self.mode = "config"
            return "Enter configuration commands, one per line.  End with CNTL/Z."
        if cmd in ("exit", "logout"):
            return None
        return "% Invalid input detected at '^' marker."

    def _config(self, line, cmd, words):
        if cmd == "end":
            self.mode, self.blk = "exec", None
        elif cmd == "exit":
            self.mode, self.blk = ("config", None) if self.mode != "config" else ("exec", None)
        elif cmd == "hostname" and self.mode == "config" and len(words) == 2:
            self.cfg.blocks = [[f"hostname {words[1]}", []] if b[0].startswith("hostname ") else b for b in self.cfg.blocks]
        elif cmd == "interface" and len(words) == 2:
            self.blk, self.mode = self.cfg.block(f"interface {_long_name(words[1])}"), "if"
        elif cmd == "vlan" and len(words) == 2 and words[1].isdigit():
            self.blk, self.mode = self.cfg.block(f"vlan {words[1]}"), "vlan"
        elif self.mode == "if" and cmd == "description" and len(words) > 1:
            self.cfg.set_line(self.blk, "description", line.split(None, 1)[1])
        elif self.mode == "if" and cmd == "shutdown":
            self.cfg.set_line(self.blk, "shutdown", "")
        elif self.mode == "if" and cmd == "no" and words[1:2] == ["shutdown"]:
            self.cfg.set_line(self.blk, "shutdown", None)
        elif self.mode == "if" and cmd == "no" and words[1:2] == ["description"]:
            self.cfg.set_line(self.blk, "description", None)
        elif self.mode == "vlan" and cmd == "name" and len(words) == 2:
            self.cfg.set_line(self.blk, "name", words[1])
        else:
            return "% Invalid input detected at '^' marker."
        return ""


class Server(paramiko.ServerInterface):
    def __init__(self, user: str, password: str):
        self.user, self.password = user, password
        self.shell = threading.Event()

    def check_auth_password(self, username, password):
        ok = username == self.user and password == self.password
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        self.shell.set()
        return True


def _serve_channel(chan, cfg: Config):
    sess, buf = Session(cfg), ""
    chan.send(sess.prompt())
    while True:
        data = chan.recv(1024)
        if not data:
            return
        for ch in data.decode(errors="replace"):
            if ch not in "\r\n":
                buf += ch
                continue
            if ch == "\n" and buf == "":
                continue                                    # second half of \r\n
            chan.send(f"{buf}\r\n")
            out = sess.handle(buf.strip())
            buf = ""
            if out is None:
                return
            if out:
                chan.send(out.replace("\n", "\r\n") + "\r\n")
            chan.send(sess.prompt())


def _client(sock, host_key, user, password, cfg):
    transport = paramiko.Transport(sock)
    try:
        transport.add_server_key(host_key)
        server = Server(user, password)
        transport.start_server(server=server)
        chan = transport.accept(10)
        if chan and server.shell.wait(10):
            _serve_channel(chan, cfg)
    except (paramiko.SSHException, EOFError, OSError):
        pass
    finally:
        transport.close()


class IosSim:
    def __init__(self, port: int = 0, user: str = "netops", password: str = "", start: Path = START):
        self.user, self.password = user, password
        self.cfg = Config(Path(start).read_text())
        self.host_key = paramiko.RSAKey.generate(2048)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]

    def serve_forever(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=_client, args=(conn, self.host_key, self.user, self.password, self.cfg),
                             daemon=True).start()

    def start(self) -> "IosSim":
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.sock.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="IOS-like SSH stand-in for the lab")
    ap.add_argument("--port", type=int, default=2201)
    args = ap.parse_args()
    password = os.environ.get("IOS_PASSWORD")
    if not password:
        raise SystemExit("set IOS_PASSWORD")
    sim = IosSim(args.port, "netops", password)
    print(f"ios stand-in on 127.0.0.1:{sim.port} (user netops)", flush=True)
    sim.serve_forever()
