#!/usr/bin/env bash
# Archive the current working tradingview-mcp build into vendor/tradingview-mcp/.
# Run after every successful upgrade to keep a known-good fallback.
# See docs/tradingview-mcp-recovery.md for recovery instructions.
#
# Supports two layouts:
#   JS (current):         src/server.js  — archives src/ + package files
#   TypeScript (legacy):  build/index.js — archives single bundle

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
VENDOR_DIR="$REPO_ROOT/vendor/tradingview-mcp"

# Locate the source
if [[ -n "${TRADINGVIEW_MCP_PATH:-}" ]]; then
    SRC="$TRADINGVIEW_MCP_PATH"
    TV_MCP_DIR="$(dirname "$(dirname "$SRC")")"  # parent of src/ or build/
    if [[ "$(basename "$(dirname "$SRC")")" == "src" ]]; then
        LAYOUT="js"
    else
        LAYOUT="ts"
    fi
elif [[ -f "$HOME/.tradingview-mcp/src/server.js" ]]; then
    SRC="$HOME/.tradingview-mcp/src/server.js"
    TV_MCP_DIR="$HOME/.tradingview-mcp"
    LAYOUT="js"
elif [[ -f "$HOME/.tradingview-mcp/build/index.js" ]]; then
    SRC="$HOME/.tradingview-mcp/build/index.js"
    TV_MCP_DIR="$HOME/.tradingview-mcp"
    LAYOUT="ts"
else
    echo "ERROR: tradingview-mcp not found."
    echo "  JS layout:  clone and install to ~/.tradingview-mcp"
    echo "  TS layout:  build to ~/.tradingview-mcp/build/index.js"
    echo "  Or set:     TRADINGVIEW_MCP_PATH=/path/to/entry"
    exit 1
fi

if [[ ! -s "$SRC" ]]; then
    echo "ERROR: $SRC is empty."
    exit 1
fi

# Metadata
GIT_COMMIT="(not a git repo)"
GIT_REMOTE="unknown"
if git -C "$TV_MCP_DIR" rev-parse HEAD &>/dev/null 2>&1; then
    GIT_COMMIT="$(git -C "$TV_MCP_DIR" rev-parse HEAD)"
    GIT_REMOTE="$(git -C "$TV_MCP_DIR" remote get-url origin 2>/dev/null || echo 'unknown')"
fi
NODE_VERSION="$(node --version 2>/dev/null || echo 'unknown')"
ARCHIVE_DATE="$(date -u '+%Y-%m-%d %H:%M UTC')"

mkdir -p "$VENDOR_DIR"

if [[ "$LAYOUT" == "js" ]]; then
    echo "Layout:  JavaScript (src/server.js)"
    echo "Source:  $TV_MCP_DIR/src/"
    echo "Vendor:  $VENDOR_DIR/src/"
    echo ""

    # Copy src/ tree and package files
    rm -rf "$VENDOR_DIR/src"
    cp -r "$TV_MCP_DIR/src" "$VENDOR_DIR/src"
    cp "$TV_MCP_DIR/package.json" "$VENDOR_DIR/package.json"
    [[ -f "$TV_MCP_DIR/package-lock.json" ]] && cp "$TV_MCP_DIR/package-lock.json" "$VENDOR_DIR/package-lock.json"

    # Install prod-only deps into vendor so the fallback works without a network call
    echo "Installing production dependencies into vendor..."
    cd "$VENDOR_DIR" && npm ci --omit=dev --silent
    cd "$REPO_ROOT"

    SRC_SIZE="$(du -sh "$VENDOR_DIR/src" | cut -f1)"
    RESTORE_CMD="node $VENDOR_DIR/src/server.js"
    RESTORE_NOTE="node_modules/ is already present — no install step needed."

    cat > "$VENDOR_DIR/ARCHIVE_INFO" <<EOF
archived: $ARCHIVE_DATE
layout:   js (src/server.js)
source:   $TV_MCP_DIR/src/
remote:   $GIT_REMOTE
commit:   $GIT_COMMIT
node:     $NODE_VERSION
src_size: $SRC_SIZE
EOF

else
    echo "Layout:  TypeScript bundle (build/index.js)"
    echo "Source:  $SRC"
    echo "Vendor:  $VENDOR_DIR/index.js"
    echo ""

    SRC_SIZE="$(wc -c < "$SRC" | tr -d ' ')"
    cp "$SRC" "$VENDOR_DIR/index.js"
    RESTORE_CMD="node $VENDOR_DIR/index.js"
    RESTORE_NOTE="Single-bundle — no install step needed."

    cat > "$VENDOR_DIR/ARCHIVE_INFO" <<EOF
archived: $ARCHIVE_DATE
layout:   ts (build/index.js)
source:   $SRC
remote:   $GIT_REMOTE
commit:   $GIT_COMMIT
node:     $NODE_VERSION
size:     $SRC_SIZE bytes
EOF
fi

echo "Archived successfully."
echo ""
cat "$VENDOR_DIR/ARCHIVE_INFO"
echo ""
echo "To use vendor fallback:"
echo "  $RESTORE_NOTE"
echo "  Or set: TRADINGVIEW_MCP_PATH=$VENDOR_DIR/$([ "$LAYOUT" = "js" ] && echo "src/server.js" || echo "index.js")"
