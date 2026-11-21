#!/usr/bin/env bash
# End-to-end proof for the vendor targets, against the labsim stand-ins (no real IOS or FortiOS needed).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-python}
export PYTHONPATH=.
export IOS_PASSWORD=lab-pass FORTIOS_TOKEN=lab-token
DIR=$(mktemp -d)
pids=()
trap 'kill "${pids[@]}" 2>/dev/null || true; rm -rf "$DIR"' EXIT
fail() { echo "VENDOR E2E FAIL: $*" >&2; exit 1; }

$PY -m labsim.ios_sim --port 2201 >"$DIR/ios.log" 2>&1 & pids+=($!)
$PY -m labsim.fortios_sim --port 8443 >"$DIR/fortios.log" 2>&1 & pids+=($!)
for i in $(seq 1 50); do
  (exec 3<>/dev/tcp/127.0.0.1/2201 && exec 3<>/dev/tcp/127.0.0.1/8443) 2>/dev/null && break
  sleep 0.2
done

echo "== 1. backup: both targets saved as git history"
$PY -m netops vendor-backup --dir "$DIR/backups"
test "$(git -C "$DIR/backups" log --oneline | wc -l)" -ge 1 || fail "no backup commit"

echo "== 2. drift: clean targets have none"
$PY -m netops vendor-drift --dir "$DIR/backups" || fail "clean targets reported drift"

echo "== 3. drift: someone changes both by hand -> detected"
$PY scripts/vendor_poke.py ios "interface Gi0/5" shutdown
$PY scripts/vendor_poke.py fortios add-policy 4 test-allow
if $PY -m netops vendor-drift --dir "$DIR/backups" >"$DIR/drift.txt"; then fail "manual change was NOT detected"; fi
cat "$DIR/drift.txt"
grep -q "interface GigabitEthernet0/5 | shutdown" "$DIR/drift.txt" || fail "IOS change not in the report"
grep -q 'test-allow' "$DIR/drift.txt" || fail "FortiOS change not in the report"

echo "== 4. revert both -> clean again"
$PY scripts/vendor_poke.py ios "interface Gi0/5" "no shutdown"
$PY scripts/vendor_poke.py fortios del-policy 4
$PY -m netops vendor-drift --dir "$DIR/backups" || fail "still drifting after revert"
echo "VENDOR E2E OK"
