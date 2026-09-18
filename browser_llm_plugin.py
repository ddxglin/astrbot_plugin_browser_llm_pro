"""
AstrBot LLM 浏览器插件 — 增强资源管控版
基于现有浏览器插件，为每个用户创建持久化缓存文件夹，支持LLM调用
绕过系统对AI执行命令的30秒限制

新增资源限制:
1. 全局最大并发用户数
2. 全局活跃浏览器总数上限
3. 定期清理过期用户浏览器
4. 操作频率限制
5. Supervisor 进程级内存监控
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from astrbot.api import logger
from astrbot.api.message_components import Video
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform import AstrMessageEvent
from astrbot.core.star.register import register_llm_tool

try:  # mcp 是 AstrBot 的依赖；缺失时截图退化为纯文本结果
    import mcp.types as mcp_types
except Exception:  # pragma: no cover
    mcp_types = None

# 注意：必须用「包内相对导入」。绝对导入 `from core.browser import ...` 会让这些模块以
# 顶层名 core.* 注册进 sys.modules，而 AstrBot 重载插件时只清理 `data.plugins.<插件名>`
# 前缀的模块 —— 结果是热重载后插件主体是新的、core/* 仍是进程里最早那份旧代码
# （录屏、跟随标签页等改动都不会生效）。相对导入后模块名为
# data.plugins.astrbot_plugin_browser_llm.core.*，重载时会一并清除并重新导入。
from .core.browser import BrowserCore
from .core.favorite import FavoriteManager
from .core.operate import BrowserOperator
from .core.reaper import LeakReaper
from .core.supervisor import BrowserSupervisor
from .core.ticks_overlay import TickOverlay


class UserBrowserManager:
    """用户浏览器实例管理器 — 增强资源管控版"""

    def __init__(self, base_data_dir: Path, config: dict):
        self.base_data_dir = base_data_dir
        self.config = config

        # 用户浏览器实例池
        self.user_browsers: Dict[str, UserBrowserInstance] = {}
        self._lock = asyncio.Lock()

        # ===== 全局资源限制 =====
        sup_cfg = config.get("supervisor", {})
        self.max_concurrent_users: int = sup_cfg.get("max_concurrent_users", 10)
        self.global_browser_count_limit: int = sup_cfg.get("global_browser_count_limit", 20)

        # ===== 清理任务 =====
        self._cleanup_task: Optional[asyncio.Task] = None
        self._cleanup_interval: int = sup_cfg.get("idle_timeout", 600) // 2  # 闲置超时的一半

    async def initialize(self):
        """启动定期清理任务 + 泄漏回收器"""
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup_loop())
        # 回收器：清掉上一次插件实例遗留的孤儿浏览器（热重载会丢引用），并定期收 chrome 僵尸
        reap_interval = (self.config.get("supervisor") or {}).get("reap_interval", 60)
        self.reaper = LeakReaper(interval=reap_interval)
        await self.reaper.start()

    async def terminate(self):
        """终止管理器，关闭所有浏览器"""
        reaper = getattr(self, "reaper", None)
        if reaper is not None:
            try:
                await reaper.stop()
            except Exception:
                pass

        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        async with self._lock:
            count = len(self.user_browsers)
            for browser_instance in self.user_browsers.values():
                try:
                    await browser_instance.terminate()
                except Exception as e:
                    logger.error(f"关闭用户浏览器失败: {e}")
            self.user_browsers.clear()
            logger.info(f"[资源管理] 已关闭全部 {count} 个用户浏览器实例")

    async def get_user_browser(self, user_id: str, event: Optional[AstrMessageEvent] = None) -> UserBrowserInstance:
        """获取用户的浏览器实例，带全局并发限制"""
        async with self._lock:
            # ===== 已存在则直接返回 =====
            if user_id in self.user_browsers:
                return self.user_browsers[user_id]

            # ===== 全局并发用户数检查 =====
            if len(self.user_browsers) >= self.max_concurrent_users:
                # 尝试清理最不活跃的浏览器
                await self._evict_least_active()
                # 如果还是满了，拒绝
                if len(self.user_browsers) >= self.max_concurrent_users:
                    raise RuntimeError(
                        f"服务器忙碌中，当前活跃用户数已达上限"
                        f"({self.max_concurrent_users}人)，请稍后再试"
                    )

            # ===== 全局浏览器总数检查 =====
            if len(self.user_browsers) >= self.global_browser_count_limit:
                await self._evict_least_active()

            # ===== 创建新实例 =====
            user_data_dir = self.base_data_dir / user_id
            user_data_dir.mkdir(parents=True, exist_ok=True)

            browser_instance = UserBrowserInstance(user_id, user_data_dir, self.config)
            await browser_instance.initialize()
            self.user_browsers[user_id] = browser_instance

            logger.info(
                f"[资源管理] 为用户 {user_id} 创建浏览器实例, "
                f"当前活跃: {len(self.user_browsers)}/{self.max_concurrent_users}"
            )

            return self.user_browsers[user_id]

    async def close_user_browser(self, user_id: str):
        """关闭指定用户的浏览器实例"""
        async with self._lock:
            if user_id in self.user_browsers:
                browser_instance = self.user_browsers[user_id]
                await browser_instance.terminate()
                del self.user_browsers[user_id]
                logger.info(
                    f"[资源管理] 已关闭用户 {user_id} 的浏览器, "
                    f"当前活跃: {len(self.user_browsers)}"
                )

    async def _evict_least_active(self):
        """驱逐最不活跃的浏览器（创建时间最早）"""
        if not self.user_browsers:
            return

        # 按创建时间排序（粗略用 user_id 稳定性排序）
        sorted_users = sorted(
            self.user_browsers.items(),
            key=lambda x: x[1].created_at
        )
        victim_id, victim_instance = sorted_users[0]
        try:
            await victim_instance.terminate()
            del self.user_browsers[victim_id]
            logger.warning(
                f"[资源管理] 驱逐用户 {victim_id} 的浏览器 (资源上限)"
            )
        except Exception as e:
            logger.error(f"[资源管理] 驱逐失败: {e}")

    async def _periodic_cleanup_loop(self):
        """定期清理空闲过期的浏览器"""
        while True:
            try:
                await asyncio.sleep(self._cleanup_interval)
                async with self._lock:
                    stale_ids = []
                    for uid, instance in self.user_browsers.items():
                        if instance.is_stale(self._cleanup_interval * 2):
                            stale_ids.append(uid)

                    for uid in stale_ids:
                        try:
                            await self.user_browsers[uid].terminate()
                            del self.user_browsers[uid]
                            logger.info(
                                f"[资源管理] 自动清理用户 {uid} 的过期浏览器"
                            )
                        except Exception as e:
                            logger.error(f"[资源管理] 清理失败: {e}")

                    if stale_ids:
                        logger.info(
                            f"[资源管理] 清理了 {len(stale_ids)} 个过期浏览器, "
                            f"当前活跃: {len(self.user_browsers)}"
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[资源管理] 清理循环异常: {e}")

    def get_status(self) -> dict:
        """获取管理器状态"""
        return {
            "active_users": len(self.user_browsers),
            "max_concurrent_users": self.max_concurrent_users,
            "global_browser_count_limit": self.global_browser_count_limit,
            "users": list(self.user_browsers.keys()),
        }


class UserBrowserInstance:
    """单个用户的浏览器实例 — 增强版"""

    def __init__(self, user_id: str, data_dir: Path, config: dict):
        if config is None:
            raise ValueError("config cannot be None")
        self.user_id = user_id
        self.data_dir = data_dir
        self.config = config
        self.created_at = time.time()
        self._last_used = time.time()

        # 用户专属缓存目录
        self.cache_dir = data_dir / "screenshot_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # 用户专属收藏夹管理器
        self.favorite_file = data_dir / "user_favorite.json"
        self.fav_mgr = FavoriteManager(self.favorite_file)

        # 用户专属刻度叠加器
        self.overlay = TickOverlay(data_dir, data_dir / "resource")

        # 用户专属监控器（增强版）
        self.supervisor = BrowserSupervisor(config.copy(), str(data_dir))

        # 用户专属浏览器操作器
        self.operator = BrowserOperator(config, self.fav_mgr, self.overlay, self.supervisor)

        # 核心浏览器对象
        self.browser_core = BrowserCore(config, data_dir)

        self.initialized = False

    @property
    def idle_seconds(self) -> float:
        """获取闲置秒数"""
        return time.time() - self._last_used

    def is_stale(self, max_idle: float = 600) -> bool:
        """判断是否过期（闲置超过阈值）"""
        return self.idle_seconds > max_idle

    def touch(self):
        """更新最后使用时间"""
        self._last_used = time.time()

    async def initialize(self):
        """初始化

        这里**不再**提前拉起自用的那套 chromium（2026-09-17 修）。

        以前这里有 `await self.browser_core.initialize()`：它会立刻启动一整套
        chromium（300~500MB），但本类所有操作其实都走 `self.supervisor.call(...)`，
        而 supervisor 自己还会懒加载它自己的 BrowserCore。结果是**每个用户实例同时
        挂着两套 chromium**：内存守护只能关掉 supervisor 那套，没人用的 browser_core
        那套一直活到实例过期——实测 21:53 创建的两个实例到 22:00 仍在跑（RSS 528MB+），
        这就是"改了 80% 阈值内存还是不下来"的直接原因。
        现在只启动监控循环，真正的浏览器在第一次操作时按需启动。
        """
        if self.initialized:
            return
        await self.supervisor.start()
        self.initialized = True
        self.touch()

    async def terminate(self):
        """终止"""
        if not self.initialized:
            return
        # browser_core 现在按需创建，可能从未 initialize 过，别去碰没启动的 playwright
        core = getattr(self, "browser_core", None)
        if core is not None and getattr(core, "playwright", None) is not None:
            await core.terminate()
        await self.supervisor.stop()
        self.initialized = False

    # ================= 浏览器操作方法（带 touch） =================

    async def search(self, url: str) -> Optional[str]:
        self.touch()
        return await self.supervisor.call("search", url=url)

    async def click_coord(self, coords: List[int]) -> Optional[str]:
        self.touch()
        return await self.supervisor.call("click_coord", coords=coords)

    async def text_input(self, text: str) -> Optional[str]:
        self.touch()
        return await self.supervisor.call("text_input", text=text)

    async def scroll_by(self, distance: int, direction: str) -> Optional[str]:
        self.touch()
        return await self.supervisor.call("scroll_by", distance=distance, direction=direction)

    async def screenshot(self, full_page: bool = False, zoom_factor: Optional[float] = None,
                         max_width: Optional[int] = None) -> Optional[str]:
        self.touch()
        return await self.supervisor.call(
            "screenshot", zoom_factor=zoom_factor, full_page=full_page, max_width=max_width)

    def flash_enabled(self) -> bool:
        """Flash（Ruffle）是否就绪。

        注意：这个判断必须问 **core**（`_ruffle_url` 在 BrowserCore 上），
        不能在实例上瞎猜——之前 `getattr(browser, "flash_enabled", lambda: False)()`
        落在 UserBrowserInstance 上永远拿到 False，于是工具直接回"Flash 支持已关闭"，
        Niko 2026-09-18 09:20 就是这么被挡住的。
        """
        core = getattr(self.supervisor, "browser", None)
        return bool(core is not None and getattr(core, "flash_enabled", lambda: False)())

    async def play_swf(self, swf_url: str, base: Optional[str] = None) -> Optional[str]:
        """打开本地 Ruffle 播放页播放指定的 .swf（Flash 游戏）。"""
        self.touch()
        return await self.supervisor.call("play_swf", _internal=True, swf_url=swf_url, base=base)

    async def form_fields(self) -> dict:
        """页面上的选择题/表单（结构化文本）。"""
        self.touch()
        r = await self.supervisor.call("form_fields", _internal=True)
        return r if isinstance(r, dict) else {}

    async def answer_form(self, answers: dict, submit_text: str = ""):
        """一次答完一批选择题。"""
        self.touch()
        return await self.supervisor.call("answer_form", _internal=True,
                                          answers=answers, submit_text=submit_text)

    async def swf_state(self) -> dict:
        """本地播放页的 Ruffle 状态（loading/ready/maybe/error）+ 画布尺寸。"""
        result = await self.supervisor.call("swf_state", _internal=True)
        return result if isinstance(result, dict) else {}

    async def click_canvas(self) -> Optional[str]:
        """点最大的 canvas 中心（游戏"抓鼠标"用）。"""
        self.touch()
        return await self.supervisor.call("click_canvas", _internal=True)

    async def canvas_state(self) -> dict:
        """画布尺寸 + 鼠标锁定状态（只读）。"""
        result = await self.supervisor.call("canvas_state", _internal=True)
        return result if isinstance(result, dict) else {}

    async def page_digest(self, max_text: int = 120) -> dict:
        """页面摘要（标题/URL/可点元素/正文开头）。内部辅助调用，不计入操作频率限制。"""
        self.touch()
        result = await self.supervisor.call("page_digest", _internal=True, max_text=max_text)
        return result if isinstance(result, dict) else {}

    async def go_back(self) -> Optional[str]:
        self.touch()
        return await self.supervisor.call("go_back")

    async def go_forward(self) -> Optional[str]:
        self.touch()
        return await self.supervisor.call("go_forward")

    async def zoom_to_scale(self, scale: float) -> Optional[str]:
        self.touch()
        return await self.supervisor.call("zoom_to_scale", scale=scale)

    async def get_all_tabs_titles(self) -> List[str]:
        self.touch()
        return await self.supervisor.call("get_all_tabs_titles")

    async def switch_tab(self, index: int) -> Optional[str]:
        self.touch()
        return await self.supervisor.call("switch_tab", index=index)

    async def close_tab(self, index: int) -> str:
        self.touch()
        return await self.supervisor.call("close_tab", index=index)

    # ================= 新功能 =================

    async def get_page_source(self) -> Optional[str]:
        self.touch()
        return await self.supervisor.call("get_page_source")

    async def click_element(self, selector: str, selector_type: str = "css") -> Optional[str]:
        self.touch()
        return await self.supervisor.call("click_element", selector=selector, selector_type=selector_type)

    async def text_input_by_selector(self, selector: str, text: str, selector_type: str = "css") -> Optional[str]:
        self.touch()
        return await self.supervisor.call("text_input_by_selector", selector=selector, text=text, selector_type=selector_type)

    async def find_elements(self, selector: str, selector_type: str = "css", attribute: Optional[str] = None) -> Any:
        self.touch()
        return await self.supervisor.call("find_elements", selector=selector, selector_type=selector_type, attribute=attribute)

    async def get_element_text(self, selector: str, selector_type: str = "css") -> Optional[str]:
        self.touch()
        return await self.supervisor.call("get_element_text", selector=selector, selector_type=selector_type)

    async def get_element_attribute(self, selector: str, attribute_name: str, selector_type: str = "css") -> Optional[str]:
        self.touch()
        return await self.supervisor.call("get_element_attribute", selector=selector, attribute_name=attribute_name, selector_type=selector_type)

    async def wait_for_element(self, selector: str, timeout: float = 30, selector_type: str = "css") -> Optional[str]:
        self.touch()
        return await self.supervisor.call("wait_for_element", selector=selector, timeout=timeout, selector_type=selector_type)

    # ================= 悬停 / 键盘 / 按文字点击 / 批量动作 =================

    async def hover(self, x: int, y: int) -> Optional[str]:
        self.touch()
        return await self.supervisor.call("hover", x=x, y=y)

    async def hover_element(self, selector: str, selector_type: str = "css") -> Optional[str]:
        self.touch()
        return await self.supervisor.call("hover_element", selector=selector, selector_type=selector_type)

    async def press_key(self, key: str, hold_ms: int = 0) -> Optional[str]:
        self.touch()
        return await self.supervisor.call("press_key", key=key, hold_ms=hold_ms)

    async def key_down(self, key: str) -> Optional[str]:
        self.touch()
        return await self.supervisor.call("key_down", key=key)

    async def key_up(self, key: str) -> Optional[str]:
        self.touch()
        return await self.supervisor.call("key_up", key=key)

    async def click_text(self, text: str, exact: bool = False) -> Optional[str]:
        self.touch()
        return await self.supervisor.call("click_text", text=text, exact=exact)

    async def act(self, actions: list):
        """批量动作：按计划时长放宽硬超时。

        为什么：`hard_operation_timeout` 默认 60s，而 Niko 常把 12 步、每步 wait 6s
        的连招合成一次调用（计划 ~72s）⇒ 会被强杀，而且旧逻辑还会顺手重启浏览器，
        页面/游戏状态全丢（2026-09-17 23:34 实测）。
        """
        self.touch()
        planned = 0.0
        for a in actions or []:
            try:
                if isinstance(a, dict) and str(a.get("type") or "").lower() == "wait":
                    planned += min(float(a.get("ms") or 0), 15000.0) / 1000.0
            except Exception:
                pass
        budget = planned * 1.5 + 0.25 * len(actions or []) + 20
        return await self.supervisor.call("act", actions=actions, _timeout=budget)

    # ================= 录屏 =================

    async def record_start(self, max_duration: Optional[int] = None) -> Optional[str]:
        self.touch()
        return await self.supervisor.call("record_start", max_duration=max_duration)

    async def record_stop(self) -> tuple[Optional[str], dict]:
        self.touch()
        result = await self.supervisor.call("record_stop")
        # 资源监控可能在录制中途关掉浏览器，此时 supervisor 返回 None
        if not result:
            return None, {"error": "录屏已中断：浏览器被资源监控关闭（内存超阈值）或已重建"}
        return result

    async def record_status(self) -> dict:
        self.touch()
        status = await self.supervisor.call("record_status")
        return status if isinstance(status, dict) else {"running": False, "frames": 0}


class BrowserLLMPlugin(Star):
    """LLM浏览器插件：增强资源管控版"""

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.base_data_dir = StarTools.get_data_dir("astrbot_plugin_browser_llm")
        self.base_data_dir.mkdir(parents=True, exist_ok=True)

        # 用户浏览器实例缓存
        self.user_browsers: Dict[str, UserBrowserInstance] = {}

        # 资源目录
        self.resource_dir = Path(__file__).resolve().parent / "resource"

        # 收藏夹文件（共享）
        self.favorite_file = Path(__file__).parent / "favorite.json"
        self.fav_mgr = FavoriteManager(self.favorite_file)

    async def initialize(self):
        """插件初始化"""
        sup = self.config.get("supervisor", {}) or {}
        logger.info(
            "[配置] 闲置回收 %ss | 整机内存阈值 %s%% | AstrBot 自身预算 %sMB | 单进程上限 %sMB"
            % (
                sup.get("idle_timeout", 600),
                sup.get("max_memory_percent", 80),
                sup.get("astrbot_memory_budget_mb", 3072),
                sup.get("max_process_memory_mb", 512),
            )
        )
        self.browser_manager = UserBrowserManager(self.base_data_dir, self.config)
        await self.browser_manager.initialize()

    async def terminate(self):
        """插件终止"""
        await self.browser_manager.terminate()

    # ================= 通用工具方法 =================

    async def _get_browser_instance(self, event: AstrMessageEvent) -> UserBrowserInstance:
        """获取用户浏览器实例（带资源检查和统一异常处理）"""
        user_id = str(event.get_sender_id())
        try:
            return await self.browser_manager.get_user_browser(user_id, event)
        except RuntimeError as e:
            raise  # 资源限制错误直接透传
        except Exception as e:
            logger.error(f"获取用户浏览器实例失败: {e}")
            raise RuntimeError(f"浏览器初始化失败: {str(e)}")

    # ================= 录屏辅助 =================

    def _record_max_duration(self) -> int:
        """单次录屏允许的最长秒数（受配置 record_max_duration 限制，上限 300s）"""
        try:
            configured = int(self.config.get("record_max_duration", 120))
        except Exception:
            configured = 120
        # 上限放开到 300s（以前写死 60，用户设了也录不长）
        return max(1, min(configured, 300))

    def _screenshot_width(self) -> int:
        """看图用的默认宽度（越小越快；实测 1920→800 省约 0.9s/次）。"""
        try:
            return max(320, int(self.config.get("screenshot_max_width", 1024)))
        except Exception:
            return 1024

    def _action_screenshot_enabled(self) -> bool:
        return bool(self.config.get("action_screenshot", True))

    def _action_digest_enabled(self) -> bool:
        return bool(self.config.get("action_digest", True))

    def _action_digest_chars(self) -> int:
        try:
            return max(0, min(600, int(self.config.get("action_digest_chars", 120))))
        except Exception:
            return 120

    async def _page_digest_line(self, browser: "UserBrowserInstance") -> str:
        """一行页面摘要（标题 | URL | 可点元素 | 正文开头）。

        为什么做：实测一段 46 步的浏览会话里，**20 步**是纯"打听信息"的往返
        （get_element_text 10 次、get_tabs 3 次、get_source/attribute 各 1 次、
        screenshot 5 次），而每次模型往返中位 3.6s。把这些信息随动作结果一起带回去，
        很多往返就不用发生了。成本约 150~250 token（远小于一次往返的 3.6s）。
        """
        if not self._action_digest_enabled():
            return ""
        try:
            data = await browser.page_digest(self._action_digest_chars())
        except Exception:
            return ""
        if not data:
            return ""
        title = str(data.get("title") or "").strip()
        url = str(data.get("url") or "").strip()
        buttons = [str(b).strip() for b in (data.get("buttons") or []) if str(b).strip()]
        text = str(data.get("text") or "").strip()
        parts = []
        head = " | ".join(x for x in (title, url) if x)
        if head:
            parts.append(f"页面：{head}")
        if buttons:
            parts.append("可点：" + " / ".join(buttons[:10]))
        # canvas 游戏（网页 DOS 模拟器/小游戏）：要不要"点一下抓鼠标"看这两行
        if data.get("gameLoading"):
            parts.append("游戏还在加载（模拟器已就位但画布还没出现）：等 10~20 秒再截图，别急着点")
        if data.get("canvas"):
            cv = data["canvas"]
            lock = "已锁定（游戏正在接管鼠标和键盘）" if data.get("pointerLock") else "未锁定（游戏可能停在『点一下抓鼠标』）"
            parts.append(f"画布：{cv.get('w')}×{cv.get('h')}，鼠标{lock}")
        # 坐标口径：模型在截图上量的像素，插件会自动换算（不然会系统性点偏）
        sp = data.get("shot") or {}
        if sp.get("img_w"):
            parts.append(
                f"坐标：按你看到的截图像素给（当前截图 {sp['img_w']}×{sp['img_h']} "
                f"← 页面 {sp['vp_w']}×{sp['vp_h']}，缩放 {int(float(sp.get('zoom') or 1) * 100)}%），"
                f"插件会自动换算"
            )
        tabs = [str(t).strip() for t in (data.get("tabs") or []) if str(t).strip()]
        if len(tabs) > 1:
            cur = int(data.get("current_tab") or 0)
            parts.append("标签页：" + " / ".join(
                f"{i + 1}.{t[:16]}{'（当前）' if i + 1 == cur else ''}" for i, t in enumerate(tabs)))
        if text:
            parts.append(f"正文开头：{text}")
        if not parts:
            return ""
        return "\n" + "\n".join(parts)

    async def _action_result(self, browser: "UserBrowserInstance", text: str):
        """动作类工具的统一收尾：把**刚截的那张图**直接附在结果里，并附一行页面摘要。

        以前这些工具只回一句"截图已更新"：图白截了（0.1~0.7s + 磁盘），模型还得再花
        一轮往返（实测中位 3.6s）去调 browser_screenshot 才看得到结果。
        现在图与文字一起返回；若画面和上一张完全相同，会额外注明——
        这等于免费告诉模型"这一步没生效"，省掉它盲目重试的往返。
        """
        digest_line = await self._page_digest_line(browser)
        if not self._action_screenshot_enabled():
            return f"{text}{digest_line}"
        try:
            path = await browser.screenshot(max_width=self._screenshot_width())
        except Exception as e:
            logger.warning(f"[动作截图] 失败: {e}")
            return f"{text}{digest_line}"
        if not path:
            return f"{text}{digest_line}"

        note = ""
        try:
            digest = hashlib.md5(Path(path).read_bytes()).hexdigest()
            if digest == getattr(browser, "_last_shot_digest", None):
                # 中性措辞：答题/勾选/输入这类操作，页面常常只变一个高亮，整页像素就是不变的，
                # 旧的"这一步可能没生效"会把 Niko 带偏（2026-09-18 实测它因此怀疑点击无效）
                note = ("（画面与上一张完全相同——若是点选/输入类操作，页面往往只有高亮变化，"
                        "不代表失败；要确认请看 browser_form 的选中状态）")
            browser._last_shot_digest = digest
        except Exception:
            pass
        return self._screenshot_result(path, f"{text}{note}{digest_line}")

    async def _send_video(self, event: AstrMessageEvent, path: str) -> tuple[bool, str]:
        """把录好的视频主动发到当前会话，返回 (是否成功, 说明)。

        为什么不用"yield 一个含视频的消息链让框架发"：那条路（`_execute_local` 里的
        `tool_direct_result`）是 fire-and-forget——只有异常会进日志，成功与否无从得知，
        结果就是工具文案说"已发送"而用户什么都没收到（2026-09-17 实测踩到）。
        这里走和内置 `send_message_to_user` 相同的通道：`Context.send_message(umo, chain)`，
        它至少会返回"有没有找到平台"，于是可以如实回报给模型。
        """
        from astrbot.api.event import MessageChain

        try:
            sent = await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Video.fromFileSystem(path)]),
            )
        except Exception as e:
            logger.error(f"[录屏] 发送视频异常: {e}", exc_info=True)
            return False, f"发送异常: {e}"
        if not sent:
            logger.warning(f"[录屏] 发送视频失败：没有匹配 {event.unified_msg_origin} 的平台")
            return False, "没有找到能发送到该会话的平台"
        logger.info(f"[录屏] 视频已发送: {Path(path).name} -> {event.unified_msg_origin}")
        return True, "已发送"

    @staticmethod
    def _audio_note(info: dict) -> str:
        """给模型的音轨说明：别让它把静音视频说成"有声"。"""
        if not info.get("audio"):
            return "（本次没录到声音）"
        if info.get("audio_silent"):
            return "（本次音轨全程静音：页面没发声或被静音）"
        db = info.get("audio_mean_db")
        return f"（带声音，平均 {db}dB）" if db is not None else "（带声音）"

    @staticmethod
    def _human_size(num_bytes: int) -> str:
        if num_bytes >= 1024 * 1024:
            return f"{num_bytes / 1024 / 1024:.1f}MB"
        return f"{max(1, num_bytes // 1024)}KB"

    @staticmethod
    def _screenshot_result(path: Optional[str], text: str):
        """把截图打包成「文本 + 图片」的工具结果。

        AstrBot 的工具循环只认 ``mcp.types.ImageContent`` 才是图片：拿到后它会缓存该图，
        并在模型支持 image 输入时作为图片塞进下一轮上下文
        （见 ``astrbot/core/agent/runners/tool_loop_agent_runner.py``）。
        以前这里只返回文件路径，模型必须先调 ``astrbot_file_read_tool`` 才能看到画面，
        每次都多花一轮 LLM 往返（实测 2.6~6.2s）。现在图片与路径一起返回。

        任何一步失败都退回原来的纯文本结果，保证截图功能不会因此不可用。
        """
        if mcp_types is None or not path:
            return text
        try:
            data = Path(path).read_bytes()
        except Exception as e:
            logger.warning(f"[截图] 读取截图文件失败，退回纯文本结果: {e}")
            return text
        if not data:
            return text

        suffix = Path(path).suffix.lower()
        mime = "image/png" if suffix == ".png" else "image/jpeg"
        try:
            logger.info(f"[截图] 图片已直接附进工具结果 ({len(data) / 1024:.0f}KB, {mime})")
            return mcp_types.CallToolResult(
                content=[
                    mcp_types.TextContent(type="text", text=text),
                    mcp_types.ImageContent(
                        type="image",
                        data=base64.b64encode(data).decode(),
                        mimeType=mime,
                    ),
                ]
            )
        except Exception as e:
            logger.warning(f"[截图] 构造图片结果失败，退回纯文本结果: {e}")
            return text

    async def _record_finish(self, browser: "UserBrowserInstance"):
        """停止录制并合成，返回 (mp4 路径, 信息) 或 (None, 错误文本)"""
        try:
            path, info = await browser.record_stop()
        except Exception as e:
            return None, f"停止录屏失败: {e}"
        if not path:
            detail = info.get("error") if isinstance(info, dict) else info
            return None, str(detail or "录屏失败")
        if not isinstance(info, dict):
            info = {"path": path}
        return path, info

    # ================= LLM 工具注册 =================

    def _purge_stale_tool_handlers(self) -> None:
        """清掉本模块上一次注册遗留的 LLM 工具 handler 缓存。

        AstrBot 的 handler 缓存键是 ``f"{模块名}_{函数名}"``（见 star_handler.get_handler_or_create），
        而插件热重载时只摘掉命令 handler、**不会**摘掉 LLM 工具 handler。后果是重载后：
          - 工具描述是新的（来自新的 docstring）；
          - 但真正执行的还是进程里第一次注册时的旧函数，而且它闭包在**旧插件实例**上，
            于是旧实例会另起一套浏览器（表现为"刚截过图，get_tabs 却说没有标签页"）。
        升级到 AstrBot 4.28.1 后这个坑才被暴露出来（重载不清 core/*、也不清工具 handler）。
        这里在注册前把本模块的旧 handler 全部摘掉，让下面的注册拿到全新 handler。
        兜底手段：改了某个工具的实现后，把它的函数名换个后缀也能绕开缓存。
        """
        try:
            from astrbot.core.star.star_handler import star_handlers_registry
        except Exception:  # 框架内部结构变化时不阻断插件加载
            return
        try:
            stale = list(star_handlers_registry.get_handlers_by_module_name(__name__))
            for md in stale:
                star_handlers_registry.remove(md)
            if stale:
                logger.info(
                    f"[工具注册] 已清理 {len(stale)} 个遗留的工具 handler 缓存（热重载生效所必需）"
                )
        except Exception as e:
            logger.warning(f"[工具注册] 清理遗留 handler 失败，可能要重启 AstrBot 才能生效: {e}")

    def register_llm_tools(self):
        """注册LLM工具"""
        self._purge_stale_tool_handlers()

        # ===== 诊断：确认注册表里本插件的工具是否出现重名（重载清理不干净会导致旧实现生效）=====
        try:
            mgr = self.context.get_llm_tool_manager()
            func_list = getattr(mgr, "func_list", []) or []
            names = [getattr(t, "name", "") for t in func_list]
            mine = [n for n in names if n.startswith("browser_")]
            dup = sorted({n for n in mine if mine.count(n) > 1})
            if dup:
                logger.warning(f"[诊断] browser_* 工具出现重名: {dup}（重载清理不干净）")
            else:
                logger.debug(f"[诊断] browser_* 工具 {len(mine)} 个，无重名")
        except Exception as e:  # 诊断失败不影响正常注册
            logger.warning(f"[诊断] 读取工具注册表失败: {e}")

        @register_llm_tool(name="browser_open")
        async def browser_open(event: AstrMessageEvent, url: str):
            """
            打开指定网页。

            Args:
                url(string): 要访问的网址
            """
            try:
                browser = await self._get_browser_instance(event)
                result = await browser.search(url)
                if result:
                    return f"访问失败: {result}"
                # 以前这里截了图却只回一句"截图已生成"（图白截了，模型还得再花一轮 3.6s 去看）
                return await self._action_result(browser, f"已打开网页: {url}")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"打开网页失败: {str(e)}"

        @register_llm_tool(name="browser_click")
        async def browser_click(event: AstrMessageEvent, x: int, y: int):
            """
            在指定坐标点击（**坐标按你看到的那张截图的像素给**，插件会自动换算到页面坐标）。

            能按文字/选择器点就别用坐标：browser_click_text / browser_act 更稳。
            游戏画面（canvas）要"点一下抓鼠标"时，用 browser_click_canvas 最省事。

            Args:
                x(number): 截图上的X像素
                y(number): 截图上的Y像素
            """
            try:
                browser = await self._get_browser_instance(event)
                result = await browser.click_coord([x, y])
                if result:
                    return f"点击失败: {result}"
                return await self._action_result(browser, f"已点击坐标({x}, {y})")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"点击失败: {str(e)}"

        @register_llm_tool(name="browser_input")
        async def browser_input(event: AstrMessageEvent, text: str):
            """
            在当前页面的输入框中输入文本。

            Args:
                text(string): 要输入的文本
            """
            try:
                browser = await self._get_browser_instance(event)
                result = await browser.text_input(text)
                if result:
                    return f"输入失败: {result}"
                return await self._action_result(browser, f"已输入文本: {text}")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"输入失败: {str(e)}"

        @register_llm_tool(name="browser_scroll")
        async def browser_scroll(event: AstrMessageEvent, direction: str = "下", distance: int = 1300):
            """
            滚动网页。

            Args:
                direction(string): 滚动方向，可选"上"、"下"、"左"、"右"
                distance(number): 滚动距离，默认1300像素
            """
            try:
                browser = await self._get_browser_instance(event)
                result = await browser.scroll_by(distance, direction)
                if result:
                    return f"滚动失败: {result}"
                return await self._action_result(browser, f"已{direction}滚动{distance}像素")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"滚动失败: {str(e)}"

        @register_llm_tool(name="browser_screenshot")
        async def browser_screenshot(event: AstrMessageEvent, full_page: bool = False, zoom_factor: Optional[float] = None, max_width: int = 0):
            """
            获取当前页面的截图，结果里**直接带着这张图**，你可以直接看到页面画面。

            返回的图片已经在你的上下文里了，不要再调用 astrbot_file_read_tool 去读这个路径。
            需要看页面颜色、布局、图形、验证码、canvas 内容时用它；只要文字内容用
            browser_get_element_text 更快。

            Args:
                full_page(boolean): 是否截取整页，默认False
                zoom_factor(number): 缩放因子，默认None
                max_width(number): 输出图片最大宽度，默认1280。**图越小模型越快**：
                    想快点看图给 800~1024；需要抠细节/看小字时才给 1600 以上
            """
            try:
                browser = await self._get_browser_instance(event)
                screenshot_path = await browser.screenshot(
                    full_page=full_page, zoom_factor=zoom_factor,
                    max_width=max_width or self._screenshot_width())
                return await self._action_result(browser, f"截图已生成并已附在本次结果中: {screenshot_path}")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"截图失败: {str(e)}"

        @register_llm_tool(name="browser_get_source")
        async def browser_get_source(event: AstrMessageEvent, save_to_file: bool = False):
            """
            获取当前页面的 HTML 源代码。

            Args:
                save_to_file(boolean): 是否保存到文件，默认False。设为True时返回文件路径
            """
            try:
                browser = await self._get_browser_instance(event)
                source = await browser.get_page_source()
                if not source:
                    return "获取页面源码失败"

                if save_to_file:
                    source_file = browser.data_dir / f"page_source_{int(time.time())}.html"
                    source_file.write_text(source, encoding='utf-8')
                    return f"页面源码已保存到: {source_file}"
                else:
                    if len(source) > 5000:
                        preview = source[:5000]
                        return (f"页面源码过长，返回前5000字符：\n{preview}"
                                f"\n...（共{len(source)}字符，如需完整内容请设 save_to_file=True）")
                    return f"页面源码：\n{source}"
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"获取页面源码失败: {str(e)}"

        @register_llm_tool(name="browser_click_element")
        async def browser_click_element(event: AstrMessageEvent, selector: str, selector_type: str = "css"):
            """
            通过选择器点击元素。

            Args:
                selector(string): 选择器表达式，如 "#submit-btn"、".btn-primary"、'//button[contains(text(), "登录")]'
                selector_type(string): 选择器类型，"css" 或 "xpath"，默认 "css"
            """
            try:
                browser = await self._get_browser_instance(event)
                result = await browser.click_element(selector, selector_type)
                if result:
                    return f"点击元素失败: {result}"
                return await self._action_result(browser, f"已点击元素(选择器: {selector})")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"点击元素失败: {str(e)}"

        @register_llm_tool(name="browser_input_by_selector")
        async def browser_input_by_selector(event: AstrMessageEvent, selector: str, text: str, selector_type: str = "css"):
            """
            通过选择器在输入框中输入文本。

            Args:
                selector(string): 选择器表达式
                text(string): 要输入的文本
                selector_type(string): 选择器类型，"css" 或 "xpath"，默认 "css"
            """
            try:
                browser = await self._get_browser_instance(event)
                result = await browser.text_input_by_selector(selector, text, selector_type)
                if result:
                    return f"输入失败: {result}"
                return await self._action_result(browser, f"已通过选择器输入文本: {text}")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"输入失败: {str(e)}"

        @register_llm_tool(name="browser_find_elements")
        async def browser_find_elements(event: AstrMessageEvent, selector: str, selector_type: str = "css", attribute: Optional[str] = None):
            """
            查找页面元素并返回其信息。

            Args:
                selector(string): 选择器表达式
                selector_type(string): 选择器类型，"css" 或 "xpath"，默认 "css"
                attribute(string): 可选，指定要获取的属性名，如 "href"、"src"、"innerText"
            """
            try:
                browser = await self._get_browser_instance(event)
                result = await browser.find_elements(selector, selector_type, attribute)
                if isinstance(result, str):
                    return result

                lines = [f"找到 {len(result)} 个元素:"]
                for i, item in enumerate(result):
                    parts = []
                    parts.append(f"[{i + 1}] <{item.get('tag', '?')}>")
                    if item.get('text'):
                        parts.append(f"文本: {item['text'][:100]}")
                    if attribute and item.get(attribute):
                        parts.append(f"{attribute}: {item[attribute]}")
                    lines.append("  ".join(parts))

                return "\n".join(lines)
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"查找元素失败: {str(e)}"

        @register_llm_tool(name="browser_get_element_text")
        async def browser_get_element_text(event: AstrMessageEvent, selector: str, selector_type: str = "css"):
            """
            获取元素的文本内容。

            Args:
                selector(string): 选择器表达式
                selector_type(string): 选择器类型，"css" 或 "xpath"，默认 "css"
            """
            try:
                browser = await self._get_browser_instance(event)
                text = await browser.get_element_text(selector, selector_type)
                if text:
                    return f"元素文本内容: {text[:1000]}"
                return "未找到指定元素或元素无文本内容"
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"获取元素文本失败: {str(e)}"

        @register_llm_tool(name="browser_get_element_attribute")
        async def browser_get_element_attribute(event: AstrMessageEvent, selector: str, attribute_name: str, selector_type: str = "css"):
            """
            获取元素的指定属性值。

            Args:
                selector(string): 选择器表达式
                attribute_name(string): 属性名，如 "href"、"src"、"class"、"id"、"alt"
                selector_type(string): 选择器类型，"css" 或 "xpath"，默认 "css"
            """
            try:
                browser = await self._get_browser_instance(event)
                value = await browser.get_element_attribute(selector, attribute_name, selector_type)
                if value is not None:
                    return f"元素属性 [{attribute_name}] = {value}"
                return "未找到指定元素或属性不存在"
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"获取元素属性失败: {str(e)}"

        @register_llm_tool(name="browser_wait_for_element")
        async def browser_wait_for_element(event: AstrMessageEvent, selector: str, timeout: float = 30, selector_type: str = "css"):
            """
            等待元素出现。

            Args:
                selector(string): 选择器表达式
                timeout(number): 超时时间（秒），默认30秒
                selector_type(string): 选择器类型，"css" 或 "xpath"，默认 "css"
            """
            try:
                browser = await self._get_browser_instance(event)
                result = await browser.wait_for_element(selector, timeout, selector_type)
                if result:
                    return f"等待元素失败: {result}"
                return await self._action_result(browser, f"元素已出现(选择器: {selector})")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"等待元素失败: {str(e)}"

        @register_llm_tool(name="browser_back")
        async def browser_back(event: AstrMessageEvent):
            """
            返回上一页。
            """
            try:
                browser = await self._get_browser_instance(event)
                result = await browser.go_back()
                if result:
                    return f"返回失败: {result}"
                return await self._action_result(browser, "已返回上一页")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"返回失败: {str(e)}"

        @register_llm_tool(name="browser_forward")
        async def browser_forward(event: AstrMessageEvent):
            """
            前往下一页。
            """
            try:
                browser = await self._get_browser_instance(event)
                result = await browser.go_forward()
                if result:
                    return f"前进失败: {result}"
                return await self._action_result(browser, "已前往下一页")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"前进失败: {str(e)}"

        @register_llm_tool(name="browser_zoom")
        async def browser_zoom(event: AstrMessageEvent, scale: float = 1.5):
            """
            缩放页面。

            Args:
                scale(number): 缩放因子，默认1.5
            """
            try:
                browser = await self._get_browser_instance(event)
                result = await browser.zoom_to_scale(scale)
                if result:
                    return f"缩放失败: {result}"
                return await self._action_result(browser, f"已缩放到 {scale} 倍")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"缩放失败: {str(e)}"

        @register_llm_tool(name="browser_close")
        async def browser_close(event: AstrMessageEvent):
            """
            关闭当前用户的浏览器实例。
            """
            user_id = str(event.get_sender_id())
            try:
                await self.browser_manager.close_user_browser(user_id)
                return "浏览器已关闭"
            except Exception as e:
                return f"关闭浏览器失败: {str(e)}"

        @register_llm_tool(name="browser_get_tabs")
        async def browser_get_tabs(event: AstrMessageEvent):
            """
            获取当前用户的标签页列表。
            """
            try:
                browser = await self._get_browser_instance(event)
                titles = await browser.get_all_tabs_titles()
                if titles:
                    return "\n".join(f"{i + 1}. {title}" for i, title in enumerate(titles))
                return "暂无打开的标签页"
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"获取标签页失败: {str(e)}"

        @register_llm_tool(name="browser_switch_tab")
        async def browser_switch_tab(event: AstrMessageEvent, index: int):
            """
            切换到指定标签页。

            Args:
                index(number): 标签页序号（从1开始）
            """
            try:
                browser = await self._get_browser_instance(event)
                result = await browser.switch_tab(index - 1)
                if result:
                    return f"切换标签页失败: {result}"
                return await self._action_result(browser, f"已切换到标签页 {index}")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"切换标签页失败: {str(e)}"

        @register_llm_tool(name="browser_close_tab")
        async def browser_close_tab(event: AstrMessageEvent, index: int):
            """
            关闭指定标签页。

            Args:
                index(number): 标签页序号（从1开始）
            """
            try:
                browser = await self._get_browser_instance(event)
                result = await browser.close_tab(index - 1)
                if result and "已关闭标签页" not in str(result):
                    return f"关闭标签页失败: {result}"
                return str(result) if result else f"已关闭标签页 {index}"
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"关闭标签页失败: {str(e)}"

        # ================= 悬停 / 键盘 / 批量动作 =================

        @register_llm_tool(name="browser_hover")
        async def browser_hover(event: AstrMessageEvent, x: int, y: int):
            """
            把鼠标移到指定坐标（不点击）。有些网站的按钮/菜单只在鼠标悬停时才显示，用它可以"唤醒"。

            Args:
                x(number): 目标X坐标
                y(number): 目标Y坐标
            """
            try:
                browser = await self._get_browser_instance(event)
                err = await browser.hover(x, y)
                if err:
                    return f"悬停失败: {err}"
                return await self._action_result(browser, f"已悬停在 ({x}, {y})，截图如下")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"悬停失败: {str(e)}"

        @register_llm_tool(name="browser_hover_element")
        async def browser_hover_element(event: AstrMessageEvent, selector: str, selector_type: str = "css"):
            """
            把鼠标移到某个元素上（不点击）。适合下拉菜单、悬停才出现的按钮。

            Args:
                selector(string): 选择器表达式
                selector_type(string): "css" 或 "xpath"，默认 "css"
            """
            try:
                browser = await self._get_browser_instance(event)
                err = await browser.hover_element(selector, selector_type)
                if err:
                    return f"悬停失败: {err}"
                return await self._action_result(browser, f"已悬停到元素 {selector}，截图如下")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"悬停失败: {str(e)}"

        @register_llm_tool(name="browser_key")
        async def browser_key(event: AstrMessageEvent, key: str, hold_ms: int = 0):
            """
            按键（键盘操作）。游戏、下拉框、快捷键都靠它。

            key 用 Playwright 的键名：方向键 ArrowUp/ArrowDown/ArrowLeft/ArrowRight、
            Enter、Space(空格)、Escape、Tab、Backspace、PageDown、Home，或单个字符如 "a"、"1"。
            hold_ms > 0 表示**按住**这么久再松（走路/加速用），例如 hold_ms=1500 表示按住 1.5 秒。

            Args:
                key(string): 键名
                hold_ms(number): 按住毫秒数，0=只按一下
            """
            try:
                browser = await self._get_browser_instance(event)
                err = await browser.press_key(key, int(hold_ms or 0))
                if err:
                    return f"按键失败: {err}"
                if int(hold_ms or 0) >= 300:
                    return await self._action_result(
                        browser, f"已按住 {key} {int(hold_ms)}ms，截图如下")
                return f"已按键 {key}" + (f"（按住 {int(hold_ms)}ms）" if hold_ms else "")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"按键失败: {str(e)}"

        @register_llm_tool(name="browser_key_down")
        async def browser_key_down(event: AstrMessageEvent, key: str):
            """
            按住某个键不放（配合 browser_key_up 松开）。需要持续按住方向键时用。

            Args:
                key(string): 键名，如 ArrowRight、Space
            """
            try:
                browser = await self._get_browser_instance(event)
                err = await browser.key_down(key)
                return f"按下 {key} 失败: {err}" if err else f"已按住 {key}（记得用 browser_key_up 松开）"
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"按下失败: {str(e)}"

        @register_llm_tool(name="browser_key_up")
        async def browser_key_up(event: AstrMessageEvent, key: str):
            """
            松开某个键（配合 browser_key_down）。

            Args:
                key(string): 键名
            """
            try:
                browser = await self._get_browser_instance(event)
                err = await browser.key_up(key)
                return f"松开 {key} 失败: {err}" if err else f"已松开 {key}"
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"松开失败: {str(e)}"

        @register_llm_tool(name="browser_form")
        async def browser_form(event: AstrMessageEvent):
            """
            读出页面上的选择题/问卷/表单（题干 + 每个选项的文字），**纯文本、不截图**。

            到处都用得上：人格测试、考试、问卷、注册/设置向导。读完用 browser_answer 一次答完，
            不要去读页面源码、也不要猜 CSS 选择器（又慢又容易错）。

            Args: 无
            """
            try:
                browser = await self._get_browser_instance(event)
                data = await browser.form_fields()
                if data.get("error"):
                    return str(data["error"])
                groups = data.get("groups") or []
                if not groups:
                    return "页面上没找到选择题（radio/checkbox）。如果题目是按钮/div 做的，用 browser_screenshot 看画面、browser_click_text 点文字。"
                lines = []
                for g in groups:
                    mark = "✔" if g.get("answered") else " "
                    opts = " | ".join(f"[{o['i']}]{o['text']}" for o in (g.get("options") or []))
                    lines.append(f"{mark} 第{g['n']}题({g.get('name')}): {g.get('question') or '(无题干)'}\n     {opts}")
                tail = ""
                if data.get("texts"):
                    tail = "\n输入框: " + ", ".join(f"{t['n']}({t.get('question') or t.get('name')})" for t in data["texts"])
                if data.get("selects"):
                    tail += "\n下拉框: " + ", ".join(f"{s['n']}({s.get('question') or s.get('name')})" for s in data["selects"])
                return (f"共 {len(groups)} 题（✔=已答）：\n" + "\n".join(lines) + tail
                        + "\n→ 用 browser_answer 一次答完，例如 {\"1\":2,\"2\":4}")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"读取表单失败: {str(e)}"

        @register_llm_tool(name="browser_answer")
        async def browser_answer(event: AstrMessageEvent, answers: str, submit_text: str = ""):
            """
            一次点完一批选择题选项。

            answers 是 JSON 对象：键 = 题号（就是 browser_form 里的"第几题"）或题目 name，
            值 = 选项序号（[1] 就是 1）或选项 value/文字片段。
            例：{"1":2,"2":4,"3":1}  或  {"q24":"2"}

            submit_text 给了就在答完后点一下这个文字的按钮（如"提交"、"下一页"、"完成"）。

            Args:
                answers(string): JSON 对象，如 {"1":2,"2":4}
                submit_text(string): 可选，答完后要点的按钮文字
            """
            try:
                browser = await self._get_browser_instance(event)
            except RuntimeError as e:
                return str(e)
            try:
                parsed = json.loads(answers) if isinstance(answers, str) else answers
                if not isinstance(parsed, dict):
                    return 'answers 必须是 JSON 对象，例如 {"1":2,"2":4}'
            except Exception as e:
                return f"answers 不是合法 JSON: {e}"
            try:
                log, err = await browser.answer_form(parsed, submit_text or "")
            except Exception as e:
                return f"答题失败: {e}"
            text = f"已作答 {len(log)} 项：\n" + "\n".join(log)
            if not any("校验" in x for x in log):
                text += "\n（提示：本次结果里没有校验行，说明这页没有可识别的 radio/checkbox 表单；请用 browser_screenshot 看画面）"
            if err:
                text += f"\n⚠️ {err}"
            return await self._action_result(browser, text)

        @register_llm_tool(name="browser_play_swf")
        async def browser_play_swf(event: AstrMessageEvent, swf_url: str, base: str = ""):
            """
            直接播放一个 .swf 文件（老 Flash 游戏）。

            用途：现代浏览器早就删掉了 Flash 支持，很多老游戏站点只剩空壳或者是 404。
            当你手里有一个 .swf 的真实地址（比如 GitHub/Internet Archive 上的存档）时用它：
            插件会用内置的 Ruffle（WASM 版 Flash 模拟器）在本地把它跑起来。
            常见入口是 loader.swf 或 swfs/game.swf，同一个站点的资源会按 base 自动加载。

            也可以给**本地相对路径**（相对插件 vendor/ruffle/ 目录），比如
            `wmw/game.swf` 或 `wmw/play.html`——文件已经在本地时走本地服务最快。

            Args:
                swf_url(string): .swf 的 http/https 直链，或相对 vendor/ruffle/ 的本地路径
                base(string): 可选，资源根目录（默认取 swf_url 所在目录）
            """
            try:
                browser = await self._get_browser_instance(event)
                # 不做前置检查：浏览器/Ruffle 都是懒启动的（第一次操作才起来），
                # 前置问 flash_enabled() 会拿到 False 而误报"Flash 支持已关闭"
                # （2026-09-18 09:20 Niko 就是这么被挡住的）。直接调用，真有问题由下面报错。
                err = await browser.play_swf(swf_url, base or None)
                if err:
                    return f"播放 .swf 失败: {err}"
                await asyncio.sleep(4)
                state = await browser.swf_state()
                note = {
                    "ready": "已加载完成",
                    "loading": "还在加载",
                    "maybe": "已交给 Ruffle（老 SWF 不一定上报事件）",
                    "error": "Ruffle 报错：文件可能已失效或不是合法 SWF",
                }.get(str(state.get("ruffle") or ""), "状态未知")
                return await self._action_result(
                    browser, f"已用 Ruffle 打开 {swf_url}（{note}）。"
                             f"想玩就点游戏画面（browser_click_canvas）然后按方向键/空格；"
                             f"要留证就 browser_record_start → 玩完 browser_record_stop")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"播放 .swf 失败: {str(e)}"

        @register_llm_tool(name="browser_click_text")
        async def browser_click_text(event: AstrMessageEvent, text: str, exact: bool = False):
            """
            按可见文字点击（先自动滚到可见、**先悬停再点**）。

            比坐标点击可靠得多：不用猜坐标，也能唤醒"悬停才出现"的按钮。
            菜单项、按钮、链接上有文字时优先用它。

            Args:
                text(string): 要点的文字，如 "登录"、"播放"
                exact(boolean): 是否要求完全匹配，默认False（包含即可）
            """
            try:
                browser = await self._get_browser_instance(event)
                err = await browser.click_text(text, exact)
                if err:
                    return f"点击失败: {err}"
                await asyncio.sleep(0.4)
                return await self._action_result(browser, f"已点击文字【{text}】，截图如下")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"点击失败: {str(e)}"

        @register_llm_tool(name="browser_click_canvas")
        async def browser_click_canvas(event: AstrMessageEvent):
            """
            点一下游戏画面（canvas）的正中心，用来"抓鼠标"/让游戏接管键盘。

            网页版 DOS 模拟器、Flash 复刻、小游戏常常会停在 "Click to capture mouse"
            这类提示上：不点一下就按键无效。它不需要你估坐标，直接点画布中心；
            结果里会告诉你鼠标有没有被游戏锁定（锁定后方向键/空格才会生效）。

            Args: 无
            """
            try:
                browser = await self._get_browser_instance(event)
                err = await browser.click_canvas()
                if err:
                    return f"点击游戏画面失败: {err}"
                state = await browser.canvas_state()
                locked = bool(state.get("locked"))
                tip = "鼠标已被游戏接管，可以按方向键/空格了" if locked else "还没锁定：再点一次，或先点画面里的开始按钮"
                if state.get("zoom_reset"):
                    tip += "（游戏画面原本超出屏幕，已把页面缩放复位到 1.0）"
                return await self._action_result(
                    browser, f"已点击游戏画面中心（画布 {state.get('w')}×{state.get('h')}，{tip}）")
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"点击游戏画面失败: {str(e)}"

        @register_llm_tool(name="browser_act")
        async def browser_act(event: AstrMessageEvent, actions: str):
            """
            一次调用连续做多步操作，最后返回一张截图。**推荐优先用它**：把 3~6 步合成一次调用，
            能省掉好几次模型往返（每次往返 2~4 秒），整体快很多。

            actions 是 JSON 数组字符串，支持的动作：
              {"type":"hover","selector":".menu"}  或 {"type":"hover","x":100,"y":200}
              {"type":"click","text":"登录"}       或 {"type":"click","selector":"#btn"} 或 {"type":"click","x":..,"y":..}
              {"type":"text","text":"关键词"}      输入到当前焦点；带 selector 则输到指定输入框
              {"type":"key","key":"Enter","hold_ms":0}
              {"type":"scroll","distance":800,"direction":"下"}
              {"type":"wait","ms":500}
              {"type":"drag","from":[x1,y1],"to":[x2,y2]}  拖拽（挖地道/拉滑块/划线类游戏）
              {"type":"canvas"}                    点游戏画面中心（等同 browser_click_canvas）
            例：[{"type":"click","text":"搜索框"},{"type":"text","text":"原神"},{"type":"key","key":"Enter"},{"type":"wait","ms":1000}]

            Args:
                actions(string): JSON 数组字符串
            """
            try:
                browser = await self._get_browser_instance(event)
            except RuntimeError as e:
                yield str(e)
                return
            try:
                parsed = json.loads(actions) if isinstance(actions, str) else actions
            except Exception as e:
                yield f"actions 不是合法 JSON: {e}"
                return
            if not isinstance(parsed, list) or not parsed:
                yield "actions 必须是非空数组"
                return
            if len(parsed) > 12:
                parsed = parsed[:12]
            try:
                err, log = await browser.act(parsed)
            except Exception as e:
                yield f"批量操作失败: {e}"
                return
            steps = "；".join(log) if log else "（无）"
            if err:
                yield f"批量操作中断：{err}；已完成：{steps}"
                return
            await asyncio.sleep(0.3)
            yield await self._action_result(
                browser, f"已完成 {len(log)} 步：{steps}。截图如下")

        # ================= 录屏工具 =================

        @register_llm_tool(name="browser_record")
        async def browser_record(event: AstrMessageEvent, duration: int = 15):
            """
            录制当前网页的画面（录屏），生成 mp4 视频并直接发送给用户。

            适合展示页面的动态效果、正在播放的视频、动画、滚动/操作过程等。
            静止的文字页面用 browser_screenshot 截图即可，不要录屏。
            录的是"当前正在显示的那一个标签页"，默认**连页面声音一起录**（可在插件配置关掉）。
            想在录制过程中操作页面/切换标签页，改用 browser_record_start + browser_record_stop。

            Args:
                duration(number): 录制时长（秒），默认15，最长60
            """
            try:
                browser = await self._get_browser_instance(event)
            except RuntimeError as e:
                yield str(e)
                return

            limit = self._record_max_duration()
            try:
                seconds = int(duration or 15)
            except Exception:
                seconds = 15
            seconds = max(1, min(seconds, limit))

            try:
                err = await browser.record_start(max_duration=min(seconds + 5, limit))
            except Exception as e:
                yield f"开始录屏失败: {e}"
                return
            if err:
                yield f"开始录屏失败: {err}"
                return
            status = await browser.record_status()
            if not (isinstance(status, dict) and status.get("running")):
                yield "开始录屏失败：浏览器刚被资源监控关闭（宿主机内存超阈值），请重试一次"
                return

            logger.info(f"[录屏] 用户 {event.get_sender_id()} 开始录制 {seconds}s")
            try:
                await asyncio.sleep(seconds)
            except asyncio.CancelledError:
                await self._record_finish(browser)
                raise

            path, info = await self._record_finish(browser)
            if not path:
                yield f"录屏失败: {info}"
                return

            size = self._human_size(int(info.get("size") or 0))
            ok, note = await self._send_video(event, path)
            if not ok:
                # 兜底：再让框架试一次（yield 回去），同时把真实情况告诉模型
                yield event.chain_result([Video.fromFileSystem(path)])
                yield (
                    f"录屏完成，但**视频发送失败**（{note}）。文件在 {path}。"
                    f"请如实告诉用户没发出去，可以稍后重试或用 send_message_to_user 手动发这个路径。"
                )
                return
            yield (
                f"录屏完成：{note} {info.get('seconds')} 秒的网页视频"
                f"（{size}，{info.get('frames')} 帧，页面 {str(info.get('url') or '')[:80]}）。"
                f"{self._audio_note(info)}"
                f"请用符合人设的简短口语向用户说明你录了什么。"
            )

        @register_llm_tool(name="browser_record_start")
        async def browser_record_start(event: AstrMessageEvent, max_duration: int = 60):
            """
            手动开始录制当前网页画面，之后可以继续做其他浏览器操作，最后用 browser_record_stop 结束并发送视频。

            只在需要边操作边录（例如录制连续多步操作过程）时使用；
            单纯想录一段固定时长的画面请直接用 browser_record。
            录的是"当前正在显示的那一个标签页"：录制期间切换标签页时画面会自动
            跟着切过去，所以可以边翻页、边切标签页、边录。

            Args:
                max_duration(number): 最长录制秒数，默认60，到达上限会自动停止采集
            """
            try:
                browser = await self._get_browser_instance(event)
                limit = self._record_max_duration()
                try:
                    wanted = int(max_duration or limit)
                except Exception:
                    wanted = limit
                err = await browser.record_start(max_duration=max(1, min(wanted, limit)))
                if err:
                    return f"开始录屏失败: {err}"
                status = await browser.record_status()
                if not (isinstance(status, dict) and status.get("running")):
                    return "开始录屏失败：浏览器刚被资源监控关闭（宿主机内存超阈值），请重试一次"
                return (
                    f"已开始录屏（最长 {status.get('limit')} 秒，当前 {status.get('frames')} 帧）。"
                    f"继续操作即可，完成后调用 browser_record_stop。"
                )
            except RuntimeError as e:
                return str(e)
            except Exception as e:
                return f"开始录屏失败: {str(e)}"

        @register_llm_tool(name="browser_record_stop")
        async def browser_record_stop(event: AstrMessageEvent):
            """
            结束录屏（配合 browser_record_start 使用），合成 mp4 并发送给用户。
            """
            try:
                browser = await self._get_browser_instance(event)
            except RuntimeError as e:
                yield str(e)
                return

            path, info = await self._record_finish(browser)
            if not path:
                yield f"录屏失败: {info}"
                return

            size = self._human_size(int(info.get("size") or 0))
            ok, note = await self._send_video(event, path)
            if not ok:
                yield event.chain_result([Video.fromFileSystem(path)])
                yield (
                    f"录屏完成，但**视频发送失败**（{note}）。文件在 {path}。"
                    f"请如实告诉用户没发出去，可以稍后重试或用 send_message_to_user 手动发这个路径。"
                )
                return
            yield (
                f"录屏完成：{note} {info.get('seconds')} 秒的网页视频"
                f"（{size}，{info.get('frames')} 帧）。{self._audio_note(info)}"
                f"请用符合人设的简短口语说明录了什么。"
            )
