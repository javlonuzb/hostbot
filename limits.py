"""Limitlar, admin tekshiruvi va promo rejim.

Promo kod botlar soni va kunlik AI kvotasini ochadi. Telegram, fayl xavfsizligi va fizik resurs chegaralari saqlanadi.
"""
import os
import re
import time

import db

DEFAULT_MAX_BOTS = int(os.getenv("MAX_BOTS", "3"))
DEFAULT_AI_DAILY = int(os.getenv("AI_DAILY", "25"))
PROMO_UNLIMITED = -1         # -1 = permanent; 0 = no promo
UNLIMITED = 0                # limit ko'rsatkichi: 0 = cheksiz

_ALLOWED = [int(x) for x in re.findall(r"\d+", os.getenv("ALLOWED_USERS", ""))]
_ENV_ADMINS = {int(x) for x in re.findall(r"\d+", (os.getenv("ADMIN_IDS", "") + "," + os.getenv("ADMIN_USERS", "")))}


def is_admin(user_id):
    """Har chaqiruvda tekshiriladi (faqat menyuda ko'rinishi yetarli emas)."""
    if user_id is None:
        return False
    if _ENV_ADMINS and user_id in _ENV_ADMINS:
        return True
    if user_id in set(db.list_admins()):
        return True
    # ADMIN_USERS bo'sh bo'lsa: ALLOWED_USERS dagi birinchi ID — bosh admin
    return bool(_ALLOWED) and user_id == _ALLOWED[0] and not _ENV_ADMINS




def promo_until(user_id):
    row = db.get_user(user_id)
    return (row["promo_until"] if row else 0) or 0


def promo_active(user_id):
    """Doimiy promo (-1) yoki muddati o'tmagan promo."""
    row = db.get_user(user_id)
    if not row:
        return False
    until = row["promo_until"] or 0
    if until == 0:
        return False
    if until < 0:
        return True
    return until > time.time()


def bot_limit(user_id):
    """0 → cheksiz."""
    if is_admin(user_id) or promo_active(user_id):
        return UNLIMITED
    row = db.get_user(user_id)
    n = (row["max_bots"] if row else 0) or 0
    return n if n > 0 else DEFAULT_MAX_BOTS


def ai_limit(user_id):
    if is_admin(user_id) or promo_active(user_id):
        return UNLIMITED
    row = db.get_user(user_id)
    n = (row["ai_daily"] if row else 0) or 0
    return n if n > 0 else DEFAULT_AI_DAILY


def bots_used(user_id):
    return db.count_hosts(user_id)


def can_add_bot(user_id):
    lim = bot_limit(user_id)
    used = bots_used(user_id)
    if lim == UNLIMITED or used < lim:
        return True, f"{used}/{lim if lim else '∞'}"
    return False, (f"Limit tugadi: {used}/{lim} bot. Promo kod bilan limitni ochish "
                   f"mumkin (🎁 Promo).")


def ai_left(user_id):
    lim = ai_limit(user_id)
    used = db.ai_used(user_id)
    if lim == UNLIMITED:
        return True, used, lim
    return used < lim, used, lim


def ai_consume(user_id):
    return db.ai_inc(user_id)


def promo_status(user_id):
    row = db.get_user(user_id)
    if not row or not promo_active(user_id):
        return ""
    until = row["promo_until"]
    if until < 0:
        return f"🎁 Promo: cheksiz ({row['promo_code']})"
    left = int((until - time.time()) // 86400)
    return f"🎁 Promo: {max(left, 0)} kun qoldi ({row['promo_code']})"


def apply_promo(user_id, code):
    """(ok, xabar) — kodni tekshiradi va qo'llaydi."""
    code = (code or "").strip().lower()
    if not code:
        return False, "Kod bo'sh."
    if len(code) > 64:
        return False, "Kod juda uzun."
    ok, msg = db.redeem_promo(user_id, code)
    return ok, ('✅ ' if ok else '❌ ') + msg


def revoke_promo(user_id):
    db.set_user(user_id, promo_until=0)
