"""每日定投状态机回归：模拟时间和上游，只写临时目录。"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import server


class DcaGuidanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_file = os.path.join(directory.name, 'nasdaq_dca_state.json')
        self.valuation = {}
        self.sentiment = {}
        for name, value in [('DATA_DIR', directory.name), ('DCA_STATE_FILE', self.state_file), ('dca_state', None)]:
            setting = patch.object(server, name, value)
            setting.start()
            self.addCleanup(setting.stop)
        for name, attr in [('get_valuation_data', 'valuation'), ('get_fear_greed_data', 'sentiment')]:
            setting = patch.object(server, name, side_effect=lambda now, attr=attr: (200, {'data': getattr(self, attr)}))
            setting.start()
            self.addCleanup(setting.stop)

    def evaluate(self, day, pe=50, emotion=50, valid=True, hour=16):
        now = datetime(2026, 11, 2, hour, tzinfo=server.DCA_TIMEZONE) + timedelta(days=day)
        self.valuation = {'valid': valid, 'pe_percentile': pe, 'data_date': now.date().isoformat()}
        self.sentiment = {'valid': valid, 'score': emotion, 'as_of': now.isoformat()}
        return server.get_nasdaq_dca_data(now.timestamp())['data']

    def test_entry_and_exit_boundaries(self):
        for pe, emotion, expected in [(30, 25, 'increase'), (30.1, 25, 'normal'),
                                      (15, 45, 'increase'), (15, 45.1, 'normal'),
                                      (15.1, 45, 'normal'), (85, 75, 'stop'),
                                      (84.9, 75, 'normal'), (85, 74.9, 'normal'),
                                      (90, 20, 'normal'), (50, 80, 'normal')]:
            with self.subTest(pe=pe, emotion=emotion):
                self.assertEqual(server._dca_target_action('normal', pe, emotion), expected)
        for action, pe, emotion, expected in [('increase', 40, 45, 'increase'),
                                              ('increase', 40.1, 45, 'normal'),
                                              ('increase', 40, 45.1, 'normal'),
                                              ('stop', 80, 65, 'stop'),
                                              ('stop', 79.9, 65, 'normal'),
                                              ('stop', 80, 64.9, 'normal')]:
            with self.subTest(action=action, pe=pe, emotion=emotion):
                self.assertEqual(server._dca_target_action(action, pe, emotion), expected)

    def test_three_days_and_same_day_refresh(self):
        first = self.evaluate(0, 25, 20)
        self.assertEqual((first['action'], first['candidate_count'], first['confirmed']), ('normal', 1, False))
        repeated = self.evaluate(0, 90, 90)
        self.assertEqual(repeated['candidate_action'], 'increase')
        self.assertEqual(repeated['candidate_count'], 1)
        second = self.evaluate(1, 25, 20)
        self.assertEqual(second['candidate_count'], 2)
        third = self.evaluate(2, 25, 20)
        self.assertEqual((third['action'], third['amount_multiplier'], third['confirmed']), ('increase', 1.5, True))

    def test_initial_normal_also_requires_confirmation(self):
        self.assertFalse(self.evaluate(0)['confirmed'])
        self.assertFalse(self.evaluate(1)['confirmed'])
        result = self.evaluate(2)
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['amount_multiplier'], 1)

    def test_increase_buffer_and_confirmed_exit(self):
        for day in range(3):
            self.evaluate(day, 25, 20)
        self.assertEqual(self.evaluate(3, 40, 45)['action'], 'increase')
        first_exit = self.evaluate(4, 41, 45)
        self.assertEqual((first_exit['action'], first_exit['candidate_count']), ('increase', 1))
        self.assertEqual(self.evaluate(7, 41, 45)['candidate_count'], 2)
        self.assertEqual(self.evaluate(8, 41, 45)['action'], 'normal')

    def test_stop_take_profit_and_exit(self):
        for day in range(3):
            result = self.evaluate(day, 90, 85)
        self.assertEqual(result['action'], 'stop')
        self.assertEqual(result['amount_multiplier'], 0)
        self.assertTrue(result['consider_take_profit'])
        self.assertEqual(self.evaluate(3, 80, 65)['candidate_count'], 0)
        self.assertEqual(self.evaluate(4, 79, 65)['candidate_count'], 1)
        self.assertEqual(self.evaluate(7, 79, 65)['candidate_count'], 2)
        result = self.evaluate(8, 79, 65)
        self.assertEqual(result['action'], 'normal')
        self.assertFalse(result['consider_take_profit'])

    def test_cutoff_weekend_and_missing_days(self):
        self.assertEqual(self.evaluate(0, 25, 20, hour=15)['candidate_count'], 0)
        self.assertEqual(self.evaluate(0, 25, 20)['candidate_count'], 1)
        self.assertEqual(self.evaluate(2, 25, 20)['candidate_count'], 1)
        self.assertEqual(self.evaluate(4, 25, 20)['candidate_count'], 1)
        self.assertEqual(self.evaluate(5, 25, 20)['candidate_count'], 1)
        self.assertEqual(self.evaluate(6, 25, 20)['candidate_count'], 1)
        self.assertEqual(self.evaluate(7, 25, 20)['candidate_count'], 2)
        self.assertEqual(self.evaluate(8, 25, 20)['action'], 'increase')

    def test_invalid_data_breaks_confirmation_and_retains_plan(self):
        first = self.evaluate(0, 25, 20)
        missing = self.evaluate(1, 25, 20, valid=False)
        self.assertFalse(missing['valid'])
        self.assertEqual(missing['candidate_count'], 0)
        self.assertEqual(missing['last_valid_at'], first['last_valid_at'])
        self.assertEqual(self.evaluate(2, 25, 20)['candidate_count'], 1)
        self.evaluate(3, 25, 20)
        active = self.evaluate(4, 25, 20)
        missing = self.evaluate(7, valid=False)
        self.assertEqual(missing['action'], 'increase')
        self.assertEqual(missing['last_valid_at'], active['last_valid_at'])

    def test_restart_restores_confirmation_without_duplicate_count(self):
        self.evaluate(0, 25, 20)
        server.dca_state = None
        self.assertEqual(self.evaluate(0, 25, 20)['candidate_count'], 1)
        self.assertEqual(self.evaluate(1, 25, 20)['candidate_count'], 2)
        with open(self.state_file, encoding='utf-8') as source:
            self.assertEqual(json.load(source)['candidate_count'], 2)
        server.dca_state = None
        self.assertEqual(self.evaluate(2, 25, 20)['action'], 'increase')

    def test_failed_persistence_does_not_advance_action(self):
        self.evaluate(0, 25, 20)
        self.evaluate(1, 25, 20)
        with patch.object(server, '_save_dca_state', side_effect=OSError('disk unavailable')):
            result = self.evaluate(2, 25, 20)
        self.assertFalse(result['valid'])
        self.assertEqual(result['action'], 'normal')
        self.assertEqual(result['candidate_count'], 2)
        self.assertEqual(self.evaluate(2, 25, 20)['action'], 'increase')

    def test_invalid_or_expired_sources_cannot_confirm(self):
        now = datetime(2026, 11, 3, 16, tzinfo=server.DCA_TIMEZONE)
        valuation = {'valid': True, 'pe_percentile': 25, 'data_date': now.date().isoformat()}
        sentiment = {'valid': True, 'score': 20, 'as_of': now.isoformat()}
        self.assertTrue(server._dca_inputs_valid(valuation, sentiment, now))
        for pe in [True, None, float('nan'), float('inf'), -1, 101]:
            self.assertFalse(server._dca_inputs_valid({**valuation, 'pe_percentile': pe}, sentiment, now))
        self.assertFalse(server._dca_inputs_valid({**valuation, 'data_date': '2026-10-26'}, sentiment, now))
        self.assertFalse(server._dca_inputs_valid(valuation, {**sentiment, 'as_of': (now - timedelta(hours=73)).isoformat()}, now))


if __name__ == '__main__':
    unittest.main()
