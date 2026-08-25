"""
Lớp dịch chịu lỗi cao — dùng chung cho cả 2 bot.

Vì sao cần file này:
    deep-translator.GoogleTranslator scrape HTML của translate.google.com/m.
    Khi Google chặn hoặc lỗi (rất hay gặp với IP datacenter như Render), trang
    trả về là trang lỗi HTML. Bộ parse lấy text của trang đó và trả về như thể
    đó là bản dịch, nên bot đăng nguyên câu

        "Error 500 (Server Error)!!1500.That's an error.There was an error.
         Please try again later.That's all we know."

    lên group.

Cách xử lý ở đây:
    1. Ưu tiên các endpoint trả JSON (translate_a). Khi bị chặn, JSON parse
       hỏng -> ném exception, KHÔNG bao giờ biến thành "bản dịch giả".
    2. Nhiều nguồn dự phòng, retry có backoff + jitter, có deadline tổng.
    3. Chốt chặn cuối: looks_like_error_page() lọc mọi kết quả trước khi trả về.
"""

import json
import random
import re
import threading
import time
from functools import lru_cache

import requests


class TranslationError(Exception):
    """Không lấy được bản dịch hợp lệ từ bất kỳ nguồn nào."""


# =========================
# Chốt chặn: nhận diện trang lỗi
# =========================
# Các mẫu này chỉ khớp trang lỗi/chặn của Google, không khớp câu người dùng
# bình thường (một câu chứa "please try again later" đơn thuần vẫn qua được).
_ERROR_PAGE_PATTERNS = [
    re.compile(r"error\s+5\d\d\s*\(server\s+error\)", re.I),
    re.compile(r"that[''']s an error.{0,200}that[''']s all we know", re.I | re.S),
    re.compile(r"!!1\s*\d{3}\.", re.I),
    re.compile(r"our systems have detected unusual traffic", re.I),
    re.compile(r"unusual traffic from your computer network", re.I),
    re.compile(r"<\s*(html|head|body|script|div)\b", re.I),
    re.compile(r"^\s*sorry\.\.\.\s*$", re.I),
    re.compile(r"\berror\s+(4\d\d|5\d\d)\b.{0,80}\bthat[''']s an error\b", re.I | re.S),
]


def looks_like_error_page(text: str) -> bool:
    """True nếu chuỗi trông như trang lỗi/chặn chứ không phải bản dịch."""
    if not text:
        return False
    return any(p.search(text) for p in _ERROR_PAGE_PATTERNS)


# =========================
# HTTP session + throttle
# =========================
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

_session = requests.Session()

REQUEST_TIMEOUT = 8       # giây, cho mỗi HTTP request
TRIES_PER_PROVIDER = 2    # số lần thử lại trên mỗi nguồn
CHUNK_DEADLINE = 25       # giây, tổng thời gian tối đa cho một đoạn text
MIN_CALL_INTERVAL = 0.15  # giây, giãn cách tối thiểu giữa 2 request để né 429

_throttle_lock = threading.Lock()
_last_call_at = 0.0


def _throttle() -> None:
    global _last_call_at
    with _throttle_lock:
        wait = MIN_CALL_INTERVAL - (time.monotonic() - _last_call_at)
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def _headers() -> dict:
    return {
        "User-Agent": random.choice(_UA_POOL),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _get_json(url: str, params: dict):
    _throttle()
    resp = _session.get(
        url, params=params, headers=_headers(), timeout=REQUEST_TIMEOUT
    )
    if resp.status_code != 200:
        raise TranslationError(f"HTTP {resp.status_code} từ {url}")

    body = resp.text
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        # Bị chặn/lỗi -> Google trả HTML. Đây chính là chỗ mà bản cũ
        # nhầm thành "bản dịch".
        raise TranslationError(f"Phản hồi không phải JSON từ {url}: {body[:120]!r}")


# =========================
# Các nguồn dịch
# =========================
def _provider_clients5(text: str, target: str, source: str) -> str:
    """clients5.google.com — client dict-chrome-ex, ít bị rate-limit nhất."""
    data = _get_json(
        "https://clients5.google.com/translate_a/t",
        {"client": "dict-chrome-ex", "sl": source, "tl": target, "q": text},
    )
    # Dạng: [["bản dịch", "ngôn ngữ nguồn"]]
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, list) and first and isinstance(first[0], str):
            return first[0]
        if isinstance(first, str):
            return "".join(x for x in data if isinstance(x, str))
    raise TranslationError(f"Cấu trúc phản hồi lạ từ clients5: {str(data)[:120]}")


def _provider_googleapis(text: str, target: str, source: str) -> str:
    """translate.googleapis.com — trả mảng theo từng câu, phải nối lại."""
    data = _get_json(
        "https://translate.googleapis.com/translate_a/single",
        {
            "client": "dict-chrome-ex",
            "sl": source,
            "tl": target,
            "dt": "t",
            "q": text,
        },
    )
    # Dạng: [[["câu 1 đã dịch", "câu 1 gốc", ...], [...]], null, "src", ...]
    if isinstance(data, list) and data and isinstance(data[0], list):
        parts = [
            seg[0]
            for seg in data[0]
            if isinstance(seg, list) and seg and isinstance(seg[0], str)
        ]
        if parts:
            return "".join(parts)
    raise TranslationError(f"Cấu trúc phản hồi lạ từ googleapis: {str(data)[:120]}")


def _provider_deep_translator(text: str, target: str, source: str) -> str:
    """Nguồn cũ. Giữ lại làm dự phòng nhưng bọc chốt chặn trang lỗi."""
    try:
        from deep_translator import GoogleTranslator
    except ImportError as e:
        raise TranslationError(f"Thiếu deep-translator: {e}")

    out = GoogleTranslator(source=source, target=target).translate(text)
    if not out:
        raise TranslationError("deep-translator trả về rỗng")
    return out


def _mymemory_source(text: str, source: str) -> str:
    """MyMemory không hỗ trợ 'auto', phải đưa mã ngôn ngữ cụ thể."""
    if source and source != "auto":
        return source
    if re.search(r"[一-鿿]", text):
        return "zh-CN"
    try:
        from langdetect import detect

        detected = detect(text)
    except Exception:
        return "en"
    if detected.startswith("zh"):
        return "zh-CN"
    return detected if detected in {"en", "vi"} else "en"


def _provider_mymemory(text: str, target: str, source: str) -> str:
    """Dự phòng cuối, khác hạ tầng Google nên vẫn chạy khi Google chặn hết."""
    src = _mymemory_source(text, source)
    if src == target:
        return text

    data = _get_json(
        "https://api.mymemory.translated.net/get",
        {"q": text, "langpair": f"{src}|{target}"},
    )
    if not isinstance(data, dict):
        raise TranslationError("MyMemory: phản hồi không hợp lệ")
    if str(data.get("responseStatus")) != "200":
        raise TranslationError(f"MyMemory lỗi: {data.get('responseDetails')}")

    out = (data.get("responseData") or {}).get("translatedText")
    if not out:
        raise TranslationError("MyMemory trả về rỗng")
    # MyMemory hay nhét thông báo lỗi vào chính trường translatedText.
    if "MYMEMORY WARNING" in out.upper() or "QUERY LENGTH LIMIT" in out.upper():
        raise TranslationError(f"MyMemory từ chối: {out[:120]}")
    return out


_PROVIDERS = (
    ("clients5", _provider_clients5),
    ("googleapis", _provider_googleapis),
    ("deep_translator", _provider_deep_translator),
    ("mymemory", _provider_mymemory),
)


# =========================
# API chính
# =========================
def _translate_uncached(text: str, target: str, source: str) -> str:
    deadline = time.monotonic() + CHUNK_DEADLINE
    last_error = None

    for name, provider in _PROVIDERS:
        for attempt in range(TRIES_PER_PROVIDER):
            if time.monotonic() >= deadline:
                raise TranslationError(f"Hết thời gian chờ. Lỗi cuối: {last_error}")

            try:
                out = (provider(text, target, source) or "").strip()
            except Exception as e:
                last_error = f"{name}: {e}"
                backoff = 0.5 * (2 ** attempt) + random.uniform(0, 0.4)
                if time.monotonic() + backoff < deadline:
                    time.sleep(backoff)
                continue

            if not out:
                last_error = f"{name}: rỗng"
                continue

            # Chốt chặn cuối — không bao giờ để trang lỗi thành bản dịch.
            if looks_like_error_page(out):
                last_error = f"{name}: trả về trang lỗi"
                print(f"[translate] Chặn trang lỗi từ {name}: {out[:100]!r}")
                break  # nguồn này đang hỏng, chuyển nguồn khác luôn

            return out

    raise TranslationError(f"Mọi nguồn đều thất bại. Lỗi cuối: {last_error}")


@lru_cache(maxsize=512)
def _translate_cached(text: str, target: str, source: str) -> str:
    return _translate_uncached(text, target, source)


def translate_chunk(text: str, target: str, source: str = "auto") -> str:
    """Dịch một đoạn text. Ném TranslationError nếu không nguồn nào chạy được.

    Kết quả thành công được cache, thất bại thì không (lru_cache không cache
    exception) nên tin nhắn lỗi sẽ được thử lại ở lần sau.
    """
    text = (text or "").strip()
    if not text:
        return ""
    return _translate_cached(text, target, source)


def safe_translate_chunk(text: str, target: str, source: str = "auto"):
    """Như translate_chunk nhưng trả None thay vì ném lỗi."""
    try:
        return translate_chunk(text, target, source)
    except TranslationError as e:
        print(f"[translate] Không dịch được sang {target}: {e}")
        return None
