"""Change a vendor stand-in by hand: vendor_poke.py ios|fortios <command...>"""
import json
import os
import sys
import time

import paramiko
import requests
import urllib3

urllib3.disable_warnings()     # the stand-in uses a throwaway self-signed cert on localhost


def ios(port, lines):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("127.0.0.1", port=port, username="netops", password=os.environ["IOS_PASSWORD"],
              look_for_keys=False, allow_agent=False)
    chan = c.invoke_shell()
    for line in ["configure terminal", *lines, "end", "exit"]:
        chan.send(line + "\n")
        time.sleep(0.2)
    c.close()


def fortios(port, cmd):
    r = requests.post(f"https://127.0.0.1:{port}/sim/cli", data=json.dumps({"cmd": cmd}), verify=False,
                      headers={"Authorization": f"Bearer {os.environ['FORTIOS_TOKEN']}"}, timeout=8)
    r.raise_for_status()


if __name__ == "__main__":
    kind, rest = sys.argv[1], sys.argv[2:]
    if kind == "ios":                                    # e.g. "interface Gi0/5" "shutdown"
        ios(int(os.environ.get("IOS_PORT", 2201)), rest)
    else:                                                # e.g. "add-policy 4 test-allow"
        fortios(int(os.environ.get("FORTIOS_PORT", 8443)), " ".join(rest))
