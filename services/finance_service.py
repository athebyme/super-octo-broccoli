# -*- coding: utf-8 -*-
"""
Сервис финансов.

Загружает отчёт реализации из WB Statistics API (reportDetailByPeriod),
агрегирует доходы/расходы, кэширует снимки в БД.
"""
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, Any, Optional, List

from models import db, FinanceSnapshot, Seller
from services.wb_api_client import WildberriesAPIClient, WBAPIException

logger = logging.getLogger('finance_service')


class FinanceService:
    """Сервис для работы с финансовыми данными продавца"""

    CACHE_TTL_HOURS = 6  # Кэш актуален 6 часов

    @staticmethod
    def _get_wb_client(seller: Seller) -> WildberriesAPIClient:
        return WildberriesAPIClient(
            api_key=seller.wb_api_key,
            timeout=120,  # reportDetailByPeriod может быть медленным
        )

    @staticmethod
    def _calc_period(period_code: str) -> tuple:
        """Вычислить даты начала/конца из кода периода."""
        today = date.today()
        if period_code == '7d':
            start = today - timedelta(days=7)
        elif period_code == '30d':
            start = today - timedelta(days=30)
        elif period_code == '90d':
            start = today - timedelta(days=90)
        elif period_code == '1y':
            start = today - timedelta(days=365)
        else:
            start = today - timedelta(days=30)
        return start, today

    @classmethod
    def get_cached_snapshot(cls, seller_id: int, period_start: date, period_end: date) -> Optional[FinanceSnapshot]:
        """Получить актуальный кэшированный снимок."""
        cutoff = datetime.utcnow() - timedelta(hours=cls.CACHE_TTL_HOURS)
        return FinanceSnapshot.query.filter(
            FinanceSnapshot.seller_id == seller_id,
            FinanceSnapshot.period_start == period_start,
            FinanceSnapshot.period_end == period_end,
            FinanceSnapshot.created_at >= cutoff,
        ).order_by(FinanceSnapshot.created_at.desc()).first()

    @classmethod
    def fetch_and_cache(
        cls,
        seller: Seller,
        period_code: str = '30d',
        force: bool = False,
    ) -> Dict[str, Any]:
        """
        Получить финансовые данные за период.
        Проверяет кэш, при необходимости загружает из WB API.
        """
        period_start, period_end = cls._calc_period(period_code)

        # Проверяем кэш
        if not force:
            cached = cls.get_cached_snapshot(seller.id, period_start, period_end)
            if cached and cached.report_rows_count:
                logger.info(f"Using cached finance snapshot for seller={seller.id}, period={period_code}")
                return cached.to_dict()

        # При force — удаляем старые снимки за этот период
        if force:
            FinanceSnapshot.query.filter(
                FinanceSnapshot.seller_id == seller.id,
                FinanceSnapshot.period_start == period_start,
                FinanceSnapshot.period_end == period_end,
            ).delete()
            db.session.commit()

        # Загружаем из WB API
        logger.info(f"Fetching finance report from WB API for seller={seller.id}, "
                     f"period={period_start}..{period_end}")

        client = cls._get_wb_client(seller)
        try:
            rows = client.get_sales_report(
                date_from=period_start.isoformat(),
                date_to=period_end.isoformat(),
            )

            if not isinstance(rows, list):
                logger.error(f"WB report returned unexpected type: {type(rows)}")
                if isinstance(rows, dict) and rows.get('errors'):
                    logger.error(f"WB report errors: {rows['errors']}")
                return cls._empty_snapshot(period_start, period_end)

            logger.info(f"WB report returned {len(rows)} rows")

            snapshot = cls._build_snapshot(seller.id, period_start, period_end, rows)
            db.session.add(snapshot)
            db.session.commit()
            logger.info(f"Finance snapshot saved: {snapshot}")

            return snapshot.to_dict()

        except WBAPIException as e:
            logger.error(f"WB API error fetching finance report: {e}")
            # Возвращаем последний доступный кэш
            fallback = FinanceSnapshot.query.filter(
                FinanceSnapshot.seller_id == seller.id,
                FinanceSnapshot.period_start == period_start,
                FinanceSnapshot.period_end == period_end,
            ).order_by(FinanceSnapshot.created_at.desc()).first()
            if fallback:
                return fallback.to_dict()
            return cls._empty_snapshot(period_start, period_end)

        except Exception as e:
            logger.exception(f"Unexpected error fetching finance report: {e}")
            db.session.rollback()
            return cls._empty_snapshot(period_start, period_end)

        finally:
            client.close()

    @classmethod
    def _build_snapshot(
        cls,
        seller_id: int,
        period_start: date,
        period_end: date,
        rows: List[Dict],
    ) -> FinanceSnapshot:
        """Построить агрегированный снимок из строк отчёта реализации."""
        sales_total = 0.0
        for_pay_positive = 0.0
        for_pay_negative = 0.0
        commission_total = 0.0
        logistics_total = 0.0
        storage_total = 0.0
        penalties_total = 0.0
        deductions_total = 0.0
        acceptance_total = 0.0
        additional_payment_total = 0.0

        # Для недельной группировки
        weekly_map = defaultdict(lambda: {
            'forPay': 0, 'commission': 0, 'logistics': 0, 'storage': 0, 'penalties': 0,
        })

        # Для последних транзакций — группируем по отчёту
        report_summaries = defaultdict(lambda: {
            'forPay': 0, 'rows': 0, 'dateFrom': None, 'dateTo': None,
            'reportId': None,
        })

        for row in rows:
            ppvz_for_pay = float(row.get('ppvz_for_pay', 0) or 0)
            retail_withdisc = float(row.get('retail_price_withdisc_rub', 0) or 0)
            commission = float(row.get('ppvz_sales_commission', 0) or 0)
            delivery = float(row.get('delivery_rub', 0) or 0)
            rebill_logistic = float(row.get('rebill_logistic_cost', 0) or 0)
            storage = float(row.get('storage_fee', 0) or 0)
            penalty = float(row.get('penalty', 0) or 0)
            deduction = float(row.get('deduction', 0) or 0)
            acceptance = float(row.get('acceptance', 0) or 0)
            add_payment = float(row.get('additional_payment', 0) or 0)

            doc_type = row.get('doc_type_name', '')
            supplier_oper = row.get('supplier_oper_name', '')

            # Выручка — только продажи
            if doc_type == 'Продажа':
                sales_total += retail_withdisc

            # К перечислению
            if ppvz_for_pay >= 0:
                for_pay_positive += ppvz_for_pay
            else:
                for_pay_negative += ppvz_for_pay

            commission_total += abs(commission)
            logistics_total += abs(delivery) + abs(rebill_logistic)
            storage_total += abs(storage)
            penalties_total += abs(penalty)
            deductions_total += abs(deduction)
            acceptance_total += abs(acceptance)
            additional_payment_total += add_payment

            # Недельная группировка по rr_dt (дата отчёта)
            rr_dt = row.get('rr_dt', '')
            if rr_dt:
                try:
                    dt = datetime.fromisoformat(rr_dt.replace('Z', '+00:00')).date()
                    # Начало недели (понедельник)
                    week_start = dt - timedelta(days=dt.weekday())
                    week_key = week_start.isoformat()
                    weekly_map[week_key]['forPay'] += ppvz_for_pay
                    weekly_map[week_key]['commission'] += abs(commission)
                    weekly_map[week_key]['logistics'] += abs(delivery) + abs(rebill_logistic)
                    weekly_map[week_key]['storage'] += abs(storage)
                    weekly_map[week_key]['penalties'] += abs(penalty)
                except (ValueError, TypeError):
                    pass

            # Группировка по отчёту для транзакций
            report_id = row.get('realizationreport_id')
            if report_id:
                rs = report_summaries[report_id]
                rs['forPay'] += ppvz_for_pay
                rs['rows'] += 1
                rs['reportId'] = report_id

                sale_dt = row.get('sale_dt', '')
                if sale_dt:
                    try:
                        dt_str = datetime.fromisoformat(sale_dt.replace('Z', '+00:00')).strftime('%d.%m.%Y')
                        if not rs['dateFrom'] or sale_dt < rs['dateFrom']:
                            rs['dateFrom'] = sale_dt
                        if not rs['dateTo'] or sale_dt > rs['dateTo']:
                            rs['dateTo'] = sale_dt
                    except (ValueError, TypeError):
                        pass

        # Формируем weekly_data
        weekly_data = []
        for week_key in sorted(weekly_map.keys()):
            w = weekly_map[week_key]
            weekly_data.append({
                'week': week_key,
                'forPay': round(w['forPay'], 2),
                'commission': round(w['commission'], 2),
                'logistics': round(w['logistics'], 2),
                'storage': round(w['storage'], 2),
                'penalties': round(w['penalties'], 2),
            })

        # Формируем транзакции (последние 15 отчётов)
        sorted_reports = sorted(
            report_summaries.values(),
            key=lambda r: r.get('dateTo') or '',
            reverse=True,
        )[:15]

        recent_transactions = []
        for rs in sorted_reports:
            amount = rs['forPay']
            tx_type = 'income' if amount >= 0 else 'expense'
            date_label = ''
            if rs.get('dateTo'):
                try:
                    date_label = datetime.fromisoformat(
                        rs['dateTo'].replace('Z', '+00:00')
                    ).strftime('%d.%m.%Y')
                except (ValueError, TypeError):
                    date_label = rs['dateTo'][:10] if rs['dateTo'] else ''

            recent_transactions.append({
                'description': f"Отчёт реализации #{rs['reportId']}",
                'mp': 'WB',
                'type': tx_type,
                'amount': round(abs(amount), 2),
                'date': date_label,
                'rows': rs['rows'],
            })

        snapshot = FinanceSnapshot(
            seller_id=seller_id,
            period_start=period_start,
            period_end=period_end,
            sales_total=round(sales_total, 2),
            for_pay_total=round(for_pay_positive, 2),
            returns_total=round(for_pay_negative, 2),
            commission_total=round(commission_total, 2),
            logistics_total=round(logistics_total, 2),
            storage_total=round(storage_total, 2),
            penalties_total=round(penalties_total, 2),
            deductions_total=round(deductions_total, 2),
            acceptance_total=round(acceptance_total, 2),
            additional_payment_total=round(additional_payment_total, 2),
            weekly_data=weekly_data,
            recent_transactions=recent_transactions,
            report_rows_count=len(rows),
        )
        return snapshot

    @staticmethod
    def _empty_snapshot(period_start: date, period_end: date) -> Dict[str, Any]:
        return {
            'period_start': period_start.isoformat(),
            'period_end': period_end.isoformat(),
            'balance': {'total': 0, 'income': 0, 'expenses': 0},
            'breakdown': {
                'salesTotal': 0, 'forPayTotal': 0, 'returnsTotal': 0,
                'commission': 0, 'logistics': 0, 'storage': 0,
                'penalties': 0, 'deductions': 0, 'acceptance': 0,
                'additionalPayment': 0,
            },
            'weeklyData': [],
            'recentTransactions': [],
            'reportRowsCount': 0,
            'created_at': None,
        }
