# AGENTS.md — ComfyUI-TopazStarlight 插件

ComfyUI 自定义节点插件（Topaz 星光超分 / Apollo 插帧 / Topaz 视频输出）。维护此项目时的铁律。

## 环境铁律

- **平铺结构**：portable 版 `folder_paths.py` 在 `D:\ComfyUI_portable_TE_v2\ComfyUI\` 根目录，`comfy\` 子目录没有它。导入必须 `try: import folder_paths except: from comfy import folder_paths`。`from comfy.utils import ProgressBar` 可用。
- 前端真实实现（DOM widget）在 `python_embeded\Lib\site-packages\comfyui_frontend_package\static\assets\`，不在 `web/static`。
- 扩展 JS URL：`/extensions/ComfyUI-TopazStarlight/js/topaz_video_preview.js`（注意不是 `/extensions/TopazStarlight/`，那个 404）。

## 改代码前必读的坑

1. **computeSize 会被无参数调用**（ComfyUI 布局时 `widget.computeSize()` 无参）：必须用 `node.size[0]` 统一计算，绝不能依赖 `width` 参数，否则 `width=undefined` → 高度 NaN → `.dom-widget` 高度 0 → 预览+按钮全塌。
2. **GPU选择 用经典列表格式**（`extra_info.options`），Auto/自动 会报"输入值不可用"。
3. **显存上限默认 96G**（用户分享给别人用，勿按本机 4070TiS 改小）。
4. 双卡 "All GPUs" 实测太慢，用户已叫停，勿主动推。
5. **改一项验证一项再继续**（用户硬性要求）。

## 前端验证

用 `computer_use_tool(plane="bu")` 访问 `http://127.0.0.1:8188/`。铁律：
- 强制刷新 `bu.navigate("http://127.0.0.1:8188/?cb="+时间戳)`（普通 reload 命中缓存）。
- 页面隐藏（bu 后台）时 video 不加载、rAF 暂停、canvas 默认 300×150 → 需 `app.canvas.resize()` + 模拟 rAF 驱动绘制，否则 `.dom-widget` 不更新、isNodeVisible=false（调试假象）。
- 判断"打开工作流"用 `app.loadGraphData()`（含 videopreview 的 widgets_values 才能复现）。

## 关键文件

- `nodes.py`（三节点）/ `topaz_pipeline.py`（超分）/ `topaz_fi_pipeline.py`（插帧，含 `_feed_and_drain` 分块）/ `topaz_save.py`（视频输出）/ `_common.py`（GPU 探测 `resolve_gpu` 双语义映射）/ `web/js/topaz_video_preview.js`（预览 DOM widget）。
- 详细经验沉淀在同机技能 `comfyui-topazstarlight`（SKILL.md + references/pitfalls.md + references/frontend.md + references/architecture.md），先读它。
