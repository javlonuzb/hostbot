"""Per-bot secrets: authenticated Fernet encryption; never inherit host secrets."""
import base64
import hashlib
import os
import re
from cryptography.fernet import Fernet, InvalidToken
import db

RESERVED = {
    'BOT_TOKEN', 'TELEGRAM_BOT_TOKEN', 'TOKEN', 'API_TOKEN', 'DB_PATH', 'BOT_DIR',
    'HOME', 'PATH', 'PYTHONPATH', 'PYTHONUNBUFFERED', 'MASTER_BOT_TOKEN',
    'ALLOWED_USERS', 'ADMIN_USERS', 'ADMIN_IDS', 'SECRET_KEY',
    'LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'BASH_ENV', 'ENV',
}


def cipher():
    raw = os.getenv('SECRET_KEY') or os.getenv('MASTER_BOT_TOKEN')
    if not raw:
        raise ValueError('SECRET_KEY yoki MASTER_BOT_TOKEN kerak.')
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(('hostbot-v2|' + raw).encode()).digest()))


def encrypt(value):
    return 'fernet:' + cipher().encrypt(str(value).encode()).decode()


def decrypt(value):
    if not str(value).startswith('fernet:'):
        return None
    try:
        return cipher().decrypt(value[7:].encode()).decode()
    except (InvalidToken, ValueError):
        return None


def valid_key(name):
    return bool(re.fullmatch(r'[A-Z_][A-Z0-9_]{0,63}', name or '')) and name not in RESERVED


def set_value(host_id, name, value):
    name = (name or '').strip().upper()
    if not valid_key(name):
        raise ValueError('Kalit nomi noto‘g‘ri yoki tizim uchun band. Masalan OPENAI_KEY.')
    if not value or len(value) > 4000 or '\x00' in value:
        raise ValueError('Qiymat 1–4000 belgi bo‘lsin, NUL taqiqlangan.')
    db.set_secret(host_id, name, encrypt(value))
    return name


def env_for(host_id):
    out = {}
    for row in db.list_secrets(host_id):
        if valid_key(row['key']):
            value = decrypt(row['value'])
            if value is None:
                raise ValueError('Maxfiy kalit ochilmadi. SECRET_KEY o‘zgarmaganini tekshiring.')
            out[row['key']] = value
    return out


def items(host_id, mask=False):
    return [(r['key'], '••••••••' if mask else decrypt(r['value'])) for r in db.list_secrets(host_id)]


def delete(host_id, name):
    return db.del_secret(host_id, name)


def redact(text, host_id=None):
    import ai
    extra = [os.getenv('MASTER_BOT_TOKEN', ''), os.getenv('API_KEY', ''), os.getenv('SECRET_KEY', '')]
    if host_id:
        h = db.get_host(host_id)
        if h:
            extra.append(h['token'])
        extra.extend(value for _, value in items(host_id) if value)
    # Longest first, also cover short user secrets (never show values in logs).
    for value in sorted(set(extra), key=len, reverse=True):
        if value:
            text = text.replace(value, '<SECRET>')
    return ai.redact(text)
