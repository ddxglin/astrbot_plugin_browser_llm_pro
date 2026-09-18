# browser_llm_pro

给 AstrBot 里的 LLM（如 Niko）一套**能真正操作浏览器**的工具集：不只是截图，而是能点、能拖、能按键、能录屏（带声音）、能玩 Flash 老游戏、能批量答选择题。

## 为什么叫 Pro

原版只有"打开/点击/输入/截图"几个工具，且截图不回传。这一版把"模型每一次往返都要花 1~4 秒"当成核心约束来设计：

- **动作结果直接带截图 + 页面摘要**（标题/URL/可点元素/正文开头/标签页），省掉"再问一次"的往返
- **`browser_act` 批量动作**：一次调用连做十几步（点击/按键/拖拽/等待），长序列按计划时长放宽超时
- **`browser_form` + `browser_answer`**：一次读全题目（纯文本），一次点完整页选项，并回报"当前已选中几题"自校验
- **`browser_play_swf`**：内置 Ruffle（WASM Flash 模拟器）播放老 .swf，Flash 游戏也能玩+录

## 主要能力

| 类别 | 工具 |
|---|---|
| 导航 | `browser_open` `browser_back` `browser_forward` `browser_get_tabs` `browser_switch_tab` `browser_close_tab` |
| 交互 | `browser_click` `browser_click_text` `browser_click_element` `browser_click_canvas` `browser_hover` `browser_hover_element` `browser_key` `browser_key_down` `browser_key_up` `browser_input` `browser_input_by_selector` `browser_scroll` `browser_zoom` `browser_act`(批量,含 drag) |
| 读取 | `browser_screenshot` `browser_get_element_text` `browser_get_element_attribute` `browser_find_elements` `browser_get_source` `browser_wait_for_element` |
| 表单 | `browser_form` `browser_answer` |
| 录屏 | `browser_record` `browser_record_start` `browser_record_stop`（CDP 截屏流 + ffmpeg，**含页面声音**） |
| Flash | `browser_play_swf`（配合 `tools/fetch_ruffle.sh` 安装 Ruffle） |

## 安装

```
cd /AstrBot/data/plugins
git clone https://github.com/<你的用户名>/browser_llm_pro.git
# Flash 支持（可选）：
bash browser_llm_pro/tools/fetch_ruffle.sh
```

依赖：Playwright（chromium 内核）+ ffmpeg。录屏带声音需要一个 PulseAudio 虚拟声卡（容器内可用 `module-null-sink` 实现，详见代码注释）。

## 一些实测结论（都写进了代码注释）

- 截图默认宽 1024：1920×1400 缩到 1024 后，图片 token 从 ~1000 降到 ~440
- 文字点击用 JS 扫描 + 真实鼠标，命中 0.7s；未命中 0.01s（原先 Playwright locator 要白等 5s）
- 不要给跑 canvas/WASM 的浏览器加 `--disable-gpu` / `--disable-accelerated-2d-canvas`，否则每个操作 10~30 秒
- 每个用户实例只保留一套 chromium（早期版本会多挂一套，白吃 500MB）
- 内存守护按"真实压力"判断（MemAvailable + PSI），只看占用百分比会误杀

## 许可

MIT（如需）。Ruffle 为 Apache-2.0/MIT，需自行下载，不随仓库分发。
