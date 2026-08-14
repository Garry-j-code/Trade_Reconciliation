## Summary

-

## Test plan

- [ ] `uv run pytest` (no RDS required)
- [ ] Did not commit `.env`, credentials, `*.pem`, or parquet caches

## Notes

Pipeline changes need tests. Agent still must not write to `trades` / `matches` / `breaks`.
