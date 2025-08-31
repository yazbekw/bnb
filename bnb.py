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
    def __init__(self, initial_balance=1000):
        # الحصول على المفاتيح من متغيرات البيئة
        api_key = os.environ.get('BINANCE_API_KEY')
        api_secret = os.environ.get('BINANCE_API_SECRET')
        telegram_token = os.environ.get('TELEGRAM_BOT_TOKEN')
        telegram_chat_id = os.environ.get('TELEGRAM_CHAT_ID')
        
        if not api_key or not api_secret:
            raise ValueError("مفاتيح Binance غير موجودة في متغيرات البيئة")
            
        # تحديد وضع الاختبار بناءً على متغير البيئة
        self.test_mode = os.environ.get('TEST_MODE', 'False').lower() == 'false'
        
        if not self.test_mode:  # التداول الفعلي
            self.client = Client(api_key, api_secret)
            logger.info("وضع التداول الفعلي مفعّل")
        else:
            self.client = Client(api_key, api_secret, testnet=True)
            logger.info("وضع الاختبار مفعّل")
            
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.fee_rate = 0.001  # عمولة Binance الأساسية
        self.slippage = 0.0005
        self.trades = []
        self.symbol = "BNBUSDT"
        
        # إعداد إشعارات Telegram إذا كانت المفاتيح متوفرة
        if telegram_token and telegram_chat_id:
            self.notifier = TelegramNotifier(telegram_token, telegram_chat_id)
            self.notifier.send_message(f"🤖 <b>بدء تشغيل بوت تداول BNB</b>\n\nالرصيد الافتتاحي: ${initial_balance:.2f}\nوضع التشغيل: {'فعلي' if not self.test_mode else 'اختبار'}")
        else:
            self.notifier = None
            logger.warning("مفاتيح Telegram غير موجودة، سيتم تعطيل الإشعارات")
    
    def send_notification(self, message):
        """إرسال إشعار إلى Telegram والتسجيل في السجلات"""
        logger.info(message)
        if self.notifier:
            self.notifier.send_message(message)
    
    def calculate_rsi(self, data, period=14):
        delta = data.diff()
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        
        avg_gain = gain.ewm(com=period-1, min_periods=period).mean()
        avg_loss = loss.ewm(com=period-1, min_periods=period).mean()
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
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
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    
    def get_historical_data(self, interval=Client.KLINE_INTERVAL_15MINUTE, lookback='500 hour ago UTC'):
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
            
            # حساب المؤشرات
            data['rsi'] = self.calculate_rsi(data['close'])
            data['ma20'] = self.calculate_ma(data['close'], 20)
            data['ma50'] = self.calculate_ma(data['close'], 50)
            data['ma200'] = self.calculate_ma(data['close'], 200)
            data['upper_bb'], data['ma20_bb'], data['lower_bb'] = self.calculate_bollinger_bands(data['close'])
            data['atr'] = self.calculate_atr(data)
            data['volume_ma20'] = self.calculate_ma(data['volume'], 20)
            
            return data
        except Exception as e:
            error_msg = f"❌ خطأ في جلب البيانات: {e}"
            self.send_notification(error_msg)
            return None
    
    def bnb_strategy(self, data):
        """استراتيجية BNB المبنية على المؤشرات المتعددة"""
        if data is None or len(data) < 50:
            return False, False, 0, 0
            
        latest = data.iloc[-1]
        prev = data.iloc[-2] if len(data) > 1 else latest
        
        # شروط الشراء لـ BNB
        rsi_condition = latest['rsi'] < 40
        price_above_ma20 = latest['close'] > latest['ma20']
        ma_trend = latest['ma20'] > latest['ma50']
        bollinger_condition = latest['close'] < latest['lower_bb']
        volume_condition = latest['volume'] > latest['volume_ma20'] * 0.8
        
        # شروط البيع لـ BNB
        rsi_sell_condition = latest['rsi'] > 65
        price_below_ma20 = latest['close'] < latest['ma20']
        bollinger_sell_condition = latest['close'] > latest['upper_bb']
        
        # إشارة الشراء (3 من 5 شروط)
        buy_conditions = [rsi_condition, price_above_ma20, ma_trend, bollinger_condition, volume_condition]
        buy_signal = sum(buy_conditions) >= 3
        
        # إشارة البيع (شرطين)
        sell_conditions = [rsi_sell_condition, price_below_ma20, bollinger_sell_condition]
        sell_signal = sum(sell_conditions) >= 2
        
        # وقف الخسارة وجني الأرباح لـ BNB
        stop_loss = 0.02  # 2%
        take_profit = 0.035  # 3.5%
        
        return buy_signal, sell_signal, stop_loss, take_profit
    
    def get_account_balance(self):
        """الحصول على الرصيد الحالي من حساب Binance"""
        try:
            account = self.client.get_account()
            balances = {asset['asset']: float(asset['free']) for asset in account['balances']}
            usdt_balance = balances.get('USDT', 0)
            bnb_balance = balances.get('BNB', 0)
            
            # الحصول على سعر BNB الحالي
            ticker = self.client.get_symbol_ticker(symbol=self.symbol)
            bnb_price = float(ticker['price'])
            
            total_balance = usdt_balance + (bnb_balance * bnb_price)
            return total_balance, usdt_balance, bnb_balance, bnb_price
        except Exception as e:
            error_msg = f"❌ خطأ في الحصول على رصيد الحساب: {e}"
            self.send_notification(error_msg)
            return None, None, None, None
    
    def execute_real_trade(self, signal_type, quantity=None):
        """تنفيذ صفقة حقيقية على Binance"""
        try:
            if signal_type == 'buy':
                # الحصول على الرصيد المتاح
                total_balance, usdt_balance, _, bnb_price = self.get_account_balance()
                
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
                total_balance, _, bnb_balance, bnb_price = self.get_account_balance()
                
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
            if not self.test_mode:  # التداول الفعلي
                # تنفيذ صفقة حقيقية
                success = self.execute_real_trade('buy')
                if success:
                    # تحديث الرصيد بعد الصفقة
                    self.balance, _, _, _ = self.get_account_balance()
                    return True
            else:
                # محاكاة الصفقة (للاختبار فقط)
                quantity = (self.balance * (1 - self.fee_rate)) / current_price
                self.balance -= self.balance * self.fee_rate
                
                msg = f"✅ <b>إشارة شراء (اختبار)</b>\n\nالسعر: ${current_price:.4f}\nالكمية: {quantity:.4f} BNB\nالقيمة: ${self.balance:.2f}\nوقف الخسارة: {stop_loss*100:.1f}%\nجني الأرباح: {take_profit*100:.1f}%"
                self.send_notification(msg)
                
                trade = {
                    'type': 'buy',
                    'price': current_price,
                    'quantity': quantity,
                    'timestamp': datetime.now(),
                    'balance': self.balance
                }
                self.trades.append(trade)
                return True
        
        # إذا كانت هناك إشارة بيع
        elif sell_signal:
            if not self.test_mode:  # التداول الفعلي
                # تنفيذ صفقة حقيقية
                success = self.execute_real_trade('sell')
                if success:
                    # تحديث الرصيد بعد الصفقة
                    self.balance, _, _, _ = self.get_account_balance()
                    return True
            else:
                # محاكاة البيع (للاختبار فقط)
                if hasattr(self, 'position') and self.position > 0:
                    sell_value = self.position * current_price
                    self.balance = sell_value * (1 - self.fee_rate)
                    profit_percent = ((current_price - self.entry_price) / self.entry_price) * 100
                    
                    msg = f"🔻 <b>إشارة بيع (اختبار)</b>\n\nالسعر: ${current_price:.4f}\nالكمية: {self.position:.4f} BNB\nالقيمة: ${sell_value:.2f}\nالربح/الخسارة: {profit_percent:.2f}%"
                    self.send_notification(msg)
                    
                    trade = {
                        'type': 'sell',
                        'price': current_price,
                        'quantity': self.position,
                        'timestamp': datetime.now(),
                        'balance': self.balance,
                        'profit_percent': profit_percent
                    }
                    self.trades.append(trade)
                    
                    # إغلاق المركز
                    del self.position
                    return True
        
        return False
    
    def run(self):
        """الدالة الرئيسية لتشغيل البوت بشكل مستمر"""
        interval_minutes = 15  # الفترة بين كل فحص للسوق
        self.send_notification(f"🚀 بدء تشغيل بوت تداول BNB\n\nسيعمل البوت على فحص السوق كل {interval_minutes} دقيقة")
        
        while True:
            try:
                # التحقق من الوقت (لا تتداول في عطلات نهاية الأسبوع أو خارج أوقات السوق)
                now = datetime.now()
                if now.weekday() >= 5:  # السبت والأحد
                    time.sleep(3600)  # الانتظار ساعة وإعادة التحقق
                    continue
                
                # تنفيذ التحليل والتداول
                trade_executed = self.execute_trade()
                
                # إرسال تحديث دوري عن الحالة
                if trade_executed or (len(self.trades) > 0 and len(self.trades) % 5 == 0):
                    total_balance, usdt_balance, bnb_balance, bnb_price = self.get_account_balance()
                    
                    if total_balance is not None:
                        total_trades = len(self.trades)
                        profitable_trades = len([t for t in self.trades if t.get('profit_percent', 0) > 0])
                        win_rate = (profitable_trades / total_trades) * 100 if total_trades > 0 else 0
                        
                        status_msg = f"📊 <b>تقرير أداء البوت</b>\n\nإجمالي الصفقات: {total_trades}\nالصفقات الرابحة: {profitable_trades}\nمعدل الربح: {win_rate:.2f}%\nالرصيد الحالي: ${total_balance:.2f}\nUSDT: {usdt_balance:.2f}\nBNB: {bnb_balance:.4f}"
                        self.send_notification(status_msg)
                
                # الانتظار للفترة التالية
                time.sleep(interval_minutes * 60)
                
            except Exception as e:
                error_msg = f"❌ خطأ غير متوقع في التشغيل: {e}"
                self.send_notification(error_msg)
                time.sleep(300)  # الانتظار 5 دقائق قبل إعادة المحاولة

# تشغيل البوت
if __name__ == "__main__":
    try:
        # الحصول على الرصيد الافتتاحي من متغير البيئة إذا كان موجوداً
        initial_balance = float(os.environ.get('INITIAL_BALANCE', 1000))
        
        bot = BNB_Trading_Bot(initial_balance=initial_balance)
        bot.run()
    except Exception as e:
        logger.error(f"فشل تشغيل البوت: {e}")
