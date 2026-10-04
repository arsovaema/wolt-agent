import asyncio

from wolt_scraper import scrape_wolt_menu


restaurants = [
    {
        "name": "Picerija 511",
        "url": "https://wolt.com/en/mkd/skopje/restaurant/picerija-511",
    },
    {
        "name": "Healthy Junk Food",
        "url": "https://wolt.com/en/mkd/skopje/restaurant/healthy-junk-food",
    },
    {
        "name": "Burger Campus",
        "url": "https://wolt.com/en/mkd/skopje/restaurant/burger-campus",
    },
    {
        "name": "Space Burger - Centar",
        "url": "https://wolt.com/en/mkd/skopje/restaurant/space-burger26",
    },
]


async def main():
    for restaurant in restaurants:
        restaurant_name = restaurant["name"]
        restaurant_url = restaurant["url"]

        print("\n")
        print("=" * 80)
        print(f"TESTING: {restaurant_name}")
        print("=" * 80)

        items = await scrape_wolt_menu(
            restaurant_url
        )

        print(
            f"\nFOUND {len(items)} MENU ITEMS "
            f"FROM {restaurant_name}\n"
        )

        for item in items:
            print(
                f"Category: {item['category']}"
            )
            print(
                f"Item: {item['item_name']}"
            )
            print(
                f"Description: {item['description']}"
            )
            print(
                f"Price: {item['price_mkd']} MKD"
            )
            print("-" * 60)


if __name__ == "__main__":
    asyncio.run(main())