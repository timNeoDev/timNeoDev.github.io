# Бот онлайн-записи для барбершопа

Telegram-бот для записи клиентов на услуги без звонков.

## Возможности
- Выбор услуги, даты (7 дней вперёд) и свободного времени
- Занятые и прошедшие слоты скрываются автоматически
- Номер телефона можно отправить одной кнопкой
- «Мои записи» с отменой записи
- Уведомления администратору о новых записях и отменах
- Команда `/today` для администратора — расписание на сегодня
- Защита от двойного бронирования одного слота (ограничение UNIQUE в БД)

## Стек
Python 3.11+, aiogram 3, SQLite, FSM (машина состояний)

## Запуск
```bash
python -m venv venv
venv\Scripts\activate          # Windows (Linux/macOS: source venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env         # Linux/macOS: cp .env.example .env
```
1. Создайте бота у [@BotFather](https://t.me/BotFather) и вставьте токен в `.env`.
2. Узнайте свой ID у [@userinfobot](https://t.me/userinfobot) и вставьте его в `ADMIN_ID`.
3. Запустите: `python bot.py`

Услуги, цены и часы работы меняются в `config.py`.

## Структура
```
bot.py        — обработчики и запуск
keyboards.py  — клавиатуры и callback-данные
db.py         — работа с SQLite
config.py     — настройки
```
