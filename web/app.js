// 共享工具：请求封装、轮询、格式化、图表工厂。

const TRACK_LABEL = { backend: '后端/服务端', ai: '算法/AI', other: '其他' };
const RECRUIT_LABEL = { campus: '校招', intern: '实习', social: '社招' };
const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (res.status === 204) return null;
  let body = null;
  try { body = await res.json(); } catch (e) { /* 空响应 */ }
  if (!res.ok) {
    const d = body && body.detail;
    const msg = typeof d === 'string' ? d
      : (d && d.message) ? d.message
      : `请求失败 (${res.status})`;
    const err = new Error(msg);
    err.status = res.status;
    err.detail = d;
    throw err;
  }
  return body;
}

function toast(msg, isErr = false) {
  const box = document.getElementById('toast');
  if (!box) return;
  const el = document.createElement('div');
  if (isErr) el.className = 'err';
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

// 轮询：任务运行时快，空闲时慢。返回 stop 函数。
function poll(fn, { fast = 1000, slow = 10000 } = {}) {
  let timer = null, stopped = false;
  async function tick() {
    if (stopped) return;
    let busy = false;
    try { busy = await fn(); } catch (e) { /* 静默，下一轮再试 */ }
    if (stopped) return;
    timer = setTimeout(tick, busy ? fast : slow);
  }
  tick();
  return () => { stopped = true; if (timer) clearTimeout(timer); };
}

function fmtDate(s) {
  if (!s) return '—';
  return s.replace('T', ' ').slice(0, 19);
}

function fmtAgo(s) {
  if (!s) return '';
  const t = new Date(s.replace(' ', 'T')).getTime();
  if (Number.isNaN(t)) return '';
  const d = Math.floor((Date.now() - t) / 1000);
  if (d < 60) return '刚刚';
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return `${Math.floor(d / 86400)} 天前`;
}

function fmtUntil(s) {
  if (!s) return '';
  const t = new Date(s.replace(' ', 'T')).getTime();
  if (Number.isNaN(t)) return '';
  const d = Math.floor((t - Date.now()) / 1000);
  if (d < 0) return '即将触发';
  if (d < 3600) return `还有 ${Math.floor(d / 60)} 分钟`;
  if (d < 86400) return `还有 ${Math.floor(d / 3600)} 小时`;
  return `还有 ${Math.floor(d / 86400)} 天`;
}

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return n;
}

function renderBanner(cov, target) {
  const box = document.getElementById(target || 'banner');
  if (!box) return;
  box.innerHTML = '';
  if (!cov || !cov.is_thin || !cov.jobs_total) return;
  box.append(el('div', { class: 'banner' },
    el('strong', {}, `分析覆盖率仅 ${cov.pct}%`),
    el('div', {}, cov.hint),
    el('div', { class: 'sub' },
      '报告只反映已分析的这部分岗位。想提高覆盖率，去任务页把分析的 limit 调大。')));
}

// ---- 图表 ----

const CHART_COLORS = ['#4f8cff', '#3ecf8e', '#e0b341', '#ff6b4a',
  '#a78bfa', '#22d3ee', '#f472b6', '#94a3b8'];

Chart.defaults.color = '#9aa3b2';
Chart.defaults.borderColor = '#2a2f3a';
Chart.defaults.font.family = '-apple-system, "PingFang SC", sans-serif';

const _charts = {};
function chart(id, config) {
  const cvs = document.getElementById(id);
  if (!cvs) return null;
  if (_charts[id]) _charts[id].destroy();
  _charts[id] = new Chart(cvs, config);
  return _charts[id];
}

function barChart(id, labels, data, { horizontal = false, label = '数量' } = {}) {
  return chart(id, {
    type: 'bar',
    data: {
      labels,
      datasets: [{ label, data, backgroundColor: '#4f8cff', borderRadius: 3 }],
    },
    options: {
      indexAxis: horizontal ? 'y' : 'x',
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { grid: { display: !horizontal } },
                y: { grid: { display: horizontal } } },
    },
  });
}

function doughnut(id, labels, data) {
  return chart(id, {
    type: 'doughnut',
    data: { labels, datasets: [{ data, backgroundColor: CHART_COLORS,
                                 borderColor: '#171a21', borderWidth: 2 }] },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '62%',
      plugins: { legend: { position: 'right' } },
    },
  });
}

function lineChart(id, labels, data, label = '发布量') {
  return chart(id, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label, data, borderColor: '#4f8cff', backgroundColor: 'rgba(79,140,255,.12)',
        fill: true, tension: .3, pointRadius: 0, borderWidth: 2,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: { ticks: { maxTicksLimit: 10 } } },
    },
  });
}

function stackedBar(id, matrix) {
  const companies = matrix.map(m => m.company);
  const tracks = ['ai', 'backend', 'other'];
  return chart(id, {
    type: 'bar',
    data: {
      labels: companies,
      datasets: tracks.map((t, i) => ({
        label: TRACK_LABEL[t], data: matrix.map(m => m[t] || 0),
        backgroundColor: CHART_COLORS[i], borderRadius: 3,
      })),
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'bottom' } },
      scales: { x: { stacked: true }, y: { stacked: true } },
    },
  });
}

// marked 关掉 HTML 透传：报告内容由大模型生成，属不完全可控输入
function renderMarkdown(md) {
  if (!md) return '';
  return marked.parse(md, { headerIds: false, mangle: false });
}

function navActive() {
  const here = location.pathname.endsWith('/') ? '/index.html' : location.pathname;
  document.querySelectorAll('header nav a').forEach(a => {
    if (a.getAttribute('href') === here.split('/').pop()
        || (here.endsWith('index.html') && a.getAttribute('href') === '/')) {
      a.classList.add('on');
    }
  });
}
document.addEventListener('DOMContentLoaded', navActive);
