---
name: page-watch
description: Watch a web page, blog or RSS/Atom feed (prices, stock, new posts, status pages) and post in a channel when it changes.
---

<!-- Adapted from OpenClaw's bundled "blogwatcher" skill (MIT, © OpenClaw Foundation), using the watch_page tool. -->

# Page / feed watcher

1. Get: the URL (page, blog or RSS feed), what counts as a change (price, stock, new post, status), and the channel
   to post in (default: the current one). Ask one short question only if the URL is missing.
2. Check it once with `read` so you know the page works, then start watching with `watch_page`.
3. Confirm in one line: what is watched, how often, and where updates go.
4. To list or stop watches, point them to `/watch list` and `/watch remove`.
