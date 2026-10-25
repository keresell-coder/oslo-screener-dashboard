import pandas as pd
from generate import parse_screener_results
from setup_semantics import describe_gates


def test_actual_missing_direction_reaches_card_without_rsi_delta_proxy():
    row=dict(ticker='X.OL',signal='BUY-watch',close=100,rsi14=35,rsi_dir=10,adx14=20,mfi14=30,macd_hist=-.001,pct_above_sma50=50,stop_loss_pct=3,primary_count=1,risk='MODERATE',date='2026-10-23',data_status='current',previous_close=100,day_direction='flat')
    results,_=parse_screener_results(pd.DataFrame([row]),{'X.OL'})
    assert 'Needs up-close (current flat)' in results[0].gate_summary
    assert results[0].signal=='BUY-watch'
    assert results[0].signal_label=='Rebound watch'
    row.pop('previous_close');row.pop('day_direction')
    assert 'Day direction unavailable' in describe_gates(row)


def test_excluded_source_cannot_reach_any_card():
    assert parse_screener_results(pd.DataFrame([{'ticker':'X.OL','signal':'BUY-watch'}]),set())[0]==[]


def test_liquidity_evidence_and_legacy_unknown_reach_cards():
    import datetime as dt
    from liquidity import assess_liquidity, session_window
    row=dict(ticker='X.OL',signal='BUY',close=100,rsi14=30,adx14=20,mfi14=30,macd_hist=-.001,pct_above_sma50=50,stop_loss_pct=3,primary_count=1,risk='MODERATE',date='2026-10-23',data_status='current')
    results,_=parse_screener_results(pd.DataFrame([row]), {'X.OL'})
    assert 'unverified' in results[0].suitability_summary
    frame=pd.DataFrame({'RawClose':100.,'Volume':1000.},index=pd.to_datetime(session_window(dt.date(2026,10,23))))
    frame.iloc[0,1]=0
    row.update(assess_liquidity(frame,dt.date(2026,10,23),'NOK'))
    results,_=parse_screener_results(pd.DataFrame([row]), {'X.OL'})
    assert results[0].signal=='BUY'  # Original technical classification unchanged.
    assert 'Ineligible under daily-trading requirement' in results[0].suitability_summary
    from jinja2 import Environment, FileSystemLoader
    from pathlib import Path
    env=Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1]/'templates'),autoescape=True)
    template=env.get_template('index.html.j2')
    assert 'suitability_summary' in template.environment.loader.get_source(env,'index.html.j2')[0]
