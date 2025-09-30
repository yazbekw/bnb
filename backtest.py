import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
import pytz
import time

# الإعدادات النهائية
symbols = ["LINKUSDT", "SOLUSDT", "ETHUSDT", "BNBUSDT"]
optimal_intervals = ['30m', '1h']

FINAL_SYMBOL_WEIGHTS = {
    'LINKUSDT': 1.4,
    'SOLUSDT': 1.2,
    'ETHUSDT': 1.0,
    'BNBUSDT': 0.7
}

def get_data(symbol, interval, days=45):
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

def enhanced_backtest(symbol, interval):
    data = get_data(symbol, interval)
    if data is None or len(data) < 100:
        return None
    
    data = calculate_indicators(data)
    
    # تصحيح: تخزين تفاصيل الصفقات بشكل صحيح
    trades_details = {
        'long_trades': {'win': 0, 'loss': 0, 'total': 0},
        'short_trades': {'win': 0, 'loss': 0, 'total': 0},
        'all_trades': {'win': 0, 'loss': 0, 'total': 0}
    }
    
    positions = []
    trade_returns = []  # تخزين عوائد الصفقات بشكل منفصل
    current_position = None
    symbol_weight = FINAL_SYMBOL_WEIGHTS.get(symbol, 1.0)
    
    for i in range(5, len(data)):
        prev, curr = data.iloc[i-1], data.iloc[i]
        
        # شروط التداول
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
        
        buy_score = sum(buy_conditions)
        sell_score = sum(sell_conditions)
        
        buy_signal = buy_score >= 3
        sell_signal = sell_score >= 3
        
        leverage = 2.0 * symbol_weight
        
        # إدارة الصفقات - التصحيح هنا
        if buy_signal:
            if current_position and current_position['side'] == 'SHORT':
                # إغلاق صفقة بيع
                exit_price = curr['open']
                pnl = (current_position['price'] - exit_price) / current_position['price'] * leverage
                positions.append(pnl)
                trade_returns.append(pnl)
                
                # تحديث الإحصائيات - التصحيح
                trades_details['short_trades']['total'] += 1
                trades_details['all_trades']['total'] += 1
                if pnl > 0:
                    trades_details['short_trades']['win'] += 1
                    trades_details['all_trades']['win'] += 1
                else:
                    trades_details['short_trades']['loss'] += 1
                    trades_details['all_trades']['loss'] += 1
                
                current_position = None
            
            if not current_position:
                current_position = {
                    'side': 'LONG', 
                    'price': curr['open'], 
                    'atr': curr['atr'],
                    'entry_index': i
                }
        
        elif sell_signal:
            if current_position and current_position['side'] == 'LONG':
                # إغلاق صفقة شراء
                exit_price = curr['open']
                pnl = (exit_price - current_position['price']) / current_position['price'] * leverage
                positions.append(pnl)
                trade_returns.append(pnl)
                
                # تحديث الإحصائيات - التصحيح
                trades_details['long_trades']['total'] += 1
                trades_details['all_trades']['total'] += 1
                if pnl > 0:
                    trades_details['long_trades']['win'] += 1
                    trades_details['all_trades']['win'] += 1
                else:
                    trades_details['long_trades']['loss'] += 1
                    trades_details['all_trades']['loss'] += 1
                
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
            entry = current_position['price']
            atr = current_position['atr']
            exit_price = None
            pnl = 0
            
            if current_position['side'] == 'LONG':
                sl = entry - (atr * 1.0)
                tp = entry + (atr * 2.0)
                
                if curr['low'] <= sl:
                    exit_price = sl
                    pnl = (exit_price - entry) / entry * leverage
                elif curr['high'] >= tp:
                    exit_price = tp
                    pnl = (exit_price - entry) / entry * leverage
                elif (i - current_position['entry_index']) > 20:  # خروج بعد 20 شمعة
                    exit_price = curr['close']
                    pnl = (exit_price - entry) / entry * leverage
            
            else:  # SHORT
                sl = entry + (atr * 1.0)
                tp = entry - (atr * 2.0)
                
                if curr['high'] >= sl:
                    exit_price = sl
                    pnl = (entry - exit_price) / entry * leverage
                elif curr['low'] <= tp:
                    exit_price = tp
                    pnl = (entry - exit_price) / entry * leverage
                elif (i - current_position['entry_index']) > 20:
                    exit_price = curr['close']
                    pnl = (entry - exit_price) / entry * leverage
            
            if exit_price is not None:
                positions.append(pnl)
                trade_returns.append(pnl)
                
                # تحديث الإحصائيات للمراكز المغلقة بإدارة المخاطر
                if current_position['side'] == 'LONG':
                    trades_details['long_trades']['total'] += 1
                    trades_details['all_trades']['total'] += 1
                    if pnl > 0:
                        trades_details['long_trades']['win'] += 1
                        trades_details['all_trades']['win'] += 1
                    else:
                        trades_details['long_trades']['loss'] += 1
                        trades_details['all_trades']['loss'] += 1
                else:
                    trades_details['short_trades']['total'] += 1
                    trades_details['all_trades']['total'] += 1
                    if pnl > 0:
                        trades_details['short_trades']['win'] += 1
                        trades_details['all_trades']['win'] += 1
                    else:
                        trades_details['short_trades']['loss'] += 1
                        trades_details['all_trades']['loss'] += 1
                
                current_position = None
    
    # إغلاق المركز المتبقي في النهاية
    if current_position:
        exit_price = data.iloc[-1]['close']
        entry = current_position['price']
        leverage = 2.0 * symbol_weight
        
        if current_position['side'] == 'LONG':
            pnl = (exit_price - entry) / entry * leverage
        else:
            pnl = (entry - exit_price) / entry * leverage
        
        positions.append(pnl)
        trade_returns.append(pnl)
        
        if current_position['side'] == 'LONG':
            trades_details['long_trades']['total'] += 1
            trades_details['all_trades']['total'] += 1
            if pnl > 0:
                trades_details['long_trades']['win'] += 1
                trades_details['all_trades']['win'] += 1
            else:
                trades_details['long_trades']['loss'] += 1
                trades_details['all_trades']['loss'] += 1
        else:
            trades_details['short_trades']['total'] += 1
            trades_details['all_trades']['total'] += 1
            if pnl > 0:
                trades_details['short_trades']['win'] += 1
                trades_details['all_trades']['win'] += 1
            else:
                trades_details['short_trades']['loss'] += 1
                trades_details['all_trades']['loss'] += 1
    
    # حساب النتائج النهائية - التصحيح
    if trade_returns:
        total_return = (np.prod([1 + p for p in trade_returns]) - 1) * 100
        if trades_details['all_trades']['total'] > 0:
            win_rate = (trades_details['all_trades']['win'] / trades_details['all_trades']['total']) * 100
        else:
            win_rate = 0
        
        # إحصائيات إضافية
        winning_trades = [p for p in trade_returns if p > 0]
        losing_trades = [p for p in trade_returns if p < 0]
        avg_win = np.mean(winning_trades) * 100 if winning_trades else 0
        avg_loss = np.abs(np.mean(losing_trades)) * 100 if losing_trades else 0
        profit_factor = sum(winning_trades) / abs(sum(losing_trades)) if losing_trades else float('inf')
    else:
        total_return, win_rate, avg_win, avg_loss, profit_factor = 0, 0, 0, 0, 0
    
    return {
        'details': trades_details,
        'trades_count': len(trade_returns),
        'total_return': round(total_return, 2),
        'win_rate': round(win_rate, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'profit_factor': round(profit_factor, 2),
        'symbol_weight': symbol_weight
    }

# التشغيل الرئيسي مع تحليل مفصل
print("🚀 التشغيل النهائي للاستراتيجية المثبتة - الإصدار المصحح")
print("=" * 60)

all_results = {}

for symbol in symbols:
    all_results[symbol] = {}
    print(f"\n🎯 {symbol} (الوزن: {FINAL_SYMBOL_WEIGHTS[symbol]}):")
    print("-" * 50)
    
    for interval in optimal_intervals:
        time.sleep(1.5)
        result = enhanced_backtest(symbol, interval)
        if result:
            all_results[symbol][interval] = result
            
            details = result['details']
            long_win_rate = (details['long_trades']['win'] / details['long_trades']['total'] * 100) if details['long_trades']['total'] > 0 else 0
            short_win_rate = (details['short_trades']['win'] / details['short_trades']['total'] * 100) if details['short_trades']['total'] > 0 else 0
            
            print(f"⏰ {interval}:")
            print(f"   📊 الصفقات: {result['trades_count']} | العائد: {result['total_return']}%")
            print(f"   ✅ نسبة الربح الإجمالية: {result['win_rate']}%")
            print(f"   📈 متوسط الربح: {result['avg_win']}% | 📉 متوسط الخسارة: {result['avg_loss']}%")
            print(f"   💰 عامل الربحية: {result['profit_factor']}")
            print(f"   🔍 التفاصيل:")
            print(f"      📈 شراء: {details['long_trades']['total']} (ربح: {long_win_rate:.1f}%)")
            print(f"      📉 بيع: {details['short_trades']['total']} (ربح: {short_win_rate:.1f}%)")

# الملخص النهائي
print("\n" + "=" * 60)
print("📈 الملخص النهائي والمقارنة")
print("=" * 60)

total_trades = 0
total_return = 0
count = 0

for symbol in symbols:
    best_interval = None
    best_return = -99999
    
    for interval in optimal_intervals:
        if symbol in all_results and interval in all_results[symbol]:
            result = all_results[symbol][interval]
            total_trades += result['trades_count']
            total_return += result['total_return']
            count += 1
            
            if result['total_return'] > best_return:
                best_return = result['total_return']
                best_interval = interval
    
    if best_interval:
        print(f"🏆 {symbol}: أفضل أداء في {best_interval} - عائد {best_return}%")

if count > 0:
    avg_return = total_return / count
    print(f"\n📊 الإجمالي:")
    print(f"   إجمالي الصفقات: {total_trades}")
    print(f"   متوسط العائد: {avg_return:.1f}%")
