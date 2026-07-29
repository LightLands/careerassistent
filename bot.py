import asyncio
import logging
import aiohttp
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from bs4 import BeautifulSoup
import re
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАСТРОЙКИ ---
BOT_TOKEN = ""
PROXY_URL = None

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")]
)
logger = logging.getLogger("career_bot")

# --- ИНИЦИАЛИЗАЦИЯ БОТА ---
if PROXY_URL:
    bot = Bot(token=BOT_TOKEN, session=AiohttpSession(proxy=PROXY_URL))
else:
    bot = Bot(token=BOT_TOKEN)

dp = Dispatcher()

# --- КЛАВИАТУРА МЕНЮ (ИСПРАВЛЕНО ДЛЯ AIogram 3.x) ---
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Меню")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True
    )

def get_menu_text():
    return (
        "📌 <b>Доступные команды:</b>\n\n"
        "🔍 <b>/search</b> — Умный поиск вакансий с фильтрами\n"
        "📂 <b>/my_vacancies</b> — Мои сохранённые вакансии\n"
        "📊 <b>/stats</b> — Статистика и аналитика откликов\n"
        "🔔 <b>/subscribe</b> — Подписаться на уведомления\n"
        "📮 <b>/my_subscriptions</b> — Мои подписки\n"
        "✉️ <b>/cover_letter</b> — Написать сопроводительное письмо\n"
        "❓ <b>/help</b> — Помощь (это сообщение)\n\n"
        "💡 <i>Нажми на команду, чтобы использовать её!</i>"
    )

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect("career_bot.db")
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS saved_vacancies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            vacancy_id TEXT NOT NULL,
            title TEXT NOT NULL,
            company TEXT,
            salary TEXT,
            location TEXT,
            url TEXT NOT NULL,
            description TEXT,
            saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'saved',
            UNIQUE(user_id, vacancy_id)
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS temp_vacancies (
            vacancy_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT,
            salary TEXT,
            location TEXT,
            url TEXT NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS job_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            tech TEXT NOT NULL,
            salary INTEGER,
            location TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, tech, salary, location)
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_vacancies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            vacancy_id TEXT NOT NULL,
            subscription_id INTEGER NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, vacancy_id)
        )
    """)
    
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")

# --- ФУНКЦИИ БД ---
def save_temp_vacancy(vacancy_data: dict):
    conn = sqlite3.connect("career_bot.db")
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT OR REPLACE INTO temp_vacancies 
            (vacancy_id, title, company, salary, location, url, description)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            vacancy_data["id"], vacancy_data["title"], vacancy_data["company"],
            vacancy_data["salary"], vacancy_data["location"], vacancy_data["url"],
            vacancy_data["description"]
        ))
        conn.commit()
    finally:
        conn.close()

def get_temp_vacancy(vacancy_id: str) -> dict:
    conn = sqlite3.connect("career_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT vacancy_id, title, company, salary, location, url, description
        FROM temp_vacancies WHERE vacancy_id = ?
    """, (vacancy_id,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return {
            "id": row[0], "title": row[1], "company": row[2],
            "salary": row[3], "location": row[4], "url": row[5], "description": row[6]
        }
    return None

def save_vacancy_to_db(user_id: int, vacancy_data: dict) -> bool:
    conn = sqlite3.connect("career_bot.db")
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO saved_vacancies (user_id, vacancy_id, title, company, salary, location, url, description)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, vacancy_data["id"], vacancy_data["title"], vacancy_data["company"], 
              vacancy_data["salary"], vacancy_data["location"], vacancy_data["url"], vacancy_data["description"]))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def get_user_vacancies(user_id: int) -> list:
    conn = sqlite3.connect("career_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, title, company, salary, location, url, saved_at, status
        FROM saved_vacancies WHERE user_id = ? ORDER BY saved_at DESC
    """, (user_id,))
    vacancies = cursor.fetchall()
    conn.close()
    return vacancies

def delete_vacancy_from_db(user_id: int, vacancy_db_id: int) -> bool:
    conn = sqlite3.connect("career_bot.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM saved_vacancies WHERE id = ? AND user_id = ?", (vacancy_db_id, user_id))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted

def update_vacancy_status(user_id: int, vac_id: int, new_status: str):
    conn = sqlite3.connect("career_bot.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE saved_vacancies SET status = ? WHERE id = ? AND user_id = ?", (new_status, vac_id, user_id))
    conn.commit()
    conn.close()

def get_user_stats(user_id: int) -> dict:
    conn = sqlite3.connect("career_bot.db")
    cursor = conn.cursor()
    stats = {}
    statuses = ['saved', 'applied', 'interview', 'offer', 'rejected']
    for status in statuses:
        cursor.execute("SELECT COUNT(*) FROM saved_vacancies WHERE user_id = ? AND status = ?", (user_id, status))
        stats[status] = cursor.fetchone()[0]
    stats['total'] = sum(stats.values())
    conn.close()
    return stats

def add_subscription(user_id: int, tech: str, salary: int, location: str) -> bool:
    conn = sqlite3.connect("career_bot.db")
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO job_subscriptions (user_id, tech, salary, location) VALUES (?, ?, ?, ?)", (user_id, tech, salary, location))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def get_active_subscriptions() -> list:
    conn = sqlite3.connect("career_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, tech, salary, location FROM job_subscriptions WHERE is_active = 1")
    subscriptions = cursor.fetchall()
    conn.close()
    return subscriptions

def mark_vacancy_sent(user_id: int, vacancy_id: str, subscription_id: int):
    conn = sqlite3.connect("career_bot.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO sent_vacancies (user_id, vacancy_id, subscription_id) VALUES (?, ?, ?)", (user_id, vacancy_id, subscription_id))
    conn.commit()
    conn.close()

def get_sent_vacancies(user_id: int) -> list:
    conn = sqlite3.connect("career_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT vacancy_id FROM sent_vacancies WHERE user_id = ?", (user_id,))
    sent = [row[0] for row in cursor.fetchall()]
    conn.close()
    return sent

def delete_subscription(subscription_id: int, user_id: int) -> bool:
    conn = sqlite3.connect("career_bot.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM job_subscriptions WHERE id = ? AND user_id = ?", (subscription_id, user_id))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def clean_html_and_extract_requirements(html_text: str, max_length: int = 700) -> str:
    if not html_text: return "Требования не указаны"
    soup = BeautifulSoup(html_text, "html.parser")
    for script in soup(["script", "style", "noscript"]): script.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > max_length: text = text[:max_length].rsplit(' ', 1)[0] + "..."
    return text if text else "Требования не указаны"

def make_progress_bar(percent: int, length: int = 10) -> str:
    filled = int(length * percent / 100)
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {percent}%"

# --- СОСТОЯНИЯ (FSM) ---
class SearchStates(StatesGroup):
    waiting_tech = State()
    waiting_salary = State()
    waiting_location = State()

class CoverLetterStates(StatesGroup):
    waiting_for_vacancy = State()
    waiting_for_profile = State()

class SubscriptionStates(StatesGroup):
    waiting_tech = State()
    waiting_salary = State()
    waiting_location = State()

# --- ХЕНДЛЕРЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "без_username"
    logger.info(f"Пользователь {user_id} (@{username}) запустил бота")

    text = (
        "👋 Привет! Я твой Карьерный Ассистент.\n\n"
        "Я помогу тебе найти работу мечты и управлять откликами.\n\n"
        "📌 <b>Мои возможности:</b>\n"
        "/search — Умный поиск вакансий\n"
        "/my_vacancies — Мои сохранённые вакансии\n"
        "/stats — Статистика и аналитика\n"
        "/subscribe — Подписка на уведомления\n"
        "/cover_letter — Сопроводительное письмо\n\n"
        "💡 <b>Нажми кнопку '📋 Меню' внизу, чтобы увидеть все команды!</b>"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard())

@dp.message(F.text == "📋 Меню")
async def show_menu(message: types.Message):
    logger.info(f"Пользователь {message.from_user.id} открыл меню")
    await message.answer(get_menu_text(), parse_mode="HTML")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(get_menu_text(), parse_mode="HTML", reply_markup=get_main_keyboard())

# --- УМНЫЙ ПОИСК ---
@dp.message(Command("search"))
async def cmd_search(message: types.Message, state: FSMContext):
    logger.info(f"Пользователь {message.from_user.id} начал умный поиск")
    await message.answer(
        "🔍 <b>Шаг 1 из 3: Технология</b>\n\n"
        "Какую технологию ищешь? (например: <b>python</b>, <b>react</b>)\n\n"
        "<i>Или нажми '📋 Меню', чтобы вернуться</i>",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    await state.set_state(SearchStates.waiting_tech)

@dp.message(StateFilter(SearchStates.waiting_tech), F.text)
async def get_tech(message: types.Message, state: FSMContext):
    if message.text == "📋 Меню":
        await show_menu(message)
        await state.clear()
        return
    tech = message.text.strip().lower()
    if not tech:
        await message.answer("⚠️ Введи хотя бы одно слово", reply_markup=get_main_keyboard())
        return
    await state.update_data(tech=tech)
    await message.answer(
        "💰 <b>Шаг 2 из 3: Минимальная зарплата</b>\n\n"
        "Сколько хочешь зарабатывать в год (в долларах)?\n"
        "Например: <b>80000</b>\n\n"
        "💡 Если не важно — напиши '<b>пропустить</b>'",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    await state.set_state(SearchStates.waiting_salary)

@dp.message(StateFilter(SearchStates.waiting_salary), F.text)
async def get_salary(message: types.Message, state: FSMContext):
    if message.text == "📋 Меню":
        await show_menu(message)
        await state.clear()
        return
    salary_text = message.text.strip().lower()
    if salary_text in ["пропустить", "skip", "не важно", "-"]:
        salary = None
    else:
        try:
            salary_digits = re.sub(r'[^\d]', '', salary_text)
            if not salary_digits: raise ValueError
            salary = int(salary_digits)
        except ValueError:
            await message.answer("⚠️ Не понял число. Введи число или 'пропустить'", reply_markup=get_main_keyboard())
            return
    await state.update_data(salary=salary)
    await message.answer(
        "🌍 <b>Шаг 3 из 3: Регион</b>\n\n"
        "Где хочешь работать?\n"
        "• <b>worldwide</b>\n"
        "• <b>europe</b>\n"
        "• <b>usa</b>\n\n"
        "💡 Если не важно — напиши '<b>пропустить</b>'",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    await state.set_state(SearchStates.waiting_location)

@dp.message(StateFilter(SearchStates.waiting_location), F.text)
async def get_location_and_search(message: types.Message, state: FSMContext):
    if message.text == "📋 Меню":
        await show_menu(message)
        await state.clear()
        return
    user_id = message.from_user.id
    location_text = message.text.strip().lower()
    location = None if location_text in ["пропустить", "skip", "не важно", "-", "worldwide"] else location_text
    
    await state.update_data(location=location)
    data = await state.get_data()
    tech, salary, location = data.get("tech"), data.get("salary"), data.get("location")
    
    search_summary = f"🔎 <b>Ищу по параметрам:</b>\n• Технология: <b>{tech}</b>\n"
    search_summary += f"• Зарплата: <b>от ${salary:,}</b>\n" if salary else "• Зарплата: <b>любая</b>\n"
    search_summary += f"• Регион: <b>{location}</b>\n" if location else "• Регион: <b>любой</b>\n"
    await message.answer(search_summary + "\n⏳ Ищу вакансии...", parse_mode="HTML", reply_markup=get_main_keyboard())
    
    url = "https://remoteok.com/api"
    params = {"tag": tech}
    if salary: params["compensation"] = str(salary)
    if location: params["location"] = location
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "application/json"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    vacancies = data[1:6] if isinstance(data, list) and len(data) > 1 else []

                    if not vacancies:
                        await message.answer("😕 Ничего не найдено. Попробуй другие фильтры.", reply_markup=get_main_keyboard())
                    else:
                        await message.answer(f"🌍 Найдено <b>{len(vacancies)}</b> вакансий!\n", parse_mode="HTML", reply_markup=get_main_keyboard())
                        
                        for i, vac in enumerate(vacancies, 1):
                            title = vac.get("position", "Без названия")
                            company = vac.get("company", "Неизвестная компания")
                            sal_min, sal_max = vac.get("salary_min"), vac.get("salary_max")
                            if sal_min and sal_max: sal_str = f"${sal_min:,} — ${sal_max:,} / год"
                            elif sal_min: sal_str = f"от ${sal_min:,} / год"
                            elif sal_max: sal_str = f"до ${sal_max:,} / год"
                            else: sal_str = "Не указана"

                            location_vac = vac.get("location", "Worldwide")
                            tags = vac.get("tags", [])
                            tags_str = ", ".join(tags[:5]) if tags else "—"
                            vac_url = vac.get("url", "https://remoteok.com")
                            date = vac.get("date", "")[:10] if vac.get("date") else "—"
                            requirements = clean_html_and_extract_requirements(vac.get("description", ""), max_length=700)

                            vacancy_id = vac.get("id", f"vac_{i}")
                            
                            vacancy_data = {
                                "id": vacancy_id, "title": title, "company": company,
                                "salary": sal_str, "location": location_vac, "url": vac_url, "description": requirements
                            }
                            save_temp_vacancy(vacancy_data)

                            vac_text = (
                                f"<b>{i}. {title}</b>\n🏢 <b>Компания:</b> {company}\n💰 <b>Зарплата:</b> {sal_str}\n"
                                f"📍 <b>Локация:</b> {location_vac}\n🏷 <b>Теги:</b> {tags_str}\n📅 <b>Опубликовано:</b> {date}\n\n"
                                f"📋 <b>Требования:</b>\n<i>{requirements}</i>\n\n🔗 <a href='{vac_url}'>Открыть вакансию</a>"
                            )
                            if len(vac_text) > 4000: vac_text = vac_text[:3950] + "\n\n... (обрезано)"

                            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💾 Сохранить в трекер", callback_data=f"save_{vacancy_id}")]])
                            await message.answer(vac_text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=keyboard)
                elif response.status == 429:
                    await message.answer("⚠️ Слишком много запросов. Подожди минуту.", reply_markup=get_main_keyboard())
                else:
                    await message.answer(f"❌ Ошибка API. Код: {response.status}", reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Ошибка при поиске: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка. Попробуй позже.", reply_markup=get_main_keyboard())
    await state.clear()

@dp.callback_query(F.data.startswith("save_"))
async def save_vacancy_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    vacancy_id = callback.data.replace("save_", "")
    logger.info(f"Пользователь {user_id} пытается сохранить вакансию {vacancy_id}")
    
    vacancy_data = get_temp_vacancy(vacancy_id)
    if not vacancy_data:
        await callback.answer("❌ Вакансия не найдена в базе данных", show_alert=True)
        return
    
    success = save_vacancy_to_db(user_id, vacancy_data)
    if success:
        await callback.answer("✅ Вакансия сохранена!", show_alert=True)
    else:
        await callback.answer("⚠️ Эта вакансия уже сохранена", show_alert=True)

@dp.message(Command("my_vacancies"))
async def cmd_my_vacancies(message: types.Message):
    user_id = message.from_user.id
    vacancies = get_user_vacancies(user_id)
    if not vacancies:
        await message.answer("📭 У тебя пока нет сохранённых вакансий.\n\nИспользуй /search и нажимай [💾 Сохранить].", parse_mode="HTML", reply_markup=get_main_keyboard())
        return
    
    await message.answer(f"📋 <b>Твои сохранённые вакансии ({len(vacancies)}):</b>\n", parse_mode="HTML", reply_markup=get_main_keyboard())
    
    status_emojis = {'saved': '📥 Сохранена', 'applied': '📤 Отклик отправлен', 'interview': '✅ Собеседование', 'offer': '🏆 Оффер!', 'rejected': '❌ Отказ'}

    for i, vac in enumerate(vacancies, 1):
        vac_db_id, title, company, salary, location, url, saved_at, status = vac
        current_status = status_emojis.get(status, '📥 Сохранена')
        
        vac_text = f"<b>{i}. {title}</b>\n🏢 {company}\n💰 {salary}\n📍 {location}\n📅 Сохранено: {saved_at[:10]}\n📊 <b>Статус:</b> {current_status}\n🔗 <a href='{url}'>Открыть</a>"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Отклик", callback_data=f"status_{vac_db_id}_applied"), InlineKeyboardButton(text="✅ Интервью", callback_data=f"status_{vac_db_id}_interview")],
            [InlineKeyboardButton(text="🏆 Оффер", callback_data=f"status_{vac_db_id}_offer"), InlineKeyboardButton(text="❌ Отказ", callback_data=f"status_{vac_db_id}_rejected")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_{vac_db_id}")]
        ])
        await message.answer(vac_text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=keyboard)

@dp.callback_query(F.data.startswith("status_"))
async def change_status_callback(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    vac_db_id = int(parts[1])
    new_status = parts[2]
    user_id = callback.from_user.id
    update_vacancy_status(user_id, vac_db_id, new_status)
    
    status_names = {'applied': '📤 Отклик отправлен', 'interview': '✅ Собеседование назначено', 'offer': '🏆 Поздравляю с оффером!', 'rejected': '❌ Статус изменен на Отказ'}
    await callback.answer(f"Статус изменен: {status_names.get(new_status, new_status)}", show_alert=False)

@dp.callback_query(F.data.startswith("delete_"))
async def delete_vacancy_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    vac_db_id = int(callback.data.replace("delete_", ""))
    if delete_vacancy_from_db(user_id, vac_db_id):
        await callback.answer("✅ Вакансия удалена", show_alert=True)
        await callback.message.delete()
    else:
        await callback.answer("❌ Не удалось удалить", show_alert=True)

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    user_id = message.from_user.id
    stats = get_user_stats(user_id)
    if stats['total'] == 0:
        await message.answer("📊 У тебя пока нет статистики. Начни с команды /search!", parse_mode="HTML", reply_markup=get_main_keyboard())
        return

    conversion = round((stats['interview'] / stats['applied']) * 100) if stats['applied'] > 0 else 0
    progress_bar = make_progress_bar(conversion)

    text = (
        f"📊 <b>Твоя карьерная статистика</b>\n\n"
        f"📥 Всего сохранено: <b>{stats['total']}</b>\n"
        f"📤 Откликов отправлено: <b>{stats['applied']}</b>\n"
        f"✅ Приглашений на интервью: <b>{stats['interview']}</b>\n"
        f"🏆 Получено офферов: <b>{stats['offer']}</b>\n"
        f"❌ Отказов: <b>{stats['rejected']}</b>\n\n"
        f"📈 <b>Конверсия в интервью:</b>\n{progress_bar}\n\n"
        f"💡 <i>Совет: Чтобы статистика росла, не забывай менять статус вакансий в разделе /my_vacancies</i>"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard())

@dp.message(Command("subscribe"))
async def cmd_subscribe(message: types.Message, state: FSMContext):
    await message.answer("🔔 <b>Подписка на уведомления</b>\n\n🔍 <b>Шаг 1 из 3: Технология</b>\nКакую технологию ищешь? (например: <b>python</b>)", parse_mode="HTML", reply_markup=get_main_keyboard())
    await state.set_state(SubscriptionStates.waiting_tech)

@dp.message(StateFilter(SubscriptionStates.waiting_tech), F.text)
async def get_sub_tech(message: types.Message, state: FSMContext):
    if message.text == "📋 Меню": await show_menu(message); await state.clear(); return
    tech = message.text.strip().lower()
    if not tech:
        await message.answer("⚠️ Введи хотя бы одно слово", reply_markup=get_main_keyboard()); return
    await state.update_data(sub_tech=tech)
    await message.answer("💰 <b>Шаг 2 из 3: Минимальная зарплата</b>\n\nСколько хочешь зарабатывать в год (в долларах)?\nНапример: <b>80000</b>\n\n💡 Если не важно — напиши '<b>пропустить</b>'", parse_mode="HTML", reply_markup=get_main_keyboard())
    await state.set_state(SubscriptionStates.waiting_salary)

@dp.message(StateFilter(SubscriptionStates.waiting_salary), F.text)
async def get_sub_salary(message: types.Message, state: FSMContext):
    if message.text == "📋 Меню": await show_menu(message); await state.clear(); return
    salary_text = message.text.strip().lower()
    if salary_text in ["пропустить", "skip", "не важно", "-"]:
        salary = None
    else:
        try:
            salary_digits = re.sub(r'[^\d]', '', salary_text)
            if not salary_digits: raise ValueError
            salary = int(salary_digits)
        except ValueError:
            await message.answer("⚠️ Не понял число. Введи число или 'пропустить'", reply_markup=get_main_keyboard()); return
    await state.update_data(sub_salary=salary)
    await message.answer("🌍 <b>Шаг 3 из 3: Регион</b>\n\nГде хочешь работать?\n• <b>worldwide</b>\n• <b>europe</b>\n• <b>usa</b>\n\n💡 Если не важно — напиши '<b>пропустить</b>'", parse_mode="HTML", reply_markup=get_main_keyboard())
    await state.set_state(SubscriptionStates.waiting_location)

@dp.message(StateFilter(SubscriptionStates.waiting_location), F.text)
async def get_sub_location(message: types.Message, state: FSMContext):
    if message.text == "📋 Меню": await show_menu(message); await state.clear(); return
    user_id = message.from_user.id
    location_text = message.text.strip().lower()
    location = None if location_text in ["пропустить", "skip", "не важно", "-", "worldwide"] else location_text
    
    await state.update_data(sub_location=location)
    data = await state.get_data()
    tech, salary, location = data.get("sub_tech"), data.get("sub_salary"), data.get("sub_location")
    
    if add_subscription(user_id, tech, salary, location):
        summary = f"✅ <b>Подписка оформлена!</b>\n\n🔍 Технология: <b>{tech}</b>\n"
        summary += f"💰 Зарплата: <b>от ${salary:,}</b>\n" if salary else "💰 Зарплата: <b>любая</b>\n"
        summary += f"🌍 Регион: <b>{location}</b>\n" if location else "🌍 Регион: <b>любой</b>\n\n"
        summary += "📬 Я буду присылать новые вакансии каждый день в <b>10:00</b>."
        await message.answer(summary, parse_mode="HTML", reply_markup=get_main_keyboard())
    else:
        await message.answer("⚠️ Такая подписка уже существует.", reply_markup=get_main_keyboard())
    await state.clear()

@dp.message(Command("my_subscriptions"))
async def cmd_my_subscriptions(message: types.Message):
    user_id = message.from_user.id
    subscriptions = get_active_subscriptions()
    user_subs = [s for s in subscriptions if s[1] == user_id]
    if not user_subs:
        await message.answer("📭 У тебя нет активных подписок.\n\nИспользуй /subscribe.", parse_mode="HTML", reply_markup=get_main_keyboard()); return
    
    await message.answer(f"🔔 <b>Твои подписки ({len(user_subs)}):</b>\n", parse_mode="HTML", reply_markup=get_main_keyboard())
    for sub in user_subs:
        sub_id, _, tech, salary, location = sub
        sub_text = f"🔍 <b>{tech}</b>\n"
        sub_text += f"💰 От ${salary:,}\n" if salary else "💰 Любая зарплата\n"
        sub_text += f"🌍 {location}\n" if location else "🌍 Любой регион\n"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗑 Отписаться", callback_data=f"unsub_{sub_id}")]])
        await message.answer(sub_text, parse_mode="HTML", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("unsub_"))
async def unsubscribe_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    sub_id = int(callback.data.replace("unsub_", ""))
    if delete_subscription(sub_id, user_id):
        await callback.answer("✅ Ты отписался от уведомлений", show_alert=True)
        await callback.message.delete()
    else:
        await callback.answer("❌ Не удалось отписаться", show_alert=True)

@dp.message(Command("cover_letter"))
async def start_cover_letter(message: types.Message, state: FSMContext):
    await message.answer("📝 Скинь мне <b>описание вакансии</b> (скопируй текст с сайта).", parse_mode="HTML", reply_markup=get_main_keyboard())
    await state.set_state(CoverLetterStates.waiting_for_vacancy)

@dp.message(StateFilter(CoverLetterStates.waiting_for_vacancy), F.text)
async def get_vacancy_text(message: types.Message, state: FSMContext):
    if message.text == "📋 Меню": await show_menu(message); await state.clear(); return
    await state.update_data(vacancy_text=message.text)
    await message.answer("Принял! Теперь напиши в двух словах <b>свой опыт</b> (технологии, годы работы).", parse_mode="HTML", reply_markup=get_main_keyboard())
    await state.set_state(CoverLetterStates.waiting_for_profile)

@dp.message(StateFilter(CoverLetterStates.waiting_for_profile), F.text)
async def generate_letter(message: types.Message, state: FSMContext):
    if message.text == "📋 Меню": await show_menu(message); await state.clear(); return
    user_profile = message.text
    data = await state.get_data()
    vacancy_text = data.get("vacancy_text")
    await message.answer("⏳ Генерирую письмо...", reply_markup=get_main_keyboard())

    vac_context = vacancy_text[:150].replace('\n', ' ') if vacancy_text else "your open position"
    generated_letter = (
        f"Dear Hiring Manager,\n\n"
        f"I am writing to express my strong interest in the position at your company. "
        f"With my background in {user_profile}, I am confident that I can make a meaningful contribution to your team.\n\n"
        f"Having reviewed your requirements regarding {vac_context}..., "
        f"I believe my skills and experience align perfectly with the needs of your team.\n\n"
        f"I would welcome the opportunity to discuss how my background can benefit your company. "
        f"Thank you for your time and consideration.\n\n"
        f"Best regards,\n[Твоё Имя]"
    )
    await message.answer(f"🎉 <b>Готово! Вот твоё письмо:</b>\n\n{generated_letter}", parse_mode="HTML", reply_markup=get_main_keyboard())
    await state.clear()

# --- ФОНОВАЯ ЗАДАЧА ---
async def check_new_vacancies():
    logger.info("Начата проверка новых вакансий для подписок...")
    subscriptions = get_active_subscriptions()
    if not subscriptions:
        logger.info("Нет активных подписок, проверка пропущена"); return
    
    for sub in subscriptions:
        sub_id, user_id, tech, salary, location = sub
        url = "https://remoteok.com/api"
        params = {"tag": tech}
        if salary: params["compensation"] = str(salary)
        if location: params["location"] = location
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "application/json"}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers) as response:
                    if response.status == 200:
                        data = await response.json()
                        vacancies = data[1:11] if isinstance(data, list) and len(data) > 1 else []
                        sent_vacancies = get_sent_vacancies(user_id)
                        new_vacancies = [v for v in vacancies if v.get("id") not in sent_vacancies]
                        
                        if new_vacancies:
                            await bot.send_message(user_id, f"🔔 <b>Новые вакансии по твоей подписке!</b>\n\n🔍 Технология: <b>{tech}</b>\nНайдено <b>{len(new_vacancies)}</b> новых вакансий:", parse_mode="HTML")
                            for i, vac in enumerate(new_vacancies[:3], 1):
                                title = vac.get("position", "Без названия")
                                company = vac.get("company", "Неизвестная компания")
                                sal_min, sal_max = vac.get("salary_min"), vac.get("salary_max")
                                sal_str = f"${sal_min:,} — ${sal_max:,} / год" if sal_min and sal_max else (f"от ${sal_min:,} / год" if sal_min else "Не указана")
                                vac_url = vac.get("url", "https://remoteok.com")
                                vacancy_id = vac.get("id", f"vac_{i}")
                                
                                vac_text = f"<b>{i}. {title}</b>\n🏢 {company}\n💰 {sal_str}\n🔗 <a href='{vac_url}'>Открыть</a>"
                                keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💾 Сохранить", callback_data=f"save_{vacancy_id}")]])
                                await bot.send_message(user_id, vac_text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=keyboard)
                                mark_vacancy_sent(user_id, vacancy_id, sub_id)
        except Exception as e:
            logger.error(f"Ошибка при проверке подписки {sub_id}: {e}", exc_info=True)
    logger.info("Проверка новых вакансий завершена")

# --- ЗАПУСК ---
async def main():
    init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_new_vacancies, 'cron', hour=10, minute=0)
    scheduler.start()
    
    logger.info("=" * 50)
    logger.info("БОТ ЗАПУСКАЕТСЯ (с исправленной клавиатурой)")
    logger.info("=" * 50)
    
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.critical(f"КРИТИЧЕСКАЯ ОШИБКА: {e}", exc_info=True)
    finally:
        scheduler.shutdown()
        logger.info("Бот остановлен")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
