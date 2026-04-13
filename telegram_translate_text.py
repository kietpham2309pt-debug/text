import os
import re
from typing import Optional, List, Dict, Tuple

from flask import Flask, request
import telebot
from telebot import types
from deep_translator import GoogleTranslator
from pypinyin import lazy_pinyin, Style

BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}" if RENDER_EXTERNAL_URL else None

MAX_TRANSLATE_CHUNK = 1000
MAX_TELEGRAM_LEN = 3500

# Regex nhận diện ngôn ngữ
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
VI_CHAR_RE = re.compile(
    r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]",
    re.IGNORECASE,
)
VI_WORD_RE = re.compile(
    r"\b(tôi|toi|bạn|ban|mình|minh|của|cua|không|khong|được|duoc|và|la|là|có|co|đi|roi|rồi|anh|chị|chi|em|nhé|nhe|giúp|giup|đang|dang|đây|day|này|nay)\b",
    re.IGNORECASE,
)
EN_WORD_RE = re.compile(
    r"\b(the|and|is|are|am|was|were|you|hello|thanks|thank|please|can|could|would|should|what|when|where|why|how|i|we|they|he|she|do|does|did|have|has|had)\b",
    re.IGNORECASE,
)

TRANSLATOR_CACHE: Dict[str, GoogleTranslator] = {}


def get_translator(target: str) -> GoogleTranslator:
    if target not in TRANSLATOR_CACHE:
        TRANSLATOR_CACHE[target] = GoogleTranslator(source="auto", target=target)
    return TRANSLATOR_CACHE[target]


def normalize_compare(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def has_chinese(text: str) -> bool:
    return bool(CJK_RE.search(text or ""))


def detect_source_language(text: str) -> str:
    """
    Trả về:
    - 'vi'
    - 'en'
    - 'zh-CN'
    - 'auto' nếu chưa chắc
    """
    cleaned = (text or "").strip()

    if has_chinese(cleaned):
        return "zh-CN"

    vi_score = len(VI_CHAR_RE.findall(cleaned)) * 3 + len(VI_WORD_RE.findall(cleaned))
    en_score = len(EN_WORD_RE.findall(cleaned))

    if vi_score >= 2 and vi_score >= en_score:
        return "vi"

    if en_score >= 1:
        return "en"

    ascii_letters = sum(1 for ch in cleaned if ch.isascii() and ch.isalpha())
    total_letters = sum(1 for ch in cleaned if ch.isalpha())

    if total_letters > 0 and ascii_letters / total_letters > 0.95:
        return "en"

    return "auto"


def split_text_for_translate(text: str, max_len: int = MAX_TRANSLATE_CHUNK) -> List[str]:
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
    translator = get_translator(target)

    for chunk in chunks:
        translated = translator.translate(chunk)
        if translated:
            translated_parts.append(translated)

    result = "\n".join(translated_parts).strip()
    return result or None


def chinese_with_pinyin(text: str) -> Tuple[Optional[str], Optional[str]]:
    if not text:
        return None, None

    hanzi = text.strip()
    parts: List[str] = []
    buffer: List[str] = []

    def flush_buffer():
        nonlocal buffer
        if buffer:
            chinese_segment = "".join(buffer)
            parts.append(" ".join(lazy_pinyin(chinese_segment, style=Style.TONE)))
            buffer = []

    for ch in hanzi:
        if CJK_RE.match(ch):
            buffer.append(ch)
        else:
            flush_buffer()
            parts.append(ch)

    flush_buffer()

    pinyin = "".join(parts)
    pinyin = re.sub(r"\s+", " ", pinyin).strip()
    pinyin = re.sub(r"\s+([,.;:!?。，！？；：])", r"\1", pinyin)

    return hanzi, pinyin


def build_translations(text: str) -> Dict[str, object]:
    source_lang = detect_source_language(text)
    original_norm = normalize_compare(text)

    if source_lang == "vi":
        target_codes = ["en", "zh-CN"]
    elif source_lang == "en":
        target_codes = ["vi", "zh-CN"]
    elif source_lang == "zh-CN":
        target_codes = ["vi", "en"]
    else:
        target_codes = ["vi", "en", "zh-CN"]

    items: List[Dict[str, str]] = []

    for target_code in target_codes:
        try:
            translated = translate_in_chunks(text, target_code)
            if not translated:
                continue

            if normalize_compare(translated) == original_norm:
                continue

            entry: Dict[str, str] = {
                "target_code": target_code,
                "text": translated.strip(),
            }

            if target_code == "zh-CN":
                hanzi, pinyin = chinese_with_pinyin(translated)
                if hanzi:
                    entry["text"] = hanzi
                if pinyin:
                    entry["pinyin"] = pinyin

            items.append(entry)

        except Exception as e:
            print(f"Lỗi dịch sang {target_code}:", e)

    return {
        "source_lang": source_lang,
        "items": items,
    }


def format_output(items: List[Dict[str, str]]) -> Optional[str]:
    if not items:
        return None

    lines: List[str] = []

    for item in items:
        target_code = item.get("target_code", "")
        text = (item.get("text") or "").strip()
        pinyin = (item.get("pinyin") or "").strip()

        if not text:
            continue

        if target_code == "en":
            lines.append(f"🇬🇧: {text}")
        elif target_code == "vi":
            lines.append(f"🇻🇳: {text}")
        elif target_code == "zh-CN":
            lines.append(f"🇨🇳: {text}")
            if pinyin:
                lines.append(f"🔤: {pinyin}")

    return "\n".join(lines).strip() or None


def split_message_for_telegram(text: str, max_len: int = MAX_TELEGRAM_LEN) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []

    if len(text) <= max_len:
        return [text]

    parts: List[str] = []
    current = ""

    for line in text.split("\n"):
        candidate = f"{current}\n{line}".strip() if current else line

        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                parts.append(current)

            if len(line) <= max_len:
                current = line
            else:
                while len(line) > max_len:
                    parts.append(line[:max_len])
                    line = line[max_len:]
                current = line

    if current:
        parts.append(current)

    return parts


def send_long_message(chat_id: int, text: str, reply_to_message_id: Optional[int] = None):
    chunks = split_message_for_telegram(text)

    for i, chunk in enumerate(chunks):
        bot.send_message(
            chat_id,
            chunk,
            reply_to_message_id=reply_to_message_id if i == 0 else None,
            disable_web_page_preview=True,
        )


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

        # Bỏ qua link và command
        if text.startswith(("http://", "https://", "/")):
            return

        payload = build_translations(text)
        output = format_output(payload["items"])  # type: ignore[index]

        if not output:
            print("Không dịch được:", repr(text[:200]))
            return

        send_long_message(
            chat_id=message.chat.id,
            text=output,
            reply_to_message_id=message.message_id,
        )

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
