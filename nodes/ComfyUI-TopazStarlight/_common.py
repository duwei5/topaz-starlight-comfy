# -*- coding: utf-8 -*-
"""TopazStarlight 两 pipeline 共享的通用函数。

提取自 topaz_pipeline.py 与 topaz_fi_pipeline.py 的重复实现：
  - _node_ffmpeg():  节点自用 ffmpeg/ffprobe 定位（写临时视频/读回输出）
  - NS_ENC:          中间编码参数（两处曾各定义一份，易漂移）
  - read_video_to_frames / read_video_to_tensor: 视频读回（流式，峰值≈输出张量）
"""
import os, shutil, subprocess, sys
import torch
from pathlib import Path

_ENGINE = Path(__file__).resolve().parent.parent.parent / 'topaz_engine'

# ---------------------------------------------------------------------------
# GPU 探测（多显卡支持）
# ---------------------------------------------------------------------------
# 两套引擎的 GPU 枚举在双卡机上实测相反，必须分开存：
#   cuda_idx  : torch/CUDA 枚举号 —— 超分 neuroserver 的 --device 语义
#               (本机实测: CUDA 0 = 4070TiS, CUDA 1 = 4060Ti, 与 ComfyUI 一致)
#   driver_idx: nvidia-smi/驱动枚举号 —— 插帧 tvai_fi 的 device 语义
#               (本机实测: driver 0 = 4060Ti, driver 1 = 4070TiS)
# UI 上统一按 CUDA 顺序显示卡名，节点内部各自映射到正确参数。


def _nvidia_smi_rows():
    """调用 nvidia-smi 拿 (driver_idx, name, uuid) 列表；失败返回 []。"""
    try:
        r = subprocess.run(
            ['nvidia-smi', '--query-gpu=index,name,uuid',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=15)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    rows = []
    for line in r.stdout.splitlines():
        parts = [p.strip() for p in line.split(',')]
        if len(parts) >= 3 and parts[0].lstrip('-').isdigit():
            rows.append((int(parts[0]), parts[1], parts[2]))
    return rows


def _norm_uuid(u):
    """统一 uuid 格式做比对（去 GPU- 前缀、0x、短横线，转小写）。"""
    if not u:
        return None
    if isinstance(u, bytes):
        u = u.decode('utf-8', errors='replace')
    u = str(u).strip()
    if u.lower().startswith('gpu-'):
        u = u[4:]
    u = u.replace('-', '').replace('0x', '').lower()
    return u or None


def detect_gpus(use_cache=True):
    """探测可用 NVIDIA 显卡，返回 [(cuda_idx, name, driver_idx), ...]。

    cuda_idx   —— CUDA 枚举号（超分 --device 语义）
    driver_idx —— nvidia-smi/驱动枚举号（插帧 tvai_fi device 语义）

    关键点：ComfyUI 主程序若用 `--cuda-device N` 启动，会自己设置
    `CUDA_VISIBLE_DEVICES`，导致进程内 torch 只看得见一张卡。因此这里
    不依赖进程内 torch 的可见卡列表，而是：
      1) nvidia-smi 枚举全部物理卡（driver 语义）
      2) 用一个不继承 CUDA_VISIBLE_DEVICES 的子进程探测完整 CUDA 枚举
      3) 按 GPU UUID 精确匹配两套枚举（同名多卡按出现顺序配对）
    无 GPU / 全部失败返回 []。结果带模块级缓存（GPU 列表不会运行时变化）。
    """
    global _DETECT_CACHE
    if use_cache and _DETECT_CACHE is not None:
        return _DETECT_CACHE
    result = _detect_gpus_uncached()
    _DETECT_CACHE = result
    return result


def _detect_gpus_uncached():
    drv = _nvidia_smi_rows()     # [(driver_idx, name, uuid)]
    cu = _cuda_probe_full()      # [(cuda_idx, name, uuid)] 完整 CUDA 枚举
    if not cu:
        # 子进程探测失败：退回 ComfyUI 进程内 torch 可见的卡
        try:
            n = torch.cuda.device_count()
            cu = [(i, torch.cuda.get_device_name(i),
                   _norm_uuid(getattr(torch.cuda.get_device_properties(i), 'uuid', None)))
                  for i in range(n)]
        except Exception:
            cu = []
    if not cu:
        return []

    # uuid 匹配：CUDA 卡 -> driver index
    drv_by_uuid = {}
    for di, name, uuid in drv:
        nu = _norm_uuid(uuid)
        if nu:
            drv_by_uuid.setdefault(nu, []).append((di, name))
    result = []
    used = set()
    for cuda_idx, name, uuid in cu:
        matched = None
        nu = _norm_uuid(uuid)
        if nu and nu in drv_by_uuid:
            matched = drv_by_uuid[nu][0]
        if matched is None:
            # 卡名匹配（按驱动顺序取未用的同名卡）
            for di, dname, _ in drv:
                if di in used:
                    continue
                if dname == name:
                    matched = (di, dname)
                    break
        if matched is None:
            matched = (cuda_idx, name)
        di, _ = matched
        used.add(di)
        result.append((cuda_idx, name, di))
    return result


_DETECT_CACHE = None


def _env_no_cuda_limit():
    """返回去掉了 CUDA_VISIBLE_DEVICES 等可见性限制的环境副本。

    引擎子进程（neuroserver / ffmpeg-tvai）继承该环境后才能看到全部物理卡，
    从而让 `--device N` / `device=N` 能指定任意一张卡（ComfyUI 若用
    --cuda-device N 启动会污染此变量，必须清除）。
    """
    env = os.environ.copy()
    for k in ('CUDA_VISIBLE_DEVICES', 'HIP_VISIBLE_DEVICES',
              'ASCEND_RT_VISIBLE_DEVICES'):
        env.pop(k, None)
    return env


def _cuda_probe_full():
    """spawn 一个不继承 CUDA_VISIBLE_DEVICES 的子进程，用 torch 探测完整
    CUDA 枚举。返回 [(cuda_idx, name, uuid), ...]；失败返回 []。"""
    code = (
        "import torch\n"
        "n = torch.cuda.device_count()\n"
        "for i in range(n):\n"
        "    p = torch.cuda.get_device_properties(i)\n"
        "    print(str(i), '||', p.name, '||', str(getattr(p, 'uuid', '') or ''))\n"
    )
    try:
        r = subprocess.run([sys.executable, '-c', code],
                           capture_output=True, text=True,
                           env=_env_no_cuda_limit(), timeout=30)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    rows = []
    for line in r.stdout.splitlines():
        if '||' not in line:
            continue
        idx_s, name, uuid = [s.strip() for s in line.split('||')]
        if idx_s.lstrip('-').isdigit():
            rows.append((int(idx_s), name, _norm_uuid(uuid)))
    return rows


def gpu_choices():
    """节点下拉选项：['Auto (自动)', 'GPU 0: <卡名>', ...]。任何异常只返回 Auto。"""
    opts = ['Auto (自动)']
    try:
        for cuda, name, _ in detect_gpus():
            opts.append(f'GPU {cuda}: {name}')
    except Exception:
        pass
    return opts


def resolve_gpu(sel):
    """把 UI 选项解析成 (cuda_idx, driver_idx)。

    'Auto (自动)' -> CUDA index 0 那张卡（ComfyUI 主卡，与旧默认一致）；
    'GPU N: <名>' -> CUDA index N 那张卡。
    无卡 / 解析失败 -> (0, 0)（引擎默认；驱动语义下单卡即 device 0）。
    """
    gpus = detect_gpus()
    cuda = 0
    if sel and sel.startswith('GPU '):
        try:
            cuda = int(sel.split(':')[0].replace('GPU ', '').strip())
        except Exception:
            cuda = 0
    for c, _, d in gpus:
        if c == cuda:
            return c, d
    # 没匹配到（无效选择 / 无卡）: 回退 CUDA 0（引擎默认，驱动语义下单卡即 device 0）
    for c, _, d in gpus:
        if c == 0:
            return 0, d
    return cuda, cuda

# 中间 h264 编码参数（NVENC constqp，近无损）。修改只改这一处。
NS_ENC = ('-c:v h264_nvenc -profile:v high -pix_fmt yuv420p -g 30 '
          '-preset p7 -tune hq -rc constqp -qp 18 -rc-lookahead 20 '
          '-spatial_aq 1 -aq-strength 15 -b:v 0 -bf 0')


def _node_ffmpeg():
    """节点自用 ffmpeg (写临时视频/读回输出): 固定用 bin/ 全功能版 (软解h264).
    bin171 是 1.7.1 引擎特供 (禁软解, 带 tvai_up), 不能用于节点读写."""
    cand = [_ENGINE / 'bin' / 'ffmpeg.exe', _ENGINE / 'bin171' / 'ffmpeg.exe',
            Path(r'D:\APP\ffmpeg\bin\ffmpeg.exe')]
    for c in cand:
        if c.is_file():
            return str(c), str(c.with_name('ffprobe.exe'))
    w = shutil.which('ffmpeg')
    if w:
        return w, w.replace('ffmpeg.exe', 'ffprobe.exe')
    raise RuntimeError('ffmpeg not found - install ffmpeg or put it on PATH')


def read_video_to_frames(path):
    """mp4 -> (B,H,W,3) uint8 RGB numpy array (一次读全帧, 方便但吃内存)."""
    import numpy as np
    ffmpeg, ffprobe = _node_ffmpeg()
    probe = subprocess.run([ffprobe, '-v', 'error', '-select_streams', 'v:0',
                            '-show_entries', 'stream=width,height',
                            '-of', 'csv=p=0', path],
                           capture_output=True, text=True)
    w, h = probe.stdout.strip().split(',')
    w, h = int(w), int(h)
    p = subprocess.run([ffmpeg, '-loglevel', 'error', '-i', path,
                        '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f'ffmpeg decode failed: {p.stderr.decode(errors="replace")[:300]}')
    raw = np.frombuffer(p.stdout, dtype=np.uint8)
    n = raw.size // (w * h * 3)
    return raw[: n * w * h * 3].reshape(n, h, w, 3)


def read_video_to_tensor(path, dtype=None, n_frames=None):
    """mp4 -> torch.Tensor (B,H,W,3), streaming with minimal peak memory.

    Pre-allocates the output tensor, then fills it frame-by-frame from an
    ffmpeg pipe.  Peak RAM ≈ output tensor size + 1 frame buffer (no double
    allocation like read_video_to_frames + astype, no list+stack bloat).

    dtype: 输出张量精度（float32 默认，float16 内存减半）。
    n_frames: 调用方已知输出帧数时传入，跳过 ffprobe -count_frames 的整段
    解码（该步对长视频等于多解一遍）。仅当帧数可靠已知时使用。
    """
    import numpy as np
    if dtype is None:
        dtype = torch.float32
    ffmpeg, ffprobe = _node_ffmpeg()
    # --- probe dimensions ---
    probe = subprocess.run([ffprobe, '-v', 'error', '-select_streams', 'v:0',
                            '-show_entries', 'stream=width,height',
                            '-of', 'csv=p=0', path], capture_output=True, text=True)
    w, h = probe.stdout.strip().split(',')
    w, h = int(w), int(h)
    frame_size = w * h * 3
    # --- count frames (已知帧数则跳过全量解码) ---
    if n_frames is None:
        cnt = subprocess.run([ffprobe, '-v', 'error', '-count_frames',
                              '-select_streams', 'v:0',
                              '-show_entries', 'stream=nb_read_frames',
                              '-of', 'csv=p=0', path],
                             capture_output=True, text=True, timeout=600)
        n_frames = int(cnt.stdout.strip().split(',')[0])
    if n_frames == 0:
        raise RuntimeError(f'No frames found in {path}')
    # --- pre-allocate output tensor ---
    out = torch.empty(n_frames, h, w, 3, dtype=dtype)
    # --- reusable buffers ---
    raw_buf = bytearray(frame_size)
    float_buf = np.empty((h, w, 3), dtype=np.float32)
    # --- stream frames ---
    p = subprocess.Popen([ffmpeg, '-loglevel', 'error', '-i', path,
                          '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                         stdout=subprocess.PIPE)
    mv = memoryview(raw_buf)
    for i in range(n_frames):
        pos = 0
        while pos < frame_size:
            n = p.stdout.readinto(mv[pos:])
            if n is None or n == 0:
                raise RuntimeError(f'Unexpected EOF at frame {i}, {pos}/{frame_size} bytes')
            pos += n
        # uint8 → float32 / 255, 零临时数组（casting='unsafe' 直接写 out，不再 astype 临时）
        np.multiply(np.frombuffer(raw_buf, dtype=np.uint8).reshape(h, w, 3),
                    1.0 / 255.0, out=float_buf, casting='unsafe')
        out[i].copy_(torch.from_numpy(float_buf))
    p.stdout.close()
    p.wait()
    return out
