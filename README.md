# Flight Deal Monitor — BUD to USA / Japan / Europe

Checks cached fare data out of Budapest (via Travelpayouts, which mirrors
Aviasales/Kiwi pricing) for round trips to the US, Japan, and wildcard
European hops, including budget-carrier combos found by searching legs
separately (e.g. Wizz Air BUD-LGW + Norse Atlantic LGW-JFK as two searches).
Pushes an iPhone notification via [ntfy.sh](https://ntfy.sh) whenever a fare
clears the "crazy deal" bar.

**Why Travelpayouts and not Kiwi/Amadeus directly:** Kiwi's Tequila API is
now partner-only (no self-serve signup), and Amadeus shut down its free
self-service API portal in July 2026. Travelpayouts is the option that's
still genuinely free with no credit card and no approval process -- the
trade-off is it's cached data (see caveats below), not a live search.

## One-time setup (~5 minutes)

1. **Get a free Travelpayouts token** (you'll need to do this step yourself —
   it requires creating an account)
   - Sign up at https://www.travelpayouts.com/
   - Find your API token in your account dashboard (Tools / API section)
   - Optional: note your affiliate "marker" too, used to build booking links

2. **Set up push notifications on your iPhone**
   - Install the free **ntfy** app from the App Store: https://apps.apple.com/app/ntfy/id1625396347
   - Pick a private topic name only you know, e.g. `bud-flight-deals-9f3k`
   - In the app, tap "+" and subscribe to that exact topic name.

3. **Provide both values to Claude** (`TRAVELPAYOUTS_TOKEN` and `NTFY_TOPIC`,
   plus optionally `TRAVELPAYOUTS_MARKER`) so the scheduled task can be wired
   up, or set them yourself as environment variables if running it manually.

## Run manually

```bash
TRAVELPAYOUTS_TOKEN=your_token NTFY_TOPIC=your_topic python flight_deals.py
```

## Important caveats of this data source

- **Cached, not live.** Every fare is "the last price someone's search
  found," not a live quote. A surfaced deal can be gone by the time you
  click through -- treat alerts as "go check this now."
- **No seat-availability data.** Unlike Kiwi Tequila, this API doesn't
  report how many seats are left at a given price.
- **No arrival timestamps**, only departure times. The "no red-eye trap"
  filter is therefore an approximation based on departure clock time
  (outbound not too late at night, return not too early in the morning),
  not true arrival-based day math.
- **Booking links are best-effort.** The Aviasales deep-link format in
  `deep_link()` should be double-checked against your Travelpayouts
  dashboard once you have an account -- it's not guaranteed stable.

## What it checks

- **USA / Japan** (NYC, Boston, LA, SF, Seattle, Tokyo): round trip,
  minimum 3 nights, per-destination price thresholds ($400-760).
- **Europe wildcard**: round trip, minimum 2 nights, scans a set of major
  European hops for anything under $55.
- Two search strategies per long-haul destination, cheaper one wins:
  the direct cached fare, and a "hub-hack" search that queries BUD→hub and
  hub→destination separately (across London, Vienna, Istanbul, Frankfurt)
  and sums the cheapest matching pair -- this is what catches
  budget-carrier combos the direct query misses.
- Thresholds, hubs, and nights are defined at the top of `flight_deals.py`.
- `seen_deals.json` tracks fares already alerted on, so you don't get
  spammed with the same deal every run -- only re-alerts if the price drops
  further.

## Scheduling

Intended to run periodically (every few hours) via a scheduled task. Ask
Claude to wire this up once you have the Travelpayouts token and ntfy topic.
