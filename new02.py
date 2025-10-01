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
            for symbol in self.symbols:
                try:
                    ticker = self.client.futures_symbol_ticker(symbol=symbol)
                    price = float(ticker.get('price', 0))
                    if price > 0:
                        self.prices[symbol] = price
                        self.last_update[symbol] = time.time()
                except Exception as e:
                    logger.error(f"❌ خطأ في تحديث سعر {symbol}: {str(e)}")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ خطأ في تحديث الأسعار: {str(e)}")
            return False

    def get_price(self, symbol):
        try:
            last_update = self.last_update.get(symbol, 0)
            if time.time() - last_update > 30:  # تحديث كل 30 ثانية
                self.update_prices()
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
                    return True

            self.recent_messages[message_hash] = current_time
            
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
                            return True
                    
                    time.sleep(delay * (2 ** attempt))
                        
                except Exception as e:
                    time.sleep(delay * (2 ** attempt))
            
            return False
                
        except Exception as e:
            return False

class FuturesTradingBot:
    _instance = None
    
    OPTIMAL_SETTINGS = {
        'symbols': ["LINKUSDT", "SOLUSDT", "ETHUSDT", "BNBUSDT"],
        'intervals': ['30m', '1h'],
        'weights': {'LINKUSDT': 1.4, 'SOLUSDT': 1.2, 'ETHUSDT': 1.0, 'BNBUSDT': 0.7},
    }
    
    TOTAL_CAPITAL = 50

    TRADING_SETTINGS = {
        'base_trade_size': 10,
        'max_leverage': 5,  # خفّضت الرافعة لتقليل المخاطرة
        'margin_type': 'ISOLATED',
        'max_active_trades': 4,
        'data_interval': '30m',
        'rescan_interval_minutes': 15,
        'price_update_interval': 2,
        'trade_timeout_hours': 8.0,  # زدت الوقت إلى 8 ساعات
        'min_signal_conditions': 4,
        'atr_stop_loss_multiplier': 1.5,  # ATR مضاعف وقف الخسارة
        'atr_take_profit_multiplier': 3.0,  # ATR مضاعف جني الأرباح
        'min_trade_duration_minutes': 30,  # أقل مدة للصفقة
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
            raise

        self.symbols = self.OPTIMAL_SETTINGS['symbols']
        self.verify_symbols_availability()
    
        self.active_trades = {}
        self.price_manager = PriceManager(self.symbols, self.client)
        self.symbol_balances = self.CAPITAL_ALLOCATION.copy()
        
        self.load_existing_trades()
        self.start_price_updater()
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
            
        except Exception as e:
            logger.error(f"❌ خطأ في التحقق من الرموز: {e}")

    def test_api_connection(self):
        try:
            server_time = self.client.futures_time()
            logger.info(f"✅ اتصال Binance API نشط")
            return True
        except Exception as e:
            logger.error(f"❌ فشل الاتصال بـ Binance API: {e}")
            raise

    def send_startup_message(self):
        if self.notifier:
            try:
                message = (
                    "🚀 <b>بدء تشغيل بوت العقود الآجلة - الإصدار المحسن</b>\n\n"
                    f"📊 <b>الإعدادات الجديدة:</b>\n"
                    f"• الرافعة: {self.TRADING_SETTINGS['max_leverage']}x\n"
                    f"• وقت الصفقة: {self.TRADING_SETTINGS['trade_timeout_hours']} ساعات\n"
                    f"• وقف الخسارة: {self.TRADING_SETTINGS['atr_stop_loss_multiplier']} x ATR\n"
                    f"• جني الأرباح: {self.TRADING_SETTINGS['atr_take_profit_multiplier']} x ATR\n"
                    f"• أقل مدة: {self.TRADING_SETTINGS['min_trade_duration_minutes']} دقيقة\n\n"
                    f"🕒 <b>وقت البدء:</b>\n"
                    f"{datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}"
                )
            
                self.notifier.send_message(message, 'startup')
                
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

    def load_existing_trades(self):
        try:
            account_info = self.client.futures_account()
            positions = account_info['positions']
            
            open_positions = [p for p in positions if float(p['positionAmt']) != 0]
            
            for position in open_positions:
                symbol = position['symbol']
                if symbol in self.symbols:
                    quantity = float(position['positionAmt'])
                    if quantity != 0:
                        trade_value = abs(quantity) * float(position['entryPrice'])
                        if symbol in self.symbol_balances:
                            self.symbol_balances[symbol] = max(0, self.symbol_balances[symbol] - trade_value)
                        
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
                        logger.info(f"✅ تم تحميل صفقة: {symbol} - {side}")
            
        except Exception as e:
            logger.error(f"❌ خطأ في تحميل الصفقات: {e}")

    def get_current_price(self, symbol):
        return self.price_manager.get_price(symbol)

    def set_leverage(self, symbol, leverage):
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            return True
        except Exception as e:
            logger.error(f"❌ خطأ في ضبط الرافعة: {e}")
            return False

    def set_margin_type(self, symbol, margin_type):
        try:
            self.client.futures_change_margin_type(symbol=symbol, marginType=margin_type)
            return True
        except Exception as e:
            if "No need to change margin type" in str(e):
                return True
            return False

    def get_historical_data(self, symbol, interval='30m', limit=100):
        try:
            klines = self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
            
            data = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                'taker_buy_quote', 'ignore'
            ])
            
            for col in ['open', 'high', 'low', 'close', 'volume']:
                data[col] = data[col].astype(float)
            
            return data
        except Exception as e:
            logger.error(f"❌ خطأ في جلب البيانات لـ {symbol}: {e}")
            return None

    def calculate_indicators(self, data):
        try:
            df = data.copy()
            if len(df) < 50:
                return df

            df['sma10'] = df['close'].rolling(10).mean()
            df['sma50'] = df['close'].rolling(50).mean()
            df['sma20'] = df['close'].rolling(20).mean()
            
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0)
            loss = (-delta).where(delta < 0, 0)
            avg_gain = gain.rolling(14).mean()
            avg_loss = loss.rolling(14).mean()
            rs = avg_gain / (avg_loss + 1e-10)
            df['rsi'] = 100 - (100 / (1 + rs))
            
            high_low = df['high'] - df['low']
            high_close = np.abs(df['high'] - df['close'].shift())
            low_close = np.abs(df['low'] - df['close'].shift())
            tr = np.maximum(np.maximum(high_low, high_close), low_close)
            df['atr'] = tr.rolling(14).mean()
            
            df['momentum'] = df['close'] / df['close'].shift(5) - 1
            df['volume_ratio'] = df['volume'] / df['volume'].rolling(20).mean()
            
            return df.dropna()
        except Exception as e:
            logger.error(f"❌ خطأ في حساب المؤشرات: {e}")
            return data

    def analyze_symbol(self, symbol):
        try:
            data = self.get_historical_data(symbol, self.TRADING_SETTINGS['data_interval'])
            if data is None or len(data) < 50:
                return False, {}, None

            data = self.calculate_indicators(data)
            if len(data) == 0:
                return False, {}, None

            latest = data.iloc[-1]
            
            buy_conditions = [
                (latest['sma10'] > latest['sma50']),
                (latest['sma10'] > latest['sma20']),
                (45 <= latest['rsi'] <= 70),
                (latest['momentum'] > 0.002),
                (latest['volume_ratio'] > 0.9),
            ]
            
            sell_conditions = [
                (latest['sma10'] < latest['sma50']),
                (latest['sma10'] < latest['sma20']),
                (30 <= latest['rsi'] <= 65),
                (latest['momentum'] < -0.003),
                (latest['volume_ratio'] > 1.1),
            ]
            
            buy_signal = sum(buy_conditions) >= self.TRADING_SETTINGS['min_signal_conditions']
            sell_signal = sum(sell_conditions) >= self.TRADING_SETTINGS['min_signal_conditions']
            
            direction = None
            if buy_signal:
                direction = 'LONG'
            elif sell_signal:
                direction = 'SHORT'

            details = {
                'signal_strength': max(sum(buy_conditions), sum(sell_conditions)) * 20,
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
                price_filter = next((f for f in symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
                
                return {
                    'step_size': float(lot_size['stepSize']) if lot_size else 0.001,
                    'tick_size': float(price_filter['tickSize']) if price_filter else 0.001,
                    'precision': int(round(-np.log10(float(lot_size['stepSize'])))) if lot_size else 3,
                    'min_qty': float(lot_size['minQty']) if lot_size else 0.001,
                }
            
            return {'step_size': 0.001, 'tick_size': 0.001, 'precision': 3, 'min_qty': 0.001}
        except Exception as e:
            logger.error(f"❌ خطأ في جلب دقة العقود: {e}")
            return {'step_size': 0.001, 'tick_size': 0.001, 'precision': 3, 'min_qty': 0.001}

    def can_open_trade(self, symbol):
        if len(self.active_trades) >= self.TRADING_SETTINGS['max_active_trades']:
            return False
            
        if symbol in self.active_trades:
            return False
            
        if self.symbol_balances.get(symbol, 0) < 5:
            return False
            
        return True

    def execute_futures_trade(self, symbol, direction, signal_strength, analysis):
        try:
            logger.info(f"🔧 محاولة تنفيذ صفقة لـ {symbol} - {direction}")
            
            if not self.can_open_trade(symbol):
                return False

            current_price = self.get_current_price(symbol)
            if current_price is None:
                return False

            # إعداد الرافعة والهامش
            self.set_leverage(symbol, self.TRADING_SETTINGS['max_leverage'])
            self.set_margin_type(symbol, self.TRADING_SETTINGS['margin_type'])

            # حساب حجم الصفقة
            available_balance = self.symbol_balances[symbol]
            trade_size_usd = min(self.TRADING_SETTINGS['base_trade_size'], available_balance)

            precision_info = self.get_futures_precision(symbol)
            quantity = trade_size_usd / current_price
            step_size = precision_info['step_size']
            precision = precision_info['precision']
            quantity = round(quantity / step_size) * step_size
            quantity = round(quantity, precision)

            if quantity <= 0 or quantity < precision_info['min_qty']:
                return False

            # حساب الرافعة - إصلاح الحساب
            symbol_weight = self.OPTIMAL_SETTINGS['weights'][symbol]
            atr = analysis['atr']
            atr_percentage = atr / current_price
            
            # رافعة أكثر تحفظاً
            leverage = min(3 / max(atr_percentage, 0.001), self.TRADING_SETTINGS['max_leverage']) * symbol_weight
            leverage = max(leverage, 1)
            leverage = int(leverage)

            self.set_leverage(symbol, leverage)

            side = Client.SIDE_BUY if direction == 'LONG' else Client.SIDE_SELL

            logger.info(f"💰 تنفيذ أمر {symbol}: {direction} - كمية: {quantity}")

            order = self.client.futures_create_order(
                symbol=symbol,
                side=side,
                type=Client.ORDER_TYPE_MARKET,
                quantity=quantity
            )

            if order['status'] == 'FILLED':
                avg_price = float(order['avgPrice'])
                
                # حساب وقف الخسارة وجني الأرباح بمضاعفات ATR
                atr_multiplier_sl = self.TRADING_SETTINGS['atr_stop_loss_multiplier']
                atr_multiplier_tp = self.TRADING_SETTINGS['atr_take_profit_multiplier']
                
                if direction == 'LONG':
                    stop_loss = avg_price - (atr * atr_multiplier_sl)
                    take_profit = avg_price + (atr * atr_multiplier_tp)
                else:
                    stop_loss = avg_price + (atr * atr_multiplier_sl)
                    take_profit = avg_price - (atr * atr_multiplier_tp)
                
                # خصم قيمة الصفقة من الرصيد
                self.symbol_balances[symbol] -= trade_size_usd
                
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
                    'trade_size_usd': trade_size_usd,
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                    'min_duration_end': datetime.now(damascus_tz) + timedelta(
                        minutes=self.TRADING_SETTINGS['min_trade_duration_minutes']
                    )
                }
                
                self.active_trades[symbol] = trade_data

                if self.notifier:
                    self.notifier.send_message(
                        f"🚀 <b>فتح صفقة عقود جديدة</b>\n"
                        f"العملة: {symbol}\n"
                        f"الاتجاه: {direction}\n"
                        f"سعر الدخول: ${avg_price:.4f}\n"
                        f"الكمية: {quantity:.6f}\n"
                        f"الحجم: ${trade_size_usd:.2f}\n"
                        f"الرافعة: {leverage}x\n"
                        f"وقف الخسارة: ${stop_loss:.4f} ({atr_multiplier_sl} x ATR)\n"
                        f"جني الأرباح: ${take_profit:.4f} ({atr_multiplier_tp} x ATR)\n"
                        f"أقل مدة: {self.TRADING_SETTINGS['min_trade_duration_minutes']} دقيقة\n"
                        f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                        f'trade_open_futures_{symbol}'
                    )

                logger.info(f"✅ فتح صفقة {symbol} {direction}")
                logger.info(f"📍 وقف الخسارة: ${stop_loss:.4f}, جني الأرباح: ${take_profit:.4f}")
                return True

            return False

        except Exception as e:
            logger.error(f"❌ خطأ في تنفيذ صفقة {symbol}: {e}")
            return False

    def update_active_trades(self):
        try:
            account_info = self.client.futures_account()
            positions = account_info['positions']
            
            current_symbols = set()
            for position in positions:
                symbol = position['symbol']
                if symbol in self.symbols:
                    quantity = float(position['positionAmt'])
                    if quantity != 0:
                        current_symbols.add(symbol)
            
            removed_trades = set(self.active_trades.keys()) - current_symbols
            for symbol in removed_trades:
                if symbol in self.active_trades:
                    logger.info(f"🔄 إزالة صفقة مغلقة: {symbol}")
                    del self.active_trades[symbol]
            
        except Exception as e:
            logger.error(f"❌ خطأ في تحديث الصفقات النشطة: {e}")

    def manage_futures_trades(self):
        if not self.active_trades:
            return
        
        self.update_active_trades()
        
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
                            logger.info(f"🔄 المركز مغلق لـ {symbol}")
                            if symbol in self.active_trades:
                                if symbol in self.symbol_balances:
                                    self.symbol_balances[symbol] += trade.get('trade_size_usd', 10)
                                del self.active_trades[symbol]
                            continue
                except Exception as e:
                    logger.error(f"❌ خطأ في التحقق من المركز لـ {symbol}: {e}")

                # التحقق من أقل مدة للصفقة
                current_time = datetime.now(damascus_tz)
                if 'min_duration_end' in trade and current_time < trade['min_duration_end']:
                    logger.debug(f"⏳ الصفقة {symbol} لم تكمل أقل مدة")
                    continue

                # إدارة وقف الخسارة وجني الأرباح
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

                # فحص انتهاء الوقت (فقط بعد انتهاء أقل مدة)
                trade_age = current_time - trade['timestamp']
                if trade_age.total_seconds() > self.TRADING_SETTINGS['trade_timeout_hours'] * 3600:
                    self.close_futures_trade(symbol, current_price, 'Timeout')

            except Exception as e:
                logger.error(f"❌ خطأ في إدارة صفقة {symbol}: {e}")

    def close_futures_trade(self, symbol, current_price, reason):
        try:
            if symbol not in self.active_trades:
                return True

            trade = self.active_trades[symbol]
            
            # التحقق من أقل مدة للصفقة (لا نغلق إذا لم تكمل المدة)
            current_time = datetime.now(damascus_tz)
            if 'min_duration_end' in trade and current_time < trade['min_duration_end']:
                if reason in ['Stop Loss', 'Take Profit']:  # لا نمنع الإغلاق بالوقت
                    logger.info(f"⏳ تأجيل إغلاق {symbol} - لم تكمل أقل مدة")
                    return False

            side = Client.SIDE_SELL if trade['side'] == 'LONG' else Client.SIDE_BUY
            quantity = trade['quantity']

            logger.info(f"🔧 إغلاق صفقة {symbol} - السبب: {reason}")

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
                    pnl_usd = (exit_price - trade['entry_price']) * trade['quantity'] * trade['leverage']
                else:
                    pnl_percent = ((trade['entry_price'] - exit_price) / trade['entry_price']) * 100 * trade['leverage']
                    pnl_usd = (trade['entry_price'] - exit_price) * trade['quantity'] * trade['leverage']
                
                # إعادة الرصيد + الربح/الخسارة
                if symbol in self.symbol_balances:
                    self.symbol_balances[symbol] += trade.get('trade_size_usd', 10) + pnl_usd
                
                emoji = "✅" if pnl_percent > 0 else "❌"
                
                if self.notifier:
                    self.notifier.send_message(
                        f"{emoji} <b>إغلاق صفقة عقود</b>\n"
                        f"العملة: {symbol}\n"
                        f"الاتجاه: {trade['side']}\n"
                        f"سبب الخروج: {reason}\n"
                        f"سعر الدخول: ${trade['entry_price']:.4f}\n"
                        f"سعر الخروج: ${exit_price:.4f}\n"
                        f"الرافعة: {trade['leverage']}x\n"
                        f"P&L: {pnl_percent:.2f}% (${pnl_usd:.2f})\n"
                        f"الرصيد الجديد: ${self.symbol_balances[symbol]:.2f}\n"
                        f"المدة: {int((current_time - trade['timestamp']).total_seconds() / 60)} دقيقة\n"
                        f"الوقت: {current_time.strftime('%Y-%m-%d %H:%M:%S')}",
                        f'trade_close_futures_{symbol}'
                    )
                
                logger.info(f"✅ إغلاق صفقة {symbol}: {reason}, P&L: {pnl_percent:.2f}%")
                
                del self.active_trades[symbol]
                return True
                    
            return False

        except Exception as e:
            logger.error(f"❌ خطأ في إغلاق صفقة {symbol}: {e}")
            return False

    def scan_market(self):
        if len(self.active_trades) >= self.TRADING_SETTINGS['max_active_trades']:
            return
        
        logger.info("🔍 بدء فحص السوق...")
        
        for symbol in self.symbols:
            try:
                has_signal, analysis, direction = self.analyze_symbol(symbol)
                
                if has_signal and direction:
                    logger.info(f"✅ إشارة لـ {symbol} - الاتجاه: {direction}")
                    
                    if self.notifier:
                        self.notifier.send_message(
                            f"🔔 <b>إشارة تداول قوية</b>\n"
                            f"العملة: {symbol}\n"
                            f"الاتجاه: {direction}\n"
                            f"شروط الدخول: {analysis['buy_conditions_met'] if direction == 'LONG' else analysis['sell_conditions_met']}/5\n"
                            f"السعر الحالي: ${analysis['price']:.4f}\n"
                            f"الرصيد المتاح: ${self.symbol_balances.get(symbol, 0):.2f}\n"
                            f"الوقت: {datetime.now(damascus_tz).strftime('%Y-%m-%d %H:%M:%S')}",
                            f'signal_futures_{symbol}'
                        )
                    
                    self.execute_futures_trade(symbol, direction, analysis['signal_strength'], analysis)

            except Exception as e:
                logger.error(f"❌ خطأ في فحص {symbol}: {e}")

    def run(self):
        logger.info("🚀 بدء تشغيل بوت العقود الآجلة...")
        
        schedule.every(self.TRADING_SETTINGS['rescan_interval_minutes']).minutes.do(self.scan_market)
        schedule.every(2).minutes.do(self.manage_futures_trades)
        schedule.every(10).minutes.do(self.update_active_trades)
        
        self.scan_market()
        
        while True:
            try:
                schedule.run_pending()
                time.sleep(1)
            except KeyboardInterrupt:
                logger.info("⏹️ إيقاف البوت بواسطة المستخدم")
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
