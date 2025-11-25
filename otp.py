import os
import time
import uuid
import threading
import requests
import telebot
from telebot import types
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- 1. KONFIGURASI DARI .ENV ---
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY = os.getenv("SMSHUB_API_KEY")
ALLOWED_USERS = [int(x) for x in os.getenv("ALLOWED_USERS", "").split(",") if x]

TARGET_COUNTRY = "6"      # Indonesia
TARGET_SERVICE = "asy"    # Fore Coffee
TARGET_PRICE = "0.0181"   # Max Harga

# Header Template Fore
BASE_HEADERS_TEMPLATE = {
    'Host': 'api.fore.coffee', 'language': 'id',
    'User-Agent': 'Fore Coffee/4.11.0 (coffee.fore.fore; build:1577; iOS 18.5.0) Alamofire/5.10.2',
    'sentry-trace': '82f1ea8728ec4d9f98e6c380e9ee3e74-0b525eb3ee734332-0',
    'country-id': '1', 'platform': 'ios', 'Connection': 'keep-alive',
    'appsflyer-id': '1759206300240-5775732', 'Accept-Language': 'en-ID;q=1.0, id-ID;q=0.9',
    'timezone': '+07:00', 'jailbroken': '0', 'device-model': 'iPhone 12',
    'Accept': '*/*', 'app-version': '4.11.0', 'os-version': '18.5',
    'Content-Type': 'application/json',
}

CREDENTIALS = [
    {'secret-key': os.getenv('SECRET_KEY_1'), 'push-token': os.getenv('PUSH_TOKEN_1')},
    {'secret-key': os.getenv('SECRET_KEY_2'), 'push-token': os.getenv('PUSH_TOKEN_2')}
]

bot = telebot.TeleBot(BOT_TOKEN)
manual_stops = {} 

# --- 2. HTTP SESSION & SAFE TELEGRAM (ANTI CRASH) ---

def get_session():
    """Membuat session request yang tahan banting (Auto Retry)"""
    session = requests.Session()
    retry = Retry(connect=3, backoff_factor=0.5)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session
session = get_session()

def safe_send_message(chat_id, text, reply_markup=None):
    """Kirim pesan dengan retry otomatis jika internet RTO"""
    for i in range(3):
        try:
            return bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)
        except Exception as e:
            print(f"⚠️ Gagal kirim pesan (Attempt {i+1}): {e}")
            time.sleep(2)
    return None

def safe_edit_message(text, chat_id, message_id, reply_markup=None):
    """Edit pesan dengan retry otomatis"""
    for i in range(3):
        try:
            return bot.edit_message_text(text, chat_id, message_id, parse_mode="Markdown", reply_markup=reply_markup)
        except Exception as e:
            if "message is not modified" in str(e): return
            print(f"⚠️ Gagal edit pesan (Attempt {i+1}): {e}")
            time.sleep(2)
    return None

# --- 3. FUNGSI API FORE & SMSHUB ---

def normalize_phone(phone):
    phone = str(phone).strip().replace('-', '').replace(' ', '').replace('+', '')
    if phone.startswith('62'): return phone[2:] 
    if phone.startswith('08'): return phone[1:]
    return phone

def get_fresh_token(headers):
    try:
        r = session.get('https://api.fore.coffee/auth/get-token', headers=headers, timeout=10).json()
        if r.get('statusCode') == 200: return r.get('payload', {}).get('access_token')
    except: pass
    return None

def check_is_registered(phone, headers):
    try:
        data = {"phone": f"+62{phone}"}
        r = session.post('https://api.fore.coffee/auth/check-phone', headers=headers, json=data, timeout=10).json()
        if r.get('status') == 'success': return r.get('payload', {}).get('is_registered') == 0
    except: pass
    return None

def request_otp_fore(phone, headers):
    try:
        data = {"method": "", "phone": f"+62{phone}"}
        r = session.post('https://api.fore.coffee/auth/req-login-code', headers=headers, json=data, timeout=10).json()
        return r.get('status') == 'success'
    except: return False

def order_smshub():
    try:
        url = f"https://smshub.org/stubs/handler_api.php?api_key={API_KEY}&action=getNumber&service={TARGET_SERVICE}&country={TARGET_COUNTRY}&maxPrice={TARGET_PRICE}"
        resp = session.get(url, timeout=15).text
        if "ACCESS_NUMBER" in resp:
            parts = resp.split(":")
            return {"status": "success", "id": parts[1], "number": parts[2]}
        return {"status": "error", "msg": resp}
    except Exception as e: return {"status": "error", "msg": str(e)}

def set_status(id, status):
    try: session.get(f"https://smshub.org/stubs/handler_api.php?api_key={API_KEY}&action=setStatus&id={id}&status={status}", timeout=10)
    except: pass

def get_status(id):
    try: return session.get(f"https://smshub.org/stubs/handler_api.php?api_key={API_KEY}&action=getStatus&id={id}", timeout=10).text
    except: return "ERROR"

def get_balance():
    try:
        resp = session.get(f"https://smshub.org/stubs/handler_api.php?api_key={API_KEY}&action=getBalance", timeout=10).text
        return resp.split(":")[1] + " RUB" if "ACCESS_BALANCE" in resp else resp
    except: return "Error"

# --- 4. WORKER LOGIC (THE HUNTER) ---

def worker_hunt_otp(chat_id, cred_index, worker_num):
    """
    Worker ini tidak akan berhenti sampai mendapatkan 1 OTP valid.
    - Terdaftar? -> Silent Skip -> Beli Baru
    - Fresh tapi no OTP (5 menit)? -> Cancel -> Beli Baru
    """
    
    while True: # Loop sampai dapat OTP
        # Cek Stop Manual
        if manual_stops.get(f"worker_{worker_num}", False):
            break

        # 1. ORDER NOMOR
        res = order_smshub()
        if res['status'] != 'success':
            msg = str(res['msg'])
            if "NO_NUMBERS" in msg:
                print(f"[Worker {worker_num}] Stok Habis. Retrying...")
                time.sleep(5) # Tunggu stok
                continue
            elif "NO_BALANCE" in msg:
                safe_send_message(chat_id, f"⚠️ Worker {worker_num} Stop: Saldo Habis.")
                break
            # Error lain (Limit active number, dll)
            time.sleep(3)
            continue

        act_id = res['id']
        full_phone = res['number']
        clean_phone = normalize_phone(full_phone)

        # 2. SETUP FORE
        cred = CREDENTIALS[cred_index % len(CREDENTIALS)]
        headers = BASE_HEADERS_TEMPLATE.copy()
        headers['secret-key'] = cred['secret-key']
        headers['push-token'] = cred['push-token']
        headers['device-id'] = str(uuid.uuid4()).upper()

        token = get_fresh_token(headers)
        if not token: 
            # Gagal token, skip nomor ini (biarkan silent timeout)
            time.sleep(1)
            continue
        headers['access-token'] = token

        # 3. CEK FRESH
        is_fresh = check_is_registered(clean_phone, headers)

        if is_fresh is True:
            # === NOMOR FRESH ===
            if request_otp_fore(clean_phone, headers):
                # Update UI
                msg_text = (
                    f"⚡ **Worker {worker_num}: Menunggu OTP**\n"
                    f"📱 `{full_phone}`\n"
                    f"⏳ _Exp 5 Menit..._"
                )
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("❌ Cancel Manual", callback_data=f"stop_{worker_num}_{act_id}"))
                
                sent_msg = safe_send_message(chat_id, msg_text, reply_markup=markup)
                if not sent_msg: continue 

                # === MONITORING OTP (MAX 5 MENIT) ===
                wait_start = time.time()
                got_otp = False
                
                while time.time() - wait_start < 300: # 300 detik
                    if manual_stops.get(f"worker_{worker_num}", False):
                        set_status(act_id, 8)
                        safe_edit_message(f"🚫 Worker {worker_num} Stopped.", chat_id, sent_msg.message_id)
                        return 

                    status_sms = get_status(act_id)

                    if "STATUS_OK" in status_sms:
                        otp_code = status_sms.split(":")[1]
                        
                        # SUKSES DAPAT OTP
                        success_text = (
                            f"✅ **OTP SUKSES (Worker {worker_num})**\n"
                            f"📱 `{full_phone}`\n"
                            f"🔑 `{otp_code}`\n"
                        )
                        markup_done = types.InlineKeyboardMarkup()
                        markup_done.add(types.InlineKeyboardButton("✅ Simpan (Done)", callback_data=f"done_{act_id}"))
                        
                        safe_send_message(chat_id, f"🔔 OTP: `{otp_code}`")
                        safe_edit_message(success_text, chat_id, sent_msg.message_id, reply_markup=markup_done)
                        
                        got_otp = True
                        break 
                    
                    elif "STATUS_CANCEL" in status_sms:
                        break 

                    time.sleep(3)
                
                # === KEPUTUSAN SETELAH LOOP ===
                if got_otp:
                    break # MISI SELESAI, KELUAR DARI LOOP UTAMA
                else:
                    # Timeout 5 menit -> Anggap Busuk -> Cancel -> Beli Baru
                    print(f"[Worker {worker_num}] {full_phone} Timeout (No OTP). Cancel & Replace.")
                    set_status(act_id, 8) 
                    safe_edit_message(f"♻️ `{full_phone}` No OTP > 5m. Mencari baru...", chat_id, sent_msg.message_id)
                    # Lanjut loop while True (Beli lagi)

            else:
                print(f"[Worker {worker_num}] Gagal Tembak OTP API.")
        
        else:
            # === NOMOR TERDAFTAR ===
            print(f"[Worker {worker_num}] {full_phone} Terdaftar. Skip (Silent).")
            # Jangan cancel, biarkan SMSHub timeout sendiri (Anti-Ban)
            time.sleep(2)
            # Lanjut loop while True (Beli lagi)

# --- 5. HANDLER TELEGRAM ---

def is_allowed(uid): return uid in ALLOWED_USERS

@bot.message_handler(commands=['start'])
def start(m):
    if not is_allowed(m.from_user.id): return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🚀 Beli Massal", "💰 Cek Saldo")
    safe_send_message(m.chat.id, "🤖 **Fore Hunter V4 Final**\n- Auto Replace Bad Number\n- Silent Skip Registered\n- Anti Crash", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "💰 Cek Saldo")
def cek_saldo(m):
    if not is_allowed(m.from_user.id): return
    safe_send_message(m.chat.id, f"Saldo: `{get_balance()}`")

@bot.message_handler(func=lambda m: m.text == "🚀 Beli Massal")
def ask_qty(m):
    if not is_allowed(m.from_user.id): return
    msg = safe_send_message(m.chat.id, "🔢 **Butuh berapa OTP Sukses?**\n(Contoh: 5)")
    if msg: bot.register_next_step_handler(msg, process_buy)

def process_buy(m):
    if not is_allowed(m.from_user.id): return
    try:
        qty = int(m.text)
        if qty > 20: return safe_send_message(m.chat.id, "⚠️ Maks 20 Worker.")
    except: return
    
    safe_send_message(m.chat.id, f"⚡ Mengerahkan {qty} Worker...\nWorker hanya berhenti jika dapat OTP.")
    
    for i in range(1, qty+1):
        manual_stops[f"worker_{i}"] = False
        t = threading.Thread(target=worker_hunt_otp, args=(m.chat.id, i, i))
        t.start()
        time.sleep(1.5)

@bot.callback_query_handler(func=lambda call: True)
def cb(call):
    action = call.data.split("_")[0]
    
    if action == "stop":
        worker_num = call.data.split("_")[1]
        act_id = call.data.split("_")[2]
        
        manual_stops[f"worker_{worker_num}"] = True
        set_status(act_id, 8) 
        bot.answer_callback_query(call.id, "Stopped")
        
    elif action == "done":
        act_id = call.data.split("_")[1]
        set_status(act_id, 6) 
        bot.answer_callback_query(call.id, "Saved")
        safe_edit_message("✅ Order Selesai.", call.message.chat.id, call.message.message_id)

# --- 6. MAIN LOOP (CLEAN STOP & ANTI CRASH) ---
print("Bot Fore Hunter Berjalan... (Tekan Ctrl+C untuk Stop)")

if __name__ == "__main__":
    try:
        while True:
            try:
                # Polling dengan timeout panjang agar hemat resource dan stabil
                bot.infinity_polling(timeout=90, long_polling_timeout=5)
            except Exception as e:
                # Jika error Stop Manual, keluar loop
                if "Break infinity polling" in str(e):
                    break
                
                # Jika error koneksi, print dan reconnect
                print(f"⚠️ Koneksi Error: {e}")
                print("🔄 Reconnecting dalam 3 detik...")
                time.sleep(3)
                
    except (KeyboardInterrupt, SystemExit):
        print("\n🛑 Bot Dihentikan Manual (Ctrl+C).")
        manual_stops.clear()
        # Opsional: Bisa tambahkan logic untuk cancel semua order aktif disini jika mau
        print("✅ Shutdown Clean.")