/* ============================================================
   基金台账 · 前端逻辑
   无框架、无构建，一个文件跑完所有交互
   ============================================================ */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  meta: null,
  overview: null,
  funds: [],
  txns: [],
  templates: [],
  alloc: null,
  curTpl: null,
  chartRange: '1y',
  txnFilter: { from: '', to: '', code: '' },
};

/* ---------------- 格式化 ---------------- */

const nf2 = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function money(v, withSign = false) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  const s = nf2.format(Math.abs(v));
  if (withSign) return (v > 0 ? '+' : v < 0 ? '−' : '') + '¥' + s;
  return '¥' + s;
}
function num(v, digits = 2) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  return Number(v).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function pct(v, digits = 2) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  const sign = v > 0 ? '+' : v < 0 ? '−' : '';
  return sign + Math.abs(v).toFixed(digits) + '%';
}
function cls(v) {
  if (v === null || v === undefined || isNaN(v) || Math.abs(v) < 0.0001) return 'flat';
  return v > 0 ? 'up' : 'down';
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function isoDate(d) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function shiftRange(range) {
  const end = new Date();
  const start = new Date();
  if (range === '1m') start.setMonth(start.getMonth() - 1);
  else if (range === '3m') start.setMonth(start.getMonth() - 3);
  else if (range === '1y') start.setFullYear(start.getFullYear() - 1);
  else if (range === 'ytd') { start.setMonth(0); start.setDate(1); }
  else return { from: '2000-01-01', to: isoDate(end) };
  return { from: isoDate(start), to: isoDate(end) };
}

/* ---------------- 请求 ---------------- */

// 快速连续切换筛选/区间时，先发的请求可能后返回，用序号标记丢弃过期响应
let seqOverview = 0;
let seqTxns = 0;
let seqAlloc = 0;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!res.ok) throw new Error((data && data.error) || `请求失败 ${res.status}`);
  return data;
}
const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body) });
const put = (p, body) => api(p, { method: 'PUT', body: JSON.stringify(body) });
const patch = (p, body) => api(p, { method: 'PATCH', body: JSON.stringify(body) });
const del = (p) => api(p, { method: 'DELETE' });

/* ---------------- UI 小工具 ---------------- */

let toastTimer;
function toast(msg, isErr = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 2600);
}
function loading(on) { $('#loading').hidden = !on; }

function openModal(id) { $(id).hidden = false; }
function closeModal(id) { $(id).hidden = true; }
$$('.modal-mask').forEach((mask) => {
  mask.addEventListener('click', (e) => {
    if (e.target === mask || e.target.closest('[data-close]')) mask.hidden = true;
  });
});

/* ---------------- 视图切换 ---------------- */

function switchView(name) {
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-' + name));
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.view === name));
  $$('.bn').forEach((t) => t.classList.toggle('active', t.dataset.view === name));
  window.scrollTo({ top: 0, behavior: 'smooth' });
  if (name === 'txns') loadTxns();
  if (name === 'analysis') loadAnalysis();
  if (name === 'settings') { loadFunds(); loadBenchList(); }
  if (name === 'export') prepareExport();
}
$$('.tab, .bn').forEach((b) => b.addEventListener('click', () => switchView(b.dataset.view)));

/* ============================================================
   总览
   ============================================================ */

async function loadOverview() {
  const { from, to } = shiftRange(state.chartRange);
  const seq = ++seqOverview;
  try {
    const data = await api(`/api/overview?from=${state.chartRange === 'all' ? '2000-01-01' : from}&to=${to}`);
    if (seq !== seqOverview) return;
    state.overview = data;
    renderStats(data.summary);
    renderPositions(data.positions || []);
    renderChart((data.series && data.series.points) || []);
    loadReturns();
    loadBenchmark();
    loadAttribution();
  } catch (e) {
    toast(e.message, true);
  }
}

function renderStats(s) {
  const set = (id, val, sub, clsName) => {
    const el = $(id);
    el.textContent = val;
    el.className = 'stat-value ' + (clsName || '');
    if (sub !== undefined) $(id + '-sub').innerHTML = sub;
  };
  set('#s-value', money(s.value), `${s.holdings} 只基金 · 成本 ${money(s.cost)}`);
  set('#s-pnl', money(s.total_pnl, true),
    `浮动 ${money(s.unrealized, true)} · 已实现 ${money(s.realized, true)}`, cls(s.total_pnl));
  set('#s-today', money(s.today_pnl, true), pct(s.today_pct), cls(s.today_pnl));
  set('#s-realized', money(s.realized, true), `累计手续费 ${money(s.total_fee)}`, cls(s.realized));
}

/* ---------------- 收益质量：XIRR 年化 ---------------- */

async function loadReturns() {
  try {
    const data = await api('/api/returns');
    renderReturns(data);
  } catch (e) {
    const box = $('#returns');
    if (box) box.innerHTML = `<div class="empty">收益率计算失败：${esc(e.message)}</div>`;
  }
}

function renderReturns(data) {
  const box = $('#returns');
  if (!box) return;
  const rows = (data.rows || []).filter((r) => r.invested > 0.01);
  const p = data.portfolio || {};
  if (!rows.length) {
    box.innerHTML = '<div class="empty">还没有足够数据计算收益率</div>';
    $('#returns-hint').textContent = '';
    return;
  }

  $('#returns-hint').textContent = p.asof ? `估值日 ${p.asof}` : '';

  const cell = (v) => (v === null || v === undefined ? '<span class="flat">—</span>' : `<span class="${cls(v)}">${pct(v)}</span>`);

  const trs = rows.map((r) => `<tr>
      <td class="name full" data-label="基金">${esc(r.name)}<span class="code">${r.code}</span></td>
      <td class="num" data-label="投入">${money(r.invested)}</td>
      <td class="num" data-label="盈亏"><span class="${cls(r.pnl)}">${money(r.pnl, true)}</span></td>
      <td class="num" data-label="累计收益率">${cell(r.cumulative)}</td>
      <td class="num" data-label="年化 XIRR">${r.annualized === null ? '<span class="flat" title="' + esc(r.reason || '') + '">—</span>' : cell(r.annualized)}</td>
      <td class="num hide-m" data-label="持有天数">${r.days}</td>
    </tr>`).join('');

  const pAnn = p.annualized === null || p.annualized === undefined
    ? `<span class="flat">—</span>`
    : `<span class="${cls(p.annualized)}">${pct(p.annualized)}</span>`;
  const pCum = p.cumulative === null || p.cumulative === undefined
    ? `<span class="flat">—</span>`
    : `<span class="${cls(p.cumulative)}">${pct(p.cumulative)}</span>`;

  box.innerHTML = `
    <div class="table-wrap">
      <table class="responsive">
        <thead><tr>
          <th>基金</th><th class="num">投入</th><th class="num">盈亏</th>
          <th class="num">累计收益率</th><th class="num">年化 XIRR</th><th class="num">持有天数</th>
        </tr></thead>
        <tbody>${trs}</tbody>
        <tfoot>
          <tr style="border-top:2px solid var(--line)">
            <td class="name full" data-label="组合合计"><b>组合合计</b></td>
            <td class="num" data-label="投入"><b>${money(p.invested)}</b></td>
            <td class="num" data-label="盈亏"><b><span class="${cls(p.pnl)}">${money(p.pnl, true)}</span></b></td>
            <td class="num" data-label="累计收益率"><b>${pCum}</b></td>
            <td class="num" data-label="年化 XIRR"><b>${pAnn}</b></td>
            <td class="num hide-m" data-label="持有天数"><b>${p.days || 0}</b></td>
          </tr>
        </tfoot>
      </table>
    </div>
    <div class="hint-box" style="margin-top:12px">
      累计收益率 = 盈亏 ÷ 投入，不反映时间；年化 XIRR 按每笔现金流的实际发生日折现，是定投场景下更诚实的口径。
      持有不足 ${90} 天的基金不给年化——样本太短，折算成年化会得到 −90% 之类的失真数字。
    </div>`;
}

/* ---------------- 收益归因 ---------------- */

async function loadAttribution() {
  try {
    const data = await api('/api/attribution');
    renderAttribution(data);
  } catch (e) {
    const box = $('#attribution');
    if (box) box.innerHTML = `<div class="empty">归因计算失败：${esc(e.message)}</div>`;
  }
}

function renderAttribution(data) {
  const box = $('#attribution');
  if (!box) return;
  const rows = (data.rows || []).filter((r) => r.invested > 0.01);
  if (!rows.length) {
    box.innerHTML = '<div class="empty">还没有足够数据做归因</div>';
    $('#attr-hint').textContent = '';
    return;
  }
  const s = data.summary || {};
  $('#attr-hint').textContent = `${s.gainers || 0} 只盈利 · ${s.losers || 0} 只亏损`;

  const maxAbs = Math.max(...rows.map((r) => Math.abs(r.pnl)), 1);

  const bars = rows.map((r) => {
    const w = (Math.abs(r.pnl) / maxAbs) * 100;
    const positive = r.pnl >= 0;
    return `<div class="alloc-row" style="margin-bottom:11px">
      <div class="alloc-head">
        <span class="alloc-name">${esc(r.name)}<span class="code" style="font-size:11px;color:var(--ink-3);margin-left:6px">${r.code}</span></span>
        <span class="alloc-nums">
          <span class="${cls(r.pnl)}">${money(r.pnl, true)}</span>
          ${r.contribution_pp !== null ? ` <span class="flat">${r.contribution_pp > 0 ? '+' : ''}${r.contribution_pp.toFixed(2)}pp</span>` : ''}
        </span>
      </div>
      <div class="alloc-track" style="height:9px">
        <div class="alloc-fill" style="width:${Math.max(w, 0.8)}%;background:${positive ? 'var(--up)' : 'var(--down)'};opacity:.8"></div>
      </div>
      <div class="alloc-foot">
        <span>投入 ${money(r.invested)} ｜ 收益率 <span class="${cls(r.closed ? (r.realized / r.invested * 100) : r.unrealized_pct)}">${r.closed ? pct(r.realized / r.invested * 100) : pct(r.unrealized_pct)}</span></span>
        <span>${r.share_of_loss !== null ? `占全部亏损 ${r.share_of_loss.toFixed(1)}%` : ''}${r.closed ? ' 已清仓' : ''}</span>
      </div>
    </div>`;
  }).join('');

  const cats = (data.by_category || []).map((c) =>
    `<div class="reb-item">
      <span>${esc(c.category_name)} <span style="color:var(--ink-3);font-size:11.5px">${c.count} 只</span></span>
      <span class="amt"><span class="${cls(c.pnl)}">${money(c.pnl, true)}</span> <span class="flat" style="font-size:11.5px">${pct(c.return_pct)}</span></span>
    </div>`).join('');

  box.innerHTML = `
    ${bars}
    <div style="margin-top:18px;padding-top:16px;border-top:1px solid var(--line)">
      <div style="font-size:12.5px;font-weight:650;margin-bottom:9px">按资产类别</div>
      ${cats}
    </div>
    <div class="hint-box" style="margin-top:14px">
      贡献（pp）＝ 该基金盈亏 ÷ 组合总投入，各基金相加恰好等于组合总收益率 ${pct(s.return_pct)}。
      「占全部亏损」看的是单只基金在亏损总额中的权重——小仓位高跌幅与大仓位小跌幅，改进方向不同。
    </div>`;
}

/* ---------------- 对账校准 ---------------- */

async function openReconcile() {
  const holdings = (state.overview && state.overview.positions || []).filter((p) => !p.closed);
  if (!holdings.length) {
    toast('当前没有持仓可对账', true);
    return;
  }
  $('#reconcile-form').innerHTML = holdings.map((p) => `
    <div class="rec-row">
      <div class="rec-name">${esc(p.name)}<span class="code">${p.code}</span></div>
      <div class="rec-mine">账面：${num(p.shares)} 份 ｜ 成本 ${num(p.avg_cost, 4)}</div>
      <div class="rec-inputs">
        <input type="number" step="0.0001" placeholder="平台份额" data-rec="${p.code}" data-field="shares">
        <input type="number" step="0.0001" placeholder="平台成本价" data-rec="${p.code}" data-field="avg_cost">
      </div>
    </div>`).join('');
  $('#reconcile-result').innerHTML = '';
  openModal('#modal-reconcile');
}

async function doReconcile() {
  const map = {};
  $$('#reconcile-form input').forEach((el) => {
    const v = el.value.trim();
    if (v === '') return;
    const code = el.dataset.rec;
    if (!map[code]) map[code] = { code };
    map[code][el.dataset.field] = v;
  });
  const items = Object.values(map);
  if (!items.length) {
    toast('至少填一项才能比对', true);
    return;
  }
  try {
    loading(true);
    const data = await post('/api/reconcile', { items });
    closeModal('#modal-reconcile');
    renderReconcile(data);
    toast(`比对 ${data.summary.checked} 只：${data.summary.matched} 只一致，${data.summary.mismatched} 只有差异`,
      data.summary.mismatched > 0);
  } catch (e) {
    toast(e.message, true);
  } finally {
    loading(false);
  }
}

function renderReconcile(data) {
  const box = $('#reconcile-result');
  if (!box) return;
  const rows = [];
  for (const it of data.items) {
    if (it.error) {
      rows.push(`<div class="rec-item bad"><b>${esc(it.code)}</b> ${esc(it.error)}</div>`);
      continue;
    }
    const head = `<div class="rec-item ${it.ok ? 'good' : 'bad'}">
      <div class="rec-head">
        <b>${esc(it.name)}</b>
        <span class="pill ${it.ok ? 'act-open' : 'act-close'}">${it.ok ? '一致' : '有差异'}</span>
      </div>`;
    const detail = it.items.map((x) => `
      <div class="rec-diff">
        <span>${x.field}：账面 ${num(x.mine, 4)} ／ 平台 ${num(x.platform, 4)}</span>
        <span class="${x.within ? 'flat' : cls(-Math.abs(x.diff))}">
          差 ${x.diff > 0 ? '+' : ''}${num(x.diff, 4)}${x.diff_pct !== null ? `（${x.diff_pct > 0 ? '+' : ''}${x.diff_pct}%）` : ''}
        </span>
        ${x.within ? '' : `<div class="rec-hint">${esc(x.hint)}</div>`}
      </div>`).join('');
    rows.push(head + detail + '</div>');
  }
  box.innerHTML = rows.join('') + `<div class="hint-box" style="margin-top:10px">
    差异只是提示，不会自动改动记录。确认是录入错误的话，到「操作流水」里改对应那笔。
  </div>`;
}

/* ---------------- 基准对比 ---------------- */

async function loadBenchmark() {
  const box = $('#benchmark');
  if (!box) return;
  try {
    if (!state.benchmarks) {
      const meta = await api('/api/benchmarks');
      state.benchmarks = (meta.items || []).filter((b) => b.n > 0);
      state.benchPresets = meta.presets || [];
      renderBenchGroup();
    }
    const secid = state.benchSecid || (state.benchmarks[0] && state.benchmarks[0].secid);
    if (!secid) {
      box.innerHTML = '<div class="empty">还没有基准数据。到设置页点「更新基准」</div>';
      return;
    }
    state.benchSecid = secid;
    const data = await api('/api/benchmark?secid=' + encodeURIComponent(secid));
    renderBenchmark(data);
  } catch (e) {
    box.innerHTML = `<div class="empty">基准对比失败：${esc(e.message)}</div>`;
  }
}

function renderBenchGroup() {
  const g = $('#bench-group');
  if (!g || !state.benchmarks) return;
  g.innerHTML = state.benchmarks
    .map((b) => `<button data-secid="${b.secid}" class="${b.secid === (state.benchSecid || state.benchmarks[0].secid) ? 'active' : ''}">${esc(b.name)}</button>`)
    .join('');
  $$('#bench-group button').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.benchSecid = btn.dataset.secid;
      $$('#bench-group button').forEach((x) => x.classList.toggle('active', x === btn));
      loadBenchmark();
    });
  });
}

function renderBenchmark(data) {
  const box = $('#benchmark');
  if (!box) return;
  if (data.error) {
    box.innerHTML = `<div class="empty">${esc(data.error)}</div>`;
    return;
  }
  const m = data.mine;
  const b = data.benchmark_metrics;
  const diff = data.diff_cumulative;
  const verdictCls = diff > 2 ? 'up' : diff < -2 ? 'down' : 'flat';
  const row = (label, x, highlight) => `<tr${highlight ? ' style="border-top:2px solid var(--line)"' : ''}>
      <td class="name full" data-label="口径">${highlight ? '<b>' + label + '</b>' : label}</td>
      <td class="num" data-label="期末市值">${money(x.terminal)}</td>
      <td class="num" data-label="盈亏"><span class="${cls(x.pnl)}">${money(x.pnl, true)}</span></td>
      <td class="num" data-label="累计收益率"><span class="${cls(x.cumulative)}">${pct(x.cumulative)}</span></td>
      <td class="num" data-label="年化">${x.annualized === null ? '<span class="flat">—</span>' : `<span class="${cls(x.annualized)}">${pct(x.annualized)}</span>`}</td>
    </tr>`;

  box.innerHTML = `
    <div class="bench-verdict">
      <span class="bv-label">同样 ${money(m.invested)}、同样的时点，只换标的</span>
      <span class="bv-diff ${verdictCls}">${diff > 0 ? '+' : ''}${diff}pp</span>
      <span class="pill ${diff > 2 ? 'act-open' : diff < -2 ? 'act-close' : ''}">${esc(data.verdict || '—')}</span>
    </div>
    <div class="table-wrap">
      <table class="responsive">
        <thead><tr>
          <th>口径</th><th class="num">期末市值</th><th class="num">盈亏</th>
          <th class="num">累计收益率</th><th class="num">年化</th>
        </tr></thead>
        <tbody>
          ${row('我的实际组合', m)}
          ${row(`全买${esc(data.benchmark.name)}`, b, true)}
        </tbody>
      </table>
    </div>
    <div class="hint-box" style="margin-top:12px">
      口径说明：把你每一笔买入的金额与日期原样搬到「${esc(data.benchmark.name)}」上，卖出也同日等额模拟。
      这样差别只剩「买什么」这一项，排除了择时差异。
      参考：${esc(data.benchmark.name)}区间本身涨跌 ${pct(data.index_return)}，估值日 ${data.asof}。
    </div>`;
}

/* ---------------- 基准管理 ---------------- */

async function loadBenchList() {
  const box = $('#bench-list');
  if (!box) return;
  try {
    const data = await api('/api/benchmarks');
    state.benchmarks = (data.items || []).filter((b) => b.n > 0);
    state.benchPresets = data.presets || [];
    const items = data.items || [];
    if (!items.length) {
      box.innerHTML = '<div class="empty">还没有基准数据，点右上角「更新全部基准」</div>';
      return;
    }
    box.innerHTML = items.map((b) => `
      <div class="fund-row">
        <div class="fmain">
          <div class="fname">${esc(b.name)}</div>
          <div class="fcode">${b.secid} · ${b.n} 条 · 更新于 ${(b.updated_at || '').slice(0, 16)}</div>
        </div>
        <button class="icon-btn" data-bsync="${b.secid}" title="更新" style="font-size:13px;padding:3px 6px">↻</button>
      </div>`).join('');
    box.querySelectorAll('[data-bsync]').forEach((btn) => {
      btn.addEventListener('click', () => syncBench(btn.dataset.bsync));
    });
  } catch (e) {
    box.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

async function syncBench(secid) {
  const preset = (state.benchPresets || []).find((p) => p.secid === secid);
  try {
    loading(true);
    const r = await post('/api/benchmarks/sync', { secid, name: preset ? preset.name : null });
    toast(`${r.name} 已更新 ${r.rows} 条`);
    state.benchmarks = null;
    await loadBenchList();
    loadBenchmark();
  } catch (e) {
    toast(e.message, true);
  } finally {
    loading(false);
  }
}

function renderPositions(positions) {
  const holding = positions.filter((p) => !p.closed);
  $('#pos-count').textContent = holding.length ? `共 ${holding.length} 只` : '';
  const box = $('#positions');
  if (!holding.length) {
    box.innerHTML = `<div class="empty"><b>还没有持仓</b>先在「设置」里添加基金，然后点右上角「记一笔」记录建仓</div>`;
    return;
  }

  const rows = holding.map((p) => `
    <tr>
      <td class="name full" data-label="基金">
        ${esc(p.name)}<span class="code">${p.code}</span>
      </td>
      <td data-label="类别"><span class="pill cat-${p.category}">${esc(p.category_name)}</span></td>
      <td class="num" data-label="份额">${num(p.shares)}</td>
      <td class="num hide-m" data-label="成本单价">${num(p.avg_cost, 4)}</td>
      <td class="num" data-label="净值">${num(p.nav, 4)}</td>
      <td class="num ${cls(p.pct)}" data-label="日涨跌">${pct(p.pct)}</td>
      <td class="num" data-label="市值">${money(p.value)}</td>
      <td class="num ${cls(p.unrealized)}" data-label="浮动盈亏">${money(p.unrealized, true)}</td>
      <td class="num ${cls(p.unrealized_pct)}" data-label="收益率">${pct(p.unrealized_pct)}</td>
      <td class="num ${cls(p.today_pnl)} hide-m" data-label="当日盈亏">${money(p.today_pnl, true)}</td>
      <td class="num hide-m" data-label="已实现">${money(p.realized, true)}</td>
    </tr>`).join('');

  box.innerHTML = `
    <div class="table-wrap">
      <table class="responsive">
        <thead><tr>
          <th>基金</th><th>类别</th><th class="num">份额</th><th class="num">成本单价</th>
          <th class="num">净值</th><th class="num">日涨跌</th><th class="num">市值</th>
          <th class="num">浮动盈亏</th><th class="num">收益率</th><th class="num">当日盈亏</th><th class="num">已实现</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

/* ---------------- SVG 走势图 ---------------- */

function renderChart(points) {
  const box = $('#chart');
  const legend = $('#chart-legend');
  if (!points.length) {
    box.innerHTML = '<div class="empty" style="padding:60px 0">暂无净值数据，点右上角「更新净值」</div>';
    legend.innerHTML = '';
    return;
  }

  const W = 760, H = 250, P = { t: 14, r: 12, b: 24, l: 58 };
  const iw = W - P.l - P.r, ih = H - P.t - P.b;

  const vals = points.flatMap((p) => [p.value, p.cost]);
  let min = Math.min(...vals), max = Math.max(...vals);
  if (max - min < 1) { max += 1; min = Math.max(0, min - 1); }
  const pad = (max - min) * 0.12;
  min = Math.max(0, min - pad);
  max = max + pad;

  const x = (i) => P.l + (points.length === 1 ? iw / 2 : (i / (points.length - 1)) * iw);
  const y = (v) => P.t + ih - ((v - min) / (max - min)) * ih;

  const linePath = (key) => points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join('');
  const areaPath = `${linePath('value')}L${x(points.length - 1).toFixed(1)},${(P.t + ih).toFixed(1)}L${x(0).toFixed(1)},${(P.t + ih).toFixed(1)}Z`;

  const ticks = 4;
  let grid = '';
  for (let i = 0; i <= ticks; i++) {
    const v = min + ((max - min) * i) / ticks;
    const yy = y(v);
    grid += `<line x1="${P.l}" y1="${yy.toFixed(1)}" x2="${W - P.r}" y2="${yy.toFixed(1)}" stroke="var(--line)" stroke-width="1" opacity=".7"/>`;
    const label = Math.abs(v) >= 10000 ? (v / 10000).toFixed(1) + '万' : v.toFixed(0);
    grid += `<text x="${P.l - 8}" y="${(yy + 3.5).toFixed(1)}" text-anchor="end" font-size="10" fill="var(--ink-3)" font-family="var(--mono)">${label}</text>`;
  }

  const xCount = Math.min(5, points.length);
  let xLabels = '';
  for (let i = 0; i < xCount; i++) {
    const idx = Math.round((i / Math.max(1, xCount - 1)) * (points.length - 1));
    const d = points[idx].date.slice(5);
    const anchor = i === 0 ? 'start' : i === xCount - 1 ? 'end' : 'middle';
    xLabels += `<text x="${x(idx).toFixed(1)}" y="${H - 6}" text-anchor="${anchor}" font-size="10" fill="var(--ink-3)" font-family="var(--mono)">${d}</text>`;
  }

  const last = points[points.length - 1];
  const lastColor = last.value >= last.cost ? 'var(--up)' : 'var(--down)';

  box.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" id="chart-svg">
      <defs>
        <linearGradient id="gArea" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${lastColor}" stop-opacity=".16"/>
          <stop offset="100%" stop-color="${lastColor}" stop-opacity="0"/>
        </linearGradient>
      </defs>
      ${grid}
      <path d="${areaPath}" fill="url(#gArea)"/>
      <path d="${linePath('cost')}" fill="none" stroke="var(--ink-3)" stroke-width="1.4" stroke-dasharray="4 3" opacity=".8" vector-effect="non-scaling-stroke"/>
      <path d="${linePath('value')}" fill="none" stroke="${lastColor}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
      <circle cx="${x(points.length - 1).toFixed(1)}" cy="${y(last.value).toFixed(1)}" r="3.5" fill="${lastColor}"/>
      ${xLabels}
      <line id="crosshair" x1="0" y1="${P.t}" x2="0" y2="${P.t + ih}" stroke="var(--ink-3)" stroke-width="1" stroke-dasharray="3 3" opacity="0" vector-effect="non-scaling-stroke"/>
    </svg>
    <div class="chart-tip" id="chart-tip" style="position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:7px 10px;font-size:11.5px;box-shadow:var(--shadow);opacity:0;transition:opacity .12s;white-space:nowrap;z-index:5"></div>`;

  legend.innerHTML = `
    <span><i style="background:${lastColor}"></i>持仓市值</span>
    <span><i style="background:var(--ink-3)"></i>持仓成本</span>
    <span style="margin-left:auto">区间 ${points[0].date} → ${last.date}</span>`;

  // 悬停查看
  const svg = $('#chart-svg');
  const tip = $('#chart-tip');
  const cross = $('#crosshair');
  svg.addEventListener('mousemove', (ev) => {
    const rect = svg.getBoundingClientRect();
    const rel = ((ev.clientX - rect.left) / rect.width) * W;
    const ratio = Math.max(0, Math.min(1, (rel - P.l) / iw));
    const idx = Math.round(ratio * (points.length - 1));
    const p = points[idx];
    if (!p) return;
    cross.setAttribute('x1', x(idx));
    cross.setAttribute('x2', x(idx));
    cross.setAttribute('opacity', '.6');
    tip.style.opacity = '1';
    const leftPct = (x(idx) / W) * 100;
    tip.style.left = Math.min(Math.max(leftPct, 8), 78) + '%';
    tip.style.top = '8px';
    tip.innerHTML = `<b>${p.date}</b><br>市值 ${money(p.value)}<br>成本 ${money(p.cost)}<br>
      <span style="color:${p.pnl >= 0 ? 'var(--up)' : 'var(--down)'}">当日 ${money(p.pnl, true)}</span>`;
  });
  svg.addEventListener('mouseleave', () => {
    cross.setAttribute('opacity', '0');
    tip.style.opacity = '0';
  });
}

$$('#chart-range button').forEach((b) => {
  b.addEventListener('click', () => {
    $$('#chart-range button').forEach((x) => x.classList.remove('active'));
    b.classList.add('active');
    state.chartRange = b.dataset.range;
    loadOverview();
  });
});

/* ============================================================
   操作流水
   ============================================================ */

async function loadTxns() {
  const f = state.txnFilter;
  const qs = new URLSearchParams();
  if (f.from) qs.set('from', f.from);
  if (f.to) qs.set('to', f.to);
  if (f.code) qs.set('code', f.code);
  const seq = ++seqTxns;
  try {
    const data = await api('/api/txns?' + qs.toString());
    if (seq !== seqTxns) return;
    state.txns = data.items || [];
    renderTxns();
  } catch (e) { toast(e.message, true); }
}

function renderTxns() {
  const box = $('#txns');
  const list = state.txns;
  const sum = $('#txn-summary');

  if (!list.length) {
    box.innerHTML = `<div class="empty"><b>该区间没有操作记录</b>点「记一笔」开始记录，或调整上面的日期筛选</div>`;
    sum.textContent = '';
    return;
  }

  const buys = list.filter((t) => ['open', 'add', 'sip'].includes(t.action));
  const sells = list.filter((t) => ['reduce', 'close'].includes(t.action));
  const buyAmt = buys.reduce((a, t) => a + t.amount, 0);
  const sellAmt = sells.reduce((a, t) => a + t.amount - t.fee, 0);
  sum.innerHTML = `共 ${list.length} 笔 ｜ 买入 ${buys.length} 笔 <span class="cur">${money(buyAmt)}</span> ｜ 卖出 ${sells.length} 笔 <span class="cur">${money(sellAmt)}</span>`;

  const actionName = state.meta.actions;
  const rows = list.map((t) => `
    <tr>
      <td data-label="日期">${t.date}</td>
      <td class="name full" data-label="基金">${esc(t.fund_name)}<span class="code">${t.code}</span></td>
      <td data-label="操作"><span class="pill act-${t.action}">${actionName[t.action] || t.action}</span></td>
      <td class="num" data-label="金额">${money(t.amount)}</td>
      <td class="num hide-m" data-label="份额">${num(t.shares)}</td>
      <td class="num hide-m" data-label="成交净值">${num(t.nav, 4)}</td>
      <td class="num ${cls(t.day_pct)}" data-label="当日涨跌">${pct(t.day_pct)}</td>
      <td class="num hide-m" data-label="手续费">${money(t.fee)}</td>
      <td data-label="备注" class="muted" style="font-size:12px">${esc(t.note || '')}</td>
      <td style="text-align:right;white-space:nowrap">
        <button class="icon-btn" style="font-size:13px;padding:3px 6px" data-edit="${t.id}" title="编辑">改</button>
        <button class="icon-btn" style="font-size:13px;padding:3px 6px" data-del="${t.id}" title="删除">删</button>
      </td>
    </tr>`).join('');

  box.innerHTML = `
    <div class="table-wrap">
      <table class="responsive">
        <thead><tr>
          <th>日期</th><th>基金</th><th>操作</th><th class="num">金额</th><th class="num">份额</th>
          <th class="num">成交净值</th><th class="num">当日涨跌</th><th class="num">手续费</th><th>备注</th><th></th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;

  box.querySelectorAll('[data-edit]').forEach((b) => {
    b.addEventListener('click', () => {
      const t = state.txns.find((x) => String(x.id) === b.dataset.edit);
      if (t) editTxn(t);
    });
  });
  box.querySelectorAll('[data-del]').forEach((b) => {
    b.addEventListener('click', async () => {
      const t = state.txns.find((x) => String(x.id) === b.dataset.del);
      if (!confirm(`删除这条记录？\n\n${t.date} ${t.fund_name} ${actionName[t.action]} ${money(t.amount)}`)) return;
      try {
        await del('/api/txns/' + b.dataset.del);
        toast('已删除');
        loadTxns();
        loadOverview();
      } catch (e) { toast(e.message, true); }
    });
  });
}

/* ---------------- 录入弹窗 ---------------- */

function fillFundSelect(sel, selected) {
  sel.innerHTML = state.funds.map((f) =>
    `<option value="${f.code}" ${String(selected) === f.code ? 'selected' : ''}>${esc(f.name)} (${f.code})</option>`
  ).join('');
}

function openTxnModal(txn) {
  if (!state.funds.length) {
    toast('请先在「设置」里添加基金', true);
    switchView('settings');
    return;
  }
  $('#txn-modal-title').textContent = txn ? '编辑记录' : '记一笔';
  $('#f-id').value = txn ? txn.id : '';
  fillFundSelect($('#f-code'), txn ? txn.code : state.funds[0].code);
  $('#f-date').value = txn ? txn.date : (state.meta.today || isoDate(new Date()));
  $('#f-action').value = txn ? txn.action : 'add';
  $('#f-amount').value = txn ? txn.amount : '';
  $('#f-shares').value = txn ? txn.shares : '';
  $('#f-nav').value = txn && txn.nav ? txn.nav : '';
  $('#f-fee').value = txn ? txn.fee : '';
  $('#f-note').value = txn ? (txn.note || '') : '';
  updateTxnHint();
  openModal('#modal-txn');
}

function editTxn(t) {
  openTxnModal(t);
}

function updateTxnHint() {
  const action = $('#f-action').value;
  const code = $('#f-code').value;
  const fund = state.funds.find((f) => f.code === code);
  const box = $('#f-hint');
  const tips = {
    open: '首次买入。填金额即可，份额和手续费会按当日净值自动估算。',
    add: '已有持仓追加。填金额即可自动算份额。',
    sip: '定投扣款，记录方式同加仓。想批量生成，用流水页的「批量定投」。',
    reduce: '部分卖出。填份额，金额按当日净值自动算。',
    close: '全部卖出。份额留空会自动取当前持仓，金额自动算。',
    dividend: '现金分红填金额；红利再投填份额（成本不变，单价被摊薄）。',
  };
  let extra = '';
  if (fund && fund.nav) {
    extra = ` 当前净值 ${num(fund.nav, 4)}（${fund.nav_date}），持仓 ${num(fund.shares)} 份。`;
  }
  box.textContent = (tips[action] || '') + extra;
}

/* ============================================================
   配置分析
   ============================================================ */

async function loadAnalysis() {
  try {
    const [tplData] = await Promise.all([api('/api/templates')]);
    state.templates = tplData.items || [];
    const sel = $('#tpl-select');
    const prev = state.curTpl ? String(state.curTpl) : sel.value;
    sel.innerHTML = state.templates.map((t) =>
      `<option value="${t.id}">${esc(t.name)}</option>`).join('') || '<option value="">无模板</option>';
    if (prev && state.templates.some((t) => String(t.id) === String(prev))) sel.value = prev;
    state.curTpl = sel.value;
    await refreshAlloc();
  } catch (e) { toast(e.message, true); }
}

async function refreshAlloc() {
  const tid = $('#tpl-select').value;
  const extra = parseFloat($('#extra-cash').value) || 0;
  const qs = new URLSearchParams();
  if (tid) qs.set('template_id', tid);
  if (extra > 0) qs.set('extra_cash', extra);
  const seq = ++seqAlloc;
  try {
    const data = await api('/api/analysis?' + qs.toString());
    if (seq !== seqAlloc) return;
    state.alloc = data;
    renderAlloc(data);
  } catch (e) { toast(e.message, true); }
}

function renderAlloc(data) {
  const tpl = data.template;
  $('#tpl-desc').textContent = tpl ? tpl.description : '';
  $('#alloc-hint').textContent = tpl
    ? `阈值 ±${tpl.threshold}pp ｜ 总市值 ${money(data.total_value)}`
    : `总市值 ${money(data.total_value)}`;

  const bars = $('#alloc-bars');
  if (!data.rows.length) {
    bars.innerHTML = '<div class="empty">还没有持仓数据</div>';
    $('#rebalance').innerHTML = '';
    return;
  }
  if ((data.total_value || 0) < 0.01) {
    bars.innerHTML = '<div class="empty"><b>还没有持仓</b>先在「设置」里添加基金，再记录一笔建仓，这里才会算出配置偏离</div>';
    $('#rebalance').innerHTML = '';
    return;
  }

  const colors = {};
  (state.meta.categories || []).forEach((c) => { colors[c.key] = c.color; });

  bars.innerHTML = data.rows.map((r) => {
    const target = r.target_pct === null ? r.actual_pct : r.target_pct;
    const scale = Math.max(100, target * 1.2, r.actual_pct * 1.1);
    const w = (r.actual_pct / scale) * 100;
    const tp = (target / scale) * 100;
    const diffCls = r.diff === null ? 'flat' : (Math.abs(r.diff) <= (data.threshold ?? 5) ? 'flat' : (r.diff > 0 ? 'up' : 'down'));
    return `
      <div class="alloc-row">
        <div class="alloc-head">
          <span class="alloc-name">${esc(r.category_name)}</span>
          <span class="alloc-nums">
            实际 ${r.actual_pct}% ／ 目标 ${r.target_pct === null ? '—' : r.target_pct + '%'}
            ${r.diff !== null ? ` <span class="${diffCls}">(${r.diff > 0 ? '+' : ''}${r.diff}pp)</span>` : ''}
          </span>
        </div>
        <div class="alloc-track">
          <div class="alloc-fill" style="width:${Math.max(w, 0.5)}%;background:${colors[r.category] || 'var(--ink-3)'};opacity:.85"></div>
          ${r.target_pct !== null ? `<div class="alloc-target" style="left:${Math.min(tp, 99.5)}%" data-label="${r.target_pct}%"></div>` : ''}
        </div>
        <div class="alloc-foot">
          <span>${money(r.actual_value)}</span>
          <span>${r.status}${r.suggest_side && r.suggest_side !== '不动' ? ' · 建议' + r.suggest_side + ' ' + money(r.suggest_amount) : ''}</span>
        </div>
      </div>`;
  }).join('');

  // 再平衡建议
  const reb = $('#rebalance');
  const detail = data.alloc_detail || [];
  const missing = data.alloc_missing || [];
  if (!tpl) {
    reb.innerHTML = '<div class="empty">选择一个投资模板后，这里会给出具体的买卖清单</div>';
    return;
  }
  const need = (data.rows || []).filter((r) => r.suggest_side !== '不动' && r.suggest_amount > 0.01);
  if (!need.length && !detail.length && !missing.length) {
    reb.innerHTML = `<div class="empty"><b>配置在阈值内</b>各类资产偏离都不超过 ±${tpl.threshold}pp，暂时不需要调整</div>`;
    return;
  }

  let html = detail.map((grp) => `
    <div class="reb-group">
      <div class="reb-title">${esc(grp.category_name)}
        <span class="side ${grp.side === '买入' ? 'buy' : 'sell'}">${grp.side}</span>
      </div>
      ${grp.items.filter((i) => i.amount > 0.01).map((i) => `
        <div class="reb-item">
          <span>${esc(i.name)} <span style="color:var(--ink-3);font-size:11.5px">${i.code}</span></span>
          <span class="amt">${money(i.amount)}</span>
        </div>`).join('')}
    </div>`).join('');

  if (missing.length) {
    html += `<div class="reb-group">
      <div class="reb-title">待补类别</div>
      ${missing.map((m) => `
        <div class="reb-item">
          <span>${esc(m.category_name)} <span style="color:var(--ink-3);font-size:11.5px">该类别下还没有基金</span></span>
          <span class="amt">${money(m.amount)}</span>
        </div>`).join('')}
      <div class="hint-box" style="margin-top:8px">
        这些类别需要买入，但基金库里没有对应基金。到「设置 → 添加基金」补上，并把它的类别改成对应的资产类别。
      </div>
    </div>`;
  }

  if (data.unallocated > 0.01) {
    html += `<div class="hint-box">新增资金还有 ${money(data.unallocated)} 没分配出去，因为各类资产都已达到或超过目标比例。</div>`;
  }

  reb.innerHTML = html || '<div class="empty">无需操作</div>';
}

/* ---------------- 模板管理 ---------------- */

function openTplModal(tpl) {
  $('#tpl-modal-title').textContent = tpl ? '编辑模板' : '新建模板';
  $('#t-id').value = tpl ? tpl.id : '';
  $('#t-name').value = tpl ? tpl.name : '';
  $('#t-desc').value = tpl ? tpl.description : '';
  $('#t-threshold').value = tpl ? tpl.threshold : 5;
  $('#btn-del-tpl').hidden = !tpl;

  const targets = {};
  (tpl ? tpl.items : []).forEach((i) => { targets[i.category] = i.target; });
  $('#t-items').innerHTML = state.meta.categories.map((c) => `
    <div class="tpl-item">
      <span class="tn">${esc(c.name)}</span>
      <input type="number" data-cat="${c.key}" min="0" max="100" step="0.5"
             value="${targets[c.key] ?? ''}" placeholder="0">
      <span style="font-size:12px;color:var(--ink-3)">%</span>
    </div>`).join('');
  $$('#t-items input').forEach((i) => i.addEventListener('input', updateTplTotal));
  updateTplTotal();
  openModal('#modal-tpl');
}

function updateTplTotal() {
  const total = $$('#t-items input').reduce((a, i) => a + (parseFloat(i.value) || 0), 0);
  const el = $('#t-total');
  el.textContent = `合计 ${total.toFixed(1)}%${Math.abs(total - 100) < 0.5 ? ' ✓' : '（应为 100%）'}`;
  el.className = 'tpl-total ' + (Math.abs(total - 100) < 0.5 ? 'ok' : 'bad');
}

/* ============================================================
   导出
   ============================================================ */

function prepareExport() {
  if (!$('#exp-from').value) {
    const { from, to } = shiftRange('all');
    $('#exp-from').value = from;
    $('#exp-to').value = to;
  }
}

function exportParams() {
  const p = new URLSearchParams();
  p.set('from', $('#exp-from').value || '2000-01-01');
  p.set('to', $('#exp-to').value || isoDate(new Date()));
  p.set('format', ($('input[name="format"]:checked') || {}).value || 'md');
  p.set('level', ($('input[name="level"]:checked') || {}).value || 'standard');
  if ($('#exp-with-tpl').checked && state.curTpl) p.set('template_id', state.curTpl);
  const extra = parseFloat($('#extra-cash').value) || 0;
  if (extra > 0) p.set('extra_cash', extra);
  return p;
}

async function doExport() {
  try {
    loading(true);
    const res = await fetch('/api/export?' + exportParams().toString());
    if (!res.ok) {
      const t = await res.text();
      throw new Error(t || '导出失败');
    }
    const blob = await res.blob();
    const cd = res.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename\*=UTF-8''(.+)$/);
    const name = m ? decodeURIComponent(m[1]) : 'fund-ledger.txt';
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    toast('已下载 ' + name);
  } catch (e) {
    toast(e.message, true);
  } finally {
    loading(false);
  }
}

async function doPreview() {
  try {
    loading(true);
    const res = await fetch('/api/export?' + exportParams().toString());
    const text = await res.text();
    if (!res.ok) throw new Error(text);
    $('#preview-panel').hidden = false;
    $('#preview-body').textContent = text;
    $('#preview-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    toast(e.message, true);
  } finally {
    loading(false);
  }
}

/* ============================================================
   设置 · 基金库
   ============================================================ */

async function loadFunds() {
  try {
    const data = await api('/api/funds');
    state.funds = data.items || [];
    renderFunds();
    refreshFundSelectors();
  } catch (e) { toast(e.message, true); }
}

function renderFunds() {
  const box = $('#fund-list');
  if (!state.funds.length) {
    box.innerHTML = `<div class="empty"><b>基金库是空的</b>点右上角「添加基金」搜索代码，比如 110022</div>`;
    return;
  }
  const cats = state.meta.categories;
  box.innerHTML = state.funds.map((f) => `
    <div class="fund-row">
      <div class="fmain">
        <div class="fname">${esc(f.name)}</div>
        <div class="fcode">${f.code} · ${f.txn_count} 笔${f.holding ? ` · 持仓 ${money(f.value)}` : ''}</div>
      </div>
      <select data-cat="${f.code}">
        ${cats.map((c) => `<option value="${c.key}" ${c.key === f.category ? 'selected' : ''}>${c.name}</option>`).join('')}
      </select>
      <button class="icon-btn" data-sync="${f.code}" title="更新净值" style="font-size:13px;padding:3px 6px">↻</button>
      <button class="icon-btn" data-rm="${f.code}" title="移除" style="font-size:13px;padding:3px 6px">✕</button>
    </div>`).join('');

  box.querySelectorAll('[data-cat]').forEach((s) => {
    s.addEventListener('change', async () => {
      try {
        await patch('/api/funds/' + s.dataset.cat, { category: s.value });
        toast('已更新分类');
        loadFunds();
        loadOverview();
      } catch (e) { toast(e.message, true); }
    });
  });
  box.querySelectorAll('[data-sync]').forEach((b) => {
    b.addEventListener('click', async () => {
      try {
        loading(true);
        await post(`/api/funds/${b.dataset.sync}/sync`, { full: true });
        toast('净值已更新');
        loadFunds(); loadOverview();
      } catch (e) { toast(e.message, true); } finally { loading(false); }
    });
  });
  box.querySelectorAll('[data-rm]').forEach((b) => {
    b.addEventListener('click', async () => {
      const f = state.funds.find((x) => x.code === b.dataset.rm);
      if (!confirm(`移除 ${f.name}？\n\n该基金的所有操作记录和净值数据会一起删除，不可恢复。`)) return;
      try {
        await del('/api/funds/' + b.dataset.rm);
        toast('已移除');
        loadFunds(); loadOverview();
      } catch (e) { toast(e.message, true); }
    });
  });
}

function refreshFundSelectors() {
  const opts = state.funds.map((f) => `<option value="${f.code}">${esc(f.name)} (${f.code})</option>`).join('');
  const txnFund = $('#txn-fund');
  if (txnFund) txnFund.innerHTML = '<option value="">全部基金</option>' + opts;
  const sip = $('#sip-code');
  if (sip) sip.innerHTML = opts;
}

/* ---------------- 基金搜索 ---------------- */

let searchTimer;
function bindFundSearch() {
  const input = $('#fund-search');
  input.addEventListener('input', () => {
    clearTimeout(searchTimer);
    const kw = input.value.trim();
    if (!kw) { $('#fund-search-result').innerHTML = ''; return; }
    searchTimer = setTimeout(async () => {
      try {
        const data = await api('/api/search?q=' + encodeURIComponent(kw));
        const box = $('#fund-search-result');
        if (!data.items.length) { box.innerHTML = '<div class="hint-box">没有匹配的基金</div>'; return; }
        box.innerHTML = data.items.map((it) => `
          <div class="search-item" data-code="${it.code}" data-cat="${it.is_money ? 'cash' : ''}">
            <div><div class="sname">${esc(it.name)}</div>
              <div class="smeta">${it.code}${it.fund_type ? ' · ' + esc(it.fund_type) : ''}${it.company ? ' · ' + esc(it.company) : ''}</div></div>
            <span style="font-size:12px;color:var(--accent);font-weight:600">添加</span>
          </div>`).join('');
        box.querySelectorAll('.search-item').forEach((el) => {
          el.addEventListener('click', () => addFund(el.dataset.code));
        });
      } catch (e) { toast(e.message, true); }
    }, 320);
  });
}

async function addFund(code, category) {
  try {
    loading(true);
    const res = await post('/api/funds', { code, category });
    toast(`已添加 ${res.name}，导入 ${res.nav_rows} 条净值`);
    closeModal('#modal-fund');
    $('#fund-search').value = '';
    $('#fund-search-result').innerHTML = '';
    $('#fund-code-direct').value = '';
    await loadFunds();
    await loadOverview();
  } catch (e) {
    toast(e.message, true);
  } finally {
    loading(false);
  }
}

/* ============================================================
   事件绑定
   ============================================================ */

function bindEvents() {
  // 顶栏
  $('#btn-new-txn').addEventListener('click', () => openTxnModal(null));
  $('#btn-new-txn2').addEventListener('click', () => openTxnModal(null));
  $('#btn-sync').addEventListener('click', async () => {
    if (!state.funds.length) { toast('基金库是空的，先添加基金', true); return; }
    try {
      loading(true);
      const res = await post('/api/funds/sync-all', {});
      const ok = (res.synced || []).length;
      const bad = (res.failed || []).length;
      toast(bad ? `更新完成：成功 ${ok} 只，失败 ${bad} 只` : `已更新 ${ok} 只基金的净值`);
      if (bad) console.warn('同步失败：', res.failed);
      await loadOverview();
      if ($('#view-settings').classList.contains('active')) loadFunds();
    } catch (e) { toast(e.message, true); } finally { loading(false); }
  });

  // 流水筛选
  const applyFilter = () => {
    state.txnFilter.from = $('#txn-from').value;
    state.txnFilter.to = $('#txn-to').value;
    state.txnFilter.code = $('#txn-fund').value;
    loadTxns();
  };
  ['#txn-from', '#txn-to', '#txn-fund'].forEach((s) => $(s).addEventListener('change', applyFilter));

  // 录入弹窗
  $('#f-action').addEventListener('change', updateTxnHint);
  $('#f-code').addEventListener('change', updateTxnHint);
  $('#btn-save-txn').addEventListener('click', async () => {
    const id = $('#f-id').value;
    const body = {
      code: $('#f-code').value,
      date: $('#f-date').value,
      action: $('#f-action').value,
      amount: parseFloat($('#f-amount').value) || 0,
      shares: parseFloat($('#f-shares').value) || 0,
      nav: $('#f-nav').value === '' ? null : parseFloat($('#f-nav').value),
      fee: parseFloat($('#f-fee').value) || 0,
      note: $('#f-note').value,
    };
    if (!body.date) { toast('请选择日期', true); return; }
    try {
      loading(true);
      if (id) await put('/api/txns/' + id, body);
      else await post('/api/txns', body);
      toast(id ? '已保存' : '已记录');
      closeModal('#modal-txn');
      await loadOverview();
      if ($('#view-txns').classList.contains('active')) loadTxns();
    } catch (e) { toast(e.message, true); } finally { loading(false); }
  });

  // 批量定投
  $('#btn-sip').addEventListener('click', () => {
    if (!state.funds.length) { toast('先添加基金', true); return; }
    refreshFundSelectors();
    $('#sip-start').value = isoDate(new Date(new Date().getFullYear(), 0, 1));
    $('#sip-end').value = state.meta.today;
    openModal('#modal-sip');
  });
  $('#sip-freq').addEventListener('change', () => {
    const isMonth = $('#sip-freq').value === 'month';
    $('#sip-day-wrap').querySelector('label').textContent = isMonth ? '扣款日（几号）' : '扣款日（周几）';
    $('#sip-day').max = isMonth ? 31 : 6;
    $('#sip-day').value = isMonth ? 15 : 1;
  });
  $('#btn-do-sip').addEventListener('click', async () => {
    const body = {
      code: $('#sip-code').value,
      start: $('#sip-start').value,
      end: $('#sip-end').value,
      freq: $('#sip-freq').value,
      day: parseInt($('#sip-day').value) || 0,
      amount: parseFloat($('#sip-amount').value) || 0,
      fee_rate: parseFloat($('#sip-fee').value) || 0,
    };
    try {
      loading(true);
      const res = await post('/api/txns/batch', body);
      toast(`生成 ${res.count} 条定投记录${res.skipped.length ? `，跳过 ${res.skipped.length} 条` : ''}`);
      closeModal('#modal-sip');
      await loadOverview();
      if ($('#view-txns').classList.contains('active')) loadTxns();
    } catch (e) { toast(e.message, true); } finally { loading(false); }
  });

  // 添加基金
  $('#btn-add-fund').addEventListener('click', () => openModal('#modal-fund'));
  $('#btn-reconcile').addEventListener('click', openReconcile);
  $('#btn-sync-bench').addEventListener('click', async () => {
    const presets = state.benchPresets || [];
    if (!presets.length) { toast('没有可用的预设指数', true); return; }
    const okList = [], bad = [];
    loading(true);
    for (const p of presets) {
      try {
        await post('/api/benchmarks/sync', { secid: p.secid, name: p.name });
        okList.push(p.name);
      } catch (e) { bad.push(p.name); }
    }
    loading(false);
    toast(bad.length ? `已更新 ${okList.length} 个，失败 ${bad.length} 个` : `已更新 ${okList.length} 个基准`);
    state.benchmarks = null;
    await loadBenchList();
    loadBenchmark();
  });
  $('#btn-do-reconcile').addEventListener('click', doReconcile);
  bindFundSearch();
  $('#btn-add-direct').addEventListener('click', () => {
    const code = $('#fund-code-direct').value.trim();
    if (!/^\d{6}$/.test(code)) { toast('请输入 6 位基金代码', true); return; }
    addFund(code, $('#fund-cat-direct').value || undefined);
  });

  // 配置分析
  $('#tpl-select').addEventListener('change', () => { state.curTpl = $('#tpl-select').value; refreshAlloc(); });
  let cashTimer;
  $('#extra-cash').addEventListener('input', () => {
    clearTimeout(cashTimer);
    cashTimer = setTimeout(refreshAlloc, 350);
  });
  $('#btn-manage-tpl').addEventListener('click', () => {
    const t = state.templates.find((x) => String(x.id) === String($('#tpl-select').value));
    openTplModal(t);
  });

  // 模板保存/删除
  $('#btn-save-tpl').addEventListener('click', async () => {
    const id = $('#t-id').value;
    const items = $$('#t-items input')
      .filter((i) => (parseFloat(i.value) || 0) > 0)
      .map((i) => ({ category: i.dataset.cat, target: parseFloat(i.value) }));
    const body = {
      name: $('#t-name').value.trim(),
      description: $('#t-desc').value.trim(),
      threshold: parseFloat($('#t-threshold').value) || 5,
      items,
    };
    try {
      loading(true);
      if (id) await put('/api/templates/' + id, body);
      else await post('/api/templates', body);
      toast('模板已保存');
      closeModal('#modal-tpl');
      await loadAnalysis();
    } catch (e) { toast(e.message, true); } finally { loading(false); }
  });
  $('#btn-del-tpl').addEventListener('click', async () => {
    const id = $('#t-id').value;
    if (!id || !confirm('删除这个模板？内置模板删掉后可以重新新建，但不会自动恢复。')) return;
    try {
      await del('/api/templates/' + id);
      toast('已删除');
      closeModal('#modal-tpl');
      state.curTpl = null;
      await loadAnalysis();
    } catch (e) { toast(e.message, true); }
  });

  // 导出
  $$('.quick-range button').forEach((b) => {
    b.addEventListener('click', () => {
      $$('.quick-range button').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      const { from, to } = shiftRange(b.dataset.q);
      $('#exp-from').value = from;
      $('#exp-to').value = to;
    });
  });
  $$('input[name="level"], input[name="format"]').forEach((r) => {
    r.addEventListener('change', () => {
      const group = r.name === 'level' ? '#level-group' : '#format-group';
      $$(group + ' .choice').forEach((c) => c.classList.toggle('checked', c.querySelector('input').checked));
    });
  });
  $('#btn-export').addEventListener('click', doExport);
  $('#btn-preview').addEventListener('click', doPreview);
  $('#btn-copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('#preview-body').textContent);
      toast('已复制到剪贴板');
    } catch { toast('复制失败，请手动选中', true); }
  });
}

/* ============================================================
   启动
   ============================================================ */

async function boot() {
  try {
    loading(true);
    state.meta = await api('/api/meta');
    await loadFunds();
    await loadOverview();
    // 预取模板，供导出与配置页使用
    try {
      const t = await api('/api/templates');
      state.templates = t.items || [];
      state.curTpl = state.templates[0] ? state.templates[0].id : null;
    } catch { /* 忽略 */ }
    prepareExport();
    bindEvents();
  } catch (e) {
    document.body.insertAdjacentHTML('afterbegin',
      `<div style="padding:16px;background:#c0392b;color:#fff;font-size:13px">启动失败：${esc(e.message)}</div>`);
  } finally {
    loading(false);
  }
}

boot();
