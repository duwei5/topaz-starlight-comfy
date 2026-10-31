#!/usr/bin/env bash
# Frame upscale + deliverable encode (AutoDL / any Linux GPU box).
#
#   bash linux/run_sr.sh input.mp4 --scale 2 --target 1080p [--prores]
#   bash linux/run_sr.sh input.mp4 --scale 4 --target 2k
#
# Steps: extract frames -> Real-ESRGAN -> downscale to target -> encode with source audio.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA=/root/autodl-tmp

VIDEO=""; SCALE=2; TARGET=1080p; BATCH=None; PROFILE=0; CRF=17
while [ $# -gt 0 ]; do
  case "$1" in
    --scale)  SCALE="$2"; shift 2 ;;
    --target) TARGET="$2"; shift 2 ;;
    --batch)  BATCH="$2"; shift 2 ;;
    --crf)    CRF="$2"; shift 2 ;;
    --prores) PROFILE=1; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) VIDEO="$1"; shift ;;
  esac
done
[ -z "$VIDEO" ] && { echo "usage: run_sr.sh <video> [--scale 2|4] [--target 1080p|2k|4k]"; exit 2; }
[ -f "$VIDEO" ] || { echo "no such file: $VIDEO"; exit 2; }

case "$TARGET" in
  1080p) TW=1920; TH=1080 ;;
  2k)    TW=2560; TH=1440 ;;
  4k)    TW=3840; TH=2160 ;;
  *) echo "target must be 1080p / 2k / 4k"; exit 2 ;;
esac

NAME="$(basename "${VIDEO%.*}")"
WORK="$DATA/sr/$NAME"
mkdir -p "$WORK/frames" "$WORK/up"
[ -n "$(ls -A "$WORK/frames" 2>/dev/null)" ] || {
  echo "[1/4] extracting frames"
  ffmpeg -hide_banner -loglevel error -y -i "$VIDEO" -an -fps_mode passthrough \
         -start_number 1 "$WORK/frames/f%05d.png"
}
N=$(ls "$WORK/frames" | wc -l)
echo "frames: $N"

# fps from the source, fall back to 24
FPS=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of csv=p=0 "$VIDEO" | head -1)
[ -z "$FPS" ] && FPS=24

if [ "$BATCH" = "None" ]; then
  # small input (736x416): batch is memory-cheap, push it on big cards
  VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | tr -d ' ')
  if   [ "$VRAM" -ge 30000 ]; then BATCH=24
  elif [ "$VRAM" -ge 20000 ]; then BATCH=16
  elif [ "$VRAM" -ge 12000 ]; then BATCH=8
  else BATCH=4; fi
fi

echo "[2/4] upscaling x$SCALE (batch $BATCH)"
python "$ROOT/linux/upscale_frames.py" \
  --weights "$ROOT/weights/RealESRGAN_x${SCALE}plus.pth" \
  --in "$WORK/frames" --out "$WORK/up" --scale "$SCALE" --batch "$BATCH"

echo "[3/4] downscale to ${TW}x${TH}"
VF="scale=${TW}:-2:flags=lanczos,crop=${TW}:${TH},unsharp=5:5:0.35:5:5:0.0"

OUT="$WORK/${NAME}_${TARGET}"
if [ "$PROFILE" = "1" ]; then
  echo "[4/4] encoding ProRes 422 HQ (10-bit, for grading in FCPX/DaVinci)"
  ffmpeg -hide_banner -loglevel error -y -framerate "$FPS" -i "$WORK/up/f%05d.png" \
         -i "$VIDEO" -map 0:v -map 1:a? -vf "$VF" \
         -c:v prores_ks -profile:v 3 -vendor apl0 -pix_fmt yuv422p10le \
         -c:a pcm_s16le "${OUT}_ProRes.mov"
  ls -lh "${OUT}_ProRes.mov"
else
  echo "[4/4] encoding H.264 crf$CRF"
  ffmpeg -hide_banner -loglevel error -y -framerate "$FPS" -i "$WORK/up/f%05d.png" \
         -i "$VIDEO" -map 0:v -map 1:a? -vf "$VF" \
         -c:v libx264 -preset slow -crf "$CRF" -pix_fmt yuv420p -profile:v high -level 4.1 \
         -c:a copy -movflags +faststart "${OUT}.mp4"
  ls -lh "${OUT}.mp4"
fi

echo
echo "frames in : $(ls "$WORK/frames" | wc -l)"
echo "frames out: $(ls "$WORK/up" | wc -l)   (must match input)"
echo "output    : $WORK"