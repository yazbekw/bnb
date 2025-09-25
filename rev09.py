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
import threading
import schedule
from flask import Flask, jsonify
import pytz

# ضبط توقيت الخادم إلى توقيت دمشق
damascus_tz = pytz.timezone('Asia/Damascus')
os.environ['TZ'] = 'Asia/Damascus'
if hasattr(time, 'tzset'):
    time.tzset()

# تحميل متغيرات البيئة
load_dotenv()

# إنشاء تطبيق Flask للرصد الصحي
app = Flask(__name__)

@app.route('/')
def health_check():
    return {'status': 'healthy', 'service': 'futures-trading-bot', 'timestamp': datetime.now(damascus_tz).isoformat()}

@app.route('/active_trades')
def active_trades():
    try:
        bot = FuturesTradingBot()
        return jsonify(list(bot.active_trades.values()))
    except Exception as e:
        return {'error': str(e)}

def run_flask_app():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

# إعداد logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('futures_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class PriceManager:
    def __init__(self, symbols):
        self.symbols = symbols
        self.prices = {}
        self.last_update = {}
        
    def update_prices(self):
        """تحديث الأسعار لجميع الرموز باستخدام REST API"""
        try:
            for symbol in self.symbols:
                try:
                    ticker = Client().futures_symbol_ticker(symbol=symbol)
                    if ticker and 'price' in ticker:
                        price = float(ticker['price'])
                        self.prices[symbol] = price
                        self.last_update[symbol] = time.time()
                        logger.debug(f"✅ تم تحديث سعر {symbol}: ${price}")
                except Exception as e:
                    logger.warning(f"⚠️ فشل تحديث سعر {symbol}: {e}")
                    continue
                    
            logger.info(f"✅ تم تحديث أسعار {len([p for p in self.prices if time.time() - self.last_update.get(p, 0) < 30])} رمز")
            return True
        except Exception as e:
            logger.error(f"❌ خطأ في تحديث الأسعار: {e}")
            return False

    def get_price(self, symbol):
        """جلب السعر الحالي للرمز"""
        try:
            # إذا كان السعر قديم (أكثر من 30 ثانية)، نقوم بتحديثه
            last_update = self.last_update.get(symbol, 0)
            if time.time() - last_update > 30:
                self.update_single_price(symbol)
                
            return self.prices.get(symbol)
        except Exception as e:
            logger.error(f"❌ خطأ في جلب سعر {symbol}: {e}")
            return None

    def update_single_price(self, symbol):
        """تحديث سعر رمز واحد فقط"""
        try:
            ticker = Client().futures_symbol_ticker(symbol=symbol)
            if ticker and 'price' in ticker:
                price = float(ticker['price'])
                self.prices[symbol] = price
                self.last_update[symbol] = time.time()
                return True
            return False
        except Exception as e:
            logger.error(f"❌ خطأ في تحديث سعر {symbol}: {e}")
            return False

    def is_connected(self):
        """التحقق من وجود أسعار حديثة"""
        current_time = time.time()
        recent_prices = [sym for sym in self.symbols 
                        if current_time - self.last_update.get(sym, 0) < 60]
        return len(recent_prices) > 0

class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.last_notification = time.time()

    def send_message(self, message, message_type='info'):
        """إرسال رسالة إلى Telegram مع منع التكرار"""
        try:
            # منع التكرار خلال 10 ثوان
            current_time = time.time()
            if current_time - self.last_notification < 10 and message_type != 'trade':
                return True
                
            self.last_notification = current_time
            
            url = f"{self.base_url}/sendMessage"
            payload = {
                'chat_id': self.chat_id, 
                'text': message, 
                'parse_mode': 'HTML'
            }
            
            response = requests.post(url, data=payload, timeout=10)
            if response.status_code == 200:
                logger.info(f"✅ تم إرسال إشعار Telegram: {message_type}")
                return True
            else:
                logger.error(f"❌ فشل إرسال Telegram: {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"❌ خطأ في إرسال Telegram: {e}")
            return False

class FuturesTradingBot:
    TRADING_SETTINGS = {
        'min_trade_size': 10,  # 10 دولار
        'max_trade_size': 50,
        'leverage': 10,  # رافعة 10x
        'margin_type': 'ISOLATED',
        'base_risk_pct': 0.002,
        'risk_reward_ratio': 2.0,
        'max_active_trades': 3,
        'rsi_overbought': 75,
        'rsi_oversold': 35,
        'data_interval': '15m',
        'rescan_interval_minutes': 5,
        'stop_loss_pct': 1.0,  # وقف خسارة 1%
        'take_profit_pct': 2.0,  # أخذ ربح 2%
        'trade_timeout_hours': 2,
        'price_update_interval': 1,  # تحديث الأسعار كل دقيقة
    }

    def __init__(self):
        self.api_key = os.environ.get('BINANCE_API_KEY')
        self.api_secret = os.environ.get('BINANCE_API_SECRET')
        self.telegram_token = os.environ.get('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = os.environ.get('TELEGRAM_CHAT_ID')

        if not all([self.api_key, self.api_secret]):
            raise ValueError("مفاتيح Binance مطلوبة")

        # استخدام Futures API
        self.client = Client(self.api_key, self.api_secret)
        self.notifier = TelegramNotifier(self.telegram_token, self.telegram_chat_id) if self.telegram_token and self.telegram_chat_id else None
        
        # أفضل 6 عملات للتداول
        self.symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "ADAUSDT", "DOTUSDT", "LINKUSDT"]
        self.active_trades = {}
        
        # تهيئة مدير الأسعار (بدون WebSocket)
        self.price_manager = PriceManager(self.symbols)
        self.start_price_updater()

        # تحميل الصفقات المفتوحة
        self.load_existing_trades()
        
        # إرسال رسالة بدء التشغيل
        if self.notifier:
            self.notifier.send_message(
                f"🚀 <b>بدء تشغيل بوت العقود الآجلة (بدون WebSocket)</b>\n"
                f"📊 العملات: {', '.join(self.symbols)}\n"
                f"💼 الرافعة: {self.TRADING_SETTINGS['leverage']}x\n"
                f"⏰ الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                'startup'
            )

    def start_price_updater(self):
        """بدء تحديث الأسعار بشكل دوري"""
        def price_update_thread():
            while True:
                try:
                    self.price_manager.update_prices()
                    time.sleep(self.TRADING_SETTINGS['price_update_interval'] * 60)  # التحويل إلى دقائق
                except Exception as e:
                    logger.error(f"❌ خطأ في تحديث الأسعار: {e}")
                    time.sleep(60)

        threading.Thread(target=price_update_thread, daemon=True).start()
        logger.info("✅ بدء تحديث الأسعار الدوري")

    def load_existing_trades(self):
        """تحميل الصفقات المفتوحة من العقود الآجلة"""
        try:
            # جلب المراكز المفتوحة
            positions = self.client.futures_account()['positions']
            
            open_positions = [p for p in positions if float(p['positionAmt']) != 0]
            
            for position in open_positions:
                symbol = position['symbol']
                if symbol in self.symbols:
                    quantity = float(position['positionAmt'])
                    if quantity > 0:  # صفقات شراء فقط
                        entry_price = float(position['entryPrice'])
                        leverage = float(position['leverage'])
                        
                        trade_data = {
                            'symbol': symbol,
                            'quantity': abs(quantity),
                            'entry_price': entry_price,
                            'leverage': leverage,
                            'timestamp': datetime.now(damascus_tz),
                            'status': 'open'
                        }
                        
                        self.active_trades[symbol] = trade_data
                        logger.info(f"✅ تم تحميل الصفقة: {symbol} - كمية: {quantity}")
                        
                        if self.notifier:
                            self.notifier.send_message(
                                f"📥 <b>صفقة مفتوحة محملة</b>\n"
                                f"العملة: {symbol}\n"
                                f"الكمية: {abs(quantity):.6f}\n"
                                f"سعر الدخول: ${entry_price:.4f}\n"
                                f"الرافعة: {leverage}x",
                                f'load_{symbol}'
                            )
            
            if not open_positions:
                logger.info("⚠️ لا توجد صفقات مفتوحة")
                
        except Exception as e:
            logger.error(f"❌ خطأ في تحميل الصفقات: {e}")

    def get_current_price(self, symbol):
        """جلب السعر الحالي باستخدام REST API"""
        try:
            price = self.price_manager.get_price(symbol)
            if price:
                return price
            
            # Fallback مباشر إلى REST API
            ticker = self.client.futures_symbol_ticker(symbol=symbol)
            if ticker and 'price' in ticker:
                price = float(ticker['price'])
                self.price_manager.prices[symbol] = price
                self.price_manager.last_update[symbol] = time.time()
                return price
            
            logger.warning(f"⚠️ لا يمكن جلب سعر {symbol}")
            return None
            
        except Exception as e:
            logger.error(f"❌ خطأ في جلب سعر {symbol}: {e}")
            return None

    def set_leverage(self, symbol, leverage):
        """ضبط الرافعة المالية"""
        try:
            self.client.futures_change_leverage(
                symbol=symbol, 
                leverage=leverage
            )
            logger.info(f"✅ ضبط الرافعة لـ {symbol} إلى {leverage}x")
            
            if self.notifier:
                self.notifier.send_message(
                    f"⚙️ <b>ضبط الرافعة</b>\nالعملة: {symbol}\nالرافعة: {leverage}x",
                    f'leverage_{symbol}'
                )
            return True
        except Exception as e:
            logger.error(f"❌ خطأ في ضبط الرافعة: {e}")
            return False

    def set_margin_type(self, symbol, margin_type):
        """ضبط نوع الهامش"""
        try:
            self.client.futures_change_margin_type(
                symbol=symbol,
                marginType=margin_type
            )
            logger.info(f"✅ ضبط الهامش لـ {symbol} إلى {margin_type}")
            return True
        except Exception as e:
            logger.error(f"❌ خطأ في ضبط الهامش: {e}")
            return False

    def get_historical_data(self, symbol, interval='15m', limit=50):
        """جلب البيانات التاريخية"""
        try:
            klines = self.client.futures_klines(
                symbol=symbol, 
                interval=interval, 
                limit=limit
            )
            
            data = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                'taker_buy_quote', 'ignore'
            ])
            
            data['close'] = data['close'].astype(float)
            data['volume'] = data['volume'].astype(float)
            data['high'] = data['high'].astype(float)
            data['low'] = data['low'].astype(float)
            
            return data
        except Exception as e:
            logger.error(f"❌ خطأ في جلب البيانات لـ {symbol}: {e}")
            return None

    def calculate_indicators(self, data):
        """حساب المؤشرات الفنية"""
        try:
            df = data.copy()
            if len(df) < 20:
                return df

            # المتوسطات المتحركة
            df['ema8'] = df['close'].ewm(span=8, adjust=False).mean()
            df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
            
            # RSI
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)
            avg_gain = gain.rolling(14).mean()
            avg_loss = loss.rolling(14).mean()
            rs = avg_gain / (avg_loss + 1e-10)
            df['rsi'] = 100 - (100 / (1 + rs))
            
            return df
        except Exception as e:
            logger.error(f"❌ خطأ في حساب المؤشرات: {e}")
            return data

    def analyze_symbol(self, symbol):
        """تحليل الرمز لإشارات التداول"""
        try:
            data = self.get_historical_data(symbol, self.TRADING_SETTINGS['data_interval'])
            if data is None or len(data) < 20:
                return False, {}

            data = self.calculate_indicators(data)
            latest = data.iloc[-1]
            previous = data.iloc[-2]

            # تقاطع المتوسطات (إشارة شراء)
            buy_signal = (
                latest['ema8'] > latest['ema21'] and 
                previous['ema8'] <= previous['ema21']
            )

            # RSI في منطقة مناسبة
            rsi_ok = (
                self.TRADING_SETTINGS['rsi_oversold'] < latest['rsi'] < 
                self.TRADING_SETTINGS['rsi_overbought']
            )

            signal_strength = 0
            if buy_signal:
                signal_strength += 60
            if rsi_ok:
                signal_strength += 40

            details = {
                'signal_strength': signal_strength,
                'ema8': latest['ema8'],
                'ema21': latest['ema21'],
                'rsi': latest['rsi'],
                'price': latest['close'],
                'buy_signal': buy_signal,
                'rsi_ok': rsi_ok
            }

            return signal_strength >= 60, details

        except Exception as e:
            logger.error(f"❌ خطأ في تحليل {symbol}: {e}")
            return False, {}

    def get_symbol_precision(self, symbol):
        """جلب دقة الكمية للسعر"""
        try:
            info = self.client.futures_exchange_info()
            symbol_info = next((s for s in info['symbols'] if s['symbol'] == symbol), None)
            
            if symbol_info:
                lot_size = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
                if lot_size:
                    step_size = float(lot_size['stepSize'])
                    precision = int(round(-np.log10(step_size)))
                    return {'step_size': step_size, 'precision': precision}
            
            return {'step_size': 0.001, 'precision': 3}
        except Exception as e:
            logger.error(f"❌ خطأ في جلب الدقة: {e}")
            return {'step_size': 0.001, 'precision': 3}

    def execute_trade(self, symbol):
        """تنفيذ صفقة جديدة"""
        try:
            if len(self.active_trades) >= self.TRADING_SETTINGS['max_active_trades']:
                logger.info(f"⏸️ الحد الأقصى للصفقات ({self.TRADING_SETTINGS['max_active_trades']})")
                return False

            # التحليل الفني
            should_trade, analysis = self.analyze_symbol(symbol)
            if not should_trade:
                return False

            current_price = self.get_current_price(symbol)
            if current_price is None:
                return False

            # ضبط الرافعة والهامش
            self.set_leverage(symbol, self.TRADING_SETTINGS['leverage'])
            self.set_margin_type(symbol, self.TRADING_SETTINGS['margin_type'])

            # حساب الكمية
            trade_size_usd = self.TRADING_SETTINGS['min_trade_size']
            quantity = trade_size_usd / current_price
            
            # تقريب الكمية
            precision_info = self.get_symbol_precision(symbol)
            quantity = round(quantity - (quantity % precision_info['step_size']), precision_info['precision'])

            if quantity <= 0:
                logger.error(f"❌ كمية غير صالحة: {quantity}")
                return False

            # تنفيذ الصفقة
            order = self.client.futures_create_order(
                symbol=symbol,
                side='BUY',
                type='MARKET',
                quantity=quantity
            )

            if order['status'] == 'FILLED':
                avg_price = float(order['avgPrice'])
                
                # حفظ بيانات الصفقة
                trade_data = {
                    'symbol': symbol,
                    'quantity': quantity,
                    'entry_price': avg_price,
                    'leverage': self.TRADING_SETTINGS['leverage'],
                    'timestamp': datetime.now(damascus_tz),
                    'status': 'open',
                    'order_id': order['orderId']
                }
                
                self.active_trades[symbol] = trade_data

                # إرسال إشعار النجاح
                if self.notifier:
                    self.notifier.send_message(
                        f"🚀 <b>فتح صفقة جديدة</b>\n"
                        f"العملة: {symbol}\n"
                        f"السعر: ${avg_price:.4f}\n"
                        f"الكمية: {quantity:.6f}\n"
                        f"الحجم: ${trade_size_usd:.2f}\n"
                        f"الرافعة: {self.TRADING_SETTINGS['leverage']}x\n"
                        f"القوة: {analysis['signal_strength']}%",
                        f'trade_open_{symbol}'
                    )

                logger.info(f"✅ فتح صفقة {symbol} بسعر {avg_price}")
                return True

            return False

        except Exception as e:
            logger.error(f"❌ خطأ في تنفيذ صفقة {symbol}: {e}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>فشل فتح صفقة</b>\nالعملة: {symbol}\nالخطأ: {str(e)}",
                    f'error_{symbol}'
                )
            return False

    def manage_trades(self):
        """إدارة الصفقات المفتوحة"""
        for symbol, trade in list(self.active_trades.items()):
            try:
                current_price = self.get_current_price(symbol)
                if current_price is None:
                    continue

                # حساب الربح/الخسارة
                pnl_percent = ((current_price - trade['entry_price']) / trade['entry_price']) * 100
                trade_duration = (datetime.now(damascus_tz) - trade['timestamp']).total_seconds() / 3600

                # التحقق من وقف الخسارة
                if pnl_percent <= -self.TRADING_SETTINGS['stop_loss_pct']:
                    self.close_trade(symbol, current_price, 'وقف الخسارة')
                    continue

                # التحقق من أخذ الربح
                if pnl_percent >= self.TRADING_SETTINGS['take_profit_pct']:
                    self.close_trade(symbol, current_price, 'أخذ الربح')
                    continue

                # انتهاء الوقت (2 ساعة كحد أقصى)
                if trade_duration >= self.TRADING_SETTINGS['trade_timeout_hours']:
                    self.close_trade(symbol, current_price, 'انتهاء الوقت')
                    continue

                # تحديث حالة الصفقة كل 5 دقائق
                if self.notifier and int(trade_duration * 60) % 5 == 0:
                    self.notifier.send_message(
                        f"📊 <b>تتبع الصفقة</b>\n"
                        f"العملة: {symbol}\n"
                        f"السعر الحالي: ${current_price:.4f}\n"
                        f"الربح/الخسارة: {pnl_percent:+.2f}%\n"
                        f"المدة: {trade_duration:.1f} ساعة",
                        f'track_{symbol}'
                    )

            except Exception as e:
                logger.error(f"❌ خطأ في إدارة صفقة {symbol}: {e}")

    def close_trade(self, symbol, exit_price, reason):
        """إغلاق الصفقة"""
        try:
            trade = self.active_trades[symbol]
            quantity = trade['quantity']

            # إغلاق الصفقة
            order = self.client.futures_create_order(
                symbol=symbol,
                side='SELL',
                type='MARKET',
                quantity=quantity
            )

            if order['status'] == 'FILLED':
                # حساب الربح/الخسارة
                pnl = (exit_price - trade['entry_price']) * quantity
                pnl_percent = ((exit_price - trade['entry_price']) / trade['entry_price']) * 100

                # إرسال إشعار الإغلاق
                if self.notifier:
                    emoji = "✅" if pnl > 0 else "❌"
                    self.notifier.send_message(
                        f"{emoji} <b>إغلاق الصفقة</b>\n"
                        f"العملة: {symbol}\n"
                        f"السبب: {reason}\n"
                        f"سعر الخروج: ${exit_price:.4f}\n"
                        f"الربح/الخسارة: ${pnl:.2f} ({pnl_percent:+.2f}%)\n"
                        f"المدة: {(datetime.now(damascus_tz) - trade['timestamp']).total_seconds()/60:.1f} دقيقة",
                        f'trade_close_{symbol}'
                    )

                # حذف الصفقة من القائمة
                del self.active_trades[symbol]
                logger.info(f"🔚 إغلاق {symbol}: {reason} - ربح: ${pnl:.2f}")

                return True

            return False

        except Exception as e:
            logger.error(f"❌ خطأ في إغلاق صفقة {symbol}: {e}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>فشل إغلاق الصفقة</b>\nالعملة: {symbol}\nالخطأ: {str(e)}",
                    f'error_close_{symbol}'
                )
            return False

    def scan_opportunities(self):
        """مسح الفرص المتاحة"""
        try:
            logger.info("🔍 مسح الفرص المتاحة...")
            
            for symbol in self.symbols:
                if symbol not in self.active_trades:
                    should_trade, analysis = self.analyze_symbol(symbol)
                    
                    if should_trade:
                        logger.info(f"🎯 إشارة شراء لـ {symbol} (قوة: {analysis['signal_strength']}%)")
                        
                        if self.notifier:
                            self.notifier.send_message(
                                f"🎯 <b>إشارة شراء</b>\n"
                                f"العملة: {symbol}\n"
                                f"السعر: ${analysis['price']:.4f}\n"
                                f"EMA8: ${analysis['ema8']:.4f}\n"
                                f"EMA21: ${analysis['ema21']:.4f}\n"
                                f"RSI: {analysis['rsi']:.1f}\n"
                                f"القوة: {analysis['signal_strength']}%",
                                f'signal_{symbol}'
                            )
                        
                        # تنفيذ الصفقة بعد تحليل جميع العوامل
                        time.sleep(1)
                        self.execute_trade(symbol)
                        break  # تنفيذ صفقة واحدة فقط في كل دورة

        except Exception as e:
            logger.error(f"❌ خطأ في مسح الفرص: {e}")

    def run_bot(self):
        """تشغيل البوت الرئيسي"""
        logger.info("🚀 بدء تشغيل بوت العقود الآجلة (بدون WebSocket)")
        
        # جدولة المهام
        schedule.every(self.TRADING_SETTINGS['rescan_interval_minutes']).minutes.do(self.scan_opportunities)
        schedule.every(1).minutes.do(self.manage_trades)
        schedule.every(5).minutes.do(self.price_manager.update_prices)  # تحديث الأسعار كل 5 دقائق
        
        # التشغيل الفوري للمسح الأول
        self.scan_opportunities()
        self.price_manager.update_prices()
        
        # الحلقة الرئيسية
        while True:
            try:
                schedule.run_pending()
                time.sleep(1)
            except Exception as e:
                logger.error(f"❌ خطأ في الحلقة الرئيسية: {e}")
                time.sleep(60)

def main():
    try:
        # بدء خادم Flask في thread منفصل
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        
        # بدء البوت
        bot = FuturesTradingBot()
        bot.run_bot()
        
    except Exception as e:
        logger.error(f"❌ خطأ فادح: {e}")
        # محاولة إرسال إشعار خطأ إذا كان البوت يعمل
        try:
            bot = FuturesTradingBot()
            if bot.notifier:
                bot.notifier.send_message(f"❌ <b>إيقاف البوت</b>\nخطأ فادح: {e}", 'fatal_error')
        except:
            pass

if __name__ == "__main__":
    # التحقق من المتغيرات البيئية
    required_env_vars = ['BINANCE_API_KEY', 'BINANCE_API_SECRET']
    missing_vars = [var for var in required_env_vars if not os.getenv(var)]
    
    if missing_vars:
        print(f"❌ متغيرات بيئية مفقودة: {missing_vars}")
        exit(1)
    
    main()
