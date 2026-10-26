"""Telegram-бот записи в барбершоп: aiogram 3 + SQLite3.

Путь клиента (7 шагов): старт → услуга → мастер → день → время → телефон → подтверждение.
Повторному клиенту шаг с телефоном не показывается. Бот напоминает о визите
за сутки и за 2 часа, из напоминания можно подтвердить приход или отменить запись.

Услуги и цены лежат в data/services.json, расписание мастеров в data/masters.json.
Токен и настройки читаются из .env (BOT_TOKEN, ADMIN_CHAT_ID, TZ).
"""
import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove)
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bookings.db")
SLOT_STEP = 30          # сетка начала записи, минут
MIN_LEAD = 60           # раньше чем за час до начала записаться нельзя, минут
DAYS_AHEAD = 7          # сколько дней показываем
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
SHOP = "Барбершоп «Бритва»"
ADDRESS = "ул. Лесная, 12, вход со двора"

load_dotenv(os.path.join(BASE_DIR, ".env"))
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()

dp = Dispatcher(storage=MemoryStorage())


class Booking(StatesGroup):
    """Шаги записи, на которых бот ждёт ввод текстом (остальное кнопками)."""
    waiting_phone = State()


# ---------- Данные: услуги и мастера ----------

def load_json(name: str) -> list:
    with open(os.path.join(BASE_DIR, "data", name), encoding="utf-8") as f:
        return json.load(f)


SERVICES = {s["id"]: s for s in load_json("services.json")}
MASTERS = {m["id"]: m for m in load_json("masters.json")}


def now_local() -> datetime:
    """Текущее время барбершопа без tzinfo: в базе всё хранится в местном времени."""
    return datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)


def fmt_day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"


def fmt_price(p: int) -> str:
    return f"{p:,}".replace(",", " ") + " ₽"


# ---------- База данных ----------

def init_db(db_path: str = DB_PATH) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS bookings (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id INTEGER NOT NULL,
                   client_name TEXT NOT NULL,
                   phone TEXT NOT NULL,
                   service_id TEXT NOT NULL,
                   master_id TEXT NOT NULL,
                   start_at TEXT NOT NULL,
                   end_at TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active',
                   reminded_24 INTEGER NOT NULL DEFAULT 0,
                   reminded_2 INTEGER NOT NULL DEFAULT 0,
                   created_at TEXT NOT NULL
               )"""
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS clients (user_id INTEGER PRIMARY KEY, phone TEXT NOT NULL)"
        )


def busy_intervals(master_id: str, day: date, db_path: str = DB_PATH) -> list:
    """Занятые интервалы мастера на день: [(начало, конец), ...]."""
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT start_at, end_at FROM bookings WHERE master_id = ? AND status = 'active' "
            "AND substr(start_at, 1, 10) = ?",
            (master_id, day.isoformat()),
        ).fetchall()
    return [(datetime.fromisoformat(s), datetime.fromisoformat(e)) for s, e in rows]


def master_slots(master_id: str, day: date, minutes: int, now: datetime,
                 db_path: str = DB_PATH) -> list:
    """Свободные начала записи у мастера в этот день под услугу длительностью minutes."""
    hours = MASTERS[master_id]["schedule"].get(str(day.weekday()))
    if not hours:
        return []
    start_s, end_s = hours.split("-")
    work_start = datetime.combine(day, time.fromisoformat(start_s))
    work_end = datetime.combine(day, time.fromisoformat(end_s))
    busy = busy_intervals(master_id, day, db_path)
    dur = timedelta(minutes=minutes)
    earliest = now + timedelta(minutes=MIN_LEAD)
    slots, t = [], work_start
    while t + dur <= work_end:
        if t >= earliest and all(t + dur <= b_start or t >= b_end for b_start, b_end in busy):
            slots.append(t.strftime("%H:%M"))
        t += timedelta(minutes=SLOT_STEP)
    return slots


def free_slots(master_id: str, day: date, minutes: int, now: datetime,
               db_path: str = DB_PATH) -> dict:
    """Свободное время → какой мастер свободен. master_id='any' собирает время всех мастеров."""
    ids = list(MASTERS) if master_id == "any" else [master_id]
    result = {}
    for mid in ids:
        for slot in master_slots(mid, day, minutes, now, db_path):
            result.setdefault(slot, mid)
    return dict(sorted(result.items()))


def days_with_slots(master_id: str, minutes: int, now: datetime, db_path: str = DB_PATH) -> list:
    days = [now.date() + timedelta(days=i) for i in range(DAYS_AHEAD)]
    return [d for d in days if free_slots(master_id, d, minutes, now, db_path)]


def create_booking(user_id: int, client_name: str, phone: str, service_id: str, master_id: str,
                   start: datetime, now: datetime, db_path: str = DB_PATH):
    """Сохраняет запись, если время всё ещё свободно. Возвращает id записи или None."""
    minutes = SERVICES[service_id]["minutes"]
    if start.strftime("%H:%M") not in master_slots(master_id, start.date(), minutes, now, db_path):
        return None
    end = start + timedelta(minutes=minutes)
    # если до визита меньше суток (или 2 часов), соответствующее напоминание уже не нужно
    r24 = int(start - now <= timedelta(hours=24))
    r2 = int(start - now <= timedelta(hours=2))
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO bookings (user_id, client_name, phone, service_id, master_id, start_at, "
            "end_at, reminded_24, reminded_2, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, client_name, phone, service_id, master_id, start.isoformat(timespec="minutes"),
             end.isoformat(timespec="minutes"), r24, r2, now.isoformat(timespec="minutes")),
        )
        conn.execute("INSERT OR REPLACE INTO clients (user_id, phone) VALUES (?, ?)", (user_id, phone))
        return cur.lastrowid


def get_booking(booking_id: int, db_path: str = DB_PATH):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()


def user_bookings(user_id: int, now: datetime, db_path: str = DB_PATH) -> list:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM bookings WHERE user_id = ? AND status = 'active' AND start_at > ? "
            "ORDER BY start_at",
            (user_id, now.isoformat(timespec="minutes")),
        ).fetchall()


def cancel_booking(booking_id: int, user_id: int, db_path: str = DB_PATH) -> bool:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND user_id = ? AND status = 'active'",
            (booking_id, user_id),
        )
        return cur.rowcount == 1


def saved_phone(user_id: int, db_path: str = DB_PATH):
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT phone FROM clients WHERE user_id = ?", (user_id,)).fetchone()
    return row[0] if row else None


def due_reminders(now: datetime, db_path: str = DB_PATH) -> list:
    """Записи, по которым пора отправить напоминание: [(kind, row), ...], kind = '24' или '2'."""
    soon24 = (now + timedelta(hours=24)).isoformat(timespec="minutes")
    soon2 = (now + timedelta(hours=2)).isoformat(timespec="minutes")
    now_s = now.isoformat(timespec="minutes")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        r2 = conn.execute(
            "SELECT * FROM bookings WHERE status = 'active' AND reminded_2 = 0 "
            "AND start_at <= ? AND start_at > ?", (soon2, now_s)).fetchall()
        r24 = conn.execute(
            "SELECT * FROM bookings WHERE status = 'active' AND reminded_24 = 0 "
            "AND start_at <= ? AND start_at > ?", (soon24, soon2)).fetchall()
    return [("2", r) for r in r2] + [("24", r) for r in r24]


def mark_reminded(booking_id: int, kind: str, db_path: str = DB_PATH) -> None:
    column = "reminded_2" if kind == "2" else "reminded_24"
    with sqlite3.connect(db_path) as conn:
        # после напоминания за 2 часа суточное уже неактуально
        extra = ", reminded_24 = 1" if kind == "2" else ""
        conn.execute(f"UPDATE bookings SET {column} = 1{extra} WHERE id = ?", (booking_id,))


def normalize_phone(text: str):
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return None
    return "+" + digits


# ---------- Клавиатуры и тексты ----------

def kb(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def main_menu() -> InlineKeyboardMarkup:
    return kb([[("✂️ Записаться", "book")],
               [("💈 Услуги и цены", "prices"), ("📅 Мои записи", "my")]])


def services_kb() -> InlineKeyboardMarkup:
    rows = [[(f"{s['name']} · {fmt_price(s['price'])}", f"svc:{s['id']}")] for s in SERVICES.values()]
    return kb(rows + [[("← В меню", "menu")]])


def masters_kb() -> InlineKeyboardMarkup:
    rows = [[("Любой свободный мастер", "mst:any")]]
    rows += [[(f"{m['name']}: {m['about']}", f"mst:{m['id']}")] for m in MASTERS.values()]
    return kb(rows + [[("← Назад", "book")]])


def days_kb(days: list) -> InlineKeyboardMarkup:
    today = now_local().date()
    labels = [("Сегодня" if d == today else "Завтра" if d == today + timedelta(days=1) else fmt_day(d),
               f"day:{d.isoformat()}") for d in days]
    rows = [labels[i:i + 3] for i in range(0, len(labels), 3)]
    return kb(rows + [[("← Назад", "back:mst")]])


def times_kb(slots: list) -> InlineKeyboardMarkup:
    buttons = [(s, f"tm:{s}") for s in slots]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    return kb(rows + [[("← Другой день", "back:day")]])


def booking_text(service_id: str, master_id: str, start: datetime) -> str:
    s, m = SERVICES[service_id], MASTERS[master_id]
    return (f"💈 {s['name']}, {s['minutes']} мин\n"
            f"👤 Мастер: {m['name']}\n"
            f"📅 {fmt_day(start.date())}, {start:%H:%M}\n"
            f"💳 {fmt_price(s['price'])}, оплата на месте\n"
            f"📍 {ADDRESS}")


def prices_text() -> str:
    lines = [f"• {s['name']}: {s['minutes']} мин, {fmt_price(s['price'])}" for s in SERVICES.values()]
    return "Услуги и цены:\n\n" + "\n".join(lines)


# ---------- Шаг 1. Старт ----------

@dp.message(CommandStart())
@dp.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        f"Привет! Это {SHOP}.\nЗапишу к мастеру за полминуты: выберите услугу, время, и готово.",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(f"{SHOP}. Что делаем?", reply_markup=main_menu())
    await call.answer()


@dp.callback_query(F.data == "prices")
async def cb_prices(call: CallbackQuery) -> None:
    await call.message.edit_text(prices_text(), reply_markup=kb([[("✂️ Записаться", "book")],
                                                                 [("← В меню", "menu")]]))
    await call.answer()


# ---------- Шаг 2. Услуга ----------

@dp.callback_query(F.data == "book")
async def cb_book(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Шаг 1 из 5. Какая услуга?", reply_markup=services_kb())
    await call.answer()


# ---------- Шаг 3. Мастер ----------

@dp.callback_query(F.data.startswith("svc:"))
async def cb_service(call: CallbackQuery, state: FSMContext) -> None:
    service_id = call.data.split(":", 1)[1]
    if service_id not in SERVICES:
        await call.answer("Услуга не найдена", show_alert=True)
        return
    await state.update_data(service_id=service_id)
    await call.message.edit_text("Шаг 2 из 5. К кому записать?", reply_markup=masters_kb())
    await call.answer()


@dp.callback_query(F.data == "back:mst")
async def cb_back_master(call: CallbackQuery) -> None:
    await call.message.edit_text("Шаг 2 из 5. К кому записать?", reply_markup=masters_kb())
    await call.answer()


# ---------- Шаг 4. День ----------

async def show_days(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if "service_id" not in data:
        await call.message.edit_text("Начнём сначала.", reply_markup=main_menu())
        return
    minutes = SERVICES[data["service_id"]]["minutes"]
    days = days_with_slots(data["master_id"], minutes, now_local())
    if not days:
        await call.message.edit_text(
            "На ближайшую неделю свободного времени нет. Попробуйте другого мастера.",
            reply_markup=masters_kb())
        return
    await call.message.edit_text("Шаг 3 из 5. Какой день?", reply_markup=days_kb(days))


@dp.callback_query(F.data.startswith("mst:"))
async def cb_master(call: CallbackQuery, state: FSMContext) -> None:
    master_id = call.data.split(":", 1)[1]
    if master_id != "any" and master_id not in MASTERS:
        await call.answer("Мастер не найден", show_alert=True)
        return
    await state.update_data(master_id=master_id)
    await show_days(call, state)
    await call.answer()


@dp.callback_query(F.data == "back:day")
async def cb_back_day(call: CallbackQuery, state: FSMContext) -> None:
    await show_days(call, state)
    await call.answer()


# ---------- Шаг 5. Время ----------

@dp.callback_query(F.data.startswith("day:"))
async def cb_day(call: CallbackQuery, state: FSMContext) -> None:
    day = date.fromisoformat(call.data.split(":", 1)[1])
    data = await state.get_data()
    if "service_id" not in data:
        await call.message.edit_text("Начнём сначала.", reply_markup=main_menu())
        await call.answer()
        return
    slots = free_slots(data["master_id"], day, SERVICES[data["service_id"]]["minutes"], now_local())
    if not slots:
        await call.answer("На этот день всё занято", show_alert=True)
        await show_days(call, state)
        return
    await state.update_data(day=day.isoformat(), slots=slots)
    await call.message.edit_text(f"Шаг 4 из 5. {fmt_day(day)}, во сколько?",
                                 reply_markup=times_kb(list(slots)))
    await call.answer()


# ---------- Шаг 6. Телефон (только для новых клиентов) ----------

@dp.callback_query(F.data.startswith("tm:"))
async def cb_time(call: CallbackQuery, state: FSMContext) -> None:
    slot = call.data.split(":", 1)[1]
    data = await state.get_data()
    if slot not in data.get("slots", {}):
        await call.answer("Это время уже недоступно", show_alert=True)
        return
    await state.update_data(time=slot, master_id_final=data["slots"][slot])
    phone = saved_phone(call.from_user.id)
    if phone:
        await state.update_data(phone=phone)
        await show_confirm(call.message, state, edit=True)
    else:
        await call.message.edit_text("Шаг 5 из 5. Оставьте телефон, чтобы мастер мог связаться.")
        await call.message.answer(
            "Нажмите кнопку ниже или напишите номер.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
                resize_keyboard=True, one_time_keyboard=True),
        )
        await state.set_state(Booking.waiting_phone)
    await call.answer()


@dp.message(Booking.waiting_phone, F.contact)
async def got_contact(message: Message, state: FSMContext) -> None:
    await state.update_data(phone=normalize_phone(message.contact.phone_number) or message.contact.phone_number)
    await state.set_state(None)
    await message.answer("Спасибо!", reply_markup=ReplyKeyboardRemove())
    await show_confirm(message, state)


@dp.message(Booking.waiting_phone, F.text)
async def got_phone_text(message: Message, state: FSMContext) -> None:
    phone = normalize_phone(message.text)
    if not phone:
        await message.answer("Не похоже на номер. Пример: +7 900 123-45-67")
        return
    await state.update_data(phone=phone)
    await state.set_state(None)
    await message.answer("Спасибо!", reply_markup=ReplyKeyboardRemove())
    await show_confirm(message, state)


# ---------- Шаг 7. Подтверждение ----------

async def show_confirm(message: Message, state: FSMContext, edit: bool = False) -> None:
    data = await state.get_data()
    start = datetime.combine(date.fromisoformat(data["day"]), time.fromisoformat(data["time"]))
    text = "Проверьте запись:\n\n" + booking_text(data["service_id"], data["master_id_final"], start)
    markup = kb([[("✅ Подтвердить", "confirm")],
                 [("Другое время", "back:day"), ("Отмена", "menu")]])
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@dp.callback_query(F.data == "confirm")
async def cb_confirm(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    if "phone" not in data or "time" not in data:
        await call.message.edit_text("Сессия устарела, начнём сначала.", reply_markup=main_menu())
        await call.answer()
        return
    start = datetime.combine(date.fromisoformat(data["day"]), time.fromisoformat(data["time"]))
    booking_id = create_booking(call.from_user.id, call.from_user.full_name, data["phone"],
                                data["service_id"], data["master_id_final"], start, now_local())
    if booking_id is None:
        await call.answer("Это время только что заняли", show_alert=True)
        await show_days(call, state)
        return
    await state.clear()
    details = booking_text(data["service_id"], data["master_id_final"], start)
    lead = start - now_local()
    remind = ("Напомню за сутки и за 2 часа до визита." if lead > timedelta(hours=24)
              else "Напомню за 2 часа до визита." if lead > timedelta(hours=2) else "Ждём вас!")
    await call.message.edit_text(
        f"Готово, вы записаны! 🎉\n\n{details}\n\n{remind}",
        reply_markup=kb([[("📅 Мои записи", "my"), ("← В меню", "menu")]]))
    await call.answer()
    if ADMIN_CHAT_ID:
        await bot.send_message(ADMIN_CHAT_ID, f"🆕 Новая запись №{booking_id}\n{details}\n"
                                              f"Клиент: {call.from_user.full_name}, {data['phone']}")


# ---------- Мои записи и отмена ----------

@dp.callback_query(F.data == "my")
async def cb_my(call: CallbackQuery) -> None:
    rows = user_bookings(call.from_user.id, now_local())
    if not rows:
        await call.message.edit_text("Активных записей нет.", reply_markup=main_menu())
        await call.answer()
        return
    text = "Ваши записи:\n\n" + "\n\n".join(
        booking_text(r["service_id"], r["master_id"], datetime.fromisoformat(r["start_at"])) for r in rows)
    buttons = [[(f"Отменить {fmt_day(datetime.fromisoformat(r['start_at']).date())}, "
                 f"{r['start_at'][11:]}", f"cancel:{r['id']}")] for r in rows]
    await call.message.edit_text(text, reply_markup=kb(buttons + [[("← В меню", "menu")]]))
    await call.answer()


@dp.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(call: CallbackQuery, bot: Bot) -> None:
    booking_id = int(call.data.split(":", 1)[1])
    row = get_booking(booking_id)
    if not cancel_booking(booking_id, call.from_user.id):
        await call.answer("Запись уже отменена или не найдена", show_alert=True)
        return
    await call.message.edit_text("Запись отменена. Будем рады видеть в другой раз.",
                                 reply_markup=kb([[("✂️ Записаться снова", "book")]]))
    await call.answer()
    if ADMIN_CHAT_ID:
        start = datetime.fromisoformat(row["start_at"])
        await bot.send_message(ADMIN_CHAT_ID, f"❌ Отмена записи №{booking_id}\n"
                                              f"{booking_text(row['service_id'], row['master_id'], start)}")


@dp.callback_query(F.data.startswith("come:"))
async def cb_come(call: CallbackQuery) -> None:
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Отлично, ждём вас!", show_alert=True)


# ---------- Напоминания ----------

async def reminder_loop(bot: Bot) -> None:
    """Раз в минуту проверяет базу и отправляет напоминания. Отметка о отправке хранится в базе,
    поэтому после перезапуска бот не пришлёт напоминание повторно."""
    while True:
        for kind, row in due_reminders(now_local()):
            start = datetime.fromisoformat(row["start_at"])
            when = "Через 2 часа" if kind == "2" else "Завтра" if start.date() > now_local().date() else "Сегодня"
            try:
                await bot.send_message(
                    row["user_id"],
                    f"⏰ Напоминание. {when} у вас запись в {SHOP}:\n\n"
                    f"{booking_text(row['service_id'], row['master_id'], start)}",
                    reply_markup=kb([[("👍 Приду", f"come:{row['id']}"),
                                      ("Отменить", f"cancel:{row['id']}")]]))
            except Exception:
                logging.exception("Не удалось отправить напоминание по записи %s", row["id"])
            mark_reminded(row["id"], kind)
        await asyncio.sleep(60)


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Нет BOT_TOKEN: скопируйте .env.example в .env и вставьте токен от @BotFather")
    init_db()
    bot = Bot(token)
    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
