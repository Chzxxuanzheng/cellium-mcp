#!/usr/bin/env bash
# 把组件以软链接方式安装到 Cellium components 目录
set -euo pipefail

ROOT="$(cd "$(dirname "${0}")"/.. && pwd)"
SRC="$ROOT/src/cellium_mcp"
DEST="${CELLIUM_COMPONENTS_DIR:-$HOME/.local/share/CelliumAgent/components}/cellium_mcp"

if [[ "${1:-}" == "--remove" || "${1:-}" == "-r" ]]; then
  if [[ -L "$DEST" ]]; then
    rm "$DEST"
    echo "已卸载: $DEST"
  else
    echo "未发现软链接: $DEST"
  fi
  exit 0
fi

if [[ ! -f "$SRC/__init__.py" ]]; then
  echo "源码目录不完整: $SRC" >&2
  exit 1
fi

mkdir -p "$(dirname "$DEST")"
ln -sfn "$SRC" "$DEST"
echo "已安装软链接:"
ls -l "$DEST"
