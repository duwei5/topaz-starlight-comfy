#!/usr/bin/env bash
# AutoDL one-shot: clone -> env+weights -> optional smoke -> upscale.
#
#   bash autodl_quickstart.sh                          # env + weights + self test
#   bash autodl_quickstart.sh --run /root/autodl-tmp/master.mp4 --scale 2 --target 1080p
#
# Safe to re-run: every step skips what is already there.
set -euo pipefail

REPO_URL="https://github.com/duwei5/topaz-starlight-comfy.git"
DEST=/root/sr
VIDEO=""; SCALE=2; TARGET=1080p

while [ $# -gt 0 ]; do
  case "$1" in
    --run)    VIDEO="$2"; shift 2 ;;
    --scale)  SCALE="$2"; shift 2 ;;
    --target) TARGET="$2"; shift 2 ;;
    --dest)   DEST="$2"; shift 2 ;;
    *) echo "unknown arg: $1"; exit 2 ;;
  esac
done

echo "== 0. network =="
# AutoDL ships an academic proxy; enable it if present (GitHub / HF are slow otherwise)
[ -f /etc/network_turbo ] && { echo "  enabling network_turbo"; source /etc/network_turbo; } || echo "  no /etc/network_turbo"

echo "== 1. disk =="
df -h /root/autodl-tmp 2>/dev/null || df -h /root
echo "  system disk:"; df -h / | tail -1

echo "== 2. clone =="
if [ -d "$DEST/.git" ]; then
  echo "  exists, pulling"; git -C "$DEST" pull --ff-only || true
else
  git clone --depth 1 "$REPO_URL" "$DEST"
fi

echo "== 3. env + weights + self test =="
bash "$DEST/linux/setup_autodl.sh" --scale "$SCALE"

if [ -n "$VIDEO" ]; then
  [ -f "$VIDEO" ] || { echo "no such video: $VIDEO"; exit 2; }
  echo "== 4. upscale =="
  bash "$DEST/linux/run_sr.sh" "$VIDEO" --scale "$SCALE" --target "$TARGET"
  echo
  echo "== 5. result =="
  ls -lh /root/autodl-tmp/sr/*/
  echo "download: JupyterLab file browser -> /root/autodl-tmp/sr/<name>/"
else
  echo
  echo "env ready. now put a video in /root/autodl-tmp and run:"
  echo "  bash $DEST/linux/run_sr.sh /root/autodl-tmp/<your>.mp4 --scale $SCALE --target $TARGET"
fi