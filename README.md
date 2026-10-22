# Hosting Bot

Telegram bot kodini (.py yoki .zip) qabul qilib, avtomatik ishga tushiradi.

## GitHub'da yoqish
1. @BotFather'da asosiy bot yarating, tokenni oling.
2. **Private** repo oching, shu fayllarni yuklang (`.github` papkasi bilan).
3. Settings -> Secrets and variables -> Actions:
   - `MASTER_BOT_TOKEN` = asosiy bot tokeni
   - `ALLOWED_USERS` = sizning Telegram ID'ingiz (bir nechta bo'lsa vergul bilan)
   ID'ni bilmasangiz: avval faqat `MASTER_BOT_TOKEN` bilan ishga tushiring, botga /id yozing, so'ng `ALLOWED_USERS` qo'shib qayta ishga tushiring.
4. Actions -> `test` -> Run workflow.

## Ishlatish
Botga /start -> Bot qo'shish -> token -> kod fayli. Bot darhol ishga tushadi, panel chiqadi.
Hosting qilinayotgan botga `BOT_TOKEN` va `DB_PATH` muhit o'zgaruvchilari beriladi.

## Cheklovlar
- GitHub Actions doimiy hosting emas: job ~6 soatda qayta ishga tushadi (qisqa uzilish bo'ladi).
- Boshqalarning kodini ishga tushirmang: ALLOWED_USERS faqat o'zingiz/ishongan odamlar uchun.
