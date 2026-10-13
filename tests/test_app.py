import os
import tempfile
import uuid

import pytest

from app import app, init_db


@pytest.fixture
def client():
    app.config["TESTING"] = True
    db_path = os.path.join(tempfile.gettempdir(), f"expense_tracker_test_{uuid.uuid4().hex}.db")
    app.config["DATABASE"] = db_path
    with app.app_context():
        init_db()
    with app.test_client() as client:
        yield client
    if os.path.exists(db_path):
        os.remove(db_path)


def test_database_url_from_environment(monkeypatch):
    import app as app_module

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@aws-db.example.com:5432/expense_tracker")
    try:
        app_module.refresh_database_config()
        assert app_module.app.config["DATABASE_URL"] == "postgresql://user:pass@aws-db.example.com:5432/expense_tracker"
    finally:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        app_module.refresh_database_config()


def test_home_page_loads(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Expense Tracker" in response.data


def test_can_create_expense(client):
    response = client.post(
        "/expenses",
        data={
            "title": "Groceries",
            "amount": "120.50",
            "category": "Food",
            "date": "2026-09-25",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Groceries" in response.data
    assert b"120.50" in response.data


def test_summary_totals_by_category(client):
    client.post(
        "/expenses",
        data={
            "title": "Lunch",
            "amount": "25.00",
            "category": "Food",
            "date": "2026-09-25",
        },
    )
    client.post(
        "/expenses",
        data={
            "title": "Transport",
            "amount": "10.00",
            "category": "Travel",
            "date": "2026-09-25",
        },
    )

    response = client.get("/summary")
    assert response.status_code == 200
    assert b"Food" in response.data
    assert b"25.00" in response.data
    assert b"Travel" in response.data
    assert b"10.00" in response.data


def test_dashboard_api_analytics(client):
    client.post(
        "/expenses",
        data={
            "title": "Lunch",
            "amount": "25.00",
            "category": "Food",
            "date": "2026-09-25",
        },
    )
    client.post(
        "/expenses",
        data={
            "title": "Transport",
            "amount": "10.00",
            "category": "Travel",
            "date": "2026-09-25",
        },
    )

    response = client.get("/api/dashboard-data")
    assert response.status_code == 200
    data = response.get_json()
    assert data["totalSpent"] == 35.0
    assert any(item["category"] == "Food" for item in data["categoryTotals"])
    assert any(item["month"] == "2026-09" for item in data["monthlyTotals"])


def test_inr_currency_display(client):
    client.post(
        "/expenses",
        data={
            "title": "Lunch",
            "amount": "25.00",
            "category": "Food",
            "date": "2026-09-25",
        },
    )

    response = client.get("/")
    assert response.status_code == 200
    assert b"\xe2\x82\xb9" in response.data
    assert b"$" not in response.data


def test_edit_and_delete_expense(client):
    client.post(
        "/expenses",
        data={
            "title": "Lunch",
            "amount": "25.00",
            "category": "Food",
            "date": "2026-09-25",
        },
    )

    response = client.post(
        "/expenses/1/edit",
        data={
            "title": "Team Lunch",
            "amount": "40.00",
            "category": "Meals",
            "date": "2026-09-26",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Team Lunch" in response.data
    assert b"Meals" in response.data

    response = client.post("/expenses/1/delete", follow_redirects=True)
    assert response.status_code == 200
    assert b"Team Lunch" not in response.data


def test_category_and_month_filters(client):
    client.post(
        "/expenses",
        data={
            "title": "Lunch",
            "amount": "25.00",
            "category": "Food",
            "date": "2026-09-25",
        },
    )
    client.post(
        "/expenses",
        data={
            "title": "Fuel",
            "amount": "80.00",
            "category": "Travel",
            "date": "2026-10-02",
        },
    )

    response = client.get("/?category=Food&month=2026-09")
    assert response.status_code == 200
    assert b"Lunch" in response.data
    assert b"Fuel" not in response.data


def test_can_export_csv_report(client):
    client.post(
        "/expenses",
        data={
            "title": "Lunch",
            "amount": "25.00",
            "category": "Food",
            "date": "2026-09-25",
        },
    )

    response = client.get("/export/csv")
    assert response.status_code == 200
    assert "text/csv" in response.content_type
    assert b"Lunch" in response.data
    assert b"25.00" in response.data


def test_budget_can_be_set(client):
    response = client.post(
        "/budget",
        data={"monthly_budget": "1500.00"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"1500.00" in response.data


def test_recurring_expenses_are_tracked(client):
    client.post(
        "/expenses",
        data={
            "title": "Internet",
            "amount": "699.00",
            "category": "Utilities",
            "date": "2026-09-25",
            "is_recurring": "on",
        },
    )

    response = client.get("/api/dashboard-data")
    assert response.status_code == 200
    data = response.get_json()
    assert data["recurringExpenseCount"] == 1
    assert any(item["title"] == "Internet" for item in data["recurringExpenses"])


def test_user_register_and_login(client):
    register_response = client.post(
        "/register",
        data={"username": "alice", "password": "secret123"},
        follow_redirects=True,
    )
    assert register_response.status_code == 200

    login_response = client.post(
        "/login",
        data={"username": "alice", "password": "secret123"},
        follow_redirects=True,
    )
    assert login_response.status_code == 200
    assert b"alice" in login_response.data.lower()


def test_can_edit_and_delete_expense(client):
    create_response = client.post(
        "/expenses",
        data={
            "title": "Lunch",
            "amount": "25.00",
            "category": "Food",
            "date": "2026-09-25",
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200

    response = client.post(
        "/expenses/1/edit",
        data={
            "title": "Business Lunch",
            "amount": "40.00",
            "category": "Meals",
            "date": "2026-09-26",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Business Lunch" in response.data
    assert b"Meals" in response.data

    delete_response = client.post("/expenses/1/delete", follow_redirects=True)
    assert delete_response.status_code == 200
    assert b"Business Lunch" not in delete_response.data


def test_can_filter_by_category(client):
    client.post(
        "/expenses",
        data={
            "title": "Lunch",
            "amount": "25.00",
            "category": "Food",
            "date": "2026-09-25",
        },
    )
    client.post(
        "/expenses",
        data={
            "title": "Train",
            "amount": "30.00",
            "category": "Travel",
            "date": "2026-09-26",
        },
    )

    response = client.get("/?category=Food")
    assert response.status_code == 200
    assert b"Lunch" in response.data
    assert b"Train" not in response.data


def test_index_handles_missing_guest_user(client):
    with app.app_context():
        db = client.application.config["DATABASE"]
        conn = __import__("sqlite3").connect(db)
        conn.execute("DELETE FROM users WHERE username = ?", ("guest",))
        conn.commit()
        conn.close()

    response = client.get("/")
    assert response.status_code == 200
    assert b"Expense Tracker" in response.data


def test_admin_panel_and_history_tracking(client):
    with app.app_context():
        db = client.application.config["DATABASE"]
        conn = __import__("sqlite3").connect(db)
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("adminuser", __import__("werkzeug.security").security.generate_password_hash("secret123"), "admin"),
        )
        conn.commit()
        conn.close()

    client.post("/login", data={"username": "adminuser", "password": "secret123"}, follow_redirects=True)
    response = client.get("/admin")
    assert response.status_code == 200
    assert b"Admin Panel" in response.data

    client.post(
        "/expenses",
        data={
            "title": "Monthly groceries",
            "amount": "55.00",
            "category": "Food",
            "date": "2026-09-17",
        },
    )

    with app.app_context():
        db = client.application.config["DATABASE"]
        conn = __import__("sqlite3").connect(db)
        history_count = conn.execute("SELECT COUNT(*) FROM expense_history").fetchone()[0]
        season_total = conn.execute(
            "SELECT SUM(amount) FROM expenses WHERE substr(date, 6, 2) IN ('09', '10', '11')"
        ).fetchone()[0]
        conn.close()

    assert history_count >= 1
    assert season_total is not None


def test_admin_can_view_and_manage_user_expense(client):
    with app.app_context():
        db = client.application.config["DATABASE"]
        conn = __import__("sqlite3").connect(db)
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("alice", __import__("werkzeug.security").security.generate_password_hash("secret123"), "user"),
        )
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("adminuser", __import__("werkzeug.security").security.generate_password_hash("secret123"), "admin"),
        )
        alice_id = conn.execute("SELECT id FROM users WHERE username = ?", ("alice",)).fetchone()[0]
        conn.execute(
            "INSERT INTO expenses (title, amount, category, date, is_recurring, user_id) VALUES (?, ?, ?, ?, ?, ?)",
            ("Alice groceries", "88.00", "Food", "2026-09-20", 0, alice_id),
        )
        conn.commit()
        conn.close()

    client.post("/login", data={"username": "adminuser", "password": "secret123"}, follow_redirects=True)
    with app.app_context():
        db = client.application.config["DATABASE"]
        conn = __import__("sqlite3").connect(db)
        alice_id = conn.execute("SELECT id FROM users WHERE username = ?", ("alice",)).fetchone()[0]
        expense_id = conn.execute("SELECT id FROM expenses WHERE title = ?", ("Alice groceries",)).fetchone()[0]
        conn.close()

    response = client.get(f"/?user_id={alice_id}")
    assert response.status_code == 200
    assert b"Alice groceries" in response.data

    response = client.post(f"/expenses/{expense_id}/delete", follow_redirects=True)
    assert response.status_code == 200
    assert b"Alice groceries" not in response.data
