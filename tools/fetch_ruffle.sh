#!/usr/bin/env bash
# 下载 Ruffle 自托管版到 vendor/ruffle/（Flash 游戏支持需要；不进 git 是因为 wasm 有 ~28MB）
set -e
VER="${1:-0.6.0}"
DIR="$(cd "$(dirname "$0")/.." && pwd)/vendor/ruffle"
mkdir -p "$DIR"
URL="https://github.com/ruffle-rs/ruffle/releases/download/v${VER}/ruffle-${VER}-web-selfhosted.zip"
echo "下载 $URL"
curl -fsSL -o /tmp/ruffle-ssh.zip "$URL"
python3 - "$DIR" <<'PYEOF'
import sys, zipfile
zipfile.ZipFile("/tmp/ruffle-ssh.zip").extractall(sys.argv[1])
print("解压到", sys.argv[1])
PYEOF
echo "完成。Ruffle 版本 $VER"
