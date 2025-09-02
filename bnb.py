import os
import pandas as pd
import numpy as np
from binance.client import Client
from binance.enums import *
import time
from datetime import datetime, timedelta
import requests
import logging
import warnings
warnings.filterwarnings('ignore')
from dotenv import load_dotenv
from flask import Flask
import threading

# تحميل متغيرات البيئة
load_dotenv()

# إنشاء تطبيق Flask
app = Flask(__name__)

@app.route('/')
def health_check():
    return {'status': 'healthy', 'service': 'bnb-trading-bot', 'timestamp': datetime.now().isoformat()}

@app.route('/status')
def status():
    return {'status': 'running', 'bot': 'BNB Trading Bot', 'time': datetime.now().isoformat()}

def run_flask_app():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

# إعداد logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
    
    def send_message(self, message):
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }
            response = requests.post(url, data=payload, timeout=10)
            if response.status_code != 200:
                logger.error(f"فشل إرسال رسالة Telegram: {response.text}")
        except Exception as e:
            logger.error(f"خطأ في إرسال رسالة Telegram: {e}")

class BNB_Trading_Bot:
    def __init__(self, api_key=None, api_secret=None, telegram_token=None, telegram_chat_id=None):
        self.notifier = None
        
        self.api_key = api_key or os.environ.get('BINANCE_API_KEY')
        self.api_secret = api_secret or os.environ.get('BINANCE_API_SECRET')
        telegram_token = telegram_token or os.environ.get('TELEGRAM_BOT_TOKEN')
        telegram_chat_id = telegram_chat_id or os.environ.get('TELEGRAM_CHAT_ID')
        
        if not self.api_key or not self.api_secret:
            error_msg = "❌ مفاتيح Binance غير موجودة"
            logger.error(error_msg)
            raise ValueError(error_msg)
            
        try:
            self.client = Client(self.api_key, self.api_secret)
            logger.info("✅ تم الاتصال بمنصة Binance الفعلية")
            self.test_connection()
                
        except Exception as e:
            error_msg = f"❌ فشل الاتصال بـ Binance: {e}"
            logger.error(error_msg)
            raise ConnectionError(error_msg)
            
        self.fee_rate = 0.0005
        self.slippage = 0.00015
        self.trades = []
        self.symbol = "BNBUSDT"
        self.base_trade_size = 7
        self.active_trades = {}
        
        # إعدادات إدارة الأوامر
        self.MAX_ALGO_ORDERS = 20  # زيادة الحد لأننا نستخدم أوامر منفصلة
        self.ORDERS_TO_CANCEL = 2
        
        # إعدادات حجم الصفقة المتدرج
        self.MIN_TRADE_SIZE = 3
        self.MAX_TRADE_SIZE = 15
        
        # الحصول على معلومات الرمز لمعرفة الحدود
        self.symbol_info = self.get_symbol_info()
        self.min_notional = float([f['minNotional'] for f in self.symbol_info['filters'] if f['filterType'] == 'NOTIONAL'][0])
        self.step_size = float([f['stepSize'] for f in self.symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'][0])
        self.tick_size = float([f['tickSize'] for f in self.symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER'][0])
        
        if telegram_token and telegram_chat_id:
            self.notifier = TelegramNotifier(telegram_token, telegram_chat_id)
            logger.info("تم تهيئة إشعارات Telegram")
        else:
            logger.warning("مفاتيح Telegram غير موجودة، سيتم تعطيل الإشعارات")
        
        try:
            self.initial_balance = self.get_real_balance()
            success_msg = f"✅ تم تهيئة البوت بنجاح - الرصيد الابتدائي: ${self.initial_balance:.2f}"
            logger.info(success_msg)
            if self.notifier:
                self.notifier.send_message(f"🤖 <b>بدء تشغيل بوت تداول BNB</b>\n\n{success_msg}\nحجم الصفقة: ${self.MIN_TRADE_SIZE}-${self.MAX_TRADE_SIZE}\nالحد الأدنى للقيمة: ${self.min_notional}")
        except Exception as e:
            logger.error(f"خطأ في جلب الرصيد الابتدائي: {e}")
            self.initial_balance = 0

    def get_symbol_info(self):
        """الحصول على معلومات الرمز مرة واحدة عند التهيئة"""
        try:
            info = self.client.get_symbol_info(self.symbol)
            logger.info(f"✅ تم جلب معلومات الرمز: {self.symbol}")
            return info
        except Exception as e:
            logger.error(f"❌ خطأ في جلب معلومات الرمز: {e}")
            return None

    def test_connection(self):
        try:
            server_time = self.client.get_server_time()
            logger.info(f"✅ الاتصال ناجح - وقت الخادم: {server_time['serverTime']}")
            
            account_info = self.client.get_account()
            logger.info("✅ جلب معلومات الحساب ناجح")
            
            public_ip = self.get_public_ip()
            logger.info(f"🌐 IP الخادم: {public_ip}")
            
            print("="*50)
            print("✅ اختبار الاتصال ناجح!")
            print("وضع التشغيل: فعلي")
            print(f"IP الخادم: {public_ip}")
            print(f"حجم الصفقة: ${self.MIN_TRADE_SIZE}-${self.MAX_TRADE_SIZE}")
            print(f"الحد الأدنى للقيمة: ${self.min_notional}")
            print("="*50)
            
            return True
        except Exception as e:
            logger.error(f"❌ فشل الاتصال: {e}")
            return False

    def get_public_ip(self):
        try:
            response = requests.get('https://api.ipify.org?format=json', timeout=10)
            return response.json()['ip']
        except:
            return "غير معروف"
    
    def get_real_balance(self):
        try:
            account = self.client.get_account()
            balances = {asset['asset']: float(asset['free']) + float(asset['locked']) for asset in account['balances']}
            
            prices = self.client.get_all_tickers()
            price_dict = {item['symbol']: float(item['price']) for item in prices}
            
            total_balance = 0
            for asset, balance in balances.items():
                if balance > 0:
                    if asset == 'USDT':
                        total_balance += balance
                    else:
                        symbol = asset + 'USDT'
                        if symbol in price_dict:
                            total_balance += balance * price_dict[symbol]
            
            return total_balance
        except Exception as e:
            error_msg = f"❌ خطأ في جلب الرصيد من المنصة: {e}"
            logger.error(error_msg)
            if self.notifier:
                self.notifier.send_message(error_msg)
            raise
    
    def get_account_balance_details(self):
        try:
            account = self.client.get_account()
            balances = {asset['asset']: {
                'free': float(asset['free']),
                'locked': float(asset['locked']),
                'total': float(asset['free']) + float(asset['locked'])
            } for asset in account['balances'] if float(asset['free']) > 0 or float(asset['locked']) > 0}
            
            ticker = self.client.get_symbol_ticker(symbol=self.symbol)
            bnb_price = float(ticker['price'])
            
            total_balance = self.get_real_balance()
            
            return total_balance, balances, bnb_price
        except Exception as e:
            error_msg = f"❌ خطأ في الحصول على رصيد الحساب: {e}"
            logger.error(error_msg)
            return None, None, None
    
    def send_notification(self, message):
        logger.info(message)
        if self.notifier:
            self.notifier.send_message(message)
    
    def format_quantity(self, quantity):
        """تقريب الكمية حسب stepSize"""
        precision = len(str(self.step_size).split('.')[1].rstrip('0'))
        rounded_quantity = round(quantity - (quantity % self.step_size), precision)
        return max(rounded_quantity, 0)  # التأكد من عدم كونها سالبة
    
    def format_price(self, price):
        """تقريب السعر حسب tickSize"""
        precision = len(str(self.tick_size).split('.')[1].rstrip('0'))
        rounded_price = round(price / self.tick_size) * self.tick_size
        return round(rounded_price, precision)
    
    def check_notional(self, quantity, price):
        """التحقق من الحد الأدنى للقيمة (NOTIONAL)"""
        notional_value = quantity * price
        return notional_value >= self.min_notional
    
    def get_algo_orders_count(self, symbol):
        """الحصول على عدد الأوامر الآلية الحالية"""
        try:
            open_orders = self.client.get_open_orders(symbol=symbol)
            algo_orders = [o for o in open_orders if o['type'] in ['STOP_LOSS_LIMIT', 'TAKE_PROFIT_LIMIT']]
            return len(algo_orders)
        except Exception as e:
            logger.error(f"خطأ في جلب عدد الأوامر الآلية: {e}")
            return 0
    
    def cancel_oldest_algo_orders(self, symbol, num_to_cancel=2):
        """إلغاء أقدم الأوامر الآلية"""
        try:
            open_orders = self.client.get_open_orders(symbol=symbol)
            algo_orders = []
            
            for order in open_orders:
                if order['type'] in ['STOP_LOSS_LIMIT', 'TAKE_PROFIT_LIMIT']:
                    order_time = datetime.fromtimestamp(order['time'] / 1000)
                    algo_orders.append({
                        'orderId': order['orderId'],
                        'time': order_time,
                        'type': order['type'],
                        'price': order.get('price', 'N/A')
                    })
            
            algo_orders.sort(key=lambda x: x['time'])
            
            cancelled_count = 0
            cancelled_info = []
            
            for i in range(min(num_to_cancel, len(algo_orders))):
                try:
                    self.client.cancel_order(
                        symbol=symbol,
                        orderId=algo_orders[i]['orderId']
                    )
                    cancelled_count += 1
                    cancelled_info.append(f"{algo_orders[i]['type']} - {algo_orders[i]['price']}")
                    logger.info(f"تم إلغاء الأمر القديم: {algo_orders[i]['orderId']}")
                except Exception as e:
                    logger.error(f"خطأ في إلغاء الأمر {algo_orders[i]['orderId']}: {e}")
            
            return cancelled_count, cancelled_info
            
        except Exception as e:
            logger.error(f"خطأ في إلغاء الأوامر القديمة: {e}")
            return 0, []
    
    def manage_order_space(self, symbol):
        """إدارة مساحة الأوامر"""
        try:
            current_orders = self.get_algo_orders_count(symbol)
            
            if current_orders >= self.MAX_ALGO_ORDERS:
                self.send_notification(f"⚠️ الحد الأقصى للأوامر ممتلئ ({current_orders}/{self.MAX_ALGO_ORDERS})")
                
                cancelled_count, cancelled_info = self.cancel_oldest_algo_orders(symbol, self.ORDERS_TO_CANCEL)
                
                if cancelled_count > 0:
                    msg = f"♻️ <b>تم إلغاء {cancelled_count} أوامر قديمة</b>\n\n"
                    for info in cancelled_info:
                        msg += f"• {info}\n"
                    self.send_notification(msg)
                    
                    current_orders = self.get_algo_orders_count(symbol)
                    return current_orders < self.MAX_ALGO_ORDERS
                else:
                    self.send_notification("❌ فشل إلغاء الأوامر القديمة")
                    return False
            else:
                return True
                
        except Exception as e:
            logger.error(f"خطأ في إدارة مساحة الأوامر: {e}")
            return False
    
    def calculate_signal_strength(self, data, signal_type='buy'):
        """تقييم قوة الإشارة من 0 إلى 100%"""
        latest = data.iloc[-1]
        score = 0
        
        if signal_type == 'buy':
            if 25 <= latest['rsi'] <= 30: score += 30
            elif 30 < latest['rsi'] <= 35: score += 15
            
            macd_strength = (latest['macd'] - latest['macd_sig']) / latest['macd_sig']
            if macd_strength > 0.12: score += 35
            elif macd_strength > 0.08: score += 20
            elif macd_strength > 0.05: score += 10
            
            if latest['ema9'] > latest['ema20'] > latest['ema50'] > latest['ema200']: score += 25
            elif latest['ema9'] > latest['ema20'] > latest['ema50']: score += 15
            elif latest['ema9'] > latest['ema20']: score += 5
            
            if latest['vol_ratio'] > 2.5: score += 10
            elif latest['vol_ratio'] > 1.5: score += 5
        
        else:
            if latest['rsi'] > 75: score += 35
            elif latest['rsi'] > 70: score += 20
            elif latest['rsi'] < 25: score += 30
            
            macd_strength = (latest['macd_sig'] - latest['macd']) / latest['macd']
            if macd_strength > 0.1: score += 30
            elif macd_strength > 0.06: score += 15
            
            if latest['ema9'] < latest['ema20'] < latest['ema50']: score += 25
            elif latest['ema9'] < latest['ema20']: score += 10
            
            if latest['vol_ratio'] > 2.0: score += 10
        
        return min(score, 100)
    
    def calculate_dynamic_size(self, signal_strength):
        """حساب حجم الصفقة المتدرج من 3$ إلى 15$"""
        if signal_strength >= 90:
            return self.MAX_TRADE_SIZE
        elif signal_strength >= 70:
            return self.MAX_TRADE_SIZE * 0.6
        elif signal_strength >= 50:
            return self.MAX_TRADE_SIZE * 0.4
        else:
            return self.MIN_TRADE_SIZE
    
    def get_strength_level(self, strength):
        """الحصول على اسم مستوى القوة"""
        if strength >= 90: return "4 🟢 (قوي جداً)"
        elif strength >= 70: return "3 🟡 (قوي)"
        elif strength >= 50: return "2 🔵 (متوسط)"
        else: return "1 ⚪ (ضعيف)"
    
    def calculate_rsi(self, data, period=14):
        delta = data.diff()
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        
        avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi.fillna(50)
    
    def calculate_ma(self, data, period):
        return data.rolling(window=period).mean()
    
    def calculate_bollinger_bands(self, data, period=20, std_dev=2):
        sma = data.rolling(window=period).mean()
        std = data.rolling(window=period).std()
        upper_band = sma + (std * std_dev)
        lower_band = sma - (std * std_dev)
        return upper_band, sma, lower_band
    
    def calculate_atr(self, df, period=14):
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr1 = (high - low).abs()
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    
    def calculate_macd(self, series, fast=12, slow=26, signal=9):
        ema_fast = series.ewm(span=fast, adjust=False).mean()
        ema_slow = series.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        sig = macd_line.ewm(span=signal, adjust=False).mean()
        hist = macd_line - sig
        return macd_line, sig, hist
    
    def get_historical_data(self, interval=Client.KLINE_INTERVAL_15MINUTE, lookback='2000 hour ago UTC'):
        try:
            klines = self.client.get_historical_klines(self.symbol, interval, lookback)
            if not klines:
                error_msg = f"⚠️ لا توجد بيانات لـ {self.symbol}"
                self.send_notification(error_msg)
                return None
                
            data = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 
                                                'close_time', 'quote_asset_volume', 'number_of_trades', 
                                                'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'])
            data['timestamp'] = pd.to_datetime(data['timestamp'], unit='ms')
            for col in ['open', 'high', 'low', 'close', 'volume']:
                data[col] = pd.to_numeric(data[col], errors='coerce')
            
            data = data.dropna()
            
            if len(data) < 100:
                error_msg = f"⚠️ بيانات غير كافية لـ {self.symbol}: {len(data)} صفوف فقط"
                self.send_notification(error_msg)
                return None
            
            data['rsi'] = self.calculate_rsi(data['close'])
            data['atr'] = self.calculate_atr(data)
            data['ema200'] = data['close'].ewm(span=200, adjust=False).mean()
            data['ema50'] = data['close'].ewm(span=50, adjust=False).mean()
            data['ema20'] = data['close'].ewm(span=20, adjust=False).mean()
            data['ema9'] = data['close'].ewm(span=9, adjust=False).mean()
            data['vol_ma20'] = data['volume'].rolling(20).mean()
            data['vol_ratio'] = data['volume'] / data['vol_ma20']
            
            macd_line, macd_sig, macd_hist = self.calculate_macd(data['close'])
            data['macd'] = macd_line
            data['macd_sig'] = macd_sig
            data['macd_hist'] = macd_hist
            
            data['atr_ma20'] = data['atr'].rolling(20).mean()
            data['trend_strong'] = (data['ema9'] > data['ema20']) & (data['ema20'] > data['ema50']) & (data['close'] > data['ema200'])
            data['price_above_ema50'] = data['close'] > data['ema50']
            
            return data
        except Exception as e:
            error_msg = f"❌ خطأ في جلب البيانات: {e}"
            self.send_notification(error_msg)
            return None
    
    def bnb_strategy(self, data):
        if data is None or len(data) < 100:
            return False, False, 0, 0
        
        latest = data.iloc[-1]
        prev = data.iloc[-2]
    
        buy_condition_1 = 30 <= latest['rsi'] <= 35
        buy_condition_2 = latest['macd'] > latest['macd_sig'] and latest['macd_hist'] > 0.05
        buy_condition_3 = latest['close'] > latest['ema20'] and latest['ema9'] > latest['ema20']
    
        sell_condition_1 = latest['rsi'] > 70 or latest['rsi'] < 25
        sell_condition_2 = latest['macd_hist'] < -0.05 or latest['close'] < latest['ema9']
    
        buy_conditions = [buy_condition_1, buy_condition_2, buy_condition_3]
        buy_signal = sum(buy_conditions) >= 2
    
        sell_signal = any([sell_condition_1, sell_condition_2])
    
        atr_val = latest['atr']
        stop_loss = latest['close'] - (2.5 * atr_val)
        take_profit = latest['close'] + (3.5 * atr_val)
    
        return buy_signal, sell_signal, stop_loss, take_profit
    
    def check_balance_before_trade(self, required_usdt):
        """التحقق من الرصيد قبل التنفيذ"""
        try:
            total_balance, balances, _ = self.get_account_balance_details()
            usdt_balance = balances.get('USDT', {}).get('free', 0)
            
            if usdt_balance >= required_usdt:
                return True, usdt_balance
            else:
                return False, usdt_balance
        except Exception as e:
            logger.error(f"خطأ في التحقق من الرصيد: {e}")
            return False, 0
    
    def execute_real_trade(self, signal_type, current_price, stop_loss, take_profit, trade_size):
        """تنفيذ صفقة حقيقية"""
        try:
            can_trade, usdt_balance = self.check_balance_before_trade(trade_size)
            
            if not can_trade:
                self.send_notification(f"⚠️ رصيد غير كافي للصفقة. المطلوب: ${trade_size:.2f}، المتاح: ${usdt_balance:.2f}")
                return False
            
            if signal_type == 'buy':
                quantity = trade_size / current_price
                quantity = self.format_quantity(quantity)
                
                if quantity <= 0:
                    self.send_notification("⚠️ الكمية غير صالحة للشراء")
                    return False
                
                # التحقق من NOTIONAL قبل الشراء
                if not self.check_notional(quantity, current_price):
                    self.send_notification(f"⚠️ القيمة أقل من الحد الأدنى (${self.min_notional})")
                    return False
                
                order = self.client.order_market_buy(
                    symbol=self.symbol,
                    quantity=quantity
                )
                
                formatted_stop_loss = self.format_price(stop_loss)
                formatted_take_profit = self.format_price(take_profit)
                formatted_current = self.format_price(current_price)
                
                if not self.manage_order_space(self.symbol):
                    self.send_notification("❌ لا يمكن وضع أوامر الوقف - المساحة ممتلئة")
                    try:
                        self.client.order_market_sell(
                            symbol=self.symbol,
                            quantity=quantity
                        )
                        self.send_notification("⚠️ تم البيع فورياً بسبب عدم إمكانية وضع وقف")
                    except Exception as sell_error:
                        self.send_notification(f"❌ فشل البيع أيضاً: {sell_error}")
                    return False
                
                # استخدام أوامر منفصلة بدل OCO
                try:
                    # أمر وقف الخسارة
                    stop_order = self.client.order_stop_loss_limit(
                        symbol=self.symbol,
                        quantity=quantity,
                        stopPrice=formatted_stop_loss,
                        price=formatted_stop_loss,
                        timeInForce='GTC'
                    )
                    
                    # أمر جني الأرباح
                    profit_order = self.client.order_limit_sell(
                        symbol=self.symbol,
                        quantity=quantity,
                        price=formatted_take_profit,
                        timeInForce='GTC'
                    )
                    
                    msg = f"📊 <b>تم وضع أوامر منفصلة</b>\n\n"
                    msg += f"وقف الخسارة: ${formatted_stop_loss:.4f}\n"
                    msg += f"جني الأرباح: ${formatted_take_profit:.4f}\n"
                    msg += f"الكمية: {quantity:.4f} BNB"
                    self.send_notification(msg)
                    
                except Exception as e:
                    error_msg = f"⚠️ فشل وضع أوامر الوقف: {e}"
                    self.send_notification(error_msg)
                    
                    # حاول وضع أمر وقف فقط
                    try:
                        stop_order = self.client.order_stop_loss_limit(
                            symbol=self.symbol,
                            quantity=quantity,
                            stopPrice=formatted_stop_loss,
                            price=formatted_stop_loss,
                            timeInForce='GTC'
                        )
                        self.send_notification("✅ تم وضع وقف الخسارة فقط")
                    except:
                        self.send_notification("❌ فشل وضع أي أوامر وقف")
                
                return True
                
            elif signal_type == 'sell':
                total_balance, balances, _ = self.get_account_balance_details()
                bnb_balance = balances.get('BNB', {}).get('free', 0)
                
                if bnb_balance <= 0.001:
                    self.send_notification("⚠️ رصيد BNB غير كافي للبيع")
                    return False
                
                quantity = self.format_quantity(bnb_balance)
                
                if quantity <= 0:
                    self.send_notification("⚠️ الكمية غير صالحة للبيع")
                    return False
                
                # التحقق من NOTIONAL قبل البيع
                if not self.check_notional(quantity, current_price):
                    self.send_notification(f"⚠️ القيمة أقل من الحد الأدنى (${self.min_notional})")
                    return False
                
                order = self.client.order_market_sell(
                    symbol=self.symbol,
                    quantity=quantity
                )
                
                return True
                
        except Exception as e:
            error_msg = f"❌ خطأ في تنفيذ الصفقة: {e}"
            self.send_notification(error_msg)
            return False
    
    def execute_trade(self):
        data = self.get_historical_data()
        if data is None:
            return False
            
        buy_signal, sell_signal, stop_loss, take_profit = self.bnb_strategy(data)
        latest = data.iloc[-1]
        current_price = latest['close']
        
        if buy_signal:
            signal_strength = self.calculate_signal_strength(data, 'buy')
            trade_size = self.calculate_dynamic_size(signal_strength)
            
            can_trade, usdt_balance = self.check_balance_before_trade(trade_size)
            if can_trade:
                success = self.execute_real_trade('buy', current_price, stop_loss, take_profit, trade_size)
                if success:
                    level = self.get_strength_level(signal_strength)
                    msg = f"🎯 <b>شراء بمستوى {level}</b>\n\n"
                    msg += f"قوة الإشارة: {signal_strength}%\n"
                    msg += f"حجم الصفقة: ${trade_size:.2f}\n"
                    msg += f"السعر: ${current_price:.4f}\n"
                    msg += f"وقف الخسارة: ${stop_loss:.4f}\n"
                    msg += f"جني الأرباح: ${take_profit:.4f}\n"
                    msg += f"الرصيد المتبقي: ${usdt_balance - trade_size:.2f}"
                    self.send_notification(msg)
                return success
            else:
                self.send_notification(f"⚠️ إشارة شراء ولكن الرصيد غير كافي. المطلوب: ${trade_size:.2f}، المتاح: ${usdt_balance:.2f}")
        
        elif sell_signal:
            signal_strength = self.calculate_signal_strength(data, 'sell')
            trade_size = self.calculate_dynamic_size(signal_strength)
            
            total_balance, balances, _ = self.get_account_balance_details()
            bnb_balance = balances.get('BNB', {}).get('free', 0)
            if bnb_balance > 0.001:
                success = self.execute_real_trade('sell', current_price, 0, 0, trade_size)
                if success:
                    level = self.get_strength_level(signal_strength)
                    msg = f"🎯 <b>بيع بمستوى {level}</b>\n\n"
                    msg += f"قوة الإشارة: {signal_strength}%\n"
                    msg += f"حجم الصفقة: ${trade_size:.2f}\n"
                    msg += f"السعر: ${current_price:.4f}\n"
                    msg += f"الرصيد المتبقي: ${total_balance:.2f}"
                    self.send_notification(msg)
                return success
        
        return False
    
    def send_performance_report(self):
        try:
            total_balance, balances, bnb_price = self.get_account_balance_details()
            
            if total_balance is None:
                return
            
            profit_loss = total_balance - self.initial_balance
            profit_loss_percent = (profit_loss / self.initial_balance) * 100 if self.initial_balance > 0 else 0
            
            current_orders = self.get_algo_orders_count(self.symbol)
            
            balance_details = ""
            for asset, balance_info in balances.items():
                if balance_info['total'] > 0.0001:
                    if asset == 'USDT':
                        balance_details += f"{asset}: {balance_info['total']:.2f}\n"
                    else:
                        balance_details += f"{asset}: {balance_info['total']:.6f}\n"
            
            message = f"📊 <b>تقرير أداء البوت</b>\n\n"
            message += f"الرصيد الابتدائي: ${self.initial_balance:.2f}\n"
            message += f"الرصيد الحالي: ${total_balance:.2f}\n"
            message += f"الأرباح/الخسائر: ${profit_loss:.2f} ({profit_loss_percent:+.2f}%)\n"
            message += f"الأوامر النشطة: {current_orders}/{self.MAX_ALGO_ORDERS}\n"
            message += f"نطاق الصفقة: ${self.MIN_TRADE_SIZE}-${self.MAX_TRADE_SIZE}\n\n"
            message += f"<b>تفاصيل الرصيد:</b>\n{balance_details}"
            
            if bnb_price:
                message += f"\nسعر BNB الحالي: ${bnb_price:.4f}"
            
            self.send_notification(message)
            
        except Exception as e:
            error_msg = f"❌ خطأ في إرسال تقرير الأداء: {e}"
            logger.error(error_msg)
    
    def run(self):
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        
        interval_minutes = 15
        self.send_notification(f"🚀 بدء تشغيل بوت تداول BNB\n\nسيعمل البوت على فحص السوق كل {interval_minutes} دقيقة\nنطاق حجم الصفقة: ${self.MIN_TRADE_SIZE}-${self.MAX_TRADE_SIZE}\nالحد الأدنى للقيمة: ${self.min_notional}")
        
        self.send_performance_report()
        
        report_counter = 0
        
        while True:
            try:
                trade_executed = self.execute_trade()
                
                report_counter += 1
                if trade_executed or report_counter >= 4:
                    self.send_performance_report()
                    report_counter = 0
                
                time.sleep(interval_minutes * 60)
                
            except Exception as e:
                error_msg = f"❌ خطأ غير متوقع في التشغيل: {e}"
                self.send_notification(error_msg)
                time.sleep(300)

if __name__ == "__main__":
    try:
        print("🚀 بدء تشغيل بوت تداول BNB...")
        print("=" * 60)
        
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        print("🌐 خادم الويب يعمل على المنفذ 10000")
        
        bot = BNB_Trading_Bot()
        
        if bot.test_connection():
            print("✅ اختبار الاتصال ناجح!")
            print("🎯 بدء التشغيل الفعلي للبوت...")
            bot.run()
        
    except Exception as e:
        logger.error(f"فشل تشغيل البوت: {e}")
        print(f"❌ فشل تشغيل البوت: {e}")
