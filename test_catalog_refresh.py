"""Regression: unchanged prices still renew a genuinely observed menu item."""
import unittest
from unittest.mock import AsyncMock, patch, MagicMock
from scripts import refresh_menu_prices as refresh


class CatalogRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_unambiguous_observed_items_become_fresh(self):
        db=MagicMock()
        db.__aenter__=AsyncMock(return_value=db)
        db.__aexit__=AsyncMock(return_value=False)
        restaurants=MagicMock()
        restaurants.mappings.return_value.all.return_value=[{'id':1,'name':'Fixture','url':'https://example.invalid'}]
        rows=MagicMock()
        rows.mappings.return_value.all.return_value=[{'id':1,'item_name':'Unchanged','price_mkd':200},
            {'id':2,'item_name':'Removed','price_mkd':200},{'id':3,'item_name':'Ambiguous','price_mkd':200}]
        calls=[]
        async def execute(query,params=None):
            sql=str(query)
            calls.append((sql,params))
            return restaurants if 'SELECT id,name,url' in sql else rows
        db.execute=AsyncMock(side_effect=execute)
        db.commit=AsyncMock()
        items=[{'item_name':'Unchanged','price_mkd':200},{'item_name':'Ambiguous','price_mkd':200},
               {'item_name':'Ambiguous','price_mkd':250}]
        with patch.object(refresh.main,'async_session',return_value=db), patch.object(refresh,'scrape_wolt_menu',AsyncMock(return_value=items)):
            await refresh.run(dispose_engine=False)
        fresh=[params['id'] for sql,params in calls if 'SET last_seen_at=' in sql]
        self.assertEqual(fresh,[1])
        removed=[params['id'] for sql,params in calls if 'SET is_available=false' in sql]
        self.assertEqual(removed,[2])
        db.commit.assert_awaited_once()
