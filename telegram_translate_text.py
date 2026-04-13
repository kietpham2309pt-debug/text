import os
from flask import Flask, request
import telebot
from telebot import types
from deep_translator import GoogleTranslator

BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}" if RENDER_EXTERNAL_URL else None


def split_text_for_translate(text: str, max_len: int = 1000) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []

    lines = text.splitlines()
    chunks = []
    current = ""

    for line in lines:
        line = line.rstrip()
        candidate = f"{current}\n{line}".strip() if current else line

        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            while len(line) > max_len:
                chunks.append(line[:max_len])
                line = line[max_len:]
            current = line

    if current:
        chunks.append(current)

    return chunks


def translate_in_chunks(text: str, target: str) -> str | None:
    chunks = split_text_for_translate(text)
    if not chunks:
        return None

    translated_parts = []
    for chunk in chunks:
        translated = GoogleTranslator(source="auto", target=target).translate(chunk)
        if translated:
            translated_parts.append(translated)

    result = "\n".join(translated_parts).strip()
    return result or None


def translate_text(text: str):
    text = (text or "").strip()
    if not text:
        return None

    if text.startswith("http://") or text.startswith("https://"):
        return None

    try:
        translated_en = translate_in_chunks(text, "en")
        if translated_en and translated_en.strip().lower() != text.strip().lower():
            return "VI → EN", translated_en
    except Exception as e:
        print("Lỗi dịch sang EN:", e)

    try:
        translated_vi = translate_in_chunks(text, "vi")
        if translated_vi and translated_vi.strip().lower() != text.strip().lower():
            return "EN → VI", translated_vi
    except Exception as e:
        print("Lỗi dịch sang VI:", e)

    return None


@bot.message_handler(content_types=["text"])
def handle_message(message: types.Message):
    try:
        if message.from_user and message.from_user.is_bot:
            return

        if message.chat.type not in ["group", "supergroup"]:
            return

        text = (message.text or "").strip()
        if not text:
            return

        result = translate_text(text)
        if not result:
            print("Không dịch được:", repr(text[:200]))
            return

        label, translated = result
        sender = message.from_user.first_name or "User"

        max_telegram_len = 3500
        if len(translated) > max_telegram_len:
            translated = translated[:max_telegram_len] + "\n\n...[message truncated]"

        bot.send_message(message.chat.id, f"[{label}] {sender}: {translated}")

    except Exception as e:
        print("Lỗi handle_message:", e)


@app.route("/", methods=["GET"])
def healthcheck():
    return "Bot is running", 200


@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    try:
        if request.headers.get("content-type") == "application/json":
            json_str = request.get_data().decode("utf-8")
            update = types.Update.de_json(json_str)
            bot.process_new_updates([update])
            return "", 200
        return "Unsupported Media Type", 415
    except Exception as e:
        print("Lỗi webhook:", e)
        return "Internal Server Error", 500


if __name__ == "__main__":
    if not RENDER_EXTERNAL_URL:
        raise RuntimeError("Missing RENDER_EXTERNAL_URL")

    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)