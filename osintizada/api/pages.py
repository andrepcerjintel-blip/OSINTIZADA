"""Páginas HTML da API com a identidade visual do RINO (tela inicial)."""

from __future__ import annotations

from html import escape

from osintizada.branding import COLORS, FULL_TITLE, PRODUCT_NAME, TAGLINE


def home_html(version: str) -> str:
    """Tela inicial: logo, versão, estado dos componentes (via /health) e links da documentação.

    Não exibe dados de investigação (a API de dados exige token quando configurado).
    """
    c = COLORS
    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(FULL_TITLE)}</title>
<link rel="icon" type="image/png" href="/branding/icon.png">
<style>
:root{{--ink:{c['ink']};--graphite:{c['graphite']};--steel:{c['steel']};--accent:{c['accent']};
--glow:{c['accent_glow']};--paper:{c['paper']}}}
*{{box-sizing:border-box}}
html{{background:var(--ink)}}
body{{margin:0;min-height:100vh;background:radial-gradient(circle at 50% 0,#18202b 0,var(--ink) 60%);
color:var(--paper);font-family:system-ui,-apple-system,"Segoe UI",sans-serif;display:flex;
align-items:center;justify-content:center;padding:24px 16px}}
main{{width:100%;max-width:560px;text-align:center}}
.logo{{width:200px;height:200px;border-radius:24px;background:#fff;padding:8px;
box-shadow:0 0 0 1px #2b3440,0 0 32px rgba(30,144,255,.25)}}
h1{{margin:20px 0 4px;font-size:40px;letter-spacing:.18em;font-weight:800}}
.tag{{color:#aeb7c2;margin:0 0 24px}}
.ver{{color:var(--steel);font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin:24px 0}}
.card{{background:var(--graphite);border:1px solid #2b3440;border-radius:10px;padding:10px 6px}}
.card b{{display:block;font-size:12px;color:#aeb7c2;text-transform:uppercase;letter-spacing:.08em}}
.card span{{display:block;font-size:13px;font-weight:600}}
.ok{{color:var(--glow)}} .bad{{color:#ff6b6b}} .warn{{color:#f0b429}}
nav a{{color:var(--glow);text-decoration:none;margin:0 10px;font-weight:600}}
nav a:hover{{text-decoration:underline}}
@media (max-width:480px){{.grid{{grid-template-columns:repeat(2,1fr)}}h1{{font-size:32px}}
.logo{{width:160px;height:160px}}}}
</style></head>
<body><main>
<img class="logo" src="/branding/logo.png" alt="Logo {PRODUCT_NAME}">
<h1>{PRODUCT_NAME}</h1>
<p class="tag">{escape(TAGLINE)}</p>
<div class="grid" id="status">
  <div class="card"><b>API</b><span id="s-api">…</span></div>
  <div class="card"><b>Banco</b><span id="s-database">…</span></div>
  <div class="card"><b>Redis</b><span id="s-redis">…</span></div>
  <div class="card"><b>Worker</b><span id="s-worker">…</span></div>
</div>
<nav><a href="/docs">Swagger</a><a href="/redoc">ReDoc</a><a href="/health">Health</a></nav>
<p class="ver">versão {escape(version)}</p>
</main>
<script>
const cls = v => (v === "ok" || v === "ONLINE") ? "ok" : (v === "STALE" || v === "degraded") ? "warn" : "bad";
const LABELS = {{ok: "ok", ONLINE: "online", STALE: "atrasado", OFFLINE: "offline", NOT_CONFIGURED: "não configurado",
  WORKER_UNAVAILABLE: "indisponível", UNAVAILABLE: "indisponível", UNKNOWN: "desconhecido"}};
fetch("/health").then(r => r.json()).then(h => {{
  const set = (id, v) => {{
    const e = document.getElementById(id);
    e.textContent = LABELS[v] || v; e.title = v; e.className = cls(v);  // código original no title
  }};
  set("s-api", h.api.status); set("s-database", h.database.status);
  set("s-redis", h.redis.status); set("s-worker", h.worker.status);
}}).catch(() => {{ document.getElementById("s-api").textContent = "indisponível"; }});
</script>
</body></html>"""
