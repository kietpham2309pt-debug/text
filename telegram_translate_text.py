import os
import re
import html
from typing import Optional, List, Dict, Any

from flask import Flask, request
import telebot
from telebot import types
from deep_translator import GoogleTranslator
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


# =========================
# Text utils
# =========================
def normalize_spaces(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text.strip()


def split_text_for_translate(text: str, max_len: int = 1000) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []

    lines = text.splitlines()
    chunks: List[str] = []
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


def translate_in_chunks(text: str, target: str) -> Optional[str]:
    chunks = split_text_for_translate(text)
    if not chunks:
        return None

    translated_parts: List[str] = []
    for chunk in chunks:
        translated = GoogleTranslator(source="auto", target=target).translate(chunk)
        if translated:
            translated_parts.append(translated)

    result = "\n".join(translated_parts).strip()
    return result or None


def safe_translate(text: str, target: str) -> Optional[str]:
    try:
        return translate_in_chunks(text, target)
    except Exception as e:
        print(f"Lỗi dịch sang {target}: {e}")
        return None


# =========================
# Language detection
# =========================
def contains_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def has_vietnamese_chars(text: str) -> bool:
    return bool(re.search(
        r"[ăâđêôơưĂÂĐÊÔƠƯáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệ"
        r"íìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ]",
        text
    ))


def strip_for_detect(text: str) -> str:
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^\w\s\u4e00-\u9fffÀ-ỹ]", " ", text, flags=re.UNICODE)
    return normalize_spaces(text)


def detect_input_language(text: str) -> str:
    """
    Trả về: vi / en / zh
    """
    raw = (text or "").strip()
    if not raw:
        return "en"

    if contains_chinese(raw):
        return "zh"

    if has_vietnamese_chars(raw):
        return "vi"

    cleaned = strip_for_detect(raw)

    letters_only = re.sub(r"[^A-Za-zÀ-ỹ\u4e00-\u9fff]", "", cleaned)
    if len(letters_only) <= 2:
        return "en"

    try:
        detected = detect(cleaned)
        if detected == "vi":
            return "vi"
        if detected == "en":
            return "en"
        if detected.startswith("zh"):
            return "zh"
    except Exception as e:
        print("Lỗi detect language:", e)

    return "en"


def is_noise_message(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return True

    if text.startswith("/"):
        return True

    if text.startswith("http://") or text.startswith("https://"):
        return True

    has_letters = bool(re.search(r"[A-Za-zÀ-ỹ\u4e00-\u9fff]", text))
    if not has_letters:
        return True

    return False


# =========================
# IPA & Pinyin
# =========================
def text_to_ipa(text: str) -> Optional[str]:
    if not text:
        return None

    try:
        ipa_text = ipa.convert(text, keep_punct=True, stress_marks="both")
        ipa_text = ipa_text.replace("*", "")
        ipa_text = normalize_spaces(ipa_text)
        return ipa_text or None
    except Exception as e:
        print("Lỗi chuyển IPA:", e)
        return None


def text_to_pinyin(text: str) -> Optional[str]:
    if not text:
        return None

    try:
        tokens = re.findall(
            r"[\u4e00-\u9fff]+|[A-Za-z0-9]+|\s+|[^\w\s]",
            text,
            flags=re.UNICODE
        )

        out: List[str] = []
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


# =========================
# Core translate logic
# =========================
def translate_text(text: str) -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return None

    source_lang = detect_input_language(text)

    # VI -> EN + ZH
    if source_lang == "vi":
        translated_en = safe_translate(text, "en")
        translated_zh = safe_translate(text, "zh-CN")

        if not translated_en and not translated_zh:
            return None

        return {
            "source_lang": "vi",
            "en": translated_en,
            "ipa": text_to_ipa(translated_en) if translated_en else None,
            "zh": translated_zh,
            "pinyin": text_to_pinyin(translated_zh) if translated_zh else None,
            "vi": None,
        }

    # ZH -> EN + VI
    if source_lang == "zh":
        translated_en = safe_translate(text, "en")
        translated_vi = safe_translate(text, "vi")

        if not translated_en and not translated_vi:
            return None

        return {
            "source_lang": "zh",
            "en": translated_en,
            "ipa": text_to_ipa(translated_en) if translated_en else None,
            "zh": None,
            "pinyin": text_to_pinyin(text),
            "vi": translated_vi,
        }

    # EN -> VI + ZH
    translated_vi = safe_translate(text, "vi")
    translated_zh = safe_translate(text, "zh-CN")

    if not translated_vi and not translated_zh:
        return None

    return {
        "source_lang": "en",
        "en": None,
        "ipa": text_to_ipa(text),
        "zh": translated_zh,
        "pinyin": text_to_pinyin(translated_zh) if translated_zh else None,
        "vi": translated_vi,
    }


# =========================
# Output format
# =========================
def get_user_mention(user: types.User) -> str:
    """
    Ưu tiên @username.
    Nếu không có username thì mention click được.
    """
    if getattr(user, "username", None):
        return f"@{html.escape(user.username)}"

    display_name = html.escape(user.first_name or "User")
    return f'<a href="tg://user?id={user.id}">{display_name}</a>'


def format_reply(message: types.Message, data: Dict[str, Any]) -> str:
    mention = get_user_mention(message.from_user)

    vi = html.escape(data.get("vi") or "")
    en = html.escape(data.get("en") or "")
    ipa_text = html.escape(data.get("ipa") or "")
    zh = html.escape(data.get("zh") or "")
    pinyin = html.escape(data.get("pinyin") or "")
    source_lang = data.get("source_lang")

    lines: List[str] = [mention]

    # Người dùng gửi tiếng Việt -> hiện EN + IPA + ZH + pinyin
    if source_lang == "vi":
        if en:
            lines.append(f"🇬🇧: {en}")
        if ipa_text:
            lines.append(f"🔊: /{ipa_text}/")
        if zh:
            lines.append(f"🇨🇳: {zh}")
        if pinyin:
            lines.append(f"abc: {pinyin}")
        return "\n".join(lines)

    # Người dùng gửi tiếng Trung -> hiện pinyin + EN + IPA + VI
    if source_lang == "zh":
        if pinyin:
            lines.append(f"abc: {pinyin}")
        if en:
            lines.append(f"🇬🇧: {en}")
        if ipa_text:
            lines.append(f"🔊: /{ipa_text}/")
        if vi:
            lines.append(f"🇻🇳: {vi}")
        return "\n".join(lines)

    # Người dùng gửi tiếng Anh -> hiện VI + ZH + pinyin + IPA
    if vi:
        lines.append(f"🇻🇳: {vi}")
    if zh:
        lines.append(f"🇨🇳: {zh}")
    if pinyin:
        lines.append(f"abc: {pinyin}")
    if ipa_text:
        lines.append(f"🔊: /{ipa_text}/")

    return "\n".join(lines)


def trim_telegram_message(text: str, max_len: int = 3500) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "\n\n...[message truncated]"


# =========================
# Telegram handler
# =========================
@bot.message_handler(content_types=["text"])
def handle_message(message: types.Message):
    try:
        if message.from_user and message.from_user.is_bot:
            return

        if message.chat.type not in ["group", "supergroup"]:
            return

        text = (message.text or "").strip()
        if not text or is_noise_message(text):
            return

        result = translate_text(text)
        if not result:
            print("Không dịch được:", repr(text[:200]))
            return

        reply_text = format_reply(message, result)
        reply_text = trim_telegram_message(reply_text)

        bot.send_message(
            chat_id=message.chat.id,
            text=reply_text,
            reply_to_message_id=message.message_id,
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    except Exception as e:
        print("Lỗi handle_message:", e)


# =========================
# Flask routes
# =========================
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
