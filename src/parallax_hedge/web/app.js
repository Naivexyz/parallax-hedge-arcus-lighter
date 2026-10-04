'use strict';

const $ = (id) => document.getElementById(id);
const fmt = (v, d = 3) => (v === null || v === undefined || Number.isNaN(v) ? '—' : Number(v).toFixed(d));
const cls = (v) => (v > 0 ? 'pos' : v < 0 ? 'neg' : 'dim');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let costBps = 8;
let timer = null;

function tickClock() {
  $('clock').textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
}

function renderVenues(venues) {
  const names = { arcus: ['Arcus', 'Arcus Perps · 全仓'], lighter: ['Lighter', 'Robinhood Chain'] };
  $('venue-grid').innerHTML = Object.entries(venues || {}).map(([key, v]) => {
    const [title, sub] = names[key] || [key, ''];
    const state = !v.ok ? 'bad' : v.stale ? 'stale' : '';
    const label = !v.ok ? '离线' : v.stale ? `沿用 ${v.age_seconds}s 前` : '正常';
    return `<div class="venue-card ${state}">
      <header><span class="pulse-dot"></span><div><b>${esc(title)}</b></div><i>${esc(label)}</i></header>
      <div class="row"><span>${esc(sub)}</span></div>
      <div class="row"><span>网络出口</span><b>${esc(v.proxy)}</b></div>
      ${v.error ? `<div class="row"><span style="color:var(--amber)">${esc(v.error).slice(0, 90)}</span></div>` : ''}
    </div>`;
  }).join('');
}

function renderWarnings(list) {
  const box = $('period-warnings');
  if (!list || !list.length) { box.innerHTML = ''; box.style.display = 'none'; return; }
  box.style.display = '';
  box.innerHTML = list.map((w) =>
    `<p class="assumption-note" style="border-left-color:var(--red);border-color:rgba(255,109,122,.28);background:rgba(255,109,122,.05)">
      <b style="color:var(--red)">周期自检告警</b> ${esc(w)}
    </p>`).join('');
}

function renderPositions(snap) {
  const grid = $('pos-grid');
  const note = $('acct-note');
  const acct = snap.accounts || {};
  const missing = [];
  if (!acct.arcus_configured) missing.push('Arcus 地址');
  if (!acct.lighter_configured) missing.push('Lighter 账户索引');
  const errs = Object.entries(acct.errors || {});
  if (missing.length) {
    note.innerHTML = `尚未配置 ${esc(missing.join(' 和 '))} —— 在「账户设置」里填上即可读取持仓。`
      + ` <b style="color:var(--green)">这一步只读公开数据，不需要任何私钥。</b>`;
  } else if (errs.length) {
    note.innerHTML = errs.map(([k, v]) =>
      `<span style="color:var(--amber)">${esc(k)} 账户读取失败：${esc(v).slice(0, 110)}</span>`).join('<br>');
  } else {
    note.innerHTML = `两边账户读取正常。危险线：任一条腿距强平价小于 `
      + `<b>${esc(snap.liquidation_warn_pct)}%</b> —— `
      + `<b style="color:var(--amber)">处置动作是双腿同时平掉，绝不单腿止损</b>`
      + `（单腿平掉的瞬间，另一条腿就变成满仓裸单边）。`
      + `引擎只处理「轮换任务」里启用的币种。`;
  }

  const held = (snap.rows || []).filter((r) => r.position && r.position.status !== 'flat');
  $('pos-meta').textContent = held.length ? `${held.length} 个币种有持仓` : '当前空仓';
  if (!held.length) {
    grid.innerHTML = '<div class="pos-empty">当前两边都没有持仓。'
      + '手动在两个所各开一笔小额反向仓，这里就会显示强平价和距离，用来核对数字读得对不对。</div>';
    return;
  }
  grid.innerHTML = held.map((r) => {
    const p = r.position;
    const legs = ['lighter', 'arcus'].map((v) => {
      const l = p[v];
      if (!l || !l.size) return '';
      const isRisk = p.riskiest_venue === v;
      const sane = l.liquidation_sane;
      return `<div class="leg ${isRisk ? 'risk' : 'safe'}">
        <span class="leg-venue">${v === 'lighter' ? 'LIGHTER' : 'ARCUS'}</span>
        <span class="leg-main">${esc(l.side)} ${fmt(Math.abs(l.size), 4)}
          <small>开仓 ${fmt(l.entry_price, 4)} · 标记 ${fmt(l.mark_price, 4)} · 强平 ${fmt(l.liquidation_price, 4)}${sane ? '' : ' ⚠方向异常'}</small></span>
        <span class="leg-dist">${l.distance_pct === null ? '—' : fmt(l.distance_pct, 2) + '%'}
          <small>距强平</small></span>
      </div>`;
    }).join('');
    return `<div class="pos-card ${esc(p.status)}">
      <header><b>${esc(r.asset)}</b><i>${esc(p.status_text)}</i></header>
      <div class="pos-sub">净敞口 ${fmt(p.net_size, 4)} · 浮盈亏 ${fmt(p.total_unrealized, 2)} USDC${p.is_hedged ? ' · 对冲完好' : ''}</div>
      ${legs}
    </div>`;
  }).join('');
}

function renderStats(s) {
  const tradable = (s.rows || []).filter((r) => r.tradable);
  const best = tradable[0];
  const cards = [
    ['共有币种', s.matched_count ?? 0, 'MATCHED', `两所交集 ${s.market_count ?? 0} 个市场`],
    ['可交易', tradable.length, 'TRADABLE', s.suspect_count ? `${s.suspect_count} 个存疑已排除` : '全部通过合理性检查'],
    ['最优净费率', best ? fmt(best.net_bps_per_hour, 3) : '—', 'BPS/H', best ? `${best.asset} · ${best.direction_label}` : '等待数据'],
    ['最优年化', best ? fmt(best.apr_pct, 1) + '%' : '—', 'APR', best && best.breakeven_hours ? `回本约 ${fmt(best.breakeven_hours, 1)} 小时` : '—'],
  ];
  $('stats-grid').innerHTML = cards.map(([label, value, unit, foot]) => `
    <div class="stat-card">
      <div class="stat-top"><span>${esc(label)}</span><i>${esc(unit)}</i></div>
      <div class="stat-value">${esc(value)}</div>
      <div class="stat-foot"><span class="trend neutral">●</span><span>${esc(foot)}</span></div>
    </div>`).join('');
}

function renderTable(rows) {
  const body = $('funding-body');
  if (!rows || !rows.length) {
    body.innerHTML = '<tr><td colspan="9" class="dim" style="text-align:center;padding:40px">暂无数据</td></tr>';
    return;
  }
  body.innerHTML = rows.map((r) => {
    const dirCls = r.direction === 'long_lighter_short_arcus' ? 'long-lighter' : 'short-lighter';
    const blocked = !r.tradable;
    return `<tr class="${blocked ? 'blocked' : ''}">
      <td><div class="asset-cell"><b>${esc(r.asset)}</b><small>${esc(r.lighter_symbol)} / ${esc(r.arcus_symbol)}${r.off_hours ? ' · 休市中（Arcus 杠杆上限降低）' : ''}</small></div></td>
      <td class="${cls(r.lighter_bps_per_hour)}">${fmt(r.lighter_bps_per_hour)}</td>
      <td class="${cls(r.arcus_bps_per_hour)}">${fmt(r.arcus_bps_per_hour)}</td>
      <td class="${cls(r.net_bps_per_hour)}"><b>${fmt(r.net_bps_per_hour)}</b></td>
      <td>${blocked
        ? `<span class="warn-badge" title="${esc(r.reason)}">存疑</span>`
        : `<span class="dir-pill ${dirCls}">${esc(r.direction_label)}</span>`}</td>
      <td class="${cls(r.apr_pct)}">${fmt(r.apr_pct, 1)}%</td>
      <td class="${cls(r.net_bps_per_day)}">${fmt(r.net_bps_per_day, 2)}</td>
      <td class="${r.breakeven_hours && r.breakeven_hours < 24 ? 'pos' : 'dim'}">${r.breakeven_hours ? fmt(r.breakeven_hours, 1) : '—'}</td>
      <td class="dim">${esc(r.max_leverage)}×</td>
    </tr>`;
  }).join('');
}

async function load(force) {
  const btn = $('refresh-button');
  btn.classList.add('rotating');
  try {
    const res = await fetch(`/api/funding?cost_bps=${encodeURIComponent(costBps)}${force ? '&refresh=true' : ''}`);
    const s = await res.json();
    renderVenues(s.venues);
    if (s.error) {
      $('conn-title').textContent = '拉取失败';
      $('conn-sub').textContent = String(s.error).slice(0, 40);
      $('conn-card').className = 'connection-card offline';
      renderTable([]);
      $('stats-grid').innerHTML = '';
      return;
    }
    const anyStale = Object.values(s.venues || {}).some((v) => v.stale);
    $('conn-card').className = 'connection-card' + (anyStale ? ' degraded' : '');
    $('conn-title').textContent = anyStale ? '数据有延迟' : '运行中';
    $('conn-sub').textContent = `${s.matched_count ?? 0} 个共有币种`;
    $('nav-count').textContent = s.matched_count ?? 0;
    $('table-meta').textContent = new Date((s.generated_at || 0) * 1000).toLocaleTimeString('zh-CN', { hour12: false }) + ' 更新';
    renderWarnings(s.period_warnings);
    renderPositions(s);
    renderStats(s);
    renderTable(s.rows);
    if (!assets.length) loadSpreads();
    $('system-dump').textContent = JSON.stringify({ ...s, rows: `(${(s.rows || []).length} 行，见表格)` }, null, 2);
  } catch (e) {
    $('conn-title').textContent = '连接失败';
    $('conn-sub').textContent = String(e.message || e).slice(0, 40);
    $('conn-card').className = 'connection-card offline';
  } finally {
    btn.classList.remove('rotating');
  }
}


// ── 账户与交易统计 ───────────────────────────────────────
const money = (v, d = 2) => (v === null || v === undefined || Number.isNaN(Number(v))
  ? '—'
  : (Number(v) < 0 ? '-$' : '$') + Math.abs(Number(v)).toLocaleString('en-US',
      { minimumFractionDigits: d, maximumFractionDigits: d }));
const signed = (v, d = 2) => (v === null || v === undefined || Number.isNaN(Number(v))
  ? '—' : (Number(v) > 0 ? '+' : '') + money(v, d));
const hhmm = (ts) => new Date(ts * 1000).toLocaleString('zh-CN',
  { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });

function feeCell(v) {
  // 一笔都还没核对上时显示「—」，别让还不知道的手续费看起来像 $0.00
  if (v.fee_unknown > 0 && v.fee_unknown >= v.fills) return ['—', `${v.fee_unknown} 笔待交易所核对`];
  const main = money(v.fees, 2);
  return v.fee_unknown > 0 ? [main, `另有 ${v.fee_unknown} 笔待核对`] : [main, ''];
}
const MODE_LABEL = { ISOLATED: '逐仓', CROSS: '全仓', 'CROSS/ISOLATED': '全仓+逐仓' };

function ledgerCard(title, sub, bal, v, opts = {}) {
  const [fee, feeNote] = feeCell(v);
  const cells = opts.total
    ? [
        ['可用', bal ? money(bal.available) : '—', ''],
        ['浮动盈亏', bal ? signed(bal.unrealized_pnl) : '—', '', bal ? cls(bal.unrealized_pnl) : ''],
        ['交易量', money(v.volume, 0), `${v.fills} 笔成交`],
        ['手续费', fee, feeNote],
        ['净损益', v.net_pnl === null ? '—' : signed(v.net_pnl),
          v.net_pnl === null ? '两边平仓都对上账后才算' : '价差损益 − 手续费',
          v.net_pnl === null ? '' : cls(v.net_pnl)],
        ['每万美元磨损', v.wear_per_10k === null ? '—' : money(v.wear_per_10k),
          v.wear_per_10k === null ? '' : '刷 1 万美元交易量的成本'],
      ]
    : [
        ['可用', bal ? money(bal.available) : '—', ''],
        ['浮动盈亏', bal ? signed(bal.unrealized_pnl) : '—', '', bal ? cls(bal.unrealized_pnl) : ''],
        ['持仓', bal ? `${bal.open_positions} 个` : '—', bal && bal.mode ? (MODE_LABEL[bal.mode] || bal.mode) : ''],
        ['交易量', money(v.volume, 0), ''],
        ['手续费', fee, feeNote],
        ['成交', `${v.fills} 笔`, v.pending ? `${v.pending} 笔对账中` : (v.estimated ? `${v.estimated} 笔为估算` : '')],
      ];
  return `<div class="ledger-card${opts.total ? ' total' : ''}">
    <header><span class="venue-logo ${opts.logo || ''}">${esc(opts.mark || '∑')}</span>
      <div><b>${esc(title)}</b><small>${esc(sub)}</small></div>
      <i class="${bal ? '' : 'offline'}">${bal ? '账户权益' : '读取失败'}</i></header>
    <div class="ledger-equity">${bal ? money(bal.equity) : '—'}<small>USDC</small></div>
    <div class="ledger-cells">${cells.map(([k, val, note, c]) => `<div>
      <span>${esc(k)}</span><b class="${c || ''}">${esc(val)}</b>${note ? `<small>${esc(note)}</small>` : ''}
    </div>`).join('')}</div>
  </div>`;
}

function renderLedger(s) {
  const L = s.ledger || {};
  const venues = L.venues || {};
  const bal = s.balances || {};
  const empty = { volume: 0, fees: 0, fee_unknown: 0, fills: 0, pending: 0, estimated: 0,
                  net_pnl: null, wear_per_10k: null };
  $('ledger-grid').innerHTML = [
    ledgerCard('Lighter', 'Robinhood Chain', bal.lighter, venues.lighter || empty,
      { logo: 'lighter-logo', mark: 'L' }),
    ledgerCard('Arcus', 'Arcus Perps · 全仓', bal.arcus, venues.arcus || empty,
      { logo: 'arcus-logo', mark: 'A' }),
    ledgerCard('合计', '两边加总 · 全部任务', bal.total, L.total || empty, { total: true }),
  ].join('');

  const total = L.total || empty;
  const rec = s.reconciler || {};
  const meta = [];
  if (L.since) meta.push(`统计起点 ${hhmm(L.since)}`);
  if (total.pending) meta.push(`${total.pending} 笔对账中`);
  if (rec.at) meta.push(`上次对账 ${new Date(rec.at * 1000).toLocaleTimeString('zh-CN', { hour12: false })}`);
  $('ledger-meta').textContent = meta.join(' · ') || (s.dry_run ? '演练模式' : '还没有成交');

  const notes = [];
  if (s.dry_run) notes.push('<b>演练模式</b>：这里的交易量是模拟成交（按盘口参考价计），没有真实手续费。');
  Object.entries(s.account_errors || {}).forEach(([k, v]) =>
    notes.push(`<b>${esc(k)} 账户读取失败</b>：${esc(String(v).slice(0, 120))}`));
  (rec.errors || []).forEach((e) => notes.push(`<b>对账</b>：${esc(String(e).slice(0, 160))}`));
  if (!s.dry_run && total.fills && total.net_pnl === null) {
    notes.push('净损益和磨损要等<b>两边</b>的平仓成交都和交易所对上账才给出 —— '
      + '对冲的两条腿一赚一亏，只看一边会得出完全相反的结论。');
  }
  const note = $('ledger-note');
  note.style.display = notes.length ? '' : 'none';
  note.innerHTML = notes.join('<br>');

  const enabled = {};
  (s.tasks || []).forEach((t) => { enabled[t.asset] = t.enabled; });
  const byAsset = L.by_asset || {};
  const names = Array.from(new Set([...Object.keys(byAsset), ...Object.keys(enabled)])).sort();
  $('ledger-body').innerHTML = names.length
    ? names.map((a) => {
        const r = byAsset[a] || { lighter: empty, arcus: empty, total: { ...empty, realized_pnl: 0, pnl_unknown: 0 }, opens: 0 };
        const t = r.total;
        const state = a in enabled ? (enabled[a] ? '' : '<small class="dim">（已停用）</small>')
                                   : '<small class="dim">（任务已删除）</small>';
        const [fee, feeNote] = feeCell(t);
        return `<tr>
          <td><div class="asset-cell"><b>${esc(a)}</b>${state}</div></td>
          <td>${esc(r.opens)}</td>
          <td>${money(r.lighter.volume, 0)}</td>
          <td>${money(r.arcus.volume, 0)}</td>
          <td><b>${money(t.volume, 0)}</b></td>
          <td title="${esc(feeNote)}">${fee}${t.fee_unknown ? ' <span class="dim">*</span>' : ''}</td>
          <td class="${t.pnl_unknown ? 'dim' : cls(t.realized_pnl)}">${t.pnl_unknown ? '—' : signed(t.realized_pnl)}</td>
          <td class="${t.net_pnl === null ? 'dim' : cls(t.net_pnl)}">${t.net_pnl === null ? '—' : signed(t.net_pnl)}</td>
          <td class="${t.wear_per_10k === null ? 'dim' : ''}">${t.wear_per_10k === null ? '—' : money(t.wear_per_10k)}</td>
        </tr>`;
      }).join('')
    : '<tr><td colspan="9" class="dim" style="text-align:center;padding:26px">还没有成交记录。</td></tr>';
}

async function loadStats() {
  try {
    const s = await fetch('/api/stats').then((r) => r.json());
    renderLedger(s);
  } catch (e) {
    $('ledger-meta').textContent = '统计加载失败';
  }
}

// ── 轮换任务 ─────────────────────────────────────────────
let assets = [];
let spreadSignature = '';

// 币种下拉：按两所价差从窄到宽排（后台每 10 分钟扫一次）。
// 挂单模式按 Lighter 价差排（Arcus 那边是挂单，不付价差），吃单模式按两边之和排。
async function loadSpreads() {
  try {
    const r = await fetch('/api/spreads').then((x) => x.json());
    const list = r.assets || [];
    const label = (x) => {
      if (x.total_bps === undefined) return `${x.asset}  · 价差待扫描`;
      const main = r.maker ? x.lighter_bps : x.total_bps;
      return `${x.asset}  · ${fmt(main, 2)} bps（L ${fmt(x.lighter_bps, 2)} / A ${fmt(x.arcus_bps, 2)}）`;
    };
    const sig = list.map(label).join('|');
    if (!list.length || sig === spreadSignature) return;
    spreadSignature = sig;
    assets = list.map((x) => x.asset);
    const sel = $('f-asset');
    const keep = sel.value;
    sel.innerHTML = list.map((x) => `<option value="${esc(x.asset)}">${esc(label(x))}</option>`).join('');
    if (keep && assets.includes(keep)) sel.value = keep;
    const hint = $('spread-hint');
    if (hint) {
      hint.textContent = r.scanned_at
        ? `按${r.maker ? ' Lighter ' : '两所合计'}价差从窄到宽排序 · ${new Date(r.scanned_at * 1000).toLocaleTimeString('zh-CN', { hour12: false })} 扫描`
        : (r.scanning ? '正在扫描两所价差…' : '价差扫描将在启动后约 15 秒开始');
    }
  } catch (e) { /* 下拉框不该因为这个崩掉 */ }
}
let formTouched = false;
let settingsTouched = false;

function fillSettings(snapshot) {
  // 这四个数是引擎全局设置，不是某个任务的。正在改的时候不要被 10 秒刷新盖掉。
  if (settingsTouched) return;
  if (!$('f-min-hold')) return;
  $('f-min-hold').value = snapshot.min_hold_sec ?? 3;
  $('f-max-hold').value = snapshot.max_hold_sec ?? 300;
  $('f-pnl-usd').value = snapshot.pnl_close_usd ?? 0.02;
  if ($('f-cycle')) {
    $('f-cycle').value = snapshot.engine_cycle_seconds ?? snapshot.cycle_seconds ?? 20;
  }
}

function fillFormFromTask(tasks) {
  // 选中已有任务时把它的设置带进表单，免得改一个字段时把别的字段覆盖成默认值
  if (formTouched) return;
  const task = tasks.find((t) => t.asset === $('f-asset').value);
  if (!task) return;
  $('f-leverage').value = task.leverage;
  $('f-notional').value = task.notional_usdc ?? '';
}

function renderUnmanaged(list) {
  const boxes = ['unmanaged-warn', 'unmanaged-warn-top'].map((id) => $(id)).filter(Boolean);
  if (!boxes.length) return;
  if (!list || !list.length) {
    boxes.forEach((b) => { b.innerHTML = ''; b.style.display = 'none'; });
    return;
  }
  const html = list.map((u) => {
    const dist = u.min_distance_pct === null || u.min_distance_pct === undefined
      ? '—' : fmt(u.min_distance_pct, 2) + '%';
    return `<p class="assumption-note" style="border-left-color:var(--red);border-color:rgba(255,109,122,.3);background:rgba(255,109,122,.06)">
      <b style="color:var(--red)">${esc(u.asset)} 有持仓，但没有启用中的任务在管它。</b>
      当前状态「${esc(u.status_text || '')}」，距强平 ${esc(dist)}。
      面板会标红，但<b>引擎不会对它动手</b> —— 引擎只处理下面列表里启用的币种。
      要让程序接管，就给它建一个任务并启用；否则请自己盯着或手动平掉。
    </p>`;
  }).join('');
  boxes.forEach((b) => { b.style.display = ''; b.innerHTML = html; });
}

function planChip(plan, urgent) {
  const map = { open: ['on', '已开仓'], close: ['hold', '已平仓'], idle: ['off', '闲置'],
                blocked: ['blocked', '受阻'], flatten_orphan: ['urgent', '孤腿抢救'],
                open_failed: ['urgent', '开仓失败'], close_failed: ['urgent', '平仓失败'],
                flatten_orphan_failed: ['urgent', '抢救失败'],
                adopt: ['hold', '接管'], ledger: ['off', '账本'], rebase: ['off', '基准重设'],
                maker_wait: ['off', '未成交'], maker_started: ['hold', '挂单中'],
                maker_busy: ['hold', '挂单中'],
                spread_wait: ['hold', '价差等待'], close_wait: ['hold', '等待平仓'],
                topup: ['on', '已补仓'], topup_failed: ['urgent', '补仓失败'],
                topup_stop: ['hold', '叫停补仓'],
                error: ['urgent', '出错'] };
  const [cls, text] = map[plan] || ['off', plan];
  return `<span class="chip ${urgent ? 'urgent' : cls}">${esc(text)}</span>`;
}

async function loadTasks() {
  try {
    const [t, c] = await Promise.all([
      fetch('/api/tasks').then((r) => r.json()),
      fetch('/api/cycles?limit=40').then((r) => r.json()),
    ]);
    const pill = $('mode-pill');
    if (pill) {
      pill.className = 'mode-pill' + (t.dry_run ? '' : ' live');
      pill.querySelector('b').textContent = t.dry_run ? '演练 · 不下单' : '实盘 · 会下单';
    }
    renderUnmanaged(t.unmanaged);
    const banner = $('dry-banner');
    banner.className = 'dry-banner' + (t.dry_run ? '' : ' live');
    const gateNote = ` <b>开仓</b>：不要求买在更便宜的一边。所间价差不拦开仓。`
      + `Arcus 只做 maker，必须先成交（0 费）。成交之后立刻用 Lighter 吃单对冲（Lighter 无手续费）。Arcus 没成交就不发 Lighter，也不把 Arcus 改成吃单。`
      + `两腿都成交后至少持有 ${esc(t.min_hold_sec ?? 3)} 秒。`
      + `之后看每一边自己的开仓价对上自己的平仓价，合计不低于 -${esc(t.pnl_close_usd ?? 0.02)} USDC 就先挂 Arcus maker，成交后 Lighter 吃单平掉。差于这个差额就先不发，Arcus 不吃单。所间价差不算亏损。`
      + `<b>持有满 ${esc(t.max_hold_sec ?? 300)} 秒可以跟盘挂 Arcus maker，成交后 Lighter 吃单，Arcus 仍不吃单。</b>`
      + `关掉程序会撤掉挂单，并把还开着的仓位挂 maker 平掉。`
      + `同一套规则用于全部重叠市场，包括美股永续。只有风控和临近强平才让 Arcus 吃单。`;
    banner.innerHTML = t.dry_run
      ? `<b>演练模式</b> —— 引擎照常做全部判断并记录意图，但<b>不会提交任何订单</b>。`
        + (t.maker ? ` 挂单模式已开（演练里仍按吃单模拟成交）。` : '')
        + ` 开仓门槛：离强平 ≥ ${esc(t.min_corridor_pct)}%；平仓线 ${esc(t.close_corridor_pct)}%；每 ${esc(t.cycle_seconds)} 秒一轮。`
        + gateNote
        + ` 确认判断无误后，把 .env 里的 DRY_RUN 改成 false 再重启。`
      : `<b>实盘模式</b> —— 引擎会真实下单。`
        + gateNote
        + (t.maker ? `<b>Arcus 挂单模式</b>（0 手续费，每次最多等 ${esc(t.maker_wait_seconds)} 秒，在后台等；风控平仓仍立刻吃单）。`
          + (t.bell ? (t.bell.healthy ? ' 成交推送：<b style="color:var(--green)">已连接</b>（成交后立刻对冲）。'
                                      : ' 成交推送：<b style="color:var(--amber)">未连接</b>，已退回每 0.5 秒查一次成交。') : '') : '')
        + `开仓门槛 ${esc(t.min_corridor_pct)}%，平仓线 ${esc(t.close_corridor_pct)}%，`
        + ` 每 ${esc(t.cycle_seconds)} 秒一轮。`;

    fillSettings(t);
    $('nav-tasks').textContent = (t.tasks || []).length;
    const byAsset = {};
    (t.decisions || []).forEach((d) => { byAsset[d.asset] = d; });

    $('task-list').innerHTML = (t.tasks || []).length
      ? t.tasks.map((task) => {
          const d = byAsset[task.asset];
          const held = task.opened_at
            ? ((Date.now() / 1000 - task.opened_at) / 3600).toFixed(2) + ' 小时'
            : '空仓';
          const clock = `${esc(t.min_hold_sec ?? 3)}–${esc(t.max_hold_sec ?? 300)} 秒`;
          return `<div class="task-row">
            <div><b>${esc(task.asset)}</b></div>
            <div>${esc(task.leverage)}×<div class="muted">杠杆</div></div>
            <div>${clock}<div class="muted">平仓扫描 · 浮盈亏差额 ${esc(t.pnl_close_usd ?? 0.02)} U</div></div>
            <div>${esc(held)}<div class="muted">${task.open_direction ? esc(task.open_direction.startsWith('long') ? 'Lighter 多' : 'Lighter 空') : '当前持仓'}</div></div>
            <div>${task.corridor_at_open ? fmt(task.corridor_at_open, 2) + '%' : '—'}<div class="muted">开仓走廊</div></div>
            <div>${d ? planChip(d.plan, d.urgent) : ''}<div class="muted" title="${esc(d?.reason || '')}">${esc((d?.reason || '').slice(0, 40))}</div></div>
            <div style="display:flex;gap:6px">
              <button class="mini-btn" data-toggle="${esc(task.asset)}" data-on="${task.enabled ? 0 : 1}">${task.enabled ? '停用' : '启用'}</button>
              <button class="mini-btn danger" data-del="${esc(task.asset)}">删</button>
            </div>
          </div>`;
        }).join('')
      : '<div class="pos-empty">还没有任务。上面选个币种、设好杠杆就能建。</div>';
    fillFormFromTask(t.tasks || []);

    $('cycle-log').innerHTML = (c.cycles || []).length
      ? c.cycles.map((x) => `<div class="log-line">
          <time>${new Date(x.created_at * 1000).toLocaleTimeString('zh-CN', { hour12: false })}</time>
          <span>${esc(x.asset)}</span>
          <span>${planChip(x.plan, x.urgent)} ${esc(x.reason || '')}</span>
        </div>`).join('')
      : '<div class="pos-empty">暂无执行记录。</div>';
  } catch (e) { /* 面板不该因为这个崩掉 */ }
}

document.addEventListener('click', async (ev) => {
  const t = ev.target.closest('[data-toggle]');
  const d = ev.target.closest('[data-del]');
  if (t) {
    await fetch('/api/tasks', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ asset: t.dataset.toggle, enabled: t.dataset.on === '1' }) });
    loadTasks();
  } else if (d) {
    const res = await fetch('/api/tasks/' + encodeURIComponent(d.dataset.del), { method: 'DELETE' });
    if (!res.ok) alert((await res.json()).detail || '删除失败');
    loadTasks();
  }
});

$('f-save').addEventListener('click', async () => {
  const minHold = Number($('f-min-hold').value);
  const maxHold = Number($('f-max-hold').value);
  const pnlUsd = Number($('f-pnl-usd').value);
  const cycle = Number($('f-cycle').value);
  if (!Number.isFinite(minHold) || minHold < 0) { alert('两腿成交后开始扫描的秒数不能小于 0'); return; }
  if (!Number.isFinite(maxHold) || maxHold < minHold) {
    alert(`最长持有 ${maxHold} 秒不能小于开始扫描的 ${minHold} 秒`); return;
  }
  if (!Number.isFinite(pnlUsd) || pnlUsd < 0) { alert('浮盈亏差额必须是大于等于 0 的数字，单位 USDC，例如 0.02'); return; }
  if (!Number.isFinite(cycle) || cycle < 5) { alert('开仓扫描间隔不能小于 5 秒'); return; }
  const settingsRes = await fetch('/api/settings', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      min_hold_sec: minHold,
      max_hold_sec: maxHold,
      pnl_close_usd: pnlUsd,
      engine_cycle_seconds: cycle,
    }) });
  if (!settingsRes.ok) { alert((await settingsRes.json()).detail || '设置保存失败'); return; }
  settingsTouched = false;
  const asset = $('f-asset').value;
  if (!asset) { loadTasks(); return; }
  const body = {
    asset,
    leverage: Number($('f-leverage').value),
    notional_usdc: $('f-notional').value === '' ? null : Number($('f-notional').value),
  };
  const res = await fetch('/api/tasks', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!res.ok) { alert((await res.json()).detail || '保存失败'); return; }
  formTouched = false;
  loadTasks();
});

['f-leverage', 'f-notional'].forEach((id) =>
  $(id).addEventListener('input', () => { formTouched = true; }));
['f-min-hold', 'f-max-hold', 'f-pnl-usd', 'f-cycle'].forEach((id) =>
  $(id).addEventListener('input', () => { settingsTouched = true; }));
$('f-asset').addEventListener('change', () => { formTouched = false; loadTasks(); });

$('run-preflight').addEventListener('click', async () => {
  const btn = $('run-preflight');
  const box = $('preflight-box');
  btn.textContent = '检查中…';
  try {
    const r = await fetch('/api/preflight', { method: 'POST' }).then((x) => x.json());
    const mark = (ok) => ok
      ? '<span style="color:var(--green)">通过</span>'
      : '<span style="color:var(--red)">未过</span>';
    const checks = (r.checks || []).map((c) => `<div class="log-line">
      <span>${mark(c.ok)}</span><span>${esc(c.name)}</span><span>${esc(c.detail)}</span>
    </div>`).join('');
    const plans = (r.plans || []).map((p) => `<div class="log-line">
      <span>${mark(p.ok)}</span><span>${esc(p.asset)}${p.enabled ? '' : '（停用）'}</span>
      <span>${p.quantity !== undefined
        ? `${esc(p.direction_label || '')} 数量 <b>${fmt(p.quantity, 6)}</b>`
          + (p.lighter_leverage ? ` · 杠杆 L ${esc(p.lighter_leverage)}× / A ${esc(p.arcus_leverage)}×` : '')
          + ` · 名义 ${fmt(p.notional, 2)} USDC · 走廊 ${fmt(p.corridor_pct, 2)}%`
          + ` · 最小量 ${esc(p.min_quantity)}`
          + (p.aligned ? '' : ' <b style="color:var(--red)">数量未对齐精度</b>')
          + ` — ${esc(p.detail)}`
        : esc(p.detail)}</span>
    </div>`).join('');
    box.style.display = '';
    box.innerHTML = `<div class="panel" style="padding:18px 20px">
      <header class="panel-header compact">
        <div><span class="kicker">PREFLIGHT</span><h2>实盘预检</h2></div>
        <span class="chip ${r.ready ? 'on' : 'urgent'}">${esc(r.summary)}</span>
      </header>
      <div style="margin-top:12px">${checks}${plans}</div>
      <p class="acct-note" style="margin-top:14px">
        预检【不会下任何单】。全部通过之后，把 .env 里的 <b>DRY_RUN 改成 false</b> 并重启，
        才会真正下单。建议第一次把「单边名义上限」设到 20~30 USDC，用一轮真实开平
        把签名、精度、报文这层走通 —— 成本约 8 bps，也就是两三分钱。
      </p>
    </div>`;
  } catch (e) {
    box.style.display = '';
    box.innerHTML = `<p class="assumption-note">预检失败：${esc(e.message || e)}</p>`;
  } finally { btn.textContent = '实盘预检'; }
});

$('run-cycle').addEventListener('click', async () => {
  $('run-cycle').textContent = '运行中…';
  try { await fetch('/api/cycle', { method: 'POST' }); } finally {
    $('run-cycle').textContent = '立即跑一轮'; loadTasks();
  }
});

document.querySelectorAll('.nav-item').forEach((b) => b.addEventListener('click', () => {
  document.querySelectorAll('.nav-item').forEach((x) => x.classList.remove('active'));
  b.classList.add('active');
  const page = b.dataset.page;
  // 用 .active 类切换，不要碰行内 display ——
  // CSS 里是 .page{display:none} + .page.active{display:block}，
  // 设 style.display='' 会落回那条隐藏规则，页面永远出不来。
  ['funding', 'tasks', 'system', 'account'].forEach((name) => {
    $('page-' + name).classList.toggle('active', page === name);
  });
  $('page-title').textContent = { funding: '资金费总览', tasks: '轮换任务', system: '系统', account: '账户设置' }[page];
  if (page === 'tasks') loadTasks();
  if (page === 'account') loadAccountSettings();
}));

$('refresh-button').addEventListener('click', () => { load(true); loadStats(); });
$('cost-input').addEventListener('change', (e) => {
  const v = Number(e.target.value);
  if (Number.isFinite(v) && v >= 0) { costBps = v; load(false); }
  else e.target.value = costBps;
});


function accountPlaceholder(field) {
  if (field.kind === 'secret' || field.kind === 'identifier') {
    if (!field.set) return '未设置';
    return (field.display || '****') + '  · 留空不修改';
  }
  return '';
}

function renderAccountSettings(data) {
  const form = $('account-form');
  const groups = data.groups || [];
  form.innerHTML = groups.map((group) => {
    const fields = (data.fields || []).filter((field) => field.group === group.id);
    const inputs = fields.map((field) => {
      const masked = field.kind === 'secret' || field.kind === 'identifier';
      const inputType = field.kind === 'secret' ? 'password' : 'text';
      const value = masked ? '' : (field.value || '');
      return `<label class="field">
        <span>${esc(field.label)}</span>
        <input data-env="${esc(field.key)}" data-kind="${esc(field.kind)}"
          type="${inputType}" autocomplete="off" spellcheck="false"
          value="${esc(value)}" placeholder="${esc(accountPlaceholder(field))}" />
        <span class="field-hint">${esc(field.hint || '')}${field.set && masked ? ' 当前：' + esc(field.display || '') : ''}</span>
      </label>`;
    }).join('');
    return `<section class="account-group">
      <header class="panel-header compact"><div><span class="kicker">${esc(group.id.toUpperCase())}</span><h2>${esc(group.label)}</h2></div></header>
      <div class="account-grid">${inputs}</div>
    </section>`;
  }).join('') + `<div class="account-actions"><button class="primary-button" type="submit">保存账户设置</button></div>`;
}

async function loadAccountSettings() {
  const form = $('account-form');
  if (!form) return;
  try {
    const data = await fetch('/api/account-settings').then((r) => r.json());
    renderAccountSettings(data);
  } catch (e) {
    form.innerHTML = `<p class="acct-note">账户设置加载失败：${esc(e.message || e)}</p>`;
  }
}

$('account-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const status = $('account-status');
  const payload = {};
  $('account-form').querySelectorAll('[data-env]').forEach((input) => {
    payload[input.dataset.env] = input.value;
  });
  const button = $('account-form').querySelector('button[type="submit"]');
  if (button) button.disabled = true;
  try {
    const res = await fetch('/api/account-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      status.style.display = '';
      status.textContent = data.detail || '保存失败';
      return;
    }
    renderAccountSettings(data);
    status.style.display = '';
    status.textContent = data.note || '已保存';
  } catch (e) {
    status.style.display = '';
    status.textContent = '保存失败';
  } finally {
    const again = $('account-form').querySelector('button[type="submit"]');
    if (again) again.disabled = false;
  }
});

fetch('/api/status').then((r) => r.json()).then((s) => {
  $('meta-version').textContent = s.version || '—';
  $('meta-port').textContent = s.port || '—';
  const eff = s.effective || {};
  const dump = $('system-dump');
  if (dump) {
    dump.textContent = '当前实际生效的设置（不管 .env 里写没写，这里是准的）：\n\n'
      + Object.entries(eff).map(([k, v]) => `  ${k} = ${v}`).join('\n')
      + `\n\n  代理 · Arcus = ${s.proxies?.arcus || '—'}`
      + `\n  代理 · Lighter  = ${s.proxies?.lighter || '—'}`;
  }
}).catch(() => {});

tickClock();
setInterval(tickClock, 1000);
loadSpreads();
setInterval(loadSpreads, 30000);
load(true);
loadStats();
timer = setInterval(() => { load(false); loadStats(); }, 15000);
loadTasks();
setInterval(loadTasks, 10000);
