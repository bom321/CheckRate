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

  async function _startJob(endpoint, only, startingText) {
    setButtonsDisabled(true);
    setStatus('running', startingText);
    if (outputEl) outputEl.style.display = 'none';
    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(only ? { only: only } : {}),
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

  function startBackfill(only) {
    return _startJob('/api/backfill', only, '⏳ กำลังเติมข้อมูลย้อนหลัง...');
  }

  function startDiscoverYear(only) {
    return _startJob('/api/discover-year', only, '⏳ กำลังสแกนหาประวัติทั้งปี (ใช้เวลานาน)...');
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

    const BACKFILL_MSG = 'เติมข้อมูลย้อนหลังจาก PDF ที่เก็บไว้?\n' +
      'ระบบจะสร้างไฟล์ CSV ใหม่จาก PDF ทั้งหมดที่เก็บไว้ (ค่าที่ติดตามใหม่จะถูกเติมย้อนหลัง)';

    const backfillAllBtn = document.getElementById('backfill-all');
    if (backfillAllBtn) {
      backfillAllBtn.setAttribute('data-run-trigger', '');
      backfillAllBtn.addEventListener('click', () => { if (confirm(BACKFILL_MSG)) startBackfill(null); });
    }

    const backfillBankBtn = document.getElementById('backfill-bank');
    if (backfillBankBtn) {
      backfillBankBtn.setAttribute('data-run-trigger', '');
      backfillBankBtn.addEventListener('click', () => { if (confirm(BACKFILL_MSG)) startBackfill(backfillBankBtn.dataset.code); });
    }

    const DISCOVER_YEAR_MSG = 'สแกนหาประกาศทั้งปีนี้แบบละเอียด?\n' +
      'ระบบจะไล่ตรวจทุกวันตั้งแต่ต้นปีถึงวันนี้ (ใช้เวลาหลายนาที) ดาวน์โหลดไฟล์ที่ยังไม่มีในเครื่อง ' +
      'แล้วสร้าง CSV ใหม่ให้อัตโนมัติ — ใช้เมื่อสงสัยว่ามีประกาศบางช่วงที่การตรวจสอบปกติพลาดไป';

    const discoverYearAllBtn = document.getElementById('discover-year-all');
    if (discoverYearAllBtn) {
      discoverYearAllBtn.setAttribute('data-run-trigger', '');
      discoverYearAllBtn.addEventListener('click', () => {
        if (confirm(DISCOVER_YEAR_MSG + '\n(ธนาคารที่ไม่รองรับจะถูกข้ามอัตโนมัติ)')) startDiscoverYear(null);
      });
    }

    const discoverYearBtn = document.getElementById('discover-year-bank');
    if (discoverYearBtn) {
      discoverYearBtn.setAttribute('data-run-trigger', '');
      discoverYearBtn.addEventListener('click', () => {
        if (confirm(DISCOVER_YEAR_MSG)) startDiscoverYear(discoverYearBtn.dataset.code);
      });
    }
    // เช็คสถานะครั้งแรก: ถ้ามีงานค้างรันอยู่ (จากรีเฟรช/แท็บอื่น) จะ poll ต่อ
    // แต่ถ้าเป็นผลของ run เก่าที่จบไปแล้ว จะแค่แสดงสถานะเฉย ๆ ไม่ยิง reload
    poll();
  });
})();
