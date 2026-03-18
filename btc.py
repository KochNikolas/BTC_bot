import math
import requests
import json
import sys
import os
import csv
import time
from datetime import datetime, timezone
from eth_account import Account

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, OrderArgs, MarketOrderArgs
from py_clob_client.constants import POLYGON

BUY = "BUY"
SELL = "SELL"

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
            "BTC_ANNUALIZED_VOL": 1.2,
            "TAKE_PROFIT_PCT": 0.05,
            "STOP_LOSS_PCT": 0.05,
            "BLOCK_THRESHOLD": 0.85,
            "EXIT_THRESHOLD": 0.90,
            "COOLDOWN_SEC": 300,
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

CONFIG = load_config()

def get_conf(key):
    global CONFIG
    return CONFIG.get(key)

def refresh_config():
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

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})

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

def get_btc_price_now():
    try:
        resp = session.get(BINANCE_TICK, params={"symbol": BTC_SYMBOL}, timeout=1)
        return float(resp.json()["price"])
    except: return None

def get_btc_open_price(start_ts_ms):
    try:
        params = {"symbol": BTC_SYMBOL, "interval": "5m", "startTime": start_ts_ms, "limit": 1}
        resp = session.get(BINANCE_KLINES, params=params, timeout=1)
        return float(resp.json()[0][1])
    except: return None

def calc_up_probability(current_price, open_price, secs_left, vol):
    """Berechnet P(S_T > S_0) via Normalverteilung CDF."""
    if secs_left <= 0: return 1.0 if current_price > open_price else 0.0

    sigma_min = vol / math.sqrt(MINUTES_PER_YEAR)
    sigma_T = sigma_min * math.sqrt(secs_left / 60.0)

    if sigma_T == 0: return 1.0 if current_price > open_price else 0.0

    d = math.log(current_price / open_price) / sigma_T
    return 0.5 * (1.0 + math.erf(d / math.sqrt(2)))

def calc_kelly_stake(edge, price, budget, fraction):
    if price <= 0 or price >= 1 or edge <= 0: return 0.0
    odds_minus_one = (1.0 / price) - 1.0
    if odds_minus_one <= 0: return 0.0
    stake_fraction = fraction * (edge / odds_minus_one)
    stake_fraction = min(stake_fraction, 0.25)
    return round(stake_fraction * budget, 2)

# =============================================================
#  POLYMARKET API
# =============================================================

def find_market_by_slug(slug):
    try:
        resp = session.get(f"{GAMMA_API}?slug={slug}", timeout=2)
        data = resp.json()
        return data[0] if data else None
    except Exception as e:
        print(f" \n⚠️ Gamma API Fehler: {e}")
        return None

def get_market_prices(client, yes_id, no_id, slug):
    """Holt Preise über den CLOB-Preis-Endpunkt (schneller)."""
    y_price, n_price = None, None

    try:
        def fetch_p(tid):
            url = f"{POLY_HOST}/price"
            r = session.get(url, params={"token_id": tid, "side": "buy"}, timeout=1)
            if r.status_code == 200:
                return float(r.json().get("price"))
            return None

        y_price = fetch_p(yes_id)
        n_price = fetch_p(no_id)
    except:
        pass

    if (y_price is None or n_price is None) or (y_price + n_price > 1.10):
        try:
            resp = session.get(f"{GAMMA_API}?slug={slug}", timeout=1)
            data = resp.json()
            if data and len(data) > 0:
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
            writer.writerow(["timestamp", "market", "direction", "action", "price", "edge",
                             "stake", "probability", "profit_usdc", "modus", "target_price", "order_status"])

def log_trade_event(market, side, action, price, edge, stake, p_win, profit, dry_mode,
                    target_price=0, order_status="FILLED"):
    log_file = get_log_file()
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
            f.seek(0, os.SEEK_END)

        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "market", "direction", "action", "price", "edge",
                             "stake", "probability", "profit_usdc", "modus", "target_price", "order_status"])

        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            market, side, action,
            round(price, 4) if price else 0,
            round(edge, 4) if edge else 0,
            stake,
            round(p_win, 4) if p_win else 0,
            round(profit, 4),
            "DRY" if dry_mode else "LIVE",
            round(target_price, 4) if target_price else 0,
            order_status
        ])

# =============================================================
#  LIVE EXECUTION & SECURITY
# =============================================================

IS_GEOBLOCKED = False
LAST_GEO_CHECK = 0

def get_token_balance(wallet, token_address):
    """Scannt Guthaben eines ERC20 Tokens via RPC."""
    rpc_url = "https://polygon-bor-rpc.publicnode.com"
    try:
        data = "0x70a08231" + wallet[2:].lower().rjust(64, '0')
        payload = {"jsonrpc": "2.0", "method": "eth_call",
                   "params": [{"to": token_address, "data": data}, "latest"], "id": 1}
        resp = requests.post(rpc_url, json=payload, timeout=5).json()
        if 'result' in resp:
            return round(int(resp['result'], 16) / 10**6, 2)
    except: pass
    return 0.0

def check_geoblock():
    global IS_GEOBLOCKED, LAST_GEO_CHECK
    IS_GEOBLOCKED = False
    LAST_GEO_CHECK = time.time()
    return False

def get_live_client(cfg):
    if cfg.get("DRY_RUN", True):
        return ClobClient(host=POLY_HOST)

    try:
        pk = cfg.get("POLY_PRIVATE_KEY")
        funder_addr = "0xAC0D49bEf9C3B97F64193C0DA507848EF9e64D49"

        temp_client = ClobClient(host=POLY_HOST, key=pk, chain_id=POLYGON)
        creds = temp_client.create_or_derive_api_creds()

        client = ClobClient(
            host=POLY_HOST,
            key=pk,
            chain_id=POLYGON,
            creds=creds,
            signature_type=1,
            funder=funder_addr
        )
        return client
    except Exception as e:
        print(f" ❌ KRITISCHER INITIALISIERUNGSFEHLER: {e}")
        return ClobClient(host=POLY_HOST)

def place_limit_order(client, token_id, amount_usdc, limit_price, side="buy"):
    """
    Strict Limit Order.
    BUY:  Exakt bei limit_price – niemals darüber (kein Slippage).
    SELL: Exakt bei limit_price (TP/SL-Zielpreis).
    Returns: order_id (str) on success, None on failure.
    """
    global LAST_ORDER_TS

    if IS_GEOBLOCKED:
        print("\n\033[91m ❌ LIVE-ORDER ABGEBROCHEN: Geo-Blocking!\033[0m")
        return None

    now = time.time()
    if now - LAST_ORDER_TS < 5:
        time.sleep(5 - (now - LAST_ORDER_TS))

    try:
        order_side = BUY if side == "buy" else SELL
        price = round(limit_price, 4)
        token_qty = round(amount_usdc / limit_price, 2)

        print(f" 📡 Sende LIMIT {side.upper()} Order: {token_qty} Tokens @ {price*100:.1f}¢")

        order_args = OrderArgs(
            price=price,
            size=token_qty,
            side=order_side,
            token_id=token_id
        )

        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order)

        LAST_ORDER_TS = time.time()

        if resp and isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("id")
            if order_id:
                print(f" ✅ Limit Order platziert! ID: {order_id}")
                return order_id
            print(f" ❌ API Ablehnung (keine Order-ID): {resp}")
        else:
            print(f" ❌ API Ablehnung: {resp}")
        return None

    except Exception as e:
        print(f" ❌ Order-Fehler: {e}")
        return None

def check_order_fill(client, order_id):
    """
    Prüft den Füll-Status einer Order.
    Returns: "FILLED" | "LIVE" | "CANCELLED" | "UNKNOWN"
    """
    try:
        order = client.get_order(order_id)
        if not order:
            return "UNKNOWN"
        status = str(order.get("status", "")).upper()
        size_matched = float(order.get("size_matched", 0) or 0)
        original_size = float(order.get("original_size", 1) or 1)

        if status == "MATCHED" or size_matched >= original_size * 0.99:
            return "FILLED"
        if status in ("CANCELLED", "CANCELED"):
            return "CANCELLED"
        return "LIVE"
    except:
        return "UNKNOWN"

def cancel_order_safe(client, order_id):
    """Storniert eine Order ohne Exception."""
    try:
        client.cancel_order(order_id)
        return True
    except:
        return False

# =============================================================
#  MAIN LOOP
# =============================================================

def main():
    cfg_initial = load_config()
    init_csv()

    client = get_live_client(cfg_initial)

    if not cfg_initial.get("DRY_RUN", True):
        try:
            priv_key = cfg_initial.get("POLY_PRIVATE_KEY")
            account = Account.from_key(priv_key)
            eoa_address = account.address

            try:
                proxy_address = client.get_address()
            except:
                proxy_address = cfg_initial.get("POLY_PROXY_ADDRESS", "None")

            USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
            USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

            usdc_n_eoa = get_token_balance(eoa_address, USDC_NATIVE)
            usdc_e_eoa = get_token_balance(eoa_address, USDC_E)

            usdc_n_proxy = 0.0
            usdc_e_proxy = 0.0
            if proxy_address != "N/A":
                usdc_n_proxy = get_token_balance(proxy_address, USDC_NATIVE)
                usdc_e_proxy = get_token_balance(proxy_address, USDC_E)

            pol_balance = "N/A"
            try:
                rpc_url = "https://polygon-bor-rpc.publicnode.com"
                rpc_payload = {"jsonrpc": "2.0", "method": "eth_getBalance",
                               "params": [eoa_address, "latest"], "id": 1}
                rpc_resp = requests.post(rpc_url, json=rpc_payload, timeout=5).json()
                wei = int(rpc_resp['result'], 16)
                pol_balance = f"{wei / 10**18:.4f}"
            except: pass

            print("\n" + "─"*65)
            print(f"📡 EOA (Signier-Key):    \033[1m{eoa_address}\033[0m")
            print(f"🏢 PROXY (Handels-Acc):  \033[1m{proxy_address}\033[0m")
            print(f"⛽ POL-GAS (EOA):        \033[93m\033[1m{pol_balance} POL\033[0m")
            print("─" * 65)
            print(f"💰 USDC (NATUR)  | EOA: {usdc_n_eoa} | PROXY: \033[92m\033[1m{usdc_n_proxy} USDC\033[0m")
            print(f"💰 USDC.e (BRIDGED) | EOA: {usdc_e_eoa} | PROXY: \033[92m\033[1m{usdc_e_proxy} USDC.e\033[0m")
            print("─" * 65)

            sig_type = 1
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
    print(f" 🚀 POLYMARKET BTC VALUE-BOT (Limit-Order-Modus)")
    print("="*65 + "\033[0m")

    # Start-Cleanup: Alle offenen Orders löschen
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
    slug = ""

    # active_pos-Struktur:
    # {
    #   'side': 'UP'|'DOWN',
    #   'entry_price': float,
    #   'stake': float,
    #   'p_win': float,
    #   'sell_order_id': str|None,    # Laufende Sell-Order
    #   'sell_order_ts': float|None,  # Zeitstempel der Sell-Order
    #   'sell_order_action': str|None,# SELL_TP, SELL_SL, SELL_LIMIT, SELL_FORCE
    #   'sell_price': float|None      # Gesetzter Zielpreis
    # }
    active_pos = None
    available_balance = load_config().get("TOTAL_BUDGET", 100.0)

    while True:
        cfg = load_config()
        poll_int = 1
        dry_run = cfg.get("DRY_RUN", True)

        if not dry_run:
            if ('last_dry_mode' in locals() and last_dry_mode == True) or (time.time() - LAST_GEO_CHECK > 600):
                check_geoblock()
        else:
            IS_GEOBLOCKED = False

        if 'last_dry_mode' in locals() and last_dry_mode != dry_run:
            print(f"\n 🔄 MODUS-WECHSEL ERKANNT: {'DRY' if dry_run else 'LIVE'}")
            client = get_live_client(cfg)
            init_csv()
        last_dry_mode = dry_run

        total_budget = cfg.get("TOTAL_BUDGET", 100.0)
        min_edge = cfg.get("MIN_EDGE", 0.08)
        activate_sec = 280
        tp_pct = cfg.get("TAKE_PROFIT_PCT", 0.04)
        sl_pct = cfg.get("STOP_LOSS_PCT", 0.05)
        cooldown = cfg.get("COOLDOWN_SEC", 300)   # 300s = 1 Intervall Cooldown
        last_sale = cfg.get("LAST_SALE_TS", 0)

        block_thresh = cfg.get("BLOCK_THRESHOLD", 0.85)
        exit_thresh = cfg.get("EXIT_THRESHOLD", 0.90)

        # Cooldown: gesperrt für 300s nach jedem Verkauf
        trade_done = (time.time() - last_sale) < cooldown

        invested = active_pos['stake'] if active_pos else 0
        equity = available_balance + invested
        now_ts = int(time.time())
        interval_start = (now_ts // 300) * 300
        interval_end = interval_start + 300
        secs_left = interval_end - now_ts

        # ── NEUES INTERVALL ─────────────────────────────────────────
        if interval_start != last_interval:
            last_interval = interval_start

            # Offene Sell-Order des alten Intervalls stornieren
            if active_pos and active_pos.get('sell_order_id') and not dry_run:
                print(f"\n ⚠️ Intervall-Wechsel: Storniere alte Sell-Order...")
                cancel_order_safe(client, active_pos['sell_order_id'])
                active_pos['sell_order_id'] = None
                active_pos['sell_order_ts'] = None

            slug = f"btc-updown-5m-{interval_start}"
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ⏳ Neues Intervall | Suche Slug: {slug}")

            update_web_status({
                "slug": slug,
                "secs_left": secs_left,
                "market": "Suche Markt..."
            }, reset_history=True)

            market_data = None
            market_info = None
            for _ in range(5):
                market_data = find_market_by_slug(slug)
                if market_data: break
                time.sleep(2)

            if market_data:
                def parse_field(field):
                    if isinstance(field, str):
                        try: return json.loads(field)
                        except: return []
                    return field or []

                ids = parse_field(market_data.get("clobTokenIds"))
                outcomes = parse_field(market_data.get("outcomes"))

                if len(ids) >= 2:
                    try:
                        yes_idx = next((i for i, o in enumerate(outcomes) if o.lower() in ["up", "yes"]), 0)
                        no_idx = next((i for i, o in enumerate(outcomes) if o.lower() in ["down", "no"]), 1)
                    except:
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
            else:
                print(" ❌ Markt nicht gefunden.")

        if not market_info:
            time.sleep(poll_int)
            continue

        # ── MARKTANALYSE ────────────────────────────────────────────
        btc_now = get_btc_price_now()
        btc_start = get_btc_open_price(interval_start * 1000)

        if not btc_now or not btc_start:
            time.sleep(poll_int)
            continue

        # Vol = 1.2 (konservativ gedämpft, verhindert Phantom-Edges)
        prob_up = calc_up_probability(btc_now, btc_start, secs_left, 1.2)
        prob_down = 1.0 - prob_up

        price_yes, price_no = get_market_prices(client, market_info["yes_id"], market_info["no_id"], slug)

        edge_yes = (prob_up - price_yes) if price_yes is not None else 0
        edge_no = (prob_down - price_no) if price_no is not None else 0

        if not dry_run:
            try:
                sig_type = getattr(client.builder, "sig_type", 0)
                resp = client.get_balance_allowance(BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=sig_type))
                available_balance = float(resp.get("balance", "0")) / 10**6
            except:
                pass

        invested = active_pos['stake'] if active_pos else 0
        equity = available_balance + invested

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

        # ── EXIT LOGIK ──────────────────────────────────────────────
        if active_pos:
            curr_p = price_yes if active_pos['side'] == 'UP' else price_no
            # Sicherheits-Puffer: 10s vor Rundenende erzwinge Sell
            force_end = secs_left <= 10

            # ── FALL A: Laufende Sell-Order überwachen (nur Live) ───
            if active_pos.get('sell_order_id') and not dry_run:
                elapsed = time.time() - active_pos['sell_order_ts']
                fill_status = check_order_fill(client, active_pos['sell_order_id'])

                if fill_status == "FILLED":
                    sell_p = active_pos['sell_price']
                    change_pct = (sell_p - active_pos['entry_price']) / active_pos['entry_price']
                    profit_usdc = active_pos['stake'] * change_pct
                    available_balance += (active_pos['stake'] + profit_usdc)
                    action = active_pos['sell_order_action']

                    color = "\033[92m" if change_pct >= 0 else "\033[91m"
                    print(f"\n{color}✅ SELL GEFÜLLT ({action}): {sell_p*100:.1f}¢ ({change_pct*100:+.1f}%) | Profit: {profit_usdc:+.2f} USDC\033[0m")
                    log_trade_event(market_info["question"], active_pos['side'], action,
                                    sell_p, 0, active_pos['stake'], 0, profit_usdc, dry_run,
                                    order_status="FILLED")
                    cfg["LAST_SALE_TS"] = time.time()
                    save_config(cfg)
                    active_pos = None
                    time.sleep(poll_int)
                    continue

                elif elapsed > 10 or force_end:
                    # TTL abgelaufen oder Interval-Ende → Order stornieren
                    cancel_order_safe(client, active_pos['sell_order_id'])
                    log_reason = "EXPIRED" if force_end else "CANCELLED"
                    print(f"\n ⚠️ Sell-Order {log_reason} (nach {elapsed:.0f}s). Storniert.")

                    if force_end:
                        # Notfall-SELL_FORCE: 8% unter Marktpreis für garantierte Füllung
                        sell_p = round(max((curr_p or active_pos['entry_price']) * 0.92, 0.01), 4)
                        token_id = market_info["yes_id"] if active_pos['side'] == 'UP' else market_info["no_id"]
                        place_limit_order(client, token_id, active_pos['stake'], sell_p, side="sell")

                        change_pct = (sell_p - active_pos['entry_price']) / active_pos['entry_price']
                        profit_usdc = active_pos['stake'] * change_pct
                        available_balance += (active_pos['stake'] + profit_usdc)
                        log_trade_event(market_info["question"], active_pos['side'], "SELL_FORCE",
                                        sell_p, 0, active_pos['stake'], 0, profit_usdc, dry_run,
                                        order_status="EXPIRED")
                        cfg["LAST_SALE_TS"] = time.time()
                        save_config(cfg)
                        active_pos = None
                    else:
                        # TTL-Ablauf ohne Rundenende: Sell-Tracking zurücksetzen, neu evaluieren
                        active_pos['sell_order_id'] = None
                        active_pos['sell_order_ts'] = None
                        active_pos['sell_order_action'] = None
                        active_pos['sell_price'] = None

                    time.sleep(poll_int)
                    continue

                else:
                    # Order noch aktiv, warte weiter
                    time.sleep(poll_int)
                    continue

            # ── FALL B: Neue Sell-Entscheidung treffen ──────────────
            is_limit_reached = False
            if curr_p is not None:
                p_val = prob_up if active_pos['side'] == 'UP' else prob_down
                if p_val >= exit_thresh or p_val <= (1.0 - exit_thresh) \
                        or curr_p >= exit_thresh or curr_p <= (1.0 - exit_thresh):
                    is_limit_reached = True

            if curr_p is not None or force_end:
                if curr_p is None: curr_p = 0
                change_pct = (curr_p - active_pos['entry_price']) / active_pos['entry_price']

                is_tp = change_pct >= tp_pct
                is_sl = change_pct <= -sl_pct

                if is_tp or is_sl or force_end or is_limit_reached:
                    if is_limit_reached: action = "SELL_LIMIT"
                    elif is_tp:          action = "SELL_TP"
                    elif is_sl:          action = "SELL_SL"
                    else:                action = "SELL_FORCE"

                    color = "\033[92m" if change_pct >= 0 else "\033[91m"
                    tag = ("🎯 TAKE PROFIT" if is_tp else
                           "🛑 STOP LOSS" if is_sl else
                           "⚠️ LIMIT REACHED" if is_limit_reached else
                           "⏰ RUNDEN-ENDE")
                    print(f"\n{color}{tag}: Initiiere {action} bei {curr_p*100:.1f}¢ ({change_pct*100:+.1f}%)\033[0m")

                    if dry_run:
                        # Dry-Run: sofortiger Abschluss
                        profit_usdc = active_pos['stake'] * change_pct
                        available_balance += (active_pos['stake'] + profit_usdc)
                        log_trade_event(market_info["question"], active_pos['side'], action,
                                        curr_p, 0, active_pos['stake'], 0, profit_usdc, dry_run,
                                        order_status="FILLED")
                        cfg["LAST_SALE_TS"] = time.time()
                        save_config(cfg)
                        active_pos = None
                    else:
                        # Live: Limit-Order setzen
                        # SELL_FORCE: 8% unter Markt für zuverlässige Füllung
                        if action == "SELL_FORCE":
                            target_p = round(max(curr_p * 0.92, 0.01), 4)
                        else:
                            target_p = round(curr_p, 4)

                        token_id = market_info["yes_id"] if active_pos['side'] == 'UP' else market_info["no_id"]
                        order_id = place_limit_order(client, token_id, active_pos['stake'], target_p, side="sell")

                        if order_id:
                            active_pos['sell_order_id'] = order_id
                            active_pos['sell_order_ts'] = time.time()
                            active_pos['sell_order_action'] = action
                            active_pos['sell_price'] = target_p
                            print(f" ⏳ Sell-Order platziert (TTL: 10s). Warte auf Füllung...")
                        else:
                            # Placement fehlgeschlagen → Verlust buchen
                            profit_usdc = active_pos['stake'] * change_pct
                            available_balance += (active_pos['stake'] + profit_usdc)
                            log_trade_event(market_info["question"], active_pos['side'], action,
                                            curr_p, 0, active_pos['stake'], 0, profit_usdc, dry_run,
                                            order_status="CANCELLED")
                            cfg["LAST_SALE_TS"] = time.time()
                            save_config(cfg)
                            active_pos = None

        # ── TRADE VORBEREITUNG ──────────────────────────────────────
        if not active_pos and edge_yes > edge_no and edge_yes >= min_edge:
            t_side, b_edge, t_p, t_price = "UP", edge_yes, prob_up, price_yes
        elif not active_pos and edge_no >= min_edge:
            t_side, b_edge, t_p, t_price = "DOWN", edge_no, prob_down, price_no
        else:
            t_side, b_edge, t_p, t_price = None, 0, 0, None

        # ── ENTRY LOGIK ─────────────────────────────────────────────
        if not trade_done and not active_pos and t_side and t_price and t_p > 0 and secs_left <= activate_sec:
            is_blocked = (t_p >= block_thresh or t_p <= (1.0 - block_thresh)
                          or t_price >= block_thresh or t_price <= (1.0 - block_thresh))

            if not is_blocked:
                stake = cfg.get("STAKE_USD", 1.0)
                min_reserve = cfg.get("MIN_RESERVE_USD", 0.0)

                if stake > 0 and (available_balance - stake) >= min_reserve:
                    print(f"\n\033[1m\033[94mTRADE AUSGELÖST: Kaufe {t_side} bei {t_price*100:.1f}¢ | Edge {b_edge*100:.2f}%\033[0m")
                    t_target = t_price * (1 + tp_pct)
                    order_success = False

                    if dry_run:
                        order_success = True
                        log_trade_event(market_info["question"], t_side, "BUY",
                                        t_price, b_edge, stake, t_p, 0, dry_run,
                                        target_price=t_target, order_status="FILLED")
                    else:
                        # Live: Strict Limit Buy + 10s TTL
                        token_id = market_info["yes_id"] if t_side == "UP" else market_info["no_id"]
                        order_id = place_limit_order(client, token_id, stake, t_price, side="buy")

                        if order_id:
                            print(f" ⏳ Warte auf Füllung der Buy-Order (max 10s)...")
                            deadline = time.time() + 10
                            fill_status = "PENDING"
                            while time.time() < deadline:
                                fill_status = check_order_fill(client, order_id)
                                if fill_status == "FILLED":
                                    break
                                time.sleep(0.5)

                            if fill_status != "FILLED":
                                cancel_order_safe(client, order_id)
                                fill_status = "CANCELLED"
                                print(f" ⚠️ Buy-Order nach 10s nicht gefüllt. Storniert.")

                            log_trade_event(market_info["question"], t_side, "BUY",
                                            t_price, b_edge, stake, t_p, 0, dry_run,
                                            target_price=t_target, order_status=fill_status)

                            if fill_status == "FILLED":
                                order_success = True
                        else:
                            log_trade_event(market_info["question"], t_side, "BUY",
                                            t_price, b_edge, stake, t_p, 0, dry_run,
                                            target_price=t_target, order_status="CANCELLED")

                    if order_success:
                        active_pos = {
                            'side': t_side,
                            'entry_price': t_price,
                            'stake': stake,
                            'p_win': t_p,
                            'sell_order_id': None,
                            'sell_order_ts': None,
                            'sell_order_action': None,
                            'sell_price': None
                        }
                        available_balance -= stake

        time.sleep(poll_int)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n ⏹  Bot gestoppt.")
