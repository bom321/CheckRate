(() => {
  const saveBtn = document.getElementById('save-manual');
  if (!saveBtn) return;
  const code = saveBtn.dataset.code;
  const msgEl = document.getElementById('msg');

  function setMsg(cls, text) {
    if (msgEl) msgEl.innerHTML = `<div class="notice ${cls}">${text}</div>`;
  }

  saveBtn.addEventListener('click', async () => {
    const inputs = document.querySelectorAll('#manual-table input[data-date]');
    const payload = {};
    let changedCount = 0;
    inputs.forEach((inp) => {
      const orig = inp.dataset.orig || '';
      const cur = inp.value.trim();
      if (cur === orig) return;   // ไม่แก้ไข ข้าม — ส่งเฉพาะช่องที่เปลี่ยนจริง
      const date = inp.dataset.date;
      const key = inp.dataset.key;
      payload[date] = payload[date] || {};
      payload[date][key] = cur === '' ? null : cur;   // ว่าง = ลบ override
      changedCount++;
    });
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
      setMsg('ok', `บันทึกแล้ว ${body.changed} ช่อง — กำลัง rebuild ข้อมูล (ใช้ cache จึงเร็วมาก)...`);
      setTimeout(() => location.reload(), 1500);
    } catch (e) {
      setMsg('err', 'เชื่อมต่อไม่ได้');
      saveBtn.disabled = false;
    }
  });
})();
