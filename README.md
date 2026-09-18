# browser_llm_pro

> 给 AstrBot 上的大模型一套**能真正操作浏览器**的工具集：不止截图，还能点、拖、按键、批量答题、玩 Flash 老游戏，并把过程**录成带声音的视频**发出来。

适合的场景：让机器人自己去查资料、填表单、做在线测试、玩网页小游戏、把操作过程录屏发给用户。

---

## 设计取舍

让模型操作浏览器最贵的不是点击本身，而是**每一次往返**——实测每次工具调用往返 1~4 秒。
所以这里的每个设计都在减少往返次数和 token：

| 做法 | 效果 |
|---|---|
| 动作类工具的结果里**直接带上截图 + 页面摘要**（标题 / URL / 可点元素 / 正文开头 / 标签页） | 省掉"再问一次"的往返 |
| `browser_act` **批量动作**：一次调用连做十几步（点击 / 按键 / 拖拽 / 等待），长序列按计划时长放宽超时 | 十几步 = 1 次往返 |
| `browser_form` + `browser_answer`：一次读全题目（纯文本），一次点完整页选项，并回报"当前已选中几题" | 整页选择题 = 2 次往返 |
| 截图默认缩到 1024px 宽 | 图片 token 从 ~1000 降到 ~440 |
| 文字点击改用 JS 扫描 + 真实鼠标 | 命中 0.7s；未命中 0.01s（原先要白等 5s） |

---

## 功能一览（33 个工具）

| 类别 | 工具 |
|---|---|
| 导航 | `browser_open` `browser_back` `browser_forward` `browser_get_tabs` `browser_switch_tab` `browser_close_tab` |
| 交互 | `browser_click` `browser_click_text` `browser_click_element` `browser_click_canvas` `browser_hover` `browser_hover_element` `browser_key` `browser_key_down` `browser_key_up` `browser_input` `browser_input_by_selector` `browser_scroll` `browser_zoom` `browser_act`（批量，含拖拽） |
| 读取 | `browser_screenshot` `browser_get_element_text` `browser_get_element_attribute` `browser_find_elements` `browser_get_source` `browser_wait_for_element` |
| 表单 / 问卷 | `browser_form` `browser_answer` |
| 录屏 | `browser_record` `browser_record_start` `browser_record_stop` |
| Flash 游戏 | `browser_play_swf` |

其它：按用户隔离的浏览器实例与持久化缓存、闲置回收、内存守护、孤儿进程与僵尸自动清理。

---

## 演示

下面这段是插件自己录的：模型读题 → 一次点完整页选项 → 提交 → 结果。

![答题演示](docs/demo-quiz.gif)

（录屏本身输出的是 mp4，默认连页面声音一起录；这里为了在 README 里直接播放，转成了 GIF。）

---

## 安装

```bash
cd /AstrBot/data/plugins
git clone https://github.com/ddxglin/browser_llm_pro.git

# 可选：Flash（SWF 老游戏）支持，会下载 Ruffle 到 vendor/ruffle/
bash browser_llm_pro/tools/fetch_ruffle.sh
```

依赖：
- **Playwright（chromium 内核）**：`pip install playwright && playwright install chromium`
- **ffmpeg**：录屏合成用（`apt install ffmpeg`）
- **PulseAudio 虚拟声卡**（仅"录屏带声音"需要）：容器里没有真实声卡也能做——加载 `module-null-sink`，浏览器通过 `PULSE_SERVER` 把声音输出进去。细节见 `core/audio.py` 与 `core/recorder.py` 的注释。

装好后在 AstrBot 面板的插件页启用即可，44 项配置都在面板上，无需改代码。

---

## 常用配置

| 配置 | 默认 | 说明 |
|---|---|---|
| `screenshot_max_width` | 1024 | 截图宽度。越小 token 越少、模型越快；看小字再调大 |
| `action_screenshot` / `action_digest` | 开 | 动作结果是否附带截图 / 页面摘要（省往返的关键） |
| `zoom_factor` | 1.5 | 页面整体缩放。文字页面 1.5 更好读；**canvas 游戏请设 1.0** |
| `idle_timeout` | 300 | 闲置多久回收浏览器（游戏类任务建议 1800） |
| `max_memory_percent` / `mem_min_available_mb` | 80 / 900 | 内存守护：**只有真实可用内存低于阈值才会关浏览器**，避免把 page cache 当压力误杀 |
| `record_audio` | 开 | 录屏是否连页面声音一起录 |
| `record_max_duration` | 120 | 单次录屏上限（最高 300） |
| `record_max_width/height` | 0 | 0 = 跟随视口，不缩放不变形 |
| `flash_ruffle` | 开 | 是否启用 Flash（Ruffle）支持 |
| `browser_proxy` | 空 | 留空直连；填 `env` 跟随环境变量；填 `direct` 强制直连 |

---

## 一些踩过的坑（都体现在代码注释里）

- **别给跑 canvas/WASM 的浏览器加 `--disable-gpu` 或 `--disable-accelerated-2d-canvas`**：会让每个操作从 0.3 秒变成 10~30 秒。
- **文字点击不要用 Playwright 的 `get_by_text().wait_for()`**：找不到时要白等满 5 秒；改成一次 JS 扫描拿坐标，未命中 0.01 秒就能返回近似候选。
- **坐标要换算**：截图会被缩到 `screenshot_max_width`，模型给的是图片像素；直接当页面坐标点会系统性偏左上。插件会按 (图片尺寸 → 视口尺寸 ÷ 页面缩放) 自动换算。
- **每个用户实例只保留一套 chromium**：早期实现会多挂一套没人用的，白吃 300~500MB。
- **内存守护要看真实压力**：只看"占用百分比"会把 page cache 算进去，导致浏览器被反复误杀。
- **网页进全屏时把 `body zoom` 复位**，否则 canvas 会被放大到超出视口，录出来只有左上角。

---

## Flash（SWF）老游戏

现代浏览器早已移除 Flash。插件内置方案：

1. `tools/fetch_ruffle.sh` 下载 [Ruffle](https://ruffle.rs/)（WASM 版 Flash 模拟器）到 `vendor/ruffle/`
2. `browser_play_swf("https://.../game.swf")`：本地起一个只读静态服务，把远端 SWF 取回后交给 Ruffle 播放（本地服务同源，绕开 CORS）
3. 也可以传本地相对路径，例如 `browser_play_swf("mygame/loader.swf")`（放在 `vendor/ruffle/` 下）

⚠️ 只分发本插件代码，**不包含任何游戏文件**；Ruffle 遵循其自身许可（Apache-2.0 / MIT）。

---

## 目录结构

```
browser_llm_pro/
├─ main.py                  # AstrBot 入口：配置转换
├─ browser_llm_plugin.py    # LLM 工具定义 + 每用户实例管理
├─ core/
│  ├─ browser.py            # Playwright 封装（导航/点击/拖拽/表单/截图）
│  ├─ recorder.py           # CDP 截屏流 + ffmpeg 录屏（含音频对齐）
│  ├─ audio.py              # PulseAudio 虚拟声卡与录音
│  ├─ flash.py              # Ruffle 本地服务与注入
│  ├─ supervisor.py         # 超时 / 频率限制 / 内存守护 / 闲置回收
│  └─ reaper.py             # 孤儿浏览器与僵尸进程回收
├─ tools/fetch_ruffle.sh
└─ _conf_schema.json        # 面板配置（44 项）
```

---

## 常见问题

**录屏没有声音？**
容器里需要 PulseAudio 虚拟声卡（`module-null-sink`）。另外**不能**让 Chromium 带 `--mute-audio`（Playwright 默认会加，插件已用 `ignore_default_args` 处理）。

**内存被浏览器吃满？**
调低 `max_memory_percent`，或把 `idle_timeout` 调小；`min_browser_mb_to_kill` / `mem_kill_cooldown` 可以控制误杀。真实内存充足时插件不会动手。

**点击没反应？**
先看返回里的页面摘要。若页面是 canvas 游戏，用 `browser_click_canvas` 抢焦点；若是表单，用 `browser_form` 看选项状态，不要靠"画面没变化"判断失败——选中往往只变一个高亮。

---

## 致谢与来源

- 本插件基于 [under-the-ocean/astrbot_plugin_browser_llm](https://github.com/under-the-ocean/astrbot_plugin_browser_llm) 二次开发，感谢原作者。
- Flash 模拟器：[Ruffle](https://ruffle.rs/)（Apache-2.0 / MIT，需自行下载）。
- 图标：Wikimedia Commons 的 *Internet Explorer logo for Windows 7*（CC0）。

## 许可

MIT。Ruffle 依其自身许可，需自行下载，不随仓库分发。
