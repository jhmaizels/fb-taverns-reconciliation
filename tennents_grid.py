"""
The Tennents price grid — the LWC pricing master's shape for the Scottish
estate: products down the rows, pubs across the columns, each cell the
tenant's OFF-INVOICE £/brl for that product at that pub (blank = not on the
pub's price file). The toggle flips every cell to what that split leaves FB
(the retro £/brl) with the tenant's net keg price underneath.

Pure render: takes a TennentsMaster and the request's query params, returns
the page body as HTML. No Airtable access here — the routes in webapp.py load
the master (cached) and apply cell edits through tennents_master's planners
and airtable_io's writers:

  cell (pub × product)   → POST /tennents/cell/apply          off_invoice
  Total / WSP (product)  → POST /tennents/product-cell/apply  total, wsp
  + Add product          → GET  /tennents/product/new
  + Add pub              → GET  /tennents/pub/new

Edit mode reuses the LWC grid's machinery (form.cellf + input.cell-input,
dirty highlighting, "Save changes" posting each dirty form with ajax=1 and
reading JSON) and the same CSS classes, so the two grids look and behave the
same. The pricing structure differs and the grid says so: there is ONE
estate-wide total discount per product (the Total column, editable in edit
mode), and a cell only ever moves the split between tenant and FB.
"""
from __future__ import annotations

from datetime import date
from html import escape
from urllib.parse import urlencode

from auth_supabase import ext_url
from tennents_master import TennentsMaster, keg_brl_factor


def _money(v) -> str:
    return "—" if v is None else f"£{v:,.2f}"


def _hidden(fields: dict[str, str]) -> str:
    return "".join(f'<input type="hidden" name="{escape(k)}" value="{escape(v)}">' for k, v in fields.items())


def _qs(**kw) -> str:
    pairs = [(k, v) for k, v in kw.items() if v]
    return ("?" + urlencode(pairs)) if pairs else ""


def grid_sites(master: TennentsMaster, include_managed: bool = False) -> list:
    """The columns: pubs with a real account number, by name. Managed pubs
    take the whole discount off-invoice by rule, so they have no split to set
    and are left out unless asked for (shown greyed, 'all off')."""
    out = [s for s in master.sites if s.account and s.account.upper() != "TBC"
           and (include_managed or not s.is_managed)]
    return sorted(out, key=lambda s: (s.site_name or "").lower())


def grid_skus(master: TennentsMaster, q: str = "") -> list:
    """The rows: every SKU on the master, by product name then container
    then code, filtered by a free-text search over code, alt codes, brand
    and name."""
    q_low = (q or "").strip().lower()

    def _match(k) -> bool:
        if not q_low:
            return True
        return q_low in f"{k.sku_code} {k.alt_code} {k.brand} {k.product} {k.container}".lower()

    return sorted((k for k in master.skus if k.sku_code and _match(k)),
                  key=lambda k: ((k.product or k.brand or "").upper(), (k.container or "").upper(), k.sku_code))


def render_tennents_grid(master: TennentsMaster, params: dict, is_admin: bool,
                         banner_html: str = "", recent: list[dict] | None = None) -> str:
    q = (params.get("q") or "").strip()
    site_f = (params.get("site") or "").strip()
    include_managed = (params.get("managed") or "") == "1"
    edit = bool(is_admin and (params.get("edit") == "1"))
    today = date.today()

    sites = grid_sites(master, include_managed=include_managed or bool(site_f))
    if site_f and not any(s.account == site_f for s in sites):
        site_f = ""
    cols = [s for s in sites if s.account == site_f] if site_f else sites
    skus = grid_skus(master, q)
    n_sites, n_prods = len(cols), len(skus)

    base = ext_url("/tennents/master")
    e = "1" if edit else ""
    mg = "1" if include_managed else ""
    clear = f' <a href="{base}{_qs(edit=e, site=site_f, managed=mg)}" style="font-size:0.85em">clear</a>' if q else ""
    keep = _hidden({"edit": e, "managed": mg}) if (e or mg) else ""
    all_sites = grid_sites(master, include_managed=True)
    site_opts = ['<option value="">All pubs</option>'] + [
        f'<option value="{escape(s.account)}"{" selected" if s.account == site_f else ""}>'
        f'{escape(s.site_name)}{" (managed)" if s.is_managed else ""} — {escape(s.account)}</option>'
        for s in all_sites
    ]
    if not is_admin:
        edit_btns = ""
    elif edit:
        save_reload = base + _qs(saved="1", edit="1", site=site_f, q=q, managed=mg)
        edit_btns = (
            f'<a class="button" style="margin-top:0" href="{ext_url("/tennents/product/new")}">+ Add product</a>'
            f'<a class="button" style="margin-top:0" href="{ext_url("/tennents/pub/new")}">+ Add pub</a>'
            f'<button type="button" id="grid-save" class="button" style="margin-top:0; background:#1b5e20" '
            f'data-reload="{escape(save_reload, quote=True)}" disabled>Save changes</button>'
            f'<a class="button" id="grid-done" style="margin-top:0; background:#666" '
            f'href="{base}{_qs(site=site_f, q=q, managed=mg)}">Done editing</a>'
        )
    else:
        edit_btns = (
            f'<a class="button" style="margin-top:0" href="{base}{_qs(edit="1", site=site_f, q=q, managed=mg)}">Edit prices</a>'
        )
    managed_link = (
        f'<a href="{base}{_qs(edit=e, site=site_f, q=q)}">hide managed pubs</a>' if include_managed
        else f'<a href="{base}{_qs(edit=e, site=site_f, q=q, managed="1")}">show managed pubs</a>'
    )
    toolbar = f"""
<div class="pivot-toolbar">
  <button type="button" id="pivot-toggle" class="toggle">Show retro &amp; net keg</button>
  <form method="get" action="{base}">
    {keep}<select name="site" onchange="this.form.submit()" style="padding:0.45em; margin:0">{''.join(site_opts)}</select>
    <input type="search" name="q" value="{escape(q)}" placeholder="Filter products…">
    <button type="submit">Search</button>{clear}
  </form>
  {edit_btns}
  <span class="grow"></span>
  <span class="help" style="margin:0">{n_prods} products × {n_sites} pub{"s" if n_sites != 1 else ""} · {managed_link} · as of {today.isoformat()}</span>
  <a href="{base}?view=tables">Workbook tables / exceptions →</a>
</div>"""
    saved_banner = ""
    if params.get("saved"):
        saved_banner = (
            '<div class="master-banner" style="background:#e8f5e9; border-color:#a5d6a7; color:#1b5e20">'
            'Saved. The grid below already reflects your change; the price files follow at once.</div>'
        )
    intro = (
        '<p class="help" style="margin-top:0">Each cell is the <strong>off-invoice £/brl</strong> the tenant gets on '
        'that product at that pub — what goes on the pub\'s price file. Blank = not sold there. The <strong>Total</strong> '
        'column is the one estate-wide discount Tennents give FB on the product; what the tenant is not given '
        'off-invoice comes to FB as the monthly <strong>retro</strong>, which is what the toggle shows '
        '(with the tenant\'s net keg price underneath). Managed pubs take the whole total off-invoice by rule.</p>'
    )
    edit_help = (
        '<p class="help" style="margin-top:0">Editing works like the spreadsheet: '
        '<strong>type off-invoice figures into as many cells as you like</strong>, then press '
        '<strong>Save changes</strong> to commit them all · <strong>clear a cell</strong> (or type 0) to take the '
        'product off that pub\'s price file · the <strong>WSP</strong> and <strong>Total</strong> columns are editable '
        'too — one figure per product, every pub (the retro at each pub follows; a total below a pub\'s off-invoice is '
        'refused). <strong>+ Add product</strong> for a product new to the master, <strong>+ Add pub</strong> for a '
        'pub not yet on it. (Pressing Enter in a single cell still saves just that one.) '
        'Unsaved cells are highlighted; you\'ll be warned before leaving with unsaved changes. '
        'Changes reach the price files and the monthly reconciliation at once; the workbook is told on its next upload.</p>'
    ) if edit else ""

    back = (f'<p class="sub" style="margin-top:0; margin-bottom:0.4em">'
            f'<a href="{ext_url("/tennents")}">← Back to Tennents</a></p>')
    if not skus or not cols:
        empty = "No products match this filter." if q else "No Tennents master loaded yet."
        return (f'<div class="pivot-wide">{back}<h1>Tennents price grid</h1>{banner_html}{saved_banner}'
                f'{toolbar}<p class="help">{escape(empty)}</p></div>')

    # ---- header ----
    def _site_head(s) -> str:
        tag = '<span class="acct">managed · all off</span>' if s.is_managed else (
            f'<span class="acct">{escape(s.discount_construct)}</span>' if s.is_bespoke else "")
        return (f'<th class="num site" title="{escape(s.site_name)} — Tennents account {escape(s.account)}">'
                f'{escape(s.site_name)}<span class="sid">{escape(s.account)}</span>{tag}</th>')

    thead = ('<thead><tr><th class="sticky-col c1">Code</th><th class="sticky-col c2">Product</th>'
             '<th class="num">WSP £/brl</th><th class="num">Total £/brl</th>'
             + "".join(_site_head(s) for s in cols) + '</tr></thead>')

    apply_url = ext_url("/tennents/cell/apply")
    papply_url = ext_url("/tennents/product-cell/apply")

    def _split(k, off: float | None):
        """(retro £/brl, net keg £) for an off-invoice at this SKU."""
        total = None if k.correct_total_per_brl is None else float(k.correct_total_per_brl)
        if off is None or total is None:
            return None, None
        retro = round(total - off, 2)
        net = None if k.wsp_per_brl is None else round((float(k.wsp_per_brl) - off) * keg_brl_factor(k), 2)
        return retro, net

    def _margin_html(k, off: float | None) -> str:
        retro, net = _split(k, off)
        if retro is None:
            return '<span class="cell-margin pivot-empty">n/a</span>'
        net_html = f'<span class="pct">{escape(_money(net))}/keg</span>' if net is not None else '<span class="pct">no WSP</span>'
        cls = "cell-neg" if retro < -0.005 else ("cell-warn" if retro < 0.005 else "cell-pos")
        return f'<span class="cell-margin {cls}">{escape(_money(retro))}{net_html}</span>'

    def _read_cell(k, s) -> str:
        if s.is_managed:
            if k.correct_total_per_brl is None:
                return '<td class="num pinfo"><span class="pivot-empty">·</span></td>'
            off = round(float(k.correct_total_per_brl), 2)
            return (f'<td class="num pinfo" title="managed pub: the whole total off-invoice">'
                    f'<span class="cell-price pivot-empty">{escape(_money(off))}</span>{_margin_html(k, off)}</td>')
        sp = master.site_price(s.account, k.sku_code)
        off = None if sp is None else round(float(sp.off_invoice_per_brl or 0.0), 2)
        if not off:
            return '<td class="num"><span class="pivot-empty">·</span></td>'
        bar = str(sp.source_file or "").startswith("bar plan:")
        title = f' title="set on the page: {escape(sp.source_file[9:].strip())}"' if bar else ""
        return (f'<td class="num{" cell-support" if bar else ""}"{title}>'
                f'<span class="cell-price">{escape(_money(off))}</span>{_margin_html(k, off)}</td>')

    def _edit_cell(k, s) -> str:
        if s.is_managed:
            return _read_cell(k, s)
        sp = master.site_price(s.account, k.sku_code)
        off = None if sp is None else round(float(sp.off_invoice_per_brl or 0.0), 2)
        val = "" if not off else f"{off:.2f}"
        hidden = _hidden({"account": s.account, "sku_code": k.sku_code, "fsite": site_f, "fq": q, "fmanaged": mg})
        if k.correct_total_per_brl is None:
            return (f'<td class="num edit-cell"><form method="post" action="{apply_url}" class="cellf">{hidden}'
                    f'<input type="number" step="0.01" min="0" name="off_invoice" class="cell-input" value="{val}" '
                    f'data-prev="{val}" disabled title="RATE TBC — set the product\'s total first"></form></td>')
        title = (f"{s.site_name} — off-invoice £/brl; Enter saves; clear or 0 takes it off the price file"
                 + (f" (flat £{s.flat_retro_per_brl:,.2f}/brl retro pub)" if s.flat_retro_per_brl is not None else ""))
        return (f'<td class="num edit-cell"><form method="post" action="{apply_url}" class="cellf">{hidden}'
                f'<input type="number" step="0.01" min="0" name="off_invoice" class="cell-input" value="{val}" '
                f'data-prev="{val}" title="{escape(title)}">{_margin_html(k, off)}</form></td>')

    def _pinfo(k, idx: int) -> str:
        if not edit:
            tot = ('<span class="pivot-empty">RATE TBC</span>' if k.correct_total_per_brl is None
                   else f"<strong>{escape(_money(float(k.correct_total_per_brl)))}</strong>")
            return (f'<td class="num pinfo">{escape(_money(k.wsp_per_brl))}</td>'
                    f'<td class="num pinfo">{tot}</td>')
        wsp_val = "" if k.wsp_per_brl is None else f"{float(k.wsp_per_brl):.2f}"
        tot_val = "" if k.correct_total_per_brl is None else f"{float(k.correct_total_per_brl):.2f}"
        hidden = _hidden({"sku_code": k.sku_code, "fsite": site_f, "fq": q, "fmanaged": mg})
        return (
            f'<td class="num pinfo edit-cell"><form method="post" action="{papply_url}" class="cellf" id="pcell-{idx}">'
            f'{hidden}<input type="number" step="0.01" min="0" name="wsp" class="cell-input" value="{wsp_val}" '
            f'data-prev="{wsp_val}" title="WSP £/brl — the list price the discount comes off; blank leaves it">'
            f'<button type="submit" style="display:none" tabindex="-1" aria-hidden="true"></button></form></td>'
            f'<td class="num pinfo edit-cell"><input type="number" step="0.01" min="0" name="total" form="pcell-{idx}" '
            f'class="cell-input" value="{tot_val}" data-prev="{tot_val}" '
            f'title="Agreed total discount £/brl — estate-wide; every pub\'s retro follows"'
            f'{" placeholder=&quot;TBC&quot;" if not tot_val else ""}></td>'
        )

    body: list[str] = []
    for i, k in enumerate(skus):
        name = escape(k.product or k.brand)
        cont = f'<span class="sid" style="display:block; color:#789; font-size:0.82em">{escape(k.container)}</span>' if k.container else ""
        alt = f' <span style="color:#9aa" title="alt codes">/ {escape(k.alt_code)}</span>' if k.alt_code else ""
        prod_cells = (f'<td class="sticky-col c1 pcode">{escape(k.sku_code)}{alt}</td>'
                      f'<td class="sticky-col c2">{name}{cont}</td>')
        cells = "".join((_edit_cell if edit else _read_cell)(k, s) for s in cols)
        total_attr = "" if k.correct_total_per_brl is None else f"{float(k.correct_total_per_brl):.2f}"
        wsp_attr = "" if k.wsp_per_brl is None else f"{float(k.wsp_per_brl):.2f}"
        body.append(f'<tr data-total="{total_attr}" data-wsp="{wsp_attr}" data-keg="{keg_brl_factor(k):.6f}">'
                    f'{prod_cells}{_pinfo(k, i)}{cells}</tr>')

    table = ('<div class="pivot-topscroll" id="pivot-top"><div id="pivot-top-inner"></div></div>'
             f'<div class="pivot-wrap" id="pivot-wrap"><table class="pivot{" editing" if edit else ""}" id="pivot-tbl">'
             f'{thead}<tbody>{"".join(body)}</tbody></table></div>')
    legend = ('<p class="help" style="margin-top:0">Retro view: FB\'s retro £/brl on the product at that pub '
              '(total − off-invoice), with the tenant\'s net keg price (WSP − off-invoice, × barrels per keg) underneath. '
              'A cell with a dashed outline was set on this page rather than the workbook. '
              'Blank cell = product not on that pub\'s price file.</p>')

    recent_html = ""
    if recent:
        rows = "".join(
            f"<tr><td>{escape(r.get('site_name') or 'every pub')}</td>"
            f"<td>{escape(str(r.get('product') or r.get('sku_code', '')))}</td>"
            f"<td class='r'>{_money(r.get('figure'))}{' total' if r.get('kind') == 'sku' else ''}</td>"
            f"<td>{escape(str(r.get('who', '')))}</td>"
            f"<td class='sub'>{escape(str(r.get('notes', ''))[:160])}</td></tr>"
            for r in recent
        )
        recent_html = (f'<h2>Recent changes made here</h2><table><thead><tr><th>Pub</th><th>Product</th>'
                       f'<th class="r">£/brl</th><th>Who, when</th><th>Note</th></tr></thead><tbody>{rows}</tbody></table>')

    return (f'<div class="pivot-wide">{back}<h1>Tennents price grid</h1>{banner_html}{saved_banner}'
            f'{toolbar}{intro}{edit_help}{table}{legend}{GRID_JS}{recent_html}</div>')


# In-grid editing, the LWC grid's driver cut down to this grid's two cell
# kinds. Each cell is a <form class=cellf>; "Save changes" POSTs every dirty
# form SEQUENTIALLY with ajax=1 and reads {ok, errors} JSON; Enter in one cell
# saves just that cell; an emptied or zeroed off-invoice cell asks before it
# takes the product off the pub's price file. Typing recomputes the retro /
# net keg figures live from the row's data-total / data-wsp / data-keg.
GRID_JS = r"""<script>(function(){
var t=document.getElementById('pivot-tbl');
var tog=document.getElementById('pivot-toggle');
if(tog&&t){tog.addEventListener('click',function(){var on=t.classList.toggle('show-margin');tog.textContent=on?'Show off-invoice':'Show retro & net keg';});}
if(!t)return;
var saveBtn=document.getElementById('grid-save');
var doneLink=document.getElementById('grid-done');
function sameNum(a,b){a=(a||'').trim();b=(b||'').trim();if(a===''&&b==='')return true;if(a===''||b==='')return false;var x=parseFloat(a),y=parseFloat(b);if(isNaN(x)||isNaN(y))return a===b;return Math.abs(x-y)<0.005;}
function dirty(i){return !i.disabled&&!sameNum(i.value,i.getAttribute('data-prev')||'');}
function refresh(){var n=0;Array.prototype.forEach.call(t.querySelectorAll('input.cell-input'),function(i){if(dirty(i)){i.classList.add('dirty');n++;}else{i.classList.remove('dirty');}});if(saveBtn){saveBtn.textContent=n?('Save changes ('+n+')'):'Save changes';saveBtn.disabled=(n===0);}return n;}
function fmt(x){return '£'+x.toLocaleString('en-GB',{minimumFractionDigits:2,maximumFractionDigits:2});}
function isZero(s){var x=parseFloat((s||'').trim());return !isNaN(x)&&Math.abs(x)<0.005;}
function rowNums(tr){var ti=tr.querySelector('input[name=total]'),wi=tr.querySelector('input[name=wsp]');var tot=ti?parseFloat(ti.value):parseFloat(tr.getAttribute('data-total'));var wsp=wi?parseFloat(wi.value):parseFloat(tr.getAttribute('data-wsp'));return{tot:tot,wsp:wsp,keg:parseFloat(tr.getAttribute('data-keg'))||1};}
function updRow(tr){var n=rowNums(tr);Array.prototype.forEach.call(tr.querySelectorAll('input[name=off_invoice]'),function(oi){var m=oi.parentNode.querySelector('.cell-margin');if(!m)return;var off=parseFloat(oi.value);if(isNaN(off)||isNaN(n.tot)){m.className='cell-margin pivot-empty';m.textContent='n/a';return;}var retro=n.tot-off;var cls=retro<-0.005?'cell-neg':(retro<0.005?'cell-warn':'cell-pos');m.className='cell-margin '+cls;m.innerHTML=fmt(retro)+'<span class="pct">'+(isNaN(n.wsp)?'no WSP':fmt((n.wsp-off)*n.keg)+'/keg')+'</span>';});}
t.addEventListener('input',function(e){var i=e.target;if(!i||!i.classList||!i.classList.contains('cell-input'))return;refresh();var tr=i.closest('tr');if(tr)updRow(tr);});
function post(f){var body=new URLSearchParams(new FormData(f));body.set('ajax','1');return fetch(f.getAttribute('action'),{method:'POST',credentials:'same-origin',headers:{'X-Requested-With':'fetch','Content-Type':'application/x-www-form-urlencoded'},body:body.toString()}).then(function(r){return r.json().catch(function(){return{ok:false,errors:['HTTP '+r.status],raw:true};}).then(function(j){return{status:r.status,j:j};});}).catch(function(){return{status:0,j:{ok:false,errors:['network error'],raw:true}};});}
function flash(i){i.classList.add('saved');setTimeout(function(){i.classList.remove('saved');},1000);}
function errmsg(res){var m=(res.j&&res.j.errors&&res.j.errors.join('; '))||('HTTP '+res.status);if(res.j&&res.j.raw)m+='\nThe save may still have landed — reload the grid to check before retrying.';return m;}
var busy=false;var chain=Promise.resolve();
function enq(fn){var p=chain.then(fn,fn);chain=p.then(function(){},function(){});return p;}
function cellInputs(f){return Array.prototype.filter.call(f.elements,function(el){return el.classList&&el.classList.contains('cell-input')&&!el.disabled;});}
function formDirty(f){return cellInputs(f).some(dirty);}
function removals(forms){var n=0;forms.forEach(function(f){cellInputs(f).forEach(function(i){var p=(i.getAttribute('data-prev')||'').trim();if(i.name==='off_invoice'&&p!==''&&!isZero(p)&&(i.value.trim()===''||isZero(i.value)))n++;});});return n;}
function restoreTotals(forms){forms.forEach(function(f){cellInputs(f).forEach(function(i){if(i.name==='total'&&i.value.trim()===''&&(i.getAttribute('data-prev')||'')!==''){i.value=i.getAttribute('data-prev');var tr=i.closest('tr');if(tr)updRow(tr);}});});}
function saveOne(f){restoreTotals([f]);var ins=cellInputs(f);if(!ins.length||!formDirty(f)){refresh();return Promise.resolve(true);}var rem=removals([f]);if(rem&&!confirm('Take this product off the pub\'s price file? (Off-invoice becomes £0.)')){ins.forEach(function(i){i.value=i.getAttribute('data-prev')||'';});refresh();var tr=f.closest('tr');if(tr)updRow(tr);return Promise.resolve(false);}var dl=ins.filter(dirty);return enq(function(){return post(f);}).then(function(res){if(res.j&&res.j.ok){ins.forEach(function(i){i.setAttribute('data-prev',i.value.trim());i.classList.remove('dirty','err');});dl.forEach(flash);refresh();return true;}dl.forEach(function(i){i.classList.add('err');});alert('Could not save: '+errmsg(res));return false;});}
function saveAll(){if(busy)return Promise.resolve({ok:false,wrote:0});var forms=[];Array.prototype.forEach.call(t.querySelectorAll('form.cellf'),function(f){if(formDirty(f))forms.push(f);});restoreTotals(forms);forms=forms.filter(formDirty);if(!forms.length){refresh();return Promise.resolve({ok:true,wrote:0});}var rem=removals(forms);if(rem&&!confirm('Take '+rem+' product'+(rem>1?'s':'')+' off the price file'+(rem>1?'s':'')+'? (Off-invoice becomes £0.)')){refresh();return Promise.resolve({ok:false,wrote:0});}busy=true;if(saveBtn){saveBtn.disabled=true;saveBtn.textContent='Saving…';}var idx=0,ok=true,wrote=0;function step(){if(idx>=forms.length)return Promise.resolve(ok);var f=forms[idx++];var dl=cellInputs(f).filter(dirty);return enq(function(){return post(f);}).then(function(res){if(res.j&&res.j.ok){if(res.j.saved)wrote++;cellInputs(f).forEach(function(i){i.setAttribute('data-prev',i.value.trim());i.classList.remove('dirty','err');});return step();}ok=false;dl.forEach(function(i){i.classList.add('err');});if(dl[0])dl[0].focus();alert('Could not save: '+errmsg(res)+'\nStopped — earlier cells were saved; the rest are still unsaved.');return ok;});}return step().then(function(all){busy=false;refresh();return{ok:all,wrote:wrote};});}
if(saveBtn){saveBtn.addEventListener('click',function(){saveAll().then(function(r){if(r.ok&&r.wrote){location=saveBtn.getAttribute('data-reload')||location.href;}});});}
if(doneLink){doneLink.addEventListener('click',function(e){if(refresh()>0){e.preventDefault();var href=doneLink.getAttribute('href');saveAll().then(function(r){if(r.ok)location=href;});}});}
t.addEventListener('submit',function(e){var f=e.target;if(!f.classList||!f.classList.contains('cellf'))return;e.preventDefault();saveOne(f);});
window.addEventListener('beforeunload',function(e){if(refresh()>0){e.preventDefault();e.returnValue='';return '';}});
refresh();
var w=document.getElementById('pivot-wrap'),tp=document.getElementById('pivot-top'),ti=document.getElementById('pivot-top-inner');
if(w&&tp&&ti){var sync=function(){ti.style.width=t.scrollWidth+'px';tp.style.display=(t.scrollWidth>w.clientWidth)?'block':'none';};sync();window.addEventListener('resize',sync);var lock=false;tp.addEventListener('scroll',function(){if(lock){lock=false;return;}lock=true;w.scrollLeft=tp.scrollLeft;});w.addEventListener('scroll',function(){if(lock){lock=false;return;}lock=true;tp.scrollLeft=w.scrollLeft;});}
})();</script>"""
