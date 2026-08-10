"""
Flight deal monitor: BUD -> USA / Japan / wildcard Europe.

Data source: Travelpayouts Data API (api.travelpayouts.com), which mirrors
Aviasales/Kiwi cached fare data. This is a genuinely free, self-serve,
no-credit-card data source -- unlike Kiwi Tequila (now partner-only) or
Amadeus Self-Service (self-service portal shut down July 2026).

The trade-off for "free and no approval process" is real, and worth knowing
up front:

  - CACHED, NOT LIVE. Every record is "the last price someone's search
    found," with an `expires_at` estimate -- not a live quote. A surfaced
    deal can be gone by the time you click through. Treat alerts as
    "go check this now," not "guaranteed bookable."
  - NO SEAT-AVAILABILITY DATA. Unlike Kiwi Tequila, this API does not
    report how many seats are left at a given price.
  - NO ARRIVAL TIMESTAMPS, only departure_at/return_at plus a duration in
    minutes. estimated_arrival() derives an arrival time from those (real
    duration, not a guess), which is enough to enforce a genuine minimum
    layover on hub-hack connections and a same-ballpark red-eye filter --
    but it's still an estimate, not a scheduled arrival time.

Two search strategies per long-haul destination, cheaper one wins:

  1. "native"   -- direct BUD<->DEST cached fares.
  2. "hub-hack" -- BUD->HUB and HUB->DEST (and the mirrored return legs)
                  queried independently across a few candidate transfer
                  hubs, cheapest matching pair wins. This is the manual
                  "search the legs separately" approach -- it can beat the
                  direct query because the cache for a direct route doesn't
                  always include self-transfer budget-carrier combos.

Pushes an iPhone notification via ntfy.sh whenever a fare clears the
"crazy deal" bar for that destination.

Run manually:
    TRAVELPAYOUTS_TOKEN=xxx NTFY_TOPIC=yyy python flight_deals.py

Designed to be run periodically by a scheduler (no daemon/loop inside).
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ORIGIN = "BUD"

# destination IATA/metro code -> (display name, round-trip "crazy deal" USD
# threshold, minimum nights at destination). Iceland is priced much closer
# to a European hop than a transatlantic one, hence the lower bar.
LONG_HAUL_DESTINATIONS = {
    "NYC": ("New York", 400, 3),
    "BOS": ("Boston", 400, 3),
    "LAX": ("Los Angeles", 560, 3),
    "SFO": ("San Francisco", 560, 3),
    "SEA": ("Seattle", 560, 3),
    "TYO": ("Tokyo", 760, 3),
    "KEF": ("Reykjavik", 200, 3),
}
LONG_HAUL_MAX_NIGHTS = 14

# Candidate virtual-interlining gateways out of BUD -- used for the
# hub-hack search only. Kept short to bound the number of API calls.
HUBS = ["LON", "VIE", "IST", "FRA"]

# Curated set of "wildcard Europe" hops to scan for absurd short-haul deals.
# Note: VIE (Vienna) stays in HUBS above for long-haul virtual-interlining
# even though it's no longer monitored here as its own destination.
EUROPE_WILDCARD_DESTINATIONS = {
    "LON": "London", "PAR": "Paris", "BCN": "Barcelona", "MAD": "Madrid",
    "ROM": "Rome", "MIL": "Milan", "LIS": "Lisbon", "ATH": "Athens",
    "CPH": "Copenhagen", "STO": "Stockholm", "OSL": "Oslo", "DUB": "Dublin",
    "AMS": "Amsterdam", "BER": "Berlin", "IST": "Istanbul",
    "BGO": "Bergen", "TRD": "Trondheim", "OPO": "Porto", "MMX": "Malmo",
    "FNC": "Madeira", "PDL": "Azores",
}
EUROPE_CRAZY_THRESHOLD = 55  # USD round trip
EUROPE_MIN_NIGHTS = 2
EUROPE_MAX_NIGHTS = 6

# A few well-established carriers' own homepages, keyed by IATA airline
# code (that's what this data source reports -- not a friendly name), for
# cross-checking a fare directly with the airline. Deliberately short:
# only codes confirmed against real data, not guessed -- an unconfirmed
# code risks mislabeling a push notification, which is worse than omitting
# the link entirely.
# {IATA code: (display name, homepage url)}. One shared source of truth --
# the JS side needs the same mapping since real leg data comes back keyed
# by these codes, not by friendly names.
AIRLINES = {
    "W6": ("Wizz Air", "https://wizzair.com"),
    "W4": ("Wizz Air Malta", "https://wizzair.com"),
    "FR": ("Ryanair", "https://www.ryanair.com"),
    "U2": ("easyJet", "https://www.easyjet.com"),
    "BA": ("British Airways", "https://www.britishairways.com"),
    "LX": ("SWISS", "https://www.swiss.com"),
    "LH": ("Lufthansa", "https://www.lufthansa.com"),
    "KL": ("KLM", "https://www.klm.com"),
    "TK": ("Turkish Airlines", "https://www.turkishairlines.com"),
    "OS": ("Austrian", "https://www.austrian.com"),
    "DE": ("Condor", "https://www.condor.com"),
    "LO": ("LOT", "https://www.lot.com"),
    "DL": ("Delta", "https://www.delta.com"),
    "WK": ("Edelweiss", "https://www.edelweissair.com"),
}

MONTHS_AHEAD = 4          # how many calendar months of cached data to scan.
                           # Higher = more chance of finding a real deal
                           # further out, but the hub-hack search already
                           # fans out to dozens of requests per destination,
                           # so this multiplies request volume directly --
                           # went with a moderate bump rather than a large
                           # one, to stay reasonably clear of the free
                           # tier's (undocumented) rate limit.
MIN_CONNECT_HOURS = 1.5   # minimum layover between leg1's estimated arrival and leg2's departure
MAX_CONNECT_HOURS = 30    # beyond this it's not a connection, it's a second trip
REQUEST_DELAY_SECONDS = 0.3

# A "deal" is defined relative to what the route normally costs, not a
# hand-picked dollar figure -- $40 to Milan is a deal, $40 to Trondheim
# might not be. Two separate bars: the dashboard shows anything reasonably
# good, but a push notification is worth interrupting you for only when
# it's genuinely exceptional.
MIN_DISCOUNT_PCT_DISPLAY = 0.40
MIN_DISCOUNT_PCT_NOTIFY = 0.65
BASELINE_MONTHS_AHEAD = 2  # months of calendar spread used to compute the median

STATE_FILE = os.path.join(os.path.dirname(__file__), "seen_deals.json")

TRAVELPAYOUTS_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN")
TRAVELPAYOUTS_MARKER = os.environ.get("TRAVELPAYOUTS_MARKER", "")  # affiliate marker, optional
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")


def load_seen():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_seen(seen):
    with open(STATE_FILE, "w") as f:
        json.dump(seen, f, indent=2)


def parse_dt(s):
    # e.g. "2026-08-14T18:35:00Z" or "...+00:00"
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


def is_fresh(record, now_utc):
    # expires_at is a UTC ("Z") timestamp -- must be compared against UTC
    # now, not local time, or every record looks expired/fresh depending on
    # which side of UTC the machine's clock happens to sit.
    expires = record.get("expires_at")
    if not expires:
        return True  # unknown freshness -- don't discard, just can't confirm
    try:
        return parse_dt(expires) > now_utc
    except Exception:
        return True


def fetch_cheap(origin, destination, months_ahead=MONTHS_AHEAD):
    """Cached cheapest-fares-found for a route, scanned a few months out.
    Returns a flat list of records with an added 'origin'/'destination'."""
    out = []
    today = datetime.now()
    for i in range(months_ahead):
        month = (today.replace(day=1) + timedelta(days=32 * i)).strftime("%Y-%m")
        params = {
            "origin": origin,
            "destination": destination,
            "depart_date": month,
            "currency": "usd",
        }
        url = "https://api.travelpayouts.com/v1/prices/cheap?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"X-Access-Token": TRAVELPAYOUTS_TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read())
        except Exception as e:
            print(f"[{origin}->{destination}] {month}: request failed: {e}")
            continue
        finally:
            time.sleep(REQUEST_DELAY_SECONDS)
        if not body.get("success"):
            continue
        # The API normalizes a specific airport code (e.g. JFK) to its city
        # code (NYC) as the response key, but echoes city/metro codes
        # (LON, VIE) back unchanged -- so don't key the lookup on what we
        # requested, just flatten whatever comes back (the request already
        # filtered server-side to this exact origin/destination pair).
        for variants in body.get("data", {}).values():
            for variant in variants.values():
                record = dict(variant)
                record["origin"] = origin
                record["destination"] = destination
                out.append(record)
    return out


def month_matrix(origin, destination, month):
    """One-way price spread for a route across a whole month -- many date
    points from real cached data, used to compute what the route normally
    costs (not the cheapest we can find, the *typical* price)."""
    params = {
        "origin": origin,
        "destination": destination,
        "month": month,
        "currency": "usd",
    }
    url = "https://api.travelpayouts.com/v2/prices/month-matrix?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Access-Token": TRAVELPAYOUTS_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except Exception as e:
        print(f"[{origin}->{destination}] month-matrix {month}: request failed: {e}")
        return []
    finally:
        time.sleep(REQUEST_DELAY_SECONDS)
    if not body.get("success"):
        return []
    return body.get("data", [])


def median(values):
    values = sorted(values)
    n = len(values)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def route_baseline(origin, dest):
    """Real round-trip 'normal price' for a route, derived from live data:
    median one-way price each direction (across a couple of months of
    calendar spread), summed. Returns None if there's not enough data to
    compute one -- callers should fall back to a fixed threshold then."""
    out_prices, back_prices = [], []
    today = datetime.now()
    for i in range(BASELINE_MONTHS_AHEAD):
        month = (today.replace(day=1) + timedelta(days=32 * i)).strftime("%Y-%m-%d")
        out_prices += [d["value"] for d in month_matrix(origin, dest, month) if d.get("value")]
        back_prices += [d["value"] for d in month_matrix(dest, origin, month) if d.get("value")]
    med_out, med_back = median(out_prices), median(back_prices)
    if med_out is None or med_back is None:
        return None
    return med_out + med_back


def estimated_arrival(record):
    """departure_at + duration_to (minutes) -- this data source has no
    arrival timestamp, but does report flight duration, so this is a real
    estimate rather than a pure guess."""
    depart = parse_dt(record["departure_at"])
    duration_to = record.get("duration_to")
    if duration_to:
        return depart + timedelta(minutes=duration_to)
    return depart


def departs_ok(dt, is_return_leg):
    """Red-eye-trap filter. For the return leg this checks departure time
    directly (leaving too early eats into the last day); for the outbound
    leg the caller should pass the *estimated arrival* time (arriving too
    late/overnight eats into day 1)."""
    hour = dt.hour
    return hour >= 7 if is_return_leg else hour <= 23


def native_round_trip(dest, threshold, min_nights, max_nights, now):
    records = fetch_cheap(ORIGIN, dest)
    candidates = []
    for r in records:
        if not r.get("return_at") or not is_fresh(r, now):
            continue
        try:
            depart = parse_dt(r["departure_at"])
            ret = parse_dt(r["return_at"])
        except Exception:
            continue
        nights = (ret.date() - depart.date()).days
        if not (min_nights <= nights <= max_nights):
            continue
        if not (departs_ok(estimated_arrival(r), False) and departs_ok(ret, True)):
            continue
        if r["price"] > threshold:
            continue
        candidates.append({
            "price": r["price"],
            "method": "native",
            "departure": r["departure_at"],
            "return": r["return_at"],
            "nights": nights,
            "deep_link": deep_link(ORIGIN, dest, depart, ret),
            # per-leg price is unknown here -- it's one combined fare, not
            # two separately priced tickets, so there's nothing to split.
            # tuple shape is (carrier, from, to, price_or_None) throughout.
            "legs": [(r.get("airline", "?"), ORIGIN, dest, None), (r.get("airline", "?"), dest, ORIGIN, None)],
        })
    if not candidates:
        return None
    return min(candidates, key=lambda c: c["price"])


def cheapest_hub_split(fly_from, fly_to, now):
    """fly_from->HUB + HUB->fly_to as two independent one-way fares, paired
    by leg1's *estimated arrival* at the hub vs leg2's departure (using
    duration_to for the estimate -- this data source has no arrival
    timestamps of its own)."""
    best = None
    for hub in HUBS:
        if hub in (fly_from, fly_to):
            continue
        leg1_records = [r for r in fetch_cheap(fly_from, hub) if is_fresh(r, now)]
        leg2_records = [r for r in fetch_cheap(hub, fly_to) if is_fresh(r, now)]
        for leg1 in leg1_records:
            try:
                leg1_arrival = estimated_arrival(leg1)
            except Exception:
                continue
            for leg2 in leg2_records:
                try:
                    leg2_dt = parse_dt(leg2["departure_at"])
                except Exception:
                    continue
                connect_hours = (leg2_dt - leg1_arrival).total_seconds() / 3600
                if not (MIN_CONNECT_HOURS <= connect_hours <= MAX_CONNECT_HOURS):
                    continue
                total = leg1["price"] + leg2["price"]
                if best is None or total < best["price"]:
                    best = {
                        "price": total,
                        "departure": leg1["departure_at"],       # when you leave fly_from
                        "final_arrival": estimated_arrival(leg2),  # when you actually land at fly_to
                        "legs": [
                            (leg1.get("airline", "?"), fly_from, hub, leg1["price"]),
                            (leg2.get("airline", "?"), hub, fly_to, leg2["price"]),
                        ],
                    }
    return best


def hub_hack_round_trip(dest, min_nights, max_nights, now):
    outbound = cheapest_hub_split(ORIGIN, dest, now)
    if not outbound:
        return None

    inbound = cheapest_hub_split(dest, ORIGIN, now)
    if not inbound:
        return None
    inbound_departure = parse_dt(inbound["departure"])

    nights = (inbound_departure.date() - outbound["final_arrival"].date()).days
    if not (min_nights <= nights <= max_nights):
        return None
    if not (departs_ok(outbound["final_arrival"], False) and departs_ok(inbound_departure, True)):
        return None

    return {
        "price": outbound["price"] + inbound["price"],
        "method": "hub-hack",
        "departure": outbound["departure"],
        "return": inbound["final_arrival"].isoformat(),
        "nights": nights,
        "deep_link": "",
        "legs": outbound["legs"] + inbound["legs"],
    }


def deep_link(origin, dest, depart, ret):
    # Best-effort Aviasales search link. Verify the exact template (and
    # append your affiliate marker) against the Travelpayouts dashboard
    # once you have an account -- this format is not guaranteed stable.
    path = f"{origin}{depart.strftime('%d%m')}{dest}{ret.strftime('%d%m')}1"
    url = f"https://www.aviasales.com/search/{path}"
    if TRAVELPAYOUTS_MARKER:
        url += f"?marker={TRAVELPAYOUTS_MARKER}"
    return url


def best_deal_for(dest, threshold, min_nights, max_nights, now, use_hub_hack):
    candidates = []
    native = native_round_trip(dest, threshold, min_nights, max_nights, now)
    if native:
        candidates.append(native)
    if use_hub_hack:
        hack = hub_hack_round_trip(dest, min_nights, max_nights, now)
        if hack:
            candidates.append(hack)
    if not candidates:
        return None
    return min(candidates, key=lambda c: c["price"])


def check_destination(key, label, threshold, notify_threshold, min_nights, max_nights, seen, min_price_for_alert, now, results, region, baseline=None, use_hub_hack=True):
    try:
        best = best_deal_for(key, threshold, min_nights, max_nights, now, use_hub_hack)
    except Exception as e:
        print(f"[{key}] search failed: {e}")
        return

    if not best:
        print(f"[{key}] no qualifying round trip under ${threshold} right now")
        return

    price = best["price"]
    is_deal = price <= threshold

    # record the current best fare for this destination regardless of
    # whether it clears the alert bar -- this is what the dashboard reads,
    # so it should reflect reality even for "watching" (not-yet-cheap) routes
    depart_dt = parse_dt(best["departure"])
    return_dt = parse_dt(best["return"])
    results.append({
        "code": key,
        "city": label,
        "region": region,
        "price": round(price),
        "threshold": threshold,
        "baseline": round(baseline) if baseline else None,
        "deal": is_deal,
        "nights": best["nights"],
        "departure": best["departure"],
        "return": best["return"],
        "dates": f"{depart_dt.strftime('%b')} {depart_dt.day} - {return_dt.strftime('%b')} {return_dt.day}",
        "method": best["method"],
        "seats": None,  # not available from this data source
        "legs": best["legs"],
        "deep_link": best.get("deep_link", ""),
    })

    if not is_deal:
        print(f"[{key}] cheapest qualifying trip is ${price:.0f}, above ${threshold} bar")
        return
    if price < min_price_for_alert:
        print(f"[{key}] skipping ${price:.0f} (below sanity floor ${min_price_for_alert})")
        return
    if price > notify_threshold:
        print(f"[{key}] ${price:.0f} clears the show-on-dashboard bar but not the notify bar (${notify_threshold}) -- no push")
        return

    deal_id = f"{key}:{best['departure']}:{best['return']}:{round(price)}"
    if seen.get(deal_id, float("inf")) <= price:
        print(f"[{key}] already alerted this fare (${price:.0f})")
        return

    legs_desc = "\n".join(
        f"{carrier} {frm} -> {to}" + (f" (${leg_price})" if leg_price else "")
        for carrier, frm, to, leg_price in best["legs"]
    )
    carriers = {carrier for carrier, _frm, _to, _leg_price in best["legs"]}
    booking_note = f"{len(carriers)} separate tickets -- book each carrier" if len(carriers) > 1 else "One ticket"
    direct_links = "\n".join(
        f"Check {AIRLINES[carrier][0]} directly: {AIRLINES[carrier][1]}"
        for carrier in sorted(carriers) if carrier in AIRLINES
    )
    discount_line = f"{(1 - price / baseline):.0%} below the ${baseline:.0f} this route normally runs\n" if baseline else ""
    send_push(
        title=f"Contrail: BUD <-> {label} ${price:.0f} RT",
        message=f"Out {best['departure'][:10]}, back {best['return'][:10]} -- {booking_note}\n"
                f"{discount_line}"
                f"Price last confirmed by a real search -- verify before booking.\n"
                f"{legs_desc}\n{best.get('deep_link', '')}"
                + (f"\n{direct_links}" if direct_links else ""),
        click_url=best.get("deep_link") or None,
    )
    seen[deal_id] = price
    print(f"[{key}] ALERT sent: ${price:.0f} via {best['method']}")


def send_push(title, message, click_url=None):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set, skipping push:", title)
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        method="POST",
    )
    req.add_header("Title", title)
    req.add_header("Priority", "high")
    req.add_header("Tags", "airplane,moneybag")
    if click_url:
        req.add_header("Click", click_url)
    urllib.request.urlopen(req, timeout=15)


def main():
    if not TRAVELPAYOUTS_TOKEN:
        print("TRAVELPAYOUTS_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC, to match parse_dt()
    seen = load_seen()
    results = []

    def resolve_thresholds(key, fallback):
        baseline = route_baseline(ORIGIN, key)
        if baseline:
            display = round(baseline * (1 - MIN_DISCOUNT_PCT_DISPLAY))
            notify = round(baseline * (1 - MIN_DISCOUNT_PCT_NOTIFY))
            print(f"[{key}] real median RT ${baseline:.0f} -> show bar ${display} ({MIN_DISCOUNT_PCT_DISPLAY:.0%} off), notify bar ${notify} ({MIN_DISCOUNT_PCT_NOTIFY:.0%} off)")
        else:
            display = fallback
            # no real baseline to scale from -- fall back to a stricter
            # fixed dollar figure for the notify bar rather than guessing a %
            notify = round(fallback * (1 - (MIN_DISCOUNT_PCT_NOTIFY - MIN_DISCOUNT_PCT_DISPLAY)))
            print(f"[{key}] not enough data for a real median, using fallback show ${display} / notify ${notify}")
        return display, notify, baseline

    for key, (label, fallback_threshold, min_nights) in LONG_HAUL_DESTINATIONS.items():
        threshold, notify_threshold, baseline = resolve_thresholds(key, fallback_threshold)
        check_destination(key, label, threshold, notify_threshold, min_nights, LONG_HAUL_MAX_NIGHTS, seen, min_price_for_alert=80, now=now, results=results, region="long_haul", baseline=baseline)
        time.sleep(1)

    for key, label in EUROPE_WILDCARD_DESTINATIONS.items():
        threshold, notify_threshold, baseline = resolve_thresholds(key, EUROPE_CRAZY_THRESHOLD)
        check_destination(key, label, threshold, notify_threshold, EUROPE_MIN_NIGHTS, EUROPE_MAX_NIGHTS, seen, min_price_for_alert=15, now=now, results=results, region="europe", baseline=baseline, use_hub_hack=False)
        time.sleep(1)

    save_seen(seen)

    # split straight into the shape the dashboard's fetch() expects -- no
    # transformation step needed on the way out
    data = {
        "checkedAt": now.isoformat() + "Z",
        "monitoringCount": len(LONG_HAUL_DESTINATIONS) + len(EUROPE_WILDCARD_DESTINATIONS),
        "longHaul": [r for r in results if r["region"] == "long_haul"],
        "europe": [r for r in results if r["region"] == "europe"],
    }
    data_path = os.path.join(os.path.dirname(__file__), "data.json")
    with open(data_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Wrote {len(results)} results to {data_path}")


if __name__ == "__main__":
    main()
