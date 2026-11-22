PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS orders (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 customer_name VARCHAR(255) NOT NULL,
 customer_email VARCHAR(255) NOT NULL,
 total_amount DECIMAL(10,2) NOT NULL DEFAULT 0 CHECK(total_amount >= 0 AND total_amount <= 99999999.99),
 status VARCHAR(50) NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','completed','cancelled')),
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS order_items (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
 product_id INTEGER NOT NULL CHECK(product_id > 0),
 product_name VARCHAR(255) NOT NULL,
 quantity INTEGER NOT NULL CHECK(quantity > 0),
 unit_price DECIMAL(10,2) NOT NULL CHECK(unit_price >= 0),
 total_price DECIMAL(10,2) NOT NULL CHECK(total_price >= 0 AND total_price <= 99999999.99),
 UNIQUE(order_id, product_id)
);

-- A pending completion survives network timeouts and freezes its order lines.
CREATE TABLE IF NOT EXISTS stock_completions (
 order_id INTEGER PRIMARY KEY REFERENCES orders(id) ON DELETE CASCADE,
 attempt TEXT NOT NULL UNIQUE
);
