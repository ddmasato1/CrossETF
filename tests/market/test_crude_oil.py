"""原油期货行情解析、批量请求及宏观隔离回归，无网络或归档写入。"""
import unittest
from unittest.mock import patch

from app.market import quotes as server


class CrudeOilTests(unittest.TestCase):
    def quote(self, symbol, price, settle, size=15):
        fields = [''] * 15
        fields[0], fields[6], fields[7] = price, '15:48:53', settle
        fields[12], fields[13] = '2026-11-03', 'test oil'
        return f'var hq_str_{symbol}="' + ','.join(fields[:size]) + '";'

    def payload(self, raw):
        registry = dict(status='test', status_desc='test', cached_date='test', updated_at='test',
                        broad_etfs=[], sector_etfs=[], sp500_etfs=[])
        with patch.object(server, 'cache_data', dict(timestamp=0, payload=None)), \
             patch.object(server, 'discover_etfs_online', return_value=registry), \
             patch.object(server, 'record_daily_history'), \
             patch.object(server, 'fetch_url', side_effect=lambda url, **kw: raw if 'hq.sinajs' in url else '') as upstream:
            result = server.get_market_data(force=True)
            self.assertEqual(upstream.call_count, 2)
            sina_url = upstream.call_args_list[1].args[0]
            self.assertIn('hf_CL,hf_OIL', sina_url)
            return result['macro']

    def test_both_benchmarks_and_settlement_changes(self):
        raw = self.quote('hf_CL', '97.807', '96.460') + self.quote('hf_OIL', '104.45', '104.77')
        oil = self.payload(raw)['crude_oil']
        self.assertEqual(oil['wti'], dict(price=97.81, settle=96.46, change=1.35,
                                        change_pct=1.40, valid=True, date='2026-11-03', time='15:48:53'))
        self.assertTrue(oil['brent']['valid'])
        self.assertEqual(oil['brent']['change'], -0.32)
        self.assertEqual(oil['brent']['change_pct'], -0.31)

    def test_invalid_wti_does_not_hide_brent(self):
        for price, settle, size in [('bad', '96', 15), ('nan', '96', 15), ('inf', '96', 15),
                                    ('97', '', 15), ('97', '0', 15), ('97', '-1', 15),
                                    ('97', 'nan', 15), ('97', 'inf', 15), ('97', '96', 8)]:
            with self.subTest(price=price, settle=settle, size=size):
                oil = self.payload(self.quote('hf_CL', price, settle, size) + self.quote('hf_OIL', '100', '100'))['crude_oil']
                self.assertFalse(oil['wti']['valid'])
                self.assertIsNone(oil['wti']['price'])
                self.assertIsNone(oil['wti']['change_pct'])
                self.assertTrue(oil['brent']['valid'])
                self.assertEqual(oil['brent']['change_pct'], 0)

    def test_missing_quotes_and_oil_isolation(self):
        baseline = self.payload('')
        self.assertFalse(baseline['crude_oil']['wti']['valid'])
        self.assertFalse(baseline['crude_oil']['brent']['valid'])
        with_oil = self.payload(self.quote('hf_CL', '97', '96') + self.quote('hf_OIL', '106', '104'))
        for key in baseline:
            if key != 'crude_oil':
                self.assertEqual(with_oil[key], baseline[key], key)

    def test_nonpositive_oil_prices_remain_representable(self):
        # 原油期货曾出现负价格；昨结算价有效时保留真实报价。
        for price in ['0', '-37.63']:
            with self.subTest(price=price):
                oil = self.payload(self.quote('hf_CL', price, '18.27'))['crude_oil']['wti']
                self.assertTrue(oil['valid'])
                self.assertEqual(oil['price'], float(price))


if __name__ == '__main__':
    unittest.main()
