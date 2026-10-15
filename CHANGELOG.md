# Changelog

## 1.11.0 (15 October 2026)

* New setting, **Play podcasts and YouTube in Alfred RSS Player** (Podcasts, in the Workflow's Configuration, off by default). With the free [Alfred RSS Player](https://github.com/haydn-rfp/alfred-rss-player) installed, ↩ plays podcast episodes and YouTube videos in a small floating player in the menu bar, and <kbd>⇧</kbd><kbd>⌘</kbd><kbd>↩</kbd> opens them in the browser instead. Items it can play show ▶.
* Podcast episodes now keep their audio file address, so feeds that link to a web page (or to Spotify) can still be played.
* If the setting is on but the player isn't installed, items open normally and a notification says where to get it.

## 1.10.0 (14 October 2026)

* Feeds that stop updating now stay flagged: a **⚠ feeds not updating** row remains at the top of the list until they recover, instead of appearing once and vanishing. Choose it to see each failing feed, the reason, when it last updated, and to check all feeds again. If every feed fails at once, it says you may be offline.
* YouTube fallback: when YouTube's feed service is down (it periodically returns 404 for every channel), channels are read from their Videos page instead. Older videos that only appear on that page are added to search as read, so they don't flood the unread list.
* Fixed: adding a YouTube channel during an outage now works.

## 1.9.0 (13 October 2026)

* Follow YouTube channels from more kinds of link: a handle on its own (`@name`), a video link (`watch?v=`, `youtu.be`, Shorts, live), and playlists (`playlist?list=`), as well as channel links and channel IDs.
* README: new YouTube section explaining what you can paste and where feed addresses come from.

## 1.8.0 (13 October 2026)

First public release.

* Unread items from all your feeds via one keyword; type to search every item, read or unread.
* Add feeds from a feed URL, a website, a YouTube channel or channel ID. Sites without an advertised feed fall back to an unlisted feed, the sitemap, or the page's headlines.
* Mark items, whole feeds or everything as read. Read items stay searchable.
* Categories, with feeds in as many categories as you like.
* Feed and channel icons, YouTube profile pictures, and previews of each item's description.
* Spotify-hosted podcast episodes play in the Spotify app.
* Configurable keyword, subtitle, history size, unread window and refresh interval.
