#!/usr/bin/env bash
# Archive the current working tradingview-mcp build into vendor/tradingview-mcp/.
# Run after every successful upgrade to keep a known-good fallback.
# See docs/tradingview-mcp-recovery.md for recovery instructions.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
VENDOR_DIR="$REPO_ROOT/vendor/tradingview-mcp"

# Locate the source build
if [[ -n "${TRADINGVIEW_MCP_PATH:-}" ]]; then
    SRC="$TRADINGVIEW_MCP_PATH"
    TV_MCP_DIR="$(dirname "$SRC")"
elif [[ -f "$HOME/.tradingview-mcp/build/index.js" ]]; then
    SRC="$HOME/.tradingview-mcp/build/index.js"
    TV_MCP_DIR="$HOME/.tradingview-mcp"
else
    echo "ERROR: tradingview-mcp build not found."
    echo "  Set TRADINGVIEW_MCP_PATH or build to ~/.tradingview-mcp/build/index.js"
    exit 1
fi

echo "Source:  $SRC"
echo "Vendor:  $VENDOR_DIR/index.js"
echo ""

# Smoke-test: verify the file is a non-empty JS bundle
if [[ ! -s "$SRC" ]]; then
    echo "ERROR: $SRC is empty — build may have failed."
    exit 1
fi

# Capture metadata
GIT_COMMIT="(not a git repo)"
if git -C "$TV_MCP_DIR" rev-parse HEAD &>/dev/null 2>&1; then
    GIT_COMMIT="$(git -C "$TV_MCP_DIR" rev-parse HEAD)"
    GIT_REMOTE="$(git -C "$TV_MCP_DIR" remote get-url origin 2>/dev/null || echo 'unknown')"
else
    GIT_REMOTE="unknown"
fi
NODE_VERSION="$(node --version 2>/dev/null || echo 'unknown')"
ARCHIVE_DATE="$(date -u '+%Y-%m-%d %H:%M UTC')"
SRC_SIZE="$(wc -c < "$SRC" | tr -d ' ')"

mkdir -p "$VENDOR_DIR"

# Copy the build
cp "$SRC" "$VENDOR_DIR/index.js"

# Write the info file (committed to git — the binary is not)
cat > "$VENDOR_DIR/ARCHIVE_INFO" <<EOF
archived: $ARCHIVE_DATE
source:   $SRC
remote:   $GIT_REMOTE
commit:   $GIT_COMMIT
node:     $NODE_VERSION
size:     $SRC_SIZE bytes
EOF

echo "Archived successfully."
echo ""
cat "$VENDOR_DIR/ARCHIVE_INFO"
echo ""
echo "To restore: cp vendor/tradingview-mcp/index.js ~/.tradingview-mcp/build/index.js"
echo "Or set:     export TRADINGVIEW_MCP_PATH=$VENDOR_DIR/index.js"
