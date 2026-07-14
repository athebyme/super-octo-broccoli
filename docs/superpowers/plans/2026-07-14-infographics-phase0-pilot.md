# «Фотостудия» Phase 0 (пилот) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Провайдер-слой image-edit (Gen-API, AITunnel) + пилот-скрипт, который на 20–30 реальных товарах прогоняет матрицу «модель × режим A/B × уровни текста», меряет отказы/качество/цену и выдаёт HTML-отчёт для выбора primary-модели.

**Architecture:** Расширяем существующий `services/image_generation_service.py` (паттерн: `ImageGenerator` с tuple-контрактом `(success, bytes, error)`, submit+poll HTTP, конфиг-dataclass). Пилот — one-off скрипт по образцу `scripts/download_all_photos.py`. GPU-ветка — автономный скрипт для арендованного H100 (без Flask) + runbook. Спека: `docs/superpowers/specs/2026-07-14-infographics-design.md`.

**Tech Stack:** Python 3.11, Flask app-context (только в скрипте), requests, unittest/pytest с `unittest.mock`, Playwright (уже в зависимостях, см. `services/infographic_renderer.py`), diffusers/torch (только на арендованном GPU, в repo — не ставится).

## Global Constraints

- Тесты не делают реальных WB/LLM/HTTP вызовов (AGENTS.md) — все `requests` мокаются.
- Тесты и скрипты запускаются с `SKIP_SCHEDULER=1`.
- Секреты только из env (`GEN_API_KEY`, `AITUNNEL_API_KEY`); в код/логи/доки не вставлять.
- Tenant scope: выборка товаров всегда `id + seller_id` (AGENTS.md).
- Внешние HTTP: timeout обязателен, ошибки санитайзятся (`response.text[:300]`).
- Кодировка UTF-8; имена кода английские, доменные комментарии можно по-русски.
- Пилот не пишет в `Product`/`ImportedProduct` и не трогает цены/остатки — только чтение и файлы в `data/infographic_pilot/`.
- Перед завершением любого Python-изменения: `python -m py_compile <file>` и `git diff --check`.
- Проверка тестов: `SKIP_SCHEDULER=1 python -m pytest -q <файл>`.
- Phase 1 (миграции, AgentTask-пайплайн, страница `/infographics`, WB upload, skills) — ОТДЕЛЬНЫЙ план после результатов пилота. В этом плане его НЕ реализовывать.

---

### Task 1: NSFW-классификатор, enum/config новых провайдеров, `from_env`, базовый `edit`

**Files:**
- Modify: `services/image_generation_service.py` (enum `ImageProvider` ~строка 55, `PROVIDER_CONFIG` ~67, dataclass `ImageGenerationConfig` ~138, ABC `ImageGenerator` ~271)
- Test: `tests/test_image_edit_providers.py` (новый)

**Interfaces:**
- Produces: `ImageProvider.GEN_API`, `ImageProvider.AITUNNEL`; поля конфига `gen_api_key`, `gen_api_model`, `gen_api_edit_model`, `aitunnel_api_key`, `aitunnel_model`, `aitunnel_edit_model`; `ImageGenerationConfig.from_env(provider) -> Optional[ImageGenerationConfig]`; `is_censorship_refusal(msg: Optional[str]) -> bool`; `ImageGenerator.edit(prompt, source_image_url=None, source_image_bytes=None, width=900, height=1200) -> Tuple[bool, Optional[bytes], str]` (дефолт: не поддерживается).

- [ ] **Step 1: Write the failing test**

Создай `tests/test_image_edit_providers.py`:

```python
# -*- coding: utf-8 -*-
import os
import unittest
from unittest import mock

os.environ.setdefault("SKIP_SCHEDULER", "1")

from services.image_generation_service import (
    ImageGenerationConfig,
    ImageGenerator,
    ImageProvider,
    is_censorship_refusal,
)


class CensorshipClassifierTests(unittest.TestCase):
    def test_nsfw_marker_detected(self):
        self.assertTrue(is_censorship_refusal("Request flagged by content policy"))
        self.assertTrue(is_censorship_refusal("NSFW content detected"))

    def test_technical_error_not_nsfw(self):
        self.assertFalse(is_censorship_refusal("HTTP 502 Bad Gateway"))

    def test_none_and_empty_are_not_nsfw(self):
        self.assertFalse(is_censorship_refusal(None))
        self.assertFalse(is_censorship_refusal(""))


class FromEnvTests(unittest.TestCase):
    def test_gen_api_key_from_env(self):
        with mock.patch.dict(os.environ, {"GEN_API_KEY": "k1"}):
            cfg = ImageGenerationConfig.from_env(ImageProvider.GEN_API)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.provider, ImageProvider.GEN_API)
        self.assertEqual(cfg.gen_api_key, "k1")

    def test_aitunnel_key_from_env(self):
        with mock.patch.dict(os.environ, {"AITUNNEL_API_KEY": "k2"}):
            cfg = ImageGenerationConfig.from_env(ImageProvider.AITUNNEL)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.aitunnel_api_key, "k2")

    def test_missing_key_returns_none(self):
        env = {k: v for k, v in os.environ.items() if k != "GEN_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(ImageGenerationConfig.from_env(ImageProvider.GEN_API))


class BaseEditContractTests(unittest.TestCase):
    def test_default_edit_reports_unsupported(self):
        class Dummy(ImageGenerator):
            def generate(self, prompt, width=1440, height=810, reference_image_url=None):
                return True, b"x", ""

        ok, data, err = Dummy().edit(prompt="p", source_image_url="http://x/1.png")
        self.assertFalse(ok)
        self.assertIsNone(data)
        self.assertIn("не поддерживает", err)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_image_edit_providers.py`
Expected: FAIL — `ImportError: cannot import name 'is_censorship_refusal'`.

- [ ] **Step 3: Write minimal implementation**

В `services/image_generation_service.py`.

3a. Убедись, что в шапке модуля есть `import os` и `import base64` (сейчас там `time`, `requests` и пр. — если нет, добавь к остальным import).

3b. В enum `ImageProvider` (после `SDXL = "sdxl"`):

```python
    GEN_API = "gen_api"  # Gen-API.ru — российский агрегатор, оплата в рублях
    AITUNNEL = "aitunnel"  # AITunnel.ru — OpenAI-совместимый агрегатор, рубли
```

3c. В `PROVIDER_CONFIG` (перед закрывающей скобкой словаря):

```python
    ImageProvider.GEN_API: {
        "name": "Gen-API",
        "description": "Российский агрегатор (FLUX.2, Kontext Pro, Seedream, Nano Banana), рубли",
        "api_url": "https://api.gen-api.ru/api/v1",
        "price_per_image": "3.3-10 ₽",
        "max_size": "2048x2048",
        "supports_reference": True,
        "recommended": True
    },
    ImageProvider.AITUNNEL: {
        "name": "AITunnel",
        "description": "OpenAI-совместимый российский агрегатор (Seedream, GPT Image), рубли",
        "api_url": "https://api.aitunnel.ru/v1",
        "price_per_image": "1.5-7 ₽",
        "max_size": "2048x2048",
        "supports_reference": True,
        "recommended": True
    },
```

3d. После `PROVIDER_CONFIG` — классификатор отказов:

```python
# Маркеры цензурного отказа провайдера (а не технического сбоя).
# Используется пилотом и fallback-цепочкой Phase 1 для честной метрики отказов.
NSFW_ERROR_MARKERS = (
    "nsfw",
    "safety",
    "content policy",
    "content_policy",
    "moderation",
    "flagged",
    "censor",
    "policy violation",
    "недопустимый контент",
)


def is_censorship_refusal(error_message):
    """True, если ошибка похожа на отказ цензуры/модерации провайдера."""
    msg = (error_message or "").lower()
    return any(marker in msg for marker in NSFW_ERROR_MARKERS)
```

3e. В dataclass `ImageGenerationConfig` после блока `# TensorArt specific`:

```python
    # Gen-API specific (https://gen-api.ru — slugs сетей уточняются в Task 2)
    gen_api_key: str = ""
    gen_api_model: str = "flux-2"          # t2i-сеть (режим B)
    gen_api_edit_model: str = "nano-banana"  # i2i-сеть (режим A)
    # AITunnel specific (https://aitunnel.ru — OpenAI images API)
    aitunnel_api_key: str = ""
    aitunnel_model: str = "gpt-image-2"
    aitunnel_edit_model: str = "seedream-4.5"
```

3f. После `from_settings` — метод `from_env` (для one-off скриптов, ключи не из БД):

```python
    @classmethod
    def from_env(cls, provider):
        """Конфиг из переменных окружения — для one-off скриптов (пилот).

        GEN_API_KEY / AITUNNEL_API_KEY. Возвращает None, если ключа нет.
        """
        if provider == ImageProvider.GEN_API:
            key = os.environ.get('GEN_API_KEY', '')
            if not key:
                return None
            return cls(provider=provider, api_key=key, gen_api_key=key)
        if provider == ImageProvider.AITUNNEL:
            key = os.environ.get('AITUNNEL_API_KEY', '')
            if not key:
                return None
            return cls(provider=provider, api_key=key, aitunnel_api_key=key)
        return None
```

3g. В ABC `ImageGenerator` после абстрактного `generate` — неабстрактный дефолт `edit` (существующие генераторы не ломаются):

```python
    def edit(
        self,
        prompt: str,
        source_image_url: Optional[str] = None,
        source_image_bytes: Optional[bytes] = None,
        width: int = 900,
        height: int = 1200,
    ) -> Tuple[bool, Optional[bytes], str]:
        """Image-to-image edit. Провайдеры без i2i возвращают ошибку."""
        return False, None, (
            f"Провайдер {self.__class__.__name__} не поддерживает image-to-image"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_image_edit_providers.py`
Expected: PASS (7 tests). Затем `python -m py_compile services/image_generation_service.py`.

- [ ] **Step 5: Commit**

```bash
git add services/image_generation_service.py tests/test_image_edit_providers.py
git commit -m "feat(image-gen): провайдеры Gen-API/AITunnel в enum, from_env, NSFW-классификатор, базовый edit-контракт"
```

---

### Task 2: Адаптер Gen-API (generate + edit, submit+poll)

**Files:**
- Modify: `services/image_generation_service.py` (новый класс после `ReplicateImageGenerator`, ~строка 1155)
- Test: `tests/test_image_edit_providers.py` (дополнить)

**Interfaces:**
- Consumes: `ImageGenerationConfig` (поля `gen_api_key`, `gen_api_model`, `gen_api_edit_model`, `timeout`), `is_censorship_refusal`.
- Produces: `class GenApiImageGenerator(ImageGenerator)` с `generate(...)` и `edit(...)` (контракты Task 1); внутренний `_submit_and_poll(network: str, payload: dict)`.

- [ ] **Step 1: Верификация контракта по докам**

Открой https://gen-api.ru/docs (и страницы сетей flux-2 / nano-banana / seedream / flux-kontext) через WebFetch/браузер. Сверь: путь создания задачи (`POST /api/v1/networks/<slug>`), путь поллинга (`GET /api/v1/request/get/<id>`), имена полей результата (`status`, `result`/`output`), способ передачи исходного изображения для i2i (`image_urls` массив или base64) и точные slugs сетей. Если фактический контракт отличается — поправь константы/ключи payload в коде Step 3 и ожидания в тестах Step 2 (это конфигурационная правка, структура кода не меняется). Зафиксируй проверенные slugs в докстринге класса.

- [ ] **Step 2: Write the failing test**

Добавь в `tests/test_image_edit_providers.py`:

```python
def _resp(status_code=200, json_data=None, content=b""):
    r = mock.Mock()
    r.status_code = status_code
    r.json.return_value = json_data or {}
    r.content = content
    r.text = str(json_data or "")
    return r


class GenApiGeneratorTests(unittest.TestCase):
    def _config(self):
        return ImageGenerationConfig(
            provider=ImageProvider.GEN_API,
            api_key="k",
            gen_api_key="k",
            timeout=10,
        )

    @mock.patch("services.image_generation_service.time.sleep", lambda *_: None)
    @mock.patch("services.image_generation_service.requests.get")
    @mock.patch("services.image_generation_service.requests.post")
    def test_edit_success_via_poll(self, m_post, m_get):
        from services.image_generation_service import GenApiImageGenerator

        m_post.return_value = _resp(json_data={"request_id": 42})
        m_get.side_effect = [
            _resp(json_data={"status": "processing"}),
            _resp(json_data={"status": "success", "result": ["http://cdn/img.png"]}),
            _resp(content=b"PNGDATA"),
        ]
        gen = GenApiImageGenerator(self._config())
        ok, data, err = gen.edit(prompt="scene", source_image_url="http://p/1.jpg")
        self.assertTrue(ok, err)
        self.assertEqual(data, b"PNGDATA")
        # edit идёт в i2i-сеть из конфига
        self.assertIn("nano-banana", m_post.call_args[0][0])
        # исходное фото передано в payload
        self.assertEqual(
            m_post.call_args[1]["json"]["image_urls"], ["http://p/1.jpg"]
        )

    @mock.patch("services.image_generation_service.time.sleep", lambda *_: None)
    @mock.patch("services.image_generation_service.requests.get")
    @mock.patch("services.image_generation_service.requests.post")
    def test_nsfw_failure_is_marked(self, m_post, m_get):
        from services.image_generation_service import GenApiImageGenerator

        m_post.return_value = _resp(json_data={"request_id": 43})
        m_get.return_value = _resp(
            json_data={"status": "failed", "error": "blocked by content policy"}
        )
        gen = GenApiImageGenerator(self._config())
        ok, data, err = gen.edit(prompt="scene", source_image_url="http://p/1.jpg")
        self.assertFalse(ok)
        self.assertTrue(err.startswith("NSFW:"), err)

    @mock.patch("services.image_generation_service.requests.post")
    def test_generate_uses_t2i_network(self, m_post):
        from services.image_generation_service import GenApiImageGenerator

        m_post.return_value = _resp(status_code=500, json_data={"error": "boom"})
        gen = GenApiImageGenerator(self._config())
        ok, _, err = gen.generate(prompt="bg", width=900, height=1200)
        self.assertFalse(ok)
        self.assertIn("flux-2", m_post.call_args[0][0])

    def test_edit_without_source_rejected(self):
        from services.image_generation_service import GenApiImageGenerator

        ok, data, err = GenApiImageGenerator(self._config()).edit(prompt="x")
        self.assertFalse(ok)
        self.assertIn("исходное", err)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_image_edit_providers.py`
Expected: FAIL — `ImportError: ... GenApiImageGenerator`.

- [ ] **Step 4: Write implementation**

В `services/image_generation_service.py` после класса `ReplicateImageGenerator`:

```python
class GenApiImageGenerator(ImageGenerator):
    """Gen-API.ru — российский агрегатор генеративных сетей (рубли).

    Контракт (сверен с https://gen-api.ru/docs в Task 2 Step 1):
    - POST {BASE_URL}/networks/<slug>  -> {"request_id": int}
    - GET  {BASE_URL}/request/get/<request_id> -> {"status": "processing|success|failed", ...}
    Slugs: t2i — config.gen_api_model, i2i — config.gen_api_edit_model.
    """

    BASE_URL = "https://api.gen-api.ru/api/v1"

    def __init__(self, config: ImageGenerationConfig):
        self.config = config

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.config.gen_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _submit_and_poll(self, network: str, payload: dict) -> Tuple[bool, Optional[bytes], str]:
        try:
            response = requests.post(
                f"{self.BASE_URL}/networks/{network}",
                json=payload,
                headers=self._headers(),
                timeout=30,
            )
            if response.status_code != 200:
                return False, None, f"Gen-API {network}: HTTP {response.status_code} {response.text[:300]}"
            request_id = (response.json() or {}).get("request_id")
            if not request_id:
                return False, None, f"Gen-API {network}: не получен request_id"

            waited, poll_interval = 0, 3
            while waited < self.config.timeout:
                time.sleep(poll_interval)
                waited += poll_interval
                poll = requests.get(
                    f"{self.BASE_URL}/request/get/{request_id}",
                    headers=self._headers(),
                    timeout=30,
                )
                if poll.status_code != 200:
                    continue
                data = poll.json() or {}
                status = data.get("status")
                if status == "success":
                    urls = data.get("result") or data.get("output") or []
                    if isinstance(urls, str):
                        urls = [urls]
                    if urls:
                        img = requests.get(urls[0], timeout=60)
                        if img.status_code == 200:
                            return True, img.content, ""
                    return False, None, f"Gen-API {network}: пустой результат"
                if status in ("failed", "error"):
                    err = str(data.get("error") or data.get("message") or "unknown")
                    if is_censorship_refusal(err):
                        return False, None, f"NSFW: {err[:200]}"
                    return False, None, f"Gen-API {network}: {err[:300]}"
            return False, None, f"Gen-API {network}: таймаут ({self.config.timeout}с)"
        except requests.exceptions.Timeout:
            return False, None, f"Gen-API {network}: таймаут запроса"
        except Exception as e:
            logger.error(f"Gen-API {network} ошибка: {e}")
            return False, None, f"Gen-API {network}: {e}"

    def generate(
        self,
        prompt: str,
        width: int = 900,
        height: int = 1200,
        reference_image_url: Optional[str] = None,
    ) -> Tuple[bool, Optional[bytes], str]:
        payload = {"prompt": prompt, "width": width, "height": height}
        if reference_image_url:
            payload["image_urls"] = [reference_image_url]
        return self._submit_and_poll(self.config.gen_api_model, payload)

    def edit(
        self,
        prompt: str,
        source_image_url: Optional[str] = None,
        source_image_bytes: Optional[bytes] = None,
        width: int = 900,
        height: int = 1200,
    ) -> Tuple[bool, Optional[bytes], str]:
        if not source_image_url and not source_image_bytes:
            return False, None, "Gen-API edit: не передано исходное изображение"
        payload = {"prompt": prompt}
        if source_image_url:
            payload["image_urls"] = [source_image_url]
        else:
            payload["image_base64"] = base64.b64encode(source_image_bytes).decode("ascii")
        return self._submit_and_poll(self.config.gen_api_edit_model, payload)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_image_edit_providers.py`
Expected: PASS. Затем `python -m py_compile services/image_generation_service.py`.

- [ ] **Step 6: Commit**

```bash
git add services/image_generation_service.py tests/test_image_edit_providers.py
git commit -m "feat(image-gen): адаптер Gen-API — t2i и i2i edit через submit+poll, NSFW-маркировка отказов"
```

---

### Task 3: Адаптер AITunnel (OpenAI-совместимый: generations + edits)

**Files:**
- Modify: `services/image_generation_service.py` (класс после `GenApiImageGenerator`)
- Test: `tests/test_image_edit_providers.py` (дополнить)

**Interfaces:**
- Consumes: конфиг-поля `aitunnel_api_key`, `aitunnel_model`, `aitunnel_edit_model`, `timeout`; `is_censorship_refusal`.
- Produces: `class AITunnelImageGenerator(ImageGenerator)` с `generate(...)`/`edit(...)` (контракты Task 1).

- [ ] **Step 1: Верификация контракта**

Открой https://docs.aitunnel.ru (раздел Images). Сверь: base URL (`https://api.aitunnel.ru/v1`), формат `POST /images/generations` (JSON: model, prompt, n, size, response_format) и `POST /images/edits` (multipart: image, model, prompt), точные slugs моделей (`seedream-4.5`? `gpt-image-2`?). При расхождении поправь константы payload и тесты — структура кода не меняется. Зафиксируй в докстринге.

- [ ] **Step 2: Write the failing test**

Добавь в `tests/test_image_edit_providers.py`:

```python
class AITunnelGeneratorTests(unittest.TestCase):
    def _config(self):
        return ImageGenerationConfig(
            provider=ImageProvider.AITUNNEL,
            api_key="k",
            aitunnel_api_key="k",
            timeout=10,
        )

    @mock.patch("services.image_generation_service.requests.post")
    def test_generate_b64_success(self, m_post):
        import base64 as b64
        from services.image_generation_service import AITunnelImageGenerator

        m_post.return_value = _resp(
            json_data={"data": [{"b64_json": b64.b64encode(b"IMG").decode()}]}
        )
        ok, data, err = AITunnelImageGenerator(self._config()).generate(
            prompt="bg", width=900, height=1200
        )
        self.assertTrue(ok, err)
        self.assertEqual(data, b"IMG")
        self.assertIn("/images/generations", m_post.call_args[0][0])
        self.assertEqual(m_post.call_args[1]["json"]["model"], "gpt-image-2")

    @mock.patch("services.image_generation_service.requests.post")
    def test_edit_multipart_with_bytes(self, m_post):
        import base64 as b64
        from services.image_generation_service import AITunnelImageGenerator

        m_post.return_value = _resp(
            json_data={"data": [{"b64_json": b64.b64encode(b"OUT").decode()}]}
        )
        ok, data, err = AITunnelImageGenerator(self._config()).edit(
            prompt="scene", source_image_bytes=b"SRC"
        )
        self.assertTrue(ok, err)
        self.assertEqual(data, b"OUT")
        self.assertIn("/images/edits", m_post.call_args[0][0])
        self.assertEqual(m_post.call_args[1]["data"]["model"], "seedream-4.5")
        self.assertIn("image", m_post.call_args[1]["files"])

    @mock.patch("services.image_generation_service.requests.post")
    def test_moderation_refusal_marked_nsfw(self, m_post):
        from services.image_generation_service import AITunnelImageGenerator

        m_post.return_value = _resp(
            status_code=400,
            json_data={"error": {"message": "rejected by moderation"}},
        )
        ok, _, err = AITunnelImageGenerator(self._config()).edit(
            prompt="scene", source_image_bytes=b"SRC"
        )
        self.assertFalse(ok)
        self.assertTrue(err.startswith("NSFW:"), err)

    @mock.patch("services.image_generation_service.requests.get")
    @mock.patch("services.image_generation_service.requests.post")
    def test_edit_downloads_source_url_when_no_bytes(self, m_post, m_get):
        import base64 as b64
        from services.image_generation_service import AITunnelImageGenerator

        m_get.return_value = _resp(content=b"SRCIMG")
        m_post.return_value = _resp(
            json_data={"data": [{"b64_json": b64.b64encode(b"OUT").decode()}]}
        )
        ok, data, err = AITunnelImageGenerator(self._config()).edit(
            prompt="scene", source_image_url="http://p/1.jpg"
        )
        self.assertTrue(ok, err)
        m_get.assert_called_once()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_image_edit_providers.py`
Expected: FAIL — `ImportError: ... AITunnelImageGenerator`.

- [ ] **Step 4: Write implementation**

После `GenApiImageGenerator`:

```python
class AITunnelImageGenerator(ImageGenerator):
    """AITunnel — OpenAI-совместимый российский агрегатор (рубли).

    Контракт (сверен с https://docs.aitunnel.ru в Task 3 Step 1):
    - POST {BASE_URL}/images/generations — JSON (model, prompt, n, size, response_format)
    - POST {BASE_URL}/images/edits — multipart (image, model, prompt, ...)
    """

    BASE_URL = "https://api.aitunnel.ru/v1"

    def __init__(self, config: ImageGenerationConfig):
        self.config = config

    def _auth(self):
        return {"Authorization": f"Bearer {self.config.aitunnel_api_key}"}

    def _fail(self, response) -> Tuple[bool, Optional[bytes], str]:
        err = response.text[:300]
        if is_censorship_refusal(err):
            return False, None, f"NSFW: {err[:200]}"
        return False, None, f"AITunnel: HTTP {response.status_code} {err}"

    def _extract_image(self, data: dict) -> Tuple[bool, Optional[bytes], str]:
        items = data.get("data") or []
        if not items:
            return False, None, "AITunnel: пустой ответ"
        first = items[0] or {}
        if first.get("b64_json"):
            try:
                return True, base64.b64decode(first["b64_json"]), ""
            except Exception:
                return False, None, "AITunnel: битый base64 в ответе"
        if first.get("url"):
            img = requests.get(first["url"], timeout=60)
            if img.status_code == 200:
                return True, img.content, ""
        return False, None, "AITunnel: нет изображения в ответе"

    def generate(
        self,
        prompt: str,
        width: int = 900,
        height: int = 1200,
        reference_image_url: Optional[str] = None,
    ) -> Tuple[bool, Optional[bytes], str]:
        payload = {
            "model": self.config.aitunnel_model,
            "prompt": prompt,
            "n": 1,
            "size": f"{width}x{height}",
            "response_format": "b64_json",
        }
        try:
            response = requests.post(
                f"{self.BASE_URL}/images/generations",
                json=payload,
                headers={**self._auth(), "Content-Type": "application/json"},
                timeout=self.config.timeout,
            )
            if response.status_code != 200:
                return self._fail(response)
            return self._extract_image(response.json() or {})
        except requests.exceptions.Timeout:
            return False, None, f"AITunnel: таймаут ({self.config.timeout}с)"
        except Exception as e:
            logger.error(f"AITunnel ошибка: {e}")
            return False, None, f"AITunnel: {e}"

    def edit(
        self,
        prompt: str,
        source_image_url: Optional[str] = None,
        source_image_bytes: Optional[bytes] = None,
        width: int = 900,
        height: int = 1200,
    ) -> Tuple[bool, Optional[bytes], str]:
        if source_image_bytes is None:
            if not source_image_url:
                return False, None, "AITunnel edit: не передано исходное изображение"
            try:
                src = requests.get(source_image_url, timeout=60)
                if src.status_code != 200:
                    return False, None, f"AITunnel edit: исходное фото HTTP {src.status_code}"
                source_image_bytes = src.content
            except Exception as e:
                return False, None, f"AITunnel edit: не скачалось исходное фото: {e}"
        try:
            response = requests.post(
                f"{self.BASE_URL}/images/edits",
                files={"image": ("source.png", source_image_bytes, "image/png")},
                data={
                    "model": self.config.aitunnel_edit_model,
                    "prompt": prompt,
                    "n": "1",
                    "size": f"{width}x{height}",
                    "response_format": "b64_json",
                },
                headers=self._auth(),
                timeout=self.config.timeout,
            )
            if response.status_code != 200:
                return self._fail(response)
            return self._extract_image(response.json() or {})
        except requests.exceptions.Timeout:
            return False, None, f"AITunnel: таймаут ({self.config.timeout}с)"
        except Exception as e:
            logger.error(f"AITunnel edit ошибка: {e}")
            return False, None, f"AITunnel: {e}"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_image_edit_providers.py`
Expected: PASS. `python -m py_compile services/image_generation_service.py`.

- [ ] **Step 6: Commit**

```bash
git add services/image_generation_service.py tests/test_image_edit_providers.py
git commit -m "feat(image-gen): адаптер AITunnel — OpenAI-совместимые generations/edits, multipart, NSFW-маркировка"
```

---

### Task 4: Фасад `edit_image` + маршрутизация фабрики

**Files:**
- Modify: `services/image_generation_service.py` (`ImageGenerationService.__init__` ~строка 1172, новый метод после `generate_from_prompt` ~1212)
- Test: `tests/test_image_edit_providers.py` (дополнить)

**Interfaces:**
- Consumes: `GenApiImageGenerator`, `AITunnelImageGenerator` (Tasks 2–3).
- Produces: `ImageGenerationService.edit_image(prompt, source_image_url=None, source_image_bytes=None, width=None, height=None) -> Tuple[bool, Optional[bytes], str]` — единственная точка входа для пилота и Phase 1.

- [ ] **Step 1: Write the failing test**

```python
class ServiceFacadeTests(unittest.TestCase):
    def test_factory_routes_new_providers(self):
        from services.image_generation_service import (
            AITunnelImageGenerator,
            GenApiImageGenerator,
            ImageGenerationService,
        )

        svc = ImageGenerationService(ImageGenerationConfig(
            provider=ImageProvider.GEN_API, api_key="k", gen_api_key="k"))
        self.assertIsInstance(svc.generator, GenApiImageGenerator)

        svc = ImageGenerationService(ImageGenerationConfig(
            provider=ImageProvider.AITUNNEL, api_key="k", aitunnel_api_key="k"))
        self.assertIsInstance(svc.generator, AITunnelImageGenerator)

    def test_edit_image_delegates_with_default_size(self):
        from services.image_generation_service import ImageGenerationService

        svc = ImageGenerationService(ImageGenerationConfig(
            provider=ImageProvider.GEN_API, api_key="k", gen_api_key="k"))
        svc.generator = mock.Mock()
        svc.generator.edit.return_value = (True, b"X", "")
        ok, data, err = svc.edit_image(prompt="p", source_image_url="http://s/1.png")
        self.assertTrue(ok)
        kwargs = svc.generator.edit.call_args[1]
        self.assertEqual(kwargs["width"], 900)   # default_width из конфига
        self.assertEqual(kwargs["height"], 1200)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_image_edit_providers.py`
Expected: FAIL — фабрика роутит GEN_API в `ReplicateImageGenerator` (else-ветка), у сервиса нет `edit_image`.

- [ ] **Step 3: Write implementation**

В `ImageGenerationService.__init__` добавь ветки перед финальным `else`:

```python
        elif config.provider == ImageProvider.GEN_API:
            self.generator = GenApiImageGenerator(config)
        elif config.provider == ImageProvider.AITUNNEL:
            self.generator = AITunnelImageGenerator(config)
```

После `generate_from_prompt` добавь:

```python
    def edit_image(
        self,
        prompt: str,
        source_image_url: Optional[str] = None,
        source_image_bytes: Optional[bytes] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> Tuple[bool, Optional[bytes], str]:
        """Image-to-image: сцена вокруг товара по исходному фото (режим A)."""
        return self.generator.edit(
            prompt=prompt,
            source_image_url=source_image_url,
            source_image_bytes=source_image_bytes,
            width=width or self.config.default_width,
            height=height or self.config.default_height,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_image_edit_providers.py`
Expected: PASS (все тесты файла). `python -m py_compile services/image_generation_service.py`.

- [ ] **Step 5: Commit**

```bash
git add services/image_generation_service.py tests/test_image_edit_providers.py
git commit -m "feat(image-gen): фасад edit_image и маршрутизация Gen-API/AITunnel в фабрике"
```

---

### Task 5: Промпт-пресеты атмосфер и санитайзер (`services/infographic_prompts.py`)

**Files:**
- Create: `services/infographic_prompts.py`
- Test: `tests/test_infographic_prompts.py` (новый)

**Interfaces:**
- Produces: `ATMOSPHERE_PRESETS: dict` (ключи `boudoir|neon|luxury|spa`), `build_edit_prompt(preset_key) -> str` (режим A), `build_background_prompt(preset_key) -> str` (режим B), `sanitize_prompt(text) -> str`, `TEXT_SAMPLE_PHRASES: list[str]` (15 русских фраз), `SHORT_TEXT_SAMPLES: list[str]`. Используются пилотом (Tasks 6–8) и Phase 1.

- [ ] **Step 1: Write the failing test**

Создай `tests/test_infographic_prompts.py`:

```python
# -*- coding: utf-8 -*-
import os
import unittest

os.environ.setdefault("SKIP_SCHEDULER", "1")

from services.infographic_prompts import (
    ATMOSPHERE_PRESETS,
    SHORT_TEXT_SAMPLES,
    TEXT_SAMPLE_PHRASES,
    build_background_prompt,
    build_edit_prompt,
    sanitize_prompt,
)


class PresetTests(unittest.TestCase):
    def test_all_presets_present(self):
        self.assertEqual(
            set(ATMOSPHERE_PRESETS.keys()), {"boudoir", "neon", "luxury", "spa"}
        )

    def test_edit_prompt_keeps_product_and_bans_text(self):
        p = build_edit_prompt("boudoir")
        self.assertIn("Keep the product", p)
        self.assertIn("no text", p.lower())
        self.assertIn("no people", p.lower())

    def test_background_prompt_has_no_product_words(self):
        p = build_background_prompt("neon")
        self.assertIn("empty", p.lower())
        self.assertIn("no people", p.lower())

    def test_unknown_preset_raises(self):
        with self.assertRaises(KeyError):
            build_edit_prompt("disco")


class SanitizerTests(unittest.TestCase):
    def test_risky_terms_replaced(self):
        out = sanitize_prompt("вибратор для взрослых, эротический стиль")
        low = out.lower()
        self.assertNotIn("вибратор", low)
        self.assertNotIn("эрот", low)

    def test_neutral_text_unchanged(self):
        self.assertEqual(sanitize_prompt("гель на водной основе"),
                         "гель на водной основе")


class TextSampleTests(unittest.TestCase):
    def test_phrase_pool_sizes(self):
        self.assertGreaterEqual(len(TEXT_SAMPLE_PHRASES), 12)
        self.assertGreaterEqual(len(SHORT_TEXT_SAMPLES), 4)
        # уровни текста 2–3: длинные фразы и короткие слова разделены
        self.assertTrue(all(len(s) <= 12 for s in SHORT_TEXT_SAMPLES))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_infographic_prompts.py`
Expected: FAIL — `ModuleNotFoundError: services.infographic_prompts`.

- [ ] **Step 3: Write implementation**

Создай `services/infographic_prompts.py`:

```python
# -*- coding: utf-8 -*-
"""Промпт-пресеты «Фотостудии»: атмосферы 18+-безопасных сцен и санитайзер.

Спека: docs/superpowers/specs/2026-07-14-infographics-design.md (§3).
Принципы: эмоции через свет/фактуры, нейтральная лексика, промпты на английском,
запрет текста/людей в кадре (текст кладёт Playwright-рендерер).
"""

import re

# Сцены атмосфер. mode A (edit) — сцена вокруг товара; mode B — пустой фон.
ATMOSPHERE_PRESETS = {
    "boudoir": {
        "label": "Будуар",
        "scene": (
            "an elegant boudoir scene: dark silk fabric, warm candlelight, "
            "soft shadows, intimate cozy mood"
        ),
    },
    "neon": {
        "label": "Неон",
        "scene": (
            "a moody scene lit by magenta and violet neon glow, dark "
            "background, subtle reflections on a glossy surface"
        ),
    },
    "luxury": {
        "label": "Люкс",
        "scene": (
            "a luxury still-life scene: black marble surface, gold accents, "
            "dramatic single spotlight, premium editorial look"
        ),
    },
    "spa": {
        "label": "Спа",
        "scene": (
            "a serene spa scene: light stone, eucalyptus leaves, soft "
            "daylight, gentle steam, clean minimal composition"
        ),
    },
}

_EDIT_TEMPLATE = (
    "Keep the product from the source photo exactly as it is: same shape, "
    "colors, materials, packaging and printed label. Replace the background "
    "with {scene}. Professional commercial product photography, cinematic "
    "soft lighting, vertical 3:4 composition, sharp focus on the product. "
    "No people, no text, no watermarks, no logos."
)

_BACKGROUND_TEMPLATE = (
    "Empty product photography backdrop: {scene}. A clear empty area in the "
    "center of the surface for product placement, nothing in the middle. "
    "Professional commercial photography, cinematic soft lighting, vertical "
    "3:4 composition. No objects in focus, no people, no text, no watermarks."
)

# Рискованная лексика -> нейтральная (только для текста, попадающего в промпт;
# fail-open: незнакомые слова не трогаем, товар в промпте не описываем без нужды).
_SANITIZE_REPLACEMENTS = (
    (re.compile(r"вибратор\w*", re.IGNORECASE), "аксессуар"),
    (re.compile(r"эрот\w+", re.IGNORECASE), "элегантный"),
    (re.compile(r"секс[\w-]*", re.IGNORECASE), "для двоих"),
    (re.compile(r"интим\w*", re.IGNORECASE), "личный"),
    (re.compile(r"фаллоимитатор\w*", re.IGNORECASE), "аксессуар"),
    (re.compile(r"страпон\w*", re.IGNORECASE), "аксессуар"),
    (re.compile(r"бдсм", re.IGNORECASE), "смелый стиль"),
    (re.compile(r"анальн\w+", re.IGNORECASE), "компактный"),
    (re.compile(r"мастурбатор\w*", re.IGNORECASE), "аксессуар"),
)

# Уровень 2 (стилизация): реалистичные фразы преимуществ для замера глифов.
TEXT_SAMPLE_PHRASES = [
    "Гипоаллергенный медицинский силикон",
    "Бесшумный мотор до 40 дБ",
    "10 режимов вибрации",
    "Зарядка через USB Type-C",
    "Водонепроницаемый корпус IPX7",
    "Мягкое покрытие софт-тач",
    "До 2 часов работы без подзарядки",
    "Анатомическая форма",
    "Премиальная подарочная упаковка",
    "Гарантия 12 месяцев",
    "Сделано из безопасных материалов",
    "Полностью водостойкий",
    "Идея подарка для пары",
    "Компактный дорожный формат",
    "Лёгкий уход и очистка",
]

# Уровень 3 (свободная генерация): только короткие слова/цифры.
SHORT_TEXT_SAMPLES = ["−30%", "ХИТ", "NEW", "ТОП", "18+"]


def build_edit_prompt(preset_key: str) -> str:
    """Промпт режима A: сцена вокруг товара с исходного фото."""
    return _EDIT_TEMPLATE.format(scene=ATMOSPHERE_PRESETS[preset_key]["scene"])


def build_background_prompt(preset_key: str) -> str:
    """Промпт режима B: пустой атмосферный фон без товара."""
    return _BACKGROUND_TEMPLATE.format(scene=ATMOSPHERE_PRESETS[preset_key]["scene"])


def sanitize_prompt(text: str) -> str:
    """Заменяет рискованную лексику на нейтральную перед вставкой в промпт."""
    out = text or ""
    for pattern, replacement in _SANITIZE_REPLACEMENTS:
        out = pattern.sub(replacement, out)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_infographic_prompts.py`
Expected: PASS. `python -m py_compile services/infographic_prompts.py`.

- [ ] **Step 5: Commit**

```bash
git add services/infographic_prompts.py tests/test_infographic_prompts.py
git commit -m "feat(infographics): промпт-пресеты атмосфер (18+-safe), санитайзер, пул фраз для теста глифов"
```

---

### Task 6: Пилот-скрипт — выбор товаров, варианты, бюджет, dry-run

**Files:**
- Create: `scripts/infographic_pilot.py`
- Test: `tests/test_infographic_pilot.py` (новый)

**Interfaces:**
- Consumes: `ImageGenerationConfig.from_env`, `ImageGenerationService` (Tasks 1–4), `services.infographic_prompts` (Task 5), модели `ImportedProduct` (только чтение).
- Produces: CLI-скрипт; чистые функции `parse_variants(spec: str) -> list[Variant]`, `estimate_cost_rub(variants, n_products) -> float`, `first_photo_url(photo_urls_json: str) -> Optional[str]`; `Variant = dataclass(provider: str, model: str, mode: str)`; константа `PRICE_TABLE_RUB: dict[str, float]`; выходной формат `results.jsonl` (одна строка на генерацию: `{"product_id", "variant", "mode", "status": "ok|nsfw|error", "latency_s", "cost_rub", "output", "error"}`).

- [ ] **Step 1: Write the failing test**

Создай `tests/test_infographic_pilot.py`:

```python
# -*- coding: utf-8 -*-
import importlib.util
import json
import os
import unittest
from pathlib import Path

os.environ.setdefault("SKIP_SCHEDULER", "1")

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "infographic_pilot.py"


def _load():
    spec = importlib.util.spec_from_file_location("infographic_pilot", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VariantParsingTests(unittest.TestCase):
    def test_parse_variants(self):
        m = _load()
        variants = m.parse_variants("gen_api:flux-2:B,aitunnel:seedream-4.5:A")
        self.assertEqual(len(variants), 2)
        self.assertEqual(variants[0].provider, "gen_api")
        self.assertEqual(variants[0].model, "flux-2")
        self.assertEqual(variants[0].mode, "B")
        self.assertEqual(variants[1].mode, "A")

    def test_invalid_mode_rejected(self):
        m = _load()
        with self.assertRaises(ValueError):
            m.parse_variants("gen_api:flux-2:X")

    def test_unknown_provider_rejected(self):
        m = _load()
        with self.assertRaises(ValueError):
            m.parse_variants("magic:flux-2:A")


class CostEstimateTests(unittest.TestCase):
    def test_estimate_uses_price_table(self):
        m = _load()
        variants = m.parse_variants("gen_api:flux-2:A,aitunnel:gpt-image-2:B")
        cost = m.estimate_cost_rub(variants, n_products=10)
        expected = (m.PRICE_TABLE_RUB["gen_api:flux-2"]
                    + m.PRICE_TABLE_RUB["aitunnel:gpt-image-2"]) * 10
        self.assertAlmostEqual(cost, expected)

    def test_unknown_model_uses_default_price(self):
        m = _load()
        variants = m.parse_variants("gen_api:new-model:A")
        cost = m.estimate_cost_rub(variants, n_products=2)
        self.assertAlmostEqual(cost, m.DEFAULT_PRICE_RUB * 2)


class PhotoUrlTests(unittest.TestCase):
    def test_first_photo_from_json_list(self):
        m = _load()
        url = m.first_photo_url(json.dumps(["http://a/1.jpg", "http://a/2.jpg"]))
        self.assertEqual(url, "http://a/1.jpg")

    def test_empty_or_broken_json_gives_none(self):
        m = _load()
        self.assertIsNone(m.first_photo_url(None))
        self.assertIsNone(m.first_photo_url("not-json"))
        self.assertIsNone(m.first_photo_url("[]"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_infographic_pilot.py`
Expected: FAIL — файла скрипта нет.

- [ ] **Step 3: Write implementation**

Создай `scripts/infographic_pilot.py`. Важно: тяжёлые импорты (Flask app, сервис) — только внутри `main()`/раннера, чтобы тесты грузили модуль без побочных эффектов:

```python
# -*- coding: utf-8 -*-
"""Пилот «Фотостудии» (Phase 0): матрица генераций для выбора модели и режима.

Спека: docs/superpowers/specs/2026-07-14-infographics-design.md (§3, Phase 0).

Использование:
    SKIP_SCHEDULER=1 GEN_API_KEY=... AITUNNEL_API_KEY=... \\
    python scripts/infographic_pilot.py \\
        --seller-id 1 --limit 20 --preset boudoir \\
        --variants gen_api:flux-2:B,gen_api:nano-banana:A,aitunnel:seedream-4.5:A \\
        --budget-rub 800 [--products 1,2,3] [--dry-run] \\
        [--export-gpu-bundle] [--extra-results path/to/gpu/results.jsonl]

Пишет data/infographic_pilot/run_<id>/: PNG-файлы, results.jsonl, report.html.
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("SKIP_SCHEDULER", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KNOWN_PROVIDERS = ("gen_api", "aitunnel")

# Цены за 1 генерацию, ₽ (июль 2026, Gen-API/AITunnel; спека §2.1)
PRICE_TABLE_RUB = {
    "gen_api:flux-2": 3.3,
    "gen_api:flux-kontext-pro": 8.0,
    "gen_api:seedream-4-5": 10.0,
    "gen_api:nano-banana": 9.75,
    "aitunnel:seedream-4.5": 6.8,
    "aitunnel:gpt-image-2": 1.53,
}
DEFAULT_PRICE_RUB = 10.0  # незнакомая модель — консервативная оценка


@dataclass
class Variant:
    provider: str
    model: str
    mode: str  # "A" — i2i edit вокруг товара, "B" — пустой фон (t2i)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"

    @property
    def slug(self) -> str:
        return f"{self.provider}_{self.model}_{self.mode}".replace(".", "-")


def parse_variants(spec: str):
    """'gen_api:flux-2:B,aitunnel:seedream-4.5:A' -> [Variant, ...]"""
    variants = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 3:
            raise ValueError(f"Вариант '{chunk}': ожидается provider:model:mode")
        provider, model, mode = parts[0].strip(), parts[1].strip(), parts[2].strip().upper()
        if provider not in KNOWN_PROVIDERS:
            raise ValueError(f"Неизвестный провайдер '{provider}' (можно: {KNOWN_PROVIDERS})")
        if mode not in ("A", "B"):
            raise ValueError(f"Режим '{mode}' — допустимы только A или B")
        variants.append(Variant(provider=provider, model=model, mode=mode))
    if not variants:
        raise ValueError("Пустой список вариантов")
    return variants


def estimate_cost_rub(variants, n_products: int) -> float:
    """Оценка стоимости прогона всей матрицы, ₽."""
    return sum(PRICE_TABLE_RUB.get(v.key, DEFAULT_PRICE_RUB) for v in variants) * n_products


def first_photo_url(photo_urls_json):
    """Первый URL из JSON-поля ImportedProduct.photo_urls, иначе None."""
    if not photo_urls_json:
        return None
    if isinstance(photo_urls_json, list):
        urls = photo_urls_json
    else:
        try:
            urls = json.loads(photo_urls_json)
        except (ValueError, TypeError):
            return None
    if isinstance(urls, list) and urls and isinstance(urls[0], str) and urls[0].strip():
        return urls[0].strip()
    return None


def _make_service(variant):
    """Создаёт ImageGenerationService под вариант (ключи из env)."""
    from services.image_generation_service import (
        ImageGenerationConfig,
        ImageGenerationService,
        ImageProvider,
    )

    provider = ImageProvider(variant.provider)
    config = ImageGenerationConfig.from_env(provider)
    if config is None:
        raise SystemExit(
            f"Нет API-ключа для {variant.provider}: задайте "
            f"{'GEN_API_KEY' if variant.provider == 'gen_api' else 'AITUNNEL_API_KEY'}"
        )
    if provider == ImageProvider.GEN_API:
        if variant.mode == "A":
            config.gen_api_edit_model = variant.model
        else:
            config.gen_api_model = variant.model
    else:
        if variant.mode == "A":
            config.aitunnel_edit_model = variant.model
        else:
            config.aitunnel_model = variant.model
    return ImageGenerationService(config)


def select_products(seller_id, product_ids, limit):
    """Товары с фото, tenant-scoped (id + seller_id). Возвращает [(id, title, photo_url)]."""
    from models import ImportedProduct

    query = ImportedProduct.query.filter(ImportedProduct.seller_id == seller_id)
    if product_ids:
        query = query.filter(ImportedProduct.id.in_(product_ids))
    rows = query.limit(500).all()
    selected = []
    for p in rows:
        url = first_photo_url(getattr(p, "photo_urls", None))
        if url:
            selected.append((p.id, (p.title or f"product-{p.id}"), url))
        if len(selected) >= limit:
            break
    return selected


def run_matrix(products, variants, preset, run_dir, budget_rub):
    """Прогоняет матрицу товары × варианты. Возвращает список строк results.jsonl."""
    from services.image_generation_service import is_censorship_refusal
    from services.infographic_prompts import build_background_prompt, build_edit_prompt

    results = []
    spent = 0.0
    results_path = run_dir / "results.jsonl"
    with open(results_path, "a", encoding="utf-8") as sink:
        for variant in variants:
            service = _make_service(variant)
            price = PRICE_TABLE_RUB.get(variant.key, DEFAULT_PRICE_RUB)
            prompt = (build_edit_prompt(preset) if variant.mode == "A"
                      else build_background_prompt(preset))
            for product_id, title, photo_url in products:
                if spent + price > budget_rub:
                    print(f"⛔ Бюджет {budget_rub}₽ исчерпан (потрачено ~{spent:.0f}₽) — стоп.")
                    return results
                started = time.monotonic()
                if variant.mode == "A":
                    ok, image, err = service.edit_image(
                        prompt=prompt, source_image_url=photo_url)
                else:
                    ok, image, err = service.generate_from_prompt(
                        prompt=prompt, width=900, height=1200)
                latency = round(time.monotonic() - started, 1)
                spent += price
                out_name = f"{product_id}_{variant.slug}.png"
                if ok and image:
                    (run_dir / out_name).write_bytes(image)
                    status = "ok"
                else:
                    out_name = None
                    status = "nsfw" if (err or "").startswith("NSFW:") or is_censorship_refusal(err) else "error"
                row = {
                    "product_id": product_id,
                    "title": title,
                    "variant": variant.key,
                    "mode": variant.mode,
                    "status": status,
                    "latency_s": latency,
                    "cost_rub": price,
                    "output": out_name,
                    "error": (err or "")[:300],
                }
                results.append(row)
                sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                sink.flush()
                print(f"[{status:5s}] {variant.key} mode={variant.mode} "
                      f"product={product_id} {latency}s ~{price}₽")
    return results


def main():
    parser = argparse.ArgumentParser(description="Пилот генерации инфографики")
    parser.add_argument("--seller-id", type=int, required=True)
    parser.add_argument("--products", help="Явные ID через запятую (иначе --limit выборка)")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--preset", default="boudoir",
                        choices=["boudoir", "neon", "luxury", "spa"])
    parser.add_argument("--variants", required=True,
                        help="provider:model:mode через запятую, напр. gen_api:flux-2:B")
    parser.add_argument("--budget-rub", type=float, default=500.0)
    parser.add_argument("--dry-run", action="store_true",
                        help="Только оценка стоимости, без API-вызовов")
    parser.add_argument("--export-gpu-bundle", action="store_true",
                        help="Собрать input-бандл для GPU-ветки (Task 8)")
    parser.add_argument("--extra-results",
                        help="results.jsonl GPU-прогона для объединённого отчёта")
    args = parser.parse_args()

    variants = parse_variants(args.variants)
    product_ids = None
    if args.products:
        product_ids = [int(x) for x in args.products.split(",") if x.strip()]

    from seller_platform import app  # noqa: WPS433 — паттерн one-off скриптов репо

    with app.app_context():
        products = select_products(args.seller_id, product_ids, args.limit)
        if not products:
            print("❌ Не найдено товаров с фото для этого seller_id")
            sys.exit(1)

        estimate = estimate_cost_rub(variants, len(products))
        print(f"Товаров: {len(products)}, вариантов: {len(variants)}, "
              f"оценка: ~{estimate:.0f}₽ (бюджет {args.budget_rub:.0f}₽)")
        if args.dry_run:
            return

        run_dir = Path("data/infographic_pilot") / time.strftime("run_%Y%m%d_%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)

        if args.export_gpu_bundle:
            raise SystemExit("--export-gpu-bundle появится в Task 8")

        results = run_matrix(products, variants, args.preset, run_dir, args.budget_rub)
        print(f"✅ Готово: {run_dir}/results.jsonl (report.html — Task 7)")


if __name__ == "__main__":
    main()
```

(`--export-gpu-bundle` и `report.html` — честные заглушки: реализуются в Tasks 7–8.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_infographic_pilot.py`
Expected: PASS. `python -m py_compile scripts/infographic_pilot.py`.

- [ ] **Step 5: Commit**

```bash
git add scripts/infographic_pilot.py tests/test_infographic_pilot.py
git commit -m "feat(infographics): пилот-скрипт — варианты, tenant-scoped выборка, бюджет-стоп, results.jsonl, dry-run"
```

---

### Task 7: HTML-отчёт пилота (матрица товар × вариант, merge GPU-результатов)

**Files:**
- Modify: `scripts/infographic_pilot.py`
- Test: `tests/test_infographic_pilot.py` (дополнить)

**Interfaces:**
- Consumes: формат строк `results.jsonl` (Task 6).
- Produces: `build_report_html(results: list[dict], extra_results_path: Optional[str] = None) -> str` — самодостаточный HTML (относительные `<img>`-пути, бейджи nsfw/error, latency и ₽); вызов в `main()` после `run_matrix` (замени заглушку Task 6).

- [ ] **Step 1: Write the failing test**

Добавь в `tests/test_infographic_pilot.py`:

```python
class ReportTests(unittest.TestCase):
    def _rows(self):
        return [
            {"product_id": 1, "title": "Товар А", "variant": "gen_api:flux-2",
             "mode": "B", "status": "ok", "latency_s": 4.2, "cost_rub": 3.3,
             "output": "1_gen_api_flux-2_B.png", "error": ""},
            {"product_id": 1, "title": "Товар А", "variant": "aitunnel:seedream-4.5",
             "mode": "A", "status": "nsfw", "latency_s": 2.0, "cost_rub": 6.8,
             "output": None, "error": "NSFW: blocked"},
        ]

    def test_report_contains_grid_and_badges(self):
        m = _load()
        html = m.build_report_html(self._rows())
        self.assertIn("1_gen_api_flux-2_B.png", html)   # картинка по относительному пути
        self.assertIn("NSFW", html)                      # бейдж отказа
        self.assertIn("Товар А", html)
        self.assertIn("6.8", html)                       # цена в ячейке

    def test_report_merges_extra_results(self, ):
        import tempfile
        m = _load()
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                         encoding="utf-8") as f:
            f.write(json.dumps({
                "product_id": 1, "title": "Товар А", "variant": "gpu:qwen-edit",
                "mode": "A", "status": "ok", "latency_s": 4.6, "cost_rub": 0.6,
                "output": "gpu/1_qwen_A.png", "error": ""}, ensure_ascii=False) + "\n")
            path = f.name
        html = m.build_report_html(self._rows(), extra_results_path=path)
        self.assertIn("gpu:qwen-edit", html)
        os.unlink(path)

    def test_summary_counts_by_variant(self):
        m = _load()
        html = m.build_report_html(self._rows())
        # сводка: для каждого варианта — ok/nsfw/error счётчики
        self.assertIn("gen_api:flux-2", html)
        self.assertIn("aitunnel:seedream-4.5", html)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_infographic_pilot.py`
Expected: FAIL — `AttributeError: ... build_report_html`.

- [ ] **Step 3: Write implementation**

Добавь в `scripts/infographic_pilot.py` (после `run_matrix`):

```python
_BADGE_COLORS = {"ok": "#1a7f37", "nsfw": "#b35900", "error": "#c0392b"}


def _load_extra_results(path):
    """Строки results.jsonl соседнего прогона; пути картинок делаем относительными
    отчёту: results.jsonl лежит рядом со своими PNG, поэтому префиксуем имя папки."""
    rows = []
    if not path:
        return rows
    base = Path(path).resolve().parent.name
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("output"):
                row["output"] = f"{base}/{row['output']}"
            rows.append(row)
    return rows


def build_report_html(results, extra_results_path=None):
    """Самодостаточный HTML-отчёт: сводка по вариантам + матрица товар × вариант."""
    rows = list(results) + _load_extra_results(extra_results_path)
    variants = sorted({r["variant"] + ":" + r["mode"] for r in rows})
    products = {}
    for r in rows:
        products.setdefault(r["product_id"], {"title": r.get("title", ""), "cells": {}})
        products[r["product_id"]]["cells"][r["variant"] + ":" + r["mode"]] = r

    # Сводка по вариантам
    summary = {}
    for r in rows:
        s = summary.setdefault(r["variant"] + ":" + r["mode"],
                               {"ok": 0, "nsfw": 0, "error": 0,
                                "latency": [], "cost": 0.0})
        s[r["status"]] = s.get(r["status"], 0) + 1
        s["latency"].append(r.get("latency_s") or 0)
        s["cost"] += r.get("cost_rub") or 0

    parts = [
        "<!doctype html><meta charset='utf-8'><title>Infographic pilot report</title>",
        "<style>body{font-family:Inter,Arial,sans-serif;margin:24px;background:#faf7f2}",
        "table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:6px;",
        "vertical-align:top;text-align:center}img{max-width:180px;display:block}",
        ".badge{display:inline-block;padding:2px 8px;border-radius:6px;color:#fff;",
        "font-size:12px}</style>",
        "<h1>Пилот «Фотостудии»</h1><h2>Сводка по вариантам</h2>",
        "<table><tr><th>Вариант</th><th>ok</th><th>nsfw</th><th>error</th>",
        "<th>сред. время, с</th><th>потрачено, ₽</th></tr>",
    ]
    for key in variants:
        s = summary[key]
        avg = round(sum(s["latency"]) / max(len(s["latency"]), 1), 1)
        parts.append(
            f"<tr><td>{key}</td><td>{s['ok']}</td><td>{s['nsfw']}</td>"
            f"<td>{s['error']}</td><td>{avg}</td><td>{round(s['cost'], 2)}</td></tr>")
    parts.append("</table><h2>Матрица</h2><table><tr><th>Товар</th>")
    parts.extend(f"<th>{key}</th>" for key in variants)
    parts.append("</tr>")
    for product_id, info in sorted(products.items()):
        parts.append(f"<tr><td><b>{product_id}</b><br>{info['title'][:60]}</td>")
        for key in variants:
            cell = info["cells"].get(key)
            if not cell:
                parts.append("<td>—</td>")
                continue
            color = _BADGE_COLORS.get(cell["status"], "#777")
            badge = (f"<span class='badge' style='background:{color}'>"
                     f"{cell['status'].upper()}</span>")
            img = (f"<img src='{cell['output']}' loading='lazy'>"
                   if cell.get("output") else "")
            parts.append(
                f"<td>{img}{badge}<br><small>{cell['latency_s']}с · "
                f"{cell['cost_rub']}₽</small><br>"
                f"<small>{(cell.get('error') or '')[:80]}</small></td>")
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)
```

И в `main()` замени заглушку Task 6 на реальный вызов:

```python
        results = run_matrix(products, variants, args.preset, run_dir, args.budget_rub)
        report = build_report_html(results, extra_results_path=args.extra_results)
        (run_dir / "report.html").write_text(report, encoding="utf-8")
        print(f"✅ Готово: {run_dir}/report.html")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_infographic_pilot.py`
Expected: PASS. `python -m py_compile scripts/infographic_pilot.py`.

- [ ] **Step 5: Commit**

```bash
git add scripts/infographic_pilot.py tests/test_infographic_pilot.py
git commit -m "feat(infographics): HTML-отчёт пилота — сводка по вариантам, матрица, merge GPU-результатов"
```

---

### Task 8: GPU-бандл — экспорт фото, манифест, текст-сэмплы (Playwright)

**Files:**
- Modify: `scripts/infographic_pilot.py`
- Test: `tests/test_infographic_pilot.py` (дополнить)

**Interfaces:**
- Consumes: `select_products` (Task 6), `services.infographic_prompts` (пресеты, `TEXT_SAMPLE_PHRASES`, `SHORT_TEXT_SAMPLES`).
- Produces: `build_gpu_manifest(products, presets, text_files) -> dict`; `export_gpu_bundle(products, bundle_dir: Path) -> Path` — скачивает фото в `bundle/photos/<id>.png`, рендерит `bundle/text/phrase_<NN>.png` (Playwright), пишет `bundle/manifest.json`. Формат манифеста читает GPU-скрипт (Task 9):

```json
{
  "products": [{"id": 1, "photo": "photos/1.png"}],
  "presets": {"boudoir": {"edit_prompt": "...", "background_prompt": "..."}},
  "text_samples": [{"file": "text/phrase_01.png", "phrase": "..."}],
  "short_texts": ["−30%", "ХИТ", "NEW", "ТОП", "18+"]
}
```

- [ ] **Step 1: Write the failing test**

Добавь в `tests/test_infographic_pilot.py`:

```python
class GpuBundleTests(unittest.TestCase):
    def test_manifest_structure(self):
        m = _load()
        manifest = m.build_gpu_manifest(
            products=[(7, "Товар", "http://a/1.jpg")],
            presets=["boudoir", "neon"],
            text_files=[("text/phrase_01.png", "Гипоаллергенный силикон")],
        )
        self.assertEqual(manifest["products"], [{"id": 7, "photo": "photos/7.png"}])
        self.assertIn("boudoir", manifest["presets"])
        self.assertIn("edit_prompt", manifest["presets"]["boudoir"])
        self.assertIn("background_prompt", manifest["presets"]["neon"])
        self.assertEqual(manifest["text_samples"][0]["phrase"],
                         "Гипоаллергенный силикон")
        self.assertGreaterEqual(len(manifest["short_texts"]), 4)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_infographic_pilot.py`
Expected: FAIL — `AttributeError: ... build_gpu_manifest`.

- [ ] **Step 3: Write implementation**

В `scripts/infographic_pilot.py`:

```python
def build_gpu_manifest(products, presets, text_files):
    """Манифест input-бандла для GPU-скрипта (scripts/gpu_pilot/)."""
    from services.infographic_prompts import (
        SHORT_TEXT_SAMPLES,
        build_background_prompt,
        build_edit_prompt,
    )

    return {
        "products": [{"id": pid, "photo": f"photos/{pid}.png"}
                     for pid, _title, _url in products],
        "presets": {
            key: {
                "edit_prompt": build_edit_prompt(key),
                "background_prompt": build_background_prompt(key),
            }
            for key in presets
        },
        "text_samples": [{"file": rel, "phrase": phrase}
                         for rel, phrase in text_files],
        "short_texts": list(SHORT_TEXT_SAMPLES),
    }


_TEXT_SAMPLE_HTML = """<!doctype html><meta charset="utf-8">
<style>body{{margin:0;width:900px;height:300px;display:flex;align-items:center;
justify-content:center;background:#ffffff}}
div{{font-family:'Inter',Arial,sans-serif;font-weight:800;font-size:64px;
color:#111;text-align:center;padding:0 40px}}</style>
<body><div>{phrase}</div></body>"""


def _render_text_samples(text_dir):
    """Рендерит фразы уровня 2 в PNG через Playwright (глифы задаём мы)."""
    from playwright.sync_api import sync_playwright

    from services.infographic_prompts import TEXT_SAMPLE_PHRASES

    text_dir.mkdir(parents=True, exist_ok=True)
    rendered = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 900, "height": 300})
        for idx, phrase in enumerate(TEXT_SAMPLE_PHRASES, start=1):
            page.set_content(_TEXT_SAMPLE_HTML.format(phrase=phrase))
            rel = f"text/phrase_{idx:02d}.png"
            page.screenshot(path=str(text_dir / f"phrase_{idx:02d}.png"))
            rendered.append((rel, phrase))
        browser.close()
    return rendered


def export_gpu_bundle(products, bundle_dir):
    """Собирает input-бандл для GPU-ветки: фото + текст-сэмплы + manifest.json."""
    import requests as _requests

    photos_dir = bundle_dir / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)
    exported = []
    for pid, title, url in products:
        try:
            resp = _requests.get(url, timeout=60)
            if resp.status_code == 200 and resp.content:
                (photos_dir / f"{pid}.png").write_bytes(resp.content)
                exported.append((pid, title, url))
            else:
                print(f"⚠️ Фото товара {pid}: HTTP {resp.status_code} — пропуск")
        except Exception as e:
            print(f"⚠️ Фото товара {pid}: {e} — пропуск")

    text_files = _render_text_samples(bundle_dir / "text")
    manifest = build_gpu_manifest(
        exported, presets=list(("boudoir", "neon", "luxury", "spa")),
        text_files=text_files)
    (bundle_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ GPU-бандл: {bundle_dir} (фото: {len(exported)}, "
          f"текстов: {len(text_files)})")
    return bundle_dir
```

В `main()` замени заглушку `--export-gpu-bundle` (Task 6) на:

```python
        if args.export_gpu_bundle:
            export_gpu_bundle(products, run_dir / "gpu_bundle")
```

(размести этот блок ПЕРЕД `run_matrix`, чтобы бандл собирался даже при нулевом API-бюджете; при `--variants` можно передать один дешёвый вариант и `--budget-rub 0` — матрица честно остановится, а бандл соберётся).

- [ ] **Step 4: Run tests to verify they pass**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_infographic_pilot.py`
Expected: PASS (Playwright в тестах не вызывается — `_render_text_samples` тестом не покрыт, это runtime-путь). `python -m py_compile scripts/infographic_pilot.py`.

- [ ] **Step 5: Commit**

```bash
git add scripts/infographic_pilot.py tests/test_infographic_pilot.py
git commit -m "feat(infographics): экспорт GPU-бандла — фото, Playwright текст-сэмплы, manifest.json"
```

---

### Task 8.5: Композит режима B — товар поверх AI-фона (rembg)

Открытый вопрос №3 спеки («достаточно ли режима B по сросшести») нельзя оценить
по голому фону — нужен композит: вырезанный товар поверх сгенерированной сцены.

**Files:**
- Modify: `scripts/infographic_pilot.py`
- Test: `tests/test_infographic_pilot.py` (дополнить)

**Interfaces:**
- Consumes: результат mode B из `run_matrix` (Task 6), `first_photo_url`.
- Produces: `compose_product_on_background(background_bytes: bytes, product_bytes: bytes, cutout=None) -> bytes` (PNG; `cutout` инжектируется для тестов, по умолчанию `rembg.remove`); `try_compose_mode_b(background_bytes, product_photo_url) -> Optional[bytes]` (None при любой ошибке, включая отсутствие rembg — пилот не падает).

- [ ] **Step 1: Write the failing test**

Добавь в `tests/test_infographic_pilot.py`:

```python
class ComposeTests(unittest.TestCase):
    def test_compose_places_product_on_background(self):
        import io

        from PIL import Image

        m = _load()

        def fake_cutout(product_bytes):
            img = Image.new("RGBA", (100, 200), (255, 0, 0, 255))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

        bg = Image.new("RGB", (900, 1200), (10, 10, 10))
        bg_buf = io.BytesIO()
        bg.save(bg_buf, format="PNG")
        out = m.compose_product_on_background(
            bg_buf.getvalue(), b"src", cutout=fake_cutout)
        result = Image.open(io.BytesIO(out))
        self.assertEqual(result.size, (900, 1200))
        # товар (красный) появился в нижней трети по центру
        r, g, b = result.getpixel((450, 1000))[:3]
        self.assertGreater(r, 150)
        self.assertLess(g, 100)

    def test_try_compose_swallows_errors(self):
        m = _load()
        self.assertIsNone(m.try_compose_mode_b(b"bg", "http://nonexistent.invalid/x.png"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_infographic_pilot.py`
Expected: FAIL — `AttributeError: ... compose_product_on_background`.

- [ ] **Step 3: Write implementation**

В `scripts/infographic_pilot.py`:

```python
def compose_product_on_background(background_bytes, product_bytes, cutout=None):
    """Режим B: вырезанный товар поверх AI-фона с мягкой тенью (низ-центр)."""
    import io

    from PIL import Image, ImageFilter

    if cutout is None:
        from rembg import remove as cutout  # dev-зависимость: pip install rembg

    bg = Image.open(io.BytesIO(background_bytes)).convert("RGBA")
    product = Image.open(io.BytesIO(cutout(product_bytes))).convert("RGBA")
    target_h = int(bg.height * 0.55)
    scale = target_h / max(product.height, 1)
    product = product.resize((max(int(product.width * scale), 1), target_h))
    x = (bg.width - product.width) // 2
    y = bg.height - product.height - int(bg.height * 0.08)

    shadow = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    alpha = product.split()[3].point(lambda a: int(a * 0.45))
    shadow.paste((0, 0, 0, 255), (x + 10, y + 18), mask=alpha)
    shadow = shadow.filter(ImageFilter.GaussianBlur(12))
    bg = Image.alpha_composite(bg, shadow)
    bg.paste(product, (x, y), product)

    out = io.BytesIO()
    bg.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def try_compose_mode_b(background_bytes, product_photo_url):
    """Композит или None (нет rembg, сеть, битое фото) — пилот не падает."""
    try:
        import requests as _requests

        photo = _requests.get(product_photo_url, timeout=60)
        if photo.status_code != 200:
            return None
        return compose_product_on_background(background_bytes, photo.content)
    except Exception as e:
        print(f"⚠️ Композит режима B пропущен: {e}")
        return None
```

И в `run_matrix` замени блок сохранения результата:

```python
                if ok and image:
                    (run_dir / out_name).write_bytes(image)
                    status = "ok"
```

на:

```python
                if ok and image:
                    (run_dir / out_name).write_bytes(image)
                    status = "ok"
                    if variant.mode == "B":
                        composite = try_compose_mode_b(image, photo_url)
                        if composite:
                            comp_name = f"{product_id}_{variant.slug}_comp.png"
                            (run_dir / comp_name).write_bytes(composite)
                            out_name = comp_name  # в отчёт идёт композит
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_infographic_pilot.py`
Expected: PASS. `python -m py_compile scripts/infographic_pilot.py`.

- [ ] **Step 5: Commit**

```bash
git add scripts/infographic_pilot.py tests/test_infographic_pilot.py
git commit -m "feat(infographics): композит режима B — rembg-вырезка товара поверх AI-фона с тенью"
```

---

### Task 9: GPU-скрипт для арендованного H100 + runbook

**Files:**
- Create: `scripts/gpu_pilot/run_qwen_pilot.py` (автономный, НЕ импортирует Flask/models)
- Create: `scripts/gpu_pilot/README.md` (runbook)

**Interfaces:**
- Consumes: `manifest.json` бандла (Task 8).
- Produces: `results.jsonl` в формате Task 6 (`variant` = `"gpu:qwen-edit"`/`"gpu:qwen-t2i"`, `cost_rub` считается из `--rub-per-hour`), совместимый с `--extra-results` отчёта (Task 7).

- [ ] **Step 1: Write the GPU script**

Создай `scripts/gpu_pilot/run_qwen_pilot.py` (тестов в репо нет — на машинах без GPU он не исполняется; контроль — py_compile):

```python
# -*- coding: utf-8 -*-
"""GPU-ветка пилота: Qwen-Image-Edit / Qwen-Image на арендованном H100.

Запускается НА арендованной машине (не в репо-окружении):
    python run_qwen_pilot.py --bundle ./gpu_bundle --out ./gpu_out \\
        --mode all --lightning --rub-per-hour 342

Режимы: a (i2i сцена вокруг товара), b (t2i фон), text2 (стилизация PNG-фраз),
text3 (свободная генерация коротких слов). Пишет results.jsonl (формат пилота).
"""

import argparse
import json
import time
from pathlib import Path

EDIT_MODEL = "Qwen/Qwen-Image-Edit-2511"
T2I_MODEL = "Qwen/Qwen-Image-2512"
# Lightning LoRA для ускорения (4 шага) — уточнить актуальный репозиторий
# на HF перед запуском: lightx2v/Qwen-Image-Lightning (t2i) и Edit-вариант.
LIGHTNING_LORA_T2I = "lightx2v/Qwen-Image-Lightning"
LIGHTNING_LORA_EDIT = "lightx2v/Qwen-Image-Edit-Lightning"

STYLIZE_PROMPT = (
    "Turn the plain black text into glowing neon tube letters on a dark "
    "background. Keep every letter shape and every character exactly as in "
    "the source image, same spelling. Add soft neon glow and reflections."
)
SHORT_TEXT_PROMPT = (
    'A bold promotional sticker with the text "{text}" in clean modern '
    "Cyrillic-capable typography, neon glow on dark background, centered."
)


def load_pipelines(mode, lightning):
    import torch
    from diffusers import QwenImageEditPipeline, QwenImagePipeline

    edit_pipe = t2i_pipe = None
    if mode in ("all", "a", "text2"):
        edit_pipe = QwenImageEditPipeline.from_pretrained(
            EDIT_MODEL, torch_dtype=torch.bfloat16).to("cuda")
        if lightning:
            edit_pipe.load_lora_weights(LIGHTNING_LORA_EDIT)
    if mode in ("all", "b", "text3"):
        t2i_pipe = QwenImagePipeline.from_pretrained(
            T2I_MODEL, torch_dtype=torch.bfloat16).to("cuda")
        if lightning:
            t2i_pipe.load_lora_weights(LIGHTNING_LORA_T2I)
    return edit_pipe, t2i_pipe


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--mode", default="all",
                        choices=["all", "a", "b", "text2", "text3"])
    parser.add_argument("--lightning", action="store_true")
    parser.add_argument("--steps", type=int, default=None,
                        help="Шаги инференса (default: 4 c --lightning, иначе 40)")
    parser.add_argument("--rub-per-hour", type=float, default=342.0)
    parser.add_argument("--preset", default="boudoir")
    args = parser.parse_args()

    from PIL import Image

    bundle = Path(args.bundle)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    steps = args.steps or (4 if args.lightning else 40)
    rub_per_second = args.rub_per_hour / 3600.0

    edit_pipe, t2i_pipe = load_pipelines(args.mode, args.lightning)
    preset = manifest["presets"][args.preset]
    sink = open(out_dir / "results.jsonl", "a", encoding="utf-8")

    def record(variant, mode, product_id, latency, out_name, error=""):
        sink.write(json.dumps({
            "product_id": product_id,
            "title": "",
            "variant": variant,
            "mode": mode,
            "status": "ok" if out_name else "error",
            "latency_s": round(latency, 1),
            "cost_rub": round(latency * rub_per_second, 3),
            "output": out_name,
            "error": error[:300],
        }, ensure_ascii=False) + "\n")
        sink.flush()

    if args.mode in ("all", "a"):
        for item in manifest["products"]:
            src = Image.open(bundle / item["photo"]).convert("RGB")
            started = time.monotonic()
            try:
                result = edit_pipe(image=src, prompt=preset["edit_prompt"],
                                   num_inference_steps=steps).images[0]
                name = f"{item['id']}_gpu_qwen-edit_A.png"
                result.save(out_dir / name)
                record("gpu:qwen-edit", "A", item["id"],
                       time.monotonic() - started, name)
            except Exception as e:
                record("gpu:qwen-edit", "A", item["id"],
                       time.monotonic() - started, None, str(e))

    if args.mode in ("all", "b"):
        for item in manifest["products"]:
            started = time.monotonic()
            try:
                result = t2i_pipe(prompt=preset["background_prompt"],
                                  num_inference_steps=steps,
                                  width=896, height=1152).images[0]
                name = f"{item['id']}_gpu_qwen-t2i_B.png"
                result.save(out_dir / name)
                record("gpu:qwen-t2i", "B", item["id"],
                       time.monotonic() - started, name)
            except Exception as e:
                record("gpu:qwen-t2i", "B", item["id"],
                       time.monotonic() - started, None, str(e))

    if args.mode in ("all", "text2"):
        for idx, sample in enumerate(manifest["text_samples"], start=1):
            src = Image.open(bundle / sample["file"]).convert("RGB")
            started = time.monotonic()
            try:
                result = edit_pipe(image=src, prompt=STYLIZE_PROMPT,
                                   num_inference_steps=steps).images[0]
                name = f"text2_{idx:02d}.png"
                result.save(out_dir / name)
                record("gpu:qwen-edit-text", "T2", idx,
                       time.monotonic() - started, name)
            except Exception as e:
                record("gpu:qwen-edit-text", "T2", idx,
                       time.monotonic() - started, None, str(e))

    if args.mode in ("all", "text3"):
        for idx, text in enumerate(manifest["short_texts"], start=1):
            started = time.monotonic()
            try:
                result = t2i_pipe(prompt=SHORT_TEXT_PROMPT.format(text=text),
                                  num_inference_steps=steps,
                                  width=896, height=1152).images[0]
                name = f"text3_{idx:02d}.png"
                result.save(out_dir / name)
                record("gpu:qwen-t2i-text", "T3", idx,
                       time.monotonic() - started, name)
            except Exception as e:
                record("gpu:qwen-t2i-text", "T3", idx,
                       time.monotonic() - started, None, str(e))

    sink.close()
    print(f"✅ GPU-прогон завершён: {out_dir}/results.jsonl")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write the runbook**

Создай `scripts/gpu_pilot/README.md`:

```markdown
# GPU-ветка пилота «Фотостудии» (H100, immers.cloud)

Цель: прогнать те же товары через открытые Qwen-Image-Edit-2511 / Qwen-Image-2512
(Apache 2.0) и сравнить с API-провайдерами по качеству/скорости/цене.
Бюджет: 2–3 часа H100 (~342₽/час) ≈ 700–1100₽.

## 1. Подготовка бандла (локально, в репо)

    SKIP_SCHEDULER=1 python scripts/infographic_pilot.py \
        --seller-id 1 --limit 20 --variants gen_api:flux-2:B \
        --budget-rub 0 --export-gpu-bundle
    # бандл: data/infographic_pilot/run_*/gpu_bundle/

## 2. Аренда машины

immers.cloud → конфигурация с 1× H100 80 ГБ (или H200), образ Ubuntu 22.04 + CUDA 12.
Диск от 200 ГБ (веса двух моделей ≈ 100–120 ГБ). Тарификация посекундная —
после прогона машину ГАСИТЬ или ставить на паузу.

## 3. Установка окружения (на машине, ~10 минут)

    python3 -m venv venv && source venv/bin/activate
    pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
    pip install diffusers transformers accelerate safetensors peft pillow sentencepiece
    # Проверить актуальные имена Lightning LoRA на huggingface.co (lightx2v/...)
    # и при необходимости поправить константы в run_qwen_pilot.py.

## 4. Загрузка данных и запуск

    scp -r data/infographic_pilot/run_*/gpu_bundle user@host:~/
    scp scripts/gpu_pilot/run_qwen_pilot.py user@host:~/
    # Первый запуск скачает веса с HF (~30–40 минут на быстром канале):
    python run_qwen_pilot.py --bundle ~/gpu_bundle --out ~/gpu_out \
        --mode all --lightning --rub-per-hour 342
    # Для сравнения качества полного инференса добавь второй прогон:
    python run_qwen_pilot.py --bundle ~/gpu_bundle --out ~/gpu_out_full \
        --mode a --steps 40 --rub-per-hour 342

## 5. Забрать результаты и объединить отчёт

    rsync -av user@host:~/gpu_out/ data/infographic_pilot/run_<id>/gpu/
    # пересобрать report.html с GPU-строками (пути картинок отчёт сам
    # префиксует папкой gpu/ — results.jsonl лежит рядом со своими PNG):
    SKIP_SCHEDULER=1 python - <<'EOF'
    import importlib.util, json
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("p", "scripts/infographic_pilot.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    run = Path("data/infographic_pilot/run_<id>")
    rows = [json.loads(l) for l in (run / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    html = m.build_report_html(rows, extra_results_path=str(run / "gpu" / "results.jsonl"))
    (run / "report.html").write_text(html, encoding="utf-8")
    print("report.html обновлён")
    EOF

## 6. Не забыть

- ПОГАСИТЬ машину (посекундная тарификация).
- Не коммитить сгенерированные изображения и бандлы (data/ в .gitignore).
- В results.jsonl `cost_rub` = wall-clock × тариф — честная цена, включая загрузку весов.
```

- [ ] **Step 3: Verify**

Run: `python -m py_compile scripts/gpu_pilot/run_qwen_pilot.py && git diff --check`
Expected: без ошибок. (diffusers/torch в репо не ставятся — импорты внутри функций.)

- [ ] **Step 4: Commit**

```bash
git add scripts/gpu_pilot/run_qwen_pilot.py scripts/gpu_pilot/README.md
git commit -m "feat(infographics): GPU-ветка пилота — автономный Qwen-скрипт для H100 и runbook immers.cloud"
```

---

### Task 10: Финальная проверка и обновление AGENTS.md

**Files:**
- Modify: `AGENTS.md` (раздел «Карта репозитория» / новые возможности)
- Modify: `docs/superpowers/specs/2026-07-14-infographics-design.md` (отметить Phase 0 как реализованный код, ждёт прогона)

**Interfaces:**
- Consumes: всё выше.
- Produces: зелёный полный прогон новых тестов; актуализированные доки.

- [ ] **Step 1: Полный прогон новых тестов**

Run:
```bash
SKIP_SCHEDULER=1 python -m pytest -q \
  tests/test_image_edit_providers.py \
  tests/test_infographic_prompts.py \
  tests/test_infographic_pilot.py
```
Expected: PASS, 0 failed.

- [ ] **Step 2: Смоук dry-run на реальной базе (без трат)**

Run: `SKIP_SCHEDULER=1 python scripts/infographic_pilot.py --seller-id 1 --limit 5 --variants gen_api:flux-2:B --dry-run`
Expected: печать «Товаров: N, вариантов: 1, оценка: ~X₽», exit 0. (Если БД пустая/нет seller — честная ошибка «Не найдено товаров», это тоже валидный смоук.)

- [ ] **Step 3: Обновить AGENTS.md**

В раздел «Карта репозитория» после блока про «Качество карточек» добавь абзац:

```markdown
Пилот «Фотостудии» (Phase 0): `services/image_generation_service.py` получил
провайдеры Gen-API/AITunnel (рубли) с i2i-методом `edit_image` и
NSFW-классификацией отказов; промпт-пресеты 18+-безопасных атмосфер — в
`services/infographic_prompts.py`; матрица-пилот — `scripts/infographic_pilot.py`
(tenant-scoped выборка, бюджет-стоп в рублях, results.jsonl + report.html,
экспорт GPU-бандла); автономная GPU-ветка для арендованного H100 —
`scripts/gpu_pilot/`. Ключи только из env (`GEN_API_KEY`, `AITUNNEL_API_KEY`).
Продакшен-пайплайн, миграции и UI — Phase 1 по спеке
`docs/superpowers/specs/2026-07-14-infographics-design.md` (отдельный план после
результатов пилота).
```

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md docs/superpowers/specs/2026-07-14-infographics-design.md
git commit -m "docs(infographics): AGENTS.md — карта Phase 0 пилота; спека — статус"
```

---

## Что нужно от пользователя перед прогоном пилота (не блокирует Tasks 1–10)

1. **Gen-API**: регистрация на gen-api.ru, пополнение ~500₽, ключ → env `GEN_API_KEY`.
2. **AITunnel**: регистрация на aitunnel.ru, пополнение ~500₽ (минимальный порог), ключ → env `AITUNNEL_API_KEY`.
3. **immers.cloud** (GPU-ветка): аккаунт, пополнение ~1500₽, создание H100-машины по runbook `scripts/gpu_pilot/README.md`.
4. Выбор 20–30 товаров (ID через `--products`) либо согласие на автоматическую выборку `--limit 20`.
   Для композита режима B в dev-окружение ставится `pip install rembg` (~200 МБ с onnxruntime; без него пилот работает, но режим B показывает голый фон).
5. Прогон матрицы, разглядывание `report.html`, решение: primary-модель, режим A/B, уровни текста, API vs self-host vs гибрид → после этого пишется план Phase 1.
