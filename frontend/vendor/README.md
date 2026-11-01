# Vendored libraries

"Vendored" means a copy of someone else's library is kept inside this repository, so the
dashboard works with no internet connection and always runs the exact version that was checked.

## Chart.js

| Item | Value |
|------|-------|
| File | `chart.umd.min.js` (the minified UMD build: one file that adds a global `Chart`) |
| Version | 4.6.1 (the latest 4.x stable release on the day it was downloaded, 2026-11-01) |
| Source URL | https://cdn.jsdelivr.net/npm/chart.js@4.6.1/dist/chart.umd.min.js |
| Source | jsDelivr, the open-source CDN that serves files from the official `chart.js` package on npm |
| Licence | MIT (stated in the file header) |
| Size | 210082 bytes |
| Checksum | sha256: 87364cc2b2a76a6684929e7d6b80b18a897794db38400caff1fcf1791c6925d2 |

The owner approved this one download (owner decision 2 of the T044 to T061 batch). The file is
served as-is and never edited. A test (`tests/integration/test_frontend_static.py`) checks the
file against the checksum above, so an accidental change is caught.

To update it later: download the new version from the same place, replace the file, and update
the version, URL, size and checksum in this table in the same commit.
