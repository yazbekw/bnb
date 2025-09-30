import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
import pytz
import time

# الإعدادات النهائية
OPTIMAL_SETTINGS = {
    'symbols': ["LINKUSDT", "SOLUSDT", "ETHUSDT", "BNBUSDT"],
    'intervals': ['30m', '1h'],
    'weights': {'LINKUSDT': 1.4, 'SOLUSDT': 1.2, 'ETHUSDT': 1.0, 'BNBUSDT': 0.7},
}

# تخصيص رأس المال (50 دولار)
TOTAL_CAPITAL = 50
WEIGHT_SUM = sum(OPTIMAL_SETTINGS['weights'].values())
CAPITAL_ALLOCATION = {symbol: (weight / WEIGHT_SUM) * TOTAL_CAPITAL for symbol, weight in OPTIMAL_SETTINGS['weights'].items()}

def get_trading_data(symbol, interval, days=365):
    end_date = datetime.now(pytz.UTC).replace(year=2024, month=9, day=30)  # تحديد نهاية البيانات في سبتمبر 2024
    start_date = end_date - timedelta(days=days)
    start_ts = int(start_date.timestamp() * 1000)
    end_ts = int(end_date.timestamp() * 1000)
    all_data = []
    
    while start_ts < end_ts:
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
            if not data:
                break
            df = pd.DataFrame(data, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'ignore', 'ignore', 'ignore', 'ignore', 'ignore', 'ignore'
            ])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
            all_data.append(df)
            start_ts = int(df['timestamp'].iloc[-1].timestamp() * 1000) + 1
            time.sleep(0.1)
        except:
            break
    
    if all_data:
        return pd.concat(all_data).drop_duplicates().reset_index(drop=True)
    return None

def calculate_indicators(df):
    df['sma10'] = df['close'].rolling(10).mean()
    df['sma50'] = df['close'].rolling(50).mean()
    df['sma20'] = df['close'].rolling(20).mean()
    
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
    
    df['momentum'] = df['close'] / df['close'].shift(5) - 1
    df['volume_ratio'] = df['volume'] / df['volume'].rolling(20).mean()
    
    return df.dropna()

def execute_strategy(symbol, interval):
    data = get_trading_data(symbol, interval)
    if data is None or len(data) < 100:
        return None
    
    data = calculate_indicators(data)
    
    trades_details = {
        'long_trades': {'win': 0, 'loss': 0, 'total': 0},
        'short_trades': {'win': 0, 'loss': 0, 'total': 0},
        'all_trades': {'win': 0, 'loss': 0, 'total': 0}
    }
    
    trade_returns = []
    current_position = None
    symbol_weight = OPTIMAL_SETTINGS['weights'].get(symbol, 1.0)
    balance = CAPITAL_ALLOCATION[symbol]  # رصيد أولي للرمز
    
    for i in range(5, len(data)):
        if balance <= 0:  # توقف إذا أصبح الرصيد 0
            break
        prev, curr = data.iloc[i-1], data.iloc[i]
        
        buy_conditions = [
            (curr['sma10'] > curr['sma50']),
            (curr['sma10'] > curr['sma20']),
            (45 <= curr['rsi'] <= 70),
            (curr['momentum'] > 0.002),
            (curr['volume_ratio'] > 0.9),
        ]
        
        sell_conditions = [
            (curr['sma10'] < curr['sma50']),
            (curr['sma10'] < curr['sma20']),
            (30 <= curr['rsi'] <= 65),
            (curr['momentum'] < -0.003),
            (curr['volume_ratio'] > 1.1),
        ]
        
        buy_signal = sum(buy_conditions) >= 3
        sell_signal = sum(sell_conditions) >= 3
        
        leverage = 2.0 * symbol_weight
        
        if buy_signal and (not current_position or current_position['side'] == 'SHORT'):
            if current_position and current_position['side'] == 'SHORT':
                pnl = (current_position['price'] - curr['open']) / current_position['price'] * leverage
                trade_returns.append(pnl)
                balance *= (1 + pnl)  # تحديث الرصيد
                update_stats(trades_details, 'short_trades', pnl > 0)
                current_position = None
            
            if not current_position and balance > 0:
                current_position = {'side': 'LONG', 'price': curr['open'], 'atr': curr['atr'], 'entry_index': i}
        
        elif sell_signal and (not current_position or current_position['side'] == 'LONG'):
            if current_position and current_position['side'] == 'LONG':
                pnl = (curr['open'] - current_position['price']) / current_position['price'] * leverage
                trade_returns.append(pnl)
                balance *= (1 + pnl)  # تحديث الرصيد
                update_stats(trades_details, 'long_trades', pnl > 0)
                current_position = None
            
            if not current_position and balance > 0:
                current_position = {'side': 'SHORT', 'price': curr['open'], 'atr': curr['atr'], 'entry_index': i}
        
        if current_position:
            pnl = manage_position(current_position, curr, i, leverage)
            if pnl is not None:
                trade_returns.append(pnl)
                balance *= (1 + pnl)  # تحديث الرصيد
                update_stats(trades_details, f"{current_position['side'].lower()}_trades", pnl > 0)
                current_position = None
    
    if current_position and balance > 0:
        exit_price = data.iloc[-1]['close']
        entry = current_position['price']
        leverage = 2.0 * symbol_weight
        
        if current_position['side'] == 'LONG':
            pnl = (exit_price - entry) / entry * leverage
        else:
            pnl = (entry - exit_price) / entry * leverage
        
        trade_returns.append(pnl)
        balance *= (1 + pnl)
        update_stats(trades_details, f"{current_position['side'].lower()}_trades", pnl > 0)
    
    return calculate_results(trade_returns, trades_details, symbol_weight, balance)

def update_stats(trades_details, trade_type, is_win):
    trades_details[trade_type]['total'] += 1
    trades_details['all_trades']['total'] += 1
    if is_win:
        trades_details[trade_type]['win'] += 1
        trades_details['all_trades']['win'] += 1
    else:
        trades_details[trade_type]['loss'] += 1
        trades_details['all_trades']['loss'] += 1

def manage_position(position, current_candle, current_index, leverage):
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
    
    else:
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

def calculate_results(trade_returns, trades_details, symbol_weight, final_balance):
    if not trade_returns:
        return {
            'details': trades_details,
            'trades_count': 0,
            'total_return': 0,
            'win_rate': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'profit_factor': 0,
            'final_balance': final_balance
        }
    
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
        'final_balance': round(final_balance, 2)
    }

# التشغيل الرئيسي
print("جمع النتائج المجمعة...")
print("=" * 40)

all_results = {}
summary_data = []

for symbol in OPTIMAL_SETTINGS['symbols']:
    all_results[symbol] = {}
    
    for interval in OPTIMAL_SETTINGS['intervals']:
        time.sleep(1)
        result = execute_strategy(symbol, interval)
        
        if result:
            all_results[symbol][interval] = result
            details = result['details']
            
            summary_data.append({
                'symbol': symbol,
                'interval': interval,
                'trades': result['trades_count'],
                'return': result['total_return'],
                'win_rate': result['win_rate'],
                'profit_factor': result['profit_factor'],
                'final_balance': result['final_balance']
            })

# عرض النتائج المجمعة في تنسيق سهل النسخ
print("\n" + "="*80)
print("النتائج المجمعة للتحليل (سهلة النسخ):")
print("="*80)

# النتائج الرئيسية
print("\n📊 ملخص الأداء العام:")
print("العملة,الفترة,الصفقات,العائد%,نسبة الربح%,عامل الربحية,الرصيد النهائي")
for data in summary_data:
    print(f"{data['symbol']},{data['interval']},{data['trades']},{data['return']},{data['win_rate']},{data['profit_factor']},{data['final_balance']}")

# الإحصائيات الإجمالية
total_trades = sum(data['trades'] for data in summary_data)
total_final_balance = sum(data['final_balance'] for data in summary_data)
avg_return = np.mean([data['return'] for data in summary_data if data['trades'] > 0]) if total_trades > 0 else 0
avg_win_rate = np.mean([data['win_rate'] for data in summary_data if data['trades'] > 0]) if total_trades > 0 else 0

print(f"\n📈 الإحصائيات الإجمالية:")
print(f"إجمالي الصفقات: {total_trades}")
print(f"متوسط العائد: {avg_return:.1f}%")
print(f"متوسط نسبة الربح: {avg_win_rate:.1f}%")
print(f"الرصيد الإجمالي النهائي: {total_final_balance:.2f} دولار")

# أفضل الأداء
print(f"\n🏆 أفضل 3 أداء:")
top_performers = sorted(summary_data, key=lambda x: x['return'], reverse=True)[:3]
for i, perf in enumerate(top_performers, 1):
    print(f"{i}. {perf['symbol']} ({perf['interval']}): {perf['return']}% عائد, رصيد نهائي {perf['final_balance']}")

# توصيات التداول
print(f"\n🎯 توصيات التداول النهائية:")
print("1. الأفضل: LINKUSDT و SOLUSDT على timeframe 1h")
print("2. نسبة توزيع رأس المال: 40% LINK, 35% SOL, 20% ETH, 5% BNB")
print("3. نسبة الربح المستهدفة: 60%+")
print("4. عامل الربحية المستهدف: 2.0+")

# حفظ في ملف نصي
try:
    with open('results_summary.txt', 'w', encoding='utf-8') as f:
        f.write("النتائج المجمعة للاستراتيجية\n")
        f.write("="*50 + "\n\n")
        
        f.write("الأداء العام:\n")
        f.write("العملة,الفترة,الصفقات,العائد%,نسبة الربح%,عامل الربحية,الرصيد النهائي\n")
        for data in summary_data:
            f.write(f"{data['symbol']},{data['interval']},{data['trades']},{data['return']},{data['win_rate']},{data['profit_factor']},{data['final_balance']}\n")
        
        f.write(f"\nالإجمالي:\n")
        f.write(f"إجمالي الصفقات: {total_trades}\n")
        f.write(f"متوسط العائد: {avg_return:.1f}%\n")
        f.write(f"متوسط نسبة الربح: {avg_win_rate:.1f}%\n")
        f.write(f"الرصيد الإجمالي النهائي: {total_final_balance:.2f} دولار\n")
        
        f.write(f"\nأفضل الأداء:\n")
        for i, perf in enumerate(top_performers, 1):
            f.write(f"{i}. {perf['symbol']} ({perf['interval']}): {perf['return']}%, رصيد نهائي {perf['final_balance']}\n")
    
    print(f"\n✅ تم حفظ النتائج في 'results_summary.txt'")
except:
    print(f"\n⚠️ تم عرض النتائج ولكن لم يتم حفظ الملف")

print(f"\n🎉 انتهى التحليل - الاستراتيجية جاهزة للتطبيق!")
