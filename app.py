import os
import sqlite3
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests
from flask import Flask, abort, g, jsonify, request
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 65536
ROOT = Path(__file__).resolve().parent
DB = ROOT / "reporting.sqlite3"
SERVICES = {name: os.getenv(name + "_URL", f"http://127.0.0.1:{port}")
            for name, port in [("AUTH", 5001), ("PRODUCT", 5002), ("ORDER", 5003)]}
TABLES = {"products": "product_reports", "orders": "orders_reports"}
MONEY = {"revenue", "cost", "profit", "total_revenue", "total_cost", "total_profit"}

with closing(sqlite3.connect(DB)) as connection:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS orders_reports (
            id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL,
            total_revenue INTEGER NOT NULL, total_cost INTEGER NOT NULL, total_profit INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS product_reports (
            id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, product_name TEXT NOT NULL,
            order_report_id INTEGER REFERENCES orders_reports(id) ON DELETE CASCADE,
            stock_quantity INTEGER NOT NULL, total_sold INTEGER NOT NULL,
            revenue INTEGER NOT NULL, cost INTEGER NOT NULL, profit INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS order_costs (
            order_id INTEGER, product_id INTEGER, unit_cost INTEGER NOT NULL,
            PRIMARY KEY(order_id, product_id));
    """)


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close(error):
    if "db" in g:
        g.db.close()


@app.errorhandler(HTTPException)
def failure(error):
    return jsonify(error=error.description), error.code


def fetch(service, path):
    try:
        response = requests.get(SERVICES[service] + path,
                                headers={"Authorization": request.headers["Authorization"]},
                                timeout=5, allow_redirects=False)
        if response.status_code in (401, 403, 404):
            abort(response.status_code, f"{service}: {path}")
        if response.status_code != 200:
            abort(502, f"{service} returned {response.status_code}")
        return response.json()
    except (requests.RequestException, ValueError):
        abort(502, f"{service} service unavailable")


@app.before_request
def authenticate():
    if request.path.startswith("/reports/"):
        if not request.headers.get("Authorization", "").lower().startswith("bearer "):
            abort(401, "Bearer token required")
        auth = fetch("AUTH", "/auth")
        if not isinstance(auth, dict):
            abort(502, "AUTH service returned invalid data")
        if auth.get("authenticated") is not True:
            abort(401, "Invalid token")


@app.get("/")
@app.get("/health")
def health():
    return jsonify(service="BTH5 Reporting", products="/reports/products", orders="/reports/orders")


def cents(value, invalid_status=400):
    try:
        price = Decimal(str(value))
        if price.is_finite() and 0 <= price <= Decimal("99999999.99") and price == price.quantize(Decimal(".01")):
            return int(price * 100)
    except (ValueError, InvalidOperation):
        pass
    abort(invalid_status, "Invalid monetary amount")


def upstream_item(item):
    if (not isinstance(item, dict) or type(item.get("product_id")) is not int
            or item["product_id"] <= 0 or type(item.get("quantity")) is not int
            or item["quantity"] <= 0 or "total_price" not in item):
        abort(502, "ORDER service returned an invalid item")
    cents(item["total_price"], 502)
    return item


def completed_product_items(product_id):
    orders = fetch("ORDER", "/orders")
    order_items = fetch("ORDER", "/order_items")
    if not isinstance(orders, list) or not isinstance(order_items, list):
        abort(502, "ORDER service returned invalid data")
    completed_ids = set()
    for order in orders:
        if (not isinstance(order, dict) or type(order.get("id")) is not int
                or order["id"] <= 0 or order.get("status") not in
                ("pending", "completed", "cancelled")):
            abort(502, "ORDER service returned invalid data")
        if order["status"] == "completed":
            completed_ids.add(order["id"])
    result = []
    for item in order_items:
        if (not isinstance(item, dict) or type(item.get("order_id")) is not int
                or item["order_id"] <= 0):
            abort(502, "ORDER service returned an invalid item")
        upstream_item(item)
        if item["order_id"] in completed_ids and item["product_id"] == product_id:
            result.append((item["order_id"], item))
    return completed_ids, result


def parse_order_costs(values, items):
    lines = {(str(order_id), str(item["product_id"])) for order_id, item in items}
    result = {}
    for order_id, product_costs in values.items():
        if not isinstance(product_costs, dict) or not product_costs:
            abort(400, "Each order_unit_costs entry must contain product IDs and unit costs")
        for product_id, value in product_costs.items():
            if (order_id, product_id) not in lines:
                abort(400, f"Order {order_id}, product {product_id} is outside this report")
            result[(int(order_id), int(product_id))] = cents(value)
    return result


def totals(items, defaults, by_order, key, identifier):
    saved = {(row["order_id"], row["product_id"]): row["unit_cost"] for row in db().execute(
        f"SELECT order_id, product_id, unit_cost FROM order_costs WHERE {key}=?", (identifier,)
    )}
    multiple_orders = len({order_id for order_id, _ in items}) > 1
    specified_prices = set(by_order.values())
    sold = revenue = cost = 0
    for order_id, item in items:
        pid = item["product_id"]
        pair = (order_id, pid)
        default = defaults.get(str(pid))
        specific = by_order.get(pair)
        if pair in saved:
            unit_cost = saved[pair]
            if specific is None and not multiple_orders:
                specific = default
            if specific is not None and specific != unit_cost:
                abort(409, f"Cost already recorded for order {order_id}, product {pid}")
        else:
            unit_cost = specific
            if unit_cost is None:
                # A common price is sufficient for the first uniform report. Later
                # additions or a price change must identify the newly priced order.
                if multiple_orders and (saved or specified_prices.difference({default})):
                    abort(422, f"Supply order_unit_costs for order {order_id}, product {pid}")
                if default is None:
                    abort(422, f"Supply a unit cost for order {order_id}, product {pid}")
                unit_cost = default
            db().execute("INSERT INTO order_costs VALUES (?,?,?)", (order_id, pid, unit_cost))
        sold += item["quantity"]
        revenue += cents(item["total_price"], 502)
        cost += item["quantity"] * unit_cost
    # Amounts are stored in hundredths; DECIMAL(10,2) allows ten digits.
    if any(abs(amount) > 9_999_999_999 for amount in (revenue, cost, revenue - cost)):
        abort(422, "Report total exceeds DECIMAL(10,2)")
    return sold, revenue, cost


def positive(value):
    if type(value) is not int or not 0 < value < 2**63:
        abort(400, "Expected a positive integer ID")
    return value


def completed_order(identifier):
    order = fetch("ORDER", f"/orders/{identifier}")
    if (not isinstance(order, dict) or order.get("id") != identifier
            or not isinstance(order.get("items"), list)
            or order.get("status") not in ("pending", "completed", "cancelled")):
        abort(502, "ORDER service returned invalid data")
    if order["status"] != "completed":
        abort(409, "Order must be completed")
    for item in order["items"]:
        upstream_item(item)
    return order


def report(table, identifier):
    row = db().execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone()
    if row is None:
        abort(404, "Report not found")
    return row


def serialize(row):
    return {k: format(Decimal(row[k]) / 100, ".2f") if k in MONEY else row[k] for k in row.keys()}


def create(kind, table):
    data = request.get_json(silent=True)
    key = "product_id" if kind == "products" else "order_id"
    allowed = {key, "unit_costs", "order_unit_costs"} | ({"order_report_id"} if kind == "products" else set())
    if (not isinstance(data, dict) or set(data) - allowed
            or not isinstance(data.get("unit_costs", {}), dict)
            or not isinstance(data.get("order_unit_costs", {}), dict)):
        abort(400, "Invalid report parameters")
    identifier = positive(data.get(key))
    defaults = {pid: cents(value) for pid, value in data.get("unit_costs", {}).items()}
    parent = data.get("order_report_id")
    if kind == "orders":
        order = completed_order(identifier)
        items = [(identifier, item) for item in order["items"]]
    else:
        product = fetch("PRODUCT", f"/products/{identifier}")
        if (not isinstance(product, dict) or product.get("id") != identifier
                or not isinstance(product.get("name"), str)
                or type(product.get("quantity")) is not int or product["quantity"] < 0):
            abort(502, "PRODUCT service returned invalid data")
        completed_ids, all_items = completed_product_items(identifier)
        if parent is None:
            items = all_items
        else:
            order_id = report("orders_reports", positive(parent))["order_id"]
            if order_id not in completed_ids:
                abort(409, "Linked order must be completed")
            items = [(item_order_id, item) for item_order_id, item in all_items
                     if item_order_id == order_id]
        stock_quantity = product["quantity"]
    if parent is not None and not items:
        abort(422, "Product absent from linked order")
    by_order = parse_order_costs(data.get("order_unit_costs", {}), items)
    with db():
        db().execute("BEGIN IMMEDIATE")
        sold, revenue, cost = totals(items, defaults, by_order, key, identifier)
        if kind == "orders":
            cursor = db().execute("""INSERT INTO orders_reports(order_id,total_revenue,total_cost,total_profit)
                                  VALUES(?,?,?,?)""", (identifier, revenue, cost, revenue-cost))
        else:
            cursor = db().execute("""INSERT INTO product_reports(product_id,product_name,order_report_id,
                                  stock_quantity,total_sold,revenue,cost,profit) VALUES(?,?,?,?,?,?,?,?)""",
                                  (identifier, product["name"], parent, stock_quantity, sold, revenue, cost, revenue-cost))
        result = serialize(report(table, cursor.lastrowid))
    return jsonify(result), 201


@app.route("/reports/<kind>", methods=["GET", "POST"])
def collection(kind):
    if kind not in TABLES:
        abort(404)
    table = TABLES[kind]
    if request.method == "POST":
        return create(kind, table)
    return jsonify([serialize(row) for row in db().execute(f"SELECT * FROM {table} ORDER BY id DESC")])


@app.route("/reports/<kind>/<int:identifier>", methods=["GET", "DELETE"])
def detail(kind, identifier):
    if kind not in TABLES:
        abort(404)
    table = TABLES[kind]
    row = report(table, positive(identifier))
    if request.method == "GET":
        return jsonify(serialize(row))
    with db():
        db().execute(f"DELETE FROM {table} WHERE id=?", (identifier,))
    return "", 204


if __name__ == "__main__":
    app.run(port=5004)
