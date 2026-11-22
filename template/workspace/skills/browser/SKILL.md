---
name: browser
description: Driving the real headless browser — store/category pages, React/Next sites, stock checks, clicking tabs, screenshots with vision.
---
# Browser playbook
Tools: `read` (one page, fast) · `page_items` (product/plan cards: price, old price, stock, buttons) ·
`browse` (open + click/type/scroll, then read) · `look` (screenshot + vision) · `run_steps` (several in a row).

1. Price / stock on a store site → `page_items | <url> | <product words>`. Answer from the cards:
   `• **Name** — ৳price (was ৳old) · ✅ in stock / ❌ sold out`. "disabled" buy button = not available.
2. Products hidden behind a tab/category/search → `browse | <url> | click <category>; wait 2` or
   `browse | <url> | type search=<words>`. Never click buy / add to cart / login / submit — the browser refuses anyway.
3. Page text looks empty or the answer is in an image/banner → `look | <url> | <what to find>`.
4. Several pages or steps → `run_steps` with one `tool | input` per line, e.g.
   `page_items | blockifystore.xyz | cape` then `calc | 450*3`.
5. If the item isn't on the page, say so plainly and name the page you checked. Never guess a price or stock.
