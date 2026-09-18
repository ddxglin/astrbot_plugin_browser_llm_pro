"""
浏览器资源监控器 — 增强版
支持：
- 进程级内存监控（单个浏览器进程）
- 整体服务器内存监控
- 闲置自动关闭
- 截图缓存目录大小监控与自动清理
- 操作频率限制（熔断）
- 硬超时熔断
"""

import asyncio
import os
import shutil
import time
import traceback
from pathlib import Path
from typing import Any, Optional

import psutil

from astrbot.api import logger


class BrowserSupervisor:
    """
    增强版浏览器资源监控器
    每个用户浏览器实例对应一个监控器
    """

    def __init__(self, config: dict, data_dir: str):
        self.config = config
        sup_cfg: dict[str, Any] = config.get("supervisor", {})

        # ===== 内存限制 =====
        # 主判据：服务器整体内存阈值（百分比）
        #
        # ⚠️ psutil.virtual_memory() 读的是 /proc/meminfo，而 Docker 不对 /proc/meminfo
        # 做命名空间隔离 —— 这个百分比是**宿主机整机**的水位，不是本容器/AstrBot 自己的。
        # 用户明确要求按这个口径（80%，≈6GB）在浏览器吃内存时先关浏览器。
        self.max_memory_percent: float = sup_cfg.get("max_memory_percent", 80)
        # 次级判据：AstrBot 自身（进程树：本体 + 它启动的浏览器）内存预算（MB）。
        # 设为 0 或负数即关闭这条；默认 3072。
        self.astrbot_memory_budget_mb: int = sup_cfg.get(
            "astrbot_memory_budget_mb", 3072
        )
        # 单个浏览器进程最大内存（MB）
        self.max_process_memory_mb: int = sup_cfg.get("max_process_memory_mb", 512)

        # ===== 闲置超时 =====
        self.idle_timeout: int = sup_cfg.get("idle_timeout", 600)  # 默认10分钟

        # ===== 监控间隔 =====
        self.monitor_interval: float = sup_cfg.get("monitor_interval", 10.0)

        # ===== 缓存清理 =====
        self.cache_max_size_mb: int = sup_cfg.get("cache_max_size_mb", 200)
        self.cache_max_age_hours: int = sup_cfg.get("cache_max_age_hours", 2)

        # ===== 操作频率限制 =====
        self.max_operations_per_minute: int = sup_cfg.get("max_operations_per_minute", 20)
        # 内存守护的"归因 + 冷却"（2026-09-18 加）：
        # 实测一早上触发了 6 次（07:58/08:02/08:05/08:08/08:10/08:14），整机一直卡在 80~84%，
        # 关掉浏览器也降不下来（涨的是 AstrBot 自己和其他容器），结果 Niko 每 2~3 分钟就被
        # 清空一次页面，游戏永远玩不成。所以只在"浏览器确实是主因"时才关，并且关完给冷却。
        self.min_browser_mb_to_kill: int = int(sup_cfg.get("min_browser_mb_to_kill", 250))
        # 真实内存压力判据（2026-09-18 本机实测）：MemTotal 7.9GB、MemAvailable 3.06GB、
        # PSI memory some avg10≈1.3% —— 也就是"整机 80% 占用"大多是 page cache，并不缺内存。
        # 只看百分比会把缓存算成压力，于是浏览器被反复误杀。这里改成：
        # 百分比超阈值 **且** 真的没内存（可用 < mem_min_available_mb 或 PSI 有明显停顿）才算压力。
        self.mem_min_available_mb: int = int(sup_cfg.get("mem_min_available_mb", 900))
        self.mem_psi_full_threshold: float = float(sup_cfg.get("mem_psi_full_threshold", 5.0))
        self._last_pressure_skip_log_ts: float = 0.0
        self.mem_kill_cooldown: int = int(sup_cfg.get("mem_kill_cooldown", 180))
        self._last_mem_kill_ts: float = 0.0
        self._last_mem_skip_log_ts: float = 0.0
        self._last_mem_kill_mb: float = 0.0

        # ===== 截图大小限制 =====
        self.screenshot_max_bytes: int = sup_cfg.get("screenshot_max_bytes", 5 * 1024 * 1024)  # 5MB

        # ===== 硬超时 =====
        self.hard_operation_timeout: int = sup_cfg.get("hard_operation_timeout", 60)

        self.browser_type = config.get("browser_type", "chromium")
        self.verify_browser = config.get("verify_browser", True)

        self.data_dir = data_dir
        self.cache_dir = Path(data_dir) / "screenshot_cache"

        self.browser = None  # BrowserCore 实例

        self._call_lock = asyncio.Lock()
        self._browser_lock = asyncio.Lock()

        self._last_active: float = time.time()
        self._monitor_task: Optional[asyncio.Task] = None

        # ===== 操作频率统计 =====
        self._operation_timestamps: list[float] = []
        self._rate_limit_lock = asyncio.Lock()

        # ===== 浏览器进程PID追踪 =====
        self._browser_pids: list[int] = []
        # 用来在进程命令行里认出"本插件的浏览器"——和 core/reaper.py 用同一组标记，
        # 避免两处各写一份、以后改一处漏一处。
        try:
            from .reaper import BROWSER_MARKERS as _MARKERS
        except Exception:
            _MARKERS = ()
        self._browser_markers: tuple = tuple(_MARKERS)

    # =====================================================
    # 生命周期
    # =====================================================

    async def start(self):
        """启动监控协程"""
        async with self._call_lock:
            if self._monitor_task is None or self._monitor_task.done():
                self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        """停止监控和浏览器"""
        async with self._call_lock:
            if self._monitor_task and not self._monitor_task.done():
                self._monitor_task.cancel()
                try:
                    await self._monitor_task
                except asyncio.CancelledError:
                    pass
                self._monitor_task = None

            if self.browser:
                try:
                    await self.browser.terminate()
                except Exception:
                    pass
                self.browser = None
            self._browser_pids.clear()

    # =====================================================
    # 操作频率限制（熔断检查）
    # =====================================================

    async def check_rate_limit(self) -> bool:
        """
        检查是否触发操作频率限制。
        返回 True = 允许操作，False = 触发熔断。
        """
        async with self._rate_limit_lock:
            now = time.time()
            # 移除60秒前的记录
            cutoff = now - 60
            self._operation_timestamps = [
                t for t in self._operation_timestamps if t > cutoff
            ]
            # 检查是否超出限制
            if len(self._operation_timestamps) >= self.max_operations_per_minute:
                logger.warning(
                    f"[Supervisor] 操作频率限制触发: "
                    f"{len(self._operation_timestamps)}次/分钟 "
                    f"(上限{self.max_operations_per_minute})"
                )
                return False
            # 记录本次操作
            self._operation_timestamps.append(now)
            return True

    async def reset_rate_limit(self):
        """重置频率限制计数器（例如关闭浏览器时）"""
        async with self._rate_limit_lock:
            self._operation_timestamps.clear()

    # =====================================================
    # 对外调用接口（带超时和频率限制）
    # =====================================================

    async def call(self, method: str, **kwargs):
        """调用浏览器方法，带超时和频率限制。

        `_internal=True` 表示插件自己发起的辅助调用（如页面摘要），
        不计入"每分钟操作次数"的熔断统计。
        """
        internal = bool(kwargs.pop("_internal", False))
        # 调用方可以声明"这是一次计划内长操作"的用时预算（如 browser_act 里排了 12 步、
        # 每步 wait 6s）。否则硬超时 60s 会把刚排好的动作掐掉，还会顺手重启浏览器
        # ——2026-09-17 23:34 实测：act 被强杀 + 浏览器重启，游戏直接回到空白页。
        extra_timeout = kwargs.pop("_timeout", None)
        # 频率限制检查
        allowed = True if internal else await self.check_rate_limit()
        if not allowed:
            raise RuntimeError(
                f"操作过于频繁（上限 {self.max_operations_per_minute} 次/分钟），"
                f"请稍后再试"
            )

        async with self._call_lock:
            if not self.browser:
                await self._start_browser()

            async with self._browser_lock:
                browser = self.browser
                if not browser:
                    return None
                func = getattr(browser, method, None)

            if func is None:
                raise AttributeError(f"BrowserCore 没有方法 {method}")

            self._last_active = time.time()

            # 带硬超时的调用（计划内长操作按声明的预算放宽）
            budget = float(self.hard_operation_timeout)
            if extra_timeout:
                try:
                    budget = max(budget, float(extra_timeout))
                except Exception:
                    pass
            try:
                return await asyncio.wait_for(func(**kwargs), timeout=budget)
            except asyncio.TimeoutError:
                logger.error(
                    f"[Supervisor] 操作 {method} 超时 (>{budget:.0f}s)，强制终止"
                )
                if extra_timeout:
                    # 排好的长动作超时：只报错，**不重启浏览器**（重启会把页面/游戏全丢掉）
                    raise RuntimeError(
                        f"操作超时（超过 {budget:.0f} 秒）。浏览器保持原状，"
                        f"页面和登录状态都还在，可以直接继续"
                    )
                # 普通操作疑似卡死：重启浏览器兜底
                asyncio.create_task(self._restart_browser())
                raise RuntimeError(
                    f"操作超时（超过 {budget:.0f} 秒），浏览器已自动重启"
                )

    # =====================================================
    # 浏览器生命周期管理
    # =====================================================

    async def _start_browser(self):
        """启动浏览器并追踪其进程PID"""
        async with self._browser_lock:
            if not self.browser:
                if self.verify_browser:
                    from .downloader import BrowserDownloader
                    if not await BrowserDownloader.verify_browser(self.browser_type):
                        logger.error("浏览器未安装或不可用")
                        raise RuntimeError("浏览器未安装或不可用")

                from .browser import BrowserCore

                core = BrowserCore(self.config, Path(self.data_dir))
                try:
                    await core.initialize()
                except Exception:
                    logger.error("[Supervisor] BrowserCore.initialize 失败")
                    raise

                self.browser = core
                self._last_active = time.time()
                self._operation_timestamps.clear()

                logger.info(
                    "[Supervisor] 内存守护：整机水位 %.1f%% / 阈值 %s%%（读的是宿主机）；"
                    "AstrBot 自身 %.0fMB / 预算 %sMB"
                    % (
                        psutil.virtual_memory().percent,
                        self.max_memory_percent,
                        self.astrbot_tree_memory_mb(),
                        self.astrbot_memory_budget_mb,
                    )
                )

                # 异步追踪浏览器子进程PID
                asyncio.create_task(self._track_browser_pids())

    async def _track_browser_pids(self):
        """追踪浏览器引擎的子进程PID（延迟获取，等进程启动）"""
        await asyncio.sleep(3)
        if not self.browser:
            return
        try:
            pids = []
            browser_obj = self.browser.browser
            if browser_obj and browser_obj.contexts:
                for context in browser_obj.contexts:
                    for page in context.pages:
                        try:
                            await page.evaluate("() => 1")  # 测试连接
                            # 只收"命令行里带本插件 extensions 目录"的浏览器，
                            # 否则别处（别的插件/别的用户）的 chrome 也会被算进本实例预算
                            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                                name = (proc.info.get('name') or '').lower()
                                if not any(kw in name for kw in
                                           ['chromium', 'chrome', 'firefox', 'geckodriver', 'msedge']):
                                    continue
                                cmdline = " ".join(proc.info.get('cmdline') or [])
                                if self._browser_markers and not any(
                                        m in cmdline for m in self._browser_markers):
                                    continue
                                if proc.pid not in pids:
                                    pids.append(proc.pid)
                        except Exception:
                            pass
            self._browser_pids = list(set(pids))
            if pids:
                logger.debug(f"[Supervisor] 追踪到浏览器进程PID: {pids}")
        except Exception:
            pass  # 追踪失败不影响主流程

    @staticmethod
    def memory_facts() -> dict:
        """读真实内存状况：可用内存 MB + PSI 停顿百分比（读不到就给保守值）。"""
        facts = {"available_mb": None, "psi_some": None, "psi_full": None}
        try:
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        facts["available_mb"] = float(line.split()[1]) / 1024.0
                        break
        except Exception:
            pass
        try:
            with open("/proc/pressure/memory", encoding="utf-8") as f:
                for line in f:
                    parts = dict(kv.split("=") for kv in line.split()[1:])
                    if line.startswith("some"):
                        facts["psi_some"] = float(parts.get("avg10", 0))
                    elif line.startswith("full"):
                        facts["psi_full"] = float(parts.get("avg10", 0))
        except Exception:
            pass
        return facts

    def _real_memory_pressure(self) -> tuple[bool, str]:
        """整机是不是真的缺内存（而不是"缓存把百分比顶上去了"）。"""
        f = self.memory_facts()
        avail, full = f["available_mb"], f["psi_full"]
        if avail is not None and avail < self.mem_min_available_mb:
            return True, f"可用内存仅 {avail:.0f}MB"
        if full is not None and full > self.mem_psi_full_threshold:
            return True, f"内存停顿 PSI full={full:.1f}%"
        detail = f"可用 {avail:.0f}MB" if avail is not None else "可用未知"
        if full is not None:
            detail += f"，PSI full={full:.1f}%"
        return False, detail

    def _recording_active(self) -> bool:
        """正在录屏吗？录制中绝不关浏览器（关掉等于把演示视频毁了）。"""
        try:
            rec = getattr(self.browser, "recorder", None)
            if rec is None:
                return False
            return bool(getattr(rec, "status", {}).get("running"))
        except Exception:
            return False

    def browser_memory_mb(self) -> float:
        """本插件浏览器进程的 RSS 合计（用于判断"关了它到底有没有用"）。"""
        total = 0.0
        try:
            for pid in list(self._browser_pids):
                try:
                    proc = psutil.Process(pid)
                    if proc.is_running():
                        total += proc.memory_info().rss
                except Exception:
                    continue
        except Exception:
            return 0.0
        return total / 1024 / 1024

    async def _stop_browser(self):
        """停止浏览器并清理"""
        async with self._browser_lock:
            if self.browser:
                try:
                    await self.browser.terminate()
                except Exception:
                    logger.error("[Supervisor] BrowserCore.terminate 失败")
                self.browser = None
                self._browser_pids.clear()
                await self.reset_rate_limit()
                self._last_active = time.time()

    async def _restart_browser(self):
        """重启浏览器（异步，由监控或超时触发）"""
        logger.warning("[Supervisor] 浏览器重启中...")
        await self._stop_browser()
        await asyncio.sleep(2)
        try:
            await self._start_browser()
            logger.info("[Supervisor] 浏览器重启完成")
        except Exception as e:
            logger.error(f"[Supervisor] 浏览器重启失败: {e}")

    # =====================================================
    # 缓存清理
    # =====================================================

    async def _cleanup_cache(self):
        """清理截图缓存目录：按大小和时效"""
        if not self.cache_dir.exists():
            return

        try:
            # 1. 计算总大小
            total_size = 0
            files = []
            for f in self.cache_dir.iterdir():
                if f.is_file():
                    try:
                        stat = f.stat()
                        total_size += stat.st_size
                        files.append((f, stat.st_mtime, stat.st_size))
                    except Exception:
                        continue

            max_bytes = self.cache_max_size_mb * 1024 * 1024
            now = time.time()
            max_age_seconds = self.cache_max_age_hours * 3600

            # 2. 按修改时间排序（最旧在前）
            files.sort(key=lambda x: x[1])

            removed_count = 0
            removed_size = 0

            for fpath, mtime, fsize in files:
                should_remove = False

                # 超过时效
                if now - mtime > max_age_seconds:
                    should_remove = True
                # 超过总大小限制
                elif total_size > max_bytes:
                    should_remove = True

                if should_remove:
                    try:
                        fpath.unlink()
                        removed_count += 1
                        removed_size += fsize
                        total_size -= fsize
                    except Exception:
                        continue

            if removed_count > 0:
                logger.info(
                    f"[Supervisor] 缓存清理: 删除了 {removed_count} 个文件, "
                    f"释放 {removed_size / 1024 / 1024:.1f}MB"
                )

        except Exception as e:
            logger.error(f"[Supervisor] 缓存清理异常: {e}")

    # =====================================================
    # AstrBot 自身内存（进程树）
    # =====================================================

    def astrbot_tree_memory_mb(self) -> float:
        """AstrBot 自身占用的内存（MB）。

        优先读**本容器 cgroup v2 的 anon**（`/sys/fs/cgroup/memory.stat`）：一次文件读取
        就能拿到"AstrBot 进程树全部匿名内存"，不重复计共享页、也不含 page cache。
        实测 2026-09-17：进程树 RSS 求和 = 1804MB，而真实 PSS 合计 ≈1060MB —— RSS 口径
        虚高约 70%，会让预算被误触。cgroup 不可用时回落到进程树 RSS 求和（偏高但可用）。
        """
        anon = self._cgroup_anon_mb()
        if anon is not None:
            return anon
        try:
            proc = psutil.Process(os.getpid())
            total = proc.memory_info().rss
            for child in proc.children(recursive=True):
                try:
                    total += child.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return total / 1024 / 1024
        except Exception:
            return 0.0

    @staticmethod
    def _cgroup_anon_mb() -> Optional[float]:
        """读本容器 cgroup v2 的匿名内存（MB）；不可用返回 None。"""
        try:
            with open("/sys/fs/cgroup/memory.stat", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("anon "):
                        return int(line.split()[1]) / 1024 / 1024
        except Exception:
            return None
        return None

    # =====================================================
    # 进程级内存检查
    # =====================================================

    async def _check_process_memory(self) -> bool:
        """
        检查浏览器子进程的内存占用。
        返回 True = 正常，False = 超出限制需要重启。
        """
        if not self._browser_pids:
            return True

        max_bytes = self.max_process_memory_mb * 1024 * 1024
        total_process_memory = 0

        for pid in list(self._browser_pids):
            try:
                proc = psutil.Process(pid)
                if not proc.is_running():
                    self._browser_pids.remove(pid)
                    continue
                mem_info = proc.memory_info()
                rss = mem_info.rss
                total_process_memory += rss

                if rss > max_bytes:
                    logger.warning(
                        f"[Supervisor] 浏览器进程 PID={pid} 内存超限: "
                        f"{rss / 1024 / 1024:.1f}MB > {self.max_process_memory_mb}MB"
                    )
                    return False

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                try:
                    self._browser_pids.remove(pid)
                except ValueError:
                    pass
                continue

        # 检查总内存
        if total_process_memory > max_bytes * 2:  # 所有进程总和的宽松限制
            logger.warning(
                f"[Supervisor] 浏览器进程总内存超限: "
                f"{total_process_memory / 1024 / 1024:.1f}MB"
            )
            return False

        return True

    # =====================================================
    # 监控主循环
    # =====================================================

    async def _monitor_loop(self):
        """增强版监控循环"""
        cache_clean_interval = max(self.monitor_interval * 6, 60)  # 至少每分钟清理一次
        _cache_tick = 0

        while True:
            try:
                await asyncio.sleep(self.monitor_interval)
                _cache_tick += 1

                if not self.browser:
                    continue

                # ===== 1. 空闲检测 =====
                idle_time = time.time() - self._last_active
                if idle_time > self.idle_timeout and not self._recording_active():
                    await self._stop_browser()
                    logger.warning(
                        f"[Supervisor] 浏览器闲置超过 {self.idle_timeout}s，自动关闭"
                    )
                    continue

                # ===== 2. 整机内存水位（主判据，默认 80%）=====
                # psutil 读到的是宿主机水位（Docker 不隔离 /proc/meminfo）；
                # 按用户要求：浏览器吃内存时优先关浏览器。
                mem = psutil.virtual_memory()
                if mem.percent > self.max_memory_percent:
                    pressured, why = self._real_memory_pressure()
                    if not pressured:
                        now = time.time()
                        if now - self._last_pressure_skip_log_ts > 600:
                            self._last_pressure_skip_log_ts = now
                            logger.info(
                                f"[Supervisor] 整机占用 {mem.percent:.1f}% 但**并不缺内存**（{why}），"
                                f"大概率是 page cache，按真实压力判据不关浏览器"
                            )
                        continue
                    own_mb = self.browser_memory_mb()
                    now = time.time()
                    if self._recording_active():
                        if now - self._last_mem_skip_log_ts > 60:
                            self._last_mem_skip_log_ts = now
                            logger.info(
                                f"[Supervisor] 整机 {mem.percent:.1f}% 偏高，但正在录屏，先不动浏览器"
                            )
                    elif own_mb < self.min_browser_mb_to_kill:
                        if now - self._last_mem_skip_log_ts > 300:
                            self._last_mem_skip_log_ts = now
                            logger.info(
                                f"[Supervisor] 整机 {mem.percent:.1f}% 偏高，但本浏览器只占 "
                                f"{own_mb:.0f}MB（<{self.min_browser_mb_to_kill}MB），关它也降不下来，先留着"
                            )
                    elif now - self._last_mem_kill_ts < self.mem_kill_cooldown:
                        if now - self._last_mem_skip_log_ts > 120:
                            self._last_mem_skip_log_ts = now
                            logger.info(
                                f"[Supervisor] 刚关过一次浏览器（{int(now - self._last_mem_kill_ts)}s 前），"
                                f"整机仍 {mem.percent:.1f}%，说明不是浏览器的锅，本次不再关"
                            )
                    else:
                        self._last_mem_kill_ts = now
                        self._last_mem_kill_mb = own_mb
                        await self._stop_browser()
                        logger.warning(
                            f"[Supervisor] 整机内存 {mem.percent:.1f}% > {self.max_memory_percent}%，"
                            f"关闭浏览器（它占 {own_mb:.0f}MB；{why}）"
                        )
                    continue

                # ===== 3. AstrBot 自身内存预算（次级，默认 3072MB，≤0 关闭）=====
                if self.astrbot_memory_budget_mb > 0:
                    self_mb = self.astrbot_tree_memory_mb()
                    if self_mb > self.astrbot_memory_budget_mb:
                        await self._stop_browser()
                        logger.warning(
                            f"[Supervisor] AstrBot 自身内存 {self_mb:.0f}MB 超过预算 "
                            f"{self.astrbot_memory_budget_mb}MB，关闭浏览器"
                        )
                        continue

                # ===== 4. 进程级内存检测 =====
                mem_ok = await self._check_process_memory()
                if not mem_ok:
                    logger.warning("[Supervisor] 浏览器进程内存超限，重启浏览器")
                    asyncio.create_task(self._restart_browser())
                    continue

                # ===== 5. 定期缓存清理 =====
                if _cache_tick * self.monitor_interval >= cache_clean_interval:
                    _cache_tick = 0
                    await self._cleanup_cache()

            except asyncio.CancelledError:
                break
            except Exception:
                logger.error(f"[Supervisor] 监控循环异常:\n{traceback.format_exc()}")

    # =====================================================
    # 对外状态查询
    # =====================================================

    def get_status(self) -> dict:
        """获取当前监控器状态"""
        return {
            "has_browser": self.browser is not None,
            "idle_seconds": int(time.time() - self._last_active) if self.browser else -1,
            "idle_timeout": self.idle_timeout,
            "browser_pids": self._browser_pids,
            "memory_limit_mb": self.max_process_memory_mb,
            "astrbot_memory_mb": round(self.astrbot_tree_memory_mb(), 1),
            "astrbot_memory_budget_mb": self.astrbot_memory_budget_mb,
            "host_memory_percent": round(psutil.virtual_memory().percent, 1),
            "host_memory_percent_limit": self.max_memory_percent,
            "operations_last_minute": len(
                [t for t in self._operation_timestamps if t > time.time() - 60]
            ),
            "max_operations_per_minute": self.max_operations_per_minute,
            "cache_dir": str(self.cache_dir),
            "cache_max_size_mb": self.cache_max_size_mb,
        }
