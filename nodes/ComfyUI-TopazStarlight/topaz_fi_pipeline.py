# -*- coding: utf-8 -*-
"""Topaz ffmpeg-based frame interpolation pipeline (Apollo / Chronos).

Unlike the Starlight pipeline (which calls neuroserver.exe), the interpolation
models run as ffmpeg filters built into the Topaz 1.7.1 ffmpeg (tvai_fi). We
invoke bin171/ffmpeg.exe as a subprocess with -filter_complex.

Verified:
  - tvai_fi=model=apo-8:fps=60        (4s@30fps -> 239 frames, ~0.12x realtime)
  - Weights auto-download on first use (download=1) to TVAI_MODEL_DATA_DIR.

Layout:
  <ComfyUI>/
    topaz_engine/
      bin171/ffmpeg.exe               ← Topaz 1.7.1 ffmpeg with tvai_* filters
      tvmd/VR.lic                     ← license (shared with neuroserver)
"""
import os, re, subprocess, shutil, threading
from collections import deque
import torch
from pathlib import Path
from ._common import NS_ENC, _node_ffmpeg, read_video_to_frames, read_video_to_tensor, _env_no_cuda_limit

_ENGINE = Path(__file__).resolve().parent.parent.parent / 'topaz_engine'

# Supported frame interpolation models (all use tvai_fi; 4× 4帧→16帧)
# 模型名 = ModelManager 注册名 (ProgramData *.json 文件名):
#   apo-8 (Apollo), chr-2 (Chronos), aion-1 (Aion 光流补帧/去重复帧,
#   2026-08-30 GUI 实测 tvai_fi=model=aion-1 验证通过), apf-2, chf-3
FI_MODELS = {'apo-8': 'tvai_fi', 'chr-2': 'tvai_fi',
             'aion-1': 'tvai_fi', 'apf-2': 'tvai_fi',
             'chf-3': 'tvai_fi'}


def _topaz_ffmpeg():
    """Topaz 1.7.1 bundled ffmpeg (has tvai_* filters).

    优先级: 便携包 bin171 > 完整安装版。
    实测 (2026-08-29): bin171/ffmpeg.exe 与 D:/APP/topaz171/ffmpeg.exe
    sha256 完全一致（同一文件），用 GUI 同款参数 (-hwaccel cuvid +
    [0:v]hwdownload,format=nv12,format=rgb48le,...[vout] -map [vout])
    在 bin171 上验证成功 30fps->60fps (119帧)。
    关键坑: 用 -c:v h264_cuvid 明确指定解码器时, ffmpeg 会插入 auto_scale
    转换链导致 hwdownload 失败 ("Impossible to convert")。必须用 GUI 的
    -hwaccel cuvid 形式让 ffmpeg 自动选 cuvid 解码器。
    """
    cand = [
        _ENGINE / 'bin171' / 'ffmpeg.exe',       # 便携包 (自包含, 与安装版同 hash 验证可用)
        Path(r'D:\APP\topaz171\ffmpeg.exe'),     # 完整安装备份 (1.7.1)
        Path(r'D:\APP\topaz\ffmpeg.exe'),        # 完整安装 (1.6.1, 无 tvai_fi)
    ]
    for c in cand:
        if c.is_file():
            return str(c), str(c.with_name('ffprobe.exe'))
    w = shutil.which('ffmpeg')
    if w:
        return w, w.replace('ffmpeg.exe', 'ffprobe.exe')
    raise RuntimeError('未找到 Topaz 1.7.1 ffmpeg (D:/APP/topaz171/ffmpeg.exe 或 bin171)')


def _model_env():
    """Env vars for TVAI model location.

    TVAI_MODEL_DIR = metadata .json files (model registry)
    TVAI_MODEL_DATA_DIR = weight cache (auto-downloads here on first use)

    元数据优先引擎包自带 topaz_engine/models/model_meta（与 ProgramData
    逐文件 diff 一致），便携包完全自包含；引擎包没有才回退 ProgramData
    （需已装 Topaz）。
    """
    bundled_meta = _ENGINE / 'models' / 'model_meta'
    meta = bundled_meta if bundled_meta.is_dir() else Path(r'C:\ProgramData\Topaz Labs LLC\Topaz Video\models')
    data_dir = _ENGINE / 'models' / 'tvai_cache'
    data_dir.mkdir(parents=True, exist_ok=True)
    return {
        'TVAI_MODEL_DIR': str(meta),
        'TVAI_MODEL_DATA_DIR': str(data_dir),
        'TOPAZ_MODEL_STORE': str(meta),
    }


def write_video_frames(frames_uint8, fps, path, qp=14):
    """(B,H,W,3) uint8 RGB -> h264 nvenc mp4."""
    ffmpeg, _ = _node_ffmpeg()
    b, h, w, _ = frames_uint8.shape
    cmd = [ffmpeg, '-y', '-loglevel', 'error',
           '-f', 'rawvideo', '-pix_fmt', 'rgb24',
           '-s', f'{w}x{h}', '-r', str(fps), '-i', '-',
           '-c:v', 'h264_nvenc', '-preset', 'p7', '-tune', 'hq',
           '-rc', 'constqp', '-qp', str(qp), '-pix_fmt', 'yuv420p', path]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    # memoryview 零复制直写 pipe，避免 tobytes() 全量副本（大视频时省一份完整内存）
    _, err = p.communicate(input=memoryview(frames_uint8))
    if p.returncode != 0:
        raise RuntimeError(f'ffmpeg encode failed: {err.decode(errors="replace")[:300]}')


_FRAME_RE = re.compile(r'frame=\s*(\d+)')


def _consume(proc, log, on_progress, est_total):
    """消费 ffmpeg stdout, 解析 frame= 统计行做进度回调 (0-99, 收尾交给调用方)."""
    for line in proc.stdout:
        line = line.rstrip()
        if on_progress is not None and est_total > 0:
            m = _FRAME_RE.search(line)
            if m:
                done = int(m.group(1))
                # ffmpeg 在完成前会打出最终帧数, 钳到 99 等收尾跳到 100
                on_progress(min(99, int(done * 100 / est_total)))
        if any(k in line for k in ['tvai_fi', 'License', 'Model', 'Error',
                                    'error', 'Output #', 'Stream #',
                                    'frame=']):
            log('  [Topaz-FI] ' + line)
    proc.wait()


def _consume_binary(proc, log, on_progress, est_total, tail=None):
    """同 _consume，但面向 binary stdout（raw 直通路径需写 bytes stdin，
    因此不能用 text=True，只能 binary 读再手动 decode）。

    tail: 可选 deque(maxlen=N)，把所有行存进去，报错时带出来给用户看。
    """
    for raw in proc.stdout:
        line = raw.decode('utf-8', errors='replace').rstrip()
        if tail is not None:
            tail.append(line)
        if on_progress is not None and est_total > 0:
            m = _FRAME_RE.search(line)
            if m:
                done = int(m.group(1))
                on_progress(min(99, int(done * 100 / est_total)))
        if any(k in line for k in ['tvai_fi', 'License', 'Model', 'Error',
                                    'error', 'failed', 'Failed', 'cannot',
                                    'Cannot', 'unable', 'Unable', 'GPU', 'CUDA',
                                    'device', 'license', 'Output #', 'Stream #',
                                    'frame=']):
            log('  [Topaz-FI] ' + line)
    proc.wait()


def run_topaz_fi(in_path, out_path, fps_in, fps_out, model='apo-8',
                 slowmo=1.0, device=1, vram=1.0, instances=1,
                 log=print, on_progress=None, frames_in=None,
                 remove_dup=True, sensitivity=0.01, scene_detect=False):
    """Run ffmpeg with tvai_fi filter on a video file (frame interpolation only).

    Args:
      in_path, out_path: input/output mp4 paths
      fps_in, fps_out: input/output frame rate (fps_out > fps_in = interpolate)
      model: frame interp model short name ('apo-8' default, 'chr-2' backup)
      slowmo: slow motion factor (1.0 = no slowmo, 2.0 = output plays 2x slower
              = output duration is 2x input duration, more generated frames)
      device: GPU index (1 = first NVIDIA GPU, -2 = auto)
      vram: max VRAM usage (0.1-1.0)
      instances: parallel model instances (0-3, more = faster on big GPU)
    """
    if model not in FI_MODELS:
        raise RuntimeError(f'不支持的插帧模型: {model}, 可选: {list(FI_MODELS)}')
    if fps_out < fps_in:
        raise RuntimeError(f'输出帧率({fps_out})不能小于输入帧率({fps_in})')
    # 输出==输入 且 不慢放 无意义；slowmo>1 时允许纯慢动作（帧率不变，时长翻倍）
    if fps_out == fps_in and slowmo <= 1.0:
        raise RuntimeError(f'输出帧率需大于输入帧率，或 slowmo>1 做纯慢动作')

    ffmpeg, _ = _topaz_ffmpeg()
    env = _env_no_cuda_limit()
    env['PATH'] = str(Path(ffmpeg).parent) + os.pathsep + env.get('PATH', '')
    env.update(_model_env())

    rdt_val = sensitivity if remove_dup else 0.0
    fi_filter = (f'tvai_fi=model={model}:slowmo={slowmo}:rdt={rdt_val}:fps={fps_out}:'
                 f'device={device}:vram={vram}:instances={instances}')
    if scene_detect:
        # 场景切换阈值: 硬切镜头处不做去重/插帧误判 (videoai.dll 通用选项)
        fi_filter += ':parameters=scene_change_threshold=1'
    # 完全照抄 GUI 1.7.1 实际调用行 (显式标签格式, GUI 第二命令这么写):
    #   -hwaccel cuvid                        (NVIDIA 硬解, 1.7.1 禁了 h264 软解)
    #   -sws_flags spline+accurate_rnd+full_chroma_int   (高质量缩放)
    #   -filter_complex [0:v]hwdownload,format=nv12,format=rgb48le,tvai_fi=...[vout]
    #   -map [vout]
    # NS_ENC 是空格分隔的字符串，必须 split 成参数列表——否则 Popen 会当
    # 一个参数传给 ffmpeg（bash 自动分词所以 shell 成功，Popen 列表不会）
    cmd = [ffmpeg, '-y', '-stats_period', '5', '-loglevel', 'info', '-nostdin',
           '-hwaccel', 'cuvid',
           '-sws_flags', 'spline+accurate_rnd+full_chroma_int',
           '-i', in_path,
           '-filter_complex',
           f'[0:v]hwdownload,format=nv12,format=rgb48le,{fi_filter}[vout]',
           '-map', '[vout]',
           *NS_ENC.split(),
           out_path]

    log(f'  [Topaz-FI] ffmpeg: {ffmpeg}')
    log(f'  [Topaz-FI] 模型: {model}')
    log(f'  [Topaz-FI] 帧率: {fps_in} -> {fps_out} (slowmo={slowmo})')
    log(f'  [Topaz-FI] GPU device={device}, vram={vram}, instances={instances}')
    log(f'  [Topaz-FI] 缓存目录: {env["TVAI_MODEL_DATA_DIR"]}')

    # 预计输出帧数（进度条基准）：输入帧数 / 输入帧率 × 慢动作 × 输出帧率
    if frames_in is None:
        # 调用方没给输入帧数时用 ffprobe 数帧 (mp4 的 nb_frames 常为 N/A,
        # -count_frames 真解码计数, 慢但对短测试片段可接受)
        try:
            _fp = _node_ffmpeg()[1]
            r = subprocess.run([_fp, '-v', 'error', '-count_frames',
                                '-select_streams', 'v:0',
                                '-show_entries', 'stream=nb_read_frames',
                                '-of', 'csv=p=0', in_path],
                               capture_output=True, text=True, timeout=120)
            frames_in = int(r.stdout.strip().split(',')[0])
        except Exception:
            frames_in = 0
    est_total = int(round(frames_in / fps_in * slowmo * fps_out)) if fps_in else 0
    if est_total <= 0:
        est_total = 1
    proc = subprocess.Popen(cmd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace')
    _consume(proc, log, on_progress, est_total)
    # 0xC0000005 (3221225477) = Apollo tile JIT/TRT 首次现场编译偶发段错误。
    # 实测 (2026-08-29): 首次碰到某分辨率 tile 时崩，权重/JIT 缓存就绪后
    # 重跑即稳定。自动重试一次。
    if proc.returncode == 3221225477:
        log('  [Topaz-FI] 首次编译崩溃 (0xC0000005)，自动重试一次 ...')
        proc = subprocess.Popen(cmd, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace')
        _consume(proc, log, on_progress, est_total)
    if proc.returncode != 0:
        raise RuntimeError(f'ffmpeg 失败 (exit {proc.returncode})')
    if not os.path.isfile(out_path):
        raise RuntimeError('ffmpeg 未产生输出文件')


def run_topaz_fi_raw(frames_uint8, fps_in, out_path, fps_out, model='apo-8',
                     slowmo=1.0, device=1, vram=1.0, instances=1,
                     log=print, on_progress=None,
                     remove_dup=True, sensitivity=0.01, scene_detect=False):
    """raw 帧直通 tvai_fi（免中间 h264 文件）。

    相比 run_topaz_fi（写 mp4 -> cuvid 硬解 -> tvai_fi）：
      - 输入 raw 帧直接写 stdin，省一次 NVENC 编码 + 一次解码 + 一代有损
      - filter 前缀用软件输入 [0:v]format=rgb48le，不经过 hwdownload/cuvid 链
      - 注意：不能带 -nostdin（stdin 是数据通道），stdin 用完即 close

    首次碰到新分辨率 tile 时偶发 0xC0000005 段错误（现场编译），自动重试一次；
    重试时需重新把 raw 帧喂回 stdin。
    """
    if model not in FI_MODELS:
        raise RuntimeError(f'不支持的插帧模型: {model}, 可选: {list(FI_MODELS)}')
    if fps_out < fps_in:
        raise RuntimeError(f'输出帧率({fps_out})不能小于输入帧率({fps_in})')
    # 输出==输入 且 不慢放 无意义；slowmo>1 时允许纯慢动作（帧率不变，时长翻倍）
    if fps_out == fps_in and slowmo <= 1.0:
        raise RuntimeError(f'输出帧率需大于输入帧率，或 slowmo>1 做纯慢动作')

    b, h, w, _ = frames_uint8.shape
    ffmpeg, _ = _topaz_ffmpeg()
    env = _env_no_cuda_limit()
    env['PATH'] = str(Path(ffmpeg).parent) + os.pathsep + env.get('PATH', '')
    env.update(_model_env())

    rdt_val = sensitivity if remove_dup else 0.0
    fi_filter = (f'tvai_fi=model={model}:slowmo={slowmo}:rdt={rdt_val}:fps={fps_out}:'
                 f'device={device}:vram={vram}:instances={instances}')
    if scene_detect:
        fi_filter += ':parameters=scene_change_threshold=1'
    cmd = [ffmpeg, '-y', '-stats_period', '5', '-loglevel', 'info',
           '-f', 'rawvideo', '-pix_fmt', 'rgb24',
           '-s', f'{w}x{h}', '-r', str(fps_in), '-i', '-',
           '-filter_complex',
           f'[0:v]format=rgb48le,{fi_filter}[vout]',
           '-map', '[vout]',
           *NS_ENC.split(),
           out_path]

    log(f'  [Topaz-FI] raw直通 ffmpeg: {ffmpeg}')
    log(f'  [Topaz-FI] 模型: {model}')
    log(f'  [Topaz-FI] 帧率: {fps_in} -> {fps_out} (slowmo={slowmo})')
    log(f'  [Topaz-FI] GPU device={device}, vram={vram}, instances={instances}')
    log(f'  [Topaz-FI] 缓存目录: {env["TVAI_MODEL_DATA_DIR"]}')

    est_total = int(round(b / fps_in * slowmo * fps_out)) if fps_in else 1
    if est_total <= 0:
        est_total = 1
    tail = deque(maxlen=40)  # 保存 ffmpeg 最后 40 行，报错时带出来

    def _feed_and_drain(p):
        """边喂 stdin 边消费 stdout（reader 线程），彻底避免双管道死锁。

        旧实现: Popen 后立刻 p.stdin.write(整个视频) —— 管道写满后 write
        阻塞，_consume_binary 永远不执行 → ffmpeg 的 stdout(stats/日志)
        无人读取 → ffmpeg 阻塞 → 三方互相等待死锁（症状: CPU 0%、输出
        文件不增长、进度条不动、跑上千秒不完）。

        新实现:
          1. stdout 由后台 reader 线程从进程一启动就持续消费（防止 ffmpeg
            写 stdout 缓冲满而阻塞）；
          2. stdin 分块写入（8MiB/块），配合 reader 保持 backpressure 顺畅，
            不会因单次 write 整个大数组而长时间卡死。
        """
        reader = threading.Thread(
            target=_consume_binary, args=(p, log, on_progress, est_total, tail),
            daemon=True)
        reader.start()
        try:
            mv = memoryview(frames_uint8)
            CHUNK = 8 * 1024 * 1024
            for i in range(0, len(mv), CHUNK):
                p.stdin.write(mv[i:i + CHUNK])
        except BrokenPipeError:
            pass
        finally:
            try:
                p.stdin.close()
            except Exception:
                pass
        reader.join()

    proc = subprocess.Popen(cmd, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    _feed_and_drain(proc)
    # 0xC0000005 = Apollo tile JIT/TRT 首次现场编译偶发段错误，自动重试一次
    if proc.returncode == 3221225477:
        log('  [Topaz-FI] 首次编译崩溃 (0xC0000005)，自动重试一次 ...')
        tail.clear()
        proc = subprocess.Popen(cmd, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        _feed_and_drain(proc)
    if proc.returncode != 0:
        tail_text = '\n'.join(tail)
        raise RuntimeError(
            f'ffmpeg 失败 (exit {proc.returncode})\n'
            f'--- ffmpeg 最后输出 ---\n{tail_text}\n'
            f'--- 命令行 ---\n{" ".join(cmd)}')
    if not os.path.isfile(out_path):
        raise RuntimeError('ffmpeg 未产生输出文件')


def estimate_output_frames(b, fps_in, fps_out, slowmo):
    """Rough estimate of output frame count.

    Output duration = input duration * slowmo, so:
      output_frames = (b / fps_in) * slowmo * fps_out

    Apollo's actual count may differ due to duplicate-frame detection (rdt).
    """
    return int(round((b / fps_in) * slowmo * fps_out))
