"""
reaction_bot.py — Sirf REACTIONS ke liye dedicated bot.
Ye bot views bilkul nahi karta. Alag DB table: reaction_clients
config.py mein REACTION_BOT_TOKEN add karo.

Fixes:
- Dead/deleted/frozen accounts ka permanent cleanup
- Session + JSON + DB cleanup consistency
- Safer channel peer handling
- Lower/safer concurrency
- Stale cache invalidation
- Better error handling
"""

import asyncio
import json
import os
import random
import warnings
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from typing import Optional
from math import ceil

try:
    import fcntl
except ImportError:
    fcntl = None

warnings.filterwarnings("ignore", message=".*per_message.*")

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from pyrogram import Client as PyroClient
from pyrogram.errors import (
    SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired,
    FloodWait, UserAlreadyParticipant, ReactionInvalid,
    ChannelPrivate, InviteHashInvalid, InviteHashExpired,
    UsernameInvalid, UsernameNotOccupied, PeerIdInvalid
)
from pyrogram.raw import functions as raw_functions
from pyrogram.raw import types as raw_types

import config

# ── Logging ──────────────────────────────────────────────────
_LOG_FMT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_LOG_TO_FILE = os.environ.get("REACT_BOT_LOG_FILE")
_log_handlers = [logging.StreamHandler()]
if _LOG_TO_FILE:
    try:
        _log_handlers = [RotatingFileHandler(_LOG_TO_FILE, maxBytes=5 * 1024 * 1024,
                                             backupCount=3, encoding="utf-8")]
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format=_LOG_FMT, handlers=_log_handlers)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("pyrogram").setLevel(logging.ERROR)
logging.getLogger("pyrogram.session").setLevel(logging.ERROR)
logging.getLogger("pyrogram.connection").setLevel(logging.ERROR)
logging.getLogger("asyncio").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────
USE_POSTGRES      = getattr(config, 'USE_POSTGRES', True)
POSTGRES_HOST     = getattr(config, 'POSTGRES_HOST', 'localhost')
POSTGRES_PORT     = getattr(config, 'POSTGRES_PORT', 5432)
POSTGRES_DB       = getattr(config, 'POSTGRES_DB', 'zayrosmm')
POSTGRES_USER     = getattr(config, 'POSTGRES_USER', 'postgres')
POSTGRES_PASSWORD = getattr(config, 'POSTGRES_PASSWORD', 'zayrosmm')

BOT_TOKEN = getattr(config, 'REACTION_BOT_TOKEN', config.BOT_TOKEN)

POSTGRES_AVAILABLE = False
if USE_POSTGRES:
    try:
        import asyncpg
        POSTGRES_AVAILABLE = True
        logger.info("PostgreSQL support enabled (Reaction Bot)")
    except ImportError:
        logger.warning("asyncpg not installed. Run: pip install asyncpg")
        USE_POSTGRES = False

# safer concurrency defaults
ACCOUNTS_PER_PAGE         = 10
CLIENTS_PER_PAGE          = 5
ACTION_DELAY_MIN          = 0.7
ACTION_DELAY_MAX          = 1.8
RECENT_POST_LIMIT         = 50
SESSION_START_BATCH_SIZE   = 8
SESSION_START_DELAY       = 4
CHANNEL_CONCURRENCY       = 3
GLOBAL_ACTION_CONCURRENCY = 18

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
DATA_FILE    = os.path.join(BASE_DIR, "data", "data.json")
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)

# ── Emoji helpers ────────────────────────────────────────────
def ce(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

E_CROWN   = ce("5039727497143387500","👑"); E_STAR  = ce("5042176294222037888","⭐")
E_FIRE    = ce("5389038097860144794","🔥"); E_CHECK = ce("5039844895779455925","✅")
E_CROSS   = ce("5040042498634810056","❌"); E_PHONE = ce("5407025283456835913","📱")
E_CHANNEL = ce("5041888071851705019","📣"); E_CAL   = ce("5413879192267805083","🗓")
E_CLOCK   = ce("6285240160120477644","⏰"); E_WARN  = ce("5039665997506675838","⚠️")
E_GREEN   = ce("5039928501612839813","🟢"); E_RED   = ce("5042042652019655612","🔴")
E_YELLOW  = ce("5339082633160703625","🟡"); E_LOCK  = ce("5305609152704297298","🔒")
E_ROCKET  = ce("5389057356493511934","🚀"); E_BELL  = ce("5042111805288089118","🔔")
E_GIFT    = ce("5039778134807806727","🎁"); E_CHART = ce("5042290883949495533","📊")
E_PERSON  = ce("6165860934242798778","👤"); E_REFRESH = ce("5041837837914211014","🔄")
E_TRASH   = ce("5039614900280754969","🗑"); E_SHIELD = ce("5042328396193864923","🛡")
E_PLUS    = ce("5039844895779455925","➕"); E_LEFT  = ce("5041837837914211014","⬅️")
E_RIGHT   = ce("5041837837914211014","➡️"); E_PAGE  = ce("5042290883949495533","📄")
E_SPARKLE = ce("5389038097860144794","✨"); E_HEART = ce("5040042498634810056","💖")
E_TARGET  = ce("5041888071851705019","🎯"); E_DIAMOND= ce("6285240160120477644","💎")

REACTION_EMOJIS_WEIGHTED = {
    "❤": 25, "👍": 22, "🔥": 20, "🎉": 15, "😍": 18, "👏": 14,
    "🥰": 16, "💯": 12, "👀": 10, "💪": 11, "🏆": 8,  "🎯": 9,
    "🤩": 13, "😎": 17, "🙏": 10, "🌟": 14, "💥": 9,  "✨": 12,
    "🎊": 8,  "🥳": 10,
}
REACTION_EMOJIS = list(REACTION_EMOJIS_WEIGHTED.keys())

(
    ADD_PHONE, ADD_OTP, ADD_2FA,
    SETUP_UID, SETUP_CHAN, SETUP_ACCS, SETUP_REACTS, SETUP_DAYS,
    ADD_MORE_ACCOUNT, IMPORT_ZIP, JOINALL_CHAN,
    ADMIN_ADD_ID, ADMIN_ADD_NAME,
    ADD_CONFIRM_RELOGIN,
) = range(14)

def normalize_channel(raw: str) -> str:
    raw = raw.strip()
    if raw.lstrip("-").isdigit():
        return raw
    if "+joinchat/" in raw or "/joinchat/" in raw:
        return raw
    if raw.startswith("https://t.me/+") or raw.startswith("http://t.me/+"):
        return raw
    if raw.startswith("t.me/+"):
        return "https://" + raw
    for prefix in ["https://t.me/", "http://t.me/", "t.me/"]:
        if raw.startswith(prefix):
            username = raw[len(prefix):].split("/")[0].split("?")[0]
            return "@" + username if not username.startswith("@") else username
    if raw.startswith("@"):
        return raw
    return "@" + raw

def c_channel(c: dict) -> str: return c.get("channel_link") or c.get("channel", "")
def c_channel_id(c: dict) -> Optional[int]:
    cid = c.get("channel_id")
    if cid:
        try: return int(cid)
        except: pass
    return None
def c_joined(c: dict) -> list: return c.get("joined_phones") or c.get("joined_accounts") or []
def c_count(c: dict) -> int: return c.get("join_count") or c.get("accounts_count") or 0
def c_user_id(c: dict) -> int: return c.get("owner_id") or c.get("user_id") or 0

def parse_expiry(val) -> datetime:
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(float(val))
    try:
        return datetime.fromisoformat(str(val))
    except:
        return datetime.now()

def days_left(c: dict) -> Optional[float]:
    val = c.get("subscribed_until")
    if val is None:
        return None
    try:
        return (parse_expiry(val) - datetime.now()).total_seconds() / 86400
    except:
        return None

def expiry_str(c: dict) -> str:
    val = c.get("subscribed_until")
    if val is None:
        return f"{E_WARN} No expiry set"
    try:
        dt = parse_expiry(val)
        diff = dt - datetime.now()
        if diff.total_seconds() < 0:
            return f"{E_RED} <b>EXPIRED</b>"
        days = diff.days
        if days == 0:
            return f"{E_WARN} Expires today ({int(diff.total_seconds()/3600)}h left)"
        return f"{E_CAL} {days}d left ({dt.strftime('%d %b %Y')})"
    except:
        return f"{E_WARN} Unknown expiry"

def status_dot(status: str) -> str:
    return {"online": E_GREEN, "running": E_ROCKET, "stopped": E_RED, "expired": E_YELLOW}.get(status, E_RED)

def make_bar(current: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "░" * width
    filled = min(int((current / total) * width), width)
    return "█" * filled + "░" * (width - filled)

# ── DB Managers ──────────────────────────────────────────────
class PostgresDataManager:
    TABLE = "reaction_clients"

    def __init__(self, host, port, database, user, password):
        self.host = host; self.port = port; self.database = database
        self.user = user; self.password = password
        self._pool = None

    async def connect(self):
        import asyncpg
        self._pool = await asyncpg.create_pool(
            host=self.host, port=self.port, database=self.database,
            user=self.user, password=self.password,
            min_size=3, max_size=12, command_timeout=60
        )
        await self._create_tables()
        logger.info("PostgreSQL DataManager initialized (reaction_clients)")

    async def disconnect(self):
        if self._pool:
            await self._pool.close()

    async def _create_tables(self):
        async with self._pool.acquire() as conn:
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE} (
                    client_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    channel_link TEXT NOT NULL,
                    channel_id BIGINT DEFAULT NULL,
                    channel_name TEXT DEFAULT '',
                    join_count INTEGER DEFAULT 0,
                    reactions_per_post INTEGER DEFAULT 0,
                    subscribed_until TIMESTAMP NOT NULL,
                    status TEXT DEFAULT 'running',
                    joined_phones JSONB DEFAULT '[]',
                    total_reactions BIGINT DEFAULT 0,
                    last_post_id BIGINT DEFAULT 0,
                    added_on TIMESTAMP DEFAULT NOW(),
                    notified_stages TEXT DEFAULT '',
                    admin_id TEXT DEFAULT ''
                )
            """)
            await conn.execute(f"CREATE INDEX IF NOT EXISTS idx_rc_owner ON {self.TABLE}(owner_id)")
            await conn.execute(f"CREATE INDEX IF NOT EXISTS idx_rc_status ON {self.TABLE}(status)")
            await conn.execute(f"CREATE INDEX IF NOT EXISTS idx_rc_admin ON {self.TABLE}(admin_id)")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS react_admins (
                    admin_id TEXT PRIMARY KEY,
                    name TEXT DEFAULT '',
                    added_by TEXT DEFAULT '',
                    added_on TIMESTAMP DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS react_global_stats (
                    id TEXT PRIMARY KEY DEFAULT 'global',
                    total_reactions BIGINT DEFAULT 0
                )
            """)
            await conn.execute("""
                INSERT INTO react_global_stats (id, total_reactions)
                VALUES ('global', 0) ON CONFLICT (id) DO NOTHING
            """)

    def _row_to_client(self, row) -> dict:
        c = dict(row)
        for key in ['subscribed_until', 'added_on']:
            if c.get(key):
                c[key] = c[key].timestamp()
        if isinstance(c.get('joined_phones'), str):
            c['joined_phones'] = json.loads(c['joined_phones'])
        return c

    async def get_clients(self) -> dict:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(f"SELECT * FROM {self.TABLE}")
        return {r['client_id']: self._row_to_client(r) for r in rows}

    async def get_clients_sorted(self) -> list:
        c = await self.get_clients()
        return sorted(c.items(), key=lambda x: x[0])

    async def get_clients_by_owner(self, owner_id: str) -> list:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(f"SELECT * FROM {self.TABLE} WHERE owner_id = $1 ORDER BY client_id", str(owner_id))
        return [(r['client_id'], self._row_to_client(r)) for r in rows]

    async def next_client_id(self, user_id: int) -> str:
        uid = str(user_id)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT client_id FROM {self.TABLE} WHERE client_id LIKE $1 ORDER BY client_id DESC LIMIT 1",
                f"{uid}_%"
            )
        if not row:
            return f"{uid}_1"
        try:
            return f"{uid}_{int(row['client_id'].split('_')[1]) + 1}"
        except:
            return f"{uid}_1"

    async def add_client(self, client_id, user_id, channel, accounts_count, reactions, days, admin_id=""):
        expiry = datetime.now() + timedelta(days=days)
        async with self._pool.acquire() as conn:
            await conn.execute(f"""
                INSERT INTO {self.TABLE} (
                    client_id, owner_id, channel_link, channel_id, channel_name,
                    join_count, reactions_per_post, subscribed_until,
                    status, joined_phones, total_reactions, last_post_id, added_on, admin_id
                ) VALUES ($1,$2,$3,NULL,$4,$5,$6,$7,'running',$8::jsonb,0,0,NOW(),$9)
            """, client_id, str(user_id), channel, channel, accounts_count,
                 reactions, expiry, json.dumps([]), str(admin_id))

    async def get_client(self, client_id: str) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(f"SELECT * FROM {self.TABLE} WHERE client_id = $1", client_id)
        return self._row_to_client(row) if row else None

    async def update_client(self, client_id: str, **kwargs):
        if not kwargs:
            return
        fields, values = [], []
        for key, value in kwargs.items():
            fields.append(f"{key} = ${len(values)+2}")
            values.append(json.dumps(value) if key == "joined_phones" and isinstance(value, list) else value)
        async with self._pool.acquire() as conn:
            await conn.execute(f"UPDATE {self.TABLE} SET {', '.join(fields)} WHERE client_id = $1", client_id, *values)

    async def delete_client(self, client_id: str):
        async with self._pool.acquire() as conn:
            await conn.execute(f"DELETE FROM {self.TABLE} WHERE client_id = $1", client_id)

    async def extend_client(self, client_id: str, days: int):
        c = await self.get_client(client_id)
        if not c:
            return
        current = parse_expiry(c.get("subscribed_until", datetime.now().timestamp()))
        new_exp = max(current, datetime.now()) + timedelta(days=days)
        await self.update_client(client_id, subscribed_until=new_exp, notified_stages="")

    async def is_subscribed(self, client_id: str) -> bool:
        c = await self.get_client(client_id)
        if not c:
            return False
        try:
            return parse_expiry(c.get("subscribed_until", 0)) > datetime.now()
        except:
            return False

    async def add_global_stats(self, reactions: int):
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE react_global_stats SET total_reactions = total_reactions + $1 WHERE id = 'global'",
                reactions
            )

    async def stats(self) -> dict:
        async with self._pool.acquire() as conn:
            cli_count = await conn.fetchval(f"SELECT COUNT(*) FROM {self.TABLE}")
            active_count = await conn.fetchval(f"SELECT COUNT(*) FROM {self.TABLE} WHERE status='running'")
            gstats = await conn.fetchrow("SELECT * FROM react_global_stats WHERE id='global'")
            cr = await conn.fetchval(f"SELECT COALESCE(SUM(total_reactions),0) FROM {self.TABLE}")
        online_count = len(acm.get_online_accounts()) if acm else 0
        json_db = JsonDataManager(DATA_FILE)
        acc_count = len(json_db.get_accounts())
        return {
            "total_accounts": acc_count,
            "online": online_count,
            "offline": acc_count - online_count,
            "total_clients": cli_count or 0,
            "active_clients": active_count or 0,
            "total_reactions": max(gstats['total_reactions'] if gstats else 0, cr or 0),
        }

    async def get_admins(self) -> list:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT admin_id, name, added_by FROM react_admins ORDER BY added_on")
        return [dict(r) for r in rows]

    async def add_admin(self, admin_id: str, name: str = "", added_by: str = ""):
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO react_admins (admin_id, name, added_by) VALUES ($1, $2, $3)
                ON CONFLICT (admin_id) DO UPDATE SET name = EXCLUDED.name
            """, str(admin_id), name, str(added_by))

    async def remove_admin(self, admin_id: str):
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM react_admins WHERE admin_id = $1", str(admin_id))

class JsonDataManager:
    def __init__(self, path: str):
        self.path = path
        self._data = {"accounts": {}}
        self._lock = asyncio.Lock()
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    if fcntl:
                        fcntl.flock(f, fcntl.LOCK_SH)
                    self._data = json.load(f)
                    if fcntl:
                        fcntl.flock(f, fcntl.LOCK_UN)
                self._data.setdefault("accounts", {})
            except Exception as e:
                logger.error(f"Failed to load JSON: {e}. Starting fresh.")
                self._data = {"accounts": {}}
                self.save()
        else:
            self.save()

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                if fcntl:
                    fcntl.flock(f, fcntl.LOCK_EX)
                json.dump(self._data, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
                if fcntl:
                    fcntl.flock(f, fcntl.LOCK_UN)
        except Exception as e:
            logger.error(f"Failed to save JSON: {e}")

    def get_accounts(self) -> dict:
        return self._data.get("accounts", {})

    def get_accounts_sorted(self) -> list:
        return sorted(self._data.get("accounts", {}).items(), key=lambda x: x[0])

    def add_account(self, phone: str, first_name: str = "", user_id: int = 0,
                    api_id: int = None, api_hash: str = None):
        self._data["accounts"][phone] = {
            "phone": phone, "first_name": first_name, "username": None,
            "user_id": user_id, "logged_in_at": datetime.now().timestamp(),
            "api_id": api_id, "api_hash": api_hash
        }
        self.save()

    def delete_account(self, phone: str):
        self._data["accounts"].pop(phone, None)
        self.save()

class HybridDataManager:
    def __init__(self):
        self.json_db = JsonDataManager(DATA_FILE)
        self.pg_db = None
        self._using_postgres = False

    async def connect(self):
        if USE_POSTGRES and POSTGRES_AVAILABLE:
            try:
                self.pg_db = PostgresDataManager(
                    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB,
                    POSTGRES_USER, POSTGRES_PASSWORD
                )
                await self.pg_db.connect()
                self._using_postgres = True
            except Exception as e:
                logger.error(f"PostgreSQL connection failed: {e}")
                self._using_postgres = False

    async def disconnect(self):
        if self.pg_db:
            await self.pg_db.disconnect()

    def get_accounts(self) -> dict: return self.json_db.get_accounts()
    def get_accounts_sorted(self) -> list: return self.json_db.get_accounts_sorted()
    def add_account(self, phone, first_name="", user_id=0, api_id=None, api_hash=None):
        return self.json_db.add_account(phone, first_name, user_id, api_id, api_hash)
    def delete_account(self, phone): return self.json_db.delete_account(phone)

    async def get_clients(self) -> dict:
        if self._using_postgres: return await self.pg_db.get_clients()
        return {}
    async def get_clients_sorted(self) -> list:
        if self._using_postgres: return await self.pg_db.get_clients_sorted()
        return []
    async def get_clients_by_owner(self, owner_id: str) -> list:
        if self._using_postgres: return await self.pg_db.get_clients_by_owner(owner_id)
        return []
    async def next_client_id(self, user_id: int) -> str:
        if self._using_postgres: return await self.pg_db.next_client_id(user_id)
        return f"{user_id}_1"
    async def add_client(self, client_id, user_id, channel, accounts_count, reactions, days, admin_id=""):
        if self._using_postgres:
            return await self.pg_db.add_client(client_id, user_id, channel, accounts_count, reactions, days, admin_id)
    async def get_client(self, client_id: str) -> Optional[dict]:
        if self._using_postgres: return await self.pg_db.get_client(client_id)
        return None
    async def update_client(self, client_id: str, **kwargs):
        if self._using_postgres: return await self.pg_db.update_client(client_id, **kwargs)
    async def delete_client(self, client_id: str):
        if self._using_postgres: return await self.pg_db.delete_client(client_id)
    async def extend_client(self, client_id: str, days: int):
        if self._using_postgres: return await self.pg_db.extend_client(client_id, days)
    async def is_subscribed(self, client_id: str) -> bool:
        if self._using_postgres: return await self.pg_db.is_subscribed(client_id)
        return False
    async def add_global_stats(self, reactions: int):
        if self._using_postgres: return await self.pg_db.add_global_stats(reactions)
    async def stats(self) -> dict:
        if self._using_postgres: return await self.pg_db.stats()
        online_count = len(acm.get_online_accounts()) if acm else 0
        acc_count = len(self.json_db.get_accounts())
        return {"total_accounts": acc_count, "online": online_count,
                "offline": acc_count - online_count, "total_clients": 0,
                "active_clients": 0, "total_reactions": 0}
    async def get_admins(self) -> list:
        if self._using_postgres: return await self.pg_db.get_admins()
        return []
    async def add_admin(self, admin_id, name="", added_by=""):
        if self._using_postgres: return await self.pg_db.add_admin(admin_id, name, added_by)
    async def remove_admin(self, admin_id: str):
        if self._using_postgres: return await self.pg_db.remove_admin(admin_id)

db = None
acm = None

async def init_database():
    global db
    db = HybridDataManager()
    await db.connect()

# ── AccountManager ───────────────────────────────────────────
class AccountManager:
    def __init__(self):
        self._clients: dict[str, PyroClient] = {}
        self._pending: dict[str, dict] = {}
        self._channel_id_cache: dict[str, int] = {}
        self._account_semaphores: dict[str, asyncio.Semaphore] = {}
        self._chat_cache: dict[str, dict] = {}
        self._user_info_cache: dict[int, dict] = {}
        self._channel_info_cache: dict[str, dict] = {}
        self._reactions_cache: dict = {}
        self._action_sem = asyncio.Semaphore(GLOBAL_ACTION_CONCURRENCY)

    def _session_path(self, phone: str) -> str:
        return os.path.join(SESSIONS_DIR, phone.replace("+", "").replace(" ", ""))

    def _find_session_name(self, phone: str) -> Optional[str]:
        candidates = [
            self._session_path(phone),
            os.path.join(SESSIONS_DIR, phone),
            os.path.join(SESSIONS_DIR, phone.replace(" ", ""))
        ]
        for name in candidates:
            if os.path.exists(name + ".session"):
                return name
        return None

    def _creds_for(self, phone: str) -> tuple:
        try:
            info = db.get_accounts().get(phone) if db else None
        except Exception:
            info = None
        if info and info.get("api_id") and info.get("api_hash"):
            return info["api_id"], info["api_hash"]
        return config.API_ID, config.API_HASH

    def _make_client(self, phone: str, api_id=None, api_hash=None) -> PyroClient:
        if api_id is None or api_hash is None:
            api_id, api_hash = self._creds_for(phone)
        return PyroClient(self._session_path(phone), api_id=api_id, api_hash=api_hash, no_updates=True)

    def get_account_semaphore(self, phone: str) -> asyncio.Semaphore:
        if phone not in self._account_semaphores:
            self._account_semaphores[phone] = asyncio.Semaphore(1)
        return self._account_semaphores[phone]

    async def start_session(self, phone: str) -> bool:
        if phone in self._clients:
            return True
        session_name = self._find_session_name(phone)
        if not session_name:
            return False
        try:
            api_id, api_hash = self._creds_for(phone)
            cl = PyroClient(session_name, api_id=api_id, api_hash=api_hash, no_updates=True)
            await cl.start()
            self._clients[phone] = cl
            self._account_semaphores[phone] = asyncio.Semaphore(1)
            self._chat_cache[phone] = {}
            return True
        except Exception as e:
            logger.error(f"start_session {phone}: {e}")
            return False

    async def stop_session(self, phone: str):
        cl = self._clients.pop(phone, None)
        self._account_semaphores.pop(phone, None)
        self._chat_cache.pop(phone, None)
        if cl:
            try:
                await cl.stop()
            except:
                pass

    async def stop_all(self):
        for phone in list(self._clients):
            await self.stop_session(phone)

    def is_online(self, phone: str) -> bool:
        return phone in self._clients

    def get_online_accounts(self) -> list:
        return list(self._clients.keys())

    def get_online_count(self) -> int:
        return len(self._clients)

    def invalidate_channel_cache(self, key: str):
        self._channel_id_cache.pop(key, None)
        self._reactions_cache.pop(str(key), None)

    async def get_chat_cached(self, phone: str, channel_identifier):
        if phone not in self._chat_cache:
            self._chat_cache[phone] = {}
        cache_key = str(channel_identifier)
        if cache_key not in self._chat_cache[phone]:
            cl = self._clients.get(phone)
            if cl:
                chat = await cl.get_chat(channel_identifier)
                if not hasattr(chat, "id"):
                    return None
                self._chat_cache[phone][cache_key] = chat
        return self._chat_cache[phone].get(cache_key)

    async def resolve_channel_id(self, phone: str, raw_channel: str) -> Optional[int]:
        raw_channel = raw_channel.strip()
        if raw_channel.lstrip("-").isdigit():
            return int(raw_channel)
        if raw_channel in self._channel_id_cache:
            return self._channel_id_cache[raw_channel]
        chat = await self.get_chat_cached(phone, normalize_channel(raw_channel))
        if chat:
            self._channel_id_cache[raw_channel] = chat.id
            return chat.id
        return None

    async def join_channel(self, phone: str, raw_channel: str, stored_id=None) -> Optional[int]:
        cl = self._clients.get(phone)
        if not cl:
            return None
        identifier = normalize_channel(raw_channel)
        try:
            chat = await cl.join_chat(identifier)
            self._channel_id_cache[raw_channel] = chat.id
            if phone in self._chat_cache:
                self._chat_cache[phone][raw_channel] = chat
            await asyncio.sleep(random.uniform(0.1, 0.3))
            return chat.id
        except UserAlreadyParticipant:
            cid = stored_id or await self.resolve_channel_id(phone, raw_channel)
            if cid:
                self._channel_id_cache[raw_channel] = cid
            return cid
        except (InviteHashInvalid, InviteHashExpired, ChannelPrivate, UsernameInvalid, UsernameNotOccupied):
            return None
        except FloodWait as e:
            await asyncio.sleep(e.value)
            return None
        except:
            return None

    async def get_message_ids_after(self, phone: str, channel_identifier, after_id: int, limit: int = RECENT_POST_LIMIT) -> list:
        try:
            chat = await self.get_chat_cached(phone, channel_identifier)
            if not chat:
                return []
            cl = self._clients.get(phone)
            if not cl:
                return []
            ids = []
            async for msg in cl.get_chat_history(chat.id, limit=limit):
                ids.append(msg.id)
            if not ids:
                return []
            if not after_id:
                return [max(ids)]
            new_ids = [msg_id for msg_id in ids if msg_id > after_id]
            return sorted(new_ids) if new_ids else []
        except Exception as e:
            logger.debug(f"get_message_ids_after error for {phone}: {e}")
            return []

    async def get_allowed_reactions(self, phone: str, channel_identifier) -> Optional[list]:
        key = str(channel_identifier)
        if key in self._reactions_cache:
            return self._reactions_cache[key]
        allowed = None
        try:
            chat = await self.get_chat_cached(phone, channel_identifier)
            ar = getattr(chat, "available_reactions", None)
            if ar is not None and not getattr(ar, "all_are_enabled", False):
                emojis = [getattr(r, "emoji", None) for r in (getattr(ar, "reactions", None) or [])]
                allowed = [e for e in emojis if e]
        except Exception:
            allowed = None
        self._reactions_cache[key] = allowed
        return allowed

    async def _handle_send_error(self, phone: str, err: Exception):
        msg = str(err).upper()
        permanent = (
            "USER_DEACTIVATED", "AUTH_KEY_UNREGISTERED", "SESSION_REVOKED",
            "USER_DEACTIVATED_BAN", "AUTH_KEY_DUPLICATED"
        )
        frozen = ("FROZEN_METHOD_INVALID", "FROZEN")
        if any(m in msg for m in permanent):
            logger.warning(f"Purging dead account {phone}: {err}")
            await purge_account(phone)
        elif any(m in msg for m in frozen):
            logger.warning(f"Purging frozen account {phone}: {err}")
            await purge_account(phone)
        elif "PEER ID INVALID" in msg:
            self.invalidate_channel_cache(msg)
        else:
            logger.debug(f"send error for {phone}: {err}")

    async def send_reaction(self, phone: str, channel_identifier, message_id: int, emoji: str = "❤") -> bool:
        async with self.get_account_semaphore(phone), self._action_sem:
            try:
                chat = await self.get_chat_cached(phone, channel_identifier)
                if not chat:
                    return False
                cl = self._clients.get(phone)
                if not cl:
                    return False
                await asyncio.sleep(random.uniform(ACTION_DELAY_MIN, ACTION_DELAY_MAX))
                try:
                    await cl.invoke(raw_functions.messages.SendReaction(
                        peer=await cl.resolve_peer(chat.id),
                        msg_id=message_id,
                        add_to_recent=False,
                        reaction=[raw_types.ReactionEmoji(emoticon=emoji)]
                    ))
                    return True
                except ReactionInvalid:
                    try:
                        await cl.invoke(raw_functions.messages.SendReaction(
                            peer=await cl.resolve_peer(chat.id),
                            msg_id=message_id,
                            add_to_recent=False,
                            reaction=[raw_types.ReactionEmoji(emoticon=random.choice(REACTION_EMOJIS))]
                        ))
                        return True
                    except:
                        return False
            except Exception as e:
                await self._handle_send_error(phone, e)
                return False

    async def send_otp(self, phone: str) -> str:
        cl = self._make_client(phone)
        await cl.connect()
        sent = await cl.send_code(phone)
        self._pending[phone] = {"client": cl, "phone_code_hash": sent.phone_code_hash}
        return sent.phone_code_hash

    async def verify_otp(self, phone: str, code: str) -> str:
        p = self._pending.get(phone)
        if not p:
            return "no_pending"
        try:
            cl = p["client"]
            result = cl.sign_in(phone, p["phone_code_hash"], code)
            user = await result if asyncio.iscoroutine(result) or asyncio.isfuture(result) else result
            if user is None:
                try:
                    user = await cl.get_me()
                except:
                    user = None
            self._clients[phone] = cl
            self._account_semaphores[phone] = asyncio.Semaphore(1)
            self._chat_cache[phone] = {}
            self._pending.pop(phone, None)
            await db.add_account(
                phone,
                first_name=getattr(user, "first_name", "") if user else "",
                user_id=getattr(user, "id", 0) if user else 0
            )
            return "ok"
        except SessionPasswordNeeded:
            return "2fa"
        except (PhoneCodeInvalid, PhoneCodeExpired):
            return "invalid"
        except Exception as e:
            logger.error(f"verify_otp {phone}: {type(e).__name__}: {e}")
            return f"error:{type(e).__name__}:{e}"

    async def verify_2fa(self, phone: str, password: str) -> bool:
        p = self._pending.get(phone)
        if not p:
            return False
        try:
            user = await p["client"].check_password(password)
            self._clients[phone] = p["client"]
            self._account_semaphores[phone] = asyncio.Semaphore(1)
            self._chat_cache[phone] = {}
            self._pending.pop(phone, None)
            await db.add_account(phone, first_name=getattr(user, "first_name", ""), user_id=getattr(user, "id", 0))
            return True
        except:
            return False

    async def cancel_pending(self, phone: str):
        p = self._pending.pop(phone, None)
        if p:
            try:
                await p["client"].disconnect()
            except:
                pass

    async def get_account_info(self, phone: str) -> Optional[dict]:
        cl = self._clients.get(phone)
        if not cl:
            return None
        try:
            me = await cl.get_me()
            return {"name": f"{me.first_name or ''} {me.last_name or ''}".strip(),
                    "username": me.username or "N/A", "id": me.id}
        except:
            return None

    async def get_user_info(self, user_id: int) -> Optional[dict]:
        if user_id in self._user_info_cache:
            return self._user_info_cache[user_id]
        for phone in self.get_online_accounts():
            cl = self._clients.get(phone)
            if not cl:
                continue
            try:
                user = await cl.get_users(user_id)
                if user:
                    info = {"name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
                            "username": user.username or "N/A", "id": user.id}
                    self._user_info_cache[user_id] = info
                    return info
            except:
                continue
        return None

    async def get_channel_info(self, channel_link: str) -> Optional[dict]:
        if channel_link in self._channel_info_cache:
            return self._channel_info_cache[channel_link]
        for phone in self.get_online_accounts():
            cl = self._clients.get(phone)
            if not cl:
                continue
            try:
                chat = await cl.get_chat(normalize_channel(channel_link))
                if chat:
                    info = {"title": getattr(chat, "title", channel_link),
                            "username": getattr(chat, "username", "N/A"),
                            "id": chat.id, "members": getattr(chat, "members_count", 0)}
                    self._channel_info_cache[channel_link] = info
                    return info
            except:
                continue
        return None

# ── Reaction Distributor ─────────────────────────────────────
class ReactionDistributor:
    def __init__(self):
        self.emoji_weights = REACTION_EMOJIS_WEIGHTED
        self.emojis  = list(self.emoji_weights.keys())
        self.weights = list(self.emoji_weights.values())

    def distribute(self, total_reactions: int, allowed_emojis=None) -> dict:
        if total_reactions <= 0: return {}
        pool = allowed_emojis if allowed_emojis else self.emojis
        if not pool: pool = self.emojis
        pool_weights = [self.emoji_weights.get(e, 10) for e in pool]
        cap = min(len(pool), total_reactions)
        lo = max(1, min(8, cap)); hi = max(lo, min(11, cap))
        num_emojis = random.randint(lo, hi)
        selected, available, avail_weights = [], list(pool), list(pool_weights)
        for _ in range(num_emojis):
            if not available: break
            total_w = sum(avail_weights)
            r = random.uniform(0, total_w) if total_w > 0 else 0
            cumsum, pick = 0, available[0]
            for i, (emoji, w) in enumerate(zip(available, avail_weights)):
                cumsum += w
                if r <= cumsum: pick = emoji; break
            selected.append(pick); idx = available.index(pick)
            available.pop(idx); avail_weights.pop(idx)
        if not selected: return {}
        selected_weights = [self.emoji_weights.get(e, 10) for e in selected]
        sorted_pairs = sorted(zip(selected, selected_weights), key=lambda x: x[1], reverse=True)
        selected = [e for e, _ in sorted_pairs]
        if random.random() < 0.3 and len(selected) >= 3:
            top3 = selected[:3]; random.shuffle(top3); selected = top3 + selected[3:]
        counts = {}; remaining = total_reactions
        shares = [0.30, 0.22, 0.15, 0.11, 0.08] + [0.05] * max(0, len(selected) - 5)
        for i, emoji in enumerate(selected):
            if remaining <= 0: break
            share = shares[i] if i < len(shares) else 0.05
            count = max(1, min(int(total_reactions * share * random.uniform(0.85, 1.15)), remaining))
            counts[emoji] = count; remaining -= count
        if remaining > 0:
            for emoji in list(counts.keys())[:3]:
                if remaining <= 0: break
                add = min(remaining, random.randint(1, max(1, remaining // 2)))
                counts[emoji] += add; remaining -= add
        return counts

    def get_emoji_for_reaction(self, reaction_index: int, total_reactions: int,
                               allowed_emojis=None) -> str:
        pool = allowed_emojis if allowed_emojis else self.emojis
        if not pool: pool = self.emojis
        pool_weights = [self.emoji_weights.get(e, 10) for e in pool]
        total_w = sum(pool_weights)
        if total_w <= 0: return random.choice(pool)
        r = random.uniform(0, total_w); cumsum = 0
        for emoji, w in zip(pool, pool_weights):
            cumsum += w
            if r <= cumsum: return emoji
        return pool[-1]


reaction_distributor = ReactionDistributor()


# ── ServiceManager (reactions only) ─────────────────────────
class ServiceManager:
    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}
        self._probe_indices: dict = {}
        self._rr_index: dict = {}
        self._channel_sem = asyncio.Semaphore(CHANNEL_CONCURRENCY)

    def is_running(self, client_id: str) -> bool:
        t = self._tasks.get(client_id)
        return t is not None and not t.done()

    def start(self, client_id: str):
        if self.is_running(client_id): return
        asyncio.create_task(db.update_client(client_id, status="running"))
        self._tasks[client_id] = asyncio.create_task(self._run(client_id))

    def stop(self, client_id: str):
        t = self._tasks.pop(client_id, None)
        if t and not t.done(): t.cancel()
        asyncio.create_task(db.update_client(client_id, status="stopped"))

    def stop_all(self):
        for cid in list(self._tasks): self.stop(cid)

    def shutdown(self):
        for cid in list(self._tasks):
            t = self._tasks.pop(cid, None)
            if t and not t.done(): t.cancel()

    def _pick_round_robin(self, key: str, eligible: list, count: int) -> list:
        n = len(eligible)
        if n == 0 or count <= 0: return []
        count = min(count, n)
        start = self._rr_index.get(key, 0) % n
        picked = [eligible[(start + i) % n] for i in range(count)]
        self._rr_index[key] = (start + count) % n
        return picked

    async def _process_one_message(self, cid, channel_id, channel_ref,
                                   msg_id, eligible, max_reacts, allowed_reacts) -> int:
        react_pool  = eligible
        react_count = min(max_reacts, len(react_pool))
        react_workers = self._pick_round_robin(channel_id + "|r", react_pool, react_count) if react_count > 0 else []

        react_distribution = reaction_distributor.distribute(react_count, allowed_reacts)
        emoji_assignments = []
        idx = 0
        for emoji, count in react_distribution.items():
            for _ in range(count):
                if idx < len(react_workers):
                    emoji_assignments.append((react_workers[idx], emoji)); idx += 1
        while idx < len(react_workers):
            emoji = reaction_distributor.get_emoji_for_reaction(idx, react_count, allowed_reacts)
            emoji_assignments.append((react_workers[idx], emoji)); idx += 1

        if not emoji_assignments: return 0

        results = await asyncio.gather(
            *(acm.send_reaction(phone, channel_ref, msg_id, emoji)
              for phone, emoji in emoji_assignments),
            return_exceptions=True)
        total_reacts = sum(1 for r in results if r is True)

        logger.info(
            f"[{cid}] ✓ +React:{total_reacts}/{max_reacts}"
            f" | msg:{msg_id} | accs:{len(eligible)} (react:{len(react_workers)})"
        )
        return total_reacts

    async def _process_single_channel(self, cid: str):
        async with self._channel_sem:
            try:
                c = await db.get_client(cid)
                if not c or not await db.is_subscribed(cid): return

                raw_channel = c_channel(c)
                max_reacts  = c.get("reactions_per_post", 0)
                last_id     = c.get("last_post_id", 0)
                channel_id  = str(c_channel_id(c) or raw_channel)
                channel_ref = c_channel_id(c) or raw_channel

                eligible = [p for p in c_joined(c) if acm.is_online(p)]
                if not eligible: return

                pidx  = self._probe_indices.get(channel_id, 0) % len(eligible)
                self._probe_indices[channel_id] = pidx + 1
                probe = eligible[pidx]

                if not c_channel_id(c):
                    resolved = await acm.resolve_channel_id(probe, raw_channel)
                    if resolved:
                        channel_ref = resolved
                        await db.update_client(cid, channel_id=resolved)

                message_ids = await acm.get_message_ids_after(probe, channel_ref, last_id)
                if not message_ids: return

                allowed_reacts = await acm.get_allowed_reactions(probe, channel_ref)

                # All new posts processed CONCURRENTLY — no sequential delay
                msg_coros = [
                    self._process_one_message(
                        cid, channel_id, channel_ref, msg_id,
                        eligible, max_reacts, allowed_reacts)
                    for msg_id in message_ids
                ]
                all_results = await asyncio.gather(*msg_coros, return_exceptions=True)
                total_reacts = sum(r for r in all_results if isinstance(r, int))

                latest_processed = max(message_ids)
                new_r = c.get("total_reactions", 0) + total_reacts
                await db.update_client(cid, last_post_id=latest_processed, total_reactions=new_r)
                await db.add_global_stats(total_reacts)

                if len(message_ids) > 1:
                    logger.info(f"[{cid}] processed {len(message_ids)} posts | "
                                f"{message_ids[0]}-{latest_processed}")
            except Exception as e:
                logger.error(f"Channel {cid} error: {e}")

    async def _run(self, client_id: str):
        while True:
            try:
                await self._process_single_channel(client_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"run {client_id}: {e}")
            try:
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break


svm = ServiceManager()

SUPER_ADMINS = set(config.ADMIN_IDS)
DB_ADMINS: set = set()

def is_super_admin(uid: int) -> bool: return uid in SUPER_ADMINS
def is_admin(uid: int) -> bool:       return uid in SUPER_ADMINS or uid in DB_ADMINS

async def load_db_admins():
    global DB_ADMINS
    try:
        admins  = await db.get_admins()
        DB_ADMINS = set(int(a["admin_id"]) for a in admins
                        if str(a["admin_id"]).lstrip("-").isdigit())
        logger.info(f"Loaded {len(DB_ADMINS)} sub-admin(s)")
    except Exception as e:
        logger.error(f"load_db_admins: {e}")

async def visible_clients(uid: int) -> list:
    all_clients = await db.get_clients_sorted()
    if is_super_admin(uid): return all_clients
    return [(cid, c) for cid, c in all_clients if str(c.get("admin_id") or "") == str(uid)]

async def can_manage(uid: int, cid: str) -> bool:
    if is_super_admin(uid): return True
    c = await db.get_client(cid)
    return bool(c) and str(c.get("admin_id") or "") == str(uid)


def btn(text, callback_data, style=None, emoji_id=None):
    api_kwargs = {}
    if style:    api_kwargs["style"] = style
    if emoji_id: api_kwargs["icon_custom_emoji_id"] = emoji_id
    if api_kwargs: return InlineKeyboardButton(text, callback_data=callback_data, api_kwargs=api_kwargs)
    return InlineKeyboardButton(text, callback_data=callback_data)

def btn_url(text, url, style=None, emoji_id=None):
    api_kwargs = {}
    if style:    api_kwargs["style"] = style
    if emoji_id: api_kwargs["icon_custom_emoji_id"] = emoji_id
    if api_kwargs: return InlineKeyboardButton(text, url=url, api_kwargs=api_kwargs)
    return InlineKeyboardButton(text, url=url)

async def error_handler(update, ctx):
    logger.error(f"Error: {ctx.error}", exc_info=ctx.error)

async def notify(bot, chat_id, text: str) -> bool:
    if not chat_id: return False
    try:
        await bot.send_message(chat_id=int(chat_id), text=text, parse_mode=ParseMode.HTML)
        return True
    except Exception as e:
        logger.info(f"notify -> {chat_id} failed: {e}")
        return False

def get_page_items(items: list, page: int, per_page: int) -> tuple:
    total_pages = max(1, ceil(len(items) / per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    return items[start:start + per_page], total_pages

def get_pagination_keyboard(current_page: int, total_pages: int, prefix: str) -> list:
    rows, nav = [], []
    if current_page > 1:   nav.append(btn("Prev", f"{prefix}_page_{current_page-1}", "primary"))
    nav.append(btn(f"📄 {current_page}/{total_pages}", "noop", "primary"))
    if current_page < total_pages: nav.append(btn("Next", f"{prefix}_page_{current_page+1}", "primary"))
    if nav: rows.append(nav)
    return rows

def _fmt_days(dl) -> str:
    if dl is None: return "N/A"
    if dl < 0: return "expired"
    d = int(dl)
    if d >= 1: return f"{d} day{'s' if d != 1 else ''}"
    hours = max(0, int(dl * 24))
    return f"{hours} hour{'s' if hours != 1 else ''}"


# ─── UI: Home ────────────────────────────────────────────────
async def home_menu(update, ctx):
    uid = update.effective_user.id if update.effective_user else 0
    s   = await db.stats()
    if not is_super_admin(uid):
        my = await visible_clients(uid)
        s  = dict(s)
        s["total_clients"]   = len(my)
        s["active_clients"]  = sum(1 for cid, _ in my if svm.is_running(cid))
        s["total_reactions"] = sum(c.get("total_reactions", 0) for _, c in my)
    online_pct = (s["online"] / s["total_accounts"] * 100) if s["total_accounts"] > 0 else 0
    active_pct = (s["active_clients"] / s["total_clients"] * 100) if s["total_clients"] > 0 else 0
    acc_bar    = make_bar(s["online"], s["total_accounts"])
    cli_bar    = make_bar(s["active_clients"], s["total_clients"])
    text = (
        f"{E_CROWN} <b>REACTION BOT PANEL</b> {E_CROWN}\n"
        f"<i>Sirf Reactions — Super Fast</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{E_PHONE} <b>ACCOUNTS</b>  ·  {s['online']}/{s['total_accounts']} online ({online_pct:.0f}%)\n"
        f"<code>{acc_bar}</code>\n"
        f"{E_GREEN} Online <b>{s['online']}</b>   {E_RED} Offline <b>{s['offline']}</b>\n\n"
        f"{E_PERSON} <b>CLIENTS</b>  ·  {s['active_clients']}/{s['total_clients']} active ({active_pct:.0f}%)\n"
        f"<code>{cli_bar}</code>\n\n"
        f"{E_CHART} <b>DELIVERED</b>\n"
        f"{E_FIRE} Reactions <code>{s['total_reactions']:,}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{E_CLOCK} <i>{datetime.now().strftime('%d %b %Y · %H:%M:%S')}</i>"
    )
    kb_rows = [
        [btn("Accounts", "menu_accounts", "primary"), btn("Clients", "menu_clients", "primary")],
        [btn("New Client Setup", "menu_setup", "success")],
        [btn("Join All Accounts", "joinall_menu", "primary")],
        [btn("Expiring Soon", "menu_expiring", "danger"), btn("Live Stats", "menu_stats", "primary")],
    ]
    if is_super_admin(uid):
        kb_rows.append([btn("Manage Admins", "menu_admins", "primary")])
    kb_rows.append([btn("Refresh", "menu_home", "primary")])
    kb = InlineKeyboardMarkup(kb_rows)
    msg = update.message or update.callback_query.message
    if update.callback_query:
        try:
            await msg.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception as e:
            if "Message is not modified" not in str(e):
                await msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def show_accounts_page(message, page: int = 1):
    accounts = db.get_accounts_sorted()
    if not accounts:
        try:
            await message.edit_text(
                f"{E_PHONE} <b>Accounts</b>\n\nNo accounts added yet.",
                reply_markup=InlineKeyboardMarkup([
                    [btn("Add Account", "acc_add", "success")],
                    [btn("Import Sessions (ZIP)", "acc_import", "primary")],
                    [btn("Home", "menu_home", "primary")],
                ]), parse_mode=ParseMode.HTML)
        except Exception as e:
            if "Message is not modified" not in str(e): raise
        return
    online_count = sum(1 for phone, _ in accounts if acm.is_online(phone))
    page_items, total_pages = get_page_items(accounts, page, ACCOUNTS_PER_PAGE)
    text = (
        f"{E_PHONE} <b>Accounts Manager</b>\n"
        f"Total: <b>{len(accounts)}</b> | {E_GREEN} {online_count} Online | {E_RED} {len(accounts)-online_count} Offline\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    rows = []
    for phone, info in page_items:
        dot  = E_GREEN if acm.is_online(phone) else E_RED
        name = (info.get("first_name", "") or "Account")[:15]
        text += f"{dot} <code>{phone}</code> <b>{name}</b>\n"
        rows.append([
            btn("Start", f"acc_start|{phone}", "success"),
            btn("Stop",  f"acc_stop|{phone}",  "danger"),
            btn("Info",  f"acc_info|{phone}",  "primary"),
            btn("Del",   f"acc_del|{phone}",   "danger"),
        ])
    rows += get_pagination_keyboard(page, total_pages, "acc")
    rows += [
        [btn("Add Account", "acc_add", "success"), btn("Import ZIP", "acc_import", "primary")],
        [btn("Start All", "acc_start_all", "success"), btn("Stop All", "acc_stop_all", "danger")],
        [btn("Remove Dead / Frozen", "acc_remove_dead", "danger")],
        [btn("Home", "menu_home", "primary")],
    ]
    try:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
    except Exception as e:
        if "Message is not modified" not in str(e):
            try: await message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
            except Exception: pass


async def show_clients_page(message, page: int = 1, uid: int = 0):
    clients = await visible_clients(uid)
    if not clients:
        try:
            await message.edit_text(
                f"{E_PERSON} <b>Clients</b>\n\nNo clients yet.",
                reply_markup=InlineKeyboardMarkup([
                    [btn("New Client", "menu_setup", "success")],
                    [btn("Home", "menu_home", "primary")],
                ]), parse_mode=ParseMode.HTML)
        except Exception as e:
            if "Message is not modified" not in str(e): raise
        return
    running_count = sum(1 for cid, _ in clients if svm.is_running(cid))
    page_items, total_pages = get_page_items(clients, page, CLIENTS_PER_PAGE)
    text = (
        f"{E_PERSON} <b>Clients Manager (Reactions)</b>\n"
        f"Total: <b>{len(clients)}</b> | {E_ROCKET} {running_count} Running | {E_RED} {len(clients)-running_count} Stopped\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    rows = []
    for cid, c in page_items:
        status   = "running" if svm.is_running(cid) else c.get("status", "stopped")
        name     = (c.get("channel_name") or c_channel(c))[:25]
        joined   = len(c_joined(c))
        total    = c_count(c)
        sub_icon = E_GREEN if await db.is_subscribed(cid) else E_RED
        text += (
            f"{status_dot(status)} <b>{cid}</b> {sub_icon}\n"
            f"  {E_CHANNEL} <code>{name}</code>\n"
            f"  {E_PHONE} {joined}/{total} | {E_FIRE} R:{c.get('reactions_per_post',0)}\n\n"
        )
        rows.append([
            btn(f"{cid}", f"client_detail|{cid}", "primary"),
            btn("Start",   f"svc_start|{cid}", "success"),
            btn("Stop",    f"svc_stop|{cid}",  "danger"),
        ])
    rows += get_pagination_keyboard(page, total_pages, "cli")
    rows += [
        [btn("Start All", "svc_start_all", "success"), btn("Stop All", "svc_stop_all", "danger")],
        [btn("Home", "menu_home", "primary")],
    ]
    try:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
    except Exception as e:
        if "Message is not modified" not in str(e):
            try: await message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
            except Exception: pass


async def _show_client_detail(message, cid: str):
    c = await db.get_client(cid)
    if not c:
        await message.edit_text("Client not found.")
        return
    status   = "running" if svm.is_running(cid) else c.get("status", "stopped")
    joined   = c_joined(c)
    online_j = [p for p in joined if acm.is_online(p)]
    owner_id = c_user_id(c)
    user_info = await acm.get_user_info(owner_id) if owner_id else None
    user_display = (f"@{user_info['username']}" if user_info and user_info.get("username") != "N/A"
                    else (user_info["name"] if user_info else f"User {owner_id}"))
    channel_link = c_channel(c)
    channel_info = await acm.get_channel_info(channel_link) if channel_link else None
    channel_display = channel_info["title"] if channel_info else channel_link
    text = (
        f"{E_CROWN} <b>CLIENT DETAILS (Reactions)</b> {E_CROWN}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{E_TARGET} <b>ID:</b> <code>{cid}</code>\n"
        f"{E_PERSON} <b>Owner:</b> <code>{user_display}</code>\n"
        f"{E_CHANNEL} <b>Channel:</b> <code>{channel_display}</code>\n"
        f"{status_dot(status)} <b>Status:</b> <b>{status.upper()}</b>\n"
        f"{expiry_str(c)}\n\n"
        f"<b>PACKAGE</b>\n"
        f"{E_PHONE} Accounts: <b>{len(joined)}</b>/{c_count(c)}  ({len(online_j)} online)\n"
        f"{E_FIRE} Reactions/Post: <b>{c.get('reactions_per_post',0)}</b>\n\n"
        f"<b>DELIVERED</b>\n"
        f"{E_FIRE} Reactions: <code>{c.get('total_reactions',0):,}</code>"
    )
    rows = [
        [btn("Start", f"svc_start|{cid}", "success"), btn("Stop", f"svc_stop|{cid}", "danger")],
        [btn("+30d", f"extend|{cid}|30", "primary"),
         btn("+60d", f"extend|{cid}|60", "primary"),
         btn("+90d", f"extend|{cid}|90", "primary")],
        [btn("View All User Subs", f"user_subs|{owner_id}", "primary")],
        [btn("Remove", f"remove_client|{cid}", "danger"), btn("Back", "menu_clients", "primary")],
    ]
    try:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
    except Exception as e:
        if "Message is not modified" not in str(e):
            await message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)


# ── Helper functions ─────────────────────────────────────────
def _delete_account_files(phone: str):
    for cand in [os.path.join(SESSIONS_DIR, phone.replace("+", "").replace(" ", "")),
                 os.path.join(SESSIONS_DIR, phone)]:
        for ext in [".session", ".session-journal", ".session-wal", ".session-shm", ".json"]:
            f = cand + ext
            if os.path.exists(f):
                try: os.remove(f)
                except Exception: pass

async def purge_account(phone: str):
    try: await acm.stop_session(phone)
    except Exception: pass
    try: db.delete_account(phone)
    except Exception: pass
    _delete_account_files(phone)
    try:
        for cid, c in await db.get_clients_sorted():
            joined = c_joined(c)
            if phone in joined:
                await db.update_client(cid, joined_phones=[p for p in joined if p != phone])
    except Exception as e:
        logger.debug(f"purge_account cleanup {phone}: {e}")

async def do_join_and_start(cid: str) -> dict:
    c = await db.get_client(cid)
    if not c: return {"error": "Client not found"}
    raw_channel = c_channel(c)
    needed      = c_count(c)
    joined      = list(c_joined(c))
    stored_id   = c_channel_id(c)
    online      = acm.get_online_accounts()
    if not online: return {"error": "No online accounts."}
    to_join  = [p for p in online if p not in joined][:max(0, needed - len(joined))]
    channel_id = stored_id
    results  = await asyncio.gather(*[acm.join_channel(p, raw_channel, stored_id) for p in to_join],
                                    return_exceptions=True)
    for phone, result_id in zip(to_join, results):
        if isinstance(result_id, int) and result_id:
            joined.append(phone)
            if not channel_id: channel_id = result_id
    await db.update_client(cid, joined_phones=joined, channel_id=channel_id)
    if not joined: return {"error": "Could not join any accounts."}
    svm.start(cid)
    return {"joined": len(joined), "channel_id": channel_id, "started": True}

async def join_accounts_to_all_clients(phones: list) -> int:
    phones  = [p for p in phones if acm.is_online(p)]
    if not phones: return 0
    clients = await db.get_clients_sorted()
    total   = 0
    for cid, c in clients:
        raw       = c_channel(c)
        stored_id = c_channel_id(c)
        joined    = list(c_joined(c))
        to_join   = [p for p in phones if p not in joined]
        if not to_join: continue
        for i in range(0, len(to_join), SESSION_START_BATCH_SIZE):
            batch   = to_join[i:i + SESSION_START_BATCH_SIZE]
            results = await asyncio.gather(*(acm.join_channel(p, raw, stored_id) for p in batch),
                                           return_exceptions=True)
            for p, rid in zip(batch, results):
                if isinstance(rid, int) and rid:
                    joined.append(p); total += 1
                    if not stored_id: stored_id = rid
            if i + SESSION_START_BATCH_SIZE < len(to_join):
                await asyncio.sleep(SESSION_START_DELAY)
        await db.update_client(cid, joined_phones=joined, channel_id=stored_id)
    return total


# ── Account management handlers ──────────────────────────────
async def accounts_menu(update, ctx):
    await update.callback_query.answer()
    await show_accounts_page(update.callback_query.message, 1)

async def clients_menu(update, ctx):
    await update.callback_query.answer()
    await show_clients_page(update.callback_query.message, 1, update.effective_user.id)

async def acc_page_handler(update, ctx):
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    if q.data.startswith("acc_page_"):
        await show_accounts_page(q.message, int(q.data.split("_")[-1]))

async def clients_page_handler(update, ctx):
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    if q.data.startswith("cli_page_"):
        await show_clients_page(q.message, int(q.data.split("_")[-1]), update.effective_user.id)

async def acc_bulk_action(update, ctx):
    q = update.callback_query
    try: await q.answer("Processing...")
    except Exception: pass
    if q.data == "acc_start_all":
        all_phones = [phone for phone, _ in db.get_accounts_sorted() if not acm.is_online(phone)]
        started = 0
        for i in range(0, len(all_phones), SESSION_START_BATCH_SIZE):
            batch   = all_phones[i:i + SESSION_START_BATCH_SIZE]
            results = await asyncio.gather(*(acm.start_session(p) for p in batch), return_exceptions=True)
            started += sum(1 for r in results if r is True)
            if i + SESSION_START_BATCH_SIZE < len(all_phones):
                await asyncio.sleep(SESSION_START_DELAY)
        await q.message.reply_text(f"{E_CHECK} Started <b>{started}</b> accounts.", parse_mode=ParseMode.HTML)
    elif q.data == "acc_stop_all":
        phones = acm.get_online_accounts()
        for phone in phones: await acm.stop_session(phone)
        await q.message.reply_text(f"{E_RED} Stopped <b>{len(phones)}</b> accounts.", parse_mode=ParseMode.HTML)
    await show_accounts_page(q.message, 1)

async def acc_remove_dead(update, ctx):
    q = update.callback_query
    try: await q.answer("Scanning...")
    except Exception: pass
    accounts = db.get_accounts_sorted()
    offline  = [phone for phone, _ in accounts if not acm.is_online(phone)]
    status   = await q.message.reply_text(
        f"{E_CLOCK} Testing <b>{len(offline)}</b> offline accounts...", parse_mode=ParseMode.HTML)
    dead = []
    for i in range(0, len(offline), SESSION_START_BATCH_SIZE):
        batch   = offline[i:i + SESSION_START_BATCH_SIZE]
        results = await asyncio.gather(*(acm.start_session(p) for p in batch), return_exceptions=True)
        for p, r in zip(batch, results):
            if r is not True: dead.append(p)
        if i + SESSION_START_BATCH_SIZE < len(offline):
            await asyncio.sleep(SESSION_START_DELAY)
    for phone in dead: await purge_account(phone)
    await status.edit_text(
        f"{E_TRASH} <b>Cleanup Complete</b>\n"
        f"{E_CROSS} Removed: <b>{len(dead)}</b>\n"
        f"{E_GREEN} Revived: <b>{len(offline) - len(dead)}</b>",
        parse_mode=ParseMode.HTML)
    await show_accounts_page(q.message, 1)

async def acc_action(update, ctx):
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    parts  = q.data.split("|", 1)
    action, phone = parts[0], parts[1]
    if action == "acc_start":
        ok = await acm.start_session(phone)
        await q.message.reply_text(f"{E_CHECK} Online" if ok else f"{E_CROSS} Failed", parse_mode=ParseMode.HTML)
    elif action == "acc_stop":
        await acm.stop_session(phone)
        await q.message.reply_text(f"{E_RED} Stopped.", parse_mode=ParseMode.HTML)
    elif action == "acc_info":
        info = await acm.get_account_info(phone)
        if info:
            await q.message.reply_text(
                f"{E_PERSON} <b>{info['name']}</b>\n@{info['username']}\nID: <code>{info['id']}</code>",
                parse_mode=ParseMode.HTML)
        else:
            await q.message.reply_text(f"{E_WARN} Offline.", parse_mode=ParseMode.HTML)
        return
    elif action == "acc_del":
        await purge_account(phone)
        await q.message.reply_text(f"{E_TRASH} Deleted.", parse_mode=ParseMode.HTML)
    await show_accounts_page(q.message, 1)

async def client_detail(update, ctx):
    q   = update.callback_query
    try: await q.answer()
    except Exception: pass
    cid = q.data.split("|", 1)[1]
    uid = update.effective_user.id if update.effective_user else 0
    if not await can_manage(uid, cid):
        await q.answer("Access denied — not your client.", show_alert=True); return
    await _show_client_detail(q.message, cid)

async def client_action(update, ctx):
    q   = update.callback_query
    try: await q.answer("Processing...")
    except Exception: pass
    uid   = update.effective_user.id if update.effective_user else 0
    parts = q.data.split("|")
    action = parts[0]
    if action == "svc_start_all":
        clients  = await visible_clients(uid)
        eligible = [cid for cid, _ in clients if await db.is_subscribed(cid) and not svm.is_running(cid)]
        results  = await asyncio.gather(*(do_join_and_start(cid) for cid in eligible), return_exceptions=True)
        started  = sum(1 for r in results if isinstance(r, dict) and "error" not in r)
        await q.message.reply_text(f"{E_ROCKET} Started <b>{started}</b> clients.", parse_mode=ParseMode.HTML)
        await show_clients_page(q.message, 1, uid); return
    if action == "svc_stop_all":
        running = [cid for cid, _ in await visible_clients(uid) if svm.is_running(cid)]
        for cid in running: svm.stop(cid)
        await q.message.reply_text(f"{E_RED} Stopped <b>{len(running)}</b> clients.", parse_mode=ParseMode.HTML)
        await show_clients_page(q.message, 1, uid); return
    cid = parts[1]
    if not await can_manage(uid, cid):
        await q.message.reply_text(f"{E_CROSS} Access denied.", parse_mode=ParseMode.HTML); return
    if action == "svc_start":
        if not await db.is_subscribed(cid):
            await q.message.reply_text(f"{E_WARN} Expired!", parse_mode=ParseMode.HTML); return
        if svm.is_running(cid):
            await q.message.reply_text(f"{E_WARN} Already running.", parse_mode=ParseMode.HTML); return
        result = await do_join_and_start(cid)
        if "error" in result:
            await q.message.reply_text(f"{E_CROSS} {result['error']}", parse_mode=ParseMode.HTML)
        else:
            await q.message.reply_text(f"{E_ROCKET} Running! {result['joined']} accounts.", parse_mode=ParseMode.HTML)
    elif action == "svc_stop":
        svm.stop(cid)
        await q.message.reply_text(f"{E_RED} Stopped.", parse_mode=ParseMode.HTML)
    elif action == "extend":
        days = int(parts[2])
        await db.extend_client(cid, days)
        c   = await db.get_client(cid)
        exp = parse_expiry(c.get("subscribed_until", 0)) if c else datetime.now()
        await q.message.reply_text(f"{E_GIFT} +{days}d -> {exp.strftime('%d %b %Y')}", parse_mode=ParseMode.HTML)
    elif action == "remove_client":
        svm.stop(cid)
        await db.delete_client(cid)
        await q.message.reply_text(f"{E_TRASH} Removed.", parse_mode=ParseMode.HTML)
        await show_clients_page(q.message, 1, uid); return
    await _show_client_detail(q.message, cid)


# ── Add account flow ─────────────────────────────────────────
async def acc_add_start(update, ctx):
    from telegram.ext import ConversationHandler as CH
    if not is_admin(update.effective_user.id if update.effective_user else 0):
        await update.callback_query.answer("Access denied", show_alert=True); return CH.END
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    ctx.user_data.clear()
    await q.message.reply_text(
        f"{E_PHONE} <b>Add Account</b>\n\nEnter phone with country code:\nExample: <code>+919876543210</code>\n\n/cancel to abort",
        parse_mode=ParseMode.HTML)
    return ADD_PHONE

async def acc_recv_phone(update, ctx):
    phone = update.message.text.strip()
    if not phone.startswith("+"):
        await update.message.reply_text(f"{E_WARN} Must start with + (country code)")
        return ADD_PHONE
    ctx.user_data["add_phone"] = phone
    existing = db.get_accounts().get(phone)
    if existing:
        name = existing.get("first_name") or "Unknown"
        await update.message.reply_text(
            f"{E_WARN} <b>Account Already Exists!</b>\n\n"
            f"📱 <code>{phone}</code> — <b>{name}</b>\n\n"
            f"<b>Fresh Re-login</b> se pura purana data delete hoga aur naya create hoga.\n\n"
            f"Proceed karna chahte ho?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [btn("🔄 Fresh Re-login", "relogin_yes", "success"),
                 btn("❌ Cancel", "relogin_no", "danger")],
            ]))
        return ADD_CONFIRM_RELOGIN
    return await _do_send_otp(update, phone)

async def acc_confirm_relogin(update, ctx):
    q     = update.callback_query
    try: await q.answer()
    except Exception: pass
    phone = ctx.user_data.get("add_phone")
    if q.data == "relogin_no" or not phone:
        await q.message.reply_text(f"{E_CROSS} Cancelled.", parse_mode=ParseMode.HTML)
        from telegram.ext import ConversationHandler as CH; return CH.END
    cleaning_msg = await q.message.reply_text(
        f"{E_CLOCK} Purana sara data (session + DB) delete ho raha hai...", parse_mode=ParseMode.HTML)
    try: await acm.stop_session(phone)
    except Exception: pass
    _delete_account_files(phone)
    try: db.delete_account(phone)
    except Exception: pass
    await cleaning_msg.edit_text(
        f"{E_CHECK} Pura data delete ho gaya. Ab fresh OTP bhej raha hoon...", parse_mode=ParseMode.HTML)
    return await _do_send_otp(q.message, phone)

async def _do_send_otp(update_or_msg, phone: str):
    import telegram
    msg_obj = update_or_msg if isinstance(update_or_msg, telegram.Message) else update_or_msg.message
    msg = await msg_obj.reply_text(f"{E_CLOCK} Sending OTP to <code>{phone}</code>...", parse_mode=ParseMode.HTML)
    try:
        await acm.send_otp(phone)
        await msg.edit_text(f"{E_CHECK} OTP sent!\n\nEnter the code:", parse_mode=ParseMode.HTML)
    except FloodWait as e:
        await msg.edit_text(f"{E_WARN} FloodWait - retry after <b>{e.value}s</b>", parse_mode=ParseMode.HTML)
        await msg_obj.reply_text(f"{E_WARN} Something went wrong.\n\n<b>Add another account?</b>",
                                  parse_mode=ParseMode.HTML,
                                  reply_markup=InlineKeyboardMarkup([[
                                      btn("Add Another", "add_more", "success"),
                                      btn("Done / Home", "menu_home", "primary")]]))
        return ADD_MORE_ACCOUNT
    except Exception as e:
        await msg.edit_text(f"{E_CROSS} Error: <code>{e}</code>", parse_mode=ParseMode.HTML)
        await msg_obj.reply_text(f"{E_WARN} Something went wrong.\n\n<b>Add another account?</b>",
                                  parse_mode=ParseMode.HTML,
                                  reply_markup=InlineKeyboardMarkup([[
                                      btn("Add Another", "add_more", "success"),
                                      btn("Done / Home", "menu_home", "primary")]]))
        return ADD_MORE_ACCOUNT
    return ADD_OTP

async def acc_recv_otp(update, ctx):
    code   = update.message.text.strip()
    phone  = ctx.user_data.get("add_phone")
    result = await acm.verify_otp(phone, code)
    if result == "ok":
        asyncio.create_task(join_accounts_to_all_clients([phone]))
        await update.message.reply_text(
            f"{E_CHECK} <b>Account Added!</b>\n<code>{phone}</code> is online {E_GREEN}\n\n<b>Add another account?</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                btn("Add Another", "add_more", "success"),
                btn("Done / Home", "menu_home", "primary")]]))
        return ADD_MORE_ACCOUNT
    elif result == "2fa":
        await update.message.reply_text(f"{E_LOCK} 2FA required - Enter cloud password:", parse_mode=ParseMode.HTML)
        return ADD_2FA
    elif result == "invalid":
        await update.message.reply_text(f"{E_CROSS} Wrong code. Try again:")
        return ADD_OTP
    elif result == "no_pending":
        await update.message.reply_text(f"{E_CROSS} Session expired! Phone dobara daalo.",
                                         parse_mode=ParseMode.HTML,
                                         reply_markup=InlineKeyboardMarkup([[
                                             btn("Add Another", "add_more", "success"),
                                             btn("Done / Home", "menu_home", "primary")]]))
        return ADD_MORE_ACCOUNT
    else:
        await update.message.reply_text(
            f"{E_CROSS} Login failed!\n\n<code>{result.replace('error:','',1)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                btn("Add Another", "add_more", "success"),
                btn("Done / Home", "menu_home", "primary")]]))
        return ADD_MORE_ACCOUNT

async def acc_recv_2fa(update, ctx):
    pw    = update.message.text.strip()
    phone = ctx.user_data.get("add_phone")
    ok    = await acm.verify_2fa(phone, pw)
    if ok:
        asyncio.create_task(join_accounts_to_all_clients([phone]))
        await update.message.reply_text(
            f"{E_CHECK} <b>Account Added!</b>\n<code>{phone}</code> is online {E_GREEN}\n\n<b>Add another account?</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                btn("Add Another", "add_more", "success"),
                btn("Done / Home", "menu_home", "primary")]]))
        return ADD_MORE_ACCOUNT
    else:
        await update.message.reply_text(f"{E_CROSS} Wrong password.")
        return ADD_MORE_ACCOUNT

async def add_more_handler(update, ctx):
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    from telegram.ext import ConversationHandler as CH
    if q.data == "add_more":
        ctx.user_data.clear()
        await q.message.reply_text(
            f"{E_PHONE} Enter phone with country code:\nExample: <code>+919876543210</code>\n\n/cancel to finish",
            parse_mode=ParseMode.HTML)
        return ADD_PHONE
    elif q.data == "menu_home":
        await home_menu(update, ctx); return CH.END


# ── ZIP import ───────────────────────────────────────────────
PYROGRAM_SESSION_VERSION = 3
PYROGRAM_SESSION_SCHEMA  = """
CREATE TABLE sessions (dc_id INTEGER PRIMARY KEY,api_id INTEGER,test_mode INTEGER,auth_key BLOB,date INTEGER NOT NULL,user_id INTEGER,is_bot INTEGER);
CREATE TABLE peers (id INTEGER PRIMARY KEY,access_hash INTEGER,type INTEGER NOT NULL,username TEXT,phone_number TEXT,last_update_on INTEGER NOT NULL DEFAULT (CAST(STRFTIME('%s','now') AS INTEGER)));
CREATE TABLE version (number INTEGER PRIMARY KEY);
CREATE INDEX idx_peers_id ON peers (id);CREATE INDEX idx_peers_username ON peers (username);CREATE INDEX idx_peers_phone_number ON peers (phone_number);
CREATE TRIGGER trg_peers_last_update_on AFTER UPDATE ON peers BEGIN UPDATE peers SET last_update_on=CAST(STRFTIME('%s','now') AS INTEGER) WHERE id=NEW.id; END;
"""

def convert_telethon_session(tele_path, pyro_path, api_id, user_id, is_bot=False) -> bool:
    import sqlite3, shutil
    src = sqlite3.connect(tele_path)
    try:
        cols = [r[1] for r in src.execute("PRAGMA table_info(sessions)")]
        if not cols: raise ValueError("no sessions table")
        if "api_id" in cols:
            src.close()
            if os.path.abspath(tele_path) != os.path.abspath(pyro_path):
                shutil.copyfile(tele_path, pyro_path)
            return True
        row = src.execute("SELECT dc_id, auth_key FROM sessions WHERE auth_key IS NOT NULL LIMIT 1").fetchone()
    finally:
        try: src.close()
        except Exception: pass
    if not row or not row[1]: raise ValueError("no auth_key found in session")
    dc_id, auth_key = row
    if len(auth_key) != 256: raise ValueError(f"unexpected auth_key length {len(auth_key)}")
    if os.path.exists(pyro_path): os.remove(pyro_path)
    con = sqlite3.connect(pyro_path)
    try:
        con.executescript(PYROGRAM_SESSION_SCHEMA)
        con.execute("INSERT INTO version (number) VALUES (?)", (PYROGRAM_SESSION_VERSION,))
        con.execute("INSERT INTO sessions (dc_id,api_id,test_mode,auth_key,date,user_id,is_bot) VALUES (?,?,?,?,?,?,?)",
                    (int(dc_id), int(api_id), 0, auth_key, 0, int(user_id or 0), 1 if is_bot else 0))
        con.commit()
    finally: con.close()
    return True

async def acc_import_start(update, ctx):
    from telegram.ext import ConversationHandler as CH
    if not is_admin(update.effective_user.id if update.effective_user else 0):
        await update.callback_query.answer("Access denied", show_alert=True); return CH.END
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    await q.message.reply_text(
        f"{E_PHONE} <b>Bulk Import Sessions</b>\n\nUpload a <b>.zip</b> with pairs of:\n"
        f"• <code>&lt;phone&gt;.session</code>\n• <code>&lt;phone&gt;.json</code>\n\n/cancel to abort",
        parse_mode=ParseMode.HTML)
    return IMPORT_ZIP

async def acc_import_zip(update, ctx):
    from telegram.ext import ConversationHandler as CH
    doc = update.message.document if update.message else None
    if not doc or not (doc.file_name or "").lower().endswith(".zip"):
        await update.message.reply_text(f"{E_WARN} Please upload a <b>.zip</b> file. /cancel to abort", parse_mode=ParseMode.HTML)
        return IMPORT_ZIP
    import zipfile, tempfile, shutil
    status = await update.message.reply_text(f"{E_CLOCK} Downloading ZIP...", parse_mode=ParseMode.HTML)
    tmpdir = tempfile.mkdtemp(prefix="import_")
    try:
        try:
            f = await doc.get_file()
            zip_path = os.path.join(tmpdir, "upload.zip")
            await f.download_to_drive(zip_path)
        except Exception as e:
            await status.edit_text(f"{E_CROSS} Download failed: <code>{e}</code>", parse_mode=ParseMode.HTML)
            return CH.END
        extract_dir = os.path.join(tmpdir, "ex"); os.makedirs(extract_dir, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                for member in zf.namelist():
                    if member.endswith("/"): continue
                    target = os.path.join(extract_dir, os.path.basename(member))
                    with zf.open(member) as srcf, open(target, "wb") as outf:
                        shutil.copyfileobj(srcf, outf)
        except zipfile.BadZipFile:
            await status.edit_text(f"{E_CROSS} That's not a valid ZIP file."); return CH.END
        sessions = [x for x in os.listdir(extract_dir) if x.endswith(".session")]
        if not sessions:
            await status.edit_text(f"{E_WARN} No .session files found.", parse_mode=ParseMode.HTML); return CH.END
        await status.edit_text(f"{E_CLOCK} Found <b>{len(sessions)}</b> sessions. Importing...", parse_mode=ParseMode.HTML)
        imported_phones, skipped, skip_reasons = [], 0, []
        for sfile in sessions:
            base   = sfile[:-len(".session")]
            phone  = "+" + base.lstrip("+").replace(" ", "")
            meta   = {}
            jpath  = os.path.join(extract_dir, base + ".json")
            if os.path.exists(jpath):
                try: meta = json.load(open(jpath, encoding="utf-8"))
                except Exception: meta = {}
            api_id     = meta.get("api_id")     or config.API_ID
            api_hash   = meta.get("api_hash")   or config.API_HASH
            user_id    = meta.get("id") or meta.get("user_id") or 0
            first_name = meta.get("first_name") or "Account"
            dest = acm._session_path(phone) + ".session"
            try: convert_telethon_session(os.path.join(extract_dir, sfile), dest, api_id, user_id)
            except Exception as e:
                skipped += 1
                if len(skip_reasons) < 6: skip_reasons.append(f"{phone}: {e}")
                continue
            db.add_account(phone, first_name=first_name, user_id=user_id, api_id=api_id, api_hash=api_hash)
            imported_phones.append(phone)
        started = 0
        for i in range(0, len(imported_phones), SESSION_START_BATCH_SIZE):
            batch   = imported_phones[i:i + SESSION_START_BATCH_SIZE]
            results = await asyncio.gather(*(acm.start_session(p) for p in batch), return_exceptions=True)
            started += sum(1 for r in results if r is True)
            if i + SESSION_START_BATCH_SIZE < len(imported_phones):
                await asyncio.sleep(SESSION_START_DELAY)
        report = (f"{E_CHECK} <b>IMPORT COMPLETE</b>\n{E_GREEN} Imported: <b>{len(imported_phones)}</b>\n"
                  f"{E_ROCKET} Online now: <b>{started}</b>\n{E_RED} Skipped: <b>{skipped}</b>")
        if skip_reasons:
            report += "\n<b>Sample errors:</b>\n" + "\n".join(f"• <code>{r}</code>" for r in skip_reasons)
        online_new = [p for p in imported_phones if acm.is_online(p)]
        if online_new:
            report += f"\n{E_ROCKET} Auto-joining <b>{len(online_new)}</b> accounts (background)..."
            asyncio.create_task(join_accounts_to_all_clients(online_new))
        await status.edit_text(report, parse_mode=ParseMode.HTML)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return CH.END


# ── Join all ─────────────────────────────────────────────────
async def joinall_menu(update, ctx):
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    online = len(acm.get_online_accounts())
    await q.message.edit_text(
        f"{E_ROCKET} <b>JOIN ALL ACCOUNTS</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{E_PHONE} Online accounts: <b>{online}</b>\n\n"
        f"Join all accounts into all client channels.",
        reply_markup=InlineKeyboardMarkup([
            [btn("All Clients' Channels", "joinall_clients", "success")],
            [btn("Home", "menu_home", "primary")],
        ]), parse_mode=ParseMode.HTML)

async def joinall_clients(update, ctx):
    q = update.callback_query
    try: await q.answer("Starting...")
    except Exception: pass
    if not acm.get_online_accounts():
        await q.message.reply_text(f"{E_WARN} No online accounts.", parse_mode=ParseMode.HTML); return
    clients = await visible_clients(update.effective_user.id if update.effective_user else 0)
    if not clients:
        await q.message.reply_text(f"{E_WARN} No clients to join.", parse_mode=ParseMode.HTML); return
    online = acm.get_online_accounts()
    msg    = await q.message.reply_text(f"{E_CLOCK} Joining all accounts into {len(clients)} channels...", parse_mode=ParseMode.HTML)
    total  = 0
    for cid, c in clients:
        raw       = c_channel(c); stored_id = c_channel_id(c)
        joined    = list(c_joined(c))
        to_join   = [p for p in online if p not in joined]
        for i in range(0, len(to_join), SESSION_START_BATCH_SIZE):
            batch   = to_join[i:i + SESSION_START_BATCH_SIZE]
            results = await asyncio.gather(*(acm.join_channel(p, raw, stored_id) for p in batch), return_exceptions=True)
            for p, rid in zip(batch, results):
                if isinstance(rid, int) and rid:
                    joined.append(p); total += 1
                    if not stored_id: stored_id = rid
            if i + SESSION_START_BATCH_SIZE < len(to_join):
                await asyncio.sleep(SESSION_START_DELAY)
        await db.update_client(cid, joined_phones=joined, channel_id=stored_id)
    await msg.edit_text(f"{E_CHECK} Done! Total new joins: <b>{total}</b>", parse_mode=ParseMode.HTML)


# ── Client setup (Reactions bot: no views step) ──────────────
async def setup_start(update, ctx):
    from telegram.ext import ConversationHandler as CH
    if not is_admin(update.effective_user.id if update.effective_user else 0):
        await update.callback_query.answer("Access denied", show_alert=True); return CH.END
    await update.callback_query.answer()
    ctx.user_data["setup_admin"] = update.effective_user.id if update.effective_user else 0
    if not acm.get_online_accounts():
        await update.callback_query.message.reply_text(f"{E_WARN} No online accounts!", parse_mode=ParseMode.HTML)
        return CH.END
    await update.callback_query.message.reply_text(
        f"{E_ROCKET} <b>New Reaction Client Setup</b>\n\n<b>Step 1/5</b> - Client's Telegram User ID:\n\n/cancel to abort",
        parse_mode=ParseMode.HTML)
    return SETUP_UID

async def setup_uid(update, ctx):
    try: ctx.user_data["setup_uid"] = int(update.message.text.strip())
    except:
        await update.message.reply_text(f"{E_WARN} Valid numeric ID:"); return SETUP_UID
    await update.message.reply_text(f"{E_CHECK} <b>Step 2/5</b> - Channel Link:", parse_mode=ParseMode.HTML)
    return SETUP_CHAN

async def setup_chan(update, ctx):
    ctx.user_data["setup_chan"] = update.message.text.strip()
    await update.message.reply_text(
        f"{E_CHECK} <b>Step 3/5</b> - Accounts to join?\nOnline: <b>{len(acm.get_online_accounts())}</b>",
        parse_mode=ParseMode.HTML)
    return SETUP_ACCS

async def setup_accs(update, ctx):
    try:
        n = int(update.message.text.strip())
        if n <= 0: raise ValueError
        ctx.user_data["setup_accs"] = n
    except:
        await update.message.reply_text(f"{E_WARN} Positive number:"); return SETUP_ACCS
    await update.message.reply_text(f"{E_CHECK} <b>Step 4/5</b> - Reactions/post:", parse_mode=ParseMode.HTML)
    return SETUP_REACTS

async def setup_reacts(update, ctx):
    try: ctx.user_data["setup_reacts"] = int(update.message.text.strip())
    except:
        await update.message.reply_text(f"{E_WARN} Number:"); return SETUP_REACTS
    await update.message.reply_text(f"{E_CHECK} <b>Step 5/5</b> - Days:", parse_mode=ParseMode.HTML)
    return SETUP_DAYS

async def setup_days(update, ctx):
    try:
        days = int(update.message.text.strip())
        if days <= 0: raise ValueError
    except:
        await update.message.reply_text(f"{E_WARN} Positive days:"); return SETUP_DAYS
    ud  = ctx.user_data
    cid = await db.next_client_id(ud["setup_uid"])
    admin_id = str(ud.get("setup_admin") or "")
    await db.add_client(cid, ud["setup_uid"], ud["setup_chan"],
                        ud["setup_accs"], ud["setup_reacts"], days, admin_id=admin_id)
    msg    = await update.message.reply_text(f"{E_CLOCK} Auto-starting...", parse_mode=ParseMode.HTML)
    result = await do_join_and_start(cid)
    user_info    = await acm.get_user_info(ud["setup_uid"]) if ud.get("setup_uid") else None
    channel_info = await acm.get_channel_info(ud["setup_chan"]) if ud.get("setup_chan") else None
    user_display    = user_info["name"] if user_info else f"User {ud['setup_uid']}"
    channel_display = channel_info["title"] if channel_info else ud["setup_chan"]
    expiry_date     = (datetime.now() + timedelta(days=days)).strftime('%d %b %Y')
    if "error" in result:
        await msg.edit_text(
            f"{E_CHECK} <b>Client Created!</b>\nID: <code>{cid}</code>\n"
            f"{E_PERSON} Owner: <code>{user_display}</code>\n{E_CHANNEL} Channel: <code>{channel_display}</code>\n"
            f"{E_WARN} Start manually: {result['error']}", parse_mode=ParseMode.HTML)
    else:
        await msg.edit_text(
            f"{E_CROWN} <b>REACTION CLIENT CREATED!</b> {E_CROWN}\n\n"
            f"{E_TARGET} Client ID: <code>{cid}</code>\n"
            f"{E_PERSON} Customer: <code>{user_display}</code>\n"
            f"{E_CHANNEL} Channel: <code>{channel_display}</code>\n"
            f"{E_PHONE} Accounts: <b>{ud['setup_accs']}</b>\n"
            f"{E_FIRE} Reactions/Post: <b>{ud['setup_reacts']}</b>\n"
            f"{E_CAL} Expiry: <b>{expiry_date}</b> ({days} days)\n"
            f"{E_GREEN} Status: ACTIVE & RUNNING {E_ROCKET}\n"
            f"{E_CHECK} Joined: <b>{result['joined']}</b> accounts",
            parse_mode=ParseMode.HTML)
    from telegram.ext import ConversationHandler as CH
    return CH.END

async def setup_cancel(update, ctx):
    await update.message.reply_text(f"{E_CROSS} Cancelled.")
    from telegram.ext import ConversationHandler as CH; return CH.END


# ── Stats / Expiring / Admins ────────────────────────────────
async def stats_menu(update, ctx):
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    s = await db.stats()
    text = (
        f"{E_CHART} <b>Live Stats (Reactions Bot)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{E_PHONE} Accounts: <b>{s['total_accounts']}</b> ({E_GREEN}{s['online']} online)\n"
        f"{E_PERSON} Clients: <b>{s['total_clients']}</b> ({E_ROCKET}{s['active_clients']} active)\n\n"
        f"{E_FIRE} Reactions: <code>{s['total_reactions']:,}</code>\n\n"
        f"{E_CLOCK} <i>{datetime.now().strftime('%d %b %Y %H:%M:%S')}</i>"
    )
    await q.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[
        btn("Refresh", "menu_stats", "primary"), btn("Home", "menu_home", "primary")]]),
        parse_mode=ParseMode.HTML)

async def admin_expiring(update, ctx):
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    clients = await visible_clients(update.effective_user.id if update.effective_user else 0)
    expired, urgent, soon = [], [], []
    for cid, c in clients:
        dl = days_left(c)
        if dl is None: continue
        if dl <= 0: expired.append((cid, c, dl))
        elif dl <= 3: urgent.append((cid, c, dl))
        elif dl <= 7: soon.append((cid, c, dl))
    text = (f"{E_CLOCK} <b>EXPIRING SOON</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{E_RED} Expired: <b>{len(expired)}</b>   {E_CLOCK} ≤3d: <b>{len(urgent)}</b>   "
            f"{E_YELLOW} ≤7d: <b>{len(soon)}</b>\n━━━━━━━━━━━━━━━━━━━━━━\n")
    rows = []
    for bucket, icon, label in [(expired, E_RED, "EXPIRED"), (urgent, E_CLOCK, "WITHIN 3 DAYS"), (soon, E_YELLOW, "WITHIN 7 DAYS")]:
        if bucket:
            text += f"\n{icon} <b>{label}</b>\n"
            for cid, c, dl in bucket:
                channel = (c.get("channel_name") or c_channel(c))[:24]
                owner   = c_user_id(c)
                lbl     = "EXPIRED" if dl <= 0 else f"{_fmt_days(dl)} left"
                text   += f"{icon} <b>{cid}</b> · {channel}\n   {E_PERSON} {owner} — {lbl}\n"
                rows.append([btn(f"Manage {cid}", f"client_detail|{cid}", "primary")])
    if not (expired or urgent or soon): text += f"\n{E_GREEN} All subscriptions are healthy.\n"
    rows.append([btn("Refresh", "menu_expiring", "primary"), btn("Home", "menu_home", "primary")])
    await q.message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)

async def menu_admins(update, ctx):
    if not is_super_admin(update.effective_user.id if update.effective_user else 0):
        await update.callback_query.answer("Super-admin only", show_alert=True); return
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    admins = await db.get_admins()
    text   = (f"{E_SHIELD} <b>MANAGE ADMINS</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
              f"{E_CROWN} Super-admins: <b>{len(SUPER_ADMINS)}</b> (from config)\n"
              f"{E_PERSON} Sub-admins: <b>{len(admins)}</b>\n\n")
    rows = []
    for a in admins:
        aid  = str(a["admin_id"]); name = a.get("name") or ""
        text += f"{E_PERSON} <code>{aid}</code>{(' · ' + name) if name else ''}\n"
        rows.append([btn(f"Remove {name or aid}", f"admin_del|{aid}", "danger")])
    rows.append([btn("Add Admin", "admin_add", "success")])
    rows.append([btn("Home", "menu_home", "primary")])
    await q.message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)

async def admin_del(update, ctx):
    if not is_super_admin(update.effective_user.id if update.effective_user else 0):
        await update.callback_query.answer("Super-admin only", show_alert=True); return
    q = update.callback_query; aid = q.data.split("|", 1)[1]
    await db.remove_admin(aid)
    try: DB_ADMINS.discard(int(aid))
    except: pass
    try: await q.answer("Removed")
    except Exception: pass
    await menu_admins(update, ctx)

async def admin_add_start(update, ctx):
    from telegram.ext import ConversationHandler as CH
    if not is_super_admin(update.effective_user.id if update.effective_user else 0):
        await update.callback_query.answer("Super-admin only", show_alert=True); return CH.END
    q = update.callback_query
    try: await q.answer()
    except Exception: pass
    await q.message.reply_text(f"{E_SHIELD} <b>Add Admin</b>\n\nSend Telegram User ID:\n\n/cancel to abort", parse_mode=ParseMode.HTML)
    return ADMIN_ADD_ID

async def admin_add_id(update, ctx):
    try: new_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(f"{E_WARN} Send a valid numeric user id."); return ADMIN_ADD_ID
    ctx.user_data["new_admin_id"] = new_id
    await update.message.reply_text(f"{E_CHECK} ID: <code>{new_id}</code>\n\nSend a <b>name/label</b> (or <code>-</code> to skip):", parse_mode=ParseMode.HTML)
    return ADMIN_ADD_NAME

async def admin_add_name(update, ctx):
    name   = update.message.text.strip()
    if name == "-": name = ""
    new_id = ctx.user_data.get("new_admin_id")
    if not new_id:
        await update.message.reply_text(f"{E_CROSS} Session expired. Try again.")
        from telegram.ext import ConversationHandler as CH; return CH.END
    added_by = update.effective_user.id if update.effective_user else ""
    await db.add_admin(str(new_id), name, str(added_by))
    DB_ADMINS.add(int(new_id))
    await update.message.reply_text(f"{E_CHECK} <b>Admin Added!</b>\n{E_PERSON} <code>{new_id}</code>{(' · ' + name) if name else ''}", parse_mode=ParseMode.HTML)
    await notify(ctx.bot, new_id,
        f"{E_CROWN} <b>You are now an admin (Reaction Bot)!</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\nSend /start to open your panel.")
    from telegram.ext import ConversationHandler as CH; return CH.END


# ── Customer-facing ──────────────────────────────────────────
async def client_home(update, ctx):
    user = update.effective_user
    uid  = user.id if user else 0
    name = (user.first_name if user and user.first_name else "there")
    clients = await db.get_clients_by_owner(str(uid))
    if not clients:
        text = (f"{E_CROWN} <b>MY SUBSCRIPTIONS (Reactions)</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{E_PERSON} Hi <b>{name}</b>!\n\n{E_WARN} No subscription yet.\n"
                f"{E_HEART} Please contact the admin to get started.")
        kb = InlineKeyboardMarkup([[btn("Refresh", "cust_home", "primary")]])
    else:
        active = sum(1 for cid, c in clients if await db.is_subscribed(cid))
        text   = (f"{E_CROWN} <b>MY SUBSCRIPTIONS (Reactions)</b>\n{E_PERSON} <b>{name}</b>\n"
                  f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                  f"{E_DIAMOND} Total: <b>{len(clients)}</b>   {E_GREEN} Active: <b>{active}</b>   "
                  f"{E_RED} Expired: <b>{len(clients)-active}</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n")
        for cid, c in clients:
            dot     = E_GREEN if await db.is_subscribed(cid) else E_RED
            running = E_ROCKET if svm.is_running(cid) else E_RED
            channel = (c.get("channel_name") or c_channel(c))[:30]
            text   += (f"{dot} <b>{channel}</b>\n"
                       f"   {E_PHONE} {c_count(c)} acc  {E_FIRE} R:{c.get('reactions_per_post',0)}\n"
                       f"   {expiry_str(c)}\n"
                       f"   {E_CHART} Delivered: <b>{c.get('total_reactions',0):,}</b> reactions\n\n")
        kb = InlineKeyboardMarkup([[btn("Refresh", "cust_home", "primary")]])
    msg = update.message or update.callback_query.message
    if update.callback_query:
        try: await update.callback_query.answer()
        except Exception: pass
        try: await msg.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception: await msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def start_dispatch(update, ctx):
    uid = update.effective_user.id if update.effective_user else 0
    if is_admin(uid): await home_menu(update, ctx)
    else:             await client_home(update, ctx)


# ── Reminder loop ────────────────────────────────────────────
REMINDER_STAGES   = [7, 3, 1]
REMINDER_INTERVAL = 3600

async def reminder_loop(app):
    await asyncio.sleep(20)
    logger.info("Reminder loop started")
    while True:
        try:
            clients = await db.get_clients()
            for cid, c in list(clients.items()):
                if c.get("subscribed_until") is None: continue
                dl = days_left(c)
                if dl is None: continue
                stages = set(s for s in (c.get("notified_stages") or "").split(",") if s)
                owner  = c_user_id(c)
                channel = c.get("channel_name") or c_channel(c)
                if dl <= 0:
                    if "expired" not in stages:
                        await notify(app.bot, owner,
                            f"{E_RED} <b>SUBSCRIPTION EXPIRED</b>\n{E_CHANNEL} Channel: <code>{channel}</code>\n"
                            f"{E_CROSS} Your service has been paused. Contact admin to renew.")
                        svm.stop(cid); stages.add("expired")
                        await db.update_client(cid, notified_stages=",".join(sorted(stages)), status="expired")
                else:
                    applicable = [s for s in REMINDER_STAGES if dl <= s]
                    unsent     = [s for s in applicable if str(s) not in stages]
                    if unsent:
                        exp = parse_expiry(c.get("subscribed_until")).strftime('%d %b %Y')
                        await notify(app.bot, owner,
                            f"{E_WARN} <b>SUBSCRIPTION EXPIRING SOON</b>\n"
                            f"{E_CHANNEL} Channel: <code>{channel}</code>\n"
                            f"{E_CLOCK} Time left: <b>{_fmt_days(dl)}</b>\n"
                            f"{E_CAL} Expires on: <b>{exp}</b>\n\n"
                            f"{E_BELL} Renew now to keep your reactions running.")
                        for s in applicable: stages.add(str(s))
                        await db.update_client(cid, notified_stages=",".join(sorted(stages)))
        except asyncio.CancelledError: break
        except Exception as e: logger.error(f"reminder_loop: {e}")
        await asyncio.sleep(REMINDER_INTERVAL)


# ── Auto-start ───────────────────────────────────────────────
async def auto_start_services():
    logger.info("=== Auto-starting services (Reaction Bot) ===")
    accounts   = db.get_accounts()
    all_phones = list(accounts.keys())
    for i in range(0, len(all_phones), SESSION_START_BATCH_SIZE):
        batch = all_phones[i:i + SESSION_START_BATCH_SIZE]
        await asyncio.gather(*[acm.start_session(p) for p in batch], return_exceptions=True)
        if i + SESSION_START_BATCH_SIZE < len(all_phones):
            await asyncio.sleep(SESSION_START_DELAY)
    online  = acm.get_online_accounts()
    clients = await db.get_clients()
    logger.info(f"Accounts online: {len(online)}/{len(accounts)}")
    running_clients = [cid for cid, c in clients.items() if await db.is_subscribed(cid) and c.get("status") == "running"]
    BATCH_SIZE = 20
    for i in range(0, len(running_clients), BATCH_SIZE):
        batch   = running_clients[i:i + BATCH_SIZE]
        results = await asyncio.gather(*[do_join_and_start(cid) for cid in batch], return_exceptions=True)
        for cid, result in zip(batch, results):
            if isinstance(result, dict):
                if "error" in result: logger.warning(f"Auto-start {cid}: {result['error']}")
                else: logger.info(f"Auto-started {cid}: {result['joined']} accounts")
        if i + BATCH_SIZE < len(running_clients): await asyncio.sleep(2)
    logger.info("=== Auto-start complete (Reaction Bot) ===")

async def on_startup(app: Application):
    global acm
    logger.info("=== Reaction Bot Starting ===")
    acm = AccountManager()
    await init_database()
    await load_db_admins()
    await auto_start_services()
    app.bot_data["reminder_task"] = asyncio.create_task(reminder_loop(app))
    logger.info("=== Reaction Bot Ready ===")

async def on_shutdown(app: Application):
    task = app.bot_data.get("reminder_task")
    if task: task.cancel()
    svm.shutdown()
    if acm: await acm.stop_all()
    if db:  await db.disconnect()
    logger.info("Reaction Bot stopped.")


# ── Router ───────────────────────────────────────────────────
async def router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data if update.callback_query else ""
    uid  = update.effective_user.id if update.effective_user else 0
    if data == "cust_home":
        await client_home(update, ctx); return
    if not is_admin(uid):
        try: await update.callback_query.answer("Access denied", show_alert=True)
        except Exception: pass
        return
    if data == "noop":
        await update.callback_query.answer("Use Prev/Next to navigate"); return
    if   data == "menu_home":      await home_menu(update, ctx)
    elif data == "menu_accounts":  await accounts_menu(update, ctx)
    elif data == "menu_clients":   await clients_menu(update, ctx)
    elif data == "menu_stats":     await stats_menu(update, ctx)
    elif data == "menu_expiring":  await admin_expiring(update, ctx)
    elif data == "menu_admins":    await menu_admins(update, ctx)
    elif data.startswith("admin_del|"):   await admin_del(update, ctx)
    elif data == "joinall_menu":          await joinall_menu(update, ctx)
    elif data == "joinall_clients":       await joinall_clients(update, ctx)
    elif data in ("acc_start_all", "acc_stop_all"): await acc_bulk_action(update, ctx)
    elif data == "acc_remove_dead":       await acc_remove_dead(update, ctx)
    elif data.startswith("acc_page_"):    await acc_page_handler(update, ctx)
    elif data.startswith("cli_page_"):    await clients_page_handler(update, ctx)
    elif data.startswith("client_detail|"): await client_detail(update, ctx)
    elif data in ("svc_start_all", "svc_stop_all"): await client_action(update, ctx)
    elif data.startswith(("svc_start|", "svc_stop|", "extend|", "remove_client|")):
        await client_action(update, ctx)
    elif data.startswith(("acc_start|", "acc_stop|", "acc_del|", "acc_info|")):
        await acc_action(update, ctx)
    elif data.startswith("user_subs|"):
        user_id = data.split("|", 1)[1]
        clients = await db.get_clients_by_owner(user_id)
        if not is_super_admin(uid):
            clients = [(cid, c) for cid, c in clients if str(c.get("admin_id") or "") == str(uid)]
        text = f"{E_PERSON} <b>User Subscriptions</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        rows = []
        for cid, c in clients:
            status = "running" if svm.is_running(cid) else c.get("status", "stopped")
            name   = (c.get("channel_name") or c_channel(c))[:20]
            text  += f"{status_dot(status)} <b>{cid}</b>\n  {E_CHANNEL} {name}\n  {expiry_str(c)}\n\n"
            rows.append([btn(f"Manage {cid}", f"client_detail|{cid}", "primary")])
        rows.append([btn("Back", "menu_clients", "primary")])
        await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
    else:
        try: await update.callback_query.answer("Unknown")
        except Exception: pass


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start_dispatch))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(acc_add_start, pattern="^acc_add$")],
        states={
            ADD_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, acc_recv_phone)],
            ADD_CONFIRM_RELOGIN: [CallbackQueryHandler(acc_confirm_relogin, pattern="^(relogin_yes|relogin_no)$")],
            ADD_OTP:  [MessageHandler(filters.TEXT & ~filters.COMMAND, acc_recv_otp)],
            ADD_2FA:  [MessageHandler(filters.TEXT & ~filters.COMMAND, acc_recv_2fa)],
            ADD_MORE_ACCOUNT: [CallbackQueryHandler(add_more_handler, pattern="^(add_more|menu_home)$")],
        },
        fallbacks=[CommandHandler("cancel", setup_cancel)],
        allow_reentry=True, per_message=False,
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(setup_start, pattern="^menu_setup$")],
        states={
            SETUP_UID:   [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_uid)],
            SETUP_CHAN:  [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_chan)],
            SETUP_ACCS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_accs)],
            SETUP_REACTS:[MessageHandler(filters.TEXT & ~filters.COMMAND, setup_reacts)],
            SETUP_DAYS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_days)],
        },
        fallbacks=[CommandHandler("cancel", setup_cancel)],
        allow_reentry=True, per_message=False,
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(acc_import_start, pattern="^acc_import$")],
        states={IMPORT_ZIP: [
            MessageHandler(filters.Document.ALL, acc_import_zip),
            MessageHandler(filters.TEXT & ~filters.COMMAND, acc_import_zip),
        ]},
        fallbacks=[CommandHandler("cancel", setup_cancel)],
        allow_reentry=True, per_message=False,
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_start, pattern="^admin_add$")],
        states={
            ADMIN_ADD_ID:   [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_id)],
            ADMIN_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_name)],
        },
        fallbacks=[CommandHandler("cancel", setup_cancel)],
        allow_reentry=True, per_message=False,
    ))

    app.add_handler(CallbackQueryHandler(router))
    logger.info("Reaction Bot running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()



