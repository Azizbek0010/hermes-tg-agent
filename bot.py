#!/usr/bin/env python3
"""
tg-opencode-bot — мост Telegram ↔ opencode serve

Архитектура (путь №3 из гайда, адаптация под opencode):
  Telegram (long-polling) → этот бот → HTTP API opencode serve → ПОЛНАЯ сессия
  (плагины, авто-RAG, флот агентов, память — всё работает как в TUI)

Безопасность:
  - токен только в .env (600), не в коде
  - паринг: первый /start фиксирует владельца (owner.json); остальные — отказ
  - один поллер: run.sh убивает старые инстансы (урок 409 Conflict)
"""

import asyncio
import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, ChatMemberUpdated, CallbackQuery

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OC_PORT = int(os.environ.get("OC_PORT", "46100"))
OC_AGENT = os.environ.get("OC_AGENT", "build")
OC_CWD = os.environ.get("OC_CWD", str(Path.home()))
OC_MODEL_PROVIDER = os.environ.get("OC_MODEL_PROVIDER", "opencode")
OC_MODEL_ID = os.environ.get("OC_MODEL_ID", "big-pickle")
OWNER_ID_ENV = int(os.environ["OWNER_ID"]) if os.environ.get("OWNER_ID") else None
API = f"http://127.0.0.1:{OC_PORT}"
OWNER_FILE = BASE / "owner.json"
SESSIONS_FILE = BASE / "sessions.json"
HTTP_TIMEOUT = 900  # 15 мин на тяжёлые задачи

# Гейт разрешений: agents_watch.py кладёт сюда запросы «чужой просит сделать
# работу», а нажатия кнопок обрабатываются ЗДЕСЬ — getUpdates может
# опрашивать только один процесс, иначе TelegramConflictError.
AGENTS_GROUP_ID = int(os.environ["AGENTS_GROUP_ID"]) if os.environ.get("AGENTS_GROUP_ID") else None
PENDING_FILE = Path(os.environ.get("PENDING_APPROVALS_PATH", str(BASE / "pending_approvals.json")))

# Куда агент кладёт созданные файлы. ВАЖНО: контейнер Render эфемерный и до
# локального Obsidian владельца (C:\Users\user\Documents\my-brain) дотянуться
# физически не может — проверено, переменная OBSIDIAN_VAULT там не задана.
# Поэтому созданные файлы не «сохраняются на сервере», а ОТПРАВЛЯЮТСЯ
# владельцу в Telegram документом: так они реально попадают к нему в руки и
# он кладёт их в Obsidian сам.
WORKSPACE = Path(os.environ.get("WORKSPACE_PATH", str(BASE / "workspace")))


def send_document(chat_id: int, file_path: Path, caption: str = ""):
    """Отправляет файл владельцу. Telegram Bot API отдаёт до 50 МБ."""
    try:
        with open(file_path, "rb") as fh:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id": chat_id, "caption": caption[:1000]},
                files={"document": (file_path.name, fh)},
                timeout=120,
            )
        return True
    except Exception as e:
        print(f"[bot] не смог отправить файл {file_path}: {e}")
        return False


def post_to_group(text: str, reply_to: int = None) -> bool:
    """Отправка в группу с проверкой ответа Telegram и запасным путём.

    Реальный случай 2026-08-30: якорное сообщение (реплика чужого бота с
    ошибкой 402) успели удалить, Telegram вернул "message to be replied not
    found", и результат выполненной работы пропал молча — код не смотрел на
    ответ API. Теперь при неудачной привязке шлём то же самое без reply_to,
    а любую ошибку пишем в лог."""
    if not (AGENTS_GROUP_ID and text):
        return False
    ok_all = True
    for i in range(0, max(len(text), 1), 4000):
        chunk = text[i:i + 4000]
        payload = {"chat_id": AGENTS_GROUP_ID, "text": chunk}
        if i == 0 and reply_to:
            payload["reply_to_message_id"] = reply_to
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json=payload, timeout=20,
            )
            data = r.json()
            if not data.get("ok") and "reply" in str(data.get("description", "")).lower():
                # якорь исчез — повторяем без привязки, чтобы не потерять текст
                print(f"[bot] reply_to={reply_to} не найден, шлю без привязки")
                payload.pop("reply_to_message_id", None)
                r = requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json=payload, timeout=20,
                )
                data = r.json()
            if not data.get("ok"):
                ok_all = False
                print(f"[bot] отправка в группу не удалась: {data.get('description')}")
        except Exception as e:
            ok_all = False
            print(f"[bot] отправка в группу упала: {e}")
    return ok_all


def collect_and_send_files(chat_id: int, work_dir: Path, caption: str) -> int:
    """Отдаёт владельцу всё, что агент создал. Много файлов — одним zip."""
    if not work_dir.exists():
        return 0
    files = [p for p in work_dir.rglob("*") if p.is_file()]
    if not files:
        return 0
    if len(files) == 1:
        send_document(chat_id, files[0], caption)
        return 1
    import zipfile
    archive = work_dir.parent / f"{work_dir.name}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, p.relative_to(work_dir))
    send_document(chat_id, archive, f"{caption} ({len(files)} файлов)")
    return len(files)




HQ_DIR = os.path.join(os.environ.get("OBSIDIAN_VAULT", str(Path.home() / "Документы" / "Obsidian Vault")), os.environ.get("HQ_FOLDER", "00 — Штаб"))

def hq_log(who: str, text: str):
    """Реплика бота → общий журнал штаба (общая память пары)."""
    try:
        os.makedirs(HQ_DIR, exist_ok=True)
        f = os.path.join(HQ_DIR, time.strftime("%Y-%m-%d") + ".md")
        stamp = time.strftime("%H:%M")
        snippet = " ".join(text.split())[:200]
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"\n**[{stamp}] {who}:** {snippet}\n")
    except Exception:
        pass

# ---------------- состояние ----------------

def load_json(p: Path, default):
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return default


def save_json(p: Path, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1))


owner = load_json(OWNER_FILE, None)          # {"id": 123, "name": "..."}
sessions = load_json(SESSIONS_FILE, {})      # {chat_id_str: session_id}
busy = False                                  # одна задача за раз

# --- групповой режим / анти-петля ---
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")  # твой @username бота
BOT_NAME = os.environ.get("BOT_NAME", "my agent")          # @defactor228_bot
GROUP_COOLDOWN = int(os.environ.get("GROUP_COOLDOWN", "20"))  # сек между ответами в группе
MAX_BOT_TURNS = 4                                           # макс подряд реплик ботов без человека
_last_group_reply = 0.0
_bot_turn_streak = 0                                        # подряд реплик ботов (любых)


def _in_group(message: Message) -> bool:
    return message.chat.type in ("group", "supergroup")


def _mentioned(message: Message) -> bool:
    text = (message.text or "").lower()
    if BOT_USERNAME and BOT_USERNAME.lower() in text:
        return True
    # reply на МОЁ сообщение
    rep = message.reply_to_message
    if rep and rep.from_user and rep.from_user.is_bot:
        return True
    return False


def _loop_guard(message: Message) -> bool:
    """True = отвечать можно. Бот↔бот диалог РАЗРЕШЁН при явном упоминании,
    с жёстким лимитом подряд идущих бото-реплик."""
    global _bot_turn_streak, _last_group_reply
    if message.from_user and message.from_user.is_bot:
        if not _mentioned(message):
            return False            # без @нас — игнор даже ботов
        if _bot_turn_streak >= MAX_BOT_TURNS:
            return False            # лимит обмена — ждём человека
        _bot_turn_streak += 1
    else:
        _bot_turn_streak = 0
    if _in_group(message):
        import time as _t
        if _t.time() - _last_group_reply < GROUP_COOLDOWN:
            return False
        _last_group_reply = _t.time()
    return True


def oc(path: str, method="GET", payload=None, timeout=30):
    return requests.request(method, f"{API}{path}", json=payload, timeout=timeout)


def oc_session_for(chat_id: int) -> str:
    key = str(chat_id)
    if key in sessions:
        return sessions[key]
    r = oc("/session", "POST", {})
    sid = r.json()["id"]
    sessions[key] = sid
    save_json(SESSIONS_FILE, sessions)
    return sid


async def ask_opencode(text: str, chat_id: int) -> str:
    """POST message → блокируется до готовности → текст ассистента."""
    sid = oc_session_for(chat_id)
    loop = asyncio.get_running_loop()
    resp = await loop.run_in_executor(
        None,
        lambda: oc(f"/session/{sid}/message", "POST",
                   {"parts": [{"type": "text", "text": text}], "agent": OC_AGENT,
                    "model": {"providerID": OC_MODEL_PROVIDER, "modelID": OC_MODEL_ID}},
                   timeout=HTTP_TIMEOUT),
    )
    resp.raise_for_status()
    data = resp.json()
    parts = data.get("parts", []) if isinstance(data, dict) else []
    texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
    out = "\n".join(t for t in texts if t).strip()
    return out or "(пустой ответ)"


async def send_long(message: Message, text: str):
    """Telegram лимит 4096 — режем и шлём кусками."""
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000])


# ---------------- хендлеры ----------------


async def _probe_any(event):
    try:
        if isinstance(event, ChatMemberUpdated):
            c = event.chat
            print(f"[member] {c.get('type') if isinstance(c,dict) else c.type} id={c.get('id') if isinstance(c,dict) else c.id} status={event.new_chat_member.status}", flush=True)
        else:
            c = event.chat
            print(f"[channel_post] id={c.id} text={(event.text or '')[:40]!r}", flush=True)
    except Exception as e:
        print(f"[probe-err] {e}", flush=True)

async def cmd_start(message: Message):
    global owner
    tg_id = message.from_user.id
    if owner is None:
        owner = {"id": tg_id, "name": message.from_user.full_name}
        save_json(OWNER_FILE, owner)
        await message.answer(
            "🔐 Паринг завершён. Ты владелец — теперь только ты управляешь ботом.\n\n"
            "Пиши задачу обычным сообщением — я выполняю через полную opencode-сессию:\n"
            "код, файлы, терминал, агенты, память.\n\n"
            "Команды:\n/new — новая сессия (сброс контекста)\n"
            "/agent <имя> — сменить агента\n/status — статус моста"
        )
    elif owner["id"] == tg_id:
        await message.answer("Ты уже владелец. Просто пиши задачу.")
    else:
        await message.answer("⛔️ Бот занят другим владельцем.")


async def cmd_new(message: Message):
    global busy
    if not _is_owner(message):
        return
    if busy:
        await message.answer("⏳ Занят текущей задачей — /new после завершения.")
        return
    sessions.pop(str(message.chat.id), None)
    save_json(SESSIONS_FILE, sessions)
    sid = oc_session_for(message.chat.id)
    await message.answer(f"🆕 Новая сессия: `{sid[:16]}…`")


async def cmd_agent(message: Message):
    global OC_AGENT
    if not _is_owner(message):
        return
    arg = (message.text or "").split(maxsplit=1)[1].strip() if len((message.text or "").split()) > 1 else ""
    if not arg:
        await message.answer(f"Текущий агент: `{OC_AGENT}`.\nСмена: `/agent <имя>` (например /fleet список смотри в REGISTRY).")
        return
    OC_AGENT = arg
    await message.answer(f"✅ Агент переключён на `{arg}`.")


async def cmd_status(message: Message):
    if not _is_owner(message):
        return
    try:
        ok = oc("/doc", timeout=5).status_code == 200
        srv = "✅" if ok else "❌"
    except Exception:
        srv = "❌"
    await message.answer(
        f"📊 Статус\nopencode serve: {srv} :{OC_PORT}\nАгент: `{OC_AGENT}`\n"
        f"Сессий в памяти: {len(sessions)}\nВладелец: `{owner['id'] if owner else 'не спарен'}`"
    )


def _is_owner(message: Message) -> bool:
    return owner is not None and message.from_user.id == owner["id"]


async def on_text(message: Message):
    global busy
    # диагностика доставки
    try:
        print(f"[msg] {message.chat.type} chat={message.chat.id} from={message.from_user.id if message.from_user else '?'}: {(message.text or '')[:50]}", flush=True)
    except Exception:
        pass
    if owner is None:
        await message.answer("⚠️ Сначала /start для паринга.")
        return

    # В группах бот НЕ выполняет команды вообще — реальные действия только
    # в личке с владельцем. Группу "Agents" анализирует agents_watch.py
    # (отдельный процесс, без прав на выполнение, только запись в
    # Global Task.md) — эта развилка раньше пропускала владельца и в группе
    # выполняла задачи полноправным агентом (реальный инцидент 2026-08-27:
    # сообщение в группе создало папку на диске). Если понадобится, чтобы
    # бот участвовал в группе как раньше — включать осознанно, отдельно.
    if _in_group(message):
        return
    else:
        if message.from_user.id != owner["id"]:
            return  # молча игнорируем чужих в личке

    if busy:
        await message.answer("⏳ Обрабатываю предыдущую задачу. Подожди завершения.")
        return
    busy = True
    t0 = time.time()
    typing_task = asyncio.create_task(_typing_loop(message.bot, message.chat.id))
    try:
        answer = await ask_opencode(message.text, message.chat.id)
        dt = time.time() - t0
        await send_long(message, f"{answer}\n\n_{dt:.0f}s_")
        hq_log(BOT_NAME, answer)
    except requests.Timeout:
        await message.answer("⏰ opencode не ответил за 15 минут. Попробуй /new или упрости задачу.")
    except Exception as e:
        await message.answer(f"❌ Ошибка моста: `{str(e)[:200]}`\nПроверь: `opencode serve` запущен? (`run.sh`)")
    finally:
        typing_task.cancel()
        busy = False


async def _typing_loop(bot: Bot, chat_id: int):
    while True:
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            pass
        await asyncio.sleep(5)


async def on_voice(message: Message):
    if _is_owner(message):
        await message.answer("🎙 Голосовые в v1 не расшифровываются. Пришли текстом.")


# ---------------- запуск ----------------

async def on_approval_callback(cb: CallbackQuery):
    """Владелец нажал «Ha» или «Yo'q» на запросе разрешения из группы.

    Только владелец может решать: chat_id кнопки сверяется с owner. При «Ha»
    работа реально выполняется (через opencode) и результат уходит в группу
    ответом на исходную просьбу; при «Yo'q» — вежливый отказ."""
    data = cb.data or ""
    if not any(data.startswith(p) for p in ("apr:", "den:", "sav:")):
        return
    if not owner or cb.from_user.id != owner["id"]:
        await cb.answer("Только владелец может решать", show_alert=True)
        return

    action, req_id = data.split(":", 1)
    pending = load_json(PENDING_FILE, {})
    req = pending.get(req_id)
    if not req:
        await cb.answer("Запрос уже не актуален", show_alert=True)
        return
    if req.get("status") != "pending":
        await cb.answer(f"Уже обработан: {req.get('status')}", show_alert=True)
        return

    requester = req.get("requester", "пользователь")
    request_text = req.get("request_text", "")
    group_msg_id = req.get("group_msg_id")

    if action == "den":
        req["status"] = "denied"
        pending[req_id] = req
        save_json(PENDING_FILE, pending)
        await cb.answer("Отказано")
        try:
            await cb.message.edit_text(f"{cb.message.text}\n\n❌ Otkazano — работа не начата.")
        except Exception:
            pass
        post_to_group(
            f"{requester}, взять эту работу сейчас не могу — владелец не дал "
            f"разрешения. Если нужно обсудить или разобрать задачу "
            f"теоретически — спрашивайте, отвечу.",
            reply_to=group_msg_id,
        )
        return

    # ── «Task qilib saqla» — не делаем сейчас, оформляем задачу в .md ────────
    if action == "sav":
        req["status"] = "saved"
        pending[req_id] = req
        save_json(PENDING_FILE, pending)
        await cb.answer("Сохраняю как задачу")
        try:
            await cb.message.edit_text(f"{cb.message.text}\n\n📝 Saqlandi — задача на потом.")
        except Exception:
            pass

        prompt = (
            "Оформи эту просьбу как задачу для Obsidian в формате Markdown, "
            "по-русски. Верни ТОЛЬКО содержимое .md файла, без пояснений вокруг.\n"
            "Структура: заголовок; кто просил и когда; что именно нужно сделать; "
            "критерии готовности (чек-лист); ориентировочные шаги; открытые вопросы.\n\n"
            f"Кто просил: {requester}\n"
            f"Просьба: {request_text}"
        )
        try:
            md = await ask_opencode(prompt, owner["id"])
        except Exception as e:
            md = f"# Задача от {requester}\n\n{request_text}\n\n(агент не смог оформить: {e})"

        WORKSPACE.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in requester if c.isalnum() or c in "-_")[:20] or "task"
        md_path = WORKSPACE / f"task_{time.strftime('%Y-%m-%d')}_{safe}.md"
        md_path.write_text(md, encoding="utf-8")
        send_document(
            owner["id"], md_path,
            "📝 Задача на потом — сохрани в Obsidian.\n"
            "(Контейнер до твоего локального хранилища не дотягивается, "
            "поэтому файл приходит сюда.)",
        )
        post_to_group(
            f"{requester}, задачу принял и поставил в очередь. "
            f"Возьмусь позже — как дойдут руки у владельца.",
            reply_to=group_msg_id,
        )
        return

    # ── «Hozir qil» — выполняем прямо сейчас, с созданием файлов ────────────
    req["status"] = "approved"
    pending[req_id] = req
    save_json(PENDING_FILE, pending)
    await cb.answer("Разрешено — начинаю")
    try:
        await cb.message.edit_text(f"{cb.message.text}\n\n✅ Ruxsat berildi — приступаю.")
    except Exception:
        pass

    work_dir = WORKSPACE / req_id
    work_dir.mkdir(parents=True, exist_ok=True)
    prompt = (
        "Владелец РАЗРЕШИЛ взяться за эту работу. Выполни её полноценно, "
        "по-русски, с конкретным результатом, а не общими словами.\n\n"
        f"ВСЕ создаваемые файлы клади в каталог: {work_dir}\n"
        "Если результат — это код, сайт, схема или документ, ОБЯЗАТЕЛЬНО создай "
        "реальные файлы в этом каталоге (пиши их инструментами), а не только "
        "показывай код в ответе. В самом ответе дай краткое резюме: что сделал, "
        "какие файлы создал и как этим пользоваться.\n\n"
        f"Кто просил: {requester}\n"
        f"Просьба: {request_text}"
    )
    try:
        answer = await ask_opencode(prompt, owner["id"])
    except Exception as e:
        answer = ""
        await cb.message.answer(f"⚠️ Не смог выполнить: {e}")

    n = collect_and_send_files(owner["id"], work_dir, "📦 Файлы по выполненной задаче")
    if n:
        await cb.message.answer(f"📦 Готово: создано файлов — {n}, отправил выше.")
    else:
        await cb.message.answer("ℹ️ Файлов агент не создал — результат только текстом.")

    if answer:
        sent_ok = post_to_group(answer, reply_to=group_msg_id)
        await cb.message.answer("📨 Результат отправлен в группу."
                                if sent_ok else
                                "⚠️ Результат в группу отправить не удалось — см. логи.")


async def main():
    global owner
    # Преднастроенный владелец из .env — безопаснее паринга через чат (урок из гайда)
    if owner is None and OWNER_ID_ENV:
        owner = {"id": OWNER_ID_ENV, "name": "owner (from .env)"}
        save_json(OWNER_FILE, owner)
        print(f"[ok] владелец предзадан из .env: {OWNER_ID_ENV}")

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_new, Command("new"))
    dp.message.register(cmd_agent, Command("agent"))
    dp.message.register(cmd_status, Command("status"))
    dp.message.register(on_voice, F.voice | F.audio | F.video_note)
    dp.message.register(on_text, F.text)
    dp.callback_query.register(on_approval_callback)
    dp.chat_member.register(_probe_any)
    dp.channel_post.register(_probe_any)

    # проверка serve; если мёртв — пробуем поднять
    try:
        oc("/doc", timeout=5)
        print(f"[ok] opencode serve отвечает на :{OC_PORT}")
    except Exception:
        print("[!] serve не отвечает — пробую поднять...")
        subprocess_detach()

    me = await bot.get_me()
    who = owner["name"] if owner else "НЕ СПАРЕН — жду /start"
    print(f"[ok] бот @{me.username} запущен. Владелец: {who}")
    await dp.start_polling(bot)


def subprocess_detach():
    import subprocess
    subprocess.Popen(
        ["opencode", "serve", "--port", str(OC_PORT)],
        cwd=OC_CWD,
        stdout=open(BASE / "serve.log", "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(20):
        time.sleep(1)
        try:
            oc("/doc", timeout=3)
            print("[ok] serve поднят")
            return
        except Exception:
            continue
    print("[!] serve так и не поднялся")


if __name__ == "__main__":
    asyncio.run(main())
