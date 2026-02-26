"""
Алгоритм автоматических рекомендаций для объединения карточек WB
Улучшенная версия v2 с множеством стратегий поиска
"""
import re
from difflib import SequenceMatcher
from typing import List, Dict, Tuple, Set, Optional
from collections import defaultdict


# ============ НОРМАЛИЗАЦИЯ И УТИЛИТЫ ============

def normalize_text(text: str) -> str:
    """
    Нормализация текста для сравнения
    Убирает размеры, цвета, лишние пробелы
    """
    if not text:
        return ""

    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)

    # Убираем вариации в скобках
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\[[^\]]*\]', '', text)

    # Убираем размеры (буквенные)
    text = re.sub(r'\b(x{0,3}s|x{0,3}m|x{0,3}l|xs|xxl|xxxl|xxs)\b', '', text, flags=re.IGNORECASE)

    # Убираем числовые размеры (42, 44-46, 50/52)
    text = re.sub(r'\b\d{2}([\-/]\d{2})?\b', '', text)

    # Убираем размеры с единицами (10 см, 250 мл)
    text = re.sub(r'\b\d+\s*(см|мм|м|г|кг|мл|л|шт)\b', '', text, flags=re.IGNORECASE)

    # Убираем цвета (расширенный список)
    colors_ru = [
        'черный', 'черная', 'черное', 'черные',
        'белый', 'белая', 'белое', 'белые',
        'красный', 'красная', 'красное', 'красные',
        'синий', 'синяя', 'синее', 'синие',
        'зеленый', 'зеленая', 'зеленое', 'зеленые',
        'желтый', 'желтая', 'желтое', 'желтые',
        'серый', 'серая', 'серое', 'серые',
        'розовый', 'розовая', 'розовое', 'розовые',
        'оранжевый', 'фиолетовый', 'коричневый', 'бежевый',
        'голубой', 'голубая', 'бордовый', 'бордовая',
        'хаки', 'золотой', 'серебряный', 'бирюзовый',
        'мультиколор', 'принт', 'цветной', 'однотонный',
        'темный', 'темная', 'светлый', 'светлая',
    ]
    colors_en = [
        'black', 'white', 'red', 'blue', 'green', 'yellow',
        'gray', 'grey', 'pink', 'orange', 'purple', 'brown',
        'beige', 'navy', 'gold', 'silver'
    ]

    for color in colors_ru + colors_en:
        text = re.sub(rf'\b{color}\b', '', text, flags=re.IGNORECASE)

    # Убираем номера моделей в конце (№1, #2, модель 3)
    text = re.sub(r'[№#]\s*\d+', '', text)
    text = re.sub(r'\bмодель\s*\d+\b', '', text, flags=re.IGNORECASE)

    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_base_vendor_code(vendor_code: str) -> str:
    """
    Извлекает базовую часть артикула
    """
    if not vendor_code:
        return ""

    # Убираем суффиксы размеров и цветов
    patterns = [
        r'[-_](x{0,3}[sml]|xs|xxl|xxxl|xxs)$',  # размеры буквенные
        r'[-_]\d{1,2}$',  # числовые размеры
        r'[-_](black|white|red|blue|green|gray|grey|pink|yellow)$',
        r'[-_](черн|бел|красн|син|зелен|сер|роз|желт|коричн|беж).*$',
        r'[-_][a-z]$',  # одиночная буква
        r'[-_]\d{3,}$',  # длинные числа (штрихкоды)
    ]

    base = vendor_code
    for pattern in patterns:
        base = re.sub(pattern, '', base, flags=re.IGNORECASE)

    if len(base) < 3 and '-' in vendor_code:
        base = vendor_code.split('-')[0]

    return base if base else vendor_code


def extract_numeric_suffix(text: str) -> Tuple[str, Optional[int]]:
    """
    Извлекает числовой суффикс из текста
    "Товар 1" -> ("Товар", 1)
    "ABC-123" -> ("ABC-", 123)
    """
    # Ищем число в конце
    match = re.search(r'^(.+?)[\s\-_]*(\d+)$', text.strip())
    if match:
        return match.group(1).strip(), int(match.group(2))
    return text, None


def extract_model_pattern(vendor_code: str) -> Tuple[str, Optional[str]]:
    """
    Извлекает паттерн модели
    "ABC-001" -> ("ABC-", "001")
    "SHIRT_XL_001" -> ("SHIRT_", "001")
    """
    if not vendor_code:
        return "", None

    # Паттерн: префикс + разделитель + число
    match = re.search(r'^(.+?[-_])(\d{2,})$', vendor_code)
    if match:
        return match.group(1), match.group(2)

    # Паттерн: просто число в конце
    match = re.search(r'^(.+?)(\d{3,})$', vendor_code)
    if match:
        return match.group(1), match.group(2)

    return vendor_code, None


def get_key_words(text: str, min_length: int = 3) -> Set[str]:
    """
    Извлекает ключевые слова из текста
    Убирает стоп-слова и короткие слова
    """
    if not text:
        return set()

    stop_words = {
        'для', 'при', 'под', 'над', 'без', 'или', 'как', 'так', 'что', 'это',
        'все', 'его', 'она', 'они', 'оно', 'был', 'быть', 'есть', 'нет',
        'the', 'and', 'for', 'with', 'from', 'that', 'this', 'are', 'was',
        'комплект', 'набор', 'штук', 'штуки', 'шт', 'упаковка', 'пакет'
    }

    words = set(text.lower().split())
    return {w for w in words if len(w) >= min_length and w not in stop_words}


def word_set_similarity(words1: Set[str], words2: Set[str]) -> float:
    """Коэффициент Жаккара для множеств слов"""
    if not words1 or not words2:
        return 0.0

    intersection = len(words1 & words2)
    union = len(words1 | words2)

    return intersection / union if union > 0 else 0.0


def get_ngrams(text: str, n: int = 3) -> Set[str]:
    """Получает n-граммы из текста"""
    if len(text) < n:
        return {text}
    return {text[i:i+n] for i in range(len(text) - n + 1)}


def ngram_similarity(text1: str, text2: str, n: int = 3) -> float:
    """Схожесть по n-граммам (коэффициент Жаккара)"""
    if not text1 or not text2:
        return 0.0

    ngrams1 = get_ngrams(text1.lower(), n)
    ngrams2 = get_ngrams(text2.lower(), n)

    intersection = len(ngrams1 & ngrams2)
    union = len(ngrams1 | ngrams2)

    return intersection / union if union > 0 else 0.0


def calculate_similarity(text1: str, text2: str) -> float:
    """Комбинированная схожесть строк"""
    if not text1 or not text2:
        return 0.0

    # SequenceMatcher
    seq_ratio = SequenceMatcher(None, text1, text2).ratio()

    # N-gram similarity
    ngram_ratio = ngram_similarity(text1, text2, 3)

    # Word set similarity
    words1 = get_key_words(text1)
    words2 = get_key_words(text2)
    word_ratio = word_set_similarity(words1, words2)

    # Взвешенное среднее
    return seq_ratio * 0.35 + ngram_ratio * 0.3 + word_ratio * 0.35


# ============ СТРАТЕГИИ ПОИСКА ============

def strategy_base_vendor_code(products: List[Dict], used_nm_ids: Set[int]) -> List[Dict]:
    """
    Стратегия 1: По базовому артикулу
    ABC-123-RED, ABC-123-BLUE -> ABC-123
    """
    recommendations = []

    by_base_and_brand = defaultdict(list)
    for product in products:
        if product['nm_id'] in used_nm_ids:
            continue
        vendor = product.get('vendor_code', '')
        brand = product.get('brand', '')
        if vendor and brand:
            base = extract_base_vendor_code(vendor)
            if len(base) >= 3:
                key = (base.lower(), brand.lower())
                by_base_and_brand[key].append(product)

    for (base_code, brand), group in by_base_and_brand.items():
        if 2 <= len(group) <= 30:
            recommendations.append({
                'cards': group,
                'score': 0.95,
                'reason': f'Одинаковый базовый артикул: {base_code}',
                'suggested_target': group[0],
                'strategy': 'base_vendor_code'
            })

    return recommendations


def strategy_model_series(products: List[Dict], used_nm_ids: Set[int]) -> List[Dict]:
    """
    Стратегия 2: Серии моделей
    ABC-001, ABC-002, ABC-003
    """
    recommendations = []

    by_prefix_and_brand = defaultdict(list)
    for product in products:
        if product['nm_id'] in used_nm_ids:
            continue
        vendor = product.get('vendor_code', '')
        brand = product.get('brand', '')
        if vendor and brand:
            prefix, number = extract_model_pattern(vendor)
            if prefix and number:
                key = (prefix.lower(), brand.lower())
                by_prefix_and_brand[key].append((product, int(number)))

    for (prefix, brand), items in by_prefix_and_brand.items():
        if len(items) >= 2:
            # Сортируем по номеру модели
            items.sort(key=lambda x: x[1])
            group = [item[0] for item in items[:30]]

            # Проверяем что номера последовательные или близкие
            numbers = [item[1] for item in items]
            if max(numbers) - min(numbers) <= len(numbers) * 2:  # Допускаем пропуски
                recommendations.append({
                    'cards': group,
                    'score': 0.90,
                    'reason': f'Серия моделей: {prefix}XXX',
                    'suggested_target': group[0],
                    'strategy': 'model_series'
                })

    return recommendations


def strategy_numeric_suffix(products: List[Dict], used_nm_ids: Set[int]) -> List[Dict]:
    """
    Стратегия 3: Числовой суффикс в названии
    "Товар 1", "Товар 2", "Товар 3"
    """
    recommendations = []

    by_base_title = defaultdict(list)
    for product in products:
        if product['nm_id'] in used_nm_ids:
            continue
        title = normalize_text(product.get('title', ''))
        brand = product.get('brand', '')
        if title and brand:
            base_title, number = extract_numeric_suffix(title)
            if number is not None and len(base_title) >= 5:
                key = (base_title.lower(), brand.lower())
                by_base_title[key].append((product, number))

    for (base_title, brand), items in by_base_title.items():
        if len(items) >= 2:
            items.sort(key=lambda x: x[1])
            group = [item[0] for item in items[:30]]
            recommendations.append({
                'cards': group,
                'score': 0.88,
                'reason': f'Нумерованная серия: "{base_title[:30]}..."',
                'suggested_target': group[0],
                'strategy': 'numeric_suffix'
            })

    return recommendations


def strategy_similar_titles(products: List[Dict], used_nm_ids: Set[int], min_similarity: float = 0.75) -> List[Dict]:
    """
    Стратегия 4: Похожие названия
    """
    recommendations = []

    # Группируем по бренду
    by_brand = defaultdict(list)
    for product in products:
        if product['nm_id'] in used_nm_ids:
            continue
        brand = product.get('brand', '')
        if brand:
            by_brand[brand.lower()].append(product)

    for brand, brand_products in by_brand.items():
        if len(brand_products) < 2:
            continue

        # Ограничиваем для производительности
        brand_products = brand_products[:200]

        processed = set()
        for i, prod1 in enumerate(brand_products):
            if prod1['nm_id'] in processed or len(recommendations) >= 100:
                break

            title1 = normalize_text(prod1.get('title', ''))
            if len(title1) < 5:
                continue

            similar_group = [prod1]

            for prod2 in brand_products[i+1:]:
                if prod2['nm_id'] in processed:
                    continue
                if len(similar_group) >= 30:
                    break

                title2 = normalize_text(prod2.get('title', ''))
                if len(title2) < 5:
                    continue

                sim = calculate_similarity(title1, title2)
                if sim >= min_similarity:
                    similar_group.append(prod2)

            if len(similar_group) >= 2:
                for p in similar_group:
                    processed.add(p['nm_id'])

                avg_sim = sum(
                    calculate_similarity(
                        normalize_text(similar_group[0].get('title', '')),
                        normalize_text(p.get('title', ''))
                    ) for p in similar_group[1:]
                ) / (len(similar_group) - 1)

                recommendations.append({
                    'cards': similar_group,
                    'score': 0.6 + avg_sim * 0.3,
                    'reason': f'Похожие названия (схожесть {avg_sim:.0%})',
                    'suggested_target': similar_group[0],
                    'strategy': 'similar_titles'
                })

    return recommendations


def strategy_vendor_prefix(products: List[Dict], used_nm_ids: Set[int], prefix_length: int = 5) -> List[Dict]:
    """
    Стратегия 5: Общий префикс артикула
    """
    recommendations = []

    by_prefix_and_brand = defaultdict(list)
    for product in products:
        if product['nm_id'] in used_nm_ids:
            continue
        vendor = product.get('vendor_code', '')
        brand = product.get('brand', '')
        if vendor and len(vendor) >= prefix_length and brand:
            prefix = vendor[:prefix_length].lower()
            key = (prefix, brand.lower())
            by_prefix_and_brand[key].append(product)

    for (prefix, brand), group in by_prefix_and_brand.items():
        if 2 <= len(group) <= 30:
            recommendations.append({
                'cards': group,
                'score': 0.75,
                'reason': f'Общий префикс артикула: {prefix}...',
                'suggested_target': group[0],
                'strategy': 'vendor_prefix'
            })

    return recommendations


def strategy_key_words(products: List[Dict], used_nm_ids: Set[int], min_common_words: int = 3) -> List[Dict]:
    """
    Стратегия 6: По ключевым словам в названии
    """
    recommendations = []

    # Группируем по бренду и категории
    by_brand_cat = defaultdict(list)
    for product in products:
        if product['nm_id'] in used_nm_ids:
            continue
        brand = product.get('brand', '')
        subject = product.get('subject_id')
        if brand and subject:
            key = (brand.lower(), subject)
            by_brand_cat[key].append(product)

    for (brand, subject), group_products in by_brand_cat.items():
        if len(group_products) < 2:
            continue

        # Извлекаем ключевые слова для каждого товара
        products_with_words = []
        for p in group_products[:100]:
            title = normalize_text(p.get('title', ''))
            words = get_key_words(title, min_length=4)
            if len(words) >= 2:
                products_with_words.append((p, words))

        if len(products_with_words) < 2:
            continue

        # Группируем по общим словам
        processed = set()
        for i, (prod1, words1) in enumerate(products_with_words):
            if prod1['nm_id'] in processed:
                continue

            similar_group = [prod1]

            for prod2, words2 in products_with_words[i+1:]:
                if prod2['nm_id'] in processed:
                    continue
                if len(similar_group) >= 30:
                    break

                common = words1 & words2
                if len(common) >= min_common_words:
                    similar_group.append(prod2)

            if len(similar_group) >= 2:
                for p in similar_group:
                    processed.add(p['nm_id'])

                recommendations.append({
                    'cards': similar_group,
                    'score': 0.70,
                    'reason': f'Общие ключевые слова ({min_common_words}+ совпадений)',
                    'suggested_target': similar_group[0],
                    'strategy': 'key_words'
                })

    return recommendations


def strategy_learn_from_merged(products: List[Dict], merged_groups: Dict[int, List], used_nm_ids: Set[int]) -> List[Dict]:
    """
    Стратегия 7: Учимся на уже объединённых группах
    Если в группе есть паттерн, ищем похожие необъединённые
    """
    recommendations = []

    if not merged_groups:
        return recommendations

    # Анализируем паттерны в объединённых группах
    for imt_id, merged_products in merged_groups.items():
        if len(merged_products) < 2:
            continue

        # Извлекаем общие характеристики группы
        vendors = [p.get('vendor_code', '') for p in merged_products]
        brands = [p.get('brand', '') for p in merged_products]

        if not all(vendors) or len(set(brands)) != 1:
            continue

        brand = brands[0]

        # Ищем общий префикс артикулов в группе
        common_prefix = vendors[0]
        for v in vendors[1:]:
            while not v.startswith(common_prefix) and len(common_prefix) > 3:
                common_prefix = common_prefix[:-1]

        if len(common_prefix) < 4:
            continue

        # Ищем необъединённые товары с таким же паттерном
        matching = []
        for product in products:
            if product['nm_id'] in used_nm_ids:
                continue
            v = product.get('vendor_code', '')
            b = product.get('brand', '')
            if v.startswith(common_prefix) and b.lower() == brand.lower():
                matching.append(product)

        if len(matching) >= 2:
            recommendations.append({
                'cards': matching[:30],
                'score': 0.85,
                'reason': f'Паттерн из группы imtID={imt_id}: {common_prefix}...',
                'suggested_target': matching[0],
                'strategy': 'learn_from_merged'
            })

    return recommendations


# ============ ОСНОВНАЯ ФУНКЦИЯ ============

def find_merge_recommendations(products: List[Dict], min_score: float = 0.5, max_products: int = 2000, merged_groups: Dict = None) -> List[Dict]:
    """
    Находит рекомендации для объединения карточек
    Использует множество стратегий
    """
    all_recommendations = []

    if len(products) > max_products:
        products = products[:max_products]

    # Группируем по категориям (объединять можно только в рамках категории)
    by_category = defaultdict(list)
    for product in products:
        subject_id = product.get('subject_id')
        if subject_id:
            by_category[subject_id].append(product)

    for subject_id, category_products in by_category.items():
        if len(category_products) < 2:
            continue

        used_nm_ids = set()

        # Применяем стратегии в порядке приоритета

        # 1. Базовый артикул (самая надёжная)
        recs = strategy_base_vendor_code(category_products, used_nm_ids)
        for r in recs:
            r['subject_id'] = subject_id
            for p in r['cards']:
                used_nm_ids.add(p['nm_id'])
        all_recommendations.extend(recs)

        # 2. Серии моделей
        recs = strategy_model_series(category_products, used_nm_ids)
        for r in recs:
            r['subject_id'] = subject_id
            for p in r['cards']:
                used_nm_ids.add(p['nm_id'])
        all_recommendations.extend(recs)

        # 3. Числовые суффиксы в названии
        recs = strategy_numeric_suffix(category_products, used_nm_ids)
        for r in recs:
            r['subject_id'] = subject_id
            for p in r['cards']:
                used_nm_ids.add(p['nm_id'])
        all_recommendations.extend(recs)

        # 4. Учимся на объединённых группах
        if merged_groups:
            recs = strategy_learn_from_merged(category_products, merged_groups, used_nm_ids)
            for r in recs:
                r['subject_id'] = subject_id
                for p in r['cards']:
                    used_nm_ids.add(p['nm_id'])
            all_recommendations.extend(recs)

        # 5. Общий префикс артикула (5+ символов)
        recs = strategy_vendor_prefix(category_products, used_nm_ids, prefix_length=5)
        for r in recs:
            r['subject_id'] = subject_id
            for p in r['cards']:
                used_nm_ids.add(p['nm_id'])
        all_recommendations.extend(recs)

        # 6. Похожие названия
        recs = strategy_similar_titles(category_products, used_nm_ids, min_similarity=0.7)
        for r in recs:
            r['subject_id'] = subject_id
            for p in r['cards']:
                used_nm_ids.add(p['nm_id'])
        all_recommendations.extend(recs)

        # 7. Ключевые слова
        recs = strategy_key_words(category_products, used_nm_ids, min_common_words=3)
        for r in recs:
            r['subject_id'] = subject_id
            for p in r['cards']:
                used_nm_ids.add(p['nm_id'])
        all_recommendations.extend(recs)

        # 8. Более короткий префикс (4 символа) - менее надёжно
        recs = strategy_vendor_prefix(category_products, used_nm_ids, prefix_length=4)
        for r in recs:
            r['subject_id'] = subject_id
            r['score'] = max(r['score'] - 0.1, 0.5)  # Снижаем оценку
            for p in r['cards']:
                used_nm_ids.add(p['nm_id'])
        all_recommendations.extend(recs)

    # Фильтруем по минимальной оценке
    all_recommendations = [r for r in all_recommendations if r['score'] >= min_score]

    # Сортируем по score и количеству карточек
    all_recommendations.sort(key=lambda x: (x['score'], len(x['cards'])), reverse=True)

    return all_recommendations[:100]


def calculate_merge_score(prod1: Dict, prod2: Dict) -> Tuple[float, str]:
    """
    Вычисляет оценку схожести двух карточек для объединения
    """
    score = 0.0
    reasons = []

    # 1. Одинаковый бренд (критично)
    brand1 = (prod1.get('brand') or '').lower()
    brand2 = (prod2.get('brand') or '').lower()

    if brand1 and brand2 and brand1 == brand2:
        score += 0.2
        reasons.append(f"бренд {brand1}")
    else:
        return 0.0, "Разные бренды"

    # 2. Похожесть названий
    title1 = normalize_text(prod1.get('title', ''))
    title2 = normalize_text(prod2.get('title', ''))

    if title1 and title2:
        title_similarity = calculate_similarity(title1, title2)
        score += title_similarity * 0.4

        if title_similarity > 0.8:
            reasons.append("очень похожие названия")
        elif title_similarity > 0.6:
            reasons.append("похожие названия")

    # 3. Похожесть артикулов
    vendor1 = prod1.get('vendor_code', '')
    vendor2 = prod2.get('vendor_code', '')

    if vendor1 and vendor2:
        base1 = extract_base_vendor_code(vendor1)
        base2 = extract_base_vendor_code(vendor2)

        if base1.lower() == base2.lower() and base1:
            score += 0.3
            reasons.append(f"одинаковый базовый артикул {base1}")
        else:
            vendor_similarity = calculate_similarity(base1.lower(), base2.lower())
            if vendor_similarity > 0.7:
                score += vendor_similarity * 0.2
                reasons.append("похожие артикулы")
            elif len(vendor1) >= 4 and len(vendor2) >= 4 and vendor1[:4].lower() == vendor2[:4].lower():
                score += 0.15
                reasons.append("общий префикс артикула")

    # 4. Одинаковая категория
    if prod1.get('subject_id') != prod2.get('subject_id'):
        return 0.0, "Разные категории"
    else:
        score += 0.1

    reason = ", ".join(reasons) if reasons else "низкая схожесть"
    return min(score, 1.0), reason.capitalize()


def get_merge_recommendations_for_seller(seller_id: int, db_session, min_score: float = 0.5, max_products: int = 2000) -> List[Dict]:
    """
    Получает рекомендации для конкретного продавца из БД
    """
    from models import Product
    import time

    start_time = time.time()

    all_products = Product.query.filter_by(
        seller_id=seller_id,
        is_active=True
    ).with_entities(
        Product.nm_id,
        Product.imt_id,
        Product.vendor_code,
        Product.title,
        Product.brand,
        Product.subject_id,
        Product.object_name
    ).all()

    print(f"⏱️  Loaded {len(all_products)} products in {time.time() - start_time:.2f}s")

    # Группируем по imt_id
    imt_groups = {}
    for p in all_products:
        if p.imt_id:
            if p.imt_id not in imt_groups:
                imt_groups[p.imt_id] = []
            imt_groups[p.imt_id].append({
                'nm_id': p.nm_id,
                'imt_id': p.imt_id,
                'vendor_code': p.vendor_code,
                'title': p.title,
                'brand': p.brand,
                'subject_id': p.subject_id,
                'subject_name': p.object_name
            })

    # Находим необъединенные карточки (группы размером 1)
    single_products = []
    merged_groups = {}

    for imt_id, products in imt_groups.items():
        if len(products) == 1:
            single_products.append(products[0])
        else:
            merged_groups[imt_id] = products

    print(f"⏱️  Found {len(single_products)} unmerged products, {len(merged_groups)} merged groups")

    if len(single_products) < 2:
        return []

    print(f"⏱️  Starting recommendations analysis (max {max_products} products)...")
    recommendations = find_merge_recommendations(
        single_products,
        min_score,
        max_products,
        merged_groups
    )

    total_time = time.time() - start_time
    print(f"⏱️  Recommendations completed in {total_time:.2f}s - found {len(recommendations)} recommendations")

    return recommendations
