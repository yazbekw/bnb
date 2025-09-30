import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
import pytz
import time

# الإعدادات النهائية المثبتة
symbols = ["LINKUSDT", "SOLUSDT", "ETHUSDT", "BNBUSDT"]  # إعادة ترتيب حسب الأداء
optimal_intervals = ['30m', '1h']  # الفترات الأفضل

# الأوزان النهائية بناءً على النتائج
FINAL_SYMBOL_WEIGHTS = {
    'LINKUSDT': 1.4,  # زيادة الوزن أكثر
    'SOLUSDT': 1.2,   # زيادة الوزن
    'ETHUSDT': 1.0,   # وزن ثابت
    'BNBUSDT': 0.7    # تقليل الوزن أكثر
}

# تحسين إستراتيجية البيع بناءً على النتائج الممتازة
BUY_BIAS = 1.1  # تقليل التحيز قليلاً
SELL_BIAS = 1.0  # زيادة وزن البيع

def get_data(symbol, interval, days=45):  # تقليل الفترة لبيانات أحدث
    end_date = datetime.now(pytz.UTC)
    start_date = end_date - timedelta(days=days)
    start_ts = int(start_date.timestamp() * 1000)
    
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        'symbol': symbol, 
        'interval': interval,
        'startTime': start_ts, 
        'limit': 1000
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        df = pd.DataFrame(data, columns=[
            'timestamp', 'open', 'high', 'low', 'close', 'volume', 
            'ignore', 'ignore', 'ignore', 'ignore', 'ignore', 'ignore'
        ])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
        return df
    except Exception as e:
        print(f"Error fetching {symbol} {interval}: {e}")
        return None

def calculate_final_indicators(df):
    # المؤشرات المثبتة فعاليتها
    df['sma10'] = df['close'].rolling(10).mean()
    df['sma50'] = df['close'].rolling(50).mean()
    df['sma20'] = df['close'].rolling(20).mean()
    
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
    
    # مؤشرات الزخم
    df['momentum'] = df['close'] / df['close'].shift(5) - 1
    df['volume_ratio'] = df['volume'] / df['volume'].rolling(20).mean()
    
    return df.dropna()

def final_backtest(symbol, interval):
    data = get_data(symbol, interval)
    if data is None or len(data) < 100:
        return None
    
    data = calculate_final_indicators(data)
    
    trades_details = {
        'long_trades': {'win': 0, 'loss': 0, 'total': 0},
        'short_trades': {'win': 0, 'loss': 0, 'total': 0},
        'all_trades': {'win': 0, 'loss': 0, 'total': 0}
    }
    
    positions = []
    current_position = None
    symbol_weight = FINAL_SYMBOL_WEIGHTS.get(symbol, 1.0)
    
    for i in range(5, len(data)):
        prev, curr = data.iloc[i-1], data.iloc[i]
        
        # شروط الشراء المحسنة
        buy_conditions = [
            (curr['sma10'] > curr['sma50']),
            (curr['sma10'] > curr['sma20']),
            (45 <= curr['rsi'] <= 70),
            (curr['momentum'] > 0.002),
            (curr['volume_ratio'] > 0.9),
            (curr['close'] > curr['sma20'])
        ]
        
        # شروط البيع المحسنة - نطاق RSI أوسع
        sell_conditions = [
            (curr['sma10'] < curr['sma50']),
            (curr['sma10'] < curr['sma20']), 
            (30 <= curr['rsi'] <= 65),  # نطاق أوسع للبيع
            (curr['momentum'] < -0.003),
            (curr['volume_ratio'] > 1.1),
            (curr['close'] < curr['sma20'])
        ]
        
        buy_score = sum(buy_conditions) * BUY_BIAS * symbol_weight
        sell_score = sum(sell_conditions) * SELL_BIAS * symbol_weight
        
        buy_signal = buy_score >= 3.2
        sell_signal = sell_score >= 3.5
        
        # رافعة متوازنة بناءً على النتائج
        leverage = 2.2 * symbol_weight
        
        # إدارة الصفقات
        if buy_signal and (not current_position or current_position['side'] == 'SHORT'):
            if current_position and current_position['side'] == 'SHORT':
                pnl = (current_position['price'] - curr['open']) / current_position['price'] * leverage
                positions.append(pnl)
                # تحديث الإحصائيات...
            
            if not current_position:
                current_position = {'side': 'LONG', 'price': curr['open'], 'atr': curr['atr']}
        
        elif sell_signal and (not current_position or current_position['side'] == 'LONG'):
            if current_position and current_position['side'] == 'LONG':
                pnl = (curr['open'] - current_position['price']) / current_position['price'] * leverage
                positions.append(pnl)
                # تحديث الإحصائيات...
            
            if not current_position:
                current_position = {'side': 'SHORT', 'price': curr['open'], 'atr': curr['atr']}
        
        # إدارة المخاطر النهائية
        if current_position:
            entry = current_position['price']
            atr = current_position['atr']
            
            if current_position['side'] == 'LONG':
                sl = entry - (atr * 0.9)
                tp = entry + (atr * 2.2)
                
                if curr['low'] <= sl or curr['high'] >= tp:
                    # حساب PNL وإحصاءات...
                    current_position = None
            else:
                sl = entry + (atr * 1.1)
                tp = entry - (atr * 1.8)
                
                if curr['high'] >= sl or curr['low'] <= tp:
                    # حساب PNL وإحصاءات...
                    current_position = None
    
    # حساب النتائج النهائية
    if positions:
        total_return = (np.prod([1 + p for p in positions]) - 1) * 100
        win_rate = (trades_details['all_trades']['win'] / trades_details['all_trades']['total'] * 100) if trades_details['all_trades']['total'] > 0 else 0
    else:
        total_return, win_rate = 0, 0
    
    return {
        'details': trades_details,
        'trades_count': len(positions),
        'total_return': round(total_return, 2),
        'win_rate': round(win_rate, 2),
        'symbol_weight': symbol_weight
    }

# التشغيل النهائي
print("🚀 التشغيل النهائي للاستراتيجية المثبتة")
print("=" * 50)

for symbol in symbols:
    print(f"\n🎯 {symbol} (الوزن: {FINAL_SYMBOL_WEIGHTS[symbol]}):")
    for interval in optimal_intervals:
        time.sleep(1)
        result = final_backtest(symbol, interval)
        if result:
            print(f"   ⏰ {interval}: {result['trades_count']} صفقة | {result['total_return']}% عائد | {result['win_rate']}% ربح")
