#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Запуск агентов WB Seller Platform.

Использование:
  # Запуск конкретного агента:
  python -m agents.runner --agent seo-writer

  # С Cloud.ru GPT-OSS-120B (по умолчанию):
  LLM_PROVIDER=cloudru CLOUDRU_API_KEY=... python -m agents.runner --agent seo-writer

  # С fallback на Claude для сложных агентов:
  FALLBACK_LLM_PROVIDER=claude ANTHROPIC_API_KEY=sk-... python -m agents.runner --all

  # Все агенты (multi-agent режим):
  python -m agents.runner --all

  # Список доступных агентов:
  python -m agents.runner --list

Переменные окружения (или .env файл):
  PLATFORM_URL=http://localhost:5000
  AGENT_ID=<uuid из БД>
  AGENT_API_KEY=<ключ>
  LLM_PROVIDER=cloudru|claude|gemini|openai_compat
  CLOUDRU_API_KEY=<ключ Cloud.ru>
  FALLBACK_LLM_PROVIDER=claude  (опционально, для сложных агентов)
  ANTHROPIC_API_KEY=sk-ant-...  (если fallback=claude)
"""
import argparse
import logging
import os
import sys
import threading
import time

# Добавляем корневую директорию в path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.config import AgentConfig
from agents.catalog.seo_writer import SEOWriterAgent
from agents.catalog.category_mapper import CategoryMapperAgent
from agents.catalog.size_normalizer import SizeNormalizerAgent
from agents.catalog.characteristics_filler import CharacteristicsFillerAgent
from agents.catalog.price_optimizer import PriceOptimizerAgent
from agents.catalog.card_doctor import CardDoctorAgent
from agents.catalog.review_analyst import ReviewAnalystAgent
from agents.catalog.auto_importer import AutoImporterAgent
from agents.catalog.brand_resolver import BrandResolverAgent


# ── Реестр агентов ─────────────────────────────────────────────────

AGENT_REGISTRY = {
    'seo-writer': {
        'class': SEOWriterAgent,
        'display_name': 'Агент SEO',
        'description': 'SEO-оптимизация заголовков и описаний',
        'category': 'content',
    },
    'category-mapper': {
        'class': CategoryMapperAgent,
        'display_name': 'Агент категорий',
        'description': 'Маппинг товаров на категории WB',
        'category': 'catalog',
    },
    'size-normalizer': {
        'class': SizeNormalizerAgent,
        'display_name': 'Агент размеров',
        'description': 'Нормализация размеров и габаритов',
        'category': 'catalog',
    },
    'characteristics-filler': {
        'class': CharacteristicsFillerAgent,
        'display_name': 'Агент характеристик',
        'description': 'Заполнение характеристик карточек WB',
        'category': 'catalog',
    },
    'price-optimizer': {
        'class': PriceOptimizerAgent,
        'display_name': 'Агент цен',
        'description': 'Оптимизация ценообразования',
        'category': 'pricing',
    },
    'card-doctor': {
        'class': CardDoctorAgent,
        'display_name': 'Агент модерации',
        'description': 'Диагностика блокировок и compliance',
        'category': 'compliance',
    },
    'review-analyst': {
        'class': ReviewAnalystAgent,
        'display_name': 'Агент отзывов',
        'description': 'Анализ отзывов и инсайты',
        'category': 'analytics',
    },
    'auto-importer': {
        'class': AutoImporterAgent,
        'display_name': 'Агент импорта',
        'description': 'Полный цикл импорта товаров',
        'category': 'catalog',
    },
    'brand-resolver': {
        'class': BrandResolverAgent,
        'display_name': 'Агент брендов',
        'description': 'Распознавание и нормализация брендов',
        'category': 'content',
    },
}


def _get_model_name() -> str:
    """Возвращает имя текущей модели по провайдеру."""
    p = AgentConfig.LLM_PROVIDER.lower()
    if p == 'cloudru':
        return AgentConfig.CLOUDRU_MODEL
    elif p == 'claude':
        return AgentConfig.CLAUDE_MODEL
    elif p == 'gemini':
        return AgentConfig.GEMINI_MODEL
    elif p == 'openai_compat':
        return AgentConfig.OPENAI_COMPAT_MODEL
    return p


def load_dotenv():
    """Загружает .env файл из корня проекта (без внешних зависимостей)."""
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        '.env'
    )
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def setup_logging(level: str = 'INFO', json_format: bool = None):
    """Настройка логирования. json_format=True для structured JSON логов."""
    if json_format is None:
        json_format = os.getenv('LOG_FORMAT', '').lower() == 'json'

    log_level = getattr(logging, level.upper(), logging.INFO)

    if json_format:
        # Structured JSON logging для агрегации (ELK, Loki, CloudWatch)
        import json as _json

        class JsonFormatter(logging.Formatter):
            def format(self, record):
                log_entry = {
                    'ts': self.formatTime(record, '%Y-%m-%dT%H:%M:%S'),
                    'level': record.levelname,
                    'logger': record.name,
                    'msg': record.getMessage(),
                }
                if record.exc_info and record.exc_info[0]:
                    log_entry['error'] = self.formatException(record.exc_info)
                # Добавляем agent_name если доступен
                agent_name = os.getenv('AGENT_NAME', '')
                if agent_name:
                    log_entry['agent'] = agent_name
                return _json.dumps(log_entry, ensure_ascii=False)

        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logging.root.handlers = [handler]
        logging.root.setLevel(log_level)
    else:
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        )
    # Тихие логеры
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)


def list_agents():
    """Выводит список доступных агентов."""
    print("\n  Доступные агенты WB Seller Platform")
    print("  " + "=" * 55)

    by_category = {}
    for name, info in AGENT_REGISTRY.items():
        cat = info['category']
        by_category.setdefault(cat, []).append((name, info))

    category_labels = {
        'catalog': 'Каталог и импорт',
        'content': 'Контент и SEO',
        'pricing': 'Ценообразование',
        'compliance': 'Модерация',
        'analytics': 'Аналитика',
    }

    for cat, agents in by_category.items():
        label = category_labels.get(cat, cat)
        print(f"\n  [{label}]")
        for name, info in agents:
            print(f"    {name:<26} {info['description']}")

    print(f"\n  Всего: {len(AGENT_REGISTRY)} агентов")
    print(f"  LLM: {AgentConfig.LLM_PROVIDER} ({_get_model_name()})")
    if AgentConfig.FALLBACK_LLM_PROVIDER:
        print(f"  Fallback: {AgentConfig.FALLBACK_LLM_PROVIDER} ({AgentConfig.FALLBACK_LLM_MODEL})")
        print(f"  Сложные агенты (fallback): auto-importer, card-doctor, price-optimizer")
    print()


def run_single_agent(agent_name: str):
    """Запускает один агент."""
    info = AGENT_REGISTRY.get(agent_name)
    if not info:
        print(f"\n  [ОШИБКА] Неизвестный агент: {agent_name}")
        print(f"  Доступные: {', '.join(AGENT_REGISTRY.keys())}")
        sys.exit(1)

    try:
        AgentConfig.validate()
    except ValueError as e:
        print(f"\n  [ОШИБКА КОНФИГУРАЦИИ] Агент '{agent_name}' не может запуститься:")
        for err in str(e).replace('Agent config errors: ', '').split('; '):
            print(f"    - {err}")
        print()
        print("  Убедитесь, что в .env или переменных окружения заданы:")
        print("    AGENT_ID=<uuid из UI: /agents -> Активировать>")
        print("    AGENT_API_KEY=<ключ из UI>")
        print("    CLOUDRU_API_KEY=<ключ Cloud.ru> (если LLM_PROVIDER=cloudru)")
        print()
        sys.exit(1)

    try:
        agent_class = info['class']
        agent = agent_class()
    except Exception as e:
        print(f"\n  [ОШИБКА ИНИЦИАЛИЗАЦИИ] Агент '{agent_name}': {e}")
        logging.getLogger(__name__).error(f"Agent init failed: {e}", exc_info=True)
        sys.exit(1)

    agent_cls = info['class']
    uses_fallback = getattr(agent_cls, 'use_fallback_llm', False) and AgentConfig.FALLBACK_LLM_PROVIDER

    print(f"\n  Запуск агента: {info['display_name']} ({agent_name})")
    if uses_fallback:
        print(f"  LLM: {AgentConfig.FALLBACK_LLM_PROVIDER} ({AgentConfig.FALLBACK_LLM_MODEL}) [fallback]")
    else:
        print(f"  LLM: {AgentConfig.LLM_PROVIDER} ({_get_model_name()})")
    print(f"  Платформа: {AgentConfig.PLATFORM_URL}")
    print(f"  Polling: каждые {AgentConfig.POLL_INTERVAL}с")
    print()

    agent.run()


def run_all_agents():
    """Запускает все агенты в отдельных потоках (multi-agent)."""
    print(f"\n  Multi-agent режим: запуск {len(AGENT_REGISTRY)} агентов")
    print(f"  LLM: {AgentConfig.LLM_PROVIDER}")
    print()

    # Внимание: для multi-agent каждому агенту нужен свой AGENT_ID / AGENT_API_KEY.
    # Этот режим предназначен для разработки и тестирования.
    # В продакшене каждый агент запускается как отдельный процесс.

    agents = []
    threads = []

    for name, info in AGENT_REGISTRY.items():
        agent_class = info['class']
        try:
            agent = agent_class()
            agents.append(agent)
            t = threading.Thread(target=agent.run, name=name, daemon=True)
            threads.append(t)
            t.start()
            print(f"  [+] {info['display_name']} запущен")
        except Exception as e:
            print(f"  [!] {info['display_name']} ошибка: {e}")

    print(f"\n  Запущено {len(threads)} агентов. Ctrl+C для остановки.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Останавливаем агентов...")
        for agent in agents:
            agent.stop()
        print("  Готово.\n")


def main():
    parser = argparse.ArgumentParser(
        description='WB Seller Platform — AI Agent Runner',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  %(prog)s --list                          Список агентов
  %(prog)s --agent seo-writer              Запуск SEO-агента
  %(prog)s --agent category-mapper         Запуск агента категорий
  %(prog)s --all                           Все агенты (dev mode)

Переменные окружения:
  PLATFORM_URL, AGENT_ID, AGENT_API_KEY
  LLM_PROVIDER (cloudru|claude|gemini|openai_compat)
  CLOUDRU_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY
  FALLBACK_LLM_PROVIDER (claude — для сложных агентов)
        """
    )
    parser.add_argument('--agent', '-a', type=str,
                        help='Имя агента для запуска')
    parser.add_argument('--all', action='store_true',
                        help='Запустить все агенты (multi-agent)')
    parser.add_argument('--list', '-l', action='store_true',
                        help='Список доступных агентов')
    parser.add_argument('--log-level', default='INFO',
                        help='Уровень логирования (default: INFO)')

    args = parser.parse_args()

    load_dotenv()
    setup_logging(args.log_level)

    if args.list:
        list_agents()
        return

    if args.agent:
        run_single_agent(args.agent)
        return

    if args.all:
        try:
            AgentConfig.validate()
        except ValueError as e:
            print(f"Ошибка конфигурации: {e}")
            sys.exit(1)
        run_all_agents()
        return

    parser.print_help()


if __name__ == '__main__':
    main()
