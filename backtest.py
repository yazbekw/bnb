import pandas as pd
import numpy as np
import requests
import json
from datetime import datetime, timedelta
import pytz

# Symbols and intervals
symbols = ["SOLUSDT", "ETHUSDT", "BNBUSDT", "LINKUSDT"]
intervals = ['30m']

# Date range (last month)
end_date = datetime.now(pytz.UTC)
start_date = end_date - timedelta(days=30)
start_ts = int(start_date.timestamp() * 1000)
end_ts = int(end_date.timestamp() * 1000)

# Indicator settings
rsi_period = 14
sma_short = 50
sma_long = 200
rsi_buy_threshold = 60
rsi_sell_threshold = 40
atr_period = 14
atr_multiplier_sl = 1.5
atr_multiplier_tp = 3.0
max_leverage = 5.0
min_leverage = 1.0

def get_historical_data(symbol, interval, start_ts, end_ts):
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        'symbol': symbol,
        'interval': interval,
        'startTime': start_ts,
        'endTime': end_ts,
        'limit': 1500
    }
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        klines = json.loads(response.text)
        if not klines:
            return None
        data = pd.DataFrame(klines, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])
        data['timestamp'] = pd.to_datetime(data['timestamp'], unit='ms')
        data[['open', 'high', 'low', 'close', 'volume']] = data[['open', 'high', 'low', 'close', 'volume']].astype(float)
        return data
    except Exception as e:
        print(f"Error fetching {symbol} {interval}: {e}")
        return None

def calculate_indicators(df):
    df['sma50'] = df['close'].rolling(window=sma_short).mean()
    df['sma200'] = df['close'].rolling(window=sma_long).mean()
    
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(rsi_period).mean()
    loss = -delta.where(delta < 0, 0).rolling(rsi_period).mean()
    rs = gain / (loss + 1e-10)
    df['rsi'] = 100 - (100 / (1 + rs))
    
    tr = pd.DataFrame({
        'high_low': df['high'] - df['low'],
        'high_close': abs(df['high'] - df['close'].shift()),
        'low_close': abs(df['low'] - df['close'].shift())
    }).max(axis=1)
    df['atr'] = tr.rolling(atr_period).mean()
    
    return df.dropna().reset_index(drop=True)

def backtest(symbol, interval):
    data = get_historical_data(symbol, interval, start_ts, end_ts)
    if data is None or len(data) < sma_long:
        return {'buy_signals': 0, 'sell_signals': 0, 'expected_return': 0.0}
    
    data = calculate_indicators(data)
    
    buy_signals = 0
    sell_signals = 0
    positions = []
    current_position = None
    
    for i in range(1, len(data)):
        prev = data.iloc[i-1]
        curr = data.iloc[i]
        
        buy_signal = (curr['sma50'] > curr['sma200']) and (prev['sma50'] <= prev['sma200']) and \
                     (curr['rsi'] > rsi_buy_threshold)
        sell_signal = (curr['sma50'] < curr['sma200']) and (prev['sma50'] >= prev['sma200']) and \
                      (curr['rsi'] < rsi_sell_threshold)
        
        if buy_signal:
            buy_signals += 1
            leverage = min(max_leverage, max(min_leverage, 5.0 / (curr['atr'] / curr['open'])))
            if current_position and current_position['side'] == 'SHORT':
                exit_price = curr['open']
                pnl = (current_position['entry_price'] - exit_price) / current_position['entry_price'] * current_position['leverage']
                positions.append(pnl)
            if not current_position:
                current_position = {'side': 'LONG', 'entry_price': curr['open'], 'atr': curr['atr'], 'leverage': leverage}
        
        if sell_signal:
            sell_signals += 1
            leverage = min(max_leverage, max(min_leverage, 5.0 / (curr['atr'] / curr['open'])))
            if current_position and current_position['side'] == 'LONG':
                exit_price = curr['open']
                pnl = (exit_price - current_position['entry_price']) / current_position['entry_price'] * current_position['leverage']
                positions.append(pnl)
            if not current_position:
                current_position = {'side': 'SHORT', 'entry_price': curr['open'], 'atr': curr['atr'], 'leverage': leverage}
        
        if current_position:
            atr = current_position['atr']
            leverage = current_position['leverage']
            if current_position['side'] == 'LONG':
                sl = current_position['entry_price'] - (atr * atr_multiplier_sl)
                tp = current_position['entry_price'] + (atr * atr_multiplier_tp)
                if curr['low'] <= sl:
                    pnl = (sl - current_position['entry_price']) / current_position['entry_price'] * leverage
                    positions.append(pnl)
                    current_position = None
                elif curr['high'] >= tp:
                    pnl = (tp - current_position['entry_price']) / current_position['entry_price'] * leverage
                    positions.append(pnl)
                    current_position = None
            else:
                sl = current_position['entry_price'] + (atr * atr_multiplier_sl)
                tp = current_position['entry_price'] - (atr * atr_multiplier_tp)
                if curr['high'] >= sl:
                    pnl = (current_position['entry_price'] - sl) / current_position['entry_price'] * leverage
                    positions.append(pnl)
                    current_position = None
                elif curr['low'] <= tp:
                    pnl = (current_position['entry_price'] - tp) / current_position['entry_price'] * leverage
                    positions.append(pnl)
                    current_position = None
    
    if current_position:
        exit_price = data.iloc[-1]['close']
        leverage = current_position['leverage']
        pnl = (exit_price - current_position['entry_price']) / current_position['entry_price'] if current_position['side'] == 'LONG' else (current_position['entry_price'] - exit_price) / current_position['entry_price']
        pnl *= leverage
        positions.append(pnl)
    
    cum_return = (np.prod([1 + p for p in positions]) - 1) * 100 if positions else 0.0
    
    return {
        'buy_signals': buy_signals,
        'sell_signals': sell_signals,
        'expected_return': round(cum_return, 2)
    }

# Run and print results
results = {}
for symbol in symbols:
    results[symbol] = {}
    for interval in intervals:
        results[symbol][interval] = backtest(symbol, interval)

print("Results:")
print(results)
