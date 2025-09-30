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
start_date = end_date - timedelta(days=60)  # زيادة الفترة لشهرين
start_ts = int(start_date.timestamp() * 1000)
end_ts = int(end_date.timestamp() * 1000)

# إعدادات محسنة بشكل كبير
rsi_period = 14
sma_short = 10    # تقليل أكثر للحساسية
sma_long = 50     # تقليل أكثر
rsi_buy_threshold = 50  # تخفيف كبير
rsi_sell_threshold = 50 # تخفيف كبير
atr_period = 14
atr_multiplier_sl = 1.0  # وقف خسارة أقرب
atr_multiplier_tp = 1.5  # أهداف أقرب
max_leverage = 8.0
min_leverage = 2.0

# إعدادات المؤشرات المساعدة
volume_sma_period = 10
vwap_period = 14
macd_fast = 8
macd_slow = 21
macd_signal = 5

def get_historical_data(symbol, interval, start_ts, end_ts):
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        'symbol': symbol,
        'interval': interval,
        'startTime': start_ts,
        'endTime': end_ts,
        'limit': 2000  # زيادة الحد
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
    # المتوسطات المتحركة السريعة
    df['sma10'] = df['close'].rolling(window=sma_short).mean()
    df['sma50'] = df['close'].rolling(window=sma_long).mean()
    df['sma5'] = df['close'].rolling(window=5).mean()  # متوسط سريع إضافي
    
    # RSI مع إعدادات مرنة
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(rsi_period).mean()
    loss = -delta.where(delta < 0, 0).rolling(rsi_period).mean()
    rs = gain / (loss + 1e-10)
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # ATR
    tr = pd.DataFrame({
        'high_low': df['high'] - df['low'],
        'high_close': abs(df['high'] - df['close'].shift()),
        'low_close': abs(df['low'] - df['close'].shift())
    }).max(axis=1)
    df['atr'] = tr.rolling(atr_period).mean()
    
    # VWAP مبسط
    df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3
    df['vwap'] = (df['typical_price'] * df['volume']).rolling(vwap_period).sum() / df['volume'].rolling(vwap_period).sum()
    
    # MACD سريع
    exp1 = df['close'].ewm(span=macd_fast).mean()
    exp2 = df['close'].ewm(span=macd_slow).mean()
    df['macd'] = exp1 - exp2
    df['macd_signal'] = df['macd'].ewm(span=macd_signal).mean()
    df['macd_histogram'] = df['macd'] - df['macd_signal']
    
    # مؤشرات الحجم
    df['volume_sma'] = df['volume'].rolling(volume_sma_period).mean()
    df['volume_ratio'] = df['volume'] / df['volume_sma']
    
    # تقلبات السعر
    df['price_change'] = df['close'].pct_change()
    df['volatility'] = df['price_change'].rolling(10).std()
    
    return df.dropna().reset_index(drop=True)

def calculate_leverage(atr, price, volatility):
    """حساب رافعة أكثر عدوانية مع مراعاة التقلبات"""
    volatility_ratio = atr / price
    if volatility_ratio < 0.008:
        return max_leverage
    elif volatility_ratio < 0.015:
        return 6.0
    elif volatility_ratio < 0.025:
        return 4.0
    else:
        return min_leverage

def backtest(symbol, interval):
    data = get_historical_data(symbol, interval, start_ts, end_ts)
    if data is None or len(data) < sma_long:
        return {'buy_signals': 0, 'sell_signals': 0, 'expected_return': 0.0, 'total_trades': 0}
    
    data = calculate_indicators(data)
    
    buy_signals = 0
    sell_signals = 0
    positions = []
    current_position = None
    total_trades = 0
    
    for i in range(3, len(data)):
        prev3 = data.iloc[i-3]
        prev2 = data.iloc[i-2]
        prev = data.iloc[i-1]
        curr = data.iloc[i]
        
        # شروط شراء موسعة - أكثر مرونة
        buy_conditions = [
            # 1. تقاطع المتوسطات
            (curr['sma10'] > curr['sma50']) and (prev['sma10'] <= prev['sma50']),
            (curr['sma5'] > curr['sma10']) and (prev['sma5'] <= prev['sma10']),
            
            # 2. شروط RSI مرنة
            (curr['rsi'] > 45) and (curr['rsi'] < 70),
            (curr['rsi'] > 50) and (prev['rsi'] <= 50),
            
            # 3. اتجاه الاتجاه
            (curr['close'] > curr['sma50']),
            (curr['close'] > curr['vwap']),
            
            # 4. زخم MACD
            (curr['macd'] > curr['macd_signal']),
            (curr['macd_histogram'] > prev['macd_histogram']),
            
            # 5. شروط الحجم المخففة
            (curr['volume_ratio'] > 0.7),
            (curr['volume'] > prev['volume'])
        ]
        
        # شروط بيع موسعة
        sell_conditions = [
            # 1. تقاطع المتوسطات
            (curr['sma10'] < curr['sma50']) and (prev['sma10'] >= prev['sma50']),
            (curr['sma5'] < curr['sma10']) and (prev['sma5'] >= prev['sma10']),
            
            # 2. شروط RSI مرنة
            (curr['rsi'] < 55) and (curr['rsi'] > 30),
            (curr['rsi'] < 50) and (prev['rsi'] >= 50),
            
            # 3. اتجاه الاتجاه
            (curr['close'] < curr['sma50']),
            (curr['close'] < curr['vwap']),
            
            # 4. زخم MACD
            (curr['macd'] < curr['macd_signal']),
            (curr['macd_histogram'] < prev['macd_histogram']),
            
            # 5. شروط الحجم المخففة
            (curr['volume_ratio'] > 0.7),
            (curr['volume'] > prev['volume'])
        ]
        
        # إشارات الشراء - تحتاج 4 من 10 شروط فقط
        buy_score = sum(buy_conditions)
        buy_signal = buy_score >= 4
        
        # إشارات البيع - تحتاج 4 من 10 شروط فقط  
        sell_score = sum(sell_conditions)
        sell_signal = sell_score >= 4
        
        # إدارة المراكز
        if buy_signal:
            buy_signals += 1
            leverage = calculate_leverage(curr['atr'], curr['open'], curr['volatility'])
            
            if current_position and current_position['side'] == 'SHORT':
                exit_price = curr['open']
                pnl = (current_position['entry_price'] - exit_price) / current_position['entry_price'] * current_position['leverage']
                positions.append(pnl)
                total_trades += 1
                current_position = None
            
            if not current_position:
                current_position = {
                    'side': 'LONG', 
                    'entry_price': curr['open'], 
                    'atr': curr['atr'], 
                    'leverage': leverage,
                    'entry_time': i
                }
        
        if sell_signal:
            sell_signals += 1
            leverage = calculate_leverage(curr['atr'], curr['open'], curr['volatility'])
            
            if current_position and current_position['side'] == 'LONG':
                exit_price = curr['open']
                pnl = (exit_price - current_position['entry_price']) / current_position['entry_price'] * current_position['leverage']
                positions.append(pnl)
                total_trades += 1
                current_position = None
            
            if not current_position:
                current_position = {
                    'side': 'SHORT', 
                    'entry_price': curr['open'], 
                    'atr': curr['atr'], 
                    'leverage': leverage,
                    'entry_time': i
                }
        
        # إدارة المراكز المفتوحة مع خروج مبكر
        if current_position:
            atr = current_position['atr']
            leverage = current_position['leverage']
            entry_price = current_position['entry_price']
            time_in_trade = i - current_position['entry_time']
            
            if current_position['side'] == 'LONG':
                sl = entry_price - (atr * atr_multiplier_sl)
                tp = entry_price + (atr * atr_multiplier_tp)
                
                # خروج مبكر إذا تحقق شرط
                if curr['low'] <= sl or curr['high'] >= tp or time_in_trade > 15:
                    exit_price = sl if curr['low'] <= sl else (tp if curr['high'] >= tp else curr['close'])
                    pnl = (exit_price - entry_price) / entry_price * leverage
                    positions.append(pnl)
                    total_trades += 1
                    current_position = None
                    
            else:  # SHORT
                sl = entry_price + (atr * atr_multiplier_sl)
                tp = entry_price - (atr * atr_multiplier_tp)
                
                if curr['high'] >= sl or curr['low'] <= tp or time_in_trade > 15:
                    exit_price = sl if curr['high'] >= sl else (tp if curr['low'] <= tp else curr['close'])
                    pnl = (entry_price - exit_price) / entry_price * leverage
                    positions.append(pnl)
                    total_trades += 1
                    current_position = None
    
    # إغلاق المركز المتبقي
    if current_position:
        exit_price = data.iloc[-1]['close']
        leverage = current_position['leverage']
        if current_position['side'] == 'LONG':
            pnl = (exit_price - current_position['entry_price']) / current_position['entry_price'] * leverage
        else:
            pnl = (current_position['entry_price'] - exit_price) / current_position['entry_price'] * leverage
        positions.append(pnl)
        total_trades += 1
    
    # الإحصائيات
    if positions:
        cumulative_return = (np.prod([1 + p for p in positions]) - 1) * 100
        win_rate = (sum(1 for p in positions if p > 0) / len(positions)) * 100
        profit_factor = sum(p for p in positions if p > 0) / abs(sum(p for p in positions if p < 0)) if any(p < 0 for p in positions) else float('inf')
    else:
        cumulative_return = 0.0
        win_rate = 0.0
        profit_factor = 0.0
    
    return {
        'buy_signals': buy_signals,
        'sell_signals': sell_signals,
        'expected_return': round(cumulative_return, 2),
        'total_trades': total_trades,
        'win_rate': round(win_rate, 2),
        'profit_factor': round(profit_factor, 2),
        'avg_trade_return': round(np.mean(positions) * 100, 2) if positions else 0.0
    }

# التشغيل والنتائج
print("جاري تشغيل الاستراتيجية المحسنة لزيادة الصفقات...")
results = {}
for symbol in symbols:
    results[symbol] = {}
    for interval in intervals:
        results[symbol][interval] = backtest(symbol, interval)

print("\n" + "="*60)
print("نتائج الاستراتيجية المحسنة - زيادة الصفقات")
print("="*60)
for symbol in symbols:
    for interval in intervals:
        result = results[symbol][interval]
        print(f"\n{symbol} ({interval}):")
        print(f"  📈 إشارات شراء: {result['buy_signals']}")
        print(f"  📉 إشارات بيع: {result['sell_signals']}")
        print(f"  🔢 إجمالي الصفقات: {result['total_trades']}")
        print(f"  💰 العائد المتوقع: {result['expected_return']}%")
        print(f"  ✅ نسبة الصفقات الرابحة: {result['win_rate']}%")
        print(f"  📊 عامل الربحية: {result['profit_factor']}")
        print(f"  📋 متوسط عائد الصفقة: {result['avg_trade_return']}%")
