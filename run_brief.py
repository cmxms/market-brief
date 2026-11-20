"""Run one market brief: gather -> compose -> validate -> deliver.

Exit codes are meaningful so Task Scheduler and a human both see the real outcome:

    0  posted, or a dry run that passed every check
    2  skipped (weekend)
    3  gather failed
    4  compose failed
    5  validation failed
    6  outside the 07:30-08:30 Chicago window
    7  delivery failed
    8  monthly spend estimate reached

NYSE holidays are detected from `market_holidays.yaml` (2026-2027 closures and
early closes): a listed holiday exits 2 without posting, an early close is logged.
If the calendar file is missing the run warns and continues, so confirm the file
is present after any environment change. Coverage stops at 2027; extend the file
before then.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from zoneinfo import ZoneInfo

import config
import validate

CHI = ZoneInfo("America/Chicago")
WINDOW = (7 * 60 + 30, 8 * 60 + 30)  # 07:30-08:30 Chicago


def _log(msg: str, fh) -> None:
    line = f"{dt.datetime.now(CHI):%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    fh.write(line + "\n")
    fh.flush()


def _data_warning(payload: dict) -> str | None:
    gex = payload.get("gex")
    if not isinstance(gex, dict) or not gex:
        return "GEX unavailable: missing chains"
    failures = []
    for symbol in dict.fromkeys(("SPY", "QQQ", *gex)):
        views = gex.get(symbol)
        if not isinstance(views, dict) or not views:
            failures.append(f"{symbol} GEX unavailable: missing chains")
            continue
        if views.get("error") or views.get("data_warning"):
            failures.append(f"{symbol} GEX unavailable: {views.get('error') or views['data_warning']}")
            continue
        for view in ("near_term", "full_chain"):
            block = views.get(view)
            reason = (block.get("error") or block.get("data_warning")) if isinstance(block, dict) else None
            if not isinstance(block, dict) or not block:
                reason = "missing chain"
            if reason:
                failures.append(f"{symbol} {view} GEX unavailable: {reason}")
    return "; ".join(failures) or None


def _month_spend() -> float:
    path = config.STATE / "usage.jsonl"
    if not path.exists():
        return 0.0
    month = dt.datetime.now(CHI).strftime("%Y-%m")
    total = 0.0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("at", "")).startswith(month):
            total += float(row.get("estimated_usd", 0) or 0)
    return total


def _record_spend(date: str, usd: float) -> None:
    with (config.STATE / "usage.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"date": date, "at": dt.datetime.now(CHI).isoformat(timespec="seconds"),
                            "estimated_usd": usd}) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="YYYY-MM-DD (default: today, Chicago)")
    ap.add_argument("--dry", action="store_true", help="run and check, print the split, post nothing")
    ap.add_argument("--test", action="store_true", help="deliver to the configured test channel")
    ap.add_argument("--force-weekend", action="store_true", help="run even on a Saturday or Sunday")
    ap.add_argument("--force-holiday", action="store_true", help="run even on a listed NYSE holiday")
    ap.add_argument("--ignore-window", action="store_true", help="run outside 07:30-08:30 Chicago")
    ap.add_argument("--force", action="store_true", help="ignore the delivery ledger")
    ap.add_argument("--skip-gather", action="store_true", help="reuse today's data block")
    ap.add_argument("--deliver-only", action="store_true",
                    help="validate and deliver an existing brief without gathering or composing")
    args = ap.parse_args(argv)

    config.ensure_dirs()
    config.load_env()
    settings = config.load_settings()

    date = args.date or dt.datetime.now(CHI).date().isoformat()
    day = dt.date.fromisoformat(date)
    display = validate.display_date(day)

    log_path = config.LOGS / f"{date}.log"
    with log_path.open("a", encoding="utf-8", errors="replace") as fh:
        _log(f"run start date={date} dry={args.dry} test={args.test}", fh)

        now = dt.datetime.now(CHI)
        minutes = now.hour * 60 + now.minute
        if not args.ignore_window and not (WINDOW[0] <= minutes <= WINDOW[1]):
            _log(f"outside the Chicago window ({now:%H:%M}); exiting", fh)
            return 6

        if day.weekday() >= 5 and not args.force_weekend:
            _log("weekend; nothing to publish", fh)
            return 2

        hol_path = config.PROJECT / str(settings.get("calendar", {}).get("holidays_file", "market_holidays.yaml"))
        if hol_path.exists():
            import yaml
            cal = yaml.safe_load(hol_path.read_text(encoding="utf-8")) or {}
            holidays = {str(k): v for k, v in (cal.get("holidays") or {}).items()}
            early = {str(k): v for k, v in (cal.get("early_closes") or {}).items()}
            if date in holidays and not args.force_holiday:
                _log(f"NYSE holiday ({holidays[date]}); nothing to publish", fh)
                return 2
            if date in early:
                _log(f"NOTE early close today at {early[date]}", fh)
        else:
            _log(f"WARN holiday calendar missing at {hol_path}; holiday detection disabled", fh)

        limits = settings.get("limits", {})
        cap = float(limits.get("monthly_usd_cap", 2.0))
        per_run = float(limits.get("estimated_usd_per_run", 0.05))
        spent = _month_spend()

        data_json = config.DATA / f"{date}.json"
        data_md = config.DATA / f"{date}.data.md"
        out = config.BRIEFS / f"{date}.md"
        payload: dict = {}

        if args.deliver_only:
            _log("deliver-only: reusing the existing brief", fh)
            if not out.exists():
                _log(f"no brief at {out}", fh)
                return 5
            if data_json.exists():
                payload = json.loads(data_json.read_text(encoding="utf-8"))
        else:
            if spent + per_run > cap:
                _log(f"monthly estimate {spent:.2f} + {per_run:.2f} exceeds cap {cap:.2f}; refusing", fh)
                return 8

            # --- gather ---------------------------------------------------
            if args.skip_gather and data_json.exists():
                _log("gather skipped; reusing existing data block", fh)
            else:
                try:
                    import gather
                    gather.DATA_DIR = config.DATA
                    gather.BRIEFS_DIR = config.BRIEFS
                    gather.main()
                except Exception as exc:  # noqa: BLE001 - reported with its type
                    _log(f"gather failed: {exc!r}", fh)
                    return 3
            if not data_json.exists():
                _log(f"gather produced no data block at {data_json}", fh)
                return 3

            payload = json.loads(data_json.read_text(encoding="utf-8"))

            # --- compose --------------------------------------------------
            ok, detail = __import__("compose").run(date, data_md, out, log_path)
            _record_spend(date, per_run)
            if not ok:
                _log(f"compose failed: {detail}", fh)
                return 4
            _log(f"compose ok: {detail}", fh)

        warning = _data_warning(payload)
        _log(f"data as_of_et={payload.get('as_of_et')} gex_warning={bool(warning)}", fh)

        # --- validate -----------------------------------------------------
        brief = out.read_text(encoding="utf-8")
        errors, warnings = validate.check(
            brief, date, display, int(settings.get("brief", {}).get("max_chars", 5200)), warning,
            as_of_et=payload.get("as_of_et"),
        )
        for w in warnings:
            _log(f"WARN {w}", fh)
        if errors:
            for e in errors:
                _log(f"FAIL {e}", fh)
            return 5
        _log(f"validation passed ({len(brief)} chars)", fh)

        claims_path = config.STATE / f"claims-{date}.json"
        if claims_path.exists():
            try:
                claims = json.loads(claims_path.read_text(encoding="utf-8"))
                _log(f"claims ledger: {len(claims)} sourced claim(s)", fh)
            except json.JSONDecodeError as exc:
                _log(f"WARN claims ledger is not valid JSON: {exc}", fh)
        else:
            _log("WARN no claims ledger for this run", fh)

        # --- deliver ------------------------------------------------------
        if args.dry:
            import post_discord
            parts = post_discord.chunk(brief)
            _log(f"DRY: {len(parts)} Discord message(s), nothing posted", fh)
            for i, part in enumerate(parts, 1):
                print(f"----- message {i}/{len(parts)} ({len(part)} chars) -----")
                print(part)
            return 0

        import deliver
        if args.test:
            channel = str(settings.get("discord", {}).get("test_channel_id", ""))
            if not channel:
                _log("no test_channel_id configured", fh)
                return 7
            ok, detail = deliver.deliver_test(brief, date, channel, force=args.force)
        else:
            ok, detail = deliver.deliver_production(brief, date, force=args.force)

        if not ok:
            _log(f"delivery failed: {detail}", fh)
            return 7
        _log(f"delivered: {detail}", fh)
        return 0


if __name__ == "__main__":
    sys.exit(main())
