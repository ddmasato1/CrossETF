"""年初至今字段的真实行情解析回归（模拟上游，无网络或归档写入）。"""
import unittest
from unittest.mock import patch
import server


class YtdReturnsTests(unittest.TestCase):
    def quote(self, value, price='2.243', size=82):
        fields = [''] * size
        fields[2], fields[3] = '513100', price
        if size > 62:
            fields[62] = value
        if size > 81:
            fields[77], fields[78], fields[81] = '15.76', '1.9377', '1.9960'
        return 'v_sh513100="' + '~'.join(fields) + '";'

    def get_payload(self, raw):
        item = dict(code='513100', name='test', symbol='sh513100')
        registry = dict(status='test', status_desc='test', cached_date='test', updated_at='test',
                        broad_etfs=[item], sector_etfs=[item], sp500_etfs=[item])
        with patch.object(server, 'cache_data', dict(timestamp=0, payload=None)), \
             patch.object(server, 'discover_etfs_online', return_value=registry), \
             patch.object(server, 'record_daily_history'), \
             patch.object(server, 'fetch_url', side_effect=lambda url, **kw: raw if 'qt.gtimg' in url else ''):
            return server.get_market_data(force=True)

    def test_values_and_invalid_fields_across_categories(self):
        for raw, expected in [('17.80', 17.8), ('0', 0.0), ('-8.56', -8.56),
                              ('', None), (' ', None), ('bad', None),
                              ('nan', None), ('inf', None)]:
            with self.subTest(value=raw):
                payload = self.get_payload(self.quote(raw))
                for key in ['nasdaq100_etfs', 'sector_etfs', 'sp500_etfs']:
                    item = payload[key][0]
                    self.assertEqual(item['ytd_change_pct'], expected)
                    self.assertEqual(item['iopv'], 1.9377)
                    self.assertEqual(item['est_premium'], 15.76)

    def test_short_quote_and_missing_price(self):
        for raw in [self.quote('', size=40), self.quote('12.3', price='0'), '']:
            with self.subTest(raw=raw):
                payload = self.get_payload(raw)
                self.assertIsNone(payload['nasdaq100_etfs'][0]['ytd_change_pct'])


if __name__ == '__main__':
    unittest.main()
