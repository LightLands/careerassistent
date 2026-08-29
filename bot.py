import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from typing import List, Optional

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import AsyncOpenAI

# ==================== ЗАГРУЗКА КОНФИГА ====================

load_dotenv()

# ВСТАВЬ СВОЙ ТОКЕН СЮДА (получи у @BotFather)
BOT_TOKEN = "твой_токен_от_BotFather"  # ← СЮДА ВСТАВЬ ТОКЕН

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.0-flash-exp:free")
PROXY_URL = os.getenv("PROXY_URL")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
DB_PATH = os.getenv("DB_PATH", "career_bot.db")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан! Вставь токен в переменную BOT_TOKEN.")

# ==================== ЛОГИРОВАНИЕ ====================

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")]
)
logger = logging.getLogger("career_bot")

# ==================== МОДЕЛИ ДАННЫХ ====================

@dataclass
class Vacancy:
    id: str
    title: str
    company: str
    salary: str
    location: str
    url: str
    description: str
    tags: Optional[List[str]] = None
    date: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None

@dataclass
class SearchParams:
    tech: str
    salary: Optional[int] = None
    location: Optional[str] = None

@dataclass
class Subscription:
    id: int
    user_id: int
    tech: str
    salary: Optional[int]
    location: Optional[str]
    is_active: bool = True

@dataclass
class Stats:
    saved: int = 0
    applied: int = 0
    interview: int = 0
    offer: int = 0
    rejected: int = 0
    
    @property
    def total(self) -> int:
        return self.saved + self.applied + self.interview + self.offer + self.rejected
    
    @property
    def conversion_rate(self) -> int:
        return round((self.interview / self.applied) * 100) if self.applied > 0 else 0

# ==================== БАЗА ДАННЫХ ====================

class SQLiteRepository:
    def __init__(self, db_path: str = "career_bot.db"):
        self.db_path = db_path
        self._init_tables()
    
    def _get_connection(self):
        return sqlite3.connect(self.db_path)
    
    def _init_tables(self):
        with self._get_connection() as conn:
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
            logger.info("Database initialized")
    
    def save_temp_vacancy(self, vacancy: Vacancy) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO temp_vacancies 
                (vacancy_id, title, company, salary, location, url, description)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (vacancy.id, vacancy.title, vacancy.company, vacancy.salary,
                  vacancy.location, vacancy.url, vacancy.description))
            conn.commit()
    
    def get_temp_vacancy(self, vacancy_id: str) -> Optional[Vacancy]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT vacancy_id, title, company, salary, location, url, description
                FROM temp_vacancies WHERE vacancy_id = ?
            """, (vacancy_id,))
            row = cursor.fetchone()
            
            if row:
                return Vacancy(
                    id=row[0], title=row[1], company=row[2],
                    salary=row[3], location=row[4], url=row[5], description=row[6]
                )
            return None
    
    def save_vacancy(self, user_id: int, vacancy: Vacancy) -> bool:
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO saved_vacancies 
                    (user_id, vacancy_id, title, company, salary, location, url, description)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (user_id, vacancy.id, vacancy.title, vacancy.company,
                      vacancy.salary, vacancy.location, vacancy.url, vacancy.description))
                conn.commit()
                return True
        except sqlite3.IntegrityError:
            return False
    
    def get_user_vacancies(self, user_id: int) -> List[tuple]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, title, company, salary, location, url, saved_at, status
                FROM saved_vacancies WHERE user_id = ? ORDER BY saved_at DESC
            """, (user_id,))
            return cursor.fetchall()
    
    def delete_vacancy(self, user_id: int, vacancy_db_id: int) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM saved_vacancies WHERE id = ? AND user_id = ?",
                (vacancy_db_id, user_id)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def update_vacancy_status(self, user_id: int, vacancy_db_id: int, new_status: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("SELECT status FROM saved_vacancies WHERE id = ? AND user_id = ?", 
                          (vacancy_db_id, user_id))
            row = cursor.fetchone()
            if not row:
                return False
            
            current_status = row[0]
            status_flow = ['saved', 'applied', 'interview', 'offer', 'rejected']
            
            if new_status not in status_flow or current_status not in status_flow:
                return False
            
            if status_flow.index(new_status) <= status_flow.index(current_status):
                logger.warning(f"Invalid status transition: {current_status} -> {new_status}")
                return False
            
            cursor.execute(
                "UPDATE saved_vacancies SET status = ? WHERE id = ? AND user_id = ?",
                (new_status, vacancy_db_id, user_id)
            )
            conn.commit()
            return True
    
    def get_stats(self, user_id: int) -> Stats:
        stats = Stats()
        statuses = ['saved', 'applied', 'interview', 'offer', 'rejected']
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for status in statuses:
                cursor.execute(
                    "SELECT COUNT(*) FROM saved_vacancies WHERE user_id = ? AND status = ?",
                    (user_id, status)
                )
                count = cursor.fetchone()[0]
                setattr(stats, status, count)
        
        return stats
    
    def add_subscription(self, user_id: int, tech: str, salary: Optional[int], location: Optional[str]) -> bool:
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO job_subscriptions (user_id, tech, salary, location) VALUES (?, ?, ?, ?)",
                    (user_id, tech, salary, location)
                )
                conn.commit()
                return True
        except sqlite3.IntegrityError:
            return False
    
    def get_active_subscriptions(self) -> List[Subscription]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, user_id, tech, salary, location FROM job_subscriptions WHERE is_active = 1"
            )
            rows = cursor.fetchall()
            return [
                Subscription(id=r[0], user_id=r[1], tech=r[2], salary=r[3], location=r[4])
                for r in rows
            ]
    
    def get_user_subscriptions(self, user_id: int) -> List[Subscription]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, user_id, tech, salary, location FROM job_subscriptions WHERE user_id = ? AND is_active = 1",
                (user_id,)
            )
            rows = cursor.fetchall()
            return [
                Subscription(id=r[0], user_id=r[1], tech=r[2], salary=r[3], location=r[4])
                for r in rows
            ]
    
    def delete_subscription(self, subscription_id: int, user_id: int) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM job_subscriptions WHERE id = ? AND user_id = ?",
                (subscription_id, user_id)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def mark_vacancy_sent(self, user_id: int, vacancy_id: str, subscription_id: int) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO sent_vacancies (user_id, vacancy_id, subscription_id) VALUES (?, ?, ?)",
                (user_id, vacancy_id, subscription_id)
            )
            conn.commit()
    
    def get_sent_vacancies(self, user_id: int) -> List[str]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT vacancy_id FROM sent_vacancies WHERE user_id = ?", (user_id,))
            return [row[0] for row in cursor.fetchall()]

# ==================== ИНТЕГРАЦИИ ====================

class RemoteOKClient:
    BASE_URL = "https://remoteok.com/api"
    
    def __init__(self, session: Optional[aiohttp.ClientSession] = None):
        # Если сессия не передана, создаем ее позже в main()
        self._session = session
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json"
        }
    
    @property
    def session(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def search(self, params: SearchParams, limit: int = 20) -> List[Vacancy]:
        query_params = {"tag": params.tech}
        if params.salary:
            query_params["compensation"] = str(params.salary)
        if params.location:
            query_params["location"] = params.location
        
        try:
            async with self.session.get(
                self.BASE_URL, 
                params=query_params, 
                headers=self.headers
            ) as response:
                if response.status != 200:
                    logger.error(f"RemoteOK API error: {response.status}")
                    return []
                
                data = await response.json()
                raw_vacancies = data[1:limit+1] if isinstance(data, list) and len(data) > 1 else []
                
                vacancies = [self._parse_vacancy(vac) for vac in raw_vacancies if vac]
                filtered = self._filter_vacancies(vacancies, params)
                return filtered[:5]
                
        except Exception as e:
            logger.error(f"Error fetching from RemoteOK: {e}")
            return []
    
    def _parse_vacancy(self, data: dict) -> Vacancy:
        sal_min, sal_max = data.get("salary_min"), data.get("salary_max")
        
        if sal_min and sal_max:
            salary_str = f"${sal_min:,} — ${sal_max:,} / год"
        elif sal_min:
            salary_str = f"от ${sal_min:,} / год"
        elif sal_max:
            salary_str = f"до ${sal_max:,} / год"
        else:
            salary_str = "Не указана"
        
        return Vacancy(
            id=data.get("id", ""),
            title=data.get("position", "Без названия"),
            company=data.get("company", "Неизвестная компания"),
            salary=salary_str,
            salary_min=sal_min,
            salary_max=sal_max,
            location=data.get("location", "Worldwide"),
            url=data.get("url", "https://remoteok.com"),
            description=self._clean_description(data.get("description", "")),
            tags=data.get("tags", []),
            date=data.get("date", "")[:10] if data.get("date") else None
        )
    
    def _filter_vacancies(self, vacancies: List[Vacancy], params: SearchParams) -> List[Vacancy]:
        filtered = vacancies
        
        if params.salary:
            filtered = [
                v for v in filtered 
                if (v.salary_max and v.salary_max >= params.salary) or 
                   (v.salary_min and v.salary_min >= params.salary)
            ]
        
        if params.location and params.location.lower() != "worldwide":
            loc_lower = params.location.lower()
            filtered = [
                v for v in filtered
                if v.location and loc_lower in v.location.lower()
            ]
        
        return filtered
    
    @staticmethod
    def _clean_description(html_text: str, max_length: int = 700) -> str:
        if not html_text:
            return "Требования не указаны"
        
        soup = BeautifulSoup(html_text, "html.parser")
        for script in soup(["script", "style", "noscript"]):
            script.decompose()
        
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r'\s+', ' ', text).strip()
        
        if len(text) > max_length:
            text = text[:max_length].rsplit(' ', 1)[0] + "..."
        
        return text if text else "Требования не указаны"
    
    async def close(self):
        if self._session:
            await self._session.close()

class OpenRouterClient:
    def __init__(self):
        self.client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
        ) if OPENROUTER_API_KEY else None
        self.model = OPENROUTER_MODEL
        self.is_available = bool(OPENROUTER_API_KEY)
    
    async def generate_cover_letter(self, vacancy_text: str, user_profile: str) -> str:
        if not self.is_available or not self.client:
            logger.warning("OpenRouter API ключ не настроен, используется шаблон")
            return self._generate_fallback(vacancy_text, user_profile)
        
        try:
            prompt = f"""
            Напиши профессиональное сопроводительное письмо (Cover Letter) на английском языке для вакансии.
            
            Описание вакансии:
            {vacancy_text[:1500]}
            
            Мой опыт и навыки:
            {user_profile}
            
            Требования к письму:
            1. Будь конкретным, свяжи мой опыт с требованиями вакансии
            2. Используй профессиональный, но не шаблонный тон
            3. Длина: 3-4 абзаца
            4. Начни с "Dear Hiring Manager,"
            5. Закончи предложением о созвоне и подписью "Best regards, [Моё Имя]"
            """
            
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=500
            )
            
            return response.choices[0].message.content.strip()
            
        except Exception as e:
            logger.error(f"OpenRouter API error: {e}")
            return self._generate_fallback(vacancy_text, user_profile)
    
    def _generate_fallback(self, vacancy_text: str, user_profile: str) -> str:
        vac_context = vacancy_text[:200].replace('\n', ' ') if vacancy_text else "your open position"
        
        return f"""Dear Hiring Manager,

I am writing to express my strong interest in the position at your company. With my background in {user_profile}, I am confident that I can make a meaningful contribution to your team.

Having reviewed your requirements regarding {vac_context}, I believe my skills and experience align well with your needs. I am particularly drawn to this opportunity because it combines my technical expertise with my passion for solving complex problems.

I would welcome the opportunity to discuss how my background can benefit your company. Thank you for your time and consideration.

Best regards,
[Твоё Имя]"""

# ==================== СЕРВИСЫ ====================

class VacancyService:
    def __init__(self, remoteok_client: RemoteOKClient, repository: SQLiteRepository):
        self.client = remoteok_client
        self.repo = repository
    
    async def search_vacancies(self, params: SearchParams, limit: int = 5) -> List[Vacancy]:
        vacancies = await self.client.search(params, limit)
        for vac in vacancies:
            self.repo.save_temp_vacancy(vac)
        return vacancies
    
    def save_vacancy_for_user(self, user_id: int, vacancy_id: str) -> bool:
        vacancy = self.repo.get_temp_vacancy(vacancy_id)
        if not vacancy:
            return False
        return self.repo.save_vacancy(user_id, vacancy)
    
    def get_user_vacancies(self, user_id: int) -> List[tuple]:
        return self.repo.get_user_vacancies(user_id)
    
    def delete_vacancy(self, user_id: int, vacancy_db_id: int) -> bool:
        return self.repo.delete_vacancy(user_id, vacancy_db_id)
    
    def update_status(self, user_id: int, vacancy_db_id: int, new_status: str) -> bool:
        return self.repo.update_vacancy_status(user_id, vacancy_db_id, new_status)

class AnalyticsService:
    def __init__(self, repository: SQLiteRepository):
        self.repo = repository
    
    def get_user_stats(self, user_id: int) -> Stats:
        return self.repo.get_stats(user_id)

class CoverLetterService:
    def __init__(self):
        self.openrouter = OpenRouterClient()
    
    async def generate(self, vacancy_text: str, user_profile: str) -> str:
        return await self.openrouter.generate_cover_letter(vacancy_text, user_profile)

# ==================== КЛАВИАТУРЫ ====================

def get_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📋 Меню")]],
        resize_keyboard=True,
        one_time_keyboard=False
    )

def get_status_keyboard(vacancy_id: int, current_status: str) -> InlineKeyboardMarkup:
    buttons = []
    status_flow = ['saved', 'applied', 'interview', 'offer', 'rejected']
    status_names = {
        'applied': '📤 Отклик',
        'interview': '✅ Интервью',
        'offer': '🏆 Оффер',
        'rejected': '❌ Отказ'
    }
    
    if current_status in status_flow:
        idx = status_flow.index(current_status)
        next_statuses = status_flow[idx+1:]
        
        row = []
        for status in next_statuses:
            if status in status_names:
                row.append(InlineKeyboardButton(
                    text=status_names[status],
                    callback_data=f"status_{vacancy_id}_{status}"
                ))
        if row:
            buttons.append(row)
    
    buttons.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_{vacancy_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_save_keyboard(vacancy_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="💾 Сохранить в трекер", callback_data=f"save_{vacancy_id}")
        ]]
    )

def get_unsubscribe_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Отписаться", callback_data=f"unsub_{subscription_id}")
        ]]
    )

def get_menu_text() -> str:
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

# ==================== СОСТОЯНИЯ FSM ====================

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

# ==================== ИНИЦИАЛИЗАЦИЯ ====================

# Создаем репозиторий и клиенты (без создания сессии)
repository = SQLiteRepository(DB_PATH)
remoteok_client = RemoteOKClient()  # сессия создастся позже
vacancy_service = VacancyService(remoteok_client, repository)
analytics_service = AnalyticsService(repository)
cover_letter_service = CoverLetterService()

# Бот создаем с токеном
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ==================== ОБРАБОТЧИКИ ====================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    logger.info(f"User {message.from_user.id} started the bot")
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
@dp.message(Command("help"))
async def show_menu(message: types.Message):
    await message.answer(get_menu_text(), parse_mode="HTML", reply_markup=get_main_keyboard())

@dp.message(Command("search"))
async def cmd_search(message: types.Message, state: FSMContext):
    await message.answer(
        "🔍 <b>Шаг 1 из 3: Технология</b>\n\n"
        "Какую технологию ищешь? (например: <b>python</b>, <b>react</b>)",
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
            if not salary_digits:
                raise ValueError
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
    
    location_text = message.text.strip().lower()
    location = None if location_text in ["пропустить", "skip", "не важно", "-", "worldwide"] else location_text
    
    await state.update_data(location=location)
    data = await state.get_data()
    tech, salary, location = data.get("tech"), data.get("salary"), data.get("location")
    
    search_summary = f"🔎 <b>Ищу по параметрам:</b>\n• Технология: <b>{tech}</b>\n"
    search_summary += f"• Зарплата: <b>от ${salary:,}</b>\n" if salary else "• Зарплата: <b>любая</b>\n"
    search_summary += f"• Регион: <b>{location}</b>\n" if location else "• Регион: <b>любой</b>\n"
    await message.answer(search_summary + "\n⏳ Ищу вакансии...", parse_mode="HTML", reply_markup=get_main_keyboard())
    
    params = SearchParams(tech=tech, salary=salary, location=location)
    vacancies = await vacancy_service.search_vacancies(params, limit=5)
    
    if not vacancies:
        await message.answer("😕 Ничего не найдено. Попробуй другие фильтры.", reply_markup=get_main_keyboard())
        await state.clear()
        return
    
    await message.answer(f"🌍 Найдено <b>{len(vacancies)}</b> вакансий!\n", parse_mode="HTML", reply_markup=get_main_keyboard())
    
    for i, vac in enumerate(vacancies, 1):
        tags_str = ", ".join(vac.tags[:5]) if vac.tags else "—"
        
        vac_text = (
            f"<b>{i}. {vac.title}</b>\n"
            f"🏢 <b>Компания:</b> {vac.company}\n"
            f"💰 <b>Зарплата:</b> {vac.salary}\n"
            f"📍 <b>Локация:</b> {vac.location}\n"
            f"🏷 <b>Теги:</b> {tags_str}\n"
            f"📅 <b>Опубликовано:</b> {vac.date or '—'}\n\n"
            f"📋 <b>Требования:</b>\n<i>{vac.description}</i>\n\n"
            f"🔗 <a href='{vac.url}'>Открыть вакансию</a>"
        )
        
        if len(vac_text) > 4000:
            vac_text = vac_text[:3950] + "\n\n... (обрезано)"
        
        await message.answer(
            vac_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=get_save_keyboard(vac.id)
        )
    
    await state.clear()

@dp.callback_query(F.data.startswith("save_"))
async def save_vacancy_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    vacancy_id = callback.data.replace("save_", "")
    
    success = vacancy_service.save_vacancy_for_user(user_id, vacancy_id)
    
    if success:
        await callback.answer("✅ Вакансия сохранена!", show_alert=True)
    else:
        await callback.answer("⚠️ Эта вакансия уже сохранена", show_alert=True)

@dp.message(Command("my_vacancies"))
async def cmd_my_vacancies(message: types.Message):
    user_id = message.from_user.id
    vacancies = vacancy_service.get_user_vacancies(user_id)
    
    if not vacancies:
        await message.answer(
            "📭 У тебя пока нет сохранённых вакансий.\n\nИспользуй /search",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        return
    
    status_emojis = {
        'saved': '📥 Сохранена',
        'applied': '📤 Отклик отправлен',
        'interview': '✅ Собеседование',
        'offer': '🏆 Оффер!',
        'rejected': '❌ Отказ'
    }
    
    await message.answer(
        f"📋 <b>Твои сохранённые вакансии ({len(vacancies)}):</b>\n",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    
    for vac in vacancies:
        vac_db_id, title, company, salary, location, url, saved_at, status = vac
        current_status = status_emojis.get(status, '📥 Сохранена')
        
        text = (
            f"<b>{title}</b>\n"
            f"🏢 {company}\n"
            f"💰 {salary}\n"
            f"📍 {location}\n"
            f"📊 <b>Статус:</b> {current_status}\n"
            f"🔗 <a href='{url}'>Открыть</a>"
        )
        
        await message.answer(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=get_status_keyboard(vac_db_id, status)
        )

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    user_id = message.from_user.id
    stats = analytics_service.get_user_stats(user_id)
    
    if stats.total == 0:
        await message.answer(
            "📊 У тебя пока нет статистики. Начни с /search!",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        return
    
    progress_bar = f"[{'█' * stats.conversion_rate}{'░' * (10 - stats.conversion_rate)}] {stats.conversion_rate}%"
    
    text = (
        f"📊 <b>Твоя карьерная статистика</b>\n\n"
        f"📥 Всего сохранено: <b>{stats.total}</b>\n"
        f"📤 Откликов отправлено: <b>{stats.applied}</b>\n"
        f"✅ Приглашений на интервью: <b>{stats.interview}</b>\n"
        f"🏆 Получено офферов: <b>{stats.offer}</b>\n"
        f"❌ Отказов: <b>{stats.rejected}</b>\n\n"
        f"📈 <b>Конверсия в интервью:</b>\n{progress_bar}"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard())

@dp.callback_query(F.data.startswith("status_"))
async def change_status_callback(callback: types.CallbackQuery):
    _, vac_db_id, new_status = callback.data.split("_")
    user_id = callback.from_user.id
    
    success = vacancy_service.update_status(user_id, int(vac_db_id), new_status)
    
    if success:
        status_names = {
            'applied': '📤 Отклик отправлен',
            'interview': '✅ Собеседование назначено',
            'offer': '🏆 Поздравляю с оффером!',
            'rejected': '❌ Статус изменен на Отказ'
        }
        await callback.answer(status_names.get(new_status, new_status))
        
        vacancies = vacancy_service.get_user_vacancies(user_id)
        for vac in vacancies:
            if vac[0] == int(vac_db_id):
                await callback.message.edit_reply_markup(
                    reply_markup=get_status_keyboard(int(vac_db_id), new_status)
                )
                break
    else:
        await callback.answer(
            "❌ Нельзя перепрыгивать через статусы! Используй кнопки последовательно.",
            show_alert=True
        )

@dp.callback_query(F.data.startswith("delete_"))
async def delete_vacancy_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    vac_db_id = int(callback.data.replace("delete_", ""))
    
    if vacancy_service.delete_vacancy(user_id, vac_db_id):
        await callback.answer("✅ Вакансия удалена", show_alert=True)
        await callback.message.delete()
    else:
        await callback.answer("❌ Не удалось удалить", show_alert=True)

@dp.message(Command("subscribe"))
async def cmd_subscribe(message: types.Message, state: FSMContext):
    await message.answer(
        "🔔 <b>Подписка на уведомления</b>\n\n"
        "🔍 <b>Шаг 1 из 3: Технология</b>\n"
        "Какую технологию ищешь? (например: <b>python</b>)",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    await state.set_state(SubscriptionStates.waiting_tech)

@dp.message(StateFilter(SubscriptionStates.waiting_tech), F.text)
async def get_sub_tech(message: types.Message, state: FSMContext):
    if message.text == "📋 Меню":
        await show_menu(message)
        await state.clear()
        return
    
    tech = message.text.strip().lower()
    if not tech:
        await message.answer("⚠️ Введи хотя бы одно слово", reply_markup=get_main_keyboard())
        return
    
    await state.update_data(sub_tech=tech)
    await message.answer(
        "💰 <b>Шаг 2 из 3: Минимальная зарплата</b>\n\n"
        "Сколько хочешь зарабатывать в год (в долларах)?\n"
        "Например: <b>80000</b>\n\n"
        "💡 Если не важно — напиши '<b>пропустить</b>'",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    await state.set_state(SubscriptionStates.waiting_salary)

@dp.message(StateFilter(SubscriptionStates.waiting_salary), F.text)
async def get_sub_salary(message: types.Message, state: FSMContext):
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
            if not salary_digits:
                raise ValueError
            salary = int(salary_digits)
        except ValueError:
            await message.answer("⚠️ Не понял число. Введи число или 'пропустить'", reply_markup=get_main_keyboard())
            return
    
    await state.update_data(sub_salary=salary)
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
    await state.set_state(SubscriptionStates.waiting_location)

@dp.message(StateFilter(SubscriptionStates.waiting_location), F.text)
async def get_sub_location(message: types.Message, state: FSMContext):
    if message.text == "📋 Меню":
        await show_menu(message)
        await state.clear()
        return
    
    user_id = message.from_user.id
    location_text = message.text.strip().lower()
    location = None if location_text in ["пропустить", "skip", "не важно", "-", "worldwide"] else location_text
    
    await state.update_data(sub_location=location)
    data = await state.get_data()
    tech, salary, location = data.get("sub_tech"), data.get("sub_salary"), data.get("sub_location")
    
    if repository.add_subscription(user_id, tech, salary, location):
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
    subscriptions = repository.get_user_subscriptions(user_id)
    
    if not subscriptions:
        await message.answer(
            "📭 У тебя нет активных подписок.\n\nИспользуй /subscribe.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        return
    
    await message.answer(
        f"🔔 <b>Твои подписки ({len(subscriptions)}):</b>\n",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    
    for sub in subscriptions:
        sub_text = f"🔍 <b>{sub.tech}</b>\n"
        sub_text += f"💰 От ${sub.salary:,}\n" if sub.salary else "💰 Любая зарплата\n"
        sub_text += f"🌍 {sub.location}\n" if sub.location else "🌍 Любой регион\n"
        
        await message.answer(
            sub_text,
            parse_mode="HTML",
            reply_markup=get_unsubscribe_keyboard(sub.id)
        )

@dp.callback_query(F.data.startswith("unsub_"))
async def unsubscribe_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    sub_id = int(callback.data.replace("unsub_", ""))
    
    if repository.delete_subscription(sub_id, user_id):
        await callback.answer("✅ Ты отписался от уведомлений", show_alert=True)
        await callback.message.delete()
    else:
        await callback.answer("❌ Не удалось отписаться", show_alert=True)

@dp.message(Command("cover_letter"))
async def start_cover_letter(message: types.Message, state: FSMContext):
    await message.answer(
        "📝 Скинь мне <b>описание вакансии</b> (скопируй текст с сайта).",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    await state.set_state(CoverLetterStates.waiting_for_vacancy)

@dp.message(StateFilter(CoverLetterStates.waiting_for_vacancy), F.text)
async def get_vacancy_text(message: types.Message, state: FSMContext):
    if message.text == "📋 Меню":
        await show_menu(message)
        await state.clear()
        return
    
    await state.update_data(vacancy_text=message.text)
    await message.answer(
        "Принял! Теперь напиши в двух словах <b>свой опыт</b> (технологии, годы работы).",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    await state.set_state(CoverLetterStates.waiting_for_profile)

@dp.message(StateFilter(CoverLetterStates.waiting_for_profile), F.text)
async def generate_letter(message: types.Message, state: FSMContext):
    if message.text == "📋 Меню":
        await show_menu(message)
        await state.clear()
        return
    
    user_profile = message.text
    data = await state.get_data()
    vacancy_text = data.get("vacancy_text", "")
    
    await message.answer("⏳ Генерирую письмо с помощью AI...", reply_markup=get_main_keyboard())
    
    letter = await cover_letter_service.generate(vacancy_text, user_profile)
    
    await message.answer(
        f"🎉 <b>Готово! Вот твоё письмо:</b>\n\n{letter}",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    await state.clear()

# ==================== ФОНОВАЯ ЗАДАЧА ====================

async def check_new_vacancies():
    """Проверка новых вакансий для подписок"""
    logger.info("Checking new vacancies for subscriptions...")
    subscriptions = repository.get_active_subscriptions()
    
    if not subscriptions:
        logger.info("No active subscriptions")
        return
    
    for sub in subscriptions:
        params = SearchParams(tech=sub.tech, salary=sub.salary, location=sub.location)
        vacancies = await remoteok_client.search(params, limit=10)
        
        if not vacancies:
            continue
        
        sent_vacancies = repository.get_sent_vacancies(sub.user_id)
        new_vacancies = [v for v in vacancies if v.id not in sent_vacancies]
        
        if not new_vacancies:
            continue
        
        await bot.send_message(
            sub.user_id,
            f"🔔 <b>Новые вакансии по твоей подписке!</b>\n\n"
            f"🔍 Технология: <b>{sub.tech}</b>\n"
            f"Найдено <b>{len(new_vacancies)}</b> новых вакансий:",
            parse_mode="HTML"
        )
        
        for vac in new_vacancies[:3]:
            text = (
                f"<b>{vac.title}</b>\n"
                f"🏢 {vac.company}\n"
                f"💰 {vac.salary}\n"
                f"🔗 <a href='{vac.url}'>Открыть</a>"
            )
            
            await bot.send_message(
                sub.user_id,
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=get_save_keyboard(vac.id)
            )
            
            repository.mark_vacancy_sent(sub.user_id, vac.id, sub.id)
    
    logger.info("Subscription check completed")

# ==================== ЗАПУСК ====================

async def main():
    """Главная функция запуска бота"""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_new_vacancies, 'cron', hour=10, minute=0)
    scheduler.start()
    
    logger.info("=" * 50)
    logger.info("🚀 БОТ ЗАПУЩЕН!")
    logger.info("=" * 50)
    
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.critical(f"Critical error: {e}", exc_info=True)
    finally:
        scheduler.shutdown()
        await remoteok_client.close()
        logger.info("Bot stopped")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
