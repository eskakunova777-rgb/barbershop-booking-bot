"""Проверка логики без Telegram: слоты, защита от двойной записи, напоминания, телефон."""
import os
import tempfile
from datetime import datetime, timedelta

import bot

db = os.path.join(tempfile.mkdtemp(), "test.db")
bot.init_db(db)
now = datetime(2026, 10, 26, 9, 0)  # понедельник, 9:00
monday = now.date()

# Артём работает в понедельник 10-20: стрижка 60 мин, первый слот 10:00, последний 19:00
slots = bot.master_slots("artem", monday, 60, now, db)
assert slots[0] == "10:00" and slots[-1] == "19:00", slots
# Тимур в понедельник не работает, Глеб тоже
assert bot.master_slots("timur", monday, 60, now, db) == []
# раньше чем за час записаться нельзя
assert "09:30" not in bot.master_slots("artem", monday, 30, now, db)

# запись занимает время, пересекающаяся запись невозможна
b1 = bot.create_booking(1, "Иван", "+79001234567", "combo", "artem", datetime(2026, 10, 26, 12, 0), now, db)
assert b1
for t in ("12:00", "12:30", "13:00", "11:30"):
    assert t not in bot.master_slots("artem", monday, 60, now, db), t
assert "13:30" in bot.master_slots("artem", monday, 60, now, db)
assert bot.create_booking(2, "Пётр", "+79000000000", "cut", "artem", datetime(2026, 10, 26, 13, 0), now, db) is None

# «любой мастер» во вторник: Артём с 10, Глеб с 10, время не дублируется
any_slots = bot.free_slots("any", monday + timedelta(days=1), 60, now, db)
assert list(any_slots) == sorted(set(any_slots)) and any_slots["10:00"] in ("artem", "gleb")

# на неделе есть свободные дни
assert len(bot.days_with_slots("any", 60, now, db)) == 7

# телефон запомнен, повторный клиент шаг с телефоном не проходит
assert bot.saved_phone(1, db) == "+79001234567"

# напоминания: запись на завтра 15:00
b2 = bot.create_booking(3, "Олег", "+79005556677", "cut", "artem", datetime(2026, 10, 27, 15, 0), now, db)
assert bot.due_reminders(now, db) == [] or all(r["id"] != b2 for _, r in bot.due_reminders(now, db))
later = datetime(2026, 10, 26, 15, 0)  # ровно сутки до визита
kinds = [(k, r["id"]) for k, r in bot.due_reminders(later, db)]
assert ("24", b2) in kinds, kinds
bot.mark_reminded(b2, "24", db)
assert ("24", b2) not in [(k, r["id"]) for k, r in bot.due_reminders(later, db)]
two_h = datetime(2026, 10, 27, 13, 0)
assert ("2", b2) in [(k, r["id"]) for k, r in bot.due_reminders(two_h, db)]
bot.mark_reminded(b2, "2", db)
assert all(r["id"] != b2 for _, r in bot.due_reminders(two_h, db))

# запись на сегодня (меньше суток): суточное напоминание не уходит, только за 2 часа
row = bot.get_booking(b1, db)
assert row["reminded_24"] == 1 and row["reminded_2"] == 0

# отмена освобождает время, чужую запись отменить нельзя
assert not bot.cancel_booking(b1, 999, db)
assert bot.cancel_booking(b1, 1, db)
assert "12:00" in bot.master_slots("artem", monday, 60, now, db)

assert bot.normalize_phone("8 (900) 123-45-67") == "+79001234567"
assert bot.normalize_phone("9001234567") == "+79001234567"
assert bot.normalize_phone("123") is None
print("Все проверки пройдены")
