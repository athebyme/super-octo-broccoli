"""
Модуль для работы с Telegram уведомлениями
"""
import requests
import logging
from typing import Optional, Dict, Any
from datetime import datetime


logger = logging.getLogger(__name__)


class TelegramBot:
    """Класс для работы с Telegram Bot API"""

    def __init__(self, bot_token: str):
        """
        Инициализация бота

        Args:
            bot_token: Токен бота от @BotFather
        """
        self.bot_token = bot_token
        self.api_url = f"https://api.telegram.org/bot{bot_token}"

    def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str = 'HTML',
        disable_web_page_preview: bool = True
    ) -> Optional[Dict[str, Any]]:
        """
        Отправить сообщение в чат

        Args:
            chat_id: ID чата (может быть username канала с @)
            text: Текст сообщения
            parse_mode: Режим парсинга ('HTML', 'Markdown', None)
            disable_web_page_preview: Отключить превью ссылок

        Returns:
            Ответ от API или None при ошибке
        """
        url = f"{self.api_url}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': parse_mode,
            'disable_web_page_preview': disable_web_page_preview
        }

        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            result = response.json()

            if result.get('ok'):
                logger.info(f"Telegram message sent to {chat_id}")
                return result.get('result')
            else:
                logger.error(f"Telegram API error: {result.get('description')}")
                return None

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send Telegram message: {str(e)}")
            return None

    def get_me(self) -> Optional[Dict[str, Any]]:
        """
        Получить информацию о боте

        Returns:
            Информация о боте или None при ошибке
        """
        url = f"{self.api_url}/getMe"

        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            result = response.json()

            if result.get('ok'):
                return result.get('result')
            else:
                logger.error(f"Telegram API error: {result.get('description')}")
                return None

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get bot info: {str(e)}")
            return None

    def test_connection(self, chat_id: str) -> bool:
        """
        Проверить соединение с ботом и доступность чата

        Args:
            chat_id: ID чата для проверки

        Returns:
            True если все работает, False при ошибке
        """
        # Проверяем токен
        bot_info = self.get_me()
        if not bot_info:
            return False

        # Пробуем отправить тестовое сообщение
        test_message = "✅ Подключение к Telegram успешно настроено!"
        result = self.send_message(chat_id, test_message)

        return result is not None


class TelegramNotifier:
    """Класс для отправки различных типов уведомлений"""

    def __init__(self, bot_token: str, chat_id: str):
        """
        Инициализация

        Args:
            bot_token: Токен бота
            chat_id: ID чата для уведомлений
        """
        self.bot = TelegramBot(bot_token)
        self.chat_id = chat_id

    def send_low_stock_alert(self, product_data: Dict[str, Any]) -> bool:
        """
        Отправить уведомление о низком остатке товара

        Args:
            product_data: Данные о товаре (nm_id, vendor_code, title, quantity)

        Returns:
            True если отправлено успешно
        """
        message = f"""
🔴 <b>Низкий остаток товара!</b>

📦 Товар: {product_data.get('title', 'Без названия')}
🏷 Артикул WB: {product_data.get('nm_id')}
📋 Артикул продавца: {product_data.get('vendor_code')}
📊 Остаток: <b>{product_data.get('quantity', 0)} шт.</b>

⚠️ Необходимо пополнить запас!
        """.strip()

        result = self.bot.send_message(self.chat_id, message)
        return result is not None

    def send_price_change_alert(self, product_data: Dict[str, Any], change_data: Dict[str, Any]) -> bool:
        """
        Отправить уведомление об изменении цены

        Args:
            product_data: Данные о товаре
            change_data: Данные об изменении (old_price, new_price, change_percent)

        Returns:
            True если отправлено успешно
        """
        old_price = change_data.get('old_price', 0)
        new_price = change_data.get('new_price', 0)
        change_percent = change_data.get('change_percent', 0)

        # Определяем направление изменения
        emoji = "📈" if change_percent > 0 else "📉"
        direction = "увеличилась" if change_percent > 0 else "уменьшилась"

        message = f"""
{emoji} <b>Изменение цены!</b>

📦 Товар: {product_data.get('title', 'Без названия')}
🏷 Артикул WB: {product_data.get('nm_id')}

💰 Старая цена: {old_price:.2f} ₽
💰 Новая цена: {new_price:.2f} ₽
📊 Изменение: {abs(change_percent):.1f}% ({direction})

🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}
        """.strip()

        result = self.bot.send_message(self.chat_id, message)
        return result is not None

    def send_sync_error_alert(self, error_message: str) -> bool:
        """
        Отправить уведомление об ошибке синхронизации

        Args:
            error_message: Текст ошибки

        Returns:
            True если отправлено успешно
        """
        message = f"""
❌ <b>Ошибка синхронизации с WB API!</b>

{error_message}

🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}

Проверьте настройки API ключа и повторите попытку.
        """.strip()

        result = self.bot.send_message(self.chat_id, message)
        return result is not None

    def send_import_complete_alert(self, stats: Dict[str, int]) -> bool:
        """
        Отправить уведомление о завершении импорта

        Args:
            stats: Статистика импорта (imported, skipped, failed)

        Returns:
            True если отправлено успешно
        """
        message = f"""
✅ <b>Импорт товаров завершен!</b>

📥 Импортировано: {stats.get('imported', 0)} шт.
⏭ Пропущено: {stats.get('skipped', 0)} шт.
❌ Ошибок: {stats.get('failed', 0)} шт.

🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}
        """.strip()

        result = self.bot.send_message(self.chat_id, message)
        return result is not None

    def send_bulk_operation_complete_alert(self, operation_data: Dict[str, Any]) -> bool:
        """
        Отправить уведомление о завершении массовой операции

        Args:
            operation_data: Данные об операции (description, success_count, error_count)

        Returns:
            True если отправлено успешно
        """
        message = f"""
✅ <b>Массовая операция завершена!</b>

📝 Операция: {operation_data.get('description', 'Без описания')}

✅ Успешно: {operation_data.get('success_count', 0)} шт.
❌ Ошибок: {operation_data.get('error_count', 0)} шт.

🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}
        """.strip()

        result = self.bot.send_message(self.chat_id, message)
        return result is not None

    def send_daily_summary(self, summary_data: Dict[str, Any]) -> bool:
        """
        Отправить ежедневную сводку

        Args:
            summary_data: Данные сводки (total_products, low_stock_count, price_changes, etc.)

        Returns:
            True если отправлено успешно
        """
        message = f"""
📊 <b>Ежедневная сводка</b>

📦 Всего товаров: {summary_data.get('total_products', 0)}
✅ Активных: {summary_data.get('active_products', 0)}
⚠️ Низкие остатки: {summary_data.get('low_stock_count', 0)}

💰 Изменений цен за сутки: {summary_data.get('price_changes', 0)}
📈 Средняя цена: {summary_data.get('avg_price', 0):.2f} ₽
📊 Общий остаток: {summary_data.get('total_stock', 0)} шт.

🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}
        """.strip()

        result = self.bot.send_message(self.chat_id, message)
        return result is not None

    def test_connection(self) -> bool:
        """
        Проверить подключение

        Returns:
            True если все работает
        """
        return self.bot.test_connection(self.chat_id)
