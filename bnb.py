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
import json
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
import schedule
from flask import Flask, jsonify

# تحميل متغيرات البيئة
load_dotenv()

# إنشاء تطبيق Flask للرصد الصحي
app = Flask(__name__)

@app.route('/')
def health_check():
    return {'status': 'healthy', 'service': 'momentum-hunter-bot', 'timestamp': datetime.now().isoformat()}

@app.route('/stats')
def stats():
    try:
        bot = MomentumHunterBot()
        stats = bot.get_performance_stats()
        return jsonify(stats)
    except Exception as e:
        return {'error': str(e)}

@app.route('/opportunities')
def opportunities():
    try:
        bot = MomentumHunterBot()
        opportunities = bot.get_current_opportunities()
        return jsonify(opportunities)
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
        logging.FileHandler('momentum_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.last_notifications = {}
    
    def send_message(self, message, message_type='info'):
        try:
            # منع التكرار الممل لنفس نوع الرسالة
            current_time = time.time()
            if (message_type in self.last_notifications and 
                current_time - self.last_notifications[message_type] < 300):
                return True
                
            self.last_notifications[message_type] = current_time
            
            url = f"{self.base_url}/sendMessage"
            payload = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }
            response = requests.post(url, data=payload, timeout=10)
            if response.status_code != 200:
                logger.error(f"فشل إرسال رسالة Telegram: {response.text}")
                return False
            return True
        except Exception as e:
            logger.error(f"خطأ في إرسال رسالة Telegram: {e}")
            return False

class RequestManager:
    def __init__(self):
        self.request_count = 0
        self.last_request_time = time.time()
        self.max_requests_per_minute = 1100  # زيادة الحد المسموح
        self.request_lock = threading.Lock()
        
    def safe_request(self, func, *args, **kwargs):
        with self.request_lock:
            current_time = time.time()
            elapsed = current_time - self.last_request_time
            
            # الانتظار 2 ثواني بين الطلبات
            if elapsed < 2:
                time.sleep(2 - elapsed)
            
            # إعادة تعيين العداد كل دقيقة
            if current_time - self.last_request_time >= 60:
                self.request_count = 0
                self.last_request_time = current_time
            
            if self.request_count >= self.max_requests_per_minute:
                sleep_time = 60 - (current_time - self.last_request_time)
                if sleep_time > 0:
                    time.sleep(sleep_time)
                self.request_count = 0
                self.last_request_time = time.time()
            
            self.request_count += 1
            return func(*args, **kwargs)

class MongoManager:
    def __init__(self, connection_string=None):
        # استخدام اسم المتغير الصحيح MANGO_DB_CONNECTION_STRING
        self.connection_string = (connection_string or 
                                 os.environ.get('MANGO_DB_CONNECTION_STRING') or
                                 os.environ.get('MONGODB_URI') or
                                 os.environ.get('DATABASE_URL'))
        
        # إضافة logging للتحقق
        if self.connection_string:
            logger.info(f"✅ تم العثور على رابط MongoDB")
            # إظهار أول 20 حرف فقط لأسباب أمنية
            logger.info(f"🔗 الرابط: {self.connection_string[:20]}...")
        else:
            logger.warning("❌ لم يتم العثور على رابط MongoDB")
            
        self.client = None
        self.db = None
        if self.connection_string:
            self.connect()
        else:
            logger.warning("⚠️  البوت يعمل بدون MongoDB - السجلات لن تحفظ")
    
    def connect(self):
        try:
            if not self.connection_string:
                return False
                
            self.client = MongoClient(self.connection_string, serverSelectionTimeoutMS=5000)
            self.client.admin.command('ping')
            self.db = self.client['momentum_hunter_bot']
            logger.info("✅ تم الاتصال بـ MongoDB بنجاح")
            return True
        except Exception as e:
            logger.error(f"❌ فشل الاتصال بـ MongoDB: {e}")
            return False
    
    def save_trade(self, trade_data):
        try:
            if not self.db:
                return False
                
            collection = self.db['trades']
            trade_data['timestamp'] = datetime.now()
            result = collection.insert_one(trade_data)
            return True
        except Exception as e:
            logger.error(f"خطأ في حفظ الصفقة: {e}")
            return False
    
    def save_opportunity(self, opportunity):
        try:
            if not self.db:
                return False
                
            collection = self.db['opportunities']
            opportunity['scanned_at'] = datetime.now()
            collection.insert_one(opportunity)
            return True
        except Exception as e:
            logger.error(f"خطأ في حفظ الفرصة: {e}")
            return False
    
    def get_performance_stats(self):
        try:
            if not self.db:
                return {}
                
            collection = self.db['trades']
            stats = collection.aggregate([
                {'$match': {'status': 'completed'}},
                {'$group': {
                    '_id': None,
                    'total_trades': {'$sum': 1},
                    'profitable_trades': {
                        '$sum': {'$cond': [{'$gt': ['$profit_loss', 0]}, 1, 0]}
                    },
                    'total_profit': {
                        '$sum': {'$cond': [{'$gt': ['$profit_loss', 0]}, '$profit_loss', 0]}
                    },
                    'total_loss': {
                        '$sum': {'$cond': [{'$lt': ['$profit_loss', 0]}, '$profit_loss', 0]}
                    }
                }}
            ])
            
            result = list(stats)
            if result:
                stats_data = result[0]
                win_rate = (stats_data['profitable_trades'] / stats_data['total_trades'] * 100) if stats_data['total_trades'] > 0 else 0
                return {
                    'total_trades': stats_data['total_trades'],
                    'win_rate': round(win_rate, 2),
                    'total_profit': round(stats_data['total_profit'], 2),
                    'total_loss': round(abs(stats_data['total_loss']), 2),
                    'profit_factor': round(stats_data['total_profit'] / abs(stats_data['total_loss']), 2) if stats_data['total_loss'] < 0 else float('inf')
                }
            return {}
        except Exception as e:
            logger.error(f"خطأ في جلب الإحصائيات: {e}")
            return {}

class HealthMonitor:
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.error_count = 0
        self.max_errors = 10
        self.last_health_check = datetime.now()
        
    def check_connections(self):
        try:
            # فحص اتصال Binance
            self.bot.request_manager.safe_request(self.bot.client.get_server_time)
            
            # فحص اتصال MongoDB (بتعديل الرسالة)
            if not self.bot.mongo_manager.connect():
                logger.warning("⚠️  فشل الاتصال بـ MongoDB - لكن البوت سيستمر في العمل")
                return True  # استمر حتى بدون MongoDB
                
            self.error_count = 0
            return True
            
        except Exception as e:
            self.error_count += 1
            logger.error(f"خطأ في فحص الصحة: {e}")
            
            if self.error_count >= self.max_errors:
                self.restart_bot()
                
            return False
    
    def restart_bot(self):
        logger.warning("🔄 إعادة تشغيل البوت بسبب كثرة الأخطاء")
        if self.bot.notifier:
            self.bot.notifier.send_message("🔄 <b>إعادة تشغيل البوت</b>\nكثرة الأخطاء تتطلب إعادة التشغيل", "restart")
        
        # إعادة التشغيل النظيفة
        os._exit(1)

class MomentumHunterBot:
    def __init__(self):
        self.api_key = os.environ.get('BINANCE_API_KEY')
        self.api_secret = os.environ.get('BINANCE_API_SECRET')
        self.telegram_token = os.environ.get('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = os.environ.get('TELEGRAM_CHAT_ID')
        
        if not all([self.api_key, self.api_secret]):
            raise ValueError("مفاتيح Binance مطلوبة")
            
        self.client = Client(self.api_key, self.api_secret)
        self.request_manager = RequestManager()
        self.mongo_manager = MongoManager()
        
        if self.telegram_token and self.telegram_chat_id:
            self.notifier = TelegramNotifier(self.telegram_token, self.telegram_chat_id)
        else:
            self.notifier = None
            
        self.health_monitor = HealthMonitor(self)
        
        # إعدادات التداول
        self.symbols = []  # سيتم ملؤها ديناميكياً
        self.stable_coins = ['USDT', 'BUSD', 'USDC']
        self.min_volume = 500000  # 500K USD
        self.min_momentum = 2.0   # 2% صعود
        self.max_position_size = 0.2  # 20% من الرصيد
        self.stop_loss = 0.03  # 3%
        self.take_profit = 0.08  # 8%
        self.min_hold_time = 300  # ⏰ 5 دقائق minimum holding time
        
        self.active_trades = {}
        self.last_scan_time = datetime.now()
        
        logger.info("✅ تم تهيئة بوت صائد الصاعدات بنجاح")

    def get_all_trading_symbols(self):
        try:
            # العملات المهمة من قائمتك + العملات الأساسية (برموز Binance الصحيحة)
            important_symbols = [
                # العملات الرئيسية من قائمتك
                "BTCUSDT",    # Bitcoin
                "ETHUSDT",    # Ethereum  
                "SOLUSDT",    # Solana
                "BNBUSDT",    # BNB
                "XRPUSDT",    # XRP
                "AVAXUSDT",   # Avalanche
                "XLMUSDT",    # Stellar
                "SUIUSDT",    # Sui
                "TONUSDT",    # Toncoin
                "WLDUSDT",    # Worldcoin
                
                # العملات الإضافية المهمة
                "ADAUSDT",    # Cardano
                "DOTUSDT",    # Polkadot
                "LINKUSDT",   # Chainlink
                "LTCUSDT",    # Litecoin
                "BCHUSDT",    # Bitcoin Cash
                "DOGEUSDT",   # Dogecoin
                "MATICUSDT",  # Polygon
                "ATOMUSDT",   # Cosmos
                "NEARUSDT",   # Near Protocol
                "FILUSDT",    # Filecoin
                
                # بدائل للعملات المذكورة (برموز Binance الصحيحة)
                "INJUSDT",    # Injective Protocol (بديل HYPE)
                "APTUSDT",    # Aptos (بديل MNT)
                "ARBUSDT",    # Arbitrum
                "OPUSDT",     # Optimism
                "MANAUSDT",   # Decentraland (بديل MYX)
                "SANDUSDT",   # The Sandbox
                "APEUSDT",    # ApeCoin
                "RUNEUSDT",   # THORChain
                "SEIUSDT",    # Sei Network
                "TIAUSDT",    # Celestia
                
                # عملات إضافية ذات حجم تداول عالي
                "ETCUSDT",    # Ethereum Classic
                "XMRUSDT",    # Monero
                "EOSUSDT",    # EOS
                "AAVEUSDT",   # Aave
                "UNIUSDT",    # Uniswap
                "ALGOUSDT",   # Algorand
                "XTZUSDT",    # Tezos
                "VETUSDT",    # VeChain
                "THETAUSDT",  # Theta Network
                "EGLDUSDT"    # Elrond
            ]
            
            logger.info(f"🔸 استخدام القائمة المخصصة: {len(important_symbols)} عملة")
            return important_symbols
            
        except Exception as e:
            logger.error(f"خطأ في جلب أزواج التداول: {e}")
            # العودة إلى القائمة الأساسية في حالة الخطأ
            return [
                "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                "AVAXUSDT", "XLMUSDT", "SUIUSDT", "TONUSDT", "WLDUSDT",
                "ADAUSDT", "DOTUSDT", "LINKUSDT", "LTCUSDT", "DOGEUSDT",
                "MATICUSDT", "ATOMUSDT", "NEARUSDT", "INJUSDT", "APTUSDT"
            ]

    def filter_low_volume_symbols(self, symbols, min_volume=1000000):
        """استبعاد العملات ذات حجم التداول المنخفض"""
        filtered = []
        for symbol in symbols:
            try:
                ticker = self.safe_binance_request(self.client.get_ticker, symbol=symbol)
                volume = float(ticker['volume']) * float(ticker['lastPrice'])
                if volume >= min_volume:
                    filtered.append(symbol)
            except:
                continue
        return filtered
        
    def safe_binance_request(self, func, *args, **kwargs):
        return self.request_manager.safe_request(func, *args, **kwargs)
    
    def get_account_balance(self):
        try:
            account = self.safe_binance_request(self.client.get_account)
            balances = {}
            for asset in account['balances']:
                free = float(asset['free'])
                locked = float(asset['locked'])
                if free + locked > 0:
                    balances[asset['asset']] = {
                        'free': free,
                        'locked': locked,
                        'total': free + locked
                    }
            return balances
        except Exception as e:
            logger.error(f"خطأ في جلب الرصيد: {e}")
            return {}
    
    def get_current_price(self, symbol):
        try:
            ticker = self.safe_binance_request(self.client.get_symbol_ticker, symbol=symbol)
            return float(ticker['price'])
        except Exception as e:
            logger.error(f"خطأ في جلب سعر {symbol}: {e}")
            return None
    
    def get_historical_data(self, symbol, interval='15m', limit=100):
        try:
            klines = self.safe_binance_request(self.client.get_klines, 
                                              symbol=symbol, 
                                              interval=interval, 
                                              limit=limit)
            data = []
            for k in klines:
                data.append({
                    'timestamp': k[0],
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5])
                })
            return pd.DataFrame(data)
        except Exception as e:
            logger.error(f"خطأ في جلب البيانات لـ {symbol}: {e}")
            return None
    
    def calculate_technical_indicators(self, data):
        try:
            df = data.copy()
            # المتوسطات المتحركة
            df['ema12'] = df['close'].ewm(span=12).mean()
            df['ema26'] = df['close'].ewm(span=26).mean()
            df['ema50'] = df['close'].ewm(span=50).mean()
            
            # RSI
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            df['rsi'] = 100 - (100 / (1 + rs))
            
            # حجم التداول
            df['volume_ma'] = df['volume'].rolling(window=20).mean()
            
            return df
        except Exception as e:
            logger.error(f"خطأ في حساب المؤشرات: {e}")
            return data
    
    def calculate_momentum_score(self, symbol):
        try:
            data = self.get_historical_data(symbol, '15m', 50)
            if data is None or len(data) < 20:
                return 0, {}
            
            data = self.calculate_technical_indicators(data)
            latest = data.iloc[-1]
            prev = data.iloc[-5]  # قبل 5 فترات
            
            # حساب قوة الزخم
            price_change = ((latest['close'] - prev['close']) / prev['close']) * 100
            volume_ratio = latest['volume'] / latest['volume_ma'] if latest['volume_ma'] > 0 else 1
            
            # تصحيح القيم المتطرفة
            volume_ratio = max(0.1, min(5.0, volume_ratio))
            price_change = max(-10, min(20, price_change))
            
            # حساب النتيجة النهائية
            momentum_score = min(100, max(0, 
                (price_change * 0.4) + 
                (volume_ratio * 20) + 
                (max(0, latest['rsi'] - 30) * 0.5)
            ))
            
            details = {
                'price_change': round(price_change, 2),
                'volume_ratio': round(volume_ratio, 2),
                'rsi': round(latest['rsi'], 2),
                'current_price': latest['close']
            }
            
            return momentum_score, details
            
        except Exception as e:
            logger.error(f"خطأ في حساب زخم {symbol}: {e}")
            return 0, {}
    
    def find_best_opportunities(self):
        opportunities = []
        
        for i, symbol in enumerate(self.symbols):
            try:
                if (i + 1) % 10 == 0:
                    logger.info(f"معالجة {i + 1}/{len(self.symbols)} عملة...")
                
                # الفحص السريع للحجم والسعر
                ticker = self.safe_binance_request(self.client.get_ticker, symbol=symbol)
                price_change = float(ticker['priceChangePercent'])
                volume = float(ticker['volume']) * float(ticker['lastPrice'])
                
                if volume < self.min_volume or abs(price_change) < self.min_momentum:
                    continue
                
                # التحليل العميق
                momentum_score, details = self.calculate_momentum_score(symbol)
                
                if momentum_score >= 60:
                    opportunity = {
                        'symbol': symbol,
                        'score': round(momentum_score, 2),
                        'price_change': details['price_change'],
                        'volume_ratio': details['volume_ratio'],
                        'rsi': details['rsi'],
                        'current_price': details['current_price'],
                        'volume_usd': volume,
                        'timestamp': datetime.now()
                    }
                    
                    opportunities.append(opportunity)
                    self.mongo_manager.save_opportunity(opportunity)
                    
            except Exception as e:
                logger.error(f"خطأ في تحليل {symbol}: {e}")
                continue
        
        # ترتيب الفرص من الأفضل إلى الأسوأ
        opportunities.sort(key=lambda x: x['score'], reverse=True)
        return opportunities
    
    def execute_trade(self, opportunity):
        symbol = opportunity['symbol']
        current_price = opportunity['current_price']
        
        try:
            # التحقق من عدم وجود صفقة نشطة على نفس العملة
            if symbol in self.active_trades:
                logger.info(f"تخطي {symbol} - صفقة نشطة موجودة")
                return False
            
            # حساب حجم الصفقة
            balances = self.get_account_balance()
            usdt_balance = balances.get('USDT', {}).get('free', 0)
            
            if usdt_balance < 10:  # حد أدنى 10 USDT
                logger.warning("رصيد USDT غير كافي")
                return False
            
            trade_size = min(usdt_balance * self.max_position_size, usdt_balance * 0.5)
            quantity = trade_size / current_price
            
            # التقريب حسب متطلبات Binance
            symbol_info = self.safe_binance_request(self.client.get_symbol_info, symbol=symbol)
            lot_size = next((f for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), None)
            if lot_size:
                step_size = float(lot_size['stepSize'])
                quantity = round(quantity / step_size) * step_size
            
            # تنفيذ أمر الشراء
            order = self.safe_binance_request(self.client.order_market_buy,
                                             symbol=symbol,
                                             quantity=quantity)
            
            # ✅ التأكد من تنفيذ الأمر بنجاح
            if order['status'] != 'FILLED':
                logger.error(f"❌ أمر الشراء لم ينفذ: {order['status']}")
                return False
                
            # ✅ التأكد من وجود العملة في الرصيد
            time.sleep(2)  # انتظار تحديث الرصيد
            balances = self.get_account_balance()
            asset_name = symbol.replace('USDT', '')
            if float(balances.get(asset_name, {}).get('free', 0)) < quantity * 0.9:
                logger.error(f"❌ العملة {asset_name} لم تضف إلى الرصيد")
                return False
            
            # حفظ بيانات الصفقة
            trade_data = {
                'symbol': symbol,
                'type': 'buy',
                'quantity': quantity,
                'entry_price': current_price,
                'trade_size': trade_size,
                'stop_loss': current_price * (1 - self.stop_loss),
                'take_profit': current_price * (1 + self.take_profit),
                'timestamp': datetime.now(),
                'status': 'open',
                'score': opportunity['score'],
                'min_hold_time': 300  # ⏰ 5 دقائق minimum holding time
            }
            
            self.active_trades[symbol] = trade_data
            self.mongo_manager.save_trade(trade_data)
            
            # إرسال إشعار
            if self.notifier:
                message = (
                    f"🚀 <b>تم تنفيذ صفقة شراء</b>\n\n"
                    f"• العملة: {symbol}\n"
                    f"• السعر: ${current_price:.2f}\n"
                    f"• الكمية: {quantity:.6f}\n"
                    f"• الحجم: ${trade_size:.2f}\n"
                    f"• قوة الزخم: {opportunity['score']}/100\n"
                    f"• وقف الخسارة: ${trade_data['stop_loss']:.2f}\n"
                    f"• أخذ الربح: ${trade_data['take_profit']:.2f}\n"
                    f"• وقت الاحتفاظ الأدنى: 5 دقائق\n\n"
                    f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self.notifier.send_message(message, 'trade_execution')
            
            logger.info(f"✅ تم شراء {symbol} بمبلغ ${trade_size:.2f}")
            return True
            
        except Exception as e:
            logger.error(f"❌ خطأ في تنفيذ صفقة {symbol}: {e}")
            return False
    
    def manage_active_trades(self):
        for symbol, trade in list(self.active_trades.items()):
            try:
                # 🔧 التحقق من وقت الاحتفاظ الأدنى
                trade_duration = (datetime.now() - trade['timestamp']).total_seconds()
                if trade_duration < trade.get('min_hold_time', 300):
                    continue  # انتظر 5 دقائق على الأقل قبل التحقق
                
                current_price = self.get_current_price(symbol)
                if current_price is None:
                    continue
                
                # التحقق من وقف الخسارة
                if current_price <= trade['stop_loss']:
                    self.close_trade(symbol, current_price, 'stop_loss')
                    continue
                
                # التحقق من أخذ الربح
                if current_price >= trade['take_profit']:
                    self.close_trade(symbol, current_price, 'take_profit')
                    continue
                
                # تحديث وقف الخسارة المتابع
                if current_price > trade['entry_price'] * 1.03:  # بعد 3% ربح
                    new_sl = max(trade['stop_loss'], current_price * (1 - self.stop_loss))
                    if new_sl > trade['stop_loss']:
                        trade['stop_loss'] = new_sl
                        logger.info(f"تم تحديث وقف الخسارة لـ {symbol} إلى ${new_sl:.2f}")
                        
            except Exception as e:
                logger.error(f"خطأ في إدارة صفقة {symbol}: {e}")
    
    def close_trade(self, symbol, exit_price, reason):
        try:
            logger.info(f"🔍 محاولة إغلاق {symbol} بسبب: {reason}")
            
            trade = self.active_trades[symbol]
            
            # حساب الربح/الخسارة
            pnl = (exit_price - trade['entry_price']) * trade['quantity']
            pnl_percent = (exit_price / trade['entry_price'] - 1) * 100
            
            # تحديث بيانات الصفقة
            trade['exit_price'] = exit_price
            trade['exit_time'] = datetime.now()
            trade['profit_loss'] = pnl
            trade['pnl_percent'] = pnl_percent
            trade['status'] = 'completed'
            trade['exit_reason'] = reason
            
            # حفظ في MongoDB
            self.mongo_manager.save_trade(trade)
            
            # إرسال إشعار
            if self.notifier:
                emoji = "✅" if pnl > 0 else "❌"
                message = (
                    f"{emoji} <b>تم إغلاق الصفقة</b>\n\n"
                    f"• العملة: {symbol}\n"
                    f"• السبب: {reason}\n"
                    f"• سعر الدخول: ${trade['entry_price']:.2f}\n"
                    f"• سعر الخروج: ${exit_price:.2f}\n"
                    f"• الربح/الخسارة: ${pnl:.2f} ({pnl_percent:+.2f}%)\n"
                    f"• المدة: {(trade['exit_time'] - trade['timestamp']).total_seconds() / 60:.1f} دقيقة\n\n"
                    f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                self.notifier.send_message(message, 'trade_close')
            
            logger.info(f"تم إغلاق {symbol} بـ {reason}: ${pnl:.2f} ({pnl_percent:+.2f}%)")
            del self.active_trades[symbol]
            
        except Exception as e:
            logger.error(f"خطأ في إغلاق صفقة {symbol}: {e}")
    
    def auto_convert_stuck_assets(self):
        try:
            balances = self.get_account_balance()
            usdt_value = 0
            
            for asset, balance in balances.items():
                if asset in self.stable_coins:
                    if asset != 'USDT':
                        # تحويل العملات المستقرة الأخرى إلى USDT
                        if balance['free'] > 1:
                            self.convert_to_usdt(asset, balance['free'])
                    else:
                        usdt_value = balance['free']
                elif balance['free'] > 0.0001:  # تجنب القيم الضئيلة
                    # تحويل العملات الراكدة
                    current_price = self.get_current_price(asset + 'USDT')
                    if current_price:
                        asset_value = balance['free'] * current_price
                        if asset_value > 5:  # إذا كانت القيمة أكثر من 5$
                            self.convert_to_usdt(asset, balance['free'])
            
            return usdt_value
            
        except Exception as e:
            logger.error(f"خطأ في تحويل الأصول: {e}")
            return 0
    
    def convert_to_usdt(self, asset, amount):
        try:
            if asset == 'USDT':
                return True
                
            symbol = asset + 'USDT'
            # التحقق من وجود الزوج
            symbol_info = self.safe_binance_request(self.client.get_symbol_info, symbol=symbol)
            if not symbol_info:
                return False
            
            # تنفيذ التحويل
            order = self.safe_binance_request(self.client.order_market_sell,
                                             symbol=symbol,
                                             quantity=amount)
            
            logger.info(f"تم تحويل {amount} {asset} إلى USDT")
            return True
            
        except Exception as e:
            logger.error(f"خطأ في تحويل {asset} إلى USDT: {e}")
            return False
    
    def run_scan_cycle(self):
        try:
            logger.info("🔍 بدء دورة المسح للصاعدات...")
            
            # جلب جميع أزواج التداول المتاحة
            self.symbols = self.get_all_trading_symbols()
            logger.info(f"عدد الأزواج للمسح: {len(self.symbols)}")
            
            # التحويل التلقائي للأصول الراكدة
            usdt_balance = self.auto_convert_stuck_assets()
            logger.info(f"الرصيد المتاح: {usdt_balance} USDT")
            
            # البحث عن الفرص
            opportunities = self.find_best_opportunities()
            
            if opportunities:
                best_opportunity = opportunities[0]
                logger.info(f"أفضل فرصة: {best_opportunity['symbol']} - قوة: {best_opportunity['score']}")
                
                # تنفيذ الصفقة إذا كانت الفرصة قوية
                if best_opportunity['score'] >= 70 and usdt_balance > 10:
                    self.execute_trade(best_opportunity)
            
            # إدارة الصفقات النشطة
            self.manage_active_trades()
            
            # فحص الصحة
            self.health_monitor.check_connections()
            
            logger.info(f"✅ اكتملت دورة المسح. الفرص الموجودة: {len(opportunities)}")
            
        except Exception as e:
            logger.error(f"❌ خطأ في دورة المسح: {e}")
    
    def get_performance_stats(self):
        return self.mongo_manager.get_performance_stats()
    
    def get_current_opportunities(self):
        opportunities = self.find_best_opportunities()
        return {'opportunities': opportunities, 'timestamp': datetime.now()}
    
    def start_trading(self):
        if self.notifier:
            self.notifier.send_message(
                "🚀 <b>بدء تشغيل بوت صائد الصاعدات</b>\n\n"
                "✅ البوت يعمل الآن وسيبدأ في البحث عن الفرص الصاعدة\n"
                "⏰ دورة المسح: كل 5 دقائق\n"
                "🎯 استراتيجية: شراء العملات ذات الزخم القوي\n"
                "🛡️ وقف الخسارة: 3% | أخذ الربح: 8%\n"
                "⏳ وقت الاحتفاظ الأدنى: 5 دقائق",
                'bot_start'
            )
        
        logger.info("🚀 بدء تشغيل بوت صائد الصاعدات")
        
        # تشغيل دورة المسح كل 5 دقائق
        schedule.every(5).minutes.do(self.run_scan_cycle)
        
        # تشغيل الدورة فوراً
        self.run_scan_cycle()
        
        while True:
            try:
                schedule.run_pending()
                time.sleep(60)
            except Exception as e:
                logger.error(f"خطأ في التشغيل الرئيسي: {e}")
                time.sleep(300)  # انتظار 5 دقائق قبل إعادة المحاولة

def main():
    try:
        # بدء خادم Flask في خيط منفصل
        flask_thread = threading.Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        logger.info("تم بدء خادم Flask للرصد الصحي")
        
        # تهيئة وتشغيل البوت
        bot = MomentumHunterBot()
        bot.start_trading()
        
    except Exception as e:
        logger.error(f"❌ خطأ في الدالة الرئيسية: {e}")
        if 'bot' in locals() and hasattr(bot, 'notifier') and bot.notifier:
            bot.notifier.send_message(f"❌ <b>فشل تشغيل البوت:</b>\n{str(e)}", 'error')

if __name__ == "__main__":
    main()
