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
# إضافة المكتبات المطلوبة للخادم
from flask import Flask
import threading

# تحميل متغيرات البيئة من ملف .env
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
    """تشغيل خادم Flask على المنفذ المحدد"""
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
        # تهيئة notifier أولاً لتجنب الأخطاء
        self.notifier = None
        
        # الحصول على المفاتيح من المعطيات أو من متغيرات البيئة
        self.api_key = api_key or os.environ.get('BINANCE_API_KEY')
        self.api_secret = api_secret or os.environ.get('BINANCE_API_SECRET')
        telegram_token = telegram_token or os.environ.get('TELEGRAM_BOT_TOKEN')
        telegram_chat_id = telegram_chat_id or os.environ.get('TELEGRAM_CHAT_ID')
        
        # اختبار المفاتيح وطباعة سبب المشكلة
        if not self.api_key or not self.api_secret:
            error_msg = "❌ مفاتيح Binance غير موجودة"
            logger.error(error_msg)
            print("="*50)
            print("أسباب المشكلة المحتملة:")
            print("1. لم يتم توفير مفاتيح API في الكود")
            print("2. لم يتم تعيين متغيرات البيئة BINANCE_API_KEY و BINANCE_API_SECRET")
            print("3. ملف .env غير موجود أو غير صحيح")
            print("="*50)
            raise ValueError(error_msg)
            
        try:
            # اتصال بالمنصة الفعلية فقط
            self.client = Client(self.api_key, self.api_secret)
            logger.info("✅ تم الاتصال بمنصة Binance الفعلية")
                
            # اختبار الاتصال فوراً
            self.test_connection()
                
        except Exception as e:
            error_msg = f"❌ فشل الاتصال بـ Binance: {e}"
            logger.error(error_msg)
            print("="*50)
            print("أسباب فشل الاتصال:")
            print("1. مفاتيح API غير صحيحة")
            print("2. IP غير مسموح به في إعدادات Binance API")
            print("3. مشكلة في الاتصال بالإنترنت")
            print("4. الصلاحيات غير كافية (يجب تفعيل التداول)")
            print("5. تأكد من استخدام مفاتيح Live للوضع الفعلي")
            print("="*50)
            raise ConnectionError(error_msg)
            
        self.fee_rate = 0.0005
        self.slippage = 0.00015
        self.trades = []
        self.symbol = "BNBUSDT"
        
        # إعداد إشعارات Telegram إذا كانت المفاتيح متوفرة
        if telegram_token and telegram_chat_id:
            self.notifier = TelegramNotifier(telegram_token, telegram_chat_id)
            logger.info("تم تهيئة إشعارات Telegram")
        else:
            logger.warning("مفاتيح Telegram غير موجودة، سيتم تعطيل الإشعارات")
        
        # جلب الرصيد الابتدائي (مع معالجة الأخطاء)
        try:
            self.initial_balance = self.get_real_balance()
            success_msg = f"✅ تم تهيئة البوت بنجاح - الرصيد الابتدائي: ${self.initial_balance:.2f}"
            logger.info(success_msg)
            if self.notifier:
                self.notifier.send_message(f"🤖 <b>بدء تشغيل بوت تداول BNB</b>\n\n{success_msg}\nوضع التشغيل: فعلي")
        except Exception as e:
            logger.error(f"خطأ في جلب الرصيد الابتدائي: {e}")
            self.initial_balance = 0

    def test_connection(self):
        """اختبار الاتصال بمنصة Binance"""
        try:
            # اختبار بسيط للاتصال
            server_time = self.client.get_server_time()
            logger.info(f"✅ الاتصال ناجح - وقت الخادم: {server_time['serverTime']}")
            
            # اختبار الحصول على معلومات الحساب
            account_info = self.client.get_account()
            logger.info("✅ جلب معلومات الحساب ناجح")
            
            # الحصول على IP الخادم
            public_ip = self.get_public_ip()
            logger.info(f"🌐 IP الخادم: {public_ip}")
            
            print("="*50)
            print("✅ اختبار الاتصال ناجح!")
            print("وضع التشغيل: فعلي")
            print(f"IP الخادم: {public_ip}")
            print("="*50)
            
            return True
        except Exception as e:
            logger.error(f"❌ فشل الاتصال: {e}")
            
            # الحصول على IP للتحقق منه
            public_ip = self.get_public_ip()
            print("="*50)
            print("❌ فشل اختبار الاتصال!")
            print(f"IP الخادم: {public_ip}")
            print("يرجى التأكد من:")
            print("1. إضافة هذا IP إلى القائمة البيضاء في Binance")
            print("2. استخدام مفاتيح Live للوضع الفعلي")
            print("3. تفعيل صلاحية 'التداول' في إعدادات API")
            print("="*50)
            
            return False

    def get_public_ip(self):
        """الحصول على IP العام للخادم"""
        try:
            response = requests.get('https://api.ipify.org?format=json', timeout=10)
            return response.json()['ip']
        except:
            return "غير معروف"
    
    def get_real_balance(self):
        """جلب الرصيد الحقيقي من منصة Binance"""
        try:
            account = self.client.get_account()
            balances = {asset['asset']: float(asset['free']) + float(asset['locked']) for asset in account['balances']}
            
            # الحصول على أسعار جميع الأصول
            prices = self.client.get_all_tickers()
            price_dict = {item['symbol']: float(item['price']) for item in prices}
            
            # حساب إجمالي الرصيد بالدولار
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
        """الحصول على تفاصيل الرصيد الحالي من حساب Binance"""
        try:
            account = self.client.get_account()
            balances = {asset['asset']: {
                'free': float(asset['free']),
                'locked': float(asset['locked']),
                'total': float(asset['free']) + float(asset['locked'])
            } for asset in account['balances'] if float(asset['free']) > 0 or float(asset['locked']) > 0}
            
            # الحصول على سعر BNB الحالي
            ticker = self.client.get_symbol_ticker(symbol=self.symbol)
            bnb_price = float(ticker['price'])
            
            # حساب الرصيد الإجمالي
            total_balance = self.get_real_balance()
            
            return total_balance, balances, bnb_price
        except Exception as e:
            error_msg = f"❌ خطأ في الحصول على رصيد الحساب: {e}"
            logger.error(error_msg)
            return None, None, None
    
    def send_notification(self, message):
        """إرسال إشعار إلى Telegram والتسجيل في السجلات"""
        logger.info(message)
        if self.notifier:
            self.notifier.send_message(message)
    
    def calculate_rsi(self, data, period=14):
        """حساب RSI بنفس طريقة finaleth"""
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
        """حساب ATR بنفس طريقة finaleth"""
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
        """حساب MACD بنفس طريقة finaleth"""
        ema_fast = series.ewm(span=fast, adjust=False).mean()
        ema_slow = series.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        sig = macd_line.ewm(span=signal, adjust=False).mean()
        hist = macd_line - sig
        return macd_line, sig, hist
    
    def get_historical_data(self, interval=Client.KLINE_INTERVAL_15MINUTE, lookback='2000 hour ago UTC'):
        """جلب البيانات التاريخية بنفس طريقة finaleth"""
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
            
            # حساب المؤشرات بنفس طريقة finaleth
            data['rsi'] = self.calculate_rsi(data['close'])
            data['atr'] = self.calculate_atr(data)
            data['ema200'] = data['close'].ewm(span=200, adjust=False).mean()
            data['ema50'] = data['close'].ewm(span=50, adjust=False).mean()
            data['ema20'] = data['close'].ewm(span=20, adjust=False).mean()
            data['ema9'] = data['close'].ewm(span=9, adjust=False).mean()
            data['vol_ma20'] = data['volume'].rolling(20).mean()
            data['vol_ratio'] = data['volume'] / data['vol_ma20']
            
            # حساب MACD
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
        """استراتيجية BNB معدلة بالكامل"""
        if data is None or len(data) < 100:
            return False, False, 0, 0
        
        latest = data.iloc[-1]
        prev = data.iloc[-2]
    
        # 🔄 التعديل الجذري على شروط الشراء
        rsi_condition = latest['rsi'] >= 50 and latest['rsi'] <= 65
        macd_condition = latest['macd'] > latest['macd_sig'] and latest['macd_hist'] > 0.1
        volume_condition = latest['vol_ratio'] >= 1.2  # تخفيض الحد الأدنى للحجم
    
        # 📈 شروط الاتجاه المعدلة
        price_above_ema20 = latest['close'] > latest['ema20']
        ema_alignment = latest['ema9'] > latest['ema20'] > latest['ema50']
        strong_trend = price_above_ema20 and ema_alignment
    
        # 🛑 شروط البيع المعدلة
        rsi_sell = latest['rsi'] < 40 or latest['rsi'] > 75
        macd_sell = latest['macd_hist'] < -0.1
        price_below_ema9 = latest['close'] < latest['ema9']
    
        # ✅ إشارة الشراء النهائية
        buy_signal = all([
            rsi_condition,
            macd_condition, 
            volume_condition,
            strong_trend
        ])
    
        # ❌ إشارة البيع النهائية
        sell_signal = any([
            rsi_sell,
            macd_sell,
            price_below_ema9
        ])
    
        # 🎯 إدارة المخاطر
        atr_val = latest['atr']
        stop_loss = latest['close'] - (2.0 * atr_val)  # تخفيض المضاعف
        take_profit = latest['close'] + (2.0 * 2.0 * atr_val)  # RR = 2:1
    
        return buy_signal, sell_signal, stop_loss, take_profit
    
    def execute_real_trade(self, signal_type):
        """تنفيذ صفقة حقيقية على Binance"""
        try:
            if signal_type == 'buy':
                # الحصول على الرصيد المتاح
                total_balance, balances, bnb_price = self.get_account_balance_details()
                usdt_balance = balances.get('USDT', {}).get('free', 0)
                
                if usdt_balance < 10:  # على الأقل 10 USDT للشراء
                    self.send_notification("⚠️ رصيد USDT غير كافي للشراء")
                    return False
                
                # حساب الكمية بناء على الرصيد المتاح
                amount_to_spend = usdt_balance * 0.99  # استخدم 99% من الرصيد للشراء
                quantity = amount_to_spend / bnb_price
                
                # تقريب الكمية إلى المنزلة العشرية الصحيحة لـ BNB
                info = self.client.get_symbol_info(self.symbol)
                step_size = float([f['stepSize'] for f in info['filters'] if f['filterType'] == 'LOT_SIZE'][0])
                precision = len(str(step_size).split('.')[1].rstrip('0'))
                quantity = round(quantity - (quantity % step_size), precision)
                
                # تنفيذ أمر الشراء
                order = self.client.order_market_buy(
                    symbol=self.symbol,
                    quantity=quantity
                )
                
                # إرسال إشعار بالشراء
                msg = f"✅ <b>تم الشراء فعلياً</b>\n\nالسعر: ${bnb_price:.4f}\nالكمية: {quantity:.4f} BNB\nالقيمة: ${amount_to_spend:.2f}"
                self.send_notification(msg)
                
                return True
                
            elif signal_type == 'sell':
                # الحصول على رصيد BNB
                total_balance, balances, bnb_price = self.get_account_balance_details()
                bnb_balance = balances.get('BNB', {}).get('free', 0)
                
                if bnb_balance < 0.001:  # على الأقل 0.001 BNB للبيع
                    self.send_notification("⚠️ رصيد BNB غير كافي للبيع")
                    return False
                
                # تقريب الكمية إلى المنزلة العشرية الصحيحة لـ BNB
                info = self.client.get_symbol_info(self.symbol)
                step_size = float([f['stepSize'] for f in info['filters'] if f['filterType'] == 'LOT_SIZE'][0])
                precision = len(str(step_size).split('.')[1].rstrip('0'))
                quantity = round(bnb_balance - (bnb_balance % step_size), precision)
                
                # تنفيذ أمر البيع
                order = self.client.order_market_sell(
                    symbol=self.symbol,
                    quantity=quantity
                )
                
                # إرسال إشعار بالبيع
                expected_proceeds = quantity * bnb_price
                msg = f"🔻 <b>تم البيع فعلياً</b>\n\nالسعر: ${bnb_price:.4f}\nالكمية: {quantity:.4f} BNB\nالقيمة المتوقعة: ${expected_proceeds:.2f}"
                self.send_notification(msg)
                
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
        
        # إذا كانت هناك إشارة شراء
        if buy_signal:
            # تنفيذ صفقة حقيقية
            success = self.execute_real_trade('buy')
            return success
        
        # إذا كانت هناك إشارة بيع
        elif sell_signal:
            # تنفيذ صفقة حقيقية
            success = self.execute_real_trade('sell')
            return success
        
        return False
    
    def send_performance_report(self):
        """إرسال تقرير أداء مع الرصيد الحقيقي من المنصة"""
        try:
            total_balance, balances, bnb_price = self.get_account_balance_details()
            
            if total_balance is None:
                return
            
            # حساب الأداء
            profit_loss = total_balance - self.initial_balance
            profit_loss_percent = (profit_loss / self.initial_balance) * 100 if self.initial_balance > 0 else 0
            
            # تفاصيل الرصيد
            balance_details = ""
            for asset, balance_info in balances.items():
                if balance_info['total'] > 0.0001:  # تجاهل القيم الصغيرة جداً
                    if asset == 'USDT':
                        balance_details += f"{asset}: {balance_info['total']:.2f}\n"
                    else:
                        balance_details += f"{asset}: {balance_info['total']:.6f}\n"
            
            # إعداد الرسالة
            message = f"📊 <b>تقرير أداء البوت</b>\n\n"
            message += f"الرصيد الابتدائي: ${self.initial_balance:.2f}\n"
            message += f"الرصيد الحالي: ${total_balance:.2f}\n"
            message += f"الأرباح/الخسائر: ${profit_loss:.2f} ({profit_loss_percent:+.2f}%)\n\n"
            message += f"<b>تفاصيل الرصيد:</b>\n{balance_details}"
            
            if bnb_price:
                message += f"\nسعر BNB الحالي: ${bnb_price:.4f}"
            
            self.send_notification(message)
            
        except Exception as e:
            error_msg = f"❌ خطأ في إرسال تقرير الأداء: {e}"
            logger.error(error_msg)
    
    def run(self):
        """الدالة الرئيسية لتشغيل البوت بشكل مستمر"""
        # بدء خادم Flask في thread منفصل
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        
        interval_minutes = 15  # الفترة بين كل فحص للسوق
        self.send_notification(f"🚀 بدء تشغيل بوت تداول BNB\n\nسيعمل البوت على فحص السوق كل {interval_minutes} دقيقة")
        
        # إرسال تقرير الأداء الأولي
        self.send_performance_report()
        
        report_counter = 0
        
        while True:
            try:
                # التحقق من الوقت (لا تتداول في عطلات نهاية الأسبوع أو خارج أوقات السوق)
                now = datetime.now()
                if now.weekday() >= 5:  # السبت والأحد
                    time.sleep(3600)  # الانتظار ساعة وإعادة التحقق
                    continue
                
                # تنفيذ التحليل والتداول
                trade_executed = self.execute_trade()
                
                # إرسال تحديث دوري عن الحالة كل 4 ساعات (16 دورة)
                report_counter += 1
                if trade_executed or report_counter >= 16:
                    self.send_performance_report()
                    report_counter = 0
                
                # الانتظار للفترة التالية
                time.sleep(interval_minutes * 60)
                
            except Exception as e:
                error_msg = f"❌ خطأ غير متوقع في التشغيل: {e}"
                self.send_notification(error_msg)
                time.sleep(300)  # الانتظار 5 دقائق قبل إعادة المحاولة

# تشغيل البوت
if __name__ == "__main__":
    try:
        print("🚀 بدء تشغيل بوت تداول BNB...")
        print("=" * 60)
        
        # بدء خادم Flask في thread منفصل
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        print("🌐 خادم الويب يعمل على المنفذ 10000")
        
        # وضع الباكتستنغ - إضافة معلمة test_mode=True
        bot = BNB_Trading_Bot()
        
        # اختبار الاتصال أولاً
        if bot.test_connection():
            print("✅ اختبار الاتصال ناجح!")
            
            # جلب البيانات التاريخية لفترات مختلفة
            timeframes = [
                ('1 hour ago UTC', '1 ساعة'),
                ('24 hours ago UTC', '24 ساعة'), 
                ('7 days ago UTC', '7 أيام'),
                ('30 days ago UTC', '30 يوم'),
                ('2000 hour ago UTC', '2000 ساعة')
            ]
            
            all_data = {}
            
            for lookback, label in timeframes:
                print(f"\n📊 جلب بيانات {label}...")
                try:
                    # تعديل دالة get_historical_data لقبول معامل lookback
                    data = bot.get_historical_data(lookback=lookback)
                    if data is not None:
                        all_data[label] = data
                        print(f"✅ {label}: {len(data)} صف - السعر: {data['close'].iloc[-1]:.4f} USDT")
                    else:
                        print(f"❌ فشل في جلب بيانات {label}")
                except Exception as e:
                    print(f"❌ خطأ في جلب بيانات {label}: {e}")
            
            if all_data:
                # استخدام أحدث مجموعة بيانات للتحليل
                latest_label = list(all_data.keys())[-1]
                data = all_data[latest_label]
                
                print(f"\n{'='*60}")
                print(f"📈 تحليل مفصل لأحدث بيانات ({latest_label}):")
                print(f"{'='*60}")
                
                # عرض إحصائيات مفصلة
                print(f"📅 الفترة: {data['timestamp'].iloc[0].strftime('%Y-%m-%d')} إلى {data['timestamp'].iloc[-1].strftime('%Y-%m-%d')}")
                print(f"📊 عدد الصفوف: {len(data)}")
                print(f"💰 السعر الحالي: {data['close'].iloc[-1]:.4f} USDT")
                print(f"📈 أعلى سعر: {data['high'].max():.4f} USDT")
                print(f"📉 أدنى سعر: {data['low'].min():.4f} USDT")
                print(f"📊 متوسط السعر: {data['close'].mean():.4f} USDT")
                
                # تحليل المؤشرات
                latest = data.iloc[-1]
                print(f"\n📊 المؤشرات الفنية:")
                print(f"📶 RSI: {latest['rsi']:.2f} {'(شراء)' if latest['rsi'] < 30 else '(بيع)' if latest['rsi'] > 70 else '(محايد)'}")
                print(f"📈 MACD Histogram: {latest['macd_hist']:.6f} {'(إيجابي)' if latest['macd_hist'] > 0 else '(سلبي)'}")
                print(f"📊 ATR: {latest['atr']:.4f}")
                print(f"📈 EMA9: {latest['ema9']:.4f}")
                print(f"📈 EMA20: {latest['ema20']:.4f}")
                print(f"📈 EMA50: {latest['ema50']:.4f}")
                print(f"📈 EMA200: {latest['ema200']:.4f}")
                print(f"📊 نسبة الحجم: {latest['vol_ratio']:.2f}x")
                
                # اختبار الاستراتيجية على آخر 50 شمعة
                print(f"\n{'='*60}")
                print("🤖 تحليل إشارات التداول (آخر 50 شمعة):")
                print(f"{'='*60}")
                
                buy_signals = 0
                sell_signals = 0
                
                for i in range(max(0, len(data)-50), len(data)):
                    current_data = data.iloc[:i+1]
                    buy_signal, sell_signal, stop_loss, take_profit = bot.bnb_strategy(current_data)
                    
                    if buy_signal:
                        buy_signals += 1
                        print(f"📈 إشارة شراء عند: {current_data['timestamp'].iloc[-1].strftime('%Y-%m-%d %H:%M')} - السعر: {current_data['close'].iloc[-1]:.4f}")
                    
                    if sell_signal:
                        sell_signals += 1
                        print(f"📉 إشارة بيع عند: {current_data['timestamp'].iloc[-1].strftime('%Y-%m-%d %H:%M')} - السعر: {current_data['close'].iloc[-1]:.4f}")
                
                print(f"\n📊 إجمالي الإشارات:")
                print(f"🟢 إشارات شراء: {buy_signals}")
                print(f"🔴 إشارات بيع: {sell_signals}")
                
                # اختبار الاستراتيجية الحالية
                buy_signal, sell_signal, stop_loss, take_profit = bot.bnb_strategy(data)
                print(f"\n🎯 الإشارة الحالية:")
                print(f"🟢 شراء: {buy_signal}")
                print(f"🔴 بيع: {sell_signal}")
                
                if buy_signal:
                    print(f"⛔ وقف الخسارة: {stop_loss:.4f}")
                    print(f"🎯 جني الأرباح: {take_profit:.4f}")
                    print(f"📊 نسبة المخاطرة/العائد: {((take_profit - latest['close']) / (latest['close'] - stop_loss)):.2f}:1")
                
                # تحليل الاتجاه
                print(f"\n📊 تحليل الاتجاه:")
                print(f"📈 السعر فوق EMA200: {latest['close'] > latest['ema200']}")
                print(f"📈 اتجاه صاعد قوي: {latest['trend_strong']}")
                print(f"📈 السعر فوق EMA50: {latest['price_above_ema50']}")
                
            # عرض تقرير الأداء
            print(f"\n{'='*60}")
            print("📈 تقرير الأداء:")
            print(f"{'='*60}")
            bot.send_performance_report()
            
            # اختبار إشعارات التلجرام (إذا كانت مفعلة)
            if bot.notifier:
                print(f"\n{'='*60}")
                print("📨 اختبار إرسال إشعار Telegram...")
                performance_msg = f"""
🔧 <b>تقرير اختبار البوت الشامل</b>

📊 البيانات المجمعة: {sum(len(d) for d in all_data.values())} صف
💰 السعر الحالي: {data['close'].iloc[-1]:.4f} USDT
📶 RSI الحالي: {latest['rsi']:.2f}
📈 MACD: {latest['macd_hist']:.6f}

📊 الإشارات في آخر 50 شمعة:
🟢 شراء: {buy_signals}
🔴 بيع: {sell_signals}

🎯 الإشارة الحالية:
{'🟢 شراء' if buy_signal else '🔴 بيع' if sell_signal else '⚪ محايد'}

✅ اختبار البوت مكتمل بنجاح
                """
                bot.send_notification(performance_msg)
                print("✅ تم إرسال رسالة الاختبار المفصلة")
            
            print(f"\n{'='*60}")
            print("🎯 البوت جاهز للتشغيل الفعلي!")
            print("للتشغيل الفعلي، قم بإزالة وضع الباكتستنغ وتأكد من صحة المفاتيح")
            print(f"{'='*60}")
        
    except Exception as e:
        logger.error(f"فشل تشغيل البوت: {e}")
        print(f"❌ فشل تشغيل البوت: {e}")
        print("\n🔍 أسباب محتملة:")
        print("1. مفاتيح API غير صحيحة")
        print("2. مشكلة في الاتصال بالإنترنت")
        print("3. IP غير مسموح به في Binance")
        print("4. صلاحيات API غير كافية")
