"""
浏览器核心 — 增强资源控制版
新增：
- 截图大小限制 & 压缩保护
- 缓存目录自动清理（按文件数量和总大小）
- 标签页硬上限
- 进程PID追踪（对接Supervisor内存监控）
"""

import asyncio
import json
import os
import shutil
import time
import uuid
from collections.abc import Coroutine, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, TypeVar

from astrbot.api import logger
from playwright._impl._api_structures import SetCookieParam
from playwright.async_api import BrowserContext, Cookie, Page, async_playwright

from .audio import ensure_sink
from .recorder import PageRecorder, RecordingError

T = TypeVar("T")


class CookieManager:
    def __init__(self, data_dir: Path):
        self.cookies_file = data_dir / "browser_cookies.json"

    def load_cookies(self) -> list[dict]:
        """从 json 文件加载 cookies"""
        try:
            with open(self.cookies_file, encoding="utf-8") as f:
                raw_cookies: list[dict] = json.load(f)
                return raw_cookies
        except FileNotFoundError:
            self.cookies_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cookies_file, "w", encoding="utf-8") as f:
                json.dump([], f)
            return []
        except json.JSONDecodeError:
            return []

    def save_cookies(self, cookies: list[dict]):
        self.cookies_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cookies_file, "w") as f:
            json.dump(cookies, f, indent=4, ensure_ascii=False)


class BrowserCore:
    """
    浏览器核心 — 增强资源控制版
    """

    _BROWSER_ENGINES = {"firefox", "chromium", "webkit"}

    def __init__(self, config: dict, data_dir: Path):
        self.config = config
        self.data_dir = Path(data_dir)
        self.cookie = CookieManager(data_dir)

        self.cache_dir = data_dir / "screenshot_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # 录屏器（按需创建，见 record_start）
        self.recorder: PageRecorder | None = None
        self.recordings_dir = self.data_dir / "recordings"

        self.browser_type: str = self.config.get("browser_type", "chromium")
        if self.browser_type not in self._BROWSER_ENGINES:
            raise ValueError(f"不支持的浏览器类型: {self.browser_type}")

        # Flash（SWF）支持：本插件自己起本地静态服务注入 Ruffle（见 core/flash.py）
        self.flash_ruffle: bool = bool(config.get("flash_ruffle", True))
        self._ruffle = None
        self._ruffle_url: Optional[str] = None

        # 最近一张截图对应的坐标系（图片像素 → 页面 CSS 像素的换算依据）
        self._shot_space: Optional[dict] = None
        # 当前页面 body 缩放（document.body.style.zoom）
        self._body_zoom: float = 1.0

        self.playwright = None
        self.browser = None
        self.context: BrowserContext | None = None

        self.all_pages: list[Page] = []
        self.current_index: int | None = None
        self.page: Page | None = None

        self._terminated = False
        self._op_lock = asyncio.Lock()

        # ===== 资源限制参数 =====
        # 每个用户最大标签页数
        self.max_pages: int = config.get("max_pages", 5)
        # 截图质量
        self.screenshot_quality: int = min(config.get("screenshot_quality", 80), 100)
        # 视图尺寸
        self.viewport = config.get("viewport_size", {"width": 1280, "height": 720})
        # 超时时间（秒）
        self.timeout: int = config.get("timeout", 30)
        # 打开网页的超时（秒）：比总超时短，慢站点不再把"打开"卡成 30s+
        self.nav_timeout: float = float(config.get("nav_timeout", 15))
        # 单个元素动作（点击/悬停/输入）的可操作性等待上限（秒）。
        # Playwright 默认 30s：元素被遮挡/不可见时会白等 30s（实测出现过 2 次），
        # 现在最多等 3s，然后强点或直接报"点不到"。
        self.action_timeout_ms: int = int(config.get("action_timeout_ms", 3000))
        # 无头模式
        self.headless: bool = config.get("headless", True)
        # 截图大小上限（从supervisor配置读取）
        sup_cfg = config.get("supervisor", {})
        self.screenshot_max_bytes: int = sup_cfg.get("screenshot_max_bytes", 5 * 1024 * 1024)
        # 缓存目录最大文件数（防止inode耗尽）
        self.cache_max_files: int = 500
        # 录屏是否连声音一起录（需要容器内有 PulseAudio 虚拟声卡；见 core/audio.py）
        self.record_audio: bool = bool(config.get("record_audio", True))
        # 浏览器要用的 PULSE_SERVER（启动前解析一次）
        self.audio_server: Optional[str] = None

    # ======================================================
    # 通用兜底工具
    # ======================================================

    async def _safe_await(self, coro: Coroutine[Any, Any, T],
                          retries: int = 0, timeout: float | None = None) -> T:
        """带超时的等待。

        注意：**同一个 coroutine 不能重试**——第一次 `wait_for` 超时已经把它取消，
        再 await 会抛 “cannot reuse already awaited coroutine”。老代码里的 retries
        因此是坏的：慢站点表现为"卡满 30s + 报 URL 访问失败"。要重试请由调用方
        每轮新建 coroutine。
        """
        try:
            return await asyncio.wait_for(coro, timeout or self.timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("Playwright 操作超时") from None

    # ======================================================
    # 坐标换算：模型看的是"缩放后的截图"，点击用的是"页面 CSS 坐标"
    # ======================================================

    async def _quick(self, coro, timeout: float = 5.0):
        """给单个页面/鼠标操作加短超时。

        页面被 WASM/游戏占满主线程时，CDP 调用会长时间不返回（实测 2026-09-18 08:06
        的 `click_canvas` 挂了 55s，把整批 act 拖到 60s 硬超时）。宁可快速失败并说清楚原因。
        """
        return await asyncio.wait_for(coro, timeout=timeout)

    def _remember_zoom(self, zoom: float) -> None:
        try:
            self._body_zoom = max(0.1, min(5.0, float(zoom)))
        except Exception:
            self._body_zoom = 1.0

    def shot_space(self) -> dict:
        """最近一张截图的坐标系（供页面摘要/工具文案展示）。"""
        return dict(self._shot_space or {})

    def map_coords(self, x: float, y: float) -> tuple[int, int]:
        """截图像素 → 页面 CSS 坐标（鼠标点击用的坐标系）。

        为什么必须换算：截图默认被缩到 `screenshot_max_width`（1024），而视口是
        1920×1400，模型看图给出的坐标天然是"图片像素"；再叠加 body zoom 的放大，
        直接当 CSS 坐标点下去会系统性偏向左上（实测：点在 265,132 实际应点 497,248，
        于是 Niko 点不到游戏画面/菜单）。看起来已超出图片范围时，视为模型直接给了
        页面坐标，只做 zoom 还原，避免二次缩放。
        """
        try:
            x, y = float(x), float(y)
        except Exception:
            return int(x), int(y)
        sp = self._shot_space
        if not sp or not sp.get("img_w"):
            return int(round(x)), int(round(y))
        zoom = max(0.1, min(5.0, float(sp.get("zoom") or 1.0)))
        k = (sp["vp_w"] / sp["img_w"]) if (x <= sp["img_w"] and y <= sp["img_h"]) else 1.0
        return int(round(x * k / zoom)), int(round(y * k / zoom))

    async def _safe_page_op(self, page: Page, coro: Coroutine[Any, Any, T]) -> T:
        """Page级操作兜底"""
        try:
            return await coro
        except Exception:
            await self._discard_page(page)
            raise

    async def _settle(self, page: Page, quiet: float = 0.3, timeout: float = 1.2) -> None:
        """动作后等页面稳定。

        原来是写死 sleep(1~1.5s)：每次点击都白等，模型还慢。这里改成
        "networkidle（500ms 无网络活动）就继续，最多等 timeout 秒"——
        快页面只花 ~0.8s，忙页面和原来一样有上限兜底。
        """
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout * 1000)
        except Exception:
            pass
        await asyncio.sleep(quiet)

    async def _discard_page(self, page: Page):
        try:
            await page.close()
        except Exception:
            pass

        if page in self.all_pages:
            self.all_pages.remove(page)

        if not self.all_pages:
            await self._ensure_page()
        elif self.current_index is not None:
            await self._ensure_page(
                min(self.current_index, len(self.all_pages) - 1)
            )

    # ======================================================
    # 生命周期
    # ======================================================

    async def initialize(self):
        async with self._op_lock:
            self.playwright = await async_playwright().start()
            engine = getattr(self.playwright, self.browser_type)

            # 录声音的话，先把虚拟声卡准备好（不可用则退回静音，不影响浏览）
            if self.record_audio and self.browser_type == "chromium":
                try:
                    self.audio_server = await asyncio.to_thread(ensure_sink)
                    if not self.audio_server:
                        logger.warning("[音频] 虚拟声卡不可用，本次录屏将只有画面")
                except Exception as e:
                    self.audio_server = None
                    logger.warning(f"[音频] 虚拟声卡检查失败，本次录屏将只有画面: {e}")

            self.browser = await engine.launch(
                **self._get_launch_options(self.browser_type),
            )

            screen = None
            try:
                screen = {"width": int(self.viewport["width"]), "height": int(self.viewport["height"])}
            except Exception:
                screen = None
            self.context = await self.browser.new_context(
                viewport=self.viewport,
                **( {"screen": screen} if screen else {} ),
            )
            # 网页游戏点 FULLSCREEN 后，body zoom 会让画布被放大到超出视口 ⇒ 只能看到左上角
            # （实测 zoom=1.5 时全屏画布 2754x1721 > 视口 1920x1400）。进全屏就把缩放复位。
            try:
                await self.context.add_init_script("""
                document.addEventListener('fullscreenchange', () => {
                  try {
                    if (document.fullscreenElement && document.body
                        && document.body.style.zoom && document.body.style.zoom !== '1') {
                      document.body.style.zoom = '1';
                    }
                  } catch (e) {}
                }, true);
                """)
            except Exception:
                pass

            # Flash 支持：本地起 Ruffle 静态服务，并给每个页面注入"按需接管 Flash"的脚本
            if self.flash_ruffle:
                try:
                    from .flash import RuffleServer, install_script

                    if self._ruffle is None:
                        self._ruffle = RuffleServer()
                    self._ruffle_url = self._ruffle.start()
                    if self._ruffle_url:
                        await self.context.add_init_script(install_script(self._ruffle_url))
                except Exception as e:
                    logger.warning(f"[Ruffle] 注入失败（不影响普通浏览）: {e}")

            raw_cookies = self.cookie.load_cookies()
            cookies = [
                SetCookieParam(**{k: v for k, v in c.items() if v is not None})
                for c in raw_cookies
            ]
            if cookies:
                await self.context.add_cookies(cookies)

            # 启动时清理缓存
            self._cleanup_cache_on_start()

            await self._ensure_page()

    def _cleanup_cache_on_start(self):
        """启动时清理超量缓存文件"""
        if not self.cache_dir.exists():
            return
        try:
            files = sorted(
                [f for f in self.cache_dir.iterdir() if f.is_file()],
                key=lambda f: f.stat().st_mtime,
            )
            if len(files) > self.cache_max_files:
                to_remove = files[:len(files) - self.cache_max_files]
                for f in to_remove:
                    try:
                        f.unlink()
                    except Exception:
                        pass
        except Exception:
            pass

    async def terminate(self):
        """优雅关闭，幂等执行"""
        async with self._op_lock:
            if self._terminated:
                return
            self._terminated = True

            # 先放弃进行中的录屏，避免帧回调和 CDP 会话悬挂
            await self.record_abort()

            async def safe_close(obj, close_method="close"):
                if obj is None:
                    return
                try:
                    coro = getattr(obj, close_method)
                    if asyncio.iscoroutinefunction(coro):
                        await coro()
                    else:
                        coro()
                except Exception:
                    pass

            await self.save_cookies()

            # 关闭所有页面
            for page in self.all_pages:
                await safe_close(page)
            self.all_pages.clear()
            self.current_index = None
            self.page = None

            await safe_close(self.context)
            self.context = None

            await safe_close(self.browser)
            self.browser = None

            await safe_close(self.playwright, "stop")
            self.playwright = None
            try:
                if self._ruffle is not None:
                    self._ruffle.stop()
                    self._ruffle = None
            except Exception:
                pass

            # 清理缓存（保留最近的文件）
            self._cleanup_cache_on_terminate()

    def _cleanup_cache_on_terminate(self):
        """关闭时清理缓存，只保留最近的文件"""
        if not self.cache_dir.exists():
            return
        try:
            files = sorted(
                [f for f in self.cache_dir.iterdir() if f.is_file()],
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
            # 只保留最近50个文件
            if len(files) > 50:
                for f in files[50:]:
                    try:
                        f.unlink()
                    except Exception:
                        pass
        except Exception:
            pass

    # ======================================================
    # 启动参数
    # ======================================================

    def _get_launch_options(self, engine: str) -> dict[str, Any]:
        args = [
            # 注意：这里**不能**再加 --disable-gpu / --disable-accelerated-2d-canvas。
            # 2026-09-18 实测：跑了 Ruffle（canvas + WASM）之后，页面主线程本来就满，
            # 再关掉 GPU/2D 加速会让每个 CDP 操作（截图/点击/取页面信息）都排到 10~30 秒。
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-component-extensions-with-background-pages",
            "--disable-sync",
            "--no-first-run",
        ]

        # 关键：headless 默认"屏幕"是 800x600。网页一进全屏（游戏站点的 FULLSCREEN 按钮）
        # 页面就按这个尺寸重排，视口缩成 800x600 ⇒ 录出来的视频只有左上角一块（实测
        # 2026-09-17 23:06 那次 DOOM 录像就是 800x600）。把窗口尺寸对齐视口即可。
        try:
            vw = int((self.viewport or {}).get("width") or 1280)
            vh = int((self.viewport or {}).get("height") or 720)
            args += [f"--window-size={vw},{vh}", "--window-position=0,0"]
        except Exception:
            pass

        # 代理：Chromium 不认 HTTP_PROXY 环境变量，得显式给 proxy（本机代理在 192.168.124.99:7892）。
        # 配置 browser_proxy 优先，没配就用环境里的 HTTPS_PROXY/HTTP_PROXY。
        proxy = str(self.config.get("browser_proxy") or "").strip()
        if proxy.lower() in ("direct", "none", "off"):
            proxy = ""   # 显式要求直连
        elif proxy.lower() == "env":   # 显式要求跟随环境变量
            proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                     or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "").strip()
        # 默认直连：实测本机代理会让 dos.zone 这类站点的资源加载不全（画布压根不出来），
        # 直连反而是好的，所以不自动跟随环境变量，必须显式配置。
        if proxy:
            no_proxy = str(self.config.get("browser_proxy_bypass") or "").strip()
            entry: dict[str, Any] = {"server": proxy}
            if no_proxy:
                entry["bypass"] = no_proxy
            opts["proxy"] = entry

        opts: dict[str, Any] = {"args": args}
        opts["headless"] = self.headless

        if not self.record_audio:
            # 不录声音时仍然静音，省资源
            args.append("--mute-audio")
        else:
            # 录声音的三个必要条件（2026-09-17 实测）：
            # 1) Playwright 启动 chromium 时**默认注入 --mute-audio**，只删自己的参数不够，
            #    必须显式 ignore_default_args；否则 play() 正常但录到的是 -91dB 静音。
            # 2) 浏览器要允许无手势自动播放，否则站点媒体根本不放。
            # 3) 浏览器进程要能看到虚拟声卡（PULSE_SERVER 指向 module-null-sink）。
            opts["ignore_default_args"] = ["--mute-audio"]
            args.append("--autoplay-policy=no-user-gesture-required")
            pulse_server = self.audio_server
            if pulse_server:
                opts["env"] = {**os.environ, "PULSE_SERVER": pulse_server}

        if engine == "firefox":
            opts["firefox_user_prefs"] = {
                "intl.accept_languages": "zh-CN,zh",
                "intl.locale.requested": "zh-CN",
                "general.useragent.locale": "zh-CN",
                "media.autoplay.default": 5,
                "media.autoplay.blocking_policy": 2,
                "dom.ipc.processCount": 1,
                "browser.tabs.remote.autostart": False,
                "browser.sessionhistory.max_entries": 50,
                "browser.sessionhistory.contentViewerTimeout": 0,
            }

        if engine == "chromium":
            opts["args"] = args + [
                "--no-sandbox",
                "--disable-accelerated-video-decode",
                "--disable-features=TranslateUI,BlinkGenPropertyTrees",
                "--disable-notifications",
                "--disable-speech-api",
                "--disable-sync",
                # 注：本机实测 Playwright 启动的 chromium 加载不了扩展（它默认带
                # --disable-extensions，忽略后仍不生效），所以 Flash 走 core/flash.py 的
                # Ruffle 注入方案，这里不再挂 --load-extension。
            ]

        return opts

    # ======================================================
    # 页面冻结/解冻（资源优化）
    # ======================================================

    async def _freeze_page(self, page: Page):
        """冻结页面以节省资源"""
        try:
            await page.evaluate("""
                (() => {
                    document.querySelectorAll('video,audio').forEach(v => v.pause());
                    if (!window._freeze) {
                        window._oldSetInterval = window.setInterval;
                        window._oldRequestAnimationFrame = window.requestAnimationFrame;
                        window.setInterval = () => 0;
                        window.requestAnimationFrame = () => {};
                        window._freeze = true;
                    }
                })()
            """)
        except Exception:
            pass

    async def _unfreeze_page(self, page: Page):
        """解冻页面"""
        try:
            await page.evaluate("() => { window._freeze = false; }")
        except Exception:
            pass

    # ======================================================
    # 内部保障
    # ======================================================

    def _require_context(self) -> BrowserContext:
        if self._terminated:
            raise RuntimeError("BrowserManager 已终止")
        if self.context is None:
            raise RuntimeError("BrowserContext 未初始化")
        return self.context

    async def _ensure_page(self, index: int | None = None) -> Page:
        context = self._require_context()

        if not self.all_pages:
            page = await context.new_page()
            default_url = (self.config.get("default_url") or "").strip() or "https://www.bing.com"
            logger.info(f"[BrowserCore] 起始页: {default_url}")
            await self._safe_await(page.goto(default_url))
            self.all_pages.append(page)
            self.current_index = 0
            self.page = page
            return page

        if index is None:
            index = self.current_index or 0

        index = max(0, min(index, len(self.all_pages) - 1))

        if self.page is not None and index != self.current_index:
            await self._freeze_page(self.page)

        self.current_index = index
        self.page = self.all_pages[index]
        await self._unfreeze_page(self.page)

        return self.page

    async def save_cookies(self):
        if not self.context:
            return
        cookies: list[Cookie] = await self.context.cookies()
        self.cookie.save_cookies(cookies)

    # ======================================================
    # 标签页管理（带上限保护）
    # ======================================================

    async def get_all_tabs_titles(self) -> list[str]:
        async with self._op_lock:
            return await asyncio.gather(*(p.title() for p in self.all_pages))

    async def switch_tab(self, index: int) -> Optional[str]:
        async with self._op_lock:
            if not (0 <= index < len(self.all_pages)):
                return f"无效的标签页序号 {index}"
            await self._ensure_page(index)
            return None

    async def close_tab(self, index: int) -> str:
        async with self._op_lock:
            if not (0 <= index < len(self.all_pages)):
                return f"无效的标签页序号 {index}"
            page = self.all_pages[index]
            title = await page.title()
            await self._discard_page(page)
            return f"已关闭标签页【{title}】"

    # ======================================================
    # 页面展示
    # ======================================================

    async def zoom_to_scale(self, scale: float) -> Optional[str]:
        async with self._op_lock:
            page = await self._ensure_page()
            # 限制缩放范围 0.1 ~ 5.0
            scale = max(0.1, min(5.0, scale))
            await page.evaluate(f"document.body.style.zoom = {scale};")
            self._remember_zoom(scale)
            return None

    async def screenshot(
        self,
        zoom_factor: Optional[float] = None,
        full_page: bool = False,
        max_width: Optional[int] = None,
    ) -> Optional[str]:
        async with self._op_lock:
            page = await self._ensure_page()

            async def _shot():
                if zoom_factor:
                    zoom = max(0.1, min(5.0, zoom_factor))
                    await page.evaluate(f"document.body.style.zoom = {zoom};")
                    await page.evaluate("window.scrollTo(0, 0);")
                    self._remember_zoom(zoom)

                return await page.screenshot(
                    full_page=full_page,
                    type="jpeg",
                    quality=self.screenshot_quality,
                )

            raw: bytes = await _shot()
            if raw is None:
                return None

            # ===== 截图大小保护 =====
            if len(raw) > self.screenshot_max_bytes:
                # 尝试降低质量重新截图
                reduced_quality = max(10, self.screenshot_quality // 2)
                raw = await page.screenshot(
                    full_page=False,  # 强制非全页
                    type="jpeg",
                    quality=reduced_quality,
                )
                # 如果仍然过大，再压缩视图
                if len(raw) > self.screenshot_max_bytes:
                    await page.evaluate("document.body.style.zoom = 0.5;")
                    raw = await page.screenshot(
                        full_page=False,
                        type="jpeg",
                        quality=30,
                    )
                    await page.evaluate("document.body.style.zoom = 1;")
                    logger.warning(
                        f"[BrowserCore] 截图过大，已压缩至 {len(raw) / 1024:.1f}KB"
                    )

            # ===== 缓存文件数保护 =====
            if self.cache_dir.exists():
                file_count = len(list(self.cache_dir.iterdir()))
                if file_count > self.cache_max_files:
                    # 清理最旧的文件
                    files = sorted(
                        [f for f in self.cache_dir.iterdir() if f.is_file()],
                        key=lambda f: f.stat().st_mtime,
                    )
                    to_remove = files[:file_count - self.cache_max_files]
                    for f in to_remove:
                        try:
                            f.unlink()
                        except Exception:
                            pass

            # ===== 可选降采样：宽度越大 → 图片 token 越多 → 模型越慢 =====
            # 实测（deepseek-flash，2026-09-17）：1920x1400 = 1003 prompt tokens、首个正文 2.17s；
            # 800x450 = 335 tokens、1.25s。看布局/找按钮用 800~1024 就够。
            if max_width and max_width > 0:
                try:
                    from io import BytesIO

                    from PIL import Image

                    im = Image.open(BytesIO(raw))
                    if im.width > max_width:
                        ratio = max_width / im.width
                        im = im.convert("RGB").resize((max_width, max(1, int(im.height * ratio))))
                        buf = BytesIO()
                        im.save(buf, format="JPEG", quality=self.screenshot_quality)
                        raw = buf.getvalue()
                except Exception as e:
                    logger.warning(f"[BrowserCore] 截图缩放失败（用原图）: {e}")

            # ===== 写入缓存文件 =====
            file_name = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.jpg"
            cache_path = self.cache_dir / file_name
            cache_path.write_bytes(raw)

            # 记下这张图的坐标系：模型接下来给的坐标按"图片像素"换算（见 map_coords）
            try:
                from io import BytesIO

                from PIL import Image

                with Image.open(BytesIO(raw)) as im:
                    iw, ih = im.size
                vp = page.viewport_size or {}
                self._shot_space = {
                    "img_w": iw, "img_h": ih,
                    "vp_w": int(vp.get("width") or iw), "vp_h": int(vp.get("height") or ih),
                    "zoom": float(self._body_zoom or 1.0),
                }
            except Exception:
                pass

            return str(cache_path)

    # ======================================================
    # 页面访问（带标签页上限保护）
    # ======================================================

    @staticmethod
    def _same_url(a: Optional[str], b: Optional[str]) -> bool:
        """宽松比较 URL：忽略大小写差异、协议缺失与结尾斜杠。

        Chromium 会把 ``https://a.com`` 归一化成 ``https://a.com/``，
        直接字符串比较会导致同一个站点被重复开成两个标签页。
        """
        def norm(u: Optional[str]) -> str:
            u = (u or "").strip().rstrip("/")
            for prefix in ("https://", "http://"):
                if u.startswith(prefix):
                    u = u[len(prefix):]
                    break
            return u.lower()

        na, nb = norm(a), norm(b)
        return bool(na) and na == nb

    async def search(self, url: str) -> Optional[str]:
        async with self._op_lock:
            # 检查是否已有同URL的页面
            for i, p in enumerate(self.all_pages):
                if self._same_url(p.url, url):
                    await self._ensure_page(i)
                    return None

            # ===== 标签页上限保护 =====
            if len(self.all_pages) >= self.max_pages:
                # 关闭最旧的标签页
                old_page = self.all_pages.pop(0)
                try:
                    await old_page.close()
                except Exception:
                    pass
                logger.warning(
                    f"[BrowserCore] 标签页已达上限({self.max_pages})，自动关闭最旧标签页"
                )

            page = await self._require_context().new_page()
            try:
                # domcontentloaded 就够用了；给 15s 上限，慢站点不再卡 30s（实测有 37s 的调用）
                await self._safe_await(
                    page.goto(url, wait_until="domcontentloaded"),
                    timeout=self.nav_timeout,
                )
                zoom_factor = self.config.get("zoom_factor", 1.0)
                await page.evaluate(f"document.body.style.zoom = {zoom_factor};")
                self._remember_zoom(zoom_factor)
            except Exception as e:
                # 超时不等于打不开：页面往往已经渲染出内容，先看看有没有可见文字再决定
                try:
                    body_text = (await page.inner_text("body"))[:120].strip()
                except Exception:
                    body_text = ""
                if body_text:
                    self.all_pages.append(page)
                    self.current_index = len(self.all_pages) - 1
                    self.page = page
                    await self.save_cookies()
                    return None
                await self._discard_page(page)
                return f"URL 访问失败: {e}"

            self.all_pages.append(page)
            self.current_index = len(self.all_pages) - 1
            self.page = page

            await self.save_cookies()
            return None

    # ======================================================
    # 页面交互
    # ======================================================

    async def click_coord(self, coords: Sequence[int]) -> Optional[str]:
        if len(coords) != 2:
            return "坐标参数格式错误"
        x, y = self.map_coords(coords[0], coords[1])

        async with self._op_lock:
            page = await self._ensure_page()
            new_page: Optional[Page] = None

            def on_popup(popup: Page):
                nonlocal new_page, page
                new_page = popup
                self.all_pages.append(popup)
                self.current_index = len(self.all_pages) - 1
                self.page = popup

            page.on("popup", on_popup)
            try:
                await self._safe_page_op(
                    page,
                    self._safe_await(page.mouse.click(x, y, delay=100)),
                )
                await self._settle(page)
            finally:
                page.remove_listener("popup", on_popup)

        return None

    async def scroll_by(self, distance: int, direction: str) -> Optional[str]:
        async with self._op_lock:
            page = await self._ensure_page()
            dx = dy = 0
            if direction == "上":
                dy = -distance
            elif direction == "下":
                dy = distance
            elif direction == "左":
                dx = -distance
            elif direction == "右":
                dx = distance
            else:
                return "无效的滚动方向"

            await self._safe_page_op(
                page,
                page.evaluate(f"window.scrollBy({dx}, {dy});"),
            )
            return None

    async def text_input(self, text: str, enter: bool = True) -> Optional[str]:
        async with self._op_lock:
            page = await self._ensure_page()
            await page.wait_for_load_state("load")

            inputs = await page.query_selector_all(
                "input:not([disabled]):not([readonly])"
            )
            for el in inputs:
                if await el.is_visible():
                    await el.fill(text)
                    if enter:
                        await page.keyboard.press("Enter")
                    return None
            return "未找到可用的输入框"

    # ======================================================
    # 选择器解析工具
    # ======================================================

    async def _resolve_element(
        self, page: Page, selector: str, selector_type: str = "css"
    ):
        if selector_type == "xpath":
            return await page.query_selector(f"xpath={selector}")
        else:
            return await page.query_selector(selector)

    async def _precheck_visible(self, el, selector: str) -> Optional[str]:
        """元素是否真的可见。隐藏元素（display:none / 折叠菜单）立刻返回提示，
        不再让 scroll/hover/click 各白等一次（实测隐藏按钮要白等 7~8s）。"""
        try:
            if not await el.is_visible():
                return (f"元素【{selector}】存在但当前不可见（被隐藏或未展开）——"
                        f"先 hover 它的父级/先点开菜单，或改用 browser_find_elements 看有没有别的可见元素")
        except Exception:
            pass
        return None

    async def _resolve_elements(
        self, page: Page, selector: str, selector_type: str = "css"
    ):
        if selector_type == "xpath":
            return await page.query_selector_all(f"xpath={selector}")
        else:
            return await page.query_selector_all(selector)

    # ======================================================
    # 页面源码获取（大小保护）
    # ======================================================

    async def get_page_source(self) -> Optional[str]:
        async with self._op_lock:
            page = await self._ensure_page()
            try:
                content = await page.content()
                # 限制源码大小，超过5MB截断
                max_bytes = 5 * 1024 * 1024
                if len(content.encode('utf-8')) > max_bytes:
                    content = content[:max_bytes // 4]  # 粗略截断
                    logger.warning("[BrowserCore] 页面源码超过5MB，已截断")
                return content
            except Exception:
                return None

    # ======================================================
    # 基于选择器的元素操作
    # ======================================================

    async def click_element(
        self, selector: str, selector_type: str = "css"
    ) -> Optional[str]:
        async with self._op_lock:
            page = await self._ensure_page()
            new_page: Optional[Page] = None

            def on_popup(popup: Page):
                nonlocal new_page, page
                new_page = popup
                self.all_pages.append(popup)
                self.current_index = len(self.all_pages) - 1
                self.page = popup

            page.on("popup", on_popup)
            try:
                el = await self._resolve_element(page, selector, selector_type)
                if el is None:
                    return f"未找到选择器【{selector}】对应的元素"
                err = await self._precheck_visible(el, selector)
                if err:
                    return err
                try:
                    await el.scroll_into_view_if_needed(timeout=1000)
                except Exception:
                    pass
                try:
                    await el.hover(timeout=800)
                except Exception:
                    pass
                try:
                    await el.click(delay=100, timeout=self.action_timeout_ms)
                except Exception:
                    # 被别的元素盖住/动画没停：先试强制点，不行才报错（原来是白等 30s）
                    await el.click(delay=100, timeout=1000, force=True)
                await self._settle(page)
            except Exception as e:
                return f"点击元素失败（元素存在但点不动，可能被遮挡或不可见）: {str(e)}"
            finally:
                page.remove_listener("popup", on_popup)
        return None

    async def text_input_by_selector(
        self, selector: str, text: str, selector_type: str = "css"
    ) -> Optional[str]:
        async with self._op_lock:
            page = await self._ensure_page()
            el = await self._resolve_element(page, selector, selector_type)
            if el is None:
                return f"未找到选择器【{selector}】对应的元素"
            err = await self._precheck_visible(el, selector)
            if err:
                return err
            try:
                await el.scroll_into_view_if_needed(timeout=1000)
            except Exception:
                pass
            try:
                await el.fill(text, timeout=self.action_timeout_ms)
            except Exception:
                # 有些输入框 Playwright 判定"不可编辑"，直接强填 + 补发 input 事件
                try:
                    await el.fill(text, timeout=1000, force=True)
                except Exception:
                    await el.evaluate("(el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); }", text)
            return None

    async def find_elements(
        self, selector: str, selector_type: str = "css", attribute: Optional[str] = None
    ) -> list[dict] | str:
        async with self._op_lock:
            page = await self._ensure_page()
            elements = await self._resolve_elements(page, selector, selector_type)
            if not elements:
                return f"未找到选择器【{selector}】对应的元素"

            result = []
            # 限制返回元素数量，防止LLM上下文爆炸（20 个足够决策，越小上下文越快）
            max_elements = int(self.config.get("find_elements_max") or 20)
            for el in elements[:max_elements]:
                info = {}
                try:
                    info["tag"] = await el.evaluate("el => el.tagName.toLowerCase()")
                    info["text"] = (await el.inner_text()).strip()[:120]
                except Exception:
                    info["tag"] = "unknown"
                    info["text"] = ""

                if attribute:
                    try:
                        if attribute == "innerText":
                            info[attribute] = (await el.inner_text()).strip()[:500]
                        elif attribute == "outerHTML":
                            info[attribute] = (await el.evaluate("el => el.outerHTML"))[:500]
                        else:
                            val = await el.get_attribute(attribute)
                            info[attribute] = val if val else ""
                    except Exception:
                        info[attribute] = ""

                try:
                    attrs = await el.evaluate("""el => {
                        const attrs = {};
                        for (const attr of el.attributes) {
                            attrs[attr.name] = attr.value;
                        }
                        return attrs;
                    }""")
                    info["attributes"] = attrs
                except Exception:
                    info["attributes"] = {}

                result.append(info)

            if len(elements) > max_elements:
                result.append({"note": f"...还有 {len(elements) - max_elements} 个元素未返回"})

            return result

    async def get_element_text(
        self, selector: str, selector_type: str = "css"
    ) -> Optional[str]:
        async with self._op_lock:
            page = await self._ensure_page()
            el = await self._resolve_element(page, selector, selector_type)
            if el is None:
                return None
            try:
                text = (await el.inner_text()).strip()
                # 限制文本长度，防止LLM上下文过大（中文 ~1 token/字，5000 字就是 3000+ token）
                return text[:3000]
            except Exception:
                return None

    async def get_element_attribute(
        self, selector: str, attribute_name: str, selector_type: str = "css"
    ) -> Optional[str]:
        async with self._op_lock:
            page = await self._ensure_page()
            el = await self._resolve_element(page, selector, selector_type)
            if el is None:
                return None
            try:
                return await el.get_attribute(attribute_name)
            except Exception:
                return None

    async def wait_for_element(
        self, selector: str, timeout: float = 30, selector_type: str = "css"
    ) -> Optional[str]:
        async with self._op_lock:
            page = await self._ensure_page()
            try:
                if selector_type == "xpath":
                    await page.wait_for_selector(f"xpath={selector}", timeout=timeout * 1000)
                else:
                    await page.wait_for_selector(selector, timeout=timeout * 1000)
                return None
            except Exception as e:
                return f"等待元素超时或失败: {str(e)}"

    # ======================================================
    # 鼠标悬停 / 键盘 / 按文字点击 / 批量动作
    # ======================================================

    async def hover(self, x: int, y: int) -> Optional[str]:
        """把鼠标移到坐标（有些站点的按钮只在悬停时出现）。坐标同样按截图像素给。"""
        x, y = self.map_coords(x, y)
        async with self._op_lock:
            page = await self._ensure_page()
            await self._safe_page_op(page, self._safe_await(page.mouse.move(int(x), int(y), steps=8)))
            await asyncio.sleep(0.2)
            return None

    async def hover_element(self, selector: str, selector_type: str = "css") -> Optional[str]:
        """把鼠标移到元素上（先滚到可见区域）。"""
        async with self._op_lock:
            page = await self._ensure_page()
            el = await self._resolve_element(page, selector, selector_type)
            if el is None:
                return f"未找到选择器【{selector}】对应的元素"
            err = await self._precheck_visible(el, selector)
            if err:
                return err
            try:
                await el.scroll_into_view_if_needed(timeout=1000)
                await el.hover(timeout=self.action_timeout_ms)
            except Exception as e:
                return f"悬停失败（可能被遮挡/不可见）: {e}"
            await asyncio.sleep(0.2)
            return None

    # Playwright 的键名是 KeyboardEvent.key/code 的风格：写 "Ctrl"、"Esc" 会直接报
    # Unknown key（实测 Niko 2026-09-17 23:22 因此浪费了一轮往返）。这里做个别名归一。
    _KEY_ALIASES = {
        "ctrl": "Control", "control": "Control", "ctl": "Control",
        "esc": "Escape", "escape": "Escape",
        "del": "Delete", "delete": "Delete",
        "return": "Enter", "ent": "Enter",
        "space": " ", "spacebar": " ", "空格": " ",
        "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
        "上": "ArrowUp", "下": "ArrowDown", "左": "ArrowLeft", "右": "ArrowRight",
        "win": "Meta", "cmd": "Meta", "command": "Meta", "super": "Meta",
        "caps": "CapsLock", "capslock": "CapsLock", "tab": "Tab",
        "pgup": "PageUp", "pageup": "PageUp", "pgdn": "PageDown", "pagedown": "PageDown",
        "ins": "Insert", "insert": "Insert",
        "shift": "Shift", "alt": "Alt", "option": "Alt",
        "backspace": "Backspace", "back": "Backspace",
    }

    @classmethod
    def normalize_key(cls, key: str) -> str:
        k = (key or "").strip()
        if not k:
            return k
        low = k.lower()
        if low in cls._KEY_ALIASES:
            return cls._KEY_ALIASES[low]
        # 单字母统一成小写（Playwright 两种都认，但统一后定时更稳）
        if len(k) == 1 and k.isalpha():
            return k.lower()
        return k

    async def press_key(self, key: str, hold_ms: int = 0) -> Optional[str]:
        """按键；hold_ms > 0 时按住再松开（方向键走位用）。"""
        async with self._op_lock:
            key = self.normalize_key(key)
            page = await self._ensure_page()
            try:
                if hold_ms and hold_ms > 0:
                    await page.keyboard.down(key)
                    await asyncio.sleep(min(hold_ms, 10000) / 1000.0)
                    await page.keyboard.up(key)
                else:
                    await page.keyboard.press(key)
            except Exception as e:
                return f"按键失败({key}): {e}"
            return None

    async def key_down(self, key: str) -> Optional[str]:
        async with self._op_lock:
            key = self.normalize_key(key)
            page = await self._ensure_page()
            try:
                await page.keyboard.down(key)
            except Exception as e:
                return f"按下失败({key}): {e}"
            return None

    async def key_up(self, key: str) -> Optional[str]:
        async with self._op_lock:
            key = self.normalize_key(key)
            page = await self._ensure_page()
            try:
                await page.keyboard.up(key)
            except Exception as e:
                return f"松开失败({key}): {e}"
            return None

    # ======================================================
    # 按文字找元素（JS 扫描，替代 Playwright 的 locator 等待）
    # ======================================================

    # 为什么不用 page.get_by_text(...).wait_for(...)：那套在"找不到/不可见"时要
    # 白等满 5s（实测 miss 一次 5.01s），而模型每轮往返本来就要 ~3.8s，一次 miss
    # 就是 9s 没了。这里改成一次性 JS 扫描：找得到就给坐标、找不到立刻返回近似候选。
    _TEXT_SCAN_JS = """
    (args) => {
      const needle = (args.needle || '').replace(/\\s+/g, ' ').trim();
      const exact = !!args.exact, limit = args.limit || 6;
      const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
      const matches = [];
      const near = [];
      const els = document.querySelectorAll('body *');
      for (const el of els) {
        const own = norm(el.innerText || el.textContent || '');
        if (!own) continue;
        // 只看"自己直接承载文字"的元素，避免命中整个 body/容器
        let childLen = 0;
        for (const c of el.children) childLen += norm(c.innerText || c.textContent || '').length;
        if (childLen >= own.length) continue;
        const hit = exact ? (own === needle) : own.includes(needle);
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        const visible = r.width > 1 && r.height > 1 && st.visibility !== 'hidden'
                        && st.display !== 'none' && parseFloat(st.opacity || '1') > 0.05;
        const tag = el.tagName.toLowerCase();
        const clickable = !!(el.onclick || tag === 'button' || tag === 'a' || tag === 'label'
                             || tag === 'input' || el.getAttribute('role') === 'button'
                             || st.cursor === 'pointer');
        if (hit) {
          matches.push({text: own.slice(0, 40), tag: tag, x: r.left + r.width / 2,
                        y: r.top + r.height / 2, w: Math.round(r.width), h: Math.round(r.height),
                        visible: visible, clickable: clickable});
          if (matches.length >= limit) break;
        } else if (near.length < 8 && own.length <= 60) {
          near.push(own.slice(0, 24));
        }
      }
      matches.sort((a, b) => (b.visible - a.visible) || (b.clickable - a.clickable)
                             || (a.w * a.h) - (b.w * b.h));
      return {matches: matches, near: near, url: location.href, title: document.title};
    }
    """

    async def _scan_text(self, page: Page, needle: str, exact: bool = False,
                         limit: int = 6) -> dict:
        try:
            return await page.evaluate(self._TEXT_SCAN_JS,
                                       {"needle": needle, "exact": exact, "limit": limit})
        except Exception as e:
            return {"matches": [], "near": [], "error": str(e)}

    async def _click_point(self, page: Page, x: float, y: float) -> None:
        """真实鼠标：先移上去（触发 hover 才出现的按钮）再点。"""
        await page.mouse.move(x, y, steps=6)
        await asyncio.sleep(0.12)
        await page.mouse.click(x, y, delay=60)

    async def _click_text_fast(self, page: Page, text: str, exact: bool = False) -> Optional[str]:
        """按文字点击（快路径）。返回 None 表示成功，否则是错误说明。"""
        scan = await self._scan_text(page, text, exact=exact)
        best = next((m for m in scan.get("matches", []) if m.get("visible")), None)
        if best is None:
            near = "、".join(dict.fromkeys(n for n in scan.get("near", []) if n))[:160]
            hint = f"（页面上的近似文字：{near}）" if near else ""
            return f"没有可见的元素包含文字【{text}】{hint}"
        # 滚到视口中间后再取一次坐标（滚动会让坐标失效）
        try:
            await page.evaluate("(y) => window.scrollBy(0, y - window.innerHeight / 2)", best["y"])
            await asyncio.sleep(0.1)
            scan2 = await self._scan_text(page, text, exact=exact)
            best = next((m for m in scan2.get("matches", []) if m.get("visible")), best)
        except Exception:
            pass
        try:
            await self._click_point(page, best["x"], best["y"])
        except Exception as e:
            return f"点击文字【{text}】失败: {e}"
        return None

    async def click_text(self, text: str, exact: bool = False) -> Optional[str]:
        """按可见文字点击：JS 扫坐标 → 滚到可见 → **先悬停再点**。

        很多站点（菜单、播放器控件）要"光标先移上去"才把按钮渲染出来，
        单纯 click 会点到空处；这里固定先移上去再点。
        """
        async with self._op_lock:
            page = await self._ensure_page()
            err = await self._click_text_fast(page, text, exact)
            if err:
                return err
            await self._settle(page)
            return None

    # 页面"摘要"：一次 JS 调用拿到 标题/URL/可点元素/正文开头。
    # 目的：把 Niko 原本要单独发起的 browser_get_tabs / browser_get_element_text
    # 往返（每次 ~3.8s 模型往返 + 工具时间）省掉——动作结果里直接带上。
    _PAGE_DIGEST_JS = """
    (maxText) => {
      const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
      const items = [], seen = new Set();
      const nodes = document.querySelectorAll(
        'a,button,input,label,select,textarea,[role=button],[onclick],[class*=btn],[class*=button]');
      for (const el of nodes) {
        const t = norm(el.innerText || el.value || el.getAttribute('aria-label') || el.title || '');
        if (!t || t.length > 16 || seen.has(t)) continue;
        const r = el.getBoundingClientRect(), st = getComputedStyle(el);
        if (r.width < 4 || r.height < 4) continue;
        if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity || '1') < 0.05) continue;
        seen.add(t);
        items.push(t);
        if (items.length >= 12) break;
      }
      const body = document.body ? norm(document.body.innerText) : '';
      // 画布/指针锁定：canvas 游戏（DOS 模拟器、网页小游戏）要不要"点一下抓鼠标"看这里
      let canvas = null;
      let best = 0;
      for (const c of document.querySelectorAll('canvas')) {
        const r = c.getBoundingClientRect();
        const area = r.width * r.height;
        if (area > best) { best = area; canvas = {w: Math.round(r.width), h: Math.round(r.height),
                                                 x: Math.round(r.left), y: Math.round(r.top)}; }
      }
      return {
        title: norm(document.title).slice(0, 80),
        url: location.href,
        buttons: items,
        text: body.slice(0, maxText || 0),
        textLen: body.length,
        canvas: canvas,
        canvasCount: document.querySelectorAll('canvas').length,
        gameLoading: (!canvas) && (!!window.emulators
            || !!document.querySelector('[class*=jsdos],[id*=jsdos],[class*=emulator],[class*=dosbox]')),
        pointerLock: !!document.pointerLockElement,
        activeTag: (document.activeElement && document.activeElement.tagName) || '',
        zoom: (document.body && document.body.style && document.body.style.zoom) || '1',
      };
    }
    """

    async def page_digest(self, max_text: int = 120) -> dict:
        """一次 JS 调用拿页面摘要（标题/URL/可点元素/正文开头）。失败返回空字典。"""
        async with self._op_lock:
            page = await self._ensure_page()
            try:
                data = await page.evaluate(self._PAGE_DIGEST_JS, int(max_text))
            except Exception:
                return {}
            if not isinstance(data, dict):
                return {}
            # 当前坐标系（模型给的坐标按截图像素算，这里的数字会写进页面摘要）
            data["shot"] = self.shot_space()
            # 顺手带上标签页清单：Niko 以前专门发一次 browser_get_tabs（实测一段会话 3 次）
            try:
                titles = await asyncio.gather(*(p.title() for p in self.all_pages))
                data["tabs"] = [str(t or "").strip()[:40] for t in titles]
                data["current_tab"] = (self.current_index or 0) + 1
            except Exception:
                pass
            return data

    _CANVAS_STATE_JS = """
    () => {
      let best = null, area = 0;
      for (const c of document.querySelectorAll('canvas')) {
        const r = c.getBoundingClientRect();
        if (r.width * r.height > area) { area = r.width * r.height; best = c; }
      }
      if (!best) return null;
      const r = best.getBoundingClientRect();
      return {w: Math.round(r.width), h: Math.round(r.height),
              x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
              locked: !!document.pointerLockElement};
    }
    """

    async def click_canvas(self) -> Optional[str]:
        """点最大的那块 canvas 的中心。

        用途：网页版 DOS 模拟器/小游戏会停在 "Click to capture mouse"，
        需要**真实鼠标点一下画面**才会把鼠标（和键盘焦点）交给游戏。
        这里不依赖模型估坐标，直接取 canvas 的几何中心，规避坐标换算误差。
        """
        async with self._op_lock:
            page = await self._ensure_page()
            try:
                info = await self._quick(page.evaluate(self._CANVAS_STATE_JS), 5.0)
            except asyncio.TimeoutError:
                return "页面忙（主线程被占满，可能在加载游戏/WASM）——等 5~10 秒再试一次"
            if not info:
                return "页面上没有 canvas 元素"
            # 画布比视口还大（常见于"缩放着+全屏"）：先把 body zoom 复位，
            # 否则游戏画面被放大到只看得见左上角一块。
            reset = await page.evaluate("""() => {
                const c = [...document.querySelectorAll('canvas')]
                    .reduce((a, b) => {
                        const ra = a ? a.getBoundingClientRect() : null, rb = b.getBoundingClientRect();
                        return (!ra || rb.width * rb.height > ra.width * ra.height) ? b : a;
                    }, null);
                const z = (document.body && document.body.style.zoom) || '1';
                const r = c ? c.getBoundingClientRect() : null;
                const tooBig = r && (r.width > window.innerWidth * 1.02 || r.height > window.innerHeight * 1.02);
                if (tooBig && z !== '1') { document.body.style.zoom = '1'; return z; }
                return '';
            }""")
            if reset:
                logger.info(f"[BrowserCore] 游戏画布超出视口，已把页面缩放从 {reset} 复位到 1.0")
                self._remember_zoom(1.0)
                await asyncio.sleep(0.4)
                info = await page.evaluate(self._CANVAS_STATE_JS)
                if not info:
                    return "页面上没有 canvas 元素"
            try:
                await self._quick(page.mouse.move(info["x"], info["y"], steps=8), 5.0)
                await asyncio.sleep(0.15)
                await self._quick(page.mouse.click(info["x"], info["y"], delay=60), 6.0)
                await asyncio.sleep(0.8)
                # 许多站点要第二次点击才真正接管（第一次只是聚焦）
                if not (await self._quick(page.evaluate("() => !!document.pointerLockElement"), 5.0)):
                    await self._quick(page.mouse.click(info["x"], info["y"], delay=60), 6.0)
                    await asyncio.sleep(0.6)
                await self._quick(page.keyboard.press("Enter"), 5.0)
                await asyncio.sleep(0.4)
                locked = await self._quick(page.evaluate("() => !!document.pointerLockElement"), 5.0)
            except asyncio.TimeoutError:
                return "点了画面但页面没响应（主线程忙）——等几秒再试，或用 browser_screenshot 看一眼当前状态"
            self._canvas_locked = bool(locked)
            self._canvas_size = (info["w"], info["h"])
            self._canvas_zoom_reset = bool(reset)
            return None

    def flash_enabled(self) -> bool:
        return bool(self.flash_ruffle and self._ruffle_url)

    async def play_swf(self, swf_url: str, base: Optional[str] = None) -> Optional[str]:
        """用本地 Ruffle 播放页打开 .swf。

        - 带 http(s):// 的远端地址：由本地服务 `/fetch` 取回再播（绕开 CORS）
        - 不带协议的相对路径：当成插件 `vendor/ruffle/` 里的本地文件，
          直接走本地静态服务（比 `python3 -m http.server` 单线程服务 14MB wasm 快得多）
        """
        if not self.flash_enabled():
            if not self.flash_ruffle:
                return "Flash 支持被关闭了（面板 → flash_ruffle 打开）"
            return "Ruffle 未就绪：vendor/ruffle/ruffle.js 缺失，或本地静态服务没起来"
        await self._close_other_player_tabs()
        target = (swf_url or "").strip()
        if target and not target.startswith(("http://", "https://")):
            rel = target.lstrip("/")
            root = Path(__file__).resolve().parents[1] / "vendor" / "ruffle"
            if (root / rel).is_file():
                local = self._ruffle.base_url + rel
                if rel.lower().endswith(".swf"):
                    return await self.search(self._ruffle.player_url(local))
                return await self.search(local)   # 已经是 html 播放页就直接开
            return (f"本地没有 {rel}（插件 vendor/ruffle 目录里找不到）。"
                    f"要么给完整的 http(s) .swf 地址，要么先把文件放到 vendor/ruffle/ 下")
        url = self._ruffle.player_url(target, base)
        return await self.search(url)

    async def _close_other_player_tabs(self, keep: int = 0) -> int:
        """关掉其它 Ruffle 播放页。

        为什么要：每个 Ruffle（WASM）实例都能吃满一个核，这台 NAS 只有 4 核，
        实测同时开 3 个 Flash 页时 load 冲到 4.57 ⇒ 每个浏览器操作都要等 10~30 秒。
        """
        closed = 0
        try:
            pages = list(self.all_pages)
            for p in pages:
                try:
                    if "player.html" not in (p.url or ""):
                        continue
                    if p is self.page and keep:
                        continue
                    await p.close()
                    closed += 1
                    if p in self.all_pages:
                        self.all_pages.remove(p)
                except Exception:
                    continue
            if closed:
                logger.info(f"[BrowserCore] 关掉 {closed} 个旧的 Flash 播放页（避免多开把 CPU 吃满）")
            if self.all_pages:
                self.current_index = min(self.current_index or 0, len(self.all_pages) - 1)
            else:
                self.current_index = None
        except Exception:
            pass
        return closed

    async def swf_state(self) -> dict:
        """本地播放页的 Ruffle 状态（loading/ready/maybe/error）+ 画布尺寸。"""
        async with self._op_lock:
            page = await self._ensure_page()
            try:
                return await self._quick(page.evaluate("""() => {
                    const el = document.querySelector('ruffle-player');
                    let canvas = null;
                    try {
                        const c = el && el.shadowRoot && el.shadowRoot.querySelector('canvas');
                        const r = c ? c.getBoundingClientRect() : null;
                        canvas = r ? Math.round(r.width) + 'x' + Math.round(r.height) : null;
                    } catch (e) {}
                    return {ruffle: (document.body && document.body.dataset.ruffle) || '',
                            url: location.href, canvas: canvas};
                }"""), 6.0)
            except Exception:
                return {}

    # ======================================================
    # 表单 / 问卷 / 选择题：一次读全、一次答完
    # ======================================================

    _FORM_JS = """
    () => {
      const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
      const vis = e => { if (!e) return false; const r = e.getBoundingClientRect();
        const st = getComputedStyle(e); return r.width > 1 && r.height > 1
          && st.visibility !== 'hidden' && st.display !== 'none'; };
      const labelOf = (e) => {
        let t = '';
        try { if (e.id) { const l = document.querySelector('label[for="' + e.id + '"]'); if (l) t = norm(l.innerText); } } catch (x) {}
        if (!t) { const l = e.closest('label'); if (l) t = norm(l.innerText); }
        if (!t) t = norm(e.value || '');
        return t.slice(0, 60);
      };
      const groups = new Map();
      for (const e of document.querySelectorAll('input[type=radio], input[type=checkbox]')) {
        const k = e.name || ('__anon' + groups.size);
        if (!groups.has(k)) groups.set(k, []);
        groups.get(k).push(e);
      }
      const out = [];
      let n = 0;
      for (const [name, els] of groups) {
        n++;
        let q = '';
        const holder = els[0].closest('article, .question, fieldset, li, form > div, div');
        if (holder) {
          const badge = holder.querySelector('.badge,[class*=badge]');
          const body = holder.querySelector('h1,h2,h3,h4,h5,legend,p,[class*=title],[class*=question]');
          const parts = [];
          if (badge) parts.push(norm(badge.innerText));
          if (body && body !== badge) parts.push(norm(body.innerText));
          q = (parts.join(' ') || norm(holder.innerText)).slice(0, 140);
        }
        out.push({n, name, kind: els[0].type, question: q,
          answered: els.some(e => e.checked),
          options: els.map((e, i) => ({i: i + 1, value: e.value, text: labelOf(e), checked: e.checked, visible: vis(e)}))});
      }
      const selects = [...document.querySelectorAll('select')].filter(vis).map((e, i) => ({
        n: 's' + (i + 1), name: e.name || e.id || ('select' + (i + 1)), kind: 'select',
        question: labelOf(e) || norm(e.getAttribute('aria-label') || ''), answered: e.selectedIndex > 0,
        options: [...e.options].map((o, j) => ({i: j + 1, value: o.value, text: norm(o.text).slice(0, 40), checked: o.selected}))}));
      const texts = [...document.querySelectorAll('input[type=text],input[type=email],textarea')].filter(vis)
        .slice(0, 20).map((e, i) => ({n: 't' + (i + 1), name: e.name || e.id || ('text' + (i + 1)), kind: 'text',
          question: labelOf(e), answered: !!e.value, options: []}));
      return {groups: out, selects, texts};
    }
    """

    async def _form_fields_locked(self, page) -> dict:
        try:
            data = await self._quick(page.evaluate(self._FORM_JS), 8.0)
        except Exception as e:
            return {"error": f"读取表单失败: {e}"}
        return data if isinstance(data, dict) else {}

    async def form_fields(self) -> dict:
        """把页面上的选择题/表单读成结构化数据（纯文本，无截图）。"""
        async with self._op_lock:
            page = await self._ensure_page()
            return await self._form_fields_locked(page)

    async def _click_input(self, page, name: str, value: str = "", index: int = 0) -> bool:
        """点某个选项（优先点它的 label，真实鼠标事件）。"""
        box = await page.evaluate("""(a) => {
          const els = [...document.querySelectorAll('input[type=radio],input[type=checkbox]')];
          let el = null;
          if (a.name) el = els.find(e => (e.name || '') === a.name && (a.value === '' || String(e.value) === String(a.value)));
          if (!el && a.index > 0) el = els[a.index - 1];
          if (!el) return null;
          const t = el.closest('label') || el;
          t.scrollIntoView({block: 'center'});
          const r = t.getBoundingClientRect();
          return {x: r.left + r.width / 2, y: r.top + r.height / 2};
        }""", {"name": name or "", "value": value or "", "index": index})
        if not box:
            return False
        try:
            await self._quick(page.mouse.move(box["x"], box["y"], steps=4), 5.0)
            await self._quick(page.mouse.click(box["x"], box["y"], delay=40), 6.0)
            return True
        except Exception:
            return False

    async def answer_form(self, answers: dict, submit_text: str = "") -> tuple[list, Optional[str]]:
        """按 {"第几题": 第几个选项} 一次点完。

        也接受 {"q24": "2"} 这种"name→value"。返回 (日志, 错误)。
        """
        async with self._op_lock:
            page = await self._ensure_page()
            # 注意：这里必须用无锁版本，否则会自己把自己锁死（answer_form 已持有 _op_lock）
            data = await self._form_fields_locked(page)
            groups = (data or {}).get("groups") or []
            if not groups:
                return [], "页面上没找到可作答的选择题（radio/checkbox）"
            log = []
            for key, val in (answers or {}).items():
                k = str(key).strip()
                target = None
                if k.isdigit():
                    idx = int(k)
                    if 1 <= idx <= len(groups):
                        target = groups[idx - 1]
                if target is None:
                    target = next((g for g in groups if str(g.get("name")) == k), None)
                if target is None:
                    log.append(f"{k}→未找到该题")
                    continue
                opts = target.get("options") or []
                want = str(val).strip()
                pick = None
                if want.isdigit() and 1 <= int(want) <= len(opts):
                    pick = opts[int(want) - 1]
                if pick is None:
                    pick = next((o for o in opts if str(o.get("value")) == want
                                 or want.lower() in str(o.get("text") or "").lower()), None)
                if pick is None:
                    log.append(f"第{target.get('n')}题→没有这个选项({want})")
                    continue
                ok = await self._click_input(page, str(target.get("name") or ""),
                                             str(pick.get("value") or ""), int(pick.get("i") or 0))
                log.append(f"第{target.get('n')}题→选[{pick.get('i')}]{str(pick.get('text'))[:14]}"
                           + ("" if ok else "（点击失败）"))
            err = None
            if submit_text:
                try:
                    await self._quick(page.get_by_text(submit_text).first.click(timeout=3000), 6.0)
                    log.append(f"已点提交：{submit_text}")
                except Exception as e:
                    err = f"提交按钮点击失败: {e}"

            # 自校验：重新读一遍表单，用"实际处于选中状态的题数"证明到底点中没有
            # （答题页选中往往只变一个高亮，整页像素几乎不变，"画面完全相同"不能当失败证据）
            try:
                await asyncio.sleep(0.3)
                after = await self._form_fields_locked(page)
                groups2 = (after or {}).get("groups") or []
                if groups2:
                    answered_now = sum(1 for g in groups2 if g.get("answered"))
                    asked = {str(k) for k in (answers or {}).keys()}
                    picked = [g for g in groups2 if g.get("answered")]
                    log.append(f"校验：该页 {len(groups2)} 题，当前已选中 {answered_now} 题"
                               f"（本次提交了 {len(asked)} 项）"
                               + (" ✅" if answered_now >= len([k for k in asked if str(k).isdigit()]) else " ⚠️ 有没点中的"))
            except Exception:
                pass
            return log, err

    async def canvas_state(self) -> dict:
        """画布尺寸与鼠标锁定状态（不用点，只读，附带本次是否复位过页面缩放）。"""
        async with self._op_lock:
            page = await self._ensure_page()
            try:
                info = await page.evaluate(self._CANVAS_STATE_JS)
            except Exception:
                return {}
            data = info or {}
            if isinstance(data, dict):
                data["zoom_reset"] = bool(getattr(self, "_canvas_zoom_reset", False))
            return data

    async def act(self, actions: list) -> tuple[Optional[str], list[str]]:
        """一次执行多步动作（减少 LLM 往返）。

        支持的动作：
          {"type":"hover","x":1,"y":2} / {"type":"hover","selector":"..."}
          {"type":"click","x":1,"y":2} / {"type":"click","selector":"..."} / {"type":"click","text":"登录"}
          {"type":"text","text":"..."}（打到当前焦点） / {"type":"text","selector":"...","text":"..."}
          {"type":"key","key":"Enter","hold_ms":0}
          {"type":"scroll","distance":800,"direction":"下"}
          {"type":"wait","ms":500}
          {"type":"drag","from":[x1,y1],"to":[x2,y2],"steps":18}  拖拽（挖地道/拉滑块/划线）
        """
        log: list[str] = []
        async with self._op_lock:
            page = await self._ensure_page()
            import time as _time
            t_start = _time.monotonic()
            step_ms: list[int] = []
            for i, raw in enumerate(actions or []):
                if not isinstance(raw, dict):
                    return f"第 {i + 1} 个动作不是对象", log
                step_ms.append(int((_time.monotonic() - t_start) * 1000))
                kind = str(raw.get("type") or "").lower()
                try:
                    if kind == "drag":
                        # 拖拽：挖地道、拖拽滑块、划线类游戏必需（Where's My Water 就是这么玩的）
                        f = raw.get("from") or [raw.get("x1"), raw.get("y1")]
                        t = raw.get("to") or [raw.get("x2"), raw.get("y2")]
                        if not (isinstance(f, (list, tuple)) and isinstance(t, (list, tuple)) and len(f) == 2 and len(t) == 2):
                            return f"第 {i + 1} 步：drag 需要 from/to 两个坐标", log
                        fx, fy = self.map_coords(f[0], f[1])
                        tx, ty = self.map_coords(t[0], t[1])
                        steps = max(4, min(int(raw.get("steps") or 18), 60))
                        await self._quick(page.mouse.move(fx, fy, steps=6), 8.0)
                        await self._quick(page.mouse.down(), 5.0)
                        for k in range(1, steps + 1):
                            await self._quick(
                                page.mouse.move(fx + (tx - fx) * k / steps,
                                                fy + (ty - fy) * k / steps), 5.0)
                        await self._quick(page.mouse.up(), 5.0)
                        log.append(f"{i + 1}. drag ({f[0]},{f[1]})→({t[0]},{t[1]})")
                    elif kind == "canvas":
                        err = await self.click_canvas()
                        if err:
                            return f"第 {i + 1} 步：{err}", log
                        log.append(f"{i + 1}. click_canvas")
                    elif kind == "wait":
                        await asyncio.sleep(min(float(raw.get("ms", 500)) / 1000.0, 15))
                        log.append(f"{i + 1}. wait {raw.get('ms', 500)}ms")
                    elif kind == "scroll":
                        dist = int(raw.get("distance", 800))
                        d = str(raw.get("direction") or "下")
                        dx = dy = 0
                        if d == "上": dy = -dist
                        elif d == "下": dy = dist
                        elif d == "左": dx = -dist
                        elif d == "右": dx = dist
                        await page.evaluate(f"window.scrollBy({dx}, {dy});")
                        log.append(f"{i + 1}. scroll {d} {dist}")
                    elif kind == "key":
                        k = self.normalize_key(str(raw.get("key") or ""))
                        hold = int(raw.get("hold_ms") or 0)
                        if hold > 0:
                            await self._quick(page.keyboard.down(k), 5.0)
                            await asyncio.sleep(min(hold, 10000) / 1000.0)
                            await self._quick(page.keyboard.up(k), 5.0)
                        else:
                            await self._quick(page.keyboard.press(k), 5.0)
                        log.append(f"{i + 1}. key {k}" + (f" hold {hold}ms" if hold else ""))
                    elif kind == "text":
                        if raw.get("selector"):
                            el = await self._resolve_element(page, str(raw["selector"]), str(raw.get("selector_type") or "css"))
                            if el is None:
                                return f"第 {i + 1} 步：未找到 {raw['selector']}", log
                            await el.fill(str(raw.get("text") or ""), timeout=self.action_timeout_ms)
                        else:
                            await page.keyboard.type(str(raw.get("text") or ""))
                        log.append(f"{i + 1}. text {str(raw.get('text'))[:30]!r}")
                    elif kind == "hover":
                        if raw.get("selector"):
                            el = await self._resolve_element(page, str(raw["selector"]), str(raw.get("selector_type") or "css"))
                            if el is None:
                                return f"第 {i + 1} 步：未找到 {raw['selector']}", log
                            await el.scroll_into_view_if_needed(timeout=self.action_timeout_ms)
                            await el.hover(timeout=self.action_timeout_ms)
                            log.append(f"{i + 1}. hover {raw['selector']}")
                        else:
                            mx, my = self.map_coords(raw.get("x", 0), raw.get("y", 0))
                            await page.mouse.move(mx, my, steps=8)
                            log.append(f"{i + 1}. hover ({raw.get('x')},{raw.get('y')})")
                        await asyncio.sleep(0.2)
                    elif kind == "click":
                        if raw.get("text"):
                            err = await self._click_text_fast(page, str(raw["text"]))
                            if err:
                                return f"第 {i + 1} 步：{err}", log
                            log.append(f"{i + 1}. click text={raw['text']!r}")
                        elif raw.get("selector"):
                            el = await self._resolve_element(page, str(raw["selector"]), str(raw.get("selector_type") or "css"))
                            if el is None:
                                return f"第 {i + 1} 步：未找到 {raw['selector']}", log
                            try:
                                await el.scroll_into_view_if_needed(timeout=self.action_timeout_ms)
                                await el.hover(timeout=self.action_timeout_ms)
                                await el.click(timeout=self.action_timeout_ms)
                            except Exception:
                                await el.click(timeout=1000, force=True)
                            log.append(f"{i + 1}. click {raw['selector']}")
                        else:
                            mx, my = self.map_coords(raw.get("x", 0), raw.get("y", 0))
                            await self._quick(page.mouse.move(mx, my, steps=8), 8.0)
                            await self._quick(page.mouse.click(mx, my, delay=80), 8.0)
                            log.append(f"{i + 1}. click ({raw.get('x')},{raw.get('y')})")
                        await self._settle(page, quiet=float(raw.get("after_ms", 300)) / 1000.0)
                    else:
                        return f"第 {i + 1} 个动作类型不支持: {kind}", log
                except asyncio.TimeoutError:
                    return (f"第 {i + 1} 步({kind})超时：页面没响应（主线程忙或元素卡住），"
                            f"已完成的步骤仍然生效"), log
                except Exception as e:
                    return f"第 {i + 1} 步({kind})失败: {e}", log
            # 附上每步的真实时刻（相对本次调用的起点），便于和页面侧记录对齐，
            # 也便于看出"计划 6s 实际 6.2s"这类累积漂移。
            try:
                for idx in range(len(log)):
                    if idx < len(step_ms):
                        log[idx] = f"{log[idx]} [+{step_ms[idx]}ms]"
                total = int((_time.monotonic() - t_start) * 1000)
                log.append(f"实际总耗时 {total / 1000:.2f}s")
            except Exception:
                pass
            return None, log

    # ======================================================
    # 网页录屏（Chromium CDP 截屏流 → mp4）
    # ======================================================

    async def record_start(self, max_duration: int | None = None) -> str | None:
        """开始录制当前页面画面。成功返回 None，失败返回错误文本。"""
        async with self._op_lock:
            if self.browser_type != "chromium":
                return f"录屏仅支持 chromium 内核（当前为 {self.browser_type}）"

            page = await self._ensure_page()
            if self.recorder is None:
                # page_provider: 让录屏器能跟随"当前标签页"，切到哪录到哪
                self.recorder = PageRecorder(
                    self.recordings_dir,
                    self.config,
                    page_provider=lambda: self.page,
                )
            # 把音频配置交给录屏器（虚拟声卡地址在 initialize() 里已解析）
            self.recorder.record_audio = self.record_audio and bool(self.audio_server)
            self.recorder.audio_server = self.audio_server

            try:
                info = await self.recorder.start(page, max_duration=max_duration)
            except RecordingError as e:
                return str(e)
            except Exception as e:
                logger.error(f"[BrowserCore] 启动录屏失败: {e}")
                return f"启动录屏失败: {e}"

            self.page = page
            logger.debug(f"[BrowserCore] 录屏已启动: {info}")
            return None

    async def record_stop(self) -> tuple[str | None, dict]:
        """停止录制并合成 mp4。

        Returns:
            (mp4 路径或 None, 信息字典)
        """
        async with self._op_lock:
            if self.recorder is None or not self.recorder.running:
                return None, {"error": "当前没有进行中的录屏"}

            try:
                path, info = await self.recorder.stop()
            except Exception as e:
                logger.error(f"[BrowserCore] 停止录屏失败: {e}")
                return None, {"error": f"停止录屏失败: {e}"}

            return (str(path) if path else None), info

    async def record_status(self) -> dict:
        """查询录屏状态。"""
        if self.recorder is None:
            return {"running": False, "frames": 0, "seconds": 0.0}
        return self.recorder.status

    async def record_abort(self) -> None:
        """放弃录制（关闭浏览器/异常时调用）。"""
        if self.recorder is not None and self.recorder.running:
            try:
                await self.recorder.abort()
            except Exception:
                pass

    async def go_back(self) -> Optional[str]:
        async with self._op_lock:
            page = await self._ensure_page()
            await page.go_back()
            await page.wait_for_load_state("load")
            return None

    async def go_forward(self) -> Optional[str]:
        async with self._op_lock:
            page = await self._ensure_page()
            await page.go_forward()
            await page.wait_for_load_state("load")
            return None
