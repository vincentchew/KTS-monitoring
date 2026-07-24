#!/usr/bin/env python3
"""
KTMB ticket availability watcher.

Reads trip criteria from environment (GitHub Actions secrets).
Polls online.ktmb.com.my and notifies Telegram when matching seats appear.

Does not log route, dates, or other trip details (public workflow logs).
"""

from __future__ import annotations

import html as htmlmod
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Iterable

BASE = "https://online.ktmb.com.my"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
STATE_PATH = Path(os.environ.get("STATE_PATH", ".state/alerted.json"))


@dataclass(frozen=True)
class Trip:
    leg: str  # "outbound" | "return"
    from_label: str
    to_label: str
    date: str  # YYYY-MM-DD
    service: str
    train_no: str
    depart: str  # HH:MM
    arrive: str
    duration: str
    seats: int
    fare: str

    @property
    def key(self) -> str:
        return f"{self.leg}|{self.date}|{self.train_no}|{self.depart}"


@dataclass
class Config:
    from_station: str
    to_station: str
    watch_dates: list[date]  # outbound: FROM → TO
    return_dates: list[date]  # return: TO → FROM (optional)
    time_start: int | None  # minutes from midnight (outbound)
    time_end: int | None
    return_time_start: int | None
    return_time_end: int | None
    passenger_count: int
    telegram_token: str
    telegram_chat_id: str
    dry_run: bool


class KtmbClient:
    def __init__(self) -> None:
        self.cj = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj)
        )
        self.opener.addheaders = [
            ("User-Agent", UA),
            ("Accept-Language", "en-MY,en;q=0.9"),
        ]

    def _read(self, resp: object) -> str:
        return resp.read().decode("utf-8", "replace")  # type: ignore[attr-defined]

    def get(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with self.opener.open(req, timeout=45) as resp:
            return self._read(resp)

    def post_form(self, url: str, data: dict, referer: str) -> str:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("Referer", referer)
        req.add_header("Origin", BASE)
        req.add_header("User-Agent", UA)
        with self.opener.open(req, timeout=45) as resp:
            return self._read(resp)

    def post_json(self, url: str, payload: dict, csrf: str, referer: str) -> dict:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("RequestVerificationToken", csrf)
        req.add_header("Referer", referer)
        req.add_header("Origin", BASE)
        req.add_header("X-Requested-With", "XMLHttpRequest")
        req.add_header("Accept", "application/json, text/javascript, */*; q=0.01")
        req.add_header("User-Agent", UA)
        try:
            with self.opener.open(req, timeout=45) as resp:
                raw = self._read(resp)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {e.code} from KTMB") from e
        return json.loads(raw)


def _hidden(page: str, field_id: str) -> str | None:
    m = re.search(
        rf'<input[^>]+id="{re.escape(field_id)}"[^>]*value="([^"]*)"',
        page,
        re.I,
    )
    if not m:
        return None
    return htmlmod.unescape(m.group(1))


def _csrf(page: str) -> str:
    m = re.search(
        r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
        page,
    )
    if not m:
        raise RuntimeError("CSRF token not found")
    return m.group(1)


def resolve_station(home_html: str, stations: list[dict], query: str) -> tuple[str, str]:
    """Return (station_id, station_data) for id or name."""
    q = query.strip()
    by_id = {s["Id"]: s for s in stations}
    if q in by_id:
        return q, by_id[q]["StationData"]

    # Map visible names from <option> tags
    name_to_id: dict[str, str] = {}
    for m in re.finditer(
        r'<option value="([^"]+)"[^>]*>([^<]+)</option>',
        home_html,
        re.I,
    ):
        sid, name = m.group(1), m.group(2).strip()
        if not sid or not name or name.lower().startswith("select"):
            continue
        name_to_id[name.upper()] = sid
        name_to_id[re.sub(r"\s+", " ", name.upper())] = sid

    key = re.sub(r"\s+", " ", q.upper())
    if key in name_to_id:
        sid = name_to_id[key]
        return sid, by_id[sid]["StationData"]

    # Fuzzy: unique substring match
    hits = [n for n in name_to_id if key in n or n in key]
    # dedupe by id
    ids = list({name_to_id[n] for n in hits})
    if len(ids) == 1:
        sid = ids[0]
        return sid, by_id[sid]["StationData"]

    raise RuntimeError("Station not found or ambiguous")


def parse_dates(raw: str, *, required: bool, label: str) -> list[date]:
    """
    Date list formats:
      2026-08-15
      2026-08-15,2026-08-16
      2026-08-15..2026-08-18
    """
    if raw is None or not str(raw).strip():
        if required:
            raise RuntimeError(f"{label} is required")
        return []

    out: list[date] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        if ".." in part:
            a, b = [x.strip() for x in part.split("..", 1)]
            start = date.fromisoformat(a)
            end = date.fromisoformat(b)
            if end < start:
                raise RuntimeError(f"{label} range end before start")
            cur = start
            while cur <= end:
                out.append(cur)
                cur += timedelta(days=1)
        else:
            out.append(date.fromisoformat(part))

    # unique preserve order
    seen: set[date] = set()
    uniq: list[date] = []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    if required and not uniq:
        raise RuntimeError(f"{label} parsed empty")
    return uniq


def parse_time_filter(raw: str | None) -> tuple[int | None, int | None]:
    """
    TIME_FILTER:
      empty / * / any  → no filter
      06:00-12:00      → inclusive window on departure time
      6:00-12:00
    """
    if raw is None:
        return None, None
    s = raw.strip().lower()
    if not s or s in {"*", "any", "all", "-"}:
        return None, None

    m = re.fullmatch(
        r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})",
        s,
    )
    if not m:
        raise RuntimeError("TIME_FILTER must look like HH:MM-HH:MM or be empty")

    def mins(h: str, mi: str) -> int:
        hh, mm = int(h), int(mi)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise RuntimeError("TIME_FILTER out of range")
        return hh * 60 + mm

    start = mins(m.group(1), m.group(2))
    end = mins(m.group(3), m.group(4))
    return start, end


def time_to_mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def load_config() -> Config:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    dry = os.environ.get("DRY_RUN", "").strip().lower() in {"1", "true", "yes"}

    if not dry and (not token or not chat):
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")

    pax_raw = os.environ.get("PASSENGER_COUNT", "1").strip() or "1"
    pax = int(pax_raw)
    if pax < 1 or pax > 99:
        raise RuntimeError("PASSENGER_COUNT must be 1-99")

    t0, t1 = parse_time_filter(os.environ.get("TIME_FILTER"))
    # Optional separate window for return; falls back to TIME_FILTER
    ret_filter_raw = os.environ.get("RETURN_TIME_FILTER")
    if ret_filter_raw is None or not str(ret_filter_raw).strip():
        rt0, rt1 = t0, t1
    else:
        rt0, rt1 = parse_time_filter(ret_filter_raw)

    return Config(
        from_station=os.environ.get("FROM_STATION", "").strip(),
        to_station=os.environ.get("TO_STATION", "").strip(),
        watch_dates=parse_dates(
            os.environ.get("WATCH_DATES", ""),
            required=True,
            label="WATCH_DATES",
        ),
        return_dates=parse_dates(
            os.environ.get("RETURN_DATES", ""),
            required=False,
            label="RETURN_DATES",
        ),
        time_start=t0,
        time_end=t1,
        return_time_start=rt0,
        return_time_end=rt1,
        passenger_count=pax,
        telegram_token=token,
        telegram_chat_id=chat,
        dry_run=dry,
    )


def form_date(d: date) -> str:
    """KTMB lightpick format: D MMM YYYY (e.g. 7 Aug 2026)."""
    return f"{d.day} {d.strftime('%b')} {d.year}"


def parse_trips_html(
    trip_html: str,
    day: date,
    *,
    leg: str,
    from_label: str,
    to_label: str,
) -> list[Trip]:
    """Parse trip table rows from /Trip/Trip HTML fragment."""
    trips: list[Trip] = []
    day_s = day.isoformat()

    # Row text example:
    # Gold - 9442 07:35 12:11 4h 36m 4 MYR 86.00 Login to view
    # Platinum - 9535 20:00 00:20 +1 4h 20m 280 MYR 115.00 Login to view
    row_re = re.compile(
        r"(?P<service>[A-Za-z][A-Za-z0-9 /+-]*?)\s*-\s*"
        r"(?P<train>\d+)\s+"
        r"(?P<dep>\d{1,2}:\d{2})\s+"
        r"(?P<arr>\d{1,2}:\d{2})"
        r"(?:\s*\+1)?\s+"
        r"(?P<dur>\d+h\s*\d+m)\s+"
        r"(?P<seats>\d+)\s+"
        r"MYR\s*(?P<fare>[0-9.]+)",
        re.I,
    )

    for row in re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", trip_html, re.I):
        text = " ".join(re.sub(r"<[^>]+>", " ", row).split())
        m = row_re.search(text)
        if not m:
            continue
        dep = m.group("dep")
        # normalize H:MM -> HH:MM
        dh, dm = dep.split(":")
        dep = f"{int(dh):02d}:{dm}"
        ah, am = m.group("arr").split(":")
        arr = f"{int(ah):02d}:{am}"
        trips.append(
            Trip(
                leg=leg,
                from_label=from_label,
                to_label=to_label,
                date=day_s,
                service=m.group("service").strip(),
                train_no=m.group("train"),
                depart=dep,
                arrive=arr,
                duration=re.sub(r"\s+", " ", m.group("dur").strip()),
                seats=int(m.group("seats")),
                fare=m.group("fare"),
            )
        )
    return trips


def fetch_trips_for_date(
    client: KtmbClient,
    from_id: str,
    from_data: str,
    to_id: str,
    to_data: str,
    day: date,
    pax: int,
    *,
    leg: str,
    from_label: str,
    to_label: str,
) -> list[Trip]:
    home = client.get(f"{BASE}/Home/Index")
    csrf = _csrf(home)

    trip_page = client.post_form(
        f"{BASE}/Trip",
        {
            "FromStationData": from_data,
            "ToStationData": to_data,
            "FromStationId": from_id,
            "ToStationId": to_id,
            "OnwardDate": form_date(day),
            "ReturnDate": "",
            "PassengerCount": str(pax),
            "__RequestVerificationToken": csrf,
        },
        f"{BASE}/Home/Index",
    )

    csrf2 = _csrf(trip_page)
    get_token_url = _hidden(trip_page, "GetTripTokenUrl")
    trip_url = _hidden(trip_page, "TripTripUrl")
    search_data = _hidden(trip_page, "SearchData")
    form_code = _hidden(trip_page, "FormValidationCode")
    if not all([get_token_url, trip_url, search_data, form_code]):
        raise RuntimeError("Trip page missing expected fields")

    # Absolute URLs
    if get_token_url.startswith("/"):
        get_token_url = BASE + get_token_url
    if trip_url.startswith("/"):
        trip_url = BASE + trip_url

    token_resp = client.post_json(
        get_token_url,
        {"FormToken": form_code},
        csrf2,
        f"{BASE}/Trip",
    )
    form_token = token_resp.get("formToken")
    if not form_token:
        raise RuntimeError("GetTripToken failed")

    # Site init: RenderTrip(..., false, 1) — sequence 1 = depart leg
    trip_resp = client.post_json(
        trip_url,
        {
            "SearchData": search_data,
            "FormValidationCode": form_token,
            "DepartDate": day.isoformat(),
            "IsReturn": False,
            "BookingTripSequenceNo": 1,
        },
        csrf2,
        f"{BASE}/Trip",
    )
    if not trip_resp.get("status"):
        msgs = trip_resp.get("messages") or [trip_resp.get("data") or "unknown"]
        raise RuntimeError(f"Trip lookup failed: {msgs}")

    html_frag = trip_resp.get("data") or ""
    return parse_trips_html(
        html_frag,
        day,
        leg=leg,
        from_label=from_label,
        to_label=to_label,
    )


def filter_trips(
    trips: Iterable[Trip],
    *,
    passenger_count: int,
    time_start: int | None,
    time_end: int | None,
) -> list[Trip]:
    out: list[Trip] = []
    for t in trips:
        if t.seats < passenger_count:
            continue
        mins = time_to_mins(t.depart)
        if time_start is not None and mins < time_start:
            continue
        if time_end is not None and mins > time_end:
            continue
        out.append(t)
    return out


def load_alerted() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return set(data.get("alerted") or [])
    except (OSError, json.JSONDecodeError):
        return set()


def save_alerted(keys: set[str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"alerted": sorted(keys)}, indent=2) + "\n",
        encoding="utf-8",
    )


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def send_telegram(cfg: Config, text: str, *, parse_mode: str = "HTML") -> None:
    if cfg.dry_run:
        # Keep dry-run output free of secrets; message body is intentional for local debug.
        print("DRY_RUN telegram message:")
        print(text)
        return

    # Telegram hard limit is 4096 characters per message.
    chunks: list[str] = []
    if len(text) <= 4000:
        chunks = [text]
    else:
        lines = text.split("\n")
        buf: list[str] = []
        size = 0
        for line in lines:
            add = len(line) + (1 if buf else 0)
            if buf and size + add > 3900:
                chunks.append("\n".join(buf))
                buf = [line]
                size = len(line)
            else:
                buf.append(line)
                size += add
        if buf:
            chunks.append("\n".join(buf))

    url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage"
    for chunk in chunks:
        payload = {
            "chat_id": cfg.telegram_chat_id,
            "text": chunk,
            "disable_web_page_preview": "true",
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        body = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")
            desc = ""
            try:
                desc = str(json.loads(err_body).get("description") or "")
            except json.JSONDecodeError:
                desc = err_body[:200]
            # Never log token/chat id; description is safe (e.g. "chat not found").
            raise RuntimeError(f"Telegram HTTP {e.code}: {desc}") from e
        data = json.loads(raw)
        if not data.get("ok"):
            desc = str(data.get("description") or "unknown")
            raise RuntimeError(f"Telegram API error: {desc}")


def _pretty_date(iso: str) -> str:
    """2026-08-09 → 9 Aug 2026"""
    try:
        d = date.fromisoformat(iso)
        return f"{d.day} {d.strftime('%b %Y')}"
    except ValueError:
        return iso


def format_alert(cfg: Config, new_trips: list[Trip]) -> str:
    """
    Compact, scannable HTML for Telegram.

    Example:
      🚂 KTMB seats available
      Pax: 1

      ➡️ OUT · 9 Aug 2026
      JB SENTRAL → KL SENTRAL
      <pre>07:35–12:11  Gold 9442       25  RM 84
      08:40–13:00  Platinum 9524   42  RM 113</pre>
    """
    lines: list[str] = [
        "🚂 <b>KTMB seats available</b>",
        f"Pax: {cfg.passenger_count}",
    ]

    # leg → date → trips
    order = ("outbound", "return")
    by_leg: dict[str, dict[str, list[Trip]]] = {k: {} for k in order}
    for t in new_trips:
        by_leg.setdefault(t.leg, {}).setdefault(t.date, []).append(t)

    for leg in order:
        dates = by_leg.get(leg) or {}
        if not dates:
            continue
        tag = "➡️ OUT" if leg == "outbound" else "⬅️ RET"
        for day in sorted(dates.keys()):
            trips = sorted(dates[day], key=lambda x: (x.depart, x.train_no))
            sample = trips[0]
            route = (
                f"{_html_escape(sample.from_label)} → "
                f"{_html_escape(sample.to_label)}"
            )
            lines.append("")
            lines.append(f"{tag} · <b>{_html_escape(_pretty_date(day))}</b>")
            lines.append(route)

            # Monospace block keeps columns readable on mobile
            table_lines: list[str] = []
            for t in trips:
                # Fixed-ish columns: time, service+no, seats, fare
                left = f"{t.depart}–{t.arrive}"
                mid = f"{t.service} {t.train_no}"
                seats = f"{t.seats} seat"
                if t.seats != 1:
                    seats += "s"
                fare = f"RM {t.fare}"
                # Pad mid for rough alignment inside <pre>
                table_lines.append(
                    f"{left}  {mid:<18}  {seats:>9}  {fare}"
                )
            block = _html_escape("\n".join(table_lines))
            lines.append(f"<pre>{block}</pre>")

    lines += ["", f'<a href="{BASE}">Book on KTMB</a>']
    return "\n".join(lines)


def station_label(home_html: str, station_id: str, fallback: str) -> str:
    for m in re.finditer(
        rf'<option value="{re.escape(station_id)}"[^>]*>([^<]+)</option>',
        home_html,
        re.I,
    ):
        return m.group(1).strip()
    return fallback


def main() -> int:
    try:
        cfg = load_config()
    except Exception as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if not cfg.from_station or not cfg.to_station:
        print("config error: FROM_STATION and TO_STATION are required", file=sys.stderr)
        return 2

    client = KtmbClient()
    try:
        home = client.get(f"{BASE}/Home/Index")
        stations = json.loads(
            re.search(r"var jsStations = (\[.*?\]);", home, re.S).group(1)
        )
        from_id, _from_data = resolve_station(home, stations, cfg.from_station)
        to_id, _to_data = resolve_station(home, stations, cfg.to_station)
        from_label = station_label(home, from_id, cfg.from_station)
        to_label = station_label(home, to_id, cfg.to_station)
    except Exception as e:
        print(f"init error: {e}", file=sys.stderr)
        return 1

    # Each job: (leg, origin query, dest query, origin label, dest label, dates, time window)
    jobs: list[tuple[str, str, str, str, str, list[date], int | None, int | None]] = [
        (
            "outbound",
            cfg.from_station,
            cfg.to_station,
            from_label,
            to_label,
            cfg.watch_dates,
            cfg.time_start,
            cfg.time_end,
        ),
    ]
    if cfg.return_dates:
        jobs.append(
            (
                "return",
                cfg.to_station,  # stations swapped
                cfg.from_station,
                to_label,
                from_label,
                cfg.return_dates,
                cfg.return_time_start,
                cfg.return_time_end,
            )
        )

    all_matches: list[Trip] = []
    errors = 0
    checks = 0
    for leg, origin_q, dest_q, o_label, d_label, days, t0, t1 in jobs:
        for day in days:
            checks += 1
            try:
                home = client.get(f"{BASE}/Home/Index")
                stations = json.loads(
                    re.search(r"var jsStations = (\[.*?\]);", home, re.S).group(1)
                )
                o_id, o_data = resolve_station(home, stations, origin_q)
                d_id, d_data = resolve_station(home, stations, dest_q)
                trips = fetch_trips_for_date(
                    client,
                    o_id,
                    o_data,
                    d_id,
                    d_data,
                    day,
                    cfg.passenger_count,
                    leg=leg,
                    from_label=o_label,
                    to_label=d_label,
                )
                all_matches.extend(
                    filter_trips(
                        trips,
                        passenger_count=cfg.passenger_count,
                        time_start=t0,
                        time_end=t1,
                    )
                )
            except Exception:
                errors += 1
                print(f"poll error on one leg/date: {leg}", file=sys.stderr)

    current_keys = {t.key for t in all_matches}
    alerted = load_alerted()
    # Drop keys that are no longer available so they can re-alert later
    alerted &= current_keys
    new_keys = current_keys - alerted
    new_trips = [t for t in all_matches if t.key in new_keys]
    new_trips.sort(key=lambda t: (t.leg != "outbound", t.date, t.depart, t.train_no))

    print(
        f"checked={checks} "
        f"outbound_dates={len(cfg.watch_dates)} "
        f"return_dates={len(cfg.return_dates)} "
        f"matches={len(all_matches)} "
        f"new={len(new_trips)} "
        f"errors={errors}"
    )

    if new_trips:
        msg = format_alert(cfg, new_trips)
        try:
            send_telegram(cfg, msg)
            print("telegram=sent")
        except Exception as e:
            print(f"telegram error: {e}", file=sys.stderr)
            return 1
        alerted |= new_keys

    save_alerted(alerted)

    if checks and errors == checks:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
