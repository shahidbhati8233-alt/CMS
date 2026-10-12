import base64
import io
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import click
import qrcode
from dotenv import load_dotenv
from flask import Flask, current_app, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

csrf = CSRFProtect()
ORDER_STATUSES = ("New", "Accepted", "Preparing", "Ready", "Served", "Cancelled")


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        DATABASE=os.environ.get(
            "DATABASE_PATH", str(Path(app.instance_path) / "cafe.sqlite3")
        ),
        MAX_CONTENT_LENGTH=64 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower() == "true",
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", "").strip(),
        BASE_DOMAIN=os.environ.get("BASE_DOMAIN", "localhost").strip().lower(),
        TENANT_ROUTING_MODE=os.environ.get("TENANT_ROUTING_MODE", "auto").strip().lower(),
    )
    if test_config:
        app.config.update(test_config)
    elif not app.config["SECRET_KEY"]:
        raise RuntimeError(
            "Set SECRET_KEY in your environment or .env file before starting the app."
        )

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    csrf.init_app(app)
    database_init_lock = threading.Lock()
    limiter = Limiter(
        get_remote_address,
        app=app,
        storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
    )
    app.extensions["cafe_limiter"] = limiter

    @app.teardown_appcontext
    def close_db(_error=None):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.before_request
    def ensure_database_initialized():
        if not app.extensions.get("cafe_database_initialized"):
            with database_init_lock:
                if not app.extensions.get("cafe_database_initialized"):
                    initialize_database(get_db())
                    app.extensions["cafe_database_initialized"] = True
        host = request.host.split(":", 1)[0].rstrip(".").lower()
        base_domain = app.config["BASE_DOMAIN"]
        cafe_slug = request.environ.get("cms.tenant_slug")
        g.tenant_path = cafe_slug is not None
        g.tenant_subdomain = False
        if cafe_slug is None:
            if host == base_domain or (
                base_domain == "localhost" and host == "127.0.0.1"
            ):
                cafe_slug = "juniper"
            elif host.endswith("." + base_domain):
                cafe_slug = host[: -(len(base_domain) + 1)]
                if "." in cafe_slug:
                    cafe_slug = None
                else:
                    g.tenant_subdomain = True
        g.cafe = None
        if cafe_slug:
            g.cafe = get_db().execute(
                "SELECT id, name, slug FROM cafes WHERE slug = ?",
                (cafe_slug,),
            ).fetchone()

    @app.context_processor
    def inject_cafe():
        return {"cafe": getattr(g, "cafe", None)}

    @app.cli.command("init-db")
    def init_db_command():
        """Create the local database and add starter menu data."""
        initialize_database(get_db(), include_starter_data=True)
        print("Database initialized. Starter menu and tables 1-20 are ready.")

    @app.cli.command("setup-owner")
    def setup_owner_command():
        """Create or reset the owner account using environment variables."""
        username = os.environ.get("OWNER_USERNAME", "").strip()
        password = os.environ.get("OWNER_PASSWORD", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", username):
            raise click.ClickException(
                "OWNER_USERNAME must be 3-40 characters using letters, numbers, dots, underscores, or hyphens."
            )
        if len(password) < 12 or len(password) > 128:
            raise click.ClickException("OWNER_PASSWORD must be between 12 and 128 characters.")

        db = get_db()
        initialize_database(db)
        password_hash = generate_password_hash(password, method="scrypt")
        cafe_id = db.execute(
            "SELECT id FROM cafes WHERE slug = 'juniper'"
        ).fetchone()["id"]
        existing = db.execute(
            "SELECT id FROM users WHERE username = ? AND cafe_id = ?",
            (username, cafe_id),
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE users SET password_hash = ?, role = 'owner' WHERE id = ? AND cafe_id = ?",
                (password_hash, existing["id"], cafe_id),
            )
            action = "reset"
        else:
            db.execute(
                "INSERT INTO users (username, password_hash, role, cafe_id) VALUES (?, ?, 'owner', ?)",
                (username, password_hash, cafe_id),
            )
            action = "created"
        db.commit()
        click.echo(f"Owner account {action}: {username}")

    @app.errorhandler(CSRFError)
    def handle_csrf_error(_error):
        if request.path.startswith("/owner/"):
            return render_template(
                "error.html",
                message="Your form session expired. Please go back, refresh the page, and try again.",
            ), 400
        return jsonify(error="Your session token is invalid or expired. Refresh the page and try again."), 400

    @app.errorhandler(429)
    def handle_rate_limit(_error):
        if request.path.startswith("/owner/"):
            return render_template(
                "error.html",
                message="Too many requests were made. Please wait a moment and try again.",
            ), 429
        return jsonify(error="Too many orders were submitted from this connection. Please wait a moment and try again."), 429

    @app.get("/")
    def home():
        if (g.tenant_subdomain or g.tenant_path) and g.cafe is not None:
            return redirect(url_for("menu", table=1))
        return render_template("landing.html")

    @app.route("/signup", methods=["GET", "POST"])
    @limiter.limit("5 per hour", methods=["POST"])
    def signup():
        if request.method == "POST":
            cafe_name = request.form.get("cafe_name", "").strip()
            slug = request.form.get("slug", "").strip().lower()
            password = request.form.get("password", "")
            reserved_slugs = {
                "www", "app", "api", "admin", "owner", "login", "signup",
                "static", "assets", "mail", "support",
            }
            error = None
            if not 2 <= len(cafe_name) <= 100:
                error = "Café names must be between 2 and 100 characters."
            elif (
                not 2 <= len(slug) <= 40
                or
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?", slug)
                or slug in reserved_slugs
            ):
                error = "Choose a café URL name using 2–40 lowercase letters, numbers, or single hyphens."
            elif len(password) < 12 or len(password) > 128:
                error = "Passwords must be between 12 and 128 characters."

            if error:
                return render_template(
                    "signup.html",
                    error=error,
                    form_values=request.form,
                ), 400

            try:
                tenant_host = tenant_base_url(slug)
            except ValueError as error:
                return render_template(
                    "signup.html",
                    error=str(error),
                    form_values=request.form,
                ), 400

            db = get_db()
            try:
                cursor = db.execute(
                    "INSERT INTO cafes (name, slug) VALUES (?, ?)",
                    (cafe_name, slug),
                )
                cafe_id = cursor.lastrowid
                db.execute(
                    """
                    INSERT INTO users (username, password_hash, role, cafe_id)
                    VALUES (?, ?, 'owner', ?)
                    """,
                    (
                        f"cafe-{cafe_id}",
                        generate_password_hash(password, method="scrypt"),
                        cafe_id,
                    ),
                )
                db.execute(
                    """
                    INSERT INTO cafe_tables (cafe_id, display_number, active)
                    VALUES (?, 1, 1)
                    """,
                    (cafe_id,),
                )
                db.executemany(
                    "INSERT INTO menu_categories (cafe_id, name, sort_order) VALUES (?, ?, ?)",
                    [
                        (cafe_id, "Coffee", 1),
                        (cafe_id, "Tea", 2),
                        (cafe_id, "Snacks", 3),
                        (cafe_id, "Meals", 4),
                    ],
                )
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
                return render_template(
                    "signup.html",
                    error="That café URL or username is already in use. Choose another.",
                    form_values=request.form,
                ), 400

            return redirect(f"{tenant_host}/owner/login?welcome=1")
        return render_template("signup.html", error=None, form_values={})

    @app.get("/login")
    def tenant_login_redirect():
        slug = request.args.get("cafe", "").strip().lower()
        if not re.fullmatch(r"[a-z0-9-]{2,40}", slug):
            return render_template("login_select_cafe.html", error=None), 200
        tenant = get_db().execute(
            "SELECT slug FROM cafes WHERE slug = ?", (slug,)
        ).fetchone()
        if tenant is None:
            return render_template(
                "login_select_cafe.html",
                error="We couldn't find a café with that URL name.",
            ), 404
        return redirect(f"{tenant_base_url(slug)}/owner/login")

    @app.get("/menu")
    def menu():
        if g.cafe is None:
            return render_template("error.html", message="This café menu could not be found."), 404
        table_number = parse_table_number(request.args.get("table"))
        if table_number is None:
            return render_template("error.html", message="Please scan a valid table QR code."), 400
        table = get_db().execute(
            """
            SELECT table_number FROM cafe_tables
            WHERE cafe_id = ? AND display_number = ? AND active = 1
            """,
            (g.cafe["id"], table_number),
        ).fetchone()
        if table is None:
            return render_template("error.html", message="This table is not currently available."), 404

        categories = get_db().execute(
            """
            SELECT DISTINCT c.id, c.name
            FROM menu_categories c
            JOIN menu_items i ON i.category_id = c.id
            WHERE i.visible = 1 AND i.cafe_id = c.cafe_id AND c.cafe_id = ?
            ORDER BY c.sort_order, c.name
            """,
            (g.cafe["id"],),
        ).fetchall()
        items = get_db().execute(
            """
            SELECT id, category_id, name, description, price_paise, available, image_path
            FROM menu_items
            WHERE cafe_id = ? AND visible = 1
            ORDER BY category_id, name
            """,
            (g.cafe["id"],),
        ).fetchall()
        return render_template(
            "menu.html",
            table_number=table_number,
            categories=categories,
            items=items,
        )

    def owner_required(view):
        @wraps(view)
        def wrapped_view(**kwargs):
            if g.cafe is None:
                return render_template(
                    "error.html",
                    message="This café owner page could not be found.",
                ), 404
            owner_id = session.get("owner_id")
            owner = None
            if type(owner_id) is int:
                owner = get_db().execute(
                    "SELECT id, username, role FROM users WHERE id = ? AND cafe_id = ?",
                    (owner_id, g.cafe["id"]),
                ).fetchone()
            if owner is None or owner["role"] != "owner":
                session.clear()
                return redirect(url_for("owner_login"))
            g.owner = owner
            return view(**kwargs)

        return wrapped_view

    @app.route("/owner/login", methods=["GET", "POST"])
    @limiter.limit("10 per minute", methods=["POST"])
    def owner_login():
        if g.cafe is None:
            return render_template("error.html", message="This café sign-in could not be found."), 404
        if request.method == "POST":
            password = request.form.get("password", "")
            owners = get_db().execute(
                """
                SELECT id, password_hash FROM users
                WHERE cafe_id = ? AND role = 'owner'
                ORDER BY id
                """,
                (g.cafe["id"],),
            ).fetchall()
            user = None
            if password and len(password) <= 128:
                for owner in owners:
                    if check_password_hash(owner["password_hash"], password):
                        user = owner
                        break
            if user is not None:
                session.clear()
                session["owner_id"] = user["id"]
                return redirect(url_for("owner_dashboard"))
            return render_template(
                "owner_login.html",
                error="That café address and password were not recognized.",
            ), 401
        return render_template(
            "owner_login.html",
            error=None,
            welcome=request.args.get("welcome") == "1",
        )

    @app.post("/owner/logout")
    @owner_required
    def owner_logout():
        session.clear()
        return redirect(url_for("owner_login"))

    @app.get("/owner")
    @owner_required
    def owner_dashboard():
        db = get_db()
        menu_count = db.execute(
            "SELECT COUNT(*) FROM menu_items WHERE cafe_id = ? AND visible = 1",
            (g.cafe["id"],),
        ).fetchone()[0]
        category_count = db.execute(
            "SELECT COUNT(*) FROM menu_categories WHERE cafe_id = ?",
            (g.cafe["id"],),
        ).fetchone()[0]
        new_order_count = db.execute(
            "SELECT COUNT(*) FROM orders WHERE cafe_id = ? AND status = 'New'",
            (g.cafe["id"],),
        ).fetchone()[0]
        return render_template(
            "owner_dashboard.html",
            owner=g.owner,
            menu_count=menu_count,
            category_count=category_count,
            new_order_count=new_order_count,
        )

    @app.get("/owner/orders")
    @owner_required
    def owner_orders():
        db = get_db()
        latest_order_id = db.execute(
            "SELECT COALESCE(MAX(id), 0) FROM orders WHERE cafe_id = ?",
            (g.cafe["id"],),
        ).fetchone()[0]
        order_rows = db.execute(
            """
            SELECT id, order_number, table_number, status, instructions,
                   total_paise, created_at
            FROM orders
            WHERE cafe_id = ?
            ORDER BY CASE WHEN status = 'New' THEN 0 ELSE 1 END,
                     created_at DESC, id DESC
            LIMIT 100
            """,
            (g.cafe["id"],),
        ).fetchall()
        order_ids = [order["id"] for order in order_rows]
        items_by_order = {order_id: [] for order_id in order_ids}
        if order_ids:
            placeholders = ",".join("?" for _ in order_ids)
            item_rows = db.execute(
                f"""
                SELECT order_id, item_name, unit_price_paise, quantity
                FROM order_items
                WHERE order_id IN ({placeholders})
                ORDER BY id
                """,
                tuple(order_ids),
            ).fetchall()
            for item in item_rows:
                items_by_order[item["order_id"]].append(
                    {
                        "item_name": item["item_name"],
                        "unit_price_paise": item["unit_price_paise"],
                        "quantity": item["quantity"],
                        "line_total_paise": item["unit_price_paise"] * item["quantity"],
                    }
                )

        orders = []
        for order in order_rows:
            order_data = dict(order)
            table = db.execute(
                """
                SELECT display_number FROM cafe_tables
                WHERE table_number = ? AND cafe_id = ?
                """,
                (order["table_number"], g.cafe["id"]),
            ).fetchone()
            order_data["table_number"] = table["display_number"] if table else "?"
            order_data["items"] = items_by_order[order["id"]]
            created_at = datetime.strptime(
                order["created_at"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
            order_data["created_at_iso"] = created_at.isoformat().replace(
                "+00:00", "Z"
            )
            order_data["created_at_label"] = created_at.strftime(
                "%b %d, %Y · %I:%M %p"
            )
            orders.append(order_data)
        return render_template(
            "owner_orders.html",
            owner=g.owner,
            orders=orders,
            statuses=ORDER_STATUSES,
            latest_order_id=latest_order_id,
            updates_url=url_for("owner_order_updates"),
            refreshed_at=datetime.now(timezone.utc),
        )

    @app.get("/owner/orders/updates")
    @owner_required
    def owner_order_updates():
        try:
            after_id = max(0, int(request.args.get("after_id", "0")))
        except (TypeError, ValueError):
            return jsonify(error="Invalid order cursor."), 400
        rows = get_db().execute(
            """
            SELECT o.id, o.order_number, o.total_paise, t.display_number AS table_number
            FROM orders o
            LEFT JOIN cafe_tables t
              ON t.table_number = o.table_number AND t.cafe_id = o.cafe_id
            WHERE o.cafe_id = ? AND o.id > ?
            ORDER BY o.id ASC LIMIT 20
            """,
            (g.cafe["id"], after_id),
        ).fetchall()
        return jsonify(orders=[dict(row) for row in rows])

    @app.post("/owner/orders/<int:order_id>/status")
    @owner_required
    def update_order_status(order_id):
        status = request.form.get("status", "")
        if status not in ORDER_STATUSES:
            flash("Choose a valid order status.", "error")
            return redirect(url_for("owner_orders"))
        cursor = get_db().execute(
            "UPDATE orders SET status = ? WHERE id = ? AND cafe_id = ?",
            (status, order_id, g.cafe["id"]),
        )
        if cursor.rowcount:
            get_db().commit()
            flash(f"Order status updated to {status}.", "success")
        else:
            flash("That order could not be found.", "error")
        return redirect(url_for("owner_orders"))

    @app.route("/owner/qr", methods=["GET", "POST"])
    @owner_required
    def qr_manager():
        db = get_db()
        if request.method == "POST":
            custom_number = request.form.get("custom_table_number", "").strip()
            selected_number = request.form.get("table_number", "").strip()
            table_text = custom_number or selected_number
            table_number = parse_table_number(table_text)
            if table_number is None:
                flash("Enter a table number from 1 to 999.", "error")
                return redirect(url_for("qr_manager"))
            try:
                build_table_menu_url(table_number)
            except ValueError as error:
                flash(str(error), "error")
                return redirect(url_for("qr_manager"))

            if custom_number:
                db.execute(
                    """
                    INSERT OR IGNORE INTO cafe_tables
                        (cafe_id, display_number, active)
                    VALUES (?, ?, 1)
                    """,
                    (g.cafe["id"], table_number),
                )
                db.commit()
            table = db.execute(
                """
                SELECT table_number FROM cafe_tables
                WHERE cafe_id = ? AND display_number = ? AND active = 1
                """,
                (g.cafe["id"], table_number),
            ).fetchone()
            if table is None:
                flash("That table is inactive. Choose an active table number.", "error")
                return redirect(url_for("qr_manager"))
            return redirect(url_for("qr_manager", table=table_number))

        tables = db.execute(
            """
            SELECT display_number AS table_number FROM cafe_tables
            WHERE cafe_id = ? AND active = 1 ORDER BY display_number
            """,
            (g.cafe["id"],),
        ).fetchall()
        table_number = parse_table_number(request.args.get("table"))
        menu_url = None
        qr_preview = None
        if request.args.get("table") is not None:
            table = db.execute(
                """
                SELECT table_number FROM cafe_tables
                WHERE cafe_id = ? AND display_number = ? AND active = 1
                """,
                (g.cafe["id"], table_number),
            ).fetchone() if table_number is not None else None
            if table is None:
                flash("That table is not active. Generate a QR code for an active table.", "error")
                table_number = None
            else:
                try:
                    menu_url = build_table_menu_url(table_number)
                    image_buffer = make_qr_png(menu_url)
                    qr_preview = base64.b64encode(image_buffer.getvalue()).decode("ascii")
                except ValueError as error:
                    flash(str(error), "error")
                    table_number = None

        return render_template(
            "owner_qr.html",
            owner=g.owner,
            tables=tables,
            table_number=table_number,
            menu_url=menu_url,
            qr_preview=qr_preview,
        )

    @app.get("/owner/qr/<int:table_number>.png")
    @owner_required
    def download_table_qr(table_number):
        table = get_db().execute(
            """
            SELECT table_number FROM cafe_tables
            WHERE cafe_id = ? AND display_number = ? AND active = 1
            """,
            (g.cafe["id"], table_number),
        ).fetchone()
        if table is None:
            return render_template("error.html", message="That table QR code is unavailable."), 404
        try:
            menu_url = build_table_menu_url(table_number)
            image_buffer = make_qr_png(menu_url)
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("qr_manager"))
        return send_file(
            image_buffer,
            mimetype="image/png",
            as_attachment=True,
            download_name=f"{g.cafe['slug']}-table-{table_number}.png",
            max_age=0,
        )

    @app.get("/owner/menu")
    @owner_required
    def owner_menu_manager():
        db = get_db()
        categories = db.execute(
            """
            SELECT c.id, c.name, COUNT(i.id) AS item_count
            FROM menu_categories c
            LEFT JOIN menu_items i ON i.category_id = c.id AND i.cafe_id = c.cafe_id
            WHERE c.cafe_id = ?
            GROUP BY c.id
            ORDER BY c.sort_order, c.name
            """,
            (g.cafe["id"],),
        ).fetchall()
        items = db.execute(
            """
            SELECT i.id, i.name, i.description, i.price_paise, i.available,
                   i.visible, i.image_path, c.name AS category_name
            FROM menu_items i
            JOIN menu_categories c ON c.id = i.category_id
            WHERE i.cafe_id = ? AND c.cafe_id = ?
            ORDER BY c.sort_order, c.name, i.name
            """,
            (g.cafe["id"], g.cafe["id"]),
        ).fetchall()
        return render_template(
            "menu_manager.html",
            owner=g.owner,
            categories=categories,
            items=items,
        )

    @app.post("/owner/categories")
    @owner_required
    def add_category():
        name = request.form.get("name", "").strip()
        if not 1 <= len(name) <= 60:
            flash("Category names must be between 1 and 60 characters.", "error")
            return redirect(url_for("owner_menu_manager"))
        db = get_db()
        try:
            db.execute(
                "INSERT INTO menu_categories (cafe_id, name, sort_order) VALUES (?, ?, ?)",
                (
                    g.cafe["id"],
                    name,
                    db.execute(
                    """
                    SELECT COALESCE(MAX(sort_order), 0) + 1
                    FROM menu_categories WHERE cafe_id = ?
                    """,
                    (g.cafe["id"],),
                    ).fetchone()[0],
                ),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            flash("A category with that name already exists.", "error")
            return redirect(url_for("owner_menu_manager"))
        flash("Category added.", "success")
        return redirect(url_for("owner_menu_manager"))

    @app.post("/owner/categories/<int:category_id>")
    @owner_required
    def update_category(category_id):
        name = request.form.get("name", "").strip()
        if not 1 <= len(name) <= 60:
            flash("Category names must be between 1 and 60 characters.", "error")
            return redirect(url_for("owner_menu_manager"))
        try:
            cursor = get_db().execute(
                "UPDATE menu_categories SET name = ? WHERE id = ? AND cafe_id = ?",
                (name, category_id, g.cafe["id"]),
            )
            if cursor.rowcount == 0:
                flash("That category could not be found.", "error")
            else:
                get_db().commit()
                flash("Category updated.", "success")
        except sqlite3.IntegrityError:
            get_db().rollback()
            flash("A category with that name already exists.", "error")
        return redirect(url_for("owner_menu_manager"))

    @app.route("/owner/items/new", methods=["GET", "POST"])
    @owner_required
    def new_menu_item():
        categories = get_db().execute(
            "SELECT id, name FROM menu_categories WHERE cafe_id = ? ORDER BY sort_order, name",
            (g.cafe["id"],),
        ).fetchall()
        if not categories:
            flash("Create a category before adding a menu item.", "error")
            return redirect(url_for("owner_menu_manager"))
        if request.method == "POST":
            item_data, error = validate_item_form(
                request.form, get_db(), g.cafe["id"]
            )
            if error:
                return render_template(
                    "item_form.html",
                    owner=g.owner,
                    categories=categories,
                    item=None,
                    error=error,
                    form_values=request.form,
                ), 400
            get_db().execute(
                """
                INSERT INTO menu_items
                    (cafe_id, category_id, name, description, price_paise, available, image_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    g.cafe["id"],
                    item_data["category_id"],
                    item_data["name"],
                    item_data["description"],
                    item_data["price_paise"],
                    item_data["available"],
                    item_data["image_path"],
                ),
            )
            get_db().commit()
            flash("Menu item added.", "success")
            return redirect(url_for("owner_menu_manager"))
        return render_template(
            "item_form.html",
            owner=g.owner,
            categories=categories,
            item=None,
            error=None,
        )

    @app.route("/owner/items/<int:item_id>/edit", methods=["GET", "POST"])
    @owner_required
    def edit_menu_item(item_id):
        item = get_db().execute(
            "SELECT * FROM menu_items WHERE id = ? AND cafe_id = ?",
            (item_id, g.cafe["id"]),
        ).fetchone()
        if item is None:
            return render_template("error.html", message="That menu item could not be found."), 404
        categories = get_db().execute(
            "SELECT id, name FROM menu_categories WHERE cafe_id = ? ORDER BY sort_order, name",
            (g.cafe["id"],),
        ).fetchall()
        if request.method == "POST":
            item_data, error = validate_item_form(
                request.form, get_db(), g.cafe["id"]
            )
            if error:
                return render_template(
                    "item_form.html",
                    owner=g.owner,
                    categories=categories,
                    item=item,
                    error=error,
                    form_values=request.form,
                ), 400
            get_db().execute(
                """
                UPDATE menu_items
                SET category_id = ?, name = ?, description = ?, price_paise = ?,
                    available = ?, image_path = ?
                WHERE id = ? AND cafe_id = ?
                """,
                (
                    item_data["category_id"],
                    item_data["name"],
                    item_data["description"],
                    item_data["price_paise"],
                    item_data["available"],
                    item_data["image_path"],
                    item_id,
                    g.cafe["id"],
                ),
            )
            get_db().commit()
            flash("Menu item updated.", "success")
            return redirect(url_for("owner_menu_manager"))
        return render_template(
            "item_form.html",
            owner=g.owner,
            categories=categories,
            item=item,
            error=None,
        )

    @app.post("/owner/items/<int:item_id>/visibility")
    @owner_required
    def set_menu_item_visibility(item_id):
        visible = request.form.get("visible")
        if visible not in ("0", "1"):
            flash("Choose a valid visibility setting.", "error")
        else:
            cursor = get_db().execute(
                "UPDATE menu_items SET visible = ? WHERE id = ? AND cafe_id = ?",
                (int(visible), item_id, g.cafe["id"]),
            )
            if cursor.rowcount:
                get_db().commit()
                flash("Menu visibility updated.", "success")
            else:
                flash("That menu item could not be found.", "error")
        return redirect(url_for("owner_menu_manager"))

    @app.post("/owner/items/<int:item_id>/delete")
    @owner_required
    def delete_menu_item(item_id):
        cursor = get_db().execute(
            "DELETE FROM menu_items WHERE id = ? AND cafe_id = ?",
            (item_id, g.cafe["id"]),
        )
        if cursor.rowcount:
            get_db().commit()
            flash("Menu item removed. Existing order history is preserved.", "success")
        else:
            flash("That menu item could not be found.", "error")
        return redirect(url_for("owner_menu_manager"))

    @app.post("/orders")
    @limiter.limit("30 per minute")
    def create_order():
        if g.cafe is None:
            return jsonify(error="This café could not be found."), 404
        table_number = parse_table_number(request.args.get("table"))
        if table_number is None:
            return jsonify(error="A valid table number is required."), 400
        db = get_db()
        table = db.execute(
            """
            SELECT table_number FROM cafe_tables
            WHERE cafe_id = ? AND display_number = ? AND active = 1
            """,
            (g.cafe["id"], table_number),
        ).fetchone()
        if table is None:
            return jsonify(error="This table is not currently available."), 400

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="The order details could not be read."), 400
        instructions = payload.get("instructions", "")
        items = payload.get("items")
        if not isinstance(instructions, str) or len(instructions.strip()) > 500:
            return jsonify(error="Instructions must be 500 characters or fewer."), 400
        if not isinstance(items, list) or not 1 <= len(items) <= 30:
            return jsonify(error="Add between 1 and 30 menu items to your order."), 400

        quantities = {}
        for entry in items:
            if not isinstance(entry, dict):
                return jsonify(error="One or more order items are invalid."), 400
            item_id = entry.get("id")
            quantity = entry.get("quantity")
            if (
                type(item_id) is not int
                or type(quantity) is not int
                or item_id < 1
                or not 1 <= quantity <= 20
                or item_id in quantities
            ):
                return jsonify(error="One or more item quantities are invalid."), 400
            quantities[item_id] = quantity

        placeholders = ",".join("?" for _ in quantities)
        menu_items = db.execute(
            f"""
            SELECT id, name, price_paise
            FROM menu_items
            WHERE cafe_id = ? AND available = 1 AND visible = 1
              AND id IN ({placeholders})
            """,
            (g.cafe["id"], *quantities),
        ).fetchall()
        if len(menu_items) != len(quantities):
            return jsonify(error="A selected item is no longer available. Refresh the menu and try again."), 400

        total_paise = sum(
            item["price_paise"] * quantities[item["id"]] for item in menu_items
        )
        order_number = f"CF-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(6).upper()}"
        try:
            cursor = db.execute(
                """
                INSERT INTO orders
                    (cafe_id, order_number, table_number, instructions, total_paise)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    g.cafe["id"],
                    order_number,
                    table["table_number"],
                    instructions.strip(),
                    total_paise,
                ),
            )
            order_id = cursor.lastrowid
            db.executemany(
                """
                INSERT INTO order_items
                    (order_id, menu_item_id, item_name, unit_price_paise, quantity)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        order_id,
                        item["id"],
                        item["name"],
                        item["price_paise"],
                        quantities[item["id"]],
                    )
                    for item in menu_items
                ],
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            current_app.logger.exception("Unable to save customer order")
            return jsonify(error="We couldn't save your order. Please try again."), 500

        return jsonify(
            order_number=order_number,
            message="Your order has been sent to the café. Please wait for staff confirmation.",
        ), 201

    app.wsgi_app = TenantPathMiddleware(app.wsgi_app)
    return app


class TenantPathMiddleware:
    """Route /c/<slug>/... requests through the existing tenant-aware Flask routes."""

    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        match = re.match(
            r"^/c/([a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?)(?=/|$)",
            path,
        )
        if match:
            slug = match.group(1)
            prefix = match.group(0)
            environ["cms.tenant_slug"] = slug
            existing_script_name = environ.get("SCRIPT_NAME", "").rstrip("/")
            environ["SCRIPT_NAME"] = f"{existing_script_name}{prefix}"
            environ["PATH_INFO"] = path[len(prefix):] or "/"
        return self.application(environ, start_response)


def initialize_database(db, include_starter_data=False):
    """Create the schema and migrate existing single-café data without discarding records."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS cafes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 2 AND 100),
            slug TEXT NOT NULL UNIQUE CHECK (length(slug) BETWEEN 2 AND 40),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS cafe_tables (
            table_number INTEGER PRIMARY KEY,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            cafe_id INTEGER REFERENCES cafes(id),
            display_number INTEGER CHECK (display_number BETWEEN 1 AND 999)
        );

        CREATE TABLE IF NOT EXISTS menu_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 60),
            sort_order INTEGER NOT NULL DEFAULT 0,
            cafe_id INTEGER REFERENCES cafes(id)
        );

        CREATE TABLE IF NOT EXISTS menu_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL REFERENCES menu_categories(id),
            name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 100),
            description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 300),
            price_paise INTEGER NOT NULL CHECK (price_paise >= 0),
            available INTEGER NOT NULL DEFAULT 1 CHECK (available IN (0, 1)),
            visible INTEGER NOT NULL DEFAULT 1 CHECK (visible IN (0, 1)),
            image_path TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            cafe_id INTEGER REFERENCES cafes(id)
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE CHECK (length(username) BETWEEN 3 AND 40),
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'owner' CHECK (role IN ('owner', 'staff')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            cafe_id INTEGER REFERENCES cafes(id)
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT NOT NULL UNIQUE,
            table_number INTEGER NOT NULL REFERENCES cafe_tables(table_number),
            status TEXT NOT NULL DEFAULT 'New'
                CHECK (status IN ('New', 'Accepted', 'Preparing', 'Ready', 'Served', 'Cancelled')),
            instructions TEXT NOT NULL DEFAULT '' CHECK (length(instructions) <= 500),
            total_paise INTEGER NOT NULL CHECK (total_paise >= 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            cafe_id INTEGER REFERENCES cafes(id)
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            menu_item_id INTEGER REFERENCES menu_items(id) ON DELETE SET NULL,
            item_name TEXT NOT NULL,
            unit_price_paise INTEGER NOT NULL CHECK (unit_price_paise >= 0),
            quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 20)
        );
        """
    )

    legacy_cafe = db.execute(
        "SELECT id FROM cafes WHERE slug = 'juniper'"
    ).fetchone()
    if legacy_cafe is None:
        cursor = db.execute(
            "INSERT INTO cafes (name, slug) VALUES ('Juniper Café', 'juniper')"
        )
        legacy_cafe_id = cursor.lastrowid
    else:
        legacy_cafe_id = legacy_cafe["id"]

    expected_columns = {
        "cafe_tables": {"cafe_id": "INTEGER REFERENCES cafes(id)", "display_number": "INTEGER"},
        "menu_categories": {"cafe_id": "INTEGER REFERENCES cafes(id)"},
        "menu_items": {
            "visible": "INTEGER NOT NULL DEFAULT 1 CHECK (visible IN (0, 1))",
            "cafe_id": "INTEGER REFERENCES cafes(id)",
        },
        "users": {"cafe_id": "INTEGER REFERENCES cafes(id)"},
        "orders": {"cafe_id": "INTEGER REFERENCES cafes(id)"},
    }
    for table_name, columns in expected_columns.items():
        existing_columns = {
            row["name"] for row in db.execute(f"PRAGMA table_info({table_name})")
        }
        for column_name, column_definition in columns.items():
            if column_name not in existing_columns:
                db.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
                )

    for table_name in ("menu_categories", "menu_items", "users", "orders", "cafe_tables"):
        db.execute(
            f"UPDATE {table_name} SET cafe_id = ? WHERE cafe_id IS NULL",
            (legacy_cafe_id,),
        )
    db.execute(
        "UPDATE cafe_tables SET display_number = table_number WHERE display_number IS NULL"
    )
    db.execute(
        """
        INSERT OR IGNORE INTO cafe_tables
            (table_number, active, cafe_id, display_number)
        SELECT DISTINCT orders.table_number, 1, ?, orders.table_number
        FROM orders
        WHERE NOT EXISTS (
            SELECT 1 FROM cafe_tables
            WHERE cafe_tables.table_number = orders.table_number
        )
        """,
        (legacy_cafe_id,),
    )

    if db.execute("PRAGMA user_version").fetchone()[0] < 2:
        db.commit()
        db.execute("PRAGMA foreign_keys = OFF")
        db.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE menu_categories_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cafe_id INTEGER NOT NULL REFERENCES cafes(id),
                name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 60),
                sort_order INTEGER NOT NULL DEFAULT 0,
                UNIQUE (cafe_id, name)
            );
            CREATE TABLE menu_items_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cafe_id INTEGER NOT NULL REFERENCES cafes(id),
                category_id INTEGER NOT NULL REFERENCES menu_categories_new(id),
                name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 100),
                description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 300),
                price_paise INTEGER NOT NULL CHECK (price_paise >= 0),
                available INTEGER NOT NULL DEFAULT 1 CHECK (available IN (0, 1)),
                visible INTEGER NOT NULL DEFAULT 1 CHECK (visible IN (0, 1)),
                image_path TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE users_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL CHECK (length(username) BETWEEN 3 AND 40),
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'owner' CHECK (role IN ('owner', 'staff')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                cafe_id INTEGER NOT NULL REFERENCES cafes(id),
                UNIQUE (cafe_id, username)
            );
            CREATE TABLE orders_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cafe_id INTEGER NOT NULL REFERENCES cafes(id),
                order_number TEXT NOT NULL UNIQUE,
                table_number INTEGER NOT NULL REFERENCES cafe_tables(table_number),
                status TEXT NOT NULL DEFAULT 'New'
                    CHECK (status IN ('New', 'Accepted', 'Preparing', 'Ready', 'Served', 'Cancelled')),
                instructions TEXT NOT NULL DEFAULT '' CHECK (length(instructions) <= 500),
                total_paise INTEGER NOT NULL CHECK (total_paise >= 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE order_items_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL REFERENCES orders_new(id) ON DELETE CASCADE,
                menu_item_id INTEGER REFERENCES menu_items_new(id) ON DELETE SET NULL,
                item_name TEXT NOT NULL,
                unit_price_paise INTEGER NOT NULL CHECK (unit_price_paise >= 0),
                quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 20)
            );

            INSERT INTO menu_categories_new (id, cafe_id, name, sort_order)
                SELECT id, cafe_id, name, sort_order FROM menu_categories;
            INSERT INTO menu_items_new
                (id, cafe_id, category_id, name, description, price_paise,
                 available, visible, image_path, created_at)
                SELECT id, cafe_id, category_id, name, description, price_paise,
                       available, visible, image_path, created_at FROM menu_items;
            INSERT INTO users_new (id, username, password_hash, role, created_at, cafe_id)
                SELECT id, username, password_hash, role, created_at, cafe_id FROM users;
            INSERT INTO orders_new
                (id, cafe_id, order_number, table_number, status, instructions,
                 total_paise, created_at)
                SELECT id, cafe_id, order_number, table_number, status, instructions,
                       total_paise, created_at FROM orders;
            INSERT INTO order_items_new
                (id, order_id, menu_item_id, item_name, unit_price_paise, quantity)
                SELECT id, order_id, menu_item_id, item_name, unit_price_paise, quantity
                FROM order_items;

            DROP TABLE order_items;
            DROP TABLE orders;
            DROP TABLE menu_items;
            DROP TABLE menu_categories;
            DROP TABLE users;
            ALTER TABLE menu_categories_new RENAME TO menu_categories;
            ALTER TABLE menu_items_new RENAME TO menu_items;
            ALTER TABLE users_new RENAME TO users;
            ALTER TABLE orders_new RENAME TO orders;
            ALTER TABLE order_items_new RENAME TO order_items;
            PRAGMA user_version = 2;
            COMMIT;
            """
        )
        db.execute("PRAGMA foreign_keys = ON")

    db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cafe_tables_display
        ON cafe_tables (cafe_id, display_number)
        """
    )
    db.execute(
        "INSERT OR IGNORE INTO cafe_tables (cafe_id, display_number, active) VALUES (?, 1, 1)",
        (legacy_cafe_id,),
    )

    if include_starter_data:
        for display_number in range(2, 21):
            db.execute(
                """
                INSERT OR IGNORE INTO cafe_tables (cafe_id, display_number, active)
                VALUES (?, ?, 1)
                """,
                (legacy_cafe_id, display_number),
            )
        if db.execute(
            "SELECT COUNT(*) FROM menu_categories WHERE cafe_id = ?",
            (legacy_cafe_id,),
        ).fetchone()[0] == 0:
            categories = [
                ("Coffee", 1),
                ("Tea", 2),
                ("Snacks", 3),
                ("Meals", 4),
            ]
            db.executemany(
                "INSERT INTO menu_categories (cafe_id, name, sort_order) VALUES (?, ?, ?)",
                [
                    (legacy_cafe_id, name, sort_order)
                    for name, sort_order in categories
                ],
            )
            category_ids = {
                row["name"]: row["id"]
                for row in db.execute(
                    "SELECT id, name FROM menu_categories WHERE cafe_id = ?",
                    (legacy_cafe_id,),
                )
            }
            starter_items = [
                (category_ids["Coffee"], "Cappuccino", "Espresso with steamed milk and a soft layer of foam.", 16000),
                (category_ids["Coffee"], "Cold Brew", "Slow-steeped coffee served chilled.", 14500),
                (category_ids["Tea"], "Masala Chai", "Black tea simmered with warming spices.", 9000),
                (category_ids["Snacks"], "Butter Croissant", "Flaky, golden pastry baked fresh.", 12000),
                (category_ids["Meals"], "Avocado Toast", "Sourdough toast with avocado and garden herbs.", 24000),
            ]
            db.executemany(
                """
                INSERT INTO menu_items
                    (cafe_id, category_id, name, description, price_paise)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (legacy_cafe_id, category_id, name, description, price)
                    for category_id, name, description, price in starter_items
                ],
            )
    db.commit()
    violations = db.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError(
            f"Database migration produced {len(violations)} foreign-key violations."
        )


def parse_table_number(value):
    if (
        not isinstance(value, str)
        or len(value) > 3
        or not value.isascii()
        or not value.isdecimal()
    ):
        return None
    table_number = int(value)
    if not 1 <= table_number <= 999:
        return None
    return table_number


def get_db():
    if "db" not in g:
        database_path = current_app.config["DATABASE"]
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(database_path)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def validate_item_form(form, db, cafe_id):
    name = form.get("name", "").strip()
    description = form.get("description", "").strip()
    if not 1 <= len(name) <= 100:
        return None, "Item names must be between 1 and 100 characters."
    if len(description) > 300:
        return None, "Descriptions must be 300 characters or fewer."

    category_text = form.get("category_id", "")
    if not category_text.isascii() or not category_text.isdecimal():
        return None, "Choose a valid category."
    category_id = int(category_text)
    category = db.execute(
        "SELECT id FROM menu_categories WHERE id = ? AND cafe_id = ?",
        (category_id, cafe_id),
    ).fetchone()
    if category is None:
        return None, "Choose a valid category."

    price_text = form.get("price", "").strip()
    if not re.fullmatch(r"(?:0|[1-9]\d{0,5})(?:\.\d{1,2})?", price_text):
        return None, "Enter a price in rupees with no more than two decimal places."
    whole, _, fraction = price_text.partition(".")
    price_paise = int(whole) * 100 + int((fraction + "00")[:2])
    if price_paise > 10_000_000:
        return None, "The maximum item price is ₹100,000.00."

    image_url = form.get("image_url", "").strip()
    if len(image_url) > 500:
        return None, "Image URLs must be 500 characters or fewer."
    if image_url:
        try:
            parsed_url = urlsplit(image_url)
        except ValueError:
            return None, "Enter a valid HTTPS image URL."
        if (
            parsed_url.scheme != "https"
            or not parsed_url.hostname
            or parsed_url.username
            or parsed_url.password
            or any(character.isspace() for character in image_url)
        ):
            return None, "Enter a valid HTTPS image URL."

    available_text = form.get("available", "1")
    if available_text not in ("0", "1"):
        return None, "Choose whether this item is available."

    return {
        "category_id": category_id,
        "name": name,
        "description": description,
        "price_paise": price_paise,
        "available": int(available_text),
        "image_path": image_url or None,
    }, None


def build_table_menu_url(table_number):
    if not getattr(g, "cafe", None):
        raise ValueError("A café must be selected to generate a table QR code.")
    base_url = tenant_base_url(g.cafe["slug"])
    is_development = current_app.debug or current_app.testing
    try:
        parsed_url = urlsplit(base_url)
        hostname = parsed_url.hostname
        parsed_url.port
    except ValueError:
        raise ValueError("Set a valid PUBLIC_BASE_URL before generating table QR codes.")
    configured_base = current_app.config["PUBLIC_BASE_URL"].rstrip("/")
    if configured_base:
        root_domain = urlsplit(configured_base).hostname
    else:
        root_domain = current_app.config["BASE_DOMAIN"]
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    path_tenant_url = use_path_tenants() and parsed_url.path == f"/c/{g.cafe['slug']}"
    valid_hostname = (
        hostname == root_domain if path_tenant_url
        else bool(hostname and hostname.endswith("." + root_domain))
    )
    valid_path = (
        parsed_url.path == f"/c/{g.cafe['slug']}"
        if path_tenant_url
        else parsed_url.path in ("", "/")
    )
    if (
        parsed_url.scheme not in ("http", "https")
        or not hostname
        or parsed_url.username
        or parsed_url.password
        or not valid_path
        or parsed_url.query
        or parsed_url.fragment
        or (root_domain in local_hosts and not is_development)
        or (parsed_url.scheme != "https" and not (is_development and hostname.endswith(".localhost")))
        or not valid_hostname
    ):
        raise ValueError(
            "Set BASE_DOMAIN and PUBLIC_BASE_URL to the café's canonical domain before generating table QR codes."
        )
    return f"{base_url}/menu?{urlencode({'table': table_number})}"


def use_path_tenants():
    """Use path-based tenant URLs for Render's shared onrender.com hostname."""
    mode = current_app.config.get("TENANT_ROUTING_MODE", "auto")
    if mode == "path":
        return True
    if mode == "subdomain":
        return False
    configured_base = current_app.config.get("PUBLIC_BASE_URL", "").strip()
    candidate_host = urlsplit(configured_base).hostname if configured_base else None
    request_host = request.host.split(":", 1)[0].rstrip(".").lower() if request else ""
    return bool(
        (candidate_host and candidate_host.endswith(".onrender.com"))
        or request_host.endswith(".onrender.com")
    )


def tenant_base_url(slug):
    configured_base = current_app.config["PUBLIC_BASE_URL"].rstrip("/")
    if configured_base:
        try:
            parsed = urlsplit(configured_base)
            parsed.port
        except ValueError:
            raise ValueError("PUBLIC_BASE_URL must be a valid origin.")
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("PUBLIC_BASE_URL must be your canonical HTTPS origin.")
        root_domain = parsed.hostname
        port = f":{parsed.port}" if parsed.port else ""
        if use_path_tenants():
            return f"https://{root_domain}{port}/c/{slug}"
        return f"https://{slug}.{root_domain}{port}"

    root_domain = current_app.config["BASE_DOMAIN"]
    try:
        current_port = urlsplit("//" + request.host).port
    except ValueError:
        current_port = None
    port = f":{current_port}" if current_port else ""
    scheme = request.scheme
    if use_path_tenants():
        host = request.host.split(":", 1)[0].rstrip(".").lower()
        if host.endswith(".onrender.com"):
            scheme = "https"
        elif not (current_app.debug or current_app.testing) and scheme != "https":
            raise ValueError("Configure PUBLIC_BASE_URL with HTTPS before signup or QR generation.")
        return f"{scheme}://{host}{port}/c/{slug}"
    if not (current_app.debug or current_app.testing) and scheme != "https":
        raise ValueError("Configure PUBLIC_BASE_URL with HTTPS before signup or QR generation.")
    return f"{scheme}://{slug}.{root_domain}{port}"


def make_qr_png(menu_url):
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(menu_url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#21170f", back_color="#ffffff")
    output = io.BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output


app = create_app()


if __name__ == "__main__":
    app.run()
