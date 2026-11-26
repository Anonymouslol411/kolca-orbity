// Админка: подтверждение удаления и «отметить всех»
(() => {
  const form = document.querySelector('[data-confirm-bulk]');
  if (!form) return;
  const all = form.querySelector('[data-check-all]');
  const boxes = () => [...form.querySelectorAll('input[name=ids]')];
  if (all) all.addEventListener('change', () => boxes().forEach(b => { b.checked = all.checked; }));
  form.addEventListener('submit', e => {
    const btn = e.submitter;
    let text;
    if (btn && btn.name === 'one') {
      boxes().forEach(b => { b.checked = b.value === btn.value; });
      text = `Удалить аккаунт ${btn.dataset.email}? Это нельзя отменить.`;
    } else {
      const n = boxes().filter(b => b.checked).length;
      if (!n) { e.preventDefault(); alert('Отметьте аккаунты галочками.'); return; }
      text = `Удалить отмеченные аккаунты (${n})? Это нельзя отменить.`;
    }
    if (!confirm(text)) { e.preventDefault(); return; }
    form.querySelector('input[name=confirm]').value = 'yes';
  });
})();

// Подтверждение для отдельных форм и предпросмотр цвета диска
document.querySelectorAll('form[data-confirm]').forEach(f => f.addEventListener('submit', e => {
  if (!confirm(f.dataset.confirm)) e.preventDefault();
}));
const hue = document.querySelector('[data-hue]'), prev = document.querySelector('[data-hue-preview]');
if (hue && prev) hue.addEventListener('input', () => prev.style.setProperty('--h', hue.value));
