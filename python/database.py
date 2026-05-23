import sqlite3
import uuid
from datetime import datetime, timezone, timedelta

import os
DATA_DIR = os.environ.get("ACCOUNTING_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DATA_DIR, "accounting.db")

CST = timezone(timedelta(hours=8))

def get_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR

def get_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY,
            occurred_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'expense',
            amount REAL,
            channel TEXT NOT NULL DEFAULT 'wechat',
            counterparty TEXT DEFAULT '',
            item_desc TEXT DEFAULT '',
            payment_method TEXT DEFAULT '',
            status TEXT DEFAULT '',
            external_id TEXT UNIQUE,
            flow_type TEXT DEFAULT 'unknown',
            source TEXT NOT NULL DEFAULT 'manual',
            review_status TEXT NOT NULL DEFAULT 'confirmed',
            raw_text TEXT DEFAULT '',
            merchant_id TEXT DEFAULT '',
            notes TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS buckets (
            key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            value REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'manual',
            formula TEXT,
            parent TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bucket_history (
            id TEXT PRIMARY KEY,
            bucket_key TEXT NOT NULL,
            value REAL NOT NULL,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY (bucket_key) REFERENCES buckets(key)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reconciliation_log (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            type TEXT NOT NULL,
            detail TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS deleted_ids (
            external_id TEXT PRIMARY KEY,
            deleted_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS investment_records (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'profit',
            amount REAL NOT NULL DEFAULT 0,
            recorded_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS investment_snapshots (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            market_value REAL NOT NULL DEFAULT 0,
            net_deposit REAL NOT NULL DEFAULT 0,
            notes TEXT DEFAULT ''
        );
    """)

    # migration: add parent column if missing
    try:
        conn.execute("ALTER TABLE buckets ADD COLUMN parent TEXT")
    except:
        pass

    try:
        conn.execute("ALTER TABLE transactions ADD COLUMN review_status TEXT NOT NULL DEFAULT 'confirmed'")
    except:
        pass

    try:
        conn.execute("UPDATE transactions SET review_status = 'pending' WHERE source = 'notification' AND (review_status IS NULL OR review_status = 'confirmed')")
    except:
        pass

    now = datetime.now(CST).isoformat()

    default_buckets = [
        ('total_asset', '总资产', 0, 'manual', None, now),
        ('investment_value', '投资市值', 0, 'manual', None, now),
        ('life_value', '生活资金', 0, 'formula', 'total_asset - investment_value', now),
        ('total_liability', '总负债', 0, 'formula', None, now),
        ('net_worth', '净资产', 0, 'formula', 'total_asset - total_liability', now),
        ('investment_change', '投资变动', 0, 'formula', None, now),
    ]
    for b in default_buckets:
        conn.execute(
            "INSERT OR IGNORE INTO buckets (key, label, value, source, formula, updated_at) VALUES (?,?,?,?,?,?)",
            b
        )

    default_settings = [
        ('notification_enabled', 'true'),
    ]
    for s in default_settings:
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", s)

    conn.commit()
    conn.close()

def now_cst():
    return datetime.now(CST).isoformat()
