"""Vendor targets: config noise, targets.yml, drift on fake devices, and the two stand-ins over real SSH/HTTP."""
import subprocess

import pytest

from labsim.fortios_sim import FortiosSim
from labsim.ios_sim import IosSim, Session
from netops import backup, confparse
from netops.cli import main
from netops.device import Device, DeviceError
from netops.vendor import FortiosDevice, TargetsError, load_targets, targets_from_file

IOS_RAW = """Building configuration...

Current configuration : 1432 bytes
!
! Last configuration change at 10:11:12 UTC Sat Nov 21 2026
! NVRAM config last updated at 10:11:13 UTC Sat Nov 21 2026
!
hostname sw-core
ntp clock-period 36028797
interface GigabitEthernet0/5
 shutdown
!
end
"""

FORTI_RAW = """#config-version=FGT60F-7.2.5-FW-build1517-230307:opmode=0:vdom=0:user=admin
#conf_file_ver=1234567890123456
#buildno=1517
#global_vdom=1:vd_name=root
config system global
    set hostname "fw-edge"
end
config system admin
    edit "admin"
        set password ENC SH2aaaa
    next
end
"""


class Fake(Device):
    def __init__(self, name, config="", up=True):
        super().__init__(name, "0.0.0.0")
        self.config, self.up = config, up

    def running_config(self):
        if not self.up:
            raise DeviceError("down")
        return self.config


def test_ios_noise_is_dropped():
    assert confparse.normalise(IOS_RAW) == "hostname sw-core\ninterface GigabitEthernet0/5\n shutdown\n"


def test_fortios_header_next_and_secrets():
    out = confparse.normalise(FORTI_RAW)
    assert "#" not in out and "next" not in out.split()
    assert "set password ENC <masked>" in out
    other = confparse.normalise(FORTI_RAW.replace("SH2aaaa", "SH2bbbb").replace("1234567890123456", "999"))
    assert other == out


def test_fortios_parse_keeps_edit_context():
    assert 'config system admin > edit "admin" | set password ENC <masked>' in confparse.parse(FORTI_RAW)


def write_targets(path, ios_port=2201, forti_port=8443):
    path.write_text(f"""targets:
  sw-core: {{platform: cisco_ios, host: 127.0.0.1, port: {ios_port}, user: netops, password_env: IOS_PASSWORD, intended: {path.parent}/sw.cfg}}
  fw-edge: {{platform: fortios, host: 127.0.0.1, port: {forti_port}, token_env: FORTIOS_TOKEN, verify_tls: false, intended: {path.parent}/fw.conf}}
""")
    return path


def test_targets_file_parses_with_secrets_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("IOS_PASSWORD", "pw")
    monkeypatch.setenv("FORTIOS_TOKEN", "tok")
    devs = targets_from_file(write_targets(tmp_path / "t.yml"))
    assert devs["sw-core"].password == "pw" and devs["sw-core"].port == 2201
    assert devs["fw-edge"].token == "tok" and devs["fw-edge"].verify_tls is False


def test_missing_secret_is_reported_clearly(tmp_path, monkeypatch):
    monkeypatch.delenv("IOS_PASSWORD", raising=False)
    monkeypatch.setenv("FORTIOS_TOKEN", "tok")
    with pytest.raises(TargetsError, match="sw-core: environment variable IOS_PASSWORD is not set"):
        targets_from_file(write_targets(tmp_path / "t.yml"))


def test_unknown_platform_is_rejected(tmp_path):
    f = tmp_path / "t.yml"
    f.write_text("targets: {x: {platform: junos, host: h, port: 1, intended: i}}\n")
    with pytest.raises(TargetsError):
        load_targets(f)


def test_cli_prints_the_error_not_a_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("IOS_PASSWORD", raising=False)
    assert main(["vendor-backup", "--targets", str(write_targets(tmp_path / "t.yml")), "--dir", str(tmp_path / "b")]) == 1
    assert "IOS_PASSWORD" in capsys.readouterr().err


def test_forti_backup_passes_the_hostname_guard(tmp_path):
    assert backup.backup({"fw": Fake("fw", FORTI_RAW)}, tmp_path) == {"fw": "new"}
    assert backup.backup({"fw": Fake("fw", FORTI_RAW.replace("SH2aaaa", "SH2zzzz"))}, tmp_path) == {"fw": "unchanged"}


def test_drift_on_fake_devices(tmp_path):
    dev = Fake("sw", IOS_RAW)
    intended = {"sw": confparse.normalise(IOS_RAW)}
    backup.backup({"sw": dev}, tmp_path)
    assert not backup.has_drift(backup.check_drift_files(intended, {"sw": dev}, tmp_path)["sw"])
    dev.config = IOS_RAW.replace(" shutdown\n", " description x\n")
    rep = backup.check_drift_files(intended, {"sw": dev}, tmp_path)["sw"]
    assert rep["missing"] == ["interface GigabitEthernet0/5 | shutdown"]
    assert rep["added"] == ["interface GigabitEthernet0/5 | description x"]
    assert rep["removed"] == ["interface GigabitEthernet0/5 | shutdown"]
    dev.up = False
    assert backup.check_drift_files(intended, {"sw": dev}, tmp_path)["sw"]["reachable"] is False


@pytest.fixture
def sims(tmp_path, monkeypatch):
    ios = IosSim(password="pw").start()
    forti = FortiosSim("tok").start()
    monkeypatch.setenv("IOS_PASSWORD", "pw")
    monkeypatch.setenv("FORTIOS_TOKEN", "tok")
    (tmp_path / "sw.cfg").write_text(open("vendor/intended/sw-core.cfg").read())
    (tmp_path / "fw.conf").write_text(open("vendor/intended/fw-edge.conf").read())
    yield ios, forti, write_targets(tmp_path / "t.yml", ios.port, forti.port)
    ios.stop()
    forti.stop()


def test_stand_ins_backup_and_drift_for_real(sims, tmp_path, capsys):
    ios, forti, targets = sims
    out = str(tmp_path / "b")
    assert main(["vendor-backup", "--targets", str(targets), "--dir", out]) == 0
    assert main(["vendor-drift", "--targets", str(targets), "--dir", out]) == 0
    assert "hostname sw-core" in (tmp_path / "b" / "sw-core.conf").read_text()
    assert "<masked>" in (tmp_path / "b" / "fw-edge.conf").read_text()

    sess = Session(ios.cfg)
    for line in ("configure terminal", "interface Gi0/5", "shutdown", "end"):
        sess.handle(line)
    forti.state.apply("add-policy 4 test-allow")
    capsys.readouterr()
    assert main(["vendor-drift", "--targets", str(targets), "--dir", out]) == 1
    text = capsys.readouterr().out
    assert "interface GigabitEthernet0/5 | shutdown" in text and "test-allow" in text
    log = subprocess.run(["git", "-C", out, "log", "--oneline"], capture_output=True, text=True).stdout
    assert log.count("\n") == 1


def test_wrong_credentials_mean_unreachable(sims, tmp_path, monkeypatch):
    monkeypatch.setenv("IOS_PASSWORD", "nope")
    monkeypatch.setenv("FORTIOS_TOKEN", "nope")
    assert main(["vendor-backup", "--targets", str(sims[2]), "--dir", str(tmp_path / "b")]) == 1


def test_fortios_before_7_6_backs_up_over_get():
    sim = FortiosSim("tok", backup_method="GET").start()
    try:
        dev = FortiosDevice("fw", "127.0.0.1", port=sim.port, token="tok", verify_tls=False)
        text = dev.running_config()
        assert "7.2.5" in text and "fw-edge" in text
    finally:
        sim.stop()
