import os
import re
from flask import Flask, request
import telebot
from telebot import types
from deep_translator import GoogleTranslator

# Thêm thư viện nhận diện ngôn ngữ + IPA + pinyin
from langdetect import detect, DetectorFactory
import eng_to_ipa as ipa
from pypinyin import lazy_pinyin, Style

DetectorFactory.seed = 0

BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}" if RENDER_EXTERNAL_URL else None


# -------------------------
# Helpers
# -------------------------
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


def safe_translate(text: str, target: str) -> str | None:
    try:
        return translate_in_chunks(text, target)
    except Exception as e:
        print(f"Lỗi dịch sang {target}:", e)
        return None


def normalize_spaces(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", (text or "").strip())
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text.strip()


def contains_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def has_vietnamese_chars(text: str) -> bool:
    return bool(re.search(
        r"[ăâđêôơưĂÂĐÊÔƠƯáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệ"
        r"íìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ]", text
    ))


def detect_input_language(text: str) -> str:
    """
    Trả về: 'vi', 'en', hoặc 'zh'
    """
    text = (text or "").strip()

    if contains_chinese(text):
        return "zh"

    try:
        detected = detect(text)
        if detected == "vi":
            return "vi"
        if detected == "en":
            return "en"
        if detected.startswith("zh"):
            return "zh"
    except Exception as e:
        print("Lỗi detect language:", e)

    if has_vietnamese_chars(text):
        return "vi"

    return "en"


def text_to_ipa(text: str) -> str | None:
    if not text:
        return None

    try:
        ipa_text = ipa.convert(
            text,
            keep_punct=True,
            stress_marks="both"
        )
        ipa_text = normalize_spaces(ipa_text)

        if not ipa_text:
            return None

        # Bỏ dấu * nếu thư viện không nhận một số từ riêng / brand
        ipa_text = ipa_text.replace("*", "")
        ipa_text = normalize_spaces(ipa_text)

        return ipa_text or None
    except Exception as e:
        print("Lỗi chuyển IPA:", e)
        return None


def text_to_pinyin(text: str) -> str | None:
    """
    Chuyển chữ Hán sang pinyin có dấu.
    Giữ lại dấu câu cơ bản.
    """
    if not text:
        return None

    try:
        tokens = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9]+|\s+|[^\w\s]", text, flags=re.UNICODE)
        out = []

        for token in tokens:
            if re.fullmatch(r"[\u4e00-\u9fff]+", token):
                py = " ".join(lazy_pinyin(token, style=Style.TONE, strict=False))
                out.append(py)
            else:
                out.append(token)

        result = "".join(out)
        result = normalize_spaces(result)
        return result or None
    except Exception as e:
        print("Lỗi chuyển pinyin:", e)
        return None


# -------------------------
# Core logic
# -------------------------
def translate_text(text: str):
    text = (text or "").strip()
    if not text:
        return None

    if text.startswith("http://") or text.startswith("https://"):
        return None

    source_lang = detect_input_language(text)

    # Tiếng Việt -> EN + ZH
    if source_lang == "vi":
        translated_en = safe_translate(text, "en")
        translated_zh = safe_translate(text, "zh-CN")

        if not translated_en and not translated_zh:
            return None

        return {
            "source_lang": "vi",
            "original": text,
            "en": translated_en,
            "ipa": text_to_ipa(translated_en) if translated_en else None,
            "vi": text,
            "zh": translated_zh,
            "pinyin": text_to_pinyin(translated_zh) if translated_zh else None,
        }

    # Tiếng Trung -> VI + EN
    if source_lang == "zh":
        translated_vi = safe_translate(text, "vi")
        translated_en = safe_translate(text, "en")

        if not translated_vi and not translated_en:
            return None

        return {
            "source_lang": "zh",
            "original": text,
            "en": translated_en,
            "ipa": text_to_ipa(translated_en) if translated_en else None,
            "vi": translated_vi,
            "zh": text,
            "pinyin": text_to_pinyin(text),
        }

    # Mặc định là tiếng Anh -> VI + ZH
    translated_vi = safe_translate(text, "vi")
    translated_zh = safe_translate(text, "zh-CN")

    if not translated_vi and not translated_zh:
        return None

    return {
        "source_lang": "en",
        "original": text,
        "en": text,
        "ipa": text_to_ipa(text),
        "vi": translated_vi,
        "zh": translated_zh,
        "pinyin": text_to_pinyin(translated_zh) if translated_zh else None,
    }


def format_reply(sender: str, data: dict) -> str:
    source_lang = data.get("source_lang")
    original = data.get("original")
    en = data.get("en")
    ipa_text = data.get("ipa")
    vi = data.get("vi")
    zh = data.get("zh")
    pinyin = data.get("pinyin")

    parts = []

    if source_lang == "vi":
        parts.append(f"[VI → EN | ZH] {sender}")
        parts.append(f"VI: {original}")

        if en:
            parts.append(f"\nEN: {en}")
        if ipa_text:
            parts.append(f"IPA: /{ipa_text}/")

        if zh:
            parts.append(f"\nZH: {zh}")
        if pinyin:
            parts.append(f"Pinyin: {pinyin}")

        return "\n".join(parts)

    if source_lang == "zh":
        parts.append(f"[ZH → EN | VI] {sender}")
        parts.append(f"ZH: {original}")

        if pinyin:
            parts.append(f"Pinyin: {pinyin}")

        if en:
            parts.append(f"\nEN: {en}")
        if ipa_text:
            parts.append(f"IPA: /{ipa_text}/")

        if vi:
            parts.append(f"\nVI: {vi}")

        return "\n".join(parts)

    parts.append(f"[EN → VI | ZH] {sender}")
    parts.append(f"EN: {original}")

    if ipa_text:
        parts.append(f"IPA: /{ipa_text}/")

    if vi:
        parts.append(f"\nVI: {vi}")

    if zh:
        parts.append(f"\nZH: {zh}")
    if pinyin:
        parts.append(f"Pinyin: {pinyin}")

    return "\n".join(parts)


# -------------------------
# Telegram handler
# -------------------------
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

        # Bỏ qua command
        if text.startswith("/"):
            return

        result = translate_text(text)
        if not result:
            print("Không dịch được:", repr(text[:200]))
            return

        sender = message.from_user.first_name or "User"
        reply_text = format_reply(sender, result)

        max_telegram_len = 3500
        if len(reply_text) > max_telegram_len:
            reply_text = reply_text[:max_telegram_len] + "\n\n...[message truncated]"

        bot.send_message(message.chat.id, reply_text)

    except Exception as e:
        print("Lỗi handle_message:", e)


# -------------------------
# Flask routes
# -------------------------
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
