// detail.js — วาดกราฟแนวโน้มเป็น SVG เอง (ตาม design "แนวโน้มย้อนหลัง" ไม่พึ่ง Chart.js/CDN)
(function () {
  // ตัวกรองเดือน — ต้องอยู่ก่อน guard ของกราฟ เพราะธนาคารที่ยังไม่มีข้อมูลก็ไม่มีกราฟ
  const monthSel = document.getElementById('month-select');
  if (monthSel) {
    monthSel.addEventListener('change', () => {
      if (monthSel.value) location.href = '/bank/' + monthSel.dataset.code + '?month=' + monthSel.value;
    });
  }

  // ผลิตภัณฑ์เป็น <details>: >720px = โหมดตาราง กางทุกแถวเสมอ; ≤720px = การ์ด พับแถวที่ไม่เปลี่ยนแปลง
  const rows = document.querySelectorAll('.bd-rows .ov-row');
  if (rows.length) {
    const mq = window.matchMedia('(min-width: 721px)');
    const syncRows = () => {
      rows.forEach((r) => { r.open = mq.matches || r.dataset.changed === '1'; });
    };
    syncRows();
    mq.addEventListener('change', syncRows);
  }

  // (สรุปผลการรัน + reload หลังรันเสร็จ อยู่ใน run.js — ผูกกับ #run-notice ใช้ร่วมกับหน้า overview)

  const dataEl = document.getElementById('chart-data');
  const svg = document.getElementById('trend');
  // กราฟไม่มีใน design บนมือถือ — .bd-trend{display:none} ข้ามการวาดไปเลย
  if (!dataEl || !svg || svg.offsetParent === null) return;

  const payload = JSON.parse(dataEl.textContent);
  const labels = payload.labels || [];
  // ผลิตภัณฑ์ที่ไม่มีค่าเลยสักครั้ง วาดไม่ได้ (เส้นว่าง) — ตัดออกก่อน ไม่งั้นกิน legend/สีไปเปล่า ๆ
  const series = (payload.datasets || []).filter((d) => d.data.some((v) => v !== null));
  if (!labels.length || !series.length) return;

  // สีเส้นตามลำดับคงที่ (สีที่ i เป็นของผลิตภัณฑ์ที่ i เสมอ) — 3 สีแรกมาจาก design
  // ที่เหลือต่อด้วยชุดสีที่ผ่านการตรวจตาบอดสี (protan/deuteran) เผื่อธนาคารที่ติดตามเกิน 3 รายการ
  const PALETTE = ['#1E8E5A', '#B7791F', '#9B9EA4', '#2B6CB0', '#C2410C', '#7C5CA8', '#00897B', '#B5427E'];
  const colorOf = (i) => PALETTE[i % PALETTE.length];

  // ── กรอบกราฟ (พิกัดตาม viewBox 1000×260 ของ design) ──
  const X0 = 48, X1 = 980;        // ซ้าย-ขวาของพื้นที่เส้น
  const TOP = 45;                 // เส้น grid บนสุด
  const GAP = 50;                 // ระยะห่างระหว่าง gridline (4 เส้น: 45 · 95 · 145 · 195)
  const TICKS = 4;
  const BASE = 220;               // เส้นฐาน — ต่ำกว่า gridline ล่างสุดครึ่งช่อง
  const SVGNS = 'http://www.w3.org/2000/svg';

  const values = series.flatMap((d) => d.data.filter((v) => v !== null));
  const lo = Math.min(...values), hi = Math.max(...values);

  // เลือกขั้นแกน Y แบบ "เลขสวย" ที่เล็กสุดซึ่งครอบข้อมูลได้ครบใน 4 gridline
  const NICE = [0.01, 0.02, 0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 5];
  const step = NICE.find((s) => Math.ceil(hi / s) * s - (TICKS - 1) * s <= lo) || NICE[NICE.length - 1];
  const top = Math.ceil(hi / step) * step;
  const r2 = (n) => Math.round(n * 100) / 100;   // ตัดเศษทศนิยมลอย ๆ ออกจากพิกัด SVG
  const y = (v) => r2(TOP + (top - v) * (GAP / step));
  const x = (i) => r2(labels.length === 1 ? (X0 + X1) / 2 : X0 + i * ((X1 - X0) / (labels.length - 1)));

  const el = (name, attrs, text) => {
    const n = document.createElementNS(SVGNS, name);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const add = (parent, name, attrs, text) => parent.appendChild(el(name, attrs, text));

  // ── gridline + ป้ายแกน Y + เส้นฐาน ──
  const grid = add(svg, 'g', { stroke: '#EFECE7', 'stroke-width': '1' });
  const yLab = add(svg, 'g', { fill: '#B0B3B9', 'font-size': '11', 'text-anchor': 'end' });
  for (let i = 0; i < TICKS; i++) {
    const gy = TOP + i * GAP;
    add(grid, 'line', { x1: X0, y1: gy, x2: X1, y2: gy });
    add(yLab, 'text', { x: X0 - 8, y: gy + 4 }, (top - i * step).toFixed(2));
  }
  add(svg, 'line', { x1: X0, y1: BASE, x2: X1, y2: BASE, stroke: '#E0DDD6', 'stroke-width': '1' });

  // ── เส้นแต่ละผลิตภัณฑ์ ──
  // ค่าที่ขาดหาย (null) ข้ามไป แล้วลากเชื่อมจุดถัดไป — เหมือน spanGaps เดิม
  const points = (d) => d.data.map((v, i) => (v === null ? null : [x(i), y(v)])).filter(Boolean);

  // พื้นไล่สีใต้เส้นบนสุด (design เติมเฉพาะเส้นเดียว) — เส้นอื่นทับพื้นแล้วอ่านยาก
  // เลือกจาก "อัตราสุดท้ายสูงสุด" ไม่ใช่เส้นแรก เพราะลำดับเส้นมาจากลำดับ rate_targets ของแต่ละธนาคาร
  const lastOf = (d) => d.data.reduce((acc, v) => (v === null ? acc : v), null);
  const topIdx = series.reduce((best, s, i) => (lastOf(s) > lastOf(series[best]) ? i : best), 0);
  const topPts = points(series[topIdx]);
  if (topPts.length > 1) {
    const d = `M${topPts.map((p) => p.join(',')).join(' ')} L${topPts[topPts.length - 1][0]},${BASE} L${topPts[0][0]},${BASE} Z`;
    add(svg, 'path', { d, fill: colorOf(topIdx), 'fill-opacity': '0.06' });
  }

  series.forEach((s, i) => {
    const pts = points(s);
    if (!pts.length) return;
    add(svg, 'polyline', {
      points: pts.map((p) => p.join(',')).join(' '),
      fill: 'none', stroke: colorOf(i), 'stroke-width': '2.5',
      'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    });
    pts.forEach(([px, py]) => add(svg, 'circle', {
      cx: px, cy: py, r: '3.5', fill: '#fff', stroke: colorOf(i), 'stroke-width': '2.5',
    }));
  });

  // ── ป้ายค่าล่าสุดท้ายเส้น — เลื่อนหนีกันเองไม่ให้ทับ ──
  const tails = series
    .map((s, i) => {
      const last = s.data.reduce((acc, v) => (v === null ? acc : v), null);
      return last === null ? null : { v: last, y: y(last), color: colorOf(i) };
    })
    .filter(Boolean)
    .sort((a, b) => a.y - b.y);
  const MIN_GAP = 15;
  tails.forEach((t, i) => {
    t.ty = i === 0 ? t.y + 4 : Math.max(t.y + 4, tails[i - 1].ty + MIN_GAP);
  });
  const tailG = add(svg, 'g', { 'font-size': '12', 'font-weight': '600' });
  tails.forEach((t) => {
    add(tailG, 'text', { x: X1 - 8, y: t.ty, 'text-anchor': 'end', fill: t.color }, t.v.toFixed(2) + '%');
  });

  // ── ป้ายแกน X (วันที่ประกาศ) — ข้ามเป็นช่วงเมื่อประกาศเยอะ ไม่งั้นตัวหนังสือทับกัน ──
  const skip = Math.ceil(labels.length / 6);
  const xLab = add(svg, 'g', { fill: '#9B9EA4', 'font-size': '11', 'text-anchor': 'middle' });
  labels.forEach((lb, i) => {
    if (i % skip === 0 || i === labels.length - 1) add(xLab, 'text', { x: x(i), y: BASE + 24 }, lb);
  });

  // ── legend (ก่อนกราฟ ตาม design) ──
  const legend = document.getElementById('trend-legend');
  const unit = legend.querySelector('.unit');
  series.forEach((s, i) => {
    const item = document.createElement('span');
    item.className = 'item';
    const sw = document.createElement('span');
    sw.className = 'sw';
    sw.style.background = colorOf(i);
    item.append(sw, document.createTextNode(s.label));
    // ประเภทลูกค้า (บุคคลธรรมดา/กองทุน/ราชการ) — ชื่อ series อย่างเดียวแยกไม่ออกว่าเป็นอัตราของใคร
    if (s.dep && s.dep.label) {
      const dep = document.createElement('span');
      dep.className = 'pill-dep dep-' + s.dep.slug;
      dep.textContent = s.dep.label;
      item.append(dep);
    }
    legend.insertBefore(item, unit);
  });

  document.getElementById('trend-count').textContent = labels.length;

  // ── ป้ายสรุปการเปลี่ยนแปลงครั้งล่าสุด (มุมขวาบน) ──
  // ไล่จากประกาศล่าสุดย้อนกลับไป หาครั้งแรกที่มีอัตราขยับ แล้วบอกทิศทาง + เดือนของประกาศนั้น
  const badge = document.getElementById('trend-badge');
  for (let i = labels.length - 1; i > 0 && badge; i--) {
    const deltas = series
      .map((s) => (s.data[i] === null || s.data[i - 1] === null ? 0 : s.data[i] - s.data[i - 1]))
      .filter((d) => Math.abs(d) > 1e-9);
    if (!deltas.length) continue;
    const up = deltas.some((d) => d > 0), down = deltas.some((d) => d < 0);
    badge.textContent = (up && down ? 'ปรับอัตรา ' : up ? 'ปรับขึ้น ' : 'ปรับลด ')
      // ตัดวันที่ทิ้ง เหลือ "เม.ย. 69" — label มาจาก thai_date() รูปแบบ "25 เม.ย. 69"
      + labels[i].split(' ').slice(1).join(' ');
    badge.className = 'bd-trend-badge ' + (up && down ? 'mixed' : up ? 'up' : 'down');
    badge.hidden = false;
    break;
  }

  // ── tooltip ตอน hover — จับจุดที่ใกล้เคียงที่สุด (ทั้งแกน x และ y) ──
  const tip = add(svg, 'g', { style: 'pointer-events:none', visibility: 'hidden' });
  const tipDot = add(tip, 'circle', { r: '5' });
  const tipBox = add(tip, 'rect', { rx: '7', fill: '#17181C' });
  const tipL1 = add(tip, 'text', { 'font-size': '11', fill: '#9B9EA4' });
  const tipL2 = add(tip, 'text', { 'font-size': '12', 'font-weight': '600', fill: '#fff' });

  const hit = add(svg, 'rect', {
    x: 30, y: 8, width: 958, height: 230, fill: '#000', 'fill-opacity': '0',
    style: 'cursor:crosshair;pointer-events:all',
  });

  const showTip = (ev) => {
    const box = svg.getBoundingClientRect();
    const mx = ((ev.clientX - box.left) / box.width) * 1000;
    const my = ((ev.clientY - box.top) / box.height) * 260;

    let best = null;
    series.forEach((s, si) => {
      s.data.forEach((v, i) => {
        if (v === null) return;
        const px = x(i), py = y(v);
        const dist = (px - mx) ** 2 + ((py - my) * 1.6) ** 2;   // ถ่วงแกน y ให้เลือกเส้นที่เมาส์อยู่ใกล้จริง
        if (!best || dist < best.dist) best = { dist, px, py, v, i, si };
      });
    });
    if (!best) return;

    const line1 = labels[best.i];
    const line2 = `${series[best.si].label}  ${best.v.toFixed(2)}%`;
    const w = Math.max(line1.length, line2.length) * 7 + 22;
    const h = 44;
    // เด้งไปฝั่งซ้ายของจุดเมื่อชิดขอบขวา และดันลงล่างเมื่อชิดขอบบน
    const bx = best.px + 14 + w > 1000 ? best.px - 14 - w : best.px + 14;
    const by = Math.max(4, best.py - h - 10);

    tipDot.setAttribute('cx', best.px);
    tipDot.setAttribute('cy', best.py);
    tipDot.setAttribute('fill', colorOf(best.si));
    tipBox.setAttribute('x', bx); tipBox.setAttribute('y', by);
    tipBox.setAttribute('width', w); tipBox.setAttribute('height', h);
    tipL1.setAttribute('x', bx + 11); tipL1.setAttribute('y', by + 18); tipL1.textContent = line1;
    tipL2.setAttribute('x', bx + 11); tipL2.setAttribute('y', by + 34); tipL2.textContent = line2;
    tip.setAttribute('visibility', 'visible');
  };

  hit.addEventListener('mousemove', showTip);
  hit.addEventListener('mouseleave', () => tip.setAttribute('visibility', 'hidden'));
})();
