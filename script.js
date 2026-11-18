// ===== Тема (тёмная/светлая) =====
const root = document.documentElement;
const themeToggle = document.getElementById('themeToggle');
try {
  const saved = localStorage.getItem('theme');
  if (saved) root.dataset.theme = saved;
} catch (e) {}
themeToggle.addEventListener('click', () => {
  root.dataset.theme = root.dataset.theme === 'light' ? 'dark' : 'light';
  try { localStorage.setItem('theme', root.dataset.theme); } catch (e) {}
});

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

// ===== Появление блоков при скролле =====
const observer = new IntersectionObserver(entries => {
  entries.forEach(e => {
    if (e.isIntersecting) {
      e.target.classList.add('is-visible');
      observer.unobserve(e.target);
    }
  });
}, { threshold: 0.12 });
document.querySelectorAll('.reveal').forEach(el => observer.observe(el));

// ===== Анимированная демо-переписка с ботом =====
const chat = document.getElementById('chat');
const SCRIPT = [
  { from: 'user', text: '📅 Записаться' },
  { from: 'bot', text: 'Выберите услугу:', btns: ['Стрижка', 'Борода', 'Комплекс'] },
  { from: 'user', text: 'Стрижка + борода' },
  { from: 'bot', text: 'Выберите дату:', btns: ['Сегодня', 'Чт 20.11', 'Пт 21.11'] },
  { from: 'user', text: 'Пт 21.11' },
  { from: 'bot', text: 'Выберите время:', btns: ['11:00', '14:00', '17:00'] },
  { from: 'user', text: '17:00' },
  { from: 'bot', text: '✅ Вы записаны!\nСтрижка + борода\nПт, 21.11 в 17:00' },
];

function addMessage({ from, text, btns }) {
  const el = document.createElement('div');
  el.className = `msg msg--${from}`;
  el.textContent = text;
  if (btns) {
    const wrap = document.createElement('div');
    wrap.className = 'msg__btns';
    btns.forEach(b => {
      const s = document.createElement('span');
      s.textContent = b;
      wrap.appendChild(s);
    });
    el.appendChild(wrap);
  }
  chat.appendChild(el);
  while (chat.scrollHeight > chat.clientHeight && chat.children.length > 1) {
    chat.firstElementChild.remove();
  }
}

let step = 0;
function playChat() {
  if (step === SCRIPT.length) {
    setTimeout(() => { chat.innerHTML = ''; step = 0; playChat(); }, 3500);
    return;
  }
  addMessage(SCRIPT[step++]);
  setTimeout(playChat, 1200);
}

const chatObserver = new IntersectionObserver(([entry]) => {
  if (entry.isIntersecting) {
    playChat();
    chatObserver.disconnect();
  }
});
chatObserver.observe(chat);
