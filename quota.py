"""Persistent daily quotas and Telegram Stars Plus+ passes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg

FREE_LIMIT = 5
FREE_MAX_MB = 25
PLUS_LIMIT = 30
PLUS_MAX_MB = 49
PLUS_PRICE = 99
PERIOD = timedelta(hours=24)


class StorageUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class QuotaStatus:
    is_plus: bool
    remaining: int
    resets_at: datetime
    max_mb: int


def configured() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise StorageUnavailable("Постоянная база данных не настроена.")
    return psycopg.connect(url, connect_timeout=3)


def initialize() -> None:
    if not configured():
        return
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_usage (
                user_id BIGINT PRIMARY KEY,
                window_start TIMESTAMPTZ NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plus_passes (
                charge_id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                purchased_at TIMESTAMPTZ NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                refunded BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS plus_passes_user_idx ON plus_passes (user_id, expires_at)")


def _get_status(conn, user_id: int, now: datetime, lock: bool = False) -> tuple[QuotaStatus, str | None]:
    suffix = " FOR UPDATE" if lock else ""
    passes = conn.execute(
        "SELECT charge_id, used, expires_at FROM plus_passes "
        "WHERE user_id = %s AND purchased_at <= %s AND expires_at > %s AND refunded = FALSE "
        f"ORDER BY purchased_at ASC{suffix}", (user_id, now, now)
    ).fetchall()
    for charge_id, used, expires_at in passes:
        if used < PLUS_LIMIT:
            return QuotaStatus(True, PLUS_LIMIT - used, expires_at, PLUS_MAX_MB), charge_id
    if passes:
        return QuotaStatus(True, 0, max(p[2] for p in passes), PLUS_MAX_MB), None
    row = conn.execute(
        f"SELECT window_start, used FROM daily_usage WHERE user_id = %s{suffix}", (user_id,)
    ).fetchone()
    if not row or now >= row[0] + PERIOD:
        return QuotaStatus(False, FREE_LIMIT, now + PERIOD, FREE_MAX_MB), None
    return QuotaStatus(False, max(0, FREE_LIMIT - row[1]), row[0] + PERIOD, FREE_MAX_MB), None


def get_status(user_id: int) -> QuotaStatus:
    now = datetime.now(timezone.utc)
    with _connect() as conn:
        status, _ = _get_status(conn, user_id, now)
        return status


def record_success(user_id: int) -> QuotaStatus:
    """Count a successfully delivered file, once per user at a time."""
    now = datetime.now(timezone.utc)
    with _connect() as conn:
        # Lock the user row to serialize concurrent requests across instances.
        conn.execute(
            "INSERT INTO daily_usage (user_id, window_start, used) VALUES (%s, %s, 0) "
            "ON CONFLICT (user_id) DO NOTHING", (user_id, now)
        )
        row = conn.execute(
            "SELECT window_start, used FROM daily_usage WHERE user_id = %s FOR UPDATE", (user_id,)
        ).fetchone()
        status, charge_id = _get_status(conn, user_id, now, lock=True)
        if status.remaining <= 0:
            return status
        if charge_id:
            conn.execute("UPDATE plus_passes SET used = used + 1 WHERE charge_id = %s", (charge_id,))
        else:
            if now >= row[0] + PERIOD:
                conn.execute("UPDATE daily_usage SET window_start = %s, used = 1 WHERE user_id = %s",
                             (now, user_id))
            else:
                conn.execute("UPDATE daily_usage SET used = used + 1 WHERE user_id = %s", (user_id,))
        next_status, _ = _get_status(conn, user_id, now)
        return next_status


def grant_plus(user_id: int, charge_id: str, amount: int) -> bool:
    if amount != PLUS_PRICE or not charge_id:
        raise ValueError("Неверные данные платежа Plus+.")
    now = datetime.now(timezone.utc)
    with _connect() as conn:
        result = conn.execute(
            "INSERT INTO plus_passes (charge_id, user_id, purchased_at, expires_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (charge_id) DO NOTHING",
            (charge_id, user_id, now, now + PERIOD),
        )
        return result.rowcount == 1


def refund_plus(charge_id: str) -> bool:
    with _connect() as conn:
        result = conn.execute(
            "UPDATE plus_passes SET refunded = TRUE WHERE charge_id = %s AND refunded = FALSE",
            (charge_id,),
        )
        return result.rowcount == 1
