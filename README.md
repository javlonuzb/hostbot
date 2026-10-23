# Host Bot Main — v2

Mavjud v1 asosida kengaytirilgan aiogram 3 hosting boti. Python/PHP botlarni alohida jarayonlarda ishga tushiradi. Boshqarish inline tugmalarda; yagona kirish buyrug‘i — `/start`.

## Qo‘shilgan imkoniyatlar

- **Zaxira:** `hosting.db`, foydalanuvchi SQLite bazalari va bot fayllari bitta ZIP qilib private Telegram kanaliga yuboriladi. SQLite online-backup API WAL ichidagi commit qilingan ma’lumotni ham oladi. Kunlik tekshiruv va har bir boshqariladigan job yakunida qo‘shimcha nusxa. Actions hosting ma’lumotlari uchun cache ishlatilmaydi.
- **Tiklash:** admin → nusxani tanlash yoki ZIP yuklash → tasdiqlash. Arxiv avval tekshiriladi, keyin botlar to‘xtatiladi va fayllar almashtiriladi. Avvalgi data daraxti `data.rollback` sifatida qoladi. Keyingi tiklash undan avvalgi rollbackni almashtiradi.
- **Maxfiy o‘zgaruvchilar:** har bot panelida `OPENAI_KEY=...` kabi kalitlar. Fernet bilan bazada shifrlanadi, qiymatlar UI’da ko‘rinmaydi; xabarni o‘chirishga urinish qilinadi. Kalitdan foydalanish uchun restart. Hostning API_KEY/MASTER_BOT_TOKEN/SECRET_KEY muhiti bolalarga meros qilinmaydi. Botga o‘zining API_KEY kalitini qo‘shish mumkin.
- **Qulash:** oxirgi traceback maxfiy qiymatlardan tozalanadi; egasiga `🛠 AI bilan tuzatish`, log va panel tugmalari yuboriladi. Ketma-ket xatolarda ko‘pi bilan 5 restart urinish; keyin to‘xtaydi. Kod 0 normal tugash hisoblanadi.
- **GitHub:** token kiritilgach fayl o‘rniga `https://github.com/owner/repo` yoki `owner/repo branch` yuboring. Faqat ommaviy GitHub repolar, Python/PHP entrypoint. Paneldagi yangilash tasdiqlangach manba yangilanadi; mavjud SQLite va `.env` saqlanadi, manbadan o‘chirilgan fayllar manifest bo‘yicha tozalanadi. Yangilash muvaffaqiyatsiz bo‘lsa eski nusxa qaytariladi. Private repo autentifikatsiyasi qo‘shilmagan.
- **Resurslar:** psutil orqali bot RAM’i va CPU foizi, PID/oqimlar; birinchi CPU o‘lchovi 0 bo‘lishi mumkin. RAM hisobiga bolalar ham kiradi, CPU asosiy jarayon uchun.
- **Kvotalar:** standart 3 bot va kuniga 25 AI amali; admin har foydalanuvchi uchun o‘zgartiradi. AI sarfi UTC kun bo‘yicha; generatsiya/tuzatish/tushuntirish hisoblanadi. Bir amal ichidagi sintaksisni qayta tuzatish shu kvotaga kiradi. Xato bilan tugagan so‘rov ham hisoblanadi.
- **AI:** bir fayl, ko‘p fayllik ZIP loyiha, Mini App uchun index.html + Python/PHP server, kodni tushuntirish va model tanlash. Modellar ayni API endpoint va API_KEY orqali ishlaydi. Haqiqiy AI so‘rovi uchun xizmatdagi haqiqiy kalit/model talab qilinadi.
- **Admin:** panel faqat adminlarga ko‘rinadi, barcha admin callback va FSM kirishlarida ruxsat qayta tekshiriladi. `ADMIN_IDS` va eski `ADMIN_USERS` qo‘llanadi; ikkalasi bo‘sh bo‘lsa ALLOWED_USERS dagi birinchi ID admin.
- **Kanal:** admin panelida qo‘shiladi; `getChatMember` orqali administratorlik, post va edit/pin huquqi avtomatik tekshiriladi.
- **Promo:** `infinix-host-no-limit` doimiy ravishda botlar soni va kunlik AI kvotasini ochadi. Bir foydalanuvchi bir kodni bir marta ishlatadi. Admin kod yaratadi, yoqadi/o‘chiradi va foydalanuvchi promosini bekor qiladi. Kodni o‘chirish avval berilgan huquqlarni qaytarib olmaydi.

## GitHub Actions’da sozlash

1. Private repo ochib arxiv ichidagi fayllarni, jumladan `.github/workflows/test.yml` ni yuklang. Ichki loyiha papkasi emas, uning tarkibi repo ildizida bo‘lsin.
2. Settings → Secrets and variables → Actions → Repository secrets:

| Secret | Vazifasi |
|---|---|
| `MASTER_BOT_TOKEN` | BotFather bergan asosiy bot tokeni, majburiy |
| `ADMIN_IDS` | Admin Telegram ID’lari, vergul bilan; kamida bitta o‘z ID’ingizni yozing |
| `ADMIN_USERS` | Eski v1 nomi, ixtiyoriy alias |
| `ALLOWED_USERS` | Ishonchli foydalanuvchilar ID’lari; admin bu ro‘yxatsiz ham kira oladi |
| `SECRET_KEY` | Uzoq tasodifiy, o‘zgarmas maxfiy satr; barcha qayta ishga tushish va tiklashlarda bir xil qolishi kerak |
| `BACKUP_CHANNEL_ID` | Private zaxira kanalining `-100...` ID’si; Actions sovuq startida tiklash uchun zarur |
| `API_KEY` | AI provayderi kaliti; bo‘lmasa AI ishlamaydi, hosting ishlaydi |

`SECRET_KEY` berilmasa MASTER_BOT_TOKEN dan hosil qilinadi; bunday holatda asosiy tokenni almashtirish eski shifrlangan qiymatlarni ochishni buzadi. Shuning uchun alohida, doimiy SECRET_KEY tavsiya qilinadi. To‘liq ZIPning o‘zi shifrlanmagan, unda bot tokenlari bor: kanal va uning a’zolarini himoyalang.

3. Repository Variables (ixtiyoriy):

| Variable | Standart / izoh |
|---|---|
| `AI_MODEL` | `deepseek-chat` |
| `AI_MODELS` | vergul bilan: `deepseek-chat,deepseek-reasoner`; faqat provayderdagi mavjud modellar |
| `AI_BASE_URL` | `https://api.deepseek.com`; `/chat/completions` avtomatik qo‘shiladi |
| `MAX_BOTS` | `3` |
| `AI_DAILY` | `25` |
| `PUBLIC_ACCESS` | `false`; `true` hammani kiritadi, lekin bu izolyatsiyasiz hosting uchun xavfli |

4. Botni private zaxira kanaliga administrator qilib qo‘shing. Xabar yuborish va tahrirlash/pin huquqlarini bering.
5. Actions → `host-bot-v2` → Run workflow. Botda `/start` → `👑 Admin paneli` → `📢 Baza / zaxira kanali`. Kanal ID’si yoki @username yuboring; tekshiruvdan keyin saqlanadi.
6. Panel bergan kanal ID’sini `BACKUP_CHANNEL_ID` secretiga ham yozing. Bu nusxa mahalliy baza/kesh yo‘qolganda kanalni topish uchun zarur. Paneldagi sozlamaning o‘zi yangi runnerga o‘tmaydi.
7. Admin → `💾 Hozir zaxiralash`. Hujjat kanalga kelib, pin bo‘lganini tekshiring. Kanaldagi oxirgi pinni boshqa xabar bilan almashtirmang.
8. Keyingi Actions ishga tushishida yangi runner pinned ZIPni yuklab tekshiradi, tiklaydi va autorun botlarini ishga tushiradi. Noto‘g‘ri pin, yuklash yoki kalit xatosida bo‘sh baza bilan davom etmaydi.

Birinchi ishga tushishda pin bo‘lmasa bot bo‘sh holatda ochiladi. Kanalga a’zolar qo‘shishda ehtiyot bo‘ling. Botni foydalanuvchilar o‘zlari bloklagan bo‘lsa ogohlantirishlarni yetkazish mumkin emas.

## VPS / mahalliy ishga tushirish

Python 3.12 tavsiya qilinadi; PHP botlar uchun PHP 8 + curl/sqlite va Composer, GitHub uchun git kerak. `pip install -r requirements.txt`, yuqoridagi environment qiymatlarini sozlab `python main.py` ishlating. `DATA_DIR` standart `data`, `MAX_RUNTIME_MIN=0` — avtomatik yopilmaydi. VPS’da mavjud v1 `data` katalogini saqlang: sxema ustunlari avtomatik migratsiya qilinadi. GitHub’dagi eski v1 cache yangi workflow orqali o‘qilmaydi; ko‘chishdan oldin eski bot ma’lumotlarini alohida olib qo‘ying.

## Amaliy cheklovlar va xavfsizlik

- **Cheksiz promo fizik cheksizlik emas.** Telegram fayl/tarmoq cheklovlari, API provayderi hisob/tezlik cheklovlari, AI amallari orasidagi 8 soniyalik himoya, server RAM/CPU va arxiv xavfsizlik limitlari saqlanadi. Promo botlar soni/kunlik AI kvotasini olib tashlaydi; pulli provayder balansini ko‘paytirmaydi.
- Oddiy kod yuklash 10 MB, bot ZIP ochilganda 60 MB/300 faylgacha. To‘liq Telegram zaxirasi **19 MB gacha**, ochilganda 200 MB/10000 faylgacha. Bu public Bot API orqali qayta yuklashga mos konservativ chegaradir. Katta loyihalarga tashqi obyekt saqlash kerak.
- Zaxiraga libs, vendor, node_modules, .git, loglar, kesh va vaqtinchalik fayllar kirmaydi. Bog‘liqliklar requirements.txt/composer.json orqali qayta o‘rnatiladi. Ularni loyihaga qo‘shing. Har SQLite bazasi alohida consistent snapshot; turli bazalar/fayllar bo‘yicha umumiy atomik transaction kafolatlanmaydi.
- Kunlik nusxa jarayon ishlayotgan paytda UTC kuni bo‘yicha tekshiriladi; birinchi imkoniyatda yuboriladi, majburiy aniq soat yo‘q. Job rejalashtirilgan tugashida ham zaxira yuboradi. Runner keskin o‘chsa, oxirgi muvaffaqiyatli nusxadan keyingi o‘zgarishlar yo‘qolishi mumkin.
- Actions workflow har 6 soatda rejalashtirilgan, hosting jarayoni 340 daqiqada yopiladi; test/start/schedule orasida uzilish bo‘ladi. **24/7 kafolat emas.** Doimiy hostingga VPS kerak.
- Subprocess + environment filtri va resource limitlar **xavfsiz sandbox emas**: bir OS foydalanuvchisi huquqida kod/dastur kutubxonalari bajariladi. Begona/malicious kodni hosting qilmang. PUBLIC_ACCESS=false qoldiring; ommaviy hosting uchun container/VM izolyatsiyasi kerak.
- Mini App uchun doimiy ochiq **HTTPS** domen/hosting kerak. Actions yoki promo buni avtomatik bermaydi. AI kodini ishga tushirishdan oldin ko‘rib chiqing.
- Qo‘shimcha lokal nusxa va production tokenlarini alohida himoyalash tavsiya qilinadi; sirlar yozilgan ZIPni public repoga qo‘ymang.

## Fayllar

`main.py` — mavjud aiogram handlerlar va integratsiya; `db.py` — SQLite/migratsiya; `runner.py` — ishga tushirish/watchdog/psutil/git; `backup.py` — Telegram snapshot/restore; `bot_secrets.py` — Fernet/env; `limits.py` — admin/kvota/promo; `ai.py` — mavjud AI servis; `aiex.py` — multi-file/model/explain; `extensions.py` — yangi inline panellar; `inline_ui.py` — promptlarda menyuga qaytish.

## Tekshirish

`python -m py_compile *.py tests/*.py`

`python -m unittest discover -s tests -v`

Offline testlar eski funksiyalarni, token muhitini ajratish, shifrlash, promo/kvota, admin callback/FSM rad etish, SQLite WAL zaxira/tiklash, pin orqali sovuq tiklash, model izolatsiyasi, loyiha ZIPi, git yangilash, RAM statistikasi va supervisorni tekshiradi. Telegram yetkazish/GitHub download/AI servis qismi mock bilan tekshiriladi; haqiqiy tokenlar va kalitlarsiz jonli integratsiya tasdiqlanmagan. PHP mavjud bo‘lmasa PHP runtime testi skip qilinadi; CI PHP o‘rnatadi.
