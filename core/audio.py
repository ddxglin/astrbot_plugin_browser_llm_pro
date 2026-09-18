"""录屏取声：把浏览器输出到虚拟声卡（PulseAudio null sink）的声音录成 wav。

为什么要虚拟声卡：容器里没有真声卡，headless Chromium 仍然会把音频输出到
PulseAudio（实测 old headless / new headless 都能），于是用 `module-null-sink`
造一个 sink，把浏览器指向它，再用 ffmpeg 抓这个 sink 的 `.monitor` 源。

⚠️ 关键前提（实测 2026-09-17）：Playwright 启动 chromium 时**默认注入 `--mute-audio`**，
只删自己传的参数不够，必须 `ignore_default_args=["--mute-audio"]`；否则浏览器
`play()` 正常但声音全是静音（录到 -91dB）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from astrbot.api import logger

DEFAULT_SINK = "browser"
DEFAULT_SERVER = "tcp:127.0.0.1:4713"  # 与我们写进 /etc/pulse/system.pa 的匿名 TCP 一致


def pulse_binary() -> Optional[str]:
    return shutil.which("pulseaudio")


def pactl_binary() -> Optional[str]:
    return shutil.which("pactl")


def _pactl_ok(server: str, timeout: float = 2.0) -> bool:
    pactl = pactl_binary()
    if not pactl:
        return False
    try:
        r = subprocess.run([pactl, "info"], env={**os.environ, "PULSE_SERVER": server},
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0 and "Server Name" in (r.stdout or "")
    except Exception:
        return False


def _sink_exists(server: str, sink: str, timeout: float = 2.0) -> bool:
    pactl = pactl_binary()
    if not pactl:
        return False
    try:
        r = subprocess.run([pactl, "list", "short", "sinks"], env={**os.environ, "PULSE_SERVER": server},
                           capture_output=True, text=True, timeout=timeout)
        return any(line.split("\t")[1] == sink for line in (r.stdout or "").splitlines() if "\t" in line)
    except Exception:
        return False


def ensure_keepalive(sink: str = DEFAULT_SINK, server: Optional[str] = None) -> bool:
    """往 sink 里挂一路**静音**常驻流。

    为什么要它：PulseAudio 的 null sink 在没有任何播放流时，它的 `.monitor` **不产生数据**
    （实测：录 5.5s 里只有真正出声的那 0.4s 被录下来，其余"静音"根本没写进文件），
    这会让音轨起点错位、中途断声后还会时间轴塌缩。挂一路静音流把 sink 顶成"常在线"，
    monitor 就会持续输出正确的静音帧。
    """
    pacat = shutil.which("pacat")
    if not pacat or not server:
        return False
    try:
        r = subprocess.run(["pgrep", "-f", f"pacat --device={sink}"], capture_output=True, timeout=3)
        if r.returncode == 0:
            return True  # 已在跑
    except Exception:
        pass
    try:
        # 用 sh -c 让**子进程**自己打开 /dev/zero：如果由父进程开着句柄再传进去，
        # 父进程一回收对象就会关闭 fd，pacat 收到 EOF 立刻退出（踩过）。
        subprocess.Popen(
            ["sh", "-c",
             f"exec {pacat} --device={sink} --rate=44100 --channels=2 --format=s16le < /dev/zero"],
            env={**os.environ, "PULSE_SERVER": server},   # ⚠️ 不带这个，pacat 连不上会静默退出
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        logger.info("[音频] 已挂静音保活流，保证 monitor 持续产帧")
        return True
    except Exception as e:
        logger.warning(f"[音频] 保活流启动失败: {e}")
        return False


def ensure_sink(sink: str = DEFAULT_SINK, allow_start: bool = True) -> Optional[str]:
    """确保有一个可用的 PulseAudio 服务 + 虚拟声卡，返回 PULSE_SERVER 地址（失败返回 None）。

    依次尝试：环境变量 → 我们的匿名 TCP 端口 → 系统 socket；都不行且允许时，
    自行启动 pulseaudio（system 模式）并加载 null sink。
    """
    candidates = [os.environ.get("PULSE_SERVER"), DEFAULT_SERVER, "unix:/var/run/pulse/native"]
    for server in candidates:
        if server and _pactl_ok(server):
            if _sink_exists(server, sink):
                ensure_keepalive(sink, server)
                return server
            # 服务在但没有我们的 sink：尝试加载
            try:
                pactl = pactl_binary()
                r = subprocess.run(
                    [pactl, "load-module", "module-null-sink", f"sink_name={sink}",
                     f"sink_properties=device.description={sink}"],
                    env={**os.environ, "PULSE_SERVER": server}, capture_output=True, text=True, timeout=3)
                if r.returncode == 0 and _sink_exists(server, sink):
                    logger.info(f"[音频] 已为录屏加载虚拟声卡 {sink}（{server}）")
                    ensure_keepalive(sink, server)
                    return server
            except Exception:
                pass

    if not allow_start or not pulse_binary():
        return None

    # 自行拉起：system 模式（容器内是 root），匿名访问 + null sink 见 system.pa
    try:
        logger.info("[音频] PulseAudio 未运行，尝试自行启动（system 模式）…")
        subprocess.run(["pkill", "-9", "pulseaudio"], capture_output=True, timeout=5)
        time.sleep(0.5)
        try:
            os.remove("/var/run/pulse/pid")
        except OSError:
            pass
        subprocess.Popen(
            [pulse_binary(), "--system", "-D", "--disallow-exit", "--exit-idle-time=-1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        time.sleep(1.5)
    except Exception as e:
        logger.warning(f"[音频] 启动 PulseAudio 失败: {e}")
        return None

    for server in (DEFAULT_SERVER, "unix:/var/run/pulse/native"):
        if _pactl_ok(server):
            if not _sink_exists(server, sink):
                try:
                    subprocess.run([pactl_binary(), "load-module", "module-null-sink", f"sink_name={sink}"],
                                   env={**os.environ, "PULSE_SERVER": server},
                                   capture_output=True, timeout=3)
                except Exception:
                    pass
            if _sink_exists(server, sink):
                logger.info(f"[音频] PulseAudio 就绪，虚拟声卡 {sink}（{server}）")
                ensure_keepalive(sink, server)
                return server
    return None


class AudioCapture:
    """录制期间把虚拟声卡的声音抓成 **raw PCM**（s16le/44.1k/stereo）。

    为什么用管道而不是让 ffmpeg 直接写 wav：
    ffmpeg 连上 PulseAudio 到真正开始收样有 ~0.5s（实测 0.07~0.62s 乱跳），
    如果不把这个延迟算进去，音频就会比画面"超前"一大截。
    改成读 ffmpeg 的 stdout 管道后，**第一个字节到达的时刻**就是音频的真实起点
    （管道无缓冲，raw 输出按帧写），于是每次录制都能自校准，不依赖常数。
    """

    def __init__(self, out_path: Path, sink: str = DEFAULT_SINK, server: Optional[str] = None,
                 sample_rate: int = 44100, channels: int = 2):
        self.out_path = Path(out_path)
        self.sink = sink
        self.sample_rate = sample_rate
        self.channels = channels
        self._server = server
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.Task] = None
        self._fh = None
        self.available = False
        self.error: str = ""
        self.started_at: float = 0.0        # 进程拉起时刻（time.monotonic）
        self.first_data_at: float = 0.0     # 收到第一块音频数据的时刻（音频真实起点）

    @property
    def fmt(self) -> dict:
        return {"rate": self.sample_rate, "channels": self.channels}

    async def start(self) -> bool:
        server = self._server or await asyncio.to_thread(ensure_sink, self.sink)
        if not server:
            self.error = "PulseAudio 虚拟声卡不可用"
            return False
        self._server = server
        # 每次开录都确认保活流在线：它一旦不在，空闲的 monitor 就不产帧，
        # 音频起点会被推后到"真正出声"那一刻（首块到达时刻也就不等于音频起点）。
        await asyncio.to_thread(ensure_keepalive, self.sink, server)
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        env = {**os.environ, "PULSE_SERVER": server}
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "pulse", "-i", f"{self.sink}.monitor",
            "-ac", str(self.channels), "-ar", str(self.sample_rate),
            "-f", "s16le", "-",
        ]
        try:
            self._fh = open(self.out_path, "wb")
            self.started_at = time.monotonic()
            self._proc = await asyncio.create_subprocess_exec(
                *cmd, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            self._reader = asyncio.create_task(self._pump(self._proc))
            self.available = True
            logger.info(f"[音频] 开始采集 {self.sink}.monitor（{server}，raw s16le {self.sample_rate}Hz x{self.channels}）")
            return True
        except Exception as e:
            self.error = f"启动音频采集失败: {e}"
            logger.warning(f"[音频] {self.error}")
            await self.abort()
            return False

    async def _pump(self, proc: asyncio.subprocess.Process) -> None:
        """把 ffmpeg stdout 的 PCM 落盘，并记录第一块数据到达的时刻。"""
        try:
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                if not self.first_data_at:
                    self.first_data_at = time.monotonic()
                if self._fh is not None:
                    self._fh.write(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[音频] 采集管道异常: {e}")

    async def wait_first_data(self, timeout: float = 4.0) -> bool:
        """等第一块 PCM 到达（= 音频时间轴真正开始）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.first_data_at:
                return True
            if self._proc is not None and self._proc.returncode is not None:
                self.error = "采集进程已退出"
                return False
            await asyncio.sleep(0.05)
        return bool(self.first_data_at)

    async def stop(self) -> Optional[Path]:
        """停止采集（向 ffmpeg stdin 发 'q' 让它优雅退出），返回 pcm 路径。"""
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.stdin is not None:
                    proc.stdin.write(b"q")
                    await proc.stdin.drain()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=6)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except Exception as e:
                logger.warning(f"[音频] 停止采集异常: {e}")
        if self._reader is not None:
            try:
                await asyncio.wait_for(self._reader, timeout=2)
            except Exception:
                self._reader.cancel()
            self._reader = None
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        self.available = False
        try:
            if self.out_path.exists() and self.out_path.stat().st_size > self.sample_rate // 2:
                return self.out_path
        except OSError:
            pass
        return None

    async def abort(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                await self._proc.wait()
            except Exception:
                pass
            self._proc = None
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        self.available = False
        try:
            self.out_path.unlink()
        except OSError:
            pass


def measure_volume(path: Path, fmt: Optional[dict] = None, timeout: float = 20.0) -> dict:
    """量一段音频的平均/峰值音量（用于判断"确实录到了声音"）。

    fmt 给的是 raw PCM 参数（{"rate":44100,"channels":2}），因为采集产物是 s16le raw。
    """
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    pre: list[str] = []
    if fmt:
        pre = ["-f", "s16le", "-ar", str(fmt.get("rate", 44100)), "-ac", str(fmt.get("channels", 2))]
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", *pre, "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout)
    except Exception:
        return {}
    out = {}
    for line in (r.stderr or "").splitlines():
        if "mean_volume" in line or "max_volume" in line:
            try:
                key, val = line.split("]")[-1].split(":")
                out[key.strip()] = float(val.strip().split()[0])
            except Exception:
                continue
    if "mean_volume" in out:
        out["silent"] = out["mean_volume"] <= -80.0
    return out
