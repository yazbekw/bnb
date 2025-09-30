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

def backtest_detailed(symbol):
    data = get_data(symbol)
    if data is None or len(data) < 100:
        return None
    
    data = calculate_indicators(data)
    
    # تخزين تفاصيل الصفقات
    trades_details = {
        'long_trades': {'win': 0, 'loss': 0, 'total': 0},
        'short_trades': {'win': 0, 'loss': 0, 'total': 0},
        'all_trades': {'win': 0, 'loss': 0, 'total': 0}
    }
    
    positions = []
    current_position = None
    
    for i in range(2, len(data)):
        prev, curr = data.iloc[i-1], data.iloc[i]
        
        # إشارات التداول
        buy_signal = (curr['sma10'] > curr['sma50']) and (curr['rsi'] > 45)
        sell_signal = (curr['sma10'] < curr['sma50']) and (curr['rsi'] < 55)
        
        leverage = 2.0
        
        if buy_signal and (not current_position or current_position['side'] == 'SHORT'):
            if current_position and current_position['side'] == 'SHORT':
                # إغلاق صفقة بيع
                pnl = (current_position['price'] - curr['open']) / current_position['price'] * leverage
                positions.append(pnl)
                
                # تسجيل تفاصيل الصفقة القصيرة
                trades_details['short_trades']['total'] += 1
                trades_details['all_trades']['total'] += 1
                if pnl > 0:
                    trades_details['short_trades']['win'] += 1
                    trades_details['all_trades']['win'] += 1
                else:
                    trades_details['short_trades']['loss'] += 1
                    trades_details['all_trades']['loss'] += 1
            
            # فتح صفقة شراء جديدة
            current_position = {'side': 'LONG', 'price': curr['open'], 'atr': curr['atr']}
        
        elif sell_signal and (not current_position or current_position['side'] == 'LONG'):
            if current_position and current_position['side'] == 'LONG':
                # إغلاق صفقة شراء
                pnl = (curr['open'] - current_position['price']) / current_position['price'] * leverage
                positions.append(pnl)
                
                # تسجيل تفاصيل الصفقة الطويلة
                trades_details['long_trades']['total'] += 1
                trades_details['all_trades']['total'] += 1
                if pnl > 0:
                    trades_details['long_trades']['win'] += 1
                    trades_details['all_trades']['win'] += 1
                else:
                    trades_details['long_trades']['loss'] += 1
                    trades_details['all_trades']['loss'] += 1
            
            # فتح صفقة بيع جديدة
            current_position = {'side': 'SHORT', 'price': curr['open'], 'atr': curr['atr']}
        
        # وقف الخسارة وجني الأرباح
        if current_position:
            entry = current_position['price']
            atr = current_position['atr']
            pnl = 0
            
            if current_position['side'] == 'LONG':
                if curr['low'] <= entry - (atr * 1.0):
                    pnl = -0.02
                    positions.append(pnl)
                    
                    trades_details['long_trades']['total'] += 1
                    trades_details['all_trades']['total'] += 1
                    trades_details['long_trades']['loss'] += 1
                    trades_details['all_trades']['loss'] += 1
                    
                    current_position = None
                elif curr['high'] >= entry + (atr * 1.5):
                    pnl = 0.03
                    positions.append(pnl)
                    
                    trades_details['long_trades']['total'] += 1
                    trades_details['all_trades']['total'] += 1
                    trades_details['long_trades']['win'] += 1
                    trades_details['all_trades']['win'] += 1
                    
                    current_position = None
            else:
                if curr['high'] >= entry + (atr * 1.0):
                    pnl = -0.02
                    positions.append(pnl)
                    
                    trades_details['short_trades']['total'] += 1
                    trades_details['all_trades']['total'] += 1
                    trades_details['short_trades']['loss'] += 1
                    trades_details['all_trades']['loss'] += 1
                    
                    current_position = None
                elif curr['low'] <= entry - (atr * 1.5):
                    pnl = 0.03
                    positions.append(pnl)
                    
                    trades_details['short_trades']['total'] += 1
                    trades_details['all_trades']['total'] += 1
                    trades_details['short_trades']['win'] += 1
                    trades_details['all_trades']['win'] += 1
                    
                    current_position = None
    
    # النتائج النهائية
    if positions:
        total_return = (np.prod([1 + p for p in positions]) - 1) * 100
        win_rate = (trades_details['all_trades']['win'] / trades_details['all_trades']['total']) * 100
    else:
        total_return, win_rate = 0, 0
    
    return trades_details, len(positions), round(total_return, 2), round(win_rate, 2)

# التشغيل الرئيسي
print("جاري تحليل تفاصيل الصفقات...\n")
print("=" * 60)

all_trades_summary = {
    'long': {'win': 0, 'loss': 0, 'total': 0},
    'short': {'win': 0, 'loss': 0, 'total': 0},
    'all': {'win': 0, 'loss': 0, 'total': 0}
}

for symbol in symbols:
    time.sleep(1)
    details, trades_count, ret, win_rate = backtest_detailed(symbol)
    
    if details:
        print(f"\n📊 {symbol} - التفاصيل:")
        print(f"   إجمالي الصفقات: {trades_count} | العائد: {ret}% | نسبة الربح: {win_rate}%")
        
        # صفقات الشراء
        long_win_rate = (details['long_trades']['win'] / details['long_trades']['total'] * 100) if details['long_trades']['total'] > 0 else 0
        print(f"   📈 صفقات الشراء: {details['long_trades']['total']}")
        print(f"      ✅ رابحة: {details['long_trades']['win']} | ❌ خاسرة: {details['long_trades']['loss']} | 📊 نسبة: {long_win_rate:.1f}%")
        
        # صفقات البيع
        short_win_rate = (details['short_trades']['win'] / details['short_trades']['total'] * 100) if details['short_trades']['total'] > 0 else 0
        print(f"   📉 صفقات البيع: {details['short_trades']['total']}")
        print(f"      ✅ رابحة: {details['short_trades']['win']} | ❌ خاسرة: {details['short_trades']['loss']} | 📊 نسبة: {short_win_rate:.1f}%")
        
        # تحديث الإجمالي
        all_trades_summary['long']['win'] += details['long_trades']['win']
        all_trades_summary['long']['loss'] += details['long_trades']['loss']
        all_trades_summary['long']['total'] += details['long_trades']['total']
        
        all_trades_summary['short']['win'] += details['short_trades']['win']
        all_trades_summary['short']['loss'] += details['short_trades']['loss']
        all_trades_summary['short']['total'] += details['short_trades']['total']
        
        all_trades_summary['all']['win'] += details['all_trades']['win']
        all_trades_summary['all']['loss'] += details['all_trades']['loss']
        all_trades_summary['all']['total'] += details['all_trades']['total']

# الملخص النهائي
print("\n" + "=" * 60)
print("🎯 الملخص النهائي لجميع العملات:")
print("=" * 60)

total_long_win_rate = (all_trades_summary['long']['win'] / all_trades_summary['long']['total'] * 100) if all_trades_summary['long']['total'] > 0 else 0
total_short_win_rate = (all_trades_summary['short']['win'] / all_trades_summary['short']['total'] * 100) if all_trades_summary['short']['total'] > 0 else 0
total_win_rate = (all_trades_summary['all']['win'] / all_trades_summary['all']['total'] * 100) if all_trades_summary['all']['total'] > 0 else 0

print(f"📈 إجمالي صفقات الشراء: {all_trades_summary['long']['total']}")
print(f"   ✅ رابحة: {all_trades_summary['long']['win']} | ❌ خاسرة: {all_trades_summary['long']['loss']} | 📊 نسبة: {total_long_win_rate:.1f}%")

print(f"📉 إجمالي صفقات البيع: {all_trades_summary['short']['total']}")
print(f"   ✅ رابحة: {all_trades_summary['short']['win']} | ❌ خاسرة: {all_trades_summary['short']['loss']} | 📊 نسبة: {total_short_win_rate:.1f}%")

print(f"\n🎯 الإجمالي العام: {all_trades_summary['all']['total']} صفقة")
print(f"   ✅ رابحة: {all_trades_summary['all']['win']} | ❌ خاسرة: {all_trades_summary['all']['loss']} | 📊 نسبة: {total_win_rate:.1f}%")
