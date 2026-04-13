import os
import re
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
    text = normalize_spaces(text)
    return text


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
# IPA helpers
# 1 dòng duy nhất: stress + connected speech
# =========================
EN_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
IPA_VOWELS = set("iyɪeɛæɑɒɔoʊuʊʌəɚɝɜa")

WEAK_FORMS = {
    "a": "ə",
    "an": "ən",
    "am": "əm",
    "and": "ən",
    "are": "ɚ",
    "as": "əz",
    "at": "ət",
    "can": "kən",
    "for": "fɚ",
    "from": "frəm",
    "of": "əv",
    "or": "ɚ",
    "some": "səm",
    "than": "ðən",
    "that": "ðət",
    "them": "ðəm",
    "to": "tə",
    "us": "əs",
    "was": "wəz",
    "you": "jə",
    "your": "jɚ",
}


def clean_ipa_text(text: str) -> str:
    text = (text or "").replace("*", "").strip()
    text = normalize_spaces(text)
    return text


def ipa_for_word(word: str) -> str | None:
    if not word:
        return None
    try:
        out = ipa.convert(word, keep_punct=False, stress_marks="both")
        out = clean_ipa_text(out)
        return out or None
    except Exception as e:
        print("Lỗi IPA word:", e)
        return None


def starts_with_vowel_sound(pron: str) -> bool:
    if not pron:
        return False
    for ch in pron:
        if ch in {"ˈ", "ˌ", " "}:
            continue
        return ch in IPA_VOWELS
    return False


def ends_with_t_or_d(pron: str) -> bool:
    if not pron:
        return False
    pron = pron.rstrip()
    return pron.endswith("t") or pron.endswith("d")


def apply_flap(pron: str, next_pron: str | None) -> str:
    """
    Kiểu Mỹ: t/d trước nguyên âm tiếp theo -> ɾ
    Ví dụ: get it -> gɛɾ ɪt
    """
    if not pron or not next_pron:
        return pron

    if starts_with_vowel_sound(next_pron) and ends_with_t_or_d(pron):
        return pron[:-1] + "ɾ"

    return pron


def weak_form(word: str, next_pron: str | None) -> str | None:
    w = (word or "").lower()

    if w == "the":
        return "ði" if next_pron and starts_with_vowel_sound(next_pron) else "ðə"

    return WEAK_FORMS.get(w)


def should_link(next_pron: str | None) -> bool:
    return bool(next_pron and starts_with_vowel_sound(next_pron))


def text_to_ipa(text: str) -> str | None:
    """
    1 dòng IPA duy nhất:
    - có stress marks
    - có weak forms
    - có flap t/d -> ɾ
    - có ký hiệu nối âm ‿
    """
    if not text:
        return None

    words = EN_WORD_RE.findall(text)
    if not words:
        return None

    base_prons = []
    for word in words:
        pron = ipa_for_word(word)
        if not pron:
            pron = word.lower()
        base_prons.append(pron)

    final_prons = []
    for i, word in enumerate(words):
        curr = base_prons[i]
        next_pron = base_prons[i + 1] if i + 1 < len(base_prons) else None

        reduced = weak_form(word, next_pron)
        if reduced:
            curr = reduced

        curr = apply_flap(curr, next_pron)
        final_prons.append(curr)

    parts = []
    for i, curr in enumerate(final_prons):
        parts.append(curr)
        if i < len(final_prons) - 1:
            next_pron = final_prons[i + 1]
            parts.append("‿" if should_link(next_pron) else " ")

    out = "".join(parts)
    out = clean_ipa_text(out)
    return out or None


# =========================
# Pinyin
# =========================
def text_to_pinyin(text: str) -> str | None:
    """
    Chuyển chữ Hán sang pinyin có dấu.
    Giữ số, chữ Latin, dấu câu cơ bản.
    """
    if not text:
        return None

    try:
        tokens = re.findall(
            r"[\u4e00-\u9fff]+|[A-Za-z0-9]+|\s+|[^\w\s]",
            text,
            flags=re.UNICODE
        )

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


# =========================
# Core translate logic
# =========================
def translate_text(text: str):
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
            "original": text,
            "vi": text,
            "en": translated_en,
            "ipa": text_to_ipa(translated_en) if translated_en else None,
            "zh": translated_zh,
            "pinyin": text_to_pinyin(translated_zh) if translated_zh else None,
        }

    # ZH -> EN + VI
    if source_lang == "zh":
        translated_en = safe_translate(text, "en")
        translated_vi = safe_translate(text, "vi")

        if not translated_en and not translated_vi:
            return None

        return {
            "source_lang": "zh",
            "original": text,
            "zh": text,
            "pinyin": text_to_pinyin(text),
            "en": translated_en,
            "ipa": text_to_ipa(translated_en) if translated_en else None,
            "vi": translated_vi,
        }

    # EN -> VI + ZH
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


# =========================
# Output formatting
# =========================
def format_reply(sender: str, data: dict) -> str:
    source_lang = data.get("source_lang")
    original = data.get("original")
    vi = data.get("vi")
    en = data.get("en")
    ipa_text = data.get("ipa")
    zh = data.get("zh")
    pinyin = data.get("pinyin")

    lines = []

    if source_lang == "vi":
        lines.append(f"🌐 {sender} | VI → EN + ZH")
        lines.append("")
        lines.append(f"🇻🇳 VI: {original}")

        if en:
            lines.append(f"🇺🇸 EN: {en}")
        if ipa_text:
            lines.append(f"🔊 IPA: /{ipa_text}/")

        if zh:
            lines.append(f"🇨🇳 ZH: {zh}")
        if pinyin:
            lines.append(f"🈶 Pinyin: {pinyin}")

        return "\n".join(lines)

    if source_lang == "zh":
        lines.append(f"🌐 {sender} | ZH → EN + VI")
        lines.append("")
        lines.append(f"🇨🇳 ZH: {original}")

        if pinyin:
            lines.append(f"🈶 Pinyin: {pinyin}")

        if en:
            lines.append(f"🇺🇸 EN: {en}")
        if ipa_text:
            lines.append(f"🔊 IPA: /{ipa_text}/")

        if vi:
            lines.append(f"🇻🇳 VI: {vi}")

        return "\n".join(lines)

    lines.append(f"🌐 {sender} | EN → VI + ZH")
    lines.append("")
    lines.append(f"🇺🇸 EN: {original}")

    if ipa_text:
        lines.append(f"🔊 IPA: /{ipa_text}/")

    if vi:
        lines.append(f"🇻🇳 VI: {vi}")

    if zh:
        lines.append(f"🇨🇳 ZH: {zh}")
    if pinyin:
        lines.append(f"🈶 Pinyin: {pinyin}")

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

        sender = message.from_user.first_name or "User"
        reply_text = format_reply(sender, result)
        reply_text = trim_telegram_message(reply_text)

        bot.send_message(
            chat_id=message.chat.id,
            text=reply_text,
            reply_to_message_id=message.message_id
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
