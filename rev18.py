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

    def send_message(self, message, message_type='info', retries=3, delay=5):
        """إرسال رسالة إلى Telegram مع إعادة محاولة وتقليل منع التكرار للإشعارات الهامة"""
        try:
            current_time = time.time()
            last_sent = self.notification_types.get(message_type, 0)
            
            # تعديل: 0 للأخطاء والهامة، 10 ثوانٍ للآخرين
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
                        if response.status_code == 429:  # Too Many Requests
                            time.sleep(delay * (2 ** attempt))  # Exponential backoff
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
        'data_interval': '5m',
        'rescan_interval_minutes': 10,
        'trade_timeout_hours': 0.5,
        'extended_timeout_hours': 0.75,
        'extended_take_profit_multiplier': 0.5,
        'price_update_interval': 5,
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

        # تهيئة notifier أولاً قبل أي شيء آخر
        self.notifier = None
        if self.telegram_token and self.telegram_chat_id:
            for attempt in range(3):
                try:
                    self.notifier = TelegramNotifier(self.telegram_token, self.telegram_chat_id)
                    self.notifier.send_message("🔧 <b>تهيئة البوت</b>\nجاري بدء التشغيل...\nالتاريخ: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}", 'init')
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

        # الآن تهيئة العميل واختبار الاتصال
        try:
            self.client = Client(self.api_key, self.api_secret)
            self.test_api_connection()  # الآن notifier مهيأ
        except Exception as e:
            logger.error(f"❌ فشل تهيئة العميل: {e}")
            if self.notifier:
                self.notifier.send_message(
                    f"❌ <b>فشل تهيئة العميل</b>\nالخطأ: {str(e)}",
                    'error_client_init'
                )
            raise

        # باقي الكود كما هو...
        self.symbols = ["ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT"]
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
                
            # تصحيح الرموز إذا لزم الأمر
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
                self.notifier.send_message(
                    f"🔧 <b>حالة البوت للتصحيح</b>\nعدد الصفقات النشطة: {len(self.active_trades)}\nTelegram Notifier: موجود\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
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
                    f"🔧 <b>اختبار التتبع (عقود)</b>\nالعملة: {symbol}\nتم تحميل الصفقة بنجاح\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'test_futures_{symbol}'
                )

    def test_api_connection(self):
        try:
            server_time = self.client.futures_time()
            logger.info(f"✅ اتصال Binance API نشط - وقت الخادم: {server_time}")
            if self.notifier:  # تحقق من وجود notifier قبل استخدامه
                self.notifier.send_message(
                    f"✅ <b>اتصال Binance API نشط</b>\nوقت الخادم: {server_time}\nالوقت المحلي: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    'api_connection_test'
                )
            return True
        except Exception as e:
            logger.error(f"❌ فشل الاتصال بـ Binance API: {e}")
            if self.notifier:  # تحقق من وجود notifier قبل استخدامه
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
                self.notifier.send_message(
                    f"🚀 <b>بدء تشغيل بوت العقود الآجلة</b>\n"
                    f"📊 العملات المتاحة: {', '.join(self.symbols)}\n"
                    f"💼 الرافعة الافتراضية: {self.TRADING_SETTINGS['leverage']}x\n"
                    f"⏰ الوقت الحالي: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"✅ عدد الصفقات النشطة عند البدء: {len(self.active_trades)}\n"
                    f"🔧 المؤشرات المستخدمة: EMA5/13, RSI (overbought: {self.TRADING_SETTINGS['rsi_overbought']}, oversold: {self.TRADING_SETTINGS['rsi_oversold']}), MACD\n"
                    f"📈 الحد الأقصى للصفقات النشطة: {self.TRADING_SETTINGS['max_active_trades']}\n"
                    f"⏱️ فترة فحص السوق: كل {self.TRADING_SETTINGS['rescan_interval_minutes']} دقائق",
                    'startup'
                )
            except Exception as e:
                logger.error(f"❌ فشل إرسال رسالة البدء: {e}")

    def start_price_updater(self):
        def price_update_thread():
            while True:
                try:
                    self.price_manager.update_prices()
                    if self.notifier:
                        self.notifier.send_message(
                            f"🔄 <b>تحديث الأسعار الدوري</b>\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}\nالرموز المحدثة: {', '.join(self.symbols)}",
                            'price_update'
                        )
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
        if self.notifier:
            self.notifier.send_message(
                "✅ <b>بدء تحديث الأسعار الدوري</b>\nالفاصل الزمني: كل {self.TRADING_SETTINGS['price_update_interval']} دقائق",
                'start_price_updater'
            )

    def load_existing_trades(self):
        try:
            account_info = self.client.futures_account()
            positions = account_info['positions']
            
            open_positions = [p for p in positions if float(p['positionAmt']) != 0]
            
            logger.info(f"🔍 العثور على {len(open_positions)} مركز مفتوح في العقود")
            if self.notifier:
                self.notifier.send_message(
                    f"🔍 <b>تحميل الصفقات المفتوحة</b>\nعدد المراكز المفتوحة: {len(open_positions)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    'load_trades_start'
                )
            
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
                        
                        if self.notifier:
                            self.notifier.send_message(
                                f"📥 <b>صفقة عقود مفتوحة محملة</b>\n"
                                f"العملة: {symbol}\n"
                                f"الاتجاه: {side}\n"
                                f"الكمية: {abs(quantity):.6f}\n"
                                f"سعر الدخول: ${entry_price:.4f}\n"
                                f"الرافعة: {leverage}x\n"
                                f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                                f'load_futures_{symbol}'
                            )
            
            if not open_positions:
                logger.info("⚠️ لا توجد صفقات مفتوحة في العقود")
                if self.notifier:
                    self.notifier.send_message(
                        "⚠️ <b>لا توجد صفقات مفتوحة</b>\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        'no_open_trades'
                    )
                
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
                    if self.notifier:
                        self.notifier.send_message(
                            f"💰 <b>جلب سعر حالي</b>\nالعملة: {symbol}\nالسعر: ${price}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                            f'get_price_{symbol}'
                        )
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
            
            if self.notifier:
                self.notifier.send_message(
                    f"⚙️ <b>ضبط الرافعة</b>\nالعملة: {symbol}\nالرافعة الجديدة: {leverage}x\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'leverage_{symbol}'
                )
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
            if self.notifier:
                self.notifier.send_message(
                    f"⚙️ <b>ضبط نوع الهامش</b>\nالعملة: {symbol}\nالنوع الجديد: {margin_type}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'margin_type_{symbol}'
                )
            return True
        except Exception as e:
            if "No need to change margin type" in str(e):
                logger.info(f"ℹ️ نوع الهامش مضبوط مسبقاً لـ {symbol}")
                if self.notifier:
                    self.notifier.send_message(
                        f"ℹ️ <b>نوع الهامش مضبوط مسبقاً</b>\nالعملة: {symbol}\nالنوع: {margin_type}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'margin_type_info_{symbol}'
                    )
                return True
            if "margin type cannot be changed" in str(e):
                logger.warning(f"⚠️ لا يمكن تغيير نوع الهامش لـ {symbol} بسبب مركز مفتوح")
                if self.notifier:
                    self.notifier.send_message(
                        f"⚠️ <b>فشل تغيير نوع الهامش</b>\nالعملة: {symbol}\nالسبب: مركز مفتوح\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'error_margin_change_{symbol}'
                    )
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
            if self.notifier:
                self.notifier.send_message(
                    f"📊 <b>جلب البيانات التاريخية</b>\nالعملة: {symbol}\nالفاصل: {interval}\nعدد الشموع: {limit}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'historical_data_{symbol}'
                )
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
            if len(df) < 26:  # زيادة الحد الأدنى لـ MACD
                logger.warning("⚠️ البيانات غير كافية لحساب المؤشرات")
                if self.notifier:
                    self.notifier.send_message(
                        f"⚠️ <b>البيانات غير كافية</b>\nعدد الشموع: {len(df)}\nالحد الأدنى المطلوب: 26\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        'insufficient_data'
                    )
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
            
            # حساب ATR (Average True Range)
            high_low = df['high'] - df['low']
            high_close = np.abs(df['high'] - df['close'].shift())
            low_close = np.abs(df['low'] - df['close'].shift())
            ranges = pd.concat([high_low, high_close, low_close], axis=1)
            true_range = np.max(ranges, axis=1)
            df['atr'] = true_range.rolling(14).mean()
            
            # حساب متوسط الحجم
            df['volume_avg'] = df['volume'].rolling(14).mean()
            
            logger.info("✅ تم حساب المؤشرات بنجاح")
            if self.notifier:
                self.notifier.send_message(
                    f"🔢 <b>حساب المؤشرات</b>\nعدد الشموع: {len(df)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    'calculate_indicators'
                )
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
                if self.notifier:
                    self.notifier.send_message(
                        f"⚠️ <b>بيانات غير كافية للتحليل</b>\nالعملة: {symbol}\nعدد الشموع: {len(data) if data else 0}\nالحد الأدنى: 26\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'insufficient_data_{symbol}'
                    )
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

            # تقييم قوة الإشارة مع أوزان ديناميكية
            # EMA
            if buy_signal:
                ema_diff = (latest['ema5'] - latest['ema13']) / latest['ema13'] * 100
                ema_points = min(30, max(10, 20 + ema_diff * 5))
                signal_strength += ema_points
            if sell_signal:
                ema_diff = (latest['ema13'] - latest['ema5']) / latest['ema13'] * 100
                ema_points = min(30, max(10, 20 + ema_diff * 5))
                signal_strength += ema_points

            # RSI
            if rsi_buy_ok and buy_signal:
                rsi_strength = (self.TRADING_SETTINGS['rsi_oversold'] + 10 - latest['rsi']) / (self.TRADING_SETTINGS['rsi_oversold'] + 10) * 20
                rsi_points = min(20, max(5, rsi_strength))
                signal_strength += rsi_points
            if rsi_sell_ok and sell_signal:
                rsi_strength = (latest['rsi'] - (self.TRADING_SETTINGS['rsi_overbought'] - 10)) / (100 - (self.TRADING_SETTINGS['rsi_overbought'] - 10)) * 20
                rsi_points = min(20, max(5, rsi_strength))
                signal_strength += rsi_points

            # MACD
            if macd_buy_signal and macd_above_zero:
                macd_strength = abs(latest['macd_histogram']) / max(0.001, abs(latest['macd'])) * 25
                macd_points = min(25, max(10, macd_strength))
                signal_strength += macd_points
            if macd_sell_signal and macd_below_zero:
                macd_strength = abs(latest['macd_histogram']) / max(0.001, abs(latest['macd'])) * 25
                macd_points = min(25, max(10, macd_strength))
                signal_strength += macd_points

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
                'direction': direction,
                'atr': latest['atr'],
                'volume': latest['volume'],
                'volume_avg': latest['volume_avg']
            }

            logger.info(f"🔍 تحليل {symbol} انتهى: الاتجاه {direction}, القوة {signal_strength}%")
            if self.notifier:
                self.notifier.send_message(
                    f"🔍 <b>تحليل رمز</b>\nالعملة: {symbol}\n"
                    f"الاتجاه المقترح: {direction if direction else 'لا اتجاه'}\n"
                    f"قوة الإشارة: {signal_strength}%\n"
                    f"EMA5: {latest['ema5']:.4f}, EMA13: {latest['ema13']:.4f}\n"
                    f"RSI: {latest['rsi']:.1f}\n"
                    f"MACD: {latest['macd']:.4f}, إشارة MACD: {latest['macd_signal']:.4f}, هيستوغرام: {latest['macd_histogram']:.4f}\n"
                    f"ATR: {latest['atr']:.4f}\n"
                    f"الحجم: {latest['volume']:.2f}, متوسط الحجم: {latest['volume_avg']:.2f}\n"
                    f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'analyze_symbol_{symbol}'
                )

            return signal_strength >= 70, details, direction

        except Exception as e:
            logger.error(f"❌ خطأ في تحليل {symbol}: {e}")
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
                if self.notifier:
                    self.notifier.send_message(
                        f"📏 <b>جلب دقة العقود</b>\nالعملة: {symbol}\nالدقة: {precision_info['precision']}\nالحد الأدنى للكمية: {precision_info['min_qty']}\nالحد الأدنى للقيمة: {precision_info['min_notional']}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'futures_precision_{symbol}'
                    )
                return precision_info
            
            logger.warning(f"⚠️ معلومات الرمز غير متوفرة لـ {symbol}")
            if self.notifier:
                self.notifier.send_message(
                    f"⚠️ <b>معلومات الرمز غير متوفرة</b>\nالعملة: {symbol}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'warning_precision_{symbol}'
                )
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
                if self.notifier:
                    self.notifier.send_message(
                        f"⚠️ <b>إلغاء صفقة بسبب الحد الأقصى</b>\nالعملة: {symbol}\nالحد الأقصى: {self.TRADING_SETTINGS['max_active_trades']}\nالصفقات النشطة الحالية: {len(self.active_trades)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'cancel_futures_{symbol}'
                    )
                return False

            if analysis['signal_strength'] < 70 or analysis['direction'] != direction:
                logger.info(f"❌ إشارة غير صالحة لـ {symbol}: القوة {analysis['signal_strength']}%, الاتجاه {analysis['direction']}")
                if self.notifier:
                    self.notifier.send_message(
                        f"❌ <b>إشارة غير صالحة</b>\nالعملة: {symbol}\nقوة الإشارة: {analysis['signal_strength']}%\nالاتجاه: {analysis['direction']}\nالسبب: قوة أقل من 70% أو اتجاه غير مطابق\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'invalid_signal_{symbol}'
                    )
                return False

            current_price = self.get_current_price(symbol)
            if current_price is None:
                logger.error(f"❌ فشل جلب السعر لـ {symbol}")
                return False

            self.set_leverage(symbol, self.TRADING_SETTINGS['leverage'])
            self.set_margin_type(symbol, self.TRADING_SETTINGS['margin_type'])

            # حساب الحجم الأساسي بناءً على قوة الإشارة
            base_trade_size_usd = self.TRADING_SETTINGS['min_trade_size'] + (signal_strength / 100) * (self.TRADING_SETTINGS['max_trade_size'] - self.TRADING_SETTINGS['min_trade_size'])

            # تعديل الحجم بناءً على عوامل خارجية (ATR و Volume) لزيادة الحجم ضمن الهامش 10-30
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
                if self.notifier:
                    self.notifier.send_message(
                        f"📏 <b>تعديل حجم الصفقة</b>\nالعملة: {symbol}\nالحجم الجديد: ${trade_size_usd:.2f}\nالسبب: تلبية الحد الأدنى\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'trade_size_adjust_{symbol}'
                    )

            if trade_size_usd > self.TRADING_SETTINGS['max_trade_size']:
                logger.warning(f"⚠️ حجم الصفقة {trade_size_usd:.2f} يتجاوز الحد الأقصى، تخطي {symbol}")
                if self.notifier:
                    self.notifier.send_message(
                        f"⚠️ <b>تجاوز حد الحجم</b>\nالعملة: {symbol}\nالحجم المحسوب: ${trade_size_usd:.2f}\nالحد الأقصى: {self.TRADING_SETTINGS['max_trade_size']}\nالسبب: تخطي الصفقة\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'max_size_exceeded_{symbol}'
                    )
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

    def manage_futures_trades(self):
        if not self.active_trades:
            logger.info("🔍 لا توجد صفقات نشطة في العقود")
            if self.notifier:
                self.notifier.send_message(
                    f"🔍 <b>إدارة الصفقات</b>\nلا توجد صفقات نشطة\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    'manage_trades_no_active'
                )
            return
        
        logger.info(f"🔍 إدارة {len(self.active_trades)} صفقة عقود نشطة")
        if self.notifier:
            self.notifier.send_message(
                f"🔍 <b>بدء إدارة الصفقات</b>\nعدد الصفقات النشطة: {len(self.active_trades)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                'manage_trades_start'
            )
        
        for symbol, trade in list(self.active_trades.items()):
            try:
                current_price = self.get_current_price(symbol)
                if current_price is None:
                    continue

                if trade['side'] == 'LONG':
                    pnl_percent = ((current_price - trade['entry_price']) / trade['entry_price']) * 100
                    if current_price > trade['highest_price']:
                        trade['highest_price'] = current_price
                        if self.notifier:
                            self.notifier.send_message(
                                f"📈 <b>تحديث أعلى سعر</b>\nالعملة: {symbol}\nالأعلى الجديد: ${current_price:.4f}\nP&L الحالي: {pnl_percent:.2f}%\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                                f'update_highest_price_{symbol}'
                            )
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
                            if self.notifier:
                                self.notifier.send_message(
                                    f"🔄 <b>تحديث سعر التتبع</b>\nالعملة: {symbol}\nسعر التتبع الجديد: ${new_trail:.4f}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                                    f'trail_update_{symbol}'
                                )
                        if current_price <= trade['trail_price']:
                            self.close_futures_trade(symbol, current_price, 'Trailing Stop')
                            continue
                else:
                    pnl_percent = ((trade['entry_price'] - current_price) / trade['entry_price']) * 100
                    if current_price < trade['lowest_price']:
                        trade['lowest_price'] = current_price
                        if self.notifier:
                            self.notifier.send_message(
                                f"📉 <b>تحديث أدنى سعر</b>\nالعملة: {symbol}\nالأدنى الجديد: ${current_price:.4f}\nP&L الحالي: {pnl_percent:.2f}%\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                                f'update_lowest_price_{symbol}'
                            )
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
                            if self.notifier:
                                self.notifier.send_message(
                                    f"🔄 <b>تحديث سعر التتبع</b>\nالعملة: {symbol}\nسعر التتبع الجديد: ${new_trail:.4f}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                                    f'trail_update_{symbol}'
                                )
                        if current_price >= trade['trail_price']:
                            self.close_futures_trade(symbol, current_price, 'Trailing Stop')
                            continue

                settings = self.symbol_settings.get(symbol, {'stop_loss_pct': 1.0, 'take_profit_pct': 1.5})
                stop_loss_pct = settings['stop_loss_pct']
                
                take_profit_pct = settings['take_profit_pct']
                if trade.get('extended', False) and pnl_percent < 0:
                    take_profit_pct *= self.TRADING_SETTINGS['extended_take_profit_multiplier']
                    logger.info(f"🔄 خفض حد جني الأرباح لـ {symbol} إلى {take_profit_pct:.2f}% بسبب التمديد")
                    if self.notifier:
                        self.notifier.send_message(
                            f"🔄 <b>خفض حد جني الأرباح</b>\nالعملة: {symbol}\nالحد الجديد: {take_profit_pct:.2f}%\nالسبب: تمديد صفقة خاسرة\nP&L: {pnl_percent:.2f}%\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                            f'take_profit_reduce_{symbol}'
                        )

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
                    if self.notifier:
                        self.notifier.send_message(
                            f"⏳ <b>تمديد صفقة خاسرة</b>\n"
                            f"العملة: {symbol}\n"
                            f"السبب: P&L سلبي ({pnl_percent:.2f}%)\n"
                            f"الوقت الجديد: {self.TRADING_SETTINGS['extended_timeout_hours']} ساعات\n"
                            f"حد الربح الجديد: {take_profit_pct:.2f}%\n"
                            f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                            f'extend_futures_{symbol}'
                        )
                elif trade_age.total_seconds() > timeout_hours * 3600:
                    self.close_futures_trade(symbol, current_price, 'Timeout')

                if (datetime.now(damascus_tz) - trade['last_notification']).total_seconds() > 3600:
                    if self.notifier:
                        self.notifier.send_message(
                            f"📊 <b>تحديث حالة صفقة</b>\n"
                            f"العملة: {symbol}\n"
                            f"الاتجاه: {trade['side']}\n"
                            f"سعر الدخول: ${trade['entry_price']:.4f}\n"
                            f"السعر الحالي: ${current_price:.4f}\n"
                            f"P&L: {pnl_percent:.2f}%\n"
                            f"المدة: {trade_age.seconds // 60} دقيقة\n"
                            f"ممدد: {'نعم' if trade.get('extended', False) else 'لا'}\n"
                            f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                            f'update_futures_{symbol}'
                        )
                    trade['last_notification'] = datetime.now(damascus_tz)

            except Exception as e:
                logger.error(f"❌ خطأ في إدارة صفقة عقود {symbol}: {e}")
                if self.notifier:
                    self.notifier.send_message(
                        f"❌ <b>خطأ في إدارة صفقة</b>\nالعملة: {symbol}\nالخطأ: {str(e)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'error_manage_trade_{symbol}'
                    )

    def close_futures_trade(self, symbol, current_price, reason):
        try:
            trade = self.active_trades.get(symbol)
            if not trade:
                logger.warning(f"⚠️ لا توجد صفقة للإغلاق: {symbol}")
                if self.notifier:
                    self.notifier.send_message(
                        f"⚠️ <b>لا توجد صفقة للإغلاق</b>\nالعملة: {symbol}\nالسبب: {reason}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'no_trade_close_{symbol}'
                    )
                return False

            side = Client.SIDE_SELL if trade['side'] == 'LONG' else Client.SIDE_BUY
            quantity = trade['quantity']

            # محاولة الإغلاق السوقي الأولية
            try:
                order = self.client.futures_create_order(
                    symbol=symbol,
                    side=side,
                    type=Client.ORDER_TYPE_MARKET,
                    quantity=quantity,
                    reduceOnly=True  # لضمان أنه إغلاق فقط
                )
                if order['status'] == 'FILLED':
                    # حساب PNL وإرسال إشعار (كما في الكود الأصلي)
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
                if "-2019" not in str(e):  # إذا لم يكن نقص هامش، ارجع خطأ عام
                    raise e

            # تسلسل الإنقاذ إذا كان الخطأ -2019 (نقص هامش)
            logger.warning(f"⚠️ نقص هامش لـ {symbol}، بدء تسلسل الإنقاذ...")
            if self.notifier:
                self.notifier.send_message(
                    f"⚠️ <b>نقص هامش، بدء إنقاذ</b>\nالعملة: {symbol}\nالسبب: {reason}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                    f'rescue_start_{symbol}'
                )

            # الخطوة 1: وضع أمر حدي قريب من السعر الحالي
            limit_price = current_price * 0.999 if trade['side'] == 'LONG' else current_price * 1.001  # قريب بنسبة 0.1%
            try:
                limit_order = self.client.futures_create_order(
                    symbol=symbol,
                    side=side,
                    type=Client.ORDER_TYPE_LIMIT,
                    quantity=quantity,
                    price=limit_price,
                    timeInForce=Client.TIME_IN_FORCE_GTC,  # Good Till Cancel
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

            # الخطوة 2: الانتظار 5 دقائق
            time.sleep(300)  # 5 دقائق

            # الخطوة 3: التحقق إذا تم الإغلاق
            position_info = self.client.futures_position_information(symbol=symbol)
            current_position_amt = float(position_info[0]['positionAmt']) if position_info else quantity
            if abs(current_position_amt) == 0:
                logger.info(f"✅ تم إغلاق {symbol} عبر الأمر الحدي")
                del self.active_trades[symbol]
                return True

            # إذا لم يتم الإغلاق، أغلق نصف الكمية
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
                        # تحديث الكمية المتبقية في trade
                        trade['quantity'] -= half_quantity
                except Exception as half_e:
                    logger.error(f"❌ فشل إغلاق نصف: {half_e}")

            # الخطوة 4: حل نهائي - محاولة تغيير إلى CROSS أو نقل هامش إضافي
            try:
                self.set_margin_type(symbol, 'CROSS')
                logger.info(f"✅ تغيير نوع الهامش إلى CROSS لـ {symbol}")
            except Exception as margin_e:
                if "cannot be changed" in str(margin_e):
                    # إذا فشل التغيير بسبب مركز مفتوح، نقل هامش إضافي
                    try:
                        self.client.futures_change_position_margin(
                            symbol=symbol,
                            amount=5.0,  # مبلغ إضافي، قم بتعديله حسب الحاجة
                            type=1  # 1: إضافة هامش
                        )
                        logger.info(f"✅ نقل هامش إضافي 5 USDT لـ {symbol}")
                        if self.notifier:
                            self.notifier.send_message(
                                f"💰 <b>نقل هامش إضافي</b>\nالعملة: {symbol}\nالمبلغ: 5 USDT\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                                f'margin_add_{symbol}'
                            )
                    except Exception as add_e:
                        logger.error(f"❌ فشل نقل هامش: {add_e}")

            # إعادة محاولة الإغلاق السوقي بعد الإنقاذ
            return self.close_futures_trade(symbol, self.get_current_price(symbol), reason)  # استدعاء ذاتي لإعادة المحاولة

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
        if self.notifier:
            self.notifier.send_message(
                f"🔍 <b>بدء فحص السوق</b>\nالرموز: {', '.join(self.symbols)}\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                'scan_market_start'
            )
        
        for symbol in self.symbols:
            try:
                if symbol in self.active_trades:
                    logger.info(f"⏸️ صفقة نشطة بالفعل لـ {symbol}، تخطي الفحص")
                    if self.notifier:
                        self.notifier.send_message(
                            f"⏸️ <b>تخطي فحص رمز</b>\nالعملة: {symbol}\nالسبب: صفقة نشطة بالفعل\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                            f'skip_scan_{symbol}'
                        )
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
                else:
                    logger.info(f"ℹ️ لا توجد إشارة قوية لـ {symbol} - القوة: {analysis.get('signal_strength', 0)}%")
                    if self.notifier:
                        self.notifier.send_message(
                            f"ℹ️ <b>لا إشارة قوية</b>\nالعملة: {symbol}\nقوة الإشارة: {analysis.get('signal_strength', 0)}%\nالوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                            f'no_signal_{symbol}'
                        )

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
        schedule.every().day.at("00:00").do(self.send_daily_status)
        
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
        if self.notifier:
            try:
                self.notifier.send_message(
                    f"📅 <b>تقرير يومي لتأكيد التشغيل</b>\n"
                    f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"عدد الصفقات النشطة: {len(self.active_trades)}\n"
                    f"الرموز المتاحة: {', '.join(self.symbols)}\n"
                    f"حالة الاتصال بـ Binance: {'متصل' if self.price_manager.is_connected() else 'غير متصل'}\n"
                    f"البوت يعمل بشكل طبيعي",
                    'daily_status'
                )
            except Exception as e:
                logger.error(f"❌ خطأ في إرسال التقرير اليومي: {e}")
        else:
            logger.warning("⚠️ لا يمكن إرسال التقرير اليومي: Telegram Notifier غير مُهيأ")

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
