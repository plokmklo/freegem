# Live 1000-site scrape - aggregated results

- urls attempted: **1000**
- ok: **915** (91.5%)
- failed: **85**
- thin extractions (ok but < 200 chars): **95**

## Failures by bucket

| bucket | count | example domains |
| --- | --- | --- |
| refused | 78 | w2rt6byl5nf1opmc-a.com, macromedia.com, youtube-nocookie.com, cpanel.net, wixsite.com |
| other | 6 | bit.ly, wiley.com, mailchimp.com, typeform.com, drupal.org |
| timeout | 1 | ed.ac.uk |

## Successes by via

| via | count |
| --- | --- |
| html | 732 |
| reader | 168 |
| wayback | 9 |
| ai-search | 6 |

## Extracted chars (ok rows)

mean 7464, p50 3666, p90 19658, max 50000

## Sample errors per bucket

### refused (78)

- `SSRF guard refused 'https://w2rt6byl5nf1opmc-a.com': cannot resolve 'w2rt6byl5nf1opmc-a.com': [Errno -2] Name or service not known` (https://w2rt6byl5nf1opmc-a.com)
- `every read path refused 'https://macromedia.com' — honest failure taxonomy [network]: direct: fetch failed for 'https://macromedia.com': <urlopen error timed ou` (https://macromedia.com)
- `every read path refused 'https://youtube-nocookie.com' — honest failure taxonomy [network]: direct: fetch failed for 'https://youtube-nocookie.com': <urlopen er` (https://youtube-nocookie.com)
- `[2026-10-31 01:06:39] scrape refused: cannot resolve 'wixsite.com': [Errno -5] No address associated with hostname` (https://cpanel.net)
- `SSRF guard refused 'https://wixsite.com': cannot resolve 'wixsite.com': [Errno -5] No address associated with hostname` (https://wixsite.com)

### other (6)

- `runner: no record, exit 0` (https://bit.ly)

### timeout (1)

- `batch item timed out after 300s without finishing` (https://ed.ac.uk)
