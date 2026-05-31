import asyncio
import json
import os
import random
import warnings
import logging
from datetime import datetime, timedelta
from typing import Optional
from math import ceil

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
    ChatWriteForbidden, ChannelPrivate, InviteHashInvalid,
    InviteHashExpired, UsernameInvalid, UsernameNotOccupied,
    PeerIdInvalid
)
from pyrogram.raw import functions as raw_functions
from pyrogram.raw import types as raw_types

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("pyrogram.session").setLevel(logging.WARNING)
logging.getLogger("pyrogram.connection").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────
ACCOUNTS_PER_PAGE = 10  # Show 10 accounts per page
CLIENTS_PER_PAGE = 5    # Show 5 clients per page
ACTION_DELAY_MIN = 1.0  # Per-account delay before view/reaction actions
ACTION_DELAY_MAX = 2.0
RECENT_POST_LIMIT = 50  # Max new posts to process per channel check

# ─────────────────────────────────────────────
#  PATHS
# ─────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
DATA_FILE    = os.path.join(BASE_DIR, "data", "data.json")
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)

# ─────────────────────────────────────────────
#  EMOJIS
# ─────────────────────────────────────────────
def ce(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

E_CROWN = ce("5039727497143387500", "👑")
E_STAR = ce("5042176294222037888", "⭐")
E_FIRE = ce("5389038097860144794", "🔥")
E_CHECK = ce("5039844895779455925", "✅")
E_CROSS = ce("5040042498634810056", "❌")
E_PHONE = ce("5407025283456835913", "📱")
E_CHANNEL = ce("5041888071851705019", "📣")
E_CAL = ce("5413879192267805083", "🗓")
E_CLOCK = ce("6285240160120477644", "⏰")
E_WARN = ce("5039665997506675838", "⚠️")
E_GREEN = ce("5039928501612839813", "🟢")
E_RED = ce("5042042652019655612", "🔴")
E_YELLOW = ce("5339082633160703625", "🟡")
E_LOCK = ce("5305609152704297298", "🔒")
E_ROCKET = ce("5389057356493511934", "🚀")
E_BELL = ce("5042111805288089118", "🔔")
E_GIFT = ce("5039778134807806727", "🎁")
E_CHART = ce("5042290883949495533", "📊")
E_PERSON = ce("6165860934242798778", "👤")
E_REFRESH = ce("5041837837914211014", "🔄")
E_TRASH = ce("5039614900280754969", "🗑")
E_SHIELD = ce("5042328396193864923", "🛡")
E_PLUS = ce("5039844895779455925", "➕")
E_LEFT = ce("5041837837914211014", "⬅️")
E_RIGHT = ce("5041837837914211014", "➡️")
E_PAGE = ce("5042290883949495533", "📄")

REACTION_EMOJIS = ["❤", "👍", "🔥", "🎉", "😍", "👏", "🥰", "💯", "👀", "💪", "🏆", "🎯"]

# ─────────────────────────────────────────────
#  CONVERSATION STATES
# ─────────────────────────────────────────────
(
    ADD_PHONE, ADD_OTP, ADD_2FA,
    SETUP_UID, SETUP_CHAN, SETUP_ACCS, SETUP_REACTS, SETUP_VIEWS, SETUP_DAYS,
    ADD_MORE_ACCOUNT
) = range(10)

# ─────────────────────────────────────────────
#  CHANNEL IDENTIFIER NORMALIZER
# ─────────────────────────────────────────────
def normalize_channel(raw: str) -> str:
    raw = raw.strip()
    if raw.lstrip("-").isdigit(): return raw
    if "+joinchat/" in raw or "/joinchat/" in raw: return raw
    if raw.startswith("https://t.me/+") or raw.startswith("http://t.me/+"): return raw
    if raw.startswith("t.me/+"): return "https://" + raw
    for prefix in ["https://t.me/", "http://t.me/", "t.me/"]:
        if raw.startswith(prefix):
            username = raw[len(prefix):].split("/")[0].split("?")[0]
            return "@" + username if not username.startswith("@") else username
    if raw.startswith("@"): return raw
    return "@" + raw

# ─────────────────────────────────────────────
#  JSON HELPERS
# ─────────────────────────────────────────────
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
    if isinstance(val, (int, float)): return datetime.fromtimestamp(float(val))
    try: return datetime.fromisoformat(str(val))
    except: return datetime.now()

def expiry_str(c: dict) -> str:
    val = c.get("subscribed_until")
    if val is None: return f"{E_WARN} No expiry set"
    try:
        dt = parse_expiry(val)
        diff = dt - datetime.now()
        if diff.total_seconds() < 0: return f"{E_RED} <b>EXPIRED</b>"
        days = diff.days
        if days == 0: return f"{E_WARN} Expires today ({int(diff.total_seconds()/3600)}h left)"
        return f"{E_CAL} {days}d left ({dt.strftime('%d %b %Y')})"
    except: return f"{E_WARN} Unknown expiry"

def status_dot(status: str) -> str:
    return {"online": E_GREEN, "running": E_ROCKET, "stopped": E_RED, "expired": E_YELLOW}.get(status, E_RED)

def make_bar(current: int, total: int, width: int = 12) -> str:
    if total <= 0: return "░" * width
    filled = min(int((current / total) * width), width)
    return "█" * filled + "░" * (width - filled)

# ─────────────────────────────────────────────
#  DATA MANAGER
# ─────────────────────────────────────────────
class DataManager:
    def __init__(self, path: str):
        self.path = path
        self._data = {"accounts": {}, "clients": {}, "stats": {"total_views": 0, "total_reactions": 0}}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            with open(self.path, "r") as f: self._data = json.load(f)
            self._data.setdefault("accounts", {})
            self._data.setdefault("clients", {})
            self._data.setdefault("stats", {"total_views": 0, "total_reactions": 0})
        else: self.save()

    def save(self):
        with open(self.path, "w") as f: json.dump(self._data, f, indent=2, default=str)

    def get_accounts(self) -> dict: return self._data.get("accounts", {})
    def get_accounts_sorted(self) -> list:
        """Return accounts sorted by phone number"""
        return sorted(self._data.get("accounts", {}).items(), key=lambda x: x[0])
    
    def add_account(self, phone: str, first_name: str = "", user_id: int = 0):
        self._data["accounts"][phone] = {
            "phone": phone, "first_name": first_name, "username": None,
            "user_id": user_id, "logged_in_at": datetime.now().timestamp()
        }
        self.save()

    def delete_account(self, phone: str):
        self._data["accounts"].pop(phone, None)
        self.save()

    def get_clients(self) -> dict: return self._data.get("clients", {})
    def get_clients_sorted(self) -> list:
        """Return clients sorted by client_id"""
        return sorted(self._data.get("clients", {}).items(), key=lambda x: x[0])

    def next_client_id(self, user_id: int) -> str:
        uid = str(user_id)
        existing = [k for k in self._data["clients"] if k.startswith(f"{uid}_")]
        return f"{uid}_{len(existing) + 1}"

    def add_client(self, client_id: str, user_id: int, channel: str,
                   accounts_count: int, reactions: int, views: int, days: int):
        expiry = (datetime.now() + timedelta(days=days)).timestamp()
        self._data["clients"][client_id] = {
            "owner_id": str(user_id), "client_id": client_id, "channel_link": channel,
            "channel_id": None, "channel_name": channel, "join_count": accounts_count,
            "reactions_per_post": reactions, "views_per_post": views, "subscribed_until": expiry,
            "status": "running", "joined_phones": [], "total_views": 0, "total_reactions": 0,
            "last_post_id": 0, "added_on": datetime.now().timestamp()
        }
        self.save()

    def get_client(self, client_id: str) -> Optional[dict]: return self._data["clients"].get(client_id)

    def update_client(self, client_id: str, **kwargs):
        if client_id in self._data["clients"]:
            self._data["clients"][client_id].update(kwargs)
            self.save()

    def delete_client(self, client_id: str):
        self._data["clients"].pop(client_id, None)
        self.save()

    def extend_client(self, client_id: str, days: int):
        c = self.get_client(client_id)
        if not c: return
        current = parse_expiry(c.get("subscribed_until", datetime.now().timestamp()))
        new_exp = max(current, datetime.now()) + timedelta(days=days)
        self.update_client(client_id, subscribed_until=new_exp.timestamp())

    def is_subscribed(self, client_id: str) -> bool:
        c = self.get_client(client_id)
        if not c: return False
        try: return parse_expiry(c.get("subscribed_until", 0)) > datetime.now()
        except: return False

    def add_global_stats(self, views: int, reactions: int):
        s = self._data.setdefault("stats", {"total_views": 0, "total_reactions": 0})
        s["total_views"] = s.get("total_views", 0) + views
        s["total_reactions"] = s.get("total_reactions", 0) + reactions
        self.save()

    def stats(self) -> dict:
        accounts = self.get_accounts()
        clients = self.get_clients()
        online = sum(1 for p in accounts if acm.is_online(p))
        active = sum(1 for cid in clients if svm.is_running(cid))
        gstats = self._data.get("stats", {})
        cv = sum(c.get("total_views", 0) for c in clients.values())
        cr = sum(c.get("total_reactions", 0) for c in clients.values())
        return {
            "total_accounts": len(accounts), "online": online,
            "offline": len(accounts) - online, "total_clients": len(clients),
            "active_clients": active,
            "total_views": max(gstats.get("total_views", 0), cv),
            "total_reactions": max(gstats.get("total_reactions", 0), cr)
        }

db = DataManager(DATA_FILE)

# ─────────────────────────────────────────────
#  ACCOUNT MANAGER - NON-BLOCKING
# ─────────────────────────────────────────────
class AccountManager:
    def __init__(self):
        self._clients: dict[str, PyroClient] = {}
        self._pending: dict[str, dict] = {}
        self._channel_id_cache: dict[str, int] = {}
        self._account_semaphores: dict[str, asyncio.Semaphore] = {}
        self._chat_cache: dict[str, dict] = {}

    def _session_path(self, phone: str) -> str:
        return os.path.join(SESSIONS_DIR, phone.replace("+", "").replace(" ", ""))

    def _find_session_name(self, phone: str) -> Optional[str]:
        candidates = [self._session_path(phone), os.path.join(SESSIONS_DIR, phone),
                     os.path.join(SESSIONS_DIR, phone.replace(" ", ""))]
        for name in candidates:
            if os.path.exists(name + ".session"): return name
        return None

    def _make_client(self, phone: str) -> PyroClient:
        return PyroClient(self._session_path(phone), api_id=config.API_ID,
                         api_hash=config.API_HASH, no_updates=True)

    def get_account_semaphore(self, phone: str) -> asyncio.Semaphore:
        if phone not in self._account_semaphores:
            self._account_semaphores[phone] = asyncio.Semaphore(1)
        return self._account_semaphores[phone]

    async def start_session(self, phone: str) -> bool:
        if phone in self._clients: return True
        session_name = self._find_session_name(phone)
        if not session_name:
            logger.warning(f"start_session {phone}: no .session file")
            return False
        try:
            cl = PyroClient(session_name, api_id=config.API_ID,
                          api_hash=config.API_HASH, no_updates=True)
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
            try: await cl.stop()
            except: pass

    async def stop_all(self):
        for phone in list(self._clients): await self.stop_session(phone)

    def is_online(self, phone: str) -> bool: return phone in self._clients
    def get_online_accounts(self) -> list: return list(self._clients.keys())

    async def get_chat_cached(self, phone: str, channel_identifier: str):
        if phone not in self._chat_cache: self._chat_cache[phone] = {}
        cache_key = channel_identifier
        if cache_key not in self._chat_cache[phone]:
            cl = self._clients.get(phone)
            if cl:
                self._chat_cache[phone][cache_key] = await cl.get_chat(channel_identifier)
        return self._chat_cache[phone].get(cache_key)

    async def resolve_channel_id(self, phone: str, raw_channel: str) -> Optional[int]:
        raw_channel = raw_channel.strip()
        if raw_channel.lstrip("-").isdigit(): return int(raw_channel)
        if raw_channel in self._channel_id_cache: return self._channel_id_cache[raw_channel]
        chat = await self.get_chat_cached(phone, normalize_channel(raw_channel))
        if chat:
            self._channel_id_cache[raw_channel] = chat.id
            return chat.id
        return None

    async def join_channel(self, phone: str, raw_channel: str,
                          stored_id: Optional[int] = None) -> Optional[int]:
        cl = self._clients.get(phone)
        if not cl: return None
        identifier = normalize_channel(raw_channel)
        try:
            chat = await cl.join_chat(identifier)
            self._channel_id_cache[raw_channel] = chat.id
            if phone in self._chat_cache: self._chat_cache[phone][raw_channel] = chat
            await asyncio.sleep(random.uniform(0.2, 0.5))
            return chat.id
        except UserAlreadyParticipant:
            cid = stored_id or await self.resolve_channel_id(phone, raw_channel)
            if cid: self._channel_id_cache[raw_channel] = cid
            return cid
        except (InviteHashInvalid, InviteHashExpired): return None
        except FloodWait as e:
            await asyncio.sleep(e.value)
            return None
        except: return None

    async def leave_channel(self, phone: str, channel_id: int) -> bool:
        cl = self._clients.get(phone)
        if not cl: return False
        try:
            await cl.leave_chat(channel_id)
            return True
        except: return False

    async def get_latest_message_id(self, phone: str, channel_identifier: str) -> Optional[int]:
        try:
            chat = await self.get_chat_cached(phone, channel_identifier)
            if not chat: return None
            cl = self._clients.get(phone)
            if not cl: return None
            async for msg in cl.get_chat_history(chat.id, limit=1):
                return msg.id
        except: return None

    async def get_message_ids_after(self, phone: str, channel_identifier: str,
                                    after_id: int, limit: int = RECENT_POST_LIMIT) -> list[int]:
        try:
            chat = await self.get_chat_cached(phone, channel_identifier)
            if not chat: return []
            cl = self._clients.get(phone)
            if not cl: return []

            ids = []
            async for msg in cl.get_chat_history(chat.id, limit=limit):
                if after_id and msg.id <= after_id:
                    break
                ids.append(msg.id)

            if not ids: return []
            if not after_id:
                return [max(ids)]
            return sorted(ids)
        except: return []

    async def send_view(self, phone: str, channel_identifier: str, message_id: int) -> bool:
        async with self.get_account_semaphore(phone):
            try:
                chat = await self.get_chat_cached(phone, channel_identifier)
                if not chat: return False
                cl = self._clients.get(phone)
                if not cl: return False
                await asyncio.sleep(random.uniform(ACTION_DELAY_MIN, ACTION_DELAY_MAX))
                await cl.invoke(raw_functions.messages.GetMessagesViews(
                    peer=await cl.resolve_peer(chat.id), id=[message_id], increment=True
                ))
                return True
            except: return False

    async def send_reaction(self, phone: str, channel_identifier: str,
                           message_id: int, emoji: str = "❤") -> bool:
        async with self.get_account_semaphore(phone):
            try:
                chat = await self.get_chat_cached(phone, channel_identifier)
                if not chat: return False
                cl = self._clients.get(phone)
                if not cl: return False
                await asyncio.sleep(random.uniform(ACTION_DELAY_MIN, ACTION_DELAY_MAX))
                await cl.invoke(raw_functions.messages.SendReaction(
                    peer=await cl.resolve_peer(chat.id), msg_id=message_id,
                    add_to_recent=False, reaction=[raw_types.ReactionEmoji(emoticon=emoji)]
                ))
                return True
            except ReactionInvalid:
                try:
                    chat = await self.get_chat_cached(phone, channel_identifier)
                    if chat and self._clients.get(phone):
                        await self._clients[phone].invoke(raw_functions.messages.SendReaction(
                            peer=await self._clients[phone].resolve_peer(chat.id),
                            msg_id=message_id, add_to_recent=False,
                            reaction=[raw_types.ReactionEmoji(emoticon="❤")]
                        ))
                        return True
                except: pass
                return False
            except: return False

    async def send_otp(self, phone: str) -> str:
        cl = self._make_client(phone)
        await cl.connect()
        sent = await cl.send_code(phone)
        self._pending[phone] = {"client": cl, "phone_code_hash": sent.phone_code_hash}
        return sent.phone_code_hash

    async def verify_otp(self, phone: str, code: str) -> str:
        p = self._pending.get(phone)
        if not p: return "error"
        try:
            user = await p["client"].sign_in(phone, p["phone_code_hash"], code)
            self._clients[phone] = p["client"]
            self._account_semaphores[phone] = asyncio.Semaphore(1)
            self._chat_cache[phone] = {}
            self._pending.pop(phone, None)
            db.add_account(phone, first_name=getattr(user, "first_name", ""),
                          user_id=getattr(user, "id", 0))
            return "ok"
        except SessionPasswordNeeded: return "2fa"
        except (PhoneCodeInvalid, PhoneCodeExpired): return "invalid"
        except: return "error"

    async def verify_2fa(self, phone: str, password: str) -> bool:
        p = self._pending.get(phone)
        if not p: return False
        try:
            user = await p["client"].check_password(password)
            self._clients[phone] = p["client"]
            self._account_semaphores[phone] = asyncio.Semaphore(1)
            self._chat_cache[phone] = {}
            self._pending.pop(phone, None)
            db.add_account(phone, first_name=getattr(user, "first_name", ""),
                          user_id=getattr(user, "id", 0))
            return True
        except: return False

    async def cancel_pending(self, phone: str):
        p = self._pending.pop(phone, None)
        if p:
            try: await p["client"].disconnect()
            except: pass

    async def get_account_info(self, phone: str) -> Optional[dict]:
        cl = self._clients.get(phone)
        if not cl: return None
        try:
            me = await cl.get_me()
            return {"name": f"{me.first_name or ''} {me.last_name or ''}".strip(),
                    "username": me.username or "N/A", "id": me.id}
        except: return None

acm = AccountManager()

# ─────────────────────────────────────────────
#  SERVICE MANAGER - NON-BLOCKING PARALLEL
# ─────────────────────────────────────────────
class ServiceManager:
    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}
        self._main_loop_task: Optional[asyncio.Task] = None
        self._running = False

    def is_running(self, client_id: str) -> bool:
        t = self._tasks.get(client_id)
        return t is not None and not t.done()

    def start(self, client_id: str):
        if self.is_running(client_id): return
        db.update_client(client_id, status="running")
        self._tasks[client_id] = asyncio.create_task(self._run(client_id))
        if not self._running:
            self._running = True
            self._main_loop_task = asyncio.create_task(self._main_orchestrator())

    def stop(self, client_id: str):
        t = self._tasks.pop(client_id, None)
        if t and not t.done(): t.cancel()
        db.update_client(client_id, status="stopped")
        if not self._tasks:
            self._running = False
            if self._main_loop_task: self._main_loop_task.cancel()

    def stop_all(self):
        for cid in list(self._tasks): self.stop(cid)
        self._running = False
        if self._main_loop_task: self._main_loop_task.cancel()

    def shutdown(self):
        for cid in list(self._tasks):
            t = self._tasks.pop(cid, None)
            if t and not t.done(): t.cancel()
        self._running = False
        if self._main_loop_task: self._main_loop_task.cancel()

    async def _main_orchestrator(self):
        while self._running:
            try: await self._orchestrate_non_blocking()
            except asyncio.CancelledError: break
            except Exception as e: logger.error(f"Orchestrator: {e}")
            await asyncio.sleep(5)

    async def _orchestrate_non_blocking(self):
        running_clients = [cid for cid in self._tasks if self.is_running(cid)]
        if not running_clients: return
        
        all_accounts = acm.get_online_accounts()
        if not all_accounts: return

        from collections import deque
        account_queue = deque(all_accounts)
        random.shuffle(account_queue)

        tasks = []
        for cid in running_clients:
            tasks.append(self._process_channel_immediate(cid, account_queue))
        
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_channel_immediate(self, cid: str, account_queue):
        try:
            c = db.get_client(cid)
            if not c or not db.is_subscribed(cid): return
            
            raw_channel = c_channel(c)
            max_views = c.get("views_per_post", 0)
            max_reacts = c.get("reactions_per_post", 0)
            last_id = c.get("last_post_id", 0)

            if not account_queue: return

            probe = account_queue[0]
            message_ids = await acm.get_message_ids_after(probe, raw_channel, last_id)
            if not message_ids: return

            total_views = 0
            total_reacts = 0
            needed = max(max_views, max_reacts)

            for msg_id in message_ids:
                if not account_queue: break

                workers = []
                for _ in range(min(needed, len(account_queue))):
                    workers.append(account_queue.popleft())

                all_actions = []
                for i, phone in enumerate(workers):
                    if i < max_views:
                        all_actions.append(("view", phone, raw_channel, msg_id, None))
                    if i < max_reacts:
                        all_actions.append(("react", phone, raw_channel, msg_id,
                                          REACTION_EMOJIS[i % len(REACTION_EMOJIS)]))

                action_tasks = []
                for action_type, phone, ch, action_msg_id, emoji in all_actions:
                    if action_type == "view":
                        action_tasks.append(acm.send_view(phone, ch, action_msg_id))
                    else:
                        action_tasks.append(acm.send_reaction(phone, ch, action_msg_id, emoji))

                results = await asyncio.gather(*action_tasks, return_exceptions=True)
                account_queue.extend(workers)

                actual_views = sum(1 for i, (action_type, _, _, _, _) in enumerate(all_actions)
                                   if action_type == "view" and results[i] is True)
                actual_reacts = sum(1 for i, (action_type, _, _, _, _) in enumerate(all_actions)
                                    if action_type == "react" and results[i] is True)
                total_views += actual_views
                total_reacts += actual_reacts
                logger.info(f"[{cid}] +{actual_views}V +{actual_reacts}R | msg:{msg_id} | acc:{len(workers)}")

            latest_processed = max(message_ids)
            db.update_client(cid, last_post_id=latest_processed)

            new_v = c.get("total_views", 0) + total_views
            new_r = c.get("total_reactions", 0) + total_reacts
            db.update_client(cid, total_views=new_v, total_reactions=new_r)
            db.add_global_stats(total_views, total_reacts)
            if len(message_ids) > 1:
                logger.info(f"[{cid}] processed {len(message_ids)} posts | {message_ids[0]}-{latest_processed}")

        except Exception as e:
            logger.error(f"Channel {cid} error: {e}")

    async def _run(self, client_id: str):
        while True:
            try: await asyncio.sleep(30)
            except asyncio.CancelledError: break

svm = ServiceManager()

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def is_admin(uid: int) -> bool: return uid in config.ADMIN_IDS

def admin_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        if not is_admin(uid):
            await update.effective_message.reply_text(f"{E_CROSS} <b>Access Denied.</b>", parse_mode=ParseMode.HTML)
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {ctx.error}", exc_info=ctx.error)

# ─────────────────────────────────────────────
#  PAGINATION UTILS
# ─────────────────────────────────────────────
def get_page_items(items: list, page: int, per_page: int) -> tuple:
    """Get items for a specific page. Returns (page_items, total_pages)"""
    total_pages = max(1, ceil(len(items) / per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = start + per_page
    return items[start:end], total_pages

def get_pagination_keyboard(current_page: int, total_pages: int, prefix: str) -> list:
    """Generate pagination navigation buttons"""
    rows = []
    nav = []
    
    if current_page > 1:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}_page_{current_page-1}"))
    
    nav.append(InlineKeyboardButton(f"📄 {current_page}/{total_pages}", callback_data="noop"))
    
    if current_page < total_pages:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"{prefix}_page_{current_page+1}"))
    
    if nav:
        rows.append(nav)
    
    return rows

# ─────────────────────────────────────────────
#  MENUS WITH PAGINATION
# ─────────────────────────────────────────────
async def home_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = db.stats()
    online_pct = (s['online'] / s['total_accounts'] * 100) if s['total_accounts'] > 0 else 0
    active_pct = (s['active_clients'] / s['total_clients'] * 100) if s['total_clients'] > 0 else 0
    acc_bar = make_bar(s['online'], s['total_accounts'])
    cli_bar = make_bar(s['active_clients'], s['total_clients'])

    text = (
        f"{E_CROWN} <b>TELEGRAM BOOST PANEL</b> {E_CROWN}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{E_PHONE} <b>Accounts</b>\n"
        f"  <code>{acc_bar}</code>  {s['online']}/{s['total_accounts']}  ({online_pct:.0f}%)\n\n"
        f"{E_PERSON} <b>Active Clients</b>\n"
        f"  <code>{cli_bar}</code>  {s['active_clients']}/{s['total_clients']}  ({active_pct:.0f}%)\n\n"
        f"{E_ROCKET} <b>Views</b>: <code>{s['total_views']:,}</code>\n"
        f"{E_FIRE}  <b>Reactions</b>: <code>{s['total_reactions']:,}</code>\n\n"
        f"{E_CLOCK} <i>{datetime.now().strftime('%d %b %Y  %H:%M:%S')}</i>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Accounts", callback_data="menu_accounts"),
         InlineKeyboardButton("👥 Clients", callback_data="menu_clients")],
        [InlineKeyboardButton("➕ New Client Setup", callback_data="menu_setup")],
        [InlineKeyboardButton("📊 Live Stats", callback_data="menu_stats"),
         InlineKeyboardButton("🔄 Refresh", callback_data="menu_home")],
    ])
    msg = update.message or update.callback_query.message
    if update.callback_query:
        try: await msg.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except: await msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else: await msg.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

async def show_accounts_page(message, page: int = 1):
    """Show accounts with pagination"""
    accounts = db.get_accounts_sorted()
    
    if not accounts:
        await message.edit_text(
            f"{E_PHONE} <b>Accounts</b>\n\nNo accounts added yet.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add Account", callback_data="acc_add")],
                [InlineKeyboardButton("🏠 Home", callback_data="menu_home")],
            ]),
            parse_mode=ParseMode.HTML,
        )
        return
        
    online_count = sum(1 for phone, _ in accounts if acm.is_online(phone))
    page_items, total_pages = get_page_items(accounts, page, ACCOUNTS_PER_PAGE)
    
    text = (
        f"{E_PHONE} <b>Accounts</b>\n"
        f"Total: <b>{len(accounts)}</b> | "
        f"{E_GREEN} {online_count} Online | "
        f"{E_RED} {len(accounts)-online_count} Offline\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    
    rows = []
    for phone, info in page_items:
        dot = E_GREEN if acm.is_online(phone) else E_RED
        name = (info.get("first_name", "") or "Account")[:15]
        text += f"{dot} <code>{phone}</code> <b>{name}</b>\n"
        rows.append([
            InlineKeyboardButton("▶", callback_data=f"acc_start|{phone}"),
            InlineKeyboardButton("⏹", callback_data=f"acc_stop|{phone}"),
            InlineKeyboardButton("ℹ️", callback_data=f"acc_info|{phone}"),
            InlineKeyboardButton("🗑", callback_data=f"acc_del|{phone}"),
        ])
    
    # Pagination navigation
    rows += get_pagination_keyboard(page, total_pages, "acc")
    
    # Action buttons
    rows += [
        [InlineKeyboardButton("➕ Add Account", callback_data="acc_add")],
        [InlineKeyboardButton("🚀 Start All", callback_data="acc_start_all"),
         InlineKeyboardButton("⏹ Stop All", callback_data="acc_stop_all")],
        [InlineKeyboardButton("🏠 Home", callback_data="menu_home")],
    ]
    
    await message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)

async def show_clients_page(message, page: int = 1):
    """Show clients with pagination"""
    clients = db.get_clients_sorted()
    
    if not clients:
        await message.edit_text(
            f"👥 <b>Clients</b>\n\nNo clients yet.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ New Client", callback_data="menu_setup")],
                [InlineKeyboardButton("🏠 Home", callback_data="menu_home")],
            ]),
            parse_mode=ParseMode.HTML,
        )
        return
        
    running_count = sum(1 for cid, _ in clients if svm.is_running(cid))
    page_items, total_pages = get_page_items(clients, page, CLIENTS_PER_PAGE)
    
    text = (
        f"👥 <b>Clients</b>\n"
        f"Total: <b>{len(clients)}</b> | "
        f"{E_ROCKET} {running_count} Running | "
        f"{E_RED} {len(clients)-running_count} Stopped\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    
    rows = []
    for cid, c in page_items:
        status = "running" if svm.is_running(cid) else c.get("status", "stopped")
        name = (c.get("channel_name") or c_channel(c))[:25]
        joined = len(c_joined(c))
        total = c_count(c)
        sub_icon = E_GREEN if db.is_subscribed(cid) else E_RED
        
        text += (
            f"{status_dot(status)} <b>{cid}</b> {sub_icon}\n"
            f"  {E_CHANNEL} <code>{name}</code>\n"
            f"  {E_PHONE} {joined}/{total} | "
            f"{E_FIRE} R:{c.get('reactions_per_post',0)} | "
            f"{E_ROCKET} V:{c.get('views_per_post',0)}\n\n"
        )
        rows.append([
            InlineKeyboardButton(f"📋 {cid}", callback_data=f"client_detail|{cid}"),
            InlineKeyboardButton("▶", callback_data=f"svc_start|{cid}"),
            InlineKeyboardButton("⏹", callback_data=f"svc_stop|{cid}"),
        ])
    
    # Pagination navigation
    rows += get_pagination_keyboard(page, total_pages, "cli")
    
    rows += [
        [InlineKeyboardButton("🚀 Start All", callback_data="svc_start_all"),
         InlineKeyboardButton("⏹ Stop All", callback_data="svc_stop_all")],
        [InlineKeyboardButton("🏠 Home", callback_data="menu_home")],
    ]
    
    await message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)

async def _show_client_detail(message, cid: str):
    c = db.get_client(cid)
    if not c:
        await message.edit_text("Client not found.")
        return
    status = "running" if svm.is_running(cid) else c.get("status", "stopped")
    joined = c_joined(c)
    online_j = [p for p in joined if acm.is_online(p)]
    text = (
        f"{E_CROWN} <b>Client — {cid}</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{E_CHANNEL} <code>{c_channel(c)}</code>\n"
        f"{E_PHONE} <b>{len(joined)}</b>/{c_count(c)} ({len(online_j)} online)\n"
        f"{E_ROCKET} Views: <b>{c.get('views_per_post',0)}</b>/post | "
        f"{E_FIRE} Reactions: <b>{c.get('reactions_per_post',0)}</b>/post\n"
        f"{E_CHART} Total: <b>{c.get('total_views',0):,}</b>V "
        f"<b>{c.get('total_reactions',0):,}</b>R\n"
        f"{expiry_str(c)}\n{status_dot(status)} <b>{status.upper()}</b>"
    )
    rows = [
        [InlineKeyboardButton("▶ Start", callback_data=f"svc_start|{cid}"),
         InlineKeyboardButton("⏹ Stop", callback_data=f"svc_stop|{cid}")],
        [InlineKeyboardButton("📅 +30d", callback_data=f"extend|{cid}|30"),
         InlineKeyboardButton("📅 +60d", callback_data=f"extend|{cid}|60"),
         InlineKeyboardButton("📅 +90d", callback_data=f"extend|{cid}|90")],
        [InlineKeyboardButton("🗑 Remove", callback_data=f"remove_client|{cid}"),
         InlineKeyboardButton("◀ Back", callback_data="menu_clients")],
    ]
    await message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)

# ─────────────────────────────────────────────
#  HANDLERS
# ─────────────────────────────────────────────
async def accounts_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await show_accounts_page(update.callback_query.message, 1)

async def acc_page_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle account pagination navigation"""
    q = update.callback_query
    await q.answer()
    data = q.data
    if data.startswith("acc_page_"):
        page = int(data.split("_")[-1])
        await show_accounts_page(q.message, page)

async def clients_page_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle client pagination navigation"""
    q = update.callback_query
    await q.answer()
    data = q.data
    if data.startswith("cli_page_"):
        page = int(data.split("_")[-1])
        await show_clients_page(q.message, page)

async def acc_bulk_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Processing...")
    if q.data == "acc_start_all":
        tasks = [acm.start_session(phone) for phone, _ in db.get_accounts_sorted() if not acm.is_online(phone)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        started = sum(1 for r in results if r is True)
        await q.message.reply_text(f"{E_CHECK} Started <b>{started}</b> accounts.", parse_mode=ParseMode.HTML)
    elif q.data == "acc_stop_all":
        phones = acm.get_online_accounts()
        for phone in phones: await acm.stop_session(phone)
        await q.message.reply_text(f"{E_RED} Stopped <b>{len(phones)}</b> accounts.", parse_mode=ParseMode.HTML)
    await show_accounts_page(q.message, 1)

# Account Add Handlers (unchanged from previous)
async def acc_add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data.clear()
    await q.message.reply_text(
        f"{E_PHONE} <b>Add Account</b>\n\n"
        f"Enter phone with country code:\n"
        f"Example: <code>+919876543210</code>\n\n"
        f"/cancel to abort",
        parse_mode=ParseMode.HTML
    )
    return ADD_PHONE

async def acc_recv_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not phone.startswith("+"):
        await update.message.reply_text(f"{E_WARN} Must start with + (country code)")
        return ADD_PHONE
    ctx.user_data["add_phone"] = phone
    msg = await update.message.reply_text(f"{E_CLOCK} Sending OTP to <code>{phone}</code>...", parse_mode=ParseMode.HTML)
    try:
        await acm.send_otp(phone)
        await msg.edit_text(f"{E_CHECK} OTP sent!\n\nEnter the code:", parse_mode=ParseMode.HTML)
    except FloodWait as e:
        await msg.edit_text(f"{E_WARN} FloodWait — retry after <b>{e.value}s</b>", parse_mode=ParseMode.HTML)
        return await show_add_more_menu(update, ctx)
    except Exception as e:
        await msg.edit_text(f"{E_CROSS} Error: <code>{e}</code>", parse_mode=ParseMode.HTML)
        return await show_add_more_menu(update, ctx)
    return ADD_OTP

async def acc_recv_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    phone = ctx.user_data.get("add_phone")
    result = await acm.verify_otp(phone, code)
    
    if result == "ok":
        await update.message.reply_text(
            f"{E_CHECK} <b>Account Added!</b>\n"
            f"<code>{phone}</code> is online {E_GREEN}\n\n"
            f"<b>Add another account?</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add Another Account", callback_data="add_more"),
                 InlineKeyboardButton("🏠 Done / Home", callback_data="menu_home")],
            ])
        )
        return ADD_MORE_ACCOUNT
    elif result == "2fa":
        await update.message.reply_text(f"{E_LOCK} 2FA required — Enter cloud password:", parse_mode=ParseMode.HTML)
        return ADD_2FA
    elif result == "invalid":
        await update.message.reply_text(f"{E_CROSS} Wrong code. Try again:")
        return ADD_OTP
    else:
        return await show_add_more_menu(update, ctx)

async def acc_recv_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pw = update.message.text.strip()
    phone = ctx.user_data.get("add_phone")
    ok = await acm.verify_2fa(phone, pw)
    
    if ok:
        await update.message.reply_text(
            f"{E_CHECK} <b>Account Added!</b>\n"
            f"<code>{phone}</code> is online {E_GREEN}\n\n"
            f"<b>Add another account?</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add Another Account", callback_data="add_more"),
                 InlineKeyboardButton("🏠 Done / Home", callback_data="menu_home")],
            ])
        )
        return ADD_MORE_ACCOUNT
    else:
        await update.message.reply_text(f"{E_CROSS} Wrong password.")
        return await show_add_more_menu(update, ctx)

async def add_more_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    if q.data == "add_more":
        ctx.user_data.clear()
        await q.message.reply_text(
            f"{E_PHONE} <b>Add Another Account</b>\n\n"
            f"Enter phone with country code:\n"
            f"Example: <code>+919876543210</code>\n\n"
            f"/cancel to finish",
            parse_mode=ParseMode.HTML
        )
        return ADD_PHONE
    elif q.data == "menu_home":
        await home_menu(update, ctx)
        return ConversationHandler.END

async def show_add_more_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"{E_WARN} Something went wrong.\n\n<b>Add another account?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Another Account", callback_data="add_more"),
             InlineKeyboardButton("🏠 Done / Home", callback_data="menu_home")],
        ])
    )
    return ADD_MORE_ACCOUNT

async def acc_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("|", 1)
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
                f"{E_PERSON} <b>{info['name']}</b>\n"
                f"@{info['username']}\nID: <code>{info['id']}</code>",
                parse_mode=ParseMode.HTML
            )
        else:
            await q.message.reply_text(f"{E_WARN} Offline.", parse_mode=ParseMode.HTML)
        return
    elif action == "acc_del":
        await acm.stop_session(phone)
        db.delete_account(phone)
        for cand in [os.path.join(SESSIONS_DIR, phone.replace("+", "").replace(" ", "")),
                    os.path.join(SESSIONS_DIR, phone)]:
            for ext in [".session", ".session-journal"]:
                f = cand + ext
                if os.path.exists(f): os.remove(f)
        await q.message.reply_text(f"{E_TRASH} Deleted.", parse_mode=ParseMode.HTML)
    
    # Return to current page
    page = ctx.user_data.get("acc_page", 1)
    await show_accounts_page(q.message, page)

# Client Handlers
async def clients_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await show_clients_page(update.callback_query.message, 1)

async def client_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await _show_client_detail(q.message, q.data.split("|", 1)[1])

async def client_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Processing...")
    parts = q.data.split("|")
    action = parts[0]

    if action == "svc_start_all":
        clients = db.get_clients_sorted()
        eligible = [cid for cid, _ in clients if db.is_subscribed(cid) and not svm.is_running(cid)]
        results = await asyncio.gather(*(do_join_and_start(cid) for cid in eligible), return_exceptions=True)
        started = sum(1 for r in results if isinstance(r, dict) and "error" not in r)
        failed = len(eligible) - started
        await q.message.reply_text(
            f"{E_ROCKET} Started <b>{started}</b> clients."
            + (f"\n{E_WARN} Failed: <b>{failed}</b>" if failed else ""),
            parse_mode=ParseMode.HTML
        )
        await show_clients_page(q.message, 1)
        return

    if action == "svc_stop_all":
        running = [cid for cid, _ in db.get_clients_sorted() if svm.is_running(cid)]
        for cid in running:
            svm.stop(cid)
        await q.message.reply_text(f"{E_RED} Stopped <b>{len(running)}</b> clients.", parse_mode=ParseMode.HTML)
        await show_clients_page(q.message, 1)
        return

    cid = parts[1]

    if action == "svc_start":
        if not db.is_subscribed(cid):
            await q.message.reply_text(f"{E_WARN} Expired!", parse_mode=ParseMode.HTML)
            return
        if svm.is_running(cid):
            await q.message.reply_text(f"{E_WARN} Already running.", parse_mode=ParseMode.HTML)
            return
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
        db.extend_client(cid, days)
        exp = parse_expiry(db.get_client(cid).get("subscribed_until", 0))
        await q.message.reply_text(f"{E_GIFT} +{days}d → {exp.strftime('%d %b %Y')}", parse_mode=ParseMode.HTML)
    elif action == "remove_client":
        svm.stop(cid)
        db.delete_client(cid)
        await q.message.reply_text(f"{E_TRASH} Removed.", parse_mode=ParseMode.HTML)
        await show_clients_page(q.message, 1)
        return
    await _show_client_detail(q.message, cid)

async def do_join_and_start(cid: str, notify_msg=None) -> dict:
    c = db.get_client(cid)
    if not c: return {"error": "Client not found"}
    raw_channel = c_channel(c)
    needed = c_count(c)
    joined = list(c_joined(c))
    stored_id = c_channel_id(c)
    online = acm.get_online_accounts()
    if not online: return {"error": "No online accounts."}

    to_join = [p for p in online if p not in joined][:max(0, needed - len(joined))]
    channel_id = stored_id
    newly = 0

    join_tasks = [acm.join_channel(phone, raw_channel, stored_id) for phone in to_join]
    results = await asyncio.gather(*join_tasks, return_exceptions=True)
    
    for phone, result_id in zip(to_join, results):
        if isinstance(result_id, int) and result_id:
            joined.append(phone)
            newly += 1
            if not channel_id: channel_id = result_id

    db.update_client(cid, joined_phones=joined, channel_id=channel_id)
    if not joined: return {"error": "Could not join any accounts."}
    
    svm.start(cid)
    return {"joined": len(joined), "newly": newly, "channel_id": channel_id, "started": True}

# Setup Wizard (unchanged)
async def setup_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    if not acm.get_online_accounts():
        await update.callback_query.message.reply_text(f"{E_WARN} No online accounts!", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    await update.callback_query.message.reply_text(
        f"{E_ROCKET} <b>New Client Setup</b>\n\n"
        f"<b>Step 1/6</b> — Client's Telegram User ID:\n\n/cancel to abort",
        parse_mode=ParseMode.HTML
    )
    return SETUP_UID

async def setup_uid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: ctx.user_data["setup_uid"] = int(update.message.text.strip())
    except:
        await update.message.reply_text(f"{E_WARN} Valid numeric ID:")
        return SETUP_UID
    await update.message.reply_text(f"{E_CHECK} <b>Step 2/6</b> — Channel Link:", parse_mode=ParseMode.HTML)
    return SETUP_CHAN

async def setup_chan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["setup_chan"] = update.message.text.strip()
    await update.message.reply_text(
        f"{E_CHECK} <b>Step 3/6</b> — Accounts to join?\n"
        f"Online: <b>{len(acm.get_online_accounts())}</b>",
        parse_mode=ParseMode.HTML
    )
    return SETUP_ACCS

async def setup_accs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(update.message.text.strip())
        if n <= 0: raise ValueError
        ctx.user_data["setup_accs"] = n
    except:
        await update.message.reply_text(f"{E_WARN} Positive number:")
        return SETUP_ACCS
    await update.message.reply_text(f"{E_CHECK} <b>Step 4/6</b> — Reactions/post:", parse_mode=ParseMode.HTML)
    return SETUP_REACTS

async def setup_reacts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: ctx.user_data["setup_reacts"] = int(update.message.text.strip())
    except:
        await update.message.reply_text(f"{E_WARN} Number:")
        return SETUP_REACTS
    await update.message.reply_text(f"{E_CHECK} <b>Step 5/6</b> — Views/post:", parse_mode=ParseMode.HTML)
    return SETUP_VIEWS

async def setup_views(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: ctx.user_data["setup_views"] = int(update.message.text.strip())
    except:
        await update.message.reply_text(f"{E_WARN} Number:")
        return SETUP_VIEWS
    await update.message.reply_text(f"{E_CHECK} <b>Step 6/6</b> — Days:", parse_mode=ParseMode.HTML)
    return SETUP_DAYS

async def setup_days(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        days = int(update.message.text.strip())
        if days <= 0: raise ValueError
    except:
        await update.message.reply_text(f"{E_WARN} Positive days:")
        return SETUP_DAYS

    ud = ctx.user_data
    cid = db.next_client_id(ud["setup_uid"])
    db.add_client(cid, ud["setup_uid"], ud["setup_chan"], ud["setup_accs"],
                 ud["setup_reacts"], ud["setup_views"], days)
    
    msg = await update.message.reply_text(f"{E_CLOCK} Auto-starting...", parse_mode=ParseMode.HTML)
    result = await do_join_and_start(cid, notify_msg=msg)
    
    if "error" in result:
        await msg.edit_text(
            f"{E_CHECK} Created! ID: <code>{cid}</code>\n"
            f"{E_WARN} Start manually: {result['error']}",
            parse_mode=ParseMode.HTML
        )
    else:
        await msg.edit_text(
            f"{E_ROCKET} <b>Running!</b>\n"
            f"🆔 <code>{cid}</code>\n"
            f"{E_CHANNEL} {ud['setup_chan']}\n"
            f"{E_CAL} {(datetime.now()+timedelta(days=days)).strftime('%d %b %Y')}",
            parse_mode=ParseMode.HTML
        )
    return ConversationHandler.END

async def setup_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"{E_CROSS} Cancelled.")
    return ConversationHandler.END

# Stats
async def stats_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    s = db.stats()
    text = (
        f"{E_CHART} <b>Live Stats</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{E_PHONE} Accounts: <b>{s['total_accounts']}</b> ({E_GREEN}{s['online']} online)\n"
        f"{E_PERSON} Clients: <b>{s['total_clients']}</b> ({E_ROCKET}{s['active_clients']} active)\n\n"
        f"{E_ROCKET} Views: <code>{s['total_views']:,}</code>\n"
        f"{E_FIRE} Reactions: <code>{s['total_reactions']:,}</code>\n\n"
        f"{E_CLOCK} <i>{datetime.now().strftime('%d %b %Y %H:%M:%S')}</i>"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Refresh", callback_data="menu_stats"),
        InlineKeyboardButton("🏠 Home", callback_data="menu_home"),
    ]])
    await q.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

# Router
async def router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data if update.callback_query else ""
    
    # Handle noop (for page display)
    if data == "noop":
        await update.callback_query.answer("Use ⬅️ ➡️ to navigate")
        return
    
    if data == "menu_home": await home_menu(update, ctx)
    elif data == "menu_accounts": await accounts_menu(update, ctx)
    elif data == "menu_clients": await clients_menu(update, ctx)
    elif data == "menu_stats": await stats_menu(update, ctx)
    elif data in ("acc_start_all", "acc_stop_all"): await acc_bulk_action(update, ctx)
    elif data.startswith("acc_page_"): await acc_page_handler(update, ctx)
    elif data.startswith("cli_page_"): await clients_page_handler(update, ctx)
    elif data.startswith("client_detail|"): await client_detail(update, ctx)
    elif data in ("svc_start_all", "svc_stop_all"): await client_action(update, ctx)
    elif data.startswith(("svc_start|", "svc_stop|", "extend|", "remove_client|")): await client_action(update, ctx)
    elif data.startswith(("acc_start|", "acc_stop|", "acc_del|", "acc_info|")): await acc_action(update, ctx)
    else: await update.callback_query.answer("Unknown")

# Auto-start
async def auto_start_services():
    logger.info("=== Auto-starting services ===")
    accounts = db.get_accounts()
    start_tasks = [acm.start_session(phone) for phone in accounts]
    await asyncio.gather(*start_tasks, return_exceptions=True)
    online = acm.get_online_accounts()
    logger.info(f"Accounts online: {len(online)}/{len(accounts)}")

    clients = db.get_clients()
    for cid, c in clients.items():
        if db.is_subscribed(cid) and c.get("status") == "running":
            result = await do_join_and_start(cid)
            if "error" in result:
                logger.warning(f"Auto-start {cid}: {result['error']}")
            else:
                logger.info(f"Auto-started {cid}: {result['joined']} accounts")
    logger.info("=== Auto-start complete ===")

async def on_startup(app: Application):
    logger.info("=== Bot Starting ===")
    await auto_start_services()
    logger.info("=== Bot Ready ===")

async def on_shutdown(app: Application):
    svm.shutdown()
    await acm.stop_all()
    logger.info("All stopped.")

# Main
def main():
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", admin_only(home_menu)))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(acc_add_start, pattern="^acc_add$")],
        states={
            ADD_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, acc_recv_phone)],
            ADD_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, acc_recv_otp)],
            ADD_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, acc_recv_2fa)],
            ADD_MORE_ACCOUNT: [
                CallbackQueryHandler(add_more_handler, pattern="^(add_more|menu_home)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", setup_cancel)],
        allow_reentry=True, per_message=False,
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(setup_start, pattern="^menu_setup$")],
        states={
            SETUP_UID: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_uid)],
            SETUP_CHAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_chan)],
            SETUP_ACCS: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_accs)],
            SETUP_REACTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_reacts)],
            SETUP_VIEWS: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_views)],
            SETUP_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_days)],
        },
        fallbacks=[CommandHandler("cancel", setup_cancel)],
        allow_reentry=True, per_message=False,
    ))

    app.add_handler(CallbackQueryHandler(router))
    logger.info("Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
