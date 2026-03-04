from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler
from datetime import datetime
import re
import os
import psycopg2
import psycopg2.errors
from telegram.ext import ContextTypes

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 8575573468

# ===== RATE SAFETY LIMITS =====
MIN_BUY_RATE = 115  # 115 RSD za 1 EUR
MAX_BUY_RATE = 122  # 122 RSD za 1 EUR

MIN_SPREAD = 0.1  # minimalna razlika buy/sell
MAX_SPREAD = 4.0  # maksimalna razlika buy/sell

# ===== GLOBAL CONFIRM STORAGE =====
pending_confirm = {}


# ================= DB ==================

def db():
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def init_db():
    con = db()
    cur = con.cursor()

    # USERS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id BIGINT PRIMARY KEY,
        role TEXT CHECK(role IN ('USER','ADMIN')) NOT NULL,
        is_active INTEGER DEFAULT 1,
        username TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # dodavanje admina
    cur.execute("""
    INSERT INTO users(telegram_id, role, is_active, username)
    VALUES (%s, 'ADMIN', 1, 'admin')
    ON CONFLICT (telegram_id) DO NOTHING
    """, (ADMIN_ID,))

    # RATE
    cur.execute("""
    CREATE TABLE IF NOT EXISTS rate (
        id INTEGER PRIMARY KEY,
        buy_rate REAL,
        sell_rate REAL,
        updated_at TIMESTAMP,
        updated_by BIGINT REFERENCES users(telegram_id)
    )
    """)

    cur.execute("""
    INSERT INTO rate (id)
    VALUES (1)
    ON CONFLICT (id) DO NOTHING
    """)

    # LOCATIONS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS locations (
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE,
        is_active INTEGER DEFAULT 1
    )
    """)

    # REQUESTS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS requests (
        id SERIAL PRIMARY KEY,
        created_by BIGINT REFERENCES users(telegram_id),
        amount REAL,
        currency TEXT CHECK(currency IN ('EUR','RSD')),
        rate_requested REAL,
        due_time TEXT,
        location_id INTEGER REFERENCES locations(id),
        status TEXT CHECK(status IN ('DRAFT','SENT','APPROVED','REJECTED')) DEFAULT 'DRAFT',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        admin_note TEXT
    )
    """)

    con.commit()
    con.close()


# ================= HELPERS ==================

def get_user(user_id):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT role, is_active FROM users WHERE telegram_id=%s", (user_id,))
    r = cur.fetchone()
    con.close()
    return r  # (role, is_active) or None


def get_role(uid):
    u = get_user(uid)
    return u[0] if u and u[1] == 1 else None


def is_admin(uid):
    return get_role(uid) == "ADMIN"


def get_rate():
    con = db()
    cur = con.cursor()
    cur.execute("SELECT buy_rate, sell_rate, updated_at FROM rate WHERE id=1")
    r = cur.fetchone()
    con.close()
    if not r or r[0] is None or r[1] is None:
        return None
    return r


def get_locations():
    con = db()
    cur = con.cursor()
    cur.execute("SELECT name FROM locations WHERE is_active=1")
    r = [x[0] for x in cur.fetchall()]
    con.close()
    return r


# ================= CONFIRM HANDLER ==================

async def confirm_handler(update: Update, ctx):
    query = update.callback_query
    uid = query.from_user.id
    await query.answer()

    if uid not in pending_confirm:
        return await query.edit_message_text("❌ Nema pending akcije.")

    action = pending_confirm.pop(uid)

    if query.data == "CANCEL":
        return await query.edit_message_text("❌ Akcija je otkazana.")

    con = db()
    cur = con.cursor()

    # ===== CONFIRM RATE =====
    if action["type"] == "SET_RATE":
        buy, sell = action["data"]
        cur.execute("UPDATE rate SET buy_rate=%s, sell_rate=%s, updated_at=%s, updated_by=%s WHERE id=1",
                    (buy, sell, datetime.now().isoformat(), uid))
        con.commit()
        con.close()
        return await query.edit_message_text(f"✅ Kurs postavljen\nKupovni={buy}\nProdajni={sell}")

    # ===== ADD USER =====
    if action["type"] == "ADD_USER":
        tgid, role, username = action["data"]
        try:
            cur.execute("""
                INSERT INTO users(telegram_id, role, is_active, username)
                VALUES (%s, %s, 1, %s)
            """, (tgid, role, username))

            con.commit()
            return await query.edit_message_text(
                f"✅ Korisnik je uspešno dodat:\n\n"
                f"ID: {tgid}\n"
                f"Role: {role}\n"
                f"Username: {username}"
            )

        except psycopg2.errors.UniqueViolation:
            return await query.edit_message_text("❌ Korisnik sa tim telegram_id već postoji.")

        finally:
            con.close()

    # ===== DELETE USER =====
    if action["type"] == "DELETE_USER":
        tgid = action["data"]
        cur.execute("SELECT telegram_id FROM users WHERE telegram_id=%s", (tgid,))
        user = cur.fetchone()

        if not user:
            con.close()
            return await query.edit_message_text(
                f"❌ Korisnik sa ID {tgid} ne postoji u bazi."
            )

        # delete
        cur.execute("DELETE FROM users WHERE telegram_id=%s", (tgid,))
        con.commit()
        con.close()

        return await query.edit_message_text(f"✅ Korisnik {tgid} je uspešno obrisan.")

    # ===== ADD LOCATION =====
    if action["type"] == "ADD_LOCATION":
        name = action["data"]
        cur.execute("INSERT INTO locations(name) VALUES(%s) ON CONFLICT (name) DO NOTHING", (name,))
        con.commit()
        con.close()
        return await query.edit_message_text(f"✅ Lokacija {name} je uspešno dodata.")

    # ===== CONFIRM REQUEST =====
    if action["type"] == "USER_REQUEST":
        msg = action["data"]

        # send adminu
        await ctx.bot.send_message(ADMIN_ID, msg)

        # send u grupu
        GROUP_ID = -5021696516
        await ctx.bot.send_message(GROUP_ID, msg)

        con.close()
        return await query.edit_message_text("✅ Zahtev je poslat adminu i u grupu.")


# ================= COMMANDS ADMIN ==================

def private_only(func):
    async def wrapper(update, ctx):
        if update.effective_chat.type != "private":
            return  # ignorise komande iz grupa
        return await func(update, ctx)
    return wrapper

def admin_contact_text():
    return f'\n\n📩 <a href="tg://user?id={ADMIN_ID}">Kontaktirajte admina</a>'


def confirm_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Potvrdi", callback_data="CONFIRM")],
        [InlineKeyboardButton("❌ Otkaži", callback_data="CANCEL")]
    ])


async def unknown_command(update, ctx):
    uid = update.effective_user.id
    role = get_role(uid)

    if not role:
        return await update.message.reply_text(
            "❌ Nemate pristup ovom botu."
        )

    if role == "ADMIN":
        commands = get_admin_commands()
    else:
        commands = get_user_commands()

    msg = (
        "❗ Neispravna komanda\n\n"
        "Dostupne komande:\n"
        f"{commands}"
    )

    await update.message.reply_text(msg)


def get_admin_commands():
    return (
        """
        /kurs_evra BUY_RATE SELL_RATE  
        ➡️ Postavlja dnevni kupovni i prodajni kurs evra i pamti vreme izmene.

        /add TELEGRAM_ID ROLE USERNAME
        ➡️ Dodaje novog korisnika u sistem.

        /delete TELEGRAM_ID  
        ➡️ Briše korisnika iz sistema.

        /list_users  
        ➡️ Prikazuje sve korisnike u bazi.

        /add_location NAZIV_LOKACIJE  
        ➡️ Dodaje novu lokaciju.

        /list_locations  
        ➡️ Prikazuje sve lokacije i njihov status (active/deactivated).

        /help  
        ➡️ Lista komandi dostupnih adminu.
        """
    )


async def admin_start(update, ctx):
    uid = update.effective_user.id
    role = get_role(uid)

    print("USER ID:", update.effective_user.id)

    # ako nije admin → prebaci na user start
    if role != "ADMIN":
        return await start(update, ctx)

    msg = """
        👋 Dobrodošli, ADMIN!

        Dostupne komande:

        /kurs_evra BUY_RATE SELL_RATE  
        ➡️ Postavlja dnevni kupovni i prodajni kurs evra i pamti vreme izmene.

        /add TELEGRAM_ID ROLE USERNAME  
        ➡️ Dodaje novog korisnika u sistem.

        /delete TELEGRAM_ID  
        ➡️ Briše korisnika iz sistema.

        /list_users  
        ➡️ Prikazuje sve korisnike u bazi.

        /add_location NAZIV_LOKACIJE  
        ➡️ Dodaje novu lokaciju.

        /list_locations  
        ➡️ Prikazuje sve lokacije i njihov status (active/deactivated).

        /help  
        ➡️ Lista komandi dostupnih adminu.
        """
    await update.message.reply_text(msg)


async def kurs_set(update, ctx):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("❌ Nemate prava pristupa." + admin_contact_text(), parse_mode="HTML")

    try:
        buy = float(ctx.args[0])
        sell = float(ctx.args[1])
    except:
        return await update.message.reply_text("Format: /kurs_evra BUY_RATE SELL_RATE")

    con = db()
    cur = con.cursor()
    cur.execute("""
        UPDATE rate 
        SET buy_rate=%s, sell_rate=%s, updated_at=%s, updated_by=%s
        WHERE id=1
    """, (buy, sell, datetime.now().isoformat(), update.effective_user.id))

    con.commit()
    con.close()

    await update.message.reply_text("✅ Dnevni kurs evra je ažuriran.")


async def add_user(update, ctx):
    uid = update.effective_user.id

    # only admin
    if not is_admin(uid):
        return await update.message.reply_text("❌ Nemate prava pristupa." + admin_contact_text(), parse_mode="HTML")

    if len(ctx.args) < 3:
        return await update.message.reply_text(
            "❌ Neispravan format:\n\n"
            "/add TELEGRAM_ID ROLE USERNAME\n"
            "Primer: /add 123456789 USER petar\n"
        )

    # parse args
    try:
        tgid = int(ctx.args[0])
    except ValueError:
        return await update.message.reply_text("❌ TELEGRAM_ID mora biti broj.")

    role = ctx.args[1].upper()
    username = ctx.args[2]

    # validate role
    if role not in ["USER", "ADMIN"]:
        return await update.message.reply_text("❌ Role mora biti USER ili ADMIN.")

    if not re.match(r"^[a-zA-Z0-9_]{3,32}$", username):
        return await update.message.reply_text(
            "❌ Username mora imati 3-32 karaktera (slova, brojevi, _)."
        )

    pending_confirm[uid] = {"type": "ADD_USER", "data": (tgid, role, username)}

    await update.message.reply_text(
        f"Dodati korisnika?\nID={tgid}\nRole={role}\nUsername={username}\n\n"
        f"Klikni potvrdi ili otkaži.",
        reply_markup=confirm_keyboard()
    )


async def del_user(update, ctx):
    uid = update.effective_user.id

    # permission check
    if not is_admin(uid):
        return await update.message.reply_text("❌ Nemate prava pristupa." + admin_contact_text(), parse_mode="HTML")

    # format check
    if len(ctx.args) != 1:
        return await update.message.reply_text(
            "❌ Neispravan format.\n\n"
            "/delete TELEGRAM_ID\n"
            "Primer:\n"
            "/delete 123456789"
        )

    # parse telegram_id
    try:
        tgid = int(ctx.args[0])
    except ValueError:
        return await update.message.reply_text("❌ TELEGRAM_ID mora biti broj.")

    # admin ne sme da obrise sebe
    if tgid == uid:
        return await update.message.reply_text("❌ Ne možeš obrisati samog sebe.")

    pending_confirm[uid] = {"type": "DELETE_USER", "data": tgid}

    await update.message.reply_text(
        f"Obrisati user {tgid}?\n\n"
        f"Klikni potvrdi ili otkaži.",
        reply_markup=confirm_keyboard()
    )


async def list_users(update, ctx):
    uid = update.effective_user.id

    # permission check
    if not is_admin(uid):
        return await update.message.reply_text("❌ Nemate prava pristupa." + admin_contact_text(), parse_mode="HTML")

    con = db()
    cur = con.cursor()
    cur.execute("SELECT telegram_id, role, username FROM users")
    rows = cur.fetchall()
    con.close()

    msg = "👥 Lista korisnika:\n\n"
    for tgid, role, username in rows:
        msg += f"• ID: <code>{tgid}</code>\n  Role: {role}\n  Username: @{username}\n\n"

    await update.message.reply_text(msg, parse_mode="HTML")


async def add_location(update, ctx):
    uid = update.effective_user.id

    # permission check
    if not is_admin(uid):
        return await update.message.reply_text("❌ Nemate prava pristupa." + admin_contact_text(), parse_mode="HTML")

    # format check
    if not ctx.args:
        return await update.message.reply_text(
            "❌ Neispravan format.\n\n"
            "/add_location NAZIV_LOKACIJE\n"
            "Primer: /add_location Beograd Centar"
        )

    name = " ".join(ctx.args)

    pending_confirm[uid] = {"type": "ADD_LOCATION", "data": name}

    await update.message.reply_text(
        f"Dodati lokaciju: {name}?\n\n"
        "Klikni potvrdi ili otkaži.",
        reply_markup=confirm_keyboard()
    )


def admin_locations_keyboard(rows):
    keyboard = []

    for loc_id, name, active in rows:
        if active:
            btn_text = f"🛑 Disable {name}"
            action = "DISABLE"
        else:
            btn_text = f"▶️ Enable {name}"
            action = "ENABLE"

        keyboard.append([
            InlineKeyboardButton(
                btn_text,
                callback_data=f"ADMIN_LOC_{action}:{loc_id}"
            )
        ])

    return InlineKeyboardMarkup(keyboard)


async def list_locations(update, ctx):
    uid = update.effective_user.id

    if not is_admin(uid):
        return await update.message.reply_text("❌ Nemate prava pristupa." + admin_contact_text(), parse_mode="HTML")

    con = db()
    cur = con.cursor()
    cur.execute("SELECT id, name, is_active FROM locations ORDER BY is_active DESC, name")
    rows = cur.fetchall()
    con.close()

    if not rows:
        return await update.message.reply_text("⚠️ Nema lokacija u bazi.")

    msg = "📍 *LISTA LOKACIJA*\n\n"

    for loc_id, name, active in rows:
        if active:
            msg += f"🟢 *{name}* \n"
        else:
            msg += f"🔴 {name} \n"

    msg += "\nKlikni dugme ispod za enable/disable."

    await update.message.reply_text(
        msg,
        reply_markup=admin_locations_keyboard(rows),
        parse_mode="Markdown"
    )


async def admin_location_toggle_handler(update: Update, ctx):
    query = update.callback_query
    uid = query.from_user.id
    await query.answer()

    if not is_admin(uid):
        return await query.edit_message_text("❌ Nemate prava pristupa." + admin_contact_text(), parse_mode="HTML")

    # ===== ENABLE / DISABLE =====
    action, loc_id = query.data.replace("ADMIN_LOC_", "").split(":")
    loc_id = int(loc_id)

    con = db()
    cur = con.cursor()

    if action == "ENABLE":
        cur.execute("UPDATE locations SET is_active=1 WHERE id=%s", (loc_id,))
    else:
        cur.execute("UPDATE locations SET is_active=0 WHERE id=%s", (loc_id,))

    con.commit()

    # ===== RELOAD LOCATIONS =====
    cur.execute("SELECT id, name, is_active FROM locations ORDER BY is_active DESC, name")
    rows = cur.fetchall()
    con.close()

    # rebuild text
    msg = "📍 *LISTA LOKACIJA*\n\n"
    for loc_id, name, active in rows:
        if active:
            msg += f"🟢 *{name}*\n"
        else:
            msg += f"🔴 {name}\n"

    msg += "\nKlikni dugme ispod za aktivaciju/deaktivaciju."

    # EDIT FULL MESSAGE (TEXT + BUTTONS)
    await query.edit_message_text(
        msg,
        reply_markup=admin_locations_keyboard(rows),
        parse_mode="Markdown"
    )


async def admin_help(update, ctx):
    if not is_admin(update.effective_user.id):
        return
    msg = """
        Dostupne komande:

        /kurs_evra BUY_RATE SELL_RATE  
        ➡️ Postavlja dnevni kupovni i prodajni kurs evra i pamti vreme izmene.

        /add TELEGRAM_ID ROLE USERNAME
        ➡️ Dodaje novog korisnika u sistem.

        /delete TELEGRAM_ID  
        ➡️ Briše korisnika iz sistema.

        /list_users  
        ➡️ Prikazuje sve korisnike u bazi.

        /add_location NAZIV_LOKACIJE  
        ➡️ Dodaje novu lokaciju.

        /list_locations  
        ➡️ Prikazuje sve lokacije i njihov status (active/deactivated).
        """
    await update.message.reply_text(msg)


# ================= USER ==================

def get_user_commands():
    return (
        """
       /kurs_evra  
       ➡️ Prikazuje trenutni kurs evra i datum ažuriranja.
       ➡️ Nakon toga unosite zahtev u formatu:
          IZNOS,EUR/RSD,KURS,ROK

       Primer:
       1000,EUR,117.2,18.00

       Zatim birate lokaciju i potvrđujete zahtev.
       """
    )


async def start(update, ctx):
    uid = update.effective_user.id
    role = get_role(uid)

    if not role:
        return await update.message.reply_text(
            "❌ Nemate prava pristupa." + admin_contact_text(), parse_mode="HTML"
        )

    # ako je admin, prebaci na admin_start
    if role == "ADMIN":
        return await admin_start(update, ctx)

    msg = """
        👋 Dobrodošli, USER!

        Dostupne komande:

        /kurs_evra  
        ➡️ Prikazuje trenutni kurs evra i datum ažuriranja.
        ➡️ Nakon toga unosite zahtev u formatu:
           IZNOS,EUR/RSD,KURS,ROK

        Primer:
        1000,EUR,117.2,18.00

        Zatim birate lokaciju i potvrđujete zahtev.
        """
    await update.message.reply_text(msg)


async def kurs_get(update, ctx):
    role = get_role(update.effective_user.id)
    if not role:
        return await update.message.reply_text("❌ Nemate prava pristupa." + admin_contact_text(), parse_mode="HTML")

    k = get_rate()
    if not k:
        return await update.message.reply_text("❌ Kurs nije postavljen." + admin_contact_text(), parse_mode="HTML")

    buy, sell, time_str = k

    # proveri da li je kurs postavljen danas
    if not buy or not sell or not time_str:
        return await update.message.reply_text(
            "❌ Kurs još nije postavljen danas." + admin_contact_text(), parse_mode="HTML"
        )

    try:
        dt = datetime.fromisoformat(str(time_str))
        today = datetime.now().date()

        if dt.date() != today:
            return await update.message.reply_text(
                "❌ Kurs još nije postavljen danas." + admin_contact_text(),
                parse_mode="HTML"
            )

        formatted_time = dt.strftime("%d.%m.%Y. %H:%M")

    except:
        formatted_time = time_str

    await update.message.reply_text(
        f"💱 Kurs evra:\n"
        f"Kupovni: {buy}\n"
        f"Prodajni: {sell}\n"
        f"Ažurirano: {formatted_time}\n\n"
        "Unesite zahtev u formatu:\n"
        "IZNOS,VALUTA(EUR/RSD),KURS,ROK\n"
        "Primer:\n"
        "1000,EUR,117.2,18.00"
    )


async def kurs_evra(update, ctx):
    uid = update.effective_user.id

    # USER MODE
    if not is_admin(uid):
        return await kurs_get(update, ctx)

    # ADMIN MODE
    if len(ctx.args) != 2:
        return await update.message.reply_text(
            "Format: /kurs_evra BUY SELL\nPrimer: /kurs_evra 117.2 118.0"
        )

    buy_str = ctx.args[0]
    sell_str = ctx.args[1]

    # Provera da li korisnik koristi zarez
    if "," in buy_str or "," in sell_str:
        return await update.message.reply_text(
            "❌ Koristite tačku (.) kao decimalni separator, a ne zarez (,).\n"
            "Primer: /kurs_evra 117.25 118.0"
        )

    try:
        buy = float(buy_str)
        sell = float(sell_str)
    except ValueError:
        return await update.message.reply_text("❌ Kurs mora biti broj.")

    # ===== BASIC LOGIC CHECK =====
    if buy >= sell:
        return await update.message.reply_text("❌ Kupovni kurs mora biti manji od prodajnog.")

    # ===== RANGE CHECK =====
    if not (MIN_BUY_RATE <= buy <= MAX_BUY_RATE):
        return await update.message.reply_text(
            f"❌ Kupovni kurs mora biti između {MIN_BUY_RATE} i {MAX_BUY_RATE} RSD."
        )

    spread = sell - buy
    if not (MIN_SPREAD <= spread <= MAX_SPREAD):
        return await update.message.reply_text(
            f"❌ Razlika kupovni/prodajni mora biti između {MIN_SPREAD} i {MAX_SPREAD}."
        )

    # SAVE TEMP
    pending_confirm[uid] = {"type": "SET_RATE", "data": (buy, sell)}

    await update.message.reply_text(
        f"⚠️ Potvrdi novi kurs:\n\n"
        f"Kupovni: {buy}\n"
        f"Prodajni: {sell}\n\n"
        f"Klikni potvrdi ili otkaži.",
        reply_markup=confirm_keyboard()
    )


def validate_request(parts):
    try:
        iznos = float(parts[0])
    except:
        return "❌ Iznos mora biti broj."

    # ===== VALUTA =====
    valuta = parts[1].upper()
    if valuta not in ["EUR", "RSD"]:
        return "❌ Valuta mora biti EUR ili RSD."

    try:
        kurs = float(parts[2])
    except:
        return "❌ Kurs mora biti broj."

    # ===== PROVERA KURSA PREMA ADMIN POSTAVLJENOM =====
    current_rate = get_rate()  # (buy, sell, updated_at)
    if current_rate is None:
        return "❌ Kurs nije postavljen."

    buy, sell, _ = current_rate
    if not (buy <= kurs <= sell):
        return f"❌ Kurs mora biti između trenutnog kupovnog i prodajnog kursa:\nKupovni={buy}, Prodajni={sell}"

    # ===== VREME VALIDACIJA =====
    time_str = parts[3]

    # Ako je uneo :
    if ":" in time_str:
        return "❌ Vreme mora biti u formatu HH.MM (koristite tačku, ne dvotačku).\nPrimer: 15.00"

    # Regex za HH.MM
    if not re.match(r"^\d{2}\.\d{2}$", time_str):
        return "❌ Vreme mora biti u formatu HH.MM.\nPrimer: 15.00"

    hh, mm = map(int, time_str.split("."))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return "❌ Vreme mora biti između 00.00 i 23.59."

    return None


# ================= MESSAGE FLOW ==================

pending_requests = {}


async def handle_text(update, ctx):
    if update.effective_chat.type != "private":
        return  # ne reaguje na poruke u grupi

    uid = update.effective_user.id
    role = get_role(uid)
    if not role:
        return

    text = update.message.text

    # parse input
    if "," in text and uid not in pending_requests:
        parts = [p.strip() for p in text.split(",")]

        if len(parts) < 4:
            return await update.message.reply_text(
                "❌ Neispravan format.\n"
                "Primer:\n1000,EUR,117.2,18.00"
            )

        if len(parts) > 4:
            return await update.message.reply_text(
                "❌ Iznos i kurs ne smeju sadržati zarez (,).\n"
                "Koristite tačku (.) kao decimalni separator.\n"
                "Primer: 117.25"
            )

        error = validate_request(parts)
        if error:
            return await update.message.reply_text(
                error + "\n\nIspravan format:\n1000,EUR,117.2,18.00"
            )

        pending_requests[uid] = parts

        locs = get_locations()
        keyboard = [[InlineKeyboardButton(l, callback_data=f"LOC_{l}")] for l in locs]

        await update.message.reply_text(
            "📍 Izaberite lokaciju:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # fallback - random text
    return await update.message.reply_text(
        "❗ Sve komande moraju početi sa /\n\n"
        "Dostupne komande:\n" +
        (get_admin_commands() if is_admin(uid) else get_user_commands())
    )


async def location_handler(update: Update, ctx):
    query = update.callback_query
    uid = query.from_user.id
    await query.answer()

    if not query.data.startswith("LOC_"):
        return

    location = query.data.replace("LOC_", "")
    data = pending_requests.pop(uid)

    iznos, valuta, kurs, rok = data
    iznos_f = float(iznos)
    kurs_f = float(kurs)

    if valuta.upper() == "EUR":
        spremiti = iznos_f * kurs_f
        spremiti_valuta = "RSD"
    else:
        spremiti = iznos_f / kurs_f
        spremiti_valuta = "EUR"

    msg = (
        f"📩 Novi zahtev:\n\n"
        f"Klijentu spremiti: {spremiti:.2f} {spremiti_valuta}\n"
        f"Klijent donosi: {iznos_f} {valuta.upper()}\n"
        f"Lokacija: {location}\n"
        f"Rok: {rok}\n"
        f"Kreirao: @{query.from_user.username} ({uid})"
    )

    pending_confirm[uid] = {"type": "USER_REQUEST", "data": msg}

    await query.edit_message_text(
        msg + "\n\nPotvrdi slanje adminu:",
        reply_markup=confirm_keyboard()
    )


# ================= MAIN ==================

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    # start
    app.add_handler(CommandHandler("start", private_only(start)))

    # kurs
    app.add_handler(CommandHandler("kurs_evra", private_only(kurs_evra)))

    # admin komande
    app.add_handler(CommandHandler("add", private_only(add_user)))
    app.add_handler(CommandHandler("delete", private_only(del_user)))
    app.add_handler(CommandHandler("list_users", private_only(list_users)))
    app.add_handler(CommandHandler("add_location", private_only(add_location)))
    app.add_handler(CommandHandler("list_locations", private_only(list_locations)))
    app.add_handler(CommandHandler("help", private_only(admin_help)))

    app.add_handler(CallbackQueryHandler(admin_location_toggle_handler, pattern="^ADMIN_LOC_"))
    app.add_handler(CallbackQueryHandler(location_handler, pattern="^LOC_"))
    app.add_handler(CallbackQueryHandler(confirm_handler))

    # text handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    print("Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()


