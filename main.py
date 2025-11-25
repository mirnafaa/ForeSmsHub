import requests
import time
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Konfigurasi Header Dasar (Tanpa Token)
BASE_HEADERS_TEMPLATE = {
    'Host': 'api.fore.coffee',
    'language': 'id',
    'User-Agent': 'Fore Coffee/4.11.0 (coffee.fore.fore; build:1577; iOS 18.5.0) Alamofire/5.10.2',
    'sentry-trace': '82f1ea8728ec4d9f98e6c380e9ee3e74-0b525eb3ee734332-0',
    'country-id': '1',
    'platform': 'ios',
    'Connection': 'keep-alive',
    'appsflyer-id': '1759206300240-5775732',
    'Accept-Language': 'en-ID;q=1.0, id-ID;q=0.9',
    'timezone': '+07:00',
    'jailbroken': '0',
    'device-model': 'iPhone 12',
    'Accept': '*/*',
    'app-version': '4.11.0',
    'os-version': '18.5',
    'Content-Type': 'application/json',
}

# List Kredensial Rotasi dari .env
CREDENTIALS = [
    {
        'secret-key': os.getenv('SECRET_KEY_1'),
        'push-token': os.getenv('PUSH_TOKEN_1')
    },
    {
        'secret-key': os.getenv('SECRET_KEY_2'),
        'push-token': os.getenv('PUSH_TOKEN_2')
    }
]

# Variabel Global
successful_numbers = []
print_lock = threading.Lock()

def log(message):
    with print_lock:
        print(message)

def normalize_phone(phone):
    """Normalisasi: 08xx / 8xx -> 628xx"""
    phone = str(phone).strip().replace('-', '').replace(' ', '').replace('+', '')
    if phone.startswith('08'):
        return '62' + phone[1:]
    elif phone.startswith('8'):
        return '62' + phone
    return phone

def get_fresh_token(headers):
    """Generate Access Token baru untuk setiap proses"""
    url = 'https://api.fore.coffee/auth/get-token'
    try:
        response = requests.get(url, headers=headers, timeout=10)
        resp_json = response.json()
        
        if response.status_code == 200 and resp_json.get('statusCode') == 200:
            payload = resp_json.get('payload', {})
            return payload.get('access_token')
        else:
            return None
    except Exception:
        return None

def check_is_registered(phone, headers):
    """Tahap 1: Cek Status Nomor"""
    url = 'https://api.fore.coffee/auth/check-phone'
    data = {"phone": f"+{phone}"}
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=10)
        resp_json = response.json()
        
        if response.status_code == 200 and resp_json.get('status') == 'success':
            payload = resp_json.get('payload', {})
            return payload.get('is_registered') == 0
        else:
            log(f"[-] {phone} | Gagal Cek Status: {resp_json.get('message', 'Unknown Error')}")
            return None
    except Exception as e:
        log(f"[!] {phone} | Error Check: {str(e)}")
        return None

def request_otp(phone, headers, attempt=1):
    """Tahap 2: Request OTP"""
    url = 'https://api.fore.coffee/auth/req-login-code'
    data = {"method": "", "phone": f"+{phone}"}
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=10)
        resp_json = response.json()
        
        if response.status_code == 200 and resp_json.get('status') == 'success':
            return True
        else:
            log(f"[-] {phone} | Gagal Kirim OTP ({attempt}): {resp_json.get('message', 'Unknown')}")
            return False
    except Exception as e:
        log(f"[!] {phone} | Error OTP ({attempt}): {str(e)}")
        return False

def process_number(phone_raw, cred_index):
    """Worker Utama"""
    phone = normalize_phone(phone_raw)
    
    # 1. Setup Header & Device ID Unik per Thread
    cred = CREDENTIALS[cred_index % len(CREDENTIALS)]
    device_id = str(uuid.uuid4()).upper()
    
    current_headers = BASE_HEADERS_TEMPLATE.copy()
    current_headers['secret-key'] = cred['secret-key']
    current_headers['push-token'] = cred['push-token']
    current_headers['device-id'] = device_id
    
    log(f"[*] Memproses {phone}...")
    
    # 2. Ambil Token
    access_token = get_fresh_token(current_headers)
    if not access_token:
        log(f"[!] {phone} | Gagal generate Token awal. Skip.")
        return

    current_headers['access-token'] = access_token
    
    # 3. Cek Status
    is_not_registered = check_is_registered(phone, current_headers)
    
    if is_not_registered is True:
        log(f"[>] {phone} Belum terdaftar. Mengirim OTP ke-1...")
        
        # 4. Kirim OTP Pertama
        if request_otp(phone, current_headers, attempt=1):
            log(f"[✓] {phone} OTP 1 Terkirim! Menunggu 30 detik untuk pengulangan...")
            
            # 5. Tunggu 30 Detik (Looping delay)
            time.sleep(30)
            
            # 6. Kirim OTP Kedua (Ulang 1 kali)
            log(f"[>] {phone} Mengirim OTP ke-2...")
            if request_otp(phone, current_headers, attempt=2):
                log(f"[DONE] {phone} Selesai (2x OTP terkirim).")
            else:
                log(f"[!] {phone} Gagal OTP ke-2 (Tapi OTP 1 sukses). Selesai.")
            
            successful_numbers.append(phone)
        else:
            # Jika OTP pertama gagal, kita anggap gagal total (tidak ada delay 30s)
            log(f"[X] {phone} Gagal mengirim OTP pertama.")
                
    elif is_not_registered is False:
        log(f"[-] {phone} Sudah Terdaftar. Skip.")
    else:
        log(f"[?] {phone} Skip karena error pengecekan.")

def main():
    print("=== FORE OTP SENDER (2x SEND DELAY 30s) ===")
    print("Masukkan nomor HP (Enter 2x untuk mulai):")
    
    input_lines = []
    while True:
        line = input()
        if not line:
            break
        input_lines.append(line)
    
    phone_list = [p for p in input_lines if p.strip()]
    if not phone_list:
        print("Tidak ada nomor.")
        return

    # Max threads = jumlah akun * 3
    max_workers = len(CREDENTIALS) * 3
    
    print(f"\n[INFO] Memproses {len(phone_list)} nomor dengan {max_workers} threads...")
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(process_number, phone, i) 
            for i, phone in enumerate(phone_list)
        ]
        for future in futures:
            future.result()

    print("\n" + "="*30)
    print(f"SELESAI. Berhasil: {len(successful_numbers)}")
    print("="*30)
    for num in successful_numbers:
        print(f"- {num}")

if __name__ == '__main__':
    main()