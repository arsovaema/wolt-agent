import re

import httpx
from bs4 import BeautifulSoup


# Parse the complete amount; never truncate a thousands separator.
AMOUNT = r"[0-9]+(?:[.,][0-9]+)*"
PRICE_PATTERN = re.compile(
    rf"MKD\s*({AMOUNT})|({AMOUNT})\s*MKD", re.IGNORECASE,
)


def parse_amount(value):
    from decimal import Decimal, InvalidOperation
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d{2})?", value):
        value = value.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d{2})?", value):
        value = value.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+([.,]\d{1,2})?", value):
        value = value.replace(",", ".")
    else:
        return None
    try:
        amount = Decimal(value)
        return int(amount) if amount == amount.to_integral_value() else None
    except InvalidOperation:
        return None


STOP_HEADINGS = {
    "See similar venues",
    "Address",
    "Delivery times",
    "More information",
    "Partner with Wolt",
    "Company",
    "Products",
    "Useful links",
    "Follow us",
}


def extract_price_from_container(container):
    """
    A valid meal container should contain exactly one unique price.

    This prevents category/section containers from borrowing the
    price of one of their child meals.
    """

    # Prefer the actual selling-price field over descriptions, unit prices,
    # or struck-through original prices.
    fields = container.select('[data-test-id="horizontal-item-card-price"]')
    if fields:
        text = " ".join(field.get_text(" ", strip=True) for field in fields)
    else:
        copy = BeautifulSoup(str(container), "html.parser")
        for old in copy.select('del, s, [data-test-id*="original-price"]'):
            old.decompose()
        text = copy.get_text(" ", strip=True)
    matches = PRICE_PATTERN.findall(text)
    amounts = {parse_amount(left or right) for left, right in matches}
    return next(iter(amounts)) if len(amounts) == 1 and None not in amounts else None


def find_meal_container(tag):
    """
    Starting from the possible meal-name element, walk upward
    and find the smallest parent that contains exactly one price.

    We deliberately stop after a few levels so that we don't
    accidentally reach an entire menu section.
    """

    container = tag.parent

    for _ in range(4):
        if container is None:
            return None

        if len(container.find_all(["h2", "h3", "h4"])) > 1:
            return None
        # Do not stop inside the description before reaching the card price.
        if tag.get("data-test-id") == "horizontal-item-card-header" and not container.select('[data-test-id="horizontal-item-card-price"]'):
            container = container.parent
            continue
        price = extract_price_from_container(container)

        if price is not None:
            container_text = container.get_text(
                " ",
                strip=True,
            )

            # Large containers are very likely sections,
            # not individual menu items.
            if len(container_text) > 700:
                return None

            return container, price

        container = container.parent

    return None


def extract_description(
    container,
    item_name: str,
) -> str:
    """
    Build a description from the individual meal container
    rather than taking text from unrelated siblings.
    """

    text = container.get_text(
        " ",
        strip=True,
    )

    # Remove meal name.
    text = text.replace(
        item_name,
        "",
        1,
    ).strip()

    # Remove price strings.
    text = PRICE_PATTERN.sub(
        "",
        text,
    ).strip()

    # Clean repeated whitespace.
    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    # Strip common separators.
    text = text.strip(" -–—·|")

    return text


def extract_image_url(container: BeautifulSoup) -> str | None:
    """Return the product image URL from the same small meal container."""
    image = container.find("img")
    if image is None:
        source = container.find("source")
        image = source
    if image is None:
        return None

    for attribute in ("src", "data-src", "data-lazy-src", "data-original"):
        value = image.get(attribute)
        if value and isinstance(value, str) and value.startswith(("http://", "https://")):
            return value

    srcset = image.get("srcset") or image.get("data-srcset")
    if isinstance(srcset, str):
        candidates = [part.strip().split(" ", 1)[0] for part in srcset.split(",")]
        for value in reversed(candidates):
            if value.startswith(("http://", "https://")):
                return value
    return None


async def scrape_wolt_menu(
    restaurant_url: str,
) -> list[dict]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/152.0.0.0 Safari/537.36"
        )
    }

    async with httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=30.0,
    ) as client:
        response = await client.get(
            restaurant_url
        )

        response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    menu_items = []

    current_category = None

    # Prevent the same meal from being inserted twice.
    seen_items = set()

    for tag in soup.find_all(
        ["h2", "h3", "h4"]
    ):
        text = tag.get_text(
            " ",
            strip=True,
        )

        if not text:
            continue

        # -----------------------------------
        # Stop once we reach Wolt footer/info
        # -----------------------------------

        if text in STOP_HEADINGS:
            break

        # -----------------------------------
        # Category heading
        # -----------------------------------

        if text.isupper():
            current_category = text
            continue

        # -----------------------------------
        # Possible meal name
        # -----------------------------------

        meal_result = find_meal_container(tag)

        # A meal must resolve to a nearby single-price container. Section
        # headings resolve to larger containers with multiple headings and
        # are rejected below.
        if meal_result is None:
            continue

        container, price_mkd = meal_result

        if price_mkd <= 0:
            continue

        item_name = text.strip()

        if not item_name:
            continue

        # -----------------------------------
        # Basic structural sanity checks
        # -----------------------------------

        if len(item_name) > 120:
            continue

        # Section headings often have many children/items
        # underneath them. A real product container should
        # not contain several heading elements.
        heading_count = len(
            container.find_all(
                ["h2", "h3", "h4"]
            )
        )

        if heading_count > 1:
            continue

        # -----------------------------------
        # Avoid duplicate products
        # -----------------------------------

        unique_key = (
            current_category,
            item_name,
            price_mkd,
        )

        if unique_key in seen_items:
            continue

        seen_items.add(unique_key)

        # -----------------------------------
        # Description from same container
        # -----------------------------------

        description = extract_description(
            container=container,
            item_name=item_name,
        )
        image_url = extract_image_url(container)

        menu_items.append(
            {
                "category": current_category,
                "item_name": item_name,
                "description": description,
                "image_url": image_url,
                "price_mkd": price_mkd,
            }
        )

    return menu_items
