-- Practice database: a small coffee shop
DROP TABLE IF EXISTS ratings, order_items, orders, products, customers, events CASCADE;

CREATE TABLE customers (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE,
    joined_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE products (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    price_vnd   INTEGER NOT NULL CHECK (price_vnd > 0),
    category    TEXT NOT NULL CHECK (category IN ('coffee', 'tea', 'cake'))
);

CREATE TABLE orders (
    id           BIGSERIAL PRIMARY KEY,
    customer_id  BIGINT NOT NULL REFERENCES customers(id),
    status       TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'PAID', 'CANCELLED')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- one customer can have only one OPEN order at a time
CREATE UNIQUE INDEX one_open_order_per_customer ON orders (customer_id) WHERE status = 'OPEN';

CREATE TABLE order_items (
    order_id    BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id  BIGINT NOT NULL REFERENCES products(id),
    qty         INTEGER NOT NULL CHECK (qty > 0),
    PRIMARY KEY (order_id, product_id)
);

-- latest rating per customer per product; version must only go up
CREATE TABLE ratings (
    customer_id BIGINT NOT NULL REFERENCES customers(id),
    product_id  BIGINT NOT NULL REFERENCES products(id),
    stars       INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
    version     INTEGER NOT NULL,
    rated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (customer_id, product_id)
);

-- raw log of incoming messages, kept exactly as received
CREATE TABLE events (
    id           BIGSERIAL PRIMARY KEY,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    source       TEXT NOT NULL,
    payload_text TEXT NOT NULL,
    payload      JSONB
);

INSERT INTO customers (name, email) VALUES
  ('An',   'an@example.com'),
  ('Binh', 'binh@example.com'),
  ('Chi',  'chi@example.com'),
  ('Dung', 'dung@example.com');

INSERT INTO products (name, price_vnd, category) VALUES
  ('Espresso',   35000, 'coffee'),
  ('Latte',      45000, 'coffee'),
  ('Green tea',  30000, 'tea'),
  ('Cheesecake', 55000, 'cake');

INSERT INTO orders (customer_id, status) VALUES
  (1, 'PAID'), (1, 'PAID'), (2, 'PAID'), (3, 'CANCELLED'), (2, 'OPEN');

INSERT INTO order_items (order_id, product_id, qty) VALUES
  (1, 1, 2), (1, 4, 1),
  (2, 2, 1),
  (3, 2, 2), (3, 3, 1),
  (4, 1, 1),
  (5, 4, 2);
