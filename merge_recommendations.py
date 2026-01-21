"""
Алгоритм автоматических рекомендаций для объединения карточек WB
"""
import re
from difflib import SequenceMatcher
from typing import List, Dict, Tuple
from collections import defaultdict


def normalize_text(text: str) -> str:
    """
    Нормализация текста для сравнения
    Убирает размеры, цвета, лишние пробелы
    """
    if not text:
        return ""

    # Приводим к нижнему регистру
    text = text.lower().strip()

    # Убираем множественные пробелы
    text = re.sub(r'\s+', ' ', text)

    # Убираем типичные вариации (размеры, цвета в скобках)
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\[[^\]]*\]', '', text)

    # Убираем размеры типа XS, S, M, L, XL, XXL, XXXL
    text = re.sub(r'\b(x{0,3}s|x{0,3}m|x{0,3}l)\b', '', text)

    # Убираем числовые размеры
    text = re.sub(r'\b\d{1,3}([\-/]\d{1,3})?\b', '', text)

    # Убираем общие слова вариаций
    variations = ['черный', 'белый', 'красный', 'синий', 'зеленый', 'желтый',
                  'серый', 'розовый', 'оранжевый', 'фиолетовый', 'коричневый']
    for var in variations:
        text = text.replace(var, '')

    # Очистка от лишних пробелов после удалений
    text = re.sub(r'\s+', ' ', text).strip()

    return text


def extract_base_vendor_code(vendor_code: str) -> str:
    """
    Извлекает базовую часть артикула без вариаций
    Пример: "ABC-123-RED" -> "ABC-123"
    """
    if not vendor_code:
        return ""

    # Убираем суффиксы после последнего дефиса/подчеркивания
    base = re.sub(r'[-_][^-_]*$', '', vendor_code)

    # Если после удаления ничего не осталось, возвращаем оригинал
    return base if base else vendor_code


def calculate_similarity(text1: str, text2: str) -> float:
    """
    Вычисляет коэффициент схожести между двумя строками (0.0 - 1.0)
    """
    if not text1 or not text2:
        return 0.0

    return SequenceMatcher(None, text1, text2).ratio()


def find_merge_recommendations(products: List[Dict], min_score: float = 0.6) -> List[Dict]:
    """
    Находит рекомендации для объединения карточек

    Args:
        products: Список карточек товаров
        min_score: Минимальный порог схожести (0.0 - 1.0)

    Returns:
        Список рекомендаций, отсортированных по убыванию score
        [
            {
                'cards': [card1, card2, ...],  # Карточки которые можно объединить
                'score': 0.95,                   # Уровень уверенности (0-1)
                'reason': 'Описание причины',
                'suggested_target': card1        # Рекомендуемая главная карточка
            }
        ]
    """
    recommendations = []

    # Группируем по категориям (можно объединять только карточки одной категории)
    by_category = defaultdict(list)
    for product in products:
        subject_id = product.get('subject_id')
        if subject_id:
            by_category[subject_id].append(product)

    # Анализируем каждую категорию отдельно
    for subject_id, category_products in by_category.items():
        if len(category_products) < 2:
            continue

        # Группируем по брендам (сильная связь)
        by_brand = defaultdict(list)
        for product in category_products:
            brand = product.get('brand', '') or 'NO_BRAND'
            by_brand[brand].append(product)

        # Ищем похожие карточки внутри каждого бренда
        for brand, brand_products in by_brand.items():
            if len(brand_products) < 2:
                continue

            # Попарное сравнение карточек
            compared = set()

            for i, prod1 in enumerate(brand_products):
                for prod2 in brand_products[i+1:]:
                    # Избегаем дубликатов
                    pair_key = tuple(sorted([prod1['nm_id'], prod2['nm_id']]))
                    if pair_key in compared:
                        continue
                    compared.add(pair_key)

                    # Вычисляем схожесть
                    score, reason = calculate_merge_score(prod1, prod2)

                    if score >= min_score:
                        # Ищем существующую группу рекомендаций для расширения
                        merged_into_existing = False
                        for rec in recommendations:
                            # Если одна из карточек уже в рекомендации
                            rec_nm_ids = {c['nm_id'] for c in rec['cards']}
                            if prod1['nm_id'] in rec_nm_ids or prod2['nm_id'] in rec_nm_ids:
                                # Проверяем что новая карточка совместима со всеми в группе
                                new_card = prod2 if prod1['nm_id'] in rec_nm_ids else prod1

                                compatible = all(
                                    calculate_merge_score(new_card, c)[0] >= min_score
                                    for c in rec['cards']
                                )

                                if compatible:
                                    rec['cards'].append(new_card)
                                    # Пересчитываем score как среднее
                                    all_scores = []
                                    for c1 in rec['cards']:
                                        for c2 in rec['cards']:
                                            if c1['nm_id'] != c2['nm_id']:
                                                all_scores.append(calculate_merge_score(c1, c2)[0])
                                    rec['score'] = sum(all_scores) / len(all_scores) if all_scores else score
                                    merged_into_existing = True
                                    break

                        if not merged_into_existing:
                            # Создаем новую рекомендацию
                            recommendations.append({
                                'cards': [prod1, prod2],
                                'score': score,
                                'reason': reason,
                                'suggested_target': prod1,  # Первая карточка как целевая
                                'subject_id': subject_id,
                                'brand': brand
                            })

    # Сортируем по убыванию score
    recommendations.sort(key=lambda x: x['score'], reverse=True)

    # Ограничиваем топ-50 рекомендаций
    return recommendations[:50]


def calculate_merge_score(prod1: Dict, prod2: Dict) -> Tuple[float, str]:
    """
    Вычисляет оценку схожести двух карточек для объединения

    Returns:
        (score, reason) - оценка от 0 до 1 и описание причины
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
        # Разные бренды - большой штраф
        return 0.0, "Разные бренды"

    # 2. Похожесть названий (очень важно) - до 40%
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
        # Сравниваем базовые артикулы
        base1 = extract_base_vendor_code(vendor1)
        base2 = extract_base_vendor_code(vendor2)

        if base1 == base2 and base1:
            score += 0.3
            reasons.append(f"одинаковый базовый артикул {base1}")
        else:
            # Проверяем схожесть артикулов
            vendor_similarity = calculate_similarity(base1.lower(), base2.lower())
            if vendor_similarity > 0.7:
                score += vendor_similarity * 0.2
                reasons.append("похожие артикулы")

    # 4. Одинаковая категория (обязательно)
    if prod1.get('subject_id') != prod2.get('subject_id'):
        return 0.0, "Разные категории"
    else:
        score += 0.1

    # Формируем итоговое описание причины
    reason = ", ".join(reasons) if reasons else "низкая схожесть"

    return min(score, 1.0), reason.capitalize()


def get_merge_recommendations_for_seller(seller_id: int, db_session, min_score: float = 0.6) -> List[Dict]:
    """
    Получает рекомендации для конкретного продавца из БД

    Args:
        seller_id: ID продавца
        db_session: Сессия базы данных
        min_score: Минимальный порог схожести

    Returns:
        Список рекомендаций
    """
    from models import Product
    from sqlalchemy import func

    # Получаем активные карточки
    all_products = Product.query.filter_by(
        seller_id=seller_id,
        is_active=True
    ).all()

    # Группируем по imt_id
    imt_groups = {}
    for p in all_products:
        if p.imt_id:
            if p.imt_id not in imt_groups:
                imt_groups[p.imt_id] = []
            imt_groups[p.imt_id].append(p)

    # Находим необъединенные карточки (группы размером 1)
    # Это карточки с уникальным imt_id
    single_products = []
    for imt_id, products in imt_groups.items():
        if len(products) == 1:
            single_products.append(products[0])

    # Если нет необъединенных карточек, возвращаем пустой список
    if len(single_products) < 2:
        return []

    # Преобразуем в dict для алгоритма
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

    return find_merge_recommendations(products_data, min_score)
