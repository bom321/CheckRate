// config.js — จัดการ banks_config.json + settings.json (ผู้รับอีเมล) ผ่านหน้าเว็บ

(function () {
  let state = { banks: [], settings: {} };

  const container = document.getElementById('banks-container');
  const msgEl = document.getElementById('msg');

  function notice(kind, text) {
    msgEl.innerHTML = `<div class="notice ${kind}">${text}</div>`;
    setTimeout(() => { msgEl.innerHTML = ''; }, 5000);
  }

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function targetRowHtml(bIdx, tIdx, t) {
    return `
      <div class="cfg-row" data-bank="${bIdx}" data-target="${tIdx}">
        <input type="text" class="t-key" value="${esc(t.key)}" placeholder="key เช่น rate_3m_1m">
        <input type="text" class="t-section" value="${esc(t.section_keyword || '')}" placeholder="section (ว่าง=ค่าเริ่มต้น)">
        <input type="text" class="t-row" value="${esc(t.row_keyword || '')}" placeholder="row (ว่าง=ตามเดือน)">
        <input type="text" class="t-depositor" value="${esc(t.depositor ?? '')}" placeholder="ผู้รับดอกเบี้ย (ว่าง=บุคคลธรรมดา)">
        <input type="number" step="1" class="t-tenor" value="${t.tenor_months ?? ''}" placeholder="เดือน">
        <input type="number" step="0.1" class="t-amount" value="${t.amount_m ?? ''}" placeholder="ล้านบาท">
        <input type="text" class="t-label" value="${esc(t.alias || t.label || '')}" placeholder="ชื่อ/alias ที่แสดงผล">
        <button class="btn small t-remove" title="ลบแถวนี้">✕</button>
      </div>`;
  }

  function bankCardHtml(b, bIdx) {
    const targets = (b.rate_targets || []).map((t, tIdx) => targetRowHtml(bIdx, tIdx, t)).join('');
    return `
    <div class="cfg-bank" data-bank-idx="${bIdx}">
      <div class="bank-head" style="padding:0 0 12px;border:none">
        <div class="bank-title">${esc(b.name)} <span class="code">(${esc(b.code)})</span></div>
        <label class="switch"><input type="checkbox" class="b-enabled" ${b.enabled ? 'checked' : ''}> เปิดใช้งาน</label>
      </div>

      <div class="cfg-field">
        <label>Latest PDF URL</label>
        <input type="text" class="b-latest-url" value="${esc(b.latest_pdf_url)}">
      </div>
      <div class="cfg-field">
        <label>Previous PDF URL</label>
        <input type="text" class="b-prev-url" value="${esc(b.prev_pdf_url)}">
      </div>
      <div class="cfg-field">
        <label>Referer</label>
        <input type="text" class="b-referer" value="${esc(b.referer)}">
      </div>
      ${b.latest_pdf_url ? `<p style="margin:4px 0 12px"><a class="link" href="${esc(b.latest_pdf_url)}" target="_blank" rel="noopener">เปิดลิงก์เอกสารปัจจุบัน ↗</a></p>` : ''}

      <div class="cfg-field"><label>อัตราที่ติดตาม (rate_targets)</label></div>
      <div class="cfg-row head">
        <div>Key</div><div>Section (ประเภทบัญชี)</div><div>Row (ผลิตภัณฑ์/ระยะเวลา)</div><div>ผู้รับดอกเบี้ย</div><div>เดือน</div><div>ล้านบาท</div><div>ชื่อ/alias ที่แสดง</div><div></div>
      </div>
      <div class="targets-list">${targets}</div>
      <button class="btn small add-target">+ เพิ่มอัตรา</button>
    </div>`;
  }

  function render() {
    container.innerHTML = state.banks.map((b, i) => bankCardHtml(b, i)).join('');
    document.getElementById('email-to').value =
      Array.isArray(state.settings.email_to) ? state.settings.email_to.join(', ') : (state.settings.email_to || '');
    wireEvents();
  }

  function readFormIntoState() {
    container.querySelectorAll('.cfg-bank').forEach(card => {
      const bIdx = Number(card.dataset.bankIdx);
      const b = state.banks[bIdx];
      b.enabled = card.querySelector('.b-enabled').checked;
      b.latest_pdf_url = card.querySelector('.b-latest-url').value.trim();
      b.prev_pdf_url = card.querySelector('.b-prev-url').value.trim();
      b.referer = card.querySelector('.b-referer').value.trim();
      const targets = [];
      card.querySelectorAll('.cfg-row[data-target]').forEach(row => {
        const key = row.querySelector('.t-key').value.trim();
        if (!key) return;
        const section = row.querySelector('.t-section').value.trim();
        const rowKw = row.querySelector('.t-row').value.trim();
        const depositor = row.querySelector('.t-depositor').value.trim();
        const tenor = row.querySelector('.t-tenor').value;
        const amount = row.querySelector('.t-amount').value;
        const label = row.querySelector('.t-label').value.trim();
        const target = {
          key,
          tenor_months: tenor === '' ? null : Number(tenor),
          amount_m: amount === '' ? null : Number(amount),
          label: label || key,
          alias: label || undefined,
        };
        if (section) target.section_keyword = section;
        if (rowKw) target.row_keyword = rowKw;
        if (depositor) target.depositor = depositor;
        targets.push(target);
      });
      b.rate_targets = targets;
    });
  }

  function wireEvents() {
    container.querySelectorAll('.add-target').forEach(btn => {
      btn.addEventListener('click', () => {
        readFormIntoState();
        const card = btn.closest('.cfg-bank');
        const bIdx = Number(card.dataset.bankIdx);
        state.banks[bIdx].rate_targets.push({ key: '', tenor_months: null, amount_m: null, label: '' });
        render();
      });
    });
    container.querySelectorAll('.t-remove').forEach(btn => {
      btn.addEventListener('click', () => {
        readFormIntoState();
        const row = btn.closest('.cfg-row');
        const bIdx = Number(row.dataset.bank);
        const tIdx = Number(row.dataset.target);
        state.banks[bIdx].rate_targets.splice(tIdx, 1);
        render();
      });
    });
  }

  function validateClientSide() {
    for (const b of state.banks) {
      const seen = new Set();
      for (const t of b.rate_targets) {
        if (!t.key) return `[${b.code}] มี rate target ที่ยังไม่ได้ตั้ง key`;
        if (seen.has(t.key)) return `[${b.code}] key ซ้ำ: ${t.key}`;
        seen.add(t.key);
        if (!t.row_keyword && !t.tenor_months) {
          return `[${b.code}] '${t.key}': ต้องระบุ "Row (ผลิตภัณฑ์/ระยะเวลา)" หรือ "เดือน" อย่างน้อยหนึ่งอย่าง`;
        }
      }
    }
    return null;
  }

  async function loadConfig() {
    const res = await fetch('/api/config');
    state = await res.json();
    render();
  }

  async function saveConfig() {
    readFormIntoState();
    const err = validateClientSide();
    if (err) { notice('err', err); return; }
    const res = await fetch('/api/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ banks: state.banks }),
    });
    const data = await res.json();
    if (data.ok) notice('ok', '✓ บันทึกการตั้งค่าธนาคารเรียบร้อย');
    else notice('err', 'บันทึกไม่สำเร็จ: ' + (data.error || ''));
  }

  async function saveSettings() {
    const raw = document.getElementById('email-to').value;
    const emails = raw.split(',').map(s => s.trim()).filter(Boolean);
    const res = await fetch('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email_to: emails }),
    });
    const data = await res.json();
    if (data.ok) notice('ok', '✓ บันทึกผู้รับอีเมลเรียบร้อย (' + (data.recipients || []).join(', ') + ')');
    else notice('err', 'บันทึกไม่สำเร็จ: ' + (data.error || ''));
  }

  document.getElementById('save-config').addEventListener('click', () => {
    if (confirm('ยืนยันบันทึกการตั้งค่าธนาคาร? การเปลี่ยนแปลงจะมีผลกับการรันครั้งถัดไป')) saveConfig();
  });
  document.getElementById('reload-config').addEventListener('click', () => {
    if (confirm('โหลดข้อมูลใหม่และยกเลิกการแก้ไขที่ยังไม่บันทึก?')) loadConfig();
  });
  document.getElementById('save-settings').addEventListener('click', saveSettings);

  loadConfig();
})();
