import unittest
from fastapi import HTTPException
from ordering import group_meals,restaurant_url


class OrderingTests(unittest.TestCase):
    def fixtures(self):
        meals=[{'id':i,'item_name':f'Meal {i}','price_mkd':200} for i in range(1,4)]
        rows=[{'id':i,'restaurant_id':1 if i<3 else 2,'restaurant_name':f'Venue {1 if i<3 else 2}',
               'item_name':f'Meal {i}','category':'Meals','price_mkd':200,'is_available':True,
               'url':f'https://wolt.com/en/mkd/skopje/restaurant/venue-{1 if i<3 else 2}'} for i in range(1,4)]
        return meals,rows

    def test_groups_meals_into_separate_restaurant_checkouts(self):
        groups=group_meals(*self.fixtures())
        self.assertEqual(len(groups),2)
        self.assertEqual([group['food_subtotal_mkd'] for group in groups],[400,200])
        self.assertTrue(all(meal['quantity']==1 for group in groups for meal in group['meals']))

    def test_price_name_and_availability_changes_block_handoff(self):
        for changes in ({'price_mkd':250},{'item_name':'different dish'},{'is_available':False}):
            meals,rows=self.fixtures()
            rows[0].update(changes)
            with self.assertRaises(HTTPException):
                group_meals(meals,rows)

    def test_repeated_or_missing_meals_are_rejected(self):
        meals,rows=self.fixtures()
        for invalid in ([meals[0]]*3,meals[:2],[{'id':99,'price_mkd':200},*meals[1:]]):
            with self.assertRaises(HTTPException):
                group_meals(invalid,rows)

    def test_only_wolt_restaurant_links_are_allowed(self):
        for value in ('https://evil.test/en/mkd/skopje/restaurant/x','javascript:alert(1)',
                      'https://wolt.com:bad/en/mkd/skopje/restaurant/x',
                      'https://wolt.com@evil.test/en/mkd/skopje/restaurant/x','https://wolt.com/checkout',
                      'http://wolt.com/en/mkd/skopje/restaurant/x'):
            with self.assertRaises(HTTPException):
                restaurant_url(value)
        self.assertEqual(restaurant_url('https://wolt.com/en/mkd/skopje/restaurant/x?bad=1#anchor'),
                         'https://wolt.com/en/mkd/skopje/restaurant/x')


class LiveMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_removed_toast_blocks_before_handoff(self):
        from unittest.mock import AsyncMock, patch
        from ordering import verify_live_menu
        groups=[{'url':'https://wolt.com/en/mkd/skopje/restaurant/space-burger26',
                 'meals':[{'name':'Yellow Star тост','price_mkd':140}]}]
        with patch('wolt_scraper.scrape_wolt_menu', AsyncMock(return_value=[{'item_name':'Yellow Star Burger','price_mkd':250}])):
            with self.assertRaises(HTTPException) as error:
                await verify_live_menu(groups)
            self.assertEqual(error.exception.status_code,409)

    async def test_duplicate_valid_cards_are_accepted(self):
        from unittest.mock import AsyncMock, patch
        from ordering import verify_live_menu
        item={'item_name':'Шиш Таук Врап M','price_mkd':280}
        groups=[{'url':'https://wolt.com/en/mkd/skopje/restaurant/levant-food',
                 'meals':[{'name':item['item_name'],'price_mkd':280}]}]
        with patch('wolt_scraper.scrape_wolt_menu', AsyncMock(return_value=[item,item])):
            await verify_live_menu(groups)
        with patch('wolt_scraper.scrape_wolt_menu', AsyncMock(return_value=[item | {'price_mkd':300}])):
            with self.assertRaises(HTTPException):
                await verify_live_menu(groups)

    async def test_failed_scrape_does_not_send_unverified_cart(self):
        from unittest.mock import AsyncMock, patch
        from ordering import verify_live_menu
        for response in ([],):
            with patch('wolt_scraper.scrape_wolt_menu', AsyncMock(return_value=response)):
                with self.assertRaises(HTTPException) as error:
                    await verify_live_menu([{'url':'https://wolt.com','meals':[]}])
                self.assertEqual(error.exception.status_code,503)


if __name__=='__main__':
    unittest.main()
