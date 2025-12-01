import flet as ft
import requests
import pyotp
import threading
import time
import uuid
import datetime
import traceback
import json
import os
import queue
import tempfile
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

# Use a writable path for the cache file on mobile/desktop
SCRIPMASTER_FILE = os.path.join(tempfile.gettempdir(), "scripmaster.json")

# --- GLOBAL JOB QUEUE FOR RATE LIMITING ---
# Angel One Limit: ~3 req/sec. We use 1 req / 1.5 sec to be extremely safe.
api_job_queue = queue.Queue()

def api_worker_thread():
    """Background thread that processes API requests one by one."""
    while True:
        try:
            task = api_job_queue.get()
            if task is None: break 
            
            func, args, page_ref = task
            func(*args)
            
            if page_ref:
                try: page_ref.update()
                except: pass
                
        except Exception as e:
            print(f"Worker Error: {e}")
        finally:
            if 'task' in locals() and task is not None:
                api_job_queue.task_done()
            time.sleep(1.5) 

threading.Thread(target=api_worker_thread, daemon=True).start()


class AppState:
    def __init__(self):
        self.api_key = ""
        self.client_id = ""
        self.jwt_token = None
        self.feed_token = None
        self.refresh_token = None
        self.smart_api = None
        self.sws = None
        
        self.current_view = "watchlist"
        self.scrips = []
        self.live_feed_status = "DISCONNECTED"
        self.master_loaded = False
        
        self.watchlist = [
            {"symbol": "RELIANCE-EQ", "token": "2885", "exch_seg": "NSE", "ltp": 0.0, "wc": 0.0, "loading": False},
            {"symbol": "SBIN-EQ", "token": "3045", "exch_seg": "NSE", "ltp": 0.0, "wc": 0.0, "loading": False},
        ]
        
        self.alerts = []
        self.logs = []
        self.is_paused = False
        self.connected = False

state = AppState()

# --- HELPERS ---

def load_scrips(page=None):
    def _background_load():
        try:
            # Check if file exists and is fresh (less than 24h old)
            is_fresh = False
            if os.path.exists(SCRIPMASTER_FILE):
                try:
                    if (time.time() - os.path.getmtime(SCRIPMASTER_FILE) < 86400):
                        is_fresh = True
                except: pass

            if not is_fresh:
                print("Downloading Scrip Master...")
                r = requests.get("https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json")
                data = r.json()
                state.scrips = [s for s in data if s.get('exch_seg') == 'NSE' and '-EQ' in s.get('symbol', '')]
                with open(SCRIPMASTER_FILE, 'w') as f: json.dump(state.scrips, f)
            else:
                print("Loading Scrips from cache...")
                with open(SCRIPMASTER_FILE, 'r') as f: state.scrips = json.load(f)
            
            state.master_loaded = True
            print(f"Master Data Ready: {len(state.scrips)} symbols loaded.")
            if page:
                try: page.update() 
                except: pass
        except Exception as e:
            print(f"Failed to load scrips: {e}")

    threading.Thread(target=_background_load, daemon=True).start()

def angel_login(api_key, client_id, password, totp_secret):
    try:
        smartApi = SmartConnect(api_key=api_key)
        totp_val = pyotp.TOTP(totp_secret).now()
        data = smartApi.generateSession(client_id, password, totp_val)
        
        if data.get('status'):
            state.smart_api = smartApi
            state.jwt_token = data['data']['jwtToken']
            state.feed_token = data['data']['feedToken']
            state.refresh_token = data['data']['refreshToken']
            return True, "Login Successful"
        else:
            return False, data.get('message', 'Unknown Login Error')
    except Exception as e:
        return False, str(e)

# --- DATA FETCHING (Queued) ---
def fetch_historical_data_task(stock_item):
    if not state.smart_api: return
    
    try:
        stock_item['loading'] = True
        print(f"DEBUG: Starting fetch for {stock_item['symbol']}")
        
        # 1. Fetch LTP
        try:
            ltp_data = state.smart_api.ltpData("NSE", stock_item['symbol'], stock_item['token'])
            if ltp_data and ltp_data.get('status'):
                stock_item['ltp'] = ltp_data['data']['ltp']
                print(f"DEBUG: LTP for {stock_item['symbol']} is {stock_item['ltp']}")
            else:
                print(f"DEBUG: LTP Fetch Failed for {stock_item['symbol']}: {ltp_data}")
        except Exception as e:
            print(f"DEBUG: LTP Exception for {stock_item['symbol']}: {e}")

        # 2. Fetch History (With fallback and strict dates)
        try:
            # Fix: Use current time for todate to avoid future date errors
            now = datetime.datetime.now()
            to_date_str = now.strftime('%Y-%m-%d %H:%M')
            from_date_str = (now - datetime.timedelta(days=90)).strftime('%Y-%m-%d 09:15')
            
            historic_req = {
                "exchange": "NSE",
                "symboltoken": stock_item['token'],
                "interval": "ONE_WEEK",
                "fromdate": from_date_str,
                "todate": to_date_str
            }
            
            print(f"DEBUG: Requesting Weekly Candle for {stock_item['symbol']}: {historic_req}")
            
            # Nested try-except to handle ONE_WEEK failure specifically
            try:
                hist_data = state.smart_api.getCandleData(historic_req)
                print(f"DEBUG: Weekly Candle Response: {hist_data}")
                
                if hist_data and hist_data.get('status') and hist_data.get('data'):
                    candles = hist_data['data']
                    if len(candles) > 0:
                        stock_item['wc'] = candles[-1][4] # Close price
                        print(f"DEBUG: Weekly Close for {stock_item['symbol']} set to {stock_item['wc']}")
                    else:
                        raise Exception("Empty data list")
                else:
                    raise Exception("Invalid status or no data")

            except Exception as e:
                print(f"DEBUG: ONE_WEEK failed ({e}). Falling back to ONE_DAY...")
                historic_req['interval'] = "ONE_DAY"
                # Retry with ONE_DAY
                hist_data = state.smart_api.getCandleData(historic_req)
                print(f"DEBUG: Daily Candle Response: {hist_data}")
                
                if hist_data and hist_data.get('status') and hist_data.get('data'):
                    stock_item['wc'] = hist_data['data'][-1][4]
                    print(f"DEBUG: Daily Close (Fallback) for {stock_item['symbol']} set to {stock_item['wc']}")
                else:
                    print(f"DEBUG: ONE_DAY also failed or empty.")

        except Exception as e:
            print(f"Candle Error {stock_item['symbol']}: {e}")
            traceback.print_exc()

    except Exception as e:
        print(f"Task Failed for {stock_item['symbol']}: {e}")
    finally:
        stock_item['loading'] = False
        print(f"DEBUG: Finished fetch for {stock_item['symbol']}")

def refresh_all_data(page):
    """Queues updates for all stocks."""
    for stock in state.watchlist:
        stock['loading'] = True
        api_job_queue.put((fetch_historical_data_task, [stock], page))
    try: page.update()
    except: pass

# --- WEBSOCKET ---
def start_websocket(page):
    if not all([state.jwt_token, state.api_key, state.client_id, state.feed_token]): return

    state.sws = SmartWebSocketV2(state.jwt_token, state.api_key, state.client_id, state.feed_token)

    def on_data(wsapp, message):
        if isinstance(message, list):
            for tick in message:
                try:
                    token = tick.get('tk')
                    ltp_paise = tick.get('lp') 
                    if token and ltp_paise:
                        real_price = float(ltp_paise) / 100.0
                        for stock in state.watchlist:
                            if str(stock['token']) == str(token):
                                stock['ltp'] = real_price
                                check_alerts(stock, page)
                                break
                except: pass
        if page and state.current_view == "watchlist":
             try: page.update()
             except: pass

    def on_open(wsapp):
        state.live_feed_status = "CONNECTED"
        token_list = [item['token'] for item in state.watchlist]
        state.sws.subscribe("watchlist", 1, [{"exchangeType": 1, "tokens": token_list}])
        if page:
            try: page.update() 
            except: pass

    def on_close(wsapp, code, reason):
        state.live_feed_status = "DISCONNECTED"
        if page:
            try: page.update() 
            except: pass

    state.sws.on_data = on_data
    state.sws.on_open = on_open
    state.sws.on_close = on_close
    threading.Thread(target=state.sws.connect, daemon=True).start()

# --- ALERT LOGIC ---
def check_alerts(stock, page):
    if state.is_paused: return
    triggered_something = False
    for alert in list(state.alerts):
        if str(alert["token"]) == str(stock["token"]):
            triggered = False
            if alert["condition"] == "ABOVE" and stock["ltp"] >= alert["price"]: triggered = True
            elif alert["condition"] == "BELOW" and stock["ltp"] <= alert["price"]: triggered = True
            
            if triggered:
                msg = f"{stock['symbol']} hit {alert['price']} ({alert['condition']})"
                state.logs.insert(0, {"time": datetime.datetime.now().strftime("%H:%M:%S"), "symbol": stock['symbol'], "msg": msg})
                if alert in state.alerts: state.alerts.remove(alert)
                triggered_something = True
    
    if triggered_something and page and state.current_view in ["alerts", "logs"]:
        try: page.update()
        except: pass

def generate_369_levels(ltp, weekly_close):
    if weekly_close <= 0: return []
    levels = []
    pattern = [30, 60, 90] if weekly_close > 3333 else [3, 6, 9]
    # Resistance
    curr = weekly_close
    for i in range(10):
        step = pattern[i % 3]
        curr += step
        if curr > ltp: levels.append({"price": round(curr, 2), "type": "ABOVE"})
    # Support
    curr = weekly_close
    for i in range(10):
        step = pattern[i % 3]
        curr -= step
        if curr < ltp: levels.append({"price": round(curr, 2), "type": "BELOW"})
    return levels

# --- UI MAIN ---
def main(page: ft.Page):
    page.title = "Trade Yantra"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0
    page.window_width = 400
    page.window_height = 800
    
    # Load config from client_storage
    api_key = page.client_storage.get("api_key") or ""
    client_id = page.client_storage.get("client_id") or ""
    
    load_scrips(page)

    api_input = ft.TextField(label="SmartAPI Key", password=True, value=api_key)
    client_input = ft.TextField(label="Client ID", value=client_id)
    pass_input = ft.TextField(label="Password", password=True)
    totp_input = ft.TextField(label="TOTP Secret", password=True)
    login_status = ft.Text("", color="red")
    login_progress = ft.ProgressRing(visible=False)

    def handle_login(e):
        login_progress.visible = True
        page.update()
        success, msg = angel_login(api_input.value, client_input.value, pass_input.value, totp_input.value)
        if success:
            # Save config to client_storage
            page.client_storage.set("api_key", api_input.value)
            page.client_storage.set("client_id", client_input.value)
            
            state.connected = True
            start_websocket(page)
            refresh_all_data(page)
            page.go("/app")
        else:
            login_status.value = f"Error: {msg}"
        login_progress.visible = False
        page.update()

    login_view = ft.Column([
        ft.Text("Trade Yantra", size=30, weight="bold", color="blue"), ft.Divider(),
        api_input, client_input, pass_input, totp_input,
        ft.ElevatedButton("Login", on_click=handle_login),
        ft.Row([login_progress, login_status])
    ], alignment="center", horizontal_alignment="center", spacing=20)

    body_container = ft.Container(expand=True, padding=10)

    def get_watchlist_view():
        controls = []
        status_color = "green" if state.live_feed_status == "CONNECTED" else "red"
        
        search_icon = "search"
        search_disabled = False
        search_tooltip = "Search"
        if not state.master_loaded:
            search_icon = "hourglass_empty"
            search_disabled = True
            search_tooltip = "Loading Master Data..."

        controls.append(ft.Row([
            ft.Text(f"Feed: {state.live_feed_status}", color=status_color, size=12),
            ft.IconButton(search_icon, on_click=open_search_bs, tooltip=search_tooltip, disabled=search_disabled),
            ft.IconButton("refresh", on_click=lambda e: refresh_all_data(page), tooltip="Fetch Data")
        ], alignment="spaceBetween"))
        controls.append(ft.Divider())
        
        lv = ft.ListView(expand=True, spacing=10)
        for stock in state.watchlist:
            price_txt = f"₹{stock['ltp']:.2f}"
            wc_txt = f"WC: {stock['wc']:.2f}"
            price_content = ft.ProgressRing(width=16, height=16) if stock['loading'] else ft.Column([ft.Text(price_txt, weight="bold", color="green"), ft.Text(wc_txt, size=10, color="grey")], alignment="end")
            lv.controls.append(ft.Container(content=ft.Row([ft.Column([ft.Text(stock['symbol'], weight="bold"), ft.Text(stock['token'], size=10, color="grey")]), ft.Row([price_content, ft.IconButton("delete", icon_color="red", on_click=lambda e, t=stock['token']: remove_stock(t))])], alignment="spaceBetween"), padding=10, border=ft.border.only(bottom=ft.BorderSide(1, "#333333"))))
        controls.append(lv)
        return ft.Column(controls, expand=True)

    def remove_stock(token):
        state.watchlist = [s for s in state.watchlist if s['token'] != token]
        update_view()

    def get_alerts_view():
        controls = []
        controls.append(ft.Container(content=ft.Column([ft.Text("Strategy: 3-6-9 Logic"), ft.ElevatedButton("Auto-Generate Levels", on_click=generate_alerts_ui), ft.Switch(label="Pause Monitoring", value=state.is_paused, on_change=toggle_pause)]), padding=10, bgcolor="#222222", border_radius=10))
        controls.append(ft.Divider())
        lv = ft.ListView(expand=True, spacing=5)
        if not state.alerts: lv.controls.append(ft.Text("No active alerts", color="grey"))
        for alert in state.alerts:
            col = "green" if alert['condition'] == "ABOVE" else "red"
            icon = "trending_up" if col=="green" else "trending_down"
            lv.controls.append(ft.Container(content=ft.Row([ft.Row([ft.Icon(icon, color=col), ft.Column([ft.Text(alert['symbol'], weight="bold"), ft.Text(f"Target: {alert['price']}")])]), ft.IconButton("delete", icon_color="red", on_click=lambda e, uid=alert['id']: delete_alert(uid))], alignment="spaceBetween"), padding=10, bgcolor="#222222", border_radius=5))
        controls.append(lv)
        return ft.Column(controls, expand=True)

    def generate_alerts_ui(e):
        count = 0
        for stock in state.watchlist:
            if stock['wc'] > 0:
                lvls = generate_369_levels(stock['ltp'], stock['wc'])
                for l in lvls:
                    if not any(a['token'] == stock['token'] and a['price'] == l['price'] for a in state.alerts):
                        state.alerts.append({"id": str(uuid.uuid4()), "symbol": stock['symbol'], "token": stock['token'], "price": l['price'], "condition": l['type']})
                        count += 1
        if count > 0: state.logs.insert(0, {"time": "SYS", "symbol": "AUTO", "msg": f"Generated {count} alerts"})
        update_view()

    def delete_alert(uid):
        state.alerts = [a for a in state.alerts if a['id'] != uid]
        update_view()

    def toggle_pause(e): state.is_paused = e.control.value
        
    def get_logs_view():
        lv = ft.ListView(expand=True)
        for log in state.logs: lv.controls.append(ft.Container(content=ft.Row([ft.Text(log['time'], size=10, color="grey"), ft.Text(log['symbol'], weight="bold", width=80), ft.Text(log['msg'], expand=True)]), padding=5, border=ft.border.only(bottom=ft.BorderSide(1, "#333333"))))
        return ft.Column([ft.Text("Activity Log", size=20), ft.Divider(), lv], expand=True)

    def open_search_bs(e):
        if not state.master_loaded:
            page.snack_bar = ft.SnackBar(ft.Text("Master Data Loading... Please wait."), bgcolor="orange")
            page.snack_bar.open = True
            page.update()
            return
        search_field = ft.TextField(label="Symbol (e.g. RELI)", autofocus=True, on_change=lambda e: run_search(e.data))
        results_list = ft.ListView(expand=True)
        def run_search(q):
            q = q.upper()
            results_list.controls.clear()
            if len(q) > 2:
                matches = [s for s in state.scrips if s['symbol'].startswith(q)][:15]
                for m in matches:
                    results_list.controls.append(ft.ListTile(title=ft.Text(m['symbol']), subtitle=ft.Text(m.get('name', '')), on_click=lambda e, item=m: add_stock(item)))
            bs.update()
        def add_stock(item):
            if not any(s['token'] == item['token'] for s in state.watchlist):
                new_stock = {"symbol": item['symbol'], "token": item['token'], "exch_seg": item['exch_seg'], "ltp": 0.0, "wc": 0.0, "loading": True}
                state.watchlist.append(new_stock)
                api_job_queue.put((fetch_historical_data_task, [new_stock], page))
                if state.sws: state.sws.subscribe("add", 1, [{"exchangeType": 1, "tokens": [item['token']]}])
            page.close(bs)
            update_view()
        bs = ft.BottomSheet(ft.Container(ft.Column([search_field, results_list], expand=True), padding=20, height=500), open=True)
        page.overlay.append(bs)
        page.update()

    def update_view():
        if state.current_view == "watchlist": body_container.content = get_watchlist_view()
        elif state.current_view == "alerts": body_container.content = get_alerts_view()
        elif state.current_view == "logs": body_container.content = get_logs_view()
        try: page.update()
        except: pass

    def nav_change(e):
        state.current_view = e.control.data
        update_view()

    nav_row = ft.Row([ft.IconButton("list", data="watchlist", on_click=nav_change), ft.IconButton("notifications", data="alerts", on_click=nav_change), ft.IconButton("history", data="logs", on_click=nav_change)], alignment="spaceAround")

    def route_change(e):
        page.views.clear()
        page.views.append(ft.View("/", [ft.Container(content=login_view, alignment=ft.alignment.center, expand=True)]))
        if page.route == "/app":
            page.views.append(ft.View("/app", [ft.AppBar(title=ft.Text("Trade Yantra"), bgcolor="#222222"), body_container, ft.Container(content=nav_row, bgcolor="#222222", padding=10)]))
            update_view()
        page.update()

    page.on_route_change = route_change
    page.go(page.route)

if __name__ == "__main__":
    ft.app(target=main)
