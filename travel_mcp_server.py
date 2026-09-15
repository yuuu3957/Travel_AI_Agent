from fastmcp import FastMCP
import requests
import os
from dotenv import load_dotenv
from datetime import datetime
import json
from typing import List, Dict
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import pickle
import pytz



mcp = FastMCP("travel-mcp-server")


load_dotenv()
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")
RAPID_API_HOST = os.getenv("RAPID_API_HOST")
RAPID_API_KEY = os.getenv("RAPID_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
NAVITIME_RAPIDAPI_KEY = os.getenv("NAVITIME_RAPIDAPI_KEY")

@mcp.tool(
    name="get_climate_context",
    description="Get historical climate context based on previous years for travel planning"
)
def get_climate_context(
    city: str,
    latitude: float,
    longitude: float,
    month: int,
    years_back: int = 3
):
    """
    Returns historical climate ranges for a given month
    using data from previous years.
    """

    from datetime import date

    today = date.today()
    year = today.year - 1  # 永遠用「已完整發生的一年」

    start_year = year - years_back + 1

    max_temps = []
    min_temps = []

    for y in range(start_year, year + 1):
        start_date = f"{y}-{month:02d}-01"
        end_date = f"{y}-{month:02d}-28"  # 28 天夠用，避免月長問題

        params = {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": start_date,
            "end_date": end_date,
            "daily": "temperature_2m_max,temperature_2m_min",
            "timezone": "auto"
        }

        response = requests.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params=params,
            timeout=10
        )
        response.raise_for_status()
        data = response.json()

        if "daily" not in data:
            continue

        max_temps.extend(data["daily"]["temperature_2m_max"])
        min_temps.extend(data["daily"]["temperature_2m_min"])

    if not max_temps or not min_temps:
        return {
            "location": city,
            "available": False,
            "reason": "No sufficient historical climate data"
        }

    return {
        "location": city,
        "type": "historical_climate",
        "period": {
            "month": month,
            "years": f"{start_year}–{year}"
        },
        "raw": {
            "max_temperature_range": [
                round(min(max_temps), 1),
                round(max(max_temps), 1)
            ],
            "min_temperature_range": [
                round(min(min_temps), 1),
                round(max(min_temps), 1)
            ],
            "avg_max_temperature": round(sum(max_temps) / len(max_temps), 1),
            "avg_min_temperature": round(sum(min_temps) / len(min_temps), 1)
        }
    }


def _get_exchange_rate_internal(base_currency: str, target_currency: str):
    """
    Internal exchange-rate function (callable by other server functions).
    Uses USD as pivot currency (Open Exchange Rates free plan limitation).
    """
    app_id = os.getenv("OPEN_EXCHANGE_RATES_APP_ID")
    if not app_id:
        return {"success": False, "reason": "missing_api_key"}

    base_currency = base_currency.upper().strip()
    target_currency = target_currency.upper().strip()

    url = "https://openexchangerates.org/api/latest.json"
    params = {
        "app_id": app_id,
        "symbols": f"{base_currency},{target_currency}"
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        return {"success": False, "reason": "request_failed", "error": str(e)}

    rates = data.get("rates")
    if not isinstance(rates, dict):
        return {"success": False, "reason": "rates_missing", "raw_response": data}

    if base_currency not in rates or target_currency not in rates:
        return {
            "success": False,
            "reason": "currency_missing",
            "available": list(rates.keys())
        }

    usd_to_base = rates[base_currency]
    usd_to_target = rates[target_currency]
    exchange_rate = usd_to_target / usd_to_base

    return {
        "success": True,
        "base_currency": base_currency,
        "target_currency": target_currency,
        "exchange_rate": exchange_rate,
        "source": "openexchangerates.org",
        "retrieved_at": data.get("timestamp")
    }


@mcp.tool(
    name="get_exchange_rate",
    description="""
    Retrieve the current exchange rate between two currencies.
    Data source: Open Exchange Rates.
    This tool provides RAW financial data only.
    Interpretation and decision-making must be handled by the agent.
    """
)
def get_exchange_rate(base_currency: str, target_currency: str):
    return _get_exchange_rate_internal(base_currency, target_currency)



SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

def get_calendar_service():
    creds = None

    if os.path.exists("token.json"):
        with open("token.json", "rb") as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open("token.json", "wb") as token:
            pickle.dump(creds, token)

    service = build("calendar", "v3", credentials=creds)
    return service

LOCAL_TZ = pytz.timezone("Asia/Taipei")

def get_event_details(
    service,
    start_date: str,
    end_date: str
) -> List[Dict]:
    """
    Fetch calendar events with summaries within date range.
    Returned times are localized to Asia/Taipei.
    """

    time_min = LOCAL_TZ.localize(
        datetime.strptime(start_date, "%Y-%m-%d")
    ).isoformat()

    time_max = LOCAL_TZ.localize(
        datetime.strptime(end_date, "%Y-%m-%d")
    ).replace(hour=23, minute=59, second=59).isoformat()

    events_result = service.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
        timeZone="Asia/Taipei",
    ).execute()

    results = []
    for event in events_result.get("items", []):
        results.append({
            "summary": event.get("summary", "（無標題事件）"),
            "start": event["start"].get("dateTime", event["start"].get("date")),
            "end": event["end"].get("dateTime", event["end"].get("date")),
        })

    return results

@mcp.tool(
    name="get_calendar_availability",
    description="""
    Check Google Calendar availability within a given date range.
    This tool is a HARD FEASIBILITY GATE for travel planning.

    - Time zone: Asia/Taipei
    - Returns busy slots and event summaries

    Parameters:
    - start_date: YYYY-MM-DD
    - end_date: YYYY-MM-DD

    Returns:
    - busy: true / false
    - busy_slots: list of busy time ranges (local time)
    - events: list of event summaries with start/end
    """
)
def get_calendar_availability(start_date: str, end_date: str):
    service = get_calendar_service()

    # Build localized RFC3339 timestamps
    time_min = LOCAL_TZ.localize(
        datetime.strptime(start_date, "%Y-%m-%d")
    ).isoformat()

    time_max = LOCAL_TZ.localize(
        datetime.strptime(end_date, "%Y-%m-%d")
    ).replace(hour=23, minute=59, second=59).isoformat()

    # ---- Free/Busy check (feasibility) ----
    body = {
        "timeMin": time_min,
        "timeMax": time_max,
        "timeZone": "Asia/Taipei",
        "items": [{"id": "primary"}]
    }

    fb_result = service.freebusy().query(body=body).execute()
    calendars = fb_result.get("calendars", {})
    primary = calendars.get("primary", {})
    busy_slots = primary.get("busy", [])

    # ---- Detailed events (for explanation) ----
    events = get_event_details(service, start_date, end_date)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "timezone": "Asia/Taipei",
        "busy": len(busy_slots) > 0,
        "busy_slots": busy_slots,
        "events": events
    }



def get_amadeus_token():
    url= "https://test.api.amadeus.com/v1/security/oauth2/token"
    payload ={
        "grant_type": "client_credentials",
        "client_id": AMADEUS_CLIENT_ID,
        "client_secret": AMADEUS_CLIENT_SECRET
    }
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status()
        return response.json()["access_token"]
    
    except:
        return None
    
@mcp.tool(
    name="get_flights",
    description="""
    查詢指定航線的航班資訊，適用於尚未確認航班安排的旅遊規劃情境。

    功能：
    - 查詢單程航班資訊
    - 回傳航班時間、航空公司與價格等細節

    參數：
    - origin：出發機場 IATA 代碼（例如 TPE、KIX）
    - destination：抵達機場 IATA 代碼（例如 HND、NRT）
    - date：出發日期（YYYY-MM-DD）
    - preferred_airlines（可選）：偏好航空公司 IATA 代碼，可指定多個，多個的格式為:AA,BB,CC

    回傳內容：
    - 航班清單（航空公司、出發時間、抵達時間、價格、幣別）
"""
)
def get_flights(origin: str, destination: str, date: str, preferred_airlines: str = ""):

    token = get_amadeus_token()
    if not token:
        return {
            "error": "Failed to retrieve Amadeus token. Check your API key / secret."
        }

    url = "https://test.api.amadeus.com/v2/shopping/flight-offers"
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "originLocationCode": origin,
        "destinationLocationCode": destination,
        "departureDate": date,
        "adults": 1,
        "max": 20,
        "currencyCode": "TWD"
    }
    if preferred_airlines:
        params["includedAirlineCodes"] = preferred_airlines 

    try:
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()

        data = resp.json()

        flights = []
        for offer in data.get("data", []):
            price = offer["price"]["total"]
            currency = offer["price"]["currency"]

            segment = offer["itineraries"][0]["segments"][0]
            airline = segment["carrierCode"]
            departure_time = segment["departure"]["at"]
            arrival_time = segment["arrival"]["at"]


            flights.append({
                "airline": airline,
                "price": price,
                "currency": currency,
                "departure_time": departure_time,
                "arrival_time":arrival_time
            })

        return {
            "origin": origin,
            "destination": destination,
            "date": date,
            "flights": flights
        }

    except Exception as e:
        return {
            "error": f"Flight search failed: {str(e)}"
        }
    
@mcp.tool(
    name="get_hotels",
    description="""
    查詢指定城市的飯店資訊。依城市名稱搜尋可訂房的住宿選項。

    - 若 Booking API 回傳的幣別與使用者指定幣別不同，
      會自動透過 get_exchange_rate 進行轉換。
    - 價格轉換失敗時，保留原始幣別顯示（不亂轉）。

    參數：
    - city_name：城市名稱（例如 Tokyo、Osaka、Taipei）
    - checkin_date：入住日期（YYYY-MM-DD）
    - checkout_date：退房日期（YYYY-MM-DD）
    - adults：入住人數
    - currency：顯示價格的目標幣別（例如 TWD）
    - min_rating（可選）：最低評分
    - rooms（可選）：所需房間數
    """
)
def get_hotels(
    city_name: str,
    checkin_date: str,
    checkout_date: str,
    adults: int,
    currency: str = "TWD",
    min_rating: float = 0,
    rooms: int = 1
):
    headers = {
        "x-rapidapi-host": RAPID_API_HOST,
        "x-rapidapi-key": RAPID_API_KEY
    }

    # ---------------------------
    # Step 1: 城市搜尋
    # ---------------------------
    search_url = f"https://{RAPID_API_HOST}/locations/auto-complete"
    search_params = {
        "text": city_name,
        "languagecode": "zh-tw"
    }

    try:
        search_resp = requests.get(
            search_url,
            headers=headers,
            params=search_params,
            timeout=15
        )
        search_resp.raise_for_status()
        search_data = search_resp.json()

        if not search_data:
            return {"error": f"找不到城市: {city_name}"}

        first_result = search_data[0]
        dest_id = first_result.get("dest_id")
        dest_type = first_result.get("dest_type", "city")

        if not dest_id:
            return {"error": f"無法取得城市 ID: {city_name}"}

    except Exception as e:
        return {"error": f"搜尋城市失敗: {str(e)}"}

    # ---------------------------
    # Step 2: 查詢飯店
    # ---------------------------
    hotels_url = f"https://{RAPID_API_HOST}/properties/list"
    hotels_params = {
        "dest_ids": dest_id,
        "dest_type": dest_type,
        "arrival_date": checkin_date,
        "departure_date": checkout_date,
        "adults_number": adults,
        "room_number": rooms,
        "search_type": dest_type,
        "order_by": "popularity",
        "filter_by_currency": currency,  # best effort
        "locale": "zh-tw",
        "units": "metric",
        "page_number": 0,
        "include_adjacency": "true"
    }

    try:
        hotels_resp = requests.get(
            hotels_url,
            headers=headers,
            params=hotels_params,
            timeout=30
        )
        hotels_resp.raise_for_status()
        hotels_data = hotels_resp.json()

        if not hotels_data.get("result"):
            return {"error": "該城市找不到飯店"}

        # ---------------------------
        # Step 3: 計算住宿晚數
        # ---------------------------
        nights = (
            datetime.strptime(checkout_date, "%Y-%m-%d") -
            datetime.strptime(checkin_date, "%Y-%m-%d")
        ).days

        # 匯率快取（避免重複 API 呼叫）
        exchange_cache = {}

        hotels = []

        # ---------------------------
        # Step 4: 處理每一間飯店
        # ---------------------------
        for hotel in hotels_data["result"][:20]:
            price_breakdown = hotel.get("price_breakdown", {})
            price_value = (
                price_breakdown.get("gross_price")
                or hotel.get("min_total_price")
            )

            price_currency = (
                price_breakdown.get("gross_price_currency")
                or hotel.get("currencycode")
            )

            if price_value in (None, ""):
                continue

            try:
                gross_price = float(price_value)
            except (ValueError, TypeError):
                continue

            if gross_price <= 0:
                continue

            # 評分處理
            try:
                review_score = float(hotel.get("review_score") or 0)
            except (ValueError, TypeError):
                review_score = 0.0

            if review_score < min_rating:
                continue

            # ---------------------------
            # Step 5: 幣值轉換（若需要）
            # ---------------------------
            display_price = gross_price
            display_currency = price_currency

            if price_currency and price_currency != currency:
                cache_key = (price_currency, currency)

                if cache_key not in exchange_cache:
                    rate_result = _get_exchange_rate_internal(
                        base_currency=price_currency,
                        target_currency=currency
                    )
                    if rate_result.get("success"):
                        exchange_cache[cache_key] = rate_result["exchange_rate"]
                    else:
                        exchange_cache[cache_key] = None

                rate = exchange_cache.get(cache_key)
                
                if rate:
                    display_price = round(gross_price * rate, 2)
                    display_currency = currency

            # 每晚價格
            price_per_night = (
                round(display_price / nights, 2)
                if nights > 0 else display_price
            )

            hotels.append({
                "hotel_id": hotel.get("hotel_id"),
                "name": hotel.get("hotel_name", "Unknown Hotel"),
                "type": hotel.get("accommodation_type_name", "Hotel"),
                "star_rating": hotel.get("class", "N/A"),
                "review_score": review_score,
                "review_score_word": hotel.get("review_score_word", ""),
                "review_count": hotel.get("review_nr", 0),
                "address": hotel.get("address", "地址未提供"),
                "city": hotel.get("city", ""),
                "distance_from_center": f"{hotel.get('distance', 'N/A')} km",

                # 原始價格（debug / trace 用）
                "original_total_price": gross_price,
                "original_currency": price_currency,

                # 顯示價格（可能已轉換）
                "total_price": display_price,
                "price_per_night": price_per_night,
                "currency": display_currency,
                "price_display": (
                    f"{display_currency} {price_per_night:,.0f} / 晚 "
                    f"(總計: {display_currency} {display_price:,.0f})"
                ),

                "is_free_cancellable": hotel.get("is_free_cancellable", False),
                "url": hotel.get("url", "")
            })

        return {
            "hotels": hotels,
            "total_found": len(hotels),
            "location": city_name,
            "dest_id": dest_id,
            "checkin": checkin_date,
            "checkout": checkout_date,
            "nights": nights,
            "adults": adults,
            "rooms": rooms,
            "requested_currency": currency,
            "note": "Prices are converted when necessary using Open Exchange Rates"
        }

    except Exception as e:
        return {"error": f"飯店查詢失敗: {str(e)}"}

    
@mcp.tool(
    name="get_attractions",
    description="""
        搜尋指定城市的熱門景點。
        當使用者詢問旅遊資訊時，請務必使用此工具規劃行程。

        參數：
        - city：城市名稱（例如 Tokyo、Osaka、Taipei）
        - limit（可選）：最多回傳景點數量

        回傳：
        - 景點列表，包含名稱、地址、評分及類型等資訊。
        """
)
def get_attractions(city: str, limit: int = 30):
    if not GOOGLE_API_KEY:
        return {"error": "GOOGLE_PLACES_API_KEY 尚未設定於系統環境變數中。"}
    geo_url = "https://maps.googleapis.com/maps/api/geocode/json"
    geo_params = {
        "address": city,
        "key": GOOGLE_API_KEY
    }

    geo_res = requests.get(geo_url, params=geo_params).json()

    if not geo_res.get("results"):
        return {"error": f"查無此城市：{city}"}

    location = geo_res["results"][0]["geometry"]["location"]
    lat, lng = location["lat"], location["lng"]

    places_url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    places_params = {
        "location": f"{lat},{lng}",
        "radius": 5000,                
        "type": "tourist_attraction",  
        "key": GOOGLE_API_KEY
    }

    places_res = requests.get(places_url, params=places_params).json()

    if "results" not in places_res:
        return {"error": "Google Places 回傳格式異常", "raw": places_res}

    results = places_res["results"][:limit]

    attractions = []
    for p in results:
        attractions.append({
            "name": p.get("name", "未知名稱"),
            "address": p.get("vicinity", "無地址資訊"),
            "rating": p.get("rating", None),
            "types": p.get("types", [])
        })

    return attractions if attractions else {"message": "查無景點資料"}


@mcp.tool(
    name="get_route",
    description="""
        查詢兩地之間的交通路線。支援開車、步行、騎車與大眾運輸。
        若詢問日本以外的交通方式，請使用此工具，可搭配景點工具使用。

        參數：
        - origin：出發地（地名或地址）
        - destination：目的地
        - mode：移動方式（driving／walking／bicycling／transit）

        回傳：
        - 路線摘要（時間與距離）
        - 逐步導航說明。
        """
)

def get_route(origin: str, destination: str, mode: str = "transit"):
    try:
        url = "https://maps.googleapis.com/maps/api/directions/json"
        params = {
            "origin": origin,
            "destination": destination,
            "mode": mode,
            "language": "zh-TW",
            "key": GOOGLE_API_KEY
        }

        if mode == "transit":
            params["departure_time"] = "now"


        response = requests.get(url, params=params)
        data = response.json()

        if data.get("status") != "OK":
            return {"error": f"Google Directions API 錯誤: {data.get('status')}"}

        route = data["routes"][0]
        leg = route["legs"][0]

        distance = leg["distance"]["text"]
        duration = leg["duration"]["text"]

        steps = []
        for step in leg["steps"]:
            instruction = step["html_instructions"]
            instruction = instruction.replace("<b>", "").replace("</b>", "")
            instruction = instruction.replace("<div style=\"font-size:0.9em\">", " ").replace("</div>", "")
            steps.append(instruction)

        return {
            "from": origin,
            "to": destination,
            "mode": mode,
            "distance": distance,
            "duration": duration,
            "steps": steps
        }

    except Exception as e:
        return {"error": str(e)}



# --- 日本地址轉經緯度（使用 Google Geocode） ---
def jp_geocode(address: str):
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "address": address,
        "region": "jp",           # 優先解析日本地點
        "components": "country:JP",  # 強制只搜尋日本
        "language": "ja",         # 回傳日文地名，有利後續 NAVITIME 解析
        "key": GOOGLE_API_KEY
    }

    resp = requests.get(url, params=params).json()

    if resp.get("status") != "OK" or not resp.get("results"):
        return None

    location = resp["results"][0]["geometry"]["location"]
    return location["lat"], location["lng"]

@mcp.tool(
    name="get_route_jp",
    description="""
        查詢日本境內的大眾交通路線。支援電車、地鐵、私鐵、公車與步行路段。
        若詢問日本的交通方式，請使用此工具，可搭配景點工具使用。

        參數：
        - origin：日本境內的出發地（地名或車站名）
        - destination：日本境內的目的地

        回傳：
        - 路線摘要（所需時間與距離）
        - 逐段交通方式與導覽說明。
        """
)

def get_route_jp(origin: str, destination: str):

    # Google Geocode
    ori = jp_geocode(origin)
    dest = jp_geocode(destination)

    if not ori:
        return {"error": f"解析起點失敗：{origin}"}
    if not dest:
        return {"error": f"解析終點失敗：{destination}"}

    ori_lat, ori_lng = ori
    dest_lat, dest_lng = dest

    # ✔ 必填：start_time (ISO 8601)
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # ✔ 正確 endpoint
    url = "https://navitime-route-totalnavi.p.rapidapi.com/route_transit"

    # ✔ 完全比對官方 curl 的參數格式
    params = {
        "start": f"{ori_lat},{ori_lng}",       # lat,lng
        "goal": f"{dest_lat},{dest_lng}",     # lat,lng
        "datum": "wgs84",
        "coord_unit": "degree",
        "term": 1440,                         # 與 curl 相同
        "limit": 5,                           # 與 curl 相同
        "start_time": now,
    }

    headers = {
        "X-RapidAPI-Key": NAVITIME_RAPIDAPI_KEY,
        "X-RapidAPI-Host": "navitime-route-totalnavi.p.rapidapi.com"
    }

    resp = requests.get(url, headers=headers, params=params)
    data = resp.json()

    if "items" not in data:
        return {"error": "NAVITIME 無法找到路線", "raw": data}

    route = data["items"][0]
    summary = route["summary"]

    steps = []
    for sec in route.get("sections", []):
        transport = sec.get("transport", {}).get("name", None)

        # 若沒有 transport（例如步行）
        if not transport:
            if sec.get("type") == "walk":
                transport = "徒歩"
            else:
                transport = "移動"

        guide = sec.get("guide", "")
        steps.append(f"[{transport}] {guide}")

    return {
        "from": origin,
        "to": destination,
        "duration_minutes": summary.get("move_time"),
        "distance_meters": summary.get("move_distance"),
        "steps": steps,
        "raw": data  # 可選：方便 debug
    }


DATA_DIR = "./data"
os.makedirs(DATA_DIR, exist_ok=True)

TRIPS_PATH = os.path.join(DATA_DIR, "trips.json")
PREFERENCES_PATH = os.path.join(DATA_DIR, "preferences.json")

def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def _save_json(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def normalize_destination(dest: str) -> str:
    dest = dest.lower().strip()
    mapping = {
        "switzerland": "switzerland",
        "zurich": "switzerland",
        "zrh": "switzerland",
        "瑞士": "switzerland",
        # 之後可慢慢補
    }
    return mapping.get(dest, dest)

def normalize_date(date_str: str) -> str:
    return date_str.replace("-", "")

def resolve_trip_id(destination: str, start_date: str, end_date: str) -> str:
    dest = normalize_destination(destination)
    start = normalize_date(start_date)
    end = normalize_date(end_date)
    return f"{dest}_{start}_{end}"

@mcp.tool(name="read_preferences", description="讀取使用者旅遊偏好")
def read_preferences():
    return _load_json(PREFERENCES_PATH)

@mcp.tool(
    name="update_preference",
    description="新增或更新一筆使用者旅遊偏好（name + preferences）"
)
def update_preference(
    preference_id: str,
    name: str,
    preferences: str
):
    data = _load_json(PREFERENCES_PATH)

    data[preference_id] = {
        "name": name,
        "preferences": preferences
    }

    _save_json(PREFERENCES_PATH, data)

    return {
        "message": "Preference updated",
        "preference_id": preference_id,
        "name": name,
        "preferences": preferences
    }



@mcp.tool(name="create_trip", description="建立新的旅遊行程")
def create_trip(
    destination: str,
    start_date: str,
    end_date: str,
    name: str
):
    trips = _load_json(TRIPS_PATH)

    trip_id = resolve_trip_id(destination, start_date, end_date)

    # 防止重複建立
    if trip_id in trips:
        return {
            "message": "Trip already exists",
            "trip_id": trip_id
        }

    trips[trip_id] = {
        "trip_id": trip_id,
        "destination": destination,
        "name": name,
        "start_date": start_date,
        "end_date": end_date,
        "created_at": datetime.utcnow().isoformat(),
        "flights": [],
        "hotels": [],
        "itinerary": {}
    }

    _save_json(TRIPS_PATH, trips)

    return {
        "message": "Trip created",
        "trip_id": trip_id
    }


@mcp.tool(name="read_trip", description="讀取單一旅遊行程")
def read_trip(trip_id: str):
    trips = _load_json(TRIPS_PATH)
    return trips.get(trip_id, {"error": "Trip not found"})

@mcp.tool(name="list_trips", description="列出所有旅遊行程")
def list_trips():
    trips = _load_json(TRIPS_PATH)
    return {"trips": list(trips.keys())}

@mcp.tool(name="delete_trip", description="刪除旅遊行程")
def delete_trip(trip_id: str):
    trips = _load_json(TRIPS_PATH)
    if trip_id not in trips:
        return {"error": "Trip not found"}
    trips.pop(trip_id)
    _save_json(TRIPS_PATH, trips)
    return {"message": "Trip deleted", "trip_id": trip_id}

@mcp.tool(name="add_flight_to_trip", description="新增航班至行程")
def add_flight_to_trip(
    destination: str,
    start_date: str,
    end_date: str,
    airline: str,
    departure: str,
    arrival: str,
    date: str,
    price: int
):
    trips = _load_json(TRIPS_PATH)
    trip_id = resolve_trip_id(destination, start_date, end_date)

    trip = trips.get(trip_id)
    if not trip:
        return {"error": "Trip not found"}

    trip["flights"].append({
        "airline": airline,
        "departure": departure,
        "arrival": arrival,
        "date": date,
        "price": price
    })

    _save_json(TRIPS_PATH, trips)
    return {"message": "Flight added", "trip_id": trip_id}


@mcp.tool(name="update_flight_in_trip", description="更新航班資訊（依 index）")
def update_flight_in_trip(
    destination: str,
    start_date: str,
    end_date: str,
    index: int,
    airline: str,
    departure: str,
    arrival: str,
    date: str,
    price: int
):
    trips = _load_json(TRIPS_PATH)
    trip_id = resolve_trip_id(destination, start_date, end_date)

    trip = trips.get(trip_id)
    if not trip or index >= len(trip["flights"]):
        return {"error": "Flight not found"}

    trip["flights"][index] = {
        "airline": airline,
        "departure": departure,
        "arrival": arrival,
        "date": date,
        "price": price
    }

    _save_json(TRIPS_PATH, trips)
    return {"message": "Flight updated", "trip_id": trip_id}


@mcp.tool(name="add_hotel_to_trip", description="新增飯店至行程")
def add_hotel_to_trip(
    destination: str,
    start_date: str,
    end_date: str,
    name: str,
    area: str,
    nights: int,
    price: int
):
    trips = _load_json(TRIPS_PATH)
    trip_id = resolve_trip_id(destination, start_date, end_date)

    trip = trips.get(trip_id)
    if not trip:
        return {"error": "Trip not found"}

    trip["hotels"].append({
        "name": name,
        "area": area,
        "nights": nights,
        "price": price
    })

    _save_json(TRIPS_PATH, trips)
    return {"message": "Hotel added", "trip_id": trip_id}


@mcp.tool(name="update_hotel_in_trip", description="更新飯店資訊（依 index）")
def update_hotel_in_trip(
    destination: str,
    start_date: str,
    end_date: str,
    index: int,
    name: str,
    area: str,
    nights: int,
    price: int
):
    trips = _load_json(TRIPS_PATH)
    trip_id = resolve_trip_id(destination, start_date, end_date)

    trip = trips.get(trip_id)
    if not trip or index >= len(trip["hotels"]):
        return {"error": "Hotel not found"}

    trip["hotels"][index] = {
        "name": name,
        "area": area,
        "nights": nights,
        "price": price
    }

    _save_json(TRIPS_PATH, trips)
    return {"message": "Hotel updated", "trip_id": trip_id}


@mcp.tool(name="set_itinerary", description="一次設定整個旅遊行程的每日行程")
def set_itinerary(
    destination: str,
    start_date: str,
    end_date: str,
    dates: List[str],
    activities: List[List[str]]
):
    trips = _load_json(TRIPS_PATH)
    trip_id = resolve_trip_id(destination, start_date, end_date)

    trip = trips.get(trip_id)
    if not trip:
        return {"error": "Trip not found"}

    itinerary = {}
    for d, acts in zip(dates, activities):
        itinerary[d] = acts

    trip["itinerary"] = itinerary
    _save_json(TRIPS_PATH, trips)

    return {"message": "Itinerary set", "days": len(itinerary), "trip_id": trip_id}


@mcp.tool(name="add_itinerary_day", description="新增每日行程")
def add_itinerary_day(
    destination: str,
    start_date: str,
    end_date: str,
    date: str,
    activities: List[str]
):
    trips = _load_json(TRIPS_PATH)
    trip_id = resolve_trip_id(destination, start_date, end_date)

    trip = trips.get(trip_id)
    if not trip:
        return {"error": "Trip not found"}

    trip["itinerary"][date] = activities
    _save_json(TRIPS_PATH, trips)
    return {"message": "Itinerary added", "date": date, "trip_id": trip_id}


@mcp.tool(name="update_itinerary_day", description="更新每日行程")
def update_itinerary_day(
    destination: str,
    start_date: str,
    end_date: str,
    date: str,
    activities: List[str]
):
    trips = _load_json(TRIPS_PATH)
    trip_id = resolve_trip_id(destination, start_date, end_date)

    trip = trips.get(trip_id)
    if not trip or date not in trip["itinerary"]:
        return {"error": "Itinerary not found"}

    trip["itinerary"][date] = activities
    _save_json(TRIPS_PATH, trips)
    return {"message": "Itinerary updated", "date": date, "trip_id": trip_id}


if __name__ == "__main__":
    mcp.run(port=8000, transport="http")




