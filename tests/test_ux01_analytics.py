"""Regression checks for the WB analytics summary states and request ordering."""

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'templates/analytics.html'


class AnalyticsTemplateStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = TEMPLATE.read_text(encoding='utf-8')
        start = cls.template.index('function analyticsPage()')
        end = cls.template.index('</script>', start)
        cls.component = cls.template[start:end]

    def run_component(self, scenario):
        program = r'''
const assert = require('node:assert/strict');
'''
        program += self.component
        program += '\n' + scenario
        result = subprocess.run(
            ['node', '-e', program], text=True, capture_output=True, check=True)
        return result.stdout

    def test_failed_first_summary_has_error_without_claiming_kpi_data(self):
        self.assertIn('<template x-if="error">', self.template)
        self.assertIn('<template x-if="hasData">', self.template)
        self.assertIn('displayedPeriodMessage()', self.template)
        self.assertNotIn('hasData || !loading', self.template)
        self.assertIn('role="alert"', self.template)

        self.run_component(r'''
(async () => {
  const state = analyticsPage();
  state.$nextTick = () => {};
  global.fetch = async () => { throw new Error('synthetic first-load failure'); };
  await state.loadData();
  assert.equal(state.hasData, false);
  assert.equal(state.loadedPeriod, null);
  assert.equal(state.error, 'synthetic first-load failure');
  assert.equal(state.loading, false);
  assert.equal(state.kpi.revenue, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
''')

    def test_successful_zero_summary_is_real_data_and_keeps_kpi_visible(self):
        self.run_component(r'''
(async () => {
  const state = analyticsPage();
  state.$nextTick = () => {};
  global.fetch = async url => {
    if (url.startsWith('/api/analytics/summary?')) {
      return {ok: true, json: async () => ({data: {
        kpi: {revenue: 0, orders: 0, avgCheck: 0, buyouts: 0,
          cancels: 0, openCardCount: 0, addToCartCount: 0},
        dynamics: {revenue: null, orders: null, buyouts: null},
        conversions: {addToCartPercent: 0, cartToOrderPercent: 0, buyoutPercent: 0},
        topProducts: [], dailyData: []
      }})};
    }
    return {ok: true, json: async () => ({data: {items: []}})};
  };
  await state.loadData();
  assert.equal(state.hasData, true);
  assert.equal(state.loadedPeriod, '30d');
  assert.equal(state.kpi.revenue, 0);
  assert.equal(state.error, null);
  assert.equal(state.loading, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
''')

    def test_failed_new_period_keeps_loaded_data_and_ignores_delayed_old_success(self):
        self.run_component(r'''
(async () => {
  const state = analyticsPage();
  state.$nextTick = () => {};
  let resolveOldPeriod;
  global.fetch = url => {
    const requestUrl = String(url);
    if (requestUrl.startsWith('/api/analytics/summary?period=30d')) {
      return Promise.resolve({ok: true, json: async () => ({data: {
        kpi: {revenue: 321, orders: 3, avgCheck: 107, buyouts: 2,
          cancels: 0, openCardCount: 10, addToCartCount: 4},
        topProducts: [], dailyData: []
      }})});
    }
    if (requestUrl.startsWith('/api/analytics/summary?period=7d')) {
      return new Promise(resolve => { resolveOldPeriod = resolve; });
    }
    if (requestUrl.startsWith('/api/analytics/summary?period=90d')) {
      return Promise.resolve({ok: false, json: async () => ({error: 'synthetic 90d failure'})});
    }
    return Promise.resolve({ok: true, json: async () => ({data: {items: []}})});
  };

  await state.loadData();
  assert.equal(state.loadedPeriod, '30d');
  state.period = '7d';
  const oldRequest = state.loadData();
  assert.equal(typeof resolveOldPeriod, 'function');
  state.period = '90d';
  await state.loadData();
  assert.equal(state.error, 'synthetic 90d failure');
  assert.equal(state.loadedPeriod, '30d');
  assert.equal(state.displayedPeriodMessage(), 'Показаны данные за 30д.');
  assert.equal(state.kpi.revenue, 321);
  assert.equal(state.staleMessage(),
    'Не удалось загрузить данные за 90д. На экране остаются данные за 30д.');
  assert.equal(state.loading, false);

  resolveOldPeriod({ok: true, json: async () => ({data: {
    kpi: {revenue: 999, orders: 9}, topProducts: [], dailyData: []
  }})});
  await oldRequest;
  assert.equal(state.error, 'synthetic 90d failure');
  assert.equal(state.loadedPeriod, '30d');
  assert.equal(state.kpi.revenue, 321);
  assert.equal(state.period, '90d');
  assert.equal(state.loading, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
''')

    def test_delayed_daily_responses_cannot_replace_new_period_data_or_loading_state(self):
        self.run_component(r'''
(async () => {
  const state = analyticsPage();
  state.$nextTick = () => {};
  global.setTimeout = () => 0;
  global.clearTimeout = () => {};

  const deferred = () => {
    let resolve;
    let reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return {promise, resolve, reject};
  };
  const oldDailySuccess = deferred();
  const oldDailyFailure = deferred();
  const newSummary = deferred();
  const newDaily = deferred();
  const newProducts = deferred();
  let oldDailyCalls = 0;

  global.fetch = url => {
    const requestUrl = String(url);
    if (requestUrl.startsWith('/api/analytics/summary?period=30d')) {
      return Promise.resolve({ok: true, json: async () => ({data: {
        kpi: {revenue: 321, orders: 3, avgCheck: 107, buyouts: 2,
          cancels: 0, openCardCount: 10, addToCartCount: 4},
        topProducts: [], dailyData: [{date: '2026-09-01', orderSum: 321, orderCount: 3}]
      }})});
    }
    if (requestUrl.startsWith('/api/analytics/summary?period=7d')) return newSummary.promise;
    if (requestUrl.startsWith('/api/analytics/products?period=30d')) {
      return Promise.resolve({ok: true, json: async () => ({data: {items: [{id: 'old-period-product'}]}})});
    }
    if (requestUrl.startsWith('/api/analytics/products?period=7d')) return newProducts.promise;
    if (requestUrl.startsWith('/api/analytics/daily?period=30d')) {
      oldDailyCalls += 1;
      return oldDailyCalls === 1 ? oldDailySuccess.promise : oldDailyFailure.promise;
    }
    if (requestUrl.startsWith('/api/analytics/daily?period=7d')) return newDaily.promise;
    throw new Error(`Unexpected synthetic request: ${requestUrl}`);
  };

  await state.loadData();
  await new Promise(setImmediate);
  assert.equal(state.loadedPeriod, '30d');
  assert.equal(state.productsList[0].id, 'old-period-product');
  assert.equal(state.dailyDataPeriod, '30d');

  const oldDailySuccessRequest = state.loadDailyData();
  const oldDailyFailureRequest = state.loadDailyData();
  assert.equal(state.loadingDaily, true);
  const periodChangeRequest = state.changePeriod('7d');
  assert.equal(state.loadingDaily, false);
  assert.equal(state.dailyDataPeriod, '30d');
  assert.equal(state.dailyData[0].orderSum, 321);
  assert.equal(state.productsList[0].id, 'old-period-product');
  assert.equal(state.displayedPeriodMessage(),
    'Показаны данные за 30д; загружаем период 7д.');

  newSummary.resolve({ok: true, json: async () => ({data: {
    kpi: {revenue: 70, orders: 1, avgCheck: 70, buyouts: 1,
      cancels: 0, openCardCount: 2, addToCartCount: 1},
    topProducts: [], dailyData: []
  }})});
  await periodChangeRequest;
  assert.equal(state.loadedPeriod, '7d');
  assert.equal(state.kpi.revenue, 70);
  assert.deepEqual(state.dailyData, []);
  assert.equal(state.dailyDataPeriod, null);
  assert.deepEqual(state.productsList, []);
  assert.equal(state.displayedPeriodMessage(), 'Показаны данные за 7д.');

  const newDailyRequest = state.loadDailyData();
  assert.equal(state.loadingDaily, true);
  oldDailySuccess.resolve({ok: true, json: async () => ({data: [
    {date: '2026-08-01', orderSum: 999, orderCount: 9}
  ]})});
  oldDailyFailure.reject(new Error('late failure for old period'));
  await Promise.all([oldDailySuccessRequest, oldDailyFailureRequest]);
  assert.deepEqual(state.dailyData, []);
  assert.equal(state.dailyError, null);
  assert.equal(state.loadingDaily, true);

  newDaily.resolve({ok: true, json: async () => ({data: [
    {date: '2026-09-24', orderSum: 70, orderCount: 1}
  ]})});
  await newDailyRequest;
  assert.equal(state.dailyDataPeriod, '7d');
  assert.equal(state.dailyData[0].orderSum, 70);
  assert.equal(state.dailyError, null);
  assert.equal(state.loadingDaily, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
''')


if __name__ == '__main__':
    unittest.main()
