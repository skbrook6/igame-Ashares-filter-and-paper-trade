import sqlite3
from pathlib import Path
import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  action TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  side TEXT NOT NULL,
  price REAL NOT NULL,
  shares INTEGER NOT NULL,
  status TEXT NOT NULL,
  message TEXT,
  broker_order_id TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS positions (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  shares INTEGER NOT NULL,
  avg_cost REAL NOT NULL,
  opened_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS account (
  id INTEGER PRIMARY KEY CHECK(id=1),
  cash REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS account_equity (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  total_value REAL NOT NULL,
  cash REAL NOT NULL,
  stock_value REAL NOT NULL,
  pnl REAL NOT NULL,
  pnl_pct REAL NOT NULL,
  note TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO account(id, cash) VALUES(1, 1000000);
"""

def get_conn(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn

def df_query(conn, sql, params=()):
    return pd.read_sql_query(sql, conn, params=params)

def add_decision(conn, code, name, action, reason=""):
    """
    Set the current label for a stock.

    This is intentionally NOT an append-only click log.
    A stock should have only one current label in `decisions`.

    Examples:
    - Press BUY many times -> still one BUY label.
    - Change BUY to WATCH -> BUY disappears from to_operate_list.
    - Change SELL to HOLD -> SELL disappears from to_operate_list.
    """
    code = str(code).zfill(6)
    name = str(name)
    action = str(action).upper()
    reason = "" if reason is None else str(reason)

    old = conn.execute(
        "SELECT id, action FROM decisions WHERE code=? ORDER BY id DESC LIMIT 1",
        (code,),
    ).fetchone()

    # Remove all old labels for this stock, including duplicates created by older versions.
    conn.execute("DELETE FROM decisions WHERE code=?", (code,))

    conn.execute(
        """
        INSERT INTO decisions(code, name, action, reason, created_at)
        VALUES (?, ?, ?, ?, datetime('now','localtime'))
        """,
        (code, name, action, reason),
    )
    conn.commit()

    if old is None:
        return "created"
    if old[1] == action:
        return "unchanged"
    return "updated"

def delete_decision(conn, decision_id: int):
    conn.execute("DELETE FROM decisions WHERE id=?", (decision_id,))
    conn.commit()

def clear_decisions(conn):
    conn.execute("DELETE FROM decisions")
    conn.commit()

def deduplicate_decisions(conn):
    """Keep only the newest current label for each stock code."""
    conn.execute(
        """
        DELETE FROM decisions
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM decisions
            GROUP BY code
        )
        """
    )
    conn.commit()

def get_cash(conn) -> float:
    return float(conn.execute("SELECT cash FROM account WHERE id=1").fetchone()[0])

def set_cash(conn, cash: float):
    conn.execute("UPDATE account SET cash=? WHERE id=1", (cash,))
    conn.commit()

def upsert_position(conn, code, name, side, price, shares):
    row = conn.execute("SELECT shares, avg_cost FROM positions WHERE code=?", (code,)).fetchone()
    if side == "BUY":
        if row:
            old_shares, old_cost = row
            new_shares = old_shares + shares
            new_cost = (old_shares * old_cost + shares * price) / max(new_shares, 1)
            conn.execute("UPDATE positions SET shares=?, avg_cost=?, updated_at=datetime('now','localtime') WHERE code=?",
                         (new_shares, new_cost, code))
        else:
            conn.execute("INSERT INTO positions(code,name,shares,avg_cost) VALUES(?,?,?,?)",
                         (code, name, shares, price))
    else:
        if row:
            old_shares, old_cost = row
            new_shares = old_shares - shares
            if new_shares > 0:
                conn.execute("UPDATE positions SET shares=?, updated_at=datetime('now','localtime') WHERE code=?",
                             (new_shares, code))
            else:
                conn.execute("DELETE FROM positions WHERE code=?", (code,))
    conn.commit()

def log_order(conn, result):
    conn.execute("INSERT INTO orders(code,name,side,price,shares,status,message,broker_order_id) VALUES(?,?,?,?,?,?,?,?)",
                 (result.code, result.name, result.side, result.price, result.shares, result.status, result.message, result.broker_order_id))
    conn.commit()


def get_positions(conn):
    return df_query(conn, "SELECT * FROM positions")

def log_account_equity_snapshot(conn, total_value: float, cash: float, stock_value: float, initial_cash: float = 1000000.0, note: str = ""):
    """Append an account-level equity snapshot if it meaningfully changed.

    This records the paper account's total value = cash + marked-to-market positions.
    It is intentionally account-level PnL, not per-position PnL.
    """
    total_value = float(total_value)
    cash = float(cash)
    stock_value = float(stock_value)
    initial_cash = float(initial_cash) if float(initial_cash) != 0 else 1000000.0
    pnl = total_value - initial_cash
    pnl_pct = pnl / initial_cash if initial_cash else 0.0
    note = "" if note is None else str(note)

    last = conn.execute(
        """
        SELECT total_value, cash, stock_value
        FROM account_equity
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()

    # Avoid writing a duplicate row on every Streamlit rerun.
    if last is not None:
        last_total, last_cash, last_stock = [float(x) for x in last]
        unchanged = (
            abs(last_total - total_value) < 0.01
            and abs(last_cash - cash) < 0.01
            and abs(last_stock - stock_value) < 0.01
        )
        if unchanged:
            return False

    conn.execute(
        """
        INSERT INTO account_equity(total_value, cash, stock_value, pnl, pnl_pct, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))
        """,
        (total_value, cash, stock_value, pnl, pnl_pct, note),
    )
    conn.commit()
    return True


def get_account_equity_curve(conn):
    return df_query(conn, "SELECT * FROM account_equity ORDER BY id ASC")


def reset_paper_account(conn, initial_cash: float = 1000000.0):
    """Reset paper-trading account: decisions, orders, positions, orders, equity snapshots, and cash."""
    initial_cash = float(initial_cash)
    conn.execute("DELETE FROM decisions")
    conn.execute("DELETE FROM orders")
    conn.execute("DELETE FROM positions")
    conn.execute("DELETE FROM account_equity")
    conn.execute("UPDATE account SET cash=? WHERE id=1", (initial_cash,))
    conn.commit()
    log_account_equity_snapshot(conn, initial_cash, initial_cash, 0.0, initial_cash, note="RESET")


def delete_decisions_by_code_action(conn, code: str, action: str):
    conn.execute("DELETE FROM decisions WHERE code=? AND action=?", (str(code).zfill(6), action))
    conn.commit()


def clear_operation_decisions_for_results(conn, results):
    """Remove BUY/SELL operation decisions after corresponding paper orders are filled."""
    for r in results:
        if getattr(r, "status", "") == "FILLED":
            delete_decisions_by_code_action(conn, getattr(r, "code"), getattr(r, "side"))
