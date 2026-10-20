"""站点品牌资源（单一来源）。

品牌图标的单一来源（改图标只改这里）。
放在这个独立模块里，供 `/admin` 管理端与 `/ext` 客户控制台**共用**，
由 main.py 的 `/favicon.svg` 路由对外提供 —— 避免同一份图标在两个路由文件里各存一份。
"""

FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64" role="img" aria-label="SevFish">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0E7490"/><stop offset=".52" stop-color="#1D4ED8"/><stop offset="1" stop-color="#6D28D9"/>
    </linearGradient>
  </defs>
  <rect width="64" height="64" rx="15" fill="url(#g)"/>
  <path d="M19 19.5h27L31 47" fill="none" stroke="#fff" stroke-width="9.5" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="47.5" cy="47" r="5.8" fill="#fff"/>
</svg>"""
