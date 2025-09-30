import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
import pytz
import time

# الإعدادات النهائية المثبتة
OPTIMAL_SETTINGS = {
    'symbols': ["LINKUSDT", "SOLUSDT", "ETHUSDT", "BNBUSDT"],
    'intervals': ['30m', '1h'],
    'weights': {'LINKUSDT': 1.4, 'SOLUSDT': 1.2, 'ETHUSDT': 1.0, 'BNBUSDT': 0.7},
    'leverage_base': 2.0,
    'risk_reward_ratio': 2.0
}

def get_trading_data(symbol, interval, days=45):
    """جلب بيانات التداول"""
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
        print(f"Error fetching {symbol}: {e}")
        return None

def calculate_trading_indicators(df):
    """حساب المؤشرات الفنية"""
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
    
    # مؤشرات الزخم
    df['momentum'] = df['close'] / df['close'].shift(5) - 1
    df['volume_ratio'] = df['volume'] / df['volume'].rolling(20).mean()
    
    return df.dropna()

def execute_trading_strategy(symbol, interval):
    """تنفيذ الاستراتيجية النهائية"""
    data = get_trading_data(symbol, interval)
    if data is None or len(data) < 100:
        return None
    
    data = calculate_trading_indicators(data)
    
    trades_details = {
        'long_trades': {'win': 0, 'loss': 0, 'total': 0},
        'short_trades': {'win': 0, 'loss': 0, 'total': 0},
        'all_trades': {'win': 0, 'loss': 0, 'total': 0}
    }
    
    trade_returns = []
    current_position = None
    symbol_weight = OPTIMAL_SETTINGS['weights'].get(symbol, 1.0)
    
    for i in range(5, len(data)):
        prev, curr = data.iloc[i-1], data.iloc[i]
        
        # شروط التداول المثبتة
        buy_conditions = [
            (curr['sma10'] > curr['sma50']),
            (curr['sma10'] > curr['sma20']),
            (45 <= curr['rsi'] <= 70),
            (curr['momentum'] > 0.002),
            (curr['volume_ratio'] > 0.9),
            (curr['close'] > curr['sma20'])
        ]
        
        sell_conditions = [
            (curr['sma10'] < curr['sma50']),
            (curr['sma10'] < curr['sma20']), 
            (30 <= curr['rsi'] <= 65),
            (curr['momentum'] < -0.003),
            (curr['volume_ratio'] > 1.1),
            (curr['close'] < curr['sma20'])
        ]
        
        buy_signal = sum(buy_conditions) >= 3
        sell_signal = sum(sell_conditions) >= 3
        
        leverage = OPTIMAL_SETTINGS['leverage_base'] * symbol_weight
        
        # إدارة الصفقات
        if buy_signal and (not current_position or current_position['side'] == 'SHORT'):
            if current_position and current_position['side'] == 'SHORT':
                pnl = (current_position['price'] - curr['open']) / current_position['price'] * leverage
                trade_returns.append(pnl)
                update_trade_stats(trades_details, 'short_trades', pnl > 0)
                current_position = None
            
            if not current_position:
                current_position = {
                    'side': 'LONG', 
                    'price': curr['open'], 
                    'atr': curr['atr'],
                    'entry_index': i
                }
        
        elif sell_signal and (not current_position or current_position['side'] == 'LONG'):
            if current_position and current_position['side'] == 'LONG':
                pnl = (curr['open'] - current_position['price']) / current_position['price'] * leverage
                trade_returns.append(pnl)
                update_trade_stats(trades_details, 'long_trades', pnl > 0)
                current_position = None
            
            if not current_position:
                current_position = {
                    'side': 'SHORT', 
                    'price': curr['open'], 
                    'atr': curr['atr'],
                    'entry_index': i
                }
        
        # إدارة المخاطر للمراكز المفتوحة
        if current_position:
            pnl = manage_risk(current_position, curr, i, leverage)
            if pnl is not None:
                trade_returns.append(pnl)
                update_trade_stats(trades_details, 
                                 f"{current_position['side'].lower()}_trades", 
                                 pnl > 0)
                current_position = None
    
    # إغلاق المركز المتبقي في النهاية
    if current_position:
        exit_price = data.iloc[-1]['close']
        entry = current_position['price']
        leverage = OPTIMAL_SETTINGS['leverage_base'] * symbol_weight
        
        if current_position['side'] == 'LONG':
            pnl = (exit_price - entry) / entry * leverage
        else:
            pnl = (entry - exit_price) / entry * leverage
        
        trade_returns.append(pnl)
        update_trade_stats(trades_details, 
                         f"{current_position['side'].lower()}_trades", 
                         pnl > 0)
    
    # النتائج النهائية
    return calculate_final_results(trade_returns, trades_details, symbol_weight)

def update_trade_stats(trades_details, trade_type, is_win):
    """تحديث إحصائيات الصفقات"""
    trades_details[trade_type]['total'] += 1
    trades_details['all_trades']['total'] += 1
    if is_win:
        trades_details[trade_type]['win'] += 1
        trades_details['all_trades']['win'] += 1
    else:
        trades_details[trade_type]['loss'] += 1
        trades_details['all_trades']['loss'] += 1

def manage_risk(position, current_candle, current_index, leverage):
    """إدارة المخاطر للمراكز المفتوحة"""
    entry = position['price']
    atr = position['atr']
    exit_price = None
    pnl = 0
    
    if position['side'] == 'LONG':
        sl = entry - (atr * 1.0)
        tp = entry + (atr * 2.0)
        
        if current_candle['low'] <= sl:
            exit_price = sl
            pnl = (exit_price - entry) / entry * leverage
        elif current_candle['high'] >= tp:
            exit_price = tp
            pnl = (exit_price - entry) / entry * leverage
        elif (current_index - position['entry_index']) > 20:
            exit_price = current_candle['close']
            pnl = (exit_price - entry) / entry * leverage
    
    else:  # SHORT
        sl = entry + (atr * 1.0)
        tp = entry - (atr * 2.0)
        
        if current_candle['high'] >= sl:
            exit_price = sl
            pnl = (entry - exit_price) / entry * leverage
        elif current_candle['low'] <= tp:
            exit_price = tp
            pnl = (entry - exit_price) / entry * leverage
        elif (current_index - position['entry_index']) > 20:
            exit_price = current_candle['close']
            pnl = (entry - exit_price) / entry * leverage
    
    return pnl if exit_price is not None else None

def calculate_final_results(trade_returns, trades_details, symbol_weight):
    """حساب النتائج النهائية"""
    if not trade_returns:
        return None
    
    total_return = (np.prod([1 + p for p in trade_returns]) - 1) * 100
    
    if trades_details['all_trades']['total'] > 0:
        win_rate = (trades_details['all_trades']['win'] / trades_details['all_trades']['total']) * 100
    else:
        win_rate = 0
    
    winning_trades = [p for p in trade_returns if p > 0]
    losing_trades = [p for p in trade_returns if p < 0]
    avg_win = np.mean(winning_trades) * 100 if winning_trades else 0
    avg_loss = np.abs(np.mean(losing_trades)) * 100 if losing_trades else 0
    profit_factor = sum(winning_trades) / abs(sum(losing_trades)) if losing_trades else float('inf')
    
    return {
        'details': trades_details,
        'trades_count': len(trade_returns),
        'total_return': round(total_return, 2),
        'win_rate': round(win_rate, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'profit_factor': round(profit_factor, 2),
        'symbol_weight': symbol_weight,
        'trade_returns': trade_returns  # إضافة عوائد الصفقات للتحليل
    }

# التشغيل الرئيسي وجمع النتائج
print("جمع النتائج للتحليل...")
print("=" * 50)

all_results = {}
output_lines = []

for symbol in OPTIMAL_SETTINGS['symbols']:
    all_results[symbol] = {}
    
    for interval in OPTIMAL_SETTINGS['intervals']:
        time.sleep(1.5)
        result = execute_trading_strategy(symbol, interval)
        
        if result:
            all_results[symbol][interval] = result
            
            # جمع البيانات في نص سهل النسخ
            details = result['details']
            long_win_rate = (details['long_trades']['win'] / details['long_trades']['total'] * 100) if details['long_trades']['total'] > 0 else 0
            short_win_rate = (details['short_trades']['win'] / details['short_trades']['total'] * 100) if details['short_trades']['total'] > 0 else 0
            
            line = f"{symbol} | {interval} | {result['trades_count']} | {result['total_return']}% | {result['win_rate']}% | {result['avg_win']}% | {result['avg_loss']}% | {result['profit_factor']} | {long_win_rate:.1f}% | {short_win_rate:.1f}% | {details['long_trades']['total']} | {details['short_trades']['total']}"
            output_lines.append(line)

# كتابة النتائج في تنسيق سهل النسخ
print("\n" + "="*80)
print("النتائج الكاملة للتحليل (يمكن نسخها ولصقها في Excel):")
print("="*80)
print("العملة | الفترة | الصفقات | العائد | نسبة الربح | متوسط ربح | متوسط خسارة | عامل الربحية | نسبة ربح الشراء | نسبة ربح البيع | صفقات شراء | صفقات بيع")
print("-" * 150)

for line in output_lines:
    print(line)

# إحصائيات إضافية للتحليل
print("\n" + "="*80)
print("الإحصائيات الإضافية للتحليل المتقدم:")
print("="*80)

for symbol in OPTIMAL_SETTINGS['symbols']:
    print(f"\n📊 تحليل مفصل لـ {symbol}:")
    for interval in OPTIMAL_SETTINGS['intervals']:
        if symbol in all_results and interval in all_results[symbol]:
            result = all_results[symbol][interval]
            details = result['details']
            
            print(f"\nالفترة {interval}:")
            print(f"   العوائد الفردية: {[round(x*100, 2) for x in result['trade_returns'][:10]]}...")  # أول 10 صفقات فقط
            print(f"   توزيع الصفقات: {details['long_trades']['win']}/{details['long_trades']['loss']} شراء رابحة/خاسرة, {details['short_trades']['win']}/{details['short_trades']['loss']} بيع رابحة/خاسرة")
            
            # حساب نسبة المخاطرة إلى العائد الفعلية
            if result['avg_loss'] > 0:
                actual_rr = result['avg_win'] / result['avg_loss']
                print(f"   نسبة المخاطرة إلى العائد الفعلية: 1 : {actual_rr:.2f}")

# الملخص النهائي في تنسيق سهل النسخ
print("\n" + "="*80)
print("الملخص النهائي:")
print("="*80)

total_trades = 0
total_return_sum = 0
results_count = 0

best_performers = []

for symbol in OPTIMAL_SETTINGS['symbols']:
    best_interval = None
    best_return = -99999
    
    for interval in OPTIMAL_SETTINGS['intervals']:
        if symbol in all_results and interval in all_results[symbol]:
            result = all_results[symbol][interval]
            total_trades += result['trades_count']
            total_return_sum += result['total_return']
            results_count += 1
            
            if result['total_return'] > best_return:
                best_return = result['total_return']
                best_interval = interval
    
    if best_interval:
        best_performers.append(f"{symbol}: {best_interval} - {best_return}%")
        print(f"أفضل أداء لـ {symbol}: {best_interval} - عائد {best_return}%")

if results_count > 0:
    avg_return = total_return_sum / results_count
    print(f"\nالإجمالي:")
    print(f"إجمالي الصفقات: {total_trades}")
    print(f"متوسط العائد: {avg_return:.1f}%")
    print(f"عدد النتائج: {results_count}")

print(f"\nأفضل الأداء: {', '.join(best_performers)}")

# حفظ النتائج في ملف نصي (اختياري)
try:
    with open('trading_results.txt', 'w', encoding='utf-8') as f:
        f.write("النتائج الكاملة للتحليل\n")
        f.write("="*50 + "\n")
        f.write("العملة | الفترة | الصفقات | العائد | نسبة الربح | متوسط ربح | متوسط خسارة | عامل الربحية | نسبة ربح الشراء | نسبة ربح البيع | صفقات شراء | صفقات بيع\n")
        for line in output_lines:
            f.write(line + "\n")
        f.write(f"\nالملخص: إجمالي الصفقات: {total_trades}, متوسط العائد: {avg_return:.1f}%\n")
    print("\n✅ تم حفظ النتائج في ملف 'trading_results.txt'")
except:
    print("\n⚠️ لم يتم حفظ الملف ولكن النتائج معروضة أعلاه")
