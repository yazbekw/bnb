import pandas as pd
import numpy as np
import requests
import json
from datetime import datetime, timedelta
import pytz
import time

# الإعدادات
symbols = ["SOLUSDT", "ETHUSDT", "BNBUSDT", "LINKUSDT"]
intervals = ['30m']

end_date = datetime.now(pytz.UTC)
start_date = end_date - timedelta(days=60)
start_ts = int(start_date.timestamp() * 1000)
end_ts = int(end_date.timestamp() * 1000)

# إعدادات محسنة
sma_short, sma_long = 10, 50
atr_multiplier_sl, atr_multiplier_tp = 1.0, 1.5
max_leverage, min_leverage = 3.0, 1.0  # تقليل الرافعة

def get_historical_data(symbol, interval, start_ts, end_ts):
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        'symbol': symbol, 'interval': interval,
        'startTime': start_ts, 'endTime': end_ts, 'limit': 1000
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        df = pd.DataFrame(data, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
        return df
    except:
        return None

def calculate_indicators(df):
    # المؤشرات الأساسية
    df['sma10'] = df['close'].rolling(10).mean()
    df['sma50'] = df['close'].rolling(50).mean()
    
    # RSI
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta).where(delta < 0, 0).rolling(14).mean()
    rs = gain / (loss + 1e-10)
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # ATR
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = np.maximum(np.maximum(high_low, high_close), low_close)
    df['atr'] = tr.rolling(14).mean()
    
    return df.dropna()

def backtest(symbol, interval):
    data = get_historical_data(symbol, interval, start_ts, end_ts)
    if data is None or len(data) < 100:
        return None
    
    data = calculate_indicators(data)
    
    positions = []
    current_position = None
    
    for i in range(2, len(data)):
        prev, curr = data.iloc[i-1], data.iloc[i]
        
        # شروط مبسطة
        buy_signal = (curr['sma10'] > curr['sma50']) and (curr['rsi'] > 45)
        sell_signal = (curr['sma10'] < curr['sma50']) and (curr['rsi'] < 55)
        
        leverage = 2.0  # رافعة ثابتة لتجنب المبالغة
        
        if buy_signal and (not current_position or current_position['side'] == 'SHORT'):
            if current_position and current_position['side'] == 'SHORT':
                # إغلاق قصير
                pnl = (current_position['price'] - curr['open']) / current_position['price'] * leverage
                positions.append(pnl)
            
            current_position = {'side': 'LONG', 'price': curr['open'], 'atr': curr['atr']}
        
        elif sell_signal and (not current_position or current_position['side'] == 'LONG'):
            if current_position and current_position['side'] == 'LONG':
                # إغلاق طويل
                pnl = (curr['open'] - current_position['price']) / current_position['price'] * leverage
                positions.append(pnl)
            
            current_position = {'side': 'SHORT', 'price': curr['open'], 'atr': curr['atr']}
        
        # إدارة المخاطر
        if current_position:
            entry = current_position['price']
            atr = current_position['atr']
            
            if current_position['side'] == 'LONG':
                if curr['low'] <= entry - (atr * atr_multiplier_sl):
                    pnl = -atr_multiplier_sl * leverage / 100
                    positions.append(pnl)
                    current_position = None
                elif curr['high'] >= entry + (atr * atr_multiplier_tp):
                    pnl = atr_multiplier_tp * leverage / 100
                    positions.append(pnl)
                    current_position = None
            else:
                if curr['high'] >= entry + (atr * atr_multiplier_sl):
                    pnl = -atr_multiplier_sl * leverage / 100
                    positions.append(pnl)
                    current_position = None
                elif curr['low'] <= entry - (atr * atr_multiplier_tp):
                    pnl = atr_multiplier_tp * leverage / 100
                    positions.append(pnl)
                    current_position = None
    
    # الإحصائيات
    if positions:
        total_return = (np.prod([1 + p for p in positions]) - 1) * 100
        win_rate = (sum(1 for p in positions if p > 0) / len(positions)) * 100
    else:
        total_return, win_rate = 0, 0
    
    return {
        'trades': len(positions),
        'return': round(total_return, 2),
        'win_rate': round(win_rate, 2),
        'avg_trade': round(np.mean(positions) * 100, 2) if positions else 0
    }

# التشغيل والنتائج
print("جاري الاختبار...")
results = {}

for symbol in symbols:
    time.sleep(1)
    result = backtest(symbol, '30m')
    if result:
        results[symbol] = result

# عرض مبسط
print("\n" + "="*40)
print("النتائج المبسطة")
print("="*40)

for symbol, res in results.items():
    print(f"{symbol}: {res['trades']} صفقة | {res['return']}% عائد | {res['win_rate']}% نسبة ربح")

total_trades = sum(r['trades'] for r in results.values())
avg_return = np.mean([r['return'] for r in results.values()])

print(f"\nالمجموع: {total_trades} صفقة | متوسط العائد: {avg_return:.1f}%")
