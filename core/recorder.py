"""网页录屏器 —— CDP 截屏流 + ffmpeg 合成 mp4。

设计要点：
- 依赖 Chromium 的 CDP ``Page.startScreencast``（firefox/webkit 不支持）。
- ``start()`` 立即返回，帧在后台由事件回调收集，不占用 BrowserCore 的操作锁，
  因此不会触碰 Supervisor 的 ``hard_operation_timeout``（默认 60s）。
- 画面没有重绘时 Chromium 不会产生新帧，合成阶段用最后一张画面补足时长，
  保证静态页面也能录出完整的 N 秒视频。
- 输出 mp4 带一条静音 aac 音轨，兼容 QQ 视频消息。
"""

from __future__ import annotations

import asyncio
import base64
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger

from .audio import AudioCapture, measure_volume


BUILD = "recorder-follow-v2"


class RecordingError(RuntimeError):
    """录屏相关错误。"""


class PageRecorder:
    """单个用户的网页录屏器（同一时间只允许一路录制）。"""

    def __init__(self, base_dir: Path, config: Optional[dict] = None, page_provider: Any = None):
        cfg = config or {}
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self.fps: int = int(cfg.get("record_fps", 15))
        self.quality: int = int(cfg.get("record_quality", 70))
        # 0 = 跟随浏览器视口（不缩放、不变形；以前固定 1280x720，视口 1920x1400 时
        # 会被压成 ~987x720，画面又小又糊）
        self.max_width: int = int(cfg.get("record_max_width", 0) or 0)
        self.max_height: int = int(cfg.get("record_max_height", 0) or 0)
        # 实际采集帧尺寸（start() 时按视口算好；0/0 = 跟随视口）
        self._frame_w: int = 1280
        self._frame_h: int = 720
        self._real_frame_size: tuple[int, int] | None = None
        self._size_change_warned: bool = False
        self.max_duration: int = int(cfg.get("record_max_duration", 120))
        self.crf: int = int(cfg.get("record_crf", 30))
        self.keep_files: int = int(cfg.get("record_keep_files", 20))
        self.max_frames: int = int(cfg.get("record_max_frames", 9000))
        self.ffmpeg: str = str(cfg.get("ffmpeg_path") or "") or (shutil.which("ffmpeg") or "ffmpeg")
        # 跟随当前标签页：由 BrowserCore 提供"当前页面"的取值函数
        self._page_provider = page_provider
        self.follow_interval: float = float(cfg.get("record_follow_interval", 0.3))

        self._page: Any = None
        self._target_page: Any = None
        self._target_key: str = ""
        self._cdp: Any = None
        self._session_dir: Optional[Path] = None
        self._frames: list[tuple[Path, float, str]] = []
        self._ack_tasks: set[asyncio.Task] = set()
        self._auto_stop_task: Optional[asyncio.Task] = None
        self._watch_task: Optional[asyncio.Task] = None
        self._running = False
        self._capture_closed = True
        self._start_ts = 0.0
        self._stop_ts = 0.0
        self._page_url = ""
        self._duration_limit = 0
        self._switches = 0
        # 录声音：由 BrowserCore 在启动前把解析好的 PULSE_SERVER 填进来
        self.record_audio: bool = bool(cfg.get("record_audio", True))
        self.audio_server: Optional[str] = None
        self._audio: Optional[AudioCapture] = None
        self._audio_started_at: float = 0.0   # time.monotonic()，与帧到达时刻同钟
        # ffmpeg 连上 pulse 到真正开始收样的延迟（秒）：下面算对齐时补偿掉
        # 音频起点补偿：首块数据的到达比它内容的真实起点晚一点点（管道缓冲 ~23ms + pulse 流延迟）。
        # 实测三轮偏差 +0.068/+0.054/+0.063s，扣掉这个常数后落在 ±20ms。
        self._audio_startup_latency: float = float(cfg.get("record_audio_startup_latency", 0.05))

    # =====================================================
    # 状态
    # =====================================================

    @property
    def running(self) -> bool:
        """是否正在录制（含已自动停止采集、等待合成的情况）。"""
        return self._running

    @property
    def status(self) -> dict:
        return {
            "running": self._running,
            "frames": len(self._frames),
            "seconds": round(time.monotonic() - self._start_ts, 1) if self._running else 0.0,
            "limit": self._duration_limit,
            "url": self._page_url,
            "switches": self._switches,
            "session_dir": str(self._session_dir) if self._session_dir else "",
        }

    # =====================================================
    # 开始录制
    # =====================================================

    async def start(self, page: Any, max_duration: Optional[int] = None) -> dict:
        """开始录制指定页面。

        Args:
            page: Playwright Page 对象。
            max_duration: 本次录制的时长上限（秒），超过后自动停止采集。

        Returns:
            包含 session_dir / limit 的状态字典。

        Raises:
            RecordingError: 已在录制、非 Chromium 内核或 CDP 启动失败时抛出。
        """
        if self._running:
            raise RecordingError(
                f"已经在录屏中（已 {self.status['seconds']}s，{len(self._frames)} 帧），"
                f"请先停止再开始新的录制"
            )

        browser_type = self._browser_type_name(page)
        if browser_type != "chromium":
            raise RecordingError(f"录屏仅支持 chromium 内核，当前为 {browser_type}")

        limit = int(max_duration or self.max_duration)
        limit = max(1, min(limit, self.max_duration))

        # 帧尺寸：配置为 0 就跟随视口（不缩放、不变形）。以前固定 1280x720，
        # 视口 1920x1400 时会被压成 ~987x720 两边留黑，看着"画面小小的"。
        vp = {}
        try:
            vp = page.viewport_size or {}
        except Exception:
            vp = {}
        vw, vh = int(vp.get("width") or 0), int(vp.get("height") or 0)
        # 采集框：(0 表示跟随视口)，然后等比缩放到框内——不变形、不留黑边
        box_w = self.max_width if self.max_width > 0 else (vw or 1280)
        box_h = self.max_height if self.max_height > 0 else (vh or 720)
        if vw and vh:
            k = min(box_w / vw, box_h / vh, 1.0)
            self._frame_w, self._frame_h = max(2, int(vw * k) // 2 * 2), max(2, int(vh * k) // 2 * 2)
        else:
            self._frame_w, self._frame_h = box_w, box_h
        logger.info(
            f"[Recorder] 采集尺寸 {self._frame_w}x{self._frame_h}（视口 {vw}x{vh}，"
            f"配置 {self.max_width or '跟随'}x{self.max_height or '跟随'}）"
        )

        self._page = page
        self._frames = []
        self._real_frame_size = None
        self._size_change_warned = False
        self._capture_closed = False
        self._duration_limit = limit
        try:
            self._page_url = page.url or ""
        except Exception:
            self._page_url = ""

        stamp = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
        self._session_dir = self.base_dir / f"rec_{stamp}"
        (self._session_dir / "frames").mkdir(parents=True, exist_ok=True)
        self._target_page = None
        self._target_key = ""
        self._switches = 0

        # ===== 先起音频、等它真的有数据，再开截屏流 =====
        # 反过来的话（先开截屏再起音频），ffmpeg 连上 PulseAudio 要 0.5~2.6s，
        # 这段时间的声音**根本没被录下来**；对齐只能补时间轴、补不出内容 ⇒
        # 表现就是"视频开头一秒没声音"（2026-09-17 实测踩到）。
        audio_started_at = 0.0
        if self.record_audio:
            try:
                self._audio = AudioCapture(
                    self._session_dir / "audio.pcm",
                    server=self.audio_server or None,
                )
                if await self._audio.start():
                    audio_started_at = time.monotonic()
                    if await self._audio.wait_first_data(timeout=4.0):
                        logger.info(
                            f"[Recorder] 音频已就绪（用时 {time.monotonic() - audio_started_at:.2f}s），开始录画面"
                        )
                    else:
                        logger.warning("[Recorder] 音频迟迟没有数据，仍继续录（画面为主）")
                else:
                    self._audio = None
            except Exception as e:
                logger.warning(f"[Recorder] 音频采集启动失败（只录画面）: {e}")
                self._audio = None

        if not await self._cast_page(page, wait_first=2.5):
            await self.abort()
            raise RecordingError("页面没有产生任何可录制的画面")

        self._start_ts = time.monotonic()
        # 音频起点先占位（真实值要等第一块 PCM 到达才知道，见 stop()）
        self._audio_started_at = audio_started_at
        self._stop_ts = self._start_ts
        self._running = True
        self._auto_stop_task = asyncio.create_task(self._auto_stop(limit))
        if self._page_provider is not None:
            self._watch_task = asyncio.create_task(self._watch_target())

        logger.info(
            f"[Recorder] 开始录屏: {self._session_dir.name}, "
            f"上限 {limit}s, {self._frame_w}x{self._frame_h}@{self.fps}fps, url={self._page_url[:120]}"
        )
        return {"session_dir": str(self._session_dir), "limit": limit}

    # =====================================================
    # 截屏流：跟随"当前标签页"
    # =====================================================

    @staticmethod
    def _page_key(page: Any) -> str:
        return str(id(page))

    def _make_handler(self, key: str):
        def handler(ev: dict) -> None:
            self._on_frame(ev, key)

        return handler

    async def _cast_page(self, page: Any, wait_first: float = 0.0) -> bool:
        """把截屏流切到指定页面。

        不可见/非当前的目标 Chromium 不会产生帧，所以先激活页面；
        wait_first > 0 时等首帧并最多重试一次（用于开始录制，失败即报错）。

        Returns:
            是否拿到了画面（wait_first=0 时表示截屏流已启动）。
        """
        await self._release_cdp()

        for attempt in (1, 2):
            try:
                await page.bring_to_front()
            except Exception:
                pass

            try:
                if self._cdp is None:
                    self._cdp = await page.context.new_cdp_session(page)
                    self._target_key = self._page_key(page)
                    self._cdp.on("Page.screencastFrame", self._make_handler(self._target_key))
                await self._send_start_cast()
            except Exception as e:
                logger.warning(f"[Recorder] 第 {attempt} 次启动截屏流失败: {e}")
                continue

            self._target_page = page
            try:
                self._page_url = page.url or self._page_url
            except Exception:
                pass

            if wait_first <= 0:
                return True
            if await self._wait_first_frame(wait_first, self._target_key):
                return True

            logger.warning(f"[Recorder] 第 {attempt} 次未收到画面，重试中")
            await self._reset_frames()
            await self._release_cdp()

        return False

    async def _release_cdp(self) -> None:
        """断开当前 CDP 会话（切目标/收尾时调用）。"""
        cdp, self._cdp = self._cdp, None
        self._target_key = ""
        if cdp is None:
            return
        try:
            await cdp.send("Page.stopScreencast")
        except Exception:
            pass
        try:
            await cdp.detach()
        except Exception:
            pass

    async def _watch_target(self) -> None:
        """后台跟随：BrowserCore 当前页面变了就把截屏流切过去。"""
        while True:
            try:
                await asyncio.sleep(max(0.1, self.follow_interval))
                if not self._running or self._capture_closed:
                    return
                page = self._current_page()
                if page is None or page is self._target_page:
                    continue
                if await self._retarget(page):
                    self._switches += 1
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"[Recorder] 跟随标签页异常: {e}")

    def _current_page(self) -> Any:
        """当前正在显示的页面（由 BrowserCore 提供）。"""
        provider = self._page_provider
        if provider is None:
            return None
        try:
            page = provider() if callable(provider) else provider
        except Exception:
            return None
        if page is None:
            return None
        try:
            if page.is_closed():
                return None
        except Exception:
            pass
        return page

    async def _retarget(self, page: Any) -> bool:
        """切换录制目标到新标签页。"""
        try:
            url = page.url or ""
        except Exception:
            url = ""
        ok = await self._cast_page(page, wait_first=1.5)
        if ok:
            logger.info(f"[Recorder] 录制目标已切到新标签页: {url[:120]}")
        else:
            logger.warning(f"[Recorder] 切换录制目标失败，继续沿用上一画面: {url[:120]}")
        return ok

    async def _send_start_cast(self) -> None:
        await self._cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": self.quality,
                "maxWidth": self._frame_w,
                "maxHeight": self._frame_h,
                "everyNthFrame": 1,
            },
        )

    async def _wait_first_frame(self, timeout: float, key: Optional[str] = None) -> bool:
        """等待第一帧到达（CDP 事件由回调写入 _frames）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._frames and (key is None or self._frames[-1][2] == key):
                return True
            await asyncio.sleep(0.1)
        return bool(self._frames and (key is None or self._frames[-1][2] == key))

    async def _reset_frames(self) -> None:
        """丢弃已采集的帧（重试截屏流时使用）。"""
        for path, _ts, _key in self._frames:
            try:
                path.unlink()
            except Exception:
                pass
        self._frames = []

    @staticmethod
    def _browser_type_name(page: Any) -> str:
        try:
            return str(page.context.browser.browser_type.name)
        except Exception:
            return "unknown"

    async def _auto_stop(self, limit: int) -> None:
        """到时自动停止采集（只停采集，不合成，等 stop() 收尾）。"""
        try:
            await asyncio.sleep(limit)
            if self._running and not self._capture_closed:
                logger.warning(f"[Recorder] 达到时长上限 {limit}s，自动停止采集")
                await self._close_capture()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Recorder] 自动停止采集异常: {e}")

    # =====================================================
    # 帧采集
    # =====================================================

    def _on_frame(self, ev: dict, key: str = "") -> None:
        """CDP 帧回调（同步，必须尽快返回）。

        key 是采集该帧的标签页标识：已经切走的目标发来的迟到帧直接丢弃，
        否则时间轴里会混进上一个标签页的画面。
        """
        try:
            if self._capture_closed or self._session_dir is None:
                return
            if key and key != self._target_key:
                return
            if len(self._frames) >= self.max_frames:
                return

            data = ev.get("data") or b""
            if isinstance(data, str):
                data = base64.b64decode(data)
            if not data:
                return

            # 采集尺寸监控：中途进全屏/改窗口会让帧尺寸变化（混合尺寸会糊/裁切），
            # 这里只告警不改流程——ffmpeg 端已经按固定尺寸 scale+pad 兜住了。
            try:
                meta = ev.get("metadata") or {}
                fw, fh = int(meta.get("deviceWidth") or 0), int(meta.get("deviceHeight") or 0)
                if fw and fh:
                    if self._real_frame_size is None:
                        self._real_frame_size = (fw, fh)
                        if (fw, fh) != (self._frame_w, self._frame_h):
                            logger.warning(
                                f"[Recorder] 实际采集尺寸 {fw}x{fh} 与预期 "
                                f"{self._frame_w}x{self._frame_h} 不一致（页面可能进了全屏/改了窗口）"
                            )
                    elif (fw, fh) != self._real_frame_size and not self._size_change_warned:
                        self._size_change_warned = True
                        logger.warning(f"[Recorder] 采集尺寸在录制中变化: {self._real_frame_size} → {fw}x{fh}")
            except Exception:
                pass

            index = len(self._frames)
            path = self._session_dir / "frames" / f"{index:06d}.jpg"
            path.write_bytes(data)

            # 用到达时刻而不是 CDP metadata 时间戳：切换标签页后各目标的
            # timestamp 基准可能不同，跨目标比较会算错时间轴。
            self._frames.append((path, time.monotonic(), key or self._target_key))

            sid = ev.get("sessionId")
            if sid is not None:
                task = asyncio.create_task(self._ack(sid))
                self._ack_tasks.add(task)
                task.add_done_callback(self._ack_tasks.discard)
        except Exception as e:  # 回调里绝不抛异常
            logger.error(f"[Recorder] 帧回调异常: {e}")

    async def _ack(self, session_id: Any) -> None:
        try:
            await self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            pass

    async def _close_capture(self) -> None:
        """停止 CDP 采集并断开会话（幂等）。"""
        if self._capture_closed:
            return
        self._capture_closed = True
        self._stop_ts = time.monotonic()

        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
        self._watch_task = None

        await self._release_cdp()

        for task in list(self._ack_tasks):
            task.cancel()
        self._ack_tasks.clear()

    # =====================================================
    # 停止录制 + 合成
    # =====================================================

    async def stop(self) -> tuple[Optional[Path], dict]:
        """停止录制并合成 mp4。

        Returns:
            (mp4 路径或 None, 信息字典)。失败时信息字典包含 ``error``。
        """
        if not self._running:
            return None, {"error": "当前没有进行中的录屏"}

        session_dir = self._session_dir
        await self._close_capture()

        if self._auto_stop_task and not self._auto_stop_task.done():
            self._auto_stop_task.cancel()
        self._auto_stop_task = None

        self._running = False
        span = max(0.0, self._stop_ts - self._start_ts)
        frame_count = len(self._frames)

        if self._target_page is not None:
            try:
                self._page_url = self._target_page.url or self._page_url
            except Exception:
                pass

        if frame_count == 0:
            await self._cleanup_session_dir()
            return None, {"error": "没有捕获到任何画面", "seconds": round(span, 1)}

        # 先收音频（SIGINT 让 ffmpeg 写好 wav 头）
        audio_path: Optional[Path] = None
        audio_volume: dict = {}
        audio_fmt: Optional[dict] = None
        if self._audio is not None:
            try:
                audio_fmt = dict(self._audio.fmt)
                # 自校准：音频真实起点 = 第一块 PCM 到达的时刻（start() 时还未知）
                self._audio_started_at = (
                    self._audio.first_data_at or self._audio.started_at or self._audio_started_at
                )
                audio_path = await self._audio.stop()
                if audio_path is not None:
                    audio_volume = await asyncio.to_thread(measure_volume, audio_path, audio_fmt)
            except Exception as e:
                logger.warning(f"[Recorder] 音频收尾失败: {e}")
                audio_path = None
            finally:
                self._audio = None
        if audio_path is not None and audio_volume.get("silent"):
            logger.info("[Recorder] 音频轨全是静音（页面没发声或被静音）")

        mp4_path = self.base_dir / f"{session_dir.name}.mp4"
        try:
            # ===== 音画对齐 =====
            # 视频时间轴的起点是**第一帧到达的时刻**（见 _build_timeline 的 t0），
            # 而音频是截屏流起来之后才启动的 ⇒ 音频缺了开头一段，必须**延后**（-itsoffset）。
            # （早先版本按 _start_ts 去 -ss 裁音频头，方向正好相反 ⇒ 声音超前。）
            audio_delay = 0.0     # >0：音频整体延后
            audio_trim = 0.0      # >0：音频整体提前（裁掉开头）
            if audio_path is not None and self._frames:
                video_t0 = self._frames[0][1]
                audio_t0 = self._audio_started_at - self._audio_startup_latency
                diff = audio_t0 - video_t0
                if diff >= 0:
                    audio_delay = diff
                else:
                    audio_trim = -diff
                if abs(diff) > 0.05:
                    logger.info(
                        f"[Recorder] 音画对齐：音频{'延后' if diff >= 0 else '提前'} {abs(diff):.2f}s"
                        f"（音频起点 - 首帧 = {diff:+.2f}s）"
                    )

            await asyncio.to_thread(
                self._run_ffmpeg, session_dir, mp4_path, span, audio_path,
                audio_delay, audio_trim, audio_fmt,
            )
        except Exception as e:
            logger.error(f"[Recorder] 合成失败: {e}")
            return None, {"error": f"视频合成失败: {e}", "frames": frame_count}

        size = mp4_path.stat().st_size if mp4_path.exists() else 0
        if size <= 0:
            return None, {"error": "视频合成失败: 产物为空", "frames": frame_count}

        # 合成成功后只保留 mp4，删掉原始帧
        await self._cleanup_session_dir()
        await asyncio.to_thread(self._cleanup_old_files)

        info = {
            "path": str(mp4_path),
            "frames": frame_count,
            "seconds": round(span, 1),
            "size": size,
            "url": self._page_url,
            "width": self._frame_w,
            "height": self._frame_h,
            "audio": bool(audio_path),
            "audio_mean_db": audio_volume.get("mean_volume"),
            "audio_silent": bool(audio_volume.get("silent", False)) if audio_path else None,
        }
        logger.info(
            f"[Recorder] 录屏完成: {mp4_path.name}, {span:.1f}s, "
            f"{frame_count} 帧, {size / 1024:.1f}KB, "
            f"音频={'有' if audio_path else '无'}"
            + (f"(均值 {audio_volume.get('mean_volume')}dB)" if audio_volume.get("mean_volume") is not None else "")
        )
        return mp4_path, info

    def _build_timeline(self, span: float) -> list[Path]:
        """把采集到的帧展开成固定帧率时间轴。

        concat 解复用器的 ``duration`` 只决定"下一帧出现在什么时刻"，最后一帧的
        显示时长并不受它控制；因此这里直接按 1/fps 生成输出槽位，每个槽位指向
        该时刻真正在显示的那一帧。这样：
        - 动画页面按 fps 正常降采样；
        - 静止页面（只有 1 帧）也能得到完整时长的视频。
        """
        frames = self._frames
        fps = max(1, self.fps)
        target = max(0.5, min(span, self.max_duration + 3.0))
        slots = max(1, int(round(target * fps)))

        t0 = frames[0][1]
        timeline: list[Path] = []
        index = 0
        for slot in range(slots):
            limit = t0 + slot / fps
            while index + 1 < len(frames) and frames[index + 1][1] <= limit:
                index += 1
            timeline.append(frames[index][0])
        return timeline

    def _run_ffmpeg(self, session_dir: Path, mp4_path: Path, span: float,
                    audio_path: Optional[Path] = None,
                    audio_delay: float = 0.0, audio_trim: float = 0.0,
                    audio_fmt: Optional[dict] = None) -> None:
        """写出 concat 列表并调用 ffmpeg（在线程里跑，避免阻塞事件循环）。

        audio_path 为空时退回原来的"静音 aac 轨"。有音频时按方向对齐：
        audio_delay > 0 → `adelay` 滤镜把音频整体延后；audio_trim > 0 → `-ss` 裁掉音频开头。
        （实测 -itsoffset 在"raw PCM + concat 图片视频"这条链上无效：声音仍在 0 秒；
          adelay 精确生效，2.000s 就是 2.000s。）
        """
        fps = max(1, self.fps)
        timeline = self._build_timeline(span)

        list_file = session_dir / "frames.txt"
        lines = []
        for path in timeline:
            lines.append(f"file '{path}'")
            lines.append(f"duration {1.0 / fps:.4f}")
        # 末尾再放一次最后一帧：把最后一个槽位的时长补满
        lines.append(f"file '{timeline[-1]}'")
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        audio_args: list[str]
        if audio_path is not None and Path(audio_path).exists():
            fmt = audio_fmt or {"rate": 44100, "channels": 2}
            audio_args = ["-f", "s16le", "-ar", str(fmt.get("rate", 44100)),
                          "-ac", str(fmt.get("channels", 2))]
            if audio_trim > 0.02:
                audio_args += ["-ss", f"{audio_trim:.3f}"]
            audio_args += ["-i", str(audio_path)]
            audio_codec = ["-c:a", "aac", "-b:a", "128k", "-ar", "44100"]
            if audio_delay > 0.02:
                # adelay 按毫秒给每个声道加延迟（all=1 表示所有声道）
                audio_codec = ["-af", f"adelay={int(audio_delay * 1000)}:all=1", *audio_codec]
        else:
            audio_args = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono"]
            audio_codec = ["-c:a", "aac", "-b:a", "32k"]

        cmd = [
            self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            *audio_args,
            # 固定输出尺寸：录到一半页面尺寸变了（比如中途切全屏）也不会错位/裁切
            "-vf", (f"scale={self._frame_w}:{self._frame_h}:force_original_aspect_ratio=decrease,"
                    f"pad={self._frame_w}:{self._frame_h}:(ow-iw)/2:(oh-ih)/2,fps={fps}"),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(self.crf),
            "-pix_fmt", "yuv420p",
            *audio_codec,
            "-shortest", "-movflags", "+faststart",
            str(mp4_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise RecordingError(
                f"ffmpeg 退出码 {proc.returncode}: {(proc.stderr or '').strip()[-500:]}"
            )
        if not mp4_path.exists():
            raise RecordingError("ffmpeg 未生成输出文件")

    # =====================================================
    # 取消 / 清理
    # =====================================================

    async def abort(self) -> None:
        """放弃本次录制（浏览器关闭、异常时调用），不产出视频。"""
        if not self._running and self._capture_closed:
            return
        if self._audio is not None:
            try:
                await self._audio.abort()
            except Exception:
                pass
            self._audio = None
        await self._close_capture()
        if self._auto_stop_task and not self._auto_stop_task.done():
            self._auto_stop_task.cancel()
        self._auto_stop_task = None
        self._running = False
        logger.info("[Recorder] 已放弃本次录屏")
        await self._cleanup_session_dir()

    async def _cleanup_session_dir(self) -> None:
        if self._session_dir is None:
            return
        session_dir, self._session_dir = self._session_dir, None
        try:
            if session_dir.exists():
                await asyncio.to_thread(shutil.rmtree, session_dir, True)
        except Exception:
            pass

    def _cleanup_old_files(self) -> None:
        """只保留最近 N 个 mp4；最近 10 分钟内的文件不动（QQ 可能还在读盘）。"""
        try:
            files = sorted(
                [f for f in self.base_dir.glob("*.mp4") if f.is_file()],
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            return

        now = time.time()
        for f in files[self.keep_files:]:
            try:
                if now - f.stat().st_mtime < 600:
                    continue
                f.unlink()
            except Exception:
                continue
