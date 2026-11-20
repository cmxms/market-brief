"""Run one market brief: gather -> compose -> validate -> deliver.

Exit codes are meaningful so Task Scheduler and a human both see the real outcome:

    0  posted (possibly labelled stale), or a dry run that passed every check
    1  unexpected exception or invalid arguments/configuration
    2  skipped (weekend or NYSE holiday)
    3  gather failed
    4  compose failed
    5  validation failed
    6  outside the 07:30-08:30 Chicago window
    7  delivery failed
    8  monthly spend estimate reached
    9  requested date unsupported for live gathering (today ET only)

Every failure (except skip code 2) attempts a production Discord alert, even
with --dry or --test. Alert failure never changes the original exit code.

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
import uuid
from zoneinfo import ZoneInfo

import config
import run_identity
import validate

CHI = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")
WINDOW = (7 * 60 + 30, 8 * 60 + 30)  # 07:30-08:30 Chicago


def _write_log(msg: str, fh) -> None:
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
            if isinstance(block, dict) and block.get("status") == "degraded":
                reason = reason or block.get("degraded_reason") or "degraded chain"
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
    state = {"stage": "setup", "reason": "run failed", "log_path": None, "partial": False,
             "execution_id": uuid.uuid4().hex}
    try:
        code = _run(argv, state)
    except SystemExit as exc:
        if not exc.code:  # argparse --help
            return 0
        code = 1
        state["reason"] = "invalid command-line arguments"
    except Exception as exc:  # noqa: BLE001 - central failure reporting
        code = 1
        # Exception messages may contain arbitrary vendor credentials. Keep the
        # type, not the response body, for unexpected failures.
        state["reason"] = f"unexpected {type(exc).__name__}"
    if code not in (0, 2):
        post_discord = None
        try:
            import post_discord

            if not state.get("env_loaded"):
                config.load_env()
            detail = post_discord.alert_failure(state["stage"], state["reason"], code,
                                                partial=state["partial"])
        except Exception as exc:  # noqa: BLE001 - preserve the original outcome
            reason = post_discord.sanitize(str(exc)) if post_discord is not None else type(exc).__name__
            detail = f"failure alert failed: {reason}"
        try:
            if state["log_path"] is not None:
                with state["log_path"].open("a", encoding="utf-8") as fh:
                    _write_log(f"{detail} execution_id={state['execution_id']}", fh)
            else:
                print(f"{detail} execution_id={state['execution_id']}", flush=True)
        except OSError:
            print(detail, file=sys.stderr, flush=True)
    return code


def _run(argv: list[str] | None, state: dict) -> int:
    def _log(msg: str, fh) -> None:
        state["reason"] = msg
        _write_log(f"execution_id={state['execution_id']} {msg}", fh)

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="YYYY-MM-DD (default: today, ET); other dates require existing artifacts")
    ap.add_argument("--dry", action="store_true", help="preview the brief; failures still alert production")
    ap.add_argument("--test", action="store_true", help="brief to test channel; failures still alert production")
    ap.add_argument("--force-weekend", action="store_true", help="run even on a Saturday or Sunday")
    ap.add_argument("--force-holiday", action="store_true", help="run even on a listed NYSE holiday")
    ap.add_argument("--ignore-window", action="store_true", help="run outside 07:30-08:30 Chicago")
    ap.add_argument("--force", action="store_true", help="ignore the delivery ledger")
    ap.add_argument("--skip-gather", action="store_true", help="reuse the requested date's existing data block")
    ap.add_argument("--deliver-only", action="store_true",
                    help="validate and deliver an existing brief without gathering or composing")
    args = ap.parse_args(argv)
    run_id = None

    config.ensure_dirs()
    config.load_env()
    state["env_loaded"] = True
    settings = config.load_settings()

    date = args.date or dt.datetime.now(ET).date().isoformat()
    day = dt.date.fromisoformat(date)
    display = validate.display_date(day)
    data_json = config.DATA / f"{date}.json"
    if (args.skip_gather or args.deliver_only) and data_json.exists():
        existing = json.loads(data_json.read_text(encoding="utf-8"))
        if "run_id" in existing:
            run_id = run_identity.require(existing["run_id"])
    if run_id is None:
        run_id = run_identity.new()

    log_path = config.LOGS / f"{date}.log"
    state["log_path"] = log_path
    with log_path.open("a", encoding="utf-8", errors="replace") as fh:
        _log(f"run start date={date} dry={args.dry} test={args.test} run_id={run_id}", fh)

        # Historical reconstruction is not implemented: never mix today's feeds
        # with another session's paths. Missing --skip-gather data still gathers.
        reusing = args.deliver_only or (args.skip_gather and (config.DATA / f"{date}.json").exists())
        today_et = dt.datetime.now(ET).date()
        if not reusing and day != today_et:
            _log(f"refusing date={date}: live gathering only supports today's ET date ({today_et}); "
                 "use --skip-gather with an existing data block or --deliver-only with an existing brief", fh)
            return 9

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
            state["stage"] = "validate"
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
            state["stage"] = "gather"
            if args.skip_gather and data_json.exists():
                _log("gather skipped; reusing existing data block", fh)
            else:
                try:
                    import gather
                    gather.DATA_DIR = config.DATA
                    gather.BRIEFS_DIR = config.BRIEFS
                    gather.main(day, run_id=run_id)
                except Exception as exc:  # noqa: BLE001 - reported with its type
                    _log(f"gather failed: {exc!r}", fh)
                    return 3
            if not data_json.exists():
                _log(f"gather produced no data block at {data_json}", fh)
                return 3

            payload = json.loads(data_json.read_text(encoding="utf-8"))

            # --- compose --------------------------------------------------
            if args.skip_gather and "run_id" not in payload:
                # Preserve the legacy pair before assigning composition identity.
                # Never relabel an already-written brief.
                import gather
                gather.DATA_DIR = config.DATA
                md = gather.render_md({**payload, "run_id": run_id})
                gather._archive_previous_run(date)
                payload["run_id"] = run_id
                data_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                data_md.write_text(md, encoding="utf-8")
                _log(f"legacy block retained; assigned run_id={run_id} for composition", fh)
            if payload.get("run_id") != run_id:
                _log("gathered block run_id does not match this run", fh)
                return 3
            state["stage"] = "compose"
            ok, detail = __import__("compose").run(date, data_md, out, log_path, run_id=run_id)
            _record_spend(date, per_run)
            if not ok:
                _log(f"compose failed: {detail}", fh)
                return 4
            _log(f"compose ok: run_id={run_id} {detail}", fh)

        warning = _data_warning(payload)
        _log(f"data as_of_et={payload.get('as_of_et')} gex_warning={bool(warning)}", fh)

        # --- validate -----------------------------------------------------
        state["stage"] = "validate"
        brief = out.read_text(encoding="utf-8")
        max_chars = int(settings.get("brief", {}).get("max_chars", 5200))
        errors, warnings = validate.check(
            brief, date, display, max_chars, None,
            as_of_et=payload.get("as_of_et"),
            run_id=payload.get("run_id"),
        )
        for w in warnings:
            _log(f"WARN {w}", fh)
        if errors:
            for e in errors:
                _log(f"FAIL {e}", fh)
            return 5
        issues = validate.data_issues(payload, warning)
        affected = validate.availability_errors(brief, issues)
        brief, quarantined = validate.quarantine(brief, affected, issues)
        # Recheck after repair; protected structure and residual claims still refuse.
        residual = []
        if quarantined:
            residual, _ = validate.check(brief, date, display, max_chars, None,
                                         as_of_et=payload.get("as_of_et"), run_id=payload.get("run_id"))
        residual.extend(validate.outside_data_errors(brief, issues))
        if residual:
            for error in residual:
                _log(f"FAIL {error}", fh)
            return 5
        for record in quarantined:
            _log("QUARANTINE " + json.dumps(record, ensure_ascii=False), fh)
        _log(f"validation passed ({len(validate.visible_text(brief))} chars)", fh)

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
        state["stage"] = "deliver"
        if args.dry:
            import post_discord
            parts = post_discord.chunk(brief)
            _log(f"DRY: {len(parts)} Discord message(s), nothing posted", fh)
            for i, part in enumerate(parts, 1):
                print(f"----- message {i}/{len(parts)} ({len(part)} chars) -----")
                print(part)
            return 0

        import deliver
        if quarantined:
            deliver.record_quarantine(date, brief, quarantined)
        if args.test:
            channel = str(settings.get("discord", {}).get("test_channel_id", ""))
            if not channel:
                _log("no test_channel_id configured", fh)
                return 7
            state["partial"] = True  # An exception may follow accepted messages.
            ok, detail = deliver.deliver_test(brief, date, channel, force=args.force)
        else:
            state["partial"] = True
            ok, detail = deliver.deliver_production(brief, date, force=args.force)

        if not ok:
            state["partial"] = ("outstanding destinations:" in detail or "on chunk" in detail)
            _log(f"delivery failed: {detail}", fh)
            return 7
        _log(f"delivered: {detail}", fh)
        return 0


if __name__ == "__main__":
    sys.exit(main())
