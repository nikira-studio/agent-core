"""Screenshot every dashboard page at desktop and phone width and flag overflow.

A page change is not done until the screenshots have been looked at. This takes
them. Run it against a copy of an install (or a throwaway one with sample data),
signed in as an admin, never against data you cannot afford to expose:

    python3 scripts/ui_sweep.py --base http://127.0.0.1:3591 \
        --cookie "$(cat admin.cookie)" --out /tmp/shots/after

It needs Playwright (`pip install playwright && playwright install chromium`).
It only reads pages. The overflow check prints any element wider than its card
and any page that scrolls sideways; the screenshots are for a person to open.

It also saves each page as the browser rendered it, with its stylesheets, under
<out>/rendered, and runs the design detector on that folder when it is installed
(`impeccable detect`; pass --detector PATH, or set IMPECCABLE). The detector reads
rendered pages, not Python source, so the HTML this app builds in f-strings is only
checked this way. Findings are printed and written to <out>/detect.json; they do
not change the exit status.
"""

import argparse
import asyncio
import os
import pathlib
import re
import shutil
import subprocess

from playwright.async_api import async_playwright

PAGES = [
    "/", "/activity", "/approvals", "/memory", "/users", "/agents", "/workspaces",
    "/integrations", "/connectors", "/connectors/directory", "/connectors/adapters",
    "/connectors/help", "/credentials", "/audit", "/webhooks", "/settings",
]
SETTINGS_TABS = ("memory", "data", "security")
WIDTHS = {"desktop": (1280, 900), "phone": (390, 800)}

OVERFLOW_JS = """() => {
  const vw = document.documentElement.clientWidth, bad = [];
  document.querySelectorAll('.main *').forEach(el => {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const cs = getComputedStyle(el);
    if (cs.position === 'fixed' || cs.display === 'none' || cs.visibility === 'hidden') return;
    let scrolls = false;  // inside something that scrolls on purpose (a table, a code block)
    for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
      if (getComputedStyle(a).overflowX !== 'visible') { scrolls = true; break; }
    }
    if (scrolls) return;
    const card = el.closest('.card');
    if (card && el !== card && getComputedStyle(card).overflowX === 'visible'
        && r.right > card.getBoundingClientRect().right + 2) {
      bad.push(el.tagName + '.' + String(el.className || '').slice(0, 30));
    }
  });
  return {scrolls: document.documentElement.scrollWidth > vw + 1, spill: bad.slice(0, 5), count: bad.length};
}"""


DEFAULT_DETECTOR = "~/.claude/skills/impeccable/scripts/impeccable"


async def save_rendered(page, base: str, rendered: pathlib.Path, slug: str) -> None:
    """Save the page as rendered (after its scripts ran) with the /static files it links, so a detector can open it."""
    html = await page.content()
    (rendered / f"{slug}.html").write_text(html, encoding="utf-8")
    for ref in set(re.findall(r'(?:href|src)="(/static/[^"?#]+)', html)):
        target = rendered / ref.lstrip("/")
        if target.exists():
            continue
        response = await page.context.request.get(base + ref)
        if response.ok:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(await response.body())


def run_detector(detector: str, rendered: pathlib.Path, out: pathlib.Path) -> None:
    path = os.path.expanduser(detector)
    if not (os.path.isfile(path) or shutil.which(path)):
        print("design detector not found; skipped (pass --detector PATH to run it on", rendered, ")")
        return
    result = subprocess.run([path, "detect", "--json", "--no-config", str(rendered)], capture_output=True, text=True)
    (out / "detect.json").write_text(result.stdout, encoding="utf-8")
    try:
        import json
        findings = json.loads(result.stdout or "[]")
        findings = findings if isinstance(findings, list) else findings.get("findings", [])
        counts: dict[str, int] = {}
        for item in findings:
            counts[item.get("antipattern", "?")] = counts.get(item.get("antipattern", "?"), 0) + 1
        print("design detector:", "clean" if not findings else ", ".join(f"{k} x{v}" for k, v in sorted(counts.items())),
              "- details in", out / "detect.json")
    except ValueError:
        print("design detector output was not JSON; see", out / "detect.json")


async def main(base: str, cookie: str, out: pathlib.Path, detector: str = "") -> int:
    out.mkdir(parents=True, exist_ok=True)
    rendered = out / "rendered"
    rendered.mkdir(exist_ok=True)
    problems = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        host = base.split("//", 1)[1].split("/", 1)[0].split(":")[0]
        for name, (width, height) in WIDTHS.items():
            ctx = await browser.new_context(viewport={"width": width, "height": height})
            await ctx.add_cookies([{"name": "session_token", "value": cookie, "domain": host, "path": "/"}])
            page = await ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)[:120]))
            targets = [(path, None) for path in PAGES] + [("/settings", tab) for tab in SETTINGS_TABS]
            for path, tab in targets:
                await page.goto(base + path, wait_until="load")
                await page.wait_for_timeout(400)
                if tab:
                    await page.click(f"[data-tab={tab}]")
                slug = (path.strip("/").replace("/", "_") or "overview") + (f"-{tab}" if tab else "")
                await page.screenshot(path=str(out / f"{slug}-{name}.png"), full_page=True)
                if name == "desktop":
                    await save_rendered(page, base, rendered, slug)
                result = await page.evaluate(OVERFLOW_JS)
                if result["scrolls"] or result["count"]:
                    problems += 1
                    print(f"{name:7} {path}{'#' + tab if tab else ''}: "
                          f"{'page scrolls sideways; ' if result['scrolls'] else ''}spill={result['spill']}")
            if errors:
                problems += 1
                print(name, "JavaScript errors:", errors[:5])
            await ctx.close()
        await browser.close()
    print("overflow check:", "clean" if not problems else f"{problems} problem(s)", "- now open the screenshots in", out)
    run_detector(detector or os.environ.get("IMPECCABLE", DEFAULT_DETECTOR), rendered, out)
    return problems


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, help="Dashboard address, for example http://127.0.0.1:3500")
    parser.add_argument("--cookie", required=True, help="An admin's session_token cookie value")
    parser.add_argument("--out", type=pathlib.Path, required=True, help="Folder for the screenshots")
    parser.add_argument("--detector", default="", help="Path to the impeccable command (default: $IMPECCABLE or ~/.claude/skills/impeccable/scripts/impeccable)")
    args = parser.parse_args()
    raise SystemExit(1 if asyncio.run(main(args.base.rstrip("/"), args.cookie, args.out, args.detector)) else 0)
