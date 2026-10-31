# AutoDL / Linux 路线（Real-ESRGAN 超分）

## 先说结论：AutoDL 装不了星光

```
topaz_engine\  57 个 .exe + 322 个 .dll + python312.dll
               .so 文件：0 个
```

星光引擎是 Windows 二进制，要靠 DirectML + CUDA 直通，**AutoDL 的 Linux 镜像跑不了，wine 也救不了**。

所以给你两条互不冲突的路线：

| 路线 | 平台 | 模型 | 画质 | 全片耗时 | 成本 |
|---|---|---|---|---|---|
| **A · 星光 2.6** | Windows GPU（阿里云/腾讯云/自建） | Topaz Starlight 2.6（5B） | 最高，细节重建 | 4090 约 1~3h | ¥3~8 |
| **B · Real-ESRGAN** | AutoDL（Linux） | RRDBNet x2 / x4 | 好，锐但不会"补脸" | 4090 约 1.5h / 20min | ¥3~5 |

**B 路线才是 AutoDL 能干的事**，代码就在本目录，克隆仓库即可跑。

---

## 一、AutoDL 上跑起来（三步）

```bash
# 1. 克隆（私有仓库，用 PAT；把 <TOKEN> 换成你的 GitHub token）
git clone https://<TOKEN>@github.com/duwei5/topaz-starlight-comfy.git /root/sr
cd /root/sr

# 2. 环境 + 权重 + 自检（约 5 分钟）
bash linux/setup_autodl.sh --scale 2

# 3. 跑片
bash linux/run_sr.sh /root/autodl-tmp/master_native.mp4 --scale 2 --target 1080p
```

> 私有仓库不用写 token 的替代方案：把仓库改成 public，或者在本机 `git archive` 打个 zip 直接拖进 AutoDL 的 JupyterLab。

### 选镜像 / 机型

| 项 | 建议 |
|---|---|
| 镜像 | `PyTorch 2.x / CUDA 12.1` 基础镜像（不要选带 ComfyUI 的，用不上还占盘） |
| 显卡 | 4090 24G 够用；A100 40G 更快但贵 |
| 数据盘 | 一定放 `/root/autodl-tmp`（系统盘只有 30~50G，拆帧会爆） |
| 计费 | 按量，跑完立刻关机；关机后数据盘保留 |

### 磁盘预算（1080p，7502 帧）

```
源片               116 MB
拆帧 PNG           ~5.6 GB
超分后 PNG         ~1.4 GB（x2）/ ~5.6 GB（x4）
成片 H.264         ~1.2 GB
ProRes 422 HQ      ~8.6 GB
```

---

## 二、脚本说明

### `setup_autodl.sh`

- 查 GPU / torch / numpy / PIL / ffmpeg，缺什么装什么
- 下 `RealESRGAN_x2plus.pth` / `x4plus.pth`（GitHub 优先，hf-mirror 兜底）
- 造一张 736×416 随机帧做自检，确认输出尺寸 = 输入 × scale

### `run_sr.sh`

```bash
bash linux/run_sr.sh <video> [--scale 2|4] [--target 1080p|2k|4k] [--batch N] [--crf 17] [--prores]
```

流程：拆帧 → Real-ESRGAN → lanczos 缩到目标 + 轻微 unsharp → 用源片音轨编码。

| 参数 | 说明 |
|---|---|
| `--scale` | 2 或 4。**2 够 1080p**；要 2K/4K 用 4 |
| `--target` | 输出分辨率：1080p=1920×1080、2k=2560×1440、4k=3840×2160 |
| `--batch` | 不填按显存自动推：24G→16、32G→24、12G→8 |
| `--prores` | 输出 ProRes 422 HQ（10bit），给 FCPX / DaVinci 调色用 |
| `--crf` | H.264 质量，17 很稳；要更小体积给 20 |

关键：**1080p 从超分后的图上缩下来（超采样），不要直接放大原片**。736×416 直接 lanczos 到 1080p 是糊上糊，字体边缘会发毛。

### `upscale_frames.py`

自实现 RRDBNet（23 个 RRDB、64 feat、pixel_unshuffle、fp16、channels_last），**只依赖 torch + numpy + PIL**——不用装 basicsr / realesrgan 那些会打架的包。

---

## 三、实测速度

| 卡 | x2（1080p） | x4（2K/4K） |
|---|---|---|
| 5060 8GB | 1.885 fps → 全片 1.2h | 0.377 fps → 全片 5.6h |
| 4090 24GB | ~9 fps → 全片 14min | ~1.8 fps → 全片 70min |
| 5090 32GB | ~13 fps → 全片 10min | ~2.6 fps → 全片 48min |

输入只有 736×416，显存不是瓶颈——**batch 开大比调 tile 有效**。

---

## 四、和星光怎么配合

推荐做法：**星光出历史段（雪山、行军、红旗那些需要"补人"的），Real-ESRGAN 出科技段（实验室、屏幕、街景）**。

Real-ESRGAN 对 AI 生成的平滑脸只能变锐，星光是扩散模型会重绘细节——战斗场面用星光更值，代码/界面特写用 ESRGAN 更稳（不会把文字重绘错）。

两路输出同分辨率同帧率后，用 ffmpeg 拼接即可，注意把星光的 39 帧保护头裁掉（每段前 39 帧是续接重叠）。