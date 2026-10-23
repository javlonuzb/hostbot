# Host Bot Main — v1

Telegram bot kodini (**.py**, **.php** yoki **.zip**) qabul qilib, avtomatik ishga tushiradi.
AI (DeepSeek) yordamida tavsif bo'yicha kod ham yozib beradi.

## GitHub'da yoqish
1. @BotFather'da asosiy bot yarating, tokenni oling.
2. **Private** repo oching, shu fayllarni yuklang (`.github` papkasi bilan).
3. Settings -> Secrets and variables -> Actions -> **Secrets**:
   - `MASTER_BOT_TOKEN` = asosiy bot tokeni
   - `ALLOWED_USERS` = sizning Telegram ID'ingiz (bir nechta bo'lsa vergul bilan)
   - `API_KEY` = DeepSeek API kaliti (AI xizmatlari uchun; bo'lmasa AI tugmalari o'chiq turadi)
   - `ADMIN_USERS` *(ixtiyoriy)* = yangilik qo'sha oladiganlar ID'si. Bo'sh bo'lsa, `ALLOWED_USERS` dagi **birinchi** ID admin bo'ladi.

   ID'ni bilmasangiz: avval faqat `MASTER_BOT_TOKEN` bilan ishga tushiring, botga /id yozing, so'ng `ALLOWED_USERS` qo'shib qayta ishga tushiring.
4. *(ixtiyoriy)* Settings -> Secrets and variables -> Actions -> **Variables**: `AI_MODEL` (standart `deepseek-v4-flash`, kuchliroq: `deepseek-v4-pro`).
5. Actions -> `test` -> Run workflow.

## Ishlatish
Botga /start -> Bot qo'shish -> token -> kod fayli. Bot darhol ishga tushadi, panel chiqadi.
Panelda «📄 Yangi fayl» yuborsangiz, «🚀 Asosiy fayl qilib ishga tushirish» yoki «Faqat qo'shish» ni tanlaysiz.
Hosting qilinayotgan botga `BOT_TOKEN` va `DB_PATH` muhit o'zgaruvchilari beriladi
(`API_KEY` va asosiy bot tokeni **berilmaydi**).

### 🐘 PHP botlar
- Bitta `.php` fayl yoki `.zip` (ichida `bot.php` / `index.php` / `main.php`) yuboring.
- Bot **long polling** (`getUpdates`) usulida yozilgan bo'lishi kerak. Webhook usuli ishlamaydi (GitHub Actions'da ochiq manzil yo'q).
- `composer.json` bo'lsa, `composer install` avtomatik bajariladi (`vendor/` papkasi zaxiraga kirmaydi, qayta o'rnatiladi).
- Token: `getenv('BOT_TOKEN')`, SQLite: `getenv('DB_PATH')`.

### 🧠 AI bilan kod yozish (`/ai`)
1. Tilni tanlang (Python yoki PHP), bot nima qilishini tavsiflab yozing.
2. AI tayyor faylni (`bot.py` / `bot.php`) yuboradi; kod sintaksisi tekshiriladi, xato bo'lsa AI o'zi tuzatadi.
3. «➕ Yangi bot sifatida ishga tushirish» -> token yuborasiz -> bot ishga tushadi. Yoki «✏️ O'zgartirish kiritish» bilan kodni takomillashtirasiz.

**AI ishlamasa:** botga `/aicheck` yuboring. U `API_KEY` ko'rinyaptimi va DeepSeek javob beryaptimi — shuni ko'rsatadi.
Odatiy sabablar: secret *Repository secrets* da emas, yangi `.github/workflows/test.yml` yuklanmagan, yoki secret qo'shgach workflow qayta ishga tushirilmagan.

### 🛠 AI bilan tuzatish
Bot panelida «🛠 AI bilan tuzatish»: asosiy fayl kodi va oxirgi loglar (tokenlar yashirilgan holda) AI'ga yuboriladi,
tuzatilgan versiya eski fayl o'rniga qo'yiladi (eski nusxa `*.bak` bo'lib qoladi).

### 📰 Yangiliklar (`/news`)
Hamma foydalanuvchi oxirgi yangiliklarni ko'radi. Admin yangilik qo'shadi (barchaga yuboriladi) yoki o'chiradi.

### Boshqa yangiliklar
- 📦 Botni zip qilib yuklab olish, 🟢 ishlash vaqti (uptime) va til/versiya panelda ko'rinadi.
- Maxfiy qiymatlar workflow'da faqat botni ishga tushiruvchi qadamga beriladi.

## Cheklovlar
- GitHub Actions doimiy hosting emas: job ~6 soatda qayta ishga tushadi (qisqa uzilish bo'ladi).
- Boshqalarning kodini ishga tushirmang: ALLOWED_USERS faqat o'zingiz/ishongan odamlar uchun.
- AI yozgan kod avtomatik tekshirilsa ham, ishga tushirishdan oldin ko'zdan kechiring.
- `.zip` ichida 300 tadan ortiq fayl bo'lmasin (tayyor `vendor/` ni yuklamang, `composer.json` yetarli).

## Versiyalar
- **v1** — birinchi versiya: Python/PHP hosting, AI bilan kod yozish va tuzatish (DeepSeek), yangiliklar, zip zaxira.
