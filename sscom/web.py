"""Web lapa: cenu ceļvedis + mani pirkumi. Aizsargāta ar paroli (WEB_PASSWORD, HTTP Basic)."""
from __future__ import annotations

import hmac
import logging
import os
import threading
from datetime import date

from flask import Flask, Response, jsonify, request
from sqlalchemy import or_, select

from . import db, guide, inventory

log = logging.getLogger(__name__)


def create_app(store: db.Store) -> Flask:
    app = Flask(__name__)
    password = os.getenv("WEB_PASSWORD", "")

    @app.before_request
    def _auth():
        auth = request.authorization
        if not password or not auth or not hmac.compare_digest(auth.password or "", password):
            return Response("Nepieciešama parole", 401, {"WWW-Authenticate": 'Basic realm="sscom"'})
        return None

    @app.get("/")
    def index():
        return Response(PAGE, mimetype="text/html")

    # ---------------------------------------------------------- ceļvedis
    @app.get("/api/guide")
    def api_guide():
        q = (request.args.get("q") or "").strip().lower()
        t = request.args.get("type") or ""
        G, T = guide.price_guide, guide.price_targets
        stmt = select(G, T.c.target_price, T.c.note).select_from(G.outerjoin(T, G.c.guide_key == T.c.guide_key))
        if q:
            stmt = stmt.where(G.c.guide_key.ilike(f"%{q}%"))
        if t:
            stmt = stmt.where(G.c.item_type == t)
        if request.args.get("min2"):
            stmt = stmt.where(or_(G.c.n >= 2, T.c.target_price.isnot(None)))
        stmt = stmt.order_by(G.c.n.desc(), G.c.guide_key).limit(500)
        with store.engine.connect() as c:
            rows = [dict(r._mapping) for r in c.execute(stmt)]
            updated = c.execute(select(G.c.updated_at).limit(1)).scalar()
        for r in rows:
            r["updated_at"] = None
        return jsonify(rows=rows, updated=updated.isoformat() if updated else None)

    @app.post("/api/target")
    def api_target():
        d = request.get_json(force=True)
        price = d.get("target_price")
        guide.set_target(store, d["guide_key"], float(price) if price not in (None, "") else None, d.get("note"))
        return jsonify(ok=True)

    # ---------------------------------------------------------- pirkumi
    @app.get("/api/purchases")
    def api_purchases():
        o = inventory.overview(store)
        for p in o["purchases"]:
            p["bought_at"] = p["bought_at"].isoformat()
            p["created_at"] = None
            for i in p["items"]:
                i["sold_at"] = i["sold_at"].isoformat() if i["sold_at"] else None
        return jsonify(o)

    @app.post("/api/prefill")
    def api_prefill():
        d = inventory.prefill_from_listing(store, request.get_json(force=True).get("url", ""))
        return jsonify(d or {})

    @app.post("/api/purchases")
    def api_add():
        d = request.get_json(force=True)
        pid = inventory.add_purchase(
            store, title=d["title"], total_price=float(d["total_price"]), items=d.get("items") or [],
            bought_at=date.fromisoformat(d["bought_at"]) if d.get("bought_at") else None,
            source_url=d.get("source_url"), ss_id=d.get("ss_id"), note=d.get("note"))
        return jsonify(id=pid)

    @app.post("/api/items/<int:item_id>")
    def api_item(item_id: int):
        d = request.get_json(force=True)
        for k in ("listed_price", "sold_price", "est_value", "cost"):
            if k in d:
                d[k] = float(d[k]) if d[k] not in (None, "") else None
        if d.get("sold_at"):
            d["sold_at"] = date.fromisoformat(d["sold_at"])
        inventory.update_item(store, item_id, **d)
        return jsonify(ok=True)

    @app.post("/api/purchases/<int:pid>/reallocate")
    def api_realloc(pid: int):
        inventory.reallocate(store, pid)
        return jsonify(ok=True)

    @app.delete("/api/purchases/<int:pid>")
    def api_delete(pid: int):
        inventory.delete_purchase(store, pid)
        return jsonify(ok=True)

    return app


def start_in_thread(store: db.Store) -> None:
    """Palaiž web serveri fonā (Railway dod PORT). Bez WEB_PASSWORD nestartē."""
    if not os.getenv("WEB_PASSWORD"):
        log.info("WEB_PASSWORD nav iestatīts — web lapa nestartē")
        return
    from waitress import serve
    port = int(os.getenv("PORT", "8080"))
    app = create_app(store)
    threading.Thread(target=lambda: serve(app, host="0.0.0.0", port=port, threads=4),
                     daemon=True, name="web").start()
    log.info("Web lapa: ports %d", port)


PAGE = r"""<!doctype html>
<html lang="lv"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SS.com tirgus</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2330;--mut:#6b7585;--line:#e3e6eb;--acc:#2f6fe4;--ok:#1f9d55;--bad:#d64545;--warn:#c98a00}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--fg:#e6e9ef;--mut:#9aa3b2;--line:#2b313c;--acc:#6b9cff;--ok:#43c37a;--bad:#ff7a7a;--warn:#f0b429}}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 -apple-system,system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{position:sticky;top:0;background:var(--card);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;z-index:2}
header b{margin-right:auto}button,.tab{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:6px 12px;cursor:pointer}
.tab.on{background:var(--acc);color:#fff;border-color:var(--acc)}main{padding:16px;max-width:1100px;margin:auto}
input,select{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:6px 8px;max-width:100%}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:12px}
.tbl{width:100%;overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:640px}th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--mut);font-weight:600;position:sticky;top:0;background:var(--card)}td.n{text-align:right;font-variant-numeric:tabular-nums}
.mut{color:var(--mut);font-size:13px}.ok{color:var(--ok)}.bad{color:var(--bad)}.pill{display:inline-block;border-radius:99px;padding:1px 8px;font-size:12px;border:1px solid var(--line)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:12px}.stat b{display:block;font-size:20px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}.hide{display:none}a{color:var(--acc)}
input.t{width:70px;text-align:right}
</style></head><body>
<header><b>SS.com tirgus</b>
<span class="tab on" data-t="guide">Cenu ceļvedis</span><span class="tab" data-t="inv">Mani pirkumi</span><span class="tab" data-t="add">+ Pirkums</span></header>
<main>
<section id="guide">
 <div class="row"><input id="q" placeholder="Meklēt: 750 ti, ddr4 16gb, thinkpad…" style="flex:1;min-width:200px">
 <select id="type"><option value="">Visi tipi</option><option value="gpu">Videokartes</option><option value="cpu">CPU</option><option value="ram">RAM</option><option value="storage">Diski</option><option value="motherboard">Plates</option><option value="psu">Barošana</option><option value="laptop">Portatīvie</option><option value="case">Korpusi</option><option value="cooler">Dzesēšana</option><option value="monitor">Monitori</option></select>
 <label class="mut"><input type="checkbox" id="min2" checked> tikai ar ≥2 sludinājumiem</label></div>
 <p class="mut">"Pirkt līdz" = ≥20% peļņa no ss.com mediānas un ≤50% no jaunas cenas. "Mana cena": ja sludinājums ir zem tās, bots sūtīs 🎯 ziņu. <span id="upd"></span></p>
 <div class="card tbl"><table><thead><tr><th>Modelis</th><th>ss.com (privāti)</th><th class="n">Mediāna</th><th class="n">Pārdodas</th><th class="n">Jauns LV</th><th class="n">Pirkt līdz</th><th class="n">Mana cena</th><th>Lētākais tagad</th></tr></thead><tbody id="gb"></tbody></table></div>
</section>
<section id="inv" class="hide"><div class="stats" id="st"></div><div id="pl"></div></section>
<section id="add" class="hide"><div class="card">
 <div class="row"><input id="url" placeholder="ss.com saite (aizpildīs automātiski)" style="flex:1;min-width:220px"><button id="pf">Aizpildīt</button></div>
 <div class="row"><input id="title" placeholder="Nosaukums" style="flex:1;min-width:220px"><input id="total" type="number" step="0.01" placeholder="Samaksāju €" style="width:130px"><input id="bdate" type="date"></div>
 <p class="mut">Detaļas (komplektam vairākas). "Vērtība" = cik apmēram var dabūt, pēc tās sadala pirkuma cenu.</p>
 <div id="items"></div><div class="row"><button id="addi">+ detaļa</button><span style="flex:1"></span><button id="save" style="background:var(--acc);color:#fff;border-color:var(--acc)">Saglabāt pirkumu</button></div>
 <input type="hidden" id="ssid"></div></section>
</main>
<script>
const $=s=>document.querySelector(s), eur=v=>v==null?'—':Math.round(v)+' €';
const api=(u,o={})=>fetch(u,{headers:{'Content-Type':'application/json'},...o}).then(r=>r.json());
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('on',x===t));
 ['guide','inv','add'].forEach(id=>$('#'+id).classList.toggle('hide',id!==t.dataset.t)); if(t.dataset.t==='inv')loadInv();});
let gt;function loadGuide(){const p=new URLSearchParams({q:$('#q').value,type:$('#type').value});if($('#min2').checked)p.set('min2',1);
 api('/api/guide?'+p).then(d=>{$('#upd').textContent=d.updated?'Atjaunots: '+d.updated.slice(0,16).replace('T',' ')+' UTC':'';
 $('#gb').innerHTML=d.rows.map(r=>`<tr><td><b>${esc(r.guide_key)}</b><div class="mut">${esc(r.item_type||'')}</div></td>
 <td>${r.n}× · ${eur(r.price_min)}–${eur(r.price_max)}<div class="mut">aktīvi ${r.n_active}, pazuduši ${r.n_gone}</div></td>
 <td class="n">${eur(r.price_median)}</td><td class="n">${r.days_to_sell==null?'—':r.days_to_sell+' d'}</td><td class="n">${eur(r.new_min)}</td>
 <td class="n"><b>${eur(r.buy_max)}</b></td>
 <td class="n"><input class="t" type="number" value="${r.target_price??''}" data-k="${esc(r.guide_key)}" placeholder="—"></td>
 <td>${r.cheapest_url?`<a href="${esc(r.cheapest_url)}" target="_blank">${eur(r.cheapest_price)}</a>`+(r.target_price&&r.cheapest_price<=r.target_price?' 🎯':''):'—'}</td></tr>`).join('')||'<tr><td colspan=8 class="mut">Nav datu</td></tr>';
 document.querySelectorAll('input.t').forEach(i=>i.onchange=()=>api('/api/target',{method:'POST',body:JSON.stringify({guide_key:i.dataset.k,target_price:i.value})}).then(()=>i.style.borderColor='var(--ok)'));});}
['#q','#type','#min2'].forEach(s=>$(s).addEventListener('input',()=>{clearTimeout(gt);gt=setTimeout(loadGuide,250)}));
const ST={stock:'noliktavā',listed:'izlikts',sold:'pārdots'};
function loadInv(){api('/api/purchases').then(d=>{$('#st').innerHTML=[['Ieguldīts',d.invested],['Noliktavā (pašizm.)',d.in_stock],['Pārdots par',d.sold_revenue],['Peļņa',d.sold_profit]]
 .map(([l,v])=>`<div class="card stat"><span class="mut">${l}</span><b class="${l==='Peļņa'?(v>=0?'ok':'bad'):''}">${eur(v)}</b></div>`).join('');
 $('#pl').innerHTML=d.purchases.map(p=>`<div class="card"><div class="row"><b style="flex:1">${esc(p.title)}</b><span class="pill">${p.kind==='kit'?'komplekts':'detaļa'}</span>
 <span class="mut">${p.bought_at} · ${eur(p.total_price)}</span>${p.source_url?`<a href="${esc(p.source_url)}" target="_blank">ss.com</a>`:''}</div>
 <div class="mut">Pārdots ${p.sold_count}/${p.item_count} · ieņemts ${eur(p.revenue)} · peļņa <b class="${p.profit>=0?'ok':'bad'}">${eur(p.profit)}</b></div>
 <div class="tbl"><table><thead><tr><th>Detaļa</th><th class="n">Vērtība</th><th class="n">Pašizm.</th><th>Statuss</th><th class="n">Izlikts</th><th class="n">Pārdots</th><th class="n">Peļņa</th></tr></thead><tbody>
 ${p.items.map(i=>`<tr data-i="${i.id}"><td>${esc(i.name)}<div class="mut">${esc(i.guide_key||'')}</div></td>
 <td class="n"><input class="t" data-f="est_value" type="number" value="${i.est_value??''}"></td><td class="n">${eur(i.cost)}</td>
 <td><select data-f="status">${Object.entries(ST).map(([k,v])=>`<option value="${k}" ${k===i.status?'selected':''}>${v}</option>`).join('')}</select></td>
 <td class="n"><input class="t" data-f="listed_price" type="number" value="${i.listed_price??''}"></td>
 <td class="n"><input class="t" data-f="sold_price" type="number" value="${i.sold_price??''}"></td>
 <td class="n ${i.profit==null?'':(i.profit>=0?'ok':'bad')}">${eur(i.profit)}</td></tr>`).join('')}</tbody></table></div>
 <div class="row"><button data-re="${p.id}">Pārdalīt pašizmaksu pēc vērtībām</button><button data-del="${p.id}" class="bad">Dzēst</button></div></div>`).join('')||'<p class="mut">Vēl nav pirkumu. Pievieno cilnē "+ Pirkums".</p>';
 document.querySelectorAll('tr[data-i] [data-f]').forEach(el=>el.onchange=()=>{const b={};b[el.dataset.f]=el.value;
  if(el.dataset.f==='sold_price'&&el.value)b.status='sold';api('/api/items/'+el.closest('tr').dataset.i,{method:'POST',body:JSON.stringify(b)}).then(loadInv)});
 document.querySelectorAll('[data-re]').forEach(b=>b.onclick=()=>api(`/api/purchases/${b.dataset.re}/reallocate`,{method:'POST'}).then(loadInv));
 document.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>confirm('Dzēst šo pirkumu?')&&api('/api/purchases/'+b.dataset.del,{method:'DELETE'}).then(loadInv));});}
function itemRow(i={}){const d=document.createElement('div');d.className='row';d.innerHTML=`<input placeholder="Detaļa (piem. GTX 1050 Ti 4GB)" value="${esc(i.name||'')}" style="flex:1;min-width:180px" data-n>
 <input type="number" placeholder="Vērtība €" value="${i.est_value??''}" style="width:110px" data-v><input type="hidden" value="${esc(i.item_type||'')}" data-ty><input type="hidden" value="${esc(i.guide_key||'')}" data-g><button>✕</button>`;
 d.querySelector('button').onclick=()=>d.remove();$('#items').append(d);}
$('#addi').onclick=()=>itemRow();$('#bdate').valueAsDate=new Date();itemRow();
$('#pf').onclick=()=>api('/api/prefill',{method:'POST',body:JSON.stringify({url:$('#url').value})}).then(d=>{if(!d.title){alert('Šo sludinājumu bots nezina — aizpildi ar roku.');return}
 $('#title').value=d.title;$('#total').value=d.total_price;$('#ssid').value=d.ss_id||'';$('#items').innerHTML='';d.items.forEach(itemRow);});
$('#save').onclick=()=>{const items=[...$('#items').children].map(r=>({name:r.querySelector('[data-n]').value,est_value:parseFloat(r.querySelector('[data-v]').value)||null,
 item_type:r.querySelector('[data-ty]').value||null,guide_key:r.querySelector('[data-g]').value||null})).filter(i=>i.name);
 if(!$('#title').value||!$('#total').value){alert('Ievadi nosaukumu un cenu');return}
 api('/api/purchases',{method:'POST',body:JSON.stringify({title:$('#title').value,total_price:$('#total').value,bought_at:$('#bdate').value,source_url:$('#url').value||null,ss_id:$('#ssid').value||null,items})})
 .then(()=>{$('#url').value=$('#title').value=$('#total').value='';$('#items').innerHTML='';itemRow();document.querySelector('[data-t=inv]').click();});};
loadGuide();
</script></body></html>"""
