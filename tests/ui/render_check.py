"""Render the done-tasks page markup in a real browser and screenshot it.

Feeds the ACTUAL SSR output (``out/case1.html`` and ``out/case2.html``) — produced by
the plugin's own harness — into Chromium inside a laptop-sized viewport with a minimal
stand-in stylesheet for the utility classes the plugin uses. Confirms there is
no horizontal overflow at 1280px and shows what the owner will see.

Run ``harness.mjs`` first; screenshots land in ``tests/ui/out/``.
"""
import asyncio, pathlib
from playwright.async_api import async_playwright

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / 'out'
OUT.mkdir(parents=True, exist_ok=True)

# A pragmatic subset of the utility classes the plugin emits, plus the design
# tokens it references, so the render is representative without pulling in the
# whole desktop bundle.
CSS = """
:root{
  --ui-text-primary:#e7e7ea; --ui-text-secondary:#b6b6bd; --ui-text-tertiary:#8b8b95;
  --ui-stroke-secondary:#33333a; --ui-bg-tertiary:#1b1b20; --ui-bg-quaternary:#26262c;
  --chrome-action-hover:#ffffff14;
}
*{box-sizing:border-box}
body{margin:0;background:#131317;color:var(--ui-text-primary);
  font:13px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
#root{height:100vh}
.flex{display:flex}.flex-col{flex-direction:column}.flex-wrap{flex-wrap:wrap}
.items-center{align-items:center}.items-start{align-items:flex-start}.items-baseline{align-items:baseline}
.justify-between{justify-content:space-between}.gap-1{gap:.25rem}.gap-1\\.5{gap:.375rem}
.gap-2{gap:.5rem}.gap-3{gap:.75rem}.gap-x-2{column-gap:.5rem}.gap-y-0\\.5{row-gap:.125rem}
.h-full{height:100%}.w-full{width:100%}.grow{flex-grow:1}.shrink-0{flex-shrink:0}
.min-w-0{min-width:0}.overflow-hidden{overflow:hidden}.p-4{padding:1rem}
.px-2\\.5{padding-left:.625rem;padding-right:.625rem}.py-1\\.5{padding-top:.375rem;padding-bottom:.375rem}
.py-2\\.5{padding-top:.625rem;padding-bottom:.625rem}.pl-3{padding-left:.75rem}.pr-1{padding-right:.25rem}
.pr-2\\.5{padding-right:.625rem}.mt-0\\.5{margin-top:.125rem}
.relative{position:relative}.absolute{position:absolute}.inset-y-1\\.5{top:.375rem;bottom:.375rem}
.left-0{left:0}.w-0\\.5{width:2px}.rounded-md{border-radius:6px}.rounded-full{border-radius:9999px}
.border{border:1px solid var(--ui-stroke-secondary)}
.border-amber-500\\/35{border-color:#f59e0b59}.border-amber-500\\/30{border-color:#f59e0b4d}
.border-red-500\\/30{border-color:#ef44444d}
.bg-amber-500\\/\\[0\\.06\\]{background:#f59e0b0f}.bg-amber-500\\/\\[0\\.07\\]{background:#f59e0b12}
.bg-amber-500\\/\\[0\\.10\\]{background:#f59e0b1a}.bg-amber-400{background:#fbbf24}
.bg-red-500\\/\\[0\\.07\\]{background:#ef444412}
.hover\\:bg-amber-500\\/\\[0\\.10\\]:hover{background:#f59e0b1a}
.hover\\:bg-\\(--chrome-action-hover\\):hover{background:var(--chrome-action-hover)}
.hover\\:text-\\(--ui-text-primary\\):hover{color:var(--ui-text-primary)}
.text-amber-50{color:#fffbeb}.text-amber-200\\/90{color:#fde68acc}.text-red-200\\/90{color:#fecacacc}
.text-\\(--ui-text-secondary\\){color:var(--ui-text-secondary)}
.text-\\(--ui-text-tertiary\\){color:var(--ui-text-tertiary)}
.text-xs{font-size:.75rem}.text-sm{font-size:.8125rem}.text-base{font-size:1rem}
.text-\\[0\\.6875rem\\]{font-size:.6875rem}.text-\\[0\\.75rem\\]{font-size:.75rem}
.font-medium{font-weight:500}.font-semibold{font-weight:600}
.uppercase{text-transform:uppercase}.tracking-wide{letter-spacing:.025em}
.italic{font-style:italic}.leading-snug{line-height:1.35}.leading-relaxed{line-height:1.6}
.whitespace-pre-wrap{white-space:pre-wrap}.break-words{overflow-wrap:anywhere}
.border\\(--ui-stroke-secondary\\){border-color:var(--ui-stroke-secondary)}
h1{margin:0}
ul{list-style:none;margin:0;padding:0}
button{font:inherit;color:inherit;background:transparent;border:0;cursor:pointer;
  display:inline-flex;align-items:center;border-radius:4px}
button[disabled]{opacity:.45;cursor:not-allowed}
[data-badge]{display:inline-flex;align-items:center;gap:.25rem;border-radius:3px;
  padding:2px 6px;font-size:.65rem;line-height:1;white-space:nowrap}
[data-badge="warn"]{background:#f59e0b1f;color:#fbbf24}
[data-size="micro"]{padding:2px 4px;font-size:.75rem;line-height:1rem}
[data-variant="ghost"]{color:var(--ui-text-secondary)}
[data-segmented]{display:inline-grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:2px;
  border-radius:5px;background:var(--ui-bg-tertiary);padding:2px;width:fit-content}
[data-segmented] > button{padding:2px 10px;font-size:.6875rem;font-weight:500;
  border-radius:3px;color:#97979f;justify-content:center}
[data-opt][data-active="true"]{background:#33333c;color:#f2f2f4}
[data-scroll]{overflow-y:auto}
[data-skeleton]{background:#22222a;border-radius:6px}
#marker{padding:8px 16px;font-size:11px;color:#8b8b95;border-bottom:1px solid #33333a}
"""

HTML = """<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head>
<body><div id="root"><div id="marker">{label} &nbsp;|&nbsp; viewport 1280x800</div>{body}</div></body></html>"""


async def shoot(page, body_file, label, out_name, width=1280, height=800):
    body = pathlib.Path(body_file).read_text()
    await page.set_viewport_size({'width': width, 'height': height})
    await page.set_content(HTML.format(css=CSS, body=body, label=label))
    await page.wait_for_timeout(250)
    # Measure horizontal overflow: this is the laptop-usability criterion.
    metrics = await page.evaluate("""() => ({
      docScroll: document.documentElement.scrollWidth,
      docClient: document.documentElement.clientWidth,
      rootScroll: document.getElementById('root').scrollWidth,
      bodyText: document.body.innerText.length,
      rows: document.querySelectorAll('li').length,
      badges: document.querySelectorAll('[data-badge="warn"]').length
    })""")
    path = OUT / out_name
    await page.screenshot(path=str(path))
    print(f'{label}: rows={metrics["rows"]} badges={metrics["badges"]} '
          f'scrollWidth={metrics["docScroll"]} clientWidth={metrics["docClient"]} '
          f'overflow={"YES" if metrics["docScroll"] > metrics["docClient"] else "no"} -> {path}')
    return metrics


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page()
        m1 = await shoot(page, OUT / 'case1.html', 'LIVE /done endpoint', 'done-tasks-live.png')
        m2 = await shoot(page, OUT / 'case2.html', 'Board-digest fallback', 'done-tasks-fallback.png')
        # narrow laptop
        m3 = await shoot(page, OUT / 'case1.html', 'Narrow laptop 1024px', 'done-tasks-narrow.png', width=1024, height=700)
        await b.close()
    over = [m['docScroll'] > m['docClient'] for m in (m1, m2, m3)]
    print('\nHORIZONTAL OVERFLOW ANYWHERE:', 'YES' if any(over) else 'NO')
    return 0 if not any(over) else 1

raise SystemExit(asyncio.run(main()))
