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
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

CONFIG_FILE = "config.json"
SCRIPMASTER_FILE = "scripmaster.json"

# --- GLOBAL JOB QUEUE ---
api_job_queue = queue.Queue()

def api_worker_thread():
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
            time.sleep(2.0) 

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
        
        self.watchlist = []
        self.alerts = []
        self.logs = []
        self.is_paused = False
        self.connected = False

state = AppState()

# --- HELPERS ---
def load_config():
    default = {"api_key": "", "client_id": "", "watchlist": []}
    if not os.path.exists(CONFIG_FILE): return default
    try:
        with open(CONFIG_FILE, 'r') as f:
            data = json.load(f)
            if "watchlist" not in data: data["watchlist"] = []
            return data
    except: return default

def save_config():
    data = {
        "api_key": state.api_key,
        "client_id": state.client_id,
        "watchlist": state.watchlist
    }
    try:
        with open(CONFIG_FILE, 'w') as f: json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Error saving config: {e}")

def load_scrips(page=None):
    def _background_load():
        try:
            if not os.path.exists(SCRIPMASTER_FILE) or (time.time() - os.path.getmtime(SCRIPMASTER_FILE) > 86400):
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

# --- SMART DATA FETCHING ---
def smart_candle_fetch(req):
    """Retries candle fetching with backoff."""
    retries = 3
    for i in range(retries):
        try:
            return state.smart_api.getCandleData(req)
        except Exception as e:
            err_msg = str(e)
            if "Couldn't parse" in err_msg or "timed out" in err_msg or "Max retries" in err_msg:
                wait = 4 * (i + 1)
                print(f"  Retry {i+1}/{retries} in {wait}s due to error...")
                time.sleep(wait)
                continue
            raise e 
    return None

def fetch_historical_data_task(stock_item):
    if not state.smart_api: return
    
    try:
        stock_item['loading'] = True
        
        # 1. Fetch LTP (Fast check)
        try:
            ltp_data = state.smart_api.ltpData("NSE", stock_item['symbol'], stock_item['token'])
            if ltp_data and ltp_data.get('status'):
                stock_item['ltp'] = ltp_data['data']['ltp']
        except: pass

        # 2. Fetch History (Previous Week Close Logic)
        try:
            # We fetch DAILY data for the last 45 days.
            # We then find the last candle that happened BEFORE the start of the current week (Monday).
            
            to_date_str = datetime.datetime.now().strftime('%Y-%m-%d 15:30')
            from_date_str = (datetime.datetime.now() - datetime.timedelta(days=45)).strftime('%Y-%m-%d 09:15')
            
            historic_req = {
                "exchange": "NSE",
                "symboltoken": stock_item['token'],
                "interval": "ONE_DAY",
                "fromdate": from_date_str,
                "todate": to_date_str
            }
            
            hist_data = smart_candle_fetch(historic_req)
            
            if hist_data and hist_data.get('status') and hist_data.get('data'):
                candles = hist_data['data']
                
                # Calculate the date of the Monday of the current week
                today = datetime.date.today()
                current_week_monday = today - datetime.timedelta(days=today.weekday())
                
                found_prev_week_close = False
                
                # Iterate backwards to find the first candle strictly BEFORE this Monday
                for c in reversed(candles):
                    # Candle date string format from API: "2023-10-27T00:00:00+05:30"
                    try:
                        c_date_str = c[0].split('T')[0]
                        c_date = datetime.datetime.strptime(c_date_str, "%Y-%m-%d").date()
                        
                        if c_date < current_week_monday:
                            stock_item['wc'] = c[4] # Close price
                            found_prev_week_close = True
                            print(f"Found Prev Week Close for {stock_item['symbol']} (Date: {c_date}): {stock_item['wc']}")
                            break
                    except: continue
                
                if not found_prev_week_close:
                    print(f"Could not find Previous Week candle for {stock_item['symbol']}")

        except Exception as e:
            print(f"Candle Error {stock_item['symbol']}: {e}")

    except Exception as e:
        print(f"Task Failed for {stock_item['symbol']}: {e}")
    finally:
        stock_item['loading'] = False
        save_config()

def refresh_all_data(page):
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
        if token_list:
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
    
    config = load_config()
    state.api_key = config.get("api_key", "")
    state.client_id = config.get("client_id", "")
    state.watchlist = config.get("watchlist", [])
    
    load_scrips(page)

    api_input = ft.TextField(label="SmartAPI Key", password=True, value=state.api_key)
    client_input = ft.TextField(label="Client ID", value=state.client_id)
    pass_input = ft.TextField(label="Password", password=True)
    totp_input = ft.TextField(label="TOTP Secret", password=True)
    login_status = ft.Text("", color="red")
    login_progress = ft.ProgressRing(visible=False)

    def handle_login(e):
        login_progress.visible = True
        page.update()
        
        success, msg = angel_login(api_input.value, client_input.value, pass_input.value, totp_input.value)
        if success:
            state.api_key = api_input.value
            state.client_id = client_input.value
            save_config()
            
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
            price_txt = f"₹{stock.get('ltp', 0):.2f}"
            wc_txt = f"WC: {stock.get('wc', 0):.2f}"
            
            if stock.get('loading'):
                price_content = ft.ProgressRing(width=16, height=16)
            else:
                price_content = ft.Column([
                    ft.Text(price_txt, weight="bold", color="green"),
                    ft.Text(wc_txt, size=10, color="grey")
                ], alignment="end")

            lv.controls.append(
                ft.Container(
                    content=ft.Row([
                        ft.Column([
                            ft.Text(stock['symbol'], weight="bold"),
                            ft.Text(stock['token'], size=10, color="grey")
                        ]),
                        ft.Row([
                            price_content,
                            ft.IconButton("delete", icon_color="red", 
                                          on_click=lambda e, t=stock['token']: remove_stock(t))
                        ])
                    ], alignment="spaceBetween"),
                    padding=10, border=ft.border.only(bottom=ft.BorderSide(1, "#333333"))
                )
            )
        controls.append(lv)
        return ft.Column(controls, expand=True)

    def remove_stock(token):
        state.watchlist = [s for s in state.watchlist if s['token'] != token]
        save_config()
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
            if stock.get('wc', 0) > 0:
                lvls = generate_369_levels(stock.get('ltp', 0), stock['wc'])
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
                new_stock = {
                    "symbol": item['symbol'],
                    "token": item['token'],
                    "exch_seg": item['exch_seg'],
                    "ltp": 0.0,
                    "wc": 0.0,
                    "loading": True
                }
                state.watchlist.append(new_stock)
                save_config()
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

    nav_row = ft.Row([
        ft.IconButton("list", data="watchlist", on_click=nav_change),
        ft.IconButton("notifications", data="alerts", on_click=nav_change),
        ft.IconButton("history", data="logs", on_click=nav_change),
    ], alignment="spaceAround")

    def route_change(e):
        page.views.clear()
        page.views.append(
            ft.View(
                "/",
                [ft.Container(content=login_view, alignment=ft.alignment.center, expand=True)]
            )
        )
        if page.route == "/app":
            page.views.append(
                ft.View(
                    "/app",
                    [
                        ft.AppBar(title=ft.Text("Trade Yantra"), bgcolor="#222222"),
                        body_container,
                        ft.Container(content=nav_row, bgcolor="#222222", padding=10)
                    ]
                )
            )
            update_view()
        page.update()

    page.on_route_change = route_change
    page.go(page.route)

if __name__ == "__main__":
    ft.app(target=main)
