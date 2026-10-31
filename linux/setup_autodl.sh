#!/usr/bin/env bash
# AutoDL / Linux bootstrap for the Real-ESRGAN upscale track.
#   bash linux/setup_autodl.sh [--scale 2|4]
# Tested shape: AutoDL PyTorch 2.x + CUDA 12.1 image, data disk at /root/autodl-tmp.
set -euo pipefail

SCALE=2
while [ $# -gt 0 ]; do
  case "$1" in
    --scale) SCALE="$2"; shift 2 ;;
    *) echo "unknown arg: $1"; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA=/root/autodl-tmp
mkdir -p "$ROOT/weights" "$ROOT/work" "$DATA"

echo "== gpu =="
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader || {
  echo "no NVIDIA driver"; exit 1; }

echo "== python =="
python -V
python -c "import torch;print('torch',torch.__version__,'cuda',torch.version.cuda,'avail',torch.cuda.is_available())" || {
  echo ">> installing torch cu121"
  pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cu121
}

echo "== python deps =="
python -c "import numpy, PIL" 2>/dev/null || pip install --no-cache-dir numpy pillow

echo "== ffmpeg =="
command -v ffmpeg >/dev/null || { echo ">> installing ffmpeg"; apt-get update -y && apt-get install -y ffmpeg; }

echo "== weights =="
get () {  # get <file> <url-github> <url-mirror>
  local f="$ROOT/weights/$1" g="$2" m="$3"
  [ -s "$f" ] && { echo "  have $1"; return; }
  echo "  downloading $1"
  curl -fL --retry 3 -o "$f.part" "$g" || curl -fL --retry 3 -o "$f.part" "$m"
  mv "$f.part" "$f"
}
get RealESRGAN_x2plus.pth \
  https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth \
  https://hf-mirror.com/ai-forever/Real-ESRGAN/resolve/main/RealESRGAN_x2.pth
get RealESRGAN_x4plus.pth \
  https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth \
  https://hf-mirror.com/ai-forever/Real-ESRGAN/resolve/main/RealESRGAN_x4.pth

ls -lh "$ROOT/weights"/*.pth

echo "== self test (one synthetic frame) =="
python - "$ROOT" "$SCALE" <<'PY'
import sys, os, numpy as np
from PIL import Image
root, scale = sys.argv[1], int(sys.argv[2])
os.makedirs(os.path.join(root, "work", "selftest_in"), exist_ok=True)
a = (np.random.rand(416, 736, 3) * 255).astype("uint8")
Image.fromarray(a).save(os.path.join(root, "work", "selftest_in", "f00001.png"))
print("wrote selftest frame")
PY
python "$ROOT/linux/upscale_frames.py" \
  --weights "$ROOT/weights/RealESRGAN_x${SCALE}plus.pth" \
  --in "$ROOT/work/selftest_in" --out "$ROOT/work/selftest_out" \
  --scale "$SCALE" --batch 2
python - "$ROOT" <<'PY'
import sys, os
from PIL import Image
root = sys.argv[1]
p = os.path.join(root, "work", "selftest_out")
f = sorted(os.listdir(p))[0]
im = Image.open(os.path.join(p, f))
print("selftest output:", im.size, "(expected width = input width * scale)")
PY

echo
echo "READY. run a clip:"
echo "  bash $ROOT/linux/run_sr.sh <video.mp4> --scale $SCALE --target 1080p"