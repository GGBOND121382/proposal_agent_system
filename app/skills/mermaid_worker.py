from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path


def render_one(page, request: dict) -> None:
    source = Path(request["source_path"]).read_text(encoding="utf-8")
    svg_path = Path(request["svg_path"])
    png_path = Path(request["png_path"])
    timeout_ms = int(request.get("timeout_ms", 30000))
    render_id = "proposalDiagram_" + str(request.get("request_id", "0")).replace("-", "_")
    page.set_default_timeout(timeout_ms)
    svg_text = page.evaluate(
        """async ({code, renderId}) => {
            const target = document.getElementById('target');
            target.innerHTML = '';
            const rendered = await mermaid.render(renderId, code);
            target.innerHTML = rendered.svg;
            const svg = target.querySelector('svg');
            if (!svg) throw new Error('Mermaid produced no SVG');
            svg.style.background = 'white';
            svg.style.maxWidth = '2100px';
            return svg.outerHTML;
        }""",
        {"code": source, "renderId": render_id},
    )
    page.wait_for_selector("#target svg", timeout=timeout_ms)
    svg_path.write_text(str(svg_text), encoding="utf-8")
    page.locator("#target svg").screenshot(path=str(png_path), omit_background=False, timeout=timeout_ms)


def server() -> int:
    init = json.loads(sys.stdin.readline())
    mermaid_js = init["mermaid_js"]
    executable = init["browser"]
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            executable_path=executable,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions"],
        )
        page = browser.new_page(viewport={"width": 2200, "height": 1600}, device_scale_factor=2)
        page.set_content("<html><head><meta charset='utf-8'></head><body><div id='target'></div></body></html>")
        page.add_script_tag(path=mermaid_js)
        page.evaluate(
            """() => mermaid.initialize({
              startOnLoad: false,
              securityLevel: 'strict',
              theme: 'neutral',
              fontFamily: 'Noto Sans CJK SC, Microsoft YaHei, Arial, sans-serif',
              flowchart: {htmlLabels: true, curve: 'basis', useMaxWidth: true},
              sequence: {useMaxWidth: true, wrap: true}
            })"""
        )
        print(json.dumps({"ready": True}), flush=True)
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            request = json.loads(line)
            if request.get("command") == "shutdown":
                print(json.dumps({"ok": True, "shutdown": True}), flush=True)
                break
            try:
                render_one(page, request)
                print(json.dumps({"ok": True, "request_id": request.get("request_id")}), flush=True)
            except Exception as exc:
                print(json.dumps({"ok": False, "request_id": request.get("request_id"), "error": str(exc), "traceback": traceback.format_exc()[-4000:]}), flush=True)
        browser.close()
    return 0



def once(request_path: str) -> int:
    import os
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    response_path = Path(request["response_path"])
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path=request["browser"],
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions"],
            )
            page = browser.new_page(viewport={"width": 2200, "height": 1600}, device_scale_factor=2)
            page.set_content("<html><head><meta charset='utf-8'></head><body><div id='target'></div></body></html>")
            page.add_script_tag(path=request["mermaid_js"])
            page.evaluate(
                """() => mermaid.initialize({
                  startOnLoad: false, securityLevel: 'strict', theme: 'neutral',
                  fontFamily: 'Noto Sans CJK SC, Microsoft YaHei, Arial, sans-serif',
                  flowchart: {htmlLabels: true, curve: 'basis', useMaxWidth: true},
                  sequence: {useMaxWidth: true, wrap: true}
                })"""
            )
            render_one(page, request)
            response = {"ok": True, "request_id": request.get("request_id")}
            response_path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
            # Avoid browser shutdown deadlocks.  The parent process owns and kills
            # this complete process group after reading the persisted response.
            os._exit(0)
    except Exception as exc:
        response = {
            "ok": False,
            "request_id": request.get("request_id"),
            "error": str(exc),
            "traceback": traceback.format_exc()[-4000:],
        }
        response_path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        os._exit(1)


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--server":
        return server()
    if len(sys.argv) == 3 and sys.argv[1] == "--once":
        return once(sys.argv[2])
    print("usage: python -m app.skills.mermaid_worker --server | --once REQUEST.json", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
