// detail.js — วาดกราฟแนวโน้มเป็น SVG เอง (ตาม design "แนวโน้มย้อนหลัง" ไม่พึ่ง Chart.js/CDN)
(function () {
  // ตัวกรองเดือน — ต้องอยู่ก่อน guard ของกราฟ เพราะธนาคารที่ยังไม่มีข้อมูลก็ไม่มีกราฟ
  const monthSel = document.getElementById('month-select');
  if (monthSel) {
    monthSel.addEventListener('change', () => {
      if (monthSel.value) location.href = '/bank/' + monthSel.dataset.code + '?month=' + monthSel.value;
    });
  }

  // (สรุปผลการรัน + reload หลังรันเสร็จ อยู่ใน run.js — ผูกกับ #run-notice ใช้ร่วมกับหน้า overview)

  const dataEl = document.getElementById('chart-data');
  const svg = document.getElementById('trend');
  // กราฟไม่มีใน design บนมือถือ — .bd-trend{display:none} ข้ามการวาดไปเลย
  if (!dataEl || !svg || svg.offsetParent === null) return;

  const payload = JSON.parse(dataEl.textContent);
  const allLabels = payload.labels || [];
  const allDates = payload.dates || [];
  // ผลิตภัณฑ์ที่ไม่มีค่าเลยสักครั้งในประวัติทั้งหมด วาดไม่ได้ (เส้นว่าง) — ตัดออกก่อน ไม่งั้นกิน legend/สีไปเปล่า ๆ
  // idx เป็น "อัตลักษณ์สี/การเลือกแสดง" ที่คงที่ตลอด — ผูกกับตำแหน่งในอาร์เรย์นี้ ไม่ใช่ตำแหน่งหลังกรองช่วงเวลา
  // (คนละอาร์เรย์กันหลัง computeView ตัดตาม range เพราะ series ที่ไม่มีข้อมูลในช่วงนั้นจะหลุดออกไป)
  const allSeries = (payload.datasets || [])
    .filter((d) => d.data.some((v) => v !== null))
    .map((d, idx) => ({ ...d, idx }));
  if (!allLabels.length || !allSeries.length) return;

  // สีเส้นตามลำดับคงที่ (สีที่ i เป็นของผลิตภัณฑ์ที่ i เสมอ) — 3 สีแรกมาจาก design
  // ที่เหลือต่อด้วยชุดสีที่ผ่านการตรวจตาบอดสี (protan/deuteran) เผื่อธนาคารที่ติดตามเกิน 3 รายการ
  const PALETTE = ['#1E8E5A', '#B7791F', '#9B9EA4', '#2B6CB0', '#C2410C', '#7C5CA8', '#00897B', '#B5427E'];
  const colorOf = (idx) => PALETTE[idx % PALETTE.length];
  const UP = '#1E8E5A', DOWN = '#C0432E';

  // เส้นที่โชว์บนกราฟตอนเปิดหน้าแรก — ดีไซน์ 7a–7d โชว์แค่ 3 รายการแรก (3/6/12 เดือน แบบไม่มีเงื่อนไข
  // พิเศษ) ซึ่งตรงกับ 3 รายการแรกใน rate_targets ของทุกธนาคารจริงพอดี ธนาคารที่ติดตามเกิน 3 รายการ (เช่น
  // SCB มี 7) จะยัดทุกเส้นเข้ากราฟเดียวพร้อมกันแล้วป้ายค่าท้ายเส้นจะทับกันจนอ่านไม่ออก — ให้ผู้ใช้กด legend
  // เพื่อเพิ่ม/ซ่อนเส้นเองแทน ไม่ auto-limit แบบตายตัว (รายการที่ติดตามน้อยกว่า 3 ก็โชว์ครบตามจริง)
  const visible = new Set(allSeries.slice(0, 3).map((s) => s.idx));

  // ── กรอบกราฟ (พิกัดตาม viewBox 1500×260 ของ design) ──
  const X0 = 48, X1 = 1450;       // ซ้าย-ขวาของพื้นที่เส้น
  const TOP = 45;                 // เส้น grid บนสุด
  const GAP = 50;                 // ระยะห่างระหว่าง gridline (4 เส้น: 45 · 95 · 145 · 195)
  const TICKS = 4;
  const BASE = 220;               // เส้นฐาน — ต่ำกว่า gridline ล่างสุดครึ่งช่อง
  const SVGNS = 'http://www.w3.org/2000/svg';

  const el = (name, attrs, text) => {
    const n = document.createElementNS(SVGNS, name);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const add = (parent, name, attrs, text) => parent.appendChild(el(name, attrs, text));

  // ── ตัวช่วยวันที่: บวก/ลบเดือนแบบปฏิทิน (ไม่สนวันที่ในเดือน) ──
  const addMonths = (iso, delta) => {
    const [y, m, d] = iso.split('-').map(Number);
    const dt = new Date(Date.UTC(y, m - 1 + delta, d));
    const pad = (n) => String(n).padStart(2, '0');
    return `${dt.getUTCFullYear()}-${pad(dt.getUTCMonth() + 1)}-${pad(dt.getUTCDate())}`;
  };
  // ต่างกันกี่เดือนตามปฏิทิน (ปี×12 + เดือน) — สูตรนี้ให้ผลตรงกับตัวเลขในดีไซน์ (เทียบ 26 ส.ค.68→06 ก.ค.69 = ~11)
  const monthsBetween = (iso1, iso2) => {
    const [y1, m1] = iso1.split('-').map(Number);
    const [y2, m2] = iso2.split('-').map(Number);
    return (y2 - y1) * 12 + (m2 - m1);
  };

  // ── ตัดข้อมูลตามช่วงเวลาที่เลือก (0 = ทั้งหมด) — .idx ของแต่ละ series ยังติดไปด้วยเสมอ ──
  const computeView = (monthsBack) => {
    if (!monthsBack) {
      return { labels: allLabels, dates: allDates, series: allSeries };
    }
    const lastDate = allDates[allDates.length - 1];
    const cutoff = addMonths(lastDate, -monthsBack);
    let startIdx = allDates.findIndex((d) => d >= cutoff);
    if (startIdx === -1) startIdx = allDates.length - 1;
    return {
      labels: allLabels.slice(startIdx),
      dates: allDates.slice(startIdx),
      series: allSeries
        .map((s) => ({ ...s, data: s.data.slice(startIdx) }))
        .filter((s) => s.data.some((v) => v !== null)),
    };
  };

  let currentView = null;

  // ── วาดกราฟ+legend+badge ทั้งหมดใหม่จาก view ที่กรองแล้ว (คำนึงถึง visible ด้วย) ──
  const render = (view) => {
    currentView = view;
    const { labels, dates, series } = view;
    // เส้นที่ต้องวาดจริง = series ที่ผู้ใช้เปิดไว้ ∩ ที่มีข้อมูลในช่วงเวลานี้ — ถ้ากรองแล้วไม่เหลือเลย
    // (เช่น toggle เฉพาะเส้นที่ไม่มีข้อมูลในช่วง 3 เดือนที่เพิ่งสลับมา) ให้ fallback โชว์ทุกเส้นในช่วงนั้น
    // แทนปล่อยกราฟว่างเปล่าโดยไม่บอกเหตุผล — ไม่แตะ state ของ visible เอง
    let shown = series.filter((s) => visible.has(s.idx));
    if (!shown.length) shown = series;

    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const legend = document.getElementById('trend-legend');
    legend.querySelectorAll('.item').forEach((n) => n.remove());
    const unit = legend.querySelector('.unit');

    document.getElementById('trend-count').textContent = labels.length;
    const spanEl = document.getElementById('trend-span');
    if (spanEl) {
      const months = labels.length > 1 ? monthsBetween(dates[0], dates[dates.length - 1]) : 0;
      spanEl.textContent = `~${months} เดือนย้อนหลัง`;
    }

    // ── legend — ลิสต์ทุกเส้นที่มีข้อมูลในช่วงนี้ (ไม่ใช่แค่ที่กำลังโชว์) กดเพื่อเพิ่ม/ซ่อนได้ ──
    series.forEach((s) => {
      const item = document.createElement('span');
      item.className = 'item';
      item.tabIndex = 0;
      item.setAttribute('role', 'button');
      const isOn = visible.has(s.idx);
      item.setAttribute('aria-pressed', String(isOn));
      if (!isOn) item.classList.add('off');
      const sw = document.createElement('span');
      sw.className = 'sw';
      sw.style.background = colorOf(s.idx);
      item.append(sw, document.createTextNode(s.label));
      // ประเภทลูกค้า (บุคคลธรรมดา/กองทุน/ราชการ) — ชื่อ series อย่างเดียวแยกไม่ออกว่าเป็นอัตราของใคร
      if (s.dep && s.dep.label) {
        const dep = document.createElement('span');
        dep.className = 'pill-dep dep-' + s.dep.slug;
        dep.textContent = s.dep.label;
        item.append(dep);
      }
      const toggle = () => {
        if (visible.has(s.idx)) {
          if (visible.size === 1) return;   // ต้องเหลืออย่างน้อย 1 เส้นเสมอ
          visible.delete(s.idx);
        } else {
          visible.add(s.idx);
        }
        render(currentView);
      };
      item.addEventListener('click', toggle);
      item.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggle(); }
      });
      legend.insertBefore(item, unit);
    });

    const badge = document.getElementById('trend-badge');
    if (badge) { badge.hidden = true; badge.textContent = ''; badge.className = 'bd-trend-badge'; }
    if (!labels.length) return;

    const values = shown.flatMap((d) => d.data.filter((v) => v !== null));
    if (!values.length) return;
    const lo = Math.min(...values), hi = Math.max(...values);

    // เลือกขั้นแกน Y แบบ "เลขสวย" ที่เล็กสุดซึ่งครอบข้อมูลได้ครบใน 4 gridline
    const NICE = [0.01, 0.02, 0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 5];
    const step = NICE.find((s) => Math.ceil(hi / s) * s - (TICKS - 1) * s <= lo) || NICE[NICE.length - 1];
    const top = Math.ceil(hi / step) * step;
    const r2 = (n) => Math.round(n * 100) / 100;   // ตัดเศษทศนิยมลอย ๆ ออกจากพิกัด SVG
    const y = (v) => r2(TOP + (top - v) * (GAP / step));
    // แกน X เป็นสเกลเวลาจริง — ระยะห่างระหว่างจุดสะท้อนจำนวนวันที่ห่างกันจริง ไม่ใช่ลำดับของประกาศ
    // (ประกาศออกถี่/ห่างไม่เท่ากัน ถ้าวางเป็นช่องเท่า ๆ กันจะอ่านความชันของกราฟผิด)
    // ทุกจุดวันเดียวกัน (ต่างกัน 0 ms) ให้ตกกลางกราฟ กันหารด้วยศูนย์
    const times = dates.map((d) => Date.parse(d + 'T00:00:00Z'));
    const t0 = times[0], span = times[times.length - 1] - t0;
    const x = (i) => r2(span > 0 ? X0 + ((times[i] - t0) / span) * (X1 - X0) : (X0 + X1) / 2);

    // ── gridline + ป้ายแกน Y + เส้นฐาน ──
    const grid = add(svg, 'g', { stroke: '#EFECE7', 'stroke-width': '1' });
    const yLab = add(svg, 'g', { fill: '#6E7178', 'font-size': '15', 'text-anchor': 'end' });
    for (let i = 0; i < TICKS; i++) {
      const gy = TOP + i * GAP;
      add(grid, 'line', { x1: X0, y1: gy, x2: X1, y2: gy });
      add(yLab, 'text', { x: X0 - 10, y: gy + 5 }, (top - i * step).toFixed(2));
    }
    add(svg, 'line', { x1: X0, y1: BASE, x2: X1, y2: BASE, stroke: '#E0DDD6', 'stroke-width': '1' });
    // เส้นประแนวตั้งที่จุดล่าสุด — เน้นตำแหน่งประกาศปัจจุบัน
    add(svg, 'line', { x1: X1, y1: 8, x2: X1, y2: BASE, stroke: '#D6D2CA', 'stroke-width': '1', 'stroke-dasharray': '3,4' });

    // ── เส้นแต่ละผลิตภัณฑ์ (เฉพาะที่โชว์) ──
    // ค่าที่ขาดหาย (null) ข้ามไป แล้วลากเชื่อมจุดถัดไป — เหมือน spanGaps เดิม
    const points = (d) => d.data.map((v, i) => (v === null ? null : [x(i), y(v)])).filter(Boolean);

    // พื้นสีทึบใต้เส้นบนสุด (design เติมเฉพาะเส้นเดียว) — เส้นอื่นทับพื้นแล้วอ่านยาก
    // เลือกจาก "อัตราสุดท้ายสูงสุด" ไม่ใช่เส้นแรก เพราะลำดับเส้นมาจากลำดับ rate_targets ของแต่ละธนาคาร
    const lastOf = (d) => d.data.reduce((acc, v) => (v === null ? acc : v), null);
    const topIdx = shown.reduce((best, s, i) => (lastOf(s) > lastOf(shown[best]) ? i : best), 0);
    const topPts = points(shown[topIdx]);
    if (topPts.length > 1) {
      const d = `M${topPts.map((p) => p.join(',')).join(' ')} L${topPts[topPts.length - 1][0]},${BASE} L${topPts[0][0]},${BASE} Z`;
      add(svg, 'path', { d, fill: colorOf(shown[topIdx].idx), 'fill-opacity': '0.14' });
    }

    shown.forEach((s) => {
      const pts = points(s);
      if (!pts.length) return;
      add(svg, 'polyline', {
        points: pts.map((p) => p.join(',')).join(' '),
        fill: 'none', stroke: colorOf(s.idx), 'stroke-width': '2.5',
        'stroke-linejoin': 'round', 'stroke-linecap': 'round',
      });
      pts.forEach(([px, py]) => add(svg, 'circle', {
        cx: px, cy: py, r: '3.5', fill: '#fff', stroke: colorOf(s.idx), 'stroke-width': '2.5',
      }));
    });

    // จุดเน้นค่าล่าสุดของเส้นบนสุด — ให้เห็นชัดว่าเป็นอัตราปัจจุบัน
    if (topPts.length) {
      const [lx, ly] = topPts[topPts.length - 1];
      add(svg, 'circle', { cx: lx, cy: ly, r: '5', fill: colorOf(shown[topIdx].idx), stroke: '#fff', 'stroke-width': '2' });
    }

    // ── ป้ายค่าล่าสุดท้ายเส้น — เลื่อนหนีกันเองไม่ให้ทับ ──
    const tails = shown
      .map((s) => {
        const last = s.data.reduce((acc, v) => (v === null ? acc : v), null);
        return last === null ? null : { v: last, y: y(last), color: colorOf(s.idx) };
      })
      .filter(Boolean)
      .sort((a, b) => a.y - b.y);
    const MIN_GAP = 20;
    tails.forEach((t, i) => {
      t.ty = i === 0 ? t.y + 5 : Math.max(t.y + 5, tails[i - 1].ty + MIN_GAP);
    });
    const tailG = add(svg, 'g', { 'font-size': '16', 'font-weight': '600' });
    tails.forEach((t) => {
      add(tailG, 'text', { x: X1 - 8, y: t.ty, 'text-anchor': 'end', fill: t.color }, t.v.toFixed(2) + '%');
    });

    // ── ป้ายแกน X (วันที่ประกาศ) — บังคับให้มีจุดแรก+จุดสุดท้ายเสมอ ──
    // เลือกด้วย "ระยะห่างเป็นพิกเซล" ไม่ใช่ทุก ๆ n ลำดับ เพราะบนสเกลเวลาจริงประกาศที่ออกติด ๆ กัน
    // ไม่กี่วันจะอยู่เกือบทับกัน (การเว้นตามลำดับจึงยังเลือกป้ายที่ซ้อนกันมาได้)
    const MIN_LABEL_GAP = 210;   // ~ความกว้างป้าย "25 เม.ย. 69" + ช่องว่าง
    const picked = [];
    labels.forEach((lb, i) => {
      const px = x(i);
      if (i === 0 || i === labels.length - 1 || px - picked[picked.length - 1].px >= MIN_LABEL_GAP) {
        picked.push({ lb, px });
      }
    });
    // จุดสุดท้ายสำคัญกว่า (เป็นประกาศปัจจุบัน) — ถ้าเบียดป้ายก่อนหน้า ตัดป้ายก่อนหน้าทิ้งแทน
    while (picked.length > 2 && picked[picked.length - 1].px - picked[picked.length - 2].px < MIN_LABEL_GAP) {
      picked.splice(picked.length - 2, 1);
    }
    const xLab = add(svg, 'g', { fill: '#6E7178', 'font-size': '15', 'text-anchor': 'middle' });
    picked.forEach((p) => add(xLab, 'text', { x: p.px, y: BASE + 26 }, p.lb));

    // ── ป้ายสรุปการเปลี่ยนแปลงครั้งล่าสุด (มุมขวาบน) — เฉพาะเส้นที่กำลังโชว์ ──
    // ไล่จากประกาศล่าสุดย้อนกลับไป หาครั้งแรกที่มีอัตราขยับ แล้วบอกทิศทาง + เดือนของประกาศนั้น
    for (let i = labels.length - 1; i > 0 && badge; i--) {
      const deltas = shown
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
    const tipBox = add(tip, 'rect', { rx: '9', fill: '#fff', stroke: '#E0DDD6' });
    const tipDate = add(tip, 'text', { 'font-size': '13', 'font-weight': '600', fill: '#8A8D93' });
    const tipL1 = add(tip, 'text', { 'font-size': '14', fill: '#8A8D93' });
    const tipL2 = add(tip, 'text', { 'font-size': '17', 'font-weight': '600' });

    const hit = add(svg, 'rect', {
      x: 0, y: 0, width: 1500, height: 260, fill: '#000', 'fill-opacity': '0',
      style: 'cursor:crosshair;pointer-events:all',
    });

    const showTip = (ev) => {
      const box = svg.getBoundingClientRect();
      const mx = ((ev.clientX - box.left) / box.width) * 1500;
      const my = ((ev.clientY - box.top) / box.height) * 260;

      let best = null;
      shown.forEach((s, si) => {
        s.data.forEach((v, i) => {
          if (v === null) return;
          const px = x(i), py = y(v);
          const dist = (px - mx) ** 2 + ((py - my) * 1.6) ** 2;   // ถ่วงแกน y ให้เลือกเส้นที่เมาส์อยู่ใกล้จริง
          if (!best || dist < best.dist) best = { dist, px, py, v, i, si };
        });
      });
      if (!best) return;

      const s = shown[best.si];
      const dateText = labels[best.i] || '';
      const line1 = s.dep && s.dep.label ? `${s.label} · ${s.dep.label}` : s.label;
      const prevV = best.i > 0 ? s.data[best.i - 1] : null;
      const deltaText = prevV !== null ? `(${(best.v - prevV >= 0 ? '+' : '')}${(best.v - prevV).toFixed(2)}%)` : '';
      const deltaColor = best.v - (prevV || best.v) >= 0 ? UP : DOWN;
      const line2Main = `${best.v.toFixed(2)}%`;

      while (tipL2.firstChild) tipL2.removeChild(tipL2.firstChild);
      const t1 = el('tspan', { fill: colorOf(s.idx) }, line2Main);
      tipL2.appendChild(t1);
      if (deltaText) {
        tipL2.appendChild(el('tspan', { fill: deltaColor, 'font-weight': '500', 'font-size': '15', dx: '6' }, deltaText));
      }

      const w = Math.max(dateText.length * 8, line1.length * 8.5, (line2Main + deltaText).length * 10.5) + 28;
      const h = 74;
      // เด้งไปฝั่งซ้ายของจุดเมื่อชิดขอบขวา และดันลงล่างเมื่อชิดขอบบน
      const bx = best.px + 14 + w > 1500 ? best.px - 14 - w : best.px + 14;
      const by = Math.max(4, best.py - h - 10);

      tipDot.setAttribute('cx', best.px);
      tipDot.setAttribute('cy', best.py);
      tipDot.setAttribute('fill', colorOf(s.idx));
      tipBox.setAttribute('x', bx); tipBox.setAttribute('y', by);
      tipBox.setAttribute('width', w); tipBox.setAttribute('height', h);
      tipDate.setAttribute('x', bx + 14); tipDate.setAttribute('y', by + 19); tipDate.textContent = dateText;
      tipL1.setAttribute('x', bx + 14); tipL1.setAttribute('y', by + 40); tipL1.textContent = line1;
      tipL2.setAttribute('x', bx + 14); tipL2.setAttribute('y', by + 62);
      tip.setAttribute('visibility', 'visible');
    };

    hit.addEventListener('mousemove', showTip);
    hit.addEventListener('mouseleave', () => tip.setAttribute('visibility', 'hidden'));
  };

  // ── ปุ่มช่วงเวลา (3 เดือน / 6 เดือน / 1 ปี / ทั้งหมด) ──
  const rangeBtns = document.querySelectorAll('.bd-trend-range button');
  rangeBtns.forEach((btn) => {
    const months = Number(btn.dataset.months);
    if (months && computeView(months).labels.length < 2) btn.disabled = true;
    btn.addEventListener('click', () => {
      if (btn.disabled) return;
      rangeBtns.forEach((b) => b.classList.remove('on'));
      btn.classList.add('on');
      render(computeView(months));
    });
  });

  // เริ่มต้นที่ปุ่มซึ่งมีคลาส .on อยู่แล้วในเทมเพลต (ตั้งค่าเริ่มต้นเป็น "1 ปี")
  // ธนาคารที่มีประกาศในช่วงนั้นไม่ถึง 2 ครั้ง ปุ่มนั้นจะโดน disable ไปแล้วข้างบน — ถอยไปใช้ "ทั้งหมด"
  // ไม่งั้นกราฟจะเปิดมาว่างเปล่าโดยที่ปุ่มที่ค้าง .on อยู่ก็กดไม่ได้
  const allBtn = document.querySelector('.bd-trend-range button[data-months="0"]');
  let initialBtn = document.querySelector('.bd-trend-range button.on');
  if (!initialBtn || initialBtn.disabled) {
    if (initialBtn) initialBtn.classList.remove('on');
    initialBtn = allBtn;
    if (initialBtn) initialBtn.classList.add('on');
  }
  render(computeView(initialBtn ? Number(initialBtn.dataset.months) : 0));
})();
