"""
Алгоритм автоматических рекомендаций для объединения карточек WB
Улучшенная версия с множеством стратегий поиска
"""
import re
from difflib import SequenceMatcher
from typing import List, Dict, Tuple, Set
from collections import defaultdict


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

    # Убираем размеры
    text = re.sub(r'\b(x{0,3}s|x{0,3}m|x{0,3}l|xs|xxl|xxxl)\b', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b\d{1,3}([\-/х]\d{1,3})?\s*(см|мм|м|г|кг|мл|л)?\b', '', text)

    # Убираем цвета
    colors = [
        'черный', 'белый', 'красный', 'синий', 'зеленый', 'желтый', 'серый',
        'розовый', 'оранжевый', 'фиолетовый', 'коричневый', 'бежевый', 'голубой',
        'бордовый', 'хаки', 'золотой', 'серебряный', 'мультиколор', 'принт',
        'black', 'white', 'red', 'blue', 'green', 'yellow', 'gray', 'grey', 'pink'
    ]
    for color in colors:
        text = re.sub(rf'\b{color}\b', '', text, flags=re.IGNORECASE)

    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_base_vendor_code(vendor_code: str) -> str:
    """
    Извлекает базовую часть артикула
    Примеры:
    - "ABC-123-RED" -> "ABC-123"
    - "SHIRT_XL_BLUE" -> "SHIRT"
    - "12345-A" -> "12345"
    """
    if not vendor_code:
        return ""

    # Убираем суффиксы размеров и цветов
    patterns = [
        r'[-_](x{0,3}[sml]|xs|xxl|xxxl)$',  # размеры
        r'[-_]\d{1,2}$',  # числовые размеры
        r'[-_](black|white|red|blue|green|gray|grey|pink|yellow)$',  # цвета EN
        r'[-_](черн|бел|красн|син|зелен|сер|роз|желт).*$',  # цвета RU
        r'[-_][a-z]$',  # одиночная буква
    ]

    base = vendor_code
    for pattern in patterns:
        base = re.sub(pattern, '', base, flags=re.IGNORECASE)

    # Если осталось слишком мало, берем первую часть до дефиса
    if len(base) < 3 and '-' in vendor_code:
        base = vendor_code.split('-')[0]

    return base if base else vendor_code


def extract_common_prefix(codes: List[str], min_length: int = 3) -> str:
    """Находит общий префикс списка артикулов"""
    if not codes:
        return ""

    prefix = codes[0]
    for code in codes[1:]:
        while not code.startswith(prefix) and len(prefix) > min_length:
            prefix = prefix[:-1]

    return prefix if len(prefix) >= min_length else ""


def get_ngrams(text: str, n: int = 3) -> Set[str]:
    """Получает n-граммы из текста"""
    if len(text) < n:
        return {text}
    return {text[i:i+n] for i in range(len(text) - n + 1)}


def ngram_similarity(text1: str, text2: str, n: int = 3) -> float:
    """Вычисляет схожесть по n-граммам (коэффициент Жаккара)"""
    if not text1 or not text2:
        return 0.0

    ngrams1 = get_ngrams(text1.lower(), n)
    ngrams2 = get_ngrams(text2.lower(), n)

    intersection = len(ngrams1 & ngrams2)
    union = len(ngrams1 | ngrams2)

    return intersection / union if union > 0 else 0.0


def word_overlap_score(text1: str, text2: str) -> float:
    """Вычисляет процент совпадающих слов"""
    if not text1 or not text2:
        return 0.0

    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())

    # Убираем короткие слова (предлоги и т.д.)
    words1 = {w for w in words1 if len(w) > 2}
    words2 = {w for w in words2 if len(w) > 2}

    if not words1 or not words2:
        return 0.0

    intersection = len(words1 & words2)
    min_len = min(len(words1), len(words2))

    return intersection / min_len if min_len > 0 else 0.0


def calculate_similarity(text1: str, text2: str) -> float:
    """Комбинированная схожесть строк"""
    if not text1 or not text2:
        return 0.0

    # SequenceMatcher
    seq_ratio = SequenceMatcher(None, text1, text2).ratio()

    # N-gram similarity
    ngram_ratio = ngram_similarity(text1, text2, 3)

    # Word overlap
    word_ratio = word_overlap_score(text1, text2)

    # Взвешенное среднее
    return seq_ratio * 0.4 + ngram_ratio * 0.3 + word_ratio * 0.3


def find_merge_recommendations(products: List[Dict], min_score: float = 0.6, max_products: int = 1000) -> List[Dict]:
    """
    Находит рекомендации для объединения карточек

    Использует несколько стратегий:
    1. По базовому артикулу
    2. По схожести названий
    3. По комбинации бренд + ключевые слова
    """
    recommendations = []

    if len(products) > max_products:
        products = products[:max_products]

    # Группируем по категориям
    by_category = defaultdict(list)
    for product in products:
        subject_id = product.get('subject_id')
        if subject_id:
            by_category[subject_id].append(product)

    used_nm_ids = set()

    for subject_id, category_products in by_category.items():
        if len(category_products) < 2:
            continue

        # === СТРАТЕГИЯ 1: По базовому артикулу ===
        by_base_code = defaultdict(list)
        for product in category_products:
            vendor = product.get('vendor_code', '')
            if vendor:
                base = extract_base_vendor_code(vendor)
                if len(base) >= 3:
                    by_base_code[base.lower()].append(product)

        for base_code, group in by_base_code.items():
            if len(group) >= 2 and len(group) <= 30:
                # Проверяем что все из одного бренда
                brands = set(p.get('brand', '') for p in group)
                if len(brands) == 1 and brands != {''}:
                    nm_ids = [p['nm_id'] for p in group]
                    if not any(nm_id in used_nm_ids for nm_id in nm_ids):
                        recommendations.append({
                            'cards': group,
                            'score': 0.95,
                            'reason': f'Одинаковый базовый артикул: {base_code}',
                            'suggested_target': group[0],
                            'subject_id': subject_id,
                            'brand': list(brands)[0],
                            'strategy': 'base_vendor_code'
                        })
                        for nm_id in nm_ids:
                            used_nm_ids.add(nm_id)

        # === СТРАТЕГИЯ 2: По бренду и названию ===
        by_brand = defaultdict(list)
        for product in category_products:
            if product['nm_id'] not in used_nm_ids:
                brand = product.get('brand', '') or 'NO_BRAND'
                by_brand[brand].append(product)

        for brand, brand_products in by_brand.items():
            if len(brand_products) < 2 or brand == 'NO_BRAND':
                continue

            # Ищем похожие по названию
            for i, prod1 in enumerate(brand_products):
                if prod1['nm_id'] in used_nm_ids:
                    continue
                if len(recommendations) >= 50:
                    break

                similar_group = [prod1]
                title1 = normalize_text(prod1.get('title', ''))

                for prod2 in brand_products[i+1:]:
                    if prod2['nm_id'] in used_nm_ids:
                        continue
                    if len(similar_group) >= 30:
                        break

                    title2 = normalize_text(prod2.get('title', ''))

                    # Проверяем схожесть названий
                    title_sim = calculate_similarity(title1, title2)
                    if title_sim >= 0.7:
                        similar_group.append(prod2)

                if len(similar_group) >= 2:
                    nm_ids = [p['nm_id'] for p in similar_group]
                    score = 0.2 + (calculate_similarity(
                        normalize_text(similar_group[0].get('title', '')),
                        normalize_text(similar_group[1].get('title', ''))
                    ) * 0.6)

                    recommendations.append({
                        'cards': similar_group,
                        'score': min(score, 0.9),
                        'reason': f'Похожие названия, бренд: {brand}',
                        'suggested_target': similar_group[0],
                        'subject_id': subject_id,
                        'brand': brand,
                        'strategy': 'similar_titles'
                    })
                    for nm_id in nm_ids:
                        used_nm_ids.add(nm_id)

        # === СТРАТЕГИЯ 3: По префиксу артикула ===
        for brand, brand_products in by_brand.items():
            remaining = [p for p in brand_products if p['nm_id'] not in used_nm_ids]
            if len(remaining) < 2:
                continue

            # Группируем по первым 4+ символам артикула
            by_prefix = defaultdict(list)
            for product in remaining:
                vendor = product.get('vendor_code', '')
                if vendor and len(vendor) >= 4:
                    prefix = vendor[:4].lower()
                    by_prefix[prefix].append(product)

            for prefix, group in by_prefix.items():
                if 2 <= len(group) <= 30:
                    nm_ids = [p['nm_id'] for p in group]
                    if not any(nm_id in used_nm_ids for nm_id in nm_ids):
                        recommendations.append({
                            'cards': group,
                            'score': 0.75,
                            'reason': f'Общий префикс артикула: {prefix}...',
                            'suggested_target': group[0],
                            'subject_id': subject_id,
                            'brand': brand,
                            'strategy': 'vendor_prefix'
                        })
                        for nm_id in nm_ids:
                            used_nm_ids.add(nm_id)

    # Сортируем по score и количеству карточек
    recommendations.sort(key=lambda x: (x['score'], len(x['cards'])), reverse=True)

    return recommendations[:50]


def calculate_merge_score(prod1: Dict, prod2: Dict) -> Tuple[float, str]:
    """
    Вычисляет оценку схожести двух карточек для объединения
    """
    score = 0.0
    reasons = []

    # 1. Одинаковый бренд (критично) - 20%
    brand1 = (prod1.get('brand') or '').lower()
    brand2 = (prod2.get('brand') or '').lower()

    if brand1 and brand2 and brand1 == brand2:
        score += 0.2
        reasons.append(f"бренд {brand1}")
    else:
        return 0.0, "Разные бренды"

    # 2. Похожесть названий - до 40%
    title1 = normalize_text(prod1.get('title', ''))
    title2 = normalize_text(prod2.get('title', ''))

    if title1 and title2:
        title_similarity = calculate_similarity(title1, title2)
        score += title_similarity * 0.4

        if title_similarity > 0.8:
            reasons.append("очень похожие названия")
        elif title_similarity > 0.6:
            reasons.append("похожие названия")

    # 3. Похожесть артикулов - до 30%
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
            elif vendor1[:3].lower() == vendor2[:3].lower():
                score += 0.1
                reasons.append("общий префикс артикула")

    # 4. Одинаковая категория
    if prod1.get('subject_id') != prod2.get('subject_id'):
        return 0.0, "Разные категории"
    else:
        score += 0.1

    reason = ", ".join(reasons) if reasons else "низкая схожесть"
    return min(score, 1.0), reason.capitalize()


def get_merge_recommendations_for_seller(seller_id: int, db_session, min_score: float = 0.6, max_products: int = 1000) -> List[Dict]:
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
            imt_groups[p.imt_id].append(p)

    # Находим необъединенные карточки
    single_products = []
    for imt_id, products in imt_groups.items():
        if len(products) == 1:
            single_products.append(products[0])

    print(f"⏱️  Found {len(single_products)} unmerged products (unique imt_id)")

    if len(single_products) < 2:
        return []

    products_data = [
        {
            'nm_id': p.nm_id,
            'imt_id': p.imt_id,
            'vendor_code': p.vendor_code,
            'title': p.title,
            'brand': p.brand,
            'subject_id': p.subject_id,
            'subject_name': p.object_name
        }
        for p in single_products
    ]

    print(f"⏱️  Starting recommendations analysis (max {max_products} products)...")
    recommendations = find_merge_recommendations(products_data, min_score, max_products)

    total_time = time.time() - start_time
    print(f"⏱️  Recommendations completed in {total_time:.2f}s - found {len(recommendations)} recommendations")

    return recommendations
