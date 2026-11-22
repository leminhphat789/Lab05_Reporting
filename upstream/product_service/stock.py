"""Apply a frozen order to on-hand stock exactly once."""
import re

import pymysql
import requests
from flask import current_app, jsonify, request

from database import get_db


def receipt(order_id, attempt, outcome):
    # A received decision always returns 200, including a definitive stock refusal.
    return jsonify(order_id=order_id, attempt=attempt, outcome=outcome)


def issue_stock(order_id, attempt):
    if not 0 < order_id <= 2147483647 or not re.fullmatch(r'[0-9a-f]{32}', attempt):
        return jsonify(error='INVALID_STOCK_ATTEMPT'), 400
    db = get_db()
    key = (order_id, attempt)
    with db.cursor() as cursor:
        cursor.execute('SELECT outcome FROM order_stock_attempts WHERE order_id=%s AND attempt=%s', key)
        previous = cursor.fetchone()
    db.commit()
    if previous:
        return receipt(order_id, attempt, previous['outcome'])

    # The order service owns the lines. An API caller cannot invent a sale here.
    url = current_app.config['ORDER_URL'].rstrip('/') + f'/orders/{order_id}/stock-attempts/{attempt}'
    try:
        response = requests.get(url, headers={'Authorization': request.headers['Authorization']},
                                timeout=5, allow_redirects=False)
        if response.status_code in (401, 403, 404):
            return jsonify(error='INVALID_ORDER_STOCK_ATTEMPT'), response.status_code
        response.raise_for_status()
        lines = response.json()
        if not isinstance(lines, list) or not lines:
            raise ValueError('empty or malformed stock issue')
        items = {}
        for line in lines:
            pid, qty = line['product_id'], line['quantity']
            if (type(pid) is not int or not 0 < pid <= 2147483647 or pid in items
                    or type(qty) is not int or not 0 < qty <= 2147483647):
                raise ValueError('invalid stock line')
            items[pid] = qty
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return jsonify(error='ORDER_SERVICE_UNAVAILABLE'), 502

    with db.cursor() as cursor:
        # Concurrent deliveries of one attempt take the same receipt lock.
        cursor.execute('INSERT INTO order_stock_attempts(order_id,attempt) VALUES (%s,%s) '
                       'ON DUPLICATE KEY UPDATE attempt=VALUES(attempt)', key)
        cursor.execute('SELECT outcome FROM order_stock_attempts '
                       'WHERE order_id=%s AND attempt=%s FOR UPDATE', key)
        outcome = cursor.fetchone()['outcome']
        if outcome is None:
            outcome = 'applied'
            for pid in sorted(items):
                cursor.execute('SELECT quantity FROM products WHERE id=%s FOR UPDATE', (pid,))
                row = cursor.fetchone()
                if row is None:
                    outcome = 'product_not_found'
                    break
                if row['quantity'] < items[pid]:
                    outcome = 'insufficient_stock'
                    break
            if outcome == 'applied':
                for pid, qty in items.items():
                    cursor.execute('UPDATE products SET quantity=quantity-%s WHERE id=%s', (qty, pid))
            try:
                cursor.execute('UPDATE order_stock_attempts SET outcome=%s, issued_order_id=%s '
                               'WHERE order_id=%s AND attempt=%s',
                               (outcome, order_id if outcome == 'applied' else None, *key))
            except pymysql.IntegrityError as exc:
                db.rollback()
                if exc.args[0] == 1062:
                    return jsonify(error='STOCK_ISSUED_FOR_DIFFERENT_ATTEMPT'), 409
                raise
    db.commit()
    return receipt(order_id, attempt, outcome)
