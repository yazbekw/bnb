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

# Optimized indicator settings
rsi_period = 14
sma_short = 20  # تقليل من 50 لزيادة الحساسية
sma_long = 100  # تقليل من 200 لزيادة الإشارات
rsi_buy_threshold = 55  # تخفيف الشروط
rsi_sell_threshold = 45  # تخفيف الشروط
atr_period = 14
atr_multiplier_sl = 1.2  # تقليل من 1.5
atr_multiplier_tp = 2.0  # تقليل من 3.0
max_leverage = 10.0  # زيادة الرافعة
min_leverage = 3.0   # زيادة الحد الأدنى

# إضافة مؤشرات جديدة
volume_sma_period = 20
vwap_period = 20
macd_fast = 12
macd_slow = 26
macd_signal = 9

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
    # المتوسطات المتحركة
    df['sma20'] = df['close'].rolling(window=sma_short).mean()
    df['sma100'] = df['close'].rolling(window=sma_long).mean()
    
    # RSI محسن
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
    
    # MACD مبسط
    exp1 = df['close'].ewm(span=macd_fast).mean()
    exp2 = df['close'].ewm(span=macd_slow).mean()
    df['macd'] = exp1 - exp2
    df['macd_signal'] = df['macd'].ewm(span=macd_signal).mean()
    
    # مؤشر الحجم
    df['volume_sma'] = df['volume'].rolling(volume_sma_period).mean()
    
    return df.dropna().reset_index(drop=True)

def calculate_leverage(atr, price):
    """حساب الرافعة بشكل أكثر عدوانية مع إدارة المخاطر"""
    volatility_ratio = atr / price
    if volatility_ratio < 0.01:
        return max_leverage
    elif volatility_ratio < 0.02:
        return 8.0
    elif volatility_ratio < 0.03:
        return 6.0
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
    
    for i in range(2, len(data)):
        prev2 = data.iloc[i-2]
        prev = data.iloc[i-1]
        curr = data.iloc[i]
        
        # شروط شراء محسنة - أكثر مرونة
        buy_condition1 = (curr['sma20'] > curr['sma100']) and (prev['sma20'] <= prev['sma100'])  # تقاطع صاعد
        buy_condition2 = (curr['sma20'] > curr['sma100']) and (curr['rsi'] > rsi_buy_threshold) and (curr['rsi'] < 80)  # RSI معقول
        buy_condition3 = (curr['close'] > curr['vwap'])  # السعر فوق VWAP
        buy_condition4 = (curr['macd'] > curr['macd_signal']) and (prev['macd'] <= prev['macd_signal'])  # تقاطع MACD صاعد
        buy_condition5 = (curr['volume'] > curr['volume_sma'] * 0.8)  # شرط حجم مخفف
        
        # شروط بيع محسنة - أكثر مرونة  
        sell_condition1 = (curr['sma20'] < curr['sma100']) and (prev['sma20'] >= prev['sma100'])  # تقاطع هابط
        sell_condition2 = (curr['sma20'] < curr['sma100']) and (curr['rsi'] < rsi_sell_threshold) and (curr['rsi'] > 20)  # RSI معقول
        sell_condition3 = (curr['close'] < curr['vwap'])  # السعر تحت VWAP
        sell_condition4 = (curr['macd'] < curr['macd_signal']) and (prev['macd'] >= prev['macd_signal'])  # تقاطع MACD هابط
        sell_condition5 = (curr['volume'] > curr['volume_sma'] * 0.8)  # شرط حجم مخفف
        
        # إشارات الشراء - تحتاج لتحقيق 3 من 5 شروط
        buy_score = sum([buy_condition1, buy_condition2, buy_condition3, buy_condition4, buy_condition5])
        buy_signal = buy_score >= 3
        
        # إشارات البيع - تحتاج لتحقيق 3 من 5 شروط
        sell_score = sum([sell_condition1, sell_condition2, sell_condition3, sell_condition4, sell_condition5])
        sell_signal = sell_score >= 3
        
        if buy_signal:
            buy_signals += 1
            leverage = calculate_leverage(curr['atr'], curr['open'])
            
            if current_position and current_position['side'] == 'SHORT':
                # إغلاق المركز القصير
                exit_price = curr['open']
                pnl = (current_position['entry_price'] - exit_price) / current_position['entry_price'] * current_position['leverage']
                positions.append(pnl)
                total_trades += 1
                current_position = None
            
            if not current_position:
                # فتح مركز طويل جديد
                current_position = {
                    'side': 'LONG', 
                    'entry_price': curr['open'], 
                    'atr': curr['atr'], 
                    'leverage': leverage,
                    'entry_time': i
                }
        
        if sell_signal:
            sell_signals += 1
            leverage = calculate_leverage(curr['atr'], curr['open'])
            
            if current_position and current_position['side'] == 'LONG':
                # إغلاق المركز الطويل
                exit_price = curr['open']
                pnl = (exit_price - current_position['entry_price']) / current_position['entry_price'] * current_position['leverage']
                positions.append(pnl)
                total_trades += 1
                current_position = None
            
            if not current_position:
                # فتح مركز قصير جديد
                current_position = {
                    'side': 'SHORT', 
                    'entry_price': curr['open'], 
                    'atr': curr['atr'], 
                    'leverage': leverage,
                    'entry_time': i
                }
        
        # إدارة المراكز المفتوحة مع جني الأرباح المتحرك المحسن
        if current_position:
            atr = current_position['atr']
            leverage = current_position['leverage']
            entry_price = current_position['entry_price']
            time_in_trade = i - current_position['entry_time']
            
            if current_position['side'] == 'LONG':
                # وقف الخسارة الأساسي
                sl = entry_price - (atr * atr_multiplier_sl)
                
                # جني الأرباح الديناميكي
                current_profit = (curr['close'] - entry_price) / entry_price * leverage
                if current_profit > 0.05:  # إذا كان الربح 5%
                    dynamic_tp = entry_price + (atr * atr_multiplier_tp * 1.5)
                else:
                    dynamic_tp = entry_price + (atr * atr_multiplier_tp)
                
                # الخروج إذا وصل لوقف الخسارة أو جني الأرباح
                if curr['low'] <= sl:
                    pnl = (sl - entry_price) / entry_price * leverage
                    positions.append(pnl)
                    total_trades += 1
                    current_position = None
                elif curr['high'] >= dynamic_tp:
                    pnl = (dynamic_tp - entry_price) / entry_price * leverage
                    positions.append(pnl)
                    total_trades += 1
                    current_position = None
                # جني الأرباح بعد فترة إذا كان هناك ربح صغير
                elif time_in_trade > 20 and current_profit > 0.02:  # بعد 20 شمعة وربح 2%
                    exit_price = curr['close']
                    pnl = (exit_price - entry_price) / entry_price * leverage
                    positions.append(pnl)
                    total_trades += 1
                    current_position = None
                    
            else:  # SHORT position
                sl = entry_price + (atr * atr_multiplier_sl)
                
                current_profit = (entry_price - curr['close']) / entry_price * leverage
                if current_profit > 0.05:
                    dynamic_tp = entry_price - (atr * atr_multiplier_tp * 1.5)
                else:
                    dynamic_tp = entry_price - (atr * atr_multiplier_tp)
                
                if curr['high'] >= sl:
                    pnl = (entry_price - sl) / entry_price * leverage
                    positions.append(pnl)
                    total_trades += 1
                    current_position = None
                elif curr['low'] <= dynamic_tp:
                    pnl = (entry_price - dynamic_tp) / entry_price * leverage
                    positions.append(pnl)
                    total_trades += 1
                    current_position = None
                elif time_in_trade > 20 and current_profit > 0.02:
                    exit_price = curr['close']
                    pnl = (entry_price - exit_price) / entry_price * leverage
                    positions.append(pnl)
                    total_trades += 1
                    current_position = None
    
    # إغلاق أي مركز مفتوح في النهاية
    if current_position:
        exit_price = data.iloc[-1]['close']
        leverage = current_position['leverage']
        if current_position['side'] == 'LONG':
            pnl = (exit_price - current_position['entry_price']) / current_position['entry_price'] * leverage
        else:
            pnl = (current_position['entry_price'] - exit_price) / current_position['entry_price'] * leverage
        positions.append(pnl)
        total_trades += 1
    
    # حساب العائد التراكمي
    if positions:
        cumulative_return = (np.prod([1 + p for p in positions]) - 1) * 100
        win_rate = (sum(1 for p in positions if p > 0) / len(positions)) * 100
    else:
        cumulative_return = 0.0
        win_rate = 0.0
    
    return {
        'buy_signals': buy_signals,
        'sell_signals': sell_signals,
        'expected_return': round(cumulative_return, 2),
        'total_trades': total_trades,
        'win_rate': round(win_rate, 2),
        'avg_trade_return': round(np.mean(positions) * 100, 2) if positions else 0.0
    }

# تشغيل الاختبار وطباعة النتائج
print("جاري تشغيل الاختبارات المحسنة...")
results = {}
for symbol in symbols:
    results[symbol] = {}
    for interval in intervals:
        results[symbol][interval] = backtest(symbol, interval)

print("\n" + "="*50)
print("نتائج الاستراتيجية المحسنة:")
print("="*50)
for symbol in symbols:
    for interval in intervals:
        result = results[symbol][interval]
        print(f"{symbol} ({interval}):")
        print(f"  - إشارات شراء: {result['buy_signals']}")
        print(f"  - إشارات بيع: {result['sell_signals']}")
        print(f"  - إجمالي الصفقات: {result['total_trades']}")
        print(f"  - العائد المتوقع: {result['expected_return']}%")
        print(f"  - نسبة الصفقات الرابحة: {result['win_rate']}%")
        print(f"  - متوسط عائد الصفقة: {result['avg_trade_return']}%")
        print()
