#!/bin/bash
# One-time, idempotent setup for http://opsdash.test -> 127.0.0.1:8321.
#
# Three pieces:
#   1. /etc/hosts maps opsdash.test to 127.0.0.1 (IPv4 only, on purpose:
#      the pf rule below is inet-only, and an ::1 mapping would send
#      browsers to an unredirected IPv6 port 80 first).
#   2. A pf rdr anchor redirects loopback :80 -> :8321. It is loaded into
#      the "com.apple/*" anchor namespace because macOS's stock
#      /etc/pf.conf evaluates `rdr-anchor "com.apple/*"` — this way we
#      never edit /etc/pf.conf (which OS updates can clobber).
#   3. A LaunchDaemon reloads the anchor at every boot.
#
# The dashboard server itself still binds 127.0.0.1:8321 only; nothing
# becomes reachable from the network.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run with sudo: sudo bash $0" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
ANCHOR_FILE=/etc/pf.anchors/com.tradingagents.opsdash
ANCHOR_NAME="com.apple/250.opsdash"
PLIST_SRC="$REPO_DIR/com.tradingagents.opsdash-pf.plist"
PLIST_DST=/Library/LaunchDaemons/com.tradingagents.opsdash-pf.plist

# 1. hosts entry
if ! grep -qE '^[^#]*\bopsdash\.test\b' /etc/hosts; then
  printf '127.0.0.1\topsdash.test\n' >> /etc/hosts
  echo "added opsdash.test to /etc/hosts"
else
  echo "/etc/hosts already maps opsdash.test"
fi

# 2. pf anchor
cat > "$ANCHOR_FILE" <<'EOF'
rdr pass on lo0 inet proto tcp from any to 127.0.0.1 port 80 -> 127.0.0.1 port 8321
EOF
/sbin/pfctl -E 2>/dev/null || true
/sbin/pfctl -a "$ANCHOR_NAME" -f "$ANCHOR_FILE"
echo "pf anchor loaded ($ANCHOR_NAME)"

# 3. LaunchDaemon for boot persistence
install -m 644 -o root -g wheel "$PLIST_SRC" "$PLIST_DST"
launchctl bootout system "$PLIST_DST" 2>/dev/null || true
launchctl bootstrap system "$PLIST_DST"
echo "LaunchDaemon installed"

echo "ok — open http://opsdash.test"
