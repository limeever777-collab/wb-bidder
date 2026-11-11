# Telegram для бидера: сообщения, кнопки, приём нажатий. Токены и чаты — в telegram.json (рядом, chmod 600).
import json, os, urllib.error, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(BASE, "telegram.json")


def conf(bot):
    # {"bidder": {"token": "...", "chat_id": 123}, "blocks": {...}}; нет файла или чата — бот не настроен
    try:
        c = json.load(open(CONF, encoding="utf-8")).get(bot) or {}
    except FileNotFoundError:
        return None
    return c if c.get("token") and c.get("chat_id") else None


def call(token, method, body):
    r = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(r, timeout=30).read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": e.code, "body": e.read()[:300].decode("utf-8", "replace")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send(bot, text, buttons=None):
    c = conf(bot)
    if not c:
        return None
    # chat_id может быть списком: сообщение уходит в каждый чат; результат — первый успешный ответ
    chats = c["chat_id"] if isinstance(c["chat_id"], list) else [c["chat_id"]]
    res = None
    for chat in chats:
        body = {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}
        r = call(c["token"], "sendMessage", body)
        if res is None or (r.get("ok") and not res.get("ok")):
            res = r
    return res


def updates(bot, offset):
    c = conf(bot)
    if not c:
        return []
    r = call(c["token"], "getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["callback_query", "message"]})
    return r.get("result") or [] if r.get("ok") else []


def answer(bot, callback_id, text):
    c = conf(bot)
    if c:
        call(c["token"], "answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})


def edit(bot, chat_id, message_id, text):
    c = conf(bot)
    if c:
        call(c["token"], "editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text[:4000], "parse_mode": "HTML"})
