// Копирование в буфер обмена: data-copy="id блока" — его текст, data-copy-text="..." — готовый текст (ссылка)
document.querySelectorAll('[data-copy], [data-copy-text]').forEach(btn => {
  btn.addEventListener('click', async () => {
    const text = btn.dataset.copyText ?? document.getElementById(btn.dataset.copy).innerText;
    try {
      await navigator.clipboard.writeText(text);
      const old = btn.textContent;
      btn.textContent = '✓ Скопировано';
      setTimeout(() => { btn.textContent = old; }, 1500);
    } catch (e) {
      alert('Не удалось скопировать — выделите текст вручную');
    }
  });
});

// Подтверждение удаления
document.querySelectorAll('form[data-confirm]').forEach(form => {
  form.addEventListener('submit', e => {
    if (!confirm(form.dataset.confirm)) e.preventDefault();
  });
});

// «Показать полностью» — раскрыть описание заказа в списке
document.querySelectorAll('[data-expand]').forEach(btn => {
  btn.addEventListener('click', () => {
    const desc = btn.previousElementSibling;
    const open = desc.classList.toggle('open');
    btn.textContent = open ? 'Свернуть ▴' : 'Показать полностью ▾';
  });
});
