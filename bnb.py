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
        self.trade_size = 7  # حجم الصفقة 7 دولار
        self.active_trades = {}  # لتتبع الصفقات النشطة
        
        # زيادة معدلات وقف الخسارة وجني الأرباح
        self.stop_loss_multiplier = 2.5  # كان 1.5 - زيادة وقف الخسارة
        self.take_profit_multiplier = 3.5  # كان 2.0 - زيادة جني الأرباح
        
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
                self.notifier.send_message(f"🤖 <b>بدء تشغيل بوت تداول BNB</b>\n\n{success_msg}\nحجم الصفقة: ${self.trade_size}\nوقف الخسارة: {self.stop_loss_multiplier}×ATR\nجني الأرباح: {self.take_profit_multiplier}×ATR")
        except Exception as e:
            logger.error(f"خطأ في جلب الرصيد الابتدائي: {e}")
            self.initial_balance = 0

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
            print(f"حجم الصفقة: ${self.trade_size}")
            print(f"وقف الخسارة: {self.stop_loss_multiplier}×ATR")
            print(f"جني الأرباح: {self.take_profit_multiplier}×ATR")
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
    
    def format_price(self, price, symbol):
        """تقريب السعر حسب متطلبات Binance"""
        try:
            info = self.client.get_symbol_info(symbol)
            price_filter = [f for f in info['filters'] if f['filterType'] == 'PRICE_FILTER'][0]
            tick_size = float(price_filter['tickSize'])
            
            # التقريب حسب tickSize
            formatted_price = round(price / tick_size) * tick_size
            return round(formatted_price, 8)
        except Exception as e:
            logger.error(f"خطأ في تقريب السعر: {e}")
            return round(price, 4)
    
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
    
        # شروط الشراء (2 من 3 فقط)
        buy_condition_1 = 30 <= latest['rsi'] <= 35  # RSI بين 30-35
        buy_condition_2 = latest['macd'] > latest['macd_sig'] and latest['macd_hist'] > 0.05
        buy_condition_3 = latest['close'] > latest['ema20'] and latest['ema9'] > latest['ema20']
    
        # شروط البيع المعدلة (شرطين)
        sell_condition_1 = latest['rsi'] > 70 or latest['rsi'] < 25
        sell_condition_2 = latest['macd_hist'] < -0.05 or latest['close'] < latest['ema9']
    
        # إشارة الشراء النهائية (2 من 3 شروط)
        buy_conditions = [buy_condition_1, buy_condition_2, buy_condition_3]
        buy_signal = sum(buy_conditions) >= 2  # شرطين على الأقل
    
        # إشارة البيع النهائية (2 شرط)
        sell_signal = any([sell_condition_1, sell_condition_2])
    
        # إدارة المخاطر - باستخدام المضاعفات الجديدة
        atr_val = latest['atr']
        stop_loss = latest['close'] - (self.stop_loss_multiplier * atr_val)
        take_profit = latest['close'] + (self.take_profit_multiplier * atr_val)
    
        return buy_signal, sell_signal, stop_loss, take_profit
    
    def check_balance_before_trade(self, required_usdt=7):
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
    
    def execute_real_trade(self, signal_type, current_price, stop_loss, take_profit):
        """تنفيذ صفقة حقيقية مع أوامر وقف الخسارة وجني الأرباح على المنصة"""
        try:
            # التحقق من الرصيد قبل التنفيذ
            can_trade, usdt_balance = self.check_balance_before_trade(self.trade_size)
            
            if not can_trade:
                self.send_notification(f"⚠️ رصيد غير كافي للصفقة. المطلوب: ${self.trade_size}، المتاح: ${usdt_balance:.2f}")
                return False
            
            if signal_type == 'buy':
                # حساب الكمية بناء على حجم الصفقة
                quantity = self.trade_size / current_price
                
                # تقريب الكمية
                info = self.client.get_symbol_info(self.symbol)
                step_size = float([f['stepSize'] for f in info['filters'] if f['filterType'] == 'LOT_SIZE'][0])
                precision = len(str(step_size).split('.')[1].rstrip('0'))
                quantity = round(quantity - (quantity % step_size), precision)
                
                if quantity <= 0:
                    self.send_notification("⚠️ الكمية غير صالحة للشراء")
                    return False
                
                # تنفيذ أمر الشراء
                order = self.client.order_market_buy(
                    symbol=self.symbol,
                    quantity=quantity
                )
                
                # تقريب الأسعار حسب متطلبات Binance
                formatted_stop_loss = self.format_price(stop_loss, self.symbol)
                formatted_take_profit = self.format_price(take_profit, self.symbol)
                formatted_current = self.format_price(current_price, self.symbol)
                
                # إرسال إشعار بالشراء
                msg = f"✅ <b>تم الشراء فعلياً</b>\n\n"
                msg += f"السعر: ${formatted_current:.4f}\n"
                msg += f"الكمية: {quantity:.4f} BNB\n"
                msg += f"القيمة: ${self.trade_size:.2f}\n"
                msg += f"وقف الخسارة: ${formatted_stop_loss:.4f}\n"
                msg += f"جني الأرباح: ${formatted_take_profit:.4f}\n"
                msg += f"الرصيد المتبقي: ${usdt_balance - self.trade_size:.2f}"
                self.send_notification(msg)
                
                # وضع أوامر OCO (وقف خسارة وجني أرباح) على المنصة
                try:
                    oco_order = self.client.order_oco_sell(
                        symbol=self.symbol,
                        quantity=quantity,
                        stopPrice=formatted_stop_loss,
                        stopLimitPrice=formatted_stop_loss,
                        price=formatted_take_profit,
                        stopLimitTimeInForce='GTC'
                    )
                    
                    msg = f"📊 <b>تم وضع أوامر الوقف والأرباح على المنصة</b>\n\n"
                    msg += f"وقف الخسارة: ${formatted_stop_loss:.4f}\n"
                    msg += f"جني الأرباح: ${formatted_take_profit:.4f}\n"
                    msg += f"الكمية: {quantity:.4f} BNB"
                    self.send_notification(msg)
                    
                except Exception as e:
                    error_msg = f"⚠️ فشل وضع أوامر الوقف على المنصة: {e}"
                    self.send_notification(error_msg)
                    # محاولة البيع يدوياً إذا فشل الأمر OCO
                    try:
                        self.client.order_market_sell(
                            symbol=self.symbol,
                            quantity=quantity
                        )
                        self.send_notification("⚠️ تم البيع فورياً بسبب فشل وضع أوامر الوقف")
                    except Exception as sell_error:
                        self.send_notification(f"❌ فشل البيع أيضاً: {sell_error}")
                
                return True
                
            elif signal_type == 'sell':
                # البيع بناء على إشارة استراتيجية (بيع جميع حيازات BNB)
                total_balance, balances, _ = self.get_account_balance_details()
                bnb_balance = balances.get('BNB', {}).get('free', 0)
                
                if bnb_balance <= 0.001:
                    self.send_notification("⚠️ رصيد BNB غير كافي للبيع")
                    return False
                
                # تقريب الكمية
                info = self.client.get_symbol_info(self.symbol)
                step_size = float([f['stepSize'] for f in info['filters'] if f['filterType'] == 'LOT_SIZE'][0])
                precision = len(str(step_size).split('.')[1].rstrip('0'))
                quantity = round(bnb_balance - (bnb_balance % step_size), precision)
                
                if quantity <= 0:
                    self.send_notification("⚠️ الكمية غير صالحة للبيع")
                    return False
                
                # تنفيذ أمر البيع
                order = self.client.order_market_sell(
                    symbol=self.symbol,
                    quantity=quantity
                )
                
                # إرسال إشعار بالبيع
                expected_proceeds = quantity * current_price
                msg = f"🔻 <b>تم البيع فعلياً</b>\n\n"
                msg += f"السعر: ${current_price:.4f}\n"
                msg += f"الكمية: {quantity:.4f} BNB\n"
                msg += f"القيمة: ${expected_proceeds:.2f}"
                self.send_notification(msg)
                
                # إلغاء أي أوامر OCO موجودة
                try:
                    open_orders = self.client.get_open_orders(symbol=self.symbol)
                    for open_order in open_orders:
                        if open_order['type'] == 'STOP_LOSS_LIMIT' or open_order['type'] == 'OCO':
                            self.client.cancel_order(
                                symbol=self.symbol,
                                orderId=open_order['orderId']
                            )
                except Exception as e:
                    logger.error(f"خطأ في إلغاء الأوامر المفتوحة: {e}")
                
                return True
                
        except Exception as e:
            error_msg = f"❌ خطأ في تنفيذ الصفقة: {e}"
            self.send_notification(error_msg)
            return False
    
    def check_active_trades(self, current_price):
        """التحقق من أوامر وقف الخسارة وجني الأرباح للصفقات النشطة"""
        # لم نعد بحاجة لهذه الوظيفة لأن الأوامر على المنصة
        return False
    
    def execute_trade(self):
        data = self.get_historical_data()
        if data is None:
            return False
            
        buy_signal, sell_signal, stop_loss, take_profit = self.bnb_strategy(data)
        latest = data.iloc[-1]
        current_price = latest['close']
        
        # إذا كانت هناك إشارة شراء والرصيد كافي
        if buy_signal:
            can_trade, usdt_balance = self.check_balance_before_trade(self.trade_size)
            if can_trade:
                success = self.execute_real_trade('buy', current_price, stop_loss, take_profit)
                return success
            else:
                self.send_notification(f"⚠️ إشارة شراء ولكن الرصيد غير كافي. المطلوب: ${self.trade_size}، المتاح: ${usdt_balance:.2f}")
        
        # إذا كانت هناك إشارة بيع
        elif sell_signal:
            total_balance, balances, _ = self.get_account_balance_details()
            bnb_balance = balances.get('BNB', {}).get('free', 0)
            if bnb_balance > 0.001:
                success = self.execute_real_trade('sell', current_price, 0, 0)
                return success
        
        return False
    
    def send_performance_report(self):
        try:
            total_balance, balances, bnb_price = self.get_account_balance_details()
            
            if total_balance is None:
                return
            
            profit_loss = total_balance - self.initial_balance
            profit_loss_percent = (profit_loss / self.initial_balance) * 100 if self.initial_balance > 0 else 0
            
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
            message += f"وقف الخسارة: {self.stop_loss_multiplier}×ATR\n"
            message += f"جني الأرباح: {self.take_profit_multiplier}×ATR\n"
            message += f"حجم الصفقة: ${self.trade_size}\n\n"
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
        self.send_notification(f"🚀 بدء تشغيل بوت تداول BNB\n\nسيعمل البوت على فحص السوق كل {interval_minutes} دقيقة\nحجم الصفقة: ${self.trade_size}\nوقف الخسارة: {self.stop_loss_multiplier}×ATR\nجني الأرباح: {self.take_profit_multiplier}×ATR")
        
        self.send_performance_report()
        
        report_counter = 0
        
        while True:
            try:
                # التداول على مدار الأسبوع (بما في ذلك عطلات نهاية الأسبوع)
                trade_executed = self.execute_trade()
                
                report_counter += 1
                if trade_executed or report_counter >= 4:  # تقرير كل ساعة
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
