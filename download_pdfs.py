"""Download CHI 2026 paper PDFs through a real Edge window.

This does not bypass Cloudflare. A fresh Playwright/Chromium profile will
almost always get a challenge. The working approach is:

Modern Edge ignores --remote-debugging-port on the default profile, so this
script launches a separate Edge user-data-dir and keeps it for later runs.

1. Use installed msedge.exe (not Playwright Chromium).
2. Persist cookies in .edge-cdp-profile so Cloudflare only needs a human click
   the first time.
3. Navigate inside that window. Do not use Playwright's extra HTTP stack.
4. Wait for Cloudflare's JS check; if a checkbox remains, pause for you.
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "data" / "chi26_papers.json"
PDF_DIR = ROOT / "pdfs"
PROFILE_DIR = ROOT / ".edge-cdp-profile"
PROCEEDINGS_URL = "https://dl.acm.org/doi/proceedings/10.1145/3772318"

CF_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-turnstile",
    "cdn-cgi/challenge",
    "attention required",
    "sorry, you have been blocked",
    "enable javascript and cookies",
    "verify you are human",
    "请稍候",
    "正在验证",
    "确认您是真人",
)
WINDOWS_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def load_manifest() -> dict:
    if not MANIFEST.exists():
        sys.exit(f"Missing {MANIFEST}. Run fetch_list.py first.")
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def save_manifest(data: dict) -> None:
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_filename(doi: str, title: str) -> str:
    suffix = doi.split("/", 1)[-1].replace("/", "_")
    short = WINDOWS_BAD.sub("_", title).strip(" .")[:80] or "paper"
    return f"{suffix} - {short}.pdf"


def looks_like_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-" and len(data) > 8_000


def looks_like_challenge(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in CF_MARKERS)


def snapshot(page) -> tuple[str, str]:
    try:
        return page.title(), page.url
    except Exception:
        return "", ""


def is_challenge_view(title: str, url: str) -> bool:
    blob = f"{title}\n{url}"
    return looks_like_challenge(blob) or "/cdn-cgi/" in url or "challenges.cloudflare.com" in url


def open_url(page, url: str, hint: bool = False) -> None:
    print(f"打开 {url}", flush=True)
    if hint:
        print(
            "若出现人机验证：请只在弹出的 Edge 窗口里勾选。终端不用按键，点完后脚本会自己继续。",
            flush=True,
        )
    try:
        page.goto(url, wait_until="commit", timeout=20_000)
    except PlaywrightTimeout:
        print("页面还在加载/验证，继续等待当前窗口。", flush=True)


def wait_until_ready(page, what: str, timeout: float = 300.0) -> None:
    """Poll title/url only. Never read page.content() on a CF page (it hangs)."""
    deadline = time.time() + timeout
    last_report = 0.0
    while time.time() < deadline:
        title, url = snapshot(page)
        challenged = is_challenge_view(title, url)
        acm_ok = "dl.acm.org" in url and not challenged
        if acm_ok:
            print(f"{what}已通过。标题: {title}", flush=True)
            return
        now = time.time()
        if now - last_report >= 8:
            hint = "请在 Edge 里勾选人机验证" if challenged or not url else "等待页面跳转"
            print(f"等待中（{hint}）标题={title[:50]!r}", flush=True)
            last_report = now
        time.sleep(1.5)
    print("等了很久仍未进入 ACM。请先在 Edge 里点完验证，再回到终端按回车。", flush=True)
    input("点完后按回车继续...")
    title, url = snapshot(page)
    if "dl.acm.org" not in url or is_challenge_view(title, url):
        print("仍不像 ACM 正文页。可以再试一次，或 Ctrl+C 后重跑。", flush=True)


def launch_download_edge(playwright):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print("新版 Edge 禁止调试你平时的默认配置，所以会另开一个专用下载会话。")
    print(f"配置目录: {PROFILE_DIR}")
    print("平时的 Edge 可以继续开着。第一次进 ACM 若弹出验证，在这个新窗口里点一下即可。")
    try:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="msedge",
            headless=False,
            accept_downloads=True,
            viewport={"width": 1400, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
    except Exception as exc:
        sys.exit(
            "无法启动本机 Edge。请确认已安装 Microsoft Edge，并已运行: "
            f"playwright install chromium\n原始错误: {exc}"
        )
    page = context.pages[0] if context.pages else context.new_page()
    page.set_default_timeout(15_000)
    page.set_default_navigation_timeout(20_000)
    return context, page


def warmup(page) -> None:
    open_url(page, PROCEEDINGS_URL, hint=True)
    wait_until_ready(page, "ACM 正刊页")


def fetch_pdf_via_page(page, url: str) -> bytes:
    """Pull the PDF with the tab's own fetch, then copy it out in chunks."""
    page.set_default_timeout(120_000)
    info = page.evaluate(
        """async (url) => {
            const res = await fetch(url, { credentials: 'include', redirect: 'follow' });
            const buf = new Uint8Array(await res.arrayBuffer());
            window.__chiPdf = buf;
            return {
                status: res.status,
                type: res.headers.get('content-type') || '',
                length: buf.length,
            };
        }""",
        url,
    )
    length = int(info.get("length") or 0)
    status = info.get("status")
    ctype = info.get("type") or ""
    if length < 8_000:
        raise RuntimeError(f"文件过小: HTTP {status} {ctype} {length} bytes")

    chunks: list[bytes] = []
    offset = 0
    step = 256 * 1024
    while offset < length:
        part = page.evaluate(
            """({ offset, size }) => {
                const slice = window.__chiPdf.subarray(offset, offset + size);
                let binary = '';
                const piece = 0x8000;
                for (let i = 0; i < slice.length; i += piece) {
                    binary += String.fromCharCode.apply(null, slice.subarray(i, i + piece));
                }
                return btoa(binary);
            }""",
            {"offset": offset, "size": step},
        )
        chunks.append(base64.b64decode(part))
        offset += step

    try:
        page.evaluate("() => { window.__chiPdf = null; }")
    except Exception:
        pass

    data = b"".join(chunks)
    if not looks_like_pdf(data):
        preview = data[:180].decode("utf-8", "replace")
        raise RuntimeError(f"不是 PDF (HTTP {status} {ctype}): {preview}")
    return data


def fetch_pdf_bytes(page, paper: dict) -> bytes:
    """Stay on the already-verified ACM tab and fetch. Only navigate if CF returns."""
    try:
        data = fetch_pdf_via_page(page, paper["pdf_url"])
        print("  后台拉取成功，未打开新页面", flush=True)
        return data
    except Exception as exc:
        print(f"  后台拉取失败（{exc}），改为打开 PDF 页", flush=True)
        open_url(page, paper["pdf_url"], hint=True)
        wait_until_ready(page, f"PDF {paper['doi']}")
        data = fetch_pdf_via_page(page, paper["pdf_url"])
        try:
            open_url(page, PROCEEDINGS_URL)
            wait_until_ready(page, "回到正刊页")
        except Exception:
            pass
        return data


def download_one(page, paper: dict, dest: Path) -> None:
    last_error = None
    for attempt in range(1, 4):
        try:
            data = fetch_pdf_bytes(page, paper)
            dest.write_bytes(data)
            paper["status"] = "ok"
            paper["path"] = str(dest.relative_to(ROOT))
            paper["error"] = None
            return
        except PlaywrightTimeout as exc:
            last_error = f"timeout: {exc}"
        except Exception as exc:
            last_error = str(exc)
        print(f"  retry {attempt}/3: {last_error}")
        time.sleep(3 * attempt)
    paper["status"] = "failed"
    paper["error"] = last_error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download CHI 2026 paper PDFs via Edge")
    parser.add_argument("--limit", type=int, default=0, help="Download only N pending papers")
    parser.add_argument("--min-delay", type=float, default=4.0)
    parser.add_argument("--max-delay", type=float, default=8.0)
    parser.add_argument("--retry-failed", action="store_true")
    return parser.parse_args()


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    args = parse_args()
    data = load_manifest()
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    queue = []
    for paper in data["papers"]:
        dest = PDF_DIR / safe_filename(paper["doi"], paper["title"])
        if dest.exists() and dest.stat().st_size > 8_000 and dest.read_bytes()[:5] == b"%PDF-":
            paper["status"] = "ok"
            paper["path"] = str(dest.relative_to(ROOT))
            paper["error"] = None
            continue
        if paper["status"] == "ok" and not args.retry_failed:
            continue
        if paper["status"] == "failed" and not args.retry_failed:
            continue
        queue.append((paper, dest))

    if args.limit:
        queue = queue[: args.limit]

    done = sum(1 for paper in data["papers"] if paper["status"] == "ok")
    print(f"清单: {len(data['papers'])} 篇  已下载: {done}  本轮: {len(queue)}")
    print("Cloudflare 策略: 先过正刊页，之后尽量只在后台 fetch，避免每篇都打开新页面。")
    if not queue:
        save_manifest(data)
        print("没有待下载的论文。")
        return

    with sync_playwright() as playwright:
        context, page = launch_download_edge(playwright)
        try:
            acm_cookies = context.cookies(["https://dl.acm.org"])
            print(f"ACM 相关 cookie {len(acm_cookies)} 个")
            warmup(page)

            for index, (paper, dest) in enumerate(queue, start=1):
                print(f"[{index}/{len(queue)}] {paper['doi']}  {paper['title'][:70]}")
                download_one(page, paper, dest)
                if paper["status"] == "ok":
                    print(f"  saved {dest.name} ({dest.stat().st_size // 1024} KB)")
                else:
                    print(f"  FAILED {paper['error']}")
                if index % 5 == 0:
                    save_manifest(data)
                if index != len(queue):
                    time.sleep(random.uniform(args.min_delay, args.max_delay))
        finally:
            context.close()

    save_manifest(data)
    ok = sum(1 for paper in data["papers"] if paper["status"] == "ok")
    failed = sum(1 for paper in data["papers"] if paper["status"] == "failed")
    pending = sum(1 for paper in data["papers"] if paper["status"] == "pending")
    print(f"结束 ok={ok} failed={failed} pending={pending}")


if __name__ == "__main__":
    main()
