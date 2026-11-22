// Копирование шаблона отклика в буфер обмена
document.querySelectorAll('[data-copy]').forEach(btn => {
  btn.addEventListener('click', async () => {
    const text = document.getElementById(btn.dataset.copy).innerText;
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
