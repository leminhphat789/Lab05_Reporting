"""TH4: independent order database; HTTP-only service integration."""
import json
import os
import re
import sqlite3
from uuid import uuid4
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException


class APIError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def remote(base, path, method='GET'):
    req = Request(base.rstrip('/') + path, method=method,
                  headers={'Authorization': request.headers.get('Authorization', '')})
    try:
        with urlopen(req, timeout=3) as response:
            return json.load(response)
    except HTTPError as exc:
        exc.close()
        if exc.code == 401:
            raise APIError('INVALID_OR_EXPIRED_TOKEN', 401) from exc
        if exc.code == 404 and path.startswith('/products/'):
            raise APIError('PRODUCT_NOT_FOUND', 404) from exc
        raise APIError('UPSTREAM_ERROR', 502) from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise APIError('SERVICE_UNAVAILABLE', 503) from exc


def positive(value, field):
    if type(value) is not int or not 1 <= value <= 2147483647:
        raise APIError('INVALID_' + field.upper())
    return value


def money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > Decimal('99999999.99'):
            raise ValueError()
        if amount != amount.quantize(Decimal('0.01')):
            raise ValueError()
        return amount.quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        raise APIError('INVALID_OR_EXCESSIVE_AMOUNT')


def body(allowed, required):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) - set(allowed) or set(required) - set(data):
        raise APIError('INVALID_BODY')
    return data


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(DATABASE=os.environ.get('ORDER_DATABASE', str(Path(__file__).parent / 'instance/orders.db')),
                      AUTH_URL=os.environ.get('ORDER_AUTH_URL', 'http://127.0.0.1:5001'),
                      PRODUCT_URL=os.environ.get('PRODUCT_URL', 'http://127.0.0.1:5002'),
                      MAX_CONTENT_LENGTH=65536)
    app.config.update(config or {})
    Path(app.config['DATABASE']).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(app.config['DATABASE'])) as conn:
        conn.executescript(Path(__file__).with_name('schema.sql').read_text())

    def db():
        if 'db' not in g:
            g.db = sqlite3.connect(app.config['DATABASE'], timeout=5)
            g.db.row_factory = sqlite3.Row
            g.db.execute('PRAGMA foreign_keys=ON')
        return g.db

    @app.teardown_appcontext
    def close(error=None):
        conn = g.pop('db', None)
        if conn is not None:
            conn.close()  # Rolls back any failed transaction.

    @app.before_request
    def authenticate():
        if request.path == '/health':
            return
        parts = request.headers.get('Authorization', '').split()
        if len(parts) != 2 or parts[0].lower() != 'bearer':
            raise APIError('MISSING_TOKEN', 401)
        result = remote(app.config['AUTH_URL'], '/auth')
        if not isinstance(result, dict) or result.get('authenticated') is not True:
            raise APIError('AUTH_SERVICE_INVALID_RESPONSE', 502)

    @app.errorhandler(APIError)
    def api_error(exc):
        response = jsonify(error=exc.code)
        response.status_code = exc.status
        if exc.status == 401:
            response.headers['WWW-Authenticate'] = 'Bearer'
        return response

    @app.errorhandler(HTTPException)
    def http_error(exc):
        response = exc.get_response()
        response.data = app.json.dumps({'error': exc.name.upper().replace(' ', '_')})
        response.content_type = 'application/json'
        return response

    @app.errorhandler(sqlite3.OperationalError)
    def database_busy(exc):
        app.logger.error('Database operation failed: %s', exc)
        return jsonify(error='DATABASE_UNAVAILABLE'), 503

    def find(table, identifier):
        positive(identifier, 'id')
        row = db().execute(f'SELECT * FROM {table} WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise APIError('NOT_FOUND', 404)
        return row

    def serialize(row):
        result = dict(row)
        for key in ('total_amount', 'unit_price', 'total_price'):
            if key in result:
                result[key] = format(money(result[key]), '.2f')
        return result

    def order_json(identifier):
        result = serialize(find('orders', identifier))
        result['items'] = [serialize(r) for r in db().execute(
            'SELECT * FROM order_items WHERE order_id=? ORDER BY id', (identifier,))]
        return result

    def pending(identifier):
        row = find('orders', identifier)
        if row['status'] != 'pending':
            raise APIError('ORDER_NOT_PENDING', 409)
        if db().execute('SELECT 1 FROM stock_completions WHERE order_id=?', (identifier,)).fetchone():
            raise APIError('COMPLETION_IN_PROGRESS_RETRY_COMPLETED', 409)
        return row

    def complete(identifier):
        attempt = db().execute('SELECT attempt FROM stock_completions WHERE order_id=?',
                               (identifier,)).fetchone()
        if attempt:
            token = attempt['attempt']
        else:
            if not db().execute('SELECT 1 FROM order_items WHERE order_id=?', (identifier,)).fetchone():
                raise APIError('EMPTY_ORDER', 409)
            token = uuid4().hex
            db().execute('INSERT INTO stock_completions(order_id,attempt) VALUES (?,?)', (identifier, token))
        # Persist the intent before contacting Product. A retry uses the identical key.
        db().commit()
        result = remote(app.config['PRODUCT_URL'], f'/stock/orders/{identifier}/{token}', 'PUT')
        if (not isinstance(result, dict) or result.get('order_id') != identifier or result.get('attempt') != token
                or result.get('outcome') not in ('applied', 'insufficient_stock', 'product_not_found')):
            raise APIError('PRODUCT_SERVICE_INVALID_RESPONSE', 502)
        outcome = result['outcome']
        db().execute('BEGIN IMMEDIATE')
        removed = db().execute('DELETE FROM stock_completions WHERE order_id=? AND attempt=?',
                               (identifier, token)).rowcount
        if removed and outcome == 'applied':
            db().execute("UPDATE orders SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (identifier,))
        db().commit()
        if outcome == 'insufficient_stock':
            raise APIError('INSUFFICIENT_STOCK', 409)
        if outcome == 'product_not_found':
            raise APIError('PRODUCT_NOT_FOUND', 404)

    @app.get('/orders/<int:identifier>/stock-attempts/<attempt>')
    def stock_attempt(identifier, attempt):
        # Read the frozen lines and verify their attempt in a single snapshot.
        lines = db().execute('SELECT i.product_id,i.quantity FROM order_items AS i '
                             'JOIN stock_completions AS s ON s.order_id=i.order_id '
                             'WHERE s.order_id=? AND s.attempt=? ORDER BY i.product_id',
                             (positive(identifier, 'id'), attempt)).fetchall()
        if not lines:
            raise APIError('STOCK_ATTEMPT_NOT_FOUND', 404)
        return jsonify([dict(line) for line in lines])

    def product(identifier, quantity):
        result = remote(app.config['PRODUCT_URL'], f'/products/{identifier}')
        if not isinstance(result, dict) or type(result.get('quantity')) is not int or result['quantity'] < 0 or not isinstance(result.get('name'), str) or not 1 <= len(result['name']) <= 255:
            raise APIError('PRODUCT_SERVICE_INVALID_RESPONSE', 502)
        try:
            price = money(result['price'])
        except (KeyError, APIError) as exc:
            raise APIError('PRODUCT_SERVICE_INVALID_RESPONSE', 502) from exc
        if result['quantity'] < quantity:
            raise APIError('INSUFFICIENT_STOCK', 409)
        return result['name'], price

    def total(identifier):
        value = sum((money(r['total_price']) for r in db().execute(
            'SELECT total_price FROM order_items WHERE order_id=?', (identifier,))), Decimal(0))
        db().execute('UPDATE orders SET total_amount=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                     (str(money(value)), identifier))

    def insert_item(identifier, data):
        if not isinstance(data, dict) or set(data) != {'product_id', 'quantity'}:
            raise APIError('INVALID_ITEM')
        pid = positive(data['product_id'], 'product_id')
        quantity = positive(data['quantity'], 'quantity')
        if db().execute('SELECT id FROM order_items WHERE order_id=? AND product_id=?', (identifier, pid)).fetchone():
            raise APIError('DUPLICATE_PRODUCT', 409)
        name, price = product(pid, quantity)
        return db().execute('INSERT INTO order_items (order_id,product_id,product_name,quantity,unit_price,total_price) VALUES (?,?,?,?,?,?)',
                            (identifier, pid, name, quantity, str(price), str(money(price * quantity)))).lastrowid

    @app.get('/health')
    def health():
        return jsonify(service='orders', status='ok')

    @app.get('/orders')
    def list_orders():
        return jsonify([serialize(r) for r in db().execute('SELECT * FROM orders ORDER BY id')])

    @app.get('/orders/<int:identifier>')
    def get_order(identifier):
        return jsonify(order_json(identifier))

    @app.post('/orders')
    def create_order():
        data = body(('customer_name', 'customer_email', 'items'), ('customer_name', 'customer_email', 'items'))
        for key in ('customer_name', 'customer_email'):
            if not isinstance(data[key], str) or not 1 <= len(data[key].strip()) <= 255:
                raise APIError('INVALID_' + key.upper())
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', data['customer_email'].strip()):
            raise APIError('INVALID_CUSTOMER_EMAIL')
        if not isinstance(data['items'], list) or not 1 <= len(data['items']) <= 100:
            raise APIError('INVALID_ITEMS')
        db().execute('BEGIN IMMEDIATE')
        identifier = db().execute('INSERT INTO orders (customer_name,customer_email) VALUES (?,?)',
                                  (data['customer_name'].strip(), data['customer_email'].strip())).lastrowid
        for item in data['items']:
            insert_item(identifier, item)
        total(identifier)
        db().commit()
        return jsonify(order_json(identifier)), 201

    @app.put('/orders/<int:identifier>')
    def update_order(identifier):
        data = body(('status',), ('status',))
        if data['status'] not in ('pending', 'completed', 'cancelled'):
            raise APIError('INVALID_STATUS')
        db().execute('BEGIN IMMEDIATE')
        row = find('orders', identifier)
        if row['status'] != data['status']:
            if row['status'] == 'pending' and data['status'] == 'completed':
                complete(identifier)
            else:
                pending(identifier)
                db().execute('UPDATE orders SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                             (data['status'], identifier))
        db().commit()
        return jsonify(order_json(identifier))

    @app.delete('/orders/<int:identifier>')
    def delete_order(identifier):
        db().execute('BEGIN IMMEDIATE')
        pending(identifier)
        db().execute('DELETE FROM orders WHERE id=?', (identifier,))
        db().commit()
        return '', 204

    @app.get('/order_items')
    def list_items():
        order_id = request.args.get('order_id')
        if order_id is not None:
            if not order_id.isascii() or not order_id.isdigit() or not 1 <= int(order_id) <= 2147483647:
                raise APIError('INVALID_ORDER_ID')
            rows = db().execute('SELECT * FROM order_items WHERE order_id=? ORDER BY id', (int(order_id),))
        else:
            rows = db().execute('SELECT * FROM order_items ORDER BY id')
        return jsonify([serialize(r) for r in rows])

    @app.get('/order_items/<int:identifier>')
    def get_item(identifier):
        return jsonify(serialize(find('order_items', identifier)))

    @app.post('/order_items')
    def create_item():
        data = body(('order_id', 'product_id', 'quantity'), ('order_id', 'product_id', 'quantity'))
        order_id = positive(data.pop('order_id'), 'order_id')
        db().execute('BEGIN IMMEDIATE')
        pending(order_id)
        identifier = insert_item(order_id, data)
        total(order_id)
        db().commit()
        return jsonify(serialize(find('order_items', identifier))), 201

    @app.put('/order_items/<int:identifier>')
    def update_item(identifier):
        data = body(('quantity',), ('quantity',))
        quantity = positive(data['quantity'], 'quantity')
        db().execute('BEGIN IMMEDIATE')
        item = find('order_items', identifier)
        pending(item['order_id'])
        product(item['product_id'], quantity)
        amount = money(money(item['unit_price']) * quantity)
        db().execute('UPDATE order_items SET quantity=?,total_price=? WHERE id=?', (quantity, str(amount), identifier))
        total(item['order_id'])
        db().commit()
        return jsonify(serialize(find('order_items', identifier)))

    @app.delete('/order_items/<int:identifier>')
    def delete_item(identifier):
        db().execute('BEGIN IMMEDIATE')
        item = find('order_items', identifier)
        pending(item['order_id'])
        db().execute('DELETE FROM order_items WHERE id=?', (identifier,))
        total(item['order_id'])
        db().commit()
        return '', 204

    return app


if __name__ == '__main__':
    create_app().run(host='127.0.0.1', port=5003, debug=False)
