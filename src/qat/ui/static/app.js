/* QAT UI - vanilla JS SPA. Every value comes from the local QAT service API.
   Rules: missing values are shown as "산출 불가"/UNKNOWN (never 0), PnL always has a
   sign and a text label (not colour only), Live is always BLOCKED. */
'use strict';

const view = document.getElementById('view');
const toastEl = document.getElementById('toast');
const DECIMALS = { KRW: 0, USD: 2, USDT: 2 };
const STRATEGY_LABEL = { ma_trend: '이동평균 추세', breakout: '돌파', mean_reversion: '평균회귀' };
const KIND_LABEL = { backtest: 'Backtest', walkforward: 'Walk-Forward', stress: 'Cost Stress', lockbox: 'Lockbox' };
const state = { settings: null, datasets: null, selectedDataset: null, compare: new Set(), runType: 'backtest',
  lastProposal: null, requestId: null, busy: false };

/* ------------------------------------------------------------------ utils */
function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function isNum(v) { return typeof v === 'number' && Number.isFinite(v); }
function na(label = '산출 불가') { return `<span class="badge b-unknown" title="값을 계산할 근거가 없습니다">${esc(label)}</span>`; }
function fmtNumber(v, digits = 0) {
  return Math.abs(v).toLocaleString('ko-KR', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function money(v, ccy) {
  if (!isNum(v)) return na();
  const d = DECIMALS[ccy] ?? 2;
  return `<span class="num">${v < 0 ? '−' : ''}${fmtNumber(v, d)} ${esc(ccy)}</span>`;
}
function pnl(v, ccy) {
  if (!isNum(v)) return na();
  const d = DECIMALS[ccy] ?? 2;
  if (Math.abs(v) < Math.pow(10, -d) / 2) return `<span class="num flat-pnl">0 ${esc(ccy)} (변동 없음)</span>`;
  const cls = v > 0 ? 'gain' : 'loss';
  return `<span class="num ${cls}">${v > 0 ? '+' : '−'}${fmtNumber(v, d)} ${esc(ccy)} <span class="small">(${v > 0 ? '이익' : '손실'})</span></span>`;
}
function pct(v, { signed = true, label = true } = {}) {
  if (!isNum(v)) return na();
  const txt = `${(Math.abs(v) * 100).toFixed(2)}%`;
  if (!signed) return `<span class="num">${txt}</span>`;
  if (Math.abs(v) < 0.00005) return `<span class="num flat-pnl">0.00%${label ? ' (변동 없음)' : ''}</span>`;
  const cls = v > 0 ? 'gain' : 'loss';
  return `<span class="num ${cls}">${v > 0 ? '+' : '−'}${txt}${label ? ` <span class="small">(${v > 0 ? '상승' : '하락'})</span>` : ''}</span>`;
}
function qty(v) { return isNum(v) ? `<span class="num">${v.toLocaleString('ko-KR', { maximumFractionDigits: 8 })}</span>` : na(); }
function ratio(v) { return isNum(v) ? `<span class="num">${v.toFixed(2)}</span>` : na(); }
function when(iso) {
  if (!iso) return na('시각 없음');
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return esc(iso);
  return `<time datetime="${esc(iso)}" title="${esc(iso)}">${d.toISOString().replace('T', ' ').slice(0, 16)} UTC</time>`;
}
function badge(status, text) {
  const s = String(status || '').toUpperCase();
  let cls = 'b-muted';
  if (['PASS', 'OK', 'COMPLETED', 'APPLIED', 'FILLED'].includes(s)) cls = 'b-pass';
  else if (['BLOCK', 'BLOCKED', 'FAIL', 'REJECTED', 'ERROR', 'REJECTED_MISMATCH', 'CANCELLED'].includes(s)) cls = 'b-block';
  else if (['UNKNOWN', 'WARN', 'PARTIALLY_FILLED', 'STALE'].includes(s)) cls = 'b-unknown';
  else if (['SUBMITTED', 'PAPER', 'ACTIVE'].includes(s)) cls = 'b-accent';
  return `<span class="badge ${cls}">${esc(text ?? s)}</span>`;
}
function toast(msg) {
  toastEl.textContent = msg;
  toastEl.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { toastEl.hidden = true; }, 4200);
}
function newRequestId() {
  return (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : `r-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
async function api(path) {
  const res = await fetch(path, { headers: { Accept: 'application/json' } });
  const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
async function post(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-QAT-Client': 'ui', Accept: 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) { const e = new Error(data.error || `HTTP ${res.status}`); e.status = res.status; throw e; }
  return data;
}
function loading(title) {
  view.innerHTML = `<h1>${esc(title)}</h1><div class="card" aria-busy="true"><div class="skeleton"></div><div class="skeleton tall"></div><div class="skeleton"></div></div>`;
}
function errorCard(err) {
  return `<div class="error-box" role="alert"><strong>불러오기 실패</strong><br>${esc(err.message || err)}</div>`;
}

function chart(points, baseline, label) {
  const pts = (points || []).filter(p => isNum(p.equity));
  if (pts.length < 2) return `<div class="empty">표시할 평가 곡선이 없습니다 (평가 시점 ${pts.length}개).</div>`;
  const vals = pts.map(p => p.equity);
  let lo = Math.min(...vals, isNum(baseline) ? baseline : Infinity);
  let hi = Math.max(...vals, isNum(baseline) ? baseline : -Infinity);
  if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
  const W = 600, H = 180, x = i => (i / (pts.length - 1)) * W, y = v => H - 8 - ((v - lo) / (hi - lo)) * (H - 16);
  const line = pts.map((p, i) => `${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join(' ');
  const area = `0,${H} ${line} ${W},${H}`;
  const base = isNum(baseline) ? `<line class="base" x1="0" x2="${W}" y1="${y(baseline).toFixed(1)}" y2="${y(baseline).toFixed(1)}"/>` : '';
  const missing = (points || []).length - pts.length;
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(label)}: 최저 ${esc(lo.toFixed(2))}, 최고 ${esc(hi.toFixed(2))}">
      <polygon class="area" points="${area}"/>${base}<polyline class="line" points="${line}"/></svg>
    <div class="chart-axis"><span>${when(pts[0].timestamp)}</span><span class="num">최저 ${esc(fmtNumber(lo, 0))} · 최고 ${esc(fmtNumber(hi, 0))}</span><span>${when(pts[pts.length - 1].timestamp)}</span></div>
    ${missing ? `<p class="hint">평가가격이 없는 시점 ${missing}개는 곡선에서 제외했습니다.</p>` : ''}`;
}
function sourceBadge(synthetic, source) {
  return synthetic ? `<span class="badge b-unknown">합성 데이터 · ${esc(source)}</span>` : `<span class="badge b-accent">로컬 데이터 · ${esc(source)}</span>`;
}

/* ------------------------------------------------------------- top status */
async function refreshTop() {
  const el = document.getElementById('top-status');
  try {
    const s = await api('/api/status');
    const chips = [
      `<span class="badge b-live" title="${esc(s.live.reason)}">LIVE BLOCKED</span>`,
      s.session ? `<span class="badge b-accent">PAPER · 재생</span>` : `<span class="badge b-muted">세션 없음</span>`,
    ];
    if (s.session) chips.push(s.session.synthetic ? '<span class="badge b-unknown">합성</span>' : '<span class="badge b-accent">로컬 데이터</span>');
    if (s.latches.length) chips.push(`<span class="badge b-block">안전 잠금 ${s.latches.length}</span>`);
    if (s.startup === 'BLOCK') chips.push('<span class="badge b-block">시작 점검 BLOCK</span>');
    if (s.audit && !s.audit.healthy) chips.push('<span class="badge b-block">감사 로그 FAULT</span>');
    if (s.recovery_status && s.recovery_status !== 'NORMAL') chips.push(`<span class="badge ${s.recovery_status === 'FLATTENED' ? 'b-pass' : (s.recovery_status === 'FLATTEN_SUBMITTED' ? 'b-unknown' : 'b-block')}">복구 ${esc(s.recovery_status)}</span>`);
    if (!s.settings_ok) chips.push('<span class="badge b-block">설정 오류</span>');
    el.innerHTML = chips.join('');
  } catch (err) {
    el.innerHTML = '<span class="badge b-block">서버 연결 실패</span>';
  }
}

/* --------------------------------------------------------------- overview */
function sessionForm(datasets, compact) {
  const opts = datasets.map(d => `<option value="${esc(d.id)}" ${(d.validation_status === 'FAIL' || (d.admission && !d.admission.admitted)) ? 'disabled' : ''}>
      ${esc(d.meta ? d.meta.symbol : d.id)} · ${esc(d.meta ? d.meta.timeframe : '')} · ${esc(d.validation_status)}${d.meta && d.meta.synthetic ? ' · 합성' : ''}</option>`).join('');
  return `<form id="session-form" class="form-grid" novalidate>
      <div class="field full"><label for="sf-ds">재생할 데이터셋</label><select id="sf-ds" required>${opts}</select>
        <span class="hint">검증 FAIL 데이터셋은 선택할 수 없습니다. 실시간 시세가 아니라 과거 데이터를 한 봉씩 재생합니다.</span></div>
      <div class="field"><label for="sf-cash">초기 현금</label><input id="sf-cash" inputmode="decimal" value="10000000"></div>
      <div class="field"><label for="sf-bar">시작 봉 번호</label><input id="sf-bar" inputmode="numeric" value="60"></div>
      <div class="field full"><button class="primary" type="submit">${compact ? '새 Paper 세션 시작' : 'Paper 세션 시작'}</button></div>
    </form>`;
}
function bindSessionForm() {
  const f = document.getElementById('session-form');
  if (!f) return;
  const ds = document.getElementById('sf-ds');
  const cash = document.getElementById('sf-cash');
  const syncCash = () => {
    const d = (state.datasets || []).find(x => x.id === ds.value);
    if (d && d.currency) cash.value = d.currency === 'USD' ? '10000' : (d.meta.market === 'CRYPTO' ? '100000000' : '10000000');
  };
  ds.addEventListener('change', syncCash); syncCash();
  f.addEventListener('submit', async e => {
    e.preventDefault();
    const btn = f.querySelector('button'); btn.disabled = true;
    try {
      await post('/api/paper/session', { dataset_id: ds.value, initial_cash: Number(cash.value), start_bar: Number(document.getElementById('sf-bar').value) });
      toast('Paper 세션을 시작했습니다.');
      render();
    } catch (err) { toast(`세션 시작 거절: ${err.message}`); btn.disabled = false; }
  });
}

async function viewOverview() {
  loading('개요');
  const [ov, datasets] = await Promise.all([api('/api/overview'), api('/api/datasets')]);
  state.datasets = datasets;
  const st = ov.status;
  const statusCard = `<div class="card"><div class="card-head"><h2>시스템 상태</h2></div>
      <div class="pill-list">
        <span class="badge b-live">Live: BLOCKED</span>
        ${badge('UNKNOWN', 'Net Alpha: UNKNOWN')}
        ${healthBadge(st.strategy_health, 'Strategy Health')}
        <span class="badge b-muted">Shadow: 미구현</span>
        ${st.latches.length ? `<span class="badge b-block">안전 잠금 ${st.latches.length}건</span>` : '<span class="badge b-pass">안전 잠금 없음</span>'}
      </div>
      <p class="hint">${esc(st.live.reason)}</p></div>`;
  const latest = ov.latest_run ? `<div class="card"><div class="card-head"><h2>최근 연구 실행</h2><a href="#/research/${esc(ov.latest_run.run_id)}">자세히</a></div>
      <dl class="kv"><dt>종류</dt><dd>${esc(KIND_LABEL[ov.latest_run.kind] || ov.latest_run.kind)}</dd>
      <dt>전략</dt><dd>${esc(STRATEGY_LABEL[ov.latest_run.strategy] || ov.latest_run.strategy || '-')}</dd>
      <dt>종목</dt><dd>${esc(ov.latest_run.symbol || '-')} ${ov.latest_run.synthetic ? '<span class="badge b-unknown">합성</span>' : ''}</dd>
      <dt>Net 수익률</dt><dd>${pct(ov.latest_run.net_return)}</dd></dl>
      <p class="hint">연구 결과이며 Paper 계좌 성과가 아닙니다.</p></div>` : '';

  if (!ov.session) {
    view.innerHTML = `<h1>개요</h1><p class="lede">실행 중인 Paper 세션이 없습니다.</p>
      <div class="grid two"><div class="card"><h2>Paper 세션 시작</h2>${sessionForm(datasets)}</div>
      <div class="stack">${statusCard}${latest}</div></div>`;
    bindSessionForm();
    return;
  }
  const s = ov.session, info = st.session, ccy = s.currency;
  const blocks = s.top_block_reasons.length
    ? `<ul class="list">${s.top_block_reasons.map(r => `<li class="spread"><span class="wrap small">${esc(r.reason)}</span><span class="num">${r.count}회</span></li>`).join('')}</ul>`
    : '<div class="empty">차단된 제안이 없습니다.</div>';
  const fxNote = s.missing_fx.length ? `<p class="notice">환율 없음: ${esc(s.missing_fx.join(', '))} → 기준통화(${esc(s.base_currency)}) 합산값을 만들지 않습니다.</p>` : '';
  view.innerHTML = `<h1>개요</h1>
    <p class="lede">세션 ${esc(info.id)} · ${esc(info.symbol)} (${esc(info.market)}) · 기준시각 ${when(s.as_of)} · ${sourceBadge(info.synthetic, info.source)} ${badge('STALE', '과거 데이터 재생')}</p>
    <div class="grid two">
      <div class="card hero">
        <div class="hero-label">총 평가액 (${esc(ccy)})</div>
        <div class="hero-value">${money(s.equity, ccy)}</div>
        <div class="hero-sub"><span>기간 Net 수익률 ${pct(s.net_return)}</span><span>Net 손익 ${pnl(s.net_pnl, ccy)}</span></div>
        <p class="hint">기간: ${when(s.period_start)} → ${when(s.as_of)} · 모든 비용 반영 후 (Net)</p>
        ${fxNote}
      </div>
      <div class="card"><div class="card-head"><h2>재생 제어</h2><span class="muted small">봉 ${info.bar} / ${info.bars_total - 1}</span></div>
        <p class="hint">대기 주문은 다음 봉 시가에 체결을 시도합니다 (Level-1 모델, 호가·대기열·지연 없음).</p>
        <div class="row"><button class="primary" data-adv="1">다음 봉 ▶</button><button data-adv="5">+5 봉</button><button data-adv="20">+20 봉</button></div>
        <details><summary>새 세션 시작</summary>${sessionForm(datasets, true)}</details>
      </div>
    </div>
    <div class="grid four">
      <div class="card"><div class="kpi-label">실현손익</div><div class="kpi-value">${pnl(s.realized, ccy)}</div></div>
      <div class="card"><div class="kpi-label">미실현손익</div><div class="kpi-value">${pnl(s.unrealized, ccy)}</div><div class="kpi-note">평가가격: 재생 봉 종가</div></div>
      <div class="card"><div class="kpi-label">Drawdown (세션 고점 대비)</div><div class="kpi-value">${isNum(s.drawdown) ? (s.drawdown > 0 ? `<span class="num loss">−${(s.drawdown * 100).toFixed(2)}% <span class="small">(하락)</span></span>` : '<span class="num flat-pnl">0.00% (고점)</span>') : na()}</div></div>
      <div class="card"><div class="kpi-label">미체결 · 체결</div><div class="kpi-value num">${s.open_orders} · ${s.fills}</div>
        <div class="kpi-note">${s.kill_switch ? badge('BLOCK', 'Kill Switch ON') : badge('PASS', 'Kill Switch OFF')} ${s.reservation_breaches ? badge('BLOCK', `예약 초과 ${s.reservation_breaches}`) : ''}</div></div>
    </div>
    <div class="grid two">
      <div class="card"><div class="card-head"><h2>평가 곡선</h2><span class="muted small">점선 = 초기 현금</span></div>${chart(s.equity_curve, s.initial_cash, '세션 평가액 곡선')}</div>
      <div class="card"><div class="card-head"><h2>주요 차단 사유</h2><a href="#/orders">주문·체결</a></div>${blocks}</div>
    </div>
    <div class="grid two">${statusCard}${latest}</div>`;
  view.querySelectorAll('[data-adv]').forEach(b => b.addEventListener('click', async () => {
    view.querySelectorAll('[data-adv]').forEach(x => { x.disabled = true; });
    try {
      const out = await post('/api/paper/advance', { bars: Number(b.dataset.adv) });
      toast(`${out.advanced}봉 진행 · 기준시각 ${out.as_of.slice(0, 16)}${out.new_breaches.length ? ' · 예약 초과 발생: 신규 주문 차단' : ''}${out.end_of_data ? ' · 데이터 끝' : ''}`);
    } catch (err) { toast(`진행 거절: ${err.message}`); }
    render();
  }));
  bindSessionForm();
}

/* -------------------------------------------------------------- portfolio */
async function viewPortfolio() {
  loading('포트폴리오');
  let p;
  try { p = await api('/api/portfolio'); } catch (err) {
    view.innerHTML = `<h1>포트폴리오</h1>${err.message.includes('no paper session') ? '<div class="card empty">Paper 세션이 없습니다. <a href="#/overview">개요</a>에서 시작하세요.</div>' : errorCard(err)}`;
    return;
  }
  const cards = Object.entries(p.by_currency).map(([ccy, r]) => `<div class="card"><div class="card-head"><h2>${esc(ccy)}</h2>${r.equity === null ? badge('UNKNOWN', '평가가격 누락') : ''}</div>
      <dl class="kv"><dt>현금</dt><dd>${money(r.cash, ccy)}</dd><dt>예약</dt><dd>${money(r.reserved, ccy)}</dd>
      <dt>가용</dt><dd>${money(r.available, ccy)}</dd><dt>포지션 평가</dt><dd>${money(r.positions_value, ccy)}</dd>
      <dt>평가액</dt><dd>${money(r.equity, ccy)}</dd><dt>실현손익</dt><dd>${pnl(r.realized, ccy)}</dd>
      <dt>미실현손익</dt><dd>${pnl(r.unrealized, ccy)}</dd><dt>수수료</dt><dd>${money(r.fees, ccy)}</dd>
      <dt>세금</dt><dd>${money(r.taxes, ccy)}</dd></dl></div>`).join('');
  const total = isNum(p.total_base) ? money(p.total_base, p.base_currency)
    : `${na('합산 불가')} <span class="small muted">${p.missing_fx.length ? `환율 없음: ${esc(p.missing_fx.join(', '))}` : ''}${p.missing_marks.length ? ` 평가가격 없음: ${esc(p.missing_marks.map(m => m.join(':')).join(', '))}` : ''}</span>`;
  const rows = p.positions.map(r => `<tr><td><strong>${esc(r.symbol)}</strong><br><span class="small muted">${esc(r.market)} · ${esc(r.currency)}</span></td>
      <td class="num">${qty(r.quantity)}<br><span class="small muted">예약 ${qty(r.reserved_quantity)}</span></td>
      <td class="num">${money(r.avg_cost, r.currency)}</td><td class="num">${money(r.mark, r.currency)}</td>
      <td class="num">${money(r.market_value, r.currency)}</td><td class="num">${pct(r.exposure, { signed: false })}</td>
      <td class="num">${pnl(r.unrealized, r.currency)}</td></tr>`).join('');
  view.innerHTML = `<h1>포트폴리오</h1><p class="lede">기준시각 ${when(p.as_of)} · 평가가격 출처: 재생 데이터 종가 · 평균원가에는 매수 비용 포함</p>
    <div class="card"><div class="spread"><span class="muted">기준통화(${esc(p.base_currency)}) 합산</span><strong>${total}</strong></div><p class="hint">${esc(p.fx_note)}</p></div>
    <div class="grid three">${cards}</div>
    <div class="card"><h2>포지션</h2>${p.positions.length ? `<div class="table-wrap"><table><thead><tr><th>종목</th><th class="num">수량</th><th class="num">평균원가</th><th class="num">평가가격</th><th class="num">평가금액</th><th class="num">노출</th><th class="num">미실현</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="empty">보유 포지션이 없습니다.</div>'}</div>`;
}

/* ---------------------------------------------------------------- orders */
function decisionsTimeline(decisions) {
  if (!decisions || !decisions.length) return '<p class="hint">Gate 판정 기록 없음</p>';
  return `<ul class="timeline">${decisions.map(d => `<li><span class="small">${esc(d.stage)}</span>${badge(d.status)}<span class="small wrap">${esc((d.reasons || []).join(', ') || '—')}</span></li>`).join('')}</ul>`;
}
async function viewOrders() {
  loading('주문·체결');
  let o, st;
  try { [o, st] = await Promise.all([api('/api/orders'), api('/api/status')]); } catch (err) {
    view.innerHTML = `<h1>주문·체결</h1>${err.message.includes('no paper session') ? '<div class="card empty">Paper 세션이 없습니다. <a href="#/overview">개요</a>에서 시작하세요.</div>' : errorCard(err)}`;
    return;
  }
  if (!state.requestId) state.requestId = newRequestId();
  const sess = st.session;
  const last = state.lastProposal;
  const lastCard = last ? `<div class="card flat" aria-live="polite"><div class="card-head"><h2>최근 제안 결과</h2>${last.accepted ? badge('PASS', '승인·제출') : badge(last.reason && last.reason.includes('UNKNOWN') ? 'UNKNOWN' : 'BLOCK', '거절')}</div>
      <p class="small wrap">${esc(last.reason)}${last.idempotent_replay ? ' · (중복 요청: 이전 결과 재사용, 신규 주문 없음)' : ''}</p>
      <p class="hint">기준가격(서버 결정): ${esc(last.reference_price)} · ${esc(last.expected_return_source || '')}</p>${decisionsTimeline(last.decisions)}</div>` : '';
  const orders = o.orders.map(r => `<li><div class="spread"><strong>${esc(r.side === 'BUY' ? '매수' : '매도')} ${qty(r.quantity)} · ${esc(r.order_type)}</strong>${badge(r.status)}</div>
      <div class="small muted wrap">${esc(r.order_id)} · 기준가 ${esc(r.reference_price)}${r.limit_price ? ` · 지정가 ${esc(r.limit_price)}` : ''} · 제출 ${when(r.submitted_as_of)}</div>
      <div class="small">체결 ${qty(r.filled_quantity)} / 잔량 ${qty(r.remaining_quantity)} · 평균체결가 ${isNum(r.avg_fill_price) ? esc(r.avg_fill_price.toFixed(4)) : '—'} · 잔여 예약 ${isNum(r.reservation_remaining) ? esc(r.reservation_remaining.toFixed(2)) : '—'}</div>
      <div class="pill-list">${(r.status_history || []).map(s => `<span class="badge b-muted">${esc(s)}</span>`).join('')}</div>
      ${['SUBMITTED', 'PARTIALLY_FILLED'].includes(r.status) ? `<button data-cancel="${esc(r.order_id)}">주문 취소</button>` : ''}</li>`).join('');
  const fills = o.fills.map(f => `<tr><td>${when(f.timestamp)}</td><td>${esc(f.side === 'BUY' ? '매수' : '매도')}</td><td class="num">${qty(f.quantity)}</td>
      <td class="num">${esc(f.price.toFixed(4))}<br><span class="small muted">기준 ${esc(f.reference_price)}</span></td><td class="num">${esc(f.commission.toFixed(2))}</td><td class="num">${esc(f.tax.toFixed(2))}</td><td class="num">${esc(f.slippage_estimate.toFixed(2))}</td><td>${badge(f.outcome)}</td></tr>`).join('');
  const rejections = o.rejections.map(r => `<li><div class="spread"><strong>${esc(r.side === 'BUY' ? '매수' : '매도')} ${qty(r.quantity)}</strong>${badge(r.reason.includes(':UNKNOWN') ? 'UNKNOWN' : 'BLOCK', r.stage)}</div><div class="small wrap">${esc(r.reason)}</div><div class="hint">${when(r.as_of)}</div></li>`).join('');
  const audit = o.audit.slice(0, 60).map(a => `<tr><td class="small">${esc(a.timestamp.slice(0, 19))}</td><td class="small">${esc(a.stage)}</td><td class="small wrap">${esc(JSON.stringify(Object.fromEntries(Object.entries(a).filter(([k]) => !['stage', 'timestamp'].includes(k)))).slice(0, 220))}</td></tr>`).join('');
  view.innerHTML = `<h1>주문·체결</h1><p class="lede">세션 ${esc(sess.id)} · ${esc(sess.symbol)} · 기준시각 ${when(o.as_of)} · 모든 제안은 서버의 Gateway → Validator → Net Alpha → Risk → Compliance → Integrity를 통과해야 합니다.</p>
    <div class="grid two">
      <div class="card"><h2>주문 제안</h2>
        <form id="order-form" class="form-grid" novalidate>
          <div class="field full"><span class="small muted">종목: <strong>${esc(sess.symbol)}</strong> (세션 고정) · 가격은 서버가 현재 재생 봉 종가로 결정</span></div>
          <div class="field full"><label id="side-label">방향</label><div class="seg" role="group" aria-labelledby="side-label"><button type="button" data-side="BUY" aria-pressed="true">매수</button><button type="button" data-side="SELL" aria-pressed="false">매도</button></div></div>
          <div class="field"><label for="of-qty">수량</label><input id="of-qty" inputmode="decimal" value="10" required></div>
          <div class="field"><label for="of-type">주문 유형</label><select id="of-type"><option value="MARKET">시장가</option><option value="LIMIT">지정가</option></select></div>
          <div class="field"><label for="of-limit">지정가</label><input id="of-limit" inputmode="decimal" placeholder="LIMIT일 때만" disabled><span class="hint">시장가=다음 봉 시가 체결 · 지정가=1봉 유효</span></div>
          <div class="field"><label for="of-exp">기대 총수익률 (사용자 주장)</label><input id="of-exp" inputmode="decimal" value="0.01"><span class="hint">예: 0.01 = 1%. 추정치가 아니라 입력값이며 Net Alpha Gate가 비용과 비교합니다.</span></div>
          <div class="field full"><button class="primary" type="submit" id="of-submit">제안 제출</button></div>
        </form></div>
      <div class="stack">${lastCard || '<div class="card empty">아직 이 화면에서 제출한 제안이 없습니다.</div>'}</div>
    </div>
    <div class="grid two">
      <div class="card"><h2>주문 (${o.orders.length})</h2>${o.orders.length ? `<ul class="list">${orders}</ul>` : '<div class="empty">주문이 없습니다.</div>'}</div>
      <div class="card"><h2>Gate 거절 (${o.rejections.length})</h2>${o.rejections.length ? `<ul class="list">${rejections}</ul>` : '<div class="empty">거절된 제안이 없습니다.</div>'}</div>
    </div>
    <div class="card"><h2>체결 (${o.fills.length})</h2>${o.fills.length ? `<div class="table-wrap"><table><thead><tr><th>시각</th><th>방향</th><th class="num">수량</th><th class="num">체결가</th><th class="num">수수료</th><th class="num">세금</th><th class="num">슬리피지(기록)</th><th>정산</th></tr></thead><tbody>${fills}</tbody></table></div><p class="hint">슬리피지는 체결가에 이미 반영되어 있으며 현금에서 다시 차감하지 않습니다.</p>` : '<div class="empty">체결이 없습니다.</div>'}</div>
    <div class="card"><details><summary>감사 기록 (최근 ${Math.min(60, o.audit.length)} / 전체 ${o.audit_total})</summary><p class="hint">${esc(o.audit_note)}</p><div class="table-wrap"><table><tbody>${audit}</tbody></table></div></details></div>`;

  let side = 'BUY';
  view.querySelectorAll('[data-side]').forEach(b => b.addEventListener('click', () => {
    side = b.dataset.side;
    view.querySelectorAll('[data-side]').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
  }));
  const type = document.getElementById('of-type'), limit = document.getElementById('of-limit');
  type.addEventListener('change', () => { limit.disabled = type.value !== 'LIMIT'; if (limit.disabled) limit.value = ''; });
  document.getElementById('order-form').addEventListener('submit', async e => {
    e.preventDefault();
    if (state.busy) return; // double-click guard (server is idempotent on client_request_id too)
    state.busy = true;
    const btn = document.getElementById('of-submit'); btn.disabled = true; btn.textContent = '제출 중…';
    const body = { side, quantity: Number(document.getElementById('of-qty').value), order_type: type.value,
      expected_gross_return: Number(document.getElementById('of-exp').value), client_request_id: state.requestId };
    if (type.value === 'LIMIT') body.limit_price = Number(limit.value);
    try {
      state.lastProposal = await post('/api/paper/proposals', body);
      toast(state.lastProposal.accepted ? '제안이 승인되어 제출되었습니다.' : `제안 거절: ${state.lastProposal.stage}`);
    } catch (err) {
      state.lastProposal = { accepted: false, reason: err.message, decisions: [] };
      toast(`요청 오류: ${err.message}`);
    } finally {
      state.requestId = newRequestId();
      state.busy = false;
    }
    render();
  });
  view.querySelectorAll('[data-cancel]').forEach(b => b.addEventListener('click', async () => {
    b.disabled = true;
    try { await post(`/api/paper/orders/${encodeURIComponent(b.dataset.cancel)}/cancel`, {}); toast('주문을 취소하고 잔여 예약을 해제했습니다.'); }
    catch (err) { toast(`취소 거절: ${err.message}`); }
    render();
  }));
}

/* ------------------------------------------------------------------ risk */
function ruleRow(r) {
  let status, value = '—';
  if (!r.implemented) status = '<span class="badge b-muted">미구현</span>';
  else if (r.configured === false || r.value === null) status = '<span class="badge b-unknown">미설정 (NOT_CONFIGURED)</span>';
  else if (r.supported_by_current_data === false) { status = '<span class="badge b-unknown">데이터 미지원 → UNKNOWN</span>'; value = esc(String(r.value)); }
  else {
    status = badge('PASS', '적용');
    value = r.value === undefined ? '항상 적용' : (typeof r.value === 'boolean' ? (r.value ? 'ON' : 'OFF') : esc(String(r.value)));
  }
  return `<tr><td>${esc(r.id)}</td><td>${esc(r.name)}</td><td>${status}</td><td class="num">${value}</td></tr>`;
}
async function viewRisk() {
  loading('리스크·준법');
  const r = await api('/api/risk');
  const latches = r.latches.length ? `<ul class="list">${r.latches.map(l => `<li><div class="spread"><strong>${esc(l.kind)}</strong>${badge('BLOCK', l.id)}</div><div class="small wrap">${esc(l.reason)}</div><div class="hint">${when(l.engaged_utc)}</div></li>`).join('')}</ul>` : '<div class="empty">활성 안전 잠금이 없습니다.</div>';
  const breaches = r.reservation_breaches.length ? `<ul class="list">${r.reservation_breaches.map(b => `<li class="small wrap">주문 ${esc(b.order_id)} · 실제비용 ${esc(Number(b.actual_cost).toFixed(2))} &gt; 예약 ${esc(Number(b.reservation_allocated).toFixed(2))} (초과 ${esc(Number(b.overrun).toFixed(2))})</li>`).join('')}</ul>` : '<div class="empty">예약 초과 기록 없음</div>';
  view.innerHTML = `<h1>리스크·준법</h1><p class="lede">Risk는 전략보다 우선합니다. PASS만 진행하며 BLOCK &gt; UNKNOWN &gt; PASS 순으로 결합합니다.</p>
    <div class="notice block"><strong>Live 실행: BLOCKED</strong> — 이 UI에는 Live를 켜는 기능이 없습니다. 소프트웨어 PASS는 법적 적합성 인증이 아닙니다.</div>
    <div class="grid two">
      <div class="card"><div class="card-head"><h2>Kill Switch</h2>${r.kill_switch ? badge('BLOCK', 'ON · 신규 주문 차단') : badge('PASS', 'OFF')}</div>
        <p class="small">켜면 영구 잠금이 기록되어 새로고침·세션 재시작·서버 재시작 후에도 유지됩니다. UI에서는 해제할 수 없습니다.</p>
        <p class="hint wrap">${esc(r.latch_clear_policy)}</p>
        <form id="ks-form" class="stack"><div class="field"><label for="ks-reason">사유</label><input id="ks-reason" maxlength="200" placeholder="예: 데이터 이상 확인 필요"></div>
        <button class="danger" type="submit" ${r.kill_switch ? 'disabled' : ''}>Kill Switch 켜기</button></form></div>
      <div class="card"><h2>안전 잠금</h2>${latches}</div>
    </div>
    <div class="grid three">
      <div class="card"><h2>Reservation Breach</h2>${breaches}</div>
      <div class="card"><div class="card-head"><h2>대조(Reconciliation)</h2>${badge(r.reconciliation.status)}</div><p class="small">${esc(r.reconciliation.reason)}</p></div>
      <div class="card"><h2>데이터 · FX</h2>${r.data ? `<dl class="kv"><dt>데이터 검증</dt><dd>${badge(r.data.dataset_validation)}</dd><dt>출처</dt><dd>${r.data.synthetic ? '<span class="badge b-unknown">합성</span>' : '<span class="badge b-accent">로컬</span>'}</dd>
        <dt>신선도</dt><dd>${badge('STALE', '과거 데이터 재생')}</dd><dt>평가가격 누락</dt><dd>${r.data.missing_marks.length ? badge('UNKNOWN', r.data.missing_marks.map(m => m.join(':')).join(', ')) : '없음'}</dd>
        <dt>환율 누락</dt><dd>${r.data.missing_fx.length ? badge('UNKNOWN', r.data.missing_fx.join(', ')) : '없음'}</dd></dl>` : '<div class="empty">세션 없음</div>'}</div>
    </div>
    <div class="grid two">
      <div class="card"><h2>Risk 규칙</h2><div class="table-wrap"><table><thead><tr><th>ID</th><th>규칙</th><th>상태</th><th class="num">값</th></tr></thead><tbody>${r.risk_rules.map(ruleRow).join('')}</tbody></table></div><p class="hint">한도 숫자는 확정되지 않았습니다(설정값 null = 미설정).</p></div>
      <div class="card"><h2>Market Integrity</h2><div class="table-wrap"><table><thead><tr><th>ID</th><th>검사</th><th>상태</th><th class="num">값</th></tr></thead><tbody>${r.integrity_rules.map(ruleRow).join('')}</tbody></table></div></div>
    </div>
    <div class="grid two">
      <div class="card"><h2>Compliance</h2><dl class="kv"><dt>모드</dt><dd>${esc(r.compliance.mode || '—')}</dd><dt>허용 시장</dt><dd>${esc((r.compliance.enabled_markets || ['전체']).join(', '))}</dd>
        <dt>Manual/Paper 허용 목록</dt><dd>${r.compliance.universe_configured ? badge('PASS', '설정됨') : badge('UNKNOWN', '미설정 — 전부 차단')} <span class="small wrap">${esc(r.compliance.tradable_symbols)}</span></dd></dl><p class="hint">${esc(r.compliance.universe_note)}</p><p class="hint">${esc(r.compliance.legal_note)}</p></div>
      <div class="card"><h2>최근 Gate 판정</h2>${decisionsTimeline(r.last_decisions)}<p class="hint">Strategy Health: ${healthBadge(r.strategy_health, '')} 운영 상태만 평가하며 성과는 평가하지 않습니다.</p></div>
    </div>
    <div class="card"><h2>노출 축소 SELL 처리 (Control Tower 정책)</h2><p class="hint">서버가 원장으로 판정합니다(클라이언트 주장 불가). Kill Switch는 자동 청산을 뜻하지 않습니다. 긴급 청산은 <a href="#/operations/recovery">복구 화면</a>에서 운영자가 명시적으로만 실행합니다.</p>
      <div class="table-wrap"><table><thead><tr><th>규칙</th><th>노출 축소 SELL</th><th>비고</th></tr></thead><tbody>${(r.exit_policy || []).map(x => `<tr><td>${esc(x.rule)}</td><td>${esc(x.reducing_sell)}</td><td class="small wrap">${esc(x.note)}</td></tr>`).join('')}</tbody></table></div>
      ${(r.accounting_integrity || []).length ? `<p class="notice block">회계 무결성 문제: ${esc(r.accounting_integrity.join(', '))}</p>` : ''}</div>`;
  document.getElementById('ks-form').addEventListener('submit', async e => {
    e.preventDefault();
    if (!window.confirm('Kill Switch를 켜면 UI에서 해제할 수 없습니다. 계속할까요?')) return;
    try { await post('/api/paper/kill-switch', { reason: document.getElementById('ks-reason').value || 'manual (UI)' }); toast('Kill Switch가 켜졌습니다. 모든 신규 주문이 차단됩니다.'); }
    catch (err) { toast(`실패: ${err.message}`); }
    render();
  });
}

/* -------------------------------------------------------------- research */
function metricsGrid(m) {
  const c = m.currency;
  const item = (label, html) => `<div class="card flat"><div class="kpi-label">${label}</div><div class="kpi-value">${html}</div></div>`;
  return `<div class="grid four">
    ${item('시작 → 종료 평가액', `${money(m.starting_equity, c)}<br>${money(m.ending_equity, c)}`)}
    ${item('Net 손익 · 수익률', `${pnl(m.net_pnl, c)}<br>${pct(m.net_return)}`)}
    ${item('Gross 손익 · 수익률', `${pnl(m.gross_pnl, c)}<br>${pct(m.gross_return)}`)}
    ${item('Max Drawdown', isNum(m.max_drawdown_pct) ? `<span class="num loss">−${(m.max_drawdown_pct * 100).toFixed(2)}%</span>` : na())}
    ${item('거래 (청산 · 미청산)', `<span class="num">${m.trades_closed} · ${m.trades_open}</span>`)}
    ${item('승률', isNum(m.win_rate) ? pct(m.win_rate, { signed: false }) : na('정의 불가'))}
    ${item('평균 이익 · 손실', `${isNum(m.avg_win) ? pnl(m.avg_win, c) : na('없음')}<br>${isNum(m.avg_loss) ? pnl(m.avg_loss, c) : na('없음')}`)}
    ${item('Expectancy · Profit Factor', `${isNum(m.expectancy) ? pnl(m.expectancy, c) : na('정의 불가')}<br>${isNum(m.profit_factor) ? ratio(m.profit_factor) : na('정의 불가')}`)}
    ${item('수수료 · 세금', `${money(m.commission + m.exchange_fee, c)}<br>${money(m.tax, c)}`)}
    ${item('슬리피지 (기록용)', money(m.slippage_estimate, c))}
    ${item('Turnover · 노출시간', `${ratio(m.turnover)}<br>${pct(m.exposure_time, { signed: false })}`)}
    ${item('Gate 거절 (청산 거절)', `<span class="num">${m.rejections} (${m.exit_rejections ?? 0})</span>`)}
  </div>
  <p class="hint">Gross = 체결가 기준(슬리피지·스프레드 포함) 명시적 수수료·세금 차감 전. Net = 모든 비용 후 원장 평가액 변화. 비용은 한 번만 반영합니다.</p>
  ${(m.notes || []).length ? `<div class="notice">${m.notes.map(esc).join('<br>')}</div>` : ''}`;
}
function runDetailHTML(d) {
  const man = d.manifest, m = d.metrics, res = d.result;
  const labels = man.labels || {};
  const warnings = (man.warnings || []).concat(res.metrics && res.metrics.notes ? [] : []);
  const head = `<div class="card"><div class="card-head"><h2>${esc(KIND_LABEL[man.kind] || man.kind)} · ${esc(STRATEGY_LABEL[(man.strategy || {}).name] || (man.strategy || {}).name || '')}</h2><span class="small muted wrap">${esc(man.run_id)}</span></div>
      <div class="pill-list">${labels.synthetic_data || man.synthetic_data ? '<span class="badge b-unknown">합성 데이터 — 실제 성과 아님</span>' : '<span class="badge b-accent">로컬 데이터</span>'}
      ${labels.alpha_mode === 'fixture' ? '<span class="badge b-unknown">FIXTURE 기대수익 — 추정 아님</span>' : ''}${labels.zero_cost_fixture ? '<span class="badge b-block">무비용 fixture</span>' : ''}
      <span class="badge b-muted">연구 결과 (Paper 아님)</span>${man.kind === 'walkforward' ? (man.independent_oos ? badge('PASS', '독립 OOS (최초 평가)') : badge('UNKNOWN', 'OOS 재사용 — 독립 아님')) : ''}
      ${man.kind === 'lockbox' ? (man.lockbox_independent ? badge('PASS', 'Lockbox 최초 사용') : badge('UNKNOWN', 'Lockbox 재사용')) : ''}</div>
      ${warnings.length ? `<div class="notice">${warnings.map(esc).join('<br>')}</div>` : ''}
      <details><summary>재현성 Manifest</summary><dl class="kv">
        <dt>code_commit</dt><dd>${esc(man.code_commit)}</dd><dt>git dirty</dt><dd>${esc(man.git_dirty)}</dd>
        <dt>source tree</dt><dd>${esc((man.source_tree_sha256 || '').slice(0, 16))}…</dd>
        <dt>data_version</dt><dd>${esc((man.data || {}).data_version)}</dd><dt>data sha256</dt><dd>${esc(((man.data || {}).sha256 || '').slice(0, 16))}…</dd>
        <dt>설정</dt><dd>${esc(((man.settings || {}).sha256 || man.config_version || '').slice(0, 12))} (placeholder)</dd>
        <dt>비용모델 버전</dt><dd>${esc(man.cost_model_version || '—')}</dd><dt>파라미터</dt><dd>${esc(JSON.stringify((man.strategy || {}).params || {}))}</dd>
        <dt>초기자본</dt><dd>${esc(man.initial_capital)}</dd><dt>seed</dt><dd>${esc(man.seed)}</dd>
        <dt>경제적 지문</dt><dd>${esc((man.economic_fingerprint || man.stress_fingerprint || '—').slice(0, 16))}</dd>
        <dt>Strategy Health</dt><dd><span class="badge b-muted">연구 실행에는 해당 없음 (운영 상태 전용)</span></dd></dl></details></div>`;
  if (man.kind === 'walkforward') {
    const rows = res.folds.map(f => `<tr><td>${f.fold}</td><td class="small">${f.train.join('–')}</td><td class="small">${f.test.join('–')}</td><td>${badge(f.status)}</td>
      <td class="small wrap">${esc(JSON.stringify(f.chosen_params || {}))}</td><td class="num">${f.oos_metrics ? pct(f.oos_metrics.net_return, { label: false }) : na('실패')}</td>
      <td class="num">${f.oos_metrics ? f.oos_metrics.trades_closed : '—'}</td></tr>${f.error ? `<tr><td></td><td colspan="6" class="small wrap">${esc(f.error)}</td></tr>` : ''}`).join('');
    return `${head}<div class="card"><h2>OOS 요약</h2><dl class="kv"><dt>Fold</dt><dd>${m.folds} (실패 ${m.folds_failed})</dd><dt>이어붙인 OOS 수익률</dt><dd>${pct(m.oos_stitched_return)}</dd>
      <dt>평균 · 중앙값</dt><dd>${pct(m.oos_mean_return)} · ${pct(m.oos_median_return)}</dd><dt>양(+) Fold</dt><dd>${m.oos_positive_folds} / ${m.folds}</dd>
      <dt>Lockbox</dt><dd>${esc((man.walkforward || {}).lockbox_status)} ${esc(JSON.stringify((man.walkforward || {}).lockbox_range))}</dd></dl>
      ${(m.notes || []).length ? `<div class="notice">${m.notes.map(esc).join('<br>')}</div>` : ''}
      <div class="table-wrap"><table><thead><tr><th>#</th><th>Train 봉</th><th>OOS 봉</th><th>상태</th><th>선택 파라미터(Train)</th><th class="num">OOS Net</th><th class="num">청산</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="hint">파라미터는 각 Fold의 Train 구간 성과로만 선택하고, OOS 구간은 한 번만 평가합니다. Fold마다 초기자본에서 새로 시작합니다.</p></div>`;
  }
  if (man.kind === 'stress') {
    const rows = m.rows.map(r => `<tr><td class="num">×${esc(r.cost_multiplier)}</td><td class="num">${pct(r.net_return, { label: false })}</td><td class="num">${esc(isNum(r.explicit_costs) ? r.explicit_costs.toFixed(0) : '—')}</td><td class="num">${r.fills}</td><td class="num">${r.trades_closed}</td><td class="num">${r.rejections}</td></tr>`).join('');
    return `${head}<div class="card"><h2>비용 스트레스</h2><div class="table-wrap"><table><thead><tr><th class="num">비용 배수</th><th class="num">Net 수익률</th><th class="num">명시 비용</th><th class="num">체결</th><th class="num">청산</th><th class="num">거절</th></tr></thead><tbody>${rows}</tbody></table></div><p class="hint">${esc(m.note)}</p></div>`;
  }
  const trades = (res.trades || []).slice(-50).reverse().map(t => `<tr><td class="small">${esc((t.entry_ts || '').slice(0, 10))}</td><td class="small">${esc((t.exit_ts || '미청산').slice(0, 10))}</td><td>${badge(t.status === 'CLOSED' ? 'OK' : 'UNKNOWN', t.status === 'CLOSED' ? '청산' : '보유중')}</td><td class="num">${pnl(t.net_pnl, m.currency)}</td><td class="num">${esc(t.costs.toFixed(0))}</td></tr>`).join('');
  const stages = Object.entries(m.rejections_by_stage || {}).map(([k, v]) => `<span class="badge b-block">${esc(k)} ${v}</span>`).join('');
  return `${head}<div class="card"><h2>성과 지표 (${esc(m.currency)})</h2>${metricsGrid(m)}</div>
    <div class="card"><h2>평가 곡선</h2>${chart(res.equity, m.starting_equity, '백테스트 평가액 곡선')}</div>
    <div class="grid two"><div class="card"><h2>거래 (최근 50)</h2>${trades ? `<div class="table-wrap"><table><thead><tr><th>진입</th><th>청산</th><th>상태</th><th class="num">Net</th><th class="num">비용</th></tr></thead><tbody>${trades}</tbody></table></div>` : '<div class="empty">거래 없음</div>'}</div>
    <div class="card"><h2>Gate 거절 · 종료 상태</h2><div class="pill-list">${stages || '<span class="badge b-pass">거절 없음</span>'}</div>
      <dl class="kv"><dt>종료 정책</dt><dd>${esc(res.end_state.policy)} (청산하지 않고 종가 평가)</dd><dt>미청산 수량</dt><dd>${qty(res.end_state.open_position_qty)}</dd>
      <dt>미실현</dt><dd>${pnl(res.end_state.open_position_unrealized, m.currency)}</dd><dt>잔여 예약</dt><dd>${money(res.end_state.reserved_cash, m.currency)}</dd>
      <dt>예약 초과</dt><dd>${res.end_state.reservation_breaches ? badge('BLOCK', `${res.end_state.reservation_breaches}건 — 이후 주문 차단`) : '없음'}</dd></dl>
      ${(res.rejections || []).slice(0, 5).map(r => `<p class="small wrap">봉 ${r.bar} · ${esc(r.action)} · ${esc(r.reason)}</p>`).join('')}</div></div>`;
}
function paramFields(name) {
  const def = (state.settings.strategies[name] || {}).defaults || {};
  return Object.entries(def).map(([k, v]) => `<div class="field"><label for="p-${esc(k)}">${esc(k)}</label><input id="p-${esc(k)}" data-param="${esc(k)}" inputmode="decimal" value="${esc(v)}"></div>`).join('');
}
async function viewResearch(runId) {
  loading('연구');
  const [datasets, runs, settings] = await Promise.all([api('/api/datasets'), api('/api/runs'), state.settings ? Promise.resolve(state.settings) : api('/api/settings')]);
  state.settings = settings; state.datasets = datasets;
  if (!state.selectedDataset || !datasets.find(d => d.id === state.selectedDataset)) state.selectedDataset = (datasets.find(d => d.validation_status !== 'FAIL') || {}).id;
  const ds = datasets.find(d => d.id === state.selectedDataset);
  const dsList = datasets.map(d => `<li><label class="check"><input type="radio" name="ds" value="${esc(d.id)}" aria-label="${esc(d.meta ? `${d.meta.symbol} ${d.meta.market} ${d.meta.timeframe} ${d.validation_status}` : d.id)}" ${d.id === state.selectedDataset ? 'checked' : ''}>
      <span class="wrap"><strong>${esc(d.meta ? d.meta.symbol : d.id)}</strong> · ${esc(d.meta ? `${d.meta.market} ${d.meta.timeframe}` : '')} ${badge(d.validation_status)} ${d.meta && d.meta.synthetic ? '<span class="badge b-unknown">합성</span>' : ''}</span></label></li>`).join('');
  const dsDetail = ds ? `<dl class="kv"><dt>경로</dt><dd>${esc(ds.id)}</dd><dt>data_version</dt><dd>${esc(ds.data_version)}</dd><dt>행 · 유효</dt><dd>${ds.rows} · ${ds.valid_rows}</dd>
      <dt>기간</dt><dd>${when(ds.first_ts)} → ${when(ds.last_ts)}</dd><dt>시간 기준</dt><dd>${esc(ds.meta.timezone)} · 라벨 ${esc(ds.meta.timestamp_label)} · 봉 시가시각(UTC)으로 정규화</dd>
      <dt>통화</dt><dd>${esc(ds.currency)}</dd><dt>공백</dt><dd>${esc(JSON.stringify(ds.gap_summary))}</dd></dl>
      <p class="hint">${esc(ds.calendar_note)}</p>
      ${ds.issues.filter(i => i.severity !== 'INFO').map(i => `<p class="small">${badge(i.severity === 'ERROR' ? 'FAIL' : 'WARN', i.code)} ${i.count}건 <span class="muted wrap">${esc((i.examples[0] || {}).detail || '')}</span></p>`).join('')}` : (ds && ds.error ? errorCard(ds.error) : '<div class="empty">데이터셋이 없습니다. data/raw에 CSV와 .meta.json을 두세요.</div>');
  const strategies = Object.keys(settings.strategies);
  const curStrategy = state.strategy || strategies[0];
  const runRows = runs.map(r => `<tr><td><input type="checkbox" aria-label="비교 선택 ${esc(r.run_id)}" data-compare="${esc(r.run_id)}" ${state.compare.has(r.run_id) ? 'checked' : ''}></td>
      <td><a href="#/research/${esc(r.run_id)}">${esc(KIND_LABEL[r.kind] || r.kind)}</a><br><span class="small muted">${esc((r.created_utc || '').slice(0, 16))}</span></td>
      <td>${esc(STRATEGY_LABEL[r.strategy] || r.strategy || '-')}<br><span class="small muted wrap">${esc(r.symbol || '')} ${r.synthetic ? '· 합성' : ''}</span></td><td class="num">${pct(r.net_return, { label: false })}</td></tr>`).join('');
  view.innerHTML = `<h1>연구</h1><p class="lede">Historical Data → Backtest → Walk-Forward. 결과는 저장되어 다시 열 수 있습니다. 합성 데이터 결과는 실제 시장 성과가 아닙니다.</p>
    <div class="grid two">
      <div class="card"><h2>데이터 선택·검증</h2><ul class="list" id="ds-list">${dsList || '<li class="empty">데이터셋 없음</li>'}</ul><h3>검증 결과</h3>${dsDetail}</div>
      <div class="card"><h2>실행 설정</h2>
        <div class="seg" role="group" aria-label="실행 종류">${['backtest', 'walkforward', 'stress'].map(t => `<button type="button" data-rt="${t}" aria-pressed="${state.runType === t}">${KIND_LABEL[t]}</button>`).join('')}</div>
        <form id="run-form" class="form-grid" novalidate>
          <div class="field full"><label for="rf-strategy">전략</label><select id="rf-strategy">${strategies.map(s => `<option value="${esc(s)}" ${s === curStrategy ? 'selected' : ''}>${esc(STRATEGY_LABEL[s] || s)}</option>`).join('')}</select></div>
          <div id="rf-params" class="form-grid full">${paramFields(curStrategy)}</div>
          <div class="field"><label for="rf-alpha">기대수익 근거</label><select id="rf-alpha"><option value="empirical">과거 사례 평균 (인과적)</option><option value="fixture">FIXTURE 값 (검증용)</option></select></div>
          <div class="field"><label for="rf-fix">FIXTURE 기대수익</label><input id="rf-fix" inputmode="decimal" value="0.01" disabled><span class="hint">검증 전용. 실제 Alpha 추정으로 표시되지 않습니다.</span></div>
          <div class="field"><label for="rf-cash">초기 자본 (${esc(ds ? ds.currency : '')})</label><input id="rf-cash" inputmode="decimal" value="${ds && ds.currency === 'USD' ? 10000 : (ds && ds.meta && ds.meta.market === 'CRYPTO' ? 100000000 : 10000000)}"></div>
          <div class="field"><label for="rf-cost">비용 배수</label><input id="rf-cost" inputmode="decimal" value="1"></div>
          <div class="field"><label for="rf-frac">포지션 비중</label><input id="rf-frac" inputmode="decimal" value="0.95"></div>
          <div class="field"><label for="rf-buf">예약 버퍼</label><input id="rf-buf" inputmode="decimal" value="0.02"><span class="hint">시장가 BUY 예약 여유. 기본 2%는 잠정 정책값(경험적 검증 없음). 초과 체결은 Breach로 기록·차단</span></div>
          <div class="field wf"><label for="rf-train">Train 봉</label><input id="rf-train" inputmode="numeric" value="250"></div>
          <div class="field wf"><label for="rf-test">OOS 봉</label><input id="rf-test" inputmode="numeric" value="60"></div>
          <div class="field wf"><label for="rf-lock">Lockbox 봉</label><input id="rf-lock" inputmode="numeric" value="100"></div>
          <div class="field st"><label for="rf-mults">비용 배수 목록</label><input id="rf-mults" value="1,2,3"></div>
          <div class="field full"><button class="primary" type="submit" id="rf-run" ${ds && ds.validation_status !== 'FAIL' ? '' : 'disabled'}>실행</button><span class="hint">위험·비용 수치는 settings.yaml의 placeholder를 사용합니다.</span></div>
        </form></div>
    </div>
    <div class="card"><div class="card-head"><h2>저장된 실행 (${runs.length})</h2><button id="cmp-btn" ${state.compare.size >= 2 ? '' : 'disabled'}>선택 비교 (${state.compare.size})</button></div>
      ${runs.length ? `<div class="table-wrap"><table><thead><tr><th>비교</th><th>종류</th><th>전략·종목</th><th class="num">Net</th></tr></thead><tbody>${runRows}</tbody></table></div>` : '<div class="empty">아직 실행한 연구가 없습니다.</div>'}</div>
    <div id="compare-area"></div><div id="run-detail" class="stack"></div>`;

  const syncType = () => {
    view.querySelectorAll('[data-rt]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.rt === state.runType)));
    view.querySelectorAll('.wf').forEach(el => { el.hidden = state.runType !== 'walkforward'; });
    view.querySelectorAll('.st').forEach(el => { el.hidden = state.runType !== 'stress'; });
  };
  syncType();
  view.querySelectorAll('[data-rt]').forEach(b => b.addEventListener('click', () => { state.runType = b.dataset.rt; syncType(); }));
  view.querySelectorAll('input[name="ds"]').forEach(r => r.addEventListener('change', () => { state.selectedDataset = r.value; render(); }));
  const strat = document.getElementById('rf-strategy');
  strat.addEventListener('change', () => { state.strategy = strat.value; document.getElementById('rf-params').innerHTML = paramFields(strat.value); });
  const alpha = document.getElementById('rf-alpha');
  alpha.addEventListener('change', () => { document.getElementById('rf-fix').disabled = alpha.value !== 'fixture'; });
  document.getElementById('run-form').addEventListener('submit', async e => {
    e.preventDefault();
    const btn = document.getElementById('rf-run');
    if (btn.disabled) return;
    btn.disabled = true; btn.textContent = '실행 중…';
    const params = {};
    view.querySelectorAll('[data-param]').forEach(i => { params[i.dataset.param] = Number(i.value); });
    params.alpha_mode = alpha.value;
    if (alpha.value === 'fixture') params.fixture_expected_return = Number(document.getElementById('rf-fix').value);
    const body = { dataset_id: state.selectedDataset, strategy: strat.value, params,
      initial_cash: Number(document.getElementById('rf-cash').value), cost_multiplier: Number(document.getElementById('rf-cost').value),
      position_fraction: Number(document.getElementById('rf-frac').value), reservation_buffer_pct: Number(document.getElementById('rf-buf').value) };
    if (state.runType === 'walkforward') Object.assign(body, { train_bars: Number(document.getElementById('rf-train').value), test_bars: Number(document.getElementById('rf-test').value), lockbox_bars: Number(document.getElementById('rf-lock').value) });
    if (state.runType === 'stress') body.multipliers = document.getElementById('rf-mults').value.split(',').map(Number);
    try {
      const out = await post(`/api/research/${state.runType}`, body);
      toast('실행 완료 · 결과를 저장했습니다.');
      location.hash = `#/research/${out.run_id}`;
    } catch (err) { toast(`실행 실패: ${err.message}`); btn.disabled = false; btn.textContent = '실행'; }
  });
  view.querySelectorAll('[data-compare]').forEach(c => c.addEventListener('change', () => {
    if (c.checked) state.compare.add(c.dataset.compare); else state.compare.delete(c.dataset.compare);
    const b = document.getElementById('cmp-btn'); b.disabled = state.compare.size < 2; b.textContent = `선택 비교 (${state.compare.size})`;
  }));
  document.getElementById('cmp-btn').addEventListener('click', async () => {
    const ids = [...state.compare].slice(0, 4);
    const details = await Promise.all(ids.map(id => api(`/api/runs/${encodeURIComponent(id)}`).catch(err => ({ error: err.message, id }))));
    const cols = details.map(d => d.error ? `<th>${esc(d.id)} (오류)</th>` : `<th class="num">${esc(KIND_LABEL[d.manifest.kind])}<br><span class="small muted">${esc(STRATEGY_LABEL[(d.manifest.strategy || {}).name] || '')}</span></th>`).join('');
    const rowOf = (label, fn) => `<tr><td>${label}</td>${details.map(d => `<td class="num">${d.error ? '—' : fn(d)}</td>`).join('')}</tr>`;
    document.getElementById('compare-area').innerHTML = `<div class="card"><h2>실행 비교</h2><div class="table-wrap"><table><thead><tr><th>지표</th>${cols}</tr></thead><tbody>
      ${rowOf('데이터', d => `${esc(d.manifest.symbol)} ${(d.manifest.labels || {}).synthetic_data ? '(합성)' : ''}`)}
      ${rowOf('Net 수익률', d => pct(d.metrics.net_return, { label: false }))}
      ${rowOf('Max DD', d => isNum(d.metrics.max_drawdown_pct) ? `−${(d.metrics.max_drawdown_pct * 100).toFixed(2)}%` : na('해당 없음'))}
      ${rowOf('청산 거래', d => d.metrics.trades_closed ?? d.metrics.oos_trades_closed ?? '—')}
      ${rowOf('Profit Factor', d => isNum(d.metrics.profit_factor) ? ratio(d.metrics.profit_factor) : na('정의 불가'))}
      ${rowOf('명시 비용', d => isNum(d.metrics.explicit_costs) ? esc(d.metrics.explicit_costs.toFixed(0)) : '—')}
      ${rowOf('기대수익 근거', d => esc((d.manifest.labels || {}).alpha_mode || ((d.manifest.strategy || {}).params || {}).alpha_mode || 'empirical'))}
    </tbody></table></div><p class="hint">서로 다른 종목·기간·통화의 결과는 직접 비교할 수 없습니다.</p></div>`;
  });
  if (runId) {
    const area = document.getElementById('run-detail');
    area.innerHTML = '<div class="card"><div class="skeleton tall"></div></div>';
    try { area.innerHTML = runDetailHTML(await api(`/api/runs/${encodeURIComponent(runId)}`)); area.scrollIntoView({ block: 'start' }); }
    catch (err) { area.innerHTML = errorCard(err); }
  }
}

/* -------------------------------------------------------------- settings */
async function viewSettings() {
  loading('설정·실험');
  const s = await api('/api/settings');
  state.settings = s;
  const costRows = Object.entries(s.costs || {}).map(([mk, c]) => `<tr><td>${esc(mk)}</td><td class="num">${esc(c.commission_rate)}</td><td class="num">${esc(c.tax_rate_sell)}</td><td class="num">${esc(c.half_spread_bps)}</td><td class="num">${esc(c.slippage_bps)}</td><td class="num">${esc(c.fx_cost_bps)}</td></tr>`).join('');
  const riskRows = Object.entries(s.risk || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v === null ? '<span class="badge b-unknown">미설정 (null)</span>' : `<span class="num">${esc(v)}</span>`}</td></tr>`).join('');
  view.innerHTML = `<h1>설정·실험 정보</h1>
    ${s.settings_ok ? '' : `<div class="error-box" role="alert">연구 설정을 읽을 수 없습니다: ${esc(s.settings_error)}</div>`}
    <div class="notice"><strong>PLACEHOLDER</strong> — ${esc(s.placeholder_notice)}</div>
    <div class="grid two">
      <div class="card"><h2>설정 파일</h2><dl class="kv"><dt>경로</dt><dd>${esc((s.meta || {}).path)}</dd><dt>SHA-256</dt><dd>${esc(((s.meta || {}).sha256 || '').slice(0, 24))}…</dd>
        <dt>project.mode</dt><dd>${esc((s.project || {}).mode)}</dd><dt>live_enabled</dt><dd>${esc((s.execution || {}).live_enabled)} <span class="small muted">(true여도 Live 불가)</span></dd>
        <dt>기준통화</dt><dd>${esc(s.base_currency)}</dd><dt>허용 시장</dt><dd>${esc(JSON.stringify(s.markets))}</dd>
        <dt>Net Alpha 최소</dt><dd>${esc((s.net_alpha || {}).min_net_alpha_bps)} bps</dd></dl></div>
      <div class="card"><h2>코드·재현성</h2><dl class="kv"><dt>code_commit</dt><dd>${esc(s.code.code_commit)}</dd><dt>git dirty</dt><dd>${esc(s.code.git_dirty)}</dd>
        <dt>source tree SHA-256</dt><dd>${esc(s.code.source_tree_sha256.slice(0, 24))}…</dd><dt>qat 버전</dt><dd>${esc(s.code.qat_version)}</dd><dt>Python</dt><dd>${esc(s.code.python)}</dd></dl>
        <p class="hint">Git 정보가 없으면 만들어내지 않고 unavailable로 표시하며, 소스 트리 해시로 식별합니다.</p></div>
    </div>
    <div class="grid two">
      <div class="card"><h2>비용 모델 (placeholder)</h2><div class="table-wrap"><table><thead><tr><th>시장</th><th class="num">수수료율</th><th class="num">매도세율</th><th class="num">½스프레드bp</th><th class="num">슬리피지bp</th><th class="num">FX bp</th></tr></thead><tbody>${costRows}</tbody></table></div>
        <p class="hint">Paper 체결은 시장가 기준 ½스프레드+슬리피지를 가격에 반영합니다. FX bp는 현재 Gate 추정에만 쓰이고 체결 비용으로는 부과하지 않습니다.</p></div>
      <div class="card"><h2>Risk 한도</h2><div class="table-wrap"><table><tbody>${riskRows}</tbody></table></div>
        <h3>환율 (placeholder)</h3><p class="small">${esc(JSON.stringify((s.fx || {}).rates || []))}</p></div>
    </div>
    <div class="grid two">
      <div class="card"><h2>전략 (기준전략)</h2><ul class="list">${Object.entries(s.strategies).map(([k, v]) => `<li><strong>${esc(STRATEGY_LABEL[k] || k)}</strong> <span class="small muted">v${esc(v.version)}</span><div class="small wrap">기본 ${esc(JSON.stringify(v.defaults))} · 그리드 ${esc(JSON.stringify(v.grid))}</div></li>`).join('')}</ul>
        <p class="hint">수익 보장 전략이 아니라 연구 파이프라인 검증용 기준전략입니다.</p></div>
      <div class="card"><h2>아직 결정되지 않은 항목 (UNKNOWN)</h2><div class="pill-list">${s.unknowns.map(u => `<span class="badge b-unknown">${esc(u)}</span>`).join('')}</div></div>
    </div>`;
}

/* ------------------------------------------------- operations / audit / recovery */
const HEALTH_CLASS = { PASS: 'b-pass', HEALTHY: 'b-pass', BLOCKED: 'b-block', UNHEALTHY: 'b-block', DEGRADED: 'b-unknown', UNKNOWN: 'b-unknown', NOT_CONFIGURED: 'b-muted',
  NOT_READY: 'b-unknown', GRADUATED: 'b-pass', WARN: 'b-unknown', BLOCK: 'b-block', FAIL: 'b-block', NORMAL: 'b-pass', RECOVERY_REQUIRED: 'b-block', FLATTEN_SUBMITTED: 'b-unknown', FLATTENED: 'b-pass' };
const HEALTH_LABEL = { PASS: '정상', HEALTHY: '정상', BLOCKED: '차단', UNHEALTHY: '비정상', DEGRADED: '주의', UNKNOWN: '알 수 없음', NOT_CONFIGURED: '미설정',
  NOT_READY: '준비 안 됨', GRADUATED: '졸업', WARN: '경고', BLOCK: '차단', FAIL: '미충족', NORMAL: '정상', RECOVERY_REQUIRED: '복구 필요', FLATTEN_SUBMITTED: '청산 제출됨', FLATTENED: '청산 완료' };
function healthBadge(status, label) {
  const s = String(status || 'UNKNOWN').toUpperCase();
  const cls = HEALTH_CLASS[s] || 'b-muted';
  const text = `${label ? label + ': ' : ''}${s} (${HEALTH_LABEL[s] || s})`;
  return `<span class="badge ${cls}">${esc(text)}</span>`;
}
function opsSubnav(active) {
  const items = [['', '상태·준비도'], ['audit', '감사 로그'], ['recovery', '복구']];
  return `<nav class="subnav" aria-label="운영 하위 화면">${items.map(([k, t]) => `<a href="#/operations${k ? '/' + k : ''}" ${k === active ? 'aria-current="page"' : ''}>${t}</a>`).join('')}</nav>`;
}
async function viewOperations(sub) {
  if (sub === 'audit') return viewAudit();
  if (sub === 'recovery') return viewRecovery();
  loading('운영 상태');
  const o = await api('/api/operations');
  const h = o.health;
  const comps = h.components.map(c => `<li><div class="comp-name"><span>${esc(c.name)}</span>${healthBadge(c.status, '')}${c.informational ? '<span class="small muted">참고 항목</span>' : ''}</div>
      <div class="reasons">${c.reasons.length ? c.reasons.map(esc).join('<br>') : '—'}</div></li>`).join('');
  const checks = o.startup.checks.map(c => `<tr><td class="wrap">${esc(c.id)}</td><td>${healthBadge(c.status, '')}</td><td class="small wrap">${esc(c.detail)}</td></tr>`).join('');
  const gates = o.paper_graduation.gates.map(g => `<tr><td>${esc(g.id)}</td><td class="wrap">${esc(g.name)}</td><td>${healthBadge(g.status === 'PASS' ? 'PASS' : g.status, '')}</td><td class="small wrap">${esc(g.detail)}</td></tr>`).join('');
  const sh = o.strategy_health;
  const live = o.live_readiness;
  view.innerHTML = `<h1>운영</h1>${opsSubnav('')}
    <p class="lede">시스템 구성요소별 운영 상태입니다. 가장 심각한 항목이 먼저 보입니다. 색만이 아니라 글자로도 상태를 표시합니다.</p>
    <div class="card"><div class="health-overall"><span class="big">종합</span>${healthBadge(h.overall, '')}</div><p class="hint">${esc(h.note)}</p></div>
    <div class="notice info"><strong>완료 범위</strong> — ${esc(o.completion_scope)}</div>
    <div class="grid two">
      <div class="card"><h2>구성요소 (심각한 순)</h2><ul class="comp-list">${comps}</ul></div>
      <div class="stack">
        <div class="card"><div class="card-head"><h2>Live 준비도</h2>${healthBadge(live.status, '')}</div>
          <p class="notice block">Live는 이 빌드에서 항상 BLOCKED입니다. UI·설정·증거 입력으로 켤 수 없습니다.</p>
          <ul class="list">${live.blockers.map(b => `<li class="small wrap">${esc(b)}</li>`).join('')}</ul><p class="hint">${esc(live.unblock_path)}</p></div>
        <div class="card"><div class="card-head"><h2>Paper 졸업 평가</h2>${healthBadge(o.paper_graduation.status, '')}</div>
          <p class="hint">${esc(o.paper_graduation.scope)}</p><p class="hint">${esc(o.paper_graduation.criteria_note)}</p>
          <div class="table-wrap"><table><thead><tr><th>ID</th><th>조건</th><th>결과</th><th>근거</th></tr></thead><tbody>${gates}</tbody></table></div></div>
      </div>
    </div>
    <div class="grid two">
      <div class="card"><div class="card-head"><h2>Strategy Health</h2>${healthBadge(sh ? sh.status : 'UNKNOWN', '')}</div>
        ${sh ? `<p class="small wrap">${sh.reasons.length ? sh.reasons.map(esc).join('<br>') : '이상 없음'}</p><p class="hint">${sh.notes.map(esc).join(' ')} ${esc(sh.scope)}</p>
        <dl class="kv"><dt>평가 횟수</dt><dd>${esc(sh.metrics.evaluations)}</dd><dt>신호 수</dt><dd>${esc(sh.metrics.signals)}</dd><dt>제안 수</dt><dd>${esc(sh.metrics.proposals)}</dd><dt>연속 오류</dt><dd>${esc(sh.metrics.consecutive_errors)}</dd></dl>`
          : '<div class="empty">세션이 없어 관찰된 내용이 없습니다 (UNKNOWN).</div>'}
        <p class="hint">전략 성과가 아니라 운영 상태만 봅니다. 거래가 없는 것은 실패가 아닙니다. 이 화면은 전략을 바꾸거나 끄지 않습니다.</p></div>
      <div class="card"><h2>시작 점검 (${esc(o.startup.status)})</h2><div class="table-wrap"><table><thead><tr><th>점검</th><th>결과</th><th>내용</th></tr></thead><tbody>${checks}</tbody></table></div></div>
    </div>
    <div class="grid two">
      <div class="card"><h2>Snapshot 신선도 정책</h2><dl class="kv"><dt>정책</dt><dd>${esc(o.snapshot_policy.policy)}</dd><dt>최대 허용 나이</dt><dd>${o.snapshot_policy.max_age_seconds === null ? '해당 없음' : esc(o.snapshot_policy.max_age_seconds) + '초'}</dd></dl><p class="hint">${esc(o.snapshot_policy.note)}</p></div>
      <div class="card"><h2>상태 저장</h2><dl class="kv"><dt>쓰기 방식</dt><dd>${esc(o.persistence.writes)}</dd><dt>프로세스</dt><dd>${esc(o.persistence.process_model)}</dd><dt>저장 오류</dt><dd>${o.persistence.state_fault ? healthBadge('BLOCKED', '') + ' ' + esc(o.persistence.state_fault) : '없음'}</dd></dl></div>
    </div>`;
}
const auditState = { offset: 0, limit: 25, event_type: '', session_uid: '', order: 'desc' };
async function viewAudit() {
  loading('감사 로그');
  const q = new URLSearchParams({ offset: auditState.offset, limit: auditState.limit, order: auditState.order });
  if (auditState.event_type) q.set('event_type', auditState.event_type);
  if (auditState.session_uid) q.set('session_uid', auditState.session_uid);
  const a = await api(`/api/audit?${q.toString()}`);
  const rows = a.events.map(e => `<tr><td class="num">${esc(e.seq)}</td><td>${when(e.timestamp)}</td><td class="wrap">${esc(e.event_type)}</td><td class="wrap small">${esc(e.actor || '—')}</td>
      <td class="wrap small">${esc(e.order_id || e.proposal_id || '—')}</td><td>${e.gate ? healthBadge(e.gate.status === 'PASS' ? 'PASS' : (e.gate.status === 'BLOCK' ? 'BLOCK' : 'UNKNOWN'), e.gate.stage) : '—'}</td>
      <td class="mono">${esc(String(e.hash || '').slice(0, 10))}…</td></tr>
      <tr><td></td><td colspan="6"><details><summary>상세 보기 (#${esc(e.seq)})</summary><pre class="code">${esc(JSON.stringify(e, null, 1))}</pre></details></td></tr>`).join('');
  const from = a.total ? a.offset + 1 : 0, to = Math.min(a.offset + a.limit, a.total);
  view.innerHTML = `<h1>감사 로그</h1>${opsSubnav('audit')}
    <div class="card"><div class="card-head"><h2>무결성</h2>${a.verify.ok ? healthBadge('PASS', '해시 체인 검증') : healthBadge('BLOCKED', '검증 실패')}</div>
      <dl class="kv"><dt>기록 수</dt><dd>${esc(a.verify.records)}</dd><dt>마지막 해시</dt><dd class="mono">${esc(String(a.verify.last_hash || '').slice(0, 24))}…</dd><dt>쓰기 상태</dt><dd>${a.healthy ? '정상' : healthBadge('BLOCKED', 'FAULT') + ' ' + esc(a.fault)}</dd></dl>
      ${a.error ? `<div class="error-box" role="alert">${esc(a.error)}<br>${(a.verify.errors || []).map(esc).join('<br>')}</div>` : ''}
      <p class="hint">읽기 전용입니다. 추가 전용(append-only) JSONL이며 비밀값(키·토큰)은 저장 전에 가려집니다. 이 화면에서 수정·삭제할 수 없습니다.</p></div>
    <div class="card"><h2>필터</h2><form id="audit-filter" class="filter-row">
      <div class="field"><label for="af-type">이벤트 종류</label><input id="af-type" maxlength="60" value="${esc(auditState.event_type)}" placeholder="예: fill_settled"></div>
      <div class="field"><label for="af-session">세션 UID</label><input id="af-session" maxlength="80" value="${esc(auditState.session_uid)}"></div>
      <div class="field"><label for="af-order">정렬</label><select id="af-order"><option value="desc" ${auditState.order === 'desc' ? 'selected' : ''}>최신순</option><option value="asc" ${auditState.order === 'asc' ? 'selected' : ''}>오래된순</option></select></div>
      <div class="field"><label for="af-limit">페이지 크기</label><select id="af-limit">${[10, 25, 50, 100].map(n => `<option ${n === auditState.limit ? 'selected' : ''}>${n}</option>`).join('')}</select></div>
      <div class="field full"><button class="primary" type="submit">적용</button></div></form></div>
    <div class="card"><div class="card-head"><h2>이벤트 (${esc(a.total)}건)</h2><span class="small muted num">${from}–${to}</span></div>
      ${a.events.length ? `<div class="table-wrap"><table><thead><tr><th class="num">#</th><th>시각</th><th>종류</th><th>주체</th><th>주문·제안</th><th>Gate</th><th>해시</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="empty">표시할 이벤트가 없습니다.</div>'}
      <div class="pager"><button id="ap-prev" ${a.offset <= 0 ? 'disabled' : ''}>이전</button><span class="small muted">${esc(a.offset)} / ${esc(a.total)}</span><button id="ap-next" ${a.offset + a.limit >= a.total ? 'disabled' : ''}>다음</button></div></div>`;
  document.getElementById('audit-filter').addEventListener('submit', e => {
    e.preventDefault();
    auditState.event_type = document.getElementById('af-type').value.trim();
    auditState.session_uid = document.getElementById('af-session').value.trim();
    auditState.order = document.getElementById('af-order').value;
    auditState.limit = Number(document.getElementById('af-limit').value);
    auditState.offset = 0;
    render();
  });
  document.getElementById('ap-prev').addEventListener('click', () => { auditState.offset = Math.max(0, auditState.offset - auditState.limit); render(); });
  document.getElementById('ap-next').addEventListener('click', () => { auditState.offset += auditState.limit; render(); });
}
async function viewRecovery() {
  loading('복구');
  const r = await api('/api/recovery');
  const reasons = (r.restart.reasons || []).map(x => `<li class="small wrap">${esc(x)}</li>`).join('');
  const blockers = r.assessment ? r.assessment.blockers.map(x => `<li class="small wrap">${esc(x)}</li>`).join('') : '';
  const positions = r.assessment ? r.assessment.positions : [];
  const history = ((r.state_detail || {}).history || []).slice().reverse().map(x => `<li class="small wrap">${healthBadge(x.status, '')} ${when(x.utc)} ${esc(x.note || '')} ${x.operator ? `· ${esc(x.operator)}` : ''}</li>`).join('');
  const flat = (r.state_detail || {}).flatten;
  view.innerHTML = `<h1>복구</h1>${opsSubnav('recovery')}
    <div class="card"><div class="card-head"><h2>복구 상태</h2>${healthBadge(r.state, '')}</div>
      <dl class="kv"><dt>Kill Switch</dt><dd>${r.kill_switch ? healthBadge('BLOCKED', 'ON') : healthBadge('PASS', 'OFF')}</dd>
      <dt>재시작 평가</dt><dd>${healthBadge(r.restart.state === 'MANUAL_INTERVENTION_REQUIRED' ? 'BLOCKED' : (r.restart.state === 'SAFE_RECOVERY' ? 'PASS' : 'UNKNOWN'), r.restart.state)}</dd>
      <dt>운영자 확인</dt><dd>${r.restart.acknowledged ? `확인됨 (${esc(r.restart.acknowledged_by || '')})` : '아직 없음'}</dd></dl>
      ${reasons ? `<h3>재시작 평가 사유</h3><ul class="list">${reasons}</ul>` : ''}
      ${(r.restart.notes || []).length ? `<p class="hint">${r.restart.notes.map(esc).join(' ')}</p>` : ''}
      <p class="hint">복구 필요 상태의 해제는 오프라인 운영자 작업입니다: <span class="mono">python -m qat.ui ack-recovery --approver 이름 --note 사유</span>. UI에는 해제 기능이 없습니다.</p></div>
    <div class="grid two">
      <div class="card"><h2>긴급 청산 (운영자 전용)</h2>
        <ul class="list">${r.policy.map(p => `<li class="small wrap">${esc(p)}</li>`).join('')}</ul>
        ${r.assessment ? `<h3>현재 보유</h3>${positions.length ? `<ul class="list">${positions.map(p => `<li class="spread"><span class="wrap">${esc(p.market)}:${esc(p.symbol)}</span><span class="num">${qty(p.quantity)}</span></li>`).join('')}</ul>` : '<div class="empty">청산할 보유 포지션이 없습니다.</div>'}
          ${blockers ? `<div class="notice block"><strong>청산 불가 — 수동 개입 필요</strong><ul class="list">${blockers}</ul></div>` : ''}`
          : '<div class="empty">Paper 세션이 없습니다.</div>'}
        <form id="flatten-form" class="stack" novalidate>
          <div class="field"><label for="ff-op">운영자 이름</label><input id="ff-op" maxlength="80" autocomplete="off"></div>
          <div class="field"><label for="ff-reason">사유</label><input id="ff-reason" maxlength="200" autocomplete="off"></div>
          <div class="field"><label for="ff-confirm">확인 문구 (${esc(r.confirm_token)} 를 그대로 입력)</label><input id="ff-confirm" autocomplete="off" aria-describedby="ff-hint"></div>
          <span class="hint" id="ff-hint">보유 수량만큼만 매도합니다(초과·반대 포지션 없음). Kill Switch는 해제되지 않으며 체결은 다음 재생 봉 시가에 이뤄집니다.</span>
          <button class="danger" type="submit" ${r.flatten_available ? '' : 'disabled'}>긴급 청산 실행</button></form>
        ${flat ? `<h3>마지막 청산 기록</h3><dl class="kv"><dt>운영자</dt><dd>${esc(flat.operator)}</dd><dt>사유</dt><dd class="wrap">${esc(flat.reason)}</dd><dt>주문</dt><dd>${esc((flat.orders || []).length)}건</dd><dt>요청 시각</dt><dd>${when(flat.requested_utc)}</dd></dl>` : ''}</div>
      <div class="card"><h2>상태 이력</h2>${history ? `<ul class="list">${history}</ul>` : '<div class="empty">기록 없음</div>'}</div>
    </div>`;
  document.getElementById('flatten-form').addEventListener('submit', async e => {
    e.preventDefault();
    const body = { operator: document.getElementById('ff-op').value.trim(), reason: document.getElementById('ff-reason').value.trim(), confirm: document.getElementById('ff-confirm').value };
    if (!body.operator || !body.reason) { toast('운영자 이름과 사유를 입력하세요.'); return; }
    if (body.confirm !== r.confirm_token) { toast(`확인 문구 ${r.confirm_token} 를 그대로 입력하세요.`); return; }
    if (!window.confirm('보유 포지션을 전량 매도합니다. 계속할까요?')) return;
    try { const out = await post('/api/recovery/flatten', body); toast(`긴급 청산 ${out.state}: 주문 ${(out.orders || []).length}건`); }
    catch (err) { toast(`거절됨: ${err.message}`); }
    render();
  });
}

/* ---------------------------------------------------------------- router */
const ROUTES = { overview: viewOverview, portfolio: viewPortfolio, research: viewResearch, orders: viewOrders, operations: viewOperations, risk: viewRisk, settings: viewSettings };
async function render() {
  const parts = (location.hash.replace(/^#\/?/, '') || 'overview').split('/');
  const route = ROUTES[parts[0]] ? parts[0] : 'overview';
  document.querySelectorAll('.nav a').forEach(a => { if (a.dataset.route === route) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
  refreshTop();
  try { await ROUTES[route](parts[1] ? decodeURIComponent(parts[1]) : undefined); }
  catch (err) { view.innerHTML = `<h1>오류</h1>${errorCard(err)}`; }
}
window.addEventListener('hashchange', () => { render(); view.focus({ preventScroll: true }); });
render();
