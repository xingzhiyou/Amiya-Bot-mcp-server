from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from src.app.transformers.types import Transformer

logger = logging.getLogger(__name__)

# Chromium 在容器里默认只有 64MB /dev/shm，截图与合成会退化到磁盘临时文件，
# 明显拖慢渲染；加这个参数让它改写 /tmp（官方推荐做法）。
# 部署层如果能给容器 --shm-size=2g，可以再快一截。
_DEFAULT_CHROMIUM_ARGS: tuple[str, ...] = ("--disable-dev-shm-usage",)

# --- 进程内浏览器复用 -------------------------------------------------------
# 原实现每次 transform 都 async_playwright() + launch() + close()，
# 单次光启动/销毁就要 1~3 秒。这里改成进程内单例：
#   * 首次使用时启动 Playwright 与 Chromium；
#   * 之后每次渲染只开 page / 关 page；
#   * 浏览器断连（崩溃）自动重建一次。
# 约束：Playwright async API 绑定事件循环，所以记录创建时的 loop，
#       loop 变化（例如 CLI 多次 asyncio.run）时整体重建。

_pw_manager: Any = None
_playwright: Any = None
_browser: Any = None
_browser_loop: Optional[asyncio.AbstractEventLoop] = None
_launch_lock: Optional[asyncio.Lock] = None
_launch_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _get_launch_lock() -> asyncio.Lock:
    global _launch_lock, _launch_lock_loop
    loop = asyncio.get_running_loop()
    if _launch_lock is None or _launch_lock_loop is not loop:
        _launch_lock = asyncio.Lock()
        _launch_lock_loop = loop
    return _launch_lock


async def _shutdown_browser() -> None:
    """关闭并清空进程内浏览器单例（失败也不抛）。"""
    global _pw_manager, _playwright, _browser, _browser_loop

    browser, playwright, manager = _browser, _playwright, _pw_manager
    _browser = None
    _playwright = None
    _pw_manager = None
    _browser_loop = None

    if browser is not None:
        try:
            await browser.close()
        except Exception:
            logger.debug("关闭复用的 Chromium 失败（忽略）", exc_info=True)
    if playwright is not None:
        try:
            await playwright.stop()
        except Exception:
            logger.debug("停止 Playwright 失败（忽略）", exc_info=True)
    elif manager is not None:
        try:
            await manager.__aexit__(None, None, None)
        except Exception:
            logger.debug("清理 Playwright 上下文失败（忽略）", exc_info=True)


async def shutdown_browser() -> None:
    """对外暴露的浏览器关闭入口：服务优雅退出时调用，避免残留 Chromium 子进程。"""
    await _shutdown_browser()


async def _get_browser(*, headless: bool = True, chromium_args: list[str] | None = None) -> Any:
    """取进程内复用的 Chromium；必要时启动或重建。"""
    global _pw_manager, _playwright, _browser, _browser_loop

    loop = asyncio.get_running_loop()

    if _browser is not None:
        if _browser_loop is loop and _browser.is_connected():
            return _browser
        # 事件循环换了或浏览器已断连：整体重建
        await _shutdown_browser()

    async with _get_launch_lock():
        if _browser is not None and _browser_loop is loop and _browser.is_connected():
            return _browser

        from playwright.async_api import async_playwright

        args: list[str] = list(_DEFAULT_CHROMIUM_ARGS)
        for extra in chromium_args or []:
            if extra not in args:
                args.append(extra)

        manager = async_playwright()
        playwright = await manager.start()
        try:
            browser = await playwright.chromium.launch(headless=headless, args=args)
        except Exception:
            logger.exception(
                "启动 Playwright Chromium 失败，无法渲染 PNG。请检查浏览器安装和系统依赖是否完整。"
            )
            try:
                await playwright.stop()
            except Exception:
                pass
            raise

        _pw_manager = manager
        _playwright = playwright
        _browser = browser
        _browser_loop = loop
        logger.info(
            "Playwright Chromium 已启动（进程内复用）: headless=%s args=%s", headless, args
        )
        return browser


async def probe_playwright_chromium(
    *,
    headless: bool = True,
    chromium_args: list[str] | None = None,
) -> tuple[bool, str | None]:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        return False, f"Playwright 不可用: {exc}"

    launch_args = list(_DEFAULT_CHROMIUM_ARGS)
    for extra in chromium_args or []:
        if extra not in launch_args:
            launch_args.append(extra)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless, args=launch_args)
            await browser.close()
    except Exception as exc:
        return False, str(exc)

    return True, None


class HTMLToPNGTransformer(Transformer):
    """
    使用 Playwright 把 HTML 字符串渲染为 PNG bytes。

    cfg 常用字段（都可选）：
    - viewport: {"width": 900, "height": 520, "deviceScaleFactor": 2}
    - full_page: true/false
    - wait_until: "load" | "domcontentloaded" | "networkidle"（默认 load）
    - extra_wait_ms: 0..n
    - transparent: true/false
    - chromium_args: ["--font-render-hinting=medium", ...]  # 可选，追加到默认参数之后
    - headless: true/false  # 可选

    Chromium 实例在进程内复用，每次渲染只新建/关闭一个 page。
    """

    input_mime = "text/html"
    output_mime = "image/png"

    async def transform(self, *, input: Any, cfg: Dict[str, Any] | None = None) -> bytes:
        if not isinstance(input, str):
            raise TypeError(f"HTMLToPNGTransformer expects input=str, got {type(input)}")

        cfg = cfg or {}

        viewport = cfg.get("viewport") or {"width": 900, "height": 520}
        full_page = bool(cfg.get("full_page", False))
        # 默认用 load：静态卡片 HTML 的图片由 <img> 加载，load 已覆盖；
        # networkidle 需要额外等 500ms 网络静默，弱机上会成倍放大耗时。
        wait_until = cfg.get("wait_until", "load")
        extra_wait_ms = int(cfg.get("extra_wait_ms", 0))
        transparent = bool(cfg.get("transparent", False))

        chromium_args = cfg.get("chromium_args") or []
        headless = cfg.get("headless", True)

        logger.info(
            "开始渲染 PNG: headless=%s viewport=%s full_page=%s wait_until=%s extra_wait_ms=%s chromium_args=%s",
            headless,
            viewport,
            full_page,
            wait_until,
            extra_wait_ms,
            chromium_args,
        )

        png_bytes = await self._render(
            html=input,
            viewport=viewport,
            full_page=full_page,
            wait_until=wait_until,
            extra_wait_ms=extra_wait_ms,
            transparent=transparent,
            chromium_args=chromium_args,
            headless=headless,
        )
        logger.info("渲染 PNG 成功: bytes=%s", len(png_bytes))
        return png_bytes

    async def _render(
        self,
        *,
        html: str,
        viewport: Dict[str, Any],
        full_page: bool,
        wait_until: str,
        extra_wait_ms: int,
        transparent: bool,
        chromium_args: list[str],
        headless: bool,
    ) -> bytes:
        browser = await _get_browser(headless=headless, chromium_args=chromium_args)

        for attempt in (1, 2):
            try:
                page = await browser.new_page(viewport=viewport)  # type: ignore[arg-type]
                try:
                    await page.set_content(html, wait_until=wait_until)
                    if extra_wait_ms > 0:
                        await page.wait_for_timeout(extra_wait_ms)
                    return await page.screenshot(
                        full_page=full_page,
                        type="png",
                        omit_background=transparent,
                    )
                finally:
                    try:
                        await page.close()
                    except Exception:
                        logger.debug("关闭 page 失败（忽略）", exc_info=True)
            except Exception:
                if attempt == 1:
                    # 复用的浏览器可能已崩溃/被回收：重建后重试一次
                    logger.warning("复用 Chromium 渲染失败，重建浏览器后重试", exc_info=True)
                    await _shutdown_browser()
                    browser = await _get_browser(headless=headless, chromium_args=chromium_args)
                    continue
                raise

        raise RuntimeError("渲染 PNG 失败：浏览器重建后仍不可用")
