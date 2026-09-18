"""进程回收：清掉上一次插件实例遗留的浏览器，并收掉 chrome 僵尸。

背景（2026-09-17 实测）：
- AstrBot 热重载插件时，旧实例的 Playwright 浏览器**不一定被关掉**：旧实例只要还有
  后台任务（supervisor 监控循环 / 清理循环）在跑就活着，没人调它的 terminate()；
  一旦引用彻底断开，Playwright 的 node driver 会被 init 收养（ppid=1），chromium 全家
  继续活着吃内存。实测一次会话里留下了 5 套、PSS ≈376MB。
- 容器里 AstrBot main.py 就是 PID 1，它不 wait 子进程 ⇒ 浏览器进程退出后变成僵尸
  （实测 96~124 个 `chrome-headless <defunct>`）。僵尸不占内存但占 PID，
  而且会让 `进程树 RSS 求和` 之类的统计失真。

本模块在插件加载时清孤儿、之后定期收僵尸。判断依据都取"确定的：
- 孤儿：命令行里带**本插件 extensions 目录**（只有我们的启动参数会带），
  且进程启动时间**早于本实例创建时间** —— 本实例必然是空的，所以这些一定是遗留；
- 僵尸：`ppid == 本进程` 且状态为 ZOMBIE 且名字是 chrome 系。
  Playwright 真正管理的子进程是 node driver（名字不含 chrome），不会被误收。
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from typing import Optional

import psutil

from astrbot.api import logger

# 本插件根目录（core/reaper.py 的上一级）——用它匹配自己启动的浏览器进程
PLUGIN_DIR = str(Path(__file__).resolve().parents[1])
BROWSER_MARKERS = (PLUGIN_DIR, "astrbot_plugin_browser_llm/extensions")
CHROME_NAMES = ("chrome-headless", "chrome", "chromium", "chromium-browser")


def _cmdline(proc: psutil.Process) -> str:
    try:
        return " ".join(proc.cmdline() or [])
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return ""


def _is_our_browser(cmdline: str) -> bool:
    return any(m in cmdline for m in BROWSER_MARKERS)


def _is_chromeish(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in CHROME_NAMES)


def reap_zombies(max_reap: int = 200) -> int:
    """收掉本进程的 chrome 僵尸子进程，返回收掉的个数。"""
    me = os.getpid()
    reaped = 0
    for proc in psutil.process_iter(["pid", "ppid", "name", "status"]):
        try:
            info = proc.info
            if info.get("ppid") != me or info.get("status") != psutil.STATUS_ZOMBIE:
                continue
            if not _is_chromeish(info.get("name") or ""):
                continue
            try:
                os.waitpid(info["pid"], os.WNOHANG)
                reaped += 1
            except ChildProcessError:
                continue  # 已被 asyncio 之类的其他 watcher 收走
            except OSError:
                continue
            if reaped >= max_reap:
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return reaped


def _driver_of(proc: psutil.Process) -> Optional[psutil.Process]:
    """向上找到管理这个浏览器的 playwright node driver（有则返回它）。"""
    try:
        parents = proc.parents()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    for parent in parents:
        cmd = _cmdline(parent)
        if "playwright" in cmd and ("driver/node" in cmd or "run-driver" in cmd):
            return parent
    return None


def kill_orphan_browsers(instance_started_at: float) -> tuple[int, int]:
    """杀掉早于本实例启动的、属于本插件的浏览器进程树。

    Args:
        instance_started_at: 本插件实例的创建时间戳（time.time()）。

    Returns:
        (杀掉的根进程数, 估算释放的 RSS MB)
    """
    targets: list[int] = []
    freed_mb = 0
    seen_roots: set[int] = set()

    me = os.getpid()
    for proc in psutil.process_iter(["pid", "ppid", "name", "create_time", "memory_info", "status"]):
        try:
            info = proc.info
            if info.get("status") == psutil.STATUS_ZOMBIE:
                continue
            if not _is_chromeish(info.get("name") or ""):
                continue
            if not _is_our_browser(_cmdline(proc)):
                continue
            if info.get("create_time", 0) >= instance_started_at:
                continue  # 本实例自己起的（理论上不会有），别碰

            driver = _driver_of(proc)
            root = driver.pid if driver is not None else info["pid"]
            if root in seen_roots:
                continue
            seen_roots.add(root)
            targets.append(root)
            try:
                freed_mb += (info.get("memory_info").rss // 1024 // 1024) if info.get("memory_info") else 0
            except Exception:
                pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not targets:
        return 0, 0

    for sig in (signal.SIGTERM, signal.SIGKILL):
        alive = []
        for pid in targets:
            try:
                p = psutil.Process(pid)
                if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                    os.kill(pid, sig)
                    alive.append(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
                continue
        if not alive:
            break
        time.sleep(1.5)

    still = []
    for pid in targets:
        try:
            p = psutil.Process(pid)
            if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                still.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    logger.info(
        f"[回收] 清理上一实例遗留的浏览器：根进程 {len(targets)} 个"
        f"（估算 {freed_mb}MB），残留 {len(still)} 个"
    )
    return len(targets), freed_mb


class LeakReaper:
    """插件生命周期内的回收器：加载时清孤儿，之后定期收僵尸。"""

    def __init__(self, interval: float = 60.0):
        self.interval = max(15.0, interval)
        self.started_at = time.time()
        self._task: Optional[asyncio.Task] = None
        self.reaped_total = 0
        self.killed_orphans = 0
        self.freed_mb = 0

    async def start(self) -> None:
        """启动时清一次孤儿 + 收一次僵尸，然后起定期任务。"""
        try:
            killed, freed = await asyncio.to_thread(kill_orphan_browsers, self.started_at)
            self.killed_orphans = killed
            self.freed_mb = freed
            reaped = await asyncio.to_thread(reap_zombies)
            self.reaped_total += reaped
            if reaped:
                logger.info(f"[回收] 收掉 {reaped} 个 chrome 僵尸进程")
        except Exception as e:
            logger.warning(f"[回收] 启动清理失败（不影响插件使用）: {e}")

        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval)
                n = await asyncio.to_thread(reap_zombies)
                self.reaped_total += n
                if n:
                    logger.info(f"[回收] 收掉 {n} 个 chrome 僵尸进程（累计 {self.reaped_total}）")
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"[回收] 僵尸清理异常: {e}")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def status(self) -> dict:
        return {
            "killed_orphans": self.killed_orphans,
            "freed_mb_estimate": self.freed_mb,
            "reaped_zombies": self.reaped_total,
        }
