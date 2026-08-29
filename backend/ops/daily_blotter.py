"""EOD job: daily blotter then auto-investigate new open breaks.

Pipeline matching stays LLM-free. This wrapper is the ``daily-blotter`` CLI.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from backend.agent.auto_investigate import investigate_missing_suggestions
from backend.agent.providers import BedrockAccessError
from backend.data.generator import parse_iso_date
from backend.db.session import database_url_from_env, get_engine, get_session_factory, session_scope
from backend.pipeline.daily_blotter import build_arg_parser, run_daily_blotter

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_arg_parser()
    parser.add_argument(
        "--skip-investigate",
        action="store_true",
        help="Do not run Bedrock after match (pipeline-only).",
    )
    parser.add_argument(
        "--investigate-limit",
        type=int,
        default=None,
        help="Cap auto-investigate to N open breaks without suggestions.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_db = False if args.no_db else None
    try:
        td = parse_iso_date(args.trade_date) if args.trade_date else None
        result = run_daily_blotter(
            trade_date=td,
            backfill_sessions=args.backfill_sessions,
            n_trades=args.n_trades,
            seed=args.seed,
            lookback_days=args.lookback_days,
            skip_fetch=args.skip_fetch,
            skip_s3_sync=args.skip_s3_sync,
            cache_dir=args.cache_dir,
            load_db=load_db,
            write_parquet=not args.no_parquet,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    investigate: dict[str, object] | None = None
    url = database_url_from_env()
    should_investigate = (
        not args.skip_investigate
        and load_db is not False
        and bool(url)
        and bool(result.db_loaded)
    )
    if should_investigate:
        engine = get_engine(url)  # type: ignore[arg-type]
        factory = get_session_factory(engine)
        try:
            with session_scope(factory) as session:
                investigate = investigate_missing_suggestions(
                    session,
                    provider_name=None,
                    limit=args.investigate_limit,
                )
        except BedrockAccessError as exc:
            logger.warning("Auto-investigate skipped: %s", exc)
            investigate = {
                "attempted": 0,
                "written": 0,
                "failed": 0,
                "errors": [{"break_id": None, "error": str(exc)}],
            }
        except Exception as exc:  # noqa: BLE001 — blotter already succeeded
            logger.exception("Auto-investigate batch failed")
            investigate = {
                "attempted": 0,
                "written": 0,
                "failed": 0,
                "errors": [{"break_id": None, "error": f"{type(exc).__name__}: {exc}"}],
            }

    payload = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "trade_dates": result.trade_dates,
        "skipped": result.skipped,
        "fetch": result.fetch,
        "generate": result.generate,
        "match_count": result.match_count,
        "break_count": result.break_count,
        "db_loaded": result.db_loaded,
        "notes": result.notes,
        "investigate": investigate,
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
