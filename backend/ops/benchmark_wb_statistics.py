"""Read-only benchmark of Statistics materialized-cache reads (no WB/Redis writes).

Run from backend: ../.venv/bin/python ops/benchmark_wb_statistics.py --database
../.local-preview/preview.sqlite --organization 2 --from 2026-10-01 --to 2026-10-09
--baseline /tmp/previous-wb_repricer_bff.py
The baseline is optional; both runs read identical stored payloads. Writes are
serialized into a process-local dictionary to emulate persisted cache reads.
"""
import argparse
from contextlib import ExitStack
from datetime import date, datetime
import importlib.util
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.routers import wb_repricer_bff as current


def measure(module, connection, organization, start, end):
    memory = {}
    suffix = f'{start}_{end}'
    bytes_read = [0]

    def read(org, key, **kwargs):
        if (org, key) in memory:
            raw = memory[org, key]
        else:
            # Rebuild each implementation's materialization from the same raw
            # sources, independent of caches already compacted by the server.
            if key.startswith('repricer_stats_'):
                return None
            record = connection.execute('select payload, fetched_at from wb_repricer_source_cache where organization_id=? and source_key=?', (org, key)).fetchone()
            if not record:
                return None
            raw = record[0]
        bytes_read[0] += len(raw)
        result = json.loads(raw)
        if 'fetchedAt' not in result:
            fetched = connection.execute('select fetched_at from wb_repricer_source_cache where organization_id=? and source_key=?', (org, key)).fetchone()
            if fetched:
                result['fetchedAt'] = datetime.fromisoformat(fetched[0]).isoformat() + '+00:00'
        return result

    def save(org, key, payload):
        memory[org, key] = json.dumps(payload)
        return payload

    def revision(org, prefix, **kwargs):
        row = connection.execute('select max(fetched_at) from wb_repricer_source_cache where organization_id=? and source_key like ? and range_date_from<=? and range_date_to>=?', (org, prefix+'%', str(end), str(start))).fetchone()
        return row[0] if row else None

    def resolve(org, prefix, *args, **kwargs):
        return read(org, f'{prefix}_{suffix}') or {}

    with ExitStack() as stack:
        for name, value in {'get_source_cache': read, 'save_source_cache': save,
                            'get_source_cache_range_revision': revision,
                            'get_source_cache_fetched_at': lambda org, key: None,
                            '_period_source_cache': resolve,
                            'list_cached_goods': lambda org: []}.items():
            stack.enter_context(patch.object(module, name, value))
        options = dict(wb_token=None, resolved_period_days=(end-start).days+1, period_suffix=suffix,
                       range_start=datetime.combine(start, datetime.min.time()), range_end=datetime.combine(end, datetime.min.time()))
        module._ensure_repricer_stats_period_caches(organization, 'complete', **options)
        samples, sizes = [], []
        for _ in range(7):
            bytes_read[0] = 0
            before = time.perf_counter()
            result = module._ensure_repricer_stats_period_caches(organization, 'complete', **options)
            samples.append((time.perf_counter()-before)*1000)
            sizes.append(bytes_read[0])
        return dict(median_ms=round(statistics.median(samples), 2), median_deserialized_bytes=statistics.median(sizes),
                    funnel_products=len(result['caches']['baskets'].get('aggregates', {})))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--organization', type=int, required=True)
    parser.add_argument('--from', dest='start', type=date.fromisoformat, required=True)
    parser.add_argument('--to', dest='end', type=date.fromisoformat, required=True)
    parser.add_argument('--baseline', type=Path)
    args = parser.parse_args()
    if args.end < args.start:
        parser.error('end before start')
    connection = sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True)
    connection.execute('BEGIN')  # Stable snapshot even if the local worker saves new data.
    results = {}
    if args.baseline:
        spec = importlib.util.spec_from_file_location('statistics_baseline', args.baseline)
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
        results['before'] = measure(baseline, connection, args.organization, args.start, args.end)
    results['after'] = measure(current, connection, args.organization, args.start, args.end)
    print(json.dumps(results))
