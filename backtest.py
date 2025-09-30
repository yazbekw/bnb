import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
import pytz
import time

# الإعدادات الأساسية
symbols = ["SOLUSDT", "ETHUSDT", "BNBUSDT", "LINKUSDT"]

def get_data(symbol, days=60):
    end_date = datetime.now(pytz.UTC)
    start_date = end_date - timedelta(days=days)
    start_ts = int(start_date.timestamp() * 1000)
    
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        'symbol': symbol, 'interval': '30m',
        'startTime': start_ts, 'limit': 1000
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'ignore', 'ignore', 'ignore', 'ignore', 'ignore', 'ignore'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
        return df
    except:
        return None

def calculate_indicators(df):
    df['sma10'] = df['close'].rolling(10).mean()
    df['sma50'] = df['close'].rolling(50).mean()
    
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta).where(delta < 0, 0).rolling(14).mean()
    rs = gain / (loss + 1e-10)
    df['rsi'] = 100 - (100 / (1 + rs))
    
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = np.maximum(np.maximum(high_low, high_close), low_close)
    df['atr'] = tr.rolling(14).mean()
    
    return df.dropna()

def backtest(symbol):
    data = get_data(symbol)
    if data is None or len(data) < 100:
        return None
    
    data = calculate_indicators(data)
    positions = []
    current_position = None
    
    for i in range(2, len(data)):
        prev, curr = data.iloc[i-1], data.iloc[i]
        
        # إشارات التداول
        buy_signal = (curr['sma10'] > curr['sma50']) and (curr['rsi'] > 45)
        sell_signal = (curr['sma10'] < curr['sma50']) and (curr['rsi'] < 55)
        
        leverage = 2.0
        
        if buy_signal and (not current_position or current_position['side'] == 'SHORT'):
            if current_position:
                pnl = (current_position['price'] - curr['open']) / current_position['price'] * leverage
                positions.append(pnl)
            current_position = {'side': 'LONG', 'price': curr['open'], 'atr': curr['atr']}
        
        elif sell_signal and (not current_position or current_position['side'] == 'LONG'):
            if current_position:
                pnl = (curr['open'] - current_position['price']) / current_position['price'] * leverage
                positions.append(pnl)
            current_position = {'side': 'SHORT', 'price': curr['open'], 'atr': curr['atr']}
        
        # وقف الخسارة وجني الأرباح
        if current_position:
            entry = current_position['price']
            atr = current_position['atr']
            
            if current_position['side'] == 'LONG':
                if curr['low'] <= entry - (atr * 1.0):
                    positions.append(-0.02)
                    current_position = None
                elif curr['high'] >= entry + (atr * 1.5):
                    positions.append(0.03)
                    current_position = None
            else:
                if curr['high'] >= entry + (atr * 1.0):
                    positions.append(-0.02)
                    current_position = None
                elif curr['low'] <= entry - (atr * 1.5):
                    positions.append(0.03)
                    current_position = None
    
    # النتائج
    if positions:
        total_return = (np.prod([1 + p for p in positions]) - 1) * 100
        win_rate = (sum(1 for p in positions if p > 0) / len(positions)) * 100
    else:
        total_return, win_rate = 0, 0
    
    return len(positions), round(total_return, 2), round(win_rate, 2)

# التشغيل
print("جاري الاختبار...\n")
results = {}

for symbol in symbols:
    time.sleep(1)
    trades, ret, win_rate = backtest(symbol)
    results[symbol] = (trades, ret, win_rate)
    print(f"{symbol}: {trades} صفقة | {ret}% عائد | {win_rate}% نسبة ربح")

# الملخص
total_trades = sum(r[0] for r in results.values())
avg_return = np.mean([r[1] for r in results.values()])

print(f"\nالملخص: {total_trades} صفقة | {avg_return:.1f}% متوسط العائد")
