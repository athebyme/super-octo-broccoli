# -*- coding: utf-8 -*-
"""
Фильтр запрещённых слов для Wildberries.

WB блокирует товары, содержащие ненормативную лексику в латинской транскрипции
в заголовках и описаниях. Этот модуль заменяет такие слова на допустимые
русскоязычные эквиваленты.

Примеры: Cock → Петушок, Vagina → Вагина, Fuck → (удаляется)
"""
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ============================================================================
# СЛОВАРЬ ЗАМЕН: запрещённое слово (lowercase) → допустимая замена
# ============================================================================

# Прямые замены — слово целиком заменяется на русский аналог
PROHIBITED_WORDS_REPLACEMENTS = {
    # Анатомические термины
    'cock': 'Петушок',
    'cocks': 'Петушки',
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

    # Обсценная лексика — удаляем (заменяем на пустую строку)
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

    # Товарные термины (часто встречаются в названиях товаров для взрослых)
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

# Слова, которые нужно проверять только как отдельные слова (не как подстроки)
# Например, "ass" не должно ловить "classic", "glass", "massage"
# "cock" не должно ловить "peacock" (но "peacock" тоже под вопросом)
# "tit" не должно ловить "title", "institution"
# "cum" не должно ловить "document", "cucumber"
# "sex" не должно ловить "unisex" — но для WB unisex тоже проблема, поэтому оставляем
SHORT_WORDS_REQUIRE_BOUNDARY = {'ass', 'tit', 'cum', 'sex', 'dick', 'clit', 'nude'}


class ProhibitedWordsFilter:
    """
    Фильтр запрещённых слов для WB.

    Заменяет запрещённые английские слова в заголовках и описаниях товаров
    на допустимые русскоязычные эквиваленты.
    """

    def __init__(self, custom_replacements: Optional[dict] = None):
        """
        Args:
            custom_replacements: Дополнительные замены {слово: замена},
                                 перезаписывают дефолтные.
        """
        self._replacements = dict(PROHIBITED_WORDS_REPLACEMENTS)
        if custom_replacements:
            self._replacements.update(custom_replacements)

        # Компилируем паттерны: сначала многословные (чтобы "cock ring" матчился раньше "cock")
        self._patterns = []
        sorted_words = sorted(self._replacements.keys(), key=lambda w: -len(w))
        for word in sorted_words:
            replacement = self._replacements[word]
            # Для коротких слов — строгие границы слова
            if word in SHORT_WORDS_REQUIRE_BOUNDARY:
                pattern = re.compile(
                    r'(?<![a-zA-Zа-яА-ЯёЁ])' + re.escape(word) + r'(?![a-zA-Zа-яА-ЯёЁ])',
                    re.IGNORECASE
                )
            else:
                # Для остальных — тоже границы слова, но менее строгие
                pattern = re.compile(
                    r'\b' + re.escape(word) + r'\b',
                    re.IGNORECASE
                )
            self._patterns.append((pattern, word, replacement))

    def filter_text(self, text: str) -> str:
        """
        Отфильтровать запрещённые слова в тексте.

        Args:
            text: Исходный текст (заголовок или описание)

        Returns:
            Очищенный текст с заменёнными словами
        """
        if not text:
            return text

        original = text
        for pattern, word, replacement in self._patterns:
            def _replace(match, repl=replacement, orig_word=word):
                matched = match.group(0)
                # Сохраняем регистр первой буквы оригинала
                if repl:
                    if matched[0].isupper() and repl[0].islower():
                        return repl[0].upper() + repl[1:]
                    return repl
                return ''  # Пустая замена — удаляем слово
            text = pattern.sub(_replace, text)

        # Чистим артефакты: двойные пробелы, пробелы перед запятыми/точками
        text = re.sub(r' {2,}', ' ', text)
        text = re.sub(r'\s+([,.:;!?])', r'\1', text)
        text = text.strip()

        if text != original:
            logger.info(
                f"Prohibited words filtered: '{original[:80]}' → '{text[:80]}'"
            )

        return text

    def filter_product(self, data: dict) -> dict:
        """
        Отфильтровать запрещённые слова во всех текстовых полях товара.

        Args:
            data: Словарь данных товара

        Returns:
            Словарь с очищенными текстовыми полями
        """
        result = dict(data)

        # Фильтруем заголовок
        if result.get('title') and isinstance(result['title'], str):
            result['title'] = self.filter_text(result['title'])

        # Фильтруем описание
        if result.get('description') and isinstance(result['description'], str):
            result['description'] = self.filter_text(result['description'])

        return result

    def has_prohibited_words(self, text: str) -> list:
        """
        Проверить наличие запрещённых слов в тексте.

        Args:
            text: Текст для проверки

        Returns:
            Список найденных запрещённых слов
        """
        if not text:
            return []

        found = []
        for pattern, word, _ in self._patterns:
            if pattern.search(text):
                found.append(word)
        return found


# Синглтон фильтра для переиспользования
_filter_instance = None


def get_prohibited_words_filter() -> ProhibitedWordsFilter:
    """Получить синглтон-инстанс фильтра."""
    global _filter_instance
    if _filter_instance is None:
        _filter_instance = ProhibitedWordsFilter()
    return _filter_instance


def filter_prohibited_words(text: str) -> str:
    """Удобная функция для фильтрации одного текста."""
    return get_prohibited_words_filter().filter_text(text)
