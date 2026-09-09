import copy
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest
import requests

import generate
from market_health import SCHEMA, evaluate_snapshot
from validate_dashboard import validate

NOW = dt.datetime.fromisoformat('2026-09-08T18:00:00+02:00')


def source(rows=None, **overrides):
    row = dict(ticker='A.OL', date='2026-09-08', data_status='current', signal='BUY',
               close=100, rsi14=30, rsi_dir=1, macd_hist=.1, sma50=105,
               pct_above_sma50=-4.76, adx14=24, rsi6=25, mfi14=30,
               stop_loss_pct=3, position_pct=3, primary_count=3, risk='MODERATE', snapshot_id='test')
    frame = pd.DataFrame(rows or [row])
    metadata = dict(schema=SCHEMA, snapshot_id='test', generated_at=NOW.isoformat(),
                    expected_session='2026-09-08', universe_count=len(frame), min_coverage_ratio='0.9')
    metadata.update(overrides)
    health = evaluate_snapshot(frame.to_dict('records'), metadata, NOW)
    return frame, 'fixture', metadata, health


def arrange(monkeypatch, payload=None):
    monkeypatch.setattr(generate, 'fetch_screener_csv', lambda: payload or source())
    monkeypatch.setattr(generate, 'update_ticker_cache', lambda: ([], None))
    monkeypatch.setattr(generate, 'fetch_news_for_stock', lambda stock: None)
    monkeypatch.setattr(generate, 'fetch_macro_news', lambda: ([], None))
    monkeypatch.setattr(generate.time, 'sleep', lambda seconds: None)


def test_current_snapshot_render_and_health_agree(tmp_path, monkeypatch):
    arrange(monkeypatch)
    health = generate.build_dashboard(tmp_path/'index.html', NOW)
    assert health['status'] == 'current'
    assert health['signal_counts']['BUY'] == 1
    page = (tmp_path/'index.html').read_text()
    assert 'Fixed percentage' in page and '>ATR<' not in page
    assert 'As of 2026-09-08' in page
    assert validate(tmp_path)['snapshot_id'] == 'test'


@pytest.mark.parametrize('mutation', ['old_date','intraday','missing_date','nonfinite','duplicate','missing_metadata'])
def test_bad_source_never_becomes_actionable_even_when_just_generated(tmp_path, monkeypatch, mutation):
    frame, label, metadata, health = source()
    if mutation == 'old_date': frame.loc[0,'date'] = '2026-09-04'
    elif mutation == 'intraday': metadata['generated_at'] = '2026-09-08T12:00:00+02:00'
    elif mutation == 'missing_date': frame.loc[0,'date'] = ''
    elif mutation == 'nonfinite': frame.loc[0,'close'] = float('nan')
    elif mutation == 'duplicate': frame = pd.concat([frame,frame],ignore_index=True)
    elif mutation == 'missing_metadata': metadata.pop('schema')
    health = evaluate_snapshot(frame.to_dict('records'), metadata, NOW)
    arrange(monkeypatch,(frame,label,metadata,health))
    result = generate.build_dashboard(tmp_path/'index.html', NOW)
    assert result['status'] == 'blocked' and sum(result['signal_counts'].values()) == 0
    assert '<div class="stock-card">' not in (tmp_path/'index.html').read_text()
    assert validate(tmp_path)['status'] == 'blocked'


def test_snapshot_that_was_current_is_blocked_after_next_session(tmp_path, monkeypatch):
    arrange(monkeypatch)
    tomorrow = dt.datetime.fromisoformat('2026-09-09T18:00:00+02:00')
    health = generate.build_dashboard(tmp_path/'index.html', tomorrow)
    assert health['status'] == 'blocked'
    assert 'snapshot_session_is_not_current' in health['reasons']
    assert validate(tmp_path)['signal_counts']['BUY'] == 0


def test_complete_feed_failure_publishes_safe_page(tmp_path, monkeypatch):
    arrange(monkeypatch)
    def fail(): raise RuntimeError('injected source timeout')
    monkeypatch.setattr(generate,'fetch_screener_csv',fail)
    health = generate.build_dashboard(tmp_path/'index.html', NOW)
    assert health['status'] == 'blocked'
    assert 'source_snapshot_unavailable' in health['reasons']
    assert 'Signals withheld' in (tmp_path/'index.html').read_text()
    validate(tmp_path)


def test_primary_news_outage_is_degraded_not_no_news(tmp_path, monkeypatch):
    arrange(monkeypatch)
    def missing(stock):
        stock.news_errors = {'Oslo Bors':'timeout', 'Yahoo Finance':'429'}
    monkeypatch.setattr(generate, 'fetch_news_for_stock', missing)
    monkeypatch.setattr(generate, 'fetch_macro_news', lambda: ([], 'Reuters unavailable'))
    health = generate.build_dashboard(tmp_path/'index.html', NOW)
    assert health['status'] == 'degraded' and health['market_status'] == 'current'
    assert health['news_coverage']['Oslo Bors']['failed'] == 1
    page = (tmp_path/'index.html').read_text()
    assert 'absence of headlines is not evidence' in page
    assert 'No news found in the last 14 days' not in page
    assert 'news_coverage_incomplete' in health['reasons']
    validate(tmp_path)


class Response:
    def __init__(self, text='', json_data=None):
        self.text, self.content, self.payload = text, text.encode(), json_data
    def raise_for_status(self): pass
    def json(self): return self.payload


@pytest.mark.parametrize('corruption', ['id','hash','missing_manifest'])
def test_http_fetch_rejects_mixed_or_missing_publications(monkeypatch, corruption):
    frame, _, metadata, manifest = source()
    text = '# oslo-screener ' + ' '.join(f'{k}={v}' for k,v in metadata.items()) + '\n' + frame.to_csv(index=False)
    manifest['artifacts'] = {'latest.csv': hashlib.sha256(text.encode()).hexdigest()}
    if corruption == 'id': manifest['snapshot_id'] = 'other'
    if corruption == 'hash': manifest['artifacts']['latest.csv'] = 'incorrect'
    def get(url, **kwargs):
        if url.endswith('health.json'):
            if corruption == 'missing_manifest': raise requests.Timeout('missing health')
            return Response(json_data=manifest)
        return Response(text=text)
    monkeypatch.setattr(generate.requests, 'get', get)
    with pytest.raises(RuntimeError, match='No coherent'):
        generate.fetch_screener_csv()


def test_http_snapshot_hash_and_id_are_verified(monkeypatch):
    frame, _, metadata, manifest = source()
    text = '# oslo-screener ' + ' '.join(f'{k}={v}' for k,v in metadata.items()) + '\n' + frame.to_csv(index=False)
    manifest['artifacts'] = {'latest.csv': hashlib.sha256(text.encode()).hexdigest()}
    monkeypatch.setattr(generate.requests, 'get', lambda url,**kw: Response(json_data=manifest) if url.endswith('health.json') else Response(text=text))
    assert generate.fetch_screener_csv()[2]['snapshot_id'] == 'test'


@pytest.mark.parametrize('body', ['<html>error</html>', '<rss><channel><item><title>Undated</title><link>https://example.org</link></item></channel></rss>', 'invalid'])
def test_malformed_or_undated_news_is_missing_coverage_not_empty_success(monkeypatch, body):
    monkeypatch.setattr(generate.requests, 'get', lambda *a,**kw: Response(text=body))
    with pytest.raises(ValueError):
        generate._parse_rss('https://example.org', 'test')


def test_recent_support_calculation_ignores_provisional_bars():
    days = pd.bdate_range('2026-08-03', periods=30)
    frame = pd.DataFrame({'High':[101.0]*30,'Low':[90.0]*30},index=days)
    frame.loc[days[-1],'Low'] = 1
    class Ticker:
        def history(self,**kwargs): return frame
    # Cutoff leaves fewer than the required 12 confirmed bars: truthful fixed fallback.
    assert generate._compute_sr_stop_loss(Ticker(),'BUY',100,3,'2026-08-10') == (3,'Fixed percentage')


@pytest.mark.parametrize('mode', ['current','expired','replaced','unavailable','missing_expiry','unknown_status','blocked'])
def test_browser_executes_fail_closed_health_guard(tmp_path, monkeypatch, mode):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for browser guard execution (installed in CI)')
    arrange(monkeypatch)
    health = generate.build_dashboard(tmp_path/'index.html', NOW)
    script = re.search(r'<script>(.*?)</script>', (tmp_path/'index.html').read_text(), re.S).group(1)
    if mode == 'expired': health['valid_until'] = '2026-09-07T00:00:00Z'
    if mode == 'replaced': health['snapshot_id'] = 'new'
    if mode == 'missing_expiry': health.pop('valid_until')
    if mode == 'unknown_status': health['status'] = 'success'
    if mode == 'blocked': health['status'] = health['market_status'] = 'blocked'
    runner = """
const vm = require('node:vm');
let input = ''; process.stdin.on('data', x => input += x);
process.stdin.on('end', async () => {
 const payload = JSON.parse(input); const classes = new Set(['awaiting-health']);
 const banner = {innerHTML: 'Checking observation freshness for this visit…', textContent: ''};
 class Clock extends Date { static now() { return Date.parse('2026-09-08T18:00:00+02:00'); } }
 const sandbox = {Date:Clock, AbortController,
   document: {body:{classList:{add:x=>classes.add(x),remove:x=>classes.delete(x)}},getElementById:()=>banner},
   fetch:async()=>{if(payload.mode==='unavailable')throw Error('offline');return{ok:true,json:async()=>payload.health};},
   setTimeout:()=>1,clearTimeout:()=>{},setInterval:()=>1};
 vm.runInNewContext(payload.script,sandbox);
 await new Promise(setImmediate);
 process.stdout.write(JSON.stringify({hidden:classes.has('awaiting-health'),banner:banner.textContent}));
});
"""
    result = subprocess.run([node,'-e',runner],input=json.dumps(dict(script=script,health=health,mode=mode)),text=True,capture_output=True,check=True)
    rendered = json.loads(result.stdout)
    assert rendered['hidden'] is (mode != 'current')
    if mode != 'current': assert rendered['banner'].startswith('BLOCKED')


def test_mixed_report_and_health_is_rejected(tmp_path,monkeypatch):
    arrange(monkeypatch)
    generate.build_dashboard(tmp_path/'index.html',NOW)
    health = json.loads((tmp_path/'health.json').read_text())
    health['status']='blocked'
    (tmp_path/'health.json').write_text(json.dumps(health))
    with pytest.raises(ValueError, match='blocked source'):
        validate(tmp_path)

def test_generation_crossing_session_boundary_rechecks_coverage(tmp_path,monkeypatch):
    arrange(monkeypatch)
    moments=iter([dt.datetime.fromisoformat('2026-09-09T16:44:00+02:00'),
                  dt.datetime.fromisoformat('2026-09-09T16:46:00+02:00')])
    monkeypatch.setattr(generate, '_now_utc', lambda:next(moments))
    health=generate.build_dashboard(tmp_path/'index.html')
    assert health['status']=='blocked'
    assert health['expected_session']=='2026-09-09'
    assert health['coverage']['current']==0 and health['coverage']['stale']==1
    assert health['coverage']['actionable_count']==0
    assert health['signal_counts']['BUY']==0
    validate(tmp_path)
