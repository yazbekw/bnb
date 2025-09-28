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
        
            logger.info(f"الرموز المطلوبة: {self.symbols}")
        
            if not all_tickers:
                logger.warning("⚠️ فشل جلب التيكرز، جاري المحاولة الفردية...")
                return self.fallback_price_update()
            
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
            if time.time() - last_update > 120:
                if not self.update_prices():
                    logger.warning(f"⚠️ فشل تحديث الأسعار لـ {symbol}")
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

    def send_message(self, message, message_type='info', retries=3, delay=5):
        """إرسال رسالة إلى Telegram مع إعادة محاولة وتقليل منع التكرار للإشعارات الهامة"""
        try:
            current_time = time.time()
            last_sent = self.notification_types.get(message_type, 0)
            
            min_interval = 0 if message_type.startswith('error') or message_type in ['trade_open_futures', 'trade_close_futures', 'extend_futures', 'signal_futures'] else 10
            if current_time - last_sent < min_interval:
                logger.info(f"⏳ تخطي إشعار {message_type} بسبب منع التكرار")
                return True
                
            self.notification_types[message_type] = current_time
            
            url = f"{self.base_url}/sendMessage"
            payload = {
                'chat_id': self.chat_id, 
                'text': message, 
                'parse_mode': 'HTML'
            }
            
            for attempt in range(retries):
                try:
                    response = requests.post(url, data=payload, timeout=10)
                    if response.status_code == 200:
                        logger.info(f"✅ تم إرسال إشعار Telegram: {message_type} (محاولة {attempt+1})")
                        return True
                    else:
                        logger.error(f"❌ فشل إرسال Telegram (محاولة {attempt+1}): {response.status_code} - {response.text}")
                        if response.status_code == 429:
                            time.sleep(delay * (2 ** attempt))
                        else:
                            break
                except Exception as e:
                    logger.error(f"❌ خطأ في إرسال Telegram (محاولة {attempt+1}): {e}")
                    time.sleep(delay * (2 ** attempt))
            return False
                
        except Exception as e:
            logger.error(f"❌ خطأ عام في إرسال Telegram: {e}")
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
        'max_active_trades': 3,
        'rsi_overbought': 75,
        'rsi_oversold': 35,
        'rsi_buy_threshold': 55,
        'rsi_sell_threshold': 55,
        'data_interval': '5m',
        'rescan_interval_minutes': 5,
        'trade_timeout_hours': 0.3,
        'extended_timeout_hours': 0.5,
        'extended_take_profit_multiplier': 0.5,
        'price_update_interval': 5,
        'trail_trigger_pct': 0.5,
        'trail_offset_pct': 0.5,
        'macd_signal_threshold': 0.001,
        'require_macd_confirmation': False,
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

        self.notifier = None
        if self.telegram_token and self.telegram_chat_id:
            for attempt in range(3):
                try:
                    self.notifier = TelegramNotifier(self.telegram_token, self.telegram_chat_id)
                    logger.info("✅ تهيئة Telegram Notifier ناجحة")
                    break
                except Exception as e:
                    logger.error(f"❌ فشل تهيئة Telegram (محاولة {attempt+1}): {e}")
                    time.sleep(5)
                    if attempt == 2:
                        self.notifier = None
                        logger.error("❌ تعطيل الإشعارات بعد فشل التهيئة")
        else:
            logger.warning("⚠️ مفاتيح Telegram غير موجودة - تعطيل الإشعارات")

        try:
            self.client = Client(self.api_key, self.api_secret)
            self.test_api_connection()
        except Exception as e:
            logger.error(f"❌ فشل تهيئة العميل: {e}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>فشل تهيئة العميل</b>\nالخطأ: {str(e)}",
                    'error_client_init'
                )
            raise

        self.symbols = ["ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT", "ADAUSDT", "XRPUSDT"]
        self.verify_symbols_availability()
    
        self.symbol_settings = {
            "ETHUSDT": {'stop_loss_pct': 1.0, 'take_profit_pct': 1.5},
            "BNBUSDT": {'stop_loss_pct': 1.0, 'take_profit_pct': 1.5},
            "ADAUSDT": {'stop_loss_pct': 1.0, 'take_profit_pct': 1.5},
            "XRPUSDT": {'stop_loss_pct': 1.0, 'take_profit_pct': 1.5},
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
        try:
            exchange_info = self.client.futures_exchange_info()
            available_symbols = [s['symbol'] for s in exchange_info['symbols']]
        
            logger.info(f"الرموز المتاحة في العقود: {len(available_symbols)}")
        
            for symbol in self.symbols:
                if symbol in available_symbols:
                    logger.info(f"✅ {symbol} متوفر في العقود")
                else:
                    logger.warning(f"⚠️ {symbol} غير متوفر في العقود")
                    if self.notifier:
                        self.notifier.send_message(
                            f"⚠️ <b>رمز غير متوفر</b>\nالعملة: {symbol}\nالسبب: غير متوفر في سوق العقود الآجلة",
                            f'symbol_unavailable_{symbol}'
                        )
                
            valid_symbols = [s for s in self.symbols if s in available_symbols]
            if len(valid_symbols) != len(self.symbols):
                logger.warning(f"⚠️ تصحيح الرموز من {self.symbols} إلى {valid_symbols}")
                self.symbols = valid_symbols
                if self.notifier:
                    self.notifier.send_message(
                        f"⚠️ <b>تصحيح الرموز</b>\nالرموز الأصلية: {', '.join(self.symbols)}\nالرموز المتاحة: {', '.join(valid_symbols)}",
                        'symbols_correction'
                    )
            
        except Exception as e:
            logger.error(f"❌ خطأ في التحقق من الرموز: {e}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>خطأ في التحقق من الرموز</b>\nالخطأ: {str(e)}",
                    'error_symbols_verify'
                )

    def debug_bot_status(self):
        logger.info("=== حالة البوت للتصحيح ===")
        logger.info(f"عدد الصفقات النشطة (عقود): {len(self.active_trades)}")
        logger.info(f"Telegram Notifier: {'موجود' if self.notifier else 'غير موجود'}")
        
        for symbol, trade in self.active_trades.items():
            logger.info(f"صفقة عقود {symbol}: {trade}")
        
        if self.notifier:
            try:
                logger.info("✅ اختبار الإشعارات ناجح")
            except Exception as e:
                logger.error(f"❌ فشل اختبار الإشعارات: {e}")

    def test_notifications(self):
        logger.info("🔧 اختبار الإشعارات للصفقات المحملة")
        for symbol in self.active_trades:
            if self.notifier:
                self.notifier.send_message(
                    f"🔧 <b>اختبار التتبع (عقود)</b>\nالعملة: {symbol}\nتم تحميل الصفقة بنجاح\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'test_futures_{symbol}'
                )

    def test_api_connection(self):
        try:
            server_time = self.client.futures_time()
            logger.info(f"✅ اتصال Binance API نشط - وقت الخادم: {server_time}")
            return True
        except Exception as e:
            logger.error(f"❌ فشل الاتصال بـ Binance API: {e}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>فشل الاتصال بـ Binance API</b>\nالخطأ: {str(e)}",
                    'error_api_connection'
                )
            if "Invalid API-key" in str(e):
                logger.error("❌ مفتاح API غير صحيح")
            elif "Signature" in str(e):
                logger.error("❌ سر API غير صحيح")
            elif "permissions" in str(e):
                logger.error("❌ عدم وجود صلاحيات كافية")
            raise

    def send_startup_message(self):
        pass

    def start_price_updater(self):
        def price_update_thread():
            while True:
                try:
                    self.price_manager.update_prices()
                    time.sleep(self.TRADING_SETTINGS['price_update_interval'] * 60)
                except Exception as e:
                    logger.error(f"❌ خطأ في تحديث الأسعار: {str(e)}")
                    if self.notifier:
                        self.notifier.send_message(
                            f"❌ <b>خطأ في تحديث الأسعار</b>\nالخطأ: {str(e)}",
                            'error_price_update'
                        )
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
                            'trail_started': False,
                            'extended': False
                        }
                        
                        self.active_trades[symbol] = trade_data
                        logger.info(f"✅ تم تحميل صفقة عقود: {symbol} - {side} - كمية: {abs(quantity)}")
            
            if not open_positions:
                logger.info("⚠️ لا توجد صفقات مفتوحة في العقود")
                
        except Exception as e:
            logger.error(f"❌ خطأ في تحميل الصفقات العقود: {e}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>خطأ في تحميل الصفقات</b>\nالخطأ: {str(e)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    'error_load_trades'
                )

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
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>فشل جلب سعر</b>\nالعملة: {symbol}\nالسبب: فشل بعد 3 محاولات\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'error_get_price_{symbol}'
                )
            return None
        except Exception as e:
            logger.error(f"❌ خطأ في جلب سعر {symbol}: {e}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>خطأ في جلب سعر</b>\nالعملة: {symbol}\nالخطأ: {str(e)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'error_get_price_{symbol}'
                )
            return None

    def set_leverage(self, symbol, leverage):
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            logger.info(f"✅ ضبط الرافعة لـ {symbol} إلى {leverage}x")
            return True
        except Exception as e:
            logger.error(f"❌ خطأ في ضبط الرافعة: {e}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>خطأ في ضبط الرافعة</b>\nالعملة: {symbol}\nالخطأ: {str(e)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'error_leverage_{symbol}'
                )
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
            if "margin type cannot be changed" in str(e):
                logger.warning(f"⚠️ لا يمكن تغيير نوع الهامش لـ {symbol} بسبب مركز مفتوح")
                return False
            logger.error(f"❌ خطأ في ضبط الهامش: {e}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>خطأ في ضبط نوع الهامش</b>\nالعملة: {symbol}\nالخطأ: {str(e)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'error_margin_type_{symbol}'
                )
            return False

    def get_historical_data(self, symbol, interval='5m', limit=50):
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
            
            logger.info(f"✅ تم جلب البيانات التاريخية لـ {symbol}")
            return data
        except Exception as e:
            logger.error(f"❌ خطأ في جلب البيانات لـ {symbol}: {e}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>خطأ في جلب البيانات التاريخية</b>\nالعملة: {symbol}\nالخطأ: {str(e)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'error_historical_data_{symbol}'
                )
            return None

    def calculate_indicators(self, data):
        try:
            df = data.copy()
            if len(df) < 26:
                logger.warning("⚠️ البيانات غير كافية لحساب المؤشرات")
                return df

            df['ema5'] = df['close'].ewm(span=5, adjust=False).mean()
            df['ema13'] = df['close'].ewm(span=13, adjust=False).mean()
            
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)
            avg_gain = gain.rolling(14).mean()
            avg_loss = loss.rolling(14).mean()
            rs = avg_gain / (avg_loss + 1e-10)
            df['rsi'] = 100 - (100 / (1 + rs))
            
            exp1 = df['close'].ewm(span=12, adjust=False).mean()
            exp2 = df['close'].ewm(span=26, adjust=False).mean()
            df['macd'] = exp1 - exp2
            df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
            df['macd_histogram'] = df['macd'] - df['macd_signal']
            
            high_low = df['high'] - df['low']
            high_close = np.abs(df['high'] - df['close'].shift())
            low_close = np.abs(df['low'] - df['close'].shift())
            ranges = pd.concat([high_low, high_close, low_close], axis=1)
            true_range = np.max(ranges, axis=1)
            df['atr'] = true_range.rolling(14).mean()
            
            df['volume_avg'] = df['volume'].rolling(14).mean()
            
            logger.info("✅ تم حساب المؤشرات بنجاح")
            return df
        except Exception as e:
            logger.error(f"❌ خطأ في حساب المؤشرات: {e}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>خطأ في حساب المؤشرات</b>\nالخطأ: {str(e)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    'error_calculate_indicators'
                )
            return data

    def analyze_symbol(self, symbol):
        try:
            data = self.get_historical_data(symbol, self.TRADING_SETTINGS['data_interval'])
            if data is None or len(data) < 26:
                logger.warning(f"⚠️ بيانات غير كافية لتحليل {symbol}")
                return False, {}, None

            data = self.calculate_indicators(data)
            latest = data.iloc[-1]
            previous = data.iloc[-2]

            buy_signal = (latest['ema5'] > latest['ema13'] and previous['ema5'] <= previous['ema13'])
            sell_signal = (latest['ema5'] < latest['ema13'] and previous['ema5'] >= previous['ema13'])

            rsi_buy_ok = latest['rsi'] < self.TRADING_SETTINGS['rsi_buy_threshold']
            rsi_sell_ok = latest['rsi'] > self.TRADING_SETTINGS['rsi_sell_threshold']

            macd_buy_signal = (latest['macd'] > latest['macd_signal'] and previous['macd'] <= previous['macd_signal'])
            macd_sell_signal = (latest['macd'] < latest['macd_signal'] and previous['macd'] >= previous['macd_signal'])
            
            macd_above_zero = latest['macd'] > 0
            macd_below_zero = latest['macd'] < 0

            points = 0
            max_points = 100
            
            ema_points = 0
            rsi_points_value = 0
            macd_points = 0
            volume_points = 0
            momentum_points = 0

            if buy_signal or sell_signal:
                ema_diff = abs(latest['ema5'] - latest['ema13']) / latest['ema13'] * 100
                ema_base_points = 20
                additional_ema_points = min(15, ema_diff * 8)
                ema_points = ema_base_points + additional_ema_points
                points += min(35, ema_points)
                logger.debug(f"📊 {symbol} - نقاط EMA: {min(35, ema_points):.1f}")

            rejection_reason = None
            if buy_signal and rsi_buy_ok:
                if latest['rsi'] < 25:
                    rsi_points_value = 25
                elif latest['rsi'] < 35:
                    rsi_points_value = 20
                else:
                    rsi_points_value = 15
                points += rsi_points_value
                logger.debug(f"📊 {symbol} - نقاط RSI للشراء: {rsi_points_value}")
            elif sell_signal and rsi_sell_ok:
                if latest['rsi'] > 85:
                    rsi_points_value = 25
                elif latest['rsi'] > 75:
                    rsi_points_value = 20
                else:
                    rsi_points_value = 15
                points += rsi_points_value
                logger.debug(f"📊 {symbol} - نقاط RSI للبيع: {rsi_points_value}")
            else:
                if buy_signal and not rsi_buy_ok:
                    rejection_reason = f"RSI ({latest['rsi']:.1f}) مرتفع جدًا للشراء (يجب أن يكون < {self.TRADING_SETTINGS['rsi_buy_threshold']})"
                elif sell_signal and not rsi_sell_ok:
                    rejection_reason = f"RSI ({latest['rsi']:.1f}) منخفض جدًا للبيع (يجب أن يكون > {self.TRADING_SETTINGS['rsi_sell_threshold']})"

            macd_required = self.TRADING_SETTINGS['require_macd_confirmation']
            if not macd_required or (macd_buy_signal and buy_signal) or (macd_sell_signal and sell_signal):
                macd_strength = abs(latest['macd_histogram']) / max(0.001, abs(latest['macd'])) * 20
                direction_bonus = 0
                if (macd_buy_signal and buy_signal and macd_above_zero) or (macd_sell_signal and sell_signal and macd_below_zero):
                    direction_bonus = 5
                macd_points = min(20, max(8, macd_strength + direction_bonus))
                points += macd_points
                logger.debug(f"📊 {symbol} - نقاط MACD: {macd_points:.1f}")
            else:
                logger.debug(f"📊 {symbol} - نقاط MACD: 0 (لم تستوف الشروط)")

            volume_ratio = latest['volume'] / latest['volume_avg'] if latest['volume_avg'] > 0 else 1
            if volume_ratio > 1.3:
                volume_points = 10
            elif volume_ratio > 1.0:
                volume_points = 7
            elif volume_ratio > 0.8:
                volume_points = 3
            else:
                volume_points = 0
            points += volume_points
            logger.debug(f"📊 {symbol} - نقاط الحجم: {volume_points} (نسبة: {volume_ratio:.2f})")

            if hasattr(latest, 'momentum'):
                momentum_strength = abs(latest['momentum']) / latest['close'] * 100
                momentum_points = min(10, momentum_strength * 2)
                points += momentum_points
                logger.debug(f"📊 {symbol} - نقاط الزخم: {momentum_points:.1f}")

            direction = None
            min_points_required = 55
            if points >= min_points_required:
                if buy_signal and rsi_buy_ok:
                    direction = 'LONG'
                    if hasattr(latest, 'sma20') and latest['close'] > latest['sma20']:
                        points = min(100, points + 5)
                        logger.debug(f"📊 {symbol} - نقاط إضافية للاتجاه الطويل القوي")
                elif sell_signal and rsi_sell_ok:
                    direction = 'SHORT'
                    if hasattr(latest, 'sma20') and latest['close'] < latest['sma20']:
                        points = min(100, points + 5)
                        logger.debug(f"📊 {symbol} - نقاط إضافية للاتجاه القصير القوي")

            details = {
                'signal_strength': points,
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
                'direction': direction,
                'atr': latest['atr'],
                'volume': latest['volume'],
                'volume_avg': latest['volume_avg'],
                'volume_ratio': volume_ratio,
                'points_breakdown': {
                    'ema_points': ema_points,
                    'rsi_points': rsi_points_value,
                    'macd_points': macd_points,
                    'volume_points': volume_points,
                    'momentum_points': momentum_points
                },
                'rejection_reason': rejection_reason
            }

            logger.info(f"🔍 تحليل {symbol} انتهى: النقاط {points}/{max_points}, الاتجاه {direction}, "
                       f"EMA: {'شراء' if buy_signal else 'بيع' if sell_signal else 'لا'}, "
                       f"RSI: {latest['rsi']:.1f}, MACD: {'شراء' if macd_buy_signal else 'بيع' if macd_sell_signal else 'محايد'}")

            if points >= min_points_required and direction:
                logger.info(f"🎯 إشارة قوية لـ {symbol}: {direction} بنقاط {points}/{max_points}")
            elif points >= 50:
                logger.info(f"⚠️ إشارة متوسطة لـ {symbol}: نقاط {points}/{max_points} (تحتاج {min_points_required})")
            else:
                logger.info(f"ℹ️ إشارة ضعيفة لـ {symbol}: نقاط {points}/{max_points}")

            return points >= min_points_required, details, direction

        except Exception as e:
            logger.error(f"❌ خطأ في تحليل {symbol}: {e}")
            import traceback
            logger.error(f"❌ تفاصيل الخطأ: {traceback.format_exc()}")
            
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>خطأ في تحليل رمز</b>\nالعملة: {symbol}\nالخطأ: {str(e)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'error_analyze_{symbol}'
                )
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
                logger.info(f"✅ جلب دقة العقود لـ {symbol}")
                return precision_info
            
            logger.warning(f"⚠️ معلومات الرمز غير متوفرة لـ {symbol}")
            return {'step_size': 0.001, 'precision': 3, 'min_qty': 0.001, 'min_notional': 5.0}
        except Exception as e:
            logger.error(f"❌ خطأ في جلب دقة العقود: {e}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>خطأ في جلب دقة العقود</b>\nالعملة: {symbol}\nالخطأ: {str(e)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'error_precision_{symbol}'
                )
            return {'step_size': 0.001, 'precision': 3, 'min_qty': 0.001, 'min_notional': 5.0}

    def execute_futures_trade(self, symbol, direction, signal_strength, analysis):
        try:
            if len(self.active_trades) >= self.TRADING_SETTINGS['max_active_trades']:
                logger.info(f"⏸️ الحد الأقصى للصفقات العقود ({self.TRADING_SETTINGS['max_active_trades']}) لـ {symbol}")
                return False

            if analysis['signal_strength'] < 60 or analysis['direction'] != direction:
                logger.info(f"❌ إشارة غير صالحة لـ {symbol}: القوة {analysis['signal_strength']}%, الاتجاه {analysis['direction']}")
                return False

            current_price = self.get_current_price(symbol)
            if current_price is None:
                logger.error(f"❌ فشل جلب السعر لـ {symbol}")
                return False

            self.set_leverage(symbol, self.TRADING_SETTINGS['leverage'])
            self.set_margin_type(symbol, self.TRADING_SETTINGS['margin_type'])

            base_trade_size_usd = self.TRADING_SETTINGS['min_trade_size'] + (signal_strength / 100) * (self.TRADING_SETTINGS['max_trade_size'] - self.TRADING_SETTINGS['min_trade_size'])

            volume_factor = min(0.2, (analysis['volume'] / analysis['volume_avg'] - 1) if analysis['volume_avg'] > 0 else 0)
            atr_normalized = analysis['atr'] / analysis['price'] if analysis['atr'] > 0 and analysis['price'] > 0 else 0
            atr_factor = min(0.2, max(0, 0.2 - atr_normalized * 10))

            adjusted_trade_size_usd = base_trade_size_usd * (1 + volume_factor + atr_factor)
            trade_size_usd = min(self.TRADING_SETTINGS['max_trade_size'], max(self.TRADING_SETTINGS['min_trade_size'], adjusted_trade_size_usd))

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
                logger.error(f"❌ كمية غير صالحة: {quantity} لـ {symbol}")
                if self.notifier:
                    self.notifier.send_message(
                        f"❌ <b>كمية غير صالحة</b>\nالعملة: {symbol}\nالكمية: {quantity}\nالحد الأدنى للكمية: {precision_info['min_qty']}\nالحد الأدنى للقيمة: {precision_info['min_notional']}\nالحجم: {trade_size_usd}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
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
                    'trail_started': False,
                    'extended': False
                }
                
                self.active_trades[symbol] = trade_data

                if self.notifier:
                    self.notifier.send_message(
                        f"🚀 <b>فتح صفقة عقود جديدة</b>\n"
                        f"العملة: {symbol}\n"
                        f"الاتجاه: {direction}\n"
                        f"سبب الدخول: تقاطع EMA ({'صعودي' if direction == 'LONG' else 'هبوطي'})، RSI {'منخفض' if direction == 'LONG' else 'مرتفع'} ({analysis['rsi']:.1f})، MACD {'صعودي' if direction == 'LONG' else 'هبوطي'} ({analysis['macd']:.4f})\n"
                        f"قوة الإشارة: {signal_strength}%\n"
                        f"سعر الدخول: ${avg_price:.4f}\n"
                        f"الكمية: {quantity:.6f}\n"
                        f"الحجم: ${trade_size_usd:.2f}\n"
                        f"الرافعة: {self.TRADING_SETTINGS['leverage']}x\n"
                        f"EMA5/13: {analysis['ema5']:.4f}/{analysis['ema13']:.4f}\n"
                        f"RSI: {analysis['rsi']:.1f}\n"
                        f"MACD: {analysis['macd']:.4f}\n"
                        f"ATR: {analysis['atr']:.4f}\n"
                        f"الحجم: {analysis['volume']:.2f} (متوسط: {analysis['volume_avg']:.2f})\n"
                        f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'trade_open_futures_{symbol}'
                    )

                logger.info(f"✅ فتح صفقة عقود {symbol} {direction} بسعر {avg_price}")
                return True

            logger.info(f"❌ فشل تنفيذ صفقة عقود {symbol}: حالة الأمر {order['status']}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>فشل تنفيذ صفقة</b>\nالعملة: {symbol}\nالحالة: {order['status']}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'error_execute_trade_{symbol}'
                )
            return False

        except Exception as e:
            logger.error(f"❌ خطأ في تنفيذ صفقة عقود {symbol}: {e}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>خطأ في تنفيذ صفقة</b>\nالعملة: {symbol}\nالخطأ: {str(e)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'error_execute_futures_{symbol}'
                )
            return False

    def manage_trades(self):
        self.manage_futures_trades()

    def update_active_trades(self):
        """تحديث قائمة الصفقات النشطة دورياً من API"""
        try:
            account_info = self.client.futures_account()
            positions = account_info['positions']
            
            current_positions = {}
            
            for position in positions:
                symbol = position['symbol']
                if symbol in self.symbols:
                    quantity = float(position['positionAmt'])
                    if quantity != 0:
                        if symbol in self.active_trades:
                            current_positions[symbol] = self.active_trades[symbol]
                        else:
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
                                'trail_started': False,
                                'extended': False
                            }
                            current_positions[symbol] = trade_data
                            logger.info(f"✅ اكتشاف صفقة جديدة: {symbol} - {side} - كمية: {abs(quantity)}")
            
            removed_trades = set(self.active_trades.keys()) - set(current_positions.keys())
            for symbol in removed_trades:
                logger.info(f"🔄 إزالة صفقة مغلقة من القائمة: {symbol}")
                if self.notifier:
                    self.notifier.send_message(
                        f"🔄 <b>إزالة صفقة مغلقة</b>\nالعملة: {symbol}\nتم إغلاق الصفقة خارج البوت\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'trade_removed_{symbol}'
                    )
            
            self.active_trades = current_positions
            logger.info(f"✅ تم تحديث الصفقات النشطة: {len(self.active_trades)} صفقة")
            
        except Exception as e:
            logger.error(f"❌ خطأ في تحديث الصفقات النشطة: {e}")

    def manage_futures_trades(self):
        if not self.active_trades:
            logger.info("🔍 لا توجد صفقات نشطة في العقود")
            return
        
        self.update_active_trades()
        
        if not self.active_trades:
            logger.info("🔍 لا توجد صفقات نشطة بعد التحديث")
            return
        
        logger.info(f"🔍 إدارة {len(self.active_trades)} صفقة عقود نشطة")
        
        for symbol, trade in list(self.active_trades.items()):
            try:
                current_price = self.get_current_price(symbol)
                if current_price is None:
                    continue

                try:
                    position_info = self.client.futures_position_information(symbol=symbol)
                    if position_info:
                        current_position_amt = float(position_info[0]['positionAmt'])
                        if current_position_amt == 0:
                            logger.info(f"🔄 المركز مغلق فعلياً لـ {symbol}، إزالته من القائمة")
                            del self.active_trades[symbol]
                            continue
                except Exception as e:
                    logger.error(f"❌ خطأ في التحقق من المركز لـ {symbol}: {e}")

                if trade['side'] == 'LONG':
                    pnl_percent = ((current_price - trade['entry_price']) / trade['entry_price']) * 100
                    if current_price > trade['highest_price']:
                        trade['highest_price'] = current_price
                    if not trade['trail_started'] and pnl_percent >= self.TRADING_SETTINGS['trail_trigger_pct']:
                        trade['trail_started'] = True
                        trade['trail_price'] = current_price * (1 - self.TRADING_SETTINGS['trail_offset_pct'] / 100)
                        logger.info(f"🛡️ بدء Trailing Stop لـ {symbol} عند ${trade['trail_price']:.4f}")
                        if self.notifier:
                            self.notifier.send_message(
                                f"🛡️ <b>بدء Trailing Stop</b>\nالعملة: {symbol}\nسعر التتبع: ${trade['trail_price']:.4f}\nP&L: {pnl_percent:.2f}%\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                                f'trail_start_{symbol}'
                            )
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
                        if self.notifier:
                            self.notifier.send_message(
                                f"🛡️ <b>بدء Trailing Stop</b>\nالعملة: {symbol}\nسعر التتبع: ${trade['trail_price']:.4f}\nP&L: {pnl_percent:.2f}%\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                                f'trail_start_{symbol}'
                            )
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
                if trade.get('extended', False) and pnl_percent < 0:
                    take_profit_pct *= self.TRADING_SETTINGS['extended_take_profit_multiplier']
                    logger.info(f"🔄 خفض حد جني الأرباح لـ {symbol} إلى {take_profit_pct:.2f}% بسبب التمديد")

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
                timeout_hours = self.TRADING_SETTINGS['extended_timeout_hours'] if trade.get('extended', False) else self.TRADING_SETTINGS['trade_timeout_hours']
                
                if pnl_percent < 0 and not trade.get('extended', False) and trade_age.total_seconds() > self.TRADING_SETTINGS['trade_timeout_hours'] * 3600:
                    trade['timestamp'] = datetime.now(damascus_tz) - timedelta(hours=self.TRADING_SETTINGS['trade_timeout_hours'])
                    trade['extended'] = True
                    logger.info(f"⏳ تمديد وقت الصفقة الخاسرة {symbol} إلى {self.TRADING_SETTINGS['extended_timeout_hours']} ساعات")

                elif trade_age.total_seconds() > timeout_hours * 3600:
                    self.close_futures_trade(symbol, current_price, 'Timeout')

            except Exception as e:
                logger.error(f"❌ خطأ في إدارة صفقة عقود {symbol}: {e}")
                if self.notifier:
                    self.notifier.send_message(
                        f"❌ <b>خطأ في إدارة صفقة</b>\nالعملة: {symbol}\nالخطأ: {str(e)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'error_manage_trade_{symbol}'
                    )

    def close_futures_trade(self, symbol, current_price, reason):
        try:
            if symbol not in self.active_trades:
                logger.info(f"ℹ️ الصفقة {symbol} غير موجودة في القائمة النشطة")
                return True

            trade = self.active_trades.get(symbol)
            if not trade:
                logger.warning(f"⚠️ لا توجد صفقة للإغلاق: {symbol}")
                return True

            try:
                position_info = self.client.futures_position_information(symbol=symbol)
                if position_info:
                    current_position_amt = float(position_info[0]['positionAmt'])
                    if current_position_amt == 0:
                        logger.info(f"✅ الصفقة {symbol} مغلقة بالفعل على المنصة")
                        del self.active_trades[symbol]
                        return True
            except Exception as e:
                logger.error(f"❌ خطأ في التحقق من المركز لـ {symbol}: {e}")

            side = Client.SIDE_SELL if trade['side'] == 'LONG' else Client.SIDE_BUY
            quantity = trade['quantity']

            try:
                order = self.client.futures_create_order(
                    symbol=symbol,
                    side=side,
                    type=Client.ORDER_TYPE_MARKET,
                    quantity=quantity,
                    reduceOnly=True
                )
                if order['status'] == 'FILLED':
                    exit_price = float(order['avgPrice'])
                    pnl_percent = ((exit_price - trade['entry_price']) / trade['entry_price']) * 100 if trade['side'] == 'LONG' else ((trade['entry_price'] - exit_price) / trade['entry_price']) * 100
                    pnl_usd = pnl_percent * quantity * trade['entry_price'] / 100
                    emoji = "✅" if pnl_percent > 0 else "❌"
                    if self.notifier:
                        self.notifier.send_message(
                            f"{emoji} <b>إغلاق صفقة عقود</b>\nالعملة: {symbol}\nالاتجاه: {trade['side']}\nسبب الخروج: {reason}\nسعر الدخول: ${trade['entry_price']:.4f}\nسعر الخروج: ${exit_price:.4f}\nP&L: {pnl_percent:.2f}% (${pnl_usd:.2f})\nالمدة: {(datetime.now(damascus_tz) - trade['timestamp']).seconds // 60} دقيقة\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                            f'trade_close_futures_{symbol}'
                        )
                    logger.info(f"✅ إغلاق صفقة عقود {symbol} {trade['side']}: {reason}, P&L: {pnl_percent:.2f}%")
                    del self.active_trades[symbol]
                    return True
            except Exception as e:
                if "-2019" not in str(e):
                    raise e

            logger.warning(f"⚠️ نقص هامش لـ {symbol}، بدء تسلسل الإنقاذ...")
            if self.notifier:
                self.notifier.send_message(
                    f"⚠️ <b>نقص هامش، بدء إنقاذ</b>\nالعملة: {symbol}\nالسبب: {reason}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'rescue_start_{symbol}'
                )

            limit_price = current_price * 0.999 if trade['side'] == 'LONG' else current_price * 1.001
            try:
                limit_order = self.client.futures_create_order(
                    symbol=symbol,
                    side=side,
                    type=Client.ORDER_TYPE_LIMIT,
                    quantity=quantity,
                    price=limit_price,
                    timeInForce=Client.TIME_IN_FORCE_GTC,
                    reduceOnly=True
                )
                logger.info(f"✅ وضع أمر حدي لـ {symbol}: سعر ${limit_price:.4f}")
                if self.notifier:
                    self.notifier.send_message(
                        f"📌 <b>وضع أمر حدي</b>\nالعملة: {symbol}\nسعر الحد: ${limit_price:.4f}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'limit_order_{symbol}'
                    )
            except Exception as limit_e:
                logger.error(f"❌ فشل وضع أمر حدي: {limit_e}")

            time.sleep(300)

            position_info = self.client.futures_position_information(symbol=symbol)
            current_position_amt = float(position_info[0]['positionAmt']) if position_info else quantity
            if abs(current_position_amt) == 0:
                logger.info(f"✅ تم إغلاق {symbol} عبر الأمر الحدي")
                del self.active_trades[symbol]
                return True

            half_quantity = round(quantity / 2, self.get_futures_precision(symbol)['precision'])
            if half_quantity > 0:
                try:
                    half_order = self.client.futures_create_order(
                        symbol=symbol,
                        side=side,
                        type=Client.ORDER_TYPE_MARKET,
                        quantity=half_quantity,
                        reduceOnly=True
                    )
                    if half_order['status'] == 'FILLED':
                        logger.info(f"✅ إغلاق نصف الكمية لـ {symbol}")
                        if self.notifier:
                            self.notifier.send_message(
                                f"✅ <b>إغلاق نصف الصفقة</b>\nالعملة: {symbol}\nالكمية: {half_quantity}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                                f'half_close_{symbol}'
                            )
                        trade['quantity'] -= half_quantity
                except Exception as half_e:
                    logger.error(f"❌ فشل إغلاق نصف: {half_e}")

            try:
                self.set_margin_type(symbol, 'CROSS')
                logger.info(f"✅ تغيير نوع الهامش إلى CROSS لـ {symbol}")
            except Exception as margin_e:
                if "cannot be changed" in str(margin_e):
                    try:
                        self.client.futures_change_position_margin(
                            symbol=symbol,
                            amount=5.0,
                            type=1
                        )
                        logger.info(f"✅ نقل هامش إضافي 5 USDT لـ {symbol}")
                        if self.notifier:
                            self.notifier.send_message(
                                f"💰 <b>نقل هامش إضافي</b>\nالعملة: {symbol}\nالمبلغ: 5 USDT\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                                f'margin_add_{symbol}'
                            )
                    except Exception as add_e:
                        logger.error(f"❌ فشل نقل هامش: {add_e}")

            return self.close_futures_trade(symbol, self.get_current_price(symbol), reason)

        except Exception as e:
            logger.error(f"❌ خطأ في إغلاق صفقة عقود {symbol}: {e}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>خطأ في إغلاق صفقة</b>\nالعملة: {symbol}\nالسبب: {reason}\nالخطأ: {str(e)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'error_close_futures_{symbol}'
                )
            return False

    def scan_market(self):
        logger.info("🔍 بدء فحص السوق للعقود الآجلة...")
        
        for symbol in self.symbols:
            try:
                if symbol in self.active_trades:
                    logger.info(f"⏸️ صفقة نشطة بالفعل لـ {symbol}، تخطي الفحص")
                    continue

                has_signal, analysis, direction = self.analyze_symbol(symbol)
                
                if has_signal and direction:
                    logger.info(f"✅ إشارة قوية لـ {symbol} - الاتجاه: {direction}، القوة: {analysis['signal_strength']}%")
                    
                    if self.notifier:
                        self.notifier.send_message(
                            f"🔔 <b>إشارة تداول قوية</b>\n"
                            f"العملة: {symbol}\n"
                            f"الاتجاه: {direction}\n"
                            f"قوة الإشارة: {analysis['signal_strength']}%\n"
                            f"السعر الحالي: ${analysis['price']:.4f}\n"
                            f"سبب الإشارة: تقاطع EMA ({'صعودي' if direction == 'LONG' else 'هبوطي'})، RSI ({analysis['rsi']:.1f})، MACD ({analysis['macd']:.4f})\n"
                            f"EMA5/13: {analysis['ema5']:.4f}/{analysis['ema13']:.4f}\n"
                            f"ATR: {analysis['atr']:.4f}\n"
                            f"الحجم: {analysis['volume']:.2f} (متوسط: {analysis['volume_avg']:.2f})\n"
                            f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                            f'signal_futures_{symbol}'
                        )
                    
                    self.execute_futures_trade(symbol, direction, analysis['signal_strength'], analysis)
                elif analysis['signal_strength'] >= 50 and not has_signal:
                    rejection_reason = analysis.get('rejection_reason', 'سبب غير محدد')
                    logger.warning(f"⚠️ إشارة قوية مرفوضة لـ {symbol}: القوة {analysis['signal_strength']}، السبب: {rejection_reason}")
                    if self.notifier:
                        self.notifier.send_message(
                            f"⚠️ <b>إشارة قوية مرفوضة</b>\n"
                            f"العملة: {symbol}\n"
                            f"قوة الإشارة: {analysis['signal_strength']}%\n"
                            f"السعر الحالي: ${analysis['price']:.4f}\n"
                            f"الاتجاه: {'LONG' if analysis['buy_signal'] else 'SHORT' if analysis['sell_signal'] else 'غير محدد'}\n"
                            f"السبب: {rejection_reason}\n"
                            f"EMA5/13: {analysis['ema5']:.4f}/{analysis['ema13']:.4f}\n"
                            f"RSI: {analysis['rsi']:.1f}\n"
                            f"MACD: {analysis['macd']:.4f}\n"
                            f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                            f'rejected_signal_{symbol}'
                        )
                else:
                    logger.info(f"ℹ️ لا توجد إشارة قوية لـ {symbol} - القوة: {analysis.get('signal_strength', 0)}%")

            except Exception as e:
                logger.error(f"❌ خطأ في فحص {symbol}: {e}")
                if self.notifier:
                    self.notifier.send_message(
                        f"❌ <b>خطأ في فحص السوق</b>\nالعملة: {symbol}\nالخطأ: {str(e)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'error_scan_{symbol}'
                    )

    def run(self):
        logger.info("🚀 بدء تشغيل بوت العقود الآجلة...")
        
        schedule.every(self.TRADING_SETTINGS['rescan_interval_minutes']).minutes.do(self.scan_market)
        schedule.every(2).minutes.do(self.manage_trades)
        schedule.every(5).minutes.do(self.update_active_trades)
        
        while True:
            try:
                schedule.run_pending()
                time.sleep(1)
            except KeyboardInterrupt:
                logger.info("⏹️ إيقاف البوت بواسطة المستخدم")
                if self.notifier:
                    self.notifier.send_message(
                        f"🛑 <b>إيقاف البوت</b>\nتم إيقاف البوت يدويًا\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        'shutdown'
                    )
                break
            except Exception as e:
                logger.error(f"❌ خطأ في الحلقة الرئيسية: {e}")
                if self.notifier:
                    self.notifier.send_message(
                        f"❌ <b>خطأ في الحلقة الرئيسية</b>\nالخطأ: {str(e)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        'error_main_loop'
                    )
                time.sleep(30)

    def send_daily_status(self):
        pass

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
