# -*- coding: utf-8 -*-
"""
Предвалидация CSV файлов перед импортом.

Выполняет быструю проверку CSV до начала полного парсинга:
- Автодетекция кодировки и разделителя
- Проверка структуры (количество колонок, пустые строки)
- Поиск дубликатов по external_id
- Preview первых N товаров
- Генерация предупреждений
"""
import csv
import logging
import re
from dataclasses import dataclass, field
from io import StringIO
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class PreValidationResult:
    """Результат предвалидации CSV."""
    is_valid: bool = True
    encoding_detected: str = ''
    encoding_confidence: float = 0.0
    delimiter_detected: str = ''
    total_rows: int = 0
    columns_count: int = 0
    empty_rows: int = 0
    duplicate_ids: int = 0
    duplicate_id_list: List[str] = field(default_factory=list)
    sample_products: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    field_fill_rates: Dict[str, float] = field(default_factory=dict)


class CSVPreValidator:
    """
    Предвалидация CSV перед импортом.

    Позволяет обнаружить проблемы до начала полного парсинга:
    - Битая кодировка
    - Неправильный разделитель
    - Пустой или повреждённый файл
    - Дубликаты
    """

    @staticmethod
    def detect_encoding(raw_bytes: bytes) -> Tuple[str, float]:
        """
        Детектировать кодировку файла.
        Возвращает (encoding, confidence).
        """
        try:
            import chardet
            result = chardet.detect(raw_bytes[:50000])  # Первые 50KB
            return result.get('encoding', 'utf-8'), result.get('confidence', 0.0)
        except ImportError:
            # chardet не установлен — пробуем вручную
            for enc in ('utf-8', 'cp1251', 'latin-1'):
                try:
                    raw_bytes.decode(enc)
                    return enc, 0.7
                except (UnicodeDecodeError, UnicodeError):
                    continue
            return 'utf-8', 0.3

    @staticmethod
    def detect_delimiter(csv_text: str) -> str:
        """Детектировать разделитель CSV через csv.Sniffer."""
        try:
            # Берём первые 5 строк для анализа
            sample = '\n'.join(csv_text.split('\n')[:5])
            dialect = csv.Sniffer().sniff(sample, delimiters=';,\t|')
            return dialect.delimiter
        except csv.Error:
            # Fallback: считаем количество потенциальных разделителей
            first_line = csv_text.split('\n')[0] if csv_text else ''
            candidates = {';': 0, ',': 0, '\t': 0, '|': 0}
            for char in first_line:
                if char in candidates:
                    candidates[char] += 1
            best = max(candidates, key=candidates.get)
            return best if candidates[best] > 0 else ';'

    @staticmethod
    def validate(
        csv_content: str,
        expected_delimiter: str = None,
        expected_encoding: str = None,
        id_column: int = 0,
        min_columns: int = 5,
        sample_size: int = 5,
        column_mapping: dict = None,
    ) -> PreValidationResult:
        """
        Полная предвалидация CSV контента.

        Args:
            csv_content: Текстовое содержимое CSV
            expected_delimiter: Ожидаемый разделитель (если None — автодетекция)
            expected_encoding: Ожидаемая кодировка (для отчёта)
            id_column: Номер колонки с ID товара (для поиска дублей)
            min_columns: Минимальное кол-во колонок
            sample_size: Кол-во товаров в preview
            column_mapping: Маппинг колонок (из настроек поставщика)

        Returns:
            PreValidationResult
        """
        result = PreValidationResult()

        if not csv_content or not csv_content.strip():
            result.is_valid = False
            result.errors.append("CSV файл пуст")
            return result

        # Автодетекция разделителя
        detected_delimiter = CSVPreValidator.detect_delimiter(csv_content)
        delimiter = expected_delimiter or detected_delimiter
        result.delimiter_detected = detected_delimiter

        if expected_delimiter and expected_delimiter != detected_delimiter:
            result.warnings.append(
                f"Ожидаемый разделитель '{expected_delimiter}' отличается от "
                f"обнаруженного '{detected_delimiter}'"
            )

        result.encoding_detected = expected_encoding or 'unknown'

        # Парсинг строк
        try:
            reader = csv.reader(StringIO(csv_content), delimiter=delimiter, quotechar='"')
            rows = list(reader)
        except csv.Error as e:
            result.is_valid = False
            result.errors.append(f"Ошибка парсинга CSV: {e}")
            return result

        if not rows:
            result.is_valid = False
            result.errors.append("CSV не содержит строк")
            return result

        # Анализ структуры
        result.total_rows = len(rows)
        col_counts = [len(row) for row in rows]
        result.columns_count = max(col_counts) if col_counts else 0

        # Пустые строки
        result.empty_rows = sum(1 for row in rows if not any(cell.strip() for cell in row))
        if result.empty_rows > 0:
            result.warnings.append(f"Найдено {result.empty_rows} пустых строк")

        # Проверка минимального количества колонок
        short_rows = sum(1 for c in col_counts if c < min_columns)
        if short_rows > result.total_rows * 0.1:  # >10% строк короткие
            result.warnings.append(
                f"{short_rows} строк имеют менее {min_columns} колонок "
                f"(ожидается {result.columns_count})"
            )

        # Поиск дубликатов по ID
        ids_seen = {}
        for row_num, row in enumerate(rows):
            if len(row) > id_column:
                ext_id = row[id_column].strip()
                if ext_id and ext_id != 'id' and ext_id != 'ID':
                    if ext_id in ids_seen:
                        if ext_id not in result.duplicate_id_list:
                            result.duplicate_id_list.append(ext_id)
                    ids_seen[ext_id] = row_num

        result.duplicate_ids = len(result.duplicate_id_list)
        if result.duplicate_ids > 0:
            result.warnings.append(
                f"Найдено {result.duplicate_ids} дублей external_id: "
                f"{', '.join(result.duplicate_id_list[:10])}"
                + (" и др." if result.duplicate_ids > 10 else "")
            )

        # Preview первых N товаров (пропуская пустые)
        sample_count = 0
        for row in rows:
            if sample_count >= sample_size:
                break
            if not any(cell.strip() for cell in row):
                continue
            if len(row) < min_columns:
                continue

            sample_item = {}
            if column_mapping:
                for field_name, mapping in column_mapping.items():
                    col_idx = mapping.get('column', 0)
                    if col_idx < len(row):
                        sample_item[field_name] = row[col_idx].strip()
            else:
                sample_item = {f'col_{i}': cell.strip() for i, cell in enumerate(row[:10])}

            result.sample_products.append(sample_item)
            sample_count += 1

        # Статистика заполненности полей (на первых 100 строках)
        analyze_rows = rows[:min(100, len(rows))]
        if analyze_rows:
            num_cols = result.columns_count
            fill_counts = [0] * num_cols
            total_analyzed = 0
            for row in analyze_rows:
                if not any(cell.strip() for cell in row):
                    continue
                total_analyzed += 1
                for i in range(min(len(row), num_cols)):
                    if row[i].strip():
                        fill_counts[i] += 1

            if total_analyzed > 0:
                for i in range(num_cols):
                    col_name = f'column_{i}'
                    if column_mapping:
                        for field_name, mapping in column_mapping.items():
                            if mapping.get('column') == i:
                                col_name = field_name
                                break
                    result.field_fill_rates[col_name] = round(
                        fill_counts[i] / total_analyzed, 3
                    )

        # Итоговая валидность
        if result.errors:
            result.is_valid = False
        elif result.total_rows < 2:
            result.is_valid = False
            result.errors.append("CSV содержит менее 2 строк")

        return result

    @staticmethod
    def validate_raw(
        raw_bytes: bytes,
        expected_delimiter: str = None,
        expected_encoding: str = None,
        **kwargs,
    ) -> PreValidationResult:
        """
        Предвалидация из raw bytes (до декодирования).
        Автодетектирует кодировку, декодирует и запускает validate().
        """
        if not raw_bytes:
            result = PreValidationResult()
            result.is_valid = False
            result.errors.append("Пустой файл (0 байт)")
            return result

        # Автодетекция кодировки
        detected_enc, confidence = CSVPreValidator.detect_encoding(raw_bytes)
        encoding = expected_encoding or detected_enc

        # Декодирование
        try:
            csv_text = raw_bytes.decode(encoding, errors='replace')
        except (UnicodeDecodeError, LookupError):
            csv_text = raw_bytes.decode('utf-8', errors='replace')
            encoding = 'utf-8 (fallback)'

        result = CSVPreValidator.validate(
            csv_text,
            expected_delimiter=expected_delimiter,
            expected_encoding=encoding,
            **kwargs,
        )
        result.encoding_detected = detected_enc
        result.encoding_confidence = confidence

        if confidence < 0.5:
            result.warnings.append(
                f"Низкая уверенность в кодировке: {detected_enc} ({confidence:.0%})"
            )

        return result
