import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
import pytz
import time

# الإعدادات المحسنة
symbols = ["SOLUSDT", "ETHUSDT", "BNBUSDT", "LINKUSDT"]
intervals = ['30m', '1h', '4h']  # فترات زمنية مختلفة

# أوزان العملات بناءً على الأداء السابق
SYMBOL_WEIGHTS = {
    'LINKUSDT': 1.3,  # الأفضل أداء - وزن أعلى
    'SOLUSDT': 1.1,   # جيد أداء - وزن فوق المتوسط
    'ETHUSDT': 1.0,   # متوسط الأداء - وزن عادي
    'BNBUSDT': 0.8    # أضعف أداء - وزن أقل
}

# إعدادات محسنة للشراء vs البيع
BUY_BIAS = 1.2  # التركيز على صفقات الشراء
SELL_BIAS = 0.8  # تقليل صفقات البيع

def get_data(symbol, interval, days=60):
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

def calculate_enhanced_indicators(df):
    # المتوسطات المتحركة
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
    
    # مؤشرات الزخم الإضافية
    df['momentum'] = df['close'] / df['close'].shift(5) - 1
    df['volume_ratio'] = df['volume'] / df['volume'].rolling(20).mean()
    
    return df.dropna()

def enhanced_backtest(symbol, interval):
    data = get_data(symbol, interval)
    if data is None or len(data) < 100:
        return None
    
    data = calculate_enhanced_indicators(data)
    
    trades_details = {
        'long_trades': {'win': 0, 'loss': 0, 'total': 0},
        'short_trades': {'win': 0, 'loss': 0, 'total': 0},
        'all_trades': {'win': 0, 'loss': 0, 'total': 0}
    }
    
    positions = []
    current_position = None
    symbol_weight = SYMBOL_WEIGHTS.get(symbol, 1.0)
    
    for i in range(5, len(data)):
        prev, curr = data.iloc[i-1], data.iloc[i]
        
        # ✅ شروط شراء محسنة وموسعة
        buy_conditions = [
            (curr['sma10'] > curr['sma50']),
            (curr['sma10'] > curr['sma20']),
            (40 <= curr['rsi'] <= 65),  # نطاق RSI أكثر مرونة
            (curr['momentum'] > 0),     # زخم إيجابي
            (curr['volume_ratio'] > 0.8),
            (curr['close'] > curr['sma20'])
        ]
        
        # ✅ شروط بيع محسنة - أكثر صرامة
        sell_conditions = [
            (curr['sma10'] < curr['sma50']),
            (curr['sma10'] < curr['sma20']), 
            (35 <= curr['rsi'] <= 60),  # نطاق أضيق للبيع
            (curr['momentum'] < -0.005), # زخم سلبي واضح
            (curr['volume_ratio'] > 1.0), # شرط حجم أكثر صرامة
            (curr['close'] < curr['sma20'])
        ]
        
        buy_score = sum(buy_conditions)
        sell_score = sum(sell_conditions)
        
        # تطبيق التحيز للشراء وتخصيص الأوزان
        adjusted_buy_score = buy_score * BUY_BIAS * symbol_weight
        adjusted_sell_score = sell_score * SELL_BIAS * symbol_weight
        
        buy_signal = adjusted_buy_score >= 3.5  # عتبة أقل للشراء
        sell_signal = adjusted_sell_score >= 4.0  # عتبة أعلى للبيع
        
        # تطبيق الرافعة بناءً على نوع الصفقة والوزن
        if buy_signal:
            leverage = 2.5 * symbol_weight  # رافعة أعلى للشراء
        else:
            leverage = 1.8 * symbol_weight  # رافعة أقل للبيع
        
        # إدارة الصفقات
        if buy_signal and (not current_position or current_position['side'] == 'SHORT'):
            if current_position and current_position['side'] == 'SHORT':
                pnl = (current_position['price'] - curr['open']) / current_position['price'] * leverage
                positions.append(pnl)
                
                trades_details['short_trades']['total'] += 1
                trades_details['all_trades']['total'] += 1
                if pnl > 0:
                    trades_details['short_trades']['win'] += 1
                    trades_details['all_trades']['win'] += 1
                else:
                    trades_details['short_trades']['loss'] += 1
                    trades_details['all_trades']['loss'] += 1
            
            current_position = {'side': 'LONG', 'price': curr['open'], 'atr': curr['atr']}
        
        elif sell_signal and (not current_position or current_position['side'] == 'LONG'):
            if current_position and current_position['side'] == 'LONG':
                pnl = (curr['open'] - current_position['price']) / current_position['price'] * leverage
                positions.append(pnl)
                
                trades_details['long_trades']['total'] += 1
                trades_details['all_trades']['total'] += 1
                if pnl > 0:
                    trades_details['long_trades']['win'] += 1
                    trades_details['all_trades']['win'] += 1
                else:
                    trades_details['long_trades']['loss'] += 1
                    trades_details['all_trades']['loss'] += 1
            
            current_position = {'side': 'SHORT', 'price': curr['open'], 'atr': curr['atr']}
        
        # إدارة المخاطر المحسنة
        if current_position:
            entry = current_position['price']
            atr = current_position['atr']
            
            if current_position['side'] == 'LONG':
                # وقف خسارة وجني أرباح أكثر عدوانية للشراء
                sl = entry - (atr * 0.8)   # وقف خسارة أقرب
                tp = entry + (atr * 2.0)   # جني أرباح أبعد
                
                if curr['low'] <= sl:
                    pnl = -0.015 * symbol_weight
                    positions.append(pnl)
                    trades_details['long_trades']['loss'] += 1
                    trades_details['all_trades']['loss'] += 1
                    trades_details['long_trades']['total'] += 1
                    trades_details['all_trades']['total'] += 1
                    current_position = None
                elif curr['high'] >= tp:
                    pnl = 0.04 * symbol_weight
                    positions.append(pnl)
                    trades_details['long_trades']['win'] += 1
                    trades_details['all_trades']['win'] += 1
                    trades_details['long_trades']['total'] += 1
                    trades_details['all_trades']['total'] += 1
                    current_position = None
                    
            else:  # SHORT
                # إدارة أكثر تحفظاً للبيع
                sl = entry + (atr * 1.2)   # وقف خسارة أبعد
                tp = entry - (atr * 1.2)   # جني أرباح أقرب
                
                if curr['high'] >= sl:
                    pnl = -0.01 * symbol_weight
                    positions.append(pnl)
                    trades_details['short_trades']['loss'] += 1
                    trades_details['all_trades']['loss'] += 1
                    trades_details['short_trades']['total'] += 1
                    trades_details['all_trades']['total'] += 1
                    current_position = None
                elif curr['low'] <= tp:
                    pnl = 0.025 * symbol_weight
                    positions.append(pnl)
                    trades_details['short_trades']['win'] += 1
                    trades_details['all_trades']['win'] += 1
                    trades_details['short_trades']['total'] += 1
                    trades_details['all_trades']['total'] += 1
                    current_position = None
    
    # النتائج
    if positions:
        total_return = (np.prod([1 + p for p in positions]) - 1) * 100
        win_rate = (trades_details['all_trades']['win'] / trades_details['all_trades']['total'] * 100) if trades_details['all_trades']['total'] > 0 else 0
        
        # تحليل الأداء التفصيلي
        winning_trades = [p for p in positions if p > 0]
        losing_trades = [p for p in positions if p < 0]
        avg_win = np.mean(winning_trades) * 100 if winning_trades else 0
        avg_loss = np.abs(np.mean(losing_trades)) * 100 if losing_trades else 0
        risk_reward = avg_win / avg_loss if avg_loss > 0 else 0
    else:
        total_return, win_rate, avg_win, avg_loss, risk_reward = 0, 0, 0, 0, 0
    
    return {
        'details': trades_details,
        'trades_count': len(positions),
        'total_return': round(total_return, 2),
        'win_rate': round(win_rate, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'risk_reward': round(risk_reward, 2),
        'interval': interval
    }

# التشغيل الرئيسي مع جميع الفترات
print("جاري اختبار الاستراتيجية المحسنة مع جميع الفترات...\n")
print("=" * 70)

all_results = {}

for symbol in symbols:
    all_results[symbol] = {}
    print(f"\n🎯 تحليل {symbol} (الوزن: {SYMBOL_WEIGHTS[symbol]}):")
    print("-" * 50)
    
    for interval in intervals:
        time.sleep(1.5)  # تجنب حظر API
        
        result = enhanced_backtest(symbol, interval)
        if result:
            all_results[symbol][interval] = result
            
            print(f"\n⏰ الفترة {interval}:")
            print(f"   📊 الصفقات: {result['trades_count']} | العائد: {result['total_return']}%")
            print(f"   ✅ نسبة الربح: {result['win_rate']}%")
            print(f"   📈 متوسط الربح: {result['avg_win']}% | 📉 متوسط الخسارة: {result['avg_loss']}%")
            print(f"   ⚖️ نسبة المكافأة:المخاطرة: 1 : {result['risk_reward']}")
            
            # تفاصيل الصفقات
            details = result['details']
            long_win_rate = (details['long_trades']['win'] / details['long_trades']['total'] * 100) if details['long_trades']['total'] > 0 else 0
            short_win_rate = (details['short_trades']['win'] / details['short_trades']['total'] * 100) if details['short_trades']['total'] > 0 else 0
            
            print(f"   📈 شراء: {details['long_trades']['total']} (ربح: {long_win_rate:.1f}%)")
            print(f"   📉 بيع: {details['short_trades']['total']} (ربح: {short_win_rate:.1f}%)")

# الملخص النهائي
print("\n" + "=" * 70)
print("🎯 الملخص النهائي للاستراتيجية المحسنة")
print("=" * 70)

# مقارنة أفضل فترة لكل عملة
print("\n📈 أفضل أداء لكل عملة:")
print("-" * 50)

for symbol in symbols:
    best_interval = None
    best_return = -999
    
    for interval in intervals:
        if symbol in all_results and interval in all_results[symbol]:
            ret = all_results[symbol][interval]['total_return']
            if ret > best_return:
                best_return = ret
                best_interval = interval
    
    if best_interval:
        best_result = all_results[symbol][best_interval]
        print(f"   {symbol}: الفترة {best_interval} - عائد {best_return}% - صفقات {best_result['trades_count']}")

# إحصائيات عامة
total_trades_all = 0
total_return_all = 0
count = 0

for symbol in symbols:
    for interval in intervals:
        if symbol in all_results and interval in all_results[symbol]:
            total_trades_all += all_results[symbol][interval]['trades_count']
            total_return_all += all_results[symbol][interval]['total_return']
            count += 1

avg_return = total_return_all / count if count > 0 else 0

print(f"\n📊 الإجمالي العام:")
print(f"   إجمالي الصفقات عبر جميع الفترات: {total_trades_all}")
print(f"   متوسط العائد: {avg_return:.1f}%")
print(f"   التركيز على الشراء: {BUY_BIAS}x")
print(f"   تخصيص الأوزان: {SYMBOL_WEIGHTS}")
