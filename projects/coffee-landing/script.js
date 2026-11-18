// ===== Данные меню =====
// img — id фотографии на Unsplash
const MENU = [
  { name: 'Эспрессо', price: 150, cat: 'coffee', desc: 'Двойной шот на зерне недели', img: '1514432324607-a09d9b4aefdd' },
  { name: 'Капучино', price: 220, cat: 'coffee', desc: 'Классика с плотной молочной пеной', badge: 'Хит', img: '1572442388796-11668a67e53d' },
  { name: 'Флэт уайт', price: 240, cat: 'coffee', desc: 'Больше кофе, меньше молока', img: '1534778101976-62847782c213' },
  { name: 'Кортадо', price: 210, cat: 'coffee', desc: 'Эспрессо и немного тёплого молока', img: '1559496417-e7f25cb247f3' },
  { name: 'Раф лаванда', price: 290, cat: 'coffee', desc: 'Сливки, ваниль и лавандовый сироп', badge: 'Авторский', img: '1509042239860-f550ce710b93' },
  { name: 'Фильтр V60', price: 230, cat: 'coffee', desc: 'Пуровер на зерне из Эфиопии', img: '1442512595331-e89e73853f31' },
  { name: 'Айс-латте', price: 260, cat: 'cold', desc: 'Эспрессо, молоко и много льда', img: '1517701604599-bb29b565090c' },
  { name: 'Колд брю', price: 250, cat: 'cold', desc: 'Настаиваем 18 часов в холоде', badge: 'Новинка', img: '1461023058943-07fcbe16d735' },
  { name: 'Круассан', price: 190, cat: 'food', desc: 'Слоёный, на сливочном масле', img: '1555507036-ab1f4038808a' },
  { name: 'Черничный чизкейк', price: 290, cat: 'food', desc: 'Нежный сливочный, с ягодами', img: '1533134242443-d4fd215305ad' },
  { name: 'Шоколадный торт', price: 320, cat: 'food', desc: 'Бельгийский шоколад и ганаш', img: '1578985545062-69928b1d9587' },
  { name: 'Панна-котта', price: 270, cat: 'food', desc: 'Со свежей клубникой', img: '1488477181946-6428a0291777' },
];

const photo = (id, w = 600) => `https://images.unsplash.com/photo-${id}?auto=format&fit=crop&w=${w}&q=70`;
const menuGrid = document.getElementById('menuGrid');

function renderMenu(filter = 'all') {
  const items = filter === 'all' ? MENU : MENU.filter(i => i.cat === filter);
  menuGrid.innerHTML = items.map((item, idx) => `
    <article class="menu-item" style="animation-delay:${idx * 60}ms">
      <div class="menu-item__img">
        <img src="${photo(item.img)}" alt="${item.name}" loading="lazy">
        ${item.badge ? `<span class="badge">${item.badge}</span>` : ''}
      </div>
      <div class="menu-item__body">
        <div class="menu-item__top">
          <h3>${item.name}</h3>
          <span class="menu-item__price">${item.price} ₽</span>
        </div>
        <p>${item.desc}</p>
      </div>
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

// ===== Шапка при скролле =====
const header = document.getElementById('header');
const onScroll = () => header.classList.toggle('is-scrolled', window.scrollY > 10);
window.addEventListener('scroll', onScroll, { passive: true });
onScroll();

// ===== Появление блоков и счётчики =====
const observer = new IntersectionObserver(entries => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      entry.target.classList.add('is-visible');
      observer.unobserve(entry.target);
    }
  });
}, { threshold: 0.12 });
document.querySelectorAll('.reveal').forEach(el => observer.observe(el));

function animateCounter(el) {
  const target = +el.dataset.count;
  const duration = 1600;
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
let timer;

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
  clearInterval(timer);
  timer = setInterval(() => goTo(current + 1), 6000);
}
document.getElementById('prevSlide').addEventListener('click', () => goTo(current - 1));
document.getElementById('nextSlide').addEventListener('click', () => goTo(current + 1));
goTo(0);

// ===== Форма бронирования =====
const form = document.getElementById('bookingForm');
const message = document.getElementById('formMessage');
form.elements.date.min = new Date().toISOString().split('T')[0];

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
