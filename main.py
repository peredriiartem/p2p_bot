import telebot
import firebase_admin
from firebase_admin import credentials, firestore, auth

from google.cloud.firestore_v1.base_query import FieldFilter
from eth_account import Account
from web3 import Web3
import os
from flask import Flask
import locale
from functools import wraps
from solana.rpc.api import Client

import base58
from solana.rpc.types import TokenAccountOpts
from spl.token.instructions import get_associated_token_address, create_associated_token_account, transfer_checked, TransferCheckedParams



# --- CLEAR CACHE ON START ---
cached_texts = {}
user_sessions = {}
last_trade_status = {}
last_sent_messages = {}
XMR_RPC_URL = ""


cred = credentials.Certificate('YOUR_FIREBASE_ADMIN_SDK_KEY.json')
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)
db = firestore.client()

bot = telebot.TeleBot('YOUR_TELEGRAM_BOT_TOKEN')
FIREBASE_WEB_API_KEY = "YOUR_FIREBASE_WEB_API_KEY"

# --- GEMINI AI CONFIGURATION ---
GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"


SYSTEM_PROMPT = """You are the official AI Support Assistant for a secure non-custodial P2P cryptocurrency trading platform.

### CORE KNOWLEDGE:
1. **Security:** Users hold their own private keys. We use secure smart contracts/escrow scripts.
2. **Fees:** Platform fee is exactly 1% on completed trades.
3. **Networks:** - Bitcoin (Mainnet, BIP84 SegWit)
   - Ethereum (Mainnet, ETH/USDT)
   - BSC (Mainnet, BNB/USDT)
   - Tron (Mainnet, TRX/USDT)
   - Solana (Mainnet, SOL/USDC)
4. **KYC:** Users must be verified via Blockpass (kycStatus == "approved") to create ads. Trading existing ads is available to all authorized users.
5. **Auth:** This is a closed system. Users must log in via /start to access News, Wallet, Community, or Trading.

### FEATURES & LOGIC:
- **Community Feed:** Users can share posts, like, and comment (5 posts per page pagination).
- **Instant Swap:** Integrated with ChangeNOW API for direct in-bot asset exchange.
- **Trade Workflow:** Initiated -> Accepted -> Funding (Escrow) -> Payment -> Release. 
- **Disputes:** Any user can open a dispute to freeze funds and call an Admin.
- **Dashboard:** Shows active trades with fiat amounts and limits clearly visible.

### GUIDELINES:
- Be professional, polite, and use emojis.
- NEVER ask for or reveal Private Keys, Seed Phrases, or API Keys.
- If a user is stuck in a trade, explain the Escrow process or advise opening a Dispute.
- Always reply in the same language the user uses to address you."""

MODEL_ID = "gemini-3-flash"
ai_chat_sessions = {}

# --- NETWORK CONFIGURATION (MAINNET) ---
SOL_RPC_URL = "YOUR_SOLANA_RPC_URL"
solana_client = Client(SOL_RPC_URL)

# --- LOCALE HACK ---
os.environ['LC_ALL'] = 'en_US.UTF-8'
os.environ['LANG'] = 'en_US.UTF-8'
def get_fixed_encoding():
    return 'UTF-8'
locale.getpreferredencoding = get_fixed_encoding

Account.enable_unaudited_hdwallet_features()
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is running!", 200


def log_blockchain_action(network, action_type, tx_hash, order_id=None):
    """Logs the blockchain action to console with an Explorer link"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    explorers = {
        'BTC': f"https://mempool.space/tx/{tx_hash}",
        'SOL': f"https://explorer.solana.com/tx/{tx_hash}",
        'ETH': f"https://etherscan.io/tx/{tx_hash}",
        'TRON': f"https://tronscan.org/#/transaction/{tx_hash}",
        'BSC': f"https://bscscan.com/tx/{tx_hash}"
    }

    url = explorers.get(network, "Unknown Network")

    print("\n" + "=" * 60)
    print(f"⛓️  [BLOCKCHAIN LOG] | {timestamp}")
    print(f"📦 Order ID: {order_id if order_id else 'N/A'}")
    print(f"🔧 Action:   {action_type.upper()}")
    print(f"🌐 Network:  {network}")
    print(f"🔗 Transaction Hash: {tx_hash}")
    print(f"🌍 Explorer Link:    {url}")
    print("=" * 60 + "\n")


def log_error(uid, context, error_msg):
    """Logs system error to console in a readable format"""
    timestamp = datetime.now().strftime('%H:%M:%S')
    divider = "—" * 50
    print(f"\n{divider}")
    print(f"🔴 [ERROR] {timestamp} | Context: {context}")
    print(f"👤 User UID: {uid}")
    print(f"⚠️ Message: {error_msg}")
    print(f"{divider}\n")

def send_system_message_to_chat(order_id, text):
    """Sends a system message with transaction hash to Firebase chat"""
    try:
        db.collection('orders').document(order_id).update({
            "messages": firestore.ArrayUnion([{
                "senderUid": "SYSTEM",
                "text": text,
                "timestamp": datetime.now(timezone.utc),
                "isFromBot": True
            }])
        })
    except Exception as e:
        print(f"❌ Error sending SYSTEM message to chat: {e}")


def send_or_edit_trade_card(chat_id, order_id, role, text, markup, current_msg_count=0):
    """Updates or reposts the active trade card depending on current message depth"""
    msg_key = f"{order_id}_{role}"

    if chat_id not in last_sent_messages:
        last_sent_messages[chat_id] = {}

    msg_data = last_sent_messages[chat_id].get(msg_key)

    last_msg_id = None
    last_count = 0

    if isinstance(msg_data, int):
        last_msg_id = msg_data
        last_count = current_msg_count
    elif isinstance(msg_data, dict):
        last_msg_id = msg_data.get('id')
        last_count = msg_data.get('msg_count', current_msg_count)

    if last_msg_id:
        if current_msg_count - last_count >= 15:
            try:
                bot.delete_message(chat_id, last_msg_id)
            except Exception:
                pass

            msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
            last_sent_messages[chat_id][msg_key] = {'id': msg.message_id, 'msg_count': current_msg_count}
        else:
            try:
                bot.edit_message_text(text, chat_id, last_msg_id, reply_markup=markup, parse_mode="Markdown")
            except Exception as e:
                error_msg = str(e).lower()
                if "message is not modified" in error_msg:
                    pass
                elif "not found" in error_msg:
                    msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
                    last_sent_messages[chat_id][msg_key] = {'id': msg.message_id, 'msg_count': current_msg_count}
    else:
        msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
        last_sent_messages[chat_id][msg_key] = {'id': msg.message_id, 'msg_count': current_msg_count}


def auto_markdown(method):
    @wraps(method)
    def wrapper(*args, **kwargs):
        if 'parse_mode' not in kwargs:
            kwargs['parse_mode'] = 'Markdown'
        return method(*args, **kwargs)
    return wrapper

bot.send_message = auto_markdown(bot.send_message)
bot.edit_message_text = auto_markdown(bot.edit_message_text)
bot.reply_to = auto_markdown(bot.reply_to)

PLATFORM_FEE_PERCENT = 0.05
PLATFORM_ADMIN_BTC_ADDRESS = "YOUR_PLATFORM_ADMIN_BTC_ADDRESS"
PLATFORM_ADMIN_SOL_ADDRESS = "YOUR_PLATFORM_ADMIN_SOL_ADDRESS"
BTC_API_URL = "https://mempool.space/api"

# --- PAYMENT METHODS STRUCTURE ---
PAYMENT_STRUCTURE = {
    "Bank Transfers 🏦": [
        "SWIFT international transfer", "SEPA transfer", "SEPA instant",
        "Interac e-Transfer", "Bank Transfer", "Bank of America"
    ],
    "Cash Payments 💵": [
        "Western Union", "MoneyGram", "Chase Cash Deposit", "Cash in person",
        "Cash Deposit", "Cash by mail", "Capital One Cash Deposit",
        "Bank of America Cash Deposit", "Wells Fargo Cash Deposit", "Ria Money Transfers"
    ],
    "Online Wallets 📱": [
        "ZEN", "Zelle", "YooMoney", "Yandex Money", "WebMoney", "Venmo", "Varo",
        "Wise", "Square Up", "Skrill", "Revolut", "Remitly", "QIWI", "PayPal",
        "Neteller", "Google Pay", "GoFundMe.com", "Chime instant transfer", "Cash App", "ApplePay"
    ],
    "Cards 💳": [
        "Visa", "Mastercard", "Vanilla Reload Card", "Prepaid Debit Card",
        "Discover Credit Card", "American Express"
    ],
    "Gift Cards 🎁": ["Amazon Gift Card", "Steam Gift Card", "Google Play Gift Card"]
}

ALL_METHODS_FLAT = [item for sublist in PAYMENT_STRUCTURE.values() for item in sublist]


def format_time_ago(dt):
    """Calculates time difference relative to the current timestamp"""
    if not dt: return "just now"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    seconds = (now - dt).total_seconds()
    if seconds < 60: return "just now"
    if seconds < 3600: return f"{int(seconds // 60)}m ago"
    if seconds < 86400: return f"{int(seconds // 3600)}h ago"
    if seconds < 2592000: return f"{int(seconds // 86400)}d ago"
    return f"{int(seconds // 2592000)}mo ago"


def send_post_card(chat_id, uid, post_id, post, message_id=None):
    """Sends or updates a community feed post card"""
    author = post.get('authorName', 'User')
    text = post.get('text', '')
    image_url = post.get('imageUrl')
    dt = post.get('createdAt')

    time_str = format_time_ago(dt)
    likes = post.get('likes', [])
    comments = post.get('comments', [])
    is_liked = uid in likes

    caption = f"👤 **{author}** • _{time_str}_\n\n{text}"

    markup = types.InlineKeyboardMarkup(row_width=2)
    like_btn_text = f"❤️ {len(likes)}" if is_liked else f"🤍 {len(likes)}"
    markup.add(
        types.InlineKeyboardButton(like_btn_text, callback_data=f"c_like_{post_id}"),
        types.InlineKeyboardButton(f"💬 {len(comments)}", callback_data=f"c_view_{post_id}")
    )
    markup.add(types.InlineKeyboardButton("✍️ Write Comment", callback_data=f"c_reply_{post_id}"))

    if message_id:
        try:
            bot.edit_message_reply_markup(chat_id, message_id, reply_markup=markup)
        except:
            pass
    else:
        if image_url:
            bot.send_photo(chat_id, image_url, caption=caption, reply_markup=markup, parse_mode="Markdown")
        else:
            bot.send_message(chat_id, caption, reply_markup=markup, parse_mode="Markdown")


def show_community_page(chat_id, uid, last_doc_timestamp=None):
    """Fetches and displays community feed posts with pagination"""
    try:
        query = db.collection('community_posts').order_by('createdAt', direction=firestore.Query.DESCENDING).limit(5)

        if last_doc_timestamp:
            query = query.start_after({'createdAt': last_doc_timestamp})

        posts_snap = query.get()

        if not posts_snap:
            bot.send_message(chat_id, "🏁 **No more posts to show.**")
            return

        for doc in posts_snap:
            send_post_card(chat_id, uid, doc.id, doc.to_dict())

        last_ts = posts_snap[-1].to_dict().get('createdAt')
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("⬇️ Load More Posts", callback_data=f"c_load_more_{last_ts.timestamp()}"))
        bot.send_message(chat_id, "👆 **End of page**", reply_markup=markup)

    except Exception as e:
        print(f"❌ Community error: {e}")


import requests


def get_countries_data():
    """Отримує актуальний список країн та валют ( restcountries )"""
    try:
        res = requests.get('https://restcountries.com/v3.1/all?fields=name,cca2,currencies', timeout=10)
        if res.status_code == 200:
            data = res.json()
            mapped = []
            for c in data:
                currencies = c.get('currencies', {})
                # Безпечно беремо першу валюту, якщо вона є, інакше USD
                fiat = list(currencies.keys())[0] if currencies else 'USD'
                mapped.append({'name': c['name']['common'], 'fiat': fiat})
            return sorted(mapped, key=lambda x: x['name'])
    except requests.RequestException:
        pass

    # Резервний список у разі помилки мережі
    return [{'name': 'Ukraine', 'fiat': 'UAH'}, {'name': 'United States', 'fiat': 'USD'}]


# --- BLOCKCHAIN CONFIG ---
BLOCKCHAIN_CONFIG = {
    'TRON': {
        'escrow': "TUifQn1cL4ff5XbfMrNSF8poPZzN1DJa36",
        'usdt_contract': "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",  # Справжній USDT
        'rpc': "https://prettiest-morning-energy.tron-mainnet.quiknode.pro/3edc25a92f3bde024d6c5084d3e17fd34217a234"
    },
    'ETH': {
        'escrow': "0x543584eCaCf53d06d65795055B23219FCFacf820",
        'usdt_contract': "0xdac17f958d2ee523a2206206994597c13d831ec7",  # Справжній ERC20 USDT
        'rpc': "https://prettiest-morning-energy.quiknode.pro/3edc25a92f3bde024d6c5084d3e17fd34217a234"
    },
    'BSC': {
        'escrow': "0x7777E1345e0d58790380c74CEEe420940Cb8f670",
        'usdt_contract': "0x55d398326f99059fF775485246999027B3197955",  # Справжній BEP20 USDT
        'rpc': "https://prettiest-morning-energy.bsc.quiknode.pro/3edc25a92f3bde024d6c5084d3e17fd34217a234"
    },
    'SOL': {
        'usdc_spl': "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # Справжній SOL USDC
    },
    'XMR': {
        'network': 'mainnet',
        'daemon_rpc': "https://xmr-node.cakewallet.com:18081",
    }
}

# --- CHANGENOW SWAP CONFIG ---
CHANGENOW_API_KEY = "ece940934c39864057577501b635ba16d8fc7aff4cc40454f8f972d060417724"

SWAP_ASSETS = {
    "USDT TRC20": {"ticker": "usdt", "network": "trx"},
    "USDT ERC20": {"ticker": "usdt", "network": "eth"},
    "USDC SOL": {"ticker": "usdc", "network": "sol"},
    "BTC": {"ticker": "btc", "network": "btc"},
    "ETH": {"ticker": "eth", "network": "eth"},
    "BNB": {"ticker": "bnb", "network": "bsc"},
    "TRX": {"ticker": "trx", "network": "trx"},
    "SOL": {"ticker": "sol", "network": "sol"}
}

# --- ABI ДЛЯ EVM КОНТРАКТІВ (Точний зліпок Solidity) ---
EVM_ESCROW_ABI = [
    {
        "inputs": [{"name": "_tradeId", "type": "bytes32"}, {"name": "_buyer", "type": "address"}],
        "name": "createTradeNative",
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "_tradeId", "type": "bytes32"},
            {"name": "_buyer", "type": "address"},
            {"name": "_tokenAddress", "type": "address"},
            {"name": "_amount", "type": "uint256"}
        ],
        "name": "createTrade",
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "_tradeId", "type": "bytes32"}],
        "name": "releaseFunds",
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "_tradeId", "type": "bytes32"}],
        "name": "cancelTrade",
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

# --- МОВИ ТА ТЕКСТИ ---
languages = {
    "English": "en", "Українська": "ua", "Русский": "ru", "Español": "es", "Français": "fr",
    "Deutsch": "de", "Português": "pt", "Italiano": "it", "Türkçe": "tr", "Polski": "pl",
    "Nederlands": "nl", "Čeština": "cs", "Română": "ro", "Magyar": "hu", "Ελληνικά": "el",
    "Svenska": "sv", "Dansk": "da", "Қазақ тілі": "kk", "O‘zbek": "uz", "ქართული": "ka",
    "עברית": "he", "العربية": "ar", "हिन्दी": "hi", "বাংলা": "bn", "ไทย": "th",
    "Tiếng Việt": "vi", "Bahasa Indonesia": "id", "日本語": "ja", "韓国어": "ko",
    "简体中文": "zh", "繁體中文": "zh-hant", "Kiswahili": "sw"
}

texts = {
    'en': {
        # Головне меню
        'btn_app': "🚀 App",
        'btn_wallet': "💳 Wallet",
        'btn_swap': "🔄 Swap",
        'btn_buy': "💳 Buy",
        'btn_sell': "💰 Sell",
        'btn_dashboard': "📊 Dashboard",
        'btn_create_offer': "➕ Create Offer",
        'btn_profile': "👤 Profile",
        'btn_news': "📰 News",
        'btn_community': "🌐 Community",
        'btn_support': "👨‍💻 Support",
        'btn_settings': "⚙️ Settings",
        'btn_social': "🌐 Social Media",
        'menu_msg': "💎 Menu:",

        # Авторизація та Реєстрація
        'auth_req': "❌ **Access Denied.**\nPlease log in or register first using /start.",
        'enter_email': "📧 Please enter your Email address:",
        'reg_how': "How would you like to register?",
        'reg_web': "🌐 Register on Website",
        'reg_bot': "🤖 Register here in Bot",
        'reg_new_acc': "🆕 **Creating new account**\n\nPlease enter your Email for registration:",
        'err_invalid_email': "❌ **Invalid Email format!**",
        'err_email_exists': "⚠️ **Email already registered!**",
        'reg_pass': "🔐 **Email accepted!** Now set a password (min 6 chars):",
        'err_weak_pass': "❌ **Password too weak (min 6 chars).** Try again:",
        'reg_nick': "👤 **Great!** Now choose your Nickname (max 15 chars):",
        'err_bad_nick': "❌ **Nickname must be 3-15 characters.** Try again:",
        'reg_region': "🌍 **Almost done! Select your Region or Search:**",
        'search_country_prompt': "🔍 **Type country name (e.g. Germany, Poland):**",
        'select_exact_country': "📍 **Select your exact country:**",
        'err_country_not_found': "❌ **Country not found.** Try again (e.g., Poland):",
        'generating_vault': "⚙️ **Generating secure vault and registering...**",
        'reg_success': "🎉 **Account Created Successfully!**\n\n👤 **Nickname:** `{nickname}`\n🌍 **Country:** `{country}` ({fiat})\n\n🔐 **Your Wallet Seed Phrase (SAVE THIS):**\n`{mnemonic}`",
        'reg_failed': "❌ Failed to create account: {err}",
        'login_pass_req': "🔐 **Password required:**",
        'err_session_lost': "❌ **Error:** Email session lost. Please use /start again.",
        'login_success': "✅ **Login successful!** Welcome back.",
        'err_bad_pass': "❌ **Incorrect password.** Please try again:",
        'err_auth_sys': "❌ **Auth system error.** Please try again later.",

        # Start & Settings
        'welcome_back': "✨ **Welcome back, {name}!** ✨\n━━━━━━━━━━━━━━━━━━\n🔗 Your account is linked from the website.\n\n🔐 Please enter your password to unlock access:",
        'security_check': "🔐 **Security Check:** Please enter your password:",
        'btn_login': "🔑 Login",
        'btn_register': "🆕 Register",
        'welcome_main': "👋 **Welcome to P2P!**\n━━━━━━━━━━━━━━━━━━\n🛡️ The most secure way to trade crypto directly.\n\n🚀 Do you have an account?",
        'settings_title': "⚙️ **Settings:**",
        'btn_logout': "🚪 Log Out",
        'logout_warn': "⚠️ **WARNING: SECURITY CHECK**\n\nYou are about to log out from the Telegram Bot.\n\n🛡️ **For your safety:** If you have the Web Application open, please **log out inside the Web App first** to ensure your private keys and seed phrase are cleared from the browser's cache.\n\nAre you sure you want to proceed?",
        'btn_yes_logout': "✅ Yes, Log Out",
        'btn_cancel': "❌ Cancel",
        'logout_success': "✅ **Successfully logged out.**\nAccess to all features is now restricted.",
        'login_again_msg': "To log in again, use /start.",

        # Гаманець та Профіль
        'wallet_check': "⏳ **Checking balances...**",
        'wallet_login_req': "❌ **Log in to view wallet.**",
        'profile_not_found': "❌ **Profile not found.**",
        'kyc_verified': "✅ Verified Trader",
        'kyc_not_verified': "⚠️ Not Verified",
        'kyc_rejected': "❌ Verification Rejected",
        'kyc_pending': "⏳ Verification Pending",
        'no_active_offers': "No active offers.",
        'btn_view_feedbacks': "⭐ View Feedbacks",
        'btn_verify_id': "🛡️ Verify Identity",
        'btn_check_status': "🔄 Check Status",
        'btn_leaderboard': "🏆 Leaderboard",
        'btn_edit_info': "📝 Edit Info",
        'kyc_success_msg': "✅ Success! KYC approved.",
        'kyc_verified_full': "🎉 **KYC Verified!** You can now create offers.",
        'kyc_err_rejected': "❌ Verification not found or rejected. Please try again.",
        'kyc_req_create': "⚠️ **Identity Verification Required**\n\nTo create your own offers, you must complete KYC verification.\n\n🔗 **Link:** {link}\n\nPlease submit your documents and wait for approval. You will receive a notification once it's done.",
        'kyc_under_review': "⏳ **Your verification is currently under review.**\nPlease wait for the approval notification.",

        # Створення Оголошення (Offers)
        'offer_cancel_msg': "❌ **Creation cancelled.**",
        'offer_step1': "➕ **Create Offer**\nSelect operation type:",
        'offer_step2': "💎 **Step 2: Select Crypto Asset:**",
        'offer_step3': "🌍 **Step 3: Select Region**\nYour profile country: **{country}**",
        'btn_custom_search': "🔍 Custom Search (Text)",
        'btn_back_regions': "⬅️ Back to Regions",
        'offer_country_selected': "💰 **Country:** {country}\n**Currency:** {fiat}",
        'btn_confirm_fiat': "✅ Confirm {fiat}",
        'btn_change_fiat': "🔄 Change Currency",
        'btn_back': "⬅️ Back",
        'offer_search_fiat': "🔍 **Type currency code (e.g. GBP):**",
        'offer_step5': "💵 **Step 5: Price**\nEnter price for 1 {asset} in {fiat}:",
        'offer_select_cat': "🏦 **Select Payment Category:**",
        'btn_back_cat': "⬅️ Back to Categories",
        'offer_select_method': "📍 **{cat}**\nSelect specific method:",
        'offer_created_ok': "✅ **Offer successfully created!**",
        'offer_ask_vol': "📊 **Total Volume:** How much {asset} in total?",
        'err_valid_num': "❌ Enter a valid number:",
        'offer_ask_limits': "🔢 **Enter limits in {fiat}** (e.g. 500-5000):",
        'err_limits_format': "❌ Format: 500-5000",
        'err_3_letter': "❌ Enter 3-letter code (e.g. PLN):",
        'offer_confirm_fiat_prompt': "✅ Use **{fiat}**?",

        # Фільтри та Купівля
        'filter_step1': "💎 **Step 1: Select Asset to trade:**",
        'filter_step2': "🌍 **Step 2: Select Country**\nAsset: `{asset}`",
        'btn_all_countries': "🌍 All Countries",
        'filter_select_country': "📍 **{region}**\nSelect country:",
        'filter_step3': "💰 **Step 3: Select Fiat**\nCountry: {country}",
        'btn_search_currency': "🔍 Search Currency",
        'btn_other': "🔄 Other",
        'filter_step4': "🏦 **Step 4: Payment Method**\nCurrency: {fiat}",
        'btn_all_methods': "💳 All Methods",
        'no_offers_found': "📭 No offers found.",
        'btn_reset_filters': "🔄 Reset Filters",
        'offers_list_title': "🚀 **Available Offers for {asset}**\n🌍 Country: {country}\n💳 Method: {method}",
        'err_offer_gone': "❌ Offer no longer exists.",
        'trade_open_card': "📈 **Opening Trade**\n━━━━━━━━━━━━━━━━━━━━\n👤 **Trader:** {nick}\n💵 **Price:** {price} {fiat}\n💳 **Method:** {method}\n🛡️ **Limits:** {min} - {max} {fiat}\n\n🔢 **Enter amount in {fiat} you want to trade:**",
        'err_out_of_limits': "❌ **Amount out of limits!**\nPlease enter between {min} and {max} {fiat}:",
        'trade_created_ok': "✅ **Order #{id} created!**\nNotification sent to the partner. Use /dashboard to manage.",
        'err_create_db': "❌ Error creating order in database.",

        # Swap
        'swap_login_req': "❌ **Log in first.**",
        'swap_step1': "🔄 **SWAP: Step 1/3**\n\nSelect the assets you want to exchange:",
        'btn_next_amount': "➡️ Next: Enter Amount",
        'swap_step2': "🔄 **SWAP: Step 2/3**\n━━━━━━━━━━━━━━━━━━━━\n💱 Pair: `{f_asset} ➔ {t_asset}`\n📊 Rate: `1:{rate}`\n⚠️ Min: `{min}`\n\n💰 **Amount to pay:** `{amount}`",
        'btn_set_amount': "✏️ Set Amount",
        'btn_next_review': "➡️ Next: Review",
        'swap_step3': "🏁 **SWAP: Final Step**\n━━━━━━━━━━━━━━━━━━━━\n📤 **You Send:** `{f_amount} {f_asset}`\n📥 **You Get:** `~{t_amount} {t_asset}`\n━━━━━━━━━━━━━━━━━━━━\n🚀 *Ready to execute transaction?*",
        'btn_confirm_swap': "✅ CONFIRM & SWAP",
        'swap_pick_from': "📤 Select Asset to Pay:",
        'swap_pick_to': "📥 Select Asset to Receive:",
        'err_same_assets': "⚠️ Assets must be different",
        'swap_ask_amount': "🔢 **Enter amount of {asset} you want to swap:**",
        'swap_creating_msg': "⏳ **Creating order with ChangeNOW...**",
        'swap_created_msg': "✅ **Order Created! (ID: `{id}`)**\n\n🔐 *Signing transaction to send {amount} {asset}...*",
        'swap_success': "🚀 **Swap Transaction Sent!**\n\n📥 **ChangeNOW Order ID:** `{id}`\n🔗 **Your TX Hash:** `{hash}`\n\n⏳ ChangeNOW is processing your swap. Your {t_asset} will arrive in your wallet shortly.",
        'swap_err_tx': "❌ **Transaction Failed:**\n{hash}",
        'swap_err_api': "❌ ChangeNOW API Error:\n{msg}",
        'swap_err_crit': "❌ Critical Swap Error: {err}",
        'err_positive_num': "❌ Please enter a valid positive number.",

        # Торгівля, Ескроу, Чат Угоди
        'dash_no_trades': "📭 **No active trades found.**",
        'dash_active': "📊 **Active Trades: {count}**",
        'err_invalid_trans': "⚠️ Invalid status transition!",
        'act_pending': "⚠️ Transaction is already pending!",
        'signing_tx': "🔐 **Signing transaction...**",
        'err_status_reset': "❌ **Error:** {err}\n\nStatus reset. Buttons restored.",
        'already_processing': "⚠️ Already processing...",
        'releasing_crypto': "🔐 **Releasing crypto...**",
        'err_release_fail': "❌ **Release Failed:** {err}\n\nButtons restored.",
        'err_cancel_paid': "❌ Cannot cancel paid or completed trade.",
        'refunding_msg': "⚠️ **Refunding funds from Escrow...**",
        'refund_ok': "✅ **Trade Cancelled & Refunded!**\nTX: `{res}`",
        'refund_fail': "❌ **Refund failed:** {res}\nPlease contact support.",
        'trade_cancelled': "❌ **Trade has been cancelled.**",
        'dispute_warn': "🚨 **Are you sure you want to open a dispute?**\n\nThis will freeze the trade and invite an administrator to the chat. Please only do this if there is a real problem (e.g., payment not received).",
        'btn_yes_dispute': "✅ Yes, Open Dispute",
        'err_order_not_found': "❌ Order not found.",
        'err_dispute_active': "⚠️ Dispute is already active.",
        'dispute_opened_ok': "✅ **Dispute has been successfully opened.**\nPlease go to the web chat and provide evidence (screenshots of payment/non-payment).",
        'btn_web_chat': "🌐 Open Web Chat",
        'chat_enter_msg': "✍️ **Type your message:**\nUse the button below to exit chat.",
        'btn_leave_chat': "🚫 Leave Chat Mode",
        'chat_closed': "📴 Chat closed.",
        'msg_sent': "✅ **Sent!**",
        'err_send_msg': "❌ Error sending message: {err}",
        'chat_mode_disabled': "📴 **Chat mode disabled.** Returning to menu...",
        'sys_update': "🔔 **System Update:**\n\n{msg}",
        'admin_msg': "🛡️ **Admin Message:**\n\n{msg}",
        'new_msg_from': "💬 **New message from {name}:**\n\n{msg}",
        'trade_card_dispute': "🚨 **TRADE UNDER DISPUTE** 🚨",
        'trade_card_sell': "💰 **Selling {amount} {asset}**",
        'trade_card_buy': "🛒 **Buying {amount} {asset}**",
        'btn_arbitration': "⚖️ Chat with Arbitrator",
        'btn_verify_block': "⛓️ Verifying on Blockchain...",
        'btn_processing_block': "⏳ Processing on Blockchain...",
        'btn_accept_trade': "✅ Accept Trade",
        'btn_fund_escrow': "🔒 Fund Escrow",
        'btn_release_crypto': "💸 Release Crypto",
        'btn_dispute': "🚨 Dispute",
        'btn_i_paid': "✅ I Have Paid",
        'btn_view_profile': "👤 View {name}'s Profile",

        # Statuses
        'st_created': 'Initiated 🏁',
        'st_accepted': 'Waiting for escrow ⏳',
        'st_waiting': 'Confirming... ⛓️',
        'st_funded': 'Escrow funded ✅',
        'st_paid': 'Payment sent 💸',
        'st_completed': 'Completed 🎉',
        'st_cancelled': 'Cancelled ❌',
        'st_resolved': 'Resolved by Admin 🛡️',
        'st_processing': 'Processing...',

        # Review System
        'review_invite': "🎉 **Trade completed!** Rate your experience with **{name}**:",
        'review_selected': "⭐ You selected **{stars}/5 stars**. \n\n✍️ Now, please write a short **comment** about the trade (or press skip):",
        'btn_skip_save': "⏩ Skip & Save",
        'review_saved': "✅ **Your review has been saved!** Thank you for helping the community.",

        # Community & News
        'no_more_posts': "🏁 **No more posts to show.**",
        'end_of_page': "👆 **End of page**",
        'btn_load_more': "⬇️ Load More Posts",
        'err_login_first': "❌ Login first",
        'post_deleted': "Post deleted.",
        'no_comments_yet': "📭 No comments yet.",
        'latest_comments': "💬 **Latest Comments:**\n\n",
        'type_comment_prompt': "✍️ **Type your comment below:**",
        'btn_cancel_comment': "❌ Cancel Comment",
        'comment_cancelled': "🚫 **Comment cancelled.**",
        'comment_published': "✅ **Comment published!**",
        'no_news': "📓 No news yet.",
        'top_update': "🟢 **TOP UPDATE**\n_{date}_\n\n🔥 **{title}**\n{text}\n\n",
        'recent_updates': "━━━━━━━━━━━━━━━━━━━━\n📜 **RECENT UPDATES**\n\n",
        'btn_view_website': "🌐 View all on Website",
        'err_news_load': "⚠️ Error loading news. Please try again later.",
        'social_text': "🌍 **Join Community!**\n━━━━━━━━━━━━━━━━━━━━\nStay updated with the latest news, updates, and community events on our official channels:",

        # AI Support
        'ai_support_on': "🆘 **AI Support ON.** Type your question (or 'exit'):",
        'ai_support_off': "📴 **AI Support session closed.**",
        'ai_overload': "⚠️ **System Overloaded.** I'm receiving too many requests right now. Please try again in 1 minute.",
        'ai_reboot': "🤖 Sorry, I need a moment to reboot. Please try asking again in a few seconds.",

        # Leaderboard
        'lb_empty': "Leaderboard is empty.",
        'lb_title': "🏆 **TOP TRADERS**\n━━━━━━━━━━━━━━━━━━━━\n\n",

        'btn_pro_migration': "👑 PRO Migration (Free trial)",
        'btn_buy_pro': "💳 Buy PRO Subscription",
        'mig_start_msg': "🚀 **PRO Migration**\n\nShow us your reputation on other platforms (Binance, Paxful, etc.) and get **7 days of PRO for free**.\n\nStep 1: Send the link to your external profile:",
        'mig_video_msg': "Step 2: Upload a video screen recording of that profile. You must show your nickname and trade history. (Max 50MB)",
        'mig_done': "✅ **Application Sent!** Our admins will verify your video shortly.",
        'buy_pro_msg': "💎 **PRO Benefits:**\n• 0.5% trade fee (instead of 1%)\n• Priority in ad listing\n• VIP Support badge\n\n**Price:** 100 USDT / month\n\nTo purchase, send exactly `100` USDT (TRC20) to this address:",

        # KYC & Migration Notifications
        'mig_approved_msg': "👑 **PRO Status Activated!** Your migration has been approved. Enjoy your 7-day free trial!",
        'mig_rejected_msg': "❌ **Migration Declined.** Your PRO application was not approved. Please contact support for details.",
        'kyc_approved_msg': "🎉 **Verification Successful!** Your identity has been verified. You can now create offers.",
        'kyc_rejected_msg': "❌ **Verification Failed.** Your identity documents were declined. Please try again.",

        # Media & Chat
        'uploading_msg': "⏳ **Uploading to secure storage...**",
        'err_video_req': "❌ Please send a **video file**.",
        'err_chat_media': "❌ Only photos and text are supported in chat.",
        'photo_sent_ok': "✅ **Photo sent!**",

        # Управління оголошеннями
        'btn_manage_offers': "⚙️ Manage Offers",
        'manage_offers_title': "📋 **Your Offers:**\nSelect an offer to manage:",
        'no_offers_manage': "📭 You don't have any offers to manage.",
        'ad_manage_title': "⚙️ **Managing Offer**\n\n{type} {asset} for {fiat}\n**Price:** {price}\n**Limits:** {min}-{max}\n**Status:** {status}",
        'btn_ad_status': "⏯ Toggle Status",
        'btn_ad_price': "💵 Edit Price",
        'btn_ad_limits': "⚖️ Edit Limits",
        'btn_ad_delete': "🗑 Delete Offer",
        'ad_deleted': "✅ Offer successfully deleted.",
        'ad_enter_new_price': "💵 Enter new price for this offer:",
        'ad_enter_new_limits': "⚖️ Enter new limits (format: MIN-MAX, e.g., 50-500):",
        'ad_updated': "✅ Offer successfully updated!",
        'btn_alerts': "🔔 Alerts",
    }
}

import re
from telegram import InlineKeyboardMarkup, InlineKeyboardButton


def is_valid_email(email):
    # Регулярний вираз для перевірки формату email
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None


def get_text(lang, key):
    # Повертаємо текст за мовою та ключем. Якщо мови чи ключа немає — повертаємо англійську або сам ключ.
    return texts.get(lang, texts.get('en', {})).get(key, key)


def check_and_notify_price_alerts(new_ad_data, ad_id):
    print(f"\n🔔 [ALERT CHECK] Started for Ad: {ad_id}")
    try:
        # 1. Беремо дані нового оффера
        asset = new_ad_data.get('asset')
        ad_price_raw = new_ad_data.get('price')
        ad_type = new_ad_data.get('type')  # 'buy' або 'sell'
        ad_fiat = new_ad_data.get('fiat')
        ad_country = new_ad_data.get('country', 'Unknown')
        ad_creator_uid = new_ad_data.get('uid')

        if not all([asset, ad_price_raw, ad_type, ad_fiat]):
            print("❌ [ALERT CHECK] Missing fields. Skipping.")
            return

        offer_price = float(str(ad_price_raw))
        print(f"👉 NEW OFFER: {ad_type.upper()} {asset} @ {offer_price} {ad_fiat} in {ad_country}")

        # 2. Шукаємо протилежні алерти.
        alert_type_to_find = 'sell' if ad_type == 'buy' else 'buy'

        alerts_ref = db.collection('price_alerts') \
            .where(filter=FieldFilter('asset', '==', asset)) \
            .where(filter=FieldFilter('type', '==', alert_type_to_find)) \
            .where(filter=FieldFilter('is_active', '==', True)).get()

        print(f"👀 Found {len(alerts_ref)} active alerts for {alert_type_to_find.upper()} {asset}")

        for doc in alerts_ref:
            alert = doc.to_dict()
            tg_id = alert.get('tg_id')
            alert_uid = alert.get('uid')

            # Якщо потрібно ігнорувати власні оголошення:
            # if alert_uid == ad_creator_uid: continue

            # Перевірка фіату
            if alert.get('fiat') != ad_fiat:
                continue

            # Перевірка країни
            alert_country = alert.get('country', 'All Countries')
            if alert_country != 'All Countries' and alert_country != ad_country:
                print(f"⏭️ Skip {tg_id}: Country mismatch ({alert_country} vs {ad_country})")
                continue

            # ПЕРЕВІРКА ЦІНИ (The core logic)
            alert_target_price = float(str(alert.get('target_price', 0)))
            triggered = False

            if alert['type'] == 'sell' and offer_price >= alert_target_price:
                triggered = True
                print(f"✅ MATCH (SELL): Offer price {offer_price} >= Target {alert_target_price}")
            elif alert['type'] == 'buy' and offer_price <= alert_target_price:
                triggered = True
                print(f"✅ MATCH (BUY): Offer price {offer_price} <= Target {alert_target_price}")

            # Якщо зійшлося — надсилаємо пуш з урахуванням мови користувача (якщо вона збережена в алерті)
            if triggered and tg_id:
                try:
                    u_lang = alert.get('lang', 'en')  # Отримуємо мову юзера, за замовчуванням 'en'
                    action_word = "Buying" if ad_type == "buy" else "Selling"

                    msg = (
                        f"🔔 **PRICE ALERT: MATCH FOUND!**\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"💎 Someone is {action_word}: `{asset}`\n"
                        f"💰 Price: **{offer_price} {ad_fiat}**\n"
                        f"🌍 Country: `{ad_country}`\n"
                        f"🎯 Your Target: `{alert_target_price} {ad_fiat}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🚀 Open trade before it's gone!"
                    )

                    markup = types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton("📈 View Offer", callback_data=f"init_ad_{ad_id}")
                    )
                    bot.send_message(tg_id, msg, reply_markup=markup, parse_mode="Markdown")
                    print(f"📨 PUSH SENT to {tg_id}!")
                except Exception as e:
                    print(f"❌ Failed to send to {tg_id}: {e}")

    except Exception as e:
        print(f"❌ CRITICAL ALERT ERROR: {e}")
        import traceback
        traceback.print_exc()


# ==========================================
# 🔥 PROMOCODE / CASHBACK LOGIC
# ==========================================
def process_cashback_and_increment_trades(order_id, order_data):
    """Збільшує лічильник угод продавця. Якщо це перша угода - робить кешбек 5%."""
    try:
        seller_uid = order_data.get('sellerUid')
        if not seller_uid: return

        user_ref = db.collection('users').document(seller_uid)
        user_doc = user_ref.get()
        if not user_doc.exists: return

        u_data = user_doc.to_dict()
        completed_count = u_data.get('completedTradesCount', 0)

        # 1. Завжди збільшуємо лічильник успішних угод
        user_ref.update({'completedTradesCount': firestore.Increment(1)})

        # 2. Якщо це ПЕРША угода — робимо кешбек
        if completed_count == 0:
            print(f"🎁 First trade promo for {seller_uid}! Processing cashback...")
            send_system_message_to_chat(order_id, "🎁 First trade promo! Processing platform fee cashback...")

            # Секретна фраза адміністратора
            ADMIN_MNEMONIC = ""
            fee_amount = float(order_data.get('amountCrypto', 0)) * PLATFORM_FEE_PERCENT
            asset = str(order_data.get('asset', '')).upper()
            network = str(order_data.get('network', '')).upper()

            is_tron = 'TRON' in network or 'TRC' in asset or asset == 'TRX' or (
                    asset == 'USDT' and not any(x in network for x in ['ETH', 'ERC', 'BNB', 'BSC']))
            is_bsc = 'BNB' in network or 'BSC' in network or asset == 'BNB'
            is_eth = 'ETH' in network or 'ERC' in network or asset == 'ETH'
            is_sol = 'SOL' in network or 'SOL' in asset
            is_btc = 'BTC' in network or 'BTC' in asset

            # Шукаємо гаманець продавця
            seller_addr = order_data.get('sellerWalletAddress')
            if not seller_addr:
                if is_sol:
                    seller_addr = str(get_solana_keypair(u_data.get('walletMnemonic')).pubkey())
                elif is_btc:
                    seller_addr = get_buyer_btc_address(seller_uid)
                elif is_tron:
                    seller_acc = Account.from_mnemonic(u_data.get('walletMnemonic'), account_path="m/44'/195'/0'/0/0")
                    seller_addr = eth_to_tron_address(seller_acc.address)
                else:
                    w3_temp = Web3()
                    seller_addr = w3_temp.eth.account.from_mnemonic(u_data.get('walletMnemonic'),
                                                                    account_path="m/44'/60'/0'/0/0").address

            if not seller_addr:
                raise Exception("Seller address not found")

            # Відправляємо кешбек залежно від мережі
            if is_tron:
                from tronpy.providers import HTTPProvider
                client = Tron(HTTPProvider(BLOCKCHAIN_CONFIG['TRON']['rpc']))
                acc = Account.from_mnemonic(ADMIN_MNEMONIC, account_path="m/44'/195'/0'/0/0")
                priv_key = PrivateKey(bytes.fromhex(acc.key.hex().replace('0x', '').zfill(64)))

                if asset == 'TRX':
                    txn = client.trx.transfer(eth_to_tron_address(acc.address), seller_addr,
                                              int(fee_amount * 1_000_000)).build().sign(priv_key)
                    txn.broadcast().wait()
                else:
                    usdt_addr = BLOCKCHAIN_CONFIG['TRON']['usdt_contract']
                    usdt_cntr = client.get_contract(usdt_addr)
                    txn = usdt_cntr.functions.transfer(seller_addr, int(fee_amount * 1_000_000)).with_owner(
                        eth_to_tron_address(acc.address)).fee_limit(150_000_000).build().sign(priv_key)
                    txn.broadcast().wait()

            elif is_eth or is_bsc:
                rpc = BLOCKCHAIN_CONFIG['BSC']['rpc'] if is_bsc else BLOCKCHAIN_CONFIG['ETH']['rpc']
                w3 = Web3(Web3.HTTPProvider(rpc))
                admin_acc = w3.eth.account.from_mnemonic(ADMIN_MNEMONIC, account_path="m/44'/60'/0'/0/0")

                latest_block = w3.eth.get_block('latest')
                base_fee = latest_block.get('baseFeePerGas', w3.to_wei(3, 'gwei'))
                priority_fee = w3.to_wei(2, 'gwei')
                max_fee = int(base_fee * 2) + priority_fee

                if asset in ['ETH', 'BNB']:
                    tx = {
                        'nonce': w3.eth.get_transaction_count(admin_acc.address, 'pending'),
                        'to': Web3.to_checksum_address(seller_addr),
                        'value': w3.to_wei(fee_amount, 'ether'),
                        'gas': 21000,
                        'chainId': 56 if is_bsc else 1
                    }
                    if is_bsc:
                        tx['gasPrice'] = w3.eth.gas_price
                    else:
                        tx['maxFeePerGas'] = max_fee
                        tx['maxPriorityFeePerGas'] = priority_fee

                    signed_tx = w3.eth.account.sign_transaction(tx, admin_acc.key)
                    w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                else:
                    token_addr = Web3.to_checksum_address(
                        BLOCKCHAIN_CONFIG['BSC' if is_bsc else 'ETH']['usdt_contract'])
                    token_abi = [{"constant": False,
                                  "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
                                  "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
                                 {"constant": True, "inputs": [], "name": "decimals",
                                  "outputs": [{"name": "", "type": "uint8"}], "type": "function"}]
                    contract = w3.eth.contract(address=token_addr, abi=token_abi)
                    decimals = contract.functions.decimals().call()

                    tx = contract.functions.transfer(Web3.to_checksum_address(seller_addr),
                                                     int(fee_amount * (10 ** decimals))).build_transaction({
                        'from': admin_acc.address,
                        'nonce': w3.eth.get_transaction_count(admin_acc.address, 'pending'),
                        'gas': 100000,
                        'chainId': 56 if is_bsc else 1
                    })
                    if is_bsc:
                        tx['gasPrice'] = w3.eth.gas_price
                    else:
                        tx['maxFeePerGas'] = max_fee
                        tx['maxPriorityFeePerGas'] = priority_fee

                    signed_tx = w3.eth.account.sign_transaction(tx, admin_acc.key)
                    w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            else:
                send_system_message_to_chat(order_id, "⚠️ Cashback for BTC/SOL must be processed manually by Admin.")
                return

            send_system_message_to_chat(order_id, f"✅ Cashback of {fee_amount} {asset} sent to seller!")

    except Exception as e:
        print(f"❌ Cashback error: {e}")
        send_system_message_to_chat(order_id, "⚠️ Cashback processing failed. Admin will process it manually.")


def register_user_in_firebase(chat_id, s):
    """
    Step 1: Create Auth user and send verification email.
    Profile and Wallet are NOT created yet.
    """
    email = s.get('reg_email')
    password = s.get('reg_pass')
    lang = s.get('lang', 'en')  # Отримуємо поточну мову сесії

    try:
        # 1. Створюємо користувача у Firebase Auth
        user = auth.create_user(email=email, password=password)
        uid = user.uid

        # 2. Отримуємо ID Token для відправки email верифікації через REST API
        auth_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_WEB_API_KEY}"
        r = requests.post(auth_url, json={"email": email, "password": password, "returnSecureToken": True}, timeout=10)

        if r.status_code == 200:
            id_token = r.json().get('idToken')
            verify_url = f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={FIREBASE_WEB_API_KEY}"
            requests.post(verify_url, json={"requestType": "VERIFY_EMAIL", "idToken": id_token}, timeout=10)

        # 3. Тимчасовий статус сесії
        s.update({
            "uid": uid,
            "state": "waiting_email_verification",
            "authorized": False
        })

        markup = types.InlineKeyboardMarkup()
        # Текст кнопки "I have verified my email" можна буде винести до словника, якщо додаси туди ключ
        markup.add(types.InlineKeyboardButton("🔄 I have verified my email", callback_data="check_verification"))

        msg = (
            f"📧 **Verification email sent to** `{email}`\n\n"
            f"Please click the link in the email to verify your account.\n"
            f"**Note:** Your secure wallet and profile will be generated ONLY after verification."
        )
        bot.send_message(chat_id, msg, reply_markup=markup, parse_mode="Markdown")

    except Exception as e:
        bot.send_message(chat_id, f"❌ Registration failed: {str(e)}")
        s['state'] = None


# --- Callback для перевірки верифікації ---
@bot.callback_query_handler(func=lambda call: call.data == "check_verification")
def check_email_verified(call):
    chat_id = call.message.chat.id
    s = user_sessions.get(chat_id)
    if not s or not s.get('uid'): return

    try:
        # Перевірка статусу через Admin SDK
        user = auth.get_user(s['uid'])

        if user.email_verified:
            # Успіх! Створюємо фінальний профіль та гаманець
            create_final_profile(chat_id, s)
        else:
            bot.answer_callback_query(call.id, "❌ Email is not verified yet. Check your inbox!", show_alert=True)
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error checking verification: {e}")\



def create_final_profile(chat_id, s):
    """Final step: Generate wallet and Firestore document after email is verified"""
    lang = s.get('lang', 'en')
    t = lambda key: get_text(lang, key)

    bot.send_message(chat_id, f"⚙️ **{t('msg_email_verified_generating')}**")

    uid = s['uid']
    email = s.get('reg_email')
    nickname = s.get('reg_nickname', 'User')

    # Дістаємо країну, яку ми зберегли в handle_callbacks
    country_data = s.get('temp_country_data', {'name': 'United Kingdom', 'fiat': 'GBP'})

    # Generate Wallet
    new_acc, mnemonic = Account.create_with_mnemonic()

    # Data structure
    user_data = {
        'uid': uid,
        'email': email,
        'nickname': nickname,
        'country': country_data['name'],
        'fiat': country_data['fiat'],
        'walletMnemonic': mnemonic,
        'tg_id': chat_id,
        'createdAt': datetime.now(timezone.utc).isoformat(),
        'role': 'user',
        'rating': 0,
        'tradesCount': 0,
        'is2FAEnabled': False,
        'isEmailVerified': True,
        'kycStatus': 'none'
    }

    db.collection('users').document(uid).set(user_data)

    # Очищаємо сесію та авторизуємо
    s.update({"authorized": True, "state": None})
    s.pop('reg_email', None)
    s.pop('reg_pass', None)
    s.pop('temp_country_data', None)

    success_msg = (
        f"🎉 **Account Created!**\n\n"
        f"👤 **Nickname:** `{nickname}`\n"
        f"🌍 **Country:** `{country_data['name']}`\n"
        f"🔐 **Your Seed Phrase (SAVE THIS):**\n`{mnemonic}`"
    )
    bot.send_message(chat_id, success_msg, parse_mode="Markdown")
    show_bottom_menu(chat_id, lang)


def get_ads_markup(trade_type, lang):
    """Витягує оголошення (ads) та створює клавіатуру"""
    try:
        # Якщо ми хочемо купити (buy), нам потрібні оголошення про продаж (sell)
        target_type = 'sell' if trade_type == 'buy' else 'buy'
        ads_ref = db.collection('ads').where(filter=FieldFilter('type', '==', target_type)).get()

        if not ads_ref: return None

        markup = types.InlineKeyboardMarkup(row_width=1)
        for ad in ads_ref:
            d = ad.to_dict()
            if d.get('status') != 'active': continue

            # Текст кнопки: Ціна | Актив | Метод
            label = f"💰 {d.get('price')} {d.get('fiat')} | {d.get('asset')} | 🏦 {d.get('paymentMethod')}"
            markup.add(types.InlineKeyboardButton(label, callback_data=f"init_ad_{ad.id}"))
        return markup
    except Exception as e:
        print(f"❌ Ads error: {e}")
        return None


def update_swap_estimate(s):
    """Отримує актуальний розрахунок суми та мінімальні ліміти від ChangeNOW"""
    try:
        sw = s.get('swap')
        f_asset = SWAP_ASSETS[sw['from_asset']]
        t_asset = SWAP_ASSETS[sw['to_asset']]

        # --- НОВИЙ БЛОК: Запит мінімальної суми ---
        range_res = requests.get("https://api.changenow.io/v2/exchange/range", params={
            "fromCurrency": f_asset['ticker'],
            "toCurrency": t_asset['ticker'],
            "fromNetwork": f_asset['network'],
            "toNetwork": t_asset['network'],
            "flow": "standard"
        }, headers={"x-changenow-api-key": CHANGENOW_API_KEY})

        if range_res.status_code == 200:
            s['swap']['min_amount'] = range_res.json().get('minAmount', 0.0)
        # ------------------------------------------

        if sw['from_amount'] <= 0:
            s['swap']['to_amount'] = 0.0
            s['swap']['rate'] = None
            return

        # Запит розрахунку (Estimate)
        res = requests.get("https://api.changenow.io/v2/exchange/estimated-amount", params={
            "fromCurrency": f_asset['ticker'],
            "toCurrency": t_asset['ticker'],
            "fromAmount": sw['from_amount'],
            "fromNetwork": f_asset['network'],
            "toNetwork": t_asset['network'],
            "flow": "standard"
        }, headers={"x-changenow-api-key": CHANGENOW_API_KEY})

        if res.status_code == 200:
            data = res.json()
            s['swap']['to_amount'] = data.get('toAmount', 0.0)
            if sw['from_amount'] > 0:
                s['swap']['rate'] = s['swap']['to_amount'] / sw['from_amount']
            s['swap']['error'] = None
        else:
            s['swap']['error'] = res.json().get('message', 'Pair not supported')
    except Exception as e:
        s['swap']['error'] = "API Error"


def show_swap_menu(chat_id, s, message_id=None):
    lang = s.get('lang', 'en')
    t = lambda key: get_text(lang, key)

    if 'swap' not in s or not s['swap']:
        s['swap'] = {
            'step': 1,  # Починаємо з вибору активів
            'from_asset': 'USDT TRC20', 'to_asset': 'SOL',
            'from_amount': 0.0, 'to_amount': 0.0,
            'rate': None, 'min_amount': 0.0, 'error': None
        }

    sw = s['swap']
    step = sw.get('step', 1)
    markup = types.InlineKeyboardMarkup(row_width=2)

    # --- КРОК 1: ВИБІР ПАРИ ---
    if step == 1:
        text = f"🔄 **{t('swap_step_1_title')}**\n\n{t('swap_step_1_desc')}"
        markup.row(
            types.InlineKeyboardButton(f"📤 Pay: {sw['from_asset']}", callback_data="sw_pick_from"),
            types.InlineKeyboardButton(f"📥 Get: {sw['to_asset']}", callback_data="sw_pick_to")
        )
        markup.add(types.InlineKeyboardButton(f"➡️ {t('btn_next_enter_amount')}", callback_data="sw_next_step"))

    # --- КРОК 2: ВВЕДЕННЯ СУМИ ---
    elif step == 2:
        update_swap_estimate(s)
        rate_text = f"{sw['rate']:.6f}" if sw.get('rate') else "..."
        text = (
            f"🔄 **{t('swap_step_2_title')}**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💱 Pair: `{sw['from_asset']} ➔ {sw['to_asset']}`\n"
            f"📊 Rate: `1:{rate_text}`\n"
            f"⚠️ Min: `{sw.get('min_amount', 0)}`\n\n"
            f"💰 **Amount to pay:** `{sw['from_amount']}`"
        )
        markup.add(types.InlineKeyboardButton(f"✏️ {t('btn_set_amount')}", callback_data="sw_enter_amount"))

        # Додаємо кнопку "Далі" тільки якщо сума > мінімалки
        if sw['from_amount'] >= sw.get('min_amount', 0) and sw['from_amount'] > 0:
            markup.add(types.InlineKeyboardButton(f"➡️ {t('btn_next_review')}", callback_data="sw_next_step"))

        markup.add(types.InlineKeyboardButton(f"⬅️ {t('btn_back')}", callback_data="sw_prev_step"))

    # --- КРОК 3: ФІНАЛЬНИЙ ПЕРЕГЛЯД ---
    elif step == 3:
        update_swap_estimate(s)
        text = (
            f"🏁 **{t('swap_step_3_title')}**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📤 **You Send:** `{sw['from_amount']} {sw['from_asset']}`\n"
            f"📥 **You Get:** `~{sw['to_amount']} {sw['to_asset']}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 *Ready to execute transaction?*"
        )
        markup.add(types.InlineKeyboardButton(f"✅ {t('btn_confirm_swap')}", callback_data="sw_confirm"))
        markup.add(types.InlineKeyboardButton(f"⬅️ {t('btn_back')}", callback_data="sw_prev_step"))

    # Відправка/Редагування
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode="Markdown")
        except Exception:
            pass
    else:
        try:
            msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
            s['swap_msg_id'] = msg.message_id
        except Exception:
            pass


def cancel_order_onchain(order_id, order, mnemonic):
    asset_up = str(order.get('asset', '')).upper()
    try:
        # --- SOLANA REFUND ---
        if "SOL" in asset_up or "USDC" in asset_up:
            return release_escrow_sol(order_id, order, is_refund=True)

        # --- BTC REFUND ---
        elif "BTC" in asset_up:
            return release_escrow_btc(order_id, {**order, 'buyerUid': order['sellerUid']})

        # --- EVM / TRON ---
        return "Manual refund required for this network"

    except Exception as e:
        return f"ERROR: {e}"


def show_user_profile(chat_id, target_uid, lang='en'):
    """Універсальна функція для показу профілю: аватарка, KYC, Топ-5 та логіка PRO на 7 днів"""
    t = lambda key: get_text(lang, key)

    try:
        user_doc_ref = db.collection('users').document(target_uid).get()
        if not user_doc_ref.exists:
            bot.send_message(chat_id, f"❌ **{t('profile_not_found')}**")
            return

        user_data = user_doc_ref.to_dict()
        nickname = user_data.get('nickname', 'User')
        user_blurb = user_data.get('blurb', 'No status bio set.')

        # --- ЛОГІКА ПЕРЕВІРКИ ТЕРМІНУ PRO (7 ДНІВ) ---
        is_pro_active = False
        pro_label = ""
        if user_data.get('subscriptionPlan') == 'pro_monthly_100':
            raw_date = user_data.get('proActivatedAt') or user_data.get('createdAt')

            if raw_date:
                if isinstance(raw_date, str):
                    start_date = datetime.fromisoformat(raw_date.replace('Z', '+00:00'))
                else:
                    start_date = raw_date

                expiry_date = start_date + timedelta(days=7)

                if datetime.now(timezone.utc) < expiry_date:
                    is_pro_active = True
                    pro_label = f"\n👑 **PRO Subscription active** (until {expiry_date.strftime('%d.%m.%Y')})"
                else:
                    db.collection('users').document(target_uid).update({
                        'subscriptionPlan': 'free',
                        'trialStatus': 'expired'
                    })
                    is_pro_active = False
                    if target_uid == user_sessions.get(chat_id, {}).get('uid'):
                        bot.send_message(chat_id, "⚠️ **Your PRO subscription has expired.**")

        # --- ЛОГІКА KYC ---
        kyc_status = user_data.get('kycStatus', 'none')
        if kyc_status == 'approved':
            kyc_status_text, kyc_icon = "✅ Verified Trader", "🛡️"
        elif kyc_status == 'rejected':
            kyc_status_text, kyc_icon = "❌ Verification Rejected", "👤"
        elif kyc_status == 'pending':
            kyc_status_text, kyc_icon = "⏳ Verification Pending", "👤"
        else:
            kyc_status_text, kyc_icon = "⚠️ Not Verified", "👤"

        # Розрахунок рейтингу
        rating_sum = user_data.get('ratingSum', 0)
        reviews_count = user_data.get('reviewsCount', 0)
        avg_rating = round(rating_sum / reviews_count, 1) if reviews_count > 0 else 0
        stars_visual = "⭐" * int(avg_rating) if avg_rating >= 1 else "No reviews"

        # Отримуємо активні оффери
        my_ads = db.collection('ads').where(filter=FieldFilter('uid', '==', target_uid)).where(
            filter=FieldFilter('status', '==', 'active')).get()

        offers_text = ""
        for ad_doc in my_ads:
            ad = ad_doc.to_dict()
            limits = ad.get('limits', {})
            offers_text += f"🔹 {ad.get('type').upper()} {ad.get('asset')}: {ad.get('price')} {ad.get('fiat')} (L: {limits.get('min', 0)}-{limits.get('max', 0)})\n"

        if not offers_text:
            offers_text = "No active offers."

        created_at_str = user_data.get('createdAt')
        member_since = created_at_str[:10] if isinstance(created_at_str, str) else 'N/A'

        # Формування повідомлення
        profile_msg = (
            f"{kyc_icon} **User Profile: {nickname}**{pro_label}\n"
            f"🛡️ **Status:** `{kyc_status_text}`\n"
            f"💬 _{user_blurb}_\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🌟 **Rating:** {avg_rating} / 5 {stars_visual} ({reviews_count} feedbacks)\n"
            f"📊 **Stats:** {user_data.get('tradesCount', 0)} completed trades\n"
            f"📅 **Member since:** {member_since}\n\n"
            f"🔥 **Active Offers:**\n{offers_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("⭐ View Feedbacks", callback_data=f"view_reviews_{target_uid}"))

        # ПЕРЕВІРКА: чи це мій власний профіль
        my_uid = user_sessions.get(chat_id, {}).get('uid')
        if target_uid == my_uid:
            if not is_pro_active:
                markup.row(
                    types.InlineKeyboardButton(t('btn_pro_migration'), callback_data="pro_migration_start"),
                    types.InlineKeyboardButton(t('btn_buy_pro'), callback_data="pro_buy_start")
                )

            if kyc_status != 'approved':
                blockpass_url = f"url"
                markup.add(types.InlineKeyboardButton("🛡️ Verify Identity", url=blockpass_url))
                markup.add(types.InlineKeyboardButton("🔄 Check Status", callback_data="check_kyc_status"))

            markup.add(types.InlineKeyboardButton(t('btn_manage_offers'), callback_data="manage_offers"))

            markup.row(
                types.InlineKeyboardButton("🏆 Leaderboard (Top 5)", callback_data="view_leaderboard"),
                types.InlineKeyboardButton("📝 Edit Info", url=f"url")
            )

        # Логіка аватарки
        avatar_url = user_data.get('avatarUrl')
        if avatar_url:
            try:
                bot.send_photo(chat_id, photo=avatar_url, caption=profile_msg, reply_markup=markup,
                               parse_mode="Markdown")
            except Exception as e:
                print(f"⚠️ Avatar send failed: {e}")
                bot.send_message(chat_id, profile_msg, reply_markup=markup, parse_mode="Markdown")
        else:
            bot.send_message(chat_id, profile_msg, reply_markup=markup, parse_mode="Markdown")

    except Exception as e:
        print(f"❌ Error rendering profile: {e}")
        import traceback
        traceback.print_exc()


def can_update_status(current_status, new_status):
    hierarchy = ['CREATED', 'ACCEPTED', 'WAITING_FOR_DEPOSIT', 'ESCROW_FUNDED', 'PAID', 'COMPLETED', 'CANCELLED',
                 'RESOLVED']
    try:
        return hierarchy.index(new_status) >= hierarchy.index(current_status)
    except ValueError:
        return True


import time
from datetime import datetime, timezone
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.system_program import transfer, TransferParams
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
from spl.token.instructions import transfer_checked, TransferCheckedParams, create_associated_token_account
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address


def send_bot_review_menu(order_id, order):
    """Надсилає запит на відгук обом сторонам угоди з урахуванням мови сесії"""
    buyer_chat = next((c for c, s in user_sessions.items() if s.get('uid') == order.get('buyerUid')), None)
    seller_chat = next((c for c, s in user_sessions.items() if s.get('uid') == order.get('sellerUid')), None)

    def send_markup(chat_id, target_uid, target_name):
        if not chat_id: return
        lang = user_sessions.get(chat_id, {}).get('lang', 'en')
        t = lambda key: get_text(lang, key)

        markup = types.InlineKeyboardMarkup(row_width=5)
        # Callback: rate_[target_uid]_[stars]
        btns = [types.InlineKeyboardButton(f"{i}⭐", callback_data=f"rate_{target_uid}_{i}") for i in range(1, 6)]
        markup.add(*btns)

        # Рекомендовано додати 'trade_completed_rate' у словник texts
        msg_text = f"🎉 **Trade completed!** Rate your experience with **{target_name}**:"
        bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="Markdown")

    send_markup(buyer_chat, order.get('sellerUid'), order.get('sellerName'))
    send_markup(seller_chat, order.get('buyerUid'), order.get('buyerName'))


@bot.callback_query_handler(func=lambda call: call.data.startswith('rate_'))
def handle_bot_rating(call):
    try:
        _, target_uid, stars = call.data.split('_')
        chat_id = call.message.chat.id
        lang = user_sessions.get(chat_id, {}).get('lang', 'en')
        t = lambda key: get_text(lang, key)

        user_sessions[chat_id]['pending_review'] = {
            'targetUid': target_uid,
            'rating': int(stars)
        }
        user_sessions[chat_id]['state'] = 'writing_review_text'

        bot.answer_callback_query(call.id)

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(
            t('btn_skip_save') if texts.get(lang, {}).get('btn_skip_save') else "⏩ Skip & Save",
            callback_data="skip_review_comment"))

        bot.edit_message_text(
            f"⭐ You selected **{stars}/5 stars**. \n\n✍️ Now, please write a short **comment** about the trade (or press skip):",
            chat_id, call.message.message_id, reply_markup=markup, parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Rating error: {e}")


@bot.callback_query_handler(func=lambda call: call.data == "skip_review_comment")
def skip_review_comment(call):
    chat_id = call.message.chat.id
    lang = user_sessions.get(chat_id, {}).get('lang', 'en')
    save_final_review(chat_id, "No comment provided.")
    bot.answer_callback_query(call.id, "Saved!")


def save_final_review(chat_id, comment_text):
    try:
        s = user_sessions.get(chat_id)
        rev_data = s.get('pending_review')
        if not rev_data: return

        target_uid = rev_data['targetUid']
        stars = rev_data['rating']

        # 1. Оновлюємо загальну статистику користувача
        user_ref = db.collection('users').document(target_uid)
        user_ref.update({
            'ratingSum': firestore.Increment(stars),
            'reviewsCount': firestore.Increment(1)
        })

        # 2. Додаємо окремий документ у колекцію reviews для історії
        db.collection('reviews').add({
            'targetUid': target_uid,
            'authorUid': s.get('uid'),
            'authorName': s.get('nickname') or "Trader",
            'rating': stars,
            'text': comment_text,
            'createdAt': datetime.now(timezone.utc)
        })

        bot.send_message(chat_id, "✅ **Your review has been saved!** Thank you for helping the community.",
                         parse_mode="Markdown")

        s['state'] = None
        s['pending_review'] = None
    except Exception as e:
        print(f"Save review error: {e}")


def create_order_in_db(s, ad_data, ad_id, amount_fiat):
    try:
        price = float(ad_data.get('price'))
        amount_crypto = round(amount_fiat / price, 8)
        is_buyer_of_ad = ad_data.get('type') == 'sell'

        my_name = s.get('nickname') or s.get('email', 'User').split('@')[0]

        new_order = {
            'adId': ad_id,
            'amountCrypto': amount_crypto,
            'amountFiat': amount_fiat,
            'asset': ad_data.get('asset'),
            'buyerName': my_name if is_buyer_of_ad else ad_data.get('nickname'),
            'buyerUid': s.get('uid') if is_buyer_of_ad else ad_data.get('uid'),
            'createdAt': datetime.now(timezone.utc),
            'creatorUid': s.get('uid'),
            'fiatCurrency': ad_data.get('fiat'),
            'network': ad_data.get('network', 'ETH'),
            'paymentMethod': ad_data.get('paymentMethod'),
            'priceAtMoment': price,
            'sellerName': ad_data.get('nickname') if is_buyer_of_ad else my_name,
            'sellerUid': ad_data.get('uid') if is_buyer_of_ad else s.get('uid'),
            'status': 'CREATED',
            'type': 'buy' if is_buyer_of_ad else 'sell',
            'messages': [],
            'isDisputed': False
        }
        _, doc_ref = db.collection('orders').add(new_order)
        return doc_ref.id
    except Exception as e:
        print(f"❌ Create order error: {e}")
        return None


def clean_addr(addr):
    if not addr or not isinstance(addr, str): return None
    return addr.strip().replace('\n', '').replace('\r', '').replace(' ', '')


def eth_to_tron_address(eth_addr):
    try:
        if not eth_addr: return None
        if eth_addr.startswith("T"): return clean_addr(eth_addr)
        clean_hex = eth_addr.replace('0x', '')
        tron_hex = "41" + clean_hex
        addr_bytes = bytes.fromhex(tron_hex)
        return base58.b58encode_check(addr_bytes).decode()
    except Exception as e:
        print(f"⚠️ Error converting address: {e}")
        return None


def generate_sol_escrow_for_bot(order_id, amount_crypto, asset="SOL"):
    """Генерує унікальний SOL гаманець з точною структурою для Firebase (як в React)"""
    kp = Keypair()
    address = str(kp.pubkey())
    priv_key = base58.b58encode(bytes(kp)).decode()

    trade_amount = float(amount_crypto)
    platform_fee = trade_amount * PLATFORM_FEE_PERCENT

    is_usdc = "USDC" in str(asset).upper()
    gas_reserve_sol = 0.005 if is_usdc else 0.00002

    total_required = trade_amount + platform_fee

    escrow_data = {
        'address': address,
        'secretKey': priv_key,
        'expectedAmount': f"{total_required:.6f}",
        'expectedGasReserve': gas_reserve_sol,
        'breakdown': {
            'trade': trade_amount,
            'fee': platform_fee,
            'gas': gas_reserve_sol
        },
        'network': 'SOLANA_MAINNET',
        'createdAt': datetime.now(timezone.utc)
    }

    if is_usdc:
        db.collection('orders').document(order_id).update({
            'escrowWallet': escrow_data,
            'solEscrowAddress': address
        })
    else:
        db.collection('orders').document(order_id).update({
            'escrowWallet': escrow_data
        })

    print(f"☀️ Created SOL MAINNET Escrow: {address} | Need: {total_required}")
    return address, total_required, gas_reserve_sol


def watch_sol_deposit(chat_id, order_id, escrow_address, expected_amount):
    """Моніторинг SOL балансу на ескроу-гаманці"""
    print(f"☀️ Monitoring SOL deposit for {order_id} at {escrow_address}")
    expected_lamports = int(float(expected_amount) * 1_000_000_000)

    for attempt in range(100):
        try:
            res = solana_client.get_balance(Pubkey.from_string(escrow_address))
            current_lamports = res.value if hasattr(res, 'value') else res

            if current_lamports >= expected_lamports:
                db.collection('orders').document(order_id).update({'status': 'ESCROW_FUNDED'})
                if chat_id:
                    bot.send_message(chat_id,
                                     "✅ **Solana Deposit Confirmed!**\nFunds are locked in escrow. Buyer can now pay.",
                                     parse_mode="Markdown")
                return
        except Exception as e:
            print(f"⚠️ SOL Monitoring error: {e}")
        time.sleep(10)
    print(f"⌛ SOL Monitoring timed out for {order_id}")


def auto_fund_sol_escrow(order_id, order, mnemonic_phrase):
    """Автоматичне поповнення ескроу-гаманця з підтримкою Gas Reserve"""
    try:
        seller_keypair = get_solana_keypair(mnemonic_phrase)
        seller_pubkey = seller_keypair.pubkey()

        asset_up = str(order.get('asset', '')).upper()
        is_usdc = "USDC" in asset_up

        if not order.get('escrowWallet'):
            escrow_addr, total_required, gas_reserve_sol = generate_sol_escrow_for_bot(order_id, order['amountCrypto'],
                                                                                       asset_up)
        else:
            escrow_addr = order['escrowWallet']['address']
            total_required = float(order['escrowWallet']['expectedAmount'])
            gas_reserve_sol = order['escrowWallet'].get('expectedGasReserve', 0.005 if is_usdc else 0.00002)

        dest_pubkey = Pubkey.from_string(escrow_addr)
        instructions = []

        balance_res = solana_client.get_balance(seller_pubkey)
        current_lamports = balance_res.value if hasattr(balance_res, 'value') else balance_res

        if current_lamports < 5_000_000:
            return f"ERROR: Low SOL balance for gas. Need ~0.005 SOL, have {current_lamports / 1e9} SOL."

        if is_usdc:
            usdc_mint = Pubkey.from_string(BLOCKCHAIN_CONFIG['SOL']['usdc_spl'])
            seller_ata = get_associated_token_address(seller_pubkey, usdc_mint)
            escrow_ata = get_associated_token_address(dest_pubkey, usdc_mint)

            escrow_info = solana_client.get_account_info(escrow_ata)
            if not escrow_info.value:
                instructions.append(create_associated_token_account(
                    payer=seller_pubkey, owner=dest_pubkey, mint=usdc_mint
                ))

            amount_units = int(total_required * 1_000_000)
            instructions.append(transfer_checked(
                TransferCheckedParams(
                    program_id=TOKEN_PROGRAM_ID, source=seller_ata, mint=usdc_mint,
                    dest=escrow_ata, owner=seller_pubkey, amount=amount_units, decimals=6
                )
            ))

            gas_lamports = int(gas_reserve_sol * 1_000_000_000)
            if gas_lamports > 0:
                instructions.append(transfer(TransferParams(
                    from_pubkey=seller_pubkey, to_pubkey=dest_pubkey, lamports=gas_lamports
                )))
        else:
            total_lamports = int((total_required + gas_reserve_sol) * 1_000_000_000)
            instructions.append(transfer(TransferParams(
                from_pubkey=seller_pubkey, to_pubkey=dest_pubkey, lamports=total_lamports
            )))

        recent_blockhash = solana_client.get_latest_blockhash().value.blockhash
        msg = MessageV0.try_compile(seller_pubkey, instructions, [], recent_blockhash)
        txn = VersionedTransaction(msg, [seller_keypair])

        res = solana_client.send_transaction(txn)
        tx_hash = str(res.value) if hasattr(res, 'value') else str(res)
        log_blockchain_action('SOL', 'Auto-Fund (Seller)', tx_hash, order_id)

        return tx_hash

    except Exception as e:
        import traceback
        traceback.print_exc()
        log_error(order.get('sellerUid'), f"SOL_FUND_CRITICAL_{order_id}", str(e))
        return f"ERROR: {str(e)}"


def release_escrow_sol(order_id, order, is_refund=False):
    """Повний аналог handleSolRelease з React. Розділяє Fee і Trade, працює з Idempotent-логікою"""
    try:
        escrow_data = order.get('escrowWallet')
        if not escrow_data: return "ERROR: No escrow wallet data"

        priv_key_str = escrow_data.get('secretKey') or escrow_data.get('private_key')
        if not priv_key_str: return "ERROR: No secret key found"

        keypair = Keypair.from_bytes(base58.b58decode(priv_key_str))

        if is_refund:
            target_addr_str = order.get('sellerWalletAddress') or get_buyer_sol_address(order.get('sellerUid'))
        else:
            target_addr_str = order.get('buyerWalletAddress') or get_buyer_sol_address(order.get('buyerUid'))

        if not target_addr_str: return "ERROR: Target SOL address not found"
        target_pubkey = Pubkey.from_string(target_addr_str)

        asset_up = str(order.get('asset', '')).upper()
        is_usdc = "USDC" in asset_up

        instructions = []
        admin_pubkey = Pubkey.from_string(PLATFORM_ADMIN_SOL_ADDRESS)

        if is_usdc:
            usdc_mint = Pubkey.from_string(BLOCKCHAIN_CONFIG['SOL']['usdc_spl'])
            escrow_ata = get_associated_token_address(keypair.pubkey(), usdc_mint)
            target_ata = get_associated_token_address(target_pubkey, usdc_mint)
            admin_ata = get_associated_token_address(admin_pubkey, usdc_mint)

            try:
                escrow_bal_res = solana_client.get_token_account_balance(escrow_ata)
                total_usdc_units = int(escrow_bal_res.value.amount)
            except Exception:
                return "ERROR: Escrow USDC account empty or not found"

            if is_refund:
                fee_units = 0
            else:
                trade_amount = float(order['amountCrypto'])
                fee_units = int(trade_amount * PLATFORM_FEE_PERCENT * 1_000_000)

            trade_units = total_usdc_units - fee_units

            target_info = solana_client.get_account_info(target_ata)
            if not target_info.value:
                instructions.append(create_associated_token_account(keypair.pubkey(), target_pubkey, usdc_mint))

            instructions.append(transfer_checked(
                TransferCheckedParams(
                    program_id=TOKEN_PROGRAM_ID, source=escrow_ata, mint=usdc_mint,
                    dest=target_ata, owner=keypair.pubkey(), amount=trade_units, decimals=6
                )
            ))

            if fee_units > 0:
                admin_info = solana_client.get_account_info(admin_ata)
                if not admin_info.value:
                    instructions.append(create_associated_token_account(keypair.pubkey(), admin_pubkey, usdc_mint))
                instructions.append(transfer_checked(
                    TransferCheckedParams(
                        program_id=TOKEN_PROGRAM_ID, source=escrow_ata, mint=usdc_mint,
                        dest=admin_ata, owner=keypair.pubkey(), amount=fee_units, decimals=6
                    )
                ))

            balance_res = solana_client.get_balance(keypair.pubkey())
            sol_balance = balance_res.value if hasattr(balance_res, 'value') else balance_res
            if sol_balance > 5000:
                instructions.append(transfer(TransferParams(
                    from_pubkey=keypair.pubkey(), to_pubkey=target_pubkey, lamports=int(sol_balance - 5000)
                )))

        else:
            balance_res = solana_client.get_balance(keypair.pubkey())
            current_balance = balance_res.value if hasattr(balance_res, 'value') else balance_res

            fee_lamports = 0 if is_refund else int(float(order['amountCrypto']) * PLATFORM_FEE_PERCENT * 1_000_000_000)
            NETWORK_FEE = 5000
            RENT_MIN = 890880

            if fee_lamports > 0:
                admin_bal_res = solana_client.get_balance(admin_pubkey)
                admin_balance = admin_bal_res.value if hasattr(admin_bal_res, 'value') else admin_bal_res
                if admin_balance + fee_lamports < RENT_MIN:
                    fee_lamports = 0

            trade_lamports = current_balance - fee_lamports - NETWORK_FEE

            if fee_lamports > 0:
                instructions.append(transfer(TransferParams(
                    from_pubkey=keypair.pubkey(), to_pubkey=admin_pubkey, lamports=int(fee_lamports)
                )))

            if trade_lamports > 0:
                instructions.append(transfer(TransferParams(
                    from_pubkey=keypair.pubkey(), to_pubkey=target_pubkey, lamports=int(trade_lamports)
                )))

        recent_blockhash = solana_client.get_latest_blockhash().value.blockhash
        msg = MessageV0.try_compile(keypair.pubkey(), instructions, [], recent_blockhash)
        txn = VersionedTransaction(msg, [keypair])

        res = solana_client.send_transaction(txn)
        return str(res.value) if hasattr(res, 'value') else str(res)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"ERROR: {str(e)}"

import threading
import requests
import time
from datetime import datetime, timezone
from bitcoinutils.setup import setup
from bitcoinutils.keys import PrivateKey as BUPrivateKey, P2wpkhAddress, P2wshAddress, P2pkhAddress, P2shAddress
from bitcoinutils.transactions import Transaction as BUTransaction, TxInput, TxOutput, TxWitnessInput
from bip_utils import Bip39SeedGenerator, Bip84, Bip84Coins, Bip44Changes
from web3 import Web3
from tronpy import Tron
from tronpy.keys import PrivateKey

def get_btc_fee_rate():
    try:
        res = requests.get(f"{BTC_API_URL}/v1/fees/recommended", timeout=5)
        fee = res.json().get('fastestFee', 20)
        return max(fee, 1)  # 1 sat/vB цілком достатньо для тестнету
    except Exception:
        return 30  # Запасний варіант, якщо API лежить


def _get_script_pub_key(addr_str):
    """Визначає правильний скрипт для будь-якої адреси (щоб бот міг переказати куди завгодно)"""
    setup('mainnet')
    addr_lower = addr_str.lower()
    if addr_lower.startswith('tb1q') or addr_lower.startswith('bc1q'):
        if len(addr_lower) == 42:
            return P2wpkhAddress(addr_str).to_script_pub_key()
        else:
            return P2wshAddress(addr_str).to_script_pub_key()
    elif addr_lower.startswith('m') or addr_lower.startswith('n') or addr_lower.startswith('1'):
        return P2pkhAddress(addr_str).to_script_pub_key()
    elif addr_lower.startswith('2') or addr_lower.startswith('3'):
        return P2shAddress(addr_str).to_script_pub_key()
    return P2wpkhAddress(addr_str).to_script_pub_key()


def generate_btc_escrow_for_bot(order_id, amount_crypto):
    """Генерує унікальний BTC гаманець для ескроу"""
    setup('mainnet')

    priv = BUPrivateKey()
    pub = priv.get_public_key()

    escrow_address = pub.get_segwit_address().to_string()
    private_key_wif = priv.to_wif()

    trade_amount = float(amount_crypto)
    platform_fee = round(trade_amount * PLATFORM_FEE_PERCENT, 8)

    try:
        fee_res = requests.get(f"{BTC_API_URL}/v1/fees/recommended", timeout=5).json()
        release_fee_rate = max(fee_res.get('fastestFee', 25), 15)
    except Exception:
        release_fee_rate = 30

    release_gas_btc = round((141 * release_fee_rate) / 100_000_000, 8)
    total_required = round(trade_amount + platform_fee + release_gas_btc, 8)

    escrow_data = {
        'escrowWallet': {
            'address': escrow_address,
            'private_key': private_key_wif,
            'expectedAmount': total_required,
            'network': 'mainnet',
            'witness_type': 'segwit',
            'createdAt': datetime.now(timezone.utc).isoformat()
        },
        'status': 'WAITING_FOR_DEPOSIT'
    }

    db.collection('orders').document(order_id).update(escrow_data)
    print(f"✅ [BTC] Created SegWit Escrow: {escrow_address} | Total: {total_required}")
    return escrow_address, total_required


def watch_btc_deposit(chat_id, tx_hash, order_id):
    """Моніторинг BTC балансу на ескроу-гаманці"""
    lang = user_sessions.get(chat_id, {}).get('lang', 'en') if chat_id else 'en'
    t = lambda key: get_text(lang, key)

    db.collection('orders').document(order_id).update({
        'status': 'WAITING_FOR_DEPOSIT',
        'pendingTxHash': tx_hash
    })

    for attempt in range(500):
        try:
            res = requests.get(f"{BTC_API_URL}/tx/{tx_hash}/status", timeout=10)
            if res.status_code == 200:
                data = res.json()
                if data.get('confirmed'):
                    db.collection('orders').document(order_id).update({
                        'status': 'ESCROW_FUNDED',
                        'pendingTxHash': None
                    })
                    if chat_id:
                        bot.send_message(chat_id, "✅ **BTC Escrow Confirmed!** The buyer can now pay.", parse_mode="Markdown")
                    return

            if attempt % 120 == 0 and attempt > 0 and chat_id:
                bot.send_message(chat_id, "⏳ BTC Transaction is still in mempool. Waiting for the next block...", parse_mode="Markdown")

        except Exception:
            pass

        time.sleep(30)


def resume_blockchain_monitoring():
    """Запускається ОДИН раз при старті бота. Шукає завислі транзакції."""
    print("🔄 Checking for pending blockchain transactions...")
    try:
        pending_orders = db.collection('orders').where(filter=FieldFilter('status', '==', 'WAITING_FOR_DEPOSIT')).get()

        for doc in pending_orders:
            order = doc.to_dict()
            order_id = doc.id
            tx_hash = order.get('pendingTxHash')

            if tx_hash:
                seller_uid = order.get('sellerUid')
                chat_id = next((c for c, s in user_sessions.items() if s.get('uid') == seller_uid), None)

                if chat_id:
                    threading.Thread(target=watch_btc_deposit, args=(chat_id, tx_hash, order_id), daemon=True).start()
                else:
                    threading.Thread(target=watch_btc_deposit, args=(None, tx_hash, order_id), daemon=True).start()
    except Exception as e:
        print(f"❌ Error during resuming blockchain monitoring: {e}")


def execute_swap_transfer(chat_id, swap_data, payin_address, mnemonic, order_id):
    """Виконує прямий переказ крипти на адресу ChangeNOW"""
    try:
        asset = swap_data['from_asset']
        amount = float(swap_data['from_amount'])
        print(f"🔄 Starting SWAP transfer of {amount} {asset} to {payin_address}")

        # --- 1. SOLANA ---
        if "SOL" in asset or "USDC SOL" in asset:
            seller_keypair = get_solana_keypair(mnemonic)
            dest_pubkey = Pubkey.from_string(payin_address)
            lamports = int(amount * 1_000_000_000)

            instruction = transfer(
                TransferParams(from_pubkey=seller_keypair.pubkey(), to_pubkey=dest_pubkey, lamports=lamports))
            recent_blockhash = solana_client.get_latest_blockhash().value.blockhash
            msg = MessageV0.try_compile(seller_keypair.pubkey(), [instruction], [], recent_blockhash)
            txn = VersionedTransaction(msg, [seller_keypair])
            res = solana_client.send_transaction(txn)

            return str(res.value) if hasattr(res, 'value') else str(res)

        # --- 2. TRON (TRX / USDT TRC20) ---
        elif "TRC20" in asset or asset == "TRX":
            client = Tron(HTTPProvider(BLOCKCHAIN_CONFIG['TRON']['rpc']))
            acc = Account.from_mnemonic(mnemonic, account_path="m/44'/195'/0'/0/0")
            priv_key = PrivateKey(bytes.fromhex(acc.key.hex().replace('0x', '').zfill(64)))
            owner_addr = eth_to_tron_address(acc.address)

            if asset == "TRX":
                txn = (
                    client.trx.transfer(owner_addr, payin_address, int(amount * 1_000_000))
                    .memo(f"Swap {order_id}")
                    .build().sign(priv_key)
                )
            else:  # USDT TRC20
                usdt_addr = BLOCKCHAIN_CONFIG['TRON']['usdt_contract']
                usdt_cntr = client.get_contract(usdt_addr)
                txn = (
                    usdt_cntr.functions.transfer(payin_address, int(amount * 1_000_000))
                    .with_owner(owner_addr)
                    .fee_limit(100_000_000)
                    .build().sign(priv_key)
                )
            res = txn.broadcast().wait()
            return res.get('id')

        # --- 3. EVM (ETH / BNB / ERC20) ---
        elif "ETH" in asset or "BNB" in asset or "ERC20" in asset:
            is_bsc = "BNB" in asset
            rpc = BLOCKCHAIN_CONFIG['BSC']['rpc'] if is_bsc else BLOCKCHAIN_CONFIG['ETH']['rpc']
            w3 = Web3(Web3.HTTPProvider(rpc))
            acc = w3.eth.account.from_mnemonic(mnemonic, account_path="m/44'/60'/0'/0/0")

            if asset in ["ETH", "BNB"]:
                tx = {
                    'nonce': w3.eth.get_transaction_count(acc.address, 'pending'),
                    'to': Web3.to_checksum_address(payin_address),
                    'value': w3.to_wei(amount, 'ether'),
                    'gas': 21000,
                    'gasPrice': w3.eth.gas_price,
                    'chainId': 56 if is_bsc else 1
                }
                signed_tx = w3.eth.account.sign_transaction(tx, acc.key)
                tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                return tx_hash.hex()
            else:  # USDT ERC20
                token_addr = Web3.to_checksum_address(BLOCKCHAIN_CONFIG['ETH']['usdt_contract'])
                token_abi = [{"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"}, {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}]
                contract = w3.eth.contract(address=token_addr, abi=token_abi)
                decimals = contract.functions.decimals().call()

                tx = contract.functions.transfer(Web3.to_checksum_address(payin_address), int(amount * (10 ** decimals))).build_transaction({
                    'from': acc.address,
                    'nonce': w3.eth.get_transaction_count(acc.address, 'pending'),
                    'gas': 100000,
                    'gasPrice': w3.eth.gas_price,
                    'chainId': 1
                })
                signed_tx = w3.eth.account.sign_transaction(tx, acc.key)
                tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                return tx_hash.hex()

        # --- 4. BITCOIN ---
        elif "BTC" in asset:
            from bitcoinlib.transactions import Transaction
            t = Transaction(network='bitcoin', witness_type='segwit')
            return t

    except Exception as e:
        print(f"❌ Swap TX Error: {e}")
        import traceback
        traceback.print_exc()
        return f"ERROR: {str(e)}"


def get_buyer_sol_address(uid):
    try:
        user_doc = db.collection('users').document(uid).get()
        if user_doc.exists:
            mnemonic = user_doc.to_dict().get('walletMnemonic')
            if mnemonic:
                return str(get_solana_keypair(mnemonic).pubkey())
    except Exception as e:
        log_error(uid, "DERIVE_SOL_BUYER", e)
    return None


def get_buyer_btc_address(uid):
    try:
        user_doc = db.collection('users').document(uid).get()
        if user_doc.exists:
            mnemonic = user_doc.to_dict().get('walletMnemonic')
            if mnemonic:
                seed = Bip39SeedGenerator(mnemonic).Generate()
                bip84_ctx = Bip84.FromSeed(seed, Bip84Coins.BITCOIN)
                child_key = bip84_ctx.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
                derived_addr = child_key.PublicKey().ToAddress()
                print(f"✅ Derived Buyer Address: {derived_addr}")
                return derived_addr
    except Exception as e:
        print(f"❌ Error getting buyer address: {e}")
    return None


def watch_release_confirmation(tx_hash, order_id):
    for attempt in range(240):
        try:
            res = requests.get(f"{BTC_API_URL}/tx/{tx_hash}/status", timeout=10)
            if res.status_code == 200:
                if res.json().get('confirmed'):
                    db.collection('orders').document(order_id).update({
                        'status': 'COMPLETED',
                        'pendingTxHash': None
                    })
                    print(f"✅ Order {order_id} fully COMPLETED on-chain")
                    return
        except Exception:
            pass
        time.sleep(30)


def auto_fund_btc_escrow(order_id, order, mnemonic_phrase):
    """Автоматичне поповнення ескроу-гаманця для BTC"""
    import traceback
    try:
        setup('mainnet')

        seed = Bip39SeedGenerator(mnemonic_phrase).Generate()
        bip84_ctx = Bip84.FromSeed(seed, Bip84Coins.BITCOIN)
        child_key = bip84_ctx.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)

        my_wif = child_key.PrivateKey().ToWif()
        my_address = child_key.PublicKey().ToAddress()

        priv = BUPrivateKey(wif=my_wif)
        pub = priv.get_public_key()

        escrow_data = order.get('escrowWallet')
        if not escrow_data or not isinstance(escrow_data, dict):
            escrow_addr, total_to_send_btc = generate_btc_escrow_for_bot(order_id, order['amountCrypto'])
        else:
            escrow_addr = escrow_data.get('address')
            total_to_send_btc = escrow_data.get('expectedAmount')

        if not escrow_addr: return "ERROR: Escrow address not found"

        amount_to_escrow_sats = int(float(total_to_send_btc) * 100_000_000)

        res_api = requests.get(f"{BTC_API_URL}/address/{my_address}/utxo", timeout=10)
        if res_api.status_code != 200: return "ERROR: API unreachable"
        utxo_res = res_api.json()
        if not utxo_res: return f"ERROR: Your wallet ({my_address}) is empty."

        inputs = []
        amounts = []
        total_input = 0
        sorted_utxos = sorted(utxo_res, key=lambda x: x['value'], reverse=True)
        fee_rate = get_btc_fee_rate()

        for utxo in sorted_utxos:
            inputs.append(TxInput(utxo['txid'], utxo['vout']))
            amounts.append(utxo['value'])
            total_input += utxo['value']

            estimated_vsize = 11 + (len(inputs) * 68) + (2 * 31)
            if total_input >= (amount_to_escrow_sats + int(estimated_vsize * fee_rate)):
                break

        estimated_vsize = 11 + (len(inputs) * 68) + (2 * 31)
        fee_sats = int(estimated_vsize * fee_rate)

        if total_input < (amount_to_escrow_sats + fee_sats):
            return f"ERROR: Insufficient balance. Need {(amount_to_escrow_sats + fee_sats) / 100_000_000} BTC"

        outputs = [TxOutput(amount_to_escrow_sats, _get_script_pub_key(escrow_addr))]
        change = total_input - amount_to_escrow_sats - fee_sats
        if change > 546:
            outputs.append(TxOutput(int(change), _get_script_pub_key(my_address)))

        tx = BUTransaction(inputs, outputs, has_segwit=True)

        for i in range(len(inputs)):
            script_code = pub.get_address().to_script_pub_key()
            sig = priv.sign_segwit_input(tx, i, script_code, amounts[i])
            tx.witnesses.append(TxWitnessInput([sig, pub.to_hex()]))

        tx_hex = tx.serialize()

        push_res = requests.post(f"{BTC_API_URL}/tx", data=tx_hex, timeout=15)
        if push_res.status_code == 200:
            tx_id = push_res.text
            db.collection('orders').document(order_id).update({
                'pendingTxHash': None, 'status': 'ESCROW_FUNDED', 'fundTxHash': tx_id
            })
            return tx_id
        return f"ERROR: Node rejected: {push_res.text}"
    except Exception as e:
        traceback.print_exc()
        return f"ERROR: {str(e)}"


import hashlib

from bitcoinutils.setup import setup
from bitcoinutils.keys import PrivateKey as BUPrivateKey
from bitcoinutils.transactions import Transaction as BUTransaction, TxInput, TxOutput, TxWitnessInput


def release_escrow_btc(order_id, order):
    """Реліз коштів з BTC ескроу-гаманця покупцю"""
    try:
        setup('mainnet')

        escrow_data = order.get('escrowWallet')
        if not escrow_data: return "ERROR: No escrow data"

        escrow_addr = str(escrow_data['address']).lower().strip()
        priv_key_wif = str(escrow_data['private_key']).strip()

        print("\n" + "=" * 60)
        print(f"🔍 === BTC RELEASE START FOR: {order_id} ===")
        print(f"👉 Target Escrow: {escrow_addr}")

        priv = BUPrivateKey(wif=priv_key_wif)
        pub = priv.get_public_key()
        derived_address = pub.get_segwit_address().to_string()

        print(f"🔑 Key Derived Address: {derived_address}")
        if derived_address.lower() != escrow_addr:
            return f"ERROR: Address mismatch! Key: {derived_address}, Escrow: {escrow_addr}"

        res = requests.get(f"{BTC_API_URL}/address/{escrow_addr}/utxo", timeout=10)
        utxos = res.json()
        if not utxos: return "ERROR: Escrow wallet is empty"

        inputs = []
        amounts = []
        total_balance = 0

        for u in utxos:
            inputs.append(TxInput(u['txid'], u['vout']))
            amounts.append(u['value'])
            total_balance += u['value']

        buyer_addr = order.get('buyerWalletAddress') or get_buyer_btc_address(order.get('buyerUid'))

        fee_rate = get_btc_fee_rate()
        estimated_vsize = 11 + (len(inputs) * 68) + (2 * 31)
        fee_sats = int(estimated_vsize * fee_rate)

        actual_pay_to_buyer = min(int(float(order['amountCrypto']) * 100_000_000), total_balance - fee_sats)

        if actual_pay_to_buyer < 1000:
            return "ERROR: Amount too small (Dust)"

        outputs = [TxOutput(actual_pay_to_buyer, _get_script_pub_key(buyer_addr))]

        remainder = total_balance - actual_pay_to_buyer - fee_sats
        if remainder > 2000:
            outputs.append(TxOutput(int(remainder), _get_script_pub_key(PLATFORM_ADMIN_BTC_ADDRESS)))

        tx = BUTransaction(inputs, outputs, has_segwit=True)

        for i in range(len(inputs)):
            script_code = pub.get_address().to_script_pub_key()
            sig = priv.sign_segwit_input(tx, i, script_code, amounts[i])
            tx.witnesses.append(TxWitnessInput([sig, pub.to_hex()]))

        tx_hex = tx.serialize()
        print(f"📜 Generated HEX len: {len(tx_hex)}")

        push_res = requests.post(f"{BTC_API_URL}/tx", data=tx_hex, timeout=15)
        if push_res.status_code == 200:
            tx_id = push_res.text
            db.collection('orders').document(order_id).update({
                'status': 'COMPLETED', 'pendingTxHash': None, 'releaseTxHash': tx_id
            })
            return tx_id

        return f"ERROR: Node rejected HEX: {push_res.text}"

    except Exception as e:
        traceback.print_exc()
        return f"CRITICAL_ERROR: {str(e)}"


def sign_and_broadcast_trade(order_id, order, mnemonic):
    """Створення смарт-контрактного ескроу у мережі TRON (TRX/USDT)"""
    try:
        print("\n🏗️ --- TRON ESCROW FUND START ---")
        client = Tron(HTTPProvider(BLOCKCHAIN_CONFIG['TRON']['rpc']))

        acc = Account.from_mnemonic(mnemonic, account_path="m/44'/195'/0'/0/0")
        priv_key = PrivateKey(bytes.fromhex(acc.key.hex().replace('0x', '').zfill(64)))
        owner_addr = clean_addr(eth_to_tron_address(acc.address))

        timestamp = int(time.time())
        unique_trade_id = f"{order.get('adId')}_{timestamp}"
        trade_id_hash = hashlib.sha256(unique_trade_id.encode()).digest()

        print(f"🆔 Order: {order_id} | Asset: {order.get('asset')}")
        print(f"🧬 Generated unique ID: {unique_trade_id}")

        escrow_addr = clean_addr(BLOCKCHAIN_CONFIG['TRON']['escrow'])
        asset = str(order.get('asset', 'USDT')).upper()
        buyer_addr = clean_addr(eth_to_tron_address(order.get('buyer_address'))) or owner_addr
        escrow_cntr = client.get_contract(escrow_addr)

        if asset == 'TRX':
            amount_sun = int(float(order.get('amountCrypto', 0)) * 1_000_000)
            txn = (
                escrow_cntr.functions.createTradeTRX.with_transfer(amount_sun)(trade_id_hash, buyer_addr)
                .with_owner(owner_addr).fee_limit(150_000_000).build().sign(priv_key)
            )
        else:
            usdt_addr = clean_addr(BLOCKCHAIN_CONFIG['TRON']['usdt_contract'])
            amount_token = int(float(order.get('amountCrypto', 0)) * 1_000_000)
            usdt_cntr = client.get_contract(usdt_addr)

            # Безпечний Approve на максимальне значення
            usdt_cntr.functions.approve(escrow_addr, 2 ** 256 - 1).with_owner(owner_addr).fee_limit(
                100_000_000).build().sign(priv_key).broadcast().wait()

            txn = (
                escrow_cntr.functions.createTradeToken(trade_id_hash, buyer_addr, usdt_addr, amount_token)
                .with_owner(owner_addr).fee_limit(600_000_000).build().sign(priv_key)
            )

        res = txn.broadcast().wait()
        receipt_status = res.get('receipt', {}).get('result', 'UNKNOWN')
        print(f"📊 Статус Fund: {receipt_status} | Hash: {res.get('id')}")

        if receipt_status == 'SUCCESS':
            db.collection('orders').document(order_id).update({
                'blockchainTradeId': unique_trade_id
            })
            return res.get('id')
        return None

    except Exception as e:
        print(f"❌ Помилка Fund: {e}")
        traceback.print_exc()
        return None


def release_escrow_trade(order, mnemonic):
    """Викликає функцію релізу на смарт-контракті TRON за унікальним хешем угоди"""
    try:
        print("\n🔓 --- TRON ESCROW RELEASE START ---")
        client = Tron(HTTPProvider(BLOCKCHAIN_CONFIG['TRON']['rpc']))

        acc = Account.from_mnemonic(mnemonic, account_path="m/44'/195'/0'/0/0")
        priv_key = PrivateKey(bytes.fromhex(acc.key.hex().replace('0x', '').zfill(64)))
        owner_addr = clean_addr(eth_to_tron_address(acc.address))

        unique_trade_id = order.get('blockchainTradeId')
        if not unique_trade_id:
            print("❌ Помилка: В базі немає blockchainTradeId. Треба спочатку зафондувати!")
            return None

        trade_id_hash = hashlib.sha256(unique_trade_id.encode()).digest()
        print(f"🧬 Реліз для ID з бази: {unique_trade_id}")

        escrow_addr = clean_addr(BLOCKCHAIN_CONFIG['TRON']['escrow'])
        escrow_cntr = client.get_contract(escrow_addr)
        escrow_cntr.abi = [{"name": "releaseFunds", "inputs": [{"name": "_tradeId", "type": "bytes32"}],
                            "stateMutability": "nonpayable", "type": "function"}]

        txn = (
            escrow_cntr.functions.releaseFunds(trade_id_hash)
            .with_owner(owner_addr).fee_limit(200_000_000).build().sign(priv_key)
        )

        res = txn.broadcast().wait()
        receipt_status = res.get('receipt', {}).get('result', 'UNKNOWN')
        print(f"📊 Статус Release: {receipt_status} | Hash: {res.get('id')}")
        return res
    except Exception as e:
        print(f"❌ Помилка Release: {e}")
        traceback.print_exc()
        return None


def get_trade_id_bytes(order_id):
    return Web3.keccak(text=str(order_id))


def sign_and_broadcast_eth(order_id, order, mnemonic):
    """Створення ескроу у смарт-контракті EVM (ETH/USDT) з динамічним chainId"""
    try:
        print(f"\n🚀 STARTING EVM DEPOSIT: {order_id}")
        net_cfg = BLOCKCHAIN_CONFIG.get('ETH', {})
        w3 = Web3(Web3.HTTPProvider(net_cfg['rpc']))
        acc = w3.eth.account.from_mnemonic(mnemonic, account_path="m/44'/60'/0'/0/0")

        asset = order.get('asset', 'ETH').upper()
        amount_raw = float(order.get('amountCrypto', 0))
        trade_id_bytes = os.urandom(32)
        chain_id = int(net_cfg.get('chainId', 1))

        buyer_uid = order.get('buyerUid')
        buyer_doc = db.collection('users').document(buyer_uid).get().to_dict()
        buyer_addr = w3.eth.account.from_mnemonic(buyer_doc['walletMnemonic'], account_path="m/44'/60'/0'/0/0").address

        escrow_addr = Web3.to_checksum_address(net_cfg['escrow'])
        contract = w3.eth.contract(address=escrow_addr, abi=EVM_ESCROW_ABI)

        latest_block = w3.eth.get_block('latest')
        base_fee = latest_block['baseFeePerGas']
        priority_fee = w3.to_wei(2, 'gwei')
        max_fee = int(base_fee * 2) + priority_fee

        current_nonce = w3.eth.get_transaction_count(acc.address, 'pending')

        if asset == 'ETH':
            value_wei = w3.to_wei(amount_raw, 'ether')
            balance = w3.eth.get_balance(acc.address)
            if balance < (value_wei + w3.to_wei(0.005, 'ether')):
                return "INSUFFICIENT_ETH_FOR_GAS"

            txn = contract.functions.createTradeNative(trade_id_bytes, buyer_addr).build_transaction({
                'from': acc.address,
                'value': value_wei,
                'gas': 250000,
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': priority_fee,
                'nonce': current_nonce,
                'chainId': chain_id
            })
        else:
            token_address = Web3.to_checksum_address(net_cfg['usdt_contract'])
            token_abi = [
                {"constant": False,
                 "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
                 "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
                {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}],
                 "type": "function"},
                {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "allowance",
                 "outputs": [{"name": "", "type": "uint256"}], "type": "function"}
            ]
            token_contract = w3.eth.contract(address=token_address, abi=token_abi)

            decimals = token_contract.functions.decimals().call()
            amount_units = int(amount_raw * (10 ** decimals))

            allowed = token_contract.functions.allowance(acc.address, escrow_addr).call()
            if allowed < amount_units:
                approve_txn = token_contract.functions.approve(escrow_addr, amount_units).build_transaction({
                    'from': acc.address,
                    'maxFeePerGas': max_fee,
                    'maxPriorityFeePerGas': priority_fee,
                    'nonce': current_nonce,
                    'chainId': chain_id
                })
                signed_app = w3.eth.account.sign_transaction(approve_txn, acc.key)
                w3.eth.send_raw_transaction(signed_app.raw_transaction)
                print("⏳ Waiting for Approve confirmation...")
                time.sleep(15)
                current_nonce += 1  # Збільшуємо nonce для наступної транзакції

            txn = contract.functions.createTrade(trade_id_bytes, buyer_addr, token_address,
                                                 amount_units).build_transaction({
                'from': acc.address,
                'value': 0,
                'gas': 400000,
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': priority_fee,
                'nonce': current_nonce,
                'chainId': chain_id
            })

        signed_txn = w3.eth.account.sign_transaction(txn, acc.key)
        tx_hash = w3.eth.send_raw_transaction(signed_txn.raw_transaction)

        db.collection('orders').document(order_id).update({
            'blockchainTradeId': trade_id_bytes.hex(),
            'txHash': tx_hash.hex()
        })
        print(f"✅ Success! Hash: {tx_hash.hex()}")
        return tx_hash.hex()

    except Exception as e:
        print(f"🛑 ETH Error: {str(e)}")
        traceback.print_exc()
        return f"ERROR: {str(e)}"


def release_escrow_eth(order_id, order, mnemonic):
    """Виклик релізу коштів зі смарт-контракту EVM"""
    try:
        print(f"🔓 --- EVM RELEASE START: {order_id} ---")
        net_cfg = BLOCKCHAIN_CONFIG.get('ETH', {})
        w3 = Web3(Web3.HTTPProvider(net_cfg['rpc']))
        acc = w3.eth.account.from_mnemonic(mnemonic, account_path="m/44'/60'/0'/0/0")

        hex_id = order.get('blockchainTradeId')
        if not hex_id:
            print("❌ blockchainTradeId не знайдено!")
            return None

        trade_id_bytes = bytes.fromhex(hex_id)
        escrow_addr = Web3.to_checksum_address(net_cfg['escrow'])
        contract = w3.eth.contract(address=escrow_addr, abi=EVM_ESCROW_ABI)
        chain_id = int(net_cfg.get('chainId', 1))

        latest_block = w3.eth.get_block('latest')
        base_fee = latest_block['baseFeePerGas']
        priority_fee = w3.to_wei(2, 'gwei')
        max_fee = int(base_fee * 2) + priority_fee

        txn = contract.functions.releaseFunds(trade_id_bytes).build_transaction({
            'from': acc.address,
            'chainId': chain_id,
            'gas': 200000,
            'maxFeePerGas': max_fee,
            'maxPriorityFeePerGas': priority_fee,
            'nonce': w3.eth.get_transaction_count(acc.address, 'pending'),
        })

        signed = w3.eth.account.sign_transaction(txn, acc.key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

        print(f"✅ Release Hash: {tx_hash.hex()}")
        return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    except Exception as e:
        print(f"❌ Помилка релізу: {str(e)}")
        return None



from tronpy.keys import PrivateKey, Account
from solana.rpc.api import Client as SolanaClient
from confirmed import Pubkey, Keypair, TokenAccountOpts  # Передбачаються ваші глобальні імпорти для SOL
from bip_utils import Bip39SeedGenerator, Bip84, Bip84Coins, Bip44Changes, Bip44, Bip44Coins


def sign_and_broadcast_bsc(order_id, order, mnemonic):
    """Створення ескроу у смарт-контракті Binance Smart Chain (BNB/USDT)"""
    try:
        print(f"\n🟡 STARTING BSC DEPOSIT: {order_id}")

        w3 = Web3(Web3.HTTPProvider(BLOCKCHAIN_CONFIG['BSC']['rpc']))
        acc = w3.eth.account.from_mnemonic(mnemonic, account_path="m/44'/60'/0'/0/0")

        asset = order.get('asset', 'BNB').upper()
        amount_raw = float(order.get('amountCrypto', 0))
        trade_id_bytes = os.urandom(32)

        buyer_uid = order.get('buyerUid')
        buyer_doc = db.collection('users').document(buyer_uid).get().to_dict()
        buyer_addr = w3.eth.account.from_mnemonic(buyer_doc['walletMnemonic'], account_path="m/44'/60'/0'/0/0").address

        escrow_addr = Web3.to_checksum_address(BLOCKCHAIN_CONFIG['BSC']['escrow'])
        contract = w3.eth.contract(address=escrow_addr, abi=EVM_ESCROW_ABI)

        gas_price = w3.eth.gas_price
        current_nonce = w3.eth.get_transaction_count(acc.address, 'pending')

        if asset == 'BNB':
            value_wei = w3.to_wei(amount_raw, 'ether')
            balance = w3.eth.get_balance(acc.address)
            if balance < (value_wei + w3.to_wei(0.005, 'ether')):
                return "INSUFFICIENT_BNB_FOR_GAS"

            txn = contract.functions.createTradeNative(trade_id_bytes, buyer_addr).build_transaction({
                'from': acc.address,
                'value': value_wei,
                'gas': 250000,
                'gasPrice': gas_price,
                'nonce': current_nonce,
                'chainId': 56
            })
        else:
            token_address = Web3.to_checksum_address(BLOCKCHAIN_CONFIG['BSC']['usdt_contract'])
            token_abi = [{"constant": False,
                          "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
                          "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
                         {"constant": True, "inputs": [], "name": "decimals",
                          "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
                         {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "allowance",
                          "outputs": [{"name": "", "type": "uint256"}], "type": "function"}]
            token_contract = w3.eth.contract(address=token_address, abi=token_abi)

            decimals = token_contract.functions.decimals().call()
            amount_units = int(amount_raw * (10 ** decimals))

            allowed = token_contract.functions.allowance(acc.address, escrow_addr).call()
            if allowed < amount_units:
                approve_txn = token_contract.functions.approve(escrow_addr, amount_units).build_transaction({
                    'from': acc.address,
                    'gasPrice': gas_price,
                    'nonce': current_nonce,
                    'chainId': 56
                })
                signed_app = w3.eth.account.sign_transaction(approve_txn, acc.key)
                w3.eth.send_raw_transaction(signed_app.raw_transaction)
                print("⏳ Waiting for Approve confirmation...")
                time.sleep(15)
                current_nonce += 1

            txn = contract.functions.createTrade(trade_id_bytes, buyer_addr, token_address,
                                                 amount_units).build_transaction({
                'from': acc.address,
                'value': 0,
                'gas': 400000,
                'gasPrice': gas_price,
                'nonce': current_nonce,
                'chainId': 56
            })

        signed_txn = w3.eth.account.sign_transaction(txn, acc.key)
        tx_hash = w3.eth.send_raw_transaction(signed_txn.raw_transaction)

        db.collection('orders').document(order_id).update({
            'blockchainTradeId': trade_id_bytes.hex(),
            'txHash': tx_hash.hex()
        })
        print(f"✅ Success! Hash: {tx_hash.hex()}")
        return tx_hash.hex()

    except Exception as e:
        print(f"🛑 BSC Error: {str(e)}")
        traceback.print_exc()
        return f"ERROR: {str(e)}"


def release_escrow_bsc(order_id, order, mnemonic):
    """Реліз коштів зі смарт-контракту в мережі BSC"""
    try:
        print(f"🔓 --- BSC RELEASE START: {order_id} ---")
        w3 = Web3(Web3.HTTPProvider(BLOCKCHAIN_CONFIG['BSC']['rpc']))
        acc = w3.eth.account.from_mnemonic(mnemonic, account_path="m/44'/60'/0'/0/0")

        hex_id = order.get('blockchainTradeId')
        if not hex_id:
            return None

        trade_id_bytes = bytes.fromhex(hex_id)
        escrow_addr = Web3.to_checksum_address(BLOCKCHAIN_CONFIG['BSC']['escrow'])
        contract = w3.eth.contract(address=escrow_addr, abi=EVM_ESCROW_ABI)

        gas_price = w3.eth.gas_price

        txn = contract.functions.releaseFunds(trade_id_bytes).build_transaction({
            'from': acc.address,
            'chainId': 56,
            'gas': 200000,
            'gasPrice': gas_price,
            'nonce': w3.eth.get_transaction_count(acc.address, 'pending'),
        })

        signed = w3.eth.account.sign_transaction(txn, acc.key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

        print(f"✅ Release Hash: {tx_hash.hex()}")
        return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    except Exception as e:
        print(f"❌ Помилка релізу BSC: {str(e)}")
        return None


def get_order_status_label(status, lang='en'):
    config = {
        'CREATED': {'en': 'Initiated 🏁', 'ua': 'Розпочато 🏁', 'ru': 'Начато 🏁'},
        'ACCEPTED': {'en': 'Waiting for escrow ⏳', 'ua': 'Очікування депо ⏳', 'ru': 'Ожидание депо ⏳'},
        'WAITING_FOR_DEPOSIT': {'en': 'Confirming... ⛓️', 'ua': 'Підтвердження... ⛓️', 'ru': 'Подтверждение... ⛓️'},
        'ESCROW_FUNDED': {'en': 'Escrow funded ✅', 'ua': 'Депозит внесено ✅', 'ru': 'Депозит внесен ✅'},
        'PAID': {'en': 'Payment sent 💸', 'ua': 'Оплачено 💸', 'ru': 'Оплачено 💸'},
        'COMPLETED': {'en': 'Completed 🎉', 'ua': 'Завершено 🎉', 'ru': 'Завершено 🎉'},
        'CANCELLED': {'en': 'Cancelled ❌', 'ua': 'Скасовано ❌', 'ru': 'Отменено ❌'},
        'RESOLVED': {'en': 'Resolved by Admin 🛡️', 'ua': 'Вирішено арбітром 🛡️', 'ru': 'Решено арбитром 🛡️'}
    }
    res = config.get(status, {'en': status})
    return res.get(lang, res.get('en', 'Processing...'))


def get_active_trades_markup(uid, lang):
    """Генерує інлайн-кнопки активних угод користувача із сумами у фіаті"""
    try:
        orders_ref = db.collection('orders')
        s_query = orders_ref.where(filter=FieldFilter('sellerUid', '==', uid)).get()
        b_query = orders_ref.where(filter=FieldFilter('buyerUid', '==', uid)).get()

        all_trades_dict = {doc.id: doc.to_dict() for doc in (list(s_query) + list(b_query))}

        active_list = []
        for o_id, d in all_trades_dict.items():
            if d.get('status') not in ['COMPLETED', 'CANCELLED', 'RESOLVED']:
                active_list.append((o_id, d))

        if not active_list:
            return None, 0

        markup = types.InlineKeyboardMarkup(row_width=1)
        for o_id, d in active_list:
            role = "💰 Sell" if d.get('sellerUid') == uid else "🛒 Buy"
            status_label = get_order_status_label(d.get('status'), lang)

            amount_fiat = d.get('amountFiat', 0)
            fiat_currency = d.get('fiatCurrency', 'USD')

            btn_text = f"{role} {amount_fiat} {fiat_currency} ➔ {status_label}"
            markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"view_trade_{o_id}"))

        return markup, len(active_list)
    except Exception as e:
        print(f"❌ Dashboard error: {e}")
        return None, 0


def show_bottom_menu(chat_id, lang):
    """Показує головне меню бота"""
    t = lambda key: get_text(lang, key)
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)

    # Ряд 1: Головні фінансові сервіси
    markup.row(
        types.KeyboardButton(t('btn_app')),
        types.KeyboardButton(t('btn_wallet')),
        types.KeyboardButton(t('btn_swap'))
    )

    # Ряд 2: P2P Торгівля та Профіль
    markup.row(
        types.KeyboardButton(t('btn_buy')),
        types.KeyboardButton(t('btn_sell')),
        types.KeyboardButton(t('btn_profile'))
    )

    # Ряд 3: Інформаційні кнопки
    markup.row(
        types.KeyboardButton(t('btn_news')),
        types.KeyboardButton(t('btn_community')),
        types.KeyboardButton(t('btn_alerts'))
    )

    # Ряд 4: Створення оголошення
    markup.add(types.KeyboardButton(t('btn_create_offer')))

    # Ряд 5: Технічне та Соціальне
    markup.row(
        types.KeyboardButton(t('btn_support')),
        types.KeyboardButton(t('btn_settings')),
        types.KeyboardButton("🌐 Social Media")
    )

    bot.send_message(chat_id, t('menu_msg'), reply_markup=markup)


def get_solana_keypair(mnemonic_phrase):
    """Генерує Keypair для Solana, що відповідає m/44'/501'/0'"""
    seed = Bip39SeedGenerator(mnemonic_phrase).Generate()
    bip44_mst = Bip44.FromSeed(seed, Bip44Coins.SOLANA)
    bip44_acc = bip44_mst.Purpose().Coin().Account(0)
    priv_bytes = bip44_acc.PrivateKey().Raw().ToBytes()
    return Keypair.from_seed(priv_bytes)


def get_wallet_balances(mnemonic_phrase, uid):
    """Збирає баланси по всіх підтримуваних блокчейнах, включаючи віртуальні нарахування"""
    print(f"🚀 [DEBUG] Start balance check for {uid}...")
    balances = {
        'eth_addr': 'Error', 'eth_bal': 0.0, 'usdt_erc': 0.0,
        'tron_addr': 'Error', 'trx_bal': 0.0, 'usdt_trc': 0.0,
        'btc_bal': 0.0,
        'sol_addr': 'Error', 'sol_bal': 0.0, 'sol_usdc': 0.0,
        'bnb_addr': 'Error', 'bnb_bal': 0.0,
        'xmr_addr': 'Error', 'xmr_bal': '0.0000',
        'virtual_others': []
    }

    # --- 1. EVM (ETH & BSC) ---
    try:
        w3_eth = Web3(Web3.HTTPProvider(BLOCKCHAIN_CONFIG['ETH']['rpc']))
        eth_acc = w3_eth.eth.account.from_mnemonic(mnemonic_phrase, account_path="m/44'/60'/0'/0/0")
        balances['eth_addr'] = eth_acc.address
        balances['eth_bal'] = round(w3_eth.from_wei(w3_eth.eth.get_balance(balances['eth_addr']), 'ether'), 6)

        usdt_erc20_addr = Web3.to_checksum_address(BLOCKCHAIN_CONFIG['ETH']['usdt_contract'])
        usdt_abi = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf",
                     "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}]
        usdt_erc_contract = w3_eth.eth.contract(address=usdt_erc20_addr, abi=usdt_abi)
        balances['usdt_erc'] = round(usdt_erc_contract.functions.balanceOf(balances['eth_addr']).call() / 10 ** 6, 2)

        w3_bsc = Web3(Web3.HTTPProvider(BLOCKCHAIN_CONFIG['BSC']['rpc']))
        balances['bnb_addr'] = eth_acc.address
        balances['bnb_bal'] = round(w3_bsc.from_wei(w3_bsc.eth.get_balance(balances['bnb_addr']), 'ether'), 4)
    except Exception as e:
        print(f"❌ EVM Balance Error: {e}")

    # --- 2. TRON ---
    try:
        tron_client = Tron(HTTPProvider(BLOCKCHAIN_CONFIG['TRON']['rpc']))
        tron_acc = Account.from_mnemonic(mnemonic_phrase, account_path="m/44'/195'/0'/0/0")
        balances['tron_addr'] = eth_to_tron_address(tron_acc.address)
        balances['trx_bal'] = round(tron_client.get_account_balance(balances['tron_addr']), 2)

        usdt_trc_addr = BLOCKCHAIN_CONFIG['TRON']['usdt_contract']
        usdt_trc_cntr = tron_client.get_contract(usdt_trc_addr)
        balances['usdt_trc'] = round(usdt_trc_cntr.functions.balanceOf(balances['tron_addr']) / 10 ** 6, 2)
    except Exception as e:
        print(f"❌ TRON Balance Error: {e}")

    # --- 3. BITCOIN ---
    try:
        seed = Bip39SeedGenerator(mnemonic_phrase).Generate()
        bip84_ctx = Bip84.FromSeed(seed, Bip84Coins.BITCOIN)
        child_key = bip84_ctx.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0)
        my_btc_address = child_key.PublicKey().ToAddress()

        res = requests.get(f"{BTC_API_URL}/address/{my_btc_address}", timeout=10)
        if res.status_code == 200:
            data = res.json()
            stats, mempool = data.get('chain_stats', {}), data.get('mempool_stats', {})
            total = (stats.get('funded_txo_sum', 0) + mempool.get('funded_txo_sum', 0)) - (
                        stats.get('spent_txo_sum', 0) + mempool.get('spent_txo_sum', 0))
            balances['btc_bal'] = round(total / 10 ** 8, 8)
    except Exception as e:
        print(f"❌ BTC Balance Error: {e}")

    # --- 4. SOLANA ---
    try:
        sol_keypair = get_solana_keypair(mnemonic_phrase)
        pubkey = sol_keypair.pubkey()
        balances['sol_addr'] = str(pubkey)

        sol_res = solana_client.get_balance(pubkey)
        balances['sol_bal'] = round(sol_res.value / 1_000_000_000, 4) if hasattr(sol_res, 'value') else 0

        usdc_mint = Pubkey.from_string(BLOCKCHAIN_CONFIG['SOL']['usdc_spl'])
        token_accs = solana_client.get_token_accounts_by_owner(pubkey, TokenAccountOpts(mint=usdc_mint))
        if token_accs.value:
            token_addr = token_accs.value[0].pubkey
            token_bal_res = solana_client.get_token_account_balance(token_addr)
            balances['sol_usdc'] = round(float(token_bal_res.value.ui_amount), 2)
    except Exception as e:
        print(f"❌ SOL Balance Error: {e}")

    # --- 5. MONERO ---
    try:
        xmr_keys = get_xmr_wallet_data(mnemonic_phrase, is_testnet=False)
        if xmr_keys:
            balances['xmr_addr'] = xmr_keys['address']
            bal_res = get_xmr_mainnet_balance(xmr_keys['address'], xmr_keys['view_key'])
            if isinstance(bal_res, (int, float)):
                balances['xmr_bal'] = f"{bal_res:.4f}"
            else:
                balances['xmr_bal'] = str(bal_res)
    except Exception as e:
        print(f"❌ XMR Balance Error: {e}")
        balances['xmr_bal'] = "Sync Error"

    # --- 6. ВІРТУАЛЬНЕ ПЛЮСУВАННЯ З DB ---
    try:
        user_doc = db.collection('users').document(uid).get()
        if user_doc.exists:
            v_data = user_doc.to_dict().get('balances', {})
            for key, val in v_data.items():
                p = key.lower().split('_')
                if len(p) < 3: continue
                asset, net = p[1], p[2]

                if net == 'tron':
                    if asset in ['tether', 'usdt']:
                        balances['usdt_trc'] += float(val)
                    elif asset == 'tron':
                        balances['trx_bal'] += float(val)
                elif net in ['ethereum', 'erc20']:
                    if asset in ['tether', 'usdt']:
                        balances['usdt_erc'] += float(val)
                    elif asset == 'ethereum':
                        balances['eth_bal'] += float(val)
                elif net == 'bsc' or net == 'binance':
                    if asset == 'bnb': balances['bnb_bal'] += float(val)
                else:
                    balances['virtual_others'].append({'name': asset.upper(), 'net': net.upper(), 'val': val})
    except Exception as e:
        print(f"❌ Virtual Merge Error: {e}")

    return balances

import os
import requests
import traceback
from telebot import types
from web3 import Web3
from bitcoinlib.mnemonic import Mnemonic
from bitcoinlib.keys import HDKey
from bip_utils import Bip39SeedGenerator, Monero, MoneroCoins

def get_btc_key_from_mnemonic(mnemonic_phrase):
    """Генерує Master Key для BTC з мнемоніки за стандартом BIP84 (Native SegWit)"""
    seed = Mnemonic('english').to_seed(mnemonic_phrase)
    root_key = HDKey.from_seed(seed, network='bitcoin', witness_type='segwit')
    child_key = root_key.key_for_path("m/84'/0'/0'/0/0")
    return child_key


def get_xmr_wallet_data(mnemonic_phrase, is_testnet=True):
    """Генерує Monero ключі для stagenet чи mainnet відповідно до версії bip_utils"""
    try:
        seed_bytes = Bip39SeedGenerator(mnemonic_phrase).Generate()
        coin_type = MoneroCoins.MONERO_STAGENET if is_testnet else MoneroCoins.MONERO_MAINNET
        monero = Monero.FromSeed(seed_bytes, coin_type)

        return {
            'address': monero.PrimaryAddress(),
            'view_key': monero.PrivateViewKey().Raw().ToHex(),
            'spend_key': monero.PrivateSpendKey().Raw().ToHex(),
            'network': "STAGENET" if is_testnet else "MAINNET"
        }
    except Exception as e:
        print(f"❌ [XMR ERROR]: {str(e)}")
        traceback.print_exc()
        return None


def check_xmr_mainnet_tx(tx_hash, address, view_key):
    """Перевіряє конкретний хеш транзакції через API експлорера MAINNET"""
    url = "https://xmrchain.net/api/outputsblocks"
    params = {
        "tx_hash": tx_hash,
        "address": address,
        "viewkey": view_key,
        "mclist": "1"
    }
    try:
        res = requests.get(url, params=params, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get('status') == 'success':
                total = 0
                for out in data.get('outputs', []):
                    if out.get('match') is True:
                        total += int(out.get('amount', 0))

                final_amount = round(total / 1e12, 4)
                print(f"✅ [XMR MAINNET] Знайдено прихід: {final_amount} XMR")
                return final_amount
        return 0.0
    except Exception as e:
        print(f"❌ [XMR MAINNET] Помилка: {e}")
        return 0.0


def get_xmr_mainnet_balance(address, view_key):
    """Отримує баланс через API xmrchain.net (MAINNET)"""
    url = f"https://xmrchain.net/api/address/{address}/{view_key}/1"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get('status') == 'success':
                stats = data.get('data', {})
                received = stats.get('total_received', 0)
                sent = stats.get('total_sent', 0)
                final_bal = (received - sent) / 1e12

                if final_bal == 0 and received == 0:
                    return "0.0000"

                return round(final_bal, 4)
        return "Node offline ❌"
    except Exception as e:
        print(f"DEBUG XMR: {e}")
        return "Connection Error"


def get_xmr_balance_rpc(address, view_key):
    """Запит балансу через локальний monero-wallet-rpc за View Only логікою"""
    print(f"📡 [XMR DEBUG] Requesting balance for: {address[:10]}...")
    payload = {
        "jsonrpc": "2.0",
        "id": "0",
        "method": "get_balance",
        "params": {"account_index": 0}
    }
    try:
        # Для реальної роботи розкоментувати та вказати XMR_RPC_URL
        # response = requests.post(XMR_RPC_URL, json=payload).json()
        return "Syncing (Need Wallet RPC)"
    except Exception as e:
        return f"Error: {e}"


def notify_seller(order_id, order, force_send=False):
    """Сповіщення продавця про зміну статусу ескроу угоди та нові повідомлення"""
    try:
        seller_uid = order.get('sellerUid')
        target_chat_id = next(
            (c for c, s in user_sessions.items() if s.get('uid') == seller_uid and s.get('authorized')), None)

        if not target_chat_id: return
        s = user_sessions[target_chat_id]
        lang = s.get('lang', 'en')
        status = order.get('status', 'UNKNOWN')
        is_disputed = order.get('isDisputed', False)

        telegram_enabled = True
        if not force_send:
            user_doc_ref = db.collection('users').document(seller_uid).get()
            if user_doc_ref.exists:
                notifs = user_doc_ref.to_dict().get('notifications', {})
                if notifs.get('telegram') is False:
                    telegram_enabled = False

        is_pending = status == 'WAITING_FOR_DEPOSIT' or order.get('pendingTxHash') is not None

        # --- 1. ОБРОБКА ТА НАДСИЛАННЯ ПОВІДОМЛЕНЬ ЧАТУ ---
        msgs = order.get('messages', [])
        if msgs:
            last_msg = msgs[-1]
            msg_state_key = f"msg_s_{order_id}_{len(msgs) - 1}"

            if last_msg.get('senderUid') != seller_uid and not last_trade_status.get(msg_state_key):
                last_trade_status[msg_state_key] = True

                msg_text_content = last_msg.get('text') or ''
                image_url = last_msg.get('imageUrl')

                if not msg_text_content and image_url:
                    msg_text_content = "[🖼️ Photo attached]"

                if last_msg.get('senderUid') == 'SYSTEM':
                    chat_text = f"🔔 **System Update:**\n\n{msg_text_content}"
                elif last_msg.get('isAdmin'):
                    chat_text = f"🛡️ **Admin Message:**\n\n{msg_text_content}"
                else:
                    partner_name = order.get('buyerName', 'Buyer')
                    chat_text = f"💬 **New message from {partner_name}:**\n\n{msg_text_content}"

                chat_markup = types.InlineKeyboardMarkup()
                chat_markup.add(types.InlineKeyboardButton("🌐 Open Web Chat", url=f"url"))

                if not last_msg.get('isAdmin') and last_msg.get('senderUid') != 'SYSTEM':
                    chat_markup.add(types.InlineKeyboardButton("✍️ Reply", callback_data=f"reply_{order_id}"))

                if telegram_enabled:
                    if image_url:
                        try:
                            bot.send_photo(target_chat_id, photo=image_url, caption=chat_text, reply_markup=chat_markup, parse_mode="Markdown")
                        except Exception:
                            bot.send_message(target_chat_id, f"{chat_text}\n\n🖼️ [View Image]({image_url})", reply_markup=chat_markup, disable_web_page_preview=False)
                    else:
                        bot.send_message(target_chat_id, chat_text, reply_markup=chat_markup)

        # --- 2. ОНОВЛЕННЯ ТА ВІДПРАВКА КАРТКИ УГОДИ ---
        current_hash = order.get('releaseTxHash') or order.get('pendingTxHash') or order.get('fundTxHash') or "no_hash"
        state_key = f"s_st_upd_{order_id}_{status}_{is_disputed}_{current_hash}"

        if last_trade_status.get(state_key) or not telegram_enabled: return
        last_trade_status[state_key] = True

        raw_asset = str(order.get('asset', '')).upper()
        display_asset = "USDT" if ("TRC20" in raw_asset or raw_asset == "USDT") else ("USDT ERC20" if "ERC20" in raw_asset else raw_asset)

        if "BTC" in display_asset:
            display_net = 'Bitcoin Network 🟠'
        elif "USDC" in display_asset or "SOL" in display_asset:
            display_net = 'Solana Mainnet ☀️'
        elif "BNB" in display_asset:
            display_net = 'Binance Smart Chain 🟡'
        elif display_asset == "USDT" or "TRX" in display_asset:
            display_net = 'Tron (TRC20) 🔴'
        else:
            display_net = 'Ethereum (ERC20) 🔹'

        buyer_name = order.get('buyerName', 'Buyer')
        buyer_uid = order.get('buyerUid')

        seller_user_data = db.collection('users').document(seller_uid).get().to_dict() or {}
        is_first_trade = seller_user_data.get('completedTradesCount', 0) == 0

        header = "🚨 **TRADE UNDER DISPUTE** 🚨" if is_disputed else f"💰 **Selling {order.get('amountCrypto')} {display_asset}**"
        if is_first_trade and not is_disputed:
            header += "\n🎁 **[FIRST TRADE PROMO: Platform fee will be refunded]**"

        msg_text = (
            f"{header}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 **Partner (Buyer):** {buyer_name}\n"
            f"💵 Amount: {order.get('amountFiat')} {order.get('fiatCurrency')}\n"
            f"🌐 Network: {display_net}\n"
            f"📊 Status: {get_order_status_label(status, lang)}"
        )

        proofs = ""
        if order.get('fundTxHash'): proofs += f"📥 **Deposit:** `{order.get('fundTxHash')}`\n"
        if order.get('releaseTxHash'): proofs += f"💸 **Release:** `{order.get('releaseTxHash')}`\n"
        if order.get('cancelTxHash'): proofs += f"❌ **Refund:** `{order.get('cancelTxHash')}`\n"

        if proofs:
            msg_text += f"\n\n⛓️ **Blockchain Proofs:**\n{proofs}"

        markup = types.InlineKeyboardMarkup(row_width=1)
        if is_disputed:
            markup.add(types.InlineKeyboardButton("⚖️ View Arbitration Details", url=f"url"))
        elif is_pending:
            markup.add(types.InlineKeyboardButton("⏳ Processing on Blockchain...", callback_data="none"))
        else:
            if status == 'CREATED' and order.get('creatorUid') != seller_uid:
                markup.add(types.InlineKeyboardButton("✅ Accept Trade", callback_data=f"act_ACCEPTED_{order_id}"))
            elif status == 'ACCEPTED':
                markup.add(types.InlineKeyboardButton("🔒 Fund Escrow", callback_data=f"act_ESCROW_FUNDED_{order_id}"))
            elif status == 'PAID':
                markup.add(types.InlineKeyboardButton("💸 Release Crypto", callback_data=f"act_COMPLETED_{order_id}"))

            if status in ['CREATED', 'ACCEPTED']:
                markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data=f"act_CANCELLED_{order_id}"))

            if status not in ['COMPLETED', 'CANCELLED', 'RESOLVED'] and status != 'CREATED':
                markup.add(types.InlineKeyboardButton("🚨 Dispute", callback_data=f"open_dispute_{order_id}"))

        web_chat_url = f"url"
        markup.row(types.InlineKeyboardButton("🌐 Web Chat", url=web_chat_url), types.InlineKeyboardButton("✍️ Reply", callback_data=f"reply_{order_id}"))
        markup.add(types.InlineKeyboardButton(f"👤 View {buyer_name}'s Profile", callback_data=f"view_profile_{buyer_uid}"))

        current_msg_count = len(order.get('messages', []))
        send_or_edit_trade_card(target_chat_id, order_id, 'seller', msg_text, markup, current_msg_count)

    except Exception as e:
        print(f"❌ Error notify_seller: {e}")


import time
import requests
from telebot import types
from web3 import Web3
from tronpy import Tron
from tronpy.providers import HTTPProvider
from solders.signature import Signature # Передбачається наявність solders у вашому окруженні SOL

def notify_buyer(order_id, order, force_send=False):
    """Сповіщення покупця про зміну статусу ескроу угоди та нові повідомлення"""
    try:
        buyer_uid = order.get('buyerUid')
        target_chat_id = next(
            (c for c, s in user_sessions.items() if s.get('uid') == buyer_uid and s.get('authorized')), None)
        if not target_chat_id: return

        s = user_sessions[target_chat_id]
        lang = s.get('lang', 'en')
        status = order.get('status', 'UNKNOWN')
        is_disputed = order.get('isDisputed', False)

        telegram_enabled = True
        if not force_send:
            user_doc_ref = db.collection('users').document(buyer_uid).get()
            if user_doc_ref.exists:
                notifs = user_doc_ref.to_dict().get('notifications', {})
                if notifs.get('telegram') is False:
                    telegram_enabled = False

        is_pending = status == 'WAITING_FOR_DEPOSIT' or order.get('pendingTxHash') is not None

        # --- 1. ОБРОБКА ТА НАДСИЛАННЯ ПОВІДОМЛЕНЬ ЧАТУ ---
        msgs = order.get('messages', [])
        if msgs:
            last_msg = msgs[-1]
            msg_state_key = f"msg_b_{order_id}_{len(msgs) - 1}"

            if last_msg.get('senderUid') != buyer_uid and not last_trade_status.get(msg_state_key):
                last_trade_status[msg_state_key] = True

                msg_text_content = last_msg.get('text') or ''
                image_url = last_msg.get('imageUrl')

                if not msg_text_content and image_url:
                    msg_text_content = "[🖼️ Photo attached]"

                if last_msg.get('senderUid') == 'SYSTEM':
                    chat_text = f"🔔 **System Update:**\n\n{msg_text_content}"
                elif last_msg.get('isAdmin'):
                    chat_text = f"🛡️ **Admin Message:**\n\n{msg_text_content}"
                else:
                    partner_name = order.get('sellerName', 'Seller')
                    chat_text = f"💬 **New message from {partner_name}:**\n\n{msg_text_content}"

                chat_markup = types.InlineKeyboardMarkup()
                chat_markup.add(types.InlineKeyboardButton("🌐 Open Web Chat", url=f"url"))

                if not last_msg.get('isAdmin') and last_msg.get('senderUid') != 'SYSTEM':
                    chat_markup.add(types.InlineKeyboardButton("✍️ Reply", callback_data=f"reply_{order_id}"))

                if telegram_enabled:
                    if image_url:
                        try:
                            bot.send_photo(target_chat_id, photo=image_url, caption=chat_text, reply_markup=chat_markup, parse_mode="Markdown")
                        except Exception:
                            bot.send_message(target_chat_id, f"{chat_text}\n\n🖼️ [View Image]({image_url})", reply_markup=chat_markup, disable_web_page_preview=False)
                    else:
                        bot.send_message(target_chat_id, chat_text, reply_markup=chat_markup)

        # --- 2. ОНОВЛЕННЯ ТА ВІДПРАВКА КАРТКИ УГОДИ ---
        current_hash = order.get('releaseTxHash') or order.get('pendingTxHash') or order.get('fundTxHash') or "no_hash"
        state_key = f"b_st_upd_{order_id}_{status}_{is_disputed}_{current_hash}"

        if last_trade_status.get(state_key) or not telegram_enabled: return
        last_trade_status[state_key] = True

        raw_asset = str(order.get('asset', '')).upper()
        display_asset = "USDT" if ("TRC20" in raw_asset or raw_asset == "USDT") else ("USDT ERC20" if "ERC20" in raw_asset else raw_asset)

        if "BTC" in display_asset:
            display_net = 'Bitcoin Network 🟠'
        elif "USDC" in display_asset or "SOL" in display_asset:
            display_net = 'Solana Mainnet ☀️'
        elif "BNB" in display_asset:
            display_net = 'Binance Smart Chain 🟡'
        elif display_asset == "USDT" or "TRX" in display_asset:
            display_net = 'Tron (TRC20) 🔴'
        else:
            display_net = 'Ethereum (ERC20) 🔹'

        seller_name = order.get('sellerName', 'Seller')
        seller_uid = order.get('sellerUid')

        seller_user_data = db.collection('users').document(seller_uid).get().to_dict() or {}
        is_first_trade = seller_user_data.get('completedTradesCount', 0) == 0

        header = "🚨 **TRADE UNDER DISPUTE** 🚨" if is_disputed else f"🛒 **Buying {order.get('amountCrypto')} {display_asset}**"
        if is_first_trade and not is_disputed:
            header += "\n🎁 **[FIRST TRADE PROMO ACTIVE]**"

        msg_text = (
            f"{header}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 **Partner (Seller):** {seller_name}\n"
            f"💵 Amount: {order.get('amountFiat')} {order.get('fiatCurrency')}\n"
            f"🌐 Network: {display_net}\n"
            f"📊 Status: {get_order_status_label(status, lang)}"
        )

        proofs = ""
        if order.get('fundTxHash'): proofs += f"🛡️ **Escrow Locked:** `{order.get('fundTxHash')}`\n"
        if order.get('releaseTxHash'): proofs += f"🎁 **Release TX:** `{order.get('releaseTxHash')}`\n"
        if order.get('cancelTxHash'): proofs += f"❌ **Refund TX:** `{order.get('cancelTxHash')}`\n"

        if proofs:
            msg_text += f"\n\n⛓️ **Blockchain Proofs:**\n{proofs}"

        markup = types.InlineKeyboardMarkup(row_width=1)
        if is_disputed:
            markup.add(types.InlineKeyboardButton("⚖️ Chat with Arbitrator", url=f"url"))
        elif is_pending:
            markup.add(types.InlineKeyboardButton("⛓️ Verifying on Blockchain...", callback_data="none"))
        else:
            if status == 'CREATED' and order.get('creatorUid') != buyer_uid:
                markup.add(types.InlineKeyboardButton("✅ Accept Trade", callback_data=f"act_ACCEPTED_{order_id}"))
            if status == 'ESCROW_FUNDED':
                markup.add(types.InlineKeyboardButton("✅ I Have Paid", callback_data=f"act_PAID_{order_id}"))

            if status in ['CREATED', 'ACCEPTED']:
                markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data=f"act_CANCELLED_{order_id}"))

            if status not in ['COMPLETED', 'CANCELLED', 'RESOLVED'] and status != 'CREATED':
                markup.add(types.InlineKeyboardButton("🚨 Dispute", callback_data=f"open_dispute_{order_id}"))

        web_chat_url = f"url"
        markup.row(types.InlineKeyboardButton("🌐 Web Chat", url=web_chat_url), types.InlineKeyboardButton("✍️ Reply", callback_data=f"reply_{order_id}"))
        markup.add(types.InlineKeyboardButton(f"👤 View {seller_name}'s Profile", callback_data=f"view_profile_{seller_uid}"))

        current_msg_count = len(order.get('messages', []))
        send_or_edit_trade_card(target_chat_id, order_id, 'buyer', msg_text, markup, current_msg_count)

    except Exception as e:
        print(f"❌ Error notify_buyer: {e}")


def wait_for_confirmations_with_review(chat_id, msg_id, tx_hash, order_id, action_to_set, network, order_data):
    """Універсальний фоновий моніторинг конфірмацій: ETH, TRON, SOL, BSC, BTC"""
    w3 = None
    tron_client = None
    base_url = "https://etherscan.io/tx/"
    blocks_needed = 1

    if network == 'ETH':
        w3 = Web3(Web3.HTTPProvider(BLOCKCHAIN_CONFIG['ETH']['rpc']))
        base_url = "https://etherscan.io/tx/"
        blocks_needed = 3
    elif network == 'BSC':
        w3 = Web3(Web3.HTTPProvider(BLOCKCHAIN_CONFIG['BSC']['rpc']))
        base_url = "https://bscscan.com/tx/"
        blocks_needed = 2
    elif network == 'TRON':
        tron_client = Tron(HTTPProvider(BLOCKCHAIN_CONFIG['TRON']['rpc']))
        base_url = "https://tronscan.org/#/transaction/"
        blocks_needed = 15
    elif network == 'SOL':
        base_url = "https://explorer.solana.com/tx/"
        blocks_needed = 1
    elif network == 'BTC':
        base_url = "https://mempool.space/tx/"
        blocks_needed = 1

    max_attempts = 300 if network == 'BTC' else 150
    sleep_time = 30 if network == 'BTC' else (5 if network in ['BSC', 'SOL'] else 12)

    for i in range(max_attempts):
        try:
            confirmed = False
            current_val = 0

            if network in ['ETH', 'BSC'] and w3:
                receipt = w3.eth.get_transaction_receipt(tx_hash)
                if receipt:
                    current_val = w3.eth.block_number - receipt.blockNumber
                    if current_val >= blocks_needed: confirmed = True

            elif network == 'TRON' and tron_client:
                tx_info = tron_client.get_transaction_info(tx_hash)
                if 'blockNumber' in tx_info:
                    current_val = tron_client.get_latest_block_number() - tx_info['blockNumber']
                    if current_val >= blocks_needed: confirmed = True

            elif network == 'SOL':
                sig = Signature.from_string(tx_hash)
                resp = solana_client.get_signature_statuses([sig])
                if resp.value and resp.value[0]:
                    status_str = str(resp.value[0].confirmation_status)
                    if "Finalized" in status_str or "Confirmed" in status_str:
                        confirmed = True

            elif network == 'BTC':
                res = requests.get(f"{BTC_API_URL}/tx/{tx_hash}/status", timeout=10)
                if res.status_code == 200:
                    if res.json().get('confirmed'):
                        confirmed = True

            status_visual = "CONFIRMED ✅" if confirmed else "WAITING ⛓️"
            text = (f"🛡️ **Blockchain Verification**\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Network: {network} | Status: {status_visual}\n"
                    f"🔗 [View on Explorer]({base_url}{tx_hash})\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"⚠️ Status syncs automatically.")

            try:
                bot.edit_message_text(text, chat_id, msg_id, disable_web_page_preview=True)
            except:
                pass

            if confirmed:
                fresh_data = db.collection('orders').document(order_id).get().to_dict()
                if can_update_status(fresh_data.get('status'), action_to_set):
                    db.collection('orders').document(order_id).update({
                        'status': action_to_set,
                        'pendingTxHash': None
                    })

                    bot.send_message(chat_id, f"✅ **{network} sync complete!**")
                    if action_to_set == 'COMPLETED':
                        send_bot_review_menu(order_id, fresh_data)
                        process_cashback_and_increment_trades(order_id, fresh_data)
                break
        except Exception as e:
            print(f"{network} Sync error: {e}")

        time.sleep(sleep_time)


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    chat_id = call.message.chat.id
    if chat_id not in user_sessions:
        user_sessions[chat_id] = {'lang': 'en', 'authorized': False, 'uid': None}

    s = user_sessions[chat_id]
    lang = s.get('lang', 'en')
    t = lambda k: get_text(lang, k)

    user_doc = {}
    if s.get('uid'):
        doc_ref = db.collection('users').document(s.get('uid')).get()
        if doc_ref.exists:
            user_doc = doc_ref.to_dict()

    # Внутрішня хелпер-функція для безпечного редагування (текст / фото / підпис)
    def safe_edit_message(new_text, reply_markup=None):
        try:
            if call.message.content_type == 'photo':
                bot.edit_message_caption(chat_text=new_text, chat_id=chat_id, message_id=call.message.message_id,
                                         reply_markup=reply_markup, parse_mode="Markdown")
            else:
                bot.edit_message_text(new_text, chat_id=chat_id, message_id=call.message.message_id,
                                      reply_markup=reply_markup, parse_mode="Markdown")
        except Exception:
            try:
                bot.delete_message(chat_id, call.message.message_id)
            except:
                pass
            bot.send_message(chat_id, new_text, reply_markup=reply_markup, parse_mode="Markdown")

    # ==========================================
    # SECTION 1: AUTH & SYSTEM
    # ==========================================
    if call.data == "auth_login":
        s['state'] = 'waiting_for_email'
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        bot.send_message(chat_id, "📧 Please enter your Email address:", parse_mode="Markdown")

    elif call.data == "auth_register_choice":
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("🌐 Register on Website", url="url"),
            types.InlineKeyboardButton("🤖 Register here in Bot", callback_data="auth_reg_bot")
        )
        bot.send_message(chat_id, "How would you like to register?", reply_markup=markup)

    elif call.data.startswith('reply_'):
        order_id = call.data.replace('reply_', '')
        s['active_trade'] = order_id
        s['state'] = 'chat_mode'
        bot.answer_callback_query(call.id)
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
        markup.add(types.KeyboardButton("🚫 Leave Chat Mode"))
        bot.send_message(chat_id, "✍️ **Type your message:**\nUse the button below to exit chat.", reply_markup=markup)

    elif call.data == "auth_reg_bot":
        s['state'] = 'reg_waiting_email'
        bot.send_message(chat_id, "🆕 **Creating new account**\n\nPlease enter your Email for registration:",
                         parse_mode="Markdown")

    elif call.data.startswith('view_profile_'):
        target_uid = call.data.replace('view_profile_', '')
        show_user_profile(chat_id, target_uid, lang)
        bot.answer_callback_query(call.id)

    elif call.data == "logout_warning":
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("✅ Yes, Log Out", callback_data="logout_confirm"),
            types.InlineKeyboardButton("❌ Cancel", callback_data="off_cancel")
        )
        warning_text = (
            "⚠️ **WARNING: SECURITY CHECK**\n\n"
            "You are about to log out from the Telegram Bot.\n\n"
            "🛡️ **For your safety:** If you have the Web Application open, please **log out inside the Web App first** to ensure your private keys and seed phrase are cleared from the browser's cache.\n\n"
            "Are you sure you want to proceed?"
        )
        safe_edit_message(warning_text, reply_markup=markup)

    elif call.data == "logout_confirm":
        user_sessions[chat_id] = {'lang': lang, 'authorized': False, 'uid': None, 'state': None}
        try:
            bot.set_chat_menu_button(chat_id=chat_id, menu_button=types.MenuButtonDefault())
        except:
            pass
        safe_edit_message("✅ **Successfully logged out.**\nAccess to all features is now restricted.")
        bot.send_message(chat_id, "To log in again, use /start.", reply_markup=types.ReplyKeyboardRemove())

    elif call.data.startswith('view_trade_'):
        order_id = call.data.replace('view_trade_', '')
        order_ref = db.collection('orders').document(order_id).get()

        if not order_ref.exists:
            bot.answer_callback_query(call.id, "❌ Order not found")
            return

        order_data = order_ref.to_dict()
        status = order_data.get('status')

        if order_data.get('sellerUid') == s.get('uid'):
            state_key = f"s_st_upd_{order_id}_{status}"
            if state_key in last_trade_status:
                del last_trade_status[state_key]
            notify_seller(order_id, order_data)

        elif order_data.get('buyerUid') == s.get('uid'):
            state_key = f"b_st_upd_{order_id}_{status}"
            if state_key in last_trade_status:
                del last_trade_status[state_key]
            notify_buyer(order_id, order_data)

        bot.answer_callback_query(call.id)

    elif call.data.startswith('c_load_more_'):
        ts_float = float(call.data.replace('c_load_more_', ''))
        last_dt = datetime.fromtimestamp(ts_float, tz=timezone.utc)
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        show_community_page(chat_id, s.get('uid'), last_dt)
        bot.answer_callback_query(call.id)

    elif call.data == "pro_migration_start":
        s['state'] = 'waiting_mig_link'
        safe_edit_message(t('mig_start_msg'))
        bot.answer_callback_query(call.id)

    elif call.data == "pro_buy_start":
        bot.send_message(chat_id, t('buy_pro_msg'))
        # Адреса підтягується з конфігу глобально
        bot.send_message(chat_id,
                         f"`{BLOCKCHAIN_CONFIG.get('TRON', {}).get('usdt_contract', 'T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb')}`",
                         parse_mode="Markdown")
        bot.answer_callback_query(call.id)

    # ==========================================
    # SECTION 2: AD CREATION (OFFER FLOW)
    # ==========================================
    elif call.data == "manage_offers":
        ads_ref = db.collection('ads').where(filter=FieldFilter('uid', '==', s.get('uid'))).get()
        if not ads_ref:
            return bot.answer_callback_query(call.id, t('no_offers_manage'), show_alert=True)

        markup = types.InlineKeyboardMarkup(row_width=1)
        for ad in ads_ref:
            d = ad.to_dict()
            status_icon = "🟢" if d.get('status') == 'active' else "🔴"
            btn_text = f"{status_icon} {d.get('type').upper()} {d.get('asset')} - {d.get('price')} {d.get('fiat')}"
            markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"edit_ad_{ad.id}"))

        markup.add(types.InlineKeyboardButton(t('btn_back'), callback_data=f"view_profile_{s.get('uid')}"))
        safe_edit_message(t('manage_offers_title'), reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif call.data.startswith('edit_ad_'):
        ad_id = call.data.replace('edit_ad_', '')
        ad_doc = db.collection('ads').document(ad_id).get()
        if not ad_doc.exists:
            return bot.answer_callback_query(call.id, t('err_offer_gone'), show_alert=True)

        d = ad_doc.to_dict()
        status_text = "🟢 Active" if d.get('status') == 'active' else "🔴 Paused"
        limits = d.get('limits', {})
        text = t('ad_manage_title').format(
            type=d.get('type').upper(), asset=d.get('asset'), fiat=d.get('fiat'),
            price=d.get('price'), min=limits.get('min', 0), max=limits.get('max', 0),
            status=status_text
        )

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton(t('btn_ad_price'), callback_data=f"ad_price_{ad_id}"),
            types.InlineKeyboardButton(t('btn_ad_limits'), callback_data=f"ad_limits_{ad_id}")
        )
        markup.add(
            types.InlineKeyboardButton(t('btn_ad_status'), callback_data=f"ad_status_{ad_id}"),
            types.InlineKeyboardButton(t('btn_ad_delete'), callback_data=f"ad_delete_{ad_id}")
        )
        markup.add(types.InlineKeyboardButton(t('btn_back'), callback_data="manage_offers"))
        safe_edit_message(text, reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif call.data.startswith('ad_status_'):
        ad_id = call.data.replace('ad_status_', '')
        ad_ref = db.collection('ads').document(ad_id)
        ad = ad_ref.get().to_dict()
        new_status = 'paused' if ad.get('status') == 'active' else 'active'
        ad_ref.update({'status': new_status})

        # Точкове оновлення замість handle_callbacks(call)
        call.data = f"edit_ad_{ad_id}"
        return handle_callbacks(call)

    elif call.data.startswith('ad_delete_'):
        ad_id = call.data.replace('ad_delete_', '')
        db.collection('ads').document(ad_id).delete()
        bot.answer_callback_query(call.id, t('ad_deleted'), show_alert=True)
        call.data = "manage_offers"
        return handle_callbacks(call)

    elif call.data.startswith('ad_price_'):
        ad_id = call.data.replace('ad_price_', '')
        s['state'] = 'edit_ad_price'
        s['edit_ad_id'] = ad_id
        bot.send_message(chat_id, t('ad_enter_new_price'))
        bot.answer_callback_query(call.id)
    elif call.data.startswith('ad_limits_'):
        ad_id = call.data.replace('ad_limits_', '')
        s['state'] = 'edit_ad_limits'
        s['edit_ad_id'] = ad_id
        bot.send_message(chat_id, t('ad_enter_new_limits'))
        bot.answer_callback_query(call.id)

    elif call.data == "off_cancel":
        s['state'], s['new_offer'] = None, {}
        safe_edit_message("❌ **Creation cancelled.**")
        show_bottom_menu(chat_id, lang)

    elif call.data.startswith('off_back_'):
        target = call.data.replace('off_back_', '')
        if target == "type":
            s['state'] = 'offer_type'
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(types.InlineKeyboardButton("💰 Sell", callback_data="off_type_sell"),
                       types.InlineKeyboardButton("🛒 Buy", callback_data="off_type_buy"))
            markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="off_cancel"))
            safe_edit_message("➕ **Create Offer**\nSelect operation type:", reply_markup=markup)
        elif target in ["asset", "market", "price"]:
            call.data = f"off_type_{s['new_offer']['type']}" if target == "asset" else f"off_asset_{s['new_offer']['asset']}"
            return handle_callbacks(call)
        bot.answer_callback_query(call.id)

    elif call.data.startswith('off_type_'):
        s['new_offer'] = {'type': call.data.replace('off_type_', '')}
        s['state'] = 'offer_asset'
        markup = types.InlineKeyboardMarkup(row_width=2)
        assets = ["USDT TRC20", "USDT ERC20", "USDC SOL", "BTC", "ETH", "BNB", "TRX", "SOL"]
        markup.add(*[types.InlineKeyboardButton(a, callback_data=f"off_asset_{a}") for a in assets])
        markup.row(types.InlineKeyboardButton("⬅️ Back", callback_data="off_back_type"),
                   types.InlineKeyboardButton("❌ Cancel", callback_data="off_cancel"))
        safe_edit_message("💎 **Step 2: Select Crypto Asset:**", reply_markup=markup)

    elif call.data.startswith('off_asset_'):
        asset_name = call.data.replace('off_asset_', '')
        if asset_name: s['new_offer']['asset'] = asset_name
        user_country = user_doc.get('country')
        quick_country = user_country if user_country else "United Kingdom"
        regions = ["Europe", "Americas", "Asia", "Africa", "Oceania"]
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.row(types.InlineKeyboardButton(f"📍 {quick_country}", callback_data=f"off_set_ct_{quick_country}"),
                   types.InlineKeyboardButton("🇺🇸 United States", callback_data="off_set_ct_United States"))
        for reg in regions: markup.add(types.InlineKeyboardButton(reg, callback_data=f"off_reg_{reg}"))
        markup.add(types.InlineKeyboardButton("🔍 Custom Search (Text)", callback_data="off_ct_search"))
        markup.row(types.InlineKeyboardButton("⬅️ Back", callback_data="off_back_asset"),
                   types.InlineKeyboardButton("❌ Cancel", callback_data="off_cancel"))
        safe_edit_message(f"🌍 **Step 3: Select Region**\nYour profile country: **{quick_country}**",
                          reply_markup=markup)

    elif call.data.startswith('off_reg_'):
        region_name = call.data.replace('off_reg_', '')
        all_countries = get_countries_data()
        filtered_countries = [c for c in all_countries if c.get('region') == region_name]
        markup = types.InlineKeyboardMarkup(row_width=3)
        btn_list = []
        for c in filtered_countries:
            display_name = (c['name'][:10] + '..') if len(c['name']) > 12 else c['name']
            btn_list.append(types.InlineKeyboardButton(display_name, callback_data=f"off_set_ct_{c['name']}"))
        markup.add(*btn_list)
        current_asset = s['new_offer'].get('asset', 'USDT TRC20')
        markup.row(types.InlineKeyboardButton("⬅️ Back to Regions", callback_data=f"off_asset_{current_asset}"))
        safe_edit_message(f"📍 **{region_name}**\nSelect country:", reply_markup=markup)

    elif call.data == "off_ct_search":
        s['state'] = 'search_country'
        bot.send_message(chat_id, "🔍 **Type country name (e.g. Germany):**")

    elif call.data.startswith('off_set_ct_'):
        selected_ct = call.data.replace('off_set_ct_', '')
        s['new_offer']['country'] = selected_ct
        all_countries = get_countries_data()
        found_fiat = next((c['fiat'] for c in all_countries if c['name'] == selected_ct), "USD")
        s['new_offer']['fiat'] = found_fiat
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton(f"✅ Confirm {found_fiat}", callback_data=f"off_set_fiat_{found_fiat}"),
                   types.InlineKeyboardButton("🔄 Change Currency", callback_data="off_fiat_search"),
                   types.InlineKeyboardButton("⬅️ Back", callback_data="off_back_market"))
        safe_edit_message(f"💰 **Country:** {selected_ct}\n**Currency:** {found_fiat}", reply_markup=markup)

    elif call.data == "off_fiat_search":
        s['state'] = 'search_fiat'
        bot.send_message(chat_id, "🔍 **Type currency code (e.g. GBP):**")

    elif call.data.startswith('off_set_fiat_'):
        s['new_offer']['fiat'] = call.data.replace('off_set_fiat_', '')
        s['state'] = 'offer_price'
        markup = types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton("⬅️ Back", callback_data="off_fiat_search"))
        safe_edit_message(
            f"💵 **Step 5: Price**\nEnter price for 1 {s['new_offer']['asset']} in {s['new_offer']['fiat']}:",
            reply_markup=markup)

    elif call.data.startswith('pay_cat_'):
        cat_name = call.data.replace('pay_cat_', '')
        methods = PAYMENT_STRUCTURE.get(cat_name, [])
        markup = types.InlineKeyboardMarkup(row_width=2)
        for m_btn in methods: markup.add(types.InlineKeyboardButton(m_btn, callback_data=f"off_pay_{m_btn}"))
        markup.add(types.InlineKeyboardButton("⬅️ Back to Categories", callback_data="return_to_pay_cats"))
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        bot.send_message(chat_id, f"📍 **{cat_name}**\nSelect specific method:", reply_markup=markup,
                         parse_mode="Markdown")

    elif call.data == "return_to_pay_cats":
        markup = types.InlineKeyboardMarkup(row_width=1)
        for cat in PAYMENT_STRUCTURE.keys():
            if "Gift Cards" not in cat: markup.add(types.InlineKeyboardButton(cat, callback_data=f"pay_cat_{cat}"))
        markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="off_cancel"))
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        bot.send_message(chat_id, "🏦 **Select Payment Category:**", reply_markup=markup, parse_mode="Markdown")
        bot.answer_callback_query(call.id)

    elif call.data.startswith('off_pay_'):
        method_name = call.data.replace('off_pay_', '')
        try:
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        except:
            pass
        nick = user_doc.get('nickname', 'User')
        final_ad = {
            'asset': s['new_offer']['asset'],
            'available': s['new_offer'].get('totalAmount', 0),
            'avatar': nick[0].upper() if nick else "U",
            'color': "green",
            'country': s['new_offer']['country'],
            'createdAt': datetime.now(timezone.utc),
            'fiat': s['new_offer']['fiat'],
            'isOnline': True,
            'limits': s['new_offer']['limits'],
            'margin': None,
            'nickname': nick,
            'paymentMethod': method_name,
            'paymentMethods': [method_name],
            'price': s['new_offer']['price'],
            'priceType': 'fixed',
            'speed': 'fast',
            'terms': '',
            'totalAmount': s['new_offer'].get('totalAmount', 0),
            'trades': 0,
            'type': s['new_offer']['type'],
            'uid': s.get('uid'),
            'status': 'active'
        }
        _, doc_ref = db.collection('ads').add(final_ad)
        ad_id = doc_ref.id
        import threading
        threading.Thread(target=check_and_notify_price_alerts, args=(final_ad, ad_id), daemon=True).start()
        s['state'], s['new_offer'] = None, {}
        bot.send_message(chat_id, "✅ **Offer successfully created!**")
        show_bottom_menu(chat_id, lang)

        # ==========================================
        # SECTION SWAP (ChangeNOW)
        # ==========================================
    elif call.data.startswith('sw_'):
        action = call.data.replace('sw_', '')

        if 'swap' not in s or not s['swap'] or 'step' not in s['swap']:
            s['swap'] = {
                'step': 1,
                'from_asset': 'USDT TRC20', 'to_asset': 'SOL',
                'from_amount': 0.0, 'to_amount': 0.0,
                'rate': None, 'min_amount': 0.0, 'error': None
            }

        if action in ["pick_from", "pick_to"]:
            markup = types.InlineKeyboardMarkup(row_width=2)
            btns = [types.InlineKeyboardButton(a, callback_data=f"sw_set_{action.split('_')[1]}_{a}") for a in
                    SWAP_ASSETS.keys()]
            markup.add(*btns)
            markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="sw_back"))
            title = "📤 Select Asset to Pay:" if action == "pick_from" else "📥 Select Asset to Receive:"
            safe_edit_message(title, reply_markup=markup)
            bot.answer_callback_query(call.id)

        elif action == "next_step":
            s['swap']['step'] = min(s['swap']['step'] + 1, 3)
            show_swap_menu(chat_id, s, call.message.message_id)
            bot.answer_callback_query(call.id)

        elif action == "prev_step":
            s['swap']['step'] = max(s['swap']['step'] - 1, 1)
            show_swap_menu(chat_id, s, call.message.message_id)
            bot.answer_callback_query(call.id)

        elif action.startswith('set_from_') or action.startswith('set_to_'):
            target = "from_asset" if "set_from_" in action else "to_asset"
            asset_name = action.split('_', 2)[2]
            s['swap'][target] = asset_name
            if s['swap']['from_asset'] == s['swap']['to_asset']:
                bot.answer_callback_query(call.id, "⚠️ Assets must be different", show_alert=True)
                return
            show_swap_menu(chat_id, s, call.message.message_id)
            bot.answer_callback_query(call.id)

        elif action == "flip":
            s['swap']['from_asset'], s['swap']['to_asset'] = s['swap']['to_asset'], s['swap']['from_asset']
            s['swap']['from_amount'] = s['swap']['to_amount']
            show_swap_menu(chat_id, s, call.message.message_id)
            bot.answer_callback_query(call.id)

        elif action == "back":
            show_swap_menu(chat_id, s, call.message.message_id)
            bot.answer_callback_query(call.id)

        elif action == "enter_amount":
            s['state'] = 'waiting_for_swap_amount'
            s['swap_msg_id'] = call.message.message_id
            sent_msg = bot.send_message(chat_id, f"🔢 **Enter amount of {s['swap']['from_asset']} you want to swap:**")
            s['last_ask_msg_id'] = sent_msg.message_id
            bot.answer_callback_query(call.id)

        elif action == "confirm":
            bot.answer_callback_query(call.id, "🔄 Creating order...")
            status_msg = bot.send_message(chat_id, "⏳ **Creating order with ChangeNOW...**")

            try:
                f_asset = SWAP_ASSETS[s['swap']['from_asset']]
                t_asset = SWAP_ASSETS[s['swap']['to_asset']]

                user_doc_fund = db.collection('users').document(s.get('uid')).get().to_dict() or {}
                mnemonic = user_doc_fund.get('walletMnemonic')
                t_network = t_asset['network']
                recipient_address = ""

                if t_network == "trx":
                    from eth_account import Account
                    tron_acc = Account.from_mnemonic(mnemonic, account_path="m/44'/195'/0'/0/0")
                    recipient_address = eth_to_tron_address(tron_acc.address)
                elif t_network in ["eth", "bsc"]:
                    w3_temp = Web3()
                    eth_acc = w3_temp.eth.account.from_mnemonic(mnemonic, account_path="m/44'/60'/0'/0/0")
                    recipient_address = eth_acc.address
                elif t_network == "sol":
                    sol_kp = get_solana_keypair(mnemonic)
                    recipient_address = str(sol_kp.pubkey())
                elif t_network == "btc":
                    recipient_address = get_buyer_btc_address(s.get('uid'))

                payload = {
                    "fromCurrency": f_asset['ticker'],
                    "toCurrency": t_asset['ticker'],
                    "fromAmount": str(s['swap']['from_amount']),
                    "address": recipient_address,
                    "fromNetwork": f_asset['network'],
                    "toNetwork": t_asset['network'],
                    "flow": "standard"
                }

                res = requests.post("https://api.changenow.io/v2/exchange", json=payload,
                                    headers={"x-changenow-api-key": CHANGENOW_API_KEY}, timeout=15)
                if res.status_code == 200:
                    data = res.json()
                    payin_address = data.get('payinAddress')
                    order_id = data.get('id')

                    bot.edit_message_text(
                        f"✅ **Order Created! (ID: `{order_id}`)**\n\n🔐 *Signing transaction to send {s['swap']['from_amount']} {s['swap']['from_asset']}...*",
                        chat_id, status_msg.message_id)

                    tx_hash = execute_swap_transfer(chat_id, s['swap'], payin_address, mnemonic, order_id)
                    if tx_hash and "ERROR" not in tx_hash:
                        bot.edit_message_text(
                            f"🚀 **Swap Transaction Sent!**\n\n📥 **ChangeNOW Order ID:** `{order_id}`\n🔗 **Your TX Hash:** `{tx_hash}`\n\n⏳ ChangeNOW is processing your swap. Your {s['swap']['to_asset']} will arrive shortly.",
                            chat_id, status_msg.message_id)
                        s['swap'] = None
                    else:
                        bot.edit_message_text(f"❌ **Transaction Failed:**\n{tx_hash}", chat_id, status_msg.message_id)
                else:
                    bot.edit_message_text(f"❌ ChangeNOW API Error:\n{res.json().get('message', 'Unknown Error')}",
                                          chat_id, status_msg.message_id)
                    show_swap_menu(chat_id, s)

            except Exception as e:
                bot.edit_message_text(f"❌ Critical Swap Error: {e}", chat_id, status_msg.message_id)

    # ==========================================
    # SECTION 3: ТОРГІВЛЯ ТА БЛОКЧЕЙН (Trading & Actions)
    # ==========================================
    elif call.data.startswith('act_'):
        bot.answer_callback_query(call.id)
        parts = call.data.split('_')
        order_id, action = parts[-1], "_".join(parts[1:-1])
        order_ref = db.collection('orders').document(order_id)
        order_data = order_ref.get().to_dict()

        if not order_data:
            return bot.answer_callback_query(call.id, "❌ Order not found", show_alert=True)

        if not can_update_status(order_data.get('status', 'CREATED'), action):
            return bot.answer_callback_query(call.id, "⚠️ Invalid status transition!", show_alert=True)

        user_doc_fund = db.collection('users').document(s.get('uid')).get().to_dict() or {}
        mnemonic = user_doc_fund.get('walletMnemonic')
        asset_up = str(order_data.get('asset', '')).upper()
        is_evm = ('ETH' in asset_up or 'ERC20' in asset_up)

        if action == "ACCEPTED":
            order_ref.update({'status': 'ACCEPTED'})

        elif action == "ESCROW_FUNDED":
            if order_data.get('status') == 'WAITING_FOR_DEPOSIT' or order_data.get('pendingTxHash'):
                return bot.answer_callback_query(call.id, "⚠️ Transaction is already pending!", show_alert=True)

            order_ref.update({'status': 'WAITING_FOR_DEPOSIT'})
            status_msg = bot.send_message(chat_id, "🔐 **Signing transaction...**")
            tx_hash = None
            network_name = ""

            try:
                if "BTC" in asset_up:
                    tx_hash = auto_fund_btc_escrow(order_id, order_data, mnemonic)
                    network_name = "BTC"
                elif "SOL" in asset_up or "USDC" in asset_up:
                    tx_hash = auto_fund_sol_escrow(order_id, order_data, mnemonic)
                    network_name = "SOL"
                elif "BNB" in asset_up:
                    tx_hash = sign_and_broadcast_bsc(order_id, order_data, mnemonic)
                    network_name = "BSC"
                else:
                    tx_hash = sign_and_broadcast_eth(order_id, order_data,
                                                     mnemonic) if is_evm else sign_and_broadcast_trade(order_id,
                                                                                                       order_data,
                                                                                                       mnemonic)
                    network_name = "ETH" if is_evm else "TRON"

                if tx_hash and "ERROR" not in str(tx_hash):
                    db.collection('orders').document(order_id).update({
                        'status': 'WAITING_FOR_DEPOSIT',
                        'pendingTxHash': tx_hash,
                        'fundTxHash': tx_hash
                    })
                    send_system_message_to_chat(order_id, f"Blockchain TX sent ({network_name}): {tx_hash}")

                    import threading
                    threading.Thread(target=wait_for_confirmations_with_review,
                                     args=(chat_id, status_msg.message_id, tx_hash, order_id, 'ESCROW_FUNDED',
                                           network_name, order_data),
                                     daemon=True).start()
                else:
                    raise Exception(str(tx_hash))

            except Exception as e:
                order_ref.update({'status': 'ACCEPTED', 'pendingTxHash': None})
                updated_order = order_ref.get().to_dict()

                # Повне очищення кешу статусів (враховуючи динамічний хеш)
                for k in list(last_trade_status.keys()):
                    if f"s_st_upd_{order_id}" in k:
                        last_trade_status.pop(k, None)

                notify_seller(order_id, updated_order)
                bot.edit_message_text(f"❌ **Error:** {str(e)}\n\nStatus reset. Buttons restored.", chat_id,
                                      status_msg.message_id, reply_markup=None)

        elif action == "COMPLETED":
            if order_data.get('status') == 'COMPLETED' or order_data.get('pendingTxHash'):
                return bot.answer_callback_query(call.id, "⚠️ Already processing...", show_alert=True)

            status_msg = bot.send_message(chat_id, "🔐 **Releasing crypto...**")
            tx_hash = None
            network_name = ""

            try:
                if "BTC" in asset_up:
                    tx_hash = release_escrow_btc(order_id, order_data)
                    network_name = "BTC"
                elif "SOL" in asset_up or "USDC" in asset_up:
                    tx_hash = release_escrow_sol(order_id, order_data)
                    network_name = "SOL"
                elif "BNB" in asset_up:
                    res = release_escrow_bsc(order_id, order_data, mnemonic)
                    tx_hash = res.transactionHash.hex() if res else None
                    network_name = "BSC"
                else:
                    res = release_escrow_eth(order_id, order_data, mnemonic) if is_evm else release_escrow_trade(
                        order_data, mnemonic)
                    tx_hash = res.transactionHash.hex() if (is_evm and res) else (res.get('id') if res else None)
                    network_name = "ETH" if is_evm else "TRON"

                if tx_hash and "ERROR" not in str(tx_hash):
                    db.collection('orders').document(order_id).update({
                        'pendingTxHash': tx_hash,
                        'releaseTxHash': tx_hash
                    })
                    send_system_message_to_chat(order_id, f"Release TX sent: {tx_hash}")

                    import threading
                    threading.Thread(target=wait_for_confirmations_with_review,
                                     args=(chat_id, status_msg.message_id, tx_hash, order_id, 'COMPLETED', network_name,
                                           order_data),
                                     daemon=True).start()
                else:
                    raise Exception(str(tx_hash))

            except Exception as e:
                order_ref.update({'status': 'PAID', 'pendingTxHash': None})
                updated_order = order_ref.get().to_dict()

                for k in list(last_trade_status.keys()):
                    if f"s_st_upd_{order_id}" in k:
                        last_trade_status.pop(k, None)

                notify_seller(order_id, updated_order)
                bot.edit_message_text(f"❌ **Release Failed:** {str(e)}\n\nButtons restored.", chat_id,
                                      status_msg.message_id, reply_markup=None)

        elif action == "CANCELLED":
            if order_data.get('status') in ['COMPLETED', 'PAID', 'RESOLVED']:
                return bot.answer_callback_query(call.id, "❌ Cannot cancel paid or completed trade.", show_alert=True)

            if order_data.get('status') == 'ESCROW_FUNDED':
                status_msg = bot.send_message(chat_id, "⚠️ **Refunding funds from Escrow...**")
                res = cancel_order_onchain(order_id, order_data, mnemonic)

                if res and "ERROR" not in str(res):
                    order_ref.update({'status': 'CANCELLED', 'cancelTxHash': str(res)})
                    bot.edit_message_text(f"✅ **Trade Cancelled & Refunded!**\nTX: `{res}`", chat_id,
                                          status_msg.message_id)
                else:
                    bot.edit_message_text(f"❌ **Refund failed:** {res}\nPlease contact support.", chat_id,
                                          status_msg.message_id)
                    return
            else:
                order_ref.update({'status': 'CANCELLED'})
                bot.send_message(chat_id, "❌ **Trade has been cancelled.**")

            send_system_message_to_chat(order_id, "Trade was cancelled by user.")

    elif call.data.startswith('open_dispute_'):
        order_id = call.data.replace('open_dispute_', '')
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("✅ Yes, Open Dispute", callback_data=f"confirm_dispute_{order_id}"),
            types.InlineKeyboardButton("❌ Cancel", callback_data=f"view_trade_{order_id}")
        )
        safe_edit_message(
            "🚨 **Are you sure you want to open a dispute?**\n\nThis will freeze the trade and invite an administrator to the chat. Please only do this if there is a real problem (e.g., payment not received).",
            reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif call.data.startswith('confirm_dispute_'):
        order_id = call.data.replace('confirm_dispute_', '')
        order_ref = db.collection('orders').document(order_id)
        order_data = order_ref.get().to_dict()

        if not order_data:
            return bot.answer_callback_query(call.id, "❌ Order not found.", show_alert=True)

        if order_data.get('isDisputed'):
            return bot.answer_callback_query(call.id, "⚠️ Dispute is already active.", show_alert=True)

        try:
            order_ref.update({
                'isDisputed': True,
                'disputeOpenedBy': s.get('uid'),
                'disputeOpenedAt': datetime.now(timezone.utc)
            })

            user_name = s.get('nickname', 'User')
            send_system_message_to_chat(order_id,
                                        f"🚨 DISPUTE OPENED by {user_name}. The trade is frozen. Please wait for an administrator to join the chat.")

            safe_edit_message(
                "✅ **Dispute has been successfully opened.**\nPlease go to the web chat and provide evidence (screenshots of payment/non-payment).",
                reply_markup=types.InlineKeyboardMarkup().add(
                    types.InlineKeyboardButton("🌐 Open Web Chat",
                                               url=f"url")
                ))

            updated_order = order_ref.get().to_dict()

            # Примусове очищення кешу картки угоди для обох сторін
            for k in list(last_trade_status.keys()):
                if f"s_st_upd_{order_id}" in k or f"b_st_upd_{order_id}" in k:
                    last_trade_status.pop(k, None)

            notify_seller(order_id, updated_order, force_send=True)
            notify_buyer(order_id, updated_order, force_send=True)
            bot.answer_callback_query(call.id, "Dispute Opened!")

        except Exception as e:
            bot.answer_callback_query(call.id, f"Error: {e}", show_alert=True)

    # ==========================================
    # SECTION 4: ПОШУК ТА ФІЛЬТРИ (Search & Filters)
    # ==========================================
    elif call.data == "btn_wallet":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        call.message.text = t('btn_wallet')
        return handle_all(call.message)

    elif call.data.startswith('f_asset_'):
        s['filter']['asset'] = call.data.replace('f_asset_', '')
        regions = ["Europe", "Americas", "Asia", "Africa", "Oceania"]
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(types.InlineKeyboardButton("🌍 All Countries", callback_data="f_ct_All Countries"))

        user_ct = user_doc.get('country', 'United Kingdom')
        markup.add(types.InlineKeyboardButton(f"📍 {user_ct}", callback_data=f"f_ct_{user_ct}"))
        for reg in regions:
            markup.add(types.InlineKeyboardButton(reg, callback_data=f"f_reg_{reg}"))

        safe_edit_message(f"🌍 **Step 2: Select Country**\nAsset: `{s['filter']['asset']}`", reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif call.data.startswith('c_like_'):
        post_id = call.data.replace('c_like_', '')
        my_uid = s.get('uid')
        if not my_uid:
            return bot.answer_callback_query(call.id, "❌ Login first", show_alert=True)

        post_ref = db.collection('community_posts').document(post_id)
        post_snap = post_ref.get()
        if not post_snap.exists:
            return bot.answer_callback_query(call.id, "Post deleted.", show_alert=True)

        post = post_snap.to_dict()
        likes = post.get('likes', [])

        from google.cloud import firestore
        if my_uid in likes:
            post_ref.update({'likes': firestore.ArrayRemove([my_uid])})
            likes.remove(my_uid)
        else:
            post_ref.update({'likes': firestore.ArrayUnion([my_uid])})
            likes.append(my_uid)

        post['likes'] = likes
        send_post_card(chat_id, my_uid, post_id, post, call.message.message_id)
        bot.answer_callback_query(call.id)

    elif call.data.startswith('c_view_'):
        post_id = call.data.replace('c_view_', '')
        post_snap = db.collection('community_posts').document(post_id).get()
        if not post_snap.exists:
            return bot.answer_callback_query(call.id, "Post deleted.", show_alert=True)

        comments = post_snap.to_dict().get('comments', [])
        if not comments:
            return bot.answer_callback_query(call.id, "📭 No comments yet.", show_alert=True)

        msg = "💬 **Latest Comments:**\n\n"
        for c in comments[-10:]:
            msg += f"👤 **{c.get('authorName', 'User')}**: {c.get('text', '')}\n\n"

        bot.send_message(chat_id, msg, parse_mode="Markdown")
        bot.answer_callback_query(call.id)

    elif call.data.startswith('c_reply_'):
        post_id = call.data.replace('c_reply_', '')
        s['state'] = 'writing_community_comment'
        s['active_post_id'] = post_id

        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(types.KeyboardButton("❌ Cancel Comment"))
        bot.send_message(chat_id, "✍️ **Type your comment below:**", reply_markup=markup, parse_mode="Markdown")
        bot.answer_callback_query(call.id)

    elif call.data.startswith('f_reg_'):
        region_name = call.data.replace('f_reg_', '')
        all_countries = get_countries_data()
        filtered = [c for c in all_countries if c.get('region') == region_name]
        markup = types.InlineKeyboardMarkup(row_width=3)
        btn_list = []
        for c in filtered:
            display_name = (c['name'][:10] + '..') if len(c['name']) > 12 else c['name']
            btn_list.append(types.InlineKeyboardButton(display_name, callback_data=f"f_ct_{c['name']}"))
        markup.add(*btn_list)
        markup.row(types.InlineKeyboardButton("⬅️ Back", callback_data=f"f_asset_{s['filter']['asset']}"))
        safe_edit_message(f"📍 **{region_name}**\nSelect country:", reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif call.data.startswith('f_ct_'):
        selected_ct = call.data.replace('f_ct_', '')
        s['filter']['country'] = selected_ct
        markup = types.InlineKeyboardMarkup(row_width=2)
        if selected_ct == 'All Countries':
            main_fiats = ["USD", "EUR", "UAH", "PLN", "GBP"]
            markup.add(*[types.InlineKeyboardButton(f, callback_data=f"f_fiat_{f}") for f in main_fiats])
            markup.add(types.InlineKeyboardButton("🔍 Search Currency", callback_data="f_fiat_search"))
        else:
            all_countries = get_countries_data()
            found_fiat = next((c['fiat'] for c in all_countries if c['name'] == selected_ct), "USD")
            s['filter']['fiat'] = found_fiat
            markup.add(types.InlineKeyboardButton(f"✅ Use {found_fiat}", callback_data=f"f_fiat_{found_fiat}"),
                       types.InlineKeyboardButton("🔄 Other", callback_data="f_fiat_search"))
        safe_edit_message(f"💰 **Step 3: Select Fiat**\nCountry: {selected_ct}", reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif call.data == "check_kyc_status":
        my_uid = s.get('uid')
        if not my_uid:
            return bot.answer_callback_query(call.id, "❌ Session expired.", show_alert=True)

        user_doc_check = db.collection('users').document(my_uid).get().to_dict() or {}
        status = user_doc_check.get('kycStatus', 'none')

        if status == 'approved':
            bot.answer_callback_query(call.id, "✅ Success! KYC approved.", show_alert=True)
            bot.send_message(chat_id, "🎉 **KYC Verified!** You can now create offers.")
        else:
            bot.answer_callback_query(call.id, "❌ Verification not found or rejected. Please try again.",
                                      show_alert=True)

        # ==========================================
        # SECTION 5: PRICE ALERTS (PRICE MONITORING)
        # ==========================================
    elif call.data == 'al_list_my':
        alerts = db.collection('price_alerts') \
            .where(filter=FieldFilter('tg_id', '==', chat_id)) \
            .where(filter=FieldFilter('is_active', '==', True)).get()

        if not alerts:
            return bot.answer_callback_query(call.id, "You have no active alerts.", show_alert=True)

        markup = types.InlineKeyboardMarkup()
        text = "📋 **Your Active Alerts:**\n\n"

        for i, doc in enumerate(alerts):
            al = doc.to_dict()
            emoji = "🛒" if al.get('type') == "buy" else "💰"
            text += f"{i + 1}. {emoji} {al.get('asset')} | {al.get('target_price')} {al.get('fiat')}\n"
            text += f"   📍 {al.get('country', 'All Countries')}\n\n"
            markup.add(types.InlineKeyboardButton(f"❌ Delete #{i + 1}", callback_data=f"al_del_{doc.id}"))

        markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="al_back_main"))
        safe_edit_message(text, reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif call.data == 'al_back_main':
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("➕ Create New Alert", callback_data="al_create_start"),
            types.InlineKeyboardButton("📋 My Active Alerts", callback_data="al_list_my")
        )
        safe_edit_message(
            "🔔 **Price Alerts Management**\n\nYou can set up notifications for specific prices or manage your current subscriptions.",
            reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif call.data.startswith('al_del_'):
        alert_id = call.data.replace('al_del_', '')
        db.collection('price_alerts').document(alert_id).delete()
        bot.answer_callback_query(call.id, "Alert deleted!")

        # Точкове переспрямування замість рекурсивного виклику всього handle_callbacks
        call.data = 'al_list_my'
        return handle_callbacks(call)

    elif call.data == 'al_create_start':
        s['state'] = 'alert_asset_selection'
        markup = types.InlineKeyboardMarkup(row_width=2)
        assets = ["USDT TRC20", "USDT ERC20", "BTC", "ETH", "BNB", "TRX", "SOL", "USDC SOL"]
        btns = [types.InlineKeyboardButton(a, callback_data=f"al_ast_{a}") for a in assets]
        markup.add(*btns)
        markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="al_back_main"))
        safe_edit_message("🔔 Select an asset you want to track:", reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif call.data.startswith('al_ast_'):
        asset = call.data.replace('al_ast_', '')
        s['new_alert'] = {'asset': asset}
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("🛒 Buy", callback_data="al_tp_buy"),
            types.InlineKeyboardButton("💰 Sell", callback_data="al_tp_sell")
        )
        safe_edit_message(f"💎 Asset: *{asset}*\n\nDo you want to get a notification when someone is:",
                          reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif call.data.startswith('al_tp_'):
        s['new_alert']['type'] = call.data.replace('al_tp_', '')
        markup = types.InlineKeyboardMarkup(row_width=2)
        user_ct = user_doc.get('country', 'United Kingdom')
        markup.add(types.InlineKeyboardButton(f"📍 {user_ct}", callback_data=f"al_ct_{user_ct}"))
        markup.add(types.InlineKeyboardButton("🌍 All Countries", callback_data="al_ct_All Countries"))
        safe_edit_message("🌍 **Step 3: Select Country**", reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif call.data.startswith('al_ct_'):
        s['new_alert']['country'] = call.data.replace('al_ct_', '')
        all_countries = get_countries_data()
        found_fiat = next((c['fiat'] for c in all_countries if c['name'] == s['new_alert']['country']), "USD")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(f"✅ Use {found_fiat}", callback_data=f"al_fi_{found_fiat}"))
        markup.add(types.InlineKeyboardButton("🔍 Other Currency", callback_data="al_fiat_search_alert"))
        safe_edit_message(f"💰 **Step 4: Select Fiat Currency**\n(Country: {s['new_alert']['country']})",
                          reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif call.data == 'al_fiat_search_alert':
        s['state'] = 'al_fiat_search_state'
        bot.send_message(chat_id, "🔍 **Type 3-letter currency code (e.g. USD, EUR):**")
        bot.answer_callback_query(call.id)

    elif call.data.startswith('al_fi_'):
        s['new_alert']['fiat'] = call.data.replace('al_fi_', '')
        s['state'] = 'waiting_alert_price'
        type_text = "BUYING" if s['new_alert']['type'] == "buy" else "SELLING"
        safe_edit_message(
            f"🔢 Enter your target **Price** in {s['new_alert']['fiat']} for {type_text} {s['new_alert']['asset']}:")
        bot.answer_callback_query(call.id)

        # ==========================================
        # SECTION 6: P2P FILTERS & MATCHING
        # ==========================================
    elif call.data.startswith('f_fiat_'):
        if call.data == "f_fiat_search":
            s['state'] = 'search_filter_fiat'
            bot.send_message(chat_id, "🔍 **Type 3-letter currency code:**")
            return
        s['filter']['fiat'] = call.data.replace('f_fiat_', '')
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("💳 All Methods", callback_data="f_pm_All Methods"))
        for cat in PAYMENT_STRUCTURE.keys():
            markup.add(types.InlineKeyboardButton(cat, callback_data=f"f_cat_{cat}"))
        safe_edit_message(f"Keep Currency: {s['filter']['fiat']}\n\n🏦 **Step 4: Payment Method**", reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif call.data.startswith('f_cat_'):
        cat_name = call.data.replace('f_cat_', '')
        methods = PAYMENT_STRUCTURE.get(cat_name, [])
        markup = types.InlineKeyboardMarkup(row_width=2)
        for m in methods:
            markup.add(types.InlineKeyboardButton(m, callback_data=f"f_pm_{m}"))
        markup.add(
            types.InlineKeyboardButton("⬅️ Back", callback_data=f"f_ct_{s['filter'].get('country', 'All Countries')}"))
        safe_edit_message(f"📍 **{cat_name}**\nSelect specific payment channel:", reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif call.data.startswith('f_pm_'):
        s['filter']['method'] = call.data.replace('f_pm_', '')
        f = s['filter']
        ads_ref = db.collection('ads').where(filter=FieldFilter('type', '==', f['type'])).where(
            filter=FieldFilter('asset', '==', f['asset'])).where(filter=FieldFilter('fiat', '==', f['fiat']))

        if f.get('country') != 'All Countries':
            ads_ref = ads_ref.where(filter=FieldFilter('country', '==', f['country']))
        if f.get('method') != 'All Methods':
            ads_ref = ads_ref.where(filter=FieldFilter('paymentMethod', '==', f['method']))

        results = ads_ref.get()
        markup = types.InlineKeyboardMarkup(row_width=1)
        found_active = False

        for ad in results:
            d = ad.to_dict()
            if d.get('status', 'active') == 'active':
                found_active = True
                limits = d.get('limits', {})
                min_l = limits.get('min', 0)
                max_l = limits.get('max', 0)
                btn_text = f"💰 {d.get('price')} {d.get('fiat')} | {min_l}-{max_l} {d.get('fiat')} | 🏦 {d.get('paymentMethod')}"
                markup.add(types.InlineKeyboardButton(btn_text, callback_data=f"init_ad_{ad.id}"))

        if not found_active:
            markup.add(types.InlineKeyboardButton("🔄 Reset Filters", callback_data=f"f_asset_{f['asset']}"))
            safe_edit_message("📭 No offers found with matching parameters.", reply_markup=markup)
            return

        markup.row(types.InlineKeyboardButton("🔄 Reset Filters", callback_data=f"f_asset_{f['asset']}"))
        safe_edit_message(
            f"🚀 **Available Offers for {f['asset']}**\n🌍 Country: {f['country']}\n💳 Method: {f['method']}",
            reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif call.data.startswith('init_ad_'):
        ad_id = call.data.replace('init_ad_', '')
        ad_doc = db.collection('ads').document(ad_id).get()

        if not ad_doc.exists:
            return bot.answer_callback_query(call.id, "❌ Offer no longer exists.", show_alert=True)

        ad_data = ad_doc.to_dict()
        s['active_ad_id'] = ad_id
        s['active_ad_data'] = ad_data
        s['state'] = 'waiting_for_trade_amount'

        limits = ad_data.get('limits', {})
        min_l, max_l = limits.get('min', 0), limits.get('max', 0)
        fiat = ad_data.get('fiat', 'USD')

        text = (f"📈 **Opening Trade**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 **Trader:** {ad_data.get('nickname')}\n"
                f"💵 **Price:** {ad_data.get('price')} {fiat}\n"
                f"💳 **Method:** {ad_data.get('paymentMethod')}\n"
                f"🛡️ **Limits:** {min_l} - {max_l} {fiat}\n\n"
                f"🔢 **Enter amount in {fiat} you want to trade:**")

        safe_edit_message(text)
        bot.answer_callback_query(call.id)

    elif call.data.startswith('view_reviews_'):
        target_uid = call.data.replace('view_reviews_', '')
        reviews = db.collection('reviews').where(filter=FieldFilter('targetUid', '==', target_uid)).limit(5).get()
        review_msg = "💬 **Latest Feedbacks:**\n\n"
        for r_doc in reviews:
            r = r_doc.to_dict()
            review_msg += f"{'⭐' * int(r.get('rating', 5))} **{r.get('authorName')}**: {r.get('text')}\n\n"
        if not reviews:
            review_msg = "📭 This user has no reviews yet."
        bot.send_message(chat_id, review_msg)
        bot.answer_callback_query(call.id)

        # ==========================================
        # SECTION 7: REGISTRATION FLOW
        # ==========================================
    elif call.data.startswith('reg_reg_'):
        region_name = call.data.replace('reg_reg_', '')
        all_countries = get_countries_data()
        filtered_countries = [c for c in all_countries if c.get('region') == region_name]

        markup = types.InlineKeyboardMarkup(row_width=3)
        btn_list = []
        for c in filtered_countries:
            display_name = (c['name'][:10] + '..') if len(c['name']) > 12 else c['name']
            btn_list.append(types.InlineKeyboardButton(display_name, callback_data=f"reg_set_ct_{c['name']}"))
        markup.add(*btn_list)
        markup.row(types.InlineKeyboardButton("⬅️ Back to Regions", callback_data="reg_back_to_regions"))

        safe_edit_message(f"📍 **{region_name}**\nSelect country:", reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif call.data == "reg_back_to_regions":
        regions = ["Europe", "Americas", "Asia", "Africa", "Oceania"]
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.row(
            types.InlineKeyboardButton("🇺🇸 United States", callback_data="reg_set_ct_United States"),
            types.InlineKeyboardButton("🇬🇧 United Kingdom", callback_data="reg_set_ct_United Kingdom")
        )
        for reg in regions:
            markup.add(types.InlineKeyboardButton(reg, callback_data=f"reg_reg_{reg}"))
        markup.add(types.InlineKeyboardButton("🔍 Custom Search (Text)", callback_data="reg_ct_search"))

        safe_edit_message("🌍 **Select your Region or Search:**", reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif call.data == "reg_ct_search":
        s['state'] = 'search_reg_country'
        bot.send_message(chat_id, "🔍 **Type country name (e.g. Germany, Poland):**")
        bot.answer_callback_query(call.id)

    elif call.data.startswith('reg_set_ct_'):
        selected_ct = call.data.replace('reg_set_ct_', '')
        all_countries = get_countries_data()
        found_country = next((c for c in all_countries if c['name'] == selected_ct), None)

        if found_country:
            safe_edit_message(f"✅ Country selected: {selected_ct}")
            s['temp_country_data'] = found_country
            register_user_in_firebase(chat_id, s)
        bot.answer_callback_query(call.id)

        # ==========================================
        # SECTION 8: SYSTEM MISC / LEADERBOARD
        # ==========================================
    elif call.data == "view_leaderboard":
        try:
            orders_ref = db.collection('orders').where(filter=FieldFilter('status', '==', 'COMPLETED')).get()
            if not orders_ref:
                return bot.answer_callback_query(call.id, "Leaderboard is empty.", show_alert=True)

            stats = {}
            for doc in orders_ref:
                order = doc.to_dict()
                amount = float(order.get('amountCrypto', 0))
                participants = [
                    {'uid': order.get('sellerUid'), 'name': order.get('sellerName', 'Trader')},
                    {'uid': order.get('buyerUid'), 'name': order.get('buyerName', 'Trader')}
                ]
                for p in participants:
                    u_id = p['uid']
                    if not u_id:
                        continue
                    if u_id not in stats:
                        u_doc = db.collection('users').document(u_id).get()
                        u_data = u_doc.to_dict() if u_doc.exists else {}
                        r_sum, r_count = u_data.get('ratingSum', 0), u_data.get('reviewsCount', 0)
                        stats[u_id] = {
                            'name': p['name'], 'count': 0, 'volume': 0.0,
                            'rating': round(r_sum / r_count, 1) if r_count > 0 else 0.0
                        }
                    stats[u_id]['count'] += 1
                    stats[u_id]['volume'] += amount

            leaderboard = sorted(stats.values(), key=lambda x: (x['count'], x['volume']), reverse=True)
            msg = t('lb_title')

            for i, user in enumerate(leaderboard[:5]):
                medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i]
                msg += f"{medal} **{user['name']}** | Trades: `{user['count']}`\n"
            bot.send_message(chat_id, msg)
            bot.answer_callback_query(call.id)
        except Exception as e:
            print(f"Leaderboard error: {e}")

    # ==========================================
    # EXTERNAL DEPENDENCIES & HELPERS
    # ==========================================
def get_countries_data():
        """Отримує список країн, сортує їх та включає регіон"""
        try:
            res = requests.get('https://restcountries.com/v3.1/all?fields=name,cca2,currencies,region', timeout=10)
            if res.status_code == 200:
                data = res.json()
                mapped = []
                for c in data:
                    fiat = list(c.get('currencies', {}).keys())[0] if c.get('currencies') else 'USD'
                    mapped.append({
                        'name': c['name']['common'],
                        'fiat': fiat,
                        'region': c.get('region')
                    })
                return sorted(mapped, key=lambda x: x['name'])
        except:
            pass
        return [{'name': 'Ukraine', 'fiat': 'UAH', 'region': 'Europe'},
                {'name': 'United States', 'fiat': 'USD', 'region': 'Americas'}]

def get_user_virtual_balances(uid):
        """Повний аналог VirtualCryptoService.getUserBalances на Python"""
        if not uid:
            return {}
        try:
            user_doc = db.collection('users').document(uid).get()
            if user_doc.exists:
                return user_doc.to_dict().get('balances', {})
        except Exception as e:
            print(f"❌ Error fetching virtual balances: {e}")
        return {}


# ==========================================
# SECTION 9: COMMAND /START & DEEP LINKING
# ==========================================
@bot.message_handler(commands=['start'])
def start(m):
    # Прямий file_id картинки-вітання
    welcome_photo = "AgACAgIAAxkBAAMCaZx5NT2dRW3qZjbYPx2ehwE4ieMAAkwSaxsyiulI5i_66QU9KA4BAAMCAAN5AAM6BA"
    chat_id = m.chat.id

    args = m.text.split()
    uid = args[1] if len(args) > 1 else None

    # Ініціалізація або скидання сесії
    user_sessions[chat_id] = {
        "uid": uid,
        "authorized": False,
        "lang": "en",
        "state": None,
        "active_trade": None
    }

    if not user_sessions[chat_id].get('authorized'):
        try:
            bot.set_chat_menu_button(chat_id=chat_id, menu_button=types.MenuButtonDefault())
        except:
            pass

    if uid:
        try:
            # Авторизація через веб-токен / UID (Deep Linking)
            user_record = auth.get_user(uid)
            user_doc = db.collection('users').document(uid).get().to_dict()
            name = user_doc.get('nickname') if user_doc else user_record.display_name or "User"

            user_sessions[chat_id]['state'] = 'waiting_for_password'

            welcome_back_text = (
                f"✨ **Welcome back, {name}!** ✨\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🔗 Your account is linked from the website.\n\n"
                f"🔐 Please enter your password to unlock access:"
            )
            bot.send_photo(chat_id, welcome_photo, caption=welcome_back_text, parse_mode="Markdown")

        except Exception as e:
            user_sessions[chat_id]['state'] = 'waiting_for_password'
            bot.send_photo(chat_id, welcome_photo, caption="🔐 **Security Check:** Please enter your password:",
                           parse_mode="Markdown")
    else:
        # Звичайний холодний старт бота
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🔑 Login", callback_data="auth_login"),
            types.InlineKeyboardButton("🆕 Register", callback_data="auth_register_choice")
        )

        main_start_text = (
            f"👋 **Welcome to P2P Bot!**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🛡️ The most secure way to trade crypto directly.\n\n"
            f"🚀 Do you have an account?"
        )
        bot.send_photo(chat_id, welcome_photo, caption=main_start_text, reply_markup=markup, parse_mode="Markdown")


# ==========================================
# SECTION 10: UNIVERSAL MEDIA HANDLER (CHATS & KYC)
# ==========================================
@bot.message_handler(content_types=['video', 'document', 'photo'])
def handle_media(m):
    chat_id = m.chat.id
    if chat_id not in user_sessions:
        # Безпечний перезапуск, якщо впав сервер або злетіла сесія
        user_sessions[chat_id] = {"uid": None, "authorized": False, "lang": "en", "state": None, "active_trade": None}

    s = user_sessions[chat_id]
    uid = s.get('uid')
    lang = s.get('lang', 'en')
    state = s.get('state')

    # Хелпер локалізації (динамічний або дефолтний провайдер)
    t = lambda k: get_text(lang, k) if 'get_text' in globals() else k

    # 1. Завантаження відео для PRO-Міграції аккаунта
    if state == 'waiting_mig_video':
        if m.content_type in ['video', 'document']:
            bot.send_message(chat_id, "⏳ **Processing file, please wait...**")
            file_id = m.video.file_id if m.content_type == 'video' else m.document.file_id

            try:
                # Зберігаємо Telegram file_id, оскільки прямі лінки застарівають через 1 годину!
                db.collection('users').document(uid).update({
                    'migrationStatus': 'pending',
                    'migrationLink': s.get('temp_mig_link', ''),
                    'migrationVideoFileId': file_id,
                    'subscriptionPlan': 'pro_monthly_100',
                    'trialStatus': 'pending_admin_approval',
                    'updatedAt': datetime.now(timezone.utc)
                })
                s['state'] = None
                bot.send_message(chat_id,
                                 "✅ **Video uploaded successfully!** An administrator will review your application shortly.")
            except Exception as e:
                bot.send_message(chat_id, f"❌ Firebase Update Error: {e}")
        else:
            bot.send_message(chat_id, "⚠️ Please upload a valid video file or document recording!")
        return

    # 2. Надсилання скріншотів/фото в активний P2P-чат угоди
    if s.get('active_trade'):
        if m.content_type == 'photo':
            try:
                file_id = m.photo[-1].file_id
                file_info = bot.get_file(file_id)
                img_url = f"https://api.telegram.org/file/bot{bot.token}/{file_info.file_path}"

                from google.cloud import firestore
                db.collection('orders').document(s.get('active_trade')).update({
                    "messages": firestore.ArrayUnion([{
                        "senderUid": uid or "telegram_bot",
                        "text": m.caption or "Sent a photo",
                        "imageUrl": img_url,
                        "timestamp": datetime.now(timezone.utc),
                        "isFromBot": True
                    }])
                })
                bot.send_message(chat_id, "✅ Photo attached to trade chat.")
            except Exception as e:
                bot.send_message(chat_id, f"❌ Chat Media Error: {e}")
        else:
            bot.send_message(chat_id, "⚠️ Only photos/screenshots are allowed in the trade chat.")
        return


# ==========================================
# SECTION 11: UNIVERSAL TEXT & MENU HANDLER
# ==========================================
@bot.message_handler(func=lambda m: True)
def handle_all(m):
    chat_id = m.chat.id
    text = m.text if m.text else ""

    # 1. Перевірка на існування сесії та ініціалізація базових змінних
    if chat_id not in user_sessions:
        user_sessions[chat_id] = {'lang': 'en', 'authorized': False, 'uid': None, 'state': None, 'active_trade': None}

    s = user_sessions[chat_id]
    lang = s.get('lang', 'en')
    t = lambda k: get_text(lang, k) if 'get_text' in globals() else k
    uid = s.get('uid')

    # 2. Завантажуємо дані користувача з Firestore (якщо є UID)
    user_doc = {}
    if uid:
        try:
            doc_ref = db.collection('users').document(uid).get()
            if doc_ref.exists:
                user_doc = doc_ref.to_dict()
        except Exception as e:
            print(f"Firestore error: {e}")

    # --- 3. ПРІОРИТЕТ: ОБРОБКА КНОПОК ГОЛОВНОГО МЕНЮ ---
    menu_buttons = [
        t('btn_app'), t('btn_wallet'), t('btn_swap'), t('btn_buy'), t('btn_sell'),
        t('btn_dashboard'), t('btn_support'), t('btn_settings'), t('btn_create_offer'),
        t('btn_profile'), t('btn_alerts'), "🌐 Social Media", "📰 News", "🌐 Community"
    ]

    if text in menu_buttons:
        # АВТО-ВІДМІНА: Якщо користувач був у процесі створення оголошення і натиснув меню
        if s.get('state') and str(s.get('state')).startswith('offer_'):
            s['state'], s['new_offer'] = None, {}
            bot.send_message(chat_id, "⚠️ **Action cancelled.** Switching menu...")

        # --- СОЦІАЛЬНІ МЕРЕЖІ ---
        if text == "🌐 Social Media":
            social_text = (
                "🌍 **Join our Community!**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Stay updated with the latest news, updates, and community events on our official channels:\n\n"
                "🐦 **X (Twitter):** [Follow us]\n"
                "📸 **Instagram:** [See our life]\n"
                "🎥 **YouTube:** [Watch tutorials]\n"
                "🎼 **TikTok:** [Fun content]\n"
                "👥 **Facebook:** [Connect with us]\n"
                "💼 **LinkedIn:** [Professional news]\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "🛡️ *Trade safe, trade everywhere!*"
            )

            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                types.InlineKeyboardButton("🐦 X", url="url"),
                types.InlineKeyboardButton("📸 Instagram", url="url"),
                types.InlineKeyboardButton("🎥 YouTube", url="url"),
                types.InlineKeyboardButton("🎼 TikTok", url="url"),
                types.InlineKeyboardButton("👥 Facebook", url="url"),
                types.InlineKeyboardButton("💼 LinkedIn", url="url")
            )
            bot.send_message(chat_id, social_text, reply_markup=markup, parse_mode="Markdown",
                             disable_web_page_preview=True)
            return

        # --- ЦІНОВІ АЛЕРТИ ---
        elif text == t('btn_alerts'):
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton("➕ Create New Alert", callback_data="al_create_start"),
                types.InlineKeyboardButton("📋 My Active Alerts", callback_data="al_list_my")
            )
            bot.send_message(chat_id,
                             "🔔 **Price Alerts Management**\n\nYou can set up notifications for specific prices or manage your current subscriptions.",
                             reply_markup=markup, parse_mode="Markdown")
            return

        # --- ПРОФІЛЬ КОРИСТУВАЧА ---
        elif text == t('btn_profile'):
            if not uid:
                bot.send_message(chat_id, "❌ **Please log in first.**")
                return
            if 'show_user_profile' in globals():
                show_user_profile(chat_id, uid, lang)
            else:
                bot.send_message(chat_id, "⚙️ Profile controller missing.")
            return

        # --- НОВИНИ ПЛАТФОРМИ ---
        elif text == "📰 News":
            try:
                from google.cloud import firestore
                news_snap = db.collection('news').order_by('createdAt', direction=firestore.Query.DESCENDING).limit(
                    4).get()

                if not news_snap:
                    bot.send_message(chat_id, "📭 **No news published yet.**")
                    return

                all_news = [n.to_dict() for n in news_snap]
                top_news = next((n for n in all_news if n.get('isTop')), all_news[0])
                other_news = [n for n in all_news if n != top_news]

                date_top = top_news.get('createdAt')
                date_top_str = date_top.strftime('%d %b %Y') if date_top else "Recent"

                msg = (
                    f"🟢 **TOP UPDATE**\n"
                    f"_{date_top_str}_\n\n"
                    f"🔥 **{top_news.get('title', '').upper()}**\n"
                    f"{top_news.get('text', '')}\n\n"
                )

                if other_news:
                    msg += "━━━━━━━━━━━━━━━━━━━━\n"
                    msg += "📜 **RECENT UPDATES**\n\n"
                    for idx, n in enumerate(other_news, 1):
                        date_n = n.get('createdAt')
                        date_n_str = date_n.strftime('%d %b %Y') if date_n else "Recent"
                        msg += f"{idx}. **{n.get('title')}**\n_{date_n_str}_\n{n.get('text')}\n\n"

                markup = types.InlineKeyboardMarkup()
                markup.add(
                    types.InlineKeyboardButton("🌐 View all on Website", url="url"))
                bot.send_message(chat_id, msg, reply_markup=markup, parse_mode="Markdown")
            except Exception as e:
                print(f"❌ Error rendering news feed: {e}")
                bot.send_message(chat_id, "⚠️ Error loading news. Please try again later.")
            return

        # --- СПІЛЬНОТА / СТРІЧКА ---
        elif text == "🌐 Community":
            if 'show_community_page' in globals():
                show_community_page(chat_id, uid)
            else:
                bot.send_message(chat_id, "⚙️ Community module missing.")
            return

        # --- КУПІВЛЯ / ПРОДАЖ (P2P ФІЛЬТРИ) ---
        elif text == t('btn_buy') or text == t('btn_sell'):
            s['filter'] = {
                'type': 'sell' if text == t('btn_buy') else 'buy',
                'asset': None, 'country': 'All Countries', 'method': 'All Methods', 'fiat': 'USD'
            }
            markup = types.InlineKeyboardMarkup(row_width=2).add(
                *[types.InlineKeyboardButton(a, callback_data=f"f_asset_{a}") for a in
                  ["USDT TRC20", "USDT ERC20", "USDC SOL", "BTC", "ETH", "BNB", "TRX", "SOL"]]
            )
            bot.send_message(chat_id, "💎 **Step 1: Select Asset to trade:**", reply_markup=markup)
            return

        # --- Secure Гаманець ---
        elif text == t('btn_wallet'):
            if not uid:
                bot.send_message(chat_id, "❌ **Log in to view wallet.**")
                return
            bot.send_message(chat_id, "⏳ **Checking balances...**")

            if 'get_wallet_balances' in globals():
                data = get_wallet_balances(user_doc.get('walletMnemonic'), uid)
                if data:
                    xmr_display = data.get('xmr_bal', '0.0000')
                    if "XMR" not in str(xmr_display):
                        xmr_display = f"{xmr_display} XMR"

                    msg = (
                        f"💳 **Your Secure Wallet**\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🟠 **Bitcoin:**\n"
                        f"Balance: `{data.get('btc_bal', 0.0):.8f} BTC`\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🟡 **Binance Smart Chain:**\n"
                        f"Balance: `{data.get('bnb_bal', 0.0):.4f} BNB`\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🔴 **TRON Network (TRC20):**\n"
                        f"USDT: `{data.get('usdt_trc', 0.0):.2f}` | TRX: `{data.get('trx_bal', 0.0):.2f}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🔹 **ETH Network (ERC20):**\n"
                        f"USDT: `{data.get('usdt_erc', 0.0):.2f}` | ETH: `{data.get('eth_bal', 0.0):.6f}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"☀️ **Solana Network:**\n"
                        f"SOL: `{data.get('sol_bal', 0.0):.4f}` | USDC: `{data.get('sol_usdc', 0.0):.2f}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🌑 **Monero (Stagenet):**\n"
                        f"Balance: `{xmr_display}`\n"
                    )
                    if data.get('virtual_others'):
                        for item in data['virtual_others']:
                            msg += f"━━━━━━━━━━━━━━━━━━━━\n💎 **{item['name']} ({item['net']}):**\nBalance: `{item['val']}`\n"
                    bot.send_message(chat_id, msg, parse_mode="Markdown")
            else:
                bot.send_message(chat_id, "⚙️ Wallet balance module missing.")
            return

        # --- ПАНЕЛЬ АКТИВНИХ УГОД ---
        elif text == t('btn_dashboard'):
            if not uid:
                bot.send_message(chat_id, "❌ **Log in first.**")
                return
            if 'get_active_trades_markup' in globals():
                markup, count = get_active_trades_markup(uid, lang)
                if markup:
                    bot.send_message(chat_id, f"📊 **Active Trades: {count}**", reply_markup=markup)
                else:
                    bot.send_message(chat_id, "📭 **No active trades found.**")
            return

        # --- СТВОРЕННЯ ОГОЛОШЕННЯ (P2P OFFER) ---
        elif text == t('btn_create_offer'):
            if not uid:
                bot.send_message(chat_id, "❌ **Please log in first.**")
                return

            kyc_status = user_doc.get('kycStatus', 'none')
            sub_plan = user_doc.get('subscriptionPlan', 'free')
            trial_status = user_doc.get('trialStatus', 'none')

            if (kyc_status == 'approved') or (sub_plan == 'pro_monthly_100'):
                s['state'] = 'offer_type'
                markup = types.InlineKeyboardMarkup(row_width=2)
                markup.add(types.InlineKeyboardButton("💰 Sell", callback_data="off_type_sell"),
                           types.InlineKeyboardButton("🛒 Buy", callback_data="off_type_buy"))
                markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="off_cancel"))
                bot.send_message(chat_id, "➕ **Create Offer**\nSelect operation type:", reply_markup=markup)
            else:
                if trial_status == 'pending_admin_approval':
                    bot.send_message(chat_id,
                                     "⏳ **Your PRO Migration is under review.**\nYou will be able to create offers as soon as an admin verifies your video.")
                    return

                blockpass_url = f"url"
                markup = types.InlineKeyboardMarkup(row_width=1)
                markup.add(
                    types.InlineKeyboardButton("🛡️ Verify Identity", url=blockpass_url),
                    types.InlineKeyboardButton("👑 Get PRO (Migration)", callback_data="pro_migration_start")
                )
                bot.send_message(chat_id,
                                 "⚠️ **Verification Required**\n\nTo create offers, you need to verify your identity OR apply for **PRO Migration** (7 days free).",
                                 reply_markup=markup)
            return

        # --- AI ПІДТРИМКА ---
        elif text == t('btn_support'):
            s['state'] = 'support'
            bot.send_message(chat_id, "🆘 **AI Support ON.** Type your question:")
            return

        # --- НАЛАШТУВАННЯ ---
        elif text == t('btn_settings'):
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🚪 Log Out", callback_data="logout_warning"))
            bot.send_message(chat_id, "⚙️ **Settings:**", reply_markup=markup)
            return

        # --- ВНУТРІШНІЙ СВОП ---
        elif text == t('btn_swap'):
            if not uid:
                bot.send_message(chat_id, "❌ **Log in first.**")
                return
            if 'swap' not in s or not s['swap']:
                s['swap'] = {
                    'from_asset': 'USDT TRC20', 'to_asset': 'SOL',
                    'from_amount': 0.0, 'to_amount': 0.0,
                    'rate': None, 'error': None, 'min_amount': 0.0
                }
            if 'show_swap_menu' in globals():
                show_swap_menu(chat_id, s)
            return

    # --- 4. ЛОГІКА ДЛЯ СТЕЙТІВ ТА НЕАВТОРИЗОВАНИХ (РЕЄСТРАЦІЯ / ВХІД / ЧАТ) ---
    state = s.get('state')

    if not s.get("authorized"):
        # Крок 1 реєстрації: Очікування Email
        if state == 'reg_waiting_email':
            email = text.strip().lower()
            if 'is_valid_email' in globals() and not is_valid_email(email):
                bot.send_message(chat_id, "❌ **Invalid Email format!**")
                return
            try:
                auth.get_user_by_email(email)
                bot.send_message(chat_id, "⚠️ **Email already registered!**")
            except Exception:
                s['reg_email'] = email
                s['state'] = 'reg_waiting_pass'
                bot.send_message(chat_id, "🔐 **Email accepted!** Now set a password (min 6 chars):")
            return

        # Крок 2 реєстрації: Очікування Паролю
        elif state == 'reg_waiting_pass':
            if len(text) < 6:
                bot.send_message(chat_id, "❌ **Password too weak (min 6 chars).** Try again:")
                return
            try:
                bot.delete_message(chat_id, m.message_id)
            except:
                pass
            s['reg_pass'] = text
            s['state'] = 'reg_waiting_nickname'
            bot.send_message(chat_id, "👤 **Great!** Now choose your Nickname (max 15 chars):")
            return

        # Крок 3 реєстрації: Очікування Нікнейму
        elif state == 'reg_waiting_nickname':
            nickname = text.strip()
            if len(nickname) < 3 or len(nickname) > 15:
                bot.send_message(chat_id, "❌ **Nickname must be 3-15 characters.** Try again:")
                return
            s['reg_nickname'] = nickname
            s['state'] = 'reg_select_region'

            regions = ["Europe", "Americas", "Asia", "Africa", "Oceania"]
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.row(
                types.InlineKeyboardButton("🇺🇸 United States", callback_data="reg_set_ct_United States"),
                types.InlineKeyboardButton("🇬🇧 United Kingdom", callback_data="reg_set_ct_United Kingdom")
            )
            for reg in regions:
                markup.add(types.InlineKeyboardButton(reg, callback_data=f"reg_reg_{reg}"))
            markup.add(types.InlineKeyboardButton("🔍 Custom Search (Text)", callback_data="reg_ct_search"))

            bot.send_message(chat_id, "🌍 **Almost done! Select your Region or Search:**", reply_markup=markup)
            return

        # Вхід з Deep-Linking: Очікування паролю для розблокування
        elif state == 'waiting_for_password':
            # Сюди додати вашу перевірку паролю через auth/firestore під час логіну
            pass

    # ==========================================
    # SECTION 12: STATE MACHINE (TEXT INPUTS)
    # ==========================================

    # 1. Пошук країни за текстом під час реєстрації
    if state == 'search_reg_country':
        query = text.strip().lower()
        if 'get_countries_data' in globals():
            all_countries = get_countries_data()
            matches = [c for c in all_countries if query in c['name'].lower()]

            if len(matches) == 1:
                s['temp_country_data'] = matches[0]
                if 'register_user_in_firebase' in globals():
                    register_user_in_firebase(chat_id, s)
                else:
                    bot.send_message(chat_id, "✅ Country matched. Registration helper missing.")
            elif len(matches) > 1:
                markup = types.InlineKeyboardMarkup()
                for c in matches[:5]:
                    markup.add(types.InlineKeyboardButton(f"{c['name']} ({c['fiat']})",
                                                          callback_data=f"reg_set_ct_{c['name']}"))
                bot.send_message(chat_id, "📍 **Select your exact country:**", reply_markup=markup)
            else:
                bot.send_message(chat_id, "❌ **Country not found.** Try again (e.g., Poland, Ukraine):")
        return

    # 2. Вхід (Логін): Очікування Email
    elif state == 'waiting_for_email':
        s['temp_email'] = text.strip()
        s['state'] = 'waiting_for_password'
        bot.send_message(chat_id, "🔐 **Password required:**")
        return

    # 3. Вхід (Логін / Deep Link Linkage): Перевірка Паролю через REST API
    elif state == 'waiting_for_password':
        import urllib.parse
        import requests

        email = s.get('temp_email')
        if not email and s.get('uid'):
            try:
                user_rec = auth.get_user(s['uid'])
                email = user_rec.email
            except:
                pass

        password = text

        try:
            bot.delete_message(chat_id, m.message_id)
        except:
            pass

        if not email:
            bot.send_message(chat_id, "❌ **Error:** Email session lost. Please use `/start` again.")
            s['state'] = None
            return

        try:
            auth_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_WEB_API_KEY}"
            r = requests.post(auth_url, json={
                "email": email,
                "password": password,
                "returnSecureToken": True
            }, timeout=10)

            if r.status_code == 200:
                res_data = r.json()
                new_uid = res_data['localId']

                s.update({
                    "authorized": True,
                    "uid": new_uid,
                    "email": email,
                    "password": password,
                    "state": None
                })

                db.collection('users').document(new_uid).update({'tg_id': chat_id})

                encoded_email = urllib.parse.quote(email)
                encoded_pass = urllib.parse.quote(password)
                web_app_url = f"url"

                bot.set_chat_menu_button(chat_id=chat_id, menu_button=types.MenuButtonWebApp(
                    type="web_app", text="🚀 Open App", web_app=types.WebAppInfo(url=web_app_url)
                ))

                bot.send_message(chat_id, f"✅ **Login successful!** Welcome back to our platform.")
                if 'show_bottom_menu' in globals():
                    show_bottom_menu(chat_id, lang)
            else:
                bot.send_message(chat_id, "❌ **Incorrect password.** Please try again:")
        except Exception as e:
            print(f"Критична помилка авторизації: {e}")
            bot.send_message(chat_id, "❌ **Auth system temporary offline.** Try again later.")
        return

    # 4. АКТИВНИЙ P2P ЧАТ КЛІЄНТ-ТРЕЙДЕР
    if s.get('active_trade'):
        if text.lower() in ["exit", "вихід", "🚫 leave chat mode"]:
            s['active_trade'] = None
            s['state'] = None
            bot.send_message(chat_id, "📴 **Chat mode disabled.** Returning to main menu...",
                             reply_markup=types.ReplyKeyboardRemove())
            if 'show_bottom_menu' in globals():
                show_bottom_menu(chat_id, lang)
            return

        try:
            from google.cloud import firestore
            db.collection('orders').document(s.get('active_trade')).update({
                "messages": firestore.ArrayUnion([{
                    "senderUid": uid or "telegram_bot",
                    "text": text,
                    "timestamp": datetime.now(timezone.utc),
                    "isFromBot": True
                }])
            })
            bot.send_message(chat_id, "✅ **Sent!**")
        except Exception as e:
            bot.send_message(chat_id, f"❌ Chat Error: {e}")
        return

    # 5. Очікування суми для Внутрішнього Свопу (Swap)
    if state == 'waiting_for_swap_amount':
        try:
            amount = float(text.replace(',', '.'))
            try:
                bot.delete_message(chat_id, m.message_id)
            except:
                pass

            if s.get('last_ask_msg_id'):
                try:
                    bot.delete_message(chat_id, s['last_ask_msg_id'])
                except:
                    pass

            if amount <= 0:
                raise ValueError

            s['swap']['from_amount'] = amount
            s['state'] = None

            if 'show_swap_menu' in globals():
                show_swap_menu(chat_id, s, s.get('swap_msg_id'))
        except ValueError:
            msg = bot.send_message(chat_id, "❌ Please enter a valid positive number.")
            s['last_ask_msg_id'] = msg.message_id
        return

    # 6. Цінові сповіщення (Пошук валюти)
    elif state == 'al_fiat_search_state':
        fiat_code = text.strip().upper()
        if len(fiat_code) != 3:
            bot.send_message(chat_id, "❌ **3-letter currency ISO code required (e.g. USD).**")
            return
        s['new_alert']['fiat'] = fiat_code
        s['state'] = 'waiting_alert_price'
        type_text = "BUYING" if s['new_alert']['type'] == "buy" else "SELLING"
        bot.send_message(chat_id,
                         f"✅ Currency: *{fiat_code}*\n\n🔢 Enter your target **Price** in {fiat_code} for {type_text} {s['new_alert']['asset']}:",
                         parse_mode="Markdown")
        return

    # 7. Цінові сповіщення (Введення тригерної ціни)
    elif state == 'waiting_alert_price':
        try:
            target_price = float(text.replace(',', '.'))
            if target_price <= 0:
                raise ValueError

            alert_data = {
                'uid': uid,
                'tg_id': chat_id,
                'asset': s['new_alert']['asset'],
                'type': s['new_alert']['type'],
                'country': s['new_alert']['country'],
                'fiat': s['new_alert']['fiat'],
                'target_price': target_price,
                'is_active': True,
                'createdAt': datetime.now(timezone.utc)
            }

            db.collection('price_alerts').add(alert_data)
            s['state'] = None
            s.pop('new_alert', None)

            type_emoji = "🛒" if alert_data['type'] == "buy" else "💰"
            bot.send_message(chat_id,
                             f"✅ **Alert set successfully!**\n\n{type_emoji} I will ping you as soon as there is an offer at **{target_price} {alert_data['fiat']}** or better for **{alert_data['asset']}**.",
                             parse_mode="Markdown")
            if 'show_bottom_menu' in globals():
                show_bottom_menu(chat_id, lang)
        except Exception:
            bot.send_message(chat_id, "❌ **Invalid amount!** Please enter a valid price number (e.g., 41.20):")
        return

    # 8. PRO Міграція: Прийняття посилання на профіль
    elif state == 'waiting_mig_link':
        link = text.strip()
        if not link.startswith(('http://', 'https://')):
            link = 'https://' + link
        s['temp_mig_link'] = link
        s['state'] = 'waiting_mig_video'
        bot.send_message(chat_id,
                         t('mig_video_msg') if 'get_text' in globals() else "📹 Please upload your video proof now:")
        return

    # 9. Текстова заглушка для PRO Міграції
    elif state == 'waiting_mig_video':
        bot.send_message(chat_id,
                         "⚠️ **Video file required!** Please upload or record a video message instead of typing text.")
        return

    # 10. Редагування оголошення: Зміна ціни
    elif state == 'edit_ad_price':
        try:
            new_price = float(text.replace(',', '.'))
            if new_price <= 0:
                raise ValueError
            db.collection('ads').document(s['edit_ad_id']).update({'price': new_price})
            s['state'], s['edit_ad_id'] = None, None
            bot.send_message(chat_id, t('ad_updated') if 'get_text' in globals() else "✅ Offer updated!")
            if 'show_user_profile' in globals():
                show_user_profile(chat_id, uid, lang)
        except ValueError:
            bot.send_message(chat_id, "❌ Invalid number format.")
        return

    # 11. Редагування оголошення: Зміна лімітів у форматі "Мін - Макс"
    elif state == 'edit_ad_limits':
        try:
            min_l, max_l = map(float, text.replace(' ', '').replace(',', '.').split('-'))
            db.collection('ads').document(s['edit_ad_id']).update({'limits': {'min': min_l, 'max': max_l}})
            s['state'], s['edit_ad_id'] = None, None
            bot.send_message(chat_id, t('ad_updated') if 'get_text' in globals() else "✅ Limits updated!")
            if 'show_user_profile' in globals():
                show_user_profile(chat_id, uid, lang)
        except Exception:
            bot.send_message(chat_id, "⚠️ Invalid format! Use: `100 - 5000`")
        return

    # 12. AI ПІДТРИМКА (Контекстний чат з нейромережею)
    elif state == 'support':
        if text.lower() in ["exit", "вихід", "🚫 leave chat mode"]:
            s['state'] = None
            if 'ai_chat_sessions' in globals() and chat_id in ai_chat_sessions:
                del ai_chat_sessions[chat_id]
            bot.send_message(chat_id, "📴 **AI Support session closed.**")
            if 'show_bottom_menu' in globals():
                show_bottom_menu(chat_id, lang)
        else:
            bot.send_chat_action(chat_id, 'typing')
            try:
                # Чат підтримується через глобальну сесію ai_chat_sessions
                if 'ai_chat_sessions' in globals() and chat_id in ai_chat_sessions:
                    chat_session = ai_chat_sessions[chat_id]
                    response = chat_session.send_message(text)
                    bot.send_message(chat_id, response.text, parse_mode="Markdown")
                else:
                    bot.send_message(chat_id,
                                     "🤖 *AI Engine Warm-up:* I lost the context. Please toggle support menu again.",
                                     parse_mode="Markdown")
            except Exception as e:
                error_str = str(e)
                if "429" in error_str:
                    bot.send_message(chat_id,
                                     "⚠️ **System Overloaded.** Too many requests right now. Please try again in 1 minute.")
                else:
                    bot.send_message(chat_id, "🤖 Sorry, I need a moment to reboot. Please repeat your question.")
        return

    # ==========================================
    # SECTION 13: STATE MACHINE (COMPLETION)
    # ==========================================

    # 1. Публікація коментарів у Спільноті
    if state == 'writing_community_comment':
        if text == "❌ Cancel Comment":
            s['state'], s['active_post_id'] = None, None
            bot.send_message(chat_id, "🚫 **Comment cancelled.**")
            if 'show_bottom_menu' in globals():
                show_bottom_menu(chat_id, lang)
            return

        post_id = s.get('active_post_id')
        nickname = user_doc.get('nickname', 'User')

        new_comment = {
            'id': str(int(datetime.now(timezone.utc).timestamp() * 1000)),
            'authorUid': uid,
            'authorName': f"@{nickname}",
            'text': text,
            'createdAt': datetime.now(timezone.utc)
        }

        try:
            from google.cloud import firestore
            db.collection('community_posts').document(post_id).update({
                'comments': firestore.ArrayUnion([new_comment])
            })
            s['state'], s['active_post_id'] = None, None
            bot.send_message(chat_id, "✅ **Comment published!**")
            if 'show_bottom_menu' in globals():
                show_bottom_menu(chat_id, lang)
        except Exception as e:
            bot.send_message(chat_id, f"❌ Error: {e}")
        return

    # 2. Текстовий відгук (Review) після завершення угоди
    elif state == 'writing_review_text':
        if 'save_final_review' in globals():
            save_final_review(chat_id, text)
        else:
            bot.send_message(chat_id, "⚙️ Review storage handler missing.")
        return

    # 3. Створення оголошення: Введення ціни
    elif state == 'offer_price':
        try:
            s['new_offer']['price'] = float(text.replace(',', '.'))
            s['state'] = 'offer_total_amount'
            bot.send_message(chat_id, f"📊 **Total Volume:** How much {s['new_offer'].get('asset', 'crypto')} in total?")
        except ValueError:
            bot.send_message(chat_id, "❌ Enter a valid number:")
        return

    # 4. Створення оголошення: Загальний об'єм ліквідності
    elif state == 'offer_total_amount':
        try:
            s['new_offer']['totalAmount'] = float(text.replace(',', '.'))
            s['state'] = 'offer_limits'
            bot.send_message(chat_id, f"🔢 **Enter limits in {s['new_offer'].get('fiat', 'USD')}** (e.g. 500-5000):")
        except ValueError:
            bot.send_message(chat_id, "❌ Enter a valid volume number:")
        return

    # 5. Створення оголошення: Межі/Ліміти угоди
    elif state == 'offer_limits':
        try:
            min_l, max_l = map(float, text.replace(' ', '').replace(',', '.').split('-'))
            s['new_offer']['limits'] = {'min': min_l, 'max': max_l}
            s['state'] = 'offer_payment'

            markup = types.InlineKeyboardMarkup(row_width=1)
            payment_structure = globals().get('PAYMENT_STRUCTURE', {})
            for cat in payment_structure.keys():
                if "Gift Cards" not in cat:
                    markup.add(types.InlineKeyboardButton(cat, callback_data=f"pay_cat_{cat}"))
            bot.send_message(chat_id, "🏦 **Select Category:**", reply_markup=markup)
        except Exception:
            bot.send_message(chat_id, "❌ Invalid format. Use: 500-5000")
        return

    # 6. Пошук країни для оголошення
    elif state == 'search_country':
        query = text.strip().lower()
        if 'get_countries_data' in globals():
            matches = [c for c in get_countries_data() if query in c['name'].lower()]
            if matches:
                markup = types.InlineKeyboardMarkup()
                for c in matches[:5]:
                    markup.add(types.InlineKeyboardButton(f"{c['name']} ({c['fiat']})",
                                                          callback_data=f"off_set_ct_{c['name']}"))
                bot.send_message(chat_id, "📍 **Select result:**", reply_markup=markup)
            else:
                bot.send_message(chat_id, "❌ Not found. Try again:")
        return

    # 7. Створення P2P ордера: Введення суми угоди користувачем
    elif state == 'waiting_for_trade_amount':
        try:
            amount = float(text.replace(',', '.'))
            ad_data = s.get('active_ad_data', {})
            limits = ad_data.get('limits', {})

            if amount < limits.get('min', 0) or amount > limits.get('max', 999999999):
                bot.send_message(chat_id,
                                 f"❌ **Amount out of limits!**\nPlease enter between {limits.get('min')} and {limits.get('max')} {ad_data.get('fiat')}:")
                return

            if 'create_order_in_db' in globals():
                order_id = create_order_in_db(s, ad_data, s.get('active_ad_id'), amount)
                if order_id:
                    s['state'] = None
                    bot.send_message(chat_id,
                                     f"✅ **Order #{order_id} created!**\nNotification sent to the partner. Use `/dashboard` to manage.")

                    order_doc = db.collection('orders').document(order_id).get().to_dict()
                    if ad_data.get('type') == 'sell':
                        if 'notify_buyer' in globals(): notify_buyer(order_id, order_doc, force_send=True)
                    else:
                        if 'notify_seller' in globals(): notify_seller(order_id, order_doc, force_send=True)
                else:
                    bot.send_message(chat_id, "❌ Error creating order in database.")
            else:
                bot.send_message(chat_id, "⚙️ Order creator function is missing.")
        except ValueError:
            bot.send_message(chat_id, "❌ Please enter a valid number (e.g. 500.50):")
        return

    # 8. Створення оголошення: Пошук Фіату за кодом
    elif state == 'search_fiat':
        fiat_code = text.strip().upper()
        if len(fiat_code) != 3:
            bot.send_message(chat_id, "❌ Enter 3-letter ISO code (e.g. PLN):")
            return
        s['new_offer']['fiat'] = fiat_code
        markup = types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton(f"Confirm {fiat_code}", callback_data=f"off_set_fiat_{fiat_code}"),
            types.InlineKeyboardButton("⬅️ Back", callback_data="off_fiat_search")
        )
        bot.send_message(chat_id, f"✅ Use **{fiat_code}**?", reply_markup=markup)
        return

    # 9. Пошук валюти для фільтра маркету
    elif state == 'search_filter_fiat':
        fiat_code = text.strip().upper()
        if len(fiat_code) != 3:
            bot.send_message(chat_id, "❌ **3-letter code required.**")
            return
        s['filter']['fiat'] = fiat_code
        s['state'] = None

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("💳 All Methods", callback_data="f_pm_All Methods"))
        payment_structure = globals().get('PAYMENT_STRUCTURE', {})
        for cat in payment_structure.keys():
            markup.add(types.InlineKeyboardButton(cat, callback_data=f"f_cat_{cat}"))
        bot.send_message(chat_id, f"✅ Currency: {fiat_code}\n🏦 **Step 4: Select Category:**", reply_markup=markup)
        return


# ==========================================
# SECTION 14: FIREBASE BACKGROUND LISTENERS
# ==========================================

def broadcast_news(news_data):
    """Розсилає новину всім користувачам, які не вимкнули сповіщення в Firebase"""
    title = news_data.get('title', 'Platform Update')
    text = news_data.get('text', '')
    is_top = news_data.get('isTop', False)

    header = "🌟 **TOP UPDATE** 🌟\n" if is_top else "📢 **News**\n"
    full_message = f"{header}━━━━━━━━━━━━━━━━━━━━\n**{title}**\n\n{text}\n━━━━━━━━━━━━━━━━━━━━"

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🌐 Read on Website", url="url"))

    try:
        users_ref = db.collection('users').get()
        count = 0
        for user_doc in users_ref:
            u_data = user_doc.to_dict()
            tg_id = u_data.get('tg_id')
            if not tg_id:
                continue

            notifs = u_data.get('notifications', {})
            if notifs.get('telegram') is False:
                continue

            try:
                bot.send_message(tg_id, full_message, reply_markup=markup, parse_mode="Markdown")
                count += 1
            except Exception:
                pass
        print(f"✅ News broadcasted to {count} users.")
    except Exception as e:
        print(f"❌ Broadcast error: {e}")


def start_news_listener():
    is_init = [True]

    def on_snapshot(col, changes, time_read):
        if is_init[0]:
            is_init[0] = False
            return
        for ch in changes:
            if ch.type.name == 'ADDED':
                news_data = ch.document.to_dict()
                print(f"🆕 New news detected in Firebase: {news_data.get('title')}")
                threading.Thread(target=broadcast_news, args=(news_data,), daemon=True).start()

    db.collection('news').on_snapshot(on_snapshot)


def start_listener():
    is_init = [True]

    def on_snapshot(col, changes, time_read):
        if is_init[0]:
            is_init[0] = False
            return

        for ch in changes:
            if ch.type.name in ['ADDED', 'MODIFIED']:
                if 'notify_seller' in globals(): notify_seller(ch.document.id, ch.document.to_dict())
                if 'notify_buyer' in globals(): notify_buyer(ch.document.id, ch.document.to_dict())

    db.collection('orders').on_snapshot(on_snapshot)


user_kyc_cache = {}
user_mig_cache = {}


def start_users_listener():
    """Слухає зміни статусів верифікації KYC та PRO-Міграції аккаунтів"""
    is_init = [True]

    def on_snapshot(col, changes, time_read):
        if is_init[0]:
            for doc in col:
                data = doc.to_dict()
                user_kyc_cache[doc.id] = data.get('kycStatus', 'none')
                user_mig_cache[doc.id] = data.get('migrationStatus', 'none')
            is_init[0] = False
            return

        for ch in changes:
            if ch.type.name in ['ADDED', 'MODIFIED']:
                doc_data = ch.document.to_dict()
                uid = ch.document.id
                tg_id = doc_data.get('tg_id')
                if not tg_id:
                    continue

                lang = user_sessions.get(tg_id, {}).get('lang', 'en')
                t = lambda k: get_text(lang, k) if 'get_text' in globals() else k

                # Перевірка KYC Статусу
                new_kyc = doc_data.get('kycStatus', 'none')
                if uid in user_kyc_cache and new_kyc != user_kyc_cache[uid]:
                    user_kyc_cache[uid] = new_kyc
                    msg = t('kyc_approved_msg') if new_kyc == 'approved' else t('kyc_rejected_msg')
                    try:
                        bot.send_message(tg_id, msg)
                    except:
                        pass

                # Перевірка статусу PRO Міграції
                new_mig = doc_data.get('migrationStatus', 'none')
                if uid in user_mig_cache and new_mig != user_mig_cache[uid]:
                    user_mig_cache[uid] = new_mig
                    if new_mig == 'approved':
                        db.collection('users').document(uid).update({
                            'proActivatedAt': datetime.now(timezone.utc).isoformat()
                        })
                        try:
                            bot.send_message(tg_id, t('mig_approved_msg'))
                        except:
                            pass

    db.collection('users').on_snapshot(on_snapshot)


# ==========================================
# SECTION 15: ADS ALERTS & BOT INITIALIZATION
# ==========================================

def start_ads_listener():
    """Слухає появу нових оголошень в базі і перевіряє алерти"""
    is_init = [True]

    def on_snapshot(col, changes, time_read):
        if is_init[0]:
            is_init[0] = False
            return

        for ch in changes:
            # Перевіряємо тільки НОВІ оголошення
            if ch.type.name == 'ADDED':
                new_ad_data = ch.document.to_dict()
                ad_id = ch.document.id

                # Тільки активні оголошення перевіряються
                if new_ad_data.get('status') == 'active':
                    print(f"🆕 New AD detected in DB: {ad_id}. Checking alerts...")
                    if 'check_and_notify_price_alerts' in globals():
                        threading.Thread(
                            target=check_and_notify_price_alerts,
                            args=(new_ad_data, ad_id),
                            daemon=True
                        ).start()

    db.collection('ads').on_snapshot(on_snapshot)


# --- START BOT ENTRYPOINT ---
if __name__ == "__main__":
    print("⏳ Initializing background Firebase listeners...")

    # Запускаємо всі чотири лісенери в окремих фонових потоках
    threading.Thread(target=start_users_listener, daemon=True).start()
    threading.Thread(target=start_news_listener, daemon=True).start()
    threading.Thread(target=start_listener, daemon=True).start()
    threading.Thread(target=start_ads_listener, daemon=True).start()

    # Конфігурація та перевірка XMR гаманця платформи
    test_mnemonic = ""

    if 'get_xmr_wallet_data' in globals():
        try:
            mainnet_wallet = get_xmr_wallet_data(test_mnemonic, is_testnet=False)
            if mainnet_wallet:
                print("\n🚀 --- XMR MAINNET WALLET READY ---")
                print(f"MAINNET ADDR: {mainnet_wallet['address']}")

                if 'get_xmr_mainnet_balance' in globals():
                    bal = get_xmr_mainnet_balance(mainnet_wallet['address'], mainnet_wallet['view_key'])
                    print(f"🧪 [XMR TEST] Поточний баланс гаманця: {bal} XMR\n")
        except Exception as xmr_err:
            print(f"⚠️ Monero wallet check skipped or node offline: {xmr_err}")

    # Відновлення моніторингу транзакцій блокчейну
    if 'resume_blockchain_monitoring' in globals():
        try:
            resume_blockchain_monitoring()
            print("⛓️ Blockchain monitoring successfully resumed.")
        except Exception as b_err:
            print(f"❌ Failed to resume blockchain monitoring: {b_err}")

    # Запуск Flask веб-сервера для утримання порту на хостингу (Health Checks)
    if 'app' in globals():
        port = int(os.environ.get("PORT", 8080))
        threading.Thread(
            target=lambda: app.run(host='0.0.0.0', port=port, use_reloader=False),
            daemon=True
        ).start()
        print(f"🌐 Web health-check server started on port {port}")

    # Скидання активних вебхуків перед початком Polling
    try:
        bot.remove_webhook()
    except Exception as wh_err:
        print(f"⚠️ Webhook remove warning: {wh_err}")

    print("🤖Bot is successfully starting polling mode...")

    # Запуск безкінечного циклу обробки апдейтів Telegram
    bot.infinity_polling(timeout=10, long_polling_timeout=5)