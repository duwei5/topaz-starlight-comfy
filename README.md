# topaz-starlight-comfy

把 **Topaz 星光 2.6（Starlight 2.6）** 超分能力接进 ComfyUI 的完整部署包：节点源码、调度脚本、云端 bootstrap、以及一份踩过坑的部署说明。

本仓库**只放代码和文档**，不含任何权重、引擎二进制（合计约 50GB），那些走对象存储/网盘单独传。

---

## 零、两条路线（先看这个）

| 路线 | 平台 | 模型 | 入口 | 全片耗时 |
|---|---|---|---|---|
| **A · 星光 2.6** | **Windows** GPU（阿里云/腾讯云/自建） | Topaz Starlight 2.6（5B） | `sr_run.py` + `cloud/setup_windows.ps1` | 4090 约 1~3 h |
| **B · Real-ESRGAN** | **AutoDL / 任意 Linux** | RRDBNet x2 / x4 | `linux/setup_autodl.sh` + `linux/run_sr.sh` | 4090 约 14 min（x2） |

为什么分两条：`topaz_engine` 是 **57 个 .exe + 322 个 .dll、0 个 .so** 的 Windows 二进制，
**AutoDL 的 Linux 镜像跑不了星光**（wine 也不行，要 CUDA 直通 + DirectML）。
AutoDL 上走 B 路线，代码在 `linux/`，见 [linux/README.md](linux/README.md)。

```bash
# AutoDL 三步（仓库已公开，直接 clone，不需要 token）
git clone https://github.com/duwei5/topaz-starlight-comfy.git /root/sr
cd /root/sr && bash linux/setup_autodl.sh --scale 2
bash linux/run_sr.sh /root/autodl-tmp/master_native.mp4 --scale 2 --target 1080p
```

---

## 一、这套东西解决什么

- ComfyUI 里一条 `LoadVideo → TopazStarlight → TopazVideoOutput` 的链路，直接吃 mp4 出超分 mp4（带音轨）。
- 命令行一条语句批量跑，不需要开界面。
- 同一套目录结构，本机 8GB 卡能用来**试参**，云上 24G/32G 卡能用来**出全片**。

实测参考：RTX 5060 8GB，2 倍放大，约 **20 秒/帧**（含每次 13 分钟模型冷加载）；24GB 以上卡模型可整块驻留，落到 0.5~1.5 秒/帧。

---

## 二、目录结构（必须照这个摆）

```
<ROOT>\
├─ main.py  comfy\  comfy_extras\ ...        ComfyUI 主体
├─ .venv\                                    Python 环境（要有 torch + CUDA）
├─ models\                                   ComfyUI 模型库（可与别的实例共用）
├─ input\  output\  temp\
├─ custom_nodes\
│    ComfyUI-TopazStarlight\                 ← 本仓库 nodes/ 里的内容
│    comfyui-videohelpersuite\               ← VHS，模板要用
│    rgthree-comfy\                          ← 模板里的分组旁路
├─ topaz_engine\                             ← 约 50GB，不进 git
│    neuroserver171\neuroserver.exe
│    bin171\ffmpeg.exe
│    tvmd\VR.lic
│    models\slp26\blob.*  (14.7GB)
│    models\astra\ astrahq\ astrasharp\
│    models\model_meta\
├─ sr_run.py                                 ← 本仓库根目录
├─ start_ui.ps1  stop_ui.ps1
```

两个死规矩：

1. **节点目录不能套两层**。引擎路径是节点按 `__file__` 往上三层算出来的：`custom_nodes/ComfyUI-TopazStarlight/topaz_pipeline.py` → `<ROOT>/topaz_engine`。多一层就找不到引擎。
2. **引擎要解到 `topaz_engine\` 里面**，不是 ComfyUI 根目录。

---

## 三、本机（Windows）怎么用

```powershell
# 1. 摆文件（假设 ComfyUI 已经在 D:\ComfyUI）
robocopy .\nodes\ComfyUI-TopazStarlight D:\ComfyUI\custom_nodes\ComfyUI-TopazStarlight /E
copy sr_run.py   D:\ComfyUI\
copy start_ui.ps1 D:\ComfyUI\
copy stop_ui.ps1  D:\ComfyUI\

# 2. 自检：环境、引擎、权重逐项打勾
D:\ComfyUI\.venv\Scripts\python.exe D:\ComfyUI\sr_run.py --env

# 3. 启动（端口 8189）
powershell -ExecutionPolicy Bypass -File D:\ComfyUI\start_ui.ps1

# 4. 先试 10 帧
D:\ComfyUI\.venv\Scripts\python.exe D:\ComfyUI\sr_run.py -v D:\ComfyUI\input\master.mp4 -c 10 -o test

# 5. 正式跑（736x416 → 1920x1080 是 2.609 倍）
D:\ComfyUI\.venv\Scripts\python.exe D:\ComfyUI\sr_run.py -v master.mp4 -s 2.609 -m 6 -o film1080
```

`sr_run.py` 参数：

| 参数 | 含义 | 常用值 |
|---|---|---|
| `-v` | 源视频 | — |
| `-s` | 放大倍数 | 1080p=2.609、2K=3.478、4K=5.217（按 736 宽算） |
| `-m` | 引擎显存预算 GB | 8G→4~6、16G→12~14、24G→20~22、32G→28 |
| `-o` | 输出子目录 | 自定义 |
| `-n` | 文件名前缀 | 自定义 |
| `-c` | 只跑前 N 帧 | 试跑 10，正式 0 |
| `--model` | slp / astra / astrahq / astrasharp | 默认 slp（星光 2.6） |
| `--strength` | 增强强度 | 0.7 柔和 / 1.0 默认 / 1.3 最锐 |
| `--env` | 只做环境自检 | — |

---

## 四、云端部署：**必须是 Windows GPU 实例**

这一条是最容易翻车的地方，先说结论：

> `topaz_engine` 里是 **53 个 .exe + 216 个 .dll + python312.dll**，零个 `.so`。它只能在 Windows 上跑。AutoDL / 恒源云 / RunPod 默认的 Linux 镜像**跑不了**，装 wine 也不行（CUDA 直通 + DirectML 依赖）。

所以要选：

| 平台 | 机型 | 备注 |
|---|---|---|
| 阿里云 ECS | `gn7i` / `gn8is`（A10 / L20）**Windows Server 2022** | 需要实名 + 备案无关机；按量计费 |
| 腾讯云 CVM | `GN10Xp` / `GI3X` **Windows** | 同上 |
| 华为云 | `pi2` / `g6` Windows | 同上 |
| 本地/公司机器 | 任意 24G 以上 N 卡 + Windows | 最省钱 |

24GB 显存起，32GB 更稳（模型整块驻留）。带宽建议 ≥10Mbps，否则 50GB 引擎传不完。

### 4.1 引擎怎么上云（50GB，不能进 git）

```powershell
# 本机打包，按 1.9GB 分卷（避免单文件过大）
.\cloud\pack_engine.ps1 -EngineDir D:\ComfyUI\topaz_engine -OutDir D:\pack_out

# 传到云上三选一
#  a) 对象存储：ossutil / coscmd / rclone，最稳，能断点续传
#  b) 云盘直挂：Windows 实例挂 NAS/OSS 目录，直接解压
#  c) RDP 拖拽：小文件可以，50GB 会断
```

云上解压：

```powershell
7z x topaz_engine.7z.001 -oD:\ComfyUI\topaz_engine -y
```

### 4.2 云端 bootstrap

```powershell
# 在云 Windows 实例上
git clone https://github.com/<you>/topaz-starlight-comfy.git C:\sr
powershell -ExecutionPolicy Bypass -File C:\sr\cloud\setup_windows.ps1 `
  -Root D:\ComfyUI -Repo C:\sr
```

脚本会做：检查 GPU/磁盘 → 摆节点 → 校验引擎与权重 → 建 input/output/temp → 启动服务 → 调 `/object_info` 确认 `TopazStarlight` 注册成功 → 跑 10 帧冒烟。

### 4.3 云端跑全片的建议

```powershell
# 一次跑完，别切段：每次 --once 都要重付 13 分钟冷加载
python sr_run.py -v D:\ComfyUI\input\master.mp4 -s 2.609 -m 22 -o film1080
```

时间估算（7502 帧）：

| 卡 | 每帧 | 全片 |
|---|---|---|
| 5060 8GB（实测） | ~20 s | ~41 h（不可行） |
| 4090 24GB | 0.5~1.5 s | 1~3 h |
| 5090 32GB | 0.3~1.0 s | 0.6~2 h |

---

## 五、踩过的坑

1. 节点目录套两层 → 引擎路径指错，节点加载后运行时报"未找到星光引擎"。
2. `custom_nodes` 里留 `_backup`、`旧` 目录 → ComfyUI 尝试当节点导入并报红。
3. 复制 ComfyUI 主体时用"排除 models 目录"的参数 → 会连带排掉 `comfy/ldm/models`，启动报 `No module named 'comfy.ldm.models'`。
4. 引擎的 stdout 被节点吞掉，界面/日志看不到进度；用 `nvidia-smi` 看显存爬升判断死活。加载期 GPU 0%、CPU 极低是正常的。
5. 8GB 卡把"显存上限"开到 6 以上会开始借共享内存，反而更慢。
6. 每次 `--once` 都是冷启动，13 分钟模型加载躲不掉——要跑就一次跑完整段。

---

## 六、目录

```
sr_run.py                     命令行调度（跨平台，含 --env 自检）
start_ui.ps1 / stop_ui.ps1    本机启停（端口 8189，ASCII-only，PS 5.1 安全）
nodes/ComfyUI-TopazStarlight/ 节点源码（3 个节点 + web 资源 + 模板）
workflow/                     可直接拖进 ComfyUI 的模板 json
cloud/setup_windows.ps1       云端 Windows bootstrap
cloud/pack_engine.ps1         引擎分卷打包
cloud/README.md               云端平台选择与传输细节
```

---

## 七、许可提醒

`topaz_engine` 内含 Topaz 的商业权重与授权文件（`vr.lic` / `topaz.lic`）。本仓库只做**调度与部署自动化**，不再分发这些二进制；引擎的来源与授权由使用者自行负责，别把它推到公开仓库。