"""Flash（SWF）支持：本地起一个静态小服务，把 Ruffle 注入到页面里。

为什么要自己注入而不是装扩展：本机 Playwright 启动的 chromium/chrome-headless-shell
**加载不了扩展**（Playwright 默认带 `--disable-extensions`，忽略掉之后实测仍不生效，
2026-09-18 验证），而 Ruffle 自己托管的 web 版在同样的浏览器里跑得好好的（官方 demo 页
能加载 SWF，HTTP 200）。

做法：`vendor/ruffle/` 里放 Ruffle 自托管产物（ruffle.js + *.wasm + core 分片），
本模块用标准库 http.server 在 127.0.0.1 上跑一个只读静态服务（localhost 属"可信来源"，
不受 https 页面混合内容限制），然后把 `ruffle.js` 注入页面；Ruffle 会自动接管页面里
遗留的 `<embed>/<object>` Flash 元素。只在页面确实含 Flash 元素时才注入，普通网页零开销。
"""

from __future__ import annotations

import asyncio
import http.server
import socket
import socketserver
import threading
from pathlib import Path
from typing import Optional

from astrbot.api import logger

RUFFLE_DIR = Path(__file__).resolve().parents[1] / "vendor" / "ruffle"


PLAYER_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Flash Player</title>
<style>html,body{margin:0;padding:0;height:100%;background:#000;overflow:hidden}
ruffle-player{display:block;margin:0 auto;width:min(100vw, 960px) !important;
  height:min(100vh, 600px) !important;background:#000}</style>
</head><body>
<script>
window.RufflePlayer = window.RufflePlayer || {};
window.RufflePlayer.config = {
  publicPath: "__BASE__",
  autoplay: "on", unmuteOverlay: "hidden", splashScreen: false,
  letterbox: "on", scale: "showAll", quality: "low",
  allowScriptAccess: true, logLevel: "error", warnOnUnsupportedContent: false,
  openUrlMode: "allow", favorFlash: true,
};
</script>
<script src="__BASE__ruffle.js"></script>
<script>
(function () {
  const q = new URLSearchParams(location.search);
  const swf = q.get("swf") || "";
  const base = q.get("base") || swf.replace(/[^/]*$/, "");
  document.body.dataset.ruffle = "loading";
  window.__ruffleError = "";
  const mark = (v) => { document.body.dataset.ruffle = v; };
  const fail = (m) => { window.__ruffleError = String(m); mark("error"); };
  setTimeout(() => { if (document.body.dataset.ruffle === "loading") mark("maybe"); }, 6000);
  if (!swf) { fail("no swf in query"); return; }
  try {
    // 自托管版暴露的是 API：RufflePlayer.newest().createPlayer()，
    // **不是**预先注册好的 <ruffle-player> 标签（实测 customElements.get 一直是 undefined）
    const ruffle = window.RufflePlayer.newest();
    const player = ruffle.createPlayer();
    player.id = "player";
    player.addEventListener("loadedmetadata", () => mark("ready"));
    player.addEventListener("loadeddata", () => mark("ready"));
    player.addEventListener("error", (e) => fail((e && e.detail) || "ruffle error"));
    document.body.appendChild(player);
    const p = player.load({ url: "/fetch?url=" + encodeURIComponent(swf), base: base });
    if (p && typeof p.then === "function") {
      p.then(() => mark("ready")).catch((e) => fail((e && e.message) || e));
    }
    // 兜底：Ruffle 内部就绪但没有事件的情况
    setTimeout(() => { if (document.body.dataset.ruffle === "loading") mark("maybe"); }, 6000);
  } catch (e) { fail((e && e.message) || e); }
})();
</script>
</body></html>"""

MAX_FETCH_MB = 200


class _Handler(http.server.SimpleHTTPRequestHandler):
    """vendor/ruffle 静态文件 + 两个动态端点（本地播放页、SWF 取回）。"""

    def log_message(self, *args):  # 静音
        pass

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=86400")
        super().end_headers()

    def _send_bytes(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):  # noqa: N802
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        if parsed.path == "/player.html":
            base = f"http://127.0.0.1:{self.server.server_address[1]}/"
            self._send_bytes(PLAYER_HTML.replace("__BASE__", base).encode("utf-8"),
                             "text/html; charset=utf-8")
            return
        if parsed.path == "/fetch":
            url = (parse_qs(parsed.query).get("url") or [""])[0]
            if not url.startswith(("http://", "https://")):
                self._send_bytes(b"bad url", "text/plain", 400)
                return
            try:
                import urllib.request

                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; AstrBotBrowser/1.0)",
                    "Accept": "*/*",
                })
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = r.read(MAX_FETCH_MB * 1024 * 1024 + 1)
                if len(data) > MAX_FETCH_MB * 1024 * 1024:
                    self._send_bytes(b"too large", "text/plain", 413)
                    return
                ctype = "application/x-shockwave-flash"
                if data[:3] not in (b"FWS", b"CWS", b"ZWS"):
                    ctype = "application/octet-stream"   # 不是 SWF 也照样给，Ruffle 会报错
                logger.info(f"[Ruffle] 取回 SWF {url[:90]} ({len(data) // 1024}KB)")
                self._send_bytes(data, ctype)
            except Exception as e:
                logger.warning(f"[Ruffle] 取回 SWF 失败: {e}")
                self._send_bytes(f"fetch failed: {e}".encode("utf-8"), "text/plain", 502)
            return
        super().do_GET()


class RuffleServer:
    """极简静态服务：把 vendor/ruffle 暴露在 127.0.0.1:<port>。"""

    def __init__(self, directory: Path = RUFFLE_DIR):
        self.directory = Path(directory)
        self.httpd: Optional[socketserver.TCPServer] = None
        self.port: int = 0
        self._thread: Optional[threading.Thread] = None

    @staticmethod
    def available(directory: Path = RUFFLE_DIR) -> bool:
        try:
            return (Path(directory) / "ruffle.js").is_file()
        except Exception:
            return False

    def start(self) -> Optional[str]:
        """启动服务，返回 base URL（失败返回 None）。"""
        if self.httpd is not None:
            return self.base_url
        if not self.available(self.directory):
            logger.warning(f"[Ruffle] 没找到 {self.directory}/ruffle.js，Flash 支持关闭")
            return None
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                self.port = s.getsockname()[1]
            handler = lambda *a, **kw: _Handler(*a, directory=str(self.directory), **kw)  # noqa: E731
            self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", self.port), handler)
            self.httpd.daemon_threads = True
            self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            self._thread.start()
            logger.info(f"[Ruffle] Flash 支持已就绪: {self.base_url}")
            return self.base_url
        except Exception as e:
            logger.warning(f"[Ruffle] 静态服务启动失败，Flash 支持关闭: {e}")
            self.httpd = None
            return None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def player_url(self, swf_url: str, base: Optional[str] = None) -> str:
        """本地播放页地址：把远端 .swf 通过 /fetch 取回后交给 Ruffle 播放。"""
        from urllib.parse import quote

        url = f"{self.base_url}player.html?swf={quote(swf_url, safe='')}"
        if base:
            url += f"&base={quote(base, safe='')}"
        return url

    def stop(self) -> None:
        try:
            if self.httpd is not None:
                self.httpd.shutdown()
                self.httpd.server_close()
        except Exception:
            pass
        self.httpd = None


def install_script(base_url: str) -> str:
    """注入到每个页面的脚本：只在页面确实有 Flash 元素时装 Ruffle。

    - document_start 注册，DOMContentLoaded / 2s 后各检查一次（覆盖动态插入的 embed）
    - 用 script 标签加载本地 ruffle.js，publicPath 指回本地服务，wasm 才找得到
    - 不碰普通网页（没有 embed/object[type*=flash] 就直接返回）
    """
    js = """
(() => {
  if (window.__astrbotRuffleTried) return;
  const SEL = 'embed[type*="flash" i], embed[src$=".swf" i], object[type*="flash" i], object[data$=".swf" i]';
  const inject = () => {
    try {
      if (window.__astrbotRuffleTried) return;
      if (!document.querySelector(SEL)) return;
      window.__astrbotRuffleTried = true;
      window.RufflePlayer = window.RufflePlayer || {};
      window.RufflePlayer.config = Object.assign({
        publicPath: "__BASE__",
        autoplay: "on",
        unmuteOverlay: "hidden",
        splashScreen: false,
        letterbox: "on",
        scale: "showAll",
        quality: "low",
        logLevel: "error",
      }, window.RufflePlayer.config || {});
      const s = document.createElement('script');
      s.src = "__BASE__ruffle.js";
      s.async = false;
      (document.head || document.documentElement).appendChild(s);
    } catch (e) {}
  };
  document.addEventListener('DOMContentLoaded', inject, { once: true });
  setTimeout(inject, 2500);
  setTimeout(inject, 6000);
})();
""".replace("__BASE__", base_url)
    return js
