import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import index_score as m


class ScoreTests(unittest.TestCase):
    def test_invalid_inputs(self):
        for position in range(3):
            for value in (float('nan'), float('inf'), -float('inf'), True):
                args = [0.5, 0, 15]
                args[position] = value
                with self.subTest(position=position, value=value):
                    with self.assertRaises(ValueError):
                        m.compute_scores(*args)
        for args in ((-0.1, 0, 15), (1.1, 0, 15), (0.5, 0, 0), (0.5, 0, -1)):
            with self.assertRaises(ValueError):
                m.compute_scores(*args)

    def test_extreme_deviation_no_overflow(self):
        self.assertEqual(m.compute_scores(.5, 1e308, 15)[1], 0)
        self.assertEqual(m.compute_scores(.5, -1e308, 15)[1], 40)

    def market(self, values, meta):
        df = pd.DataFrame({'Close': values}, index=pd.date_range('2025-01-01', periods=len(values)))
        fake = SimpleNamespace(Ticker=lambda _: SimpleNamespace(history=lambda **kw: df))
        with patch.object(m, 'yf', fake), patch.object(m, 'quote_session_meta', return_value=meta):
            return m.fetch_market('TEST')

    def test_insufficient_valid_closes(self):
        with self.assertRaises(RuntimeError):
            self.market([float('nan')] * 199 + [100], (100, '2025-07-19'))

    def test_append_and_replace_meta(self):
        for day in ('2025-07-19', '2025-07-20'):
            price, ma, dev, asof = self.market([100] * 200, (200, day))
            self.assertEqual((price, ma, asof), (200, 100.5, day))
            self.assertAlmostEqual(dev, (200 / 100.5 - 1) * 100)

    def test_older_meta_ignored(self):
        self.assertEqual(self.market([100] * 200, (200, '2025-07-18'))[:2], (100, 100))

    def test_invalid_prices(self):
        for value in (0, -1, float('inf')):
            with self.assertRaises(ValueError):
                self.market([100] * 199 + [value], (None, None))

    def test_intraday_excluded(self):
        now = datetime(2026, 9, 9, 14, tzinfo=timezone.utc)
        df = pd.DataFrame({'Close': [100, 200]}, index=pd.to_datetime(['2026-09-08', '2026-09-09']))
        with patch.object(m, 'datetime') as clock:
            clock.now.side_effect = lambda tz: now.astimezone(tz)
            clock.fromtimestamp.side_effect = datetime.fromtimestamp
            self.assertEqual(m.completed_closes(df).tolist(), [100])
            response = {'chart': {'result': [{'meta': {'regularMarketPrice': 200, 'regularMarketTime': now.timestamp()}}]}}
            with patch.object(m, 'http_get_json', return_value=response):
                self.assertEqual(m.quote_session_meta('TEST'), (None, None))

    def test_dates(self):
        today = date(2026, 1, 2)
        self.assertEqual(m.danjuan_date('12-31', today), '2025-12-31')
        self.assertEqual(m.danjuan_date('2025-12-31', today), '2025-12-31')
        for value in (None, '', '13-01', '02-30'):
            with self.assertRaises(ValueError):
                m.danjuan_date(value, today)
        with self.assertRaises(ValueError):
            m.date_warnings({'PE': '2026-01-03'}, today)
        notes = m.date_warnings({'PE': '2025-12-01', 'VIX': '2026-01-01'}, today)
        self.assertEqual(len(notes), 2)

    def test_invalid_pe_response(self):
        for value in ('NaN', 'Infinity', 1.1):
            response = SimpleNamespace(json=lambda: {'data': {'items': [
                {'index_code': code, 'pe_percentile': value, 'date': '09-08'} for code in ('NDX', 'SP500')
            ]}})
            with patch.object(m, 'http_get', return_value=response):
                with self.assertRaises(ValueError):
                    m.fetch_danjuan_percentiles()

    def test_report_end_to_end_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            old = os.getcwd()
            os.chdir(directory)
            try:
                with patch.object(m.sys, 'argv', ['index_score.py']), \
                     patch.object(m, 'fetch_market', return_value=(100, 100, 0, '2026-09-08')), \
                     patch.object(m, 'fetch_vix', return_value=(15, '2026-09-07')), \
                     patch.object(m, 'fetch_danjuan_percentiles', return_value={c: {'pe_percentile': .5, 'date': '2026-09-08'} for c in ('NDX', 'SP500')}), \
                     patch.object(m, 'fetch_shiller_pe_series', side_effect=RuntimeError('offline')):
                    self.assertEqual(m.main(), 0)
                    previous = {name: Path(name).read_bytes() for name in ('result.json', 'result.html')}
                    with patch.object(m, 'fetch_market', return_value=(float('nan'), 100, 0, '2026-09-08')):
                        with self.assertRaises(ValueError):
                            m.main()
                    for name, content in previous.items():
                        self.assertEqual(Path(name).read_bytes(), content)
                result = json.loads(Path('result.json').read_text(encoding='utf-8'))
                self.assertEqual(result['ndx']['vix_as_of'], '2026-09-07')
                self.assertEqual(result['spx']['pe_as_of'], '2026-09-08')
                self.assertTrue(result['warnings'])
                self.assertIsNone(result['spx']['shiller_pe_percentile'])
                self.assertIn('2026-09-07', Path('result.html').read_text(encoding='utf-8'))
            finally:
                os.chdir(old)


if __name__ == '__main__':
    unittest.main()
