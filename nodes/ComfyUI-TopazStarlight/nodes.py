# -*- coding: utf-8 -*-
"""ComfyUI nodes: Topaz Starlight + Frame Interpolation.

Two engines:
  1. TopazStarlight          — calls neuroserver.exe (slp-26, Astra family)
  2. TopazFrameInterpolation — calls Topaz 1.7.1 ffmpeg (tvai_fi filter,
                                Apollo / Chronos frame interpolation)

All input names in Chinese for readability.
"""
import os, tempfile, time, uuid
import numpy as np
import torch
from .topaz_pipeline import write_frames_to_video, run_starlight, read_video_to_tensor
from .topaz_fi_pipeline import (run_topaz_fi_raw,
                                read_video_to_tensor as _read_fi_tensor)
from ._common import gpu_choices, resolve_gpu
from .topaz_save import TopazVideoOutput


# GPU 下拉选项缓存（首次检测一次，避免每次刷新节点都调 nvidia-smi）
_GPU_OPTIONS = None
def _gpu_options():
    global _GPU_OPTIONS
    if _GPU_OPTIONS is None:
        _GPU_OPTIONS = gpu_choices()
    return _GPU_OPTIONS


def _cleanup_stale_temp(d=None, max_age=3600):
    """清理历史崩溃残留的临时视频文件。

    阈值 max_age（秒，默认 1 小时）防止误删正在进行的任务文件。
    d 为 None 时用 ComfyUI temp 目录（fallback 系统 temp）。
    模块导入时执行一次（ComfyUI 启动即清）。
    """
    if d is None:
        try:
            import folder_paths
            d = folder_paths.get_temp_directory()
        except Exception:
            try:
                from comfy import folder_paths
                d = folder_paths.get_temp_directory()
            except Exception:
                d = tempfile.gettempdir()
    if not d or not os.path.isdir(d):
        return 0
    now = time.time()
    pats = ('topaz_in_', 'topaz_out_', 'topazfi_in_', 'topazfi_out_')
    removed = 0
    for name in os.listdir(d):
        if not name.startswith(pats):
            continue
        p = os.path.join(d, name)
        try:
            if os.path.isfile(p) and (now - os.path.getmtime(p)) > max_age:
                os.remove(p)
                removed += 1
        except OSError:
            pass
    return removed


_cleanup_stale_temp()


class TopazStarlight:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图像": ("IMAGE",),
                "模型": (["星光 2.6", "Astra", "Astra HQ", "Astra Fast", "Astra Sharp"],
                         {"default": "星光 2.6",
                          "tooltip": "星光2.6=1.7.1引擎(官方换代后的星光)；Astra系列需≥9帧；Fast需Intel核显"}),
                "显存上限": ("FLOAT", {"default": 96.0, "min": 4.0, "max": 96.0, "step": 0.1,
                                     "tooltip": "神经服务器显存上限(GiB，可小数)。默认96=不设限（分享通用，引擎按需分配）。个人按卡调：16G卡建议14（防驱动借共享内存变慢）；吃紧降到12/10；低于8可能offload到CPU超慢"}),
                "放大倍数": ("FLOAT", {"default": 2.00, "min": 1.00, "max": 4.00, "step": 0.01,
                                       "tooltip": "输出放大倍数：1=不放大只增强；支持两位小数如 1.78/2.67（参考表里的精确倍率）；内部对齐到偶数像素"}),
                "帧率": ("INT", {"default": 24, "min": 1, "max": 120,
                                 "tooltip": "和视频实际帧率一致"}),
                "增强强度": ("FLOAT", {"default": 1.0, "min": 0.5, "max": 1.5, "step": 0.1,
                                       "tooltip": "细节增强力度：0.7柔和 / 1.0默认 / 1.3细节最猛"}),
                "输入质量": ("INT", {"default": 14, "min": 0, "max": 40,
                                     "tooltip": "输入编码质量（NVENC constqp，越小越无损）"}),
                "GPU选择": (_gpu_options(), {"default": _gpu_options()[0],
                                       "tooltip": "自动识别多显卡：Auto=ComfyUI 主卡(cuda:0，与旧默认一致)；可手动选任意一张。超分用 CUDA 枚举(本机 cuda:0=4070TiS, cuda:1=4060Ti)"}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},  # ComfyUI 注入节点 id，用于进度条绑定
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("图像",)
    FUNCTION = "run"
    CATEGORY = "Topaz"

    def run(self, 图像, 模型, 显存上限, 放大倍数, 帧率, 增强强度, 输入质量, GPU选择="Auto (自动)", unique_id=None):
        b, h, w, c = 图像.shape
        if c != 3:
            raise ValueError(f'TopazStarlight expects RGB images, got {c} channels')
        # 输入转换: 只留 1 份 float32 临时 + 1 份 uint8（原链 mul+clamp+astype 是 2 份 float32 临时）
        # to(uint8) 与 astype 同为截断语义，行为与原版一致
        frames = 图像.mul(255.0).clamp_(0, 255).to(torch.uint8).cpu().numpy()

        # 实时进度条: 绑定到本节点 (ComfyUI 前端显示)
        try:
            from comfy.utils import ProgressBar
            pbar = ProgressBar(total=b, node_id=unique_id)
        except Exception:
            pbar = None

        def _tick(pct):
            if pbar is not None:
                pbar.update_absolute(int(b * pct / 100))

        MODEL_IDS = {"星光 2.6": "slp-26",   # 1.7.1 官方换代: 星光即 2.6 权重
                     "Astra": "astra", "Astra HQ": "astrahq",
                     "Astra Fast": "astrafast", "Astra Sharp": "astrasharp"}
        model_id = MODEL_IDS.get(模型, "slp-26")

        tag = uuid.uuid4().hex[:10]
        # 临时文件放 ComfyUI/temp（与输出同盘，避免跨盘读写），fallback 系统 temp
        try:
            import folder_paths
            tmp = folder_paths.get_temp_directory()
        except Exception:
            try:
                from comfy import folder_paths
                tmp = folder_paths.get_temp_directory()
            except Exception:
                tmp = tempfile.gettempdir()
        in_video = os.path.join(tmp, f'topaz_in_{tag}.mp4')
        out_video = os.path.join(tmp, f'topaz_out_{tag}.mp4')
        # GPU选择 -> CUDA 设备号（超分引擎用 CUDA 枚举；Auto=引擎默认 0）
        gpu_cuda, _ = resolve_gpu(GPU选择)
        device = None if GPU选择 == 'Auto (自动)' else gpu_cuda
        try:
            print(f'[TopazStarlight] {模型} {b}帧 {w}x{h} -> {w*放大倍数}x{h*放大倍数} (x{放大倍数}) 强度{增强强度} fps={帧率} GPU={"Auto" if device is None else str(device)}')
            write_frames_to_video(frames, 帧率, in_video, 输入质量)
            run_starlight(in_video, out_video, 放大倍数, b, w, h, 增强强度, model_id, max_gpu_mem=显存上限, on_progress=_tick, device=device)
            # 流式读取: 预分配 tensor + 逐帧填入，峰值 ≈ 输出 tensor 本身
            # n_frames=b 跳过 ffprobe 整段解码数帧（超分输出帧数=输入帧数）
            tensor = read_video_to_tensor(out_video, n_frames=b)
            if pbar is not None:
                pbar.update_absolute(b)  # 收尾 100%
            print(f'[TopazStarlight] 完成: {tensor.shape[0]}帧 @ '
                  f'{tensor.shape[2]}x{tensor.shape[1]}')
        finally:
            for f in (in_video, out_video):
                try:
                    if os.path.isfile(f):
                        os.remove(f)
                except OSError:
                    pass

        return (tensor,)


class TopazFrameInterpolation:
    """Topaz Apollo 插帧节点：把视频从 X fps 提升到 Y fps（24→60 慢动作）。

    走 Topaz 1.7.1 ffmpeg 的 tvai_fi 滤镜（不用神经服务器）。
    权重首次运行时自动从 CDN 下载，之后走本地缓存。
    """
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图像": ("IMAGE",),
                "模型": (["Apollo (推荐)", "Aion 补帧", "Chronos"],
                         {"default": "Apollo (推荐)",
                          "tooltip": "Apollo=官方插帧主力(质量高)；Aion=光流补帧(去帧率提升+去重复帧，GUI 星光输出首选)；Chronos=老版备选"}),
                "输入帧率": ("INT", {"default": 24, "min": 1, "max": 120,
                                     "tooltip": "视频原始帧率，和 VHS_LoadVideo 一致"}),
                "输出帧率": ("INT", {"default": 60, "min": 2, "max": 240,
                                      "tooltip": "插帧后的目标帧率，必须大于输入帧率"}),
                "慢动作倍数": ("FLOAT", {"default": 1.0, "min": 1.0, "max": 8.0, "step": 0.1,
                                          "tooltip": "1.0=不慢放；2.0=慢动作2倍(时长翻倍)；做慢动作时才>1"}),
                "并行实例": ("INT", {"default": 2, "min": 0, "max": 3,
                                     "tooltip": "同卡多实例并行加速。实测(2026-08-29): inst=2 比 inst=1 快~31%(16G卡够)。默认已设为2。注意:双卡并列(device=0.1)当前会崩,勿用"}),
                "显存占用": ("FLOAT", {"default": 0.85, "min": 0.1, "max": 1.0, "step": 0.05,
                                       "tooltip": "插帧模型显存占用比例。0.85 留余量，防止与 ComfyUI 主流程/超分节点抢显存；独占跑可拉到 1.0"}),
                "重复帧检测": ("BOOLEAN", {"default": True,
                    "tooltip": "检测并替换重复帧(rdt>0)，GUI 同名选项默认开；关=不去重(rdt 0)"}),
                "重复帧敏感性": ("INT", {"default": 5, "min": 1, "max": 100,
                    "tooltip": "敏感度 1-100（≈GUI 同名滑块），越大检出越多重复帧。内部 rdt=0.2×s/100：5=GUI默认(0.01)，100=最强(0.2)"}),
                "场景检测": ("BOOLEAN", {"default": False,
                    "tooltip": "场景切换阈值(scene_change_threshold)：硬切镜头处不做去重/插帧误判，多镜头素材建议开"}),
                "输入质量": ("INT", {"default": 14, "min": 0, "max": 40,
                                      "tooltip": "输入编码质量(NVENC constqp，越小越无损)"}),
                "GPU选择": (_gpu_options(), {"default": _gpu_options()[0],
                                       "tooltip": "自动识别多显卡：Auto=ComfyUI 主卡(cuda:0，本机对应驱动枚举1=4070TiS，与旧默认一致)；手动选任意单卡。插帧用驱动枚举，内部已自动换算(本机 GPU0=4060Ti, GPU1=4070TiS)"}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("图像",)
    FUNCTION = "run"
    CATEGORY = "Topaz"

    def run(self, 图像, 模型, 输入帧率, 输出帧率, 慢动作倍数, 并行实例, 显存占用,
            重复帧检测, 重复帧敏感性, 场景检测, 输入质量, GPU选择="Auto (自动)", unique_id=None):
        b, h, w, c = 图像.shape
        if c != 3:
            raise ValueError(f'TopazFrameInterpolation expects RGB images, got {c} channels')
        if 输出帧率 < 输入帧率:
            raise ValueError(f'输出帧率({输出帧率})不能小于输入帧率({输入帧率})')
        # 输出==输入 且 不慢放 无意义；慢动作倍数>1 时允许纯慢动作（时长翻倍，帧率不变）
        if 输出帧率 == 输入帧率 and 慢动作倍数 <= 1.0:
            raise ValueError(f'输出帧率({输出帧率})需大于输入帧率({输入帧率})，'
                             '或把慢动作倍数设为>1做纯慢动作')
        if b < 3:
            raise ValueError(f'插帧至少需要 3 帧输入 (当前 {b} 帧)')

        # 输入转换: 只留 1 份 float32 临时 + 1 份 uint8（原链 mul+clamp+astype 是 2 份 float32 临时）
        # to(uint8) 与 astype 同为截断语义，行为与原版一致
        frames = 图像.mul(255.0).clamp_(0, 255).to(torch.uint8).cpu().numpy()

        MODEL_IDS = {"Apollo (推荐)": "apo-8", "Aion 补帧": "aion-1", "Chronos": "chr-2"}
        model_id = MODEL_IDS.get(模型, "apo-8")

        # 预计输出帧数（用于进度条）
        est_out = int(round((b / 输入帧率) * 慢动作倍数 * 输出帧率))
        print(f'[Topaz-FI] {模型} {b}帧 {w}x{h} @ {输入帧率}fps -> {输出帧率}fps '
              f'(slowmo={慢动作倍数}, 预计~{est_out}帧)')

        try:
            from comfy.utils import ProgressBar
            pbar = ProgressBar(total=est_out, node_id=unique_id)
        except Exception:
            pbar = None

        def _tick(pct):
            if pbar is not None:
                pbar.update_absolute(int(est_out * pct / 100))

        tag = uuid.uuid4().hex[:10]
        # 临时文件放 ComfyUI/temp（与输出同盘，避免跨盘读写），fallback 系统 temp
        try:
            import folder_paths
            tmp = folder_paths.get_temp_directory()
        except Exception:
            try:
                from comfy import folder_paths
                tmp = folder_paths.get_temp_directory()
            except Exception:
                tmp = tempfile.gettempdir()
        out_video = os.path.join(tmp, f'topazfi_out_{tag}.mp4')
        # GPU选择 -> 驱动设备号（插帧 tvai_fi 用驱动枚举；Auto=CUDA 0 卡对应驱动号）
        _, gpu_driver = resolve_gpu(GPU选择)
        try:
            # raw 直通: 免写中间 h264（省一次编码+解码+一代有损）
            run_topaz_fi_raw(frames, 输入帧率, out_video, 输出帧率,
                             model=model_id, slowmo=慢动作倍数, instances=并行实例,
                             device=gpu_driver, vram=显存占用, on_progress=_tick,
                             remove_dup=重复帧检测, sensitivity=0.2 * 重复帧敏感性 / 100,
                             scene_detect=场景检测)
            # 流式读取: 预分配 tensor + 逐帧填入，峰值 ≈ 输出 tensor 本身
            # float16: 峰值内存减半（7473帧@704x576: 33.9GiB -> 16.9GiB），
            # 下游 VideoCombine 以 numpy*255 转 uint8，兼容 float16
            tensor = _read_fi_tensor(out_video, dtype=torch.float16)
            if pbar is not None:
                pbar.update_absolute(est_out)
            print(f'[Topaz-FI] 完成: {tensor.shape[0]}帧 @ '
                  f'{tensor.shape[2]}x{tensor.shape[1]}')
        finally:
            # raw 直通无 in_video，只清 out_video
            try:
                if os.path.isfile(out_video):
                    os.remove(out_video)
            except OSError:
                pass

        return (tensor,)


NODE_CLASS_MAPPINGS = {
    "TopazStarlight": TopazStarlight,
    "TopazFrameInterpolation": TopazFrameInterpolation,
    "TopazVideoOutput": TopazVideoOutput,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "TopazStarlight": "Topaz 星光2.6 超分",
    "TopazFrameInterpolation": "Topaz 插帧 (Apollo/Aion/Chronos)",
    "TopazVideoOutput": "Topaz 视频输出 (快保存)",
}
