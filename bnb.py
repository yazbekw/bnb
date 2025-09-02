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
import json

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

@app.route('/recent_trades')
def recent_trades():
    try:
        bot = BNB_Trading_Bot()
        report = bot.generate_12h_trading_report()
        return report
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
        logging.FileHandler('bot_activity.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
    
    def send_message(self, message):
        try:
            logger.info(f"محاولة إرسال رسالة إلى Telegram: {message}")
            url = f"{self.base_url}/sendMessage"
            payload = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }
            response = requests.post(url, data=payload, timeout=10)
            if response.status_code != 200:
                error_msg = f"فشل إرسال رسالة Telegram: {response.text}"
                logger.error(error_msg)
                return False
            else:
                logger.info("تم إرسال الرسالة إلى Telegram بنجاح")
                return True
        except Exception as e:
            error_msg = f"خطأ في إرسال رسالة Telegram: {e}"
            logger.error(error_msg)
            return False

class BNB_Trading_Bot:
    def __init__(self, api_key=None, api_secret=None, telegram_token=None, telegram_chat_id=None):
        self.notifier = None
        self.trade_history = []
        self.load_trade_history()
        
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
        
        # إعدادات إدارة الأوامر
        self.MAX_ALGO_ORDERS = 10
        self.ORDERS_TO_CANCEL = 2
        
        # إعدادات حجم الصفقة بالدولار حسب قوة الإشارة
        self.MIN_TRADE_SIZE = 5
        self.MAX_TRADE_SIZE = 50
        
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
                self.notifier.send_message(f"🤖 <b>بدء تشغيل بوت تداول BNB</b>\n\n{success_msg}\nنطاق حجم الصفقة: ${self.MIN_TRADE_SIZE}-${self.MAX_TRADE_SIZE}\nالحد الأقصى للأوامر: {self.MAX_ALGO_ORDERS}")
        except Exception as e:
            logger.error(f"خطأ في جلب الرصيد الابتدائي: {e}")
            self.initial_balance = 0

    def load_trade_history(self):
        """تحميل تاريخ الصفقات من ملف"""
        try:
            if os.path.exists('trade_history.json'):
                with open('trade_history.json', 'r', encoding='utf-8') as f:
                    self.trade_history = json.load(f)
        except Exception as e:
            logger.error(f"خطأ في تحميل تاريخ الصفقات: {e}")
            self.trade_history = []

    def save_trade_history(self):
        """حفظ تاريخ الصفقات إلى ملف"""
        try:
            with open('trade_history.json', 'w', encoding='utf-8') as f:
                json.dump(self.trade_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"خطأ في حفظ تاريخ الصفقات: {e}")

    def add_trade_record(self, trade_type, quantity, price, trade_size, signal_strength, order_id=None, status="executed"):
        """إضافة سجل صفقة جديدة"""
        trade_record = {
            'timestamp': datetime.now().isoformat(),
            'type': trade_type,
            'quantity': quantity,
            'price': price,
            'trade_size': trade_size,
            'signal_strength': signal_strength,
            'order_id': order_id,
            'status': status
        }
        self.trade_history.append(trade_record)
        self.save_trade_history()

    def generate_12h_trading_report(self):
        """إنشاء تقرير التداول لآخر 12 ساعة"""
        try:
            twelve_hours_ago = datetime.now() - timedelta(hours=12)
            recent_trades = [
                trade for trade in self.trade_history 
                if datetime.fromisoformat(trade['timestamp']) >= twelve_hours_ago
            ]
            
            if not recent_trades:
                return {"message": "لا توجد صفقات في آخر 12 ساعة"}
            
            # حساب الإحصائيات
            buy_trades = [t for t in recent_trades if t['type'] == 'buy']
            sell_trades = [t for t in recent_trades if t['type'] == 'sell']
            
            total_buy_size = sum(t['trade_size'] for t in buy_trades)
            total_sell_size = sum(t['trade_size'] for t in sell_trades)
            avg_buy_strength = np.mean([t['signal_strength'] for t in buy_trades]) if buy_trades else 0
            avg_sell_strength = np.mean([t['signal_strength'] for t in sell_trades]) if sell_trades else 0
            
            report = {
                "period": "آخر 12 ساعة",
                "total_trades": len(recent_trades),
                "buy_trades": len(buy_trades),
                "sell_trades": len(sell_trades),
                "total_buy_size": round(total_buy_size, 2),
                "total_sell_size": round(total_sell_size, 2),
                "avg_buy_signal_strength": round(avg_buy_strength, 1),
                "avg_sell_signal_strength": round(avg_sell_strength, 1),
                "recent_trades": recent_trades[-10:]
            }
            
            return report
        except Exception as e:
            logger.error(f"خطأ في إنشاء تقرير التداول: {e}")
            return {"error": str(e)}

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
            print(f"حجم الصفقة: ${self.MIN_TRADE_SIZE}-${self.MAX_TRADE_SIZE}")
            print(f"الحد الأقصى للأوامر: {self.MAX_ALGO_ORDERS}")
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
            success = self.notifier.send_message(message)
            if not success:
                logger.error("فشل إرسال الإشعار إلى Telegram")
            return success
        return False
    
    def format_price(self, price, symbol):
        """تقريب السعر حسب متطلبات Binance"""
        try:
            info = self.client.get_symbol_info(symbol)
            price_filter = [f for f in info['filters'] if f['filterType'] == 'PRICE_FILTER'][0]
            tick_size = float(price_filter['tickSize'])
            
            formatted_price = round(price / tick_size) * tick_size
            return round(formatted_price, 8)
        except Exception as e:
            logger.error(f"خطأ في تقريب السعر: {e}")
            return round(price, 4)
    
    def get_algo_orders_count(self, symbol):
        """الحصول على عدد الأوامر الآلية الحالية"""
        try:
            open_orders = self.client.get_open_orders(symbol=symbol)
            algo_orders = [o for o in open_orders if o['type'] in ['STOP_LOSS_LIMIT', 'TAKE_PROFIT_LIMIT', 'OCO']]
            return len(algo_orders)
        except Exception as e:
            logger.error(f"خطأ في جلب عدد الأوامر الآلية: {e}")
            return 0
    
    def cancel_oldest_algo_orders(self, symbol, num_to_cancel=2):
        """إلغاء أقدم الأوامر الآلية"""
        try:
            open_orders = self.client.get_open_orders(symbol=symbol)
            algo_orders = []
            
            for order in open_orders:
                if order['type'] in ['STOP_LOSS_LIMIT', 'TAKE_PROFIT_LIMIT', 'OCO']:
                    order_time = datetime.fromtimestamp(order['time'] / 1000)
                    algo_orders.append({
                        'orderId': order['orderId'],
                        'time': order_time,
                        'type': order['type'],
                        'price': order.get('price', 'N/A')
                    })
            
            algo_orders.sort(key=lambda x: x['time'])
            
            cancelled_count = 0
            cancelled_info = []
            
            for i in range(min(num_to_cancel, len(algo_orders))):
                try:
                    self.client.cancel_order(
                        symbol=symbol,
                        orderId=algo_orders[i]['orderId']
                    )
                    cancelled_count += 1
                    cancelled_info.append(f"{algo_orders[i]['type']} - {algo_orders[i]['price']}")
                    logger.info(f"تم إلغاء الأمر القديم: {algo_orders[i]['orderId']}")
                    
                    self.add_trade_record(
                        trade_type="cancel",
                        quantity=0,
                        price=0,
                        trade_size=0,
                        signal_strength=0,
                        order_id=algo_orders[i]['orderId'],
                        status="cancelled"
                    )
                    
                except Exception as e:
                    logger.error(f"خطأ في إلغاء الأمر {algo_orders[i]['orderId']}: {e}")
            
            return cancelled_count, cancelled_info
            
        except Exception as e:
            logger.error(f"خطأ في إلغاء الأوامر القديمة: {e}")
            return 0, []
    
    def manage_order_space(self, symbol):
        """إدارة مساحة الأوامر"""
        try:
            current_orders = self.get_algo_orders_count(symbol)
            
            if current_orders >= self.MAX_ALGO_ORDERS:
                self.send_notification(f"⚠️ الحد الأقصى للأوامر ممتلئ ({current_orders}/{self.MAX_ALGO_ORDERS})")
                
                cancelled_count, cancelled_info = self.cancel_oldest_algo_orders(symbol, self.ORDERS_TO_CANCEL)
                
                if cancelled_count > 0:
                    msg = f"♻️ <b>تم إلغاء {cancelled_count} أوامر قديمة</b>\n\n"
                    for info in cancelled_info:
                        msg += f"• {info}\n"
                    self.send_notification(msg)
                    
                    current_orders = self.get_algo_orders_count(symbol)
                    return current_orders < self.MAX_ALGO_ORDERS
                else:
                    self.send_notification("❌ فشل إلغاء الأوامر القديمة")
                    return False
            else:
                return True
                
        except Exception as e:
            logger.error(f"خطأ في إدارة مساحة الأوامر: {e}")
            return False
    
    def calculate_signal_strength(self, data, signal_type='buy'):
        """تقييم قوة الإشارة من -100 إلى +100% بناء على 5 مؤشرات"""
        latest = data.iloc[-1]
        score = 0
        
        # 1. المتوسطات المتحركة (25%)
        ema_bullish = latest['ema9'] > latest['ema21'] > latest['ema50']
        ema_bearish = latest['ema9'] < latest['ema21'] < latest['ema50']
        
        if signal_type == 'buy':
            if ema_bullish: score += 25
            elif ema_bearish: score -= 25
        else:
            if ema_bearish: score += 25
            elif ema_bullish: score -= 25
        
        # 2. RSI (20%)
        if signal_type == 'buy':
            if latest['rsi'] < 30: score += 20
            elif latest['rsi'] > 70: score -= 20
            elif 40 < latest['rsi'] < 60: score += 10
        else:
            if latest['rsi'] > 70: score += 20
            elif latest['rsi'] < 30: score -= 20
            elif 40 < latest['rsi'] < 60: score += 10
        
        # 3. MACD (20%)
        macd_strength = (latest['macd'] - latest['macd_sig']) / abs(latest['macd_sig']) if latest['macd_sig'] != 0 else 0
        
        if signal_type == 'buy':
            if macd_strength > 0.1: score += 20
            elif macd_strength < -0.1: score -= 20
        else:
            if macd_strength < -0.1: score += 20
            elif macd_strength > 0.1: score -= 20
        
        # 4. Bollinger Bands (20%)
        bb_position = (latest['close'] - latest['bb_lower']) / (latest['bb_upper'] - latest['bb_lower'])
        
        if signal_type == 'buy':
            if bb_position < 0.2: score += 20  # قرب النطاق السفلي
            elif bb_position > 0.8: score -= 20  # قرب النطاق العلوي
        else:
            if bb_position > 0.8: score += 20  # قرب النطاق العلوي
            elif bb_position < 0.2: score -= 20  # قرب النطاق السفلي
        
        # 5. Volume (15%)
        volume_strength = latest['vol_ratio']
        
        if signal_type == 'buy':
            if volume_strength > 2.0 and latest['close'] > latest['open']: score += 15
            elif volume_strength > 2.0 and latest['close'] < latest['open']: score -= 15
        else:
            if volume_strength > 2.0 and latest['close'] < latest['open']: score += 15
            elif volume_strength > 2.0 and latest['close'] > latest['open']: score -= 15
        
        return max(min(score, 100), -100)
    
    def calculate_dollar_size(self, signal_strength, signal_type='buy'):
        """حساب حجم الصفقة بالدولار حسب قوة الإشارة"""
        abs_strength = abs(signal_strength)
        
        if signal_type == 'buy' and signal_strength > 0:
            if abs_strength >= 80:    # إشارة شراء قوية جداً
                base_size = 30
                bonus = (abs_strength - 80) * 1.0
                return min(base_size + bonus, 50)
            
            elif abs_strength >= 50:  # إشارة شراء جيدة
                base_size = 15
                bonus = (abs_strength - 50) * 0.5
                return min(base_size + bonus, 25)
            
            elif abs_strength >= 20:  # إشارة شراء خفيفة
                base_size = 5
                bonus = (abs_strength - 20) * 0.3
                return min(base_size + bonus, 10)
            
            else:
                return 0
                
        elif signal_type == 'sell' and signal_strength > 0:
            if abs_strength >= 80:    # إشارة بيع قوية جداً
                base_size = 30
                bonus = (abs_strength - 80) * 1.0
                return min(base_size + bonus, 50)
            
            elif abs_strength >= 50:  # إشارة بيع جيدة
                base_size = 15
                bonus = (abs_strength - 50) * 0.5
                return min(base_size + bonus, 25)
            
            elif abs_strength >= 20:  # إشارة بيع خفيفة
                base_size = 5
                bonus = (abs_strength - 20) * 0.3
                return min(base_size + bonus, 10)
            
            else:
                return 0
        else:
            return 0
    
    def get_strength_level(self, strength):
        """الحصول على اسم مستوى القوة"""
        abs_strength = abs(strength)
        if abs_strength >= 80: return "4 🟢 (قوي جداً)"
        elif abs_strength >= 50: return "3 🟡 (قوي)"
        elif abs_strength >= 20: return "2 🔵 (متوسط)"
        else: return "1 ⚪ (ضعيف)"
    
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
        
            # حساب جميع المؤشرات المطلوبة
            data['rsi'] = self.calculate_rsi(data['close'])
            data['atr'] = self.calculate_atr(data)
        
            # المتوسطات المتحركة الأسية - يجب إضافة ema21
            data['ema200'] = data['close'].ewm(span=200, adjust=False).mean()
            data['ema50'] = data['close'].ewm(span=50, adjust=False).mean()
            data['ema21'] = data['close'].ewm(span=21, adjust=False).mean()  # ⬅️ أضف هذا السطر
            data['ema9'] = data['close'].ewm(span=9, adjust=False).mean()
        
            # حساب Bollinger Bands
            data['bb_upper'], data['bb_middle'], data['bb_lower'] = self.calculate_bollinger_bands(data['close'])
        
            data['vol_ma20'] = data['volume'].rolling(20).mean()
            data['vol_ratio'] = data['volume'] / data['vol_ma20']
         
            macd_line, macd_sig, macd_hist = self.calculate_macd(data['close'])
            data['macd'] = macd_line
            data['macd_sig'] = macd_sig
            data['macd_hist'] = macd_hist
        
            return data
        except Exception as e:
            error_msg = f"❌ خطأ في جلب البيانات: {e}"
            self.send_notification(error_msg)
            return None

    
    def calculate_dynamic_stop_loss_take_profit(self, entry_price, signal_strength, atr_value):
        """حساب وقف الخسارة وجني الأرباح بشكل ديناميكي مع مسافات أوسع"""
        abs_strength = abs(signal_strength)
    
        # تحديد مضاعف ATR حسب قوة الإشارة مع توسيع المسافة
        if abs_strength >= 80:    # إشارة قوية → مجال أوسع
            stop_multiplier = 3.5    # زيادة من 3.0
            profit_multiplier = 5.0  # زيادة من 4.0
        elif abs_strength >= 50:  # إشارة متوسطة → مجال متوسط
            stop_multiplier = 3.0    # زيادة من 2.5
            profit_multiplier = 4.0  # زيادة من 3.0
        else:                     # إشارة ضعيفة → مجال أقرب
            stop_multiplier = 2.5    # زيادة من 2.0
            profit_multiplier = 3.5  # زيادة من 2.0
    
        if signal_strength > 0:  # إشارة شراء
            stop_loss = entry_price - (stop_multiplier * atr_value)
            take_profit = entry_price + (profit_multiplier * atr_value)
        else:  # إشارة بيع
            stop_loss = entry_price + (stop_multiplier * atr_value)
            take_profit = entry_price - (profit_multiplier * atr_value)
    
        return stop_loss, take_profit
    
    def execute_real_trade(self, signal_type, signal_strength, current_price, stop_loss, take_profit):
        """تنفيذ صفقة حقيقية مع إدارة الرصيد المحسنة للشراء والبيع"""
        try:
            # حساب حجم الصفقة بناء على قوة الإشارة
            trade_size = self.calculate_dollar_size(signal_strength, signal_type)
        
            if trade_size <= 0:
                return False
        
            logger.info(f"بدء تنفيذ صفقة {signal_type} بقوة {signal_strength}% بحجم {trade_size}$")
        
            if signal_type == 'buy':
                can_trade, usdt_balance = self.check_balance_before_trade(trade_size)
            
                if not can_trade:
                    # استخدام كل الرصيد المتاح مع الحفاظ على نسبة الأمان
                    available_balance = usdt_balance * 0.95  # ترك 5% هامش أمان
                    if available_balance >= 5:  # على الأقل 5$
                        trade_size = available_balance
                        self.send_notification(f"⚠️ تعديل حجم الصفقة. أصبح: ${trade_size:.2f} (الرصيد المتاح: ${usdt_balance:.2f})")
                    else:
                        self.send_notification(f"❌ رصيد غير كافي حتى لأصغر صفقة. المطلوب: $5، المتاح: ${usdt_balance:.2f}")
                        return False
            
                quantity = trade_size / current_price
            
                info = self.client.get_symbol_info(self.symbol)
                step_size = float([f['stepSize'] for f in info['filters'] if f['filterType'] == 'LOT_SIZE'][0])
                precision = len(str(step_size).split('.')[1].rstrip('0'))
                quantity = round(quantity - (quantity % step_size), precision)
            
                if quantity <= 0:
                    self.send_notification("⚠️ الكمية غير صالحة للشراء")
                    return False
            
                order = self.client.order_market_buy(
                    symbol=self.symbol,
                    quantity=quantity
                )
            
                # إضافة سجل للصفقة
                self.add_trade_record(
                    trade_type="buy",
                    quantity=quantity,
                    price=current_price,
                    trade_size=trade_size,
                    signal_strength=signal_strength,
                    order_id=order.get('orderId', 'N/A')
                )
            
                # وضع أوامر الوقف وجني الأرباح
                if not self.manage_order_space(self.symbol):
                    self.send_notification("❌ لا يمكن وضع أوامر الوقف - المساحة ممتلئة")
                    return True  # الصفقة ناجحة ولكن بدون وقف
            
                try:
                    formatted_stop_loss = self.format_price(stop_loss, self.symbol)
                    formatted_take_profit = self.format_price(take_profit, self.symbol)
                
                    oco_order = self.client.order_oco_sell(
                        symbol=self.symbol,
                        quantity=quantity,
                        stopPrice=formatted_stop_loss,
                        stopLimitPrice=formatted_stop_loss,
                        price=formatted_take_profit,
                        stopLimitTimeInForce='GTC'
                    )
                
                except Exception as e:
                    error_msg = f"⚠️ فشل وضع أوامر الوقف: {e}"
                    self.send_notification(error_msg)
            
                return True
            
            elif signal_type == 'sell':
                total_balance, balances, _ = self.get_account_balance_details()
                bnb_balance = balances.get('BNB', {}).get('free', 0)
            
                if bnb_balance <= 0.001:
                    self.send_notification("⚠️ رصيد BNB غير كافي للبيع")
                    return False
            
                # حساب الكمية بناء على حجم الصفقة المطلوب
                quantity_by_trade_size = trade_size / current_price
            
                # إذا كانت الكمية المطلوبة أكثر من الرصيد المتاح، نستخدم الرصيد كاملاً
                if quantity_by_trade_size > bnb_balance:
                    # استخدام كل الرصيد المتاح مع الحفاظ على نسبة الأمان
                    available_balance = bnb_balance * 0.95  # ترك 5% هامش أمان
                    quantity_to_sell = available_balance
                    actual_trade_size = quantity_to_sell * current_price
                
                    if actual_trade_size >= 5:  # على الأقل 5$ قيمة
                        trade_size = actual_trade_size
                        self.send_notification(f"⚠️ تعديل حجم صفقة البيع. أصبح: ${trade_size:.2f} (الرصيد المتاح: {bnb_balance:.6f} BNB)")
                    else:
                        self.send_notification(f"❌ رصيد BNB غير كافي حتى لأصغر صفقة بيع. المطلوب: $5، المتاح: ${bnb_balance * current_price:.2f}")
                        return False
                else:
                    quantity_to_sell = quantity_by_trade_size
            
                # تقريب الكمية حسب متطلبات Binance
                info = self.client.get_symbol_info(self.symbol)
                step_size = float([f['stepSize'] for f in info['filters'] if f['filterType'] == 'LOT_SIZE'][0])
                precision = len(str(step_size).split('.')[1].rstrip('0'))
                quantity = round(quantity_to_sell - (quantity_to_sell % step_size), precision)
            
                if quantity <= 0:
                    self.send_notification("⚠️ الكمية غير صالحة للبيع")
                    return False
            
                # تنفيذ أمر البيع
                order = self.client.order_market_sell(
                    symbol=self.symbol,
                    quantity=quantity
                )
            
                # إضافة سجل للصفقة
                self.add_trade_record(
                    trade_type="sell",
                    quantity=quantity,
                    price=current_price,
                    trade_size=quantity * current_price,  # الحجم الفعلي بعد التقريب
                    signal_strength=signal_strength,
                    order_id=order.get('orderId', 'N/A')
                 )
            
                return True
            
        except Exception as e:
            error_msg = f"❌ خطأ في تنفيذ الصفقة: {e}"
            self.send_notification(error_msg)
            logger.error(error_msg)
            return False
    
    def bnb_strategy(self, data):
        """استراتيجية التداول بناء على المؤشرات الخمسة"""
        if data is None or len(data) < 100:
            return 0, 0, 0, 0
        
        latest = data.iloc[-1]
        current_price = latest['close']
        atr_value = latest['atr']
        
        # حساب قوة الإشارة للشراء والبيع
        buy_strength = self.calculate_signal_strength(data, 'buy')
        sell_strength = self.calculate_signal_strength(data, 'sell')
        
        # اتخاذ القرار بناء على أقوى إشارة
        if buy_strength > 20 and buy_strength > sell_strength:
            stop_loss, take_profit = self.calculate_dynamic_stop_loss_take_profit(
                current_price, buy_strength, atr_value
            )
            return 'buy', buy_strength, stop_loss, take_profit
            
        elif sell_strength > 20 and sell_strength > buy_strength:
            stop_loss, take_profit = self.calculate_dynamic_stop_loss_take_profit(
                current_price, -sell_strength, atr_value  # سالب للإشارة البيعية
            )
            return 'sell', sell_strength, stop_loss, take_profit
        
        else:
            return 'hold', 0, 0, 0
    
    def check_balance_before_trade(self, required_usdt):
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
    
    
    
    def execute_trade(self):
        data = self.get_historical_data()
        if data is None:
            return False
            
        signal_type, signal_strength, stop_loss, take_profit = self.bnb_strategy(data)
        latest = data.iloc[-1]
        current_price = latest['close']
        
        if signal_type in ['buy', 'sell']:
            success = self.execute_real_trade(signal_type, signal_strength, current_price, stop_loss, take_profit)
            if success:
                level = self.get_strength_level(signal_strength)
                msg = f"🎯 <b>{'شراء' if signal_type == 'buy' else 'بيع'} بمستوى {level}</b>\n\n"
                msg += f"قوة الإشارة: {signal_strength}%\n"
                msg += f"حجم الصفقة: ${self.calculate_dollar_size(signal_strength, signal_type):.2f}\n"
                msg += f"السعر: ${current_price:.4f}\n"
                
                if signal_type == 'buy':
                    msg += f"وقف الخسارة: ${stop_loss:.4f}\n"
                    msg += f"جني الأرباح: ${take_profit:.4f}\n"
                
                self.send_notification(msg)
            return success
        
        return False
    
    def send_performance_report(self):
        try:
            total_balance, balances, bnb_price = self.get_account_balance_details()
            
            if total_balance is None:
                return
            
            profit_loss = total_balance - self.initial_balance
            profit_loss_percent = (profit_loss / self.initial_balance) * 100 if self.initial_balance > 0 else 0
            
            current_orders = self.get_algo_orders_count(self.symbol)
            
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
            message += f"الأوامر النشطة: {current_orders}/{self.MAX_ALGO_ORDERS}\n"
            message += f"نطاق الصفقة: ${self.MIN_TRADE_SIZE}-${self.MAX_TRADE_SIZE}\n\n"
            message += f"<b>تفاصيل الرصيد:</b>\n{balance_details}"
            
            if bnb_price:
                message += f"\nسعر BNB الحالي: ${bnb_price:.4f}"
            
            report_12h = self.generate_12h_trading_report()
            if 'total_trades' in report_12h and report_12h['total_trades'] > 0:
                message += f"\n\n📈 <b>آخر 12 ساعة:</b>"
                message += f"\nإجمالي الصفقات: {report_12h['total_trades']}"
                message += f"\nصفقات شراء: {report_12h['buy_trades']} (${report_12h['total_buy_size']})"
                message += f"\nصفقات بيع: {report_12h['sell_trades']} (${report_12h['total_sell_size']})"
            
            self.send_notification(message)
            
        except Exception as e:
            error_msg = f"❌ خطأ في إرسال تقرير الأداء: {e}"
            logger.error(error_msg)
    
    def run(self):
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        
        interval_minutes = 15
        self.send_notification(f"🚀 بدء تشغيل بوت تداول BNB\n\nسيعمل البوت على فحص السوق كل {interval_minutes} دقيقة\nنطاق حجم الصفقة: ${self.MIN_TRADE_SIZE}-${self.MAX_TRADE_SIZE}\nالحد الأقصى للأوامر: {self.MAX_ALGO_ORDERS}")
        
        self.send_performance_report()
        
        report_counter = 0
        
        while True:
            try:
                trade_executed = self.execute_trade()
                
                report_counter += 1
                if trade_executed or report_counter >= 4:
                    self.send_performance_report()
                    report_counter = 0
                
                time.sleep(interval_minutes * 60)
                
            except Exception as e:
                error_msg = f"❌ خطأ غير متوقع في التشغيل: {e}"
                self.send_notification(error_msg)
                logger.error(error_msg)
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
