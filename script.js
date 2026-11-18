// ===== Тема (тёмная/светлая) =====
const root = document.documentElement;
try {
  const saved = localStorage.getItem('theme');
  if (saved) root.dataset.theme = saved;
} catch (e) {}
document.getElementById('themeToggle').addEventListener('click', () => {
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

// ===== Шапка при скролле =====
const header = document.getElementById('header');
const onScroll = () => header.classList.toggle('is-scrolled', window.scrollY > 10);
window.addEventListener('scroll', onScroll, { passive: true });
onScroll();

// ===== Появление блоков при скролле =====
const observer = new IntersectionObserver(entries => {
  entries.forEach(e => {
    if (e.isIntersecting) {
      e.target.classList.add('is-visible');
      observer.unobserve(e.target);
    }
  });
}, { threshold: 0.1 });
document.querySelectorAll('.reveal').forEach(el => observer.observe(el));

// ===== Подсветка карточек за курсором =====
document.querySelectorAll('.spotlight').forEach(el => {
  el.addEventListener('pointermove', e => {
    const r = el.getBoundingClientRect();
    el.style.setProperty('--mx', `${e.clientX - r.left}px`);
    el.style.setProperty('--my', `${e.clientY - r.top}px`);
  });
});

// ===== Печатающийся код в редакторе =====
// Каждая строка — массив [класс, текст]; класс '' — обычный текст
const CODE = [
  [['c', '# Бот онлайн-записи · aiogram 3']],
  [['k', 'from'], ['', ' aiogram '], ['k', 'import'], ['', ' Router, F']],
  [],
  [['', 'router = '], ['f', 'Router'], ['', '()']],
  [],
  [['d', '@router.message'], ['', '(F.text == '], ['s', '"📅 Записаться"'], ['', ')']],
  [['k', 'async def'], ['f', ' booking_start'], ['', '(message, state):']],
  [['', '    '], ['k', 'await'], ['', ' state.'], ['f', 'set_state'], ['', '(Booking.service)']],
  [['', '    '], ['k', 'await'], ['', ' message.'], ['f', 'answer'], ['', '(']],
  [['', '        '], ['s', '"Выберите услугу:"'], ['', ',']],
  [['', '        reply_markup=kb.'], ['f', 'services_kb'], ['', '(),']],
  [['', '    )']],
];

const codeEl = document.getElementById('code');
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function renderCode(lineIdx, charIdx) {
  let html = '';
  for (let i = 0; i <= lineIdx && i < CODE.length; i++) {
    let left = i < lineIdx ? Infinity : charIdx;
    for (const [cls, text] of CODE[i]) {
      if (left <= 0) break;
      const part = escapeHtml([...text].slice(0, left).join(''));
      left -= [...text].length;
      html += cls ? `<span class="${cls}">${part}</span>` : part;
    }
    if (i < lineIdx) html += '\n';
  }
  codeEl.innerHTML = html + '<span class="caret"></span>';
}

if (reducedMotion) {
  renderCode(CODE.length - 1, Infinity);
} else {
  let line = 0, ch = 0;
  (function type() {
    const lineLen = (CODE[line] || []).reduce((n, [, t]) => n + [...t].length, 0);
    renderCode(line, ch);
    if (ch < lineLen) { ch++; setTimeout(type, 28); return; }
    if (line < CODE.length - 1) { line++; ch = 0; setTimeout(type, 120); return; }
    setTimeout(() => { line = 0; ch = 0; type(); }, 4000);
  })();
}

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
