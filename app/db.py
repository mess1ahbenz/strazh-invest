"""SQLite: правила и их версии, заявки, сделки, журнал, очередь уведомлений, защита от повторов."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import Execution, OrderView, Rule, RuleSpec

log = logging.getLogger(__name__)
DRAFT_TTL_SEC = 15 * 60          # черновик старше этого забывается: новое правило не сольётся со старым

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (id INTEGER PRIMARY KEY, status TEXT NOT NULL, version INTEGER NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS rule_versions (rule_id INTEGER NOT NULL, version INTEGER NOT NULL, spec TEXT NOT NULL,
    confirmed_by TEXT NOT NULL, confirmed_at REAL NOT NULL, note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (rule_id, version));
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, scenario TEXT, status TEXT NOT NULL, params TEXT NOT NULL,
    started_at REAL NOT NULL, ended_at REAL);
CREATE TABLE IF NOT EXISTS orders (client_order_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, account TEXT NOT NULL,
    ticker TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL, limit_price TEXT NOT NULL, status TEXT NOT NULL,
    filled_qty INTEGER NOT NULL, gross TEXT NOT NULL, fee TEXT NOT NULL, sent_at INTEGER, arrival_at INTEGER,
    closed_at INTEGER, reason TEXT);
CREATE TABLE IF NOT EXISTS executions (exec_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, client_order_id TEXT NOT NULL,
    account TEXT NOT NULL, ticker TEXT NOT NULL, qty INTEGER NOT NULL, price TEXT NOT NULL, gross TEXT NOT NULL,
    fee TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts INTEGER NOT NULL,
    wall REAL NOT NULL, account TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS journal_run ON journal (run_id, id);
CREATE TABLE IF NOT EXISTS actions (key TEXT PRIMARY KEY, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT NOT NULL UNIQUE,
    text TEXT NOT NULL, buttons TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    next_try REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat TEXT NOT NULL, direction TEXT NOT NULL,
    text TEXT NOT NULL, buttons TEXT NOT NULL, kind TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS drafts (chat TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS telegram_updates (update_id INTEGER PRIMARY KEY, received_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS flags (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Database:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._open(path)
        except sqlite3.DatabaseError:
            if path == ":memory:":
                raise
            # повреждённый файл не должен валить запуск: откладываем его в сторону и начинаем с чистой базы
            broken = f"{path}.corrupt-{int(time.time())}"
            for suffix in ("", "-wal", "-shm"):
                if Path(path + suffix).exists():
                    Path(path + suffix).rename(broken + suffix)
            log.error("База %s повреждена, перенесена в %s, создана новая", path, broken)
            self._open(path)
        self._depth = 0

    def _open(self, path: str) -> None:
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
        except sqlite3.DatabaseError:
            self.conn.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._depth += 1
        if self._depth == 1:
            self.conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("ROLLBACK")
            raise
        self._depth -= 1
        if self._depth == 0:
            self.conn.execute("COMMIT")

    def _rows(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # --- правила

    def create_rule(self, spec: RuleSpec, confirmed_by: str, note: str = "") -> Rule:
        now = time.time()
        with self.transaction():
            cur = self.conn.execute("INSERT INTO rules (status, version, created_at, updated_at) VALUES ('active', 1, ?, ?)",
                                    (now, now))
            rule_id = cur.lastrowid
            self._add_version(rule_id, 1, spec, confirmed_by, note, now)
        return Rule(rule_id, 1, spec)

    def add_version(self, rule_id: int, spec: RuleSpec, confirmed_by: str, note: str = "") -> Rule:
        """Новая версия только у действующего правила: удалённое и приостановленное не меняются."""
        now = time.time()
        with self.transaction():
            row = self.conn.execute("SELECT version, status FROM rules WHERE id = ?", (rule_id,)).fetchone()
            if row is None or row["status"] == "deleted":
                raise ValueError(f"Правило №{rule_id} удалено, изменить его нельзя")
            if row["status"] != "active":
                raise ValueError(f"Правило №{rule_id} приостановлено: сначала возобновите его")
            version = row["version"] + 1
            self._add_version(rule_id, version, spec, confirmed_by, note, now)
            self.conn.execute("UPDATE rules SET version = ?, updated_at = ? WHERE id = ?", (version, now, rule_id))
        return Rule(rule_id, version, spec, row["status"])

    def _add_version(self, rule_id: int, version: int, spec: RuleSpec, by: str, note: str, now: float) -> None:
        self.conn.execute("INSERT INTO rule_versions (rule_id, version, spec, confirmed_by, confirmed_at, note) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (rule_id, version, json.dumps(spec.to_dict()), by, now, note))

    def set_rule_status(self, rule_id: int, status: str) -> None:
        self.conn.execute("UPDATE rules SET status = ?, updated_at = ? WHERE id = ?", (status, time.time(), rule_id))

    def rules(self, include_deleted: bool = False) -> list[Rule]:
        rows = self._rows("SELECT r.id, r.status, r.version, v.spec FROM rules r JOIN rule_versions v "
                          "ON v.rule_id = r.id AND v.version = r.version ORDER BY r.id")
        return [Rule(r["id"], r["version"], RuleSpec.from_dict(json.loads(r["spec"])), r["status"]) for r in rows
                if include_deleted or r["status"] != "deleted"]

    def rule_versions(self, rule_id: int) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM rule_versions WHERE rule_id = ? ORDER BY version", rule_id)

    # --- прогоны и торговые записи

    def start_run(self, run_id: str, scenario: str | None, params: dict[str, Any]) -> int:
        """Вернуть число прерванных прогонов. Их незакрытые заявки получают статус `interrupted`."""
        self.conn.execute("UPDATE orders SET status = 'interrupted', reason = 'Прогон прерван' WHERE status IN "
                          "('pending', 'new', 'partial') AND run_id IN (SELECT id FROM runs WHERE status = 'active')")
        cur = self.conn.execute("UPDATE runs SET status = 'interrupted', ended_at = ? WHERE status = 'active'", (time.time(),))
        self.conn.execute("INSERT INTO runs (id, scenario, status, params, started_at) VALUES (?, ?, 'active', ?, ?)",
                          (run_id, scenario, json.dumps(params, ensure_ascii=False), time.time()))
        return cur.rowcount

    def finish_run(self, run_id: str, status: str = "finished") -> None:
        self.conn.execute("UPDATE runs SET status = ?, ended_at = ? WHERE id = ?", (status, time.time(), run_id))

    def save_order(self, view: OrderView, run_id: str) -> None:
        self.conn.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(client_order_id) DO UPDATE SET "
            "status=excluded.status, filled_qty=excluded.filled_qty, gross=excluded.gross, fee=excluded.fee, "
            "arrival_at=excluded.arrival_at, closed_at=excluded.closed_at, reason=excluded.reason",
            (view.client_order_id, run_id, view.account, view.ticker, str(view.side), view.qty, str(view.limit_price),
             str(view.status), view.filled_qty, str(view.gross), str(view.fee), view.sent_at, view.arrival_at,
             view.closed_at, view.reason))

    def save_execution(self, execution: Execution, run_id: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO executions VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (execution.exec_id, run_id, execution.client_order_id, execution.account, execution.ticker,
                           execution.qty, str(execution.price), str(execution.gross), str(execution.fee), execution.ts))

    def orders(self, run_id: str, account: str | None = None) -> list[dict[str, Any]]:
        if account:
            return self._rows("SELECT * FROM orders WHERE run_id = ? AND account = ? ORDER BY sent_at, rowid", run_id, account)
        return self._rows("SELECT * FROM orders WHERE run_id = ? ORDER BY sent_at, rowid", run_id)

    def executions(self, run_id: str, account: str | None = None) -> list[dict[str, Any]]:
        if account:
            return self._rows("SELECT * FROM executions WHERE run_id = ? AND account = ? ORDER BY ts, rowid", run_id, account)
        return self._rows("SELECT * FROM executions WHERE run_id = ? ORDER BY ts, rowid", run_id)

    def claim(self, key: str) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO actions (key, created_at) VALUES (?, ?)", (key, time.time()))
        return cur.rowcount == 1

    def claimed(self, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM actions WHERE key = ?", (key,)).fetchone() is not None

    def log(self, run_id: str, ts: int, account: str, kind: str, text: str, data: dict[str, Any]) -> None:
        self.conn.execute("INSERT INTO journal (run_id, ts, wall, account, kind, text, data) VALUES (?,?,?,?,?,?,?)",
                          (run_id, ts, time.time(), account, kind, text, json.dumps(data, ensure_ascii=False, default=str)))

    def journal(self, run_id: str, limit: int = 300, account: str | None = None) -> list[dict[str, Any]]:
        if account:
            return self._rows("SELECT * FROM journal WHERE run_id = ? AND account = ? ORDER BY id DESC LIMIT ?", run_id, account, limit)
        return self._rows("SELECT * FROM journal WHERE run_id = ? ORDER BY id DESC LIMIT ?", run_id, limit)

    def flag(self, key: str) -> bool:
        row = self.conn.execute("SELECT value FROM flags WHERE key = ?", (key,)).fetchone()
        return bool(row and row["value"] == "1")

    def set_flag(self, key: str, value: bool) -> None:
        self.conn.execute("INSERT INTO flags (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                          (key, "1" if value else "0"))

    # --- уведомления и чат

    def enqueue(self, dedupe_key: str, text: str, buttons: list | None = None) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO outbox (dedupe_key, text, buttons, status, created_at) "
                                "VALUES (?, ?, ?, 'pending', ?)",
                                (dedupe_key, text, json.dumps(buttons or [], ensure_ascii=False), time.time()))
        return cur.rowcount == 1

    def pending_outbox(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM outbox WHERE status = 'pending' AND next_try <= ? ORDER BY id LIMIT ?",
                          time.time(), limit)

    def mark_sent(self, outbox_id: int) -> None:
        self.conn.execute("UPDATE outbox SET status = 'sent', attempts = attempts + 1 WHERE id = ?", (outbox_id,))

    def mark_failed(self, outbox_id: int, error: str, delay: float) -> None:
        self.conn.execute("UPDATE outbox SET attempts = attempts + 1, next_try = ?, last_error = ? WHERE id = ?",
                          (time.time() + delay, error[:300], outbox_id))

    def mark_dead(self, outbox_id: int, error: str) -> None:
        """Постоянная ошибка: сообщение больше не отправляется, причина остаётся в базе."""
        self.conn.execute("UPDATE outbox SET status = 'failed', attempts = attempts + 1, last_error = ? WHERE id = ?",
                          (error[:300], outbox_id))

    def outbox_stats(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self._rows("SELECT status, count(*) n FROM outbox GROUP BY status")}

    def add_chat(self, chat: str, direction: str, text: str, buttons: list | None = None, kind: str = "reply") -> int:
        cur = self.conn.execute("INSERT INTO chat_messages (chat, direction, text, buttons, kind, created_at) VALUES (?,?,?,?,?,?)",
                                (chat, direction, text, json.dumps(buttons or [], ensure_ascii=False), kind, time.time()))
        return cur.lastrowid

    def chat(self, chat: str, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM chat_messages WHERE chat = ? AND id > ? ORDER BY id DESC LIMIT ?", chat, after, limit)
        for row in rows:
            row["buttons"] = json.loads(row["buttons"])
        return rows[::-1]

    def save_draft(self, chat: str, data: dict[str, Any] | None) -> None:
        if data is None:
            self.conn.execute("DELETE FROM drafts WHERE chat = ?", (chat,))
            return
        self.conn.execute("INSERT INTO drafts (chat, data, updated_at) VALUES (?, ?, ?) ON CONFLICT(chat) DO UPDATE SET "
                          "data = excluded.data, updated_at = excluded.updated_at",
                          (chat, json.dumps(data, ensure_ascii=False), time.time()))

    def draft(self, chat: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT data, updated_at FROM drafts WHERE chat = ?", (chat,)).fetchone()
        if row is None or time.time() - row["updated_at"] > DRAFT_TTL_SEC:
            return None
        return json.loads(row["data"])

    def drop_edit_drafts(self, rule_id: int) -> None:
        """Удаление и пауза правила сбрасывают черновики его правки во всех чатах."""
        for row in self._rows("SELECT chat, data FROM drafts"):
            data = json.loads(row["data"])
            if data.get("mode") == "edit" and data.get("rule_id") == rule_id:
                self.conn.execute("DELETE FROM drafts WHERE chat = ?", (row["chat"],))

    def seen_update(self, update_id: int) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO telegram_updates VALUES (?, ?)", (update_id, time.time()))
        return cur.rowcount == 0
