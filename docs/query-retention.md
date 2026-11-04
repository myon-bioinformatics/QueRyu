# Latest-only query retention

Latest-only cache queries retain the last matching row and its manifest position
per key rather than every matching history row. Sorting final positions preserves
manifest order after filtering. `history=True` keeps the original full-history
behavior. No dependencies or API changes. Source manifests are still fully parsed
by `_rows`; this is not a streaming parser or a bound on total process memory.

Run `python -S -m unittest discover -s tests -p test_query_retention.py`.
The regression checks latest order, glob/status filtering, body-only behavior and
full history. Full pytest runs in CI.

`python -S scripts/benchmark_query_retention.py` compares the pinned git baseline.
CPython 3.12.14/Linux, 100000 synthetic parsed rows / 100 keys, three traced runs:
query-only temporary peak baseline 806776/806688/806688 bytes, current
14040/8608/8608 bytes. Input rows and parsing allocations excluded. Raw evidence
is in query-retention-measurement.json. Unique-key workloads can pay for tuples
and sorting; no general speedup, build-time improvement or total-RSS reduction
is claimed.
