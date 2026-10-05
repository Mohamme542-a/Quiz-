# -*- coding: utf-8 -*-
"""
بوت لعبة الأسئلة الدينية (تيليجرام)
═══════════════════════════════════
التثبيت :  pip install -U "python-telegram-bot>=22.7"
التشغيل :  BOT_TOKEN=xxxx ADMIN_IDS=123456789 python quiz_bot.py
          (أو اكتب التوكن والأدمن مباشرة في قسم الإعدادات أدناه)

اللاعب : سؤال + خيارات زرقاء → إجابة صحيحة = مرحلة جديدة، خطأ = خسارة وإعادة من البداية.
الأدمن : /admin → إضافة أسئلة (كتابة أو ملف txt) • صيانة • نسخة احتياطية • استعادة.
"""
import asyncio
import html
import io
import json
import logging
import os
import random
import re
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (BotCommand, BotCommandScopeChat, InlineKeyboardButton,
                      InlineKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler,
                          CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters)

# ══════════════════════════ الإعدادات ══════════════════════════
def _ids(s):
    return [int(x) for x in re.split(r"[,\s]+", s or "") if x.strip().lstrip("-").isdigit()]

BOT_TOKEN = os.getenv("BOT_TOKEN", "8876836562:AAEVyAwsXLXf0aYDQe6RD2UK7t_goYKCi3U")            # أو: BOT_TOKEN = "123456:ABC..."
ADMIN_IDS = [
    # 123456789,      ← اكتب أيدي الأدمن هنا (أو استخدم متغير البيئة ADMIN_IDS)
] + _ids(os.getenv("ADMIN_IDS", "8950382997"))

DATA_DIR = os.getenv("DATA_DIR", "data")          # مجلد حفظ الأسئلة والنتائج
AUTO_BACKUP_HOURS = 24                            # إرسال نسخة احتياطية تلقائية للأدمن كل كم ساعة
MAINTENANCE_TEXT = "البوت متوقف مؤقتاً للصيانة، سنعود قريباً بإذن الله."

MIN_OPTS, MAX_OPTS = 2, 6
MAX_Q_LEN, MAX_OPT_LEN = 600, 100
MAX_FILE_BYTES = 5 * 1024 * 1024
BACKUP_FORMAT = "religious-quiz-backup"
LETTERS = ["أ", "ب", "ج", "د", "هـ", "و"]

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("quiz")
esc = html.escape


# ══════════════════════════ الأزرار الملونة ══════════════════════════
_STYLE_SUPPORTED = None


def IBtn(text, style=None, **kw):
    """زر inline. style: primary (أزرق) / success (أخضر) / danger (أحمر).
    إن كانت نسخة المكتبة قديمة لا تدعم الألوان يُنشأ زر عادي بدل أن يتعطل البوت."""
    global _STYLE_SUPPORTED
    if style and _STYLE_SUPPORTED is not False:
        try:
            b = InlineKeyboardButton(text, style=style, **kw)
            _STYLE_SUPPORTED = True
            return b
        except TypeError:
            _STYLE_SUPPORTED = False
    return InlineKeyboardButton(text, **kw)


# ══════════════════════════ أدوات النصوص ══════════════════════════
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_TASHKEEL = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")
_ALEF = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})


def norm_key(text):
    """مفتاح مقارنة (لكشف تكرار الأسئلة والخيارات): بدون تشكيل ولا علامات ترقيم."""
    t = _TASHKEEL.sub("", str(text)).translate(_ALEF).lower()
    return re.sub(r"[^\w]+", "", t)


def short(text, n=38):
    t = re.sub(r"\s+", " ", str(text)).strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def decode_text(raw: bytes) -> str:
    """يقرأ ملف نصي بأي ترميز شائع (UTF-8 / UTF-16 / ويندوز العربي)."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1256")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


# ══════════════════════════ محلل الأسئلة النصية ══════════════════════════
_OPT_PREFIX = re.compile(r"^\s*[\(\[]?\s*(?P<k>هـ|[أاإبتثجحدهوزA-Fa-f1-6])\s*[\)\]\.\-–—:،]\s*(?P<rest>.*)$")
_OPT_FIRST = {"أ", "ا", "إ", "a", "A", "1"}      # القائمة المرقّمة يجب أن تبدأ بأحدها
_Q_PREFIX = re.compile(r"^\s*(?:(?:السؤال|سؤال|س|Q|Question)\s*\d*\s*[:：\-\.\)]|\d+\s*[\.\)\-:]|\(\d+\))\s*", re.I)
_ANS = re.compile(r"^\s*(?:الجواب(?:\s+الصحيح)?|الإجابة(?:\s+الصحيحة)?|الاجابة(?:\s+الصحيحة)?|الصحيح|answer|ans|correct)"
                  r"\s*[:：\-]\s*(?P<v>.+?)\s*$", re.I)
_MARK = r"(?:[\*✓✔☑✅]+|\(\s*(?:صح|صحيح|صحيحة|correct)\s*\))"
_MARK_END = re.compile(r"\s*" + _MARK + r"\s*$", re.I)
_MARK_START = re.compile(r"^\s*" + _MARK + r"\s*", re.I)
_BULLET = re.compile(r"^[\-•–—]\s+")


def _nk(k):
    k = k.translate(AR_DIGITS).strip().lower()
    return {"ا": "أ", "إ": "أ", "ه": "هـ"}.get(k, k)


def _resolve_answer(spec, opts, keys):
    s = spec.translate(AR_DIGITS).strip().strip("()[].:-– ")
    nk = _nk(s)
    if keys and nk in keys:                       # نفس رموز الخيارات المكتوبة في الملف
        return keys.index(nk)
    order = ["أ", "ب", "ج", "د", "هـ", "و", "ز"]
    if nk in order and order.index(nk) < len(opts):
        return order.index(nk)
    if len(nk) == 1 and nk.isascii() and nk.isalpha() and 0 <= ord(nk) - 97 < len(opts):
        return ord(nk) - 97
    if s.isdigit() and 1 <= int(s) <= len(opts):
        return int(s) - 1
    key = norm_key(s)
    for i, o in enumerate(opts):                  # الإجابة مكتوبة بنصها
        if key and norm_key(o) == key:
            return i
    raise ValueError(f"تعذّر فهم الإجابة «{spec}»")


def _parse_block(raw_lines):
    lines = [l.strip() for l in raw_lines if l.strip()]
    ans_spec, body = None, []
    for l in lines:
        m = _ANS.match(l)
        if m and body and ans_spec is None:
            ans_spec = m.group("v").strip()
        else:
            body.append(l)
    if len(body) < 1 + MIN_OPTS:
        raise ValueError("يلزم نص السؤال + خيارين على الأقل")

    pref = []
    for l in body:
        m = _OPT_PREFIX.match(l.translate(AR_DIGITS))
        pref.append((m.group("k"), l[m.start("rest"):]) if m else None)
    first = next((i for i in range(1, len(body)) if pref[i] and pref[i][0] in _OPT_FIRST), None)
    prefixed = first is not None and sum(1 for p in pref[first:] if p) >= 2

    keys = None
    if prefixed:
        question = " ".join(body[:first])
        opts, keys = [], []
        for i in range(first, len(body)):
            if pref[i]:
                opts.append(pref[i][1].strip()); keys.append(_nk(pref[i][0]))
            else:                                  # سطر مكمّل للخيار السابق
                opts[-1] += " " + body[i]
    else:
        question = body[0]
        opts = [_BULLET.sub("", l) for l in body[1:]]
    question = re.sub(r"\s+", " ", _Q_PREFIX.sub("", question)).strip()

    starts = [bool(_MARK_START.match(o)) for o in opts]
    ignore_start = len(opts) > 1 and all(starts)    # قائمة نقطية كاملة بـ * وليست علامة صح
    clean, marks = [], []
    for o, st in zip(opts, starts):
        end = bool(_MARK_END.search(o))
        t = _MARK_END.sub("", o)
        if st:
            t = _MARK_START.sub("", t)
        clean.append(re.sub(r"\s+", " ", t).strip())
        marks.append(end or (st and not ignore_start))

    if not question:
        raise ValueError("نص السؤال فارغ")
    if not (MIN_OPTS <= len(clean) <= MAX_OPTS):
        raise ValueError(f"عدد الخيارات {len(clean)} (المسموح {MIN_OPTS} إلى {MAX_OPTS})")
    if any(not o for o in clean):
        raise ValueError("يوجد خيار فارغ")
    if len(question) > MAX_Q_LEN:
        raise ValueError(f"السؤال أطول من {MAX_Q_LEN} حرفاً")
    if any(len(o) > MAX_OPT_LEN for o in clean):
        raise ValueError(f"أحد الخيارات أطول من {MAX_OPT_LEN} حرفاً")
    if len({norm_key(o) for o in clean}) != len(clean):
        raise ValueError("يوجد خياران متطابقان")

    n = sum(marks)
    if n > 1:
        raise ValueError("أكثر من إجابة صحيحة (يجب واحدة فقط)")
    correct = marks.index(True) if n == 1 else None
    if ans_spec:
        idx = _resolve_answer(ans_spec, clean, keys)
        if correct is not None and idx != correct:
            raise ValueError("تعارض بين علامة * وسطر الجواب")
        correct = idx
    if correct is None:
        raise ValueError("لم تُحدَّد الإجابة الصحيحة (ضع * بجانبها أو أضف سطر «الجواب: ب»)")
    return {"q": question, "options": clean, "correct": correct}


def parse_questions(text):
    """يحوّل نصاً (عدة أسئلة بينها سطر فارغ أو ---) إلى (أسئلة صالحة, قائمة أخطاء)."""
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    blocks, cur = [], []
    for line in text.split("\n"):
        if not line.strip() or re.fullmatch(r"\s*[-=_*#~]{3,}\s*", line):
            if cur:
                blocks.append(cur); cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)
    good, errors = [], []
    for i, b in enumerate(blocks, 1):
        try:
            good.append(_parse_block(b))
        except ValueError as e:
            errors.append(f"#{i} «{short(b[0].strip(), 30)}»: {e}")
    return good, errors


def export_txt(questions):
    """نفس الصيغة التي يقرؤها البوت (قابلة لإعادة الاستيراد)."""
    out = []
    for q in questions:
        lines = [re.sub(r"\s+", " ", q["q"])]
        for i, o in enumerate(q["options"]):
            lines.append(f"{LETTERS[i]}) {o}" + (" *" if i == q["correct"] else ""))
        out.append("\n".join(lines))
    return "\n\n".join(out) + "\n"


# ══════════════════════════ التخزين ══════════════════════════
DEFAULT_SETTINGS = {"maintenance": False, "shuffle_questions": True, "auto_backup": True}
STAT_KEYS = ("best", "plays", "wins", "losses", "correct", "last")


def _now():
    return int(time.time())


def new_user():
    return {"name": "", "best": 0, "plays": 0, "wins": 0, "losses": 0, "correct": 0,
            "stage": 0, "asked": [], "cur": None, "last": 0}


def clean_user(d, keep_run=False):
    u = new_user()
    u["name"] = str(d.get("name", ""))[:64]
    for k in STAT_KEYS:
        try:
            u[k] = max(0, int(d.get(k, 0)))
        except (TypeError, ValueError):
            u[k] = 0
    if keep_run:
        try:
            u["stage"] = max(0, int(d.get("stage", 0)))
            u["asked"] = [int(x) for x in d.get("asked", [])]
            c = d.get("cur")
            if isinstance(c, dict):
                u["cur"] = {"n": int(c["n"]), "qid": int(c["qid"]),
                            "perm": [int(x) for x in c["perm"]],
                            "mid": int(c["mid"]) if c.get("mid") is not None else None}
        except (TypeError, ValueError, KeyError):
            u["stage"], u["asked"], u["cur"] = 0, [], None
    return u


def clean_question(d, qid):
    try:
        q = str(d["q"]).strip()
        opts = [str(o).strip() for o in d["options"]]
        c = int(d["correct"])
    except (KeyError, TypeError, ValueError):
        return None
    if not q or not (MIN_OPTS <= len(opts) <= MAX_OPTS) or any(not o for o in opts) or not (0 <= c < len(opts)):
        return None
    try:
        added = int(d.get("added") or 0)
    except (TypeError, ValueError):
        added = 0
    return {"id": qid, "q": q, "options": opts, "correct": c, "added": added}


def _atomic_write(path, data: bytes):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


class Store:
    def __init__(self, folder):
        self.dir = folder
        os.makedirs(folder, exist_ok=True)
        self.qpath = os.path.join(folder, "quiz.json")
        self.upath = os.path.join(folder, "users.json")
        self.questions, self.settings = [], dict(DEFAULT_SETTINGS)
        self.next_id, self.meta, self.users, self.dirty = 1, {}, {}, False

        quiz = self._load(self.qpath)
        if isinstance(quiz, dict):
            for d in quiz.get("questions", []):
                c = clean_question(d, int(d.get("id", 0) or 0)) if isinstance(d, dict) else None
                if c and c["id"] > 0:
                    self.questions.append(c)
            for k in DEFAULT_SETTINGS:
                if isinstance(quiz.get("settings", {}).get(k), bool):
                    self.settings[k] = quiz["settings"][k]
            self.meta = quiz.get("meta", {}) if isinstance(quiz.get("meta"), dict) else {}
            self.next_id = int(quiz.get("next_id", 1) or 1)
        users = self._load(self.upath)
        if isinstance(users, dict):
            for uid, d in users.items():
                if isinstance(d, dict):
                    self.users[str(uid)] = clean_user(d, keep_run=True)
        self._index()

    # ---- ملفات ----
    def _load(self, path):
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            bad = f"{path}.corrupt-{_now()}"
            os.replace(path, bad)
            log.error("ملف تالف %s (%s) — نُقل إلى %s", path, e, bad)
            return None

    def _index(self):
        self.by_id = {q["id"]: q for q in self.questions}
        self.next_id = max([self.next_id] + [q["id"] + 1 for q in self.questions])

    def save_quiz(self):
        data = {"next_id": self.next_id, "questions": self.questions,
                "settings": self.settings, "meta": self.meta}
        _atomic_write(self.qpath, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))

    def flush(self):
        if self.dirty:
            self.dirty = False
            _atomic_write(self.upath, json.dumps(self.users, ensure_ascii=False).encode("utf-8"))

    # ---- أسئلة ----
    def add_questions(self, items):
        seen = {norm_key(q["q"]) for q in self.questions}
        added, dups = [], 0
        for it in items:
            k = norm_key(it["q"])
            if k in seen:
                dups += 1; continue
            seen.add(k)
            q = {"id": self.next_id, "q": it["q"], "options": list(it["options"]),
                 "correct": it["correct"], "added": _now()}
            self.next_id += 1
            self.questions.append(q); added.append(q)
        self._index()
        if added:
            self.save_quiz()
        return added, dups

    def delete(self, qid):
        n = len(self.questions)
        self.questions = [q for q in self.questions if q["id"] != qid]
        self._index()
        self.save_quiz()
        return len(self.questions) < n

    def wipe_questions(self):
        self.questions = []
        self._index()
        self.save_quiz()
        for u in self.users.values():
            u["stage"], u["asked"], u["cur"] = 0, [], None
        self.dirty = True

    # ---- لاعبون ----
    def user(self, uid, name=None):
        u = self.users.get(str(uid))
        if u is None:
            u = self.users[str(uid)] = new_user()
        if name:
            u["name"] = name[:64]
        u["last"] = _now()
        self.dirty = True
        return u

    # ---- نسخ احتياطي ----
    def build_backup(self) -> bytes:
        obj = {"format": BACKUP_FORMAT, "version": 1,
               "created": datetime.now().isoformat(timespec="seconds"),
               "quiz": {"next_id": self.next_id, "questions": self.questions, "settings": self.settings},
               "users": {uid: {"name": u["name"], **{k: u[k] for k in STAT_KEYS}} for uid, u in self.users.items()}}
        return json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")

    def apply_backup(self, parsed):
        """استعادة كاملة: تستبدل الأسئلة والإعدادات والنتائج (حالة الصيانة الحالية تبقى كما هي)."""
        keep_maint = self.settings["maintenance"]
        self.questions = parsed["questions"]
        self.next_id = max(parsed["next_id"], 1)
        self.settings = {**DEFAULT_SETTINGS, **parsed["settings"], "maintenance": keep_maint}
        self.users = parsed["users"]
        self._index()
        self.save_quiz()
        self.dirty = True
        self.flush()


def validate_backup(obj):
    """يتحقق من ملف النسخة الاحتياطية وينظّفه، أو يرمي ValueError برسالة مفهومة."""
    if not isinstance(obj, dict) or obj.get("format") != BACKUP_FORMAT:
        raise ValueError("هذا ليس ملف نسخة احتياطية صالحاً لهذا البوت.")
    quiz = obj.get("quiz")
    if not isinstance(quiz, dict) or not isinstance(quiz.get("questions"), list):
        raise ValueError("ملف النسخة الاحتياطية تالف (لا يحتوي قائمة أسئلة).")
    questions, used, pending = [], set(), []
    for d in quiz["questions"]:
        c = clean_question(d, 0) if isinstance(d, dict) else None
        if not c:
            continue
        try:
            i = int(d.get("id"))
        except (TypeError, ValueError):
            i = 0
        if i <= 0 or i in used:
            pending.append(c)
        else:
            c["id"] = i; used.add(i); questions.append(c)
    try:
        nxt = int(quiz.get("next_id") or 1)
    except (TypeError, ValueError):
        nxt = 1
    nxt = max([nxt] + [i + 1 for i in used])
    for c in pending:
        c["id"] = nxt; used.add(nxt); nxt += 1; questions.append(c)
    if not questions:
        raise ValueError("النسخة الاحتياطية لا تحتوي أي سؤال صالح.")
    st = quiz.get("settings") if isinstance(quiz.get("settings"), dict) else {}
    settings = {k: v for k, v in st.items() if k in DEFAULT_SETTINGS and isinstance(v, bool)}
    users = {}
    if isinstance(obj.get("users"), dict):
        for uid, d in obj["users"].items():
            if str(uid).lstrip("-").isdigit() and isinstance(d, dict):
                users[str(uid)] = clean_user(d)
    return {"questions": questions, "next_id": nxt, "settings": settings, "users": users,
            "created": str(obj.get("created", ""))[:19]}


store: Store = None   # يُنشأ في main()


def is_admin(uid):
    return uid in ADMIN_IDS


# ══════════════════════════ واجهة اللاعب ══════════════════════════
def menu_text(u):
    return ("<b>لعبة الأسئلة الدينية</b>\n"
            "━━━━━━━━━━━━━━\n"
            "أجب عن الأسئلة مرحلة بعد مرحلة؛ كل إجابة صحيحة تنقلك للمرحلة التالية، "
            "وأي خطأ يعيدك من البداية.\n\n"
            f"عدد الأسئلة: <b>{len(store.questions)}</b>\n"
            f"أفضل نتيجة لك: <b>{u['best']}</b> مرحلة")


def kb_main(uid, u):
    rows = []
    if u.get("cur"):
        rows.append([IBtn("متابعة اللعبة", callback_data="u:resume", style="success")])
        rows.append([IBtn("ابدأ من جديد", callback_data="u:play", style="primary")])
    else:
        rows.append([IBtn("ابدأ اللعبة", callback_data="u:play", style="success")])
    rows.append([IBtn("المتصدرون", callback_data="u:top", style="primary"),
                 IBtn("إحصائياتي", callback_data="u:me", style="primary")])
    if is_admin(uid):
        rows.append([IBtn("لوحة الأدمن", callback_data="ad:menu")])
    return InlineKeyboardMarkup(rows)


def kb_back():
    return InlineKeyboardMarkup([[IBtn("القائمة الرئيسية", callback_data="u:menu")]])


def question_text(stage, q, footer=""):
    return (f"<b>المرحلة {stage + 1}</b> من {len(store.questions)}\n"
            f"━━━━━━━━━━━━━━\n{esc(q['q'])}{footer}")


def question_kb(cur, q):
    rows = [[IBtn(q["options"][oi], callback_data=f"a:{cur['n']}:{pos}", style="primary")]
            for pos, oi in enumerate(cur["perm"])]
    rows.append([IBtn("إنهاء اللعبة", callback_data="u:quit")])
    return InlineKeyboardMarkup(rows)


def result_kb(q, perm, chosen_pos, correct_pos):
    rows = []
    for pos, oi in enumerate(perm):
        label, style = q["options"][oi], None
        if pos == correct_pos:
            label, style = "✅ " + label, "success"
        elif pos == chosen_pos:
            label, style = "❌ " + label, "danger"
        rows.append([IBtn(label, callback_data="noop", style=style)])
    return InlineKeyboardMarkup(rows)


async def safe_edit(query, text, markup=None):
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return True
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.debug("edit failed: %s", e)
        return False


def pick_question(u):
    asked = set(u["asked"])
    pool = [q for q in store.questions if q["id"] not in asked]
    if not pool:
        return None
    return random.choice(pool) if store.settings["shuffle_questions"] else pool[0]


async def render_question(bot, chat_id, u, query=None):
    """يعرض السؤال الحالي (u['cur']) — بتعديل رسالة موجودة أو بإرسال رسالة جديدة."""
    cur = u["cur"]
    q = store.by_id[cur["qid"]]
    text, kb = question_text(u["stage"], q), question_kb(cur, q)
    mid = None
    if query is not None and await safe_edit(query, text, kb):
        mid = query.message.message_id
    if mid is None:
        msg = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        mid = msg.message_id
    cur["mid"] = mid
    store.dirty = True


async def send_question(bot, chat_id, u, query=None):
    q = pick_question(u)
    if q is None:
        return await finish_win(bot, chat_id, u, query)
    perm = list(range(len(q["options"])))
    random.shuffle(perm)                      # ترتيب الخيارات يتغير كل مرة
    u["cur"] = {"n": random.randint(100000, 999999), "qid": q["id"], "perm": perm, "mid": None}
    await render_question(bot, chat_id, u, query)


def reset_run(u):
    u["stage"], u["asked"], u["cur"] = 0, [], None
    store.dirty = True


async def finish_win(bot, chat_id, u, query=None):
    reached = u["stage"]
    u["wins"] += 1
    u["best"] = max(u["best"], reached)
    reset_run(u)
    text = (f"<b>ما شاء الله، أنهيت جميع المراحل!</b>\n━━━━━━━━━━━━━━\n"
            f"أجبت عن <b>{reached}</b> سؤالاً بشكل صحيح دون أي خطأ.\n"
            "سنضيف أسئلة جديدة قريباً بإذن الله.")
    kb = InlineKeyboardMarkup([[IBtn("العب من جديد", callback_data="u:play", style="success")],
                               [IBtn("القائمة الرئيسية", callback_data="u:menu")]])
    if query is not None and await safe_edit(query, text, kb):
        return
    await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)


def leaderboard_text(uid):
    ranked = sorted((u for u in store.users.values() if u["best"] > 0),
                    key=lambda u: (-u["best"], -u["wins"], u["last"]))
    if not ranked:
        return "<b>المتصدرون</b>\n━━━━━━━━━━━━━━\nلا توجد نتائج بعد. كن أول المتصدرين!"
    lines = ["<b>المتصدرون</b>", "━━━━━━━━━━━━━━"]
    for i, u in enumerate(ranked[:10], 1):
        lines.append(f"{i}. {esc(short(u['name'] or 'لاعب', 20))} — <b>{u['best']}</b> مرحلة")
    me = store.users.get(str(uid))
    if me and me["best"] > 0:
        rank = next(i for i, x in enumerate(ranked, 1) if x is me)   # بالهوية لا بالتساوي
        if rank > 10:
            lines.append(f"…\nترتيبك: {rank} — <b>{me['best']}</b> مرحلة")
    return "\n".join(lines)


def stats_text(u):
    return ("<b>إحصائياتي</b>\n━━━━━━━━━━━━━━\n"
            f"أفضل نتيجة: <b>{u['best']}</b> مرحلة\n"
            f"عدد المحاولات: {u['plays']}\n"
            f"إجابات صحيحة: {u['correct']}\n"
            f"الخسارات: {u['losses']}\n"
            f"مرات إنهاء كل المراحل: {u['wins']}")


async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("mode", None)
    usr = update.effective_user
    u = store.user(usr.id, usr.first_name or usr.username)
    await update.message.reply_text(menu_text(u), parse_mode=ParseMode.HTML, reply_markup=kb_main(usr.id, u))


async def on_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    usr = update.effective_user
    chat_id = q.message.chat_id
    try:
        _, n, pos = q.data.split(":")
        n, pos = int(n), int(pos)
    except ValueError:
        await q.answer(); return
    u = store.user(usr.id, usr.first_name or usr.username)
    cur = u.get("cur")
    if not cur or cur["n"] != n:                          # رسالة قديمة / ضغط مزدوج
        await q.answer("انتهى هذا السؤال")
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        return
    qq = store.by_id.get(cur["qid"])
    if qq is None or not (0 <= pos < len(cur["perm"])):   # السؤال حُذف من الأدمن أثناء اللعب
        u["cur"] = None
        await q.answer("تم تحديث الأسئلة")
        await send_question(ctx.bot, chat_id, u, q)
        return

    await q.answer()
    correct_pos = cur["perm"].index(qq["correct"])
    ok = pos == correct_pos
    perm, stage = cur["perm"], u["stage"]
    u["cur"] = None                                       # يمنع احتساب أكثر من ضغطة
    if ok:
        u["stage"] += 1
        u["asked"].append(qq["id"])
        u["correct"] += 1
        u["best"] = max(u["best"], u["stage"])
        store.dirty = True
        await safe_edit(q, question_text(stage, qq, "\n\n<b>✅ إجابة صحيحة</b>"),
                        result_kb(qq, perm, pos, correct_pos))
        await send_question(ctx.bot, chat_id, u)
    else:
        u["losses"] += 1
        reset_run(u)
        right = esc(qq["options"][qq["correct"]])
        await safe_edit(q, question_text(stage, qq, "\n\n<b>❌ إجابة خاطئة</b>"),
                        result_kb(qq, perm, pos, correct_pos))
        await ctx.bot.send_message(
            chat_id,
            f"<b>خسرت هذه الجولة</b>\n━━━━━━━━━━━━━━\n"
            f"الإجابة الصحيحة: <b>{right}</b>\n"
            f"توقفت عند المرحلة {stage + 1} بعد {stage} إجابة صحيحة.\n"
            f"أفضل نتيجة لك: <b>{u['best']}</b> مرحلة",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [IBtn("حاول من جديد", callback_data="u:play", style="success")],
                [IBtn("القائمة الرئيسية", callback_data="u:menu")]]))


async def on_user_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    if data.startswith("a:"):
        return await on_answer(update, ctx)
    usr = update.effective_user
    u = store.user(usr.id, usr.first_name or usr.username)
    chat_id = q.message.chat_id

    if data == "noop":
        await q.answer(); return
    if data == "u:play":
        if not store.questions:
            await q.answer("لا توجد أسئلة حالياً، عد لاحقاً.", show_alert=True); return
        await q.answer()
        reset_run(u)
        u["plays"] += 1
        await send_question(ctx.bot, chat_id, u, q)
    elif data == "u:resume":
        cur = u.get("cur")
        if not cur or cur["qid"] not in store.by_id:
            await q.answer()
            u["cur"] = None
            if not store.questions:
                await safe_edit(q, menu_text(u), kb_main(usr.id, u)); return
            await send_question(ctx.bot, chat_id, u, q)
        else:
            await q.answer()
            await render_question(ctx.bot, chat_id, u, q)
    elif data == "u:quit":
        await q.answer()
        reset_run(u)
        await safe_edit(q, menu_text(u), kb_main(usr.id, u))
    elif data == "u:menu":
        await q.answer()
        await safe_edit(q, menu_text(u), kb_main(usr.id, u))
    elif data == "u:top":
        await q.answer()
        await safe_edit(q, leaderboard_text(usr.id), kb_back())
    elif data == "u:me":
        await q.answer()
        await safe_edit(q, stats_text(u), kb_back())
    else:
        await q.answer()


# ══════════════════════════ واجهة الأدمن ══════════════════════════
HELP_ADD = (
    "<b>إضافة أسئلة</b>\n"
    "أرسل سؤالاً أو عدة أسئلة (بين كل سؤال وآخر سطر فارغ)، أو أرسل ملف <b>.txt</b> بنفس الصيغة:\n\n"
    "<pre>ما هو أول ركن من أركان الإسلام؟\n"
    "أ) الشهادتان *\n"
    "ب) الصلاة\n"
    "ج) الزكاة\n"
    "د) الصوم\n\n"
    "كم ركعة في صلاة الفجر؟\n"
    "أ) ثلاث\n"
    "ب) ركعتان\n"
    "ج) أربع\n"
    "د) ركعة واحدة\n"
    "الجواب: ب</pre>\n\n"
    "• ضع <b>*</b> بجانب الإجابة الصحيحة، أو اكتب سطر «الجواب: ب».\n"
    "• من 2 إلى 6 خيارات، واحد فقط صحيح (والأفضل 4).\n"
    "• الأسئلة المكررة تُتجاهل تلقائياً."
)


def panel_text():
    s = store.settings
    return ("<b>لوحة الأدمن</b>\n━━━━━━━━━━━━━━\n"
            f"الأسئلة: <b>{len(store.questions)}</b>  |  اللاعبون: <b>{len(store.users)}</b>\n"
            f"حالة البوت: <b>{'تحت الصيانة' if s['maintenance'] else 'يعمل'}</b>\n"
            f"ترتيب الأسئلة: {'عشوائي' if s['shuffle_questions'] else 'حسب الإضافة'}\n"
            f"نسخة تلقائية كل {AUTO_BACKUP_HOURS} ساعة: {'مفعّلة' if s['auto_backup'] else 'متوقفة'}\n"
            f"الأزرار الملونة: {'مدعومة' if _STYLE_SUPPORTED else 'غير مدعومة (حدّث المكتبة)'}\n\n"
            "لإضافة أسئلة بالجملة أرسل ملف <b>.txt</b>، ولاستعادة نسخة احتياطية أرسل ملف <b>.json</b>.")


def panel_kb():
    s = store.settings
    maint = (IBtn("تشغيل البوت (إنهاء الصيانة)", callback_data="ad:maint", style="success") if s["maintenance"]
             else IBtn("إيقاف البوت للصيانة", callback_data="ad:maint", style="danger"))
    return InlineKeyboardMarkup([
        [IBtn("إضافة أسئلة", callback_data="ad:add", style="primary")],
        [IBtn("قائمة الأسئلة", callback_data="ad:list:0", style="primary"),
         IBtn("إحصائيات", callback_data="ad:stats", style="primary")],
        [IBtn("نسخة احتياطية كاملة", callback_data="ad:backup", style="success"),
         IBtn("تصدير الأسئلة TXT", callback_data="ad:export", style="success")],
        [maint],
        [IBtn("الترتيب: " + ("عشوائي" if s["shuffle_questions"] else "متسلسل"), callback_data="ad:order"),
         IBtn("النسخ التلقائي: " + ("يعمل" if s["auto_backup"] else "متوقف"), callback_data="ad:auto")],
        [IBtn("حذف كل الأسئلة", callback_data="ad:wipe", style="danger")],
        [IBtn("القائمة الرئيسية", callback_data="u:menu")],
    ])


def list_view(page):
    qs = store.questions
    pages = max(1, (len(qs) + 7) // 8)
    page = min(max(page, 0), pages - 1)
    text = (f"<b>قائمة الأسئلة</b> ({len(qs)})\nصفحة {page + 1} من {pages}" if qs
            else "لا توجد أسئلة بعد.")
    rows = [[IBtn(f"{q['id']}. {short(q['q'])}", callback_data=f"ad:q:{q['id']}:{page}", style="primary")]
            for q in qs[page * 8:(page + 1) * 8]]
    nav = []
    if page > 0:
        nav.append(IBtn("السابق", callback_data=f"ad:list:{page - 1}"))
    if page < pages - 1:
        nav.append(IBtn("التالي", callback_data=f"ad:list:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([IBtn("رجوع", callback_data="ad:menu")])
    return text, InlineKeyboardMarkup(rows)


def detail_view(qid, page):
    q = store.by_id.get(qid)
    back = InlineKeyboardMarkup([[IBtn("رجوع", callback_data=f"ad:list:{page}")]])
    if not q:
        return "السؤال غير موجود (ربما حُذف).", back
    lines = [f"<b>سؤال #{qid}</b>", esc(q["q"]), ""]
    for i, o in enumerate(q["options"]):
        lines.append(f"{LETTERS[i]}) {esc(o)}" + ("  ✅" if i == q["correct"] else ""))
    return "\n".join(lines), InlineKeyboardMarkup([
        [IBtn("حذف السؤال", callback_data=f"ad:del:{qid}:{page}", style="danger")],
        [IBtn("رجوع", callback_data=f"ad:list:{page}")]])


async def send_backup(bot, chat_id, caption=None):
    data = store.build_backup()
    name = f"quiz_backup_{datetime.now():%Y-%m-%d_%H%M}.json"
    await bot.send_document(chat_id, document=io.BytesIO(data), filename=name, caption=caption)


async def import_text(update, ctx, text, source):
    good, errors = parse_questions(text)
    added, dups = store.add_questions(good)
    lines = [f"<b>نتيجة الإضافة ({source})</b>", "━━━━━━━━━━━━━━",
             f"أُضيف: <b>{len(added)}</b> سؤال"]
    if dups:
        lines.append(f"مكرر (تم تجاهله): {dups}")
    if errors:
        lines.append(f"فيها أخطاء (لم تُضف): {len(errors)}")
        lines += [esc(e) for e in errors[:8]]
        if len(errors) > 8:
            lines.append(f"… و{len(errors) - 8} أخطاء أخرى")
    lines.append(f"\nإجمالي الأسئلة الآن: <b>{len(store.questions)}</b>")
    kb = InlineKeyboardMarkup([[IBtn("لوحة الأدمن", callback_data="ad:menu")]])
    await update.message.reply_text("\n".join(lines)[:4000], parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    usr = update.effective_user
    if is_admin(usr.id) and ctx.user_data.get("mode") == "add":
        await import_text(update, ctx, update.message.text, "كتابة")
        return
    u = store.user(usr.id, usr.first_name or usr.username)
    await update.message.reply_text(menu_text(u), parse_mode=ParseMode.HTML, reply_markup=kb_main(usr.id, u))


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    usr = update.effective_user
    if not is_admin(usr.id):
        return
    doc = update.message.document
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await update.message.reply_text("الملف كبير جداً (الحد 5 ميغابايت)."); return
    tg = await doc.get_file()
    text = decode_text(bytes(await tg.download_as_bytearray()))
    name = (doc.file_name or "").lower()
    if name.endswith(".json") or text.lstrip().startswith("{"):
        try:
            parsed = validate_backup(json.loads(text))
        except (ValueError, json.JSONDecodeError) as e:
            await update.message.reply_text(f"تعذّرت قراءة النسخة الاحتياطية: {e}"); return
        ctx.application.bot_data.setdefault("restore", {})[usr.id] = parsed
        users_n = len(parsed["users"])
        kb = InlineKeyboardMarkup([
            [IBtn("استعادة كاملة (استبدال كل شيء)", callback_data="ad:rs:replace", style="danger")],
            [IBtn("دمج الأسئلة الجديدة فقط", callback_data="ad:rs:merge", style="primary")],
            [IBtn("إلغاء", callback_data="ad:rs:cancel")]])
        await update.message.reply_text(
            "<b>ملف نسخة احتياطية صالح</b>\n━━━━━━━━━━━━━━\n"
            f"التاريخ: {esc(parsed['created'] or '—')}\n"
            f"الأسئلة: <b>{len(parsed['questions'])}</b>  |  اللاعبون: <b>{users_n}</b>\n\n"
            "• <b>الاستعادة الكاملة</b>: تستبدل الأسئلة والإعدادات والنتائج الحالية بما في الملف "
            "(وأرسل لك نسخة من الوضع الحالي قبلها).\n"
            "• <b>الدمج</b>: يضيف الأسئلة غير الموجودة فقط ولا يغيّر شيئاً آخر.",
            parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        await import_text(update, ctx, text, "ملف")


async def on_admin_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("هذا الأمر للأدمن فقط", show_alert=True); return
    await q.answer()
    parts = q.data.split(":")
    act = parts[1]
    chat_id = q.message.chat_id
    s = store.settings

    if act == "menu":
        ctx.user_data.pop("mode", None)
        await safe_edit(q, panel_text(), panel_kb())
    elif act == "add":
        ctx.user_data["mode"] = "add"
        await safe_edit(q, HELP_ADD, InlineKeyboardMarkup([[IBtn("إنهاء الإضافة", callback_data="ad:menu", style="danger")]]))
    elif act == "list":
        text, kb = list_view(int(parts[2]))
        await safe_edit(q, text, kb)
    elif act == "q":
        text, kb = detail_view(int(parts[2]), int(parts[3]))
        await safe_edit(q, text, kb)
    elif act == "del":
        qid, page = int(parts[2]), int(parts[3])
        await safe_edit(q, f"هل تريد حذف السؤال #{qid}؟", InlineKeyboardMarkup([
            [IBtn("نعم، احذف", callback_data=f"ad:delok:{qid}:{page}", style="danger")],
            [IBtn("إلغاء", callback_data=f"ad:q:{qid}:{page}")]]))
    elif act == "delok":
        store.delete(int(parts[2]))
        text, kb = list_view(int(parts[3]))
        await safe_edit(q, "تم حذف السؤال.\n\n" + text, kb)
    elif act == "stats":
        users = list(store.users.values())
        day = _now() - 86400
        await safe_edit(q, (
            "<b>إحصائيات</b>\n━━━━━━━━━━━━━━\n"
            f"الأسئلة: {len(store.questions)}\n"
            f"اللاعبون: {len(users)} (نشطون آخر 24 ساعة: {sum(1 for u in users if u['last'] >= day)})\n"
            f"إجمالي المحاولات: {sum(u['plays'] for u in users)}\n"
            f"إجمالي الإجابات الصحيحة: {sum(u['correct'] for u in users)}\n"
            f"من أنهوا كل المراحل: {sum(1 for u in users if u['wins'] > 0)}\n"
            f"أعلى نتيجة: {max([u['best'] for u in users] or [0])}"),
            InlineKeyboardMarkup([[IBtn("رجوع", callback_data="ad:menu")]]))
    elif act == "backup":
        await send_backup(ctx.bot, chat_id, "نسخة احتياطية كاملة (أسئلة + إعدادات + نتائج).\n"
                                           "لاستعادتها أرسل هذا الملف للبوت.")
    elif act == "export":
        if not store.questions:
            await ctx.bot.send_message(chat_id, "لا توجد أسئلة لتصديرها."); return
        await ctx.bot.send_document(chat_id, document=io.BytesIO(export_txt(store.questions).encode("utf-8")),
                                    filename=f"questions_{datetime.now():%Y-%m-%d}.txt",
                                    caption=f"{len(store.questions)} سؤال بصيغة قابلة لإعادة الإرسال للبوت.")
    elif act == "maint":
        s["maintenance"] = not s["maintenance"]
        store.save_quiz()
        await safe_edit(q, panel_text(), panel_kb())
    elif act == "order":
        s["shuffle_questions"] = not s["shuffle_questions"]
        store.save_quiz()
        await safe_edit(q, panel_text(), panel_kb())
    elif act == "auto":
        s["auto_backup"] = not s["auto_backup"]
        store.save_quiz()
        await safe_edit(q, panel_text(), panel_kb())
    elif act == "wipe":
        await safe_edit(q, f"سيتم حذف <b>{len(store.questions)}</b> سؤال نهائياً.\n"
                           "سأرسل لك نسخة احتياطية قبل الحذف. هل أنت متأكد؟",
                        InlineKeyboardMarkup([[IBtn("نعم، احذف الكل", callback_data="ad:wipeok", style="danger")],
                                              [IBtn("إلغاء", callback_data="ad:menu")]]))
    elif act == "wipeok":
        if store.questions:
            await send_backup(ctx.bot, chat_id, "نسخة قبل حذف كل الأسئلة")
        store.wipe_questions()
        await safe_edit(q, "تم حذف كل الأسئلة.\n\n" + panel_text(), panel_kb())
    elif act == "rs":
        parsed = ctx.application.bot_data.get("restore", {}).pop(uid, None)
        mode = parts[2]
        if mode == "cancel":
            await safe_edit(q, "تم الإلغاء."); return
        if parsed is None:
            await safe_edit(q, "انتهت صلاحية الملف، أرسله من جديد."); return
        if mode == "replace":
            if store.questions or store.users:
                await send_backup(ctx.bot, chat_id, "نسخة من الوضع الحالي قبل الاستعادة")
            store.apply_backup(parsed)
            await safe_edit(q, f"تمت الاستعادة الكاملة: {len(store.questions)} سؤال، "
                               f"{len(store.users)} لاعب.\n\n" + panel_text(), panel_kb())
        else:
            added, dups = store.add_questions([{"q": x["q"], "options": x["options"], "correct": x["correct"]}
                                               for x in parsed["questions"]])
            await safe_edit(q, f"تم الدمج: أُضيف {len(added)} سؤال وتُجوهل {dups} مكرر.\n\n" + panel_text(), panel_kb())


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    ctx.user_data.pop("mode", None)
    await update.message.reply_text(panel_text(), parse_mode=ParseMode.HTML, reply_markup=panel_kb())


async def cmd_backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await send_backup(ctx.bot, update.effective_chat.id, "نسخة احتياطية كاملة")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.pop("mode", None):
        await update.message.reply_text("تم الخروج من وضع الإضافة.")


# ══════════════════════════ الصيانة / الأخطاء / الخلفية ══════════════════════════
async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """يعمل قبل كل المعالجات: أثناء الصيانة يُحجب الجميع عدا الأدمن."""
    if not store.settings["maintenance"]:
        return
    usr = update.effective_user
    if usr is None or is_admin(usr.id):
        return
    try:
        if update.callback_query:
            await update.callback_query.answer(MAINTENANCE_TEXT, show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(MAINTENANCE_TEXT)
    except TelegramError:
        pass
    raise ApplicationHandlerStop


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("خطأ غير متوقع", exc_info=ctx.error)


async def background(app: Application):
    """حفظ دوري لنتائج اللاعبين + إرسال نسخة احتياطية تلقائية للأدمن."""
    while True:
        try:
            await asyncio.sleep(5)
            store.flush()
            if store.settings["auto_backup"] and store.questions:
                last = store.meta.get("last_backup", 0)
                if not last:
                    store.meta["last_backup"] = _now(); store.save_quiz()
                elif time.time() - last >= AUTO_BACKUP_HOURS * 3600:
                    store.meta["last_backup"] = _now(); store.save_quiz()
                    for aid in ADMIN_IDS:
                        try:
                            await send_backup(app.bot, aid, "نسخة احتياطية تلقائية")
                        except TelegramError as e:
                            log.warning("تعذّر إرسال النسخة التلقائية إلى %s: %s", aid, e)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background")


async def post_init(app: Application):
    IBtn("x", style="primary", callback_data="x")   # فحص دعم الألوان
    log.info("الأزرار الملونة: %s", "مدعومة" if _STYLE_SUPPORTED else "غير مدعومة — نفّذ: pip install -U python-telegram-bot")
    try:
        await app.bot.set_my_commands([BotCommand("start", "القائمة الرئيسية")])
        for aid in ADMIN_IDS:
            await app.bot.set_my_commands(
                [BotCommand("start", "القائمة الرئيسية"), BotCommand("admin", "لوحة الأدمن"),
                 BotCommand("backup", "نسخة احتياطية")], scope=BotCommandScopeChat(aid))
    except TelegramError as e:
        log.warning("set_my_commands: %s", e)
    app.bot_data["bg"] = asyncio.create_task(background(app))


async def post_shutdown(app: Application):
    t = app.bot_data.get("bg")
    if t:
        t.cancel()
    store.flush()


def start_keepalive():
    """خادم ويب صغير للاستضافات التي تتطلب منفذاً (يعمل فقط إن وُجد متغير PORT)."""
    port = os.getenv("PORT")
    if not port:
        return

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")
        do_HEAD = do_GET

        def log_message(self, *a):
            pass

    srv = HTTPServer(("0.0.0.0", int(port)), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("keep-alive على المنفذ %s", port)

from flask import Flask
import threading
import os

web_app = Flask(__name__)
@web_app.route('/')
def home(): return "Bot is Running! 🚀"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()
def main():
    global store
    if not BOT_TOKEN:
        sys.exit("ضع توكن البوت في BOT_TOKEN (متغير بيئة أو داخل الملف).")
    if not ADMIN_IDS:
        sys.exit("حدّد أيدي الأدمن في ADMIN_IDS (متغير بيئة أو داخل الملف).")
    store = Store(DATA_DIR)
    log.info("تم تحميل %d سؤال و%d لاعب من %s", len(store.questions), len(store.users), DATA_DIR)

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(TypeHandler(Update, gate), group=-1)
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_admin_cb, pattern=r"^ad:"))
    app.add_handler(CallbackQueryHandler(on_user_cb, pattern=r"^(u:|a:|noop)"))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    start_keepalive()
    log.info("البوت يعمل…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
