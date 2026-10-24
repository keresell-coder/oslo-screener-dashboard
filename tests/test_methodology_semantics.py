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
