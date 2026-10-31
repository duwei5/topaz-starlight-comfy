# 云端部署要点

## 一、平台选型：只能是 Windows GPU

`topaz_engine` 里的二进制构成是死的证据：

```
neuroserver171\  neuroserver.exe  + 53 个 .exe + 216 个 .dll + python312.dll
0 个 .so
```

所以：

| 平台 | 能不能跑 | 说明 |
|---|---|---|
| 阿里云 ECS（Windows Server + gn7i/gn8is） | ✅ | 按量计费，A10/L20 24GB 起 |
| 腾讯云 CVM（Windows + GN10Xp/GI3X） | ✅ | 同上 |
| 华为云（Windows + pi2/g6） | ✅ | |
| 自建/公司机器（Windows + 24G 以上 N 卡） | ✅ | 最省钱 |
| AutoDL / 恒源云 / RunPod Linux 镜像 | ❌ | 无 .so，wine 也救不了（CUDA + DirectML） |
| 任何 macOS | ❌ | — |

显存：24GB 起，32GB 更稳。8GB 卡实测 20 秒/帧，24GB 卡是 0.5~1.5 秒/帧。

---

## 二、引擎传输（约 50GB，不能进 git）

```powershell
# 本机分卷打包（1.9GB 一卷，正好卡 GitHub Release 2GB 上限）
.\cloud\pack_engine.ps1 -EngineDir D:\ComfyUI\topaz_engine -OutDir D:\pack_out
```

三条传输路线：

| 方式 | 命令 | 适用 |
|---|---|---|
| 对象存储（推荐） | `ossutil cp -r D:\pack_out oss://bucket/sr/` | 断点续传，流量便宜 |
| GitHub Release | `gh release create engine-v1 D:\pack_out\topaz_engine.7z.*` | 单卷 <2GB 可行，别人也能拉 |
| RDP 直连复制 | 拖拽 | 只适合几 GB 的试参片段 |

云上解压：

```powershell
7z x topaz_engine.7z.001 -oD:\ComfyUI\topaz_engine -y
```

> 注意：分卷必须**全部下齐再解压**，少一卷 7z 会直接报错。

---

## 三、一键 bootstrap

```powershell
git clone https://github.com/<owner>/topaz-starlight-comfy.git C:\sr
powershell -ExecutionPolicy Bypass -File C:\sr\cloud\setup_windows.ps1 `
  -Root D:\ComfyUI -Repo C:\sr -Port 8189
```

脚本依次做：GPU/显存 → 磁盘（要 55GB）→ ComfyUI 主体是否在 → torch/CUDA 是否可用 →
摆节点和脚本 → 校验引擎与 `slp26` 权重 → `sr_run.py --env` 自检 → 启动服务 →
跑 10 帧冒烟。

`-EngineArchive` 可以直接指到分卷 `.001`，脚本会调 7z 解压（没装就 winget 装）。

---

## 四、跑全片

```powershell
# 订正：一次跑完，别切片。每次 --once 都要重付 13 分钟模型冷加载。
python D:\ComfyUI\sr_run.py -v D:\ComfyUI\input\master.mp4 -s 2.609 -m 22 -o film1080
```

| 输入 | 倍数 | 输出 |
|---|---|---|
| 736×416 | 2.609 | 1920×1080 |
| 736×416 | 3.478 | 2560×1440 |
| 736×416 | 5.217 | 3840×2160 |

7502 帧的耗时：4090 约 1~3 小时，5090 约 0.6~2 小时。按量计费整程大概 ¥3~8。

---

## 五、云端验收清单

```powershell
# 1. 节点注册了吗
(Invoke-RestMethod http://127.0.0.1:8189/object_info).TopazStarlight  | Out-Null; 'OK'

# 2. 引擎跑起来了吗（冒烟时另开一个窗口看）
nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv -l 2

# 3. 出片帧数对不对
ffprobe -v error -select_streams v:0 -count_frames `
  -show_entries stream=nb_read_frames,width,height -of csv=p=0 D:\ComfyUI\output\film1080\*.mp4
```

帧数必须和源片一致（每次 `--once` 会少 1 帧尾帧，属于已知行为，拼片时留出余量）。