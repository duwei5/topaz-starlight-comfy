"""Topaz Starlight runner — portable (Windows / Linux, local / cloud).

Runs one video through the Topaz Starlight ComfyUI node and prints where the
result landed.

  python sr_run.py -v input/master.mp4 -s 2.609 -m 6 -o film1080
  python sr_run.py -v clip.mp4 -c 10            # 10-frame smoke test
  python sr_run.py --env                        # environment self-check only

Layout assumed (override with --root):

  <root>/main.py  comfy/ ...        ComfyUI itself
  <root>/topaz_engine/              engine: neuroserver171, bin171, tvmd, models
  <root>/custom_nodes/ComfyUI-TopazStarlight/
  <root>/input/  <root>/output/  <root>/temp/

The engine path is derived by the node from its own location, so the node must
sit exactly at <root>/custom_nodes/ComfyUI-TopazStarlight/ (no extra nesting).
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API = "http://127.0.0.1:8189"

MODEL_HINTS = {
    "slp": "2.6",        # Starlight 2.6 (engine 1.7.1, slp-26 weights)
    "astra": "astra",
    "astrahq": "hq",
    "astrasharp": "sharp",
    "astrafast": "fast",
}


def get_json(url, payload=None, timeout=120):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def combo_options(info, node, field):
    spec = info[node]["input"].get("required", {}).get(field)
    if spec is None:
        spec = info[node]["input"].get("optional", {}).get(field)
    if isinstance(spec, list) and spec and isinstance(spec[0], list):
        return list(spec[0])
    return []


def pick(options, needle, default_index=0):
    for o in options:
        if needle.lower() in str(o).lower():
            return o
    return options[default_index] if options else None


def env_check(root: Path) -> int:
    print("root:", root)
    checks = []
    py = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    checks.append(("venv python", py.is_file()))
    checks.append(("main.py", (root / "main.py").is_file()))
    node = root / "custom_nodes" / "ComfyUI-TopazStarlight"
    checks.append(("node dir", node.is_dir()))
    checks.append(("node __init__", (node / "__init__.py").is_file()))
    eng = root / "topaz_engine"
    checks.append(("engine exe", (eng / "neuroserver171" / "neuroserver.exe").is_file()))
    checks.append(("try/1.6.1 engine", (eng / "neuroserver" / "neuroserver.exe").is_file()))
    checks.append(("engine ffmpeg", (eng / "bin171" / "ffmpeg.exe").is_file()))
    checks.append(("license tvmd", (eng / "tvmd" / "VR.lic").is_file()))
    slp = eng / "models" / "slp26"
    n_blob = len(list(slp.glob("blob.*"))) if slp.is_dir() else 0
    checks.append(("slp26 blobs (%d)" % n_blob, n_blob >= 40))
    for name, ok in checks:
        print("  [%s] %s" % ("OK " if ok else "!! ", name))
    bad = [n for n, ok in checks if not ok]
    print("\nRESULT:", "ready" if not bad else "missing: " + ", ".join(bad))
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--video")
    ap.add_argument("-s", "--scale", type=float, default=2.0)
    ap.add_argument("-m", "--max-gpu", type=float, default=6.0)
    ap.add_argument("-o", "--outdir", default="film")
    ap.add_argument("-n", "--name", default="")
    ap.add_argument("-f", "--fps", type=float, default=24.0)
    ap.add_argument("-c", "--cap", type=int, default=0)
    ap.add_argument("--model", default="slp", choices=sorted(MODEL_HINTS))
    ap.add_argument("--strength", type=float, default=1.0)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--url", default=API)
    ap.add_argument("--env", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if args.env:
        return env_check(root)
    if not args.video:
        ap.error("-v/--video is required (or use --env)")

    global API
    API = args.url
    in_dir = root / "input"
    in_dir.mkdir(parents=True, exist_ok=True)

    src = Path(args.video).resolve()
    if not src.is_file():
        sys.exit("no such file: %s" % src)
    base = src.name
    dst = in_dir / base
    if dst.resolve() != src:
        shutil.copy2(src, dst)
    name = args.name or src.stem

    try:
        info = get_json(API + "/object_info")
    except Exception as e:
        sys.exit("ComfyUI not reachable at %s (%s). Start it first." % (API, e))

    models = combo_options(info, "TopazStarlight", "\u6a21\u578b")
    naming = combo_options(info, "TopazVideoOutput", "\u547d\u540d")[0]
    enc = pick(combo_options(info, "TopazVideoOutput", "\u7f16\u7801"), "nvenc")
    gpu = pick(combo_options(info, "TopazStarlight", "GPU\u9009\u62e9"), "auto")
    model = pick(models, MODEL_HINTS[args.model])
    if model is None:
        sys.exit("no model combo found; node not loaded?")

    prompt = {
        "1": {"class_type": "VHS_LoadVideo", "inputs": {
            "video": base, "force_rate": args.fps, "custom_width": 0, "custom_height": 0,
            "frame_load_cap": args.cap, "skip_first_frames": 0, "select_every_nth": 1}},
        "2": {"class_type": "TopazStarlight", "inputs": {
            "\u56fe\u50cf": ["1", 0], "\u6a21\u578b": model,
            "\u663e\u5b58\u4e0a\u9650": args.max_gpu, "\u653e\u5927\u500d\u6570": args.scale,
            "\u5e27\u7387": int(args.fps), "\u589e\u5f3a\u5f3a\u5ea6": args.strength,
            "\u8f93\u5165\u8d28\u91cf": 14, "GPU\u9009\u62e9": gpu}},
        "3": {"class_type": "TopazVideoOutput", "inputs": {
            "\u56fe\u50cf": ["2", 0], "\u97f3\u9891": ["1", 2], "\u5e27\u7387": int(args.fps),
            "\u8f93\u51fa\u76ee\u5f55": args.outdir, "\u65e5\u671f\u5b50\u6587\u4ef6\u5939": False,
            "\u6587\u4ef6\u540d\u524d\u7f00": name, "\u547d\u540d": naming, "\u7f16\u7801": enc}},
    }

    t0 = time.time()
    try:
        res = get_json(API + "/prompt", {"prompt": prompt})
    except urllib.error.HTTPError as e:
        sys.exit("submit failed HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:2000]))
    pid = res["prompt_id"]
    print("model=%s scale=%s vram=%sGB cap=%s pid=%s"
          % (model, args.scale, args.max_gpu, args.cap or "all", pid))

    while True:
        time.sleep(10)
        h = get_json(API + "/history/" + pid)
        if pid in h:
            st = h[pid].get("status", {})
            ok = st.get("completed") and st.get("status_str") == "success"
            dt = time.time() - t0
            print("done=%s elapsed=%.1fs (%.1f min)" % (ok, dt, dt / 60))
            for node, out in h[pid].get("outputs", {}).items():
                for key, val in out.items():
                    print("  %s.%s -> %s" % (node, key, val))
            if not ok:
                print(json.dumps(st, ensure_ascii=False)[:3000])
            print("output root: %s" % (root / "output"))
            return 0 if ok else 1
        q = get_json(API + "/queue")
        sys.stdout.write("\r  %.1f min | running=%d   " % ((time.time() - t0) / 60,
                                                           len(q.get("queue_running", []))))
        sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())