# -*- coding: utf-8 -*-
"""Потокобезопасный процессный TTL-кеш с single-flight загрузкой.

Зачем не lru_cache: нужен TTL, префиксная инвалидация и защита от stampede
(конкурентные запросы одного ключа не должны дублировать тяжёлую загрузку).

Границы применимости (2 gunicorn-воркера!):
- кеш ЖИВЁТ В ПРОЦЕССЕ: явная инвалидация действует только в своём воркере,
  межворкерная консистентность обеспечивается коротким TTL — кешируйте
  только то, чему допустима staleness на длину TTL;
- кешируйте ПРОСТЫЕ данные (списки/словари/скаляры), НЕ ORM-объекты:
  detached-инстансы SQLAlchemy за пределами своей сессии — источник багов;
- ошибка загрузчика не кешируется и пробрасывается наружу.
"""
import threading
import time


class TTLCache:
    def __init__(self):
        self._data = {}    # key -> (value, expires_at_monotonic)
        self._locks = {}   # key -> Lock (single-flight на время загрузки)
        self._master = threading.Lock()

    def get_or_load(self, key: str, ttl_seconds: float, loader):
        """Вернуть кешированное значение или загрузить (single-flight)."""
        now = time.monotonic()
        with self._master:
            entry = self._data.get(key)
            if entry is not None and entry[1] > now:
                return entry[0]
            key_lock = self._locks.get(key)
            if key_lock is None:
                key_lock = threading.Lock()
                self._locks[key] = key_lock

        with key_lock:
            # Пока ждали lock, другой поток мог уже загрузить
            now = time.monotonic()
            with self._master:
                entry = self._data.get(key)
                if entry is not None and entry[1] > now:
                    return entry[0]
            value = loader()  # ошибки пробрасываются, ничего не кешируем
            with self._master:
                self._data[key] = (value, time.monotonic() + ttl_seconds)
                self._locks.pop(key, None)
            return value

    def invalidate(self, prefix: str) -> int:
        """Удалить все ключи с данным префиксом. Возвращает число удалённых."""
        with self._master:
            doomed = [k for k in self._data if k.startswith(prefix)]
            for k in doomed:
                del self._data[k]
            return len(doomed)

    def clear(self):
        with self._master:
            self._data.clear()
            self._locks.clear()


# Глобальный кеш процесса — общий для всех потребителей, ключи с префиксами
cache = TTLCache()
