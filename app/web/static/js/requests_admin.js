// requests_admin.js — ปุ่มเปลี่ยนสถานะคำขอในหน้า /requests (admin)
// ยิง POST /api/requests/{id} {status} แล้วรีโหลดหน้า (อัปเดตทั้งตัวกรอง จำนวน และ badge ให้ตรงกัน)
(function () {
  document.querySelectorAll('.req-card').forEach(function (card) {
    var id = card.dataset.id;
    card.querySelectorAll('[data-status]').forEach(function (btn) {
      btn.addEventListener('click', async function () {
        btn.disabled = true;
        try {
          var res = await fetch('/api/requests/' + encodeURIComponent(id), {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: btn.dataset.status }),
          });
          if (!res.ok) throw new Error();
          location.reload();
        } catch (e) {
          btn.disabled = false;
          alert('อัปเดตสถานะไม่สำเร็จ กรุณาลองใหม่');
        }
      });
    });
  });
})();
