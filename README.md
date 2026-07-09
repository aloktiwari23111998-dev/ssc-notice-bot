# SSC Notice Board → Telegram Auto-Poster (100% Free)

Ye system automatically ssc.gov.in ke Notice Board ko check karta rehta hai, aur jab
bhi koi NAYI notice aati hai, use automatically tumhare Telegram channel (@SSCDIARY)
pe post kar deta hai.

**Cost: ₹0** — GitHub Actions ka free tier use hota hai (public repo ke liye unlimited
free minutes).

---

## Step 1 — Telegram Bot Banao

1. Telegram par **@BotFather** ko open karo.
2. `/newbot` command bhejo, naam aur username set karo (e.g. `SSCDiaryBot`).
3. BotFather tumhe ek **Bot Token** dega, jaisa: `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
   — isko safe rakho, kisi ke saath share mat karna.
4. Apne **@SSCDIARY** channel me jao → Channel Settings → Administrators → Add Admin
   → apna naya bot add karo (isse "Post Messages" permission zaroor do).

## Step 2 — Channel ka Chat ID pata karo

Agar channel ka username public hai (jaise `@SSCDIARY`), to seedha wahi use kar sakte ho
— `CHANNEL_ID = "@SSCDIARY"`. Extra step ki zaroorat nahi.

## Step 3 — Is code ko GitHub par daalo

1. [github.com](https://github.com) par free account banao (agar nahi hai).
2. Ek naya **public repository** banao (e.g. `ssc-notice-bot`).
3. Is folder ke saare files (`notifier.py`, `requirements.txt`, `seen_notices.json`,
   `.github/workflows/check.yml`) us repo me upload/push kar do.

   Terminal se:
   ```bash
   git init
   git add .
   git commit -m "SSC notice bot setup"
   git branch -M main
   git remote add origin https://github.com/<tumhara-username>/ssc-notice-bot.git
   git push -u origin main
   ```

## Step 4 — Secrets add karo (Bot Token safe rakhne ke liye)

1. Apne GitHub repo me jao → **Settings** → **Secrets and variables** → **Actions**.
2. **New repository secret** click karo, do secrets banao:
   - `TELEGRAM_BOT_TOKEN` → BotFather wala token
   - `TELEGRAM_CHANNEL_ID` → `@SSCDIARY`

## Step 5 — Bas ho gaya!

GitHub Actions automatically har ~15 minute me `notifier.py` chalayega:
- SSC ka notice board check karega
- Nayi notice mile to Telegram channel pe post karega
- Already dekhi hui notices dobara post nahi karega

Test karne ke liye: repo me **Actions** tab → **SSC Notice Checker** workflow →
**Run workflow** button se manually bhi turant chala sakte ho.

---

## Agar kabhi notices detect na ho (site design badal jaaye)

SSC ka website kabhi-kabhi apna layout change kar sakta hai. Agar bot ko notices
milna band ho jaaye:

1. Browser me `https://ssc.gov.in/home/notice-board` open karo.
2. Right-click → **Inspect** → **Network** tab → page reload karo → filter `XHR`/`Fetch`.
3. Dekho koi API call JSON data return kar rahi hai kya (e.g. kuch `/api/...` URL) —
   agar milti hai, to wo direct JSON API use karna scraping se zyada reliable hoga.
   Mujhe wo URL bata dena, main `notifier.py` update kar dunga.
4. Ya phir simply `notifier.py` me `NOTICE_LINK_SELECTOR` variable ko naye HTML
   structure ke hisaab se update karna hoga (Inspect Element se selector copy karke).

---

## File Upload (jaisa SSC4EVER karta hai)

Ab `notifier.py` sirf link nahi bhejta — jo bhi notice ek PDF/Word/Excel file hoti hai,
uski **actual file** channel me directly post ho jaati hai (Telegram ka `sendDocument`
API use hota hai), exactly jaise screenshot me `tentative_vacancies_09072026.pdf` post
hua hai. Caption me filename, "SSC LATEST UPDATE 💣💥", notice ka title, aur
"Join 👉 @SSCDIARY 👈" line automatically add ho jaati hai.

Agar file kisi wajah se seedha URL se fetch na ho paaye, script automatically:
1. File ko khud download karke upload karne ki koshish karega, aur
2. Wo bhi fail ho to sirf link wala text message bhej dega (taaki notice miss na ho).

## Important Notes

- Ye tumhara khud ka bot hai jo sirf public notice board padhta hai — koi login/private
  data access nahi karta, so ye completely legitimate/safe hai.
- SSC website ko har 5 min se zyada frequently mat hit karo — 15 min interval is polite
  and free-tier friendly.
- Agar chaho to message format (`notifier.py` ke `send_to_telegram` function) me emoji,
  hashtags, ya extra branding easily add/change kar sakte ho.
