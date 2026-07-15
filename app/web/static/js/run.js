// run.js — ปุ่มรันตรวจสอบ (ทุกธนาคาร หรือเฉพาะธนาคาร) + poll สถานะแบบ live
// ใช้ร่วมกันในหน้า overview / bank_detail / logs

(function () {
  const statusEl = document.getElementById('run-status');
  const outputEl = document.getElementById('run-output');
  let polling = null;
  // เราจะยิง event 'run-finished' (ให้ overview รีเฟรช) เฉพาะเมื่อ "เห็น" งานกำลังรัน
  // ในเซสชันหน้านี้จริง ๆ เท่านั้น กันไม่ให้ผลลัพธ์ค้างของ run เก่าทำให้หน้ารีโหลดวนไม่จบ
  let observedRunning = false;

  function setStatus(kind, text) {
    if (!statusEl) return;
    statusEl.className = 'status-pill ' + kind;
    statusEl.textContent = text;
  }

  function showOutput(text) {
    if (!outputEl) return;
    outputEl.style.display = 'block';
    outputEl.textContent = text || '(ไม่มี output)';
    outputEl.scrollTop = outputEl.scrollHeight;
  }

  function stopPolling() {
    if (polling) { clearInterval(polling); polling = null; }
  }

  // ── สรุปผลการรันแบบสั้น + ลิงก์ไปหน้า Log ──
  // ใช้ในหน้าที่ไม่โชว์ log เต็ม (overview, bank detail): หน้าพวกนี้รีโหลดตัวเองหลังรันเสร็จ
  // เพื่ออัปเดตตัวเลข ผลการรันจึงต้องฝากข้าม reload ไว้ใน sessionStorage แล้วค่อยวาดตอนโหลดใหม่
  // หน้า Log ไม่มี #run-notice (มี #run-output แทน) — ทั้งก้อนนี้จึงไม่ทำงานที่นั่น
  const NOTICE_KEY = 'checkrate:last-run';

  function renderNotice() {
    const box = document.getElementById('run-notice');
    if (!box) return;
    let job;
    try {
      job = JSON.parse(sessionStorage.getItem(NOTICE_KEY) || 'null');
    } catch (e) {
      job = null;
    }
    sessionStorage.removeItem(NOTICE_KEY);
    if (!job) return;

    const ok = job.returncode === 0;
    box.innerHTML =
      '<div class="notice ' + (ok ? 'ok' : 'err') + '">' +
      (ok ? '✓ รันตรวจสอบเสร็จแล้ว — ข้อมูลด้านล่างอัปเดตล่าสุดแล้ว'
          : '✗ การรันล้มเหลว (code ' + job.returncode + ')') +
      ' · <a class="link" href="/logs">ดูรายละเอียดในหน้า Log →</a></div>';
  }

  if (document.getElementById('run-notice')) {
    window.addEventListener('checkrate:run-finished', (e) => {
      try {
        sessionStorage.setItem(NOTICE_KEY, JSON.stringify({ returncode: (e.detail || {}).returncode }));
      } catch (err) { /* sessionStorage ใช้ไม่ได้ก็แค่ไม่มีสรุปผล ไม่ต้องพัง */ }
      setTimeout(() => location.reload(), 800);
    });
    renderNotice();
  }

  async function poll() {
    try {
      const res = await fetch('/api/run/status');
      const job = await res.json();

      if (job.running) {
        observedRunning = true;               // เห็นว่ากำลังรันจริงในเซสชันนี้
        setStatus('running', '⏳ กำลังรัน...');
        if (!polling) polling = setInterval(poll, 1500);  // กรณีเปิดหน้ามาเจองานที่ค้างรันอยู่
        return;
      }

      // งานไม่ได้รันแล้ว — หยุด poll
      stopPolling();
      setButtonsDisabled(false);

      if (job.returncode === null) {
        setStatus('idle', 'พร้อมทำงาน');
        return;
      }
      if (job.returncode === 0) {
        setStatus('ok', '✓ เสร็จสิ้น');
      } else {
        setStatus('err', '✗ ล้มเหลว (code ' + job.returncode + ')');
      }
      showOutput(job.output);

      // ยิง event รีเฟรชเฉพาะเมื่อเราเห็นงานรันในเซสชันนี้ (กัน reload วน)
      if (observedRunning) {
        observedRunning = false;
        window.dispatchEvent(new CustomEvent('checkrate:run-finished', { detail: job }));
      }
    } catch (e) {
      stopPolling();
      setButtonsDisabled(false);
      setStatus('err', 'เชื่อมต่อไม่ได้');
    }
  }

  function setButtonsDisabled(disabled) {
    document.querySelectorAll('[data-run-trigger]').forEach(b => b.disabled = disabled);
  }

  // #year-select อยู่ทั้งหน้า Logs และหน้า bank detail (id เดียวกัน จึงใช้โค้ดชุดเดียวได้)
  // หน้าไหนไม่มี select นี้ → null = ให้ฝั่ง server ตัดสินใจเอง (discover-year default ปีปัจจุบัน)
  function selectedYear() {
    const sel = document.getElementById('year-select');
    const v = sel && sel.value ? parseInt(sel.value, 10) : NaN;
    return Number.isFinite(v) ? v : null;
  }

  async function _startJob(endpoint, only, startingText, year) {
    setButtonsDisabled(true);
    setStatus('running', startingText);
    if (outputEl) outputEl.style.display = 'none';
    const payload = {};
    if (only) payload.only = only;
    if (year) payload.year = year;
    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      // ผู้ใช้กดเอง (หรือมีงานรันอยู่แล้ว 409) → ถือว่าเซสชันนี้ "เห็น" งานรัน
      // เพื่อให้ตอนงานเสร็จมีการรีเฟรชตารางให้ 1 ครั้ง
      observedRunning = true;
      if (res.status === 409) {
        setStatus('running', '⏳ มีงานกำลังรันอยู่แล้ว');
      }
    } catch (e) {
      setStatus('err', 'เริ่มงานไม่สำเร็จ');
      setButtonsDisabled(false);
      return;
    }
    if (!polling) polling = setInterval(poll, 1500);
    poll();
  }

  function startRun(only) {
    return _startJob('/api/run', only, '⏳ กำลังเริ่ม...');
  }

  function startBackfill(only, year) {
    return _startJob('/api/backfill', only, '⏳ กำลังเติมข้อมูลย้อนหลัง...', year);
  }

  function startDiscoverYear(only, year) {
    return _startJob('/api/discover-year', only, '⏳ กำลังสแกนหาประวัติทั้งปี (ใช้เวลานาน)...', year);
  }

  window.CheckRateRun = { startRun, startBackfill, startDiscoverYear, poll, setStatus, showOutput };

  document.addEventListener('DOMContentLoaded', () => {
    const allBtn = document.getElementById('run-all');
    if (allBtn) { allBtn.setAttribute('data-run-trigger', ''); allBtn.addEventListener('click', () => startRun(null)); }

    const bankBtn = document.getElementById('run-bank');
    if (bankBtn) {
      bankBtn.setAttribute('data-run-trigger', '');
      bankBtn.addEventListener('click', () => startRun(bankBtn.dataset.code));
    }

    const thaiYear = (y) => (y ? y + 543 : null);

    function backfillMsg(year) {
      return 'เติมข้อมูลย้อนหลังจาก PDF ที่เก็บไว้?\n' +
        'ระบบจะสร้างไฟล์ CSV ใหม่จาก PDF ทั้งหมดที่เก็บไว้ (ค่าที่ติดตามใหม่จะถูกเติมย้อนหลัง)\n' +
        (year
          ? `บังคับอ่าน PDF ของปี ${thaiYear(year)} ใหม่ทั้งหมด (ข้าม cache) — ปีอื่นใช้ผลที่อ่านไว้แล้ว`
          : 'ไฟล์ที่เคยอ่านไว้แล้วจะใช้ผลจาก cache (เร็ว) — เลือกปีถ้าต้องการบังคับอ่านใหม่');
    }

    function discoverYearMsg(year) {
      const y = year ? `ปี ${thaiYear(year)}` : 'ปีนี้';
      return `สแกนหาประกาศทั้ง${y}แบบละเอียด?\n` +
        `ระบบจะไล่ตรวจทุกวันของ${y} (ใช้เวลาหลายนาที) ดาวน์โหลดไฟล์ที่ยังไม่มีในเครื่อง ` +
        'แล้วสร้าง CSV ใหม่ให้อัตโนมัติ — ใช้เมื่อสงสัยว่ามีประกาศบางช่วงที่การตรวจสอบปกติพลาดไป';
    }

    const backfillAllBtn = document.getElementById('backfill-all');
    if (backfillAllBtn) {
      backfillAllBtn.setAttribute('data-run-trigger', '');
      backfillAllBtn.addEventListener('click', () => {
        const y = selectedYear();
        if (confirm(backfillMsg(y))) startBackfill(null, y);
      });
    }

    const backfillBankBtn = document.getElementById('backfill-bank');
    if (backfillBankBtn) {
      backfillBankBtn.setAttribute('data-run-trigger', '');
      backfillBankBtn.addEventListener('click', () => {
        const y = selectedYear();
        if (confirm(backfillMsg(y))) startBackfill(backfillBankBtn.dataset.code, y);
      });
    }

    const discoverYearAllBtn = document.getElementById('discover-year-all');
    if (discoverYearAllBtn) {
      discoverYearAllBtn.setAttribute('data-run-trigger', '');
      discoverYearAllBtn.addEventListener('click', () => {
        const y = selectedYear();
        if (confirm(discoverYearMsg(y) + '\n(ธนาคารที่ไม่รองรับจะถูกข้ามอัตโนมัติ)')) startDiscoverYear(null, y);
      });
    }

    const discoverYearBtn = document.getElementById('discover-year-bank');
    if (discoverYearBtn) {
      discoverYearBtn.setAttribute('data-run-trigger', '');
      discoverYearBtn.addEventListener('click', () => {
        const y = selectedYear();
        if (confirm(discoverYearMsg(y))) startDiscoverYear(discoverYearBtn.dataset.code, y);
      });
    }
    // เช็คสถานะครั้งแรก: ถ้ามีงานค้างรันอยู่ (จากรีเฟรช/แท็บอื่น) จะ poll ต่อ
    // แต่ถ้าเป็นผลของ run เก่าที่จบไปแล้ว จะแค่แสดงสถานะเฉย ๆ ไม่ยิง reload
    poll();
  });
})();
