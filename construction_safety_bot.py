"""Telegram-бот «Техника безопасности на стройплощадке».

Функциональность:
- Интерактивный справочник по ТБ (6 ключевых разделов).
- Тест для проверки знаний по ТБ с мгновенной обратной связью и сохранением результатов.
- Предсменный чек-лист безопасности с отметками.
- Форма сообщения о нарушении / опасной ситуации (с фото) и пересылкой инженеру по ТБ.
- Личная статистика сотрудника и админ-сводка (/admin) на базе SQLite.

Требования:
    pip install aiogram>=3.10.0 python-dotenv

Переменные окружения (.env):
    BOT_TOKEN=ваш_токен_от_BotFather
    ADMIN_CHAT_ID=123456789   # необязательно: Telegram ID инженера по ТБ / админа
    DB_PATH=safety_bot.db     # необязательно: путь к файлу базы SQLite
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Конфигурация и логирование
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("safety_bot")

PASSING_SCORE_RATIO = 0.75  # 75% правильных ответов для сдачи теста


def load_config() -> tuple[str, int | None, str]:
    """Читает конфигурацию из переменных окружения."""
    token = os.getenv("BOT_TOKEN", "").strip()
    admin_raw = os.getenv("ADMIN_CHAT_ID", "").strip()
    db_path = os.getenv("DB_PATH", "safety_bot.db").strip() or "safety_bot.db"

    admin_chat_id: int | None = None
    if admin_raw:
        try:
            admin_chat_id = int(admin_raw)
        except ValueError:
            logger.warning("ADMIN_CHAT_ID должен быть числом, получено: %r", admin_raw)

    return token, admin_chat_id, db_path


# ---------------------------------------------------------------------------
# База знаний по технике безопасности
# ---------------------------------------------------------------------------

SAFETY_TOPICS: dict[str, dict[str, str]] = {
    "ppe": {
        "title": "🦺 Средства индивидуальной защиты (СИЗ)",
        "content": (
            "<b>🦺 Средства индивидуальной защиты (СИЗ)</b>\n\n"
            "Находиться на территории стройплощадки без обязательных СИЗ запрещено:\n"
            "• <b>Защитная каска</b> — надета и застёгнута на подбородочный ремень; "
            "проверяйте корпус на трещины и срок годности.\n"
            "• <b>Спецобувь</b> — с защитным подноском и антипрокольной подошвой.\n"
            "• <b>Сигнальный жилет</b> — обязателен в зонах движения техники и при слабой видимости.\n"
            "• <b>Защитные очки / щиток</b> — при резке, шлифовке, сверлении, работе с бетоном и химией.\n"
            "• <b>Перчатки</b> — подбираются по виду работ (механические, диэлектрические, термостойкие).\n"
            "• <b>Защита слуха и дыхания</b> — беруши/наушники при шуме, респиратор при пыли и покраске.\n\n"
            "⚠️ <i>Повреждённые СИЗ необходимо немедленно заменить у мастера или прораба.</i>"
        ),
    },
    "height": {
        "title": "🧗 Работы на высоте",
        "content": (
            "<b>🧗 Работы на высоте</b>\n\n"
            "К работам на высоте относятся работы с риском падения с высоты <b>1,8 м и более</b>, "
            "а также вблизи неограждённых перепадов высот, проёмов и котлованов:\n"
            "• Обязателен допуск к работам на высоте и оформление наряда-допуска (при необходимости).\n"
            "• Используйте исправную <b>страховочную привязь</b> с амортизатором; "
            "цепляйте строп за сертифицированную анкерную точку выше уровня пояса.\n"
            "• Проверяйте строительные леса, подмости и лестницы перед началом смены.\n"
            "• Оградите опасную зону внизу и установите предупреждающие знаки — "
            "инструмент на высоте фиксируйте страховочными шнурами.\n"
            "• <b>Запрещено</b> работать на высоте при сильном ветре, грозе, гололёде или плохой видимости."
        ),
    },
    "electro": {
        "title": "⚡ Электробезопасность",
        "content": (
            "<b>⚡ Электробезопасность на объекте</b>\n\n"
            "• Перед включением осмотрите корпус, кабель, вилку и изоляцию электроинструмента.\n"
            "• Подключайте переносной инструмент только через щитки с исправным <b>УЗО</b>.\n"
            "• Прокладывайте удлинители выше уровня земли или в защитных коробах, "
            "избегайте прокладки через лужи и проезды техники.\n"
            "• Во влажных помещениях и металлических ёмкостях применяйте инструмент "
            "пониженного напряжения или аккумуляторный инструмент.\n"
            "• Запрещено самостоятельно ремонтировать щиты и кабели без соответствующей группы допуска.\n"
            "• При ремонте оборудования соблюдайте правило <b>LOTO</b>: отключи → заблокируй → повесь бирку → проверь отсутствие напряжения."
        ),
    },
    "fire": {
        "title": "🔥 Пожарная безопасность и огневые работы",
        "content": (
            "<b>🔥 Пожарная безопасность и огневые работы</b>\n\n"
            "• Сварка, газорезка и работа УШМ (болгаркой) с искрами проводятся "
            "при очистке зоны от горючих материалов и наличии наряда-допуска.\n"
            "• В зоне огневых работ держите рядом исправный <b>огнетушитель</b> "
            "(порошковый ОП или углекислотный ОУ) и кошму.\n"
            "• Курение разрешено строго в специально оборудованных местах с урнами и водой.\n"
            "• ГСМ, лаки, краски и газовые баллоны храните в отдельных проветриваемых зонах "
            "вдали от источников тепла; баллоны фиксируйте от падения.\n"
            "• Не загромождайте эвакуационные выходы, проезды для спецтехники и доступ к пожарным щитам."
        ),
    },
    "machinery": {
        "title": "🏗 Техника, краны и земляные работы",
        "content": (
            "<b>🏗 Техника, грузоподъёмные работы и котлованы</b>\n\n"
            "• <b>Никогда не находитесь под поднятым грузом</b> и в зоне поворота стрелы крана или экскаватора.\n"
            "• Устанавливайте зрительный контакт с машинистом при приближении к технике; "
            "передвигайтесь по обозначенным пешеходным маршрутам.\n"
            "• Строповку грузов выполняет только обученный стропальщик с проверенными стропами.\n"
            "• Перед началом земляных работ убедитесь в отсутствии подземных кабелей и трубопроводов.\n"
            "• В траншеях и котлованах глубже нормы проверяйте крепления откосов, "
            "наличие спускных лестниц и отсутствие складирования грунта у самой бровки."
        ),
    },
    "first_aid": {
        "title": "🚑 Первая помощь и экстренные номера",
        "content": (
            "<b>🚑 Первая помощь и алгоритм при ЧП</b>\n\n"
            "<b>Экстренные службы:</b>\n"
            "• <b>112</b> — Единая служба спасения\n"
            "• <b>101</b> — Пожарная охрана\n"
            "• <b>103</b> — Скорая медицинская помощь\n\n"
            "<b>Порядок действий:</b>\n"
            "1. Оцените обстановку и убедитесь, что вам ничего не угрожает (отключите питание, остановите технику).\n"
            "2. Вызовите скорую помощь (103 / 112) и немедленно сообщите прорабу и инженеру по ТБ.\n"
            "3. <b>Сильное кровотечение:</b> прямое давление на рану стерильной повязкой; жгут — только при угрожающем жизни артериальном кровотечении с записью времени наложения.\n"
            "4. <b>Поражение током:</b> не касайтесь пострадавшего голыми руками до обесточивания источника.\n"
            "5. <b>Падение с высоты / подозрение на травму позвоночника:</b> не перемещайте пострадавшего без крайней угрозы жизни, обеспечьте покой до приезда врачей."
        ),
    },
}


# ---------------------------------------------------------------------------
# Вопросы теста по ТБ
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QuizQuestion:
    question: str
    options: tuple[str, ...]
    correct_index: int
    explanation: str


QUIZ_QUESTIONS: tuple[QuizQuestion, ...] = (
    QuizQuestion(
        question="1/8. Когда на территории стройплощадки обязательно носить защитную каску?",
        options=(
            "Только при работе крана поблизости",
            "Постоянно на всей территории стройплощадки с застёгнутым ремнём",
            "Только на открытых этажах без перекрытий",
            "По усмотрению бригадира",
        ),
        correct_index=1,
        explanation="Каска с застёгнутым подбородочным ремнём обязательна на всей территории стройплощадки.",
    ),
    QuizQuestion(
        question="2/8. С какой высоты работы обычно классифицируются как работы на высоте с повышенным риском падения?",
        options=(
            "От 0,5 метра",
            "От 1,8 метра (а также у неограждённых перепадов высот)",
            "Только выше 5 метров",
            "Только при работе на крыше",
        ),
        correct_index=1,
        explanation="Работы с риском падения с 1,8 м и более (и вблизи неограждённых проёмов) требуют мер защиты от падения.",
    ),
    QuizQuestion(
        question="3/8. Вы заметили оголённый участок на кабеле перфоратора. Ваши действия?",
        options=(
            "Обмотать изолентой и закончить смену",
            "Продолжить работу в сухих перчатках",
            "Отключить инструмент, прекратить работу и сдать его в ремонт/на замену",
            "Передать инструмент напарнику",
        ),
        correct_index=2,
        explanation="Работать с повреждённым кабелем запрещено: инструмент нужно немедленно обесточить и вывести из эксплуатации.",
    ),
    QuizQuestion(
        question="4/8. Как правильно действовать при приближении к работающему экскаватору?",
        options=(
            "Быстро пробежать за противовесом, пока ковш отвернут",
            "Установить зрительный контакт с машинистом и дождаться остановки/сигнала",
            "Постучать по корпусу машины, чтобы привлечь внимание",
            "Пройти под поднятым ковшом",
        ),
        correct_index=1,
        explanation="У спецтехники большие слепые зоны. Подходите только после зрительного контакта и остановки машины.",
    ),
    QuizQuestion(
        question="5/8. Что нужно сделать в первую очередь при обнаружении пострадавшего под действием электрического тока?",
        options=(
            "Сразу оттащить его за одежду или руки",
            "Полить водой, чтобы охладить ожог",
            "Обесточить источник питания (рубильник/автомат), не прикасаясь к пострадавшему",
            "Подождать, пока ток отключится сам",
        ),
        correct_index=2,
        explanation="Сначала необходимо безопасно обесточить цепь — иначе спасатель сам попадёт под напряжение.",
    ),
    QuizQuestion(
        question="6/8. Какой огнетушитель подходит для тушения электроустановок под напряжением до 1000 В?",
        options=(
            "Водный",
            "Воздушно-пенный",
            "Углекислотный (ОУ) или порошковый (ОП) с соответствующей маркировкой",
            "Любой, который оказался рядом",
        ),
        correct_index=2,
        explanation="Водные и пенные огнетушители проводят ток. Для электрооборудования используют ОУ или ОП.",
    ),
    QuizQuestion(
        question="7/8. Что категорически запрещено делать во время подъёма груза башенным краном?",
        options=(
            "Использовать оттяжки для направления груза",
            "Находиться под поднятым грузом и в опасной зоне его перемещения",
            "Подавать установленные сигналы крановщику",
            "Проверять стропы перед подъёмом",
        ),
        correct_index=1,
        explanation="Нахождение под грузом или на пути его перемещения смертельно опасно и строго запрещено.",
    ),
    QuizQuestion(
        question="8/8. Какие действия правильны при падении коллеги с высоты и подозрении на травму позвоночника?",
        options=(
            "Сразу усадить его и дать воды",
            "Отнести в бытовку, чтобы он отлежался",
            "Вызвать 103/112, не перемещать без прямой угрозы жизни и контролировать дыхание",
            "Попросить его встать и проверить, двигаются ли ноги",
        ),
        correct_index=2,
        explanation="При подозрении на травму позвоночника перемещение может усугубить повреждение. Вызовите скорую и обеспечьте покой.",
    ),
)


# ---------------------------------------------------------------------------
# Пункты предсменного чек-листа
# ---------------------------------------------------------------------------

CHECKLIST_ITEMS: tuple[tuple[str, str], ...] = (
    ("ppe", "СИЗ надеты и исправны (каска, обувь, жилет, очки/перчатки)"),
    ("tools", "Инструмент и кабели осмотрены, повреждений нет"),
    ("zone", "Рабочая зона очищена, проходы свободны, ограждения на месте"),
    ("permits", "Инструктаж пройден, наряд-допуск оформлен (если требуется)"),
    ("emergency", "Известно расположение аптечки, огнетушителя и выхода"),
    ("health", "Самочувствие в норме, признаков усталости/опьянения нет"),
)

INCIDENT_CATEGORIES: tuple[str, ...] = (
    "🦺 Отсутствие / повреждение СИЗ",
    "🧗 Опасность при работе на высоте",
    "⚡ Электроопасность",
    "🔥 Пожарная опасность",
    "🏗 Неисправная техника / инструмент",
    "⚠️ Другое нарушение / ЧП",
)


# ---------------------------------------------------------------------------
# Слой хранения данных (SQLite)
# ---------------------------------------------------------------------------

class SafetyDatabase:
    """Простая обёртка над SQLite для хранения пользователей, тестов, чек-листов и инцидентов."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    username TEXT,
                    registered_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS quiz_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    score INTEGER NOT NULL,
                    total INTEGER NOT NULL,
                    passed INTEGER NOT NULL,
                    completed_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                );

                CREATE TABLE IF NOT EXISTS checklists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    shift_date TEXT NOT NULL,
                    checked_items_json TEXT NOT NULL,
                    all_passed INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                );

                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    location TEXT NOT NULL,
                    description TEXT NOT NULL,
                    photo_file_id TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                );
                """
            )

    def upsert_user(self, user_id: int, full_name: str, username: str | None) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, full_name, username, registered_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    full_name = excluded.full_name,
                    username = excluded.username
                """,
                (user_id, full_name, username, now),
            )

    def save_quiz_result(self, user_id: int, score: int, total: int, passed: bool) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO quiz_results (user_id, score, total, passed, completed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, score, total, int(passed), now),
            )

    def save_checklist(self, user_id: int, checked_keys: list[str], all_passed: bool) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        today = date.today().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO checklists (user_id, shift_date, checked_items_json, all_passed, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, today, json.dumps(checked_keys, ensure_ascii=False), int(all_passed), now),
            )

    def save_incident(
        self,
        user_id: int,
        category: str,
        location: str,
        description: str,
        photo_file_id: str | None = None,
    ) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO incidents (user_id, category, location, description, photo_file_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, category, location, description, photo_file_id, now),
            )
            return int(cur.lastrowid)

    def get_user_stats(self, user_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            quiz_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS attempts,
                    COALESCE(MAX(score), 0) AS best_score,
                    COALESCE(MAX(total), 0) AS max_total,
                    COALESCE(SUM(passed), 0) AS passed_count
                FROM quiz_results
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()

            checklist_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM checklists WHERE user_id = ?",
                (user_id,),
            ).fetchone()["cnt"]

            incident_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM incidents WHERE user_id = ?",
                (user_id,),
            ).fetchone()["cnt"]

        return {
            "attempts": quiz_row["attempts"],
            "best_score": quiz_row["best_score"],
            "max_total": quiz_row["max_total"] or len(QUIZ_QUESTIONS),
            "passed_count": quiz_row["passed_count"],
            "checklist_count": checklist_count,
            "incident_count": incident_count,
        }

    def get_admin_summary(self) -> dict[str, Any]:
        today = date.today().isoformat()
        with self._connect() as conn:
            users_cnt = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"]
            quiz_row = conn.execute(
                """
                SELECT COUNT(*) AS total, COALESCE(SUM(passed), 0) AS passed
                FROM quiz_results
                """
            ).fetchone()
            today_checklists = conn.execute(
                "SELECT COUNT(*) AS cnt FROM checklists WHERE shift_date = ?",
                (today,),
            ).fetchone()["cnt"]
            incidents_cnt = conn.execute("SELECT COUNT(*) AS cnt FROM incidents").fetchone()["cnt"]
            recent_incidents = conn.execute(
                """
                SELECT i.id, i.category, i.location, i.created_at, u.full_name
                FROM incidents i
                LEFT JOIN users u ON u.user_id = i.user_id
                ORDER BY i.id DESC
                LIMIT 5
                """
            ).fetchall()

        return {
            "users_cnt": users_cnt,
            "quiz_total": quiz_row["total"],
            "quiz_passed": quiz_row["passed"],
            "today_checklists": today_checklists,
            "incidents_cnt": incidents_cnt,
            "recent_incidents": [dict(row) for row in recent_incidents],
        }


# ---------------------------------------------------------------------------
# Состояния FSM
# ---------------------------------------------------------------------------

class QuizState(StatesGroup):
    in_progress = State()


class IncidentState(StatesGroup):
    waiting_category = State()
    waiting_location = State()
    waiting_description = State()
    waiting_photo = State()


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

BTN_HANDBOOK = "📘 Справочник ТБ"
BTN_QUIZ = "📝 Тест по ТБ"
BTN_CHECKLIST = "✅ Чек-лист смены"
BTN_REPORT = "🚨 Сообщить о нарушении"
BTN_SOS = "🚑 Экстренные номера"
BTN_STATS = "📊 Моя статистика"


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_HANDBOOK), KeyboardButton(text=BTN_QUIZ)],
            [KeyboardButton(text=BTN_CHECKLIST), KeyboardButton(text=BTN_REPORT)],
            [KeyboardButton(text=BTN_SOS), KeyboardButton(text=BTN_STATS)],
        ],
        resize_keyboard=True,
    )


def topics_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, topic in SAFETY_TOPICS.items():
        builder.button(text=topic["title"], callback_data=f"topic:{key}")
    builder.adjust(1)
    return builder.as_markup()


def back_to_topics_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ К списку разделов", callback_data="topic:list")
    return builder.as_markup()


def quiz_options_kb(q_index: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    question = QUIZ_QUESTIONS[q_index]
    for opt_idx, opt_text in enumerate(question.options):
        builder.button(text=opt_text, callback_data=f"quiz:{q_index}:{opt_idx}")
    builder.button(text="✖️ Прервать тест", callback_data="quiz:cancel")
    builder.adjust(1)
    return builder.as_markup()


def checklist_kb(checked: set[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, label in CHECKLIST_ITEMS:
        mark = "✅" if key in checked else "⬜️"
        builder.button(text=f"{mark} {label}", callback_data=f"chk:toggle:{key}")
    builder.button(text="💾 Завершить и сохранить", callback_data="chk:submit")
    builder.adjust(1)
    return builder.as_markup()


def incident_category_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for idx, cat in enumerate(INCIDENT_CATEGORIES):
        builder.button(text=cat, callback_data=f"inc_cat:{idx}")
    builder.button(text="✖️ Отмена", callback_data="inc:cancel")
    builder.adjust(1)
    return builder.as_markup()


def skip_photo_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➡️ Пропустить фото", callback_data="inc:skip_photo")
    builder.button(text="✖️ Отмена", callback_data="inc:cancel")
    builder.adjust(1)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Вспомогательные функции бизнес-логики (удобно тестировать отдельно)
# ---------------------------------------------------------------------------

def evaluate_quiz(score: int, total: int) -> tuple[bool, int]:
    """Возвращает (сдан_ли_тест, процент_правильных_ответов)."""
    if total <= 0:
        return False, 0
    percent = round((score / total) * 100)
    return (score / total) >= PASSING_SCORE_RATIO, percent


def format_incident_alert(
    incident_id: int,
    reporter_name: str,
    reporter_username: str | None,
    category: str,
    location: str,
    description: str,
) -> str:
    """Форматирует HTML-сообщение об инциденте для отправки инженеру по ТБ."""
    user_ref = html.escape(reporter_name)
    if reporter_username:
        user_ref += f" (@{html.escape(reporter_username)})"
    return (
        f"🚨 <b>Новое сообщение о нарушении #{incident_id}</b>\n\n"
        f"<b>Категория:</b> {html.escape(category)}\n"
        f"<b>Участок / локация:</b> {html.escape(location)}\n"
        f"<b>Описание:</b> {html.escape(description)}\n"
        f"<b>Отправитель:</b> {user_ref}"
    )


# ---------------------------------------------------------------------------
# Создание роутера и обработчиков
# ---------------------------------------------------------------------------

def build_router(db: SafetyDatabase, admin_chat_id: int | None) -> Router:
    router = Router()

    # Хранение активных чек-листов в памяти по user_id до сохранения
    active_checklists: dict[int, set[str]] = {}

    def register_user(message_or_cb: Message | CallbackQuery) -> None:
        user = message_or_cb.from_user
        if user:
            db.upsert_user(user.id, user.full_name, user.username)

    # --- Старт и помощь ---

    @router.message(CommandStart())
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        register_user(message)
        name = html.escape(message.from_user.first_name if message.from_user else "коллега")
        await message.answer(
            f"Привет, <b>{name}</b>! 👷‍♂️\n\n"
            "Я бот по <b>технике безопасности на стройплощадке</b>.\n"
            "Помогу быстро освежить правила, пройти предсменный чек-лист, "
            "проверить знания в тесте и сообщить о нарушении инженеру по ТБ.\n\n"
            "Выберите нужный раздел в меню ниже:",
            reply_markup=main_menu_kb(),
        )

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(
            "<b>Доступные команды:</b>\n"
            "/start — главное меню\n"
            "/help — справка\n"
            "/cancel — отменить текущее действие (тест или отправку отчёта)\n"
            "/admin — сводка по объекту (для инженера по ТБ)",
            reply_markup=main_menu_kb(),
        )

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Текущее действие отменено.", reply_markup=main_menu_kb())

    # --- Справочник ТБ ---

    @router.message(F.text == BTN_HANDBOOK)
    async def show_handbook(message: Message, state: FSMContext) -> None:
        await state.clear()
        register_user(message)
        await message.answer(
            "<b>📘 Справочник по технике безопасности</b>\nВыберите интересующий раздел:",
            reply_markup=topics_kb(),
        )

    @router.callback_query(F.data == "topic:list")
    async def cb_topics_list(callback: CallbackQuery) -> None:
        await callback.message.edit_text(
            "<b>📘 Справочник по технике безопасности</b>\nВыберите интересующий раздел:",
            reply_markup=topics_kb(),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("topic:"))
    async def cb_topic_detail(callback: CallbackQuery) -> None:
        key = callback.data.split(":", 1)[1]
        topic = SAFETY_TOPICS.get(key)
        if not topic:
            await callback.answer("Раздел не найден", show_alert=True)
            return
        await callback.message.edit_text(topic["content"], reply_markup=back_to_topics_kb())
        await callback.answer()

    # --- Экстренные номера ---

    @router.message(F.text == BTN_SOS)
    async def show_sos(message: Message) -> None:
        register_user(message)
        await message.answer(SAFETY_TOPICS["first_aid"]["content"])

    # --- Тест по ТБ ---

    @router.message(F.text == BTN_QUIZ)
    async def start_quiz(message: Message, state: FSMContext) -> None:
        register_user(message)
        await state.set_state(QuizState.in_progress)
        await state.update_data(q_index=0, score=0)
        q = QUIZ_QUESTIONS[0]
        await message.answer(
            "<b>📝 Тест по технике безопасности</b>\n"
            f"Всего вопросов: {len(QUIZ_QUESTIONS)}. Проходной балл: {int(PASSING_SCORE_RATIO * 100)}%.\n\n"
            f"<b>{html.escape(q.question)}</b>",
            reply_markup=quiz_options_kb(0),
        )

    @router.callback_query(F.data == "quiz:cancel")
    async def cb_quiz_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.message.edit_text("Тест прерван. Вы можете вернуться к нему в любое время.")
        await callback.answer()

    @router.callback_query(QuizState.in_progress, F.data.startswith("quiz:"))
    async def cb_quiz_answer(callback: CallbackQuery, state: FSMContext) -> None:
        parts = callback.data.split(":")
        if len(parts) != 3:
            await callback.answer()
            return

        q_index = int(parts[1])
        chosen = int(parts[2])
        data = await state.get_data()

        if q_index != data.get("q_index"):
            await callback.answer("Этот вопрос уже пройден.")
            return

        q = QUIZ_QUESTIONS[q_index]
        score = int(data.get("score", 0))
        if chosen == q.correct_index:
            score += 1
            feedback = f"✅ <b>Верно!</b> {html.escape(q.explanation)}"
        else:
            correct_text = html.escape(q.options[q.correct_index])
            feedback = (
                f"❌ <b>Неверно.</b> Правильный ответ: <i>{correct_text}</i>\n"
                f"💡 {html.escape(q.explanation)}"
            )

        next_index = q_index + 1
        if next_index < len(QUIZ_QUESTIONS):
            await state.update_data(q_index=next_index, score=score)
            next_q = QUIZ_QUESTIONS[next_index]
            await callback.message.edit_text(
                f"{feedback}\n\n<b>{html.escape(next_q.question)}</b>",
                reply_markup=quiz_options_kb(next_index),
            )
        else:
            total = len(QUIZ_QUESTIONS)
            passed, percent = evaluate_quiz(score, total)
            if callback.from_user:
                db.save_quiz_result(callback.from_user.id, score, total, passed)
            await state.clear()

            status_line = (
                "🎉 <b>Тест сдан!</b> Отличное знание правил ТБ."
                if passed
                else "⚠️ <b>Тест не сдан.</b> Рекомендуем повторить разделы в «Справочнике ТБ» и попробовать снова."
            )
            await callback.message.edit_text(
                f"{feedback}\n\n"
                f"<b>Результат теста:</b> {score} из {total} ({percent}%)\n"
                f"{status_line}"
            )
        await callback.answer()

    # --- Предсменный чек-лист ---

    @router.message(F.text == BTN_CHECKLIST)
    async def start_checklist(message: Message, state: FSMContext) -> None:
        await state.clear()
        register_user(message)
        user_id = message.from_user.id if message.from_user else 0
        active_checklists[user_id] = set()
        await message.answer(
            "<b>✅ Предсменный чек-лист безопасности</b>\n"
            "Отметьте проверенные пункты перед началом работ и нажмите «Завершить и сохранить»:",
            reply_markup=checklist_kb(active_checklists[user_id]),
        )

    @router.callback_query(F.data.startswith("chk:toggle:"))
    async def cb_checklist_toggle(callback: CallbackQuery) -> None:
        user_id = callback.from_user.id if callback.from_user else 0
        key = callback.data.split(":", 2)[2]
        checked = active_checklists.setdefault(user_id, set())
        if key in checked:
            checked.remove(key)
        else:
            checked.add(key)
        await callback.message.edit_reply_markup(reply_markup=checklist_kb(checked))
        await callback.answer()

    @router.callback_query(F.data == "chk:submit")
    async def cb_checklist_submit(callback: CallbackQuery) -> None:
        user_id = callback.from_user.id if callback.from_user else 0
        checked = active_checklists.pop(user_id, set())
        total = len(CHECKLIST_ITEMS)
        all_passed = len(checked) == total
        db.save_checklist(user_id, sorted(checked), all_passed)

        if all_passed:
            text = (
                f"✅ <b>Предсменный чек-лист сохранён ({len(checked)}/{total}).</b>\n"
                "Все пункты проверены. Безопасной смены! 👷‍♂️"
            )
        else:
            missing = [label for key, label in CHECKLIST_ITEMS if key not in checked]
            missing_lines = "\n".join(f"• {html.escape(item)}" for item in missing)
            text = (
                f"⚠️ <b>Чек-лист сохранён с замечаниями ({len(checked)}/{total}).</b>\n\n"
                f"Не отмечены пункты:\n{missing_lines}\n\n"
                "Устраните замечания до начала работ или обратитесь к прорабу."
            )
        await callback.message.edit_text(text)
        await callback.answer("Чек-лист сохранён")

    # --- Сообщить о нарушении / ЧП ---

    @router.message(F.text == BTN_REPORT)
    async def start_incident_report(message: Message, state: FSMContext) -> None:
        register_user(message)
        await state.set_state(IncidentState.waiting_category)
        await message.answer(
            "<b>🚨 Сообщение о нарушении или опасной ситуации</b>\n"
            "Выберите категорию:",
            reply_markup=incident_category_kb(),
        )

    @router.callback_query(F.data == "inc:cancel")
    async def cb_incident_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.message.edit_text("Отправка сообщения о нарушении отменена.")
        await callback.answer()

    @router.callback_query(IncidentState.waiting_category, F.data.startswith("inc_cat:"))
    async def cb_incident_category(callback: CallbackQuery, state: FSMContext) -> None:
        idx = int(callback.data.split(":", 1)[1])
        if not (0 <= idx < len(INCIDENT_CATEGORIES)):
            await callback.answer("Неверная категория", show_alert=True)
            return
        category = INCIDENT_CATEGORIES[idx]
        await state.update_data(category=category)
        await state.set_state(IncidentState.waiting_location)
        await callback.message.edit_text(
            f"Категория: <b>{html.escape(category)}</b>\n\n"
            "Укажите <b>участок / корпус / этаж</b>, где обнаружено нарушение:"
        )
        await callback.answer()

    @router.message(IncidentState.waiting_location, F.text)
    async def msg_incident_location(message: Message, state: FSMContext) -> None:
        await state.update_data(location=message.text.strip())
        await state.set_state(IncidentState.waiting_description)
        await message.answer("Кратко опишите суть нарушения или опасной ситуации:")

    @router.message(IncidentState.waiting_description, F.text)
    async def msg_incident_description(message: Message, state: FSMContext) -> None:
        await state.update_data(description=message.text.strip())
        await state.set_state(IncidentState.waiting_photo)
        await message.answer(
            "Прикрепите <b>фотографию</b> нарушения (или нажмите «Пропустить фото»):",
            reply_markup=skip_photo_kb(),
        )

    async def finalize_incident(
        bot: Bot,
        user_id: int,
        full_name: str,
        username: str | None,
        state: FSMContext,
        photo_file_id: str | None,
    ) -> int:
        data = await state.get_data()
        category = data["category"]
        location = data["location"]
        description = data["description"]
        incident_id = db.save_incident(
            user_id=user_id,
            category=category,
            location=location,
            description=description,
            photo_file_id=photo_file_id,
        )
        await state.clear()

        if admin_chat_id:
            alert_text = format_incident_alert(
                incident_id=incident_id,
                reporter_name=full_name,
                reporter_username=username,
                category=category,
                location=location,
                description=description,
            )
            try:
                if photo_file_id:
                    await bot.send_photo(chat_id=admin_chat_id, photo=photo_file_id, caption=alert_text)
                else:
                    await bot.send_message(chat_id=admin_chat_id, text=alert_text)
            except Exception as exc:
                logger.error("Не удалось отправить уведомление админу (%s): %s", admin_chat_id, exc)

        return incident_id

    @router.message(IncidentState.waiting_photo, F.photo)
    async def msg_incident_photo(message: Message, state: FSMContext, bot: Bot) -> None:
        photo_id = message.photo[-1].file_id
        user = message.from_user
        incident_id = await finalize_incident(
            bot=bot,
            user_id=user.id if user else 0,
            full_name=user.full_name if user else "Сотрудник",
            username=user.username if user else None,
            state=state,
            photo_file_id=photo_id,
        )
        await message.answer(
            f"✅ Сообщение о нарушении <b>#{incident_id}</b> зарегистрировано и передано ответственному. Спасибо за бдительность!",
            reply_markup=main_menu_kb(),
        )

    @router.callback_query(IncidentState.waiting_photo, F.data == "inc:skip_photo")
    async def cb_incident_skip_photo(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        user = callback.from_user
        incident_id = await finalize_incident(
            bot=bot,
            user_id=user.id if user else 0,
            full_name=user.full_name if user else "Сотрудник",
            username=user.username if user else None,
            state=state,
            photo_file_id=None,
        )
        await callback.message.edit_text(
            f"✅ Сообщение о нарушении <b>#{incident_id}</b> зарегистрировано и передано ответственному. Спасибо за бдительность!"
        )
        await callback.answer()

    # --- Статистика пользователя и админ-сводка ---

    @router.message(F.text == BTN_STATS)
    async def show_stats(message: Message) -> None:
        register_user(message)
        user_id = message.from_user.id if message.from_user else 0
        stats = db.get_user_stats(user_id)
        await message.answer(
            "<b>📊 Ваша статистика по ТБ</b>\n\n"
            f"• Попыток теста: <b>{stats['attempts']}</b> (успешно сдано: <b>{stats['passed_count']}</b>)\n"
            f"• Лучший результат: <b>{stats['best_score']} / {stats['max_total']}</b>\n"
            f"• Заполнено предсменных чек-листов: <b>{stats['checklist_count']}</b>\n"
            f"• Отправлено сообщений о нарушениях: <b>{stats['incident_count']}</b>"
        )

    @router.message(Command("admin"))
    async def cmd_admin(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        if admin_chat_id is not None and user_id != admin_chat_id:
            await message.answer("⛔️ Команда доступна только инженеру по ТБ (администратору).")
            return

        summary = db.get_admin_summary()
        pass_rate = (
            round((summary["quiz_passed"] / summary["quiz_total"]) * 100)
            if summary["quiz_total"] > 0
            else 0
        )

        lines = [
            "<b>🛡 Админ-сводка по ТБ на объекте</b>\n",
            f"• Зарегистрировано сотрудников: <b>{summary['users_cnt']}</b>",
            f"• Пройдено тестов: <b>{summary['quiz_total']}</b> (сдано: {pass_rate}%)",
            f"• Чек-листов за сегодня: <b>{summary['today_checklists']}</b>",
            f"• Всего сообщений о нарушениях: <b>{summary['incidents_cnt']}</b>",
        ]
        if summary["recent_incidents"]:
            lines.append("\n<b>Последние нарушения:</b>")
            for inc in summary["recent_incidents"]:
                lines.append(
                    f"#{inc['id']} | {html.escape(inc['category'])} — "
                    f"{html.escape(inc['location'])} ({html.escape(inc['full_name'] or '—')})"
                )

        await message.answer("\n".join(lines))

    return router


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main() -> None:
    token, admin_chat_id, db_path = load_config()
    if not token:
        raise RuntimeError(
            "Не задан BOT_TOKEN. Создайте файл .env и добавьте строку BOT_TOKEN=ваш_токен"
        )

    db = SafetyDatabase(db_path)
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(build_router(db=db, admin_chat_id=admin_chat_id))

    logger.info("Запуск бота «Техника безопасности на стройплощадке»...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())