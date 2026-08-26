# -*- coding: utf-8 -*-
"""Авторизация партнёра через Telegram Mini App.

Telegram передаёт в мини-апп строку initData, подписанную ключом бота.
Проверив подпись, мы точно знаем, кто открыл кабинет — без паролей и SMS.
Документация: core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from .config import BOT_TOKEN

# initData считается протухшей через сутки — защита от переиспользования.
MAX_AGE = 86_400


class AuthError(Exception):
    """initData не прошла проверку."""


def _secret_key(token: str) -> bytes:
    return hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()


def verify_init_data(init_data: str, token: str = "") -> dict:
    """Проверяет подпись initData и возвращает данные пользователя.

    Бросает AuthError, если подпись неверна или данные устарели.
    """
    token = token or BOT_TOKEN
    if not token:
        raise AuthError("BOT_TOKEN не задан")
    if not init_data:
        raise AuthError("Пустая initData")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise AuthError("Нет подписи")

    # Подпись считается по строке вида "key=value\nkey=value", ключи по алфавиту.
    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    calc_hash = hmac.new(_secret_key(token), check_string.encode(),
                         hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calc_hash, received_hash):
        raise AuthError("Подпись не совпадает")

    auth_date = int(pairs.get("auth_date", "0") or 0)
    if auth_date and time.time() - auth_date > MAX_AGE:
        raise AuthError("Данные устарели, откройте кабинет заново")

    user_raw = pairs.get("user")
    if not user_raw:
        raise AuthError("Нет данных пользователя")

    user = json.loads(user_raw)
    return {
        "id": int(user["id"]),
        "first_name": user.get("first_name", ""),
        "last_name": user.get("last_name", ""),
        "username": user.get("username", ""),
        "auth_date": auth_date,
    }


def build_init_data(user: dict, token: str, auth_date: int | None = None) -> str:
    """Собирает корректно подписанную initData. Нужна только для тестов."""
    auth_date = auth_date or int(time.time())
    pairs = {"auth_date": str(auth_date), "user": json.dumps(user, separators=(",", ":"))}
    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    h = hmac.new(_secret_key(token), check_string.encode(), hashlib.sha256).hexdigest()
    from urllib.parse import urlencode
    return urlencode({**pairs, "hash": h})
