# -*- coding: utf-8 -*-
"""
Умный маппер категорий с контекстным анализом и обратной связью.

Расширяет базовый get_best_category_match() из wb_categories_mapping:
- Контекстный анализ: использует title + brand + description для поиска
- Обратная связь: автоматически создаёт CategoryMapping из ручных исправлений
- Кэширование: запоминает успешные маппинги в БД для будущих товаров
- Fuzzy matching: нечёткое сравнение для похожих категорий
"""
import logging
import re
from difflib import SequenceMatcher
from typing import Optional, Tuple, Dict, List

logger = logging.getLogger(__name__)


class SmartCategoryMapper:
    """
    Многоуровневый маппер категорий с обучением из исправлений.

    Приоритеты:
    1. Ручные исправления (ProductCategoryCorrection) — confidence 1.0
    2. Обученные маппинги (CategoryMapping) — confidence из БД
    3. Точное совпадение (CSV_TO_WB_EXACT_MAPPING) — confidence 0.95
    4. Контекстный поиск (title + brand + keywords) — confidence 0.6-0.85
    5. Базовый keyword match — confidence 0.5-0.75
    6. Дефолт — confidence 0.3
    """

    # Бренды, помогающие определить категорию
    BRAND_CATEGORY_HINTS = {
        'satisfyer': 5576,  # Вакуумно-волновые стимуляторы
        'womanizer': 5576,
        'we-vibe': 5067,    # Вибраторы
        'lovense': 5067,
        'svakom': 5067,
        'lelo': 5067,
        'doc johnson': 5069,  # Фаллоимитаторы
        'pipedream': 5070,    # Мастурбаторы
        'system jo': 2909,    # Лубриканты
        'swiss navy': 2909,
    }

    @classmethod
    def map_category(
        cls,
        csv_category: str,
        product_title: str = '',
        brand: str = '',
        description: str = '',
        all_categories: list = None,
        external_id: str = None,
        source_type: str = 'sexoptovik',
    ) -> Tuple[int, str, float]:
        """
        Определить WB категорию с контекстным анализом.

        Returns:
            (subject_id, subject_name, confidence)
        """
        from services.wb_categories_mapping import (
            get_best_category_match,
            WB_ADULT_CATEGORIES,
            CATEGORY_KEYWORDS_MAPPING,
        )

        # 1. Сначала пробуем базовый маппер (он уже проверяет ручные исправления)
        subject_id, subject_name, confidence = get_best_category_match(
            csv_category=csv_category,
            product_title=product_title,
            all_categories=all_categories,
            external_id=external_id,
            source_type=source_type,
        )

        # Если confidence >= 0.9 — базовый маппер уже нашёл отличный результат
        if confidence >= 0.9:
            return subject_id, subject_name, confidence

        # 2. Контекстный анализ: бренд + title + description
        context_result = cls._contextual_match(
            csv_category=csv_category,
            product_title=product_title,
            brand=brand,
            description=description,
            all_categories=all_categories,
        )

        if context_result and context_result[2] > confidence:
            subject_id, subject_name, confidence = context_result

        # 3. Fuzzy matching для категории
        if confidence < 0.7 and csv_category:
            fuzzy_result = cls._fuzzy_category_match(csv_category)
            if fuzzy_result and fuzzy_result[2] > confidence:
                subject_id, subject_name, confidence = fuzzy_result

        # 4. Проверяем обученные маппинги из CategoryMapping
        if confidence < 0.9:
            learned_result = cls._check_learned_mappings(
                csv_category, source_type
            )
            if learned_result and learned_result[2] > confidence:
                subject_id, subject_name, confidence = learned_result

        return subject_id, subject_name, confidence

    @classmethod
    def _contextual_match(
        cls,
        csv_category: str,
        product_title: str,
        brand: str,
        description: str,
        all_categories: list = None,
    ) -> Optional[Tuple[int, str, float]]:
        """Контекстный поиск категории используя все доступные данные."""
        from services.wb_categories_mapping import (
            WB_ADULT_CATEGORIES,
            CATEGORY_KEYWORDS_MAPPING,
        )

        # Собираем контекст: title + category + all_categories + description (первые 200 символов)
        context_parts = []
        if product_title:
            context_parts.append(product_title.lower())
        if csv_category:
            context_parts.append(csv_category.lower())
        if all_categories:
            context_parts.extend(c.lower() for c in all_categories if c)
        if description:
            context_parts.append(description[:200].lower())

        context = ' '.join(context_parts)
        if not context:
            return None

        # Бренд-хинты: некоторые бренды почти всегда принадлежат одной категории
        if brand:
            brand_lower = brand.lower().strip()
            hint_id = cls.BRAND_CATEGORY_HINTS.get(brand_lower)
            if hint_id:
                # Проверяем что это не противоречит категории
                cat_lower = csv_category.lower() if csv_category else ''
                # Если категория явно не указывает на другое — используем бренд-хинт
                if not any(
                    kw in cat_lower
                    for kw in ('лубрикант', 'смазк', 'презерватив', 'белье', 'костюм')
                    if hint_id not in (2909, 2865)
                ):
                    return (
                        hint_id,
                        WB_ADULT_CATEGORIES.get(hint_id, 'Unknown'),
                        0.7,
                    )

        # Мульти-keyword скоринг: ищем все совпадения и выбираем лучшее
        scores: Dict[int, float] = {}
        for keyword, subj_id in CATEGORY_KEYWORDS_MAPPING.items():
            if keyword in context:
                # Длина совпадения / длина контекста + бонус за точность
                base_score = len(keyword) / max(len(context), 1)
                # Бонус если keyword найден в title (более релевантно)
                if product_title and keyword in product_title.lower():
                    base_score += 0.15
                # Бонус если keyword найден в category
                if csv_category and keyword in csv_category.lower():
                    base_score += 0.2
                # Накапливаем баллы для одной категории (несколько keywords -> одна категория)
                scores[subj_id] = scores.get(subj_id, 0) + base_score

        if scores:
            best_id = max(scores, key=scores.get)
            best_score = min(scores[best_id], 1.0)
            # Нормализуем confidence в диапазон 0.6-0.85
            confidence = 0.6 + best_score * 0.25
            confidence = min(confidence, 0.85)
            return (
                best_id,
                WB_ADULT_CATEGORIES.get(best_id, 'Unknown'),
                round(confidence, 3),
            )

        return None

    @classmethod
    def _fuzzy_category_match(
        cls, csv_category: str, threshold: float = 0.65
    ) -> Optional[Tuple[int, str, float]]:
        """Нечёткое сравнение категории с CSV_TO_WB_EXACT_MAPPING."""
        from services.wb_categories_mapping import (
            CSV_TO_WB_EXACT_MAPPING,
            WB_ADULT_CATEGORIES,
        )

        csv_lower = csv_category.lower().strip()
        best_match = None
        best_ratio = 0.0

        for mapping_cat, subj_id in CSV_TO_WB_EXACT_MAPPING.items():
            ratio = SequenceMatcher(
                None, csv_lower, mapping_cat.lower()
            ).ratio()
            if ratio > best_ratio and ratio >= threshold:
                best_ratio = ratio
                best_match = (
                    subj_id,
                    WB_ADULT_CATEGORIES.get(subj_id, 'Unknown'),
                    round(ratio * 0.85, 3),  # Дисконт за fuzzy
                )

        return best_match

    @classmethod
    def _check_learned_mappings(
        cls, csv_category: str, source_type: str
    ) -> Optional[Tuple[int, str, float]]:
        """Проверить обученные маппинги из CategoryMapping."""
        if not csv_category:
            return None
        try:
            from models import CategoryMapping
            from services.wb_categories_mapping import WB_ADULT_CATEGORIES

            mapping = CategoryMapping.query.filter_by(
                source_category=csv_category,
                source_type=source_type,
            ).order_by(
                CategoryMapping.priority.desc(),
                CategoryMapping.confidence_score.desc(),
            ).first()

            if mapping:
                return (
                    mapping.wb_subject_id,
                    mapping.wb_subject_name or WB_ADULT_CATEGORIES.get(
                        mapping.wb_subject_id, 'Unknown'
                    ),
                    mapping.confidence_score or 0.8,
                )
        except Exception as e:
            logger.debug(f"Failed to check learned mappings: {e}")

        return None

    # ------------------------------------------------------------------
    # Feedback loop: обучение из ручных исправлений
    # ------------------------------------------------------------------

    @classmethod
    def learn_from_correction(
        cls,
        original_category: str,
        corrected_wb_subject_id: int,
        corrected_wb_subject_name: str = '',
        source_type: str = 'sexoptovik',
        confidence: float = 0.95,
    ) -> bool:
        """
        Создать/обновить CategoryMapping из ручного исправления.

        Вызывается после того как пользователь вручную исправляет категорию.
        Если для данной source_category уже есть 3+ одинаковых исправления,
        повышаем confidence и priority.
        """
        if not original_category or not corrected_wb_subject_id:
            return False

        try:
            from models import CategoryMapping, ProductCategoryCorrection, db
            from services.wb_categories_mapping import WB_ADULT_CATEGORIES

            if not corrected_wb_subject_name:
                corrected_wb_subject_name = WB_ADULT_CATEGORIES.get(
                    corrected_wb_subject_id, 'Unknown'
                )

            # Ищем существующий маппинг
            existing = CategoryMapping.query.filter_by(
                source_category=original_category,
                source_type=source_type,
                wb_subject_id=corrected_wb_subject_id,
            ).first()

            # Считаем сколько раз эту категорию исправляли на тот же subject_id
            correction_count = ProductCategoryCorrection.query.filter_by(
                original_category=original_category,
                source_type=source_type,
                corrected_wb_subject_id=corrected_wb_subject_id,
            ).count()

            # Повышаем confidence на основе числа подтверждений
            if correction_count >= 5:
                confidence = min(0.99, confidence + 0.04)
            elif correction_count >= 3:
                confidence = min(0.98, confidence + 0.02)

            if existing:
                existing.confidence_score = max(
                    existing.confidence_score or 0, confidence
                )
                existing.priority = max(existing.priority or 0, correction_count)
                existing.is_auto_mapped = False
            else:
                new_mapping = CategoryMapping(
                    source_category=original_category,
                    source_type=source_type,
                    wb_category_name=corrected_wb_subject_name,
                    wb_subject_id=corrected_wb_subject_id,
                    wb_subject_name=corrected_wb_subject_name,
                    priority=correction_count,
                    is_auto_mapped=False,
                    confidence_score=confidence,
                )
                db.session.add(new_mapping)

            db.session.commit()
            logger.info(
                f"Learned category mapping: '{original_category}' -> "
                f"{corrected_wb_subject_name} ({corrected_wb_subject_id}), "
                f"confidence={confidence}, corrections={correction_count}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to learn from correction: {e}")
            try:
                from models import db
                db.session.rollback()
            except Exception:
                pass
            return False

    @classmethod
    def sync_corrections_to_mappings(cls, source_type: str = 'sexoptovik') -> int:
        """
        Массовая синхронизация: создать CategoryMapping из всех
        ProductCategoryCorrection, которых ещё нет в маппингах.

        Returns:
            Количество созданных/обновлённых маппингов
        """
        try:
            from models import ProductCategoryCorrection, db
            from sqlalchemy import func

            # Группируем исправления по (original_category, corrected_wb_subject_id)
            corrections = (
                db.session.query(
                    ProductCategoryCorrection.original_category,
                    ProductCategoryCorrection.corrected_wb_subject_id,
                    ProductCategoryCorrection.corrected_wb_subject_name,
                    func.count().label('cnt'),
                )
                .filter_by(source_type=source_type)
                .filter(ProductCategoryCorrection.original_category.isnot(None))
                .group_by(
                    ProductCategoryCorrection.original_category,
                    ProductCategoryCorrection.corrected_wb_subject_id,
                )
                .all()
            )

            count = 0
            for row in corrections:
                original_cat = row[0]
                wb_subject_id = row[1]
                wb_subject_name = row[2] or ''
                correction_count = row[3]

                if not original_cat:
                    continue

                confidence = 0.9 + min(correction_count * 0.02, 0.09)
                result = cls.learn_from_correction(
                    original_category=original_cat,
                    corrected_wb_subject_id=wb_subject_id,
                    corrected_wb_subject_name=wb_subject_name,
                    source_type=source_type,
                    confidence=confidence,
                )
                if result:
                    count += 1

            logger.info(
                f"Synced {count} corrections to category mappings "
                f"(source_type={source_type})"
            )
            return count

        except Exception as e:
            logger.error(f"Failed to sync corrections to mappings: {e}")
            return 0
