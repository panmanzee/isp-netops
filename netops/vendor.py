"""Vendor targets: Cisco IOS over SSH, FortiGate FortiOS over its REST API."""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import paramiko
import requests
import urllib3
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from .device import Device, DeviceError

_PROMPT = re.compile(r"^\S+#\s?$")


class TargetsError(Exception):
    pass


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Literal["cisco_ios", "fortios"]
    host: str
    port: int
    intended: str
    user: str | None = None
    password_env: str | None = None
    token_env: str | None = None
    verify_tls: bool = True

    @model_validator(mode="after")
    def _needs_credentials(self):
        if self.platform == "cisco_ios" and not (self.user and self.password_env):
            raise ValueError("cisco_ios needs user and password_env")
        if self.platform == "fortios" and not self.token_env:
            raise ValueError("fortios needs token_env")
        return self


@dataclass
class IosDevice(Device):
    port: int = 22
    password: str = ""

    def _read_until_prompt(self, chan) -> str:
        buf, end = "", time.monotonic() + self.timeout
        while time.monotonic() < end:
            if chan.recv_ready():
                buf += chan.recv(65535).decode(errors="replace")
                if _PROMPT.match(buf.replace("\r", "").rsplit("\n", 1)[-1]):
                    return buf
            else:
                time.sleep(0.02)
        raise DeviceError(f"{self.name}: no prompt within {self.timeout}s")

    def running_config(self) -> str:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(self.host, port=self.port, username=self.user, password=self.password,
                           timeout=self.timeout, banner_timeout=self.timeout, auth_timeout=self.timeout,
                           look_for_keys=False, allow_agent=False)
            chan = client.invoke_shell()
            self._read_until_prompt(chan)
            chan.send("terminal length 0\n")
            self._read_until_prompt(chan)
            chan.send("show running-config\n")
            out = self._read_until_prompt(chan).replace("\r", "").split("\n")
            return "\n".join(out[1:-1])                  # drop the echoed command and the prompt
        except DeviceError:
            raise
        except Exception as exc:
            raise DeviceError(f"{self.name} ({self.host}): {exc}") from exc
        finally:
            client.close()


@dataclass
class FortiosDevice(Device):
    port: int = 443
    token: str = ""
    verify_tls: bool = True

    def running_config(self) -> str:
        if not self.verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        url = f"https://{self.host}:{self.port}/api/v2/monitor/system/config/backup"
        try:
            r = requests.get(url, params={"scope": "global"}, headers={"Authorization": f"Bearer {self.token}"},
                             verify=self.verify_tls, timeout=self.timeout)
            r.raise_for_status()
            return r.text
        except requests.RequestException as exc:
            raise DeviceError(f"{self.name} ({self.host}): {exc}") from exc


def load_targets(path: str | Path) -> dict[str, Target]:
    try:
        raw = yaml.safe_load(Path(path).read_text())["targets"]
        return {name: Target(**t) for name, t in raw.items()}
    except (OSError, KeyError, TypeError, yaml.YAMLError, ValidationError) as exc:
        raise TargetsError(f"{path}: {exc}") from exc


def _secret(name: str, var: str) -> str:
    value = os.environ.get(var)
    if not value:
        raise TargetsError(f"{name}: environment variable {var} is not set")
    return value


def targets_from_file(path: str | Path) -> dict[str, Device]:
    devices: dict[str, Device] = {}
    for name, t in load_targets(path).items():
        if t.platform == "cisco_ios":
            devices[name] = IosDevice(name, t.host, user=t.user, port=t.port, password=_secret(name, t.password_env))
        else:
            devices[name] = FortiosDevice(name, t.host, port=t.port, token=_secret(name, t.token_env),
                                          verify_tls=t.verify_tls)
    return devices


def intended_from_file(path: str | Path) -> dict[str, str]:
    try:
        return {name: Path(t.intended).read_text() for name, t in load_targets(path).items()}
    except OSError as exc:
        raise TargetsError(f"{path}: {exc}") from exc
