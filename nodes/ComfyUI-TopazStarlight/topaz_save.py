# -*- coding: utf-8 -*-
"""Topaz 视频输出节点：比 VHS VideoCombine 快数倍的保存节点。

为什么快：
  VHS 是 Python 逐帧 tensor->bytes + 逐帧写管道，704x576 只有 ~53-69fps
  （瓶颈在 Python 侧逐帧 numpy 转换，不在编码器）。
  本节点一次性整批把 IMAGE 转成 uint8，再按 16MiB 大块写 ffmpeg 管道，
  配合 GPU h264_nvenc 编码，704x576 可达数百 fps（实测 588fps）。

  - 优先用引擎自带 ffmpeg（分享给别人时引擎自带，不依赖系统 ffmpeg）
  - GPU 编码不可用时自动回退 CPU libx264
  - 支持可选音频输入（AUDIO），编码后自动合并
"""
import os
import subprocess
from datetime import datetime

import numpy as np
import torch

from ._common import _node_ffmpeg

try:
    from comfy.utils import ProgressBar
except Exception:
    ProgressBar = None
try:
    import folder_paths  # ComfyUI 根目录平铺结构（官方/portable 通用）
except ImportError:
    try:
        from comfy import folder_paths
    except ImportError:
        folder_paths = None

_CHUNK = 16 * 1024 * 1024  # 16MiB


def _pick_ffmpeg():
    """节点自用 ffmpeg（引擎 bin/ 全功能版优先，支持 nvenc）。"""
    return _node_ffmpeg()[0]


def _supports(ff, enc):
    try:
        r = subprocess.run([ff, '-hide_banner', '-encoders'],
                           capture_output=True, text=True, timeout=20)
        return enc in (r.stdout + r.stderr)
    except Exception:
        return False


def _choose_encoder(ff, want_gpu):
    """按用户选择 + 实际能力决定编码器与参数。返回 (encoder_key, args)。"""
    if want_gpu and _supports(ff, 'h264_nvenc'):
        return 'gpu', ['-c:v', 'h264_nvenc', '-pix_fmt', 'yuv420p',
                       '-preset', 'p7', '-rc', 'vbr', '-cq', '21',
                       '-b:v', '0']
    # CPU libx264（libx264 一般都有）
    return 'cpu', ['-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                   '-crf', '19', '-preset', 'medium']


class TopazVideoOutput:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图像": ("IMAGE",),
                "帧率": ("INT", {"default": 30, "min": 1, "max": 240,
                                 "tooltip": "输出视频帧率"}),
                "输出目录": ("STRING", {"default": "TopazStarlight",
                                       "tooltip": "存到 ComfyUI/output/{此目录}/ 下，可含子路径如 项目/片段"}),
                "日期子文件夹": ("BOOLEAN", {"default": True,
                                          "tooltip": "开启则自动建当天子文件夹 output/{输出目录}/{2026-09-03}/"}),
                "文件名前缀": ("STRING", {"default": "Topaz",
                                         "tooltip": "文件名前缀，如 Topaz_20260903_153045.mp4"}),
                "命名": (["前缀+时间戳", "自动编号"],
                         {"default": "前缀+时间戳",
                          "tooltip": "前缀+时间戳=每次唯一不覆盖；自动编号=前缀_00001 递增"}),
                "编码": (["GPU (h264_nvenc 快)", "CPU (libx264 兼容)"],
                         {"default": "GPU (h264_nvenc 快)"}),
            },
            "optional": {
                "音频": ("AUDIO",),
            },
        }

    RETURN_TYPES = ()
    OUTPUT_NODE = True
    FUNCTION = "save"
    CATEGORY = "Topaz Starlight"

    def save(self, 图像, 帧率, 输出目录="TopazStarlight", 日期子文件夹=True,
             文件名前缀="Topaz", 命名="前缀+时间戳", 编码="GPU (h264_nvenc 快)", 音频=None):
        if 图像 is None or (isinstance(图像, torch.Tensor) and 图像.size(0) == 0):
            return {}

        t = 图像.detach()
        if t.dtype != torch.float32 and t.dtype != torch.float16:
            t = t.float()
        if t.is_cuda:
            t = t.cpu()

        n, h, w, c = t.shape
        if c not in (3, 4):
            raise ValueError(f'TopazVideoOutput: 通道数 {c} 不支持（需 RGB 3 或 RGBA 4）')

        # RGB 输入用 rgb24；RGBA 用 rgba
        pix = 'rgba' if c == 4 else 'rgb24'

        ff = _pick_ffmpeg()
        enc_key, enc_args = _choose_encoder(ff, 编码.startswith('GPU'))

        # 输出路径：output/{输出目录}[/{YYYY-MM-DD}]/
        if folder_paths is not None:
            output_dir = folder_paths.get_output_directory()
        else:
            output_dir = os.path.join(os.getcwd(), 'output')
        out_dir = output_dir
        if 输出目录:
            out_dir = os.path.join(out_dir, 输出目录.strip('/\\'))
        if 日期子文件夹:
            out_dir = os.path.join(out_dir, datetime.now().strftime('%Y-%m-%d'))
        os.makedirs(out_dir, exist_ok=True)

        # 命名
        base = 文件名前缀.strip()
        if not base:
            base = 'Topaz'
        if 命名 == "前缀+时间戳":
            out_mp4 = os.path.join(out_dir, f'{base}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.mp4')
        else:
            existing = [f for f in os.listdir(out_dir)
                        if f.startswith(base + '_') and f.endswith('.mp4')]
            counter = 0
            for f in existing:
                try:
                    counter = max(counter, int(f[len(base) + 1:-4]))
                except ValueError:
                    pass
            out_mp4 = os.path.join(out_dir, f'{base}_{counter + 1:05}.mp4')

        # ffmpeg 命令：rawvideo -> 编码器 -> mp4
        args = [ff, '-v', 'error', '-f', 'rawvideo', '-pix_fmt', pix,
                '-color_range', 'pc', '-colorspace', 'rgb',
                '-color_primaries', 'bt709', '-color_trc', 'iec61966-2-1',
                '-s', f'{w}x{h}', '-r', str(帧率), '-i', '-'] \
            + enc_args \
            + ['-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2:out_color_matrix=bt709',
               '-color_range', 'tv', '-colorspace', 'bt709',
               '-color_primaries', 'bt709', '-color_trc', 'bt709',
               '-movflags', '+faststart', '-y', out_mp4]

        total_frames = n
        if ProgressBar is not None:
            pbar = ProgressBar(total_frames)
        else:
            pbar = None

        # 分批转换 + 直接写管道（每批 ~256 帧，内存友好；避免整批大数组
        # 与 tobytes 双倍拷贝。用 torch 转换：numpy 对 float16 算术无 SIMD
        # 优化极慢，先 .float() 提升再 *255 -> uint8 走 torch 快速路径）
        BATCH = 256
        with subprocess.Popen(args, stdin=subprocess.PIPE,
                              stderr=subprocess.PIPE) as proc:
            for start in range(0, total_frames, BATCH):
                batch = t[start:start + BATCH]
                arr = batch.float().clamp_(0.0, 1.0).mul_(255.0).round_().to(torch.uint8).numpy()
                proc.stdin.write(arr.tobytes())
                if pbar is not None:
                    pbar.update(len(batch))
            proc.stdin.close()
            err = proc.stderr.read()
            proc.wait()
        if proc.returncode != 0:
            try:
                os.remove(out_mp4)
            except OSError:
                pass
            raise RuntimeError('TopazVideoOutput ffmpeg 失败:\n'
                               + err.decode(errors='replace')[-800:])

        # 合并音频
        if 音频 is not None:
            out_mp4 = self._mux_audio(ff, out_mp4, 音频)

        # 供前端预览：计算相对 output 目录的 filename/subfolder
        preview = None
        if folder_paths is not None:
            try:
                rel = os.path.relpath(out_mp4, folder_paths.get_output_directory())
                rel = rel.replace('\\', '/')
                sf, fn = os.path.split(rel)
                preview = [{"filename": fn, "subfolder": sf,
                            "type": "output", "format": "video/mp4",
                            "fullpath": out_mp4}]
            except Exception:
                preview = None

        ui = {"ui": {"text": [f'已保存: {out_mp4} (编码器 {enc_key}, {n} 帧 {w}x{h}@{帧率}fps)']}}
        if preview:
            ui["ui"]["topaz_video"] = preview
        return ui

    @staticmethod
    def _mux_audio(ff, video_path, 音频):
        try:
            wf = 音频['waveform']  # [1, channels, samples]
            sr = int(音频['sample_rate'])
        except Exception:
            return video_path
        if wf is None or wf.numel() == 0:
            return video_path
        wf = wf.detach().cpu().float()
        if wf.dim() == 3:
            wf = wf.squeeze(0)
        channels = wf.size(0)
        audio_data = wf.transpose(0, 1).contiguous().numpy().tobytes()

        out_path = video_path[:-4] + '-audio.mp4'
        mux_args = [ff, '-v', 'error', '-y', '-i', video_path,
                    '-ar', str(sr), '-ac', str(channels),
                    '-f', 'f32le', '-i', '-',
                    '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                    '-shortest', out_path]
        try:
            r = subprocess.run(mux_args, input=audio_data,
                               capture_output=True, timeout=600)
            if r.returncode == 0 and os.path.isfile(out_path):
                os.remove(video_path)
                return out_path
            print(f'[TopazVideoOutput] 音频合并失败 rc={r.returncode}: '
                  + r.stderr.decode(errors='replace')[-400:])
        except Exception as e:
            print(f'[TopazVideoOutput] 音频合并异常: {e}')
        return video_path
