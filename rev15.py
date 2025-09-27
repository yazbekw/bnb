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
        bot = FuturesTradingBot.get_instance()
        if bot:
            active = list(bot.active_trades.values())
            return jsonify(active)
        return jsonify([])
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
    def __init__(self, symbols, client):
        self.symbols = symbols
        self.client = client
        self.prices = {}
        self.last_update = {}
        

    def update_prices(self):
        """تحديث الأسعار لجميع الرموز باستخدام طلب واحد لجميع التيكرز"""
        try:
            success_count = 0
            all_tickers = self.client.futures_ticker()
            logger.info(f"عدد التيكرز المجلوبة: {len(all_tickers) if all_tickers else 0}")
        
            # إضافة تصحيح للرموز
            logger.info(f"الرموز المطلوبة: {self.symbols}")
        
            if not all_tickers:
                logger.warning("⚠️ فشل جلب التيكرز، جاري المحاولة الفردية...")
                return self.fallback_price_update()
            
            # تسجيل أول 10 رموز للتdebug
            sample_symbols = [ticker['symbol'] for ticker in all_tickers[:10]]
            logger.info(f"عينة من الرموز المستلمة: {sample_symbols}")
        
            for ticker in all_tickers:
                symbol = ticker.get('symbol')
                if symbol in self.symbols:
                    price = float(ticker.get('price', 0))
                    if price > 0:
                        self.prices[symbol] = price
                        self.last_update[symbol] = time.time()
                        success_count += 1
                        logger.debug(f"✅ تم تحديث سعر {symbol}: ${price}")
        
            logger.info(f"✅ تم تحديث أسعار {success_count} من {len(self.symbols)} رمز")
        
            # إذا لم يتم تحديث أي رمز، جرب Fallback فوراً
            if success_count == 0:
                logger.warning("❌ لم يتم العثور على أي رمز في التيكرز، جاري Fallback...")
                return self.fallback_price_update()
            
            return True
            
        except Exception as e:
            logger.error(f"❌ خطأ في تحديث الأسعار: {str(e)}")
            return self.fallback_price_update()

    def fallback_price_update(self):
        """Fallback للجلب الفردي عند الفشل"""
        success_count = 0
        for symbol in self.symbols:
            try:
                ticker = self.client.futures_symbol_ticker(symbol=symbol)
                price = float(ticker.get('price', 0))
                if price > 0:
                    self.prices[symbol] = price
                    self.last_update[symbol] = time.time()
                    success_count += 1
                    logger.info(f"✅ Fallback: تم تحديث سعر {symbol}: ${price}")
                else:
                    logger.warning(f"⚠️ Fallback: سعر غير صالح لـ {symbol}")
            except Exception as e:
                logger.error(f"❌ Fallback فشل لـ {symbol}: {str(e)}")
    
        if success_count > 0:
            logger.info(f"✅ Fallback: تم تحديث أسرار {success_count} من {len(self.symbols)} رمز")
            return True
    
        logger.error("❌ فشل تحديث الأسعار بعد كل المحاولات")
        return False

    def get_price(self, symbol):
        """جلب السعر الحالي للرمز"""
        try:
            last_update = self.last_update.get(symbol, 0)
            if time.time() - last_update > 120:  # تحديث كل دقيقتين
                if not self.update_prices():
                    logger.warning(f"⚠️ فشل تحديث الأسعار لـ {symbol}")
                    # محاولة جلب فردي
                    try:
                        ticker = self.client.futures_symbol_ticker(symbol=symbol)
                        price = float(ticker.get('price', 0))
                        if price > 0:
                            self.prices[symbol] = price
                            self.last_update[symbol] = time.time()
                            logger.debug(f"✅ Individual fetch: تم تحديث سعر {symbol}: ${price}")
                            return price
                        logger.error(f"❌ سعر غير صالح لـ {symbol}")
                        return None
                    except Exception as e:
                        logger.error(f"❌ خطأ في جلب سعر فردي لـ {symbol}: {str(e)}")
                        return None
            return self.prices.get(symbol)
        except Exception as e:
            logger.error(f"❌ خطأ في جلب سعر {symbol}: {str(e)}")
            return None

    def is_connected(self):
        """التحقق من وجود أسعار حديثة"""
        current_time = time.time()
        recent_prices = [sym for sym in self.symbols 
                        if current_time - self.last_update.get(sym, 0) < 120]
        return len(recent_prices) > 0

class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.last_notification = time.time()
        self.notification_types = {}

    def send_message(self, message, message_type='info'):
        """إرسال رسالة إلى Telegram مع منع التكرار"""
        try:
            # منع التكرار خلال 60 ثانية لنفس النوع
            current_time = time.time()
            last_sent = self.notification_types.get(message_type, 0)
            
            if current_time - last_sent < 60 and message_type != 'trade':
                return True
                
            self.notification_types[message_type] = current_time
            
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
                logger.error(f"❌ فشل إرسال Telegram: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"❌ خطأ في إرسال Telegram: {e}")
            return False

class FuturesTradingBot:
    _instance = None
    TRADING_SETTINGS = {
        'min_trade_size': 10,
        'max_trade_size': 30,
        'leverage': 10,
        'margin_type': 'ISOLATED',
        'base_risk_pct': 0.002,
        'risk_reward_ratio': 2.0,
        'max_active_trades': 5,
        'rsi_overbought': 75,
        'rsi_oversold': 35,
        'data_interval': '5m',
        'rescan_interval_minutes': 4,
        'trade_timeout_hours': 0.5,
        'price_update_interval': 2,
        'trail_trigger_pct': 0.5,
        'trail_offset_pct': 0.5,
        'macd_signal_threshold': 0.001,
        'require_macd_confirmation': True,
    }

    @classmethod
    def get_instance(cls):
        return cls._instance

    def __init__(self):
        if FuturesTradingBot._instance is not None:
            raise Exception("هذه الفئة تستخدم نمط Singleton")
            
        self.api_key = os.environ.get('BINANCE_API_KEY')
        self.api_secret = os.environ.get('BINANCE_API_SECRET')
        self.telegram_token = os.environ.get('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = os.environ.get('TELEGRAM_CHAT_ID')

        if not all([self.api_key, self.api_secret]):
            raise ValueError("مفاتيح Binance مطلوبة")

        try:
            self.client = Client(self.api_key, self.api_secret)
            self.test_api_connection()
        except Exception as e:
            logger.error(f"❌ فشل تهيئة العميل: {e}")
            raise

        self.notifier = None
        if self.telegram_token and self.telegram_chat_id:
            try:
                self.notifier = TelegramNotifier(self.telegram_token, self.telegram_chat_id)
                self.notifier.send_message("🔧 <b>تهيئة البوت</b>\nجاري بدء التشغيل...", 'init')
            except Exception as e:
                logger.error(f"❌ فشل تهيئة Telegram: {e}")
                self.notifier = None
        else:
            logger.warning("⚠️ مفاتيح Telegram غير موجودة - تعطيل الإشعارات")
        
        self.symbols = ["ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT"]
        self.verify_symbols_availability()
        
        self.symbol_settings = {
            "ETHUSDT": {'stop_loss_pct': 1.0, 'take_profit_pct': 1.5},
            "BNBUSDT": {'stop_loss_pct': 1.0, 'take_profit_pct': 1.5},
            "SOLUSDT": {'stop_loss_pct': 1.0, 'take_profit_pct': 2.5},
            "DOGEUSDT": {'stop_loss_pct': 1.0, 'take_profit_pct': 3.0},
        }
        
        self.active_trades = {}
        
        self.price_manager = PriceManager(self.symbols, self.client)
        
        self.load_existing_trades()
        
        self.start_price_updater()
        
        if self.active_trades and self.notifier:
            threading.Timer(10, self.test_notifications).start()
        
        self.send_startup_message()
        
        self.debug_bot_status()
        
        FuturesTradingBot._instance = self
        logger.info("✅ تم تهيئة البوت بنجاح")

    def verify_symbols_availability(self):
        """التحقق من توفر الرموز في سوق العقود"""
        try:
            exchange_info = self.client.futures_exchange_info()
            available_symbols = [s['symbol'] for s in exchange_info['symbols']]
        
            logger.info(f"الرموز المتاحة في العقود: {len(available_symbols)}")
        
            for symbol in self.symbols:
                if symbol in available_symbols:
                    logger.info(f"✅ {symbol} متوفر في العقود")
                else:
                    logger.warning(f"⚠️ {symbol} غير متوفر في العقود")
                
            # تصحيح الرموز إذا لزم الأمر
            valid_symbols = [s for s in self.symbols if s in available_symbols]
            if len(valid_symbols) != len(self.symbols):
                logger.warning(f"⚠️ تصحيح الرموز من {self.symbols} إلى {valid_symbols}")
                self.symbols = valid_symbols
            
        except Exception as e:
            logger.error(f"❌ خطأ في التحقق من الرموز: {e}")

    def debug_bot_status(self):
        logger.info("=== حالة البوت للتصحيح ===")
        logger.info(f"عدد الصفقات النشطة (عقود): {len(self.active_trades)}")
        logger.info(f"Telegram Notifier: {'موجود' if self.notifier else 'غير موجود'}")
        
        for symbol, trade in self.active_trades.items():
            logger.info(f"صفقة عقود {symbol}: {trade}")
        
        if self.notifier:
            try:
                self.notifier.send_message(
                    "🔧 <b>اختبار الإشعارات</b>\nالبوت يعمل ويتم التتبع",
                    'debug_test'
                )
                logger.info("✅ اختبار الإشعارات ناجح")
            except Exception as e:
                logger.error(f"❌ فشل اختبار الإشعارات: {e}")

    def test_notifications(self):
        logger.info("🔧 اختبار الإشعارات للصفقات المحملة")
        for symbol in self.active_trades:
            if self.notifier:
                self.notifier.send_message(
                    f"🔧 <b>اختبار التتبع (عقود)</b>\nالعملة: {symbol}\nتم تحميل الصفقة بنجاح",
                    f'test_futures_{symbol}'
                )

    def test_api_connection(self):
        try:
            server_time = self.client.futures_time()
            logger.info(f"✅ اتصال Binance API نشط - وقت الخادم: {server_time}")
            return True
        except Exception as e:
            logger.error(f"❌ فشل الاتصال بـ Binance API: {e}")
            if "Invalid API-key" in str(e):
                logger.error("❌ مفتاح API غير صحيح")
            elif "Signature" in str(e):
                logger.error("❌ سر API غير صحيح")
            elif "permissions" in str(e):
                logger.error("❌ عدم وجود صلاحيات كافية")
            raise

    def send_startup_message(self):
        if self.notifier:
            try:
                self.notifier.send_message(
                    f"🚀 <b>بدء تشغيل بوت العقود الآجلة</b>\n"
                    f"📊 العملات: {', '.join(self.symbols)}\n"
                    f"💼 الرافعة: {self.TRADING_SETTINGS['leverage']}x\n"
                    f"⏰ الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"✅ الصفقات النشطة: {len(self.active_trades)}\n"
                    f"🔧 المؤشرات: EMA5/13, RSI, MACD",
                    'startup'
                )
            except Exception as e:
                logger.error(f"❌ فشل إرسال رسالة البدء: {e}")

    def start_price_updater(self):
        def price_update_thread():
            while True:
                try:
                    self.price_manager.update_prices()
                    time.sleep(self.TRADING_SETTINGS['price_update_interval'] * 60)
                except Exception as e:
                    logger.error(f"❌ خطأ في تحديث الأسعار: {str(e)}")
                    time.sleep(30)

        threading.Thread(target=price_update_thread, daemon=True).start()
        logger.info("✅ بدء تحديث الأسعار الدوري")

    def load_existing_trades(self):
        try:
            account_info = self.client.futures_account()
            positions = account_info['positions']
            
            open_positions = [p for p in positions if float(p['positionAmt']) != 0]
            
            logger.info(f"🔍 العثور على {len(open_positions)} مركز مفتوح في العقود")
            
            for position in open_positions:
                symbol = position['symbol']
                if symbol in self.symbols:
                    quantity = float(position['positionAmt'])
                    if quantity != 0:
                        entry_price = float(position['entryPrice'])
                        leverage = float(position['leverage'])
                        side = "LONG" if quantity > 0 else "SHORT"
                        
                        trade_data = {
                            'symbol': symbol,
                            'quantity': abs(quantity),
                            'entry_price': entry_price,
                            'leverage': leverage,
                            'side': side,
                            'timestamp': datetime.now(damascus_tz),
                            'status': 'open',
                            'last_notification': datetime.now(damascus_tz),
                            'trade_type': 'futures',
                            'highest_price': entry_price if side == 'LONG' else entry_price,
                            'lowest_price': entry_price if side == 'SHORT' else entry_price,
                            'trail_started': False
                        }
                        
                        self.active_trades[symbol] = trade_data
                        logger.info(f"✅ تم تحميل صفقة عقود: {symbol} - {side} - كمية: {abs(quantity)}")
                        
                        if self.notifier:
                            self.notifier.send_message(
                                f"📥 <b>صفقة عقود مفتوحة محملة</b>\n"
                                f"العملة: {symbol}\n"
                                f"الاتجاه: {side}\n"
                                f"الكمية: {abs(quantity):.6f}\n"
                                f"سعر الدخول: ${entry_price:.4f}\n"
                                f"الرافعة: {leverage}x",
                                f'load_futures_{symbol}'
                            )
            
            if not open_positions:
                logger.info("⚠️ لا توجد صفقات مفتوحة في العقود")
                
        except Exception as e:
            logger.error(f"❌ خطأ في تحميل الصفقات العقود: {e}")

    def get_current_price(self, symbol):
        try:
            for _ in range(3):
                price = self.price_manager.get_price(symbol)
                if price:
                    return price
                logger.warning(f"⚠️ فشل جلب سعر {symbol}، جاري إعادة المحاولة...")
                self.price_manager.update_prices()
                time.sleep(1)
            logger.error(f"❌ فشل جلب سعر {symbol} بعد المحاولات")
            return None
        except Exception as e:
            logger.error(f"❌ خطأ في جلب سعر {symbol}: {e}")
            return None

    def set_leverage(self, symbol, leverage):
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
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
        try:
            self.client.futures_change_margin_type(symbol=symbol, marginType=margin_type)
            logger.info(f"✅ ضبط الهامش لـ {symbol} إلى {margin_type}")
            return True
        except Exception as e:
            if "No need to change margin type" in str(e):
                logger.info(f"ℹ️ نوع الهامش مضبوط مسبقاً لـ {symbol}")
                return True
            logger.error(f"❌ خطأ في ضبط الهامش: {e}")
            return False

    def get_historical_data(self, symbol, interval='15m', limit=50):
        try:
            klines = self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
            
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
        try:
            df = data.copy()
            if len(df) < 26:  # زيادة الحد الأدنى لـ MACD
                return df

            # المؤشرات الحالية
            df['ema5'] = df['close'].ewm(span=5, adjust=False).mean()
            df['ema13'] = df['close'].ewm(span=13, adjust=False).mean()
            
            # حساب RSI
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)
            avg_gain = gain.rolling(14).mean()
            avg_loss = loss.rolling(14).mean()
            rs = avg_gain / (avg_loss + 1e-10)
            df['rsi'] = 100 - (100 / (1 + rs))
            
            # حساب MACD
            exp1 = df['close'].ewm(span=12, adjust=False).mean()
            exp2 = df['close'].ewm(span=26, adjust=False).mean()
            df['macd'] = exp1 - exp2
            df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
            df['macd_histogram'] = df['macd'] - df['macd_signal']
            
            return df
        except Exception as e:
            logger.error(f"❌ خطأ في حساب المؤشرات: {e}")
            return data

    def analyze_symbol(self, symbol):
        try:
            data = self.get_historical_data(symbol, self.TRADING_SETTINGS['data_interval'])
            if data is None or len(data) < 26:
                return False, {}, None

            data = self.calculate_indicators(data)
            latest = data.iloc[-1]
            previous = data.iloc[-2]

            # الإشارات الأساسية (EMA)
            buy_signal = (latest['ema5'] > latest['ema13'] and previous['ema5'] <= previous['ema13'])
            sell_signal = (latest['ema5'] < latest['ema13'] and previous['ema5'] >= previous['ema13'])

            # تحسين RSI
            rsi_buy_ok = latest['rsi'] < self.TRADING_SETTINGS['rsi_oversold'] + 10
            rsi_sell_ok = latest['rsi'] > self.TRADING_SETTINGS['rsi_overbought'] - 10

            # إشارات MACD
            macd_buy_signal = (latest['macd'] > latest['macd_signal'] and previous['macd'] <= previous['macd_signal'])
            macd_sell_signal = (latest['macd'] < latest['macd_signal'] and previous['macd'] >= previous['macd_signal'])
            
            # MACD فوق/تحت الصفر
            macd_above_zero = latest['macd'] > 0
            macd_below_zero = latest['macd'] < 0

            signal_strength = 0
            direction = None

            # تقييم قوة الإشارة مع MACD
            if buy_signal:
                signal_strength += 30
            if sell_signal:
                signal_strength += 30
                
            if rsi_buy_ok and buy_signal:
                signal_strength += 20
            if rsi_sell_ok and sell_signal:
                signal_strength += 20
                
            if macd_buy_signal and macd_above_zero:
                signal_strength += 25
            if macd_sell_signal and macd_below_zero:
                signal_strength += 25

            # تحديد الاتجاه النهائي
            if (buy_signal and rsi_buy_ok and macd_buy_signal and macd_above_zero):
                direction = 'LONG'
                signal_strength = min(signal_strength + 10, 100)
            elif (sell_signal and rsi_sell_ok and macd_sell_signal and macd_below_zero):
                direction = 'SHORT'
                signal_strength = min(signal_strength + 10, 100)

            details = {
                'signal_strength': signal_strength,
                'ema5': latest['ema5'],
                'ema13': latest['ema13'],
                'rsi': latest['rsi'],
                'macd': latest['macd'],
                'macd_signal': latest['macd_signal'],
                'macd_histogram': latest['macd_histogram'],
                'price': latest['close'],
                'buy_signal': buy_signal,
                'sell_signal': sell_signal,
                'macd_buy_signal': macd_buy_signal,
                'macd_sell_signal': macd_sell_signal,
                'direction': direction
            }

            return signal_strength >= 70, details, direction

        except Exception as e:
            logger.error(f"❌ خطأ في تحليل {symbol}: {e}")
            return False, {}, None

    def get_futures_precision(self, symbol):
        try:
            info = self.client.futures_exchange_info()
            symbol_info = next((s for s in info['symbols'] if s['symbol'] == symbol), None)
            
            if symbol_info:
                lot_size = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
                min_notional = next((f for f in symbol_info['filters'] if f['filterType'] == 'MIN_NOTIONAL'), None)
                
                precision_info = {
                    'step_size': float(lot_size['stepSize']) if lot_size else 0.001,
                    'precision': int(round(-np.log10(float(lot_size['stepSize'])))) if lot_size else 3,
                    'min_qty': float(lot_size['minQty']) if lot_size else 0.001,
                    'min_notional': float(min_notional['notional']) if min_notional else 5.0
                }
                return precision_info
            
            return {'step_size': 0.001, 'precision': 3, 'min_qty': 0.001, 'min_notional': 5.0}
        except Exception as e:
            logger.error(f"❌ خطأ في جلب دقة العقود: {e}")
            return {'step_size': 0.001, 'precision': 3, 'min_qty': 0.001, 'min_notional': 5.0}

    def execute_futures_trade(self, symbol, direction, signal_strength, analysis):
        try:
            if len(self.active_trades) >= self.TRADING_SETTINGS['max_active_trades']:
                logger.info(f"⏸️ الحد الأقصى للصفقات العقود ({self.TRADING_SETTINGS['max_active_trades']}) لـ {symbol}")
                if self.notifier:
                    self.notifier.send_message(
                        f"⚠️ <b>تم إلغاء صفقة</b>\nالعملة: {symbol}\nالسبب: الحد الأقصى للصفقات النشطة ({self.TRADING_SETTINGS['max_active_trades']})",
                        f'cancel_futures_{symbol}'
                    )
                return False

            if analysis['signal_strength'] < 70 or analysis['direction'] != direction:
                logger.info(f"❌ إشارة غير صالحة لـ {symbol}: القوة {analysis['signal_strength']}%, الاتجاه {analysis['direction']}")
                return False

            current_price = self.get_current_price(symbol)
            if current_price is None:
                logger.error(f"❌ فشل جلب السعر لـ {symbol}")
                return False

            self.set_leverage(symbol, self.TRADING_SETTINGS['leverage'])
            self.set_margin_type(symbol, self.TRADING_SETTINGS['margin_type'])

            trade_size_usd = self.TRADING_SETTINGS['min_trade_size'] + (signal_strength / 100) * (self.TRADING_SETTINGS['max_trade_size'] - self.TRADING_SETTINGS['min_trade_size'])

            precision_info = self.get_futures_precision(symbol)
            min_required = max(precision_info['min_notional'], precision_info['min_qty'] * current_price)

            if trade_size_usd < min_required:
                trade_size_usd = min_required
                logger.info(f"📏 تعديل حجم الصفقة لـ {symbol} إلى ${trade_size_usd:.2f} لتلبية الحد الأدنى")

            if trade_size_usd > self.TRADING_SETTINGS['max_trade_size']:
                logger.warning(f"⚠️ حجم الصفقة {trade_size_usd:.2f} يتجاوز الحد الأقصى، تخطي {symbol}")
                return False

            quantity = trade_size_usd / current_price
            step_size = precision_info['step_size']
            precision = precision_info['precision']
            quantity = max(precision_info['min_qty'], round(quantity / step_size) * step_size)
            quantity = round(quantity, precision)

            if quantity <= 0:
                logger.error(f"❌ كمية غير صالحة: {quantity} لـ {symbol}, min_qty: {precision_info['min_qty']}, min_notional: {precision_info['min_notional']}, trade_size_usd: {trade_size_usd}")
                if self.notifier:
                    self.notifier.send_message(
                        f"❌ <b>فشل فتح صفقة</b>\nالعملة: {symbol}\nالسبب: كمية غير صالحة ({quantity})",
                        f'error_quantity_{symbol}'
                    )
                return False

            side = Client.SIDE_BUY if direction == 'LONG' else Client.SIDE_SELL

            order = self.client.futures_create_order(
                symbol=symbol,
                side=side,
                type=Client.ORDER_TYPE_MARKET,
                quantity=quantity
            )

            if order['status'] == 'FILLED':
                avg_price = float(order['avgPrice'])
                
                trade_data = {
                    'symbol': symbol,
                    'quantity': quantity,
                    'entry_price': avg_price,
                    'leverage': self.TRADING_SETTINGS['leverage'],
                    'side': direction,
                    'timestamp': datetime.now(damascus_tz),
                    'status': 'open',
                    'order_id': order['orderId'],
                    'last_notification': datetime.now(damascus_tz),
                    'trade_type': 'futures',
                    'highest_price': avg_price if direction == 'LONG' else avg_price,
                    'lowest_price': avg_price if direction == 'SHORT' else avg_price,
                    'trail_started': False
                }
                
                self.active_trades[symbol] = trade_data

                if self.notifier:
                    self.notifier.send_message(
                        f"🚀 <b>فتح صفقة عقود جديدة مع MACD</b>\n"
                        f"العملة: {symbol}\n"
                        f"الاتجاه: {direction}\n"
                        f"السعر: ${avg_price:.4f}\n"
                        f"الكمية: {quantity:.6f}\n"
                        f"الحجم: ${trade_size_usd:.2f}\n"
                        f"الرافعة: {self.TRADING_SETTINGS['leverage']}x\n"
                        f"القوة: {signal_strength}%\n"
                        f"EMA5/13: {analysis['ema5']:.4f}/{analysis['ema13']:.4f}\n"
                        f"RSI: {analysis['rsi']:.1f}\n"
                        f"MACD: {analysis['macd']:.4f}",
                        f'trade_open_futures_{symbol}'
                    )

                logger.info(f"✅ فتح صفقة عقود {symbol} {direction} بسعر {avg_price}")
                return True

            logger.info(f"❌ فشل تنفيذ صفقة عقود {symbol}: حالة الأمر {order['status']}")
            return False

        except Exception as e:
            logger.error(f"❌ خطأ في تنفيذ صفقة عقود {symbol}: {e}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>فشل فتح صفقة عقود</b>\nالعملة: {symbol}\nالخطأ: {str(e)}",
                    f'error_futures_{symbol}'
                )
            return False

    def manage_trades(self):
        self.manage_futures_trades()

    def manage_futures_trades(self):
        if not self.active_trades:
            logger.info("🔍 لا توجد صفقات نشطة في العقود")
            return
        
        logger.info(f"🔍 إدارة {len(self.active_trades)} صفقة عقود نشطة")
        
        for symbol, trade in list(self.active_trades.items()):
            try:
                current_price = self.get_current_price(symbol)
                if current_price is None:
                    continue

                if trade['side'] == 'LONG':
                    pnl_percent = ((current_price - trade['entry_price']) / trade['entry_price']) * 100
                    if current_price > trade['highest_price']:
                        trade['highest_price'] = current_price
                    if not trade['trail_started'] and pnl_percent >= self.TRADING_SETTINGS['trail_trigger_pct']:
                        trade['trail_started'] = True
                        trade['trail_price'] = current_price * (1 - self.TRADING_SETTINGS['trail_offset_pct'] / 100)
                        logger.info(f"🛡️ بدء Trailing Stop لـ {symbol} عند ${trade['trail_price']:.4f}")
                    elif trade['trail_started']:
                        new_trail = current_price * (1 - self.TRADING_SETTINGS['trail_offset_pct'] / 100)
                        if new_trail > trade['trail_price']:
                            trade['trail_price'] = new_trail
                        if current_price <= trade['trail_price']:
                            self.close_futures_trade(symbol, current_price, 'Trailing Stop')
                            continue
                else:
                    pnl_percent = ((trade['entry_price'] - current_price) / trade['entry_price']) * 100
                    if current_price < trade['lowest_price']:
                        trade['lowest_price'] = current_price
                    if not trade['trail_started'] and pnl_percent >= self.TRADING_SETTINGS['trail_trigger_pct']:
                        trade['trail_started'] = True
                        trade['trail_price'] = current_price * (1 + self.TRADING_SETTINGS['trail_offset_pct'] / 100)
                        logger.info(f"🛡️ بدء Trailing Stop لـ {symbol} عند ${trade['trail_price']:.4f}")
                    elif trade['trail_started']:
                        new_trail = current_price * (1 + self.TRADING_SETTINGS['trail_offset_pct'] / 100)
                        if new_trail < trade['trail_price']:
                            trade['trail_price'] = new_trail
                        if current_price >= trade['trail_price']:
                            self.close_futures_trade(symbol, current_price, 'Trailing Stop')
                            continue

                settings = self.symbol_settings.get(symbol, {'stop_loss_pct': 1.0, 'take_profit_pct': 1.5})
                stop_loss_pct = settings['stop_loss_pct']
                take_profit_pct = settings['take_profit_pct']

                if trade['side'] == 'LONG':
                    if pnl_percent <= -stop_loss_pct:
                        self.close_futures_trade(symbol, current_price, 'Stop Loss')
                    elif pnl_percent >= take_profit_pct:
                        self.close_futures_trade(symbol, current_price, 'Take Profit')
                else:
                    if pnl_percent <= -stop_loss_pct:
                        self.close_futures_trade(symbol, current_price, 'Stop Loss')
                    elif pnl_percent >= take_profit_pct:
                        self.close_futures_trade(symbol, current_price, 'Take Profit')

                trade_age = datetime.now(damascus_tz) - trade['timestamp']
                if trade_age.total_seconds() > self.TRADING_SETTINGS['trade_timeout_hours'] * 3600:
                    self.close_futures_trade(symbol, current_price, 'Timeout')

                if (datetime.now(damascus_tz) - trade['last_notification']).total_seconds() > 3600:
                    if self.notifier:
                        self.notifier.send_message(
                            f"📊 <b>تحديث صفقة عقود</b>\n"
                            f"العملة: {symbol}\n"
                            f"الاتجاه: {trade['side']}\n"
                            f"سعر الدخول: ${trade['entry_price']:.4f}\n"
                            f"السعر الحالي: ${current_price:.4f}\n"
                            f"P&L: {pnl_percent:.2f}%\n"
                            f"المدة: {trade_age.seconds // 60} دقيقة",
                            f'update_futures_{symbol}'
                        )
                    trade['last_notification'] = datetime.now(damascus_tz)

            except Exception as e:
                logger.error(f"❌ خطأ في إدارة صفقة عقود {symbol}: {e}")

    def close_futures_trade(self, symbol, current_price, reason):
        try:
            trade = self.active_trades.get(symbol)
            if not trade:
                return False

            side = Client.SIDE_SELL if trade['side'] == 'LONG' else Client.SIDE_BUY

            order = self.client.futures_create_order(
                symbol=symbol,
                side=side,
                type=Client.ORDER_TYPE_MARKET,
                quantity=trade['quantity']
            )

            if order['status'] == 'FILLED':
                exit_price = float(order['avgPrice'])
                pnl_percent = 0
                if trade['side'] == 'LONG':
                    pnl_percent = ((exit_price - trade['entry_price']) / trade['entry_price']) * 100
                else:
                    pnl_percent = ((trade['entry_price'] - exit_price) / trade['entry_price']) * 100

                pnl_usd = pnl_percent * trade['quantity'] * trade['entry_price'] / 100

                if self.notifier:
                    emoji = "✅" if pnl_percent > 0 else "❌"
                    self.notifier.send_message(
                        f"{emoji} <b>إغلاق صفقة عقود</b>\n"
                        f"العملة: {symbol}\n"
                        f"الاتجاه: {trade['side']}\n"
                        f"السبب: {reason}\n"
                        f"سعر الدخول: ${trade['entry_price']:.4f}\n"
                        f"سعر الخروج: ${exit_price:.4f}\n"
                        f"P&L: {pnl_percent:.2f}% (${pnl_usd:.2f})\n"
                        f"المدة: {(datetime.now(damascus_tz) - trade['timestamp']).seconds // 60} دقيقة",
                        f'trade_close_futures_{symbol}'
                    )

                logger.info(f"✅ إغلاق صفقة عقود {symbol} {trade['side']}: {reason}, P&L: {pnl_percent:.2f}%")
                del self.active_trades[symbol]
                return True

            logger.info(f"❌ فشل إغلاق صفقة عقود {symbol}: حالة الأمر {order['status']}")
            return False

        except Exception as e:
            logger.error(f"❌ خطأ في إغلاق صفقة عقود {symbol}: {e}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>فشل إغلاق صفقة عقود</b>\nالعملة: {symbol}\nالسبب: {reason}\nالخطأ: {str(e)}",
                    f'error_close_futures_{symbol}'
                )
            return False

    def scan_market(self):
        logger.info("🔍 بدء فحص السوق للعقود الآجلة...")
        
        for symbol in self.symbols:
            try:
                if symbol in self.active_trades:
                    continue

                has_signal, analysis, direction = self.analyze_symbol(symbol)
                
                if has_signal and direction:
                    logger.info(f"✅ إشارة {direction} لـ {symbol} - القوة: {analysis['signal_strength']}%")
                    
                    if self.notifier:
                        self.notifier.send_message(
                            f"🔔 <b>إشارة تداول عقود جديدة</b>\n"
                            f"العملة: {symbol}\n"
                            f"الاتجاه: {direction}\n"
                            f"القوة: {analysis['signal_strength']}%\n"
                            f"السعر: ${analysis['price']:.4f}\n"
                            f"EMA5/13: {analysis['ema5']:.4f}/{analysis['ema13']:.4f}\n"
                            f"RSI: {analysis['rsi']:.1f}\n"
                            f"MACD: {analysis['macd']:.4f}",
                            f'signal_futures_{symbol}'
                        )
                    
                    self.execute_futures_trade(symbol, direction, analysis['signal_strength'], analysis)
                else:
                    logger.info(f"ℹ️ لا توجد إشارة لـ {symbol} - القوة: {analysis.get('signal_strength', 0)}%")

            except Exception as e:
                logger.error(f"❌ خطأ في فحص {symbol}: {e}")

    def run(self):
        logger.info("🚀 بدء تشغيل بوت العقود الآجلة...")
        
        schedule.every(self.TRADING_SETTINGS['rescan_interval_minutes']).minutes.do(self.scan_market)
        schedule.every(2).minutes.do(self.manage_trades)
        
        while True:
            try:
                schedule.run_pending()
                time.sleep(1)
            except KeyboardInterrupt:
                logger.info("⏹️ إيقاف البوت بواسطة المستخدم")
                if self.notifier:
                    self.notifier.send_message(
                        "🛑 <b>إيقاف البوت</b>\nتم إيقاف بوت العقود الآجلة يدوياً",
                        'shutdown'
                    )
                break
            except Exception as e:
                logger.error(f"❌ خطأ في الحلقة الرئيسية: {e}")
                time.sleep(30)

def main():
    try:
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        logger.info("✅ بدء تشغيل خادم Flask للرصد الصحي")
        
        bot = FuturesTradingBot()
        bot.run()
    except Exception as e:
        logger.error(f"❌ فشل تشغيل البوت: {e}")
        time.sleep(10)

if __name__ == "__main__":
    main()
