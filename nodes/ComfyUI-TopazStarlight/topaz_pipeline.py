# -*- coding: utf-8 -*-
"""Topaz Starlight pipeline - pure functions (testable outside ComfyUI).

Dual-engine support (2026-08-19):
  - neuroserver/      = 1.6.1 engine  -> 星光 2.5 (slp-25), fallback for Astra trio
  - neuroserver171/   = 1.7.1 engine  -> 星光 2.6 (slp-26), Astra family incl. Fast
Engine is chosen per model_id. Each engine uses its own ffmpeg (1.7.1 needs
its bundled build; 1.6.1 works with bin/ or system ffmpeg).
Astra-family models require >= 9 input frames on BOTH engines (short-clip path
is broken in both; verified 2026-08-19).
"""
import os, re, subprocess, sys, shutil
import torch
from pathlib import Path
from ._common import NS_ENC, _node_ffmpeg, read_video_to_frames, read_video_to_tensor, _env_no_cuda_limit

# <ComfyUI>/custom_nodes/ComfyUI-TopazStarlight/topaz_pipeline.py
_NODE_DIR  = Path(__file__).resolve().parent
_ENGINE    = Path(__file__).resolve().parent.parent.parent / 'topaz_engine'

_TVMD = _NODE_DIR / 'tvmd'          # 干净授权目录 (随节点分发, 1KB)

# 模型 -> 引擎目录 / ffmpeg 目录
MODEL_ENGINE = {
    'slp-26':      ('neuroserver171', 'bin171'),   # 星光 (2.5条目=2.6权重) -> 1.7.1 引擎
    'astra':       ('neuroserver171', 'bin171'),   # Astra 家族 -> 1.7.1 引擎
    'astrahq':     ('neuroserver171', 'bin171'),
    'astrasharp':  ('neuroserver171', 'bin171'),
    'astrafast':   ('neuroserver171', 'bin171'),
}
ASTRA_MODELS = ('astra', 'astrahq', 'astrasharp', 'astrafast')
MIN_FRAMES_ASTRA = 9   # Astra 家族最短帧数 (两版引擎均验证: <9 帧走坏路径)


def _ffmpeg_for(model_id):
    """返回 (ffmpeg.exe, ffprobe.exe) 绝对路径, 按模型选引擎配套 ffmpeg."""
    _, ff_dir = MODEL_ENGINE.get(model_id, ('neuroserver', 'bin'))
    cand = [
        _ENGINE / ff_dir / 'ffmpeg.exe',
        _ENGINE / 'bin' / 'ffmpeg.exe',
        Path(r'D:\APP\ffmpeg\bin\ffmpeg.exe'),
    ]
    for c in cand:
        if c.is_file():
            return str(c), str(c.with_name('ffprobe.exe'))
    w = shutil.which('ffmpeg')
    if w:
        return w, w.replace('ffmpeg.exe', 'ffprobe.exe')
    raise RuntimeError('ffmpeg not found - install ffmpeg or put it on PATH')


# --------------------------------------------------------------------------
# engine detection
# --------------------------------------------------------------------------

def _find_bundled(model_id='slp-26'):
    eng_dir, _ = MODEL_ENGINE.get(model_id, ('neuroserver171', 'bin171'))
    ns = _ENGINE / eng_dir / 'neuroserver.exe'
    if ns.is_file():
        tvmd = _ENGINE / 'tvmd'
        return {
            'name': 'bundled(%s)' % eng_dir,
            'ns': str(ns),
            'model_store': str(_ENGINE / 'models'),
            'tvai_dir': str(tvmd) if (tvmd / 'VR.lic').is_file() else str(_TVMD),
            'lic': str(_TVMD / 'VR.lic') if not (tvmd / 'VR.lic').is_file() else str(tvmd / 'VR.lic'),
        }
    return None


def _find_installed():
    """Detect a user-installed Topaz Video (registry InstallDir + common paths)."""
    candidates = []
    try:
        import winreg
        for hive, sub in ((winreg.HKEY_CURRENT_USER, r'Software\Topaz Labs LLC\Topaz Video'),
                          (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Topaz Labs LLC\Topaz Video')):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    val, _ = winreg.QueryValueEx(k, 'InstallDir')
                    if val:
                        candidates.append(Path(val))
            except OSError:
                pass
    except Exception:
        pass
    for base in (Path(os.environ.get('ProgramFiles', 'C:\\Program Files')),
                 Path(os.environ.get('ProgramFiles(x86)', 'C:\\Program Files (x86)')),
                 Path('C:/Program Files'), Path('D:/')):
        candidates.append(base / 'Topaz Labs LLC' / 'Topaz Video')

    for cand in candidates:
        ns = cand / 'neuroserver' / 'neuroserver.exe'
        if not ns.is_file():
            continue
        model_store = None
        for m in (cand / 'models' / 'models', cand / 'models',
                  Path('C:/ProgramData/Topaz Labs LLC/Topaz Video/models')):
            if (m / 'slp25').is_dir():
                model_store = str(m)
                break
        if model_store is None:
            continue
        return {
            'name': 'installed@%s' % cand,
            'ns': str(ns),
            'model_store': model_store,
            'tvai_dir': None,
            'lic': None,
        }
    return None


def _resolve_engine(model_id='slp-26'):
    mode = os.environ.get('TOPAZ_ENGINE_MODE', '').lower()
    if mode == 'detected':
        e = _find_installed() or _find_bundled(model_id)
    else:  # bundled first
        e = _find_bundled(model_id) or _find_installed()
    if e is None:
        raise RuntimeError(
            '未找到星光引擎: 请把 topaz_engine 放在 ComfyUI 目录下, '
            '或在本机安装 Topaz Video (节点会自动识别)')
    return e


def _ensure_tvmd():
    if not (_TVMD / 'VR.lic').is_file():
        _TVMD.mkdir(parents=True, exist_ok=True)
        for cand in (_ENGINE / 'tvmd',):
            if (cand / 'VR.lic').is_file():
                shutil.copy(cand / 'VR.lic', _TVMD / 'VR.lic')
                shutil.copy(cand / 'VR.lic', _TVMD / 'Topaz.lic')
                break


def engine_info(model_id='slp-26'):
    e = _resolve_engine(model_id)
    return e['name'], e['ns'], e['model_store']


def write_frames_to_video(frames_uint8, fps, path, qp=14):
    """frames_uint8: (B,H,W,3) uint8 RGB -> h264 mp4 (nvenc, high quality)."""
    ffmpeg, _ = _node_ffmpeg()
    b, h, w, c = frames_uint8.shape
    cmd = [ffmpeg, '-y', '-loglevel', 'error',
           '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{w}x{h}', '-r', str(fps), '-i', '-',
           '-c:v', 'h264_nvenc', '-preset', 'p7', '-tune', 'hq',
           '-rc', 'constqp', '-qp', str(qp), '-pix_fmt', 'yuv420p', path]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    # memoryview 零复制直写 pipe，避免 tobytes() 全量副本（大视频时省一份完整内存）
    _, err = p.communicate(input=memoryview(frames_uint8))
    if p.returncode != 0:
        raise RuntimeError(f'ffmpeg encode failed: {err.decode(errors="replace")[:300]}')


def run_starlight(in_path, out_path, scale, frames, w, h, strength=1.0, model_id='slp-26', log=print,
                  max_gpu_mem=16, on_progress=None, device=None):
    """Run the Topaz neuroserver on a video file (engine chosen per model_id).
    on_progress(pct): optional callback, receives 0-100 int as engine reports it.
    device: CUDA 设备号（--device，默认 None=引擎默认 gpu_id 0）。"""
    if model_id in ASTRA_MODELS and frames < MIN_FRAMES_ASTRA:
        raise RuntimeError(
            f'Astra 系列模型需要至少 {MIN_FRAMES_ASTRA} 帧输入 (当前 {frames} 帧), '
            '两版引擎的短视频路径均存在缺陷。请用更长的视频 (或提高帧率)。')

    eng = _resolve_engine(model_id)
    ffmpeg, ffprobe = _ffmpeg_for(model_id)
    env = _env_no_cuda_limit()
    # 神经服务器内部也会调 ffmpeg/ffprobe，子进程 PATH 必须能找到 (1.7.1 必须自带版)
    env['PATH'] = str(Path(ffmpeg).parent) + os.pathsep + env.get('PATH', '')
    env['TOPAZ_MODEL_STORE'] = eng['model_store']
    if eng['tvai_dir']:
        env['TVAI_MODEL_DIR'] = eng['tvai_dir']
        env['TOPAZLABS_LICENSE'] = eng['lic']
    else:
        env.pop('TVAI_MODEL_DIR', None)
        env.pop('TOPAZLABS_LICENSE', None)
    if model_id == 'slp-26':
        # 1.7.1 GUI 原版参数: 星光带 softness(默认1), Astra 家族不带
        filters = '[{"model": "%s", "enhancement_strength": %s, "softness": 1}]' % (model_id, strength)
    else:
        filters = '[{"model": "%s", "enhancement_strength": %s}]' % (model_id, strength)
    log('  [Topaz] 模型: %s' % model_id)
    # 输出尺寸取整并对齐到偶数（h264/yuv420p 要求偶数宽高）
    ow = int(round(w * scale)); ow += ow % 2
    oh = int(round(h * scale)); oh += oh % 2
    cmd = [eng['ns'], '--once',
           '--input-path', in_path,
           '--output-path', out_path,
           '--start-frame-idx', '0',
           '--end-frame-idx', str(frames),
           '--max-gpu-mem', str(max_gpu_mem),
           '--filters', filters,
           '--output-width', str(ow),
           '--output-height', str(oh),
           '--upscale-factor', str(scale),
           '--ffmpeg-encoding', NS_ENC]
    if device is not None:
        cmd += ['--device', str(device)]
    log('  [Topaz] 引擎: %s' % eng['name'])
    log('  [Topaz] 模型目录: %s' % eng['model_store'])
    log('  [Topaz] GPU: %s' % ('默认(0)' if device is None else str(device)))
    log('  [Topaz] 开始处理 ...')
    proc = subprocess.Popen(cmd, env=env, cwd=str(Path(eng['ns']).parent),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding='utf-8', errors='replace')
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log('  [Topaz] ' + line)
            # 解析进度 (神经服务器逐帧输出 progress 字段)
            if on_progress is not None:
                m = re.search(r'"progress"\s*:\s*(\d+)', line)
                if m:
                    on_progress(int(m.group(1)))
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f'neuroserver failed (exit {proc.returncode})')
    if not os.path.isfile(out_path):
        raise RuntimeError('neuroserver produced no output file')
