# -*- coding: utf-8 -*-
"""
Фильтр запрещённых слов для Wildberries.

WB блокирует товары, содержащие ненормативную лексику в латинской транскрипции
в заголовках и описаниях. Этот модуль заменяет такие слова на допустимые
русскоязычные эквиваленты.

Поддержка:
- Дефолтный словарь (код) + слова из БД (админ + продавец)
- Поиск по подстроке для "токсичных" корней (cock, fuck, vagina и т.д.)
- Поиск по целым словам для коротких/неоднозначных слов (ass, tit, cum)
"""
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ============================================================================
# СЛОВАРЬ ЗАМЕН: запрещённое слово (lowercase) → допустимая замена
# ============================================================================

PROHIBITED_WORDS_REPLACEMENTS = {
    # Анатомические термины
    'cock': 'Член',
    'cocks': 'Члены',
    'cockring': 'Эрекционное кольцо',
    'cock ring': 'Эрекционное кольцо',
    'cock-ring': 'Эрекционное кольцо',
    'vagina': 'Вагина',
    'vaginal': 'Вагинальный',
    'penis': 'Фаллос',
    'penises': 'Фаллосы',
    'dick': 'Фаллос',
    'dicks': 'Фаллосы',
    'pussy': 'Вагина',
    'anus': 'Анус',
    'anal': 'Анальный',
    'ass': 'Попка',
    'boobs': 'Грудь',
    'boob': 'Грудь',
    'tits': 'Грудь',
    'tit': 'Грудь',
    'nipple': 'Сосок',
    'nipples': 'Соски',
    'clitoris': 'Клитор',
    'clitoral': 'Клиторальный',
    'clit': 'Клитор',
    'orgasm': 'Оргазм',
    'erection': 'Эрекция',
    'erectile': 'Эректильный',
    'erotic': 'Эротический',
    'erotica': 'Эротика',
    'genital': 'Генитальный',
    'genitals': 'Гениталии',
    'testicle': 'Яичко',
    'testicles': 'Яички',
    'scrotum': 'Мошонка',
    'prostate': 'Простата',
    'masturbator': 'Мастурбатор',
    'masturbation': 'Мастурбация',
    'masturbate': 'Мастурбация',

    # Обсценная лексика — удаляем
    'fuck': '',
    'fucked': '',
    'fucker': '',
    'fucking': '',
    'fuсk': '',       # с кириллической 'с'
    'fck': '',
    'f*ck': '',
    'f**k': '',
    'shit': '',
    'bitch': '',
    'whore': '',
    'slut': '',
    'bastard': '',
    'damn': '',
    'cunt': '',
    'cum': 'Крем',
    'cumshot': '',
    'sperm': 'Сперма',
    'semen': 'Семя',
    'blowjob': '',
    'blow job': '',
    'handjob': '',
    'hand job': '',
    'porn': '',
    'porno': '',
    'pornography': '',
    'xxx': '',
    'nude': '',
    'naked': '',
    'sexy': 'Сексуальный',
    'sex toy': 'Интим-игрушка',
    'sex-toy': 'Интим-игрушка',
    'sextoy': 'Интим-игрушка',
    'sex': 'Интим',
    'sexual': 'Интимный',
    'sexuality': 'Сексуальность',
    'intercourse': 'Близость',

    # Товарные термины
    'dildo': 'Фаллоимитатор',
    'dildos': 'Фаллоимитаторы',
    'vibrator': 'Вибратор',
    'vibrators': 'Вибраторы',
    'butt plug': 'Анальная пробка',
    'buttplug': 'Анальная пробка',
    'butt-plug': 'Анальная пробка',
    'bondage': 'Бондаж',
    'fetish': 'Фетиш',
    'bdsm': 'БДСМ',
    'lingerie': 'Бельё',
    'g-spot': 'Точка G',
    'g spot': 'Точка G',

    # Жаргон / сленг
    'balls': 'Шарики',
    'wank': '',
    'wanker': '',
    'horny': '',
    'kinky': '',
    'naughty': 'Игривый',
    'stripper': '',
    'striptease': '',
}

# Слова, которые WB блокирует даже как ПОДСТРОКУ — ищем без границ слова.
# Например: "cockerpo", "megacock", "fuckboy" — всё равно поймаем.
SUBSTRING_MATCH_WORDS = {
    'cock', 'cocks',
    'fuck', 'fucked', 'fucker', 'fucking', 'fuсk', 'fck',
    'vagina', 'vaginal',
    'penis', 'penises',
    'pussy',
    'cunt',
    'dick', 'dicks',
    'porn', 'porno', 'pornography',
    'dildo', 'dildos',
}

# Короткие слова — строгие границы слова (не ловим подстроки).
# "ass" не ловит "classic", "tit" не ловит "title", "cum" не ловит "document"
SHORT_WORDS_REQUIRE_BOUNDARY = {'ass', 'tit', 'cum', 'sex', 'clit', 'nude', 'xxx'}


class ProhibitedWordsFilter:
    """
    Фильтр запрещённых слов для WB.

    Загружает слова из:
    1. Дефолтного словаря (PROHIBITED_WORDS_REPLACEMENTS)
    2. БД — глобальные (от админа) + персональные (от продавца)
    """

    def __init__(self, custom_replacements: Optional[dict] = None,
                 seller_id: Optional[int] = None):
        """
        Args:
            custom_replacements: Дополнительные замены {слово: замена}
            seller_id: ID продавца для загрузки персональных слов из БД
        """
        self._replacements = dict(PROHIBITED_WORDS_REPLACEMENTS)

        # Загружаем из БД (глобальные + продавец)
        db_words = self._load_from_db(seller_id)
        self._replacements.update(db_words)

        if custom_replacements:
            self._replacements.update(custom_replacements)

        self._compile_patterns()

    def _load_from_db(self, seller_id: Optional[int] = None) -> dict:
        """Загрузить слова из БД (глобальные + для конкретного продавца)."""
        try:
            from models import ProhibitedWord
            query = ProhibitedWord.query.filter_by(is_active=True)

            if seller_id:
                # Глобальные + для этого продавца
                from sqlalchemy import or_
                query = query.filter(
                    or_(
                        ProhibitedWord.scope == 'global',
                        (ProhibitedWord.scope == 'seller') & (ProhibitedWord.seller_id == seller_id)
                    )
                )
            else:
                # Только глобальные
                query = query.filter_by(scope='global')

            words = {}
            for pw in query.all():
                words[pw.word.lower()] = pw.replacement or ''

            if words:
                logger.info(f"Loaded {len(words)} prohibited words from DB"
                            f" (seller_id={seller_id})")
            return words

        except Exception as e:
            # БД может быть недоступна (миграция не прошла, тесты и т.д.)
            logger.debug(f"Could not load prohibited words from DB: {e}")
            return {}

    def _compile_patterns(self):
        """Компилировать regex-паттерны из словаря замен."""
        self._patterns = []
        # Сначала многословные (чтобы "cock ring" матчился раньше "cock")
        sorted_words = sorted(self._replacements.keys(), key=lambda w: -len(w))

        for word in sorted_words:
            replacement = self._replacements[word]

            if word in SUBSTRING_MATCH_WORDS:
                # Подстрока — ловим везде (cockerpo, megacock, и т.д.)
                pattern = re.compile(re.escape(word), re.IGNORECASE)
            elif word in SHORT_WORDS_REQUIRE_BOUNDARY:
                # Строгие границы — не ловим внутри других слов
                pattern = re.compile(
                    r'(?<![a-zA-Zа-яА-ЯёЁ])' + re.escape(word) + r'(?![a-zA-Zа-яА-ЯёЁ])',
                    re.IGNORECASE
                )
            else:
                # Стандартные границы слова
                pattern = re.compile(
                    r'\b' + re.escape(word) + r'\b',
                    re.IGNORECASE
                )

            self._patterns.append((pattern, word, replacement))

    def filter_text(self, text: str) -> str:
        """Отфильтровать запрещённые слова в тексте."""
        if not text:
            return text

        original = text
        for pattern, word, replacement in self._patterns:
            def _replace(match, repl=replacement, orig_word=word):
                matched = match.group(0)
                if repl:
                    if matched[0].isupper() and repl[0].islower():
                        return repl[0].upper() + repl[1:]
                    return repl
                return ''
            text = pattern.sub(_replace, text)

        # Чистим артефакты
        text = re.sub(r' {2,}', ' ', text)
        text = re.sub(r'\s+([,.:;!?])', r'\1', text)
        text = text.strip()

        if text != original:
            logger.info(
                f"Prohibited words filtered: '{original[:80]}' → '{text[:80]}'"
            )

        return text

    def filter_product(self, data: dict) -> dict:
        """Отфильтровать запрещённые слова в title и description товара."""
        result = dict(data)

        if result.get('title') and isinstance(result['title'], str):
            result['title'] = self.filter_text(result['title'])

        if result.get('description') and isinstance(result['description'], str):
            result['description'] = self.filter_text(result['description'])

        return result

    def has_prohibited_words(self, text: str) -> list:
        """Проверить наличие запрещённых слов. Возвращает список найденных."""
        if not text:
            return []

        found = []
        for pattern, word, _ in self._patterns:
            if pattern.search(text):
                found.append(word)
        return found


# ============================================================================
# Кэш фильтров (по seller_id)
# ============================================================================

_filter_cache = {}


def get_prohibited_words_filter(seller_id: Optional[int] = None) -> ProhibitedWordsFilter:
    """Получить фильтр (кэшированный по seller_id)."""
    cache_key = seller_id or 0
    if cache_key not in _filter_cache:
        _filter_cache[cache_key] = ProhibitedWordsFilter(seller_id=seller_id)
    return _filter_cache[cache_key]


def invalidate_filter_cache(seller_id: Optional[int] = None):
    """Сбросить кэш после изменения словаря в БД."""
    if seller_id:
        _filter_cache.pop(seller_id, None)
    else:
        # Сброс всего кэша (админ изменил глобальные слова)
        _filter_cache.clear()


def filter_prohibited_words(text: str, seller_id: Optional[int] = None) -> str:
    """Удобная функция для фильтрации одного текста."""
    return get_prohibited_words_filter(seller_id).filter_text(text)
