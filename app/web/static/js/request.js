// request.js — กล่องคำขอสาธารณะ (ไม่ต้อง login): แจ้ง/ขออัปเดตอัตรา + เสนอธนาคารใหม่
// ยิง POST /api/request (JSON) แล้วสลับเป็นหน้า "ส่งสำเร็จ" หรือโชว์ error — ดีไซน์จาก claude design
(function () {
  // ผูกปุ่มเปิดกับ dialog (มีเฉพาะหน้าที่เกี่ยวข้อง — อีกหน้าจะข้ามเงียบ ๆ)
  [['report-rate', 'report-dialog'], ['suggest-bank', 'newbank-dialog']].forEach(function (pair) {
    var btn = document.getElementById(pair[0]);
    var dlg = document.getElementById(pair[1]);
    if (btn && dlg && typeof dlg.showModal === 'function') {
      btn.addEventListener('click', function () { resetDialog(dlg); dlg.showModal(); });
    }
  });

  function resetDialog(dlg) {
    var form = dlg.querySelector('.req-form');
    var success = dlg.querySelector('.req-success');
    var err = dlg.querySelector('.req-error');
    if (form) form.hidden = false;
    if (success) success.hidden = true;
    if (err) err.hidden = true;
  }

  document.querySelectorAll('dialog.req-modal').forEach(function (dlg) {
    dlg.querySelectorAll('[data-req-close]').forEach(function (b) {
      b.addEventListener('click', function () { dlg.close(); });
    });
    // คลิกนอกกล่อง (บน backdrop) = ปิด
    dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); });
    var form = dlg.querySelector('.req-form');
    if (form) form.addEventListener('submit', function (e) { submitForm(e, dlg, form); });
  });

  async function submitForm(e, dlg, form) {
    e.preventDefault();   // submit fire เฉพาะเมื่อผ่าน native validation (อีเมล required) แล้ว
    var err = dlg.querySelector('.req-error');
    var submitBtn = form.querySelector('.req-submit');
    var payload = {};

    if (form.dataset.reqType === 'newbank') {
      payload.type = 'newbank';
      payload.bank_name = val(form, 'bank_name');
      var link = val(form, 'link');
      if (link) payload.link = link;
    } else {
      var sel = form.querySelector('input[name=reqtype]:checked');
      payload.type = sel ? sel.value : 'update';
      payload.bank_code = form.dataset.bankCode;
    }
    payload.detail = val(form, 'detail');
    payload.email = val(form, 'email');
    var hp = form.querySelector('[name=website]');
    if (hp) payload.website = hp.value;

    if (err) err.hidden = true;
    submitBtn.disabled = true;
    var oldTxt = submitBtn.textContent;
    submitBtn.textContent = 'กำลังส่ง...';
    try {
      var res = await fetch('/api/request', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      var data = await res.json().catch(function () { return {}; });
      if (!res.ok) throw new Error(data.detail || '');
      form.hidden = true;
      var success = dlg.querySelector('.req-success');
      if (success) success.hidden = false;
    } catch (ex) {
      if (err) {
        err.textContent = ex.message || 'ส่งคำขอไม่สำเร็จ กรุณาลองใหม่อีกครั้ง';
        err.hidden = false;
      }
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = oldTxt;
    }
  }

  function val(form, name) {
    var el = form.querySelector('[name=' + name + ']');
    return el ? el.value.trim() : '';
  }
})();
