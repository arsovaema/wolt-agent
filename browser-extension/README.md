# Wolt Cart Helper (Chrome / Edge)

Local helper. Live v0.1.5 testing confirmed three automatic additions at Healthy Junk Food: Hummus, Grilled Halloumi Naanwich and Heritage Naanwich, quantity one each, 573 MKD total. Payment has not been tested. The helper may pause when Wolt's page structure differs from the supported English interface.

## Install once

1. Open `chrome://extensions` in Chrome or `edge://extensions` in Edge.
2. Turn on **Developer mode**, choose **Load unpacked**, and select this `browser-extension` directory. If using the downloaded ZIP, extract it first and select the extracted directory containing `manifest.json`.
3. Sign in to Wolt in that same browser profile and set Wolt's interface language to English. Reload the meal planner at `http://127.0.0.1:8000` or `http://localhost:8000`.
4. Click a planned day, review its meals, then choose **Yes, prepare in Wolt**. The helper opens the restaurant in your existing browser session.

Review the address, delivery time, quantities, fees and total on Wolt, then pay yourself. The helper never clicks Pay or Place order. Clicking a future plan day does not schedule delivery for that day.

## Update to 0.1.8

Replace the previously extracted extension files with this version, then click Reload on its Chrome/Edge extensions-page card. Refresh the planner and open a new handoff. Old stopped handoffs remain conservative about replaying unknown additions.

Version 0.1.6 accepts repeated menu cards only when their name, selling price and card content match. It reads dedicated selling-price fields and still verifies the Add-button price before clicking. Progress distinguishes confirmed additions from a pending, unconfirmed click. Duplicate-card and price-parser regressions are covered by offline tests; this version has not been exercised against a signed-in cart.

## When it pauses

The helper requires an empty cart, exact restaurant/dish names, unchanged MKD prices, and an unambiguous Add button. Existing items, choices without supported radio defaults, unavailable dishes, login prompts, page changes or an interrupted addition need your attention. It retains Wolt’s existing radio defaults without selecting alternatives; it does not retry uncertain additions. Version 0.1.5 supports explicit empty-cart text and accessible cart labels, keeps Stop disabled after completion/failure, and shows the last preparation step and confirmed progress. When no Add action was attempted, use **Retry automatic preparation** after resolving the blocker; it rechecks the empty cart and automatically adds the meals. Finish remaining items manually in Wolt. Reopening the same handoff does not add its dishes again. Reloading an interrupted Wolt tab does not resume automation.

New plans use three meals from one restaurant per day. Each day is prepared in one helper run. Meals from legacy mixed-restaurant plans need separate checkouts. Finish the current restaurant's order before preparing another. A **Stop helper** control is available in the Wolt tab; closing the planner's dialog does not cancel a handoff already sent.

## Access and development

Content scripts run on Wolt and local pages; the bridge accepts only the planner's two port-8000 origins above. No cookies or passwords are copied. Temporary meal handoffs and duplicate-prevention state use browser session storage. Disable/remove this extension from the extensions page when no longer needed.

Run `node browser-extension/test_helper.cjs` from the project root for validation, message-origin, duplicate prevention and DOM-fixture tests. After changing extension files, click Reload on its extensions-page card and refresh the planner. Production domains and other currencies are not configured.

Version 0.1.7 ignores promotional badges when comparing repeated cards while requiring the same name, description and price. The panel shows its version so an outdated loaded copy is visible.

Version 0.1.8 waits up to five minutes for you to choose options and click Wolt’s Add button. It verifies the added quantity and selected price, including extras, before continuing. It never repeats your manual addition after interruption.
