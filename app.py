import csv
import io
import os
import sqlite3
from collections import defaultdict
from datetime import date

from flask import Flask, g, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash


def format_inr(value):
    return f"₹{float(value):,.2f}"

app = Flask(__name__)


def refresh_database_config():
    app.config["DATABASE_URL"] = os.getenv("DATABASE_URL")
    app.config["DATABASE"] = os.getenv("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "expenses.db"))
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key")
    app.config["HOST"] = os.getenv("HOST", "0.0.0.0")
    app.config["PORT"] = int(os.getenv("PORT", "5000"))
    app.config["DEBUG"] = os.getenv("FLASK_DEBUG", "0").lower() in {"1", "true", "yes", "on"}


refresh_database_config()


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection
        self.cursor = connection.cursor(cursor_factory=__import__("psycopg2.extras").extras.RealDictCursor)

    def execute(self, query, params=()):
        if "?" in query:
            query = query.replace("?", "%s")
        self.cursor.execute(query, params)
        return self.cursor

    def fetchall(self):
        return self.cursor.fetchall()

    def fetchone(self):
        return self.cursor.fetchone()

    def commit(self):
        self.connection.commit()

    def close(self):
        self.connection.close()

    def __getattr__(self, name):
        return getattr(self.connection, name)


def is_postgres_enabled():
    return bool(os.getenv("DATABASE_URL") or app.config.get("DATABASE_URL"))


def connect_db():
    database_url = os.getenv("DATABASE_URL") or app.config.get("DATABASE_URL")
    if database_url:
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError as exc:  # pragma: no cover - depends on deployment environment
            raise RuntimeError("psycopg2-binary is required when DATABASE_URL is set for AWS deployment.") from exc

        connection = psycopg2.connect(database_url)
        connection.autocommit = False
        return PostgresConnection(connection)

    connection = sqlite3.connect(app.config["DATABASE"])
    connection.row_factory = sqlite3.Row
    return connection


def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = connect_db()
        g._database = db
    return db


@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()
        g.pop("_database", None)


def get_table_columns(table_name):
    db = connect_db()
    try:
        if is_postgres_enabled():
            cursor = db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position",
                (table_name,),
            )
            return [row["column_name"] for row in cursor.fetchall()]

        cursor = db.execute(f"PRAGMA table_info({table_name})")
        return [row[1] for row in cursor.fetchall()]
    finally:
        db.close()


def add_missing_column(db, table_name, column_name, definition):
    try:
        columns = get_table_columns(table_name)
    except Exception:
        columns = []

    if column_name in columns:
        return

    try:
        db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
        db.commit()
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        if "duplicate column" not in str(exc).lower() and "already exists" not in str(exc).lower():
            raise

def init_db():
    db = connect_db()
    id_type = "SERIAL PRIMARY KEY" if os.environ.get("DATABASE_URL") else "INTEGER PRIMARY KEY AUTOINCREMENT"

    # 1. Users Table
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            id {id_type},
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user'
        )
    """)
    db.commit()

    add_missing_column(db, "users", "role", "TEXT NOT NULL DEFAULT 'user'")

    guest_user = db.execute("SELECT id FROM users WHERE username = ?", ("guest",)).fetchone()
    if guest_user is None:
        db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("guest", generate_password_hash("guestpassword"), "user"),
        )
        db.commit()

    admin_user = db.execute("SELECT id FROM users WHERE username = ?", ("admin",)).fetchone()
    if admin_user is None:
        db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("admin", generate_password_hash("admin123"), "admin"),
        )
        db.commit()

    # 2. Expenses Table
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS expenses (
            id {id_type},
            title TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            date TEXT NOT NULL,
            is_recurring INTEGER NOT NULL DEFAULT 0,
            user_id INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.commit()

    add_missing_column(db, "expenses", "is_recurring", "INTEGER NOT NULL DEFAULT 0")
    add_missing_column(db, "expenses", "user_id", "INTEGER NOT NULL DEFAULT 0")

    guest_row = db.execute("SELECT id FROM users WHERE username = ?", ("guest",)).fetchone()
    guest_id = guest_row["id"] if isinstance(guest_row, dict) else guest_row[0]
    db.execute("UPDATE expenses SET user_id = ? WHERE user_id = 0", (guest_id,))
    db.commit()

    # 3. Settings Table
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS settings (
            id {id_type},
            user_id INTEGER NOT NULL DEFAULT 0,
            key TEXT NOT NULL,
            value TEXT NOT NULL
        )
    """)
    db.commit()

    add_missing_column(db, "settings", "user_id", "INTEGER NOT NULL DEFAULT 0")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_settings_user_key ON settings(user_id, key)")
    db.execute("UPDATE settings SET user_id = ? WHERE user_id = 0", (guest_id,))
    db.commit()

    # 4. History Table
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS expense_history (
            id {id_type},
            expense_id INTEGER,
            action_type TEXT NOT NULL,
            description TEXT NOT NULL,
            amount REAL,
            category TEXT,
            action_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_id INTEGER NOT NULL DEFAULT 0
        )
    """)
    db.commit()
    db.close()

def get_guest_user():
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE username = ?", ("guest",)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("guest", generate_password_hash("guestpassword"), "user"),
        )
        db.commit()
        row = db.execute("SELECT * FROM users WHERE username = ?", ("guest",)).fetchone()
    if row is None:
        return None
    return dict(row)


def get_user_by_username(username):
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def is_admin_user(user):
    return bool(user and user.get("role") == "admin")


def get_season_for_date(date_value):
    try:
        month = int(date_value[5:7])
    except (TypeError, ValueError, IndexError):
        return "General"
    if month in (12, 1, 2):
        return "Winter"
    if month in (3, 4, 5):
        return "Spring"
    if month in (6, 7, 8):
        return "Summer"
    if month in (9, 10, 11):
        return "Autumn"
    return "General"


def record_expense_history(expense_id, action_type, description, user_id, amount=None, category=None):
    db = get_db()
    db.execute(
        "INSERT INTO expense_history (expense_id, action_type, description, amount, category, user_id) VALUES (?, ?, ?, ?, ?, ?)",
        (expense_id, action_type, description, amount, category, user_id),
    )
    db.commit()


def get_current_user():
    user_id = session.get("user_id")
    db = get_db()
    if user_id is None:
        guest_user = get_guest_user()
        if guest_user is None:
            return None
        return guest_user
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        session.pop("user_id", None)
        guest_user = get_guest_user()
        return guest_user
    return dict(row)


def get_setting(key, default="0"):
    db = get_db()
    user = get_current_user()
    user_id = user["id"] if user else 0
    row = db.execute("SELECT value FROM settings WHERE user_id = ? AND key = ?", (user_id, key)).fetchone()
    if row is None and user_id != 0:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"] if isinstance(row, dict) else row[0]


def set_setting(key, value):
    db = get_db()
    user = get_current_user()
    user_id = user["id"] if user else 0

    if is_postgres_enabled():
        db.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (%s, %s, %s) ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value",
            (user_id, key, str(value)),
        )
    else:
        db.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?) ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            (user_id, key, str(value)),
        )
    db.commit()


def get_expenses(category=None, month=None, search=None):
    db = get_db()
    user = get_current_user()
    user_id = user["id"] if user else 0
    query = "SELECT * FROM expenses WHERE user_id = ?"
    params = [user_id]
    clauses = []

    if category:
        clauses.append("category = ?")
        params.append(category)
    if month:
        clauses.append("substr(date, 1, 7) = ?")
        params.append(month)
    if search:
        clauses.append("title LIKE ?")
        params.append(f"%{search}%")

    if clauses:
        query += " AND " + " AND ".join(clauses)

    query += " ORDER BY date DESC, id DESC"
    return db.execute(query, params).fetchall()


def get_summary(category=None, month=None, search=None):
    db = get_db()
    user = get_current_user()
    user_id = user["id"] if user else 0
    query = "SELECT category, ROUND(CAST(SUM(amount) AS numeric), 2) AS total FROM expenses WHERE user_id = ?"
    params = [user_id]
    clauses = []

    if category:
        clauses.append("category = ?")
        params.append(category)
    if month:
        clauses.append("substr(date, 1, 7) = ?")
        params.append(month)
    if search:
        clauses.append("title LIKE ?")
        params.append(f"%{search}%")

    if clauses:
        query += " AND " + " AND ".join(clauses)

    query += " GROUP BY category ORDER BY total DESC"
    rows = db.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_dashboard_data(category=None, month=None, search=None):
    expenses = get_expenses(category, month, search)
    total_spent = round(sum(float(item["amount"]) for item in expenses), 2)

    recurring_expenses = []
    category_totals = defaultdict(float)
    monthly_totals = defaultdict(float)
    seasonal_totals = defaultdict(float)
    for expense in expenses:
        is_recurring = bool(int(expense["is_recurring"])) if "is_recurring" in expense.keys() else False
        if is_recurring:
            recurring_expenses.append({
                "id": expense["id"],
                "title": expense["title"],
                "amount": float(expense["amount"]),
                "category": expense["category"],
                "date": expense["date"],
            })
        category_totals[expense["category"]] += float(expense["amount"])
        month_value = expense["date"][:7] if len(expense["date"]) >= 7 else "N/A"
        monthly_totals[month_value] += float(expense["amount"])
        season_name = get_season_for_date(expense["date"])
        seasonal_totals[season_name] += float(expense["amount"])

    category_summary = [
        {"category": category, "total": round(value, 2)}
        for category, value in sorted(category_totals.items(), key=lambda item: item[1], reverse=True)
    ]
    monthly_summary = [
        {"month": month_value, "total": round(value, 2)}
        for month_value, value in sorted(monthly_totals.items())
    ]
    seasonal_summary = [
        {"season": season, "total": round(value, 2)}
        for season, value in sorted(seasonal_totals.items(), key=lambda item: item[1], reverse=True)
    ]

    avg_expense = round(total_spent / len(expenses), 2) if expenses else 0.0
    highest_category = max(category_summary, key=lambda item: item["total"], default={"category": "N/A", "total": 0.0})
    largest_month = max(monthly_summary, key=lambda item: item["total"], default={"month": "N/A", "total": 0.0})
    strongest_season = max(seasonal_summary, key=lambda item: item["total"], default={"season": "N/A", "total": 0.0})

    return {
        "totalSpent": total_spent,
        "averageExpense": avg_expense,
        "expenseCount": len(expenses),
        "recurringExpenseCount": len(recurring_expenses),
        "recurringExpenses": recurring_expenses,
        "categoryTotals": category_summary,
        "monthlyTotals": monthly_summary,
        "seasonalTotals": seasonal_summary,
        "topCategory": highest_category,
        "topMonth": largest_month,
        "topSeason": strongest_season,
    }


@app.route("/")
def index():
    current_user = get_current_user()
    selected_category = request.args.get("category", "").strip()
    selected_month = request.args.get("month", "").strip()
    search_term = request.args.get("q", "").strip()
    expenses = get_expenses(selected_category, selected_month, search_term)
    totals = get_summary(selected_category, selected_month, search_term)
    total_spent = sum(float(item["amount"]) for item in expenses)
    dashboard = get_dashboard_data(selected_category, selected_month, search_term)
    monthly_budget = float(get_setting("monthly_budget", "0"))
    remaining_budget = monthly_budget - total_spent

    user_id = current_user["id"] if current_user else 0
    db = get_db()
    categories = [
        row["category"] if isinstance(row, dict) else row[0]
        for row in db.execute("SELECT DISTINCT category FROM expenses WHERE user_id = ? ORDER BY category ASC", (user_id,)).fetchall()
    ]
    months = [
        row["month"] if isinstance(row, dict) else row[0]
        for row in db.execute("SELECT DISTINCT substr(date, 1, 7) AS month FROM expenses WHERE user_id = ? ORDER BY month DESC", (user_id,)).fetchall()
    ]

    return render_template(
        "index.html",
        expenses=expenses,
        totals=totals,
        total_spent=total_spent,
        dashboard=dashboard,
        summary_only=False,
        format_inr=format_inr,
        categories=categories,
        months=months,
        selected_category=selected_category,
        selected_month=selected_month,
        monthly_budget=monthly_budget,
        remaining_budget=remaining_budget,
        search_term=search_term,
        current_user=current_user,
    )


@app.route("/expenses", methods=["POST"])
def add_expense():
    title = request.form.get("title", "").strip()
    amount = request.form.get("amount", "").strip()
    category = request.form.get("category", "").strip()
    expense_date = request.form.get("date") or date.today().isoformat()
    is_recurring = request.form.get("is_recurring") in ("on", "true", "1", "yes")

    if not title or not amount or not category:
        return "Missing required expense details.", 400

    user = get_current_user()
    if user is None:
        return redirect("/login")

    db = get_db()
    cursor = db.execute(
        "INSERT INTO expenses (title, amount, category, date, is_recurring, user_id) VALUES (?, ?, ?, ?, ?, ?)",
        (title, float(amount), category, expense_date, int(is_recurring), user["id"]),
    )
    db.commit()
    record_expense_history(
        cursor.lastrowid,
        "created",
        f"Expense '{title}' was added.",
        user["id"],
        float(amount),
        category,
    )
    return redirect("/")


@app.route("/summary")
def summary():
    current_user = get_current_user()
    selected_category = request.args.get("category", "").strip()
    selected_month = request.args.get("month", "").strip()
    search_term = request.args.get("q", "").strip()
    expenses = get_expenses(selected_category, selected_month, search_term)
    totals = get_summary(selected_category, selected_month, search_term)
    total_spent = sum(float(item["amount"]) for item in expenses)
    dashboard = get_dashboard_data(selected_category, selected_month, search_term)
    monthly_budget = float(get_setting("monthly_budget", "0"))
    remaining_budget = monthly_budget - total_spent

    user_id = current_user["id"] if current_user else 0
    db = get_db()
    categories = [
        row["category"] if isinstance(row, dict) else row[0]
        for row in db.execute("SELECT DISTINCT category FROM expenses WHERE user_id = ? ORDER BY category ASC", (user_id,)).fetchall()
    ]
    months = [
        row["month"] if isinstance(row, dict) else row[0]
        for row in db.execute("SELECT DISTINCT substr(date, 1, 7) AS month FROM expenses WHERE user_id = ? ORDER BY month DESC", (user_id,)).fetchall()
    ]

    return render_template(
        "index.html",
        expenses=expenses,
        totals=totals,
        total_spent=total_spent,
        dashboard=dashboard,
        summary_only=True,
        format_inr=format_inr,
        categories=categories,
        months=months,
        selected_category=selected_category,
        selected_month=selected_month,
        monthly_budget=monthly_budget,
        remaining_budget=remaining_budget,
        search_term=search_term,
        current_user=current_user,
    )


@app.route("/expenses/<int:expense_id>/edit", methods=["POST"])
def edit_expense(expense_id):
    title = request.form.get("title", "").strip()
    amount = request.form.get("amount", "").strip()
    category = request.form.get("category", "").strip()
    expense_date = request.form.get("date") or date.today().isoformat()
    is_recurring = request.form.get("is_recurring") in ("on", "true", "1", "yes")

    if not title or not amount or not category:
        return "Missing required expense details.", 400

    user = get_current_user()
    if user is None:
        return redirect("/login")

    db = get_db()
    existing = db.execute("SELECT * FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user["id"])).fetchone()
    if existing is None:
        return redirect("/")

    db.execute(
        "UPDATE expenses SET title = ?, amount = ?, category = ?, date = ?, is_recurring = ? WHERE id = ? AND user_id = ?",
        (title, float(amount), category, expense_date, int(is_recurring), expense_id, user["id"]),
    )
    db.commit()
    record_expense_history(
        expense_id,
        "updated",
        f"Expense '{existing['title']}' was updated to '{title}'.",
        user["id"],
        float(amount),
        category,
    )
    return redirect("/")


@app.route("/expenses/<int:expense_id>/delete", methods=["POST"])
def delete_expense(expense_id):
    user = get_current_user()
    if user is None:
        return redirect("/login")

    db = get_db()
    existing = db.execute("SELECT * FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user["id"])).fetchone()
    if existing is not None:
        record_expense_history(
            expense_id,
            "deleted",
            f"Expense '{existing['title']}' was removed.",
            user["id"],
            float(existing["amount"]),
            existing["category"],
        )
    db.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user["id"]))
    db.commit()
    return redirect("/")


@app.route("/budget", methods=["POST"])
def set_budget():
    monthly_budget = request.form.get("monthly_budget", "0").strip()
    try:
        amount = float(monthly_budget)
    except ValueError:
        return "Invalid budget amount.", 400
    set_setting("monthly_budget", amount)
    return redirect("/")


@app.route("/export/csv")
def export_csv():
    expenses = get_expenses()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "title", "amount", "category", "date"])
    for expense in expenses:
        writer.writerow([
            expense["id"],
            expense["title"],
            f"{float(expense['amount']):.2f}",
            expense["category"],
            expense["date"],
        ])

    response = app.response_class(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=expenses_report.csv"
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_user_by_username(username)
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            return redirect("/")
        return render_template("login.html", error="Invalid username or password.")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or len(password) < 5:
            return render_template("register.html", error="Username and password are required. Password must be at least 5 characters.")
        if get_user_by_username(username):
            return render_template("register.html", error="Username already exists.")
        db = get_db()
        db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), "user"),
        )
        db.commit()
        user = get_user_by_username(username)
        session.clear()
        session["user_id"] = user["id"]
        return redirect("/")
    return render_template("register.html")


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect("/login")


@app.route("/admin")
def admin_panel():
    user = get_current_user()
    if user is None or not is_admin_user(user):
        return redirect("/login")

    db = get_db()
    users = db.execute("SELECT id, username, role FROM users ORDER BY username ASC").fetchall()
    recent_history = db.execute(
        "SELECT * FROM expense_history ORDER BY action_date DESC LIMIT 20"
    ).fetchall()
    summaries = get_dashboard_data()
    return render_template(
        "admin.html",
        current_user=user,
        users=users,
        recent_history=recent_history,
        summary=summaries,
        format_inr=format_inr,
    )


@app.route("/guide")
def guide():
    user = get_current_user()
    return render_template("guide.html", current_user=user, format_inr=format_inr)


@app.route("/history")
def expense_history_page():
    user = get_current_user()
    if user is None:
        return redirect("/login")

    db = get_db()
    if is_admin_user(user):
        history = db.execute("SELECT * FROM expense_history ORDER BY action_date DESC LIMIT 50").fetchall()
    else:
        history = db.execute(
            "SELECT * FROM expense_history WHERE user_id = ? ORDER BY action_date DESC LIMIT 50",
            (user["id"],),
        ).fetchall()

    return render_template("history.html", current_user=user, history=history, format_inr=format_inr)


@app.route("/api/dashboard-data")
def dashboard_data():
    selected_category = request.args.get("category", "").strip()
    selected_month = request.args.get("month", "").strip()
    search_term = request.args.get("q", "").strip()
    return jsonify(get_dashboard_data(selected_category, selected_month, search_term))


init_db()

if __name__ == "__main__":
    init_db()
    app.run(debug=app.config["DEBUG"], host=app.config["HOST"], port=app.config["PORT"])
