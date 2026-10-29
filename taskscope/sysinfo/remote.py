"""Read-only remote summary: a small HTTP server with a token-protected page.

Uses only the Python standard library. It binds to 127.0.0.1 unless the user
chooses another address, and every request needs the access token, which is
compared in constant time. It never accepts commands, only serves readings.
"""

import hmac
import html
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{host} - Taskscope</title>
<style>
:root{{color-scheme:light dark;--bg:#f5f6f8;--fg:#1c1f24;--card:#fff;--bar:#3b82f6;--mut:#6b7280}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181c;--fg:#e6e8eb;--card:#202328;--mut:#9aa0a6}}}}
body{{margin:0;padding:16px;font:15px system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
h1{{font-size:18px;margin:0 0 12px}}.g{{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(240px,1fr))}}
.c{{background:var(--card);border-radius:10px;padding:12px}}.t{{color:var(--mut);font-size:13px}}
.v{{font-size:22px;font-weight:600;margin:4px 0}}.b{{height:6px;border-radius:3px;background:#8882}}
.b i{{display:block;height:100%;border-radius:3px;background:var(--bar)}}
table{{width:100%;border-collapse:collapse;font-size:13px}}td{{padding:3px 4px;border-top:1px solid #8883}}
</style></head><body><h1 id="h">{host}</h1><div class="g" id="g"></div>
<div class="c" style="margin-top:12px"><div class="t">Top processes</div><table id="p"></table></div>
<script>
const T={token};
const fmt=b=>{{const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<4){{b/=1024;i++}}return b.toFixed(i?1:0)+' '+u[i]}};
function card(t,v,p,s){{return `<div class="c"><div class="t">${{t}}</div><div class="v">${{v}}</div>`+
 (p==null?'':`<div class="b"><i style="width:${{Math.min(100,p)}}%"></i></div>`)+`<div class="t">${{s||''}}</div></div>`}}
const esc=s=>String(s).replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));
async function tick(){{try{{const r=await fetch('api/summary',{{headers:{{'X-Taskscope-Token':T}}}});
const d=await r.json();let h=card('CPU',d.cpu.total.toFixed(0)+'%',d.cpu.total,esc(d.cpu.model));
h+=card('Memory',d.memory.percent.toFixed(0)+'%',d.memory.percent,fmt(d.memory.used)+' / '+fmt(d.memory.total));
for(const k of d.disks)h+=card('Disk '+esc(k.name),k.busy.toFixed(0)+'%',k.busy,'R '+fmt(k.read_rate)+'/s  W '+fmt(k.write_rate)+'/s');
for(const n of d.net)if(n.up)h+=card(esc(n.kind+' '+n.name),fmt(n.rx_rate+n.tx_rate)+'/s',null,'↓ '+fmt(n.rx_rate)+'/s  ↑ '+fmt(n.tx_rate)+'/s');
for(const g of d.gpus)h+=card('GPU',g.util==null?'n/a':g.util.toFixed(0)+'%',g.util,esc(g.name));
document.getElementById('g').innerHTML=h;
document.getElementById('p').innerHTML=d.top.map(p=>`<tr><td>${{esc(p.name)}}</td><td>${{esc(p.user)}}</td><td>${{p.cpu.toFixed(1)}}%</td><td>${{fmt(p.rss)}}</td></tr>`).join('');
}}catch(e){{document.getElementById('h').textContent='Disconnected'}}}}
tick();setInterval(tick,2000);
</script></body></html>"""


class RemoteServer:
    def __init__(self, snapshot_fn, host="127.0.0.1", port=8765, token=None):
        self.snapshot_fn = snapshot_fn
        self.host, self.port = host, port
        self.token = token or secrets.token_urlsafe(18)
        self.httpd = None
        self.thread = None

    @property
    def url(self):
        h = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{h}:{self.port}/?t={self.token}"

    def start(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _authorized(self, q):
                given = self.headers.get("X-Taskscope-Token") or (q.get("t") or [""])[0]
                return hmac.compare_digest(given.encode(), server.token.encode())

            def _send(self, code, body, ctype):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if not self._authorized(q):
                    return self._send(403, "Forbidden: missing or wrong token\n", "text/plain")
                if u.path in ("/", "/index.html"):
                    import socket
                    return self._send(200, PAGE.format(host=html.escape(socket.gethostname()),
                                                       token=json.dumps(server.token)),
                                      "text/html; charset=utf-8")
                if u.path == "/api/summary":
                    return self._send(200, json.dumps(server.snapshot_fn()), "application/json")
                return self._send(404, "Not found\n", "text/plain")

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self.url

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


def make_snapshot(perf_sample, procs):
    top = sorted(procs, key=lambda p: p.cpu, reverse=True)[:15]
    d = dict(perf_sample)
    d["top"] = [{"name": p.name, "pid": p.pid, "user": p.user, "cpu": p.cpu, "rss": p.rss} for p in top]
    return d
