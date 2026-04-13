import os
import re
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

# Regex nhận diện
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

# Cache translator để đỡ khởi tạo lại nhiều lần
TRANSLATOR_CACHE = {}


def get_translator(target: str):
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


def split_text_for_translate(text: str, max_len: int = MAX_TRANSLATE_CHUNK) -> list[str]:
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
    translator = get_translator(target)

    for chunk in chunks:
        translated = translator.translate(chunk)
        if translated:
            translated_parts.append(translated)

    result = "\n".join(translated_parts).strip()
    return result or None


def chinese_with_pinyin(text: str) -> tuple[str | None, str | None]:
    if not text:
        return None, None

    pinyin_parts = []
    for line in text.splitlines():
        if not line.strip():
            pinyin_parts.append("")
            continue
        pinyin_parts.append(" ".join(lazy_pinyin(line, style=Style.TONE)))

    return text, "\n".join(pinyin_parts).strip()


def build_translations(text: str) -> dict:
    source_lang = detect_source_language(text)
    all_targets = [
        ("vi", "🇻🇳 Tiếng Việt"),
        ("en", "🇬🇧 English"),
        ("zh-CN", "🇨🇳 中文"),
    ]

    # Nếu nhận diện chắc nguồn thì loại ngôn ngữ đó ra
    if source_lang in {"vi", "en", "zh-CN"}:
        targets = [item for item in all_targets if item[0] != source_lang]
    else:
        # Nếu chưa chắc thì dịch cả 3, cái nào giống nguyên văn sẽ tự loại
        targets = all_targets

    result = {"source_lang": source_lang, "items": []}
    original_norm = normalize_compare(text)

    for target_code, label in targets:
        try:
            translated = translate_in_chunks(text, target_code)
            if not translated:
                continue

            # Loại bản dịch bị trùng nguyên văn
            if normalize_compare(translated) == original_norm:
                continue

            entry = {
                "target_code": target_code,
                "label": label,
                "text": translated,
            }

            # Nếu output là tiếng Trung thì thêm pinyin
            if target_code == "zh-CN":
                hanzi, pinyin = chinese_with_pinyin(translated)
                if hanzi:
                    entry["text"] = hanzi
                if pinyin:
                    entry["pinyin"] = pinyin

            result["items"].append(entry)

        except Exception as e:
            print(f"Lỗi dịch sang {target_code}:", e)

    return result


def format_output(sender: str, source_lang: str, items: list[dict]) -> str | None:
    if not items:
        return None

    source_label_map = {
        "vi": "VI",
        "en": "EN",
        "zh-CN": "ZH",
        "auto": "AUTO",
    }
    source_label = source_label_map.get(source_lang, "AUTO")

    parts = [f"[{source_label}] {sender}"]

    for item in items:
        parts.append(f"\n{item['label']}\n{item['text']}")
        if item.get("pinyin"):
            parts.append(f"🔤 Pinyin\n{item['pinyin']}")

    final_text = "\n".join(parts).strip()

    if len(final_text) > MAX_TELEGRAM_LEN:
        final_text = final_text[:MAX_TELEGRAM_LEN] + "\n\n...[message truncated]"

    return final_text


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

        sender = message.from_user.first_name or "User"

        payload = build_translations(text)
        output = format_output(sender, payload["source_lang"], payload["items"])

        if not output:
            print("Không dịch được:", repr(text[:200]))
            return

        bot.send_message(
            message.chat.id,
            output,
            reply_to_message_id=message.message_id,
            disable_web_page_preview=True,
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
