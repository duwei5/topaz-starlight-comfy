import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

// Topaz 视频输出 (快保存) 节点：节点面板上的视频预览 + 工具条。
// 执行后节点下方显示 <video>，下方一排按钮：
//   打开 / 保存 / 静音 / 同步 / 暂停 / 隐藏
// 全部纯前端，不依赖任何第三方插件；同步预览作用于页面上所有本插件节点的预览。
app.registerExtension({
  name: "TopazStarlight.VideoPreview",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "TopazVideoOutput") return;

    const origOnNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = origOnNodeCreated?.apply(this, arguments);
      const node = this;

      // ---------- 预览 DOM ----------
      const el = document.createElement("div");
      el.style.width = "100%";
      el.style.overflow = "hidden";
      el.className = "topaz_preview";

      const videoWrap = document.createElement("div");
      videoWrap.style.width = "100%";
      videoWrap.style.overflow = "hidden";
      const video = document.createElement("video");
      video.style.width = "100%";
      video.style.height = "100%";
      video.style.objectFit = "contain";
      video.style.display = "none";
      video.controls = true;
      video.loop = true;
      video.muted = true;
      videoWrap.appendChild(video);

      // 分辨率标签（加载元数据后显示 宽×高 @ fps）
      const resLabel = document.createElement("div");
      resLabel.style.fontSize = "11px";
      resLabel.style.textAlign = "center";
      resLabel.style.color = "#aaa";
      resLabel.style.padding = "2px 0 0 0";
      resLabel.style.lineHeight = "16px";
      resLabel.style.height = "16px";
      resLabel.textContent = "";

      // ---------- 工具条 ----------
      const bar = document.createElement("div");
      bar.style.display = "flex";
      bar.style.flexWrap = "nowrap";
      bar.style.gap = "4px";
      bar.style.marginTop = "4px";
      bar.style.justifyContent = "center";
      bar.style.overflowX = "auto";
      bar.style.minHeight = "22px";

      const makeBtn = (label, title) => {
        const b = document.createElement("button");
        b.textContent = label;
        b.title = title || label;
        b.style.fontSize = "11px";
        b.style.padding = "1px 7px";
        b.style.height = "22px";
        b.style.boxSizing = "border-box";
        b.style.lineHeight = "20px";
        b.style.cursor = "pointer";
        b.style.border = "1px solid #888";
        b.style.background = "#333";
        b.style.color = "#eee";
        b.style.borderRadius = "3px";
        b.disabled = true;
        return b;
      };

      const btnOpen = makeBtn("打开", "在新窗口打开完整视频");
      const btnSave = makeBtn("保存", "下载保存视频文件");
      const btnMute = makeBtn("静音", "切换静音");
      const btnSync = makeBtn("同步", "所有预览回到开头同步播放");
      const btnPause = makeBtn("暂停", "播放/暂停");
      const btnHide = makeBtn("隐藏", "隐藏/显示视频预览");
      bar.append(btnOpen, btnSave, btnMute, btnSync, btnPause, btnHide);
      // 阻止按钮事件冒泡到画布（避免拖动/取消选中）
      bar.addEventListener("pointerdown", (e) => e.stopPropagation());
      videoWrap.addEventListener("pointerdown", (e) => e.stopPropagation());

      el.append(videoWrap, resLabel, bar);

      // ---------- 状态与 widget ----------
      let widget = null;
      const state = { params: null, paused: false, muted: true, hidden: false };

      const ensureWidget = () => {
        if (widget) return widget;
        widget = node.addDOMWidget("videopreview", "preview", el, {
          serialize: false,
          hideOnZoom: false,
          // 序列化：ComfyUI 保存工作流时调用 getValue() 取预览值
          getValue: () => ({
            params: state.params,
            paused: state.paused,
            muted: state.muted,
            hidden: state.hidden,
          }),
          // 恢复：加载工作流时 ComfyUI 调 setValue(v) 还原预览
          setValue: (v) => {
            if (!v || !v.params) return;
            state.params = v.params;
            state.paused = !!v.paused;
            state.muted = !!v.muted;
            state.hidden = !!v.hidden;
            videoWrap.style.display = state.hidden ? "none" : "";
            video.muted = state.muted;
            updateSource();
          },
        });
        // 恢复入口：ComfyUI 加载工作流时调内置 setValue → 设 widget.value → 触发本 callback。
        // （内置 setValue 不会调 options.setValue，所以恢复逻辑必须放这里，从 widget.value 同步 state）
        widget.callback = (value) => {
          const v = value && value.params ? value : widget.value;
          if (!v?.params) return;
          state.params = v.params;
          state.paused = !!v.paused;
          state.muted = !!v.muted;
          state.hidden = !!v.hidden;
          videoWrap.style.display = state.hidden ? "none" : "";
          video.muted = state.muted;
          updateSource();
        };
        widget.computeSize = function (width) {
          // 预览区高度：按视频宽高比随节点宽度【等比放大】，让预览跟随节点框。
          // 用 node.size[0]（而非 width 参数）统一计算：
          //   - ComfyUI 布局时会【无参数】调用 computeSize()，width=undefined 会算出 NaN
          //   - node.computeSize 内部会以它自己算的"最优宽度"(≈210)【有参】调用 computeSize()
          //   两处用不同 width 会导致 computedHeight 不一致、节点高度乱跳，
          //   所以一律以节点实际宽度为准。
          // 元数据未加载时按 16:9 估算；videoWidth=0 时视频不塌陷、不盖住工具条。
          const w = node.size[0];
          const contentW = Math.max(w - 20, 50);
          let vh;
          if (video.videoWidth > 0 && video.videoHeight > 0) {
            vh = contentW / (video.videoWidth / video.videoHeight);
          } else {
            vh = (contentW * 9) / 16;
          }
          vh = Math.min(vh, 4000); // 仅防极端失控（等比为主，让预览填满节点宽度）
          if (state.hidden) return [w, 26]; // 仅工具条
          videoWrap.style.height = vh + "px";
          videoWrap.style.display = "block";
          // widget 高度 = 预览 + 分辨率标签(16) + 工具条(22+间距) + 余量
          this.computedHeight = vh + 60;
          return [w, this.computedHeight];
        };
        return widget;
      };

      const fitHeight = () => {
        ensureWidget().computeSize(node.size[0]);
        node.setSize([node.size[0], node.computeSize([node.size[0], node.size[1]])[1]]);
        node?.graph?.setDirtyCanvas?.(true);
      };

      const setButtonsEnabled = (on) => {
        for (const b of [btnOpen, btnSave, btnMute, btnSync, btnPause, btnHide]) b.disabled = !on;
      };

      const updateBarLabels = () => {
        btnMute.textContent = state.muted ? "取消静音" : "静音";
        btnPause.textContent = state.paused ? "继续" : "暂停";
        btnHide.textContent = state.hidden ? "显示" : "隐藏";
      };

      // 显示视频分辨率（宽×高 @ 帧率）
      const updateResLabel = () => {
        if (video.videoWidth <= 0) {
          resLabel.textContent = "";
          return;
        }
        const fps = node.widgets?.find((w) => w.name === "帧率")?.value;
        resLabel.textContent =
          `${video.videoWidth} × ${video.videoHeight}` + (fps ? ` @ ${fps}fps` : "");
      };

      const videoUrl = () => {
        if (!state.params) return null;
        const p = state.params;
        const q = new URLSearchParams({
          filename: p.filename,
          subfolder: p.subfolder || "",
          type: p.type || "output",
        });
        return api.apiURL("/view?" + q.toString());
      };

      // 用当前 state.params 刷新视频显示（执行后 / 恢复时共用）
      const updateSource = () => {
        if (!state.params?.filename) return;
        const p = { ...state.params, timestamp: Date.now() }; // 时间戳防缓存
        video.src = api.apiURL("/view?" + new URLSearchParams(p));
        video.style.display = "block";
        video.muted = state.muted;
        videoWrap.style.display = state.hidden ? "none" : "";
        video.onloadedmetadata = () => {
          fitHeight();
          setButtonsEnabled(true);
          updateBarLabels();
          updateResLabel();
        };
        // 立即按估算高度布局（元数据加载前视频也有固定高度，不塌陷、不盖住工具条）
        fitHeight();
        if (!state.paused && !state.hidden) video.play().catch(() => {});
      };

      // ---------- 按钮动作 ----------
      btnOpen.onclick = () => {
        const u = videoUrl();
        if (u) window.open(u, "_blank");
      };
      btnSave.onclick = () => {
        const u = videoUrl();
        if (!u) return;
        const a = document.createElement("a");
        a.href = u;
        a.setAttribute("download", state.params.filename);
        document.body.append(a);
        a.click();
        requestAnimationFrame(() => a.remove());
      };
      btnMute.onclick = () => {
        state.muted = !state.muted;
        video.muted = state.muted;
        updateBarLabels();
      };
      btnSync.onclick = () => {
        // 本插件所有预览回到开头并播放
        for (const pv of document.querySelectorAll(".topaz_preview video")) {
          pv.currentTime = 0;
          pv.play().catch(() => {});
        }
        // 联动 VideoHelperSuite 预览一起回到开头（保持 VHS 原有播放状态）
        for (const p of document.querySelectorAll(".vhs_preview")) {
          for (const child of p.children) {
            if (child.tagName === "VIDEO") child.currentTime = 0;
            else if (child.tagName === "IMG") child.src = child.src;
          }
        }
      };
      btnPause.onclick = () => {
        state.paused = !state.paused;
        if (state.paused) video.pause();
        else video.play().catch(() => {});
        updateBarLabels();
      };
      btnHide.onclick = () => {
        state.hidden = !state.hidden;
        videoWrap.style.display = state.hidden ? "none" : "";
        if (state.hidden) video.pause();
        else if (!state.paused) video.play().catch(() => {});
        updateBarLabels();
        fitHeight();
      };

      // ---------- 执行后接收视频路径 ----------
      node.onExecuted = function (message) {
        const p = message?.topaz_video?.[0];
        if (!p) return;
        state.params = p;
        ensureWidget();
        widget.value = { params: p, paused: state.paused, muted: state.muted, hidden: state.hidden };
        updateSource();
      };

      // 立即挂载预览 widget（即使尚未执行，按钮工具条也常驻显示）
      ensureWidget();

      // ---------- 节点尺寸变化 → 预览跟随 ----------
      // 拖宽节点时预览按宽高比等比放大、节点高度自适应（setSize 容纳预览）。
      // 双保险：LiteGraph 拖拽走 node.setSize → onResize；直接改 size 走 Vue 更新
      // .dom-widget 宽度 → ResizeObserver 兜底。
      const origOnResize = node.onResize;
      let lastResizeW = node.size[0];
      node.onResize = function (size) {
        try { origOnResize?.apply(this, arguments); } catch (e) {}
        if (!size || size[0] === lastResizeW) return; // 宽度没变不重算（防 fitHeight→setSize 递归）
        lastResizeW = size[0];
        fitHeight();
      };
      if (typeof ResizeObserver !== "undefined") {
        let lastElW = el.offsetWidth;
        new ResizeObserver(() => {
          const nw = el.offsetWidth;
          if (nw === lastElW) return; // 宽度没变不重算
          lastElW = nw;
          fitHeight();
        }).observe(el);
      }

      return r;
    };
  },
});
