# -*- coding: utf-8 -*-
"""
Скрипт для массового скачивания фото всех поставщиков.

Использование:
    python3 scripts/download_all_photos.py                    # все поставщики
    python3 scripts/download_all_photos.py --supplier-id 1    # конкретный поставщик
    python3 scripts/download_all_photos.py --wait             # ждать завершения очереди
"""

import sys
import os
import time
import argparse

# Добавляем корень проекта в path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Импортируем готовое Flask-приложение из seller_platform
from seller_platform import app


def main():
    parser = argparse.ArgumentParser(description='Скачивание фото поставщиков')
    parser.add_argument('--supplier-id', type=int, help='ID конкретного поставщика')
    parser.add_argument('--wait', action='store_true', help='Ждать завершения всех загрузок')
    args = parser.parse_args()

    with app.app_context():
        from models import Supplier
        from services.photo_cache import get_photo_cache, bulk_download_supplier_photos

        cache = get_photo_cache()

        if args.supplier_id:
            suppliers = Supplier.query.filter_by(id=args.supplier_id).all()
            if not suppliers:
                print(f"❌ Поставщик с ID {args.supplier_id} не найден")
                sys.exit(1)
        else:
            suppliers = Supplier.query.filter_by(is_active=True).all()

        if not suppliers:
            print("❌ Нет активных поставщиков")
            sys.exit(1)

        print(f"📦 Найдено поставщиков: {len(suppliers)}")
        print("=" * 60)

        total_stats = {'total': 0, 'cached': 0, 'queued': 0}

        for supplier in suppliers:
            print(f"\n🏪 {supplier.name} (code={supplier.code}, id={supplier.id})")
            result = bulk_download_supplier_photos(supplier.id)

            total_stats['total'] += result['total_photos']
            total_stats['cached'] += result['already_cached']
            total_stats['queued'] += result['queued']

            print(f"   📸 Всего фото:  {result['total_photos']}")
            print(f"   ✅ В кэше:      {result['already_cached']}")
            print(f"   ⏳ В очереди:   {result['queued']}")

            if result['errors']:
                for err in result['errors']:
                    print(f"   ❌ {err}")

        print("\n" + "=" * 60)
        print(f"📊 Итого:")
        print(f"   Всего фото:    {total_stats['total']}")
        print(f"   Уже в кэше:    {total_stats['cached']}")
        print(f"   В очереди:     {total_stats['queued']}")

        if args.wait and total_stats['queued'] > 0:
            print(f"\n⏳ Ожидаем завершения загрузки...")
            prev_qsize = -1
            while True:
                stats = cache.get_stats()
                qsize = stats['queue_size']
                if qsize == 0:
                    break
                if qsize != prev_qsize:
                    print(f"   Осталось в очереди: {qsize}")
                    prev_qsize = qsize
                time.sleep(2)
            print("✅ Все фото загружены!")
        elif total_stats['queued'] > 0:
            print(f"\n💡 Фото скачиваются в фоне. Используйте --wait для ожидания.")

        # Итоговая статистика
        stats = cache.get_stats()
        print(f"\n📈 Статистика кэша:")
        print(f"   Попаданий:     {stats['cache_hits']}")
        print(f"   Промахов:      {stats['cache_misses']}")
        print(f"   Скачано:       {stats['downloads_completed']}")
        print(f"   Ошибок:        {stats['downloads_failed']}")
        print(f"   Воркеров:      {stats['workers_running']}")


if __name__ == '__main__':
    main()
