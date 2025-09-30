import os
import pandas as pd
import numpy as np
import hashlib
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
        try:
            success_count = 0
            all_tickers = self.client.futures_ticker()
            logger.info(f"عدد التيكرز المجلوبة: {len(all_tickers) if all_tickers else 0}")
            logger.info(f"الرموز المطلوبة: {self.symbols}")
            
            if not all_tickers:
                logger.warning("⚠️ فشل جلب التيكرز، الرجوع مباشرة للفردي...")
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
            
            if success_count == 0:
                logger.warning("⚠️ لم يتم العثور على أي رمز في التيكرز، جاري الرجوع للفردي...")
                return self.fallback_price_update()
            
            logger.info(f"✅ تم تحديث أسعار {success_count} من {len(self.symbols)} رمز")
            return True
            
        except Exception as e:
            logger.error(f"❌ خطأ في تحديث الأسعار الجماعي: {str(e)}")
            return self.fallback_price_update()

    def fallback_price_update(self):
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
            logger.info(f"✅ Fallback: تم تحديث أسعار {success_count} من {len(self.symbols)} رمز")
            return True
        logger.error("❌ فشل تحديث الأسعار بعد كل المحاولات")
        return False

    def get_price(self, symbol):
        try:
            last_update = self.last_update.get(symbol, 0)
            if time.time() - last_update > 120:
                if not self.update_prices():
                    logger.warning(f"⚠️ فشل تحديث الأسعار لـ {symbol}")
                    return None
            return self.prices.get(symbol)
        except Exception as e:
            logger.error(f"❌ خطأ في جلب سعر {symbol}: {str(e)}")
            return None

    def is_connected(self):
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
        self.recent_messages = {}
        self.message_cooldown = 60

    def send_message(self, message, message_type='info', retries=3, delay=5):
        try:
            if len(message) > 4096:
                message = message[:4090] + "..."

            current_time = time.time()
            message_hash = hashlib.md5(f"{message_type}_{message}".encode()).hexdigest()
            
            if message_hash in self.recent_messages:
                if current_time - self.recent_messages[message_hash] < self.message_cooldown:
                    logger.debug(f"⏳ تخطي إشعار مكرر: {message_type}")
                    return True

            self.recent_messages[message_hash] = current_time
            
            self._clean_old_messages()

            url = f"{self.base_url}/sendMessage"
            payload = {
                'chat_id': self.chat_id, 
                'text': message, 
                'parse_mode': 'HTML',
                'disable_web_page_preview': True
            }
            
            for attempt in range(retries):
                try:
                    response = requests.post(url, data=payload, timeout=15)
                    
                    if response.status_code == 200:
                        result = response.json()
                        if result.get('ok'):
                            logger.info(f"✅ تم إرسال إشعار Telegram: {message_type}")
                            return True
                        else:
                            error_desc = result.get('description', 'Unknown error')
                            logger.error(f"❌ Telegram API error: {error_desc}")
                    
                    elif response.status_code == 429:
                        retry_after = int(response.headers.get('Retry-After', 60))
                        logger.warning(f"⏳ Rate limited, retrying after {retry_after}s")
                        time.sleep(retry_after)
                        continue
                        
                    elif response.status_code >= 500:
                        logger.warning(f"⚠️ Telegram server error {response.status_code}")
                        time.sleep(delay * (2 ** attempt))
                        continue
                        
                    else:
                        logger.error(f"❌ Failed to send Telegram (attempt {attempt+1}): {response.status_code}")
                        break
                        
                except requests.exceptions.Timeout:
                    logger.warning(f"⏰ Timeout sending Telegram (attempt {attempt+1})")
                    time.sleep(delay * (2 ** attempt))
                except requests.exceptions.ConnectionError:
                    logger.warning(f"🌐 Connection error sending Telegram (attempt {attempt+1})")
                    time.sleep(delay * (2 ** attempt))
                except Exception as e:
                    logger.error(f"❌ Unexpected error sending Telegram (attempt {attempt+1}): {e}")
                    time.sleep(delay * (2 ** attempt))
            
            logger.error(f"❌ فشل إرسال الإشعار بعد {retries} محاولات")
            return False
                
        except Exception as e:
            logger.error(f"❌ General error in Telegram sending: {e}")
            return False

    def _clean_old_messages(self):
        current_time = time.time()
        expired_messages = [
            msg_hash for msg_hash, timestamp in self.recent_messages.items()
            if current_time - timestamp > self.message_cooldown * 2
        ]
        for msg_hash in expired_messages:
            del self.recent_messages[msg_hash]
            
class FuturesTradingBot:
    _instance = None
    TRADING_SETTINGS = {
        'min_trade_size': 10,
        'max_trade_size': 20,
        'leverage': 10,
        'margin_type': 'ISOLATED',
        'base_risk_pct': 0.002,
        'risk_reward_ratio': 2.0,
        'max_active_trades': 3,
        'rsi_overbought': 75,
        'rsi_oversold': 35,
        'rsi_buy_threshold': 50,  # Updated for strategy
        'rsi_sell_threshold': 75,
        'data_interval': '1h',  # Updated to hourly for trend following
        'rescan_interval_minutes': 10,
        'trade_timeout_hours': 0.3,
        'extended_timeout_hours': 0.5,
        'extended_take_profit_multiplier': 0.5,
        'price_update_interval': 5,
        'trail_trigger_pct': 0.5,
        'trail_offset_pct': 0.5,
        'macd_signal_threshold': 0.001,
        'require_macd_confirmation': False,
        'min_signal_strength': 50,
        'medium_signal_strength': 45,
        'strong_signal_bonus': 65,
        'stop_loss_pct': 2.0,  # New for strategy
        'take_profit_pct': 5.0  # New for strategy
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

        self.symbols = ["SOLUSDT", "DOGEUSDT", "XPLUSDT"]
        self.verify_symbols_availability()
    
        self.symbol_settings = {
            "XPLUSDT": {'stop_loss_pct': 2.0, 'take_profit_pct': 5.0},
            "SOLUSDT": {'stop_loss_pct': 2.0, 'take_profit_pct': 5.0},
            "DOGEUSDT": {'stop_loss_pct': 2.0, 'take_profit_pct': 5.0},
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
        if self.notifier:
            try:
                symbols_count = len(self.symbols)
                active_trades_count = len(self.active_trades)
            
                message = (
                    "🚀 <b>بدء تشغيل بوت العقود الآجلة</b>\n\n"
                    f"📊 <b>الإعدادات:</b>\n"
                    f"• عدد الرموز: {symbols_count}\n"
                    f"• الصفقات النشطة: {active_trades_count}\n"
                    f"• الرافعة: {self.TRADING_SETTINGS['leverage']}x\n"
                    f"• الفحص كل: {self.TRADING_SETTINGS['rescan_interval_minutes']} دقائق\n\n"
                    f"🕒 <b>وقت البدء:</b>\n"
                    f"{datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"⏰ توقيت: دمشق"
                )
            
                success = self.notifier.send_message(message, 'startup')
                if success:
                    logger.info("✅ تم إرسال رسالة بدء التشغيل")
                else:
                    logger.error("❌ فشل إرسال رسالة بدء التشغيل")
                
            except Exception as e:
                logger.error(f"❌ خطأ في إرسال رسالة البدء: {e}")

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

    def get_historical_data(self, symbol, interval='1h', limit=200):  # Updated limit for SMA200
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
            if len(df) < 200:  # For SMA200
                logger.warning("⚠️ البيانات غير كافية لحساب المؤشرات")
                return df

            df['sma50'] = df['close'].rolling(window=50).mean()
            df['sma200'] = df['close'].rolling(window=200).mean()
            
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)
            avg_gain = gain.rolling(14).mean()
            avg_loss = loss.rolling(14).mean()
            rs = avg_gain / (avg_loss + 1e-10)
            df['rsi'] = 100 - (100 / (1 + rs))
            
            high_low = df['high'] - df['low']
            high_close = np.abs(df['high'] - df['close'].shift())
            low_close = np.abs(df['low'] - df['close'].shift())
            ranges = pd.concat([high_low, high_close, low_close], axis=1)
            true_range = np.max(ranges, axis=1)
            df['atr'] = true_range.rolling(14).mean()
            
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
            if data is None or len(data) < 200:
                logger.warning(f"⚠️ بيانات غير كافية لتحليل {symbol}")
                return False, {}, None

            data = self.calculate_indicators(data)
            latest = data.iloc[-1]
            previous = data.iloc[-2]

            buy_signal = (latest['sma50'] > latest['sma200'] and previous['sma50'] <= previous['sma200'])  # Golden Cross
            sell_signal = (latest['sma50'] < latest['sma200'] and previous['sma50'] >= previous['sma200'])  # Death Cross

            rsi_buy_ok = latest['rsi'] > self.TRADING_SETTINGS['rsi_buy_threshold']  # RSI > 50 for buy

            points = 0
            if buy_signal and rsi_buy_ok:
                points = 100  # Strong signal for buy
            elif sell_signal:
                points = 100  # Strong signal for sell (no RSI filter for sell as per strategy)

            direction = None
            if points >= self.TRADING_SETTINGS['min_signal_strength']:
                if buy_signal and rsi_buy_ok:
                    direction = 'LONG'
                elif sell_signal:
                    direction = 'SHORT'

            details = {
                'signal_strength': points,
                'sma50': latest['sma50'],
                'sma200': latest['sma200'],
                'rsi': latest['rsi'],
                'price': latest['close'],
                'buy_signal': buy_signal,
                'sell_signal': sell_signal,
                'direction': direction,
                'atr': latest['atr'],
            }

            logger.info(f"🔍 تحليل {symbol} انتهى: النقاط {points}, الاتجاه {direction}")

            return points >= self.TRADING_SETTINGS['min_signal_strength'], details, direction

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

            if analysis['signal_strength'] < 50 or analysis['direction'] != direction:
                logger.info(f"❌ إشارة غير صالحة لـ {symbol}: القوة {analysis['signal_strength']}%, الاتجاه {analysis['direction']}")
                return False

            current_price = self.get_current_price(symbol)
            if current_price is None:
                logger.error(f"❌ فشل جلب السعر لـ {symbol}")
                return False

            self.set_leverage(symbol, self.TRADING_SETTINGS['leverage'])
            self.set_margin_type(symbol, self.TRADING_SETTINGS['margin_type'])

            # Scaled trade size between 10 and 20 based on signal strength (50-100 -> 10-20)
            trade_size_usd = self.TRADING_SETTINGS['min_trade_size'] + ((signal_strength - 50) / 50) * (self.TRADING_SETTINGS['max_trade_size'] - self.TRADING_SETTINGS['min_trade_size'])

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
                        f"سبب الدخول: {'Golden Cross' if direction == 'LONG' else 'Death Cross'}، RSI {analysis['rsi']:.1f}\n"
                        f"قوة الإشارة: {signal_strength}%\n"
                        f"سعر الدخول: ${avg_price:.4f}\n"
                        f"الكمية: {quantity:.6f}\n"
                        f"الحجم: ${trade_size_usd:.2f}\n"
                        f"الرافعة: {self.TRADING_SETTINGS['leverage']}x\n"
                        f"SMA50/200: {analysis['sma50']:.4f}/{analysis['sma200']:.4f}\n"
                        f"RSI: {analysis['rsi']:.1f}\n"
                        f"ATR: {analysis['atr']:.4f}\n"
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

                settings = self.symbol_settings.get(symbol, {'stop_loss_pct': self.TRADING_SETTINGS['stop_loss_pct'], 'take_profit_pct': self.TRADING_SETTINGS['take_profit_pct']})
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
        
        signal_summary = {}
        
        for symbol in self.symbols:
            try:
                if symbol in self.active_trades:
                    logger.info(f"⏸️ صفقة نشطة بالفعل لـ {symbol}، تخطي الفحص")
                    signal_summary[symbol] = {
                        'status': "صفقة نشطة (تخطي)",
                        'strength': None,
                        'direction': None,
                        'breakdown': None
                    }
                    continue

                has_signal, analysis, direction = self.analyze_symbol(symbol)
                
                signal_strength = analysis.get('signal_strength', 0)
                dir_text = "شراء" if direction == 'LONG' else "بيع" if direction == 'SHORT' else "لا إشارة"
                
                signal_summary[symbol] = {
                    'status': None,
                    'strength': signal_strength,
                    'direction': dir_text,
                    'breakdown': {}  # Simplified, no points breakdown
                }
                
                if has_signal and direction:
                    logger.info(f"✅ إشارة قوية لـ {symbol} - الاتجاه: {direction}، القوة: {analysis['signal_strength']}%")
                    
                    if self.notifier:
                        self.notifier.send_message(
                            f"🔔 <b>إشارة تداول قوية</b>\n"
                            f"العملة: {symbol}\n"
                            f"الاتجاه: {direction}\n"
                            f"قوة الإشارة: {analysis['signal_strength']}%\n"
                            f"السعر الحالي: ${analysis['price']:.4f}\n"
                            f"سبب الإشارة: {'Golden Cross' if direction == 'LONG' else 'Death Cross'}، RSI ({analysis['rsi']:.1f})\n"
                            f"SMA50/200: {analysis['sma50']:.4f}/{analysis['sma200']:.4f}\n"
                            f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                            f'signal_futures_{symbol}'
                        )
                    
                    self.execute_futures_trade(symbol, direction, analysis['signal_strength'], analysis)
                else:
                    logger.info(f"ℹ️ لا توجد إشارة قوية لـ {symbol} - القوة: {analysis.get('signal_strength', 0)}%")

            except Exception as e:
                logger.error(f"❌ خطأ في فحص {symbol}: {e}")
                signal_summary[symbol] = {
                    'status': "خطأ في الفحص",
                    'strength': None,
                    'direction': None,
                    'breakdown': None
                }
                if self.notifier:
                    self.notifier.send_message(
                        f"❌ <b>خطأ في فحص السوق</b>\nالعملة: {symbol}\nالخطأ: {str(e)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'error_scan_{symbol}'
                    )
        
        if self.notifier:
            summary_message = "✅ <b>تم الفحص الدوري</b>\n\n"
            for sym, info in signal_summary.items():
                if info['status']:
                    summary_message += f"{sym}: {info['status']}\n\n"
                else:
                    strength_formatted = f"{info['strength']:.1f}%" if info['strength'] is not None else "0%"
                    direction = info['direction']
                    summary_message += f"{sym}: قوة الإشارة {strength_formatted} ({direction})\n\n"
            summary_message += f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}"
            self.notifier.send_message(summary_message, 'periodic_scan_summary')

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
