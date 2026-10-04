import unittest
from bs4 import BeautifulSoup
from wolt_scraper import extract_price_from_container, find_meal_container


class MenuPriceTests(unittest.TestCase):
    def test_amount_formats(self):
        for text, expected in [('MKD 1,200',1200), ('MKD 1.200,00',1200),
                               ('1,200.00 MKD',1200), ('MKD\u00a0280',280),
                               ('MKD 280.50',None), ('MKD 1,23,4',None)]:
            with self.subTest(text=text):
                self.assertEqual(extract_price_from_container(BeautifulSoup(f'<div>{text}</div>', 'html.parser')),expected)

    def test_forum_remi_cream_thousands_price(self):
        # Forum's public card: the previous parser rounded 1,870 to 2 MKD.
        soup = BeautifulSoup('<div><button data-test-id="horizontal-item-card-button"></button>\n            <div><div><h3 data-test-id="horizontal-item-card-header">Реми крем 350gr</h3>\n            <p>Јунешки бифтек на скара, гарнир по желба, крем бел сос</p></div>\n            <div><span data-test-id="horizontal-item-card-price" aria-label="Price MKD\xa01,870">MKD\xa01,870</span></div></div></div>', 'html.parser')
        self.assertEqual(find_meal_container(soup.h3)[1], 1870)

    def test_card_price_not_description_or_original(self):
        soup=BeautifulSoup('''<div><div><h3 data-test-id="horizontal-item-card-header">Wrap</h3>
            <p>Add a drink for MKD 50</p></div><div>
            <span data-test-id="horizontal-item-card-price">MKD 280</span>
            <del>MKD 350</del></div></div>''', 'html.parser')
        self.assertEqual(find_meal_container(soup.h3)[1],280)

    def test_cannot_borrow_neighbor_price(self):
        soup=BeautifulSoup('<section><div><h3>Missing</h3></div><div><h3>Other</h3>MKD 280</div></section>','html.parser')
        self.assertIsNone(find_meal_container(soup.h3))

    def test_conflicting_prices_rejected(self):
        soup=BeautifulSoup('<div><span data-test-id="horizontal-item-card-price">MKD 280</span><span data-test-id="horizontal-item-card-price">MKD 350</span></div>','html.parser')
        self.assertIsNone(extract_price_from_container(soup))

if __name__=='__main__':
    unittest.main()
