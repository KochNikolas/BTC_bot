import math
import requests
import json
import sys
import os
import csv
import time
from datetime import datetime, timezone
from eth_account import Account

# py-clob-client für Polymarket-Daten
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, OrderArgs, MarketOrderArgs
from py_clob_client.constants import POLYGON

BUY = "BUY"
SELL = "SELL"

# Globale Variable für Rate-Limit-Schutz
LAST_ORDER_TS = 0

CONFIG_FILE = "config.json"
STATUS_FILE = "trade_status.json"

def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except:
        return {
            "DRY_RUN": False,
            "POLL_INTERVAL": 1,
            "TOTAL_BUDGET": 100.0,
            "KELLY_FRACTION": 0.5,
            "MIN_EDGE": 0.08,
            "ACTIVATE_BEFORE_END_SEC": 150,
            "BTC_ANNUALIZED_VOL": 0.65,
            "TAKE_PROFIT_PCT": 0.05,
            "STOP_LOSS_PCT": 0.05,
            "BLOCK_THRESHOLD": 0.85,
            "EXIT_THRESHOLD": 0.90,
            "COOLDOWN_SEC": 30,
            "STAKE_USD": 1.0,
            "MIN_RESERVE_USD": 0.0,
            "TRADE_DONE": False,
            "POLY_API_KEY": "",
            "POLY_API_SECRET": "",
            "POLY_API_PASSPHRASE": "",
            "POLY_PRIVATE_KEY": ""
        }

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=4)
        return True
    except Exception as e:
        print(f"Error saving config: {e}")
        return False

# Initialer Load
CONFIG = load_config()

# ─────────────────────────────────────────────────────────────
#  KONFIGURATION & MODUS (Jetzt dynamisch)
# ─────────────────────────────────────────────────────────────
def get_conf(key):
    """Gibt einen Wert aus der globalen CONFIG zurück."""
    global CONFIG
    return CONFIG.get(key)

def refresh_config():
    """Lädt die Konfiguration von der Festplatte in die globale Variable."""
    global CONFIG
    CONFIG = load_config()

# -- Endpunkte --
MINUTES_PER_YEAR = 525_600
POLY_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com/markets"
BINANCE_TICK = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BTC_SYMBOL = "BTCUSDT"

def get_log_file():
    cfg = load_config()
    return "btc_trades_log.csv" if cfg.get("DRY_RUN", True) else "real_trades_log.csv"

# Globale Session für Performance (Connection Pooling)
session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})

# Globale History für das Web-Chart
status_history = []

def update_web_status(status_dict, reset_history=False):
    global status_history
    if reset_history:
        status_history = []
        
    try:
        if status_dict and "secs_left" in status_dict:
            status_history.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "rel_sec": max(0, 300 - status_dict.get("secs_left", 0)),
                "btc_now": status_dict.get("btc_now"),
                "prob_up": status_dict.get("prob_up"),
                "prob_down": (1.0 - status_dict.get("prob_up")) if status_dict.get("prob_up") is not None else None,
                "edge_yes": status_dict.get("edge_yes"),
                "edge_no": status_dict.get("edge_no"),
                "price_yes": status_dict.get("price_yes"),
                "price_no": status_dict.get("price_no")
            })
        
        full_status = {
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "current": status_dict,
            "history": status_history,
            "config": CONFIG,
            "is_geoblocked": globals().get("IS_GEOBLOCKED", False),
            "financials": status_dict.get("financials", {"balance": CONFIG.get("TOTAL_BUDGET", 100), "invested": 0, "equity": CONFIG.get("TOTAL_BUDGET", 100)})
        }
        with open(STATUS_FILE, "w") as f:
            json.dump(full_status, f)
    except Exception as e:
        print(f"Web status update error: {e}")

# =============================================================
#  HILFSFUNKTIONEN (API & MATH)
# =============================================================

def get_btc_price_now() -> float | None:
    try:
        resp = session.get(BINANCE_TICK, params={"symbol": BTC_SYMBOL}, timeout=1)
        return float(resp.json()["price"])
    except: return None

def get_btc_open_price(start_ts_ms: int) -> float | None:
    try:
        params = {"symbol": BTC_SYMBOL, "interval": "5m", "startTime": start_ts_ms, "limit": 1}
        resp = session.get(BINANCE_KLINES, params=params, timeout=1)
        return float(resp.json()[0][1])
    except: return None

def calc_up_probability(current_price: float, open_price: float, secs_left: float, vol: float) -> float:
    """Berechnet P(S_T > S_0) via Normalverteilung CDF."""
    if secs_left <= 0: return 1.0 if current_price > open_price else 0.0
    
    sigma_min = vol / math.sqrt(MINUTES_PER_YEAR)
    sigma_T = sigma_min * math.sqrt(secs_left / 60.0)
    
    if sigma_T == 0: return 1.0 if current_price > open_price else 0.0
    
    d = math.log(current_price / open_price) / sigma_T
    return 0.5 * (1.0 + math.erf(d / math.sqrt(2)))

def calc_kelly_stake(edge: float, price: float, budget: float, fraction: float) -> float:
    """
    Kelly-Formel: Einsatz = Budget * Kelly_Fraction * Edge / ((1/Preis) - 1)
    """
    if price <= 0 or price >= 1 or edge <= 0: return 0.0
    odds_minus_one = (1.0 / price) - 1.0
    if odds_minus_one <= 0: return 0.0
    stake_fraction = fraction * (edge / odds_minus_one)
    # Sicherheits-Cap: nie mehr als 25% des Budgets pro Trade
    stake_fraction = min(stake_fraction, 0.25)
    return round(stake_fraction * budget, 2)

# =============================================================
#  POLYMARKET API
# =============================================================

def find_market_by_slug(slug: str) -> dict | None:
    try:
        resp = session.get(f"{GAMMA_API}?slug={slug}", timeout=2)
        data = resp.json()
        return data[0] if data else None
    except Exception as e:
        print(f" \n⚠️ Gamma API Fehler: {e}")
        return None

def get_market_prices(client: ClobClient, yes_id: str, no_id: str, slug: str):
    """Holt Preise über den CLOB-Preis-Endpunkt (schneller)."""
    y_price, n_price = None, None
    
    # 1. Direkter Preis-Endpunkt
    try:
        def fetch_p(tid):
            url = f"{POLY_HOST}/price"
            r = session.get(url, params={"token_id": tid, "side": "buy"}, timeout=1)
            if r.status_code == 200:
                return float(r.json().get("price"))
            return None
        
        y_price = fetch_p(yes_id)
        n_price = fetch_p(no_id)
    except Exception as e:
        pass

    # 2. Fallback: Gamma API
    if (y_price is None or n_price is None) or (y_price + n_price > 1.10):
        try:
            resp = session.get(f"{GAMMA_API}?slug={slug}", timeout=1)
            data = resp.json()
            if data and len(data) > 0:
                import json
                prices = json.loads(data[0].get("outcomePrices", "[]"))
                if len(prices) >= 2:
                    y_price = float(prices[0]) if y_price is None or (y_price + n_price > 1.10) else y_price
                    n_price = float(prices[1]) if n_price is None or (y_price + n_price > 1.10) else n_price
        except:
            pass
            
    return y_price, n_price

# =============================================================
#  LOGGING
# =============================================================

def init_csv():
    log_file = get_log_file()
    if not os.path.exists(log_file):
        with open(log_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "market", "direction", "action", "price", "edge", "stake", "probability", "profit_usdc", "modus", "target_price"])

def log_trade_event(market, side, action, price, edge, stake, p_win, profit, dry_mode, target_price=0):
    log_file = get_log_file()
    # Sicherstellen, dass die Datei mit einem Zeilenumbruch endet
    file_exists = os.path.exists(log_file)
    mode = "a+" if file_exists else "w"
    
    with open(log_file, mode, newline="") as f:
        if file_exists:
            f.seek(0, os.SEEK_END)
            if f.tell() > 0:
                f.seek(f.tell() - 1)
                try:
                    last_char = f.read(1)
                    if last_char not in ['\n', '\r']:
                        f.write('\n')
                except: pass
            f.seek(0, os.SEEK_END) # Zurück zum Ende für writer

        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "market", "direction", "action", "price", "edge", "stake", "probability", "profit_usdc", "modus", "target_price"])
            
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            market, side, action, 
            round(price, 4) if price else 0, 
            round(edge, 4) if edge else 0, 
            stake, 
            round(p_win, 4) if p_win else 0,
            round(profit, 4),
            "DRY" if dry_mode else "LIVE",
            round(target_price, 4) if target_price else 0
        ])

# =============================================================
#  LIVE EXECUTION & SECURITY
# =============================================================

IS_GEOBLOCKED = False
LAST_GEO_CHECK = 0

def get_token_balance(wallet, token_address):
    """Scannt Guthaben einers ERC20 Tokens via RPC."""
    rpc_url = "https://polygon-bor-rpc.publicnode.com"
    # ERC20 balanceO: 0x70a08231 + wallet(64 chars padded)
    try:
        data = "0x70a08231" + wallet[2:].lower().rjust(64, '0')
        payload = {"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": token_address, "data": data}, "latest"], "id": 1}
        resp = requests.post(rpc_url, json=payload, timeout=5).json()
        if 'result' in resp:
            return round(int(resp['result'], 16) / 10**6, 2)
    except: pass
    return 0.0

def check_geoblock():
    """Bypassed Geo-check (CLOB often works via VPN even if frontend doesn't)."""
    global IS_GEOBLOCKED, LAST_GEO_CHECK
    IS_GEOBLOCKED = False
    LAST_GEO_CHECK = time.time()
    return False

def get_live_client(cfg):
    """Initialisiert den ClobClient mit Proxy-Wallet Support (Magic Link)."""
    if cfg.get("DRY_RUN", True):
        return ClobClient(host=POLY_HOST)
    
    try:
        pk = cfg.get("POLY_PRIVATE_KEY")
        funder_addr = "0xAC0D49bEf9C3B97F64193C0DA507848EF9e64D49"
        
        # 1. Temporärer Client zur Ableitung der Credentials
        temp_client = ClobClient(host=POLY_HOST, key=pk, chain_id=POLYGON)
        creds = temp_client.create_or_derive_api_creds()
        
        # 2. Finaler Handels-Client mit Proxy-Settings
        client = ClobClient(
            host=POLY_HOST,
            key=pk,
            chain_id=POLYGON,
            creds=creds,
            signature_type=1, # 1 = Proxy/Magic Wallet
            funder=funder_addr # Adresse mit dem Kapital
        )
        return client
    except Exception as e:
        print(f" ❌ KRITISCHER INITIALISIERUNGSFEHLER: {e}")
        return ClobClient(host=POLY_HOST)

def place_live_order(client, token_id, amount_usdc, limit_price, side="buy"):
    """Führt eine Live-Order auf Polymarket aus mit Slippage-Schutz und API-Limits."""
    global LAST_ORDER_TS
    
    # Rate-Limit Schutz (1 Sekunde zwischen Orders)
    if time.time() - LAST_ORDER_TS < 1.0:
        time.sleep(1)

    try:
        # 1. Token-Menge berechnen
        token_qty = amount_usdc / limit_price
        
        # 2. API-Limit: Minimum 5 Tokens pro Order (vermeidet 400er Fehler)
        if token_qty < 5.0:
            token_qty = 5.0
            
        # Präzision: Max 2 Dezimalstellen wie von CLOB gefordert
        token_qty = round(token_qty, 2)

        # 3. Preis mit Slippage-Puffer (2%)
        slippage = 0.02
        if side == "buy":
            price = round(limit_price * (1 + slippage), 2)
            order_side = "BUY"
        else:
            price = round(limit_price * (1 - slippage), 2)
            order_side = "SELL"

        # Validierung des Preisbereichs [0.01, 0.99]
        price = max(0.01, min(0.99, price))

        print(f" 📡 Sende LIVE {side.upper()} @ {price*100:.1f}¢ | Menge: {token_qty}")
        
        # 4. Order-Argumente erstellen
        from py_clob_client.clob_types import OrderArgs
        
        order_args = OrderArgs(
            price=price,
            size=token_qty,
            side=order_side,
            token_id=token_id
        )
        
        # 5. Order signieren UND absenden
        # Erst Signatur lokal erstellen:
        signed_order = client.create_order(order_args)
        # Dann die SIGNIERTE Order an die API posten:
        resp = client.post_order(signed_order)
        
        LAST_ORDER_TS = time.time()
        
        if resp and resp.get("success"):
            print(f" ✅ Order erfolgreich! ID: {resp.get('orderID')}")
            return resp
        else:
            print(f" ❌ Order fehlgeschlagen: {resp}")
            return None

    except Exception as e:
        print(f" ❌ EXCEPTION in place_live_order: {e}")
        return None

# =============================================================
#  MAIN LOOP
# =============================================================

def main():
    cfg_initial = load_config()
    init_csv()
    
    # Initiale Geo-Prüfung NUR wenn Live-Modus beim Start aktiv ist
    client = get_live_client(cfg_initial)
    
    # Echter Verbindungstest & Wallet-Identity im LIVE-Modus
    if not cfg_initial.get("DRY_RUN", True):
        try:
            # 1. Adressen aus Konfiguration / Key / Client
            priv_key = cfg_initial.get("POLY_PRIVATE_KEY")
            account = Account.from_key(priv_key)
            eoa_address = account.address
            
            # Handels-Adresse dynamisch vom Client holen (jetzt mit get_address())
            try:
                proxy_address = client.get_address()
            except:
                proxy_address = cfg_initial.get("POLY_PROXY_ADDRESS", "None")
            
            # 2. Token-Scan (Native vs Bridged) für BEIDE Adressen
            USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
            USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            
            # Checks für EOA
            usdc_n_eoa = get_token_balance(eoa_address, USDC_NATIVE)
            usdc_e_eoa = get_token_balance(eoa_address, USDC_E)
            
            # Checks für Proxy (falls vorhanden)
            usdc_n_proxy = 0.0
            usdc_e_proxy = 0.0
            if proxy_address != "N/A":
                usdc_n_proxy = get_token_balance(proxy_address, USDC_NATIVE)
                usdc_e_proxy = get_token_balance(proxy_address, USDC_E)
            
            # 3. POL (Gas) - Nur für EOA relevant für Signaturen
            pol_balance = "N/A"
            try:
                rpc_url = "https://polygon-bor-rpc.publicnode.com"
                rpc_payload = {"jsonrpc": "2.0", "method": "eth_getBalance", "params": [eoa_address, "latest"], "id": 1}
                rpc_resp = requests.post(rpc_url, json=rpc_payload, timeout=5).json()
                wei = int(rpc_resp['result'], 16)
                pol_balance = f"{wei / 10**18:.4f}"
            except: pass

            # 4. Konsolen-Ausgabe (Deep-Analysis)
            print("\n" + "─"*65)
            print(f"📡 EOA (Signier-Key):    \033[1m{eoa_address}\033[0m")
            print(f"🏢 PROXY (Handels-Acc):  \033[1m{proxy_address}\033[0m")
            print(f"⛽ POL-GAS (EOA):        \033[93m\033[1m{pol_balance} POL\033[0m")
            print("─" * 65)
            print(f"💰 USDC (NATUR)  | EOA: {usdc_n_eoa} | PROXY: \033[92m\033[1m{usdc_n_proxy} USDC\033[0m")
            print(f"💰 USDC.e (BRIDGED) | EOA: {usdc_e_eoa} | PROXY: \033[92m\033[1m{usdc_e_proxy} USDC.e\033[0m")
            print("─" * 65)

            # 5. CLOB-Interner Balance-Check (wird für Trading-Entscheidungen genutzt)
            sig_type = 1 # Wir haben es in get_live_client erzwungen
            if hasattr(client, "builder") and hasattr(client.builder, "sig_type"):
                sig_type = client.builder.sig_type
            
            resp = client.get_balance_allowance(BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=sig_type))
            raw_balance = float(resp.get("balance", "0"))
            clob_balance = raw_balance / 10**6
            print(f"📊 CLOB-HANDELS-GUTHABEN: \033[1m{clob_balance:.2f} USDC\033[0m")

            if float(clob_balance) <= 0 and (usdc_n_proxy + usdc_e_proxy) <= 0:
                print("\033[93m ⚠️ WARNUNG: Kein Guthaben auf Proxy gefunden. Trades werden fehlschlagen.\033[0m")
            else:
                print("\033[92m ✅ VERBINDUNG & GUTHABEN ERKANNT! BEREIT.\033[0m")

        except Exception as e:
            print(f"\n\033[91m ❌ IDENTITY-DEEP-CHECK FEHLGESCHLAGEN: {e}\033[0m")
            sys.exit(1)
    
    print("\033[1m" + "="*65)
    print(f" 🚀 POLYMARKET BTC VALUE-BOT")
    print("="*65 + "\033[0m")

    # 🧹 Start-Cleanup: Alle offenen Orders löschen (Sicherheit & Kapitalfreigabe)
    if not cfg_initial.get("DRY_RUN", True):
        try:
            print("\n 🧹 Bereinige offene Orders...")
            open_orders = client.get_open_orders()
            if open_orders:
                for order in open_orders:
                    oid = order.get("orderID") or order.get("id")
                    if oid:
                        client.cancel_order(oid)
                print(f" ✅ {len(open_orders)} offene Orders gelöscht.")
            else:
                print(" ✅ Keine offenen Orders gefunden.")
        except Exception as e:
            print(f" ⚠️ Warnung beim Löschen offener Orders: {e}")
    
    last_interval = None
    market_info = None
    active_pos = None # {side, entry_price, stake, p_win}
    available_balance = load_config().get("TOTAL_BUDGET", 100.0)
    
    while True:
        # PERFORMANCE: Config nur 1x pro Loop laden
        cfg = load_config()
        poll_int = 1
        dry_run = cfg.get("DRY_RUN", True)

        # Periodischer Geo-Check (alle 10 Min) NUR im LIVE MODUS
        if not dry_run:
            # Sofort-Check bei Modus-Wechsel oder alle 10 Min
            if ('last_dry_mode' in locals() and last_dry_mode == True) or (time.time() - LAST_GEO_CHECK > 600):
                check_geoblock()
        else:
            # Im Dry-Run Modus setzen wir den Block-Zustand zurück, 
            # damit das Warn-Banner im UI verschwindet
            IS_GEOBLOCKED = False
        
        # Falls sich der Modus geändert hat, Client neu initialisieren
        # (Einfache Prüfung - falls Key in cfg anders als im client, wobei client privat ist)
        # Für Einfachheit initialisieren wir bei Modus-Wechsel neu:
        if 'last_dry_mode' in locals() and last_dry_mode != dry_run:
            print(f"\n 🔄 MODUS-WECHSEL ERKANNT: {'DRY' if dry_run else 'LIVE'}")
            client = get_live_client(cfg)
            init_csv() # Neues CSV falls nötig
        last_dry_mode = dry_run

        total_budget = cfg.get("TOTAL_BUDGET", 100.0)
        min_edge = cfg.get("MIN_EDGE", 0.08)
        activate_sec = 280
        tp_pct = cfg.get("TAKE_PROFIT_PCT", 0.04)
        sl_pct = cfg.get("STOP_LOSS_PCT", 0.05)
        cooldown = cfg.get("COOLDOWN_SEC", 30)
        last_sale = cfg.get("LAST_SALE_TS", 0)
        
        # Sicherheits-Thresholds
        block_thresh = cfg.get("BLOCK_THRESHOLD", 0.85)
        exit_thresh = cfg.get("EXIT_THRESHOLD", 0.90)
        
        # trade_done ist nun zeitbasiert (cooldown)
        trade_done = (time.time() - last_sale) < cooldown

        # Finanzen berechnen
        invested = active_pos['stake'] if active_pos else 0
        equity = available_balance + invested
        now_ts = int(time.time())
        interval_start = (now_ts // 300) * 300
        interval_end = interval_start + 300
        secs_left = interval_end - now_ts
                
        if interval_start != last_interval:
            last_interval = interval_start
            # Kein automatisches Zurücksetzen von TRADE_DONE mehr nötig
            # FIX: Nutze interval_start für den AKTUELL laufenden Markt
            # Um 15:45 (Start) läuft der Markt bis 15:50. Der Slug dafür ist 15:45.
            slug = f"btc-updown-5m-{interval_start}"
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ⏳ Neues Intervall | Suche Slug: {slug}")
            
            # Web History resetten und neuen Slug/Time sofort anzeigen
            update_web_status({
                "slug": slug,
                "secs_left": secs_left,
                "market": "Suche Markt..."
            }, reset_history=True)
            
            market_data = None
            market_info = None # Zurücksetzen für neuen Markt
            for _ in range(5):
                market_data = find_market_by_slug(slug)
                if market_data: break
                time.sleep(2)
            
            if market_data:
                import json
                # Gamma API liefert IDs und Outcomes oft als stringified JSON
                def parse_field(field):
                    if isinstance(field, str): 
                        try: return json.loads(field)
                        except: return []
                    return field or []

                ids = parse_field(market_data.get("clobTokenIds"))
                outcomes = parse_field(market_data.get("outcomes"))
                
                if len(ids) >= 2:
                    # Suche Indizes für Up/Yes und Down/No (Outcome-Mapping)
                    try:
                        yes_idx = next((i for i, o in enumerate(outcomes) if o.lower() in ["up", "yes"]), 0)
                        no_idx = next((i for i, o in enumerate(outcomes) if o.lower() in ["down", "no"]), 1)
                    except:
                        # FALLBACK: Index 0=YES, 1=NO
                        yes_idx, no_idx = 0, 1
                    
                    market_info = {
                        "yes_id": ids[yes_idx],
                        "no_id": ids[no_idx],
                        "question": market_data["question"],
                        "slug": market_data.get("slug", slug)
                    }
                    print(f" ✅ Markt: {market_info['question']}")
                    print(f"    IDs: YES={market_info['yes_id'][:8]} | NO={market_info['no_id'][:8]}")
                else:
                    print(" ❌ Ungültige Token-Daten erhalten.")
                    market_info = None
            else:
                print(" ❌ Markt nicht gefunden.")
                market_info = None

        if not market_info:
            time.sleep(poll_int)
            continue

        # Marktanalyse (Jeden Tick)
        btc_now = get_btc_price_now()
        btc_start = get_btc_open_price(interval_start * 1000)
        
        if not btc_now or not btc_start:
            time.sleep(poll_int)
            continue
            
        prob_up = calc_up_probability(btc_now, btc_start, secs_left, 0.70)
        prob_down = 1.0 - prob_up
        
        # Preise holen mit neuem Fallback-Check
        price_yes, price_no = get_market_prices(client, market_info["yes_id"], market_info["no_id"], slug)
        
        # Edge Berechnung
        edge_yes = (prob_up - price_yes) if price_yes is not None else 0
        edge_no = (prob_down - price_no) if price_no is not None else 0
        
        # Finanz-Update für Web-Status
        if not dry_run:
            try:
                sig_type = getattr(client.builder, "sig_type", 0)
                resp = client.get_balance_allowance(BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=sig_type))
                available_balance = float(resp.get("balance", "0")) / 10**6
            except:
                pass
        
        invested = active_pos['stake'] if active_pos else 0
        equity = available_balance + invested

        # Web Status Update
        update_web_status({
            "market": market_info["question"] if market_info else "Suche Markt...",
            "slug": slug,
            "btc_now": btc_now,
            "btc_start": btc_start,
            "prob_up": prob_up,
            "prob_down": prob_down,
            "price_yes": price_yes,
            "price_no": price_no,
            "edge_yes": edge_yes,
            "edge_no": edge_no,
            "secs_left": secs_left,
            "trade_done": trade_done,
            "financials": {
                "balance": round(available_balance, 2),
                "invested": round(invested, 2),
                "equity": round(equity, 2)
            }
        })

        # ── Konsolen-Logging ───────────────────────────────────────────
        fmt_p = lambda p: f"{p*100:2.0f}%" if p is not None else "N/A"
        fmt_c = lambda c: f"{c*100:2.0f}¢" if c is not None else "N/A"
        fmt_e = lambda e: f"{e*100:+.2f}%" if e is not None else "N/A"
        
        line = (
            f"T-{secs_left:3d}s | "
            f"[P(UP): {fmt_p(prob_up)} | Preis: {fmt_c(price_yes)} | Edge: {fmt_e(edge_yes)}] vs "
            f"[P(DOWN): {fmt_p(prob_down)} | Preis: {fmt_c(price_no)} | Edge: {fmt_e(edge_no)}]"
        )
        if os.isatty(1): print(f"\r{line}   ", end="", flush=True)
        else: print(line, flush=True)

        # ── EXIT LOGIK: Take Profit & Stop Loss ────────────────────────
        if active_pos:
            curr_p = price_yes if active_pos['side'] == 'UP' else price_no
            force_end = secs_left <= 5 # Am Ende der Runde verkaufen
            
            # Sicherheits-Exit (Limit erreicht)
            is_limit_reached = False
            if curr_p is not None:
                # Prüfe ob Wahrscheinlichkeit oder Preis zu extrem sind (> EXIT oder < 1-EXIT)
                p_val = prob_up if active_pos['side'] == 'UP' else prob_down
                if p_val >= exit_thresh or p_val <= (1.0 - exit_thresh) or curr_p >= exit_thresh or curr_p <= (1.0 - exit_thresh):
                    is_limit_reached = True

            if curr_p is not None or force_end:
                if curr_p is None: curr_p = 0
                change_pct = (curr_p - active_pos['entry_price']) / active_pos['entry_price']
                
                # Take Profit oder Stop Loss Trigger
                is_tp = change_pct >= tp_pct
                is_sl = change_pct <= -sl_pct
                
                if is_tp or is_sl or force_end or is_limit_reached:
                    if is_limit_reached: action = "SELL_LIMIT"
                    elif is_tp: action = "SELL_TP"
                    elif is_sl: action = "SELL_SL"
                    else: action = "SELL_FORCE"
                    
                    profit_usdc = active_pos['stake'] * change_pct
                    available_balance += (active_pos['stake'] + profit_usdc)
                    
                    color = "\033[92m" if change_pct >= 0 else "\033[91m"
                    tag = "🎯 TAKE PROFIT" if is_tp else ("🛑 STOP LOSS" if is_sl else ("⚠️ LIMIT REACHED" if is_limit_reached else "⏰ RUNDEN-ENDE"))
                    print(f"\n{color}{tag}: Verkauf {active_pos['side']} bei {curr_p*100:.1f}¢ ({change_pct*100:+.1f}%)\033[0m")
                    
                    if not dry_run:
                        # Echtgeld Verkauf
                        token_id = market_info["yes_id"] if active_pos['side'] == 'UP' else market_info["no_id"]
                        place_live_order(client, token_id, active_pos['stake'], curr_p, side="sell")
                    
                    log_trade_event(market_info["question"], active_pos['side'], action, curr_p, 0, active_pos['stake'], 0, profit_usdc, dry_run)
                    
                    # Cooldown setzen
                    cfg["LAST_SALE_TS"] = time.time()
                    save_config(cfg)
                    active_pos = None

        # ── TRADE VORBEREITUNG ─────────────────────────────────────────
        if not active_pos and edge_yes > edge_no and edge_yes >= min_edge:
            t_side, b_edge, t_p, t_price = "UP", edge_yes, prob_up, price_yes
        elif not active_pos and edge_no >= min_edge:
            t_side, b_edge, t_p, t_price = "DOWN", edge_no, prob_down, price_no
        else:
            t_side, b_edge, t_p, t_price = None, 0, 0, None

        # ── ENTRY LOGIK ────────────────────────────────────────────────
        if not trade_done and not active_pos and t_side and t_price and t_p > 0 and secs_left <= activate_sec:
            # Sicherheits-Check: Blockiert wenn zu sicher (> BLOCK oder < 1-BLOCK)
            is_blocked = t_p >= block_thresh or t_p <= (1.0 - block_thresh) or t_price >= block_thresh or t_price <= (1.0 - block_thresh)
            
            if not is_blocked:
                # Stake aus Config laden (fixer Betrag)
                stake = cfg.get("STAKE_USD", 1.0)
                min_reserve = cfg.get("MIN_RESERVE_USD", 0.0)
                
                if stake > 0 and (available_balance - stake) >= min_reserve:
                    print(f"\n\033[1m\033[94mTRADE AUSGELÖST: Kaufe {t_side} bei {t_price*100:.1f}¢ | Edge {b_edge*100:.2f}%\033[0m")
                    
                    order_success = True
                    if not dry_run:
                        token_id = market_info["yes_id"] if t_side == "UP" else market_info["no_id"]
                        resp = place_live_order(client, token_id, stake, t_price, side="buy")
                        if not resp:
                            order_success = False
                            print(" ⚠️ Live Kauf fehlgeschlagen. Intervall wird nicht als getradet markiert.")

                    if order_success:
                        active_pos = {'side': t_side, 'entry_price': t_price, 'stake': stake, 'p_win': t_p}
                        available_balance -= stake
                        t_target = t_price * (1 + tp_pct)
                        log_trade_event(market_info["question"], t_side, "BUY", t_price, b_edge, stake, t_p, 0, dry_run, target_price=t_target)
                        # Kein TRADE_DONE mehr hier, wird erst beim Verkauf durch Cooldown gesetzt
            else:
                # Optional: Info Print für geblockten Trade
                # print(f"\rENTRY GEBLOCKT (Threshold): P={t_p*100:.1f}% Preis={t_price*100:.1f}¢   ", end="")
                pass
        
        time.sleep(poll_int)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n ⏹  Bot gestoppt.")
