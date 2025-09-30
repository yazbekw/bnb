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
            
            if not all_tickers:
                return self.fallback_price_update()
            
            for ticker in all_tickers:
                symbol = ticker.get('symbol')
                if symbol in self.symbols:
                    price = float(ticker.get('price', 0))
                    if price > 0:
                        self.prices[symbol] = price
                        self.last_update[symbol] = time.time()
                        success_count += 1
            
            if success_count == 0:
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

class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
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
            
            # تنظيف الرسائل القديمة
            expired_messages = [
                msg_hash for msg_hash, timestamp in self.recent_messages.items()
                if current_time - timestamp > self.message_cooldown * 2
            ]
            for msg_hash in expired_messages:
                del self.recent_messages[msg_hash]

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
                    
                    time.sleep(delay * (2 ** attempt))
                        
                except Exception as e:
                    logger.error(f"❌ فشل إرسال الإشعار (محاولة {attempt+1}): {e}")
                    time.sleep(delay * (2 ** attempt))
            
            logger.error(f"❌ فشل إرسال الإشعار بعد {retries} محاولات")
            return False
                
        except Exception as e:
            logger.error(f"❌ General error in Telegram sending: {e}")
            return False

class FuturesTradingBot:
    _instance = None
    
    # الإعدادات المثلى من الاستراتيجية
    OPTIMAL_SETTINGS = {
        'symbols': ["LINKUSDT", "SOLUSDT", "ETHUSDT", "BNBUSDT"],
        'intervals': ['30m', '1h'],
        'weights': {'LINKUSDT': 1.4, 'SOLUSDT': 1.2, 'ETHUSDT': 1.0, 'BNBUSDT': 0.7},
    }
    
    # تخصيص رأس المال (50 دولار)
    TOTAL_CAPITAL = 50
    WEIGHT_SUM = sum(OPTIMAL_SETTINGS['weights'].values())
    CAPITAL_ALLOCATION = {symbol: (weight / WEIGHT_SUM) * TOTAL_CAPITAL for symbol, weight in OPTIMAL_SETTINGS['weights'].items()}

    TRADING_SETTINGS = {
        'base_trade_size': 10,
        'max_leverage': 10,
        'margin_type': 'ISOLATED',
        'max_active_trades': 4,
        'data_interval': '30m',
        'rescan_interval_minutes': 15,
        'price_update_interval': 10,
        'trade_timeout_hours': 4.0,
    }

    @classmethod
    def get_instance(cls):
        return cls._instance

    def __init__(self):
        if FuturesTradingBot._instance is not None:
            raise Exception("هذه الفئة تستخدم نمط Singleton")

        self.WEIGHT_SUM = sum(self.OPTIMAL_SETTINGS['weights'].values())
        self.CAPITAL_ALLOCATION = {
            symbol: (weight / self.WEIGHT_SUM) * self.TOTAL_CAPITAL 
            for symbol, weight in self.OPTIMAL_SETTINGS['weights'].items()
        }
        
        self.api_key = os.environ.get('BINANCE_API_KEY')
        self.api_secret = os.environ.get('BINANCE_API_SECRET')
        self.telegram_token = os.environ.get('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = os.environ.get('TELEGRAM_CHAT_ID')

        if not all([self.api_key, self.api_secret]):
            raise ValueError("مفاتيح Binance مطلوبة")

        self.notifier = None
        if self.telegram_token and self.telegram_chat_id:
            try:
                self.notifier = TelegramNotifier(self.telegram_token, self.telegram_chat_id)
                logger.info("✅ تهيئة Telegram Notifier ناجحة")
            except Exception as e:
                logger.error(f"❌ فشل تهيئة Telegram: {e}")
                self.notifier = None

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

        self.symbols = self.OPTIMAL_SETTINGS['symbols']
        self.verify_symbols_availability()
    
        self.active_trades = {}
        self.price_manager = PriceManager(self.symbols, self.client)
        self.load_existing_trades()
        self.start_price_updater()
    
        if self.active_trades and self.notifier:
            threading.Timer(10, self.test_notifications).start()
    
        self.send_startup_message()
        FuturesTradingBot._instance = self
        logger.info("✅ تم تهيئة البوت بنجاح")

    def verify_symbols_availability(self):
        try:
            exchange_info = self.client.futures_exchange_info()
            available_symbols = [s['symbol'] for s in exchange_info['symbols']]
        
            valid_symbols = [s for s in self.symbols if s in available_symbols]
            if len(valid_symbols) != len(self.symbols):
                logger.warning(f"⚠️ تصحيح الرموز من {self.symbols} إلى {valid_symbols}")
                self.symbols = valid_symbols
                if self.notifier:
                    self.notifier.send_message(
                        f"⚠️ <b>تصحيح الرموز</b>\nالرموز الأصلية: {', '.join(self.OPTIMAL_SETTINGS['symbols'])}\nالرموز المتاحة: {', '.join(valid_symbols)}",
                        'symbols_correction'
                    )
            
        except Exception as e:
            logger.error(f"❌ خطأ في التحقق من الرموز: {e}")

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
            raise

    def send_startup_message(self):
        if self.notifier:
            try:
                symbols_count = len(self.symbols)
                active_trades_count = len(self.active_trades)
            
                message = (
                    "🚀 <b>بدء تشغيل بوت العقود الآجلة</b>\n\n"
                    f"📊 <b>الإعدادات:</b>\n"
                    f"• الرموز: {', '.join(self.symbols)}\n"
                    f"• الأوزان: {self.OPTIMAL_SETTINGS['weights']}\n"
                    f"• رأس المال: {self.TOTAL_CAPITAL} دولار\n"
                    f"• التوزيع: {self.CAPITAL_ALLOCATION}\n"
                    f"• الصفقات النشطة: {active_trades_count}\n\n"
                    f"🕒 <b>وقت البدء:</b>\n"
                    f"{datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"⏰ توقيت: دمشق"
                )
            
                self.notifier.send_message(message, 'startup')
                logger.info("✅ تم إرسال رسالة بدء التشغيل")
                
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
                            'trade_type': 'futures',
                        }
                        
                        self.active_trades[symbol] = trade_data
                        logger.info(f"✅ تم تحميل صفقة عقود: {symbol} - {side} - كمية: {abs(quantity)}")
            
            if not open_positions:
                logger.info("⚠️ لا توجد صفقات مفتوحة في العقود")
                
        except Exception as e:
            logger.error(f"❌ خطأ في تحميل الصفقات العقود: {e}")

    def get_current_price(self, symbol):
        try:
            price = self.price_manager.get_price(symbol)
            if price:
                return price
            logger.error(f"❌ فشل جلب سعر {symbol}")
            return None
        except Exception as e:
            logger.error(f"❌ خطأ في جلب سعر {symbol}: {e}")
            return None

    def set_leverage(self, symbol, leverage):
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            logger.info(f"✅ ضبط الرافعة لـ {symbol} إلى {leverage}x")
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

    def get_historical_data(self, symbol, interval='1h', limit=100):
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
            data['open'] = data['open'].astype(float)
            
            return data
        except Exception as e:
            logger.error(f"❌ خطأ في جلب البيانات لـ {symbol}: {e}")
            return None

    def calculate_indicators(self, data):
        try:
            df = data.copy()
            if len(df) < 50:
                return df

            # المتوسطات المتحركة
            df['sma10'] = df['close'].rolling(10).mean()
            df['sma50'] = df['close'].rolling(50).mean()
            df['sma20'] = df['close'].rolling(20).mean()
            
            # RSI
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0)
            loss = (-delta).where(delta < 0, 0)
            avg_gain = gain.rolling(14).mean()
            avg_loss = loss.rolling(14).mean()
            rs = avg_gain / (avg_loss + 1e-10)
            df['rsi'] = 100 - (100 / (1 + rs))
            
            # ATR
            high_low = df['high'] - df['low']
            high_close = np.abs(df['high'] - df['close'].shift())
            low_close = np.abs(df['low'] - df['close'].shift())
            tr = np.maximum(np.maximum(high_low, high_close), low_close)
            df['atr'] = tr.rolling(14).mean()
            
            # الزخم
            df['momentum'] = df['close'] / df['close'].shift(5) - 1
            
            # نسبة الحجم
            df['volume_ratio'] = df['volume'] / df['volume'].rolling(20).mean()
            
            return df.dropna()
        except Exception as e:
            logger.error(f"❌ خطأ في حساب المؤشرات: {e}")
            return data

    def analyze_symbol(self, symbol):
        try:
            data = self.get_historical_data(symbol, self.TRADING_SETTINGS['data_interval'])
            if data is None or len(data) < 50:
                logger.warning(f"⚠️ بيانات غير كافية لتحليل {symbol}")
                return False, {}, None

            data = self.calculate_indicators(data)
            if len(data) == 0:
                return False, {}, None

            latest = data.iloc[-1]
            
            # شروط الشراء حسب الاستراتيجية الأصلية
            buy_conditions = [
                (latest['sma10'] > latest['sma50']),
                (latest['sma10'] > latest['sma20']),
                (45 <= latest['rsi'] <= 70),
                (latest['momentum'] > 0.002),
                (latest['volume_ratio'] > 0.9),
            ]
            
            # شروط البيع حسب الاستراتيجية الأصلية
            sell_conditions = [
                (latest['sma10'] < latest['sma50']),
                (latest['sma10'] < latest['sma20']),
                (30 <= latest['rsi'] <= 65),
                (latest['momentum'] < -0.003),
                (latest['volume_ratio'] > 1.1),
            ]
            
            buy_signal = sum(buy_conditions) >= 3
            sell_signal = sum(sell_conditions) >= 3
            
            direction = None
            signal_strength = max(sum(buy_conditions), sum(sell_conditions))
            
            if buy_signal:
                direction = 'LONG'
            elif sell_signal:
                direction = 'SHORT'

            details = {
                'signal_strength': signal_strength * 20,  # تحويل إلى نسبة مئوية
                'sma10': latest['sma10'],
                'sma20': latest['sma20'],
                'sma50': latest['sma50'],
                'rsi': latest['rsi'],
                'price': latest['close'],
                'atr': latest['atr'],
                'momentum': latest['momentum'],
                'volume_ratio': latest['volume_ratio'],
                'buy_conditions_met': sum(buy_conditions),
                'sell_conditions_met': sum(sell_conditions),
                'direction': direction,
            }

            logger.info(f"🔍 تحليل {symbol}: {direction} (قوة: {signal_strength * 20}%)")

            return direction is not None, details, direction

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
                return False

            if symbol in self.active_trades:
                logger.info(f"⏸️ صفقة نشطة بالفعل لـ {symbol}")
                return False

            current_price = self.get_current_price(symbol)
            if current_price is None:
                logger.error(f"❌ فشل جلب السعر لـ {symbol}")
                return False

            # إعداد الرافعة والهامش
            self.set_leverage(symbol, self.TRADING_SETTINGS['max_leverage'])
            self.set_margin_type(symbol, self.TRADING_SETTINGS['margin_type'])

            # حساب حجم الصفقة (10 دولار كحد أقصى حسب الاستراتيجية)
            trade_size_usd = min(self.TRADING_SETTINGS['base_trade_size'], self.CAPITAL_ALLOCATION[symbol])

            precision_info = self.get_futures_precision(symbol)
            quantity = trade_size_usd / current_price
            step_size = precision_info['step_size']
            precision = precision_info['precision']
            quantity = round(quantity / step_size) * step_size
            quantity = round(quantity, precision)

            if quantity <= 0 or quantity < precision_info['min_qty']:
                logger.error(f"❌ كمية غير صالحة: {quantity} لـ {symbol}")
                return False

            # حساب الرافعة الديناميكية حسب الاستراتيجية
            symbol_weight = self.OPTIMAL_SETTINGS['weights'][symbol]
            atr = analysis['atr']
            leverage = min(5 / max(atr, 1e-10), 10) * symbol_weight
            leverage = min(leverage, self.TRADING_SETTINGS['max_leverage'])
            leverage = max(leverage, 1)

            self.set_leverage(symbol, int(leverage))

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
                    'leverage': leverage,
                    'side': direction,
                    'timestamp': datetime.now(damascus_tz),
                    'status': 'open',
                    'order_id': order['orderId'],
                    'trade_type': 'futures',
                    'atr': atr,
                    'stop_loss': avg_price - (atr * 1.0) if direction == 'LONG' else avg_price + (atr * 1.0),
                    'take_profit': avg_price + (atr * 2.0) if direction == 'LONG' else avg_price - (atr * 2.0),
                }
                
                self.active_trades[symbol] = trade_data

                if self.notifier:
                    self.notifier.send_message(
                        f"🚀 <b>فتح صفقة عقود جديدة</b>\n"
                        f"العملة: {symbol}\n"
                        f"الاتجاه: {direction}\n"
                        f"الوزن: {symbol_weight}\n"
                        f"شروط الدخول: {analysis['buy_conditions_met'] if direction == 'LONG' else analysis['sell_conditions_met']}/5\n"
                        f"سعر الدخول: ${avg_price:.4f}\n"
                        f"الكمية: {quantity:.6f}\n"
                        f"الحجم: ${trade_size_usd:.2f}\n"
                        f"الرافعة: {leverage:.1f}x\n"
                        f"وقف الخسارة: ${trade_data['stop_loss']:.4f}\n"
                        f"جني الأرباح: ${trade_data['take_profit']:.4f}\n"
                        f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'trade_open_futures_{symbol}'
                    )

                logger.info(f"✅ فتح صفقة عقود {symbol} {direction} بسعر {avg_price}")
                return True

            logger.info(f"❌ فشل تنفيذ صفقة عقود {symbol}: حالة الأمر {order['status']}")
            return False

        except Exception as e:
            logger.error(f"❌ خطأ في تنفيذ صفقة عقود {symbol}: {e}")
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
            
            removed_trades = set(self.active_trades.keys()) - set(current_positions.keys())
            for symbol in removed_trades:
                logger.info(f"🔄 إزالة صفقة مغلقة من القائمة: {symbol}")
            
            self.active_trades = current_positions
            
        except Exception as e:
            logger.error(f"❌ خطأ في تحديث الصفقات النشطة: {e}")

    def manage_futures_trades(self):
        if not self.active_trades:
            return
        
        self.update_active_trades()
        
        if not self.active_trades:
            return
        
        for symbol, trade in list(self.active_trades.items()):
            try:
                current_price = self.get_current_price(symbol)
                if current_price is None:
                    continue

                # فحص إذا كان المركز مغلق
                try:
                    position_info = self.client.futures_position_information(symbol=symbol)
                    if position_info:
                        current_position_amt = float(position_info[0]['positionAmt'])
                        if current_position_amt == 0:
                            logger.info(f"🔄 المركز مغلق فعلياً لـ {symbol}")
                            if symbol in self.active_trades:
                                del self.active_trades[symbol]
                            continue
                except Exception as e:
                    logger.error(f"❌ خطأ في التحقق من المركز لـ {symbol}: {e}")

                # إدارة وقف الخسارة وجني الأرباح حسب الاستراتيجية
                if trade['side'] == 'LONG':
                    if current_price <= trade['stop_loss']:
                        self.close_futures_trade(symbol, current_price, 'Stop Loss')
                    elif current_price >= trade['take_profit']:
                        self.close_futures_trade(symbol, current_price, 'Take Profit')
                else:  # SHORT
                    if current_price >= trade['stop_loss']:
                        self.close_futures_trade(symbol, current_price, 'Stop Loss')
                    elif current_price <= trade['take_profit']:
                        self.close_futures_trade(symbol, current_price, 'Take Profit')

                # فحص انتهاء الوقت
                trade_age = datetime.now(damascus_tz) - trade['timestamp']
                if trade_age.total_seconds() > self.TRADING_SETTINGS['trade_timeout_hours'] * 3600:
                    self.close_futures_trade(symbol, current_price, 'Timeout')

            except Exception as e:
                logger.error(f"❌ خطأ في إدارة صفقة عقود {symbol}: {e}")

    def close_futures_trade(self, symbol, current_price, reason):
        try:
            if symbol not in self.active_trades:
                return True

            trade = self.active_trades[symbol]

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
                    
                    # حساب الربح/الخسارة
                    if trade['side'] == 'LONG':
                        pnl_percent = ((exit_price - trade['entry_price']) / trade['entry_price']) * 100 * trade['leverage']
                    else:
                        pnl_percent = ((trade['entry_price'] - exit_price) / trade['entry_price']) * 100 * trade['leverage']
                    
                    pnl_usd = (pnl_percent / 100) * (trade['quantity'] * trade['entry_price'])
                    
                    emoji = "✅" if pnl_percent > 0 else "❌"
                    
                    if self.notifier:
                        self.notifier.send_message(
                            f"{emoji} <b>إغلاق صفقة عقود</b>\n"
                            f"العملة: {symbol}\n"
                            f"الاتجاه: {trade['side']}\n"
                            f"سبب الخروج: {reason}\n"
                            f"سعر الدخول: ${trade['entry_price']:.4f}\n"
                            f"سعر الخروج: ${exit_price:.4f}\n"
                            f"الرافعة: {trade['leverage']:.1f}x\n"
                            f"P&L: {pnl_percent:.2f}% (${pnl_usd:.2f})\n"
                            f"المدة: {int((datetime.now(damascus_tz) - trade['timestamp']).total_seconds() / 60)} دقيقة\n"
                            f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                            f'trade_close_futures_{symbol}'
                        )
                    
                    logger.info(f"✅ إغلاق صفقة عقود {symbol}: {reason}, P&L: {pnl_percent:.2f}%")
                    
                    if symbol in self.active_trades:
                        del self.active_trades[symbol]
                    
                    return True
                    
            except Exception as e:
                logger.error(f"❌ فشل إغلاق صفقة {symbol}: {e}")
                return False

        except Exception as e:
            logger.error(f"❌ خطأ في إغلاق صفقة عقود {symbol}: {e}")
            return False

    def scan_market(self):
        if len(self.active_trades) >= self.TRADING_SETTINGS['max_active_trades']:
            logger.info(f"⏸️ الحد الأقصى للصفقات النشطة ({self.TRADING_SETTINGS['max_active_trades']})")
            return
        
        logger.info("🔍 بدء فحص السوق للعقود الآجلة...")
        
        for symbol in self.symbols:
            try:
                if symbol in self.active_trades:
                    continue

                has_signal, analysis, direction = self.analyze_symbol(symbol)
                
                if has_signal and direction:
                    logger.info(f"✅ إشارة قوية لـ {symbol} - الاتجاه: {direction}")
                    
                    if self.notifier:
                        self.notifier.send_message(
                            f"🔔 <b>إشارة تداول قوية</b>\n"
                            f"العملة: {symbol}\n"
                            f"الاتجاه: {direction}\n"
                            f"شروط الدخول: {analysis['buy_conditions_met'] if direction == 'LONG' else analysis['sell_conditions_met']}/5\n"
                            f"السعر الحالي: ${analysis['price']:.4f}\n"
                            f"SMA10/20/50: {analysis['sma10']:.4f}/{analysis['sma20']:.4f}/{analysis['sma50']:.4f}\n"
                            f"RSI: {analysis['rsi']:.1f}\n"
                            f"ATR: {analysis['atr']:.4f}\n"
                            f"الزخم: {analysis['momentum']:.4f}\n"
                            f"نسبة الحجم: {analysis['volume_ratio']:.2f}\n"
                            f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                            f'signal_futures_{symbol}'
                        )
                    
                    self.execute_futures_trade(symbol, direction, analysis['signal_strength'], analysis)

            except Exception as e:
                logger.error(f"❌ خطأ في فحص {symbol}: {e}")

    def run(self):
        logger.info("🚀 بدء تشغيل بوت العقود الآجلة...")
        
        schedule.every(self.TRADING_SETTINGS['rescan_interval_minutes']).minutes.do(self.scan_market)
        schedule.every(5).minutes.do(self.manage_trades)
        schedule.every(10).minutes.do(self.update_active_trades)
        
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
