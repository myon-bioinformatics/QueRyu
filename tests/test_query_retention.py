import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cache_share


class QueryRetentionTests(unittest.TestCase):
    def test_latest_order_filters_and_history(self):
        rows=[{'key':str(i%3),'status':200,'url':'https://example.test','sequence':i} for i in range(100)]
        with patch.object(cache_share,'_rows',return_value=rows):
            self.assertEqual(cache_share.query(None),rows[-3:])
            self.assertEqual(cache_share.query(None,history=True),rows)
            self.assertEqual(cache_share.query(None,key='1'),[rows[97]])
            self.assertEqual(cache_share.query(None,status=404),[])
        rows.append({'key':'0','status':500})
        with patch.object(cache_share,'_rows',return_value=rows):
            self.assertEqual(cache_share.query(None)[-1]['sequence'],99)
            self.assertEqual(cache_share.query(None,with_body=False)[-1]['status'],500)


if __name__=='__main__': unittest.main()
