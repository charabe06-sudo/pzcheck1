from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash
from functools import wraps
import sqlite3
import re
import requests
import concurrent.futures
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

from checkers import get_gateway_list, run_check

app = Flask(__name__)
app.secret_key = "change-this-to-a-random-secret-key"

ADMIN_USERNAME = "parwar"
DB_PATH = "users.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            credits INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    try:
        c.execute("ALTER TABLE users ADD COLUMN credits INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS live_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            gateway TEXT,
            card_number TEXT,
            response TEXT,
            bin TEXT,
            brand TEXT,
            card_type TEXT,
            bank TEXT,
            country TEXT,
            credits_charged INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bin_cache (
            bin TEXT PRIMARY KEY,
            brand TEXT,
            card_type TEXT,
            bank TEXT,
            country TEXT,
            cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def get_user_by_username(username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, username, password_hash, credits, created_at, banned FROM users WHERE username = ?", (username,))
    user = c.fetchone()
    conn.close()
    return user

def get_user_by_id(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, username, password_hash, credits, created_at, banned FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()
    conn.close()
    return user

def create_user(username, password_hash):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO users (username, password_hash, credits) VALUES (?, ?, 0)", (username, password_hash))
    conn.commit()
    conn.close()

def update_password(user_id, new_hash):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user_id))
    conn.commit()
    conn.close()

def get_credits(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def deduct_credits(user_id, amount):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET credits = credits - ? WHERE id = ? AND credits >= ?", (amount, user_id, amount))
    changed = c.rowcount
    conn.commit()
    conn.close()
    return changed > 0

def add_credits(user_id, amount):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET credits = credits + ? WHERE id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def _brand_from_digit(digits):
    if not digits:
        return "Unknown"
    first = digits[0]
    if first == "4":
        return "VISA"
    if first in ("5", "2"):
        return "Mastercard"
    if first == "3":
        return "Amex"
    if first == "6":
        return "Discover"
    return "Unknown"

def _cache_get(bin_num):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT brand, card_type, bank, country FROM bin_cache WHERE bin = ?", (bin_num,))
    row = c.fetchone()
    conn.close()
    return row

def _cache_set(bin_num, brand, card_type, bank, country):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO bin_cache (bin, brand, card_type, bank, country, cached_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)", (bin_num, brand, card_type, bank, country))
    conn.commit()
    conn.close()

def lookup_bin(card_number):
    digits = re.sub(r"\D", "", card_number)
    if len(digits) < 6:
        return {"bin": digits, "brand": "Unknown", "card_type": "Unknown", "bank": "Unknown", "country": "Unknown"}
    bin_num = digits[:6]
    cached = _cache_get(bin_num)
    if cached:
        return {"bin": bin_num, "brand": cached[0] or "Unknown", "card_type": cached[1] or "Unknown", "bank": cached[2] or "Unknown", "country": cached[3] or "Unknown"}
    brand = _brand_from_digit(digits)
    card_type = "Unknown"
    bank = "Unknown"
    country = "Unknown"
    try:
        resp = requests.get("https://lookup.binlist.net/" + bin_num, timeout=3, headers={"Accept-Version": "3", "User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            data = resp.json()
            brand = (data.get("scheme") or brand).upper()
            card_type = (data.get("type") or "Unknown").capitalize()
            bank = (data.get("bank") or {}).get("name") or "Unknown"
            country_data = data.get("country") or {}
            c_name = country_data.get("name") or "Unknown"
            c_alpha = (country_data.get("alpha2") or "").lower()
            if c_name != "Unknown":
                country = c_name + (" " + c_alpha if c_alpha else "")
    except Exception:
        pass
    _cache_set(bin_num, brand, card_type, bank, country)
    return {"bin": bin_num, "brand": brand, "card_type": card_type, "bank": bank, "country": country}

def save_live_card(user_id, gateway, card_number, response, credits_charged=3):
    info = lookup_bin(card_number)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO live_cards
        (user_id, gateway, card_number, response, bin, brand, card_type, bank, country, credits_charged)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, gateway, card_number, response, info["bin"], info["brand"], info["card_type"], info["bank"], info["country"], credits_charged))
    conn.commit()
    conn.close()

def get_live_cards(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, gateway, card_number, response, bin, brand, card_type, bank, country, credits_charged, created_at FROM live_cards WHERE user_id = ? ORDER BY id DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def delete_live_card(user_id, card_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM live_cards WHERE id = ? AND user_id = ?", (card_id, user_id))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, username, credits, created_at, banned FROM users ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_live_cards():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT lc.id, lc.card_number, lc.gateway, lc.response, lc.brand, lc.bank, lc.country, lc.credits_charged, lc.created_at, u.username
        FROM live_cards lc JOIN users u ON u.id = lc.user_id
        ORDER BY lc.id DESC LIMIT 100
    """)
    rows = c.fetchall()
    conn.close()
    return rows

def get_admin_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users WHERE banned = 0")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM live_cards")
    total_cards = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM live_cards WHERE created_at >= date('now')")
    today_cards = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE created_at >= date('now') AND banned = 0")
    today_users = c.fetchone()[0]
    conn.close()
    return {"total_users": total_users, "total_cards": total_cards, "today_cards": today_cards, "today_users": today_users}

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please login first", "error")
            return redirect(url_for("login"))
        
        # Check if user is banned mid-session
        user = get_user_by_id(session["user_id"])
        if not user or user[5] == 1:
            session.clear()
            flash("Your account has been banned.", "error")
            return redirect(url_for("login"))
            
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please login first", "error")
            return redirect(url_for("login"))
            
        user = get_user_by_id(session["user_id"])
        if not user or user[5] == 1:
            session.clear()
            flash("Your account has been banned.", "error")
            return redirect(url_for("login"))
            
        if session.get("username") != ADMIN_USERNAME:
            flash("Access denied - admin only", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated_function

@app.context_processor
def inject_credits():
    if "user_id" in session:
        return {"current_user_credits": get_credits(session["user_id"])}
    return {"current_user_credits": 0}

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        
        existing_user = get_user_by_username(username)
        if existing_user and existing_user[5] == 1:
            flash("This account has been permanently banned.", "error")
            return render_template("register.html")

        if not username or not password:
            flash("Username and password are required", "error")
        elif len(username) < 3:
            flash("Username must be at least 3 characters", "error")
        elif len(password) < 6:
            flash("Password must be at least 6 characters", "error")
        elif password != confirm:
            flash("Passwords do not match", "error")
        elif existing_user:
            flash("Username already exists", "error")
        else:
            password_hash = generate_password_hash(password)
            create_user(username, password_hash)
            user = get_user_by_username(username)
            session["user_id"] = user[0]
            session["username"] = user[1]
            flash("Account created! Contact admin to add credits.", "success")
            return redirect(url_for("dashboard"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        
        user = get_user_by_username(username)
        if user and user[5] == 1:
            flash("This account has been permanently banned.", "error")
            return render_template("login.html")

        if user and check_password_hash(user[2], password):
            session["user_id"] = user[0]
            session["username"] = user[1]
            flash("Welcome back!", "success")
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid username or password", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out", "info")
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", username=session.get("username"))

@app.route("/live")
@login_required
def live():
    cards = get_live_cards(session["user_id"])
    return render_template("live.html", username=session.get("username"), cards=cards)

@app.route("/live/delete/<int:card_id>", methods=["POST"])
@login_required
def live_delete(card_id):
    delete_live_card(session["user_id"], card_id)
    return jsonify({"ok": True})

@app.route("/settings")
@login_required
def settings():
    user = get_user_by_id(session["user_id"])
    if not user or user[5] == 1:
        session.clear()
        return redirect(url_for("login"))
    member_since = "Unknown"
    if user[4]:
        try:
            dt = datetime.strptime(user[4].split(".")[0], "%Y-%m-%d %H:%M:%S")
            member_since = dt.strftime("%b %Y")
        except Exception:
            member_since = user[4][:7]
    return render_template(
        "settings.html",
        username=user[1],
        member_since=member_since,
        telegram_username="@" + user[1] + "_tg",
        telegram_id="612345678",
        telegram_active=True,
        active_tab=request.args.get("tab", "account")
    )

@app.route("/settings/password", methods=["POST"])
@login_required
def change_password():
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    user = get_user_by_id(session["user_id"])
    if not user:
        session.clear()
        return redirect(url_for("login"))
    if not current or not new or not confirm:
        flash("All fields are required", "error")
    elif not check_password_hash(user[2], current):
        flash("Current password is incorrect", "error")
    elif len(new) < 6:
        flash("New password must be at least 6 characters", "error")
    elif new != confirm:
        flash("New passwords do not match", "error")
    elif new == current:
        flash("New password must be different from current", "error")
    else:
        update_password(user[0], generate_password_hash(new))
        flash("Password updated successfully", "success")
    return redirect(url_for("settings", tab="account"))

@app.route("/prices")
@login_required
def prices():
    return render_template("prices.html", username=session.get("username"))

@app.route("/admin")
@admin_required
def admin():
    return render_template(
        "admin.html",
        username=session.get("username"),
        stats=get_admin_stats(),
        users=get_all_users(),
        cards=get_all_live_cards(),
    )

@app.route("/admin/add-credits", methods=["POST"])
@admin_required
def admin_add_credits():
    data = request.get_json()
    user_id = int(data.get("user_id", 0))
    amount = int(data.get("amount", 0))
    
    if amount > 100000 or amount < -100000:
        return jsonify({"error": "Invalid amount"}), 400
        
    if amount != 0:
        add_credits(user_id, amount)
        
    return jsonify({"ok": True})

@app.route("/admin/reset-credits", methods=["POST"])
@admin_required
def admin_reset_credits():
    data = request.get_json()
    user_id = int(data.get("user_id", 0))
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET credits = 0 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    
    return jsonify({"ok": True})

@app.route("/admin/ban-user", methods=["POST"])
@admin_required
def admin_ban_user():
    data = request.get_json()
    user_id = int(data.get("user_id", 0))
    username = data.get("username", "")
    
    if username == ADMIN_USERNAME:
        return jsonify({"error": "Cannot ban yourself"}), 400
        
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Set user as banned
    c.execute("UPDATE users SET banned = 1 WHERE id = ?", (user_id,))
    # Delete their live cards
    c.execute("DELETE FROM live_cards WHERE user_id = ?", (user_id,))
    
    conn.commit()
    conn.close()
    
    return jsonify({"ok": True})

@app.route("/gateways", methods=["GET"])
@login_required
def gateways():
    gateway_list = get_gateway_list()
    proxy_count = 0
    try:
        from checkers import stripe_park
        proxy_count = stripe_park.get_proxy_count()
    except Exception:
        pass
    return jsonify({"gateways": gateway_list, "proxy_count": proxy_count})

@app.route("/check-batch", methods=["POST"])
@login_required
def check_batch():
    try:
        data = request.get_json()
        cards = data.get("cards", [])
        gateway_id = data.get("gateway", "").strip()

        if not cards or not gateway_id:
            return jsonify({"error": "Missing cards or gateway"}), 400

        available = get_credits(session["user_id"])
        if available < 3:
            return jsonify({"error": "Insufficient credits (need 3). Please top up."}), 403

        gateways_list = get_gateway_list()
        gw_name = next((g["name"] for g in gateways_list if g["id"] == gateway_id), gateway_id)
        user_id = session["user_id"]

        if gateway_id == "stripe_park":
            from checkers import stripe_park
            results = stripe_park.check_batch(cards)
        else:
            results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                futures = {executor.submit(run_check, gateway_id, card): card for card in cards}
                for future in concurrent.futures.as_completed(futures, timeout=25):
                    card = futures[future]
                    try:
                        results.append(future.result(timeout=1))
                    except Exception as e:
                        results.append({"url": card, "status": "error", "error": str(e)})

        for r in results:
            if r.get("status") == "alive":
                if deduct_credits(user_id, 3):
                    response_text = r.get("redirects_to") or r.get("error") or "Approved"
                    try:
                        save_live_card(user_id, gw_name, r["url"], response_text, 3)
                    except Exception:
                        pass
                else:
                    r["error"] = "Insufficient credits to save this live card"
                    r["status"] = "error"

        updated_credits = get_credits(user_id)
        return jsonify({"results": results, "credits": updated_credits})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Server error: " + str(e)[:150]}), 500

init_db()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000, threaded=True)