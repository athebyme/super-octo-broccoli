# Качество карточек v2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Честный Quality Score (контент относительно категории + воронка продаж WB), очередь исправлений с фильтрами по причинам, виджет дашборда с причинами и handoff выбранных карточек в единый ИИ-чат.

**Architecture:** Детерминированный скорер v2 в `services/card_quality_scorer.py` (чистые функции + DB-контекст для дубликатов/конфигов категорий), поведенческие метрики парсятся из уже получаемого ответа sales-funnel в `services/product_sync_scheduler.py` (ноль новых API-вызовов), причины хранятся CSV в `Product.attention_reasons` для SQL-фильтров, UI — Jinja2 + Alpine.js на существующих `.sh-*` токенах. ИИ-handoff — только фронтенд через существующие endpoints чата.

**Tech Stack:** Flask, SQLAlchemy (SQLite), Alpine.js, Jinja2, pytest/unittest.

**Спека:** `docs/superpowers/specs/2026-07-13-card-quality-v2-design.md`

## Global Constraints

- Тесты и любые запуски: префикс `SKIP_SCHEDULER=1` (иначе стартует APScheduler).
- Никаких реальных WB/LLM вызовов в unit-тестах.
- Tenant scope: любые запросы карточек — составное условие `id + seller_id`.
- Цены/остатки не затрагиваются; фиксы — только контент-поля из `ALLOWED_FIELDS` (`services/card_improver.py:26`).
- ИИ-write только через план/подтверждение harness; backend чата (`routes/agents.py`, `services/agent_harness.py`) НЕ менять — параллельная разработка.
- UI: CSS-переменные темы (`--bg`, `--bg-card`, `--text`, `--accent`, `--border`…), обе темы `data-theme="light|dark"`, существующие `.sh-*` классы, радиусы ≤8px.
- Минимум перед завершением каждой задачи: `python -m py_compile <файл>` и `git diff --check`.
- Коммит после каждой задачи. Рабочая ветка: `feature/card-quality-rating` (уже активна). В worktree много несвязанных изменений — в `git add` перечислять ТОЛЬКО файлы своей задачи, никогда `git add -A`.
- Номера строк в задачах ориентировочные (файлы живые) — находить места по именам функций/классов, не по строкам.
- `AGENTS.md` обновляется в том же изменении (Задача 13).

---

### Task 1: Скорер v2 — фото, описание, заголовок, новые веса

**Files:**
- Modify: `services/card_quality_scorer.py` (WEIGHTS:13-22, _dim_photos:35-44, _dim_description:67-74, _dim_title:77-85)
- Test: `tests/test_card_quality_scorer.py`

**Interfaces:**
- Consumes: существующие `compute_card_quality(card)`, `score_status`.
- Produces: card dict получает опциональный ключ `description_dup: bool` (штраф за дубликат); хелпер `_significant_words(text) -> list[str]` (используется Задачей 2 и тестами); новые WEIGHTS `{'characteristics':25,'photos':20,'description':20,'title':15,'brand':8,'barcodes':6,'price':3,'category':3}`.

- [ ] **Step 1: Обновить тесты под v2-поведение**

Заменить в `tests/test_card_quality_scorer.py` фабрики и добавить новые тесты (существующие тесты `_perfect_card`/`_empty_card` правятся, остальные классы файла не трогать, если не завязаны на старые пороги):

```python
def _perfect_card():
    return {
        'photos': ['u'] * 10,
        'characteristics': {f'Характеристика {i}': 'знач' for i in range(10)},
        'available_charcs': [
            {'name': f'Характеристика {i}', 'required': i < 3} for i in range(10)
        ],
        'title': 'Платье летнее женское хлопковое миди с поясом',
        'description': (
            'Лёгкое летнее платье из натурального хлопка свободного кроя. '
            'Дышащая ткань подходит для жаркой погоды, приталенный силуэт '
            'подчёркивает фигуру. Длина миди, съёмный пояс в комплекте. '
            'Машинная стирка при тридцати градусах, материал не мнётся и '
            'сохраняет цвет после многочисленных стирок. Подходит для '
            'офиса, прогулок и отпуска. Карманы по бокам, скрытая молния '
            'на спине, подкладка из вискозы. Размерная сетка соответствует '
            'российским стандартам, при сомнениях берите размер больше. '
            'Производство Россия, сертификат соответствия имеется.'
        ),
        'brand': 'BrandX',
        'barcodes': ['1234567890123'],
        'price': 999,
        'subject_id': 5,
    }


class TestPhotosV2(unittest.TestCase):
    def test_zero_photos_is_error(self):
        card = _perfect_card(); card['photos'] = []
        d = compute_card_quality(card)['dimensions']['photos']
        self.assertEqual(d['score'], 0); self.assertEqual(d['status'], 'error')

    def test_under_5_photos_capped_at_40(self):
        card = _perfect_card(); card['photos'] = ['u'] * 4
        d = compute_card_quality(card)['dimensions']['photos']
        self.assertEqual(d['score'], 40); self.assertEqual(d['status'], 'warning')

    def test_10_photos_is_100(self):
        card = _perfect_card(); card['photos'] = ['u'] * 10
        self.assertEqual(compute_card_quality(card)['dimensions']['photos']['score'], 100)


class TestDescriptionV2(unittest.TestCase):
    def test_length_scale_600(self):
        card = _perfect_card()
        self.assertEqual(compute_card_quality(card)['dimensions']['description']['score'], 100)

    def test_duplicate_penalty(self):
        card = _perfect_card(); card['description_dup'] = True
        d = compute_card_quality(card)['dimensions']['description']
        self.assertEqual(d['score'], 40)  # 100 * 0.4
        self.assertEqual(d['status'], 'warning')

    def test_low_unique_words_penalty(self):
        card = _perfect_card()
        card['description'] = ('слово другое ' * 60)[:650]  # длина 600+, но 2 уникальных слова
        d = compute_card_quality(card)['dimensions']['description']
        self.assertEqual(d['score'], 60)  # 100 * 0.6
        self.assertEqual(d['status'], 'warning')


class TestTitleV2(unittest.TestCase):
    def test_good_title_100(self):
        self.assertEqual(compute_card_quality(_perfect_card())['dimensions']['title']['score'], 100)

    def test_few_significant_words_penalty(self):
        card = _perfect_card(); card['title'] = 'Ааааааааааааа ббббббббббббб'  # 2 слова, длина 27
        d = compute_card_quality(card)['dimensions']['title']
        self.assertEqual(d['score'], 70); self.assertEqual(d['status'], 'warning')

    def test_word_spam_penalty(self):
        card = _perfect_card(); card['title'] = 'платье платье платье красное летнее'
        d = compute_card_quality(card)['dimensions']['title']
        self.assertEqual(d['score'], 80)  # 100 - 20 за повтор ≥3
        self.assertEqual(d['status'], 'warning')

    def test_caps_penalty(self):
        card = _perfect_card(); card['title'] = 'ПЛАТЬЕ ЛЕТНЕЕ ЖЕНСКОЕ ХЛОПКОВОЕ МИДИ'
        d = compute_card_quality(card)['dimensions']['title']
        self.assertEqual(d['score'], 80); self.assertEqual(d['status'], 'warning')

    def test_over_60_still_50(self):
        card = _perfect_card(); card['title'] = 'х' * 61
        self.assertEqual(compute_card_quality(card)['dimensions']['title']['score'], 50)
```

В `test_perfect_card_scores_100` ассерты не меняются (карта v2 даёт 100). В `test_weights_sum_to_100` ничего не менять.

- [ ] **Step 2: Прогнать тесты — убедиться, что новые падают**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_quality_scorer.py`
Expected: FAIL (старые формулы: 4 фото → 50, description 600-шкалы нет, title без штрафов).

- [ ] **Step 3: Реализация в `services/card_quality_scorer.py`**

Добавить `import re` вверху. Заменить `WEIGHTS`:

```python
WEIGHTS = {
    'characteristics': 25,
    'photos': 20,
    'description': 20,
    'title': 15,
    'brand': 8,
    'barcodes': 6,
    'price': 3,
    'category': 3,
}
```

Добавить хелпер (перед `_dim_photos`):

```python
_WORD_RE = re.compile(r'[а-яёa-z0-9]+')


def _significant_words(text: str) -> list:
    """Значимые слова: ≥3 символов, не чисто цифровые, lower-case."""
    return [w for w in _WORD_RE.findall((text or '').lower())
            if len(w) >= 3 and not w.isdigit()]
```

Заменить `_dim_photos`:

```python
def _dim_photos(card) -> tuple:
    count = len(card.get('photos') or [])
    if count == 0:
        return 0, 'error', 'Нет фото — добавьте минимум 5 (до 30 на WB)'
    sub = min(100, count * 10)
    if count < 5:
        return min(sub, 40), 'warning', f'Мало фото ({count}) — минимум 5, рекомендуем 10+'
    if count < 10:
        return sub, 'ok', f'Можно добавить фото ({count}/10)'
    return 100, 'ok', ''
```

Заменить `_dim_description`:

```python
def _dim_description(card) -> tuple:
    text = card.get('description') or ''
    length = len(text)
    if length == 0:
        return 0, 'error', 'Добавьте описание товара'
    sub = min(100, length * 100 // 600)
    hints = []
    if card.get('description_dup'):
        sub = int(sub * 0.4)
        hints.append('Описание дублируется у нескольких карточек — сделайте уникальным')
    if len(set(_significant_words(text))) < 15:
        sub = int(sub * 0.6)
        hints.append('Описание малосодержательное — добавьте конкретики')
    if length < 300:
        hints.append('Короткое описание — расширьте до 600+ символов')
    if hints:
        return sub, 'warning', '; '.join(hints)
    return sub, 'ok', ('' if sub >= 100 else 'Можно расширить описание до 600+ символов')
```

Заменить `_dim_title`:

```python
def _dim_title(card) -> tuple:
    title = card.get('title') or ''
    length = len(title)
    if length == 0:
        return 0, 'error', 'Нет заголовка'
    if length > 60:
        return 50, 'warning', 'Заголовок длиннее 60 символов — WB обрежет'
    sub = 100 if length >= 25 else min(100, length * 100 // 25)
    hints = [] if length >= 25 else ['Короткий заголовок — добавьте деталей']
    words = _significant_words(title)
    if len(words) < 4:
        sub -= 30
        hints.append('Мало значимых слов в заголовке (нужно 4+)')
    counts = {}
    for w in words:
        counts[w] = counts.get(w, 0) + 1
    if counts and max(counts.values()) >= 3:
        sub -= 20
        hints.append('Слово повторяется 3+ раз — уберите спам')
    letters = [c for c in title if c.isalpha()]
    if len(letters) >= 10 and all(c.isupper() for c in letters):
        sub -= 20
        hints.append('Заголовок капсом — снижает доверие')
    sub = max(0, sub)
    if hints:
        return sub, 'warning', '; '.join(hints)
    return sub, 'ok', ''
```

ВНИМАНИЕ: `_dim_characteristics` в этой задаче НЕ трогать (Задача 2) — но с `available_charcs` в `_perfect_card` старая реализация его игнорирует и даст 100 по count. Это ок.

- [ ] **Step 4: Прогнать тесты скорера**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_quality_scorer.py`
Expected: PASS. Если падают чужие тесты из этого файла из-за новых весов — поправить их ожидания под новые WEIGHTS (веса — единственный источник изменения).

- [ ] **Step 5: Прогнать смежные тесты**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_quality_detail.py tests/test_card_quality_summary.py tests/test_card_quality_apply_route.py`
Expected: PASS либо падения ТОЛЬКО из-за новых весов/порогов — поправить ожидаемые числа в этих тестах (логика не меняется).

- [ ] **Step 6: Commit**

```bash
git add services/card_quality_scorer.py tests/test_card_quality_scorer.py tests/test_card_quality_detail.py tests/test_card_quality_summary.py tests/test_card_quality_apply_route.py
git commit -m "feat(card-quality): скорер v2 — фото/описание/заголовок, новые веса"
```

---

### Task 2: Скорер v2 — характеристики относительно категории

**Files:**
- Modify: `services/card_quality_scorer.py` (`_dim_characteristics`)
- Test: `tests/test_card_quality_scorer.py`

**Interfaces:**
- Consumes: card dict может содержать `available_charcs: list[{'name': str, 'required': bool}] | None`.
- Produces: sub-балл характеристик = взвешенная доля заполненных (required ×3); fallback без конфига — старая шкала count×10 с потолком 70.

- [ ] **Step 1: Тесты**

Добавить в `tests/test_card_quality_scorer.py`:

```python
class TestCharacteristicsV2(unittest.TestCase):
    def _card(self, chars, available):
        card = _perfect_card()
        card['characteristics'] = chars
        card['available_charcs'] = available
        return card

    def test_full_fill_is_100(self):
        av = [{'name': 'Цвет', 'required': True}, {'name': 'Состав', 'required': False}]
        d = compute_card_quality(self._card({'Цвет': 'красный', 'Состав': 'хлопок'}, av))
        self.assertEqual(d['dimensions']['characteristics']['score'], 100)

    def test_required_weighted_x3(self):
        av = [{'name': 'Цвет', 'required': True}, {'name': 'Состав', 'required': False}]
        # заполнен только optional: 1 / (3+1) = 25
        d = compute_card_quality(self._card({'Состав': 'хлопок'}, av))
        self.assertEqual(d['dimensions']['characteristics']['score'], 25)

    def test_name_match_case_insensitive(self):
        av = [{'name': 'Цвет', 'required': False}]
        d = compute_card_quality(self._card({'цвет ': 'красный'}, av))
        self.assertEqual(d['dimensions']['characteristics']['score'], 100)

    def test_zero_filled_is_error(self):
        av = [{'name': 'Цвет', 'required': True}]
        d = compute_card_quality(self._card({}, av))['dimensions']['characteristics']
        self.assertEqual(d['score'], 0); self.assertEqual(d['status'], 'error')

    def test_fallback_without_config_capped_70(self):
        d = compute_card_quality(self._card({f'k{i}': 'v' for i in range(10)}, None))
        self.assertEqual(d['dimensions']['characteristics']['score'], 70)

    def test_list_format_characteristics(self):
        av = [{'name': 'Цвет', 'required': False}]
        chars = [{'name': 'Цвет', 'value': 'красный'}]
        d = compute_card_quality(self._card(chars, av))
        self.assertEqual(d['dimensions']['characteristics']['score'], 100)
```

- [ ] **Step 2: Прогнать — новые падают**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_quality_scorer.py -k CharacteristicsV2`
Expected: FAIL (старая count-логика).

- [ ] **Step 3: Реализация**

Заменить `_dim_characteristics` (хелпер `_count_characteristics` оставить — используется в fallback):

```python
def _norm_char_name(name) -> str:
    return str(name or '').strip().lower()


def _filled_char_names(chars) -> set:
    names = set()
    if isinstance(chars, dict):
        for k, v in chars.items():
            if not str(k).startswith('_') and v:
                names.add(_norm_char_name(k))
    elif isinstance(chars, list):
        for item in chars:
            if isinstance(item, dict):
                nm = item.get('name') or item.get('charcName')
                if nm and (item.get('value') or item.get('values')):
                    names.add(_norm_char_name(nm))
    return names


def _dim_characteristics(card) -> tuple:
    available = card.get('available_charcs')
    count = _count_characteristics(card.get('characteristics'))
    if not available:
        # Fallback без конфига категории: полноту подтвердить нельзя — потолок 70
        sub = min(70, count * 10)
        if count == 0:
            return 0, 'error', 'Заполните характеристики товара'
        if count < 3:
            return sub, 'warning', f'Мало характеристик ({count}) — WB может отклонить'
        return sub, 'ok', f'Заполнено {count}; для точной оценки нужен конфиг категории'
    filled = _filled_char_names(card.get('characteristics'))
    total_w = filled_w = total_n = filled_n = 0
    for ch in available:
        name = _norm_char_name(ch.get('name'))
        if not name:
            continue
        w = 3 if ch.get('required') else 1
        total_w += w
        total_n += 1
        if name in filled:
            filled_w += w
            filled_n += 1
    if total_w == 0:
        return (100, 'ok', '') if count else (0, 'error', 'Заполните характеристики товара')
    sub = round(100 * filled_w / total_w)
    if filled_n == 0:
        return 0, 'error', f'Заполните характеристики (0 из {total_n})'
    if sub < 60:
        return sub, 'warning', f'Заполнено {filled_n} из {total_n} характеристик категории'
    if sub < 100:
        return sub, 'ok', f'Можно заполнить ещё ({filled_n}/{total_n})'
    return sub, 'ok', ''
```

- [ ] **Step 4: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_quality_scorer.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/card_quality_scorer.py tests/test_card_quality_scorer.py
git commit -m "feat(card-quality): характеристики относительно конфига категории WB"
```

---

### Task 3: Причины и impact — compute_attention

**Files:**
- Modify: `services/card_quality_scorer.py` (добавить после `compute_card_quality`)
- Test: `tests/test_card_quality_scorer.py`

**Interfaces:**
- Produces:
  - `ATTENTION_REASONS: tuple[str]` — 9 кодов;
  - `REASON_LABELS: dict[str, str]` — русские подписи (используются в summary и UI);
  - `compute_attention(card, dimensions, nm_rating=None, feedback_rating=None, views_30d=None, orders_30d=None, cart_conv=None, buyout_rate=None) -> {'reasons': list[str], 'impact': float}`;
  - константы порогов: `NO_VIEWS_THRESHOLD=30`, `LOW_CART_CONV_MIN_VIEWS=100`, `LOW_CART_CONV_THRESHOLD=4.0`, `LOW_BUYOUT_MIN_ORDERS=5`, `LOW_BUYOUT_THRESHOLD=30.0`, `LOW_NM_RATING=6.0`, `LOW_FEEDBACK_RATING=4.0`.

- [ ] **Step 1: Тесты**

```python
from services.card_quality_scorer import compute_attention, ATTENTION_REASONS


class TestComputeAttention(unittest.TestCase):
    def _dims(self, card):
        return compute_card_quality(card)['dimensions']

    def test_perfect_card_no_content_reasons(self):
        card = _perfect_card()
        att = compute_attention(card, self._dims(card), nm_rating=9.0, views_30d=500,
                                orders_30d=20, cart_conv=8.0, buyout_rate=60.0)
        self.assertEqual(att['reasons'], [])
        self.assertEqual(att['impact'], 0.0)

    def test_few_photos(self):
        card = _perfect_card(); card['photos'] = ['u'] * 4
        att = compute_attention(card, self._dims(card), nm_rating=9.0, views_30d=500)
        self.assertIn('few_photos', att['reasons'])

    def test_no_views(self):
        card = _perfect_card()
        att = compute_attention(card, self._dims(card), nm_rating=9.0, views_30d=10)
        self.assertIn('no_views', att['reasons'])

    def test_views_none_is_not_no_views(self):
        card = _perfect_card()
        att = compute_attention(card, self._dims(card), nm_rating=9.0, views_30d=None)
        self.assertNotIn('no_views', att['reasons'])

    def test_low_cart_conv_needs_min_views(self):
        card = _perfect_card()
        att = compute_attention(card, self._dims(card), nm_rating=9.0,
                                views_30d=99, cart_conv=1.0)
        self.assertNotIn('low_cart_conv', att['reasons'])
        att = compute_attention(card, self._dims(card), nm_rating=9.0,
                                views_30d=100, cart_conv=3.9)
        self.assertIn('low_cart_conv', att['reasons'])

    def test_low_buyout(self):
        card = _perfect_card()
        att = compute_attention(card, self._dims(card), nm_rating=9.0, views_30d=500,
                                orders_30d=5, buyout_rate=29.0)
        self.assertIn('low_buyout', att['reasons'])

    def test_low_rating_by_feedback(self):
        card = _perfect_card()
        att = compute_attention(card, self._dims(card), nm_rating=8.0,
                                feedback_rating=3.9, views_30d=500)
        self.assertIn('low_rating', att['reasons'])

    def test_no_sales_signal(self):
        card = _perfect_card()
        att = compute_attention(card, self._dims(card), nm_rating=None, views_30d=0)
        self.assertIn('no_sales_signal', att['reasons'])

    def test_impact_includes_boost(self):
        card = _perfect_card()
        att = compute_attention(card, self._dims(card), nm_rating=9.0, views_30d=10)
        self.assertEqual(att['impact'], 10.0)  # контент идеален, буст no_views

    def test_weak_content_reasons(self):
        card = _perfect_card()
        card['photos'] = ['u'] * 3
        card['description'] = 'коротко'
        card['title'] = 'аб'
        card['characteristics'] = {}
        att = compute_attention(card, self._dims(card), nm_rating=9.0, views_30d=500)
        for r in ('few_photos', 'weak_chars', 'weak_description', 'weak_title'):
            self.assertIn(r, att['reasons'])
        self.assertGreater(att['impact'], 50)
```

- [ ] **Step 2: Прогнать — падают**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_quality_scorer.py -k Attention`
Expected: FAIL, ImportError `compute_attention`.

- [ ] **Step 3: Реализация**

Добавить в `services/card_quality_scorer.py` после `compute_card_quality`:

```python
# ── Причины «требует внимания» и приоритет фикса ──────────────────────

ATTENTION_REASONS = (
    'few_photos', 'weak_chars', 'weak_description', 'weak_title',
    'no_views', 'low_cart_conv', 'low_buyout', 'low_rating', 'no_sales_signal',
)

REASON_LABELS = {
    'few_photos': 'Мало фото',
    'weak_chars': 'Слабые характеристики',
    'weak_description': 'Слабое описание',
    'weak_title': 'Слабый заголовок',
    'no_views': 'Нет просмотров',
    'low_cart_conv': 'Не кладут в корзину',
    'low_buyout': 'Низкий выкуп',
    'low_rating': 'Низкий рейтинг',
    'no_sales_signal': 'Нет данных о продажах',
}

NO_VIEWS_THRESHOLD = 30
LOW_CART_CONV_MIN_VIEWS = 100
LOW_CART_CONV_THRESHOLD = 4.0
LOW_BUYOUT_MIN_ORDERS = 5
LOW_BUYOUT_THRESHOLD = 30.0
LOW_NM_RATING = 6.0
LOW_FEEDBACK_RATING = 4.0

_REASON_BOOSTS = {'no_views': 10.0, 'low_cart_conv': 8.0, 'low_buyout': 5.0, 'low_rating': 5.0}
_WEAK_DIM_SUB = 60


def compute_attention(card, dimensions, nm_rating=None, feedback_rating=None,
                      views_30d=None, orders_30d=None, cart_conv=None,
                      buyout_rate=None) -> Dict[str, Any]:
    """Причины «требует внимания» и impact (потенциал фикса) карточки.

    None-значения поведенческих метрик означают «нет данных» и не создают
    причин (кроме no_sales_signal: нет ни рейтинга, ни просмотров).
    """
    reasons = []
    if len(card.get('photos') or []) < 5:
        reasons.append('few_photos')
    if dimensions['characteristics']['score'] < _WEAK_DIM_SUB:
        reasons.append('weak_chars')
    if dimensions['description']['score'] < _WEAK_DIM_SUB:
        reasons.append('weak_description')
    if dimensions['title']['score'] < _WEAK_DIM_SUB:
        reasons.append('weak_title')
    if views_30d is not None and views_30d < NO_VIEWS_THRESHOLD:
        reasons.append('no_views')
    if (views_30d is not None and cart_conv is not None
            and views_30d >= LOW_CART_CONV_MIN_VIEWS
            and cart_conv < LOW_CART_CONV_THRESHOLD):
        reasons.append('low_cart_conv')
    if (orders_30d is not None and buyout_rate is not None
            and orders_30d >= LOW_BUYOUT_MIN_ORDERS
            and buyout_rate < LOW_BUYOUT_THRESHOLD):
        reasons.append('low_buyout')
    if ((nm_rating is not None and nm_rating < LOW_NM_RATING)
            or (feedback_rating is not None and feedback_rating < LOW_FEEDBACK_RATING)):
        reasons.append('low_rating')
    if nm_rating is None and not views_30d:
        reasons.append('no_sales_signal')

    impact = sum(d['weight'] * (100 - d['score']) / 100.0 for d in dimensions.values())
    impact += sum(_REASON_BOOSTS.get(r, 0.0) for r in reasons)
    return {'reasons': reasons, 'impact': round(impact, 1)}
```

- [ ] **Step 4: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_quality_scorer.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/card_quality_scorer.py tests/test_card_quality_scorer.py
git commit -m "feat(card-quality): причины attention + impact фикса"
```

---

### Task 4: Модели и миграция

**Files:**
- Modify: `models.py` (Product после `quality_checked_at`, ~строка 223; новая модель рядом с `CardRatingHistory`, ~строка 2126)
- Create: `migrations/migrate_add_card_quality_v2.py`
- Modify: `docker-entrypoint.sh` (после строки 138), `migrations/run_all_migrations.py` (блок products)
- Test: `tests/test_card_quality_v2_migration.py`

**Interfaces:**
- Produces: колонки `Product.wb_views_30d/wb_orders_30d (Integer)`, `wb_cart_conv/wb_order_conv/wb_buyout_rate/quality_impact (Float)`, `funnel_checked_at (DateTime)`, `attention_reasons (Text, CSV)`; модель `WbSubjectCharcsCache(subject_id PK, charcs_json Text, fetched_at DateTime)`; функция `migrate(db_path=None) -> bool` в migration-скрипте.

- [ ] **Step 1: Тест миграции**

Создать `tests/test_card_quality_v2_migration.py`:

```python
# -*- coding: utf-8 -*-
"""Тест идемпотентной миграции card-quality v2."""
import sqlite3
import unittest
import tempfile
import os

from migrations.migrate_add_card_quality_v2 import migrate


class TestCardQualityV2Migration(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, quality_score FLOAT)")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_adds_columns_and_table(self):
        self.assertTrue(migrate(self.db_path))
        conn = sqlite3.connect(self.db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
        self.assertTrue({'wb_views_30d', 'wb_orders_30d', 'wb_cart_conv', 'wb_order_conv',
                         'wb_buyout_rate', 'funnel_checked_at', 'attention_reasons',
                         'quality_impact'} <= cols)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn('wb_subject_charcs_cache', tables)
        conn.close()

    def test_idempotent(self):
        self.assertTrue(migrate(self.db_path))
        self.assertTrue(migrate(self.db_path))  # повторный запуск не падает
```

- [ ] **Step 2: Прогнать — падает (модуля нет)**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_quality_v2_migration.py`
Expected: FAIL, ModuleNotFoundError.

- [ ] **Step 3: Миграционный скрипт**

Создать `migrations/migrate_add_card_quality_v2.py`:

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Миграция: Качество карточек v2.

Добавляет в products метрики воронки продаж WB, причины attention и impact;
создаёт таблицу wb_subject_charcs_cache (кэш конфигов характеристик категорий).
Идемпотентна.

Запуск:
    python migrations/migrate_add_card_quality_v2.py [путь_к_БД]
"""
import os
import sqlite3
import sys

PRODUCT_COLUMNS = [
    ('wb_views_30d', 'INTEGER'),
    ('wb_orders_30d', 'INTEGER'),
    ('wb_cart_conv', 'FLOAT'),
    ('wb_order_conv', 'FLOAT'),
    ('wb_buyout_rate', 'FLOAT'),
    ('funnel_checked_at', 'DATETIME'),
    ('attention_reasons', 'TEXT'),
    ('quality_impact', 'FLOAT'),
]


def get_db_path():
    if len(sys.argv) > 1 and not sys.argv[1].startswith('-'):
        return sys.argv[1]
    db_path = os.environ.get('DATABASE_PATH')
    if db_path:
        return db_path
    db_url = os.environ.get('DATABASE_URL', '')
    if db_url.startswith('sqlite:///'):
        return db_url.replace('sqlite:///', '')
    for cand in ('/app/data/seller_platform.db', 'data/seller_platform.db'):
        if os.path.exists(cand):
            return cand
    return 'data/seller_platform.db'


def migrate(db_path=None) -> bool:
    db_path = db_path or get_db_path()
    if not os.path.exists(db_path):
        print(f"❌ База не найдена: {db_path}")
        return False
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(products)")
        existing = {row[1] for row in cur.fetchall()}
        for name, ctype in PRODUCT_COLUMNS:
            if name not in existing:
                cur.execute(f"ALTER TABLE products ADD COLUMN {name} {ctype}")
                print(f"  ✅ products.{name}")
            else:
                print(f"  ⏭️  products.{name} уже есть")
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='wb_subject_charcs_cache'")
        if not cur.fetchone():
            cur.execute("""
                CREATE TABLE wb_subject_charcs_cache (
                    subject_id INTEGER PRIMARY KEY,
                    charcs_json TEXT,
                    fetched_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            print("  ✅ wb_subject_charcs_cache")
        else:
            print("  ⏭️  wb_subject_charcs_cache уже есть")
        conn.commit()
        print("✅ card-quality v2: миграция завершена")
        return True
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(0 if migrate() else 1)
```

- [ ] **Step 4: Модели**

В `models.py` после блока `quality_checked_at` (строка ~223) добавить:

```python
    # Воронка продаж WB за 30 дней (sales-funnel v3, без отдельных API-вызовов)
    wb_views_30d = db.Column(db.Integer, nullable=True)     # просмотры карточки
    wb_orders_30d = db.Column(db.Integer, nullable=True)    # заказы
    wb_cart_conv = db.Column(db.Float, nullable=True)       # % просмотр→корзина
    wb_order_conv = db.Column(db.Float, nullable=True)      # % корзина→заказ
    wb_buyout_rate = db.Column(db.Float, nullable=True)     # % выкупа
    funnel_checked_at = db.Column(db.DateTime, nullable=True)

    # Причины «требует внимания» (CSV кодов из card_quality_scorer.ATTENTION_REASONS)
    attention_reasons = db.Column(db.Text, nullable=True)
    quality_impact = db.Column(db.Float, nullable=True)  # потенциал фикса для сортировки
```

Рядом с `CardRatingHistory` (после её класса, ~строка 2148) добавить:

```python
class WbSubjectCharcsCache(db.Model):
    """Кэш конфигов характеристик категорий WB (TTL обновления — 7 дней)."""
    __tablename__ = 'wb_subject_charcs_cache'

    subject_id = db.Column(db.Integer, primary_key=True)
    charcs_json = db.Column(db.Text)  # JSON [{'name','required'}]
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
```

- [ ] **Step 5: Wiring**

В `docker-entrypoint.sh` после строки 138 (`migrate_add_service_agents.py`) добавить:

```bash
python migrations/migrate_add_card_quality_v2.py /app/data/seller_platform.db || echo "⚠️ Card quality v2 migration skipped (already applied or error)"
```

В `migrations/run_all_migrations.py` найти блок, где для таблицы `products` вызывается `add_column_if_missing` (grep `'products'`), и добавить в него:

```python
    for name, ctype in [
        ('wb_views_30d', 'INTEGER'), ('wb_orders_30d', 'INTEGER'),
        ('wb_cart_conv', 'FLOAT'), ('wb_order_conv', 'FLOAT'),
        ('wb_buyout_rate', 'FLOAT'), ('funnel_checked_at', 'DATETIME'),
        ('attention_reasons', 'TEXT'), ('quality_impact', 'FLOAT'),
    ]:
        add_column_if_missing(cursor, 'products', name, ctype, existing_columns)
```

(имена локальных переменных подогнать под фактический код блока; existing_columns получить через `get_existing_columns(cursor, 'products')`, если в блоке ещё не получен).

- [ ] **Step 6: Прогнать тесты + компиляция**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_quality_v2_migration.py && python -m py_compile models.py migrations/migrate_add_card_quality_v2.py migrations/run_all_migrations.py`
Expected: PASS, без ошибок компиляции.

- [ ] **Step 7: Commit**

```bash
git add models.py migrations/migrate_add_card_quality_v2.py migrations/run_all_migrations.py docker-entrypoint.sh tests/test_card_quality_v2_migration.py
git commit -m "feat(card-quality): колонки воронки/причин + кэш конфигов категорий, миграция v2"
```

---

### Task 5: Кэш конфигов категорий + scoring context + recompute v2

**Files:**
- Create: `services/subject_charcs_cache.py`
- Modify: `services/card_quality_scorer.py` (`product_to_card_input`, `card_quality_detail`, `recompute_and_persist`)
- Test: `tests/test_subject_charcs_cache.py`

**Interfaces:**
- Consumes: `WbSubjectCharcsCache` (Task 4), `compute_attention` (Task 3).
- Produces:
  - `subject_charcs_cache.get_available_charcs(subject_id) -> list[{'name','required'}] | None`;
  - `subject_charcs_cache.refresh_subject_charcs(wb_client, subject_ids, force=False)` — лениво обновляет кэш, ошибки WB глотает (warning), коммитит сама;
  - `card_quality_scorer.build_seller_scoring_context(seller_id) -> {'dup_descriptions': set, 'charcs_by_subject': dict}`;
  - `product_to_card_input(product, context=None)` — добавляет `description_dup` и `available_charcs` при наличии context;
  - `recompute_and_persist(product, capture_history=True, context=None)` — дополнительно пишет `attention_reasons` (CSV) и `quality_impact`, возвращает cq с ключами `reasons`, `impact`;
  - `card_quality_detail(product)` — дополнительно `attention_reasons: list`, `quality_impact`, `wb_views_30d`, `wb_orders_30d`, `wb_cart_conv`, `wb_buyout_rate`.

- [ ] **Step 1: Тесты**

Создать `tests/test_subject_charcs_cache.py` (паттерн in-memory app из `tests/test_card_quality_summary.py`):

```python
# -*- coding: utf-8 -*-
"""Тесты кэша конфигов характеристик и scoring context."""
import json
import unittest
from datetime import datetime, timedelta

from flask import Flask

from models import db, Product, WbSubjectCharcsCache
from services.subject_charcs_cache import get_available_charcs, refresh_subject_charcs, CHARCS_TTL_DAYS
from services.card_quality_scorer import build_seller_scoring_context, product_to_card_input


def _make_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


class _FakeClient:
    def __init__(self):
        self.calls = []

    def get_card_characteristics_config(self, subject_id):
        self.calls.append(subject_id)
        return {'data': [
            {'name': 'Цвет', 'required': True, 'charcID': 1},
            {'name': 'Состав', 'required': False, 'charcID': 2},
        ]}


class TestCharcsCache(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def test_refresh_and_get(self):
        client = _FakeClient()
        refresh_subject_charcs(client, {5})
        self.assertEqual(client.calls, [5])
        charcs = get_available_charcs(5)
        self.assertEqual(charcs, [{'name': 'Цвет', 'required': True},
                                  {'name': 'Состав', 'required': False}])

    def test_fresh_cache_not_refetched(self):
        client = _FakeClient()
        refresh_subject_charcs(client, {5})
        refresh_subject_charcs(client, {5})
        self.assertEqual(client.calls, [5])  # второй раз из кэша

    def test_stale_cache_refetched(self):
        client = _FakeClient()
        refresh_subject_charcs(client, {5})
        row = db.session.get(WbSubjectCharcsCache, 5)
        row.fetched_at = datetime.utcnow() - timedelta(days=CHARCS_TTL_DAYS + 1)
        db.session.commit()
        refresh_subject_charcs(client, {5})
        self.assertEqual(client.calls, [5, 5])

    def test_wb_error_swallowed(self):
        class Boom:
            def get_card_characteristics_config(self, sid):
                raise RuntimeError('WB down')
        refresh_subject_charcs(Boom(), {7})  # не должно бросить
        self.assertIsNone(get_available_charcs(7))

    def test_get_missing_returns_none(self):
        self.assertIsNone(get_available_charcs(999))
        self.assertIsNone(get_available_charcs(None))


class TestScoringContext(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        long_desc = 'Одинаковое описание товара достаточной длины для проверки ' * 3
        for i in range(3):
            db.session.add(Product(seller_id=1, nm_id=100 + i, title='Товар',
                                   description=long_desc, subject_id=5, is_active=True))
        db.session.add(Product(seller_id=1, nm_id=200, title='Товар',
                               description='Уникальное описание достаточной длины для теста дубликатов ок', subject_id=5, is_active=True))
        db.session.add(WbSubjectCharcsCache(
            subject_id=5,
            charcs_json=json.dumps([{'name': 'Цвет', 'required': True}]),
            fetched_at=datetime.utcnow()))
        db.session.commit()
        self.long_desc = long_desc

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def test_context_marks_duplicates_and_charcs(self):
        ctx = build_seller_scoring_context(1)
        self.assertIn(self.long_desc.strip(), ctx['dup_descriptions'])
        self.assertEqual(ctx['charcs_by_subject'][5], [{'name': 'Цвет', 'required': True}])

    def test_card_input_gets_context_fields(self):
        ctx = build_seller_scoring_context(1)
        dup = Product.query.filter_by(nm_id=100).first()
        card = product_to_card_input(dup, ctx)
        self.assertTrue(card['description_dup'])
        self.assertEqual(card['available_charcs'], [{'name': 'Цвет', 'required': True}])
        uniq = Product.query.filter_by(nm_id=200).first()
        self.assertFalse(product_to_card_input(uniq, ctx)['description_dup'])

    def test_recompute_persists_reasons_and_impact(self):
        from services.card_quality_scorer import recompute_and_persist
        p = Product.query.filter_by(nm_id=100).first()
        cq = recompute_and_persist(p, capture_history=False,
                                   context=build_seller_scoring_context(1))
        self.assertIsInstance(cq['reasons'], list)
        self.assertIn('few_photos', p.attention_reasons)
        self.assertIsNotNone(p.quality_impact)
```

- [ ] **Step 2: Прогнать — падают**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_subject_charcs_cache.py`
Expected: FAIL, ModuleNotFoundError `services.subject_charcs_cache`.

- [ ] **Step 3: Новый сервис**

Создать `services/subject_charcs_cache.py`:

```python
# -*- coding: utf-8 -*-
"""Кэш конфигов характеристик категорий WB (таблица wb_subject_charcs_cache).

Хранит нормализованный список [{'name','required'}] на subject_id с TTL 7 дней.
Чтение — без WB; обновление — лениво, во время синка рейтингов.
"""
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger('subject_charcs_cache')

CHARCS_TTL_DAYS = 7


def get_available_charcs(subject_id):
    """Список [{'name','required'}] из кэша; None, если конфига нет/битый."""
    if not subject_id:
        return None
    from models import db, WbSubjectCharcsCache
    row = db.session.get(WbSubjectCharcsCache, int(subject_id))
    if not row or not row.charcs_json:
        return None
    try:
        data = json.loads(row.charcs_json)
    except (ValueError, TypeError):
        return None
    return data or None


def refresh_subject_charcs(wb_client, subject_ids, force=False):
    """Обновить кэш для протухших/отсутствующих subject_id.

    Ошибки WB не пробрасываются (warning в лог) — скоринг деградирует
    в fallback без конфига. Коммитит сессию сама.
    """
    from models import db, WbSubjectCharcsCache
    now = datetime.utcnow()
    cutoff = now - timedelta(days=CHARCS_TTL_DAYS)
    changed = False
    for sid in {int(s) for s in (subject_ids or []) if s}:
        row = db.session.get(WbSubjectCharcsCache, sid)
        if row and not force and row.fetched_at and row.fetched_at > cutoff:
            continue
        try:
            resp = wb_client.get_card_characteristics_config(sid)
        except Exception as e:
            logger.warning('charcs config fetch failed subject_id=%s: %s', sid, e)
            continue
        charcs = [
            {'name': c.get('name'), 'required': bool(c.get('required'))}
            for c in (resp or {}).get('data') or []
            if isinstance(c, dict) and c.get('name')
        ]
        if row is None:
            row = WbSubjectCharcsCache(subject_id=sid)
            db.session.add(row)
        row.charcs_json = json.dumps(charcs, ensure_ascii=False)
        row.fetched_at = now
        changed = True
    if changed:
        db.session.commit()
```

- [ ] **Step 4: Контекст и persist в скорере**

В `services/card_quality_scorer.py`:

1. Расширить `product_to_card_input` — сигнатура `def product_to_card_input(product, context=None) -> Dict[str, Any]:`; перед `return` собрать dict в переменную `card`, затем:

```python
    if context:
        desc = (getattr(product, 'description', '') or '').strip()
        card['description_dup'] = bool(
            desc and desc in (context.get('dup_descriptions') or ()))
        card['available_charcs'] = (context.get('charcs_by_subject') or {}).get(
            getattr(product, 'subject_id', None))
    return card
```

2. Добавить после `product_to_card_input`:

```python
DUP_DESCRIPTION_MIN_LEN = 50
DUP_DESCRIPTION_MIN_COUNT = 3


def build_seller_scoring_context(seller_id) -> Dict[str, Any]:
    """Контекст скоринга по каталогу продавца: дубликаты описаний + конфиги категорий.

    Один проход по (description, subject_id) активных карточек; конфиги — из кэша.
    """
    from models import db, Product
    from services.subject_charcs_cache import get_available_charcs

    rows = db.session.query(Product.description, Product.subject_id).filter(
        Product.seller_id == seller_id,
        Product.is_active == True,  # noqa: E712
    ).all()
    counter = {}
    subjects = set()
    for desc, subj in rows:
        if desc:
            key = desc.strip()
            if len(key) >= DUP_DESCRIPTION_MIN_LEN:
                counter[key] = counter.get(key, 0) + 1
        if subj:
            subjects.add(subj)
    dups = {k for k, v in counter.items() if v >= DUP_DESCRIPTION_MIN_COUNT}
    charcs = {s: get_available_charcs(s) for s in subjects}
    return {'dup_descriptions': dups, 'charcs_by_subject': charcs}
```

3. Переписать `recompute_and_persist`:

```python
def recompute_and_persist(product, capture_history: bool = True,
                          context=None) -> Dict[str, Any]:
    """Пересчитать Quality Score v2 карточки и записать в Product.

    Выставляет quality_score, quality_breakdown_json, attention_reasons (CSV),
    quality_impact, quality_checked_at. context=None → строится по продавцу
    (полный проход по каталогу; для батчей передавайте готовый context).
    НЕ делает commit — коммитит вызывающий код.
    """
    from models import db, CardRatingHistory

    if context is None and getattr(product, 'seller_id', None):
        context = build_seller_scoring_context(product.seller_id)

    card = product_to_card_input(product, context)
    cq = compute_card_quality(card)
    att = compute_attention(
        card, cq['dimensions'],
        nm_rating=getattr(product, 'nm_rating', None),
        feedback_rating=getattr(product, 'wb_feedback_rating', None),
        views_30d=getattr(product, 'wb_views_30d', None),
        orders_30d=getattr(product, 'wb_orders_30d', None),
        cart_conv=getattr(product, 'wb_cart_conv', None),
        buyout_rate=getattr(product, 'wb_buyout_rate', None),
    )

    product.quality_score = cq['score']
    product.quality_breakdown_json = json.dumps(cq['dimensions'], ensure_ascii=False)
    product.attention_reasons = ','.join(att['reasons'])
    product.quality_impact = att['impact']
    product.quality_checked_at = datetime.utcnow()

    if capture_history:
        db.session.add(CardRatingHistory(
            seller_id=getattr(product, 'seller_id', None),
            product_id=getattr(product, 'id', None),
            nm_id=getattr(product, 'nm_id', None),
            wb_product_rating=getattr(product, 'nm_rating', None),
            wb_feedback_rating=getattr(product, 'wb_feedback_rating', None),
            quality_score=cq['score'],
        ))

    cq['reasons'] = att['reasons']
    cq['impact'] = att['impact']
    return cq
```

4. В `card_quality_detail` перед `return` добавить в dict:

```python
        'attention_reasons': [r for r in (getattr(product, 'attention_reasons', None) or '').split(',') if r],
        'quality_impact': getattr(product, 'quality_impact', None),
        'wb_views_30d': getattr(product, 'wb_views_30d', None),
        'wb_orders_30d': getattr(product, 'wb_orders_30d', None),
        'wb_cart_conv': getattr(product, 'wb_cart_conv', None),
        'wb_buyout_rate': getattr(product, 'wb_buyout_rate', None),
```

- [ ] **Step 5: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_subject_charcs_cache.py tests/test_card_quality_scorer.py tests/test_card_quality_detail.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/subject_charcs_cache.py services/card_quality_scorer.py tests/test_subject_charcs_cache.py
git commit -m "feat(card-quality): кэш конфигов категорий + scoring context + persist причин"
```

---

### Task 6: Синк воронки продаж

**Files:**
- Modify: `services/product_sync_scheduler.py` (`parse_sales_funnel_ratings`:17-32 → `parse_sales_funnel_metrics`; `sync_card_ratings_for_seller`:381-455)
- Test: `tests/test_card_rating_sync.py`

**Interfaces:**
- Consumes: `refresh_subject_charcs`, `build_seller_scoring_context`, `product_to_card_input(product, context)`, `compute_card_quality`, `compute_attention` (Tasks 2-5).
- Produces: `parse_sales_funnel_metrics(api_response) -> {nm_id: {product_rating, feedback_rating, views, orders, cart_conv, order_conv, buyout_rate}}` (старое имя `parse_sales_funnel_ratings` удаляется).

- [ ] **Step 1: Обновить тесты парсера**

Переписать `tests/test_card_rating_sync.py` (заменить импорт и тесты старого парсера):

```python
# -*- coding: utf-8 -*-
"""Тесты парсера sales-funnel: рейтинги + метрики воронки."""
import unittest

from services.product_sync_scheduler import parse_sales_funnel_metrics


def _resp(products):
    return {'data': {'products': products}}


class TestParseSalesFunnelMetrics(unittest.TestCase):
    def test_full_item(self):
        resp = _resp([{
            'product': {'nmId': 42, 'productRating': 9.3, 'feedbackRating': 5},
            'statistics': {'selectedPeriod': {
                'openCardCount': 1200, 'ordersCount': 40,
                'conversions': {'addToCartPercent': 6.5,
                                'cartToOrderPercent': 40.0,
                                'buyoutsPercent': 55.0},
            }},
        }])
        out = parse_sales_funnel_metrics(resp)
        self.assertEqual(out[42], {
            'product_rating': 9.3, 'feedback_rating': 5,
            'views': 1200, 'orders': 40,
            'cart_conv': 6.5, 'order_conv': 40.0, 'buyout_rate': 55.0,
        })

    def test_missing_statistics(self):
        out = parse_sales_funnel_metrics(_resp([
            {'product': {'nmId': 7, 'productRating': 8.0, 'feedbackRating': 4.5}},
        ]))
        self.assertEqual(out[7]['product_rating'], 8.0)
        self.assertIsNone(out[7]['views'])
        self.assertIsNone(out[7]['cart_conv'])

    def test_empty_and_garbage(self):
        self.assertEqual(parse_sales_funnel_metrics({}), {})
        self.assertEqual(parse_sales_funnel_metrics({'data': {}}), {})
        self.assertEqual(parse_sales_funnel_metrics(_resp([{}, 'мусор', {'product': {}}])), {})

    def test_nmid_alt_key(self):
        out = parse_sales_funnel_metrics(_resp([{'product': {'nmID': 11}}]))
        self.assertIn(11, out)
```

- [ ] **Step 2: Прогнать — падают**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_rating_sync.py`
Expected: FAIL, ImportError `parse_sales_funnel_metrics`.

- [ ] **Step 3: Новый парсер**

В `services/product_sync_scheduler.py` заменить `parse_sales_funnel_ratings` (строки 17-32) на:

```python
def parse_sales_funnel_metrics(api_response: dict) -> dict:
    """Извлечь рейтинги и метрики воронки {nm_id: {...}} из ответа sales-funnel v3.

    Все поля опциональны (None = нет данных); структура ответа защищённо
    разбирается: statistics.selectedPeriod{...conversions{...}}.
    """
    out = {}
    data = (api_response or {}).get('data') or {}
    for item in data.get('products', []) or []:
        if not isinstance(item, dict):
            continue
        prod = item.get('product')
        if not isinstance(prod, dict):
            continue
        nm = prod.get('nmId', prod.get('nmID'))
        if nm is None:
            continue
        stats = item.get('statistics') if isinstance(item.get('statistics'), dict) else {}
        sel = stats.get('selectedPeriod') if isinstance(stats.get('selectedPeriod'), dict) else {}
        conv = sel.get('conversions') if isinstance(sel.get('conversions'), dict) else {}
        out[int(nm)] = {
            'product_rating': prod.get('productRating'),
            'feedback_rating': prod.get('feedbackRating'),
            'views': sel.get('openCardCount'),
            'orders': sel.get('ordersCount'),
            'cart_conv': conv.get('addToCartPercent'),
            'order_conv': conv.get('cartToOrderPercent'),
            'buyout_rate': conv.get('buyoutsPercent'),
        }
    return out
```

- [ ] **Step 4: Обновить sync_card_ratings_for_seller**

В `sync_card_ratings_for_seller`:

1. В импортах функции (строка ~389) заменить строку импорта скорера на:

```python
            from services.card_quality_scorer import (
                compute_card_quality, compute_attention, product_to_card_input,
                build_seller_scoring_context)
            from services.subject_charcs_cache import refresh_subject_charcs
```

2. В цикле батчей заменить `ratings = parse_sales_funnel_ratings(resp)` на `metrics = parse_sales_funnel_metrics(resp)` и тело цикла присвоений:

```python
                    metrics = parse_sales_funnel_metrics(resp)
                    for nm_id, m in metrics.items():
                        p = by_nm.get(nm_id)
                        if not p:
                            continue
                        if m['product_rating'] is not None:
                            p.nm_rating = m['product_rating']
                        if m['feedback_rating'] is not None:
                            p.wb_feedback_rating = m['feedback_rating']
                        p.nm_rating_checked_at = now
                        if m['views'] is not None:
                            p.wb_views_30d = int(m['views'])
                        if m['orders'] is not None:
                            p.wb_orders_30d = int(m['orders'])
                        if m['cart_conv'] is not None:
                            p.wb_cart_conv = float(m['cart_conv'])
                        if m['order_conv'] is not None:
                            p.wb_order_conv = float(m['order_conv'])
                        if m['buyout_rate'] is not None:
                            p.wb_buyout_rate = float(m['buyout_rate'])
                        p.funnel_checked_at = now
```

3. Блок «Пересчёт Quality Score» (строки ~437-447) заменить на:

```python
                # Ленивое обновление кэша конфигов категорий (TTL 7 дней)
                try:
                    refresh_subject_charcs(
                        client, {p.subject_id for p in products if p.subject_id})
                except Exception as e:
                    logger.warning(f"charcs cache refresh failed: {e}")

                # Пересчёт Quality Score v2 + причины + impact (дёшево, один контекст)
                context = build_seller_scoring_context(seller.id)
                for p in products:
                    card = product_to_card_input(p, context)
                    cq = compute_card_quality(card)
                    att = compute_attention(
                        card, cq['dimensions'],
                        nm_rating=p.nm_rating, feedback_rating=p.wb_feedback_rating,
                        views_30d=p.wb_views_30d, orders_30d=p.wb_orders_30d,
                        cart_conv=p.wb_cart_conv, buyout_rate=p.wb_buyout_rate,
                    )
                    p.quality_score = cq['score']
                    p.quality_breakdown_json = json.dumps(cq['dimensions'], ensure_ascii=False)
                    p.attention_reasons = ','.join(att['reasons'])
                    p.quality_impact = att['impact']
                    p.quality_checked_at = now
                    db.session.add(CardRatingHistory(
                        seller_id=seller.id, product_id=p.id, nm_id=p.nm_id,
                        wb_product_rating=p.nm_rating, wb_feedback_rating=p.wb_feedback_rating,
                        quality_score=cq['score'], captured_at=now,
                    ))
```

- [ ] **Step 5: Прогнать тесты + компиляция**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_rating_sync.py && python -m py_compile services/product_sync_scheduler.py && grep -rn "parse_sales_funnel_ratings" --include="*.py" . ; true`
Expected: PASS; grep не находит старое имя.

- [ ] **Step 6: Commit**

```bash
git add services/product_sync_scheduler.py tests/test_card_rating_sync.py
git commit -m "feat(card-quality): синк воронки продаж + пересчёт v2 с контекстом"
```

---

### Task 7: Summary v2 — reason_counts, trend, удаление is_weak

**Files:**
- Modify: `services/card_quality_scorer.py` (`is_weak`:208-221 удалить, `compute_quality_summary`:224-262 переписать)
- Test: `tests/test_card_quality_summary.py` (переписать), `tests/test_card_quality_summary_route.py` (проверить/поправить)

**Interfaces:**
- Produces: `compute_quality_summary(seller_id)` дополнительно возвращает `reason_counts: dict[str,int]`, `reason_labels: dict`, `trend: list[{'date','avg_quality'}]` (30 дней); `need_attention` = карточки с непустым `attention_reasons`. `is_weak`, `WEAK_QUALITY_THRESHOLD`, `WEAK_WB_RATING_THRESHOLD` удалены.

- [ ] **Step 1: Переписать тесты**

Заменить содержимое `tests/test_card_quality_summary.py`:

```python
# -*- coding: utf-8 -*-
"""Тест сводки качества карточек v2: причины, распределение, тренд."""
import json
import unittest
from datetime import datetime, timedelta

from flask import Flask

from models import db, Product, CardRatingHistory
from services.card_quality_scorer import compute_quality_summary, REASON_LABELS


def _make_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def _product(seller_id, nm_id, quality_score, nm_rating=None, reasons=''):
    return Product(
        seller_id=seller_id, nm_id=nm_id, title='Товар',
        photos_json=json.dumps([]), characteristics_json=json.dumps({}),
        quality_score=quality_score, nm_rating=nm_rating,
        attention_reasons=reasons, is_active=True,
    )


class TestQualitySummaryV2(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        db.session.add_all([
            _product(1, 101, 40.0, reasons='few_photos,weak_chars'),
            _product(1, 102, 65.0, reasons='no_views'),
            _product(1, 103, 90.0, reasons=''),
            _product(1, 104, None, reasons='no_sales_signal'),
            _product(2, 201, 30.0, reasons='few_photos'),  # чужой продавец
        ])
        now = datetime.utcnow()
        for d, score in ((2, 60.0), (1, 65.0), (0, 70.0)):
            db.session.add(CardRatingHistory(
                seller_id=1, nm_id=101, quality_score=score,
                captured_at=now - timedelta(days=d)))
        db.session.commit()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def test_need_attention_by_reasons(self):
        s = compute_quality_summary(1)
        self.assertEqual(s['need_attention'], 3)
        self.assertEqual(s['total'], 4)

    def test_reason_counts(self):
        s = compute_quality_summary(1)
        self.assertEqual(s['reason_counts']['few_photos'], 1)
        self.assertEqual(s['reason_counts']['weak_chars'], 1)
        self.assertEqual(s['reason_counts']['no_views'], 1)
        self.assertEqual(s['reason_counts']['no_sales_signal'], 1)
        self.assertEqual(s['reason_counts']['low_rating'], 0)

    def test_reason_labels_passthrough(self):
        self.assertEqual(compute_quality_summary(1)['reason_labels'], REASON_LABELS)

    def test_distribution_unchanged(self):
        s = compute_quality_summary(1)
        self.assertEqual(s['distribution'], {'poor': 1, 'average': 1, 'good': 0, 'excellent': 1})

    def test_trend_daily_avg(self):
        s = compute_quality_summary(1)
        self.assertEqual(len(s['trend']), 3)
        self.assertEqual(s['trend'][-1]['avg_quality'], 70.0)

    def test_tenant_scope(self):
        s = compute_quality_summary(2)
        self.assertEqual(s['need_attention'], 1)
        self.assertEqual(s['total'], 1)


class TestIsWeakRemoved(unittest.TestCase):
    def test_is_weak_gone(self):
        import services.card_quality_scorer as scorer
        self.assertFalse(hasattr(scorer, 'is_weak'))
```

- [ ] **Step 2: Прогнать — падают**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_quality_summary.py`
Expected: FAIL.

- [ ] **Step 3: Реализация**

В `services/card_quality_scorer.py` удалить `WEAK_QUALITY_THRESHOLD`, `WEAK_WB_RATING_THRESHOLD`, `is_weak`. Переписать `compute_quality_summary`:

```python
def compute_quality_summary(seller_id: int) -> Dict[str, Any]:
    """Сводка по качеству карточек продавца для кокпита и виджета дашборда.

    distribution — бакеты по score_status(quality_score);
    need_attention — карточки с непустым attention_reasons;
    reason_counts — счётчик карточек по каждой причине;
    trend — средний quality_score по дням за 30 дней (CardRatingHistory).
    """
    from datetime import timedelta
    from sqlalchemy import func
    from models import db, Product, CardRatingHistory

    rows = db.session.query(
        Product.quality_score, Product.nm_rating, Product.attention_reasons,
    ).filter(
        Product.seller_id == seller_id,
        Product.is_active == True,  # noqa: E712
    ).all()

    distribution = {'poor': 0, 'average': 0, 'good': 0, 'excellent': 0}
    reason_counts = {r: 0 for r in ATTENTION_REASONS}
    total = len(rows)
    need_attention = 0
    q_sum = q_cnt = r_sum = r_cnt = 0

    for quality_score, nm_rating, reasons_csv in rows:
        if quality_score is not None:
            distribution[score_status(quality_score)] += 1
            q_sum += quality_score
            q_cnt += 1
        if nm_rating is not None:
            r_sum += nm_rating
            r_cnt += 1
        reasons = [r for r in (reasons_csv or '').split(',') if r]
        if reasons:
            need_attention += 1
            for r in reasons:
                if r in reason_counts:
                    reason_counts[r] += 1

    since = datetime.utcnow() - timedelta(days=30)
    trend_rows = db.session.query(
        func.date(CardRatingHistory.captured_at),
        func.avg(CardRatingHistory.quality_score),
    ).filter(
        CardRatingHistory.seller_id == seller_id,
        CardRatingHistory.captured_at >= since,
        CardRatingHistory.quality_score.isnot(None),
    ).group_by(func.date(CardRatingHistory.captured_at)) \
     .order_by(func.date(CardRatingHistory.captured_at)).all()
    trend = [{'date': str(d), 'avg_quality': round(v, 1)}
             for d, v in trend_rows if v is not None]

    return {
        'avg_quality': round(q_sum / q_cnt, 1) if q_cnt else None,
        'avg_wb_rating': round(r_sum / r_cnt, 1) if r_cnt else None,
        'total': total,
        'need_attention': need_attention,
        'distribution': distribution,
        'reason_counts': reason_counts,
        'reason_labels': REASON_LABELS,
        'trend': trend,
    }
```

- [ ] **Step 4: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_quality_summary.py tests/test_card_quality_summary_route.py`
Expected: PASS (route-тест поправить, если ассертит старые ключи).

- [ ] **Step 5: Commit**

```bash
git add services/card_quality_scorer.py tests/test_card_quality_summary.py tests/test_card_quality_summary_route.py
git commit -m "feat(card-quality): summary v2 — счётчики причин и тренд, is_weak удалён"
```

---

### Task 8: List API — фильтры reason/bucket и сортировка impact

**Files:**
- Modify: `routes/card_quality.py` (`api_card_quality_list`:120-144, `_collect_bulk_candidates`:76-106)
- Test: `tests/test_card_quality_list_filters.py` (создать)

**Interfaces:**
- Consumes: `ATTENTION_REASONS` (Task 3), `Product.attention_reasons/quality_impact` (Task 4).
- Produces: `/api/card-quality/list?reason=<code>&bucket=<poor|average|good>&sort=<impact|quality_score|nm_rating|wb_feedback_rating>&order=&page=&per_page=`; дефолт `sort=impact&order=desc`. `_collect_bulk_candidates(seller_id, limit, product_ids=None)` — при `product_ids` берёт их (с tenant-фильтром), иначе топ по `quality_impact` среди карточек с причинами.

- [ ] **Step 1: Тест**

Создать `tests/test_card_quality_list_filters.py` (паттерн клиента/логина скопировать из `tests/test_card_quality_summary_route.py` — прочитать его перед написанием и использовать те же фикстуры создания app/seller/login; ниже — суть проверок):

```python
# Проверки (адаптировать под локальный паттерн авторизации из test_card_quality_summary_route.py):
# 1. GET /api/card-quality/list?reason=few_photos → только карточки, где
#    'few_photos' в attention_reasons; чужие seller_id не видны.
# 2. GET ...?bucket=poor → только quality_score < 50.
# 3. GET ...?sort=impact → первый элемент имеет max quality_impact.
# 4. reason вне ATTENTION_REASONS игнорируется (нет 500, фильтр не применяется).
#
# Данные: 3 карточки своего продавца (impact 5/20/40, разные reasons),
# 1 карточка чужого. Проверять по nm_id в ответе.
```

Полный тест писать по этому списку, соблюдая существующий паттерн создания приложения из route-тестов.

- [ ] **Step 2: Прогнать — падают**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_quality_list_filters.py`
Expected: FAIL (параметры не поддерживаются, сортировки impact нет).

- [ ] **Step 3: Реализация list**

В `api_card_quality_list` заменить блок сортировки/query (строки 126-135):

```python
            sort = request.args.get('sort', 'impact')
            order = request.args.get('order', 'desc' if sort == 'impact' else 'asc')
            page = request.args.get('page', 1, type=int)
            per_page = min(request.args.get('per_page', 50, type=int), 200)
            reason = request.args.get('reason')
            bucket = request.args.get('bucket')

            q = Product.query.filter_by(seller_id=current_user.seller.id, is_active=True)
            if reason in ATTENTION_REASONS:
                q = q.filter(Product.attention_reasons.like(f'%{reason}%'))
            if bucket == 'poor':
                q = q.filter(Product.quality_score < 50)
            elif bucket == 'average':
                q = q.filter(Product.quality_score >= 50, Product.quality_score < 70)
            elif bucket == 'good':
                q = q.filter(Product.quality_score >= 70)
            col = {'quality_score': Product.quality_score,
                   'nm_rating': Product.nm_rating,
                   'wb_feedback_rating': Product.wb_feedback_rating,
                   'impact': Product.quality_impact}.get(sort, Product.quality_impact)
            ordered = col.asc().nullslast() if order == 'asc' else col.desc().nullslast()
            q = q.order_by(ordered, Product.id.asc())
```

Импорт вверху файла: `from services.card_quality_scorer import card_quality_detail, compute_quality_summary, ATTENTION_REASONS`.

- [ ] **Step 4: Реализация bulk candidates**

Заменить `_collect_bulk_candidates` (сигнатура и base-query):

```python
def _collect_bulk_candidates(seller_id: int, limit: int = BULK_IMPROVE_LIMIT,
                             product_ids=None) -> dict:
    """Top-N карточек с причинами (или явно выбранные) + дифф поставщика."""
    base = Product.query.filter(
        Product.seller_id == seller_id, Product.is_active == True  # noqa: E712
    )
    if product_ids:
        base = base.filter(Product.id.in_(list(product_ids)[:limit]))
    else:
        base = base.filter(Product.attention_reasons.isnot(None),
                           Product.attention_reasons != '')
    total_weak = base.count()
    rows = base.order_by(Product.quality_impact.desc().nullslast()).limit(limit).all()
    # ... остальное тело без изменений ...
```

- [ ] **Step 5: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_quality_list_filters.py tests/test_card_quality_bulk.py`
Expected: PASS (bulk-тест поправить, если ассертит старый фильтр `<50 | <6`).

- [ ] **Step 6: Commit**

```bash
git add routes/card_quality.py tests/test_card_quality_list_filters.py tests/test_card_quality_bulk.py
git commit -m "feat(card-quality): фильтры по причинам и сортировка по impact в list API"
```

---

### Task 9: Удаление legacy-агентов из card-quality

**Files:**
- Modify: `routes/card_quality.py` (`api_card_quality_ai_analyze`:186-215 удалить; `api_card_quality_improve`:228-322 упростить; `api_card_quality_proposal`:324-376 упростить)
- Test: `tests/test_card_quality_improve_route.py`

**Interfaces:**
- Produces: `POST /api/card-quality/<id>/improve` → `{'success', 'weak_dims', 'supplier_diff'}` (без `task_ids`); `POST /api/card-quality/<id>/proposal` → как раньше, но `task_ids` в body игнорируются (proposal строится из standard photos + supplier diff). Endpoint `/ai-analyze` удалён (404).

- [ ] **Step 1: Обновить тесты**

В `tests/test_card_quality_improve_route.py` (прочитать файл, сохранить локальный паттерн):
- удалить/переписать тесты, ожидающие создание AgentTask и `task_ids` в ответе;
- добавить: improve возвращает `weak_dims` и `supplier_diff` без `task_ids`; `/ai-analyze` возвращает 404; proposal без body работает и возвращает standard-photos предложение при наличии медиа.

- [ ] **Step 2: Прогнать — падают**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_quality_improve_route.py`
Expected: FAIL.

- [ ] **Step 3: Реализация**

В `routes/card_quality.py`:
1. Удалить целиком роут `api_card_quality_ai_analyze` (строки 186-215).
2. В `api_card_quality_improve` удалить всё от комментария `# (b)/(c) диагностические агенты` до конца генеративных блоков (строки 247-316) и вернуть:

```python
            return jsonify({'success': True, 'weak_dims': weak_dims,
                            'supplier_diff': supplier_diff})
```

3. В `api_card_quality_proposal` удалить чтение `task_ids` из body и цикл сбора `task_results` (строки 333-340), заменить на `task_results = []` (вызов `build_proposal_from_tasks(product, [])` сохранить — он вернёт пустой proposal, который дополнится standard photos).
4. Удалить импорты `from services import agent_service` и `AgentTask` из строки импортов models (остальные импорты строки оставить).

- [ ] **Step 4: Прогнать тесты + компиляция**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_quality_improve_route.py tests/test_card_quality_apply_route.py && python -m py_compile routes/card_quality.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add routes/card_quality.py tests/test_card_quality_improve_route.py
git commit -m "refactor(card-quality): удалены вызовы legacy-агентов из раздела"
```

---

### Task 10: Bulk-страницы с предвыбранными карточками

**Files:**
- Modify: `routes/card_quality.py` (`card_quality_bulk_improve_page`:410-421, `card_quality_standard_photos_bulk_page`:511-545, `get_sparse_photo_candidates`:28-73)
- Test: `tests/test_card_quality_standard_photos_bulk.py`, `tests/test_card_quality_bulk.py`

**Interfaces:**
- Produces: GET `/card-quality/bulk-improve?ids=1,2,3` и GET `/card-quality/standard-photos-bulk?ids=1,2,3` — работают по выбранным ID (tenant-scoped, чужие молча отбрасываются); без `ids` — прежнее поведение (топ по impact / все sparse). `get_sparse_photo_candidates(seller, db_session, limit, product_ids=None)`.

- [ ] **Step 1: Тесты**

Добавить в соответствующие тест-файлы (паттерн — существующий в этих файлах):
- bulk-improve GET с `ids=` двух своих карточек → в candidates ровно они;
- `ids` с чужим product_id → чужой отсутствует в candidates;
- standard-photos GET с `ids=` → только выбранные sparse-карточки.

- [ ] **Step 2: Прогнать — падают**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_quality_bulk.py tests/test_card_quality_standard_photos_bulk.py`
Expected: FAIL (новые тесты).

- [ ] **Step 3: Реализация**

1. Хелпер парсинга ids (добавить рядом с константами роутов):

```python
def _parse_ids_param(raw: str, limit: int):
    ids = []
    for chunk in (raw or '').split(','):
        chunk = chunk.strip()
        if chunk.isdigit():
            ids.append(int(chunk))
    return ids[:limit] or None
```

2. В `card_quality_bulk_improve_page`:

```python
        product_ids = _parse_ids_param(request.args.get('ids', ''), BULK_IMPROVE_LIMIT)
        data = _collect_bulk_candidates(current_user.seller.id, BULK_IMPROVE_LIMIT,
                                        product_ids=product_ids)
```

3. В `get_sparse_photo_candidates` — сигнатура `def get_sparse_photo_candidates(seller, db_session, limit=STANDARD_PHOTOS_BULK_LIMIT, product_ids=None):`; query:

```python
    query = Product.query.filter_by(seller_id=seller.id, is_active=True)
    if product_ids:
        query = query.filter(Product.id.in_(product_ids))
    active_products = query.all()
```

4. В `card_quality_standard_photos_bulk_page` пробросить `product_ids=_parse_ids_param(request.args.get('ids', ''), STANDARD_PHOTOS_BULK_LIMIT)`.

- [ ] **Step 4: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_card_quality_bulk.py tests/test_card_quality_standard_photos_bulk.py tests/test_card_quality_standard_photos_improve.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add routes/card_quality.py tests/test_card_quality_bulk.py tests/test_card_quality_standard_photos_bulk.py
git commit -m "feat(card-quality): bulk-флоу принимают предвыбранные карточки"
```

---

### Task 11: UI раздела — очередь исправлений + ИИ-handoff

**Files:**
- Modify: `templates/card_quality.html` (сводка/списки: строки 33-148; JS `cardQualityPage()`: строки 360-449+; кнопки slideover: строки 205-215)

**Interfaces:**
- Consumes: `/api/card-quality/list?reason=&bucket=&sort=impact&page=` (Task 8), `summary.reason_counts/reason_labels` (Task 7), item-поля `attention_reasons`, `quality_impact` (Task 5); endpoints чата `POST /agents/api/conversations`, `POST /agents/api/conversations/<id>/messages` (НЕ менять), redirect `/agents?conversation=<id>`.
- Produces: очередь с фильтрами, чекбоксами и панелью действий; `fixWithAI(ids)`.

- [ ] **Step 1: Заменить блоки сводки и двух списков на очередь**

Вместо блоков «Требуют внимания»/«В порядке» (строки 91-148) — один блок:

```html
  {# ── Фильтры по причинам ── #}
  <div x-show="!loading" style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px">
    <button class="sh-btn sh-btn--sm"
            :class="!activeReason ? 'sh-btn--accent' : 'sh-btn--white-outline'"
            @click="setReason(null)">
      Все · <span x-text="summary.need_attention ?? 0"></span>
    </button>
    <template x-for="rc in reasonChips" :key="rc.code">
      <button class="sh-btn sh-btn--sm"
              :class="activeReason === rc.code ? 'sh-btn--accent' : 'sh-btn--white-outline'"
              @click="setReason(rc.code)"
              x-text="rc.label + ' · ' + rc.count"></button>
    </template>
  </div>

  {# ── Очередь исправлений ── #}
  <div x-show="!loading" class="sh-card" style="margin-bottom:20px">
    <div class="sh-card-header">
      <h3 class="sh-card-title">Очередь исправлений · <span x-text="items.length"></span></h3>
      <label style="font-size:12px;color:var(--text-muted);display:flex;align-items:center;gap:6px;cursor:pointer">
        <input type="checkbox" @change="toggleAll($event.target.checked)"> выбрать все
      </label>
    </div>
    <template x-if="items.length === 0">
      <div class="sh-empty"><p class="sh-empty-title">По этому фильтру карточек нет</p></div>
    </template>
    <template x-for="it in items" :key="it.product_id">
      <div class="cq-row">
        <input type="checkbox" :checked="!!selectedMap[it.product_id]"
               @change="toggleSelect(it.product_id)" style="flex-shrink:0">
        <div class="cq-thumb">
          <template x-if="firstPhoto(it)"><img :src="firstPhoto(it)" alt="" loading="lazy"></template>
          <template x-if="!firstPhoto(it)"><span class="cq-thumb-ph">нет фото</span></template>
        </div>
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:baseline;gap:8px">
            <span style="font-weight:600" x-text="it.title || ('Артикул ' + it.nm_id)"></span>
            <span style="font-size:12px;color:var(--text-muted)" x-text="it.nm_id"></span>
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px">
            <template x-for="r in (it.attention_reasons || [])" :key="r">
              <span class="sh-badge sh-badge--warning" x-text="reasonLabel(r)"></span>
            </template>
          </div>
        </div>
        <div style="text-align:right;flex-shrink:0">
          <div style="font-weight:600" x-text="'score ' + (it.quality_score ?? '—')"></div>
          <div style="font-size:12px;color:var(--accent)"
               x-show="it.quality_impact" x-text="'→ +' + Math.round(it.quality_impact)"></div>
        </div>
        <button class="sh-btn sh-btn--ghost sh-btn--sm" @click="openDetail(it.product_id)">Детали</button>
      </div>
    </template>
    <div style="display:flex;justify-content:center;gap:8px;padding:12px" x-show="pages > 1">
      <button class="sh-btn sh-btn--ghost sh-btn--sm" :disabled="page <= 1"
              @click="page--; load()">← Назад</button>
      <span style="font-size:12px;color:var(--text-muted);align-self:center"
            x-text="page + ' / ' + pages"></span>
      <button class="sh-btn sh-btn--ghost sh-btn--sm" :disabled="page >= pages"
              @click="page++; load()">Вперёд →</button>
    </div>
  </div>

  {# ── Панель массовых действий ── #}
  <div x-show="selectedCount > 0" x-cloak class="cq-actionbar">
    <span style="font-size:13px">Выбрано: <b x-text="selectedCount"></b></span>
    <a class="sh-btn sh-btn--white-outline sh-btn--sm"
       :href="'/card-quality/bulk-improve?ids=' + selectedList().join(',')">⚡ Данные поставщика</a>
    <a class="sh-btn sh-btn--white-outline sh-btn--sm"
       :href="'/card-quality/standard-photos-bulk?ids=' + selectedList().join(',')">📸 Дополнить фото</a>
    <button class="sh-btn sh-btn--accent sh-btn--sm" @click="fixWithAI()"
            :disabled="aiHandoffRunning"
            x-text="aiHandoffRunning ? 'Передаю…' : '🤖 Исправить с ИИ'"></button>
    <button class="sh-btn sh-btn--ghost sh-btn--sm" @click="clearSelection()">Сброс</button>
  </div>
```

CSS в `<style>` блок страницы (или в существующий):

```css
.cq-row { display:flex; align-items:center; gap:12px; padding:10px 0; border-top:1px solid var(--border); }
.cq-actionbar { position:sticky; bottom:16px; display:flex; align-items:center; gap:10px;
  background:var(--bg-card); border:1px solid var(--border); border-radius:8px;
  padding:10px 16px; box-shadow:0 4px 16px rgba(0,0,0,0.08); z-index:20; }
```

- [ ] **Step 2: JS-обновления в `cardQualityPage()`**

Добавить/заменить state и методы (геттеры weakItems/okItems удалить):

```js
    page: 1, pages: 1, activeReason: null,
    selectedMap: {}, aiHandoffRunning: false,
    get selectedCount() { return Object.keys(this.selectedMap).length; },
    get reasonChips() {
      const rc = (this.summary && this.summary.reason_counts) || {};
      const labels = (this.summary && this.summary.reason_labels) || {};
      return Object.entries(rc).filter(([, n]) => n > 0)
        .sort((a, b) => b[1] - a[1])
        .map(([code, count]) => ({ code, count, label: labels[code] || code }));
    },
    reasonLabel(code) {
      return ((this.summary && this.summary.reason_labels) || {})[code] || code;
    },
    setReason(code) {
      this.activeReason = code; this.page = 1; this.clearSelection(); this.load();
      const url = new URL(window.location); 
      if (code) url.searchParams.set('reason', code); else url.searchParams.delete('reason');
      history.replaceState(null, '', url);
    },
    toggleSelect(id) {
      if (this.selectedMap[id]) delete this.selectedMap[id]; else this.selectedMap[id] = true;
    },
    toggleAll(on) {
      this.selectedMap = {};
      if (on) this.items.forEach(it => { this.selectedMap[it.product_id] = true; });
    },
    clearSelection() { this.selectedMap = {}; },
    selectedList() { return Object.keys(this.selectedMap).map(Number); },
    async load() {
      this.loading = true;
      try {
        let url = '/api/card-quality/list?sort=impact&order=desc&per_page=50&page=' + this.page;
        if (this.activeReason) url += '&reason=' + encodeURIComponent(this.activeReason);
        const r = await fetch(url);
        const d = await r.json();
        this.items = d.items || []; this.summary = d.summary || {};
        this.pages = d.pages || 1;
      } finally {
        this.loading = false;
      }
    },
    async init() {
      const params = new URLSearchParams(window.location.search);
      this.activeReason = params.get('reason') || null;
      await this.load();
    },
    async fixWithAI(ids) {
      ids = ids || this.selectedList();
      if (!ids.length) return;
      if (ids.length > 50) {
        this.$store.toasts.info('Выберите не более 50 карточек за раз');
        return;
      }
      this.aiHandoffRunning = true;
      try {
        const hdrs = {'Content-Type': 'application/json', 'X-CSRFToken': '{{ csrf_token() }}'};
        const cr = await fetch('/agents/api/conversations', {
          method: 'POST', headers: hdrs,
          body: JSON.stringify({title: 'Качество карточек'}),
        });
        const cd = await cr.json();
        const cid = cd.conversation && cd.conversation.id;
        if (!cid) { this.$store.toasts.error('Не удалось создать диалог'); return; }
        const chosen = this.items.filter(it => ids.includes(it.product_id));
        const arts = chosen.map(it => it.nm_id || it.vendor_code).filter(Boolean)
          .slice(0, 10).join(', ');
        const reasons = new Set();
        chosen.forEach(it => (it.attention_reasons || [])
          .forEach(r => reasons.add(this.reasonLabel(r))));
        const message = 'Улучши карточки (' + ids.length + ' шт.)' +
          (arts ? ': артикулы ' + arts : '') +
          (reasons.size ? '. Основные проблемы: ' + Array.from(reasons).join(', ') : '') +
          '. Составь план исправления контента.';
        const mr = await fetch('/agents/api/conversations/' + cid + '/messages', {
          method: 'POST', headers: hdrs,
          body: JSON.stringify({message: message, product_ids: ids, entity_kind: 'product'}),
        });
        if (!mr.ok) {
          const err = await mr.json().catch(() => ({}));
          this.$store.toasts.error(err.error || 'Не удалось передать карточки помощнику');
          return;
        }
        window.location.href = '/agents?conversation=' + cid;
      } finally {
        this.aiHandoffRunning = false;
      }
    },
```

В slideover заменить кнопку «🤖 Глубокий AI-анализ» (строка 209) на:

```html
<button class="sh-btn sh-btn--primary sh-btn--sm" @click="fixWithAI([detail.product_id])"
        :disabled="aiHandoffRunning">🤖 Исправить с ИИ</button>
```

Удалить метод `aiAnalyze` и связанные `aiRunning/aiStatus/_poll`. Метод `startImprove` оставить (детерминированный supplier-diff/standard-photos флоу) — но убрать из него ожидание `task_ids`, если есть (ответ improve теперь `{success, weak_dims, supplier_diff}`; после него сразу вызывать `/proposal` без body).

- [ ] **Step 3: Компиляция шаблона и ручная проверка**

Run: `SKIP_SCHEDULER=1 python - <<'EOF'
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('templates'))
env.get_template('card_quality.html')
print('template OK')
EOF`
Expected: `template OK` (синтаксис Jinja валиден; `csrf_token`/`url_for` не вызываются при parse).

Ручная проверка (локальный запуск по AGENTS.md): фильтры переключаются и меняют URL, deep-link `?reason=few_photos` открывает фильтр, чекбоксы + панель действий работают, обе темы, mobile (панель не перекрывает контент), пустое состояние фильтра.

- [ ] **Step 4: Commit**

```bash
git add templates/card_quality.html
git commit -m "feat(card-quality): очередь исправлений с фильтрами причин и ИИ-handoff"
```

---

### Task 12: Виджет дашборда

**Files:**
- Modify: `templates/dashboard.html` (CSS: строки 103-135, markup: строки 341-372, JS `cardQualityWidget()`: строки 599-620)

**Interfaces:**
- Consumes: `/api/card-quality/summary` → `data.{need_attention, avg_quality, distribution, reason_counts, reason_labels, trend}` (Task 7).

- [ ] **Step 1: Markup**

Заменить блок `.dash-quality` (строки 341-372):

```html
    <div class="dash-quality" x-data="cardQualityWidget()" x-init="load()">
        <div class="dash-quality-body">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap">
                <div class="dash-quality-headline">
                    <span class="dash-quality-count" x-text="loading ? '…' : (data ? data.need_attention : 0)"></span>
                    <span class="dash-quality-headline-text">карточек теряют продажи</span>
                </div>
                <div style="display:flex;align-items:center;gap:10px" x-show="!loading && data">
                    <svg x-show="sparkPoints()" width="120" height="36" viewBox="0 0 120 36"
                         fill="none" aria-hidden="true">
                        <polyline :points="sparkPoints()" stroke="var(--accent)" stroke-width="2"
                                  fill="none" stroke-linejoin="round" stroke-linecap="round"/>
                    </svg>
                    <div style="text-align:center">
                        <div style="font-family:var(--font-display);font-style:italic;font-size:28px;line-height:1"
                             :style="'color:' + gaugeColor()"
                             x-text="data && data.avg_quality !== null ? Math.round(data.avg_quality) : '—'"></div>
                        <div style="font-size:10px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em">Quality Score</div>
                    </div>
                </div>
            </div>
            <div class="dash-quality-chips" x-show="!loading && topReasons().length">
                <template x-for="rc in topReasons()" :key="rc.code">
                    <a :href="'/card-quality?reason=' + rc.code" class="dash-quality-chip"
                       x-text="rc.label + ' · ' + rc.count"></a>
                </template>
            </div>
            <div class="dash-quality-bar" x-show="!loading && data && data.total">
                <template x-for="seg in segments()" :key="seg.key">
                    <a class="dash-quality-bar-seg" :href="'/card-quality?bucket=' + seg.bucket"
                       :style="'flex:' + seg.count + ';background:' + seg.color"
                       :title="seg.label + ': ' + seg.count"></a>
                </template>
            </div>
            <div class="dash-quality-legend" x-show="!loading && data && data.total">
                <span><span class="dash-dot" style="background:#dc2626"></span> Слабые: <span x-text="data ? data.distribution.poor : 0"></span></span>
                <span><span class="dash-dot" style="background:#d97706"></span> Средние: <span x-text="data ? data.distribution.average : 0"></span></span>
                <span><span class="dash-dot" style="background:#16a34a"></span> Хорошие: <span x-text="data ? (data.distribution.good + data.distribution.excellent) : 0"></span></span>
            </div>
            <a href="{{ url_for('card_quality_page') }}" class="dash-quality-cta">Исправить &rarr;</a>
        </div>
    </div>
```

- [ ] **Step 2: CSS**

В стилях (строки 103-135): удалить `.dash-quality-gauge`, `.dash-quality-gauge-label`; в `.dash-quality-bar` заменить `background: #ece9e4` на `background: var(--border)`; добавить:

```css
    .dash-quality-chips { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }
    .dash-quality-chip {
        font-size: 12px; font-weight: 600; color: var(--text-secondary);
        border: 1px solid var(--border); border-radius: 8px; padding: 4px 10px;
        text-decoration: none; transition: border-color 0.15s, color 0.15s;
    }
    .dash-quality-chip:hover { color: var(--accent); border-color: var(--accent); }
```

- [ ] **Step 3: JS**

Заменить `cardQualityWidget()` (строки 599-620):

```js
function cardQualityWidget() {
    return {
        loading: true,
        data: null,
        load() {
            fetch('/api/card-quality/summary')
                .then(r => r.json())
                .then(json => { if (json.success) this.data = json.data; })
                .catch(e => console.warn('Card quality widget error:', e))
                .finally(() => { this.loading = false; });
        },
        gaugeColor() {
            const v = this.data ? this.data.avg_quality : 0;
            return v >= 70 ? '#16a34a' : v >= 50 ? '#d97706' : '#dc2626';
        },
        topReasons() {
            if (!this.data || !this.data.reason_counts) return [];
            const labels = this.data.reason_labels || {};
            return Object.entries(this.data.reason_counts)
                .filter(([, n]) => n > 0)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 4)
                .map(([code, count]) => ({ code, count, label: labels[code] || code }));
        },
        sparkPoints() {
            const t = (this.data && this.data.trend) || [];
            if (t.length < 2) return '';
            const vals = t.map(p => p.avg_quality);
            const min = Math.min(...vals), max = Math.max(...vals);
            const range = (max - min) || 1;
            const W = 120, H = 36, PAD = 4;
            return vals.map((v, i) =>
                (i / (vals.length - 1) * W).toFixed(1) + ',' +
                (H - PAD - (v - min) / range * (H - 2 * PAD)).toFixed(1)
            ).join(' ');
        },
        segments() {
            if (!this.data) return [];
            const d = this.data.distribution;
            return [
                { key: 'poor', bucket: 'poor', label: 'Слабые', color: '#dc2626', count: d.poor || 0 },
                { key: 'average', bucket: 'average', label: 'Средние', color: '#d97706', count: d.average || 0 },
                { key: 'good', bucket: 'good', label: 'Хорошие', color: '#16a34a', count: (d.good || 0) + (d.excellent || 0) },
            ].filter(s => s.count > 0);
        },
    };
}
```

(если существующий `segments()` отличается по хвосту — заменить целиком функцией выше).

- [ ] **Step 4: Проверка шаблона + ручная**

Run: тот же Jinja-parse скрипт, что в Task 11, для `dashboard.html`.
Expected: `template OK`.

Ручная проверка: обе темы (чипы/полоска читабельны на dark), клик по чипу открывает `/card-quality?reason=…` с включённым фильтром, спарклайн не рисуется при <2 точках тренда, mobile без горизонтального скролла.

- [ ] **Step 5: Commit**

```bash
git add templates/dashboard.html
git commit -m "feat(dashboard): виджет качества — причины-чипы, спарклайн, deep-links"
```

---

### Task 13: Quality-данные в агентном рантайме — internal API, клиент, tool, skill

**Files:**
- Modify: `routes/internal_api.py` (новый endpoint после `internal_query_products`, ~строка 414)
- Modify: `agents/platform_client.py` (метод рядом с `query_products`, ~строка 294)
- Modify: `agents/tools.py` (регистрация в `create_platform_tools`, ~строка 147+)
- Modify: `agents/unified.py` (новый skill-класс после `CatalogQuerySkill`; регистрация в `SKILL_CLASSES`; запись в `skill_catalog` внутри `_plan_request`, ~строка 1061)
- Test: `tests/test_internal_card_quality_api.py` (создать), `tests/test_unified_quality_audit.py` (создать)

**Interfaces:**
- Consumes: `Product.attention_reasons/quality_impact/wb_*` (Tasks 4-6), `ATTENTION_REASONS`, `REASON_LABELS` (Task 3).
- Produces:
  - `POST /sellers/<id>/products/quality-brief` (internal, agent-auth) → `{'reason_labels', 'total', 'products': [{id, nm_id, vendor_code, title, quality_score, quality_impact, attention_reasons, wb_rating, wb_views_30d, wb_orders_30d, wb_cart_conv, wb_buyout_rate, recommendations}]}`;
  - `PlatformClient.get_card_quality_brief(seller_id, product_ids=None, reason=None, limit=30) -> dict`;
  - tool `get_card_quality` (read-only) в `create_platform_tools`;
  - skill `quality-audit` (класс `QualityAuditSkill`, детерминированный, без LLM) в `SKILL_CLASSES` и каталоге планировщика.

- [ ] **Step 1: Тест internal API**

Создать `tests/test_internal_card_quality_api.py`. СНАЧАЛА прочитать `tests/test_internal_agent_security.py` и скопировать оттуда паттерн создания app, агента, ключа и назначенной задачи (auth-заголовки, фикстуры). Проверки:

```python
# 1. Без agent-ключа → 401/403 (как в соседних тестах security-файла).
# 2. Задача назначена продавцу 1, запрос quality-brief для продавца 2 → отказ
#    (тот же код, что у internal_list_products при чужом seller_id).
# 3. Успех: у продавца 1 две карточки с attention_reasons='few_photos,weak_chars'
#    (quality_impact 30.0) и 'no_views' (impact 10.0) → без body возвращаются обе,
#    первая — с большим impact; в элементе есть attention_reasons (list),
#    quality_impact, wb_views_30d, recommendations (list).
# 4. body {'product_ids': [id2]} → только id2.
# 5. ?reason=few_photos → только первая; ?reason=abrakadabra → 400.
# 6. В ответе НЕТ полей price/quantity/credentials.
```

- [ ] **Step 2: Прогнать — падает**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_internal_card_quality_api.py`
Expected: FAIL (404 на endpoint).

- [ ] **Step 3: Endpoint**

В `routes/internal_api.py` после `internal_query_products` добавить (декораторы и `_assigned_task_for_seller` — те же, что у соседей; `html`, `json`, `func` уже импортированы в файле — проверить):

```python
@internal_api_bp.route('/sellers/<int:seller_id>/products/quality-brief', methods=['POST'])
@_authenticate_agent
def internal_products_quality_brief(seller_id):
    """Качество карточек для агентного рантайма (read-only).

    Body {'product_ids': [...]} (до 50) — явная выборка; без него — топ
    проблемных по quality_impact, опционально ?reason=<код>. Protected
    fields (цены/остатки/ключи) не возвращаются.
    """
    _, error = _assigned_task_for_seller(seller_id)
    if error:
        return error
    from services.card_quality_scorer import ATTENTION_REASONS, REASON_LABELS

    body = request.get_json(silent=True) or {}
    raw_ids = body.get('product_ids') or []
    if not isinstance(raw_ids, list):
        return jsonify({'error': 'product_ids must be a list'}), 400
    ids = [int(x) for x in raw_ids if str(x).isdigit()][:50]
    reason = (request.args.get('reason') or '').strip()
    if reason and reason not in ATTENTION_REASONS:
        return jsonify({'error': 'unknown reason'}), 400
    limit = min(max(request.args.get('limit', 30, type=int), 1), 50)

    q = Product.query.filter_by(seller_id=seller_id, is_active=True)
    if ids:
        q = q.filter(Product.id.in_(ids))
    else:
        q = q.filter(Product.attention_reasons.isnot(None),
                     Product.attention_reasons != '')
        if reason:
            q = q.filter(Product.attention_reasons.like(f'%{reason}%'))
    rows = q.order_by(Product.quality_impact.desc().nullslast()).limit(limit).all()

    def _top_recommendations(p):
        try:
            dims = json.loads(p.quality_breakdown_json) if p.quality_breakdown_json else {}
        except (ValueError, TypeError):
            dims = {}
        cand = [(d.get('weight', 0) * (100 - d.get('score', 0)), d.get('hint'))
                for d in dims.values() if isinstance(d, dict) and d.get('hint')]
        cand.sort(key=lambda t: -t[0])
        return [hint for _, hint in cand[:3]]

    return jsonify({
        'reason_labels': REASON_LABELS,
        'total': len(rows),
        'products': [{
            'id': p.id,
            'nm_id': p.nm_id,
            'vendor_code': p.vendor_code,
            'title': html.unescape(p.title or '')[:180],
            'quality_score': p.quality_score,
            'quality_impact': p.quality_impact,
            'attention_reasons': [r for r in (p.attention_reasons or '').split(',') if r],
            'wb_rating': p.nm_rating,
            'wb_views_30d': p.wb_views_30d,
            'wb_orders_30d': p.wb_orders_30d,
            'wb_cart_conv': p.wb_cart_conv,
            'wb_buyout_rate': p.wb_buyout_rate,
            'recommendations': _top_recommendations(p),
        } for p in rows],
    })
```

- [ ] **Step 4: Клиент + tool**

В `agents/platform_client.py` рядом с `query_products` (путь и стиль `_request` скопировать у соседа — префикс URL взять точно такой же, как у `query_products`):

```python
    def get_card_quality_brief(self, seller_id: int, product_ids=None,
                               reason: str = None, limit: int = 30) -> dict:
        """Качество карточек: причины, impact, воронка (read-only, до 50)."""
        params = {'limit': min(int(limit or 30), 50)}
        if reason:
            params['reason'] = reason
        payload = {}
        if product_ids:
            payload['product_ids'] = _validated_product_ids(product_ids, 50)
        return self._request(
            'POST', f'<тот-же-префикс-что-у-query_products>/sellers/{seller_id}/products/quality-brief',
            params=params, json=payload)
```

В `agents/tools.py` внутри `create_platform_tools` (после блока `get_product`):

```python
    registry.register(
        name='get_card_quality',
        description=('Качество карточек WB (read-only): Quality Score, причины «требует '
                     'внимания» (мало фото, слабые характеристики/описание/заголовок, '
                     'нет просмотров, низкая конверсия/выкуп/рейтинг), потенциал фикса '
                     'и метрики воронки за 30 дней. Либо по product_ids (до 50), '
                     'либо топ проблемных, опционально по причине.'),
        parameters={
            'properties': {
                'seller_id': {'type': 'integer', 'description': 'ID продавца'},
                'product_ids': {'type': 'array', 'items': {'type': 'integer'},
                                'description': 'ID карточек Product (до 50)'},
                'reason': {'type': 'string',
                           'enum': ['few_photos', 'weak_chars', 'weak_description',
                                    'weak_title', 'no_views', 'low_cart_conv',
                                    'low_buyout', 'low_rating', 'no_sales_signal'],
                           'description': 'Фильтр по одной причине'},
                'limit': {'type': 'integer',
                          'description': 'Максимум карточек (default 30, max 50)'},
            },
            'required': ['seller_id'],
        },
        handler=lambda seller_id, product_ids=None, reason=None, limit=30:
            platform_client.get_card_quality_brief(seller_id, product_ids, reason, limit),
    )
```

Least privilege сохраняется автоматически: skills с `tool_allowlist=()` инструментов не получают; добавлять `get_card_quality` в allowlist существующих skills НЕ нужно.

- [ ] **Step 5: Тест skill**

Создать `tests/test_unified_quality_audit.py`:

```python
# -*- coding: utf-8 -*-
"""Тесты детерминированного skill quality-audit."""
import unittest

from agents.config import AgentConfig
from agents.unified import QualityAuditSkill, SKILL_CLASSES


class _FakePlatform:
    def __init__(self, products):
        self._products = products
        self.calls = []

    def get_card_quality_brief(self, seller_id, product_ids=None, reason=None, limit=30):
        self.calls.append((seller_id, product_ids, reason, limit))
        return {
            'reason_labels': {'few_photos': 'Мало фото', 'no_views': 'Нет просмотров'},
            'total': len(self._products),
            'products': self._products,
        }


def _make_skill(products):
    skill = QualityAuditSkill(AgentConfig(agent_id='t', api_key='t',
                                          platform_url='http://x'))
    skill.platform = _FakePlatform(products)
    return skill


class TestQualityAuditSkill(unittest.TestCase):
    def test_registered(self):
        self.assertIn('quality-audit', SKILL_CLASSES)

    def test_aggregates_reasons_and_collection(self):
        products = [
            {'id': 1, 'nm_id': 10, 'attention_reasons': ['few_photos', 'no_views'],
             'quality_impact': 30.0},
            {'id': 2, 'nm_id': 20, 'attention_reasons': ['few_photos'],
             'quality_impact': 20.0},
        ]
        skill = _make_skill(products)
        res = skill.execute_task({'id': 't1', 'seller_id': 1, 'input_data': {}})
        self.assertEqual(res['status'], 'completed')
        self.assertEqual(res['selected_product_ids'], [1, 2])
        self.assertEqual(res['entity_kind'], 'product')
        self.assertEqual(res['reason_summary'][0]['reason'], 'few_photos')
        self.assertEqual(res['reason_summary'][0]['count'], 2)
        self.assertIn('Мало фото', res['message'])
        self.assertEqual(res['_usage']['api_requests'], 1)
        self.assertEqual(res['_usage']['total_tokens'], 0)

    def test_empty_selection(self):
        skill = _make_skill([])
        res = skill.execute_task({'id': 't1', 'seller_id': 1, 'input_data': {}})
        self.assertEqual(res['status'], 'completed')
        self.assertEqual(res['selected_product_ids'], [])

    def test_params_passthrough(self):
        skill = _make_skill([])
        skill.execute_task({'id': 't1', 'seller_id': 7, 'input_data': {
            'params': {'product_ids': [5, 6], 'reason': 'few_photos', 'limit': 10}}})
        self.assertEqual(skill.platform.calls[0], (7, [5, 6], 'few_photos', 10))
```

Если конструктор `AgentConfig`/`BaseAgent` в этом репо требует другие аргументы — скопировать инициализацию skill из существующего `tests/test_orchestrator.py` (прочитать перед написанием). `parse_input_data` — метод BaseAgent; передавать `input_data` в том формате, который он ожидает (проверить по `tests/test_base_agent.py`).

- [ ] **Step 6: Skill + регистрация**

В `agents/unified.py` после `CatalogQuerySkill` добавить:

```python
class QualityAuditSkill(BaseAgent):
    """Deterministic card-quality audit: причины, приоритеты, кандидаты на фикс."""

    agent_name = 'quality-audit'
    max_iterations = 1
    tool_allowlist = ()
    system_prompt = 'Read-only deterministic card quality audit.'

    def build_task_prompt(self, task: dict) -> str:
        return 'Используй типизированный execute_task.'

    def execute_task(self, task: dict) -> dict:
        data = self.parse_input_data(task)
        params = data.get('params') or {}
        product_ids = params.get('product_ids') or data.get('product_ids') or None
        reason = params.get('reason') or None
        limit = int(params.get('limit') or 30)

        brief = self.platform.get_card_quality_brief(
            int(task['seller_id']), product_ids, reason, limit)
        products = brief.get('products') or []
        labels = brief.get('reason_labels') or {}
        usage = {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0,
                 'api_requests': 1, 'mode': 'deterministic_aggregate'}

        if not products:
            return {'status': 'completed',
                    'message': 'Проблемных карточек по заданному фильтру не найдено.',
                    'total': 0, 'reason_summary': [], 'cards': [],
                    'selected_product_ids': [], 'entity_kind': 'product',
                    '_usage': usage}

        reason_counter = {}
        for p in products:
            for r in p.get('attention_reasons') or []:
                reason_counter[r] = reason_counter.get(r, 0) + 1
        ordered = sorted(reason_counter.items(), key=lambda t: (-t[1], t[0]))
        top = '; '.join(f'{labels.get(r, r)}: {n}' for r, n in ordered[:4])
        return {
            'status': 'completed',
            'message': (f'Карточек с проблемами: {len(products)}. Главное: {top}. '
                        'Первые в списке — с наибольшим потенциалом фикса.'),
            'total': len(products),
            'reason_summary': [
                {'reason': r, 'label': labels.get(r, r), 'count': n}
                for r, n in ordered],
            'cards': products,
            'selected_product_ids': [p['id'] for p in products],
            'entity_kind': 'product',
            '_usage': usage,
        }
```

Найти `SKILL_CLASSES` (grep) и добавить `'quality-audit': QualityAuditSkill,`. В `_plan_request` в `skill_catalog` добавить строку:

```python
            'quality-audit': ('audit_card_quality', 'Качество карточек WB: причины и приоритеты фикса'),
```

- [ ] **Step 7: Прогнать тесты + компиляция**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_internal_card_quality_api.py tests/test_unified_quality_audit.py tests/test_orchestrator.py tests/test_tools.py tests/test_internal_agent_security.py && python -m py_compile routes/internal_api.py agents/platform_client.py agents/tools.py agents/unified.py`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add routes/internal_api.py agents/platform_client.py agents/tools.py agents/unified.py tests/test_internal_card_quality_api.py tests/test_unified_quality_audit.py
git commit -m "feat(agents): skill quality-audit + tool get_card_quality поверх internal quality-brief"
```

---

### Task 14: AGENTS.md + полный прогон

**Files:**
- Modify: `AGENTS.md` (карта репозитория / safety-разделы)

- [ ] **Step 1: Обновить AGENTS.md**

В раздел «Карта репозитория» после строки про `services/` контекст не добавлять; вместо этого в раздел «Единый AI-помощник» дописать абзац:

```markdown
Раздел «Качество карточек» (`routes/card_quality.py`, `services/card_quality_scorer.py`,
`services/subject_charcs_cache.py`) не вызывает legacy-агентов. Quality Score v2 —
детерминированный: контент относительно конфига категории WB (кэш
`wb_subject_charcs_cache`, TTL 7 дней) плюс метрики воронки продаж, которые парсятся
из того же ответа sales-funnel, что и рейтинги (без дополнительных API-вызовов).
Причины «требует внимания» хранятся CSV в `Product.attention_reasons`
(коды в `card_quality_scorer.ATTENTION_REASONS`), приоритет — `Product.quality_impact`.
Кнопка «Исправить с ИИ» передаёт выбранные карточки в единый чат только через
существующие endpoints (`POST /agents/api/conversations`, `.../messages` с
`entity_kind='product'` + `product_ids`, лимит 50); write-путь остаётся
план → подтверждение → proposal. Рантайм читает quality-данные через
read-only internal endpoint `products/quality-brief` (agent-auth, до 50
карточек, protected fields не возвращаются): детерминированный skill
`quality-audit` агрегирует причины и отдаёт приоритетные карточки как
collection (`selected_product_ids` + `entity_kind='product'`), tool
`get_card_quality` доступен ReAct-skills только через allowlist.
```

В разделе «База данных и миграции» ничего менять не нужно (новый скрипт следует общим правилам). Проверить, что упоминаний `is_weak`/старых порогов в AGENTS.md нет (их нет).

- [ ] **Step 2: Полный прогон тестов**

Run: `SKIP_SCHEDULER=1 python -m pytest -q`
Expected: PASS (или падения, существовавшие до этой работы — сверить с `git stash`-прогоном не нужно, зафиксировать список в отчёте).

Run: `git diff --check`
Expected: пусто.

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs(agents): card-quality v2 — алгоритм, причины, ИИ-handoff"
```

---

## Self-Review (выполнен при написании)

- Спека ↔ задачи: скорер v2 (T1-T2), причины/impact (T3), колонки+кэш+миграция (T4), контекст+persist (T5), синк воронки (T6), summary/trend (T7), list-фильтры (T8), удаление legacy (T9), bulk ids (T10), очередь+handoff (T11), виджет (T12), quality-данные в агентном рантайме — блок 5 спеки (T13), AGENTS.md (T14). Разрыв со спекой: `page_context` в handoff НЕ передаётся (осознанно — свежий диалог не привязан к странице, `entity_kind` передаётся явно; это строже инварианта, отражено в T11).
- Имена сквозные: `parse_sales_funnel_metrics`, `compute_attention`, `build_seller_scoring_context`, `get_available_charcs`, `refresh_subject_charcs`, `ATTENTION_REASONS`, `REASON_LABELS`, `attention_reasons` (CSV), `quality_impact`, `get_card_quality_brief`, tool `get_card_quality`, skill `quality-audit` — согласованы между задачами.
- Кросс-влияние: `products.html`/`test_products_quality_filter.py` не используют `is_weak` (проверено grep) — не трогаем.
