(() => {
  const saveBtn = document.getElementById('save-manual');
  if (!saveBtn) return;
  const code = saveBtn.dataset.code;
  const msgEl = document.getElementById('msg');
  const monthSelect = document.getElementById('manual-month-select');

  function setMsg(cls, text) {
    if (msgEl) msgEl.innerHTML = `<div class="notice ${cls}">${text}</div>`;
  }

  // เก็บเฉพาะช่องที่ค่าเปลี่ยนจริง (value !== data-orig) — ใช้ทั้งตอนบันทึกและตอนเช็คว่ามี edit ค้างไหม
  function collectChanges() {
    const inputs = document.querySelectorAll('#manual-table input[data-date]');
    const payload = {};
    let changedCount = 0;
    inputs.forEach((inp) => {
      const orig = inp.dataset.orig || '';
      const cur = inp.value.trim();
      if (cur === orig) return;
      const date = inp.dataset.date;
      const key = inp.dataset.key;
      payload[date] = payload[date] || {};
      payload[date][key] = cur === '' ? null : cur;   // ว่าง = ลบ override
      changedCount++;
    });
    return { payload, changedCount };
  }

  let dirty = false;
  document.querySelectorAll('#manual-table input[data-date]').forEach((inp) => {
    inp.addEventListener('input', () => { dirty = collectChanges().changedCount > 0; });
  });

  window.addEventListener('beforeunload', (e) => {
    if (!dirty) return;
    e.preventDefault();
    e.returnValue = '';
  });

  if (monthSelect) {
    const initialMonth = monthSelect.value;
    monthSelect.addEventListener('change', () => {
      if (dirty && !confirm('มีช่องที่แก้ไขแล้วยังไม่ได้บันทึก — เปลี่ยนเดือนตอนนี้จะทำให้ค่าที่แก้หายไป ต้องการเปลี่ยนต่อหรือไม่?')) {
        monthSelect.value = initialMonth;
        return;
      }
      dirty = false;
      location.href = `/bank/${code}/manual?month=${encodeURIComponent(monthSelect.value)}`;
    });
  }

  saveBtn.addEventListener('click', async () => {
    const { payload, changedCount } = collectChanges();
    if (!changedCount) {
      setMsg('err', 'ไม่มีช่องที่แก้ไข');
      return;
    }

    saveBtn.disabled = true;
    setMsg('ok', '⏳ กำลังบันทึก...');
    try {
      const res = await fetch(`/api/manual/${code}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setMsg('err', body.detail || 'บันทึกไม่สำเร็จ');
        saveBtn.disabled = false;
        return;
      }
      dirty = false;
      setMsg('ok', `บันทึกแล้ว ${body.changed} ช่อง — กำลัง rebuild ข้อมูล (ใช้ cache จึงเร็วมาก)...`);
      setTimeout(() => location.reload(), 1500);
    } catch (e) {
      setMsg('err', 'เชื่อมต่อไม่ได้');
      saveBtn.disabled = false;
    }
  });
})();
