// ===== Данные меню =====
const MENU = [
  { name: 'Эспрессо', price: 150, cat: 'coffee', desc: 'Двойной шот на зерне недели' },
  { name: 'Капучино', price: 220, cat: 'coffee', desc: 'Классика с плотной молочной пеной', badge: 'Хит' },
  { name: 'Флэт уайт', price: 240, cat: 'coffee', desc: 'Больше кофе, меньше молока' },
  { name: 'Раф лаванда', price: 290, cat: 'coffee', desc: 'Сливки, ваниль и лавандовый сироп', badge: 'Авторский' },
  { name: 'Матча латте', price: 280, cat: 'tea', desc: 'Японская матча на растительном молоке' },
  { name: 'Какао с маршмеллоу', price: 230, cat: 'tea', desc: 'Бельгийский шоколад и зефир' },
  { name: 'Чай облепиховый', price: 260, cat: 'tea', desc: 'Облепиха, мёд, апельсин — 500 мл' },
  { name: 'Круассан с миндалём', price: 210, cat: 'food', desc: 'Печём каждое утро', badge: 'Новинка' },
  { name: 'Сырники', price: 320, cat: 'food', desc: 'Со сметаной и ягодным соусом' },
  { name: 'Чизкейк Нью-Йорк', price: 290, cat: 'food', desc: 'Нежный, с карамелью' },
];

const menuGrid = document.getElementById('menuGrid');

function renderMenu(filter = 'all') {
  const items = filter === 'all' ? MENU : MENU.filter(i => i.cat === filter);
  menuGrid.innerHTML = items.map((item, idx) => `
    <article class="menu-item" style="animation-delay:${idx * 50}ms">
      ${item.badge ? `<span class="badge">${item.badge}</span>` : ''}
      <div class="menu-item__top">
        <h3>${item.name}</h3>
        <span class="menu-item__price">${item.price} ₽</span>
      </div>
      <p>${item.desc}</p>
    </article>
  `).join('');
}

document.querySelectorAll('.filter').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelector('.filter.is-active').classList.remove('is-active');
    btn.classList.add('is-active');
    renderMenu(btn.dataset.filter);
  });
});
renderMenu();

// ===== Мобильное меню =====
const burger = document.getElementById('burger');
const nav = document.getElementById('nav');
burger.addEventListener('click', () => {
  burger.classList.toggle('is-open');
  nav.classList.toggle('is-open');
});
nav.querySelectorAll('a').forEach(a => a.addEventListener('click', () => {
  burger.classList.remove('is-open');
  nav.classList.remove('is-open');
}));

// ===== Тень у шапки при скролле =====
const header = document.getElementById('header');
window.addEventListener('scroll', () => {
  header.classList.toggle('is-scrolled', window.scrollY > 10);
});

// ===== Появление блоков и счётчики =====
const observer = new IntersectionObserver(entries => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      entry.target.classList.add('is-visible');
      observer.unobserve(entry.target);
    }
  });
}, { threshold: 0.15 });
document.querySelectorAll('.reveal').forEach(el => observer.observe(el));

function animateCounter(el) {
  const target = +el.dataset.count;
  const duration = 1500;
  const start = performance.now();
  function tick(now) {
    const progress = Math.min((now - start) / duration, 1);
    el.textContent = Math.floor(target * (1 - Math.pow(1 - progress, 3))).toLocaleString('ru-RU');
    if (progress < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}
document.querySelectorAll('[data-count]').forEach(animateCounter);

// ===== Слайдер отзывов =====
const track = document.getElementById('sliderTrack');
const dotsWrap = document.getElementById('sliderDots');
const slidesCount = track.children.length;
let current = 0;

for (let i = 0; i < slidesCount; i++) {
  const dot = document.createElement('button');
  dot.setAttribute('aria-label', `Отзыв ${i + 1}`);
  dot.addEventListener('click', () => goTo(i));
  dotsWrap.appendChild(dot);
}
function goTo(i) {
  current = (i + slidesCount) % slidesCount;
  track.style.transform = `translateX(-${current * 100}%)`;
  [...dotsWrap.children].forEach((d, idx) => d.classList.toggle('is-active', idx === current));
}
goTo(0);
setInterval(() => goTo(current + 1), 5000);

// ===== Форма бронирования =====
const form = document.getElementById('bookingForm');
const message = document.getElementById('formMessage');
const dateInput = form.elements.date;
dateInput.min = new Date().toISOString().split('T')[0];

// Маска телефона
form.elements.phone.addEventListener('input', e => {
  let digits = e.target.value.replace(/\D/g, '');
  if (digits.startsWith('8')) digits = '7' + digits.slice(1);
  if (!digits.startsWith('7')) digits = '7' + digits;
  digits = digits.slice(0, 11);
  const p = digits.padEnd(11, '_');
  e.target.value = `+7 (${p.slice(1, 4)}) ${p.slice(4, 7)}-${p.slice(7, 9)}-${p.slice(9, 11)}`.replace(/[-\s(]*_.*$/, '');
});

form.addEventListener('submit', e => {
  e.preventDefault();
  const { name, phone, date } = form.elements;
  const checks = [
    [name, name.value.trim().length >= 2],
    [phone, phone.value.replace(/\D/g, '').length === 11],
    [date, !!date.value],
  ];
  let valid = true;
  checks.forEach(([field, ok]) => {
    field.classList.toggle('is-invalid', !ok);
    if (!ok) valid = false;
  });

  if (!valid) {
    message.className = 'form__message err';
    message.textContent = 'Проверьте, пожалуйста, выделенные поля';
    return;
  }

  // Здесь можно отправить данные на сервер или в Telegram-бота через fetch()
  message.className = 'form__message ok';
  message.textContent = `Спасибо, ${name.value.trim()}! Мы скоро перезвоним.`;
  form.reset();
});
